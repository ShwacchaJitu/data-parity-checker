import random
import requests
import threading
import time
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone
from dateutil import parser
from global_data import *
from auth_manager import (
    AUTH_ERROR_STATUSES,
    AuthenticationError,
    get_user_entity,
    invalidate,
    new_session,
)

# Shared across all fetch_patient_metrics calls within a single script run.
# Prevents repeated heavy fetches (e.g. getAllUsers) when processing patients in parallel.
_shared_cache: Dict[tuple, Any] = {}
_shared_cache_lock = threading.Lock()


class ApiError(RuntimeError):
    """A request could not be completed successfully after retries."""


class _FetchFailed:
    """
    Marks a metric whose API call failed, so it is reported as FETCH_FAILED
    instead of being compared as though the dashboard held no value.

    It compares equal to everything on purpose: any comparison that slips
    through unguarded reports "no mismatch" rather than inventing one.
    """

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    def __bool__(self):
        return False

    def __hash__(self):
        return hash("FETCH_FAILED")

    def __repr__(self):
        return "FETCH_FAILED"

    def __str__(self):
        return "FETCH_FAILED"


FETCH_FAILED = _FetchFailed()


# Transient server-side conditions worth retrying.
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Sleeps double from the base: 6 attempts spans ~31s of backoff, enough to ride
# out the ~30s service blips seen on the vitals endpoints. Four attempts only bought 7s.
# ponytail: fixed budget, honour the Retry-After header if blips outlast it.
_MAX_ATTEMPTS = 6
_BACKOFF_BASE_SECS = 1.0

# Endpoints normally answer in well under 4s, so the default 15s timeout is a
# stall rather than a tight limit. Each retry still gets more room in case the
# server is briefly degraded, capped so one bad endpoint cannot hang a run.
_TIMEOUT_GROWTH = 1.5
_MAX_TIMEOUT_SECS = 45


def _is_rejected_token(response) -> bool:
    """
    Tell an expired/invalid token apart from a genuine authorization denial.

    The API answers both with 403, but they need opposite handling:
      {"message":"Invalid Token","name":"InvalidToken","code":1003}
          -> the token is stale; re-login and retry.
      {"statusCode":403,"message":"Forbidden"}
          -> this account may not see this patient; re-logging in changes
             nothing and just hammers the login endpoint.
    """
    if response.status_code == 401:
        return True
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and (
        body.get("code") == 1003 or body.get("name") == "InvalidToken"
    )


def _sent_token(response) -> Optional[str]:
    """The token this particular request actually carried."""
    header = (response.request.headers or {}).get("Authorization") or ""
    return header[7:] if header.startswith("Bearer ") else None


def api_request(session, method, url, *, profile, json=None, params=None, timeout=15, label=""):
    """
    Perform one API call, retrying auth failures and transient server errors.

    Returns the decoded JSON body. Raises ApiError if the call never succeeded,
    so a failed fetch is never silently reported as missing dashboard data.

    A 4xx that is not an auth or rate-limit error means "this patient has no
    such record" rather than "the call broke", so it yields {} as before.
    """
    delay = _BACKOFF_BASE_SECS
    last_error = "unknown error"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        attempt_timeout = min(
            timeout * (_TIMEOUT_GROWTH ** (attempt - 1)),
            max(timeout, _MAX_TIMEOUT_SECS),
        )
        try:
            response = session.request(method, url, json=json, params=params, timeout=attempt_timeout)
        except requests.exceptions.Timeout:
            last_error = f"read timeout after {attempt_timeout:.0f}s"
        except requests.exceptions.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.ok:
                try:
                    return response.json()
                except ValueError as exc:
                    # A 200 carrying an unparseable body is an overloaded gateway
                    # (HTML error page, truncated response), not "no data". Falling
                    # through to retry stops it becoming a silent blank comparison.
                    last_error = f"invalid JSON: {exc}"
            elif response.status_code in AUTH_ERROR_STATUSES and _is_rejected_token(response):
                # The token was rejected. Force a real re-login before retrying,
                # otherwise BearerAuth would replay the same rejected token.
                last_error = f"HTTP {response.status_code} (token rejected)"
                invalidate(profile, _sent_token(response))
            elif response.status_code == 403:
                # Authorized-user problem, not a token problem. Retrying and
                # re-logging in cannot help, so fail immediately and say why.
                raise ApiError(
                    f"{label or url}: HTTP 403 Forbidden — credential profile {profile} is not "
                    f"authorized for this patient. Check that the script's CSV_PATH and "
                    f"credential_profile match (see batch_parity_check.py)."
                )
            elif response.status_code in _RETRY_STATUSES or response.status_code >= 500:
                # Any 5xx is the server failing, not the patient lacking a record —
                # including CDN-specific ones (520-526) absent from _RETRY_STATUSES.
                last_error = f"HTTP {response.status_code}"
            else:
                return {}

        if attempt < _MAX_ATTEMPTS:
            print(f"↻ Retry {attempt}/{_MAX_ATTEMPTS - 1} for {label or url} ({last_error})")
            # Jittered: all workers hit the same endpoint at once, so a fixed
            # backoff retries them in lockstep and re-herds onto a struggling
            # service at exactly the same moment.
            time.sleep(delay * random.uniform(0.5, 1.5))
            delay *= 2

    raise ApiError(f"{label or url} failed after {_MAX_ATTEMPTS} attempts ({last_error})")


