import csv
import sys
sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252, which can't print the emoji in these logs
from api_client import fetch_patient_metrics, FETCH_FAILED
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dateutil import parser
# from google.cloud import storage
from global_data import *


""" 
    Usage: python batch_parity_check.py [csv_path] [credential_profile]
    Defaults: weekly_snapshot.csv, profile 1 (see .env.example and auth_manager.py).
    Run it again with another csv_path / profile to check another export or account.
"""

# ------------------------ Download latest CSV from bucket ------------------------
# def download_latest_csv_from_gcs(storage_name, container_name, prefix=""):
#     client = storage.Client()
#     bucket = client.bucket(storage_name)
#     # Iterate over blobs and find the latest CSV file
#     latest_blob = None
#     for blob in bucket.list_blobs(prefix=container_name + "/" + prefix):
#         if blob.name.endswith(".csv"):
#             if latest_blob is None or blob.time_created > latest_blob.time_created:
#                 latest_blob = blob
#     if latest_blob is None:
#         print("No CSV files found.")
#         return "NO_FILES"
#     local_filename = latest_blob.name.split("/")[-1]
#     try:
#         latest_blob.download_to_filename(local_filename)
#         print(f"Downloaded gs://{storage_name}/{latest_blob.name} to {local_filename}")
#         return local_filename
#     except Exception as e:
#         print(f"Download failed: {e}")
#         return "DOWNLOAD_FAILED"


# ------------------------ CSV Read ------------------------

def read_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            # Skip rows where all values are empty
            if not any(str(v).strip() for v in row.values()):
                continue
            rows.append(row)
    return rows


# ------------------------ Helper functions for number ------------------------
def clean_value(value):
    if value is FETCH_FAILED:
        return FETCH_FAILED
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() == "none":
        return None
    value = str(value).strip()
    if value == "":
        return None
    return value


def safe_int(value):
    if value is FETCH_FAILED:
        return FETCH_FAILED
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None

def normalize_num(v):
    if v is FETCH_FAILED:
        return FETCH_FAILED
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if v == "":
            return None
    try:
        return int(float(v))
    except:
        return None

def clean_number(value):
    """Convert float with .0 to int, keep decimals otherwise."""
    if value is FETCH_FAILED:
        return FETCH_FAILED

    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value

# ------------------------ Helper functions for string ------------------------
def clean_string(value):
    """Normalize string value to prevent false mismatches."""
    if value is FETCH_FAILED:
        return FETCH_FAILED

    if value is None:
        return ""
    return str(value).strip()


def normalize(value):
    """Normalize strings for fair comparison."""
    if value is FETCH_FAILED:
        return FETCH_FAILED

    if value is None:
        return ""
    # Remove extra spaces around commas and words, lowercase everything
    return ", ".join([v.strip() for v in str(value).split(",")]).strip().lower()


def normalize_condition_assessment(value):
    """Normalize single condition_assessment value."""
    if value is None:
        return ""
    value = str(value).strip().lower()
    mappings = {
        "well_controlled_bp": "Adequately Controlled BP",
        "poorly_controlled_bp": "Poorly Controlled BP",
        "well_controlled_lipids_disorders": "Adequately Controlled Lipid Disorders",
        "well_controlled_diabetes": "Adequately Controlled Diabetes",
        "diabetic_retinopathy": "Diabetic Retinopathy",
        "greater_risk_hypoglycaemia": "Type 2 DM at Greater Risk of Hypoglycaemia",
        "poorly_controlled_lipid_disorders": "Poorly Controlled Lipid Disorders",
        "poorly_controlled_diabetes": "Poorly Controlled Diabetes",
        "diabetic_neuropathy": "Diabetic Neuropathy",
        "not_relevant": "Not relevant",
        "high_bmi_and_wc": "High BMI and WC",
    }
    return mappings.get(value, value)

