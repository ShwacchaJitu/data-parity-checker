"""
Self-contained demo: no credentials or real API needed. Run: python demo.py

Starts a mock API on localhost, then runs the real auth_manager and
fetch_patient_metrics against it for the users in sample_data/demo_snapshot.csv.
The mock enforces token expiry and injects the failures the client is built for:
  - tokens that expire mid-run         -> automatic re-login
  - one token rejected by the server   -> drop, re-login, retry
  - a transient 503 (user ...003)      -> retry with backoff, then success
  - an endpoint that never recovers (user ...007) -> FETCH_FAILED, not compared
  - three values planted in the CSV    -> reported as mismatches
Timings are shortened (3 s tokens, 5 attempts) so the demo finishes in seconds.
"""
import base64, csv, json, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8")

TOKEN_TTL_SECS = 3
_tokens, _lock, _count = set(), threading.Lock(), {"n": 0, "rejected": False, "flaky": 0}


def truth(uid):
    n = uid - 100000000
    return {"clinical": 60 + n, "lifestyle": 70 + n, "rhr": 55 + n, "hrv": 40 + 2 * n,
            "clinic": f"Example Clinic {'AB'[n % 2]}"}


def _jwt(exp):
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self): self.do_POST()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        path = self.path
        if path == "/login":
            return self._send(200, {"tempToken": "temp", "patientId": 1, "userId": "demo"})
        if path == "/verify-pin":
            exp = time.time() + TOKEN_TTL_SECS
            token = _jwt(exp)
            with _lock: _tokens.add(token)
            return self._send(200, {"token": token})

        time.sleep(0.04)  # fake latency
        token = (self.headers.get("Authorization") or "")[7:]
        with _lock:
            _count["n"] += 1
            inject_reject = _count["n"] == 60 and not _count["rejected"]
            if inject_reject: _count["rejected"] = True; _tokens.discard(token)
        expired = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==") or b"{}").get("exp", 0) < time.time() \
            if token.count(".") == 2 else True
        if token not in _tokens or expired:
            return self._send(403, {"message": "Invalid Token", "name": "InvalidToken", "code": 1003})

        uid = body.get("patientId", 0)
        t = truth(uid) if uid > 100000000 else {}
        if path.endswith("/vitals/hrv") and uid == 100000007:
            return self._send(503, {})  # never recovers
        if path.endswith("/vitals/resting-hr") and uid == 100000003:
            with _lock: _count["flaky"] += 1; flaky = _count["flaky"] <= 2
            if flaky: return self._send(503, {})  # recovers on the third try
        hist = lambda v: {"history": [{"timestamp": "2026-01-05", "value": v}]}
        if path.endswith("/scores/analytics"): return self._send(200, hist(t.get(body.get("metric"))))
        if path.endswith("/vitals/resting-hr"): return self._send(200, hist(t.get("rhr")))
        if path.endswith("/vitals/hrv"): return self._send(200, hist(t.get("hrv")))
        if path.endswith("/users/profile"): return self._send(200, {"clinicName": t.get("clinic", "")})
        if path.endswith("/users/onboard-date"): return self._send(200, {"data": {"onBoardDate": "2025-12-01"}})
        self._send(200, {})  # every other endpoint: empty record


server = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"

for k, v in {"LOGIN_URL": base + "/login", "VERIFY_PIN_URL": base + "/verify-pin", "VERIFY_PIN": "000000",
             "LOGIN_USERNAME_1": "demo", "LOGIN_PASSWORD_1": "demo"}.items():
    os.environ[k] = v
import global_data
global_data.base_url = base  # must be set before api_client copies it
import api_client, auth_manager
from api_client import FETCH_FAILED, fetch_patient_metrics

auth_manager._REFRESH_BUFFER_SECS = 1
api_client._MAX_ATTEMPTS, api_client._BACKOFF_BASE_SECS = 5, 0.2

# CSV column -> metric returned by the API client
FIELDS = {"wellness_clinical": "clinical_ws_dashboard", "wellness_lifestyle": "lifestyle_ws_dashboard",
          "rhr": "RHR_dashboard", "hrv": "hrv_val_dashboard", "clinic": "clinic_dashboard"}
stats = {"mismatch": 0, "failed": 0}


def same(a, b):
    try: return int(float(a)) == int(float(b))
    except (TypeError, ValueError): return str(a).strip().lower() == str(b).strip().lower()


def check(row):
    uid = row["user_id"]
    m = fetch_patient_metrics(int(uid), credential_profile="1")
    for csv_col, key in FIELDS.items():
        dash = m.get(key)
        if dash is FETCH_FAILED:
            stats["failed"] += 1
            print(f"🚫 FETCH_FAILED → user_id {uid} | {csv_col} | not compared ({m['_fetch_failures'][key]})")
        elif not same(row[csv_col], dash):
            stats["mismatch"] += 1
            print(f"⚠️  Mismatch → user_id {uid} | CSV {csv_col}: {row[csv_col]} | Dashboard {csv_col}: {dash}")


start = time.time()
with open("sample_data/demo_snapshot.csv", newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(check, rows))
print(f"\nChecked {len(rows)} users in {time.time() - start:.1f}s | "
      f"{stats['mismatch']} mismatches | {stats['failed']} fetch failures (not compared)")
assert stats == {"mismatch": 3, "failed": 1}, stats  # the planted values, nothing more
print("demo ok: exactly the 3 planted mismatches and 1 failed fetch were reported")