def get_latest_value(history: List[Dict[str, Any]], value_key: str = "value") -> Optional[Any]:
        for entry in reversed(history):
            if value_key in entry and entry[value_key] is not None and entry[value_key] != 0:
                return entry[value_key]
        return None

def get_latest_value_in_range(history, start_date_str, end_date_str, value_key="value"):
    if not start_date_str or not end_date_str:
        return None
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
    for entry in reversed(history):
        ts = entry.get("timestamp") or entry.get("date")
        if not ts:
            continue
        try:
            entry_dt = datetime.strptime(ts[:10], "%Y-%m-%d")
        except ValueError:
            continue
        if start_dt <= entry_dt <= end_dt and entry.get(value_key) is not None and entry[value_key] != 0:
            return entry[value_key]
    return None

def get_latest_value_before_end(history, end_date_str):
    if not end_date_str:
        return None
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
    valid_entries = [
        entry for entry in history
        if entry.get("value") is not None and datetime.strptime(entry["timestamp"], "%Y-%m-%d") <= end_date
    ]
    if valid_entries:
        latest_entry = max(valid_entries, key=lambda x: x["timestamp"])
        latest_date = datetime.strptime(latest_entry["timestamp"], "%Y-%m-%d")
        return latest_date.strftime("%d-%m-%Y")
    return None

def _latest_bp_entry(data, end_dt, record_types):
    """Latest BP entry of the given recordType(s), ignoring records after end_date."""
    entries = [
        e for e in (data or {}).get("data", [])
        if e.get("recordType") in record_types and e.get("date")
    ]
    if not entries:
        return None
    latest_entry = max(entries, key=lambda x: x.get("ts", 0))
    # Guardrail: Only return if entry date <= end_date
    if datetime.strptime(latest_entry["date"], "%Y-%m-%d") <= end_dt:
        return latest_entry
    return None