def normalize_list(value):
    """Normalize a comma-separated list of condition assessments."""
    if value is FETCH_FAILED:
        return FETCH_FAILED

    if not value:
        return []
    return [normalize_condition_assessment(v.strip()) for v in str(value).split(",")]


# ------------------------ Helper functions for Date ------------------------
def normalize_date(value):
    """Normalize any date string to YYYY-MM-DD format (ignores time)."""
    if value is FETCH_FAILED:
        return FETCH_FAILED

    if not value:
        return ""
    value_str = str(value).strip()
    try:
        dt = parser.parse(value_str, dayfirst=False, fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return ""


def dates_match(csv_val, dash_val):
    """Check if two dates match, allowing for swapped day/month."""
    if csv_val is FETCH_FAILED or dash_val is FETCH_FAILED:
        return True  # not fetched -> reported separately, never compared

    d1 = normalize_date(csv_val)
    d2 = normalize_date(dash_val)

    if not d1 or not d2:
        return d1 == d2  # both blank is fine

    if d1 == d2:
        return True

    # Try swapped interpretation
    try:
        d1_alt = parser.parse(str(csv_val), dayfirst=True, fuzzy=True).date().isoformat()
        d2_alt = parser.parse(str(dash_val), dayfirst=True, fuzzy=True).date().isoformat()
        return d1_alt == d2 or d2_alt == d1 or d1_alt == d2_alt
    except Exception:
        return False


# ------------------------ Per-user processing ------------------------

def process_user(user):
    start_time = time.time()
    user_id = user["user_id"]
    # Debug print
    # print(f"Processing user_id: '{user_id}'")
    # Robust check
    if not user_id or not str(user_id).strip().isdigit():
        print(f"❌ Skipping user with invalid user_id: '{user_id}'")
        return
    try:
        metrics = fetch_patient_metrics(int(user_id), credential_profile=CREDENTIAL_PROFILE)

        for field, reason in (metrics.get("_fetch_failures") or {}).items():
            print(f"🚫 FETCH_FAILED → user_id {user_id} | {field} | not compared ({reason})")

        # ------------------------ Dashboard Number Values ------------------------
        clinical_ws_dashboard = clean_value(metrics.get("clinical_ws_dashboard"))
        RHR_dashboard = clean_value(metrics.get("RHR_dashboard"))
        hrv_val_dashboard = clean_value(metrics.get("hrv_val_dashboard"))
        lifestyle_ws_dashboard = clean_value(metrics.get("lifestyle_ws_dashboard"))
        steps_dashboard = clean_value(metrics.get("steps_dashboard"))
        sedentary_time_dashboard = clean_value(metrics.get("sedentary_time_dashboard"))
        sleep_dashboard = clean_value(metrics.get("sleep_dashboard"))
        mvpa_dashboard = clean_value(metrics.get("mvpa_dashboard"))
        doctor_id_engagement_dashboard = clean_value(metrics.get("doctor_id_engagement_dashboard"))

        # Note: `doctor_id_pre_survey_dashboard` is not part of the doctor_id_engagement_dashboard and doesn't have a direct mapping in the dashboard, so we will skip it for now.
        # doctor_id_pre_survey_dashboard = clean_value(metrics.get("doctor_id_engagement_dashboard"))
        
        age_dashboard = clean_value(metrics.get("age_dashboard"))
        fg_value_dashboard = clean_value(metrics.get("fbg_dashboard"))
        hba1c_value_dashboard = clean_value(metrics.get("hba1c_dashboard"))
        tc_value_dashboard = clean_value(metrics.get("tchol_dashboard"))
        hdl_value_dashboard = clean_value(metrics.get("hdl_dashboard"))
        ldl_value_dashboard = clean_value(metrics.get("ldl_dashboard"))
        tg_value_dashboard = clean_value(metrics.get("tg_dashboard"))
        bmi_value_dashboard = clean_value(metrics.get("bmi_dashboard"))
        wc_dashboard_dashboard = clean_value(metrics.get("wc_dashboard"))
        fruit_veg_dashboard = clean_value(metrics.get("fruit_vegetables_dashboard"))
        weight_dashboard = clean_value(metrics.get("weight_dashboard"))
        sbp_self_dashboard = clean_value(metrics.get("sbp_self_dashboard"))
        dbp_self_dashboard = clean_value(metrics.get("dbp_self_dashboard"))
        sbp_clinic_dashboard = clean_value(metrics.get("sbp_clinic_dashboard"))
        dbp_clinic_dashboard = clean_value(metrics.get("dbp_clinic_dashboard"))

        # ------------------------ Dashboard string Values ------------------------
        processed_food_dashboard = clean_string(metrics.get("processed_food_dashboard"))
        fitbit_device_dashboard = clean_string(metrics.get("fitbit_device_dashboard"))
        lifestyle_recs_specific_dashboard = clean_string(metrics.get("lifestyle_recs_specific_dashboard"))
        if lifestyle_recs_specific_dashboard is FETCH_FAILED:
            lifestyle_recs_selected_dashboard = FETCH_FAILED
        elif lifestyle_recs_specific_dashboard and str(lifestyle_recs_specific_dashboard).strip():
            lifestyle_recs_selected_dashboard = "Yes"
        else:
            lifestyle_recs_selected_dashboard = ""
        condition_doctor_dashboard = clean_string(metrics.get("condition_doctor_dashboard"))
        condition_assessment_dashboard = clean_string(metrics.get("condition_assessment_dashboard"))
        sex_dashboard = clean_string(metrics.get("sex_dashboard"))
        race_dashboard = clean_string(metrics.get("race_dashboard"))
        employment_dashboard = clean_string(metrics.get("employment_dashboard"))
        industry_dashboard = clean_string(metrics.get("industry_dashboard"))
        work_wellness_prog_dashboard = clean_string(metrics.get("work_wellness_prog_dashboard"))
        grocery_shopping_dashboard = clean_string(metrics.get("grocery_shopping_dashboard"))
        eating_out_dashboard = clean_string(metrics.get("eating_out_dashboard"))
        physical_activity_dashboard = clean_string(metrics.get("physical_activity_dashboard"))
        condition_self_dashboard = clean_string(metrics.get("condition_self_dashboard"))
        pcc_intervention_dashboard = clean_string(metrics.get("pcc_intervention_dashboard"))
        smoke_dashboard = clean_string(metrics.get("smoke_dashboard"))
        alcohol_dashboard = clean_string(metrics.get("alcohol_dashboard"))
        unhealthy_oil_fat_dashboard = clean_string(metrics.get("unhealthy_oil_fat_dashboard"))
        wholegrains_dashboard = clean_string(metrics.get("wholegrains_dashboard"))
        limit_sugar_dashboard = clean_string(metrics.get("limit_sugar_dashboard"))
        stress_dashboard = clean_string(metrics.get("stress_dashboard"))
        # ------------------------ Dashboard Date Values ------------------------
        os_date_dashboard = normalize_date(metrics.get("os_date_dashboard"))
        pcc_timestamp_dashboard = normalize_date(metrics.get("pcc_timestamp_dashboard"))
        last_sync_timestamp_dashboard = normalize_date(metrics.get("last_sync_timestamp_dashboard"))
        assessment_date_dashboard = metrics.get("assessment_date_dashboard")
        issue_latest_wellness_plan_time_dashboard = normalize_date( metrics.get("issue_latest_wellness_plan_time_dashboard"))

        # ------------------------ CSV number Values ------------------------
        wellness_clinical = clean_number(user.get("wellness_clinical"))
        rhr = clean_number(user.get("rhr"))
        hrv = clean_number(user.get("hrv"))
        wellness_lifestyle = clean_number(user.get("wellness_lifestyle"))
        steps = clean_number(user.get("steps"))
        sedentary_time = clean_number(user.get("sedentary_time"))
        sleep = clean_number(user.get("sleep"))
        mvpa = clean_number(user.get("mvpa"))
        doctor_id_engagement = clean_number(user.get("doctor_id_engagement"))

        """
        Legacy CSV field 'doctor_id_pre_survey' is intentionally ignored. It doesn't have a clear mapping in the dashboard and may not be consistently populated, 
        so including it in the comparison could lead to false mismatches and confusion. 
        """
        # doctor_id_pre_survey = clean_number(user.get("doctor_id_pre_survey"))
        

        age = clean_number(user.get("age"))
        fbg = clean_number(user.get("fbg"))
        hba1c = clean_number(user.get("hba1c"))
        tchol = clean_number(user.get("tchol"))
        hdl = clean_number(user.get("hdl"))
        ldl = clean_number(user.get("ldl"))
        tg = clean_number(user.get("tg"))
        bmi = clean_number(user.get("bmi"))
        wc = clean_number(user.get("wc"))
        fruit_vegetables = clean_number(user.get("fruit_vegetables"))
        weight = clean_number(user.get("weight"))
        sbp_self = clean_number(user.get("sbp_self"))
        dbp_self = clean_number(user.get("dbp_self"))
        sbp_clinic = clean_number(user.get("sbp_clinic"))
        dbp_clinic = clean_number(user.get("dbp_clinic"))

        # ------------------------ CSV string Values ------------------------

        processed_food = clean_string(user.get("processed_food"))
        fitbit_device = clean_string(user.get("fitbit_device"))
        lifestyle_recs_specific = clean_string(user.get("lifestyle_recs_specific"))
        lifestyle_recs_selected = clean_string(user.get("lifestyle_recs_selected"))
        condition_doctor = clean_string(user.get("condition_doctor"))
        condition_assessment = clean_string(user.get("condition_assessment"))
        sex = clean_string(user.get("sex"))
        race = clean_string(user.get("race"))
        employment = clean_string(user.get("employment"))
        industry = clean_string(user.get("industry"))
        work_wellness_prog = clean_string(user.get("work_wellness_prog"))
        grocery_shopping = clean_string(user.get("grocery_shopping"))
        eating_out = clean_string(user.get("eating_out"))
        physical_activity = clean_string(user.get("physical_activity"))
        condition_self = clean_string(user.get("condition_self"))
        pcc_intervention = clean_string(user.get("pcc_intervention"))
        smoking = clean_string(user.get("smoking"))
        alcohol = clean_string(user.get("alcohol"))
        unhealthy_oil_fat = clean_string(user.get("unhealthy_oil_fat"))
        wholegrains = clean_string(user.get("wholegrains"))
        limit_sugar = clean_string(user.get("limit_sugar"))
        stress = clean_string(user.get("stress"))
        # ------------------------ CSV Date Values ------------------------
        os_date = normalize_date(user.get("os_date"))
        pcc_timestamp = normalize_date(user.get("pcc_timestamp"))
        last_sync_timestamp = normalize_date(user.get("last_sync_timestamp"))
        assessment_date = user.get("assessment_date")
        issue_latest_wellness_plan_time = normalize_date(user.get("issue_latest_wellness_plan_time"))

        # ------------------------ Comparisons for number------------------------

        if safe_int(wellness_clinical) != safe_int(clinical_ws_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV wellness_clinical: {wellness_clinical} | Dashboard wellness_clinical: {clinical_ws_dashboard}")

        if safe_int(rhr) != safe_int(RHR_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV RHR: {rhr} | Dashboard wellness_RHR: {RHR_dashboard}")

        if safe_int(hrv) != safe_int(hrv_val_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV hrv_val: {hrv} | Dashboard wellness_hrv_val: {hrv_val_dashboard}")

        if safe_int(wellness_lifestyle) != safe_int(lifestyle_ws_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV wellness_lifestyle: {wellness_lifestyle} | Dashboard wellness_lifestyle: {lifestyle_ws_dashboard}")

        if normalize_num(steps) != normalize_num(steps_dashboard):
            print(f"⚠️ Mismatch → user_id {user_id} | CSV steps: {steps} | Dashboard wellness_steps: {steps_dashboard}")

        if safe_int(sedentary_time) != safe_int(sedentary_time_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV sedentary_time: {sedentary_time} | Dashboard wellness_sedentary_time: {sedentary_time_dashboard}")

        if sleep_dashboard is not None and sleep != "" and sleep_dashboard != "":
            if int(float(sleep)) != int(float(sleep_dashboard)):
                print(f"⚠️  Mismatch → user_id {user_id} | CSV sleep: {sleep} | Dashboard sleep: {sleep_dashboard}")

        if safe_int(mvpa) != safe_int(mvpa_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV mvpa: {mvpa} | Dashboard wellness_mvpa: {mvpa_dashboard}")

        if safe_int(doctor_id_engagement) != safe_int(doctor_id_engagement_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV doctor_id_engagement: {doctor_id_engagement} | Dashboard wellness_doctor_id_engagement: {doctor_id_engagement_dashboard}")

        # Since API and CSV have different definitions and data quality for `doctor_id_pre_survey`, we will skip this comparison to avoid false mismatches.
        # if safe_int(doctor_id_pre_survey) != safe_int(doctor_id_pre_survey_dashboard):
        #     print(
        #         f"⚠️  Mismatch → user_id {user_id} | CSV doctor_id_pre_survey: {doctor_id_pre_survey} | Dashboard wellness_doctor_id_pre_survey: {doctor_id_pre_survey_dashboard}")

        if safe_int(age) != safe_int(age_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV age: {age} | Dashboard age: {age_dashboard}")

        if safe_int(fbg) != safe_int(fg_value_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV fbg: {fbg} | Dashboard wellness_fbg: {fg_value_dashboard}")

        if safe_int(hba1c) != safe_int(hba1c_value_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV hba1c: {hba1c} | Dashboard wellness_hba1c: {hba1c_value_dashboard}")

        if safe_int(tchol) != safe_int(tc_value_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV tchol: {tchol} | Dashboard wellness_tchol: {tc_value_dashboard}")

        if safe_int(hdl) != safe_int(hdl_value_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV hdl: {hdl} | Dashboard wellness_hdl: {hdl_value_dashboard}")

        if safe_int(ldl) != safe_int(ldl_value_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV ldl: {ldl} | Dashboard wellness_ldl: {ldl_value_dashboard}")

        if safe_int(tg) != safe_int(tg_value_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV tg: {tg} | Dashboard wellness_tg: {tg_value_dashboard}")

        if safe_int(bmi) != safe_int(bmi_value_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV bmi: {bmi} | Dashboard bmi: {bmi_value_dashboard}")

        if safe_int(wc) != safe_int(wc_dashboard_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV wc: {wc} | Dashboard wellness_wc: {wc_dashboard_dashboard}")

        if safe_int(fruit_vegetables) != safe_int(fruit_veg_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV fruit_vegetables: {fruit_vegetables} | Dashboard wellness_fruit_vegetables: {fruit_veg_dashboard}")

        if safe_int(weight) != safe_int(weight_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV weight: {weight} | Dashboard weight: {weight_dashboard}")

        if safe_int(sbp_self) != safe_int(sbp_self_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV sbp_self: {sbp_self} | Dashboard sbp_self: {sbp_self_dashboard}")

        if safe_int(dbp_self) != safe_int(dbp_self_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV dbp_self: {dbp_self} | Dashboard dbp_self: {dbp_self_dashboard}")

        if safe_int(sbp_clinic) != safe_int(sbp_clinic_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV sbp_clinic: {sbp_clinic} | Dashboard sbp_clinic: {sbp_clinic_dashboard}")

        if safe_int(dbp_clinic) != safe_int(dbp_clinic_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV dbp_clinic: {dbp_clinic} | Dashboard dbp_clinic: {dbp_clinic_dashboard}")

        # ------------------------ Comparisons for string------------------------

        if normalize(processed_food) != normalize(processed_food_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV processed_food: {processed_food} | Dashboard processed_food: {processed_food_dashboard}")

        if normalize(fitbit_device) != normalize(fitbit_device_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV fitbit_device: {fitbit_device} | Dashboard fitbit_device: {fitbit_device_dashboard}")

        if normalize(lifestyle_recs_specific) != normalize(lifestyle_recs_specific_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV lifestyle_recs_specific: {lifestyle_recs_specific} | Dashboard lifestyle_recs_specific: {lifestyle_recs_specific_dashboard}")

        if normalize(lifestyle_recs_selected) != normalize(lifestyle_recs_selected_dashboard):
            print( f"⚠️  Mismatch → user_id {user_id} | CSV lifestyle_recs_selected: {lifestyle_recs_selected} | Dashboard lifestyle_recs_selected: {lifestyle_recs_selected_dashboard}")

        if normalize(condition_doctor) != normalize(condition_doctor_dashboard):
            print( f"⚠️  Mismatch → user_id {user_id} | CSV condition_doctor: {condition_doctor} | Dashboard condition_doctor: {condition_doctor_dashboard}")

        csv_clean = normalize_list(condition_assessment)
        dash_clean = normalize_list(condition_assessment_dashboard)

        # Compare in lowercase to ignore case differences
        if (csv_clean is not FETCH_FAILED and dash_clean is not FETCH_FAILED
                and sorted([v.lower() for v in csv_clean]) != sorted([v.lower() for v in dash_clean])):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV condition_assessment: {','.join(csv_clean)} | Dashboard condition_assessment: {','.join(dash_clean)}"
            )
        if normalize(sex) != normalize(sex_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV sex: {sex} | Dashboard sex: {sex_dashboard}")

        if normalize(race) != normalize(race_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV race: {race} | Dashboard race: {race_dashboard}")

        if normalize(employment) != normalize(employment_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV employment: {employment} | Dashboard employment: {employment_dashboard}")

        if normalize(industry) != normalize(industry_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV industry: {industry} | Dashboard industry: {industry_dashboard}")

        if normalize(work_wellness_prog) != normalize(work_wellness_prog_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV work_wellness_prog: {work_wellness_prog} | Dashboard work_wellness_prog: {work_wellness_prog_dashboard}")

        if normalize(grocery_shopping) != normalize(grocery_shopping_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV grocery_shopping: {grocery_shopping} | Dashboard grocery_shopping: {grocery_shopping_dashboard}")

        if normalize(eating_out) != normalize(eating_out_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV eating_out: {eating_out} | Dashboard eating_out: {eating_out_dashboard}")

        if normalize(physical_activity) != normalize(physical_activity_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV physical_activity: {physical_activity} | Dashboard physical_activity: {physical_activity_dashboard}")

        if normalize(condition_self) != normalize(condition_self_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV condition_self: {condition_self} | Dashboard condition_self: {condition_self_dashboard}")

        if normalize(pcc_intervention) != normalize(pcc_intervention_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV pcc_intervention: {pcc_intervention} | Dashboard pcc_intervention: {pcc_intervention_dashboard}")

        if normalize(smoking) != normalize(smoke_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV smoking: {smoking} | Dashboard smoke: {smoke_dashboard}")

        if normalize(alcohol) != normalize(alcohol_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV alcohol: {alcohol} | Dashboard alcohol: {alcohol_dashboard}")

        if normalize(unhealthy_oil_fat) != normalize(unhealthy_oil_fat_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV unhealthy_oil_fat: {unhealthy_oil_fat} | Dashboard unhealthy_oil_fat: {unhealthy_oil_fat_dashboard}")

        if normalize(wholegrains) != normalize(wholegrains_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV wholegrains: {wholegrains} | Dashboard wholegrains: {wholegrains_dashboard}")

        if normalize(limit_sugar) != normalize(limit_sugar_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV limit_sugar: {limit_sugar} | Dashboard limit_sugar: {limit_sugar_dashboard}")

        if normalize(stress) != normalize(stress_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV stress: {stress} | Dashboard stress: {stress_dashboard}")

        # ------------------------ Comparisons for Date------------------------

        if not dates_match(os_date, os_date_dashboard):
            print(f"⚠️  Mismatch → user_id {user_id} | CSV os_date: {os_date} | Dashboard os_date: {os_date_dashboard}")

        if not dates_match(pcc_timestamp, pcc_timestamp_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV pcc_timestamp: {pcc_timestamp} | Dashboard pcc_timestamp: {pcc_timestamp_dashboard}")

        if not dates_match(last_sync_timestamp, last_sync_timestamp_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV last_sync_timestamp: {last_sync_timestamp} | Dashboard last_sync_timestamp: {last_sync_timestamp_dashboard}")

        if not dates_match(assessment_date, assessment_date_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV assessment_date: {assessment_date} | Dashboard assessment_date: {assessment_date_dashboard}")

        if not dates_match(issue_latest_wellness_plan_time, issue_latest_wellness_plan_time_dashboard):
            print(
                f"⚠️  Mismatch → user_id {user_id} | CSV issue_latest_wellness_plan_time: {issue_latest_wellness_plan_time} | Dashboard issue_latest_wellness_plan_time: {issue_latest_wellness_plan_time_dashboard}")
    except Exception as e:
        # A failed fetch means we have nothing valid to compare against, so the
        # user is reported as an error rather than as a wall of false mismatches.
        print(f"❌ Error processing user {user_id} (comparison skipped): {e}")

    end_time = time.time()
    duration = end_time - start_time
    print(
        f"user_id {user_id} | End time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time))} "
        f"| Duration: {duration:.2f} seconds"
    )


# ------------------------ Process users in batches ------------------------
def process_in_batches(users, batch_size):
    # Filter out users with empty or invalid user_id
    users = [u for u in users if u.get("user_id") and str(u["user_id"]).strip().isdigit()]
    total_users = len(users)    
    for i in range(0, total_users, batch_size):
        batch = users[i:i + batch_size]
        print(f"\n🔹 Processing batch {i//batch_size + 1} ({len(batch)} users)...")
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = [executor.submit(process_user, user) for user in batch]
            for future in as_completed(futures):
                future.result()  # Wait for all users in the batch


# ------------------------ Main function ------------------------
CREDENTIAL_PROFILE = sys.argv[2] if len(sys.argv) > 2 else "1"

def main():
    main_start_time = time.time()
    # CSV_PATH = download_latest_csv_from_gcs(STORAGE_NAME, CONTAINER_NAME)
    CSV_PATH = sys.argv[1] if len(sys.argv) > 1 else "weekly_snapshot.csv"  # your local weekly CSV export

    if not os.path.exists(CSV_PATH):
        print("❌ No CSV file found. Exiting.")
        return

    data_list = read_csv(CSV_PATH)
    batch_size = 10  # Number of users processed simultaneously
    process_in_batches(data_list, batch_size=batch_size)

    main_end_time = time.time()
    print(
        f"All batches completed. Total duration: {main_end_time - main_start_time:.2f} seconds")
    if data_list and len(data_list) > 0:
        print(f"Processed {len(data_list)} users."
    )

    # Remove CSV after processing
    # if os.path.exists(CSV_PATH):
    #     os.remove(CSV_PATH)
    #     print(f"Removed temporary file: {CSV_PATH}")

if __name__ == "__main__":
    main()