def fetch_latest_bp_records(patient_id, onboard_date, end_date, session, base_url, credential_profile="1"):
    """
    Walk back day by day from end_date to onboard_date and return the latest
    self-measured (manual/omron) and clinic (doctor) BP records as a pair.

    The endpoint takes a single `date` and returns that day's records when any
    exist, otherwise the patient's latest record overall — so walking is the only
    way to find the most recent record on or before end_date when a newer record
    exists outside the reporting window. Self and clinic previously walked the
    whole range separately, sending two identical requests per day; one walk
    serves both because every response carries all recordTypes. Same results,
    roughly half the requests, and it stops as soon as both are found.
    """
    onboard_dt = datetime.strptime(onboard_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    self_rec = None
    clinic_rec = None
    self_settled = False
    clinic_settled = False

    for d in range((end_dt - onboard_dt).days + 1):
        if (self_rec is not None or self_settled) and (clinic_rec is not None or clinic_settled):
            break

        check_date = (end_dt - timedelta(days=d)).strftime("%Y-%m-%d")
        data = api_request(
            session, "post", f"{base_url}/api/v1/labs/blood-pressure",
            profile=credential_profile,
            json={"patientId": patient_id, "type": "history", "date": check_date},
            label="blood_pressure_data",
        )

        if self_rec is None:
            self_rec = _latest_bp_entry(data, end_dt, ("manual", "omron"))
        if clinic_rec is None:
            clinic_rec = _latest_bp_entry(data, end_dt, ("doctor",))

        rows = data.get("data", []) or []
        if not rows:
            break  # the patient has no blood-pressure records at all

        if all(r.get("date") != check_date for r in rows):
            # No records on this date, so the response is the API's fallback:
            # the patient's latest record of each recordType. A type missing
            # here exists nowhere in the patient's history, so continuing to
            # walk back cannot turn it up. A type that IS present but was
            # rejected by the end_date guardrail still needs the walk, because
            # an older in-window record may exist.
            types_present = {r.get("recordType") for r in rows}
            if not {"manual", "omron"} & types_present:
                self_settled = True
            if "doctor" not in types_present:
                clinic_settled = True

    return self_rec, clinic_rec


def calculate_age(dob_str: str) -> Optional[int]:
    if not dob_str:
        return None
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
        end_date_obj = datetime.strptime(endDate, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None

    if dob > end_date_obj:
        return None

    years = end_date_obj.year - dob.year

    if end_date_obj.month < 5: # subtract 1 if current month < May (month < 5)
        years -= 1

    return years


def _mark_fetch_failed(result, fetch_failures, cfg, key, exc):
    """
    Flag every result field that a failed call would have produced.

    Multi-field calls declare their fields via "result_keys" so the failure can
    be attributed precisely without running the process_fn (which, for blood
    pressure, would re-issue the very request that just failed).
    """
    reason = str(exc)
    for field in cfg.get("result_keys") or [key]:
        result[field] = FETCH_FAILED
        fetch_failures[field] = reason


def fetch_patient_metrics(patient_id: int, end_date: str = endDate, start_Date: str = startDate, credential_profile: str = "1") -> Dict[str, Any]:
    try:
        patient_id = int(patient_id)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid patient_id: {patient_id}")

    # The session authenticates per request, so a patient that takes minutes to
    # process can never keep using a token that expired after it started.
    session = new_session(credential_profile)

    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Metrics whose API call failed, as {result_field: reason}. Reported per
    # field so one broken endpoint does not invalidate the rest of the patient.
    fetch_failures: Dict[str, str] = {}

    # ---- Onboard Date ----
    # Around twenty metrics use onboard_date as their query startDate, so it is
    # not an optional field: substituting end_date collapses every one of those
    # queries to a single day, which returns empty history and would be reported
    # as ~36 false mismatches. If this call fails the patient cannot be checked
    # at all, so fail loudly here rather than compare fabricated blanks.
    onboard_data = api_request(
        session, "post", f"{base_url}/api/v1/users/onboard-date",
        profile=credential_profile, json={"patientId": patient_id}, label="onboard_date",
    )
    # A successful response that simply carries no date is a different matter:
    # keep the historical end_date fallback for that case.
    onboard_date = (onboard_data or {}).get("data", {}).get("onBoardDate") or end_date


    # ---- Metrics Map ----
    metrics_map = {
        "clinical_ws_dashboard": {
            "path": "/api/v1/scores/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "clinical"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },

        "lifestyle_ws_dashboard": {
            "path": "/api/v1/scores/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "lifestyle"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },

        "fitbit_and_last_sync_dashboard": {
            "path": "/api/v1/devices/details",
            "result_keys": ["fitbit_device_dashboard", "last_sync_timestamp_dashboard"],
            "payload": lambda patient_id: {"patientId": patient_id},
            "process_fn": lambda data, *args: {
                "fitbit_device_dashboard": (data[0].get("device_version") if isinstance(data, list) and len(data) > 0 else ""),
                "last_sync_timestamp_dashboard": (parser.parse(data[0].get("last_sync")).strftime("%Y-%m-%d") if isinstance(data, list) and data and data[0].get("last_sync") else None),},
        },

        "wellness_plan_data": {
            "path": f"/api/v1/plans/{patient_id}",
            "result_keys": ["lifestyle_recs_specific_dashboard", "condition_doctor_dashboard",
                            "condition_assessment_dashboard", "doctor_id_engagement_dashboard",
                            "issue_latest_wellness_plan_time_dashboard"],
            "method": "get",
            "process_fn": lambda data, *args: ((lambda created_at_dt:
                    {
                "lifestyle_recs_specific_dashboard": (
                    ", ".join(data.get("plan", {}).get("plan", {}).get("selectedsuggestion", []))
                    if created_at_dt and created_at_dt.date() <= datetime.strptime(end_date, "%Y-%m-%d").date() else ""
                ),
                "condition_doctor_dashboard": (
                    ", ".join(data.get("plan", {}).get("plan", {}).get("selectedMedicalCondition", []))
                    if (
                        created_at_dt and
                        created_at_dt.date() <= datetime.strptime(end_date, "%Y-%m-%d").date() and
                        isinstance(data.get("plan", {}).get("plan", {}).get("selectedMedicalCondition"), list)) else (
                        data.get("plan", {}).get("plan", {}).get("selectedMedicalCondition", "")
                        if (
                            created_at_dt and
                            created_at_dt.date() <= datetime.strptime(end_date, "%Y-%m-%d").date() and
                            data.get("plan", {}).get("plan", {}).get("selectedMedicalCondition")
                        ) else "")
                ) if isinstance(data, dict) and data.get("plan") else "",
                "condition_assessment_dashboard": (
                    ", ".join(data.get("plan", {}).get("plan", {}).get("extraCondition", []))
                    if created_at_dt and created_at_dt.date() <= datetime.strptime(end_date, "%Y-%m-%d").date() else ""
                ),
                "doctor_id_engagement_dashboard": (
                    data.get("plan", {}).get("clinicianId", "")
                    if created_at_dt and created_at_dt.date() <= datetime.strptime(end_date, "%Y-%m-%d").date() and isinstance(data.get("plan"), dict) else ""
                ) if isinstance(data, dict) and data.get("plan") else "",
                "issue_latest_wellness_plan_time_dashboard": (
                    datetime.fromtimestamp(int(data.get("plan", {}).get("createdAt")) / 1000).strftime("%Y-%m-%d").lstrip("0").replace("/0", "/")
                    if created_at_dt and created_at_dt.date() <= datetime.strptime(end_date, "%Y-%m-%d").date() else "")})
                    (datetime.fromtimestamp(int(data.get("plan", {}).get("createdAt")) / 1000) if isinstance(data, dict) and isinstance(data.get("plan"), dict) and data.get("plan").get("createdAt") else None))},

        "onboarding_metrics_dashboard": {
            "path": "/api/v1/onboarding/survey",
            "result_keys": ["sex_dashboard", "race_dashboard", "employment_dashboard", "age_dashboard",
                            "industry_dashboard", "grocery_shopping_dashboard", "eating_out_dashboard",
                            "physical_activity_dashboard", "condition_self_dashboard",
                            "work_wellness_prog_dashboard"],
            "payload": lambda patient_id: {"organisationId": organisationId, "patientId": patient_id,"requestFlag": "profile"},
            "process_fn": lambda data, *args: {
                "sex_dashboard": data.get("data", {}).get("sex", ""),
                "race_dashboard": data.get("data", {}).get("race", ""),
                "employment_dashboard": data.get("data", {}).get("employment_status", ""),
                "age_dashboard": calculate_age(data.get("data", {}).get("date_of_birth")),
                "industry_dashboard": (data.get("data", {}).get("industry", "") if data.get("data", {}).get("employment_status") == "Working" else ""),
                "grocery_shopping_dashboard": data.get("data", {}).get("diet_q1", ""),
                "eating_out_dashboard": data.get("data", {}).get("diet_q2", ""),
                "physical_activity_dashboard": data.get("data", {}).get("activity_q1", ""),
                "condition_self_dashboard": ", ".join(data.get("data", {}).get("conditions_q1", [])),
                "work_wellness_prog_dashboard": data.get("data", {}).get("program_q1", ""),
            },
        },

        "blood_pressure_data": {
            "path": None,
            "result_keys": ["sbp_self_dashboard", "dbp_self_dashboard",
                            "sbp_clinic_dashboard", "dbp_clinic_dashboard"],
            "payload": lambda patient_id: None,
            "process_fn": lambda data, result, patient_id: (
                lambda self_rec, clinic_rec: {
                    "sbp_self_dashboard": self_rec.get("systolic", {}).get("value", "") if self_rec else "",
                    "dbp_self_dashboard": self_rec.get("diastolic", {}).get("value", "") if self_rec else "",
                    "sbp_clinic_dashboard": clinic_rec.get("systolic", {}).get("value", "") if clinic_rec else "",
                    "dbp_clinic_dashboard": clinic_rec.get("diastolic", {}).get("value", "") if clinic_rec else "",
                }
            )(*fetch_latest_bp_records(patient_id, onboard_date, end_date, session, base_url, credential_profile)),
        },

        # Individual API Calls
        "steps_dashboard": {
            "path": "/api/v1/activity/steps",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": startDate, "endDate": end_date,"flag": "week"},
            "process_fn": lambda data, *args: get_latest_value_in_range(data.get("history", []), startDate, end_date),
        },
        "hrv_val_dashboard": {
            "path": "/api/v1/vitals/hrv",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "RHR_dashboard": {
            "path": "/api/v1/vitals/resting-hr",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "sedentary_time_dashboard": {
            "path": "/api/v1/activity/sedentary",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "sleep_dashboard": {
            "path": "/api/v1/activity/sleep",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "fbg_dashboard": {
            "path": "/api/v1/labs/glucose",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "hba1c_dashboard": {
            "path": "/api/v1/labs/hba1c",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "tchol_dashboard": {
            "path": "/api/v1/labs/cholesterol",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "total"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "hdl_dashboard": {
            "path": "/api/v1/labs/cholesterol",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "HDL"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "ldl_dashboard": {
            "path": "/api/v1/labs/cholesterol",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "LDL"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "tg_dashboard": {
            "path": "/api/v1/labs/cholesterol",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "triglycerides"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },

        "bmi_dashboard": {
            "path": "/api/v1/body/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "bmi", },
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "wc_dashboard": {
            "path": "/api/v1/body/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "waist_circumference", },
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },

        "weight_dashboard": {
            "path": "/api/v1/body/overview",
            "payload": lambda patient_id: {"startDate": end_date, "endDate": end_date, "type": "overview", "patientId": patient_id},
            "process_fn": lambda data, *args: (data.get("latestWeightScore", "")
                if data.get("lastUpdated") and datetime.strptime(data.get("lastUpdated"), "%Y-%m-%d") <= datetime.strptime(end_date, "%Y-%m-%d")else ""),
        },
        "smoke_dashboard": {
            "path": "/api/v1/self-reported/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "habit_a"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "alcohol_dashboard": {
            "path": "/api/v1/self-reported/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "habit_b"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "unhealthy_oil_fat_dashboard": {
            "path": "/api/v1/self-reported/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "diet_a"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "fruit_vegetables_dashboard": {
            "path": "/api/v1/self-reported/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "diet_b"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "wholegrains_dashboard": {
            "path": "/api/v1/self-reported/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "diet_c"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "limit_sugar_dashboard": {
            "path": "/api/v1/self-reported/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "diet_d"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "stress_dashboard": {
            "path": "/api/v1/self-reported/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"flag": "week", "metric": "mood_a"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "processed_food_dashboard": {
            "path": "/api/v1/self-reported/analytics",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": onboard_date, "endDate": end_date,"metric": "diet_e", "flag": "week"},
            "process_fn": lambda data, *args: get_latest_value(data.get("history", [])),
        },
        "mvpa_dashboard": {
            "path": "/api/v1/activity/exercise",
            "payload": lambda patient_id: {"patientId": patient_id, "startDate": start_Date, "endDate": end_date,"metric": "diet_e", "flag": "week"},
            "process_fn": lambda data, *args: data.get("total") or "",
        },
        "clinic_dashboard": {
            "path": "/api/v1/users/profile",
            "payload": lambda patient_id: {"patientId": patient_id, "isProfile": True, "userId": f"{patient_id}"},
            "process_fn": lambda data, *args: data.get("clinicName", ""),
        },
        
        "assessment_date_dashboard": {
            "path": "/api/v1/self-reported/analytics",
            "payload": lambda patient_id: {"patientId": patient_id,"startDate": onboard_date,"endDate": end_date, "metric": "diet_e","flag": "month"},
            "process_fn": lambda data, *args: get_latest_value_before_end(data.get("history", []), data.get("endDate"))
        },

        "pcc_timestamp_dashboard": {
            "path": "/api/v1/users/linked",
            "payload": lambda patient_id: {"doctorId": get_user_entity(credential_profile)},
            "timeout": 60,
            "process_fn": lambda data, *args: (
                (lambda max_date: datetime.fromtimestamp(max_date / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                            if max_date and datetime.fromtimestamp(max_date / 1000, tz=timezone.utc).date() <= datetime.strptime(end_date, "%Y-%m-%d").date() else "")
                            (max(u.get("pcc_dates", []) or [0])
                            if (u := next((x for x in (data or []) if x.get("id") == patient_id), None)) and u.get("pcc_dates") else 0)),
        },

        "pcc_intervention_dashboard": {
            "path": "/api/v1/users/linked",
            "payload": lambda patient_id: {"doctorId": get_user_entity(credential_profile)},
            "timeout": 60,
            "process_fn": lambda data, *args: ((lambda max_date: ( "Yes" if max_date and datetime.fromtimestamp(max_date / 1000, 
                            tz=timezone.utc).date() <= datetime.strptime(end_date, "%Y-%m-%d").date() else "No"))
                            (max(u.get("pcc_dates", []) or [0])
                            if (u := next((x for x in (data or []) if x.get("id") == patient_id), None)) and u.get("pcc_dates") else 0)),
        },
        
        "os_date_dashboard": {
            "path": None,
            "payload": lambda patient_id: None,
            "process_fn": lambda data, *args: onboard_date,
        },
    }

    # ---- Result dict ----
    result: Dict[str, Any] = {}


    # MVPA
    # mvpa_found = False
    # guard_rail = 14
    # mvpa_url = f"{base_url}/api/v1/activity/exercise"
    # # mvpa_url = f"{base_url}//wellness-score/wellness-score/wellness-wheel/get?requested=lifestyle,wellnessScoreSnapshotsData"
    # current_end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # for i in range(guard_rail + 1):
    #     current_start_dt = current_end_dt - timedelta(days=6)
    #     start_date_str = current_start_dt.strftime("%Y-%m-%d")
    #     end_date_str = current_end_dt.strftime("%Y-%m-%d")

    #     try:
    #         payload = {"patientId": patient_id, "startDate": start_date_str, "endDate": end_date_str, "flag": "week"}
    #         response = session.post(mvpa_url, json=payload)
    #         response.raise_for_status()
    #         data = response.json()

    #         if i == 0 and data.get("total", 0) > 0:
    #             result["mvpa_dashboard"] = data["total"]
    #             mvpa_found = True
    #             break

    #         latest_value = get_latest_value(data.get("history", []))
    #         if latest_value is not None:
    #             result["mvpa_dashboard"] = latest_value
    #             mvpa_found = True
    #             break

    #     except requests.exceptions.RequestException:
    #         pass

    #     current_end_dt -= timedelta(days=1)

    # if not mvpa_found:
    #     result["mvpa_dashboard"] = None


    # ---- Fetch all metrics from metrics_map ----
    response_cache = {}

    for key, cfg in metrics_map.items():
        data = None

        if cfg.get("path"):
            method = cfg.get("method", "post").lower()
            payload = cfg.get("payload", lambda _: None)(patient_id)
            params = cfg.get("params", lambda _: None)(patient_id)
            cache_key = (method, cfg["path"], repr(payload), repr(params))

            if cache_key not in response_cache:
                if cache_key in _shared_cache:
                    data = _shared_cache[cache_key]
                else:
                    # api_request raises ApiError rather than returning {} on a
                    # failed call, so a broken request can never be mistaken for
                    # "the dashboard has no value here".
                    try:
                        data = api_request(
                            session, method,
                            f"{base_url}{cfg['path']}".format(patient_id=patient_id),
                            profile=credential_profile,
                            json=payload,
                            params=params,
                            timeout=cfg.get("timeout", 15),
                            label=key,
                        )
                    except ApiError as exc:
                        _mark_fetch_failed(result, fetch_failures, cfg, key, exc)
                        continue
                    # ---- ADD THIS LINE ----
                    # print(f"\n--- API Response for {key} ---\n{data}\n--- End of Response ---\n")
                    # ---- END ADDITION ----

                    # Only cache successful non-empty responses so a timeout doesn't poison future patients
                    if data:
                        with _shared_cache_lock:
                            _shared_cache[cache_key] = data

                response_cache[cache_key] = data
            else:
                data = response_cache[cache_key]

        try:
            processed_data = cfg["process_fn"](data, result, patient_id)
            if isinstance(processed_data, dict):
                result.update(processed_data)
            else:
                result[key] = processed_data
        except AuthenticationError:
            raise
        except ApiError as exc:
            # Raised by a process_fn that fetches on its own (blood pressure).
            _mark_fetch_failed(result, fetch_failures, cfg, key, exc)
        except Exception as e:
            result[key] = None
            print(f"Error processing {key}: {e}")

    result["_fetch_failures"] = fetch_failures
    return result