import base64
import json
import threading
import time
import requests
from dotenv import load_dotenv
import os

load_dotenv()

_LOGIN_URL = os.getenv("LOGIN_URL")
_VERIFY_PIN_URL = os.getenv("VERIFY_PIN_URL")
_VERIFY_PIN = os.getenv("VERIFY_PIN")

# Credentials per profile. To add another account, add an entry here and the
# matching LOGIN_USERNAME_<n> / LOGIN_PASSWORD_<n> pair to .env.
_PROFILES = {
    "1": {
        "username": os.getenv("LOGIN_USERNAME_1"),
        "password": os.getenv("LOGIN_PASSWORD_1"),
    },
}

# Separate token cache per profile
_token_store: dict = {
    p: {"token": "", "exp": 0.0, "user_entity": "", "fail_until": 0.0, "fail_reason": ""}
    for p in _PROFILES
}
_lock = threading.Lock()

# After a failed login, refuse to call the login endpoint again for this long.
# Parallel workers otherwise stampede it — and if the failure was a throttle or
# lockout, retrying is exactly what keeps it locked.
_LOGIN_COOLDOWN_SECS = 30


class AuthenticationError(RuntimeError):
    """Login itself failed: bad credentials, a lockout, or the auth service is down."""

# Refresh token this many seconds before it actually expires.
# Must comfortably exceed the wall-clock time a single patient can take to
# process, otherwise a token that looked "valid" at the start of a patient can
# die halfway through it.
_REFRESH_BUFFER_SECS = 300

# The API answers an expired/invalid token with 403 (not 401), so both must be
# treated as "re-authenticate and retry".
AUTH_ERROR_STATUSES = (401, 403)

# Fallback lifetime used only if a token's `exp` claim cannot be decoded.
# Without this, exp stays 0.0 and every single call would trigger a fresh login.
_FALLBACK_TTL_SECS = 25 * 60


def _decode_exp(token: str) -> float:
    """Decode the exp claim from a JWT without verifying signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return float(decoded.get("exp", 0))
    except Exception:
        return 0.0


def _fetch_new_token(profile: str) -> tuple:
    """Two-step auth for a given profile: login → tempToken + patientId, then verify PIN → auth token."""
    creds = _PROFILES.get(profile)
    if not creds or not creds["username"] or not creds["password"]:
        raise EnvironmentError(
            f"LOGIN_USERNAME_{profile} and LOGIN_PASSWORD_{profile} must be set in .env"
        )
    if not all([_LOGIN_URL, _VERIFY_PIN_URL, _VERIFY_PIN]):
        raise EnvironmentError(
            "LOGIN_URL, VERIFY_PIN_URL, VERIFY_PIN must be set in .env"
        )

    # Step 1: Login
    login_response = requests.post(
        _LOGIN_URL,
        json={"userId": creds["username"], "password": creds["password"]},
        timeout=15,
    )
    login_response.raise_for_status()
    login_data = login_response.json()

    temp_token = login_data.get("tempToken")
    patient_id = login_data.get("patientId")
    user_entity = login_data.get("userId", "")

    if not temp_token:
        raise ValueError(f"tempToken not found in login response: {login_data}")
    if not patient_id:
        raise ValueError(f"patientId not found in login response: {login_data}")

    # Step 2: Verify PIN
    pin_response = requests.post(
        _VERIFY_PIN_URL,
        json={"patientId": patient_id, "pin": _VERIFY_PIN},
        headers={"Authorization": f"Bearer {temp_token}", "Content-Type": "application/json"},
        timeout=15,
    )
    pin_response.raise_for_status()
    pin_data = pin_response.json()

    token = pin_data.get("token")
    if not token:
        raise ValueError(f"token not found in verify PIN response: {pin_data}")

    return token, user_entity


def get_valid_token(profile: str = "1") -> str:
    """
    Return a valid token for the given profile. Fetches a new one via two-step
    login if the current token is missing or within REFRESH_BUFFER_SECS of expiry.
    Thread-safe.
    """
    store = _token_store[profile]

    if time.time() < store["exp"] - _REFRESH_BUFFER_SECS:
        return store["token"]

    with _lock:
        # Re-check inside lock in case another thread already refreshed
        if time.time() < store["exp"] - _REFRESH_BUFFER_SECS:
            return store["token"]

        now = time.time()
        if now < store["fail_until"]:
            raise AuthenticationError(
                f"login for profile {profile} failed {int(now - (store['fail_until'] - _LOGIN_COOLDOWN_SECS))}s ago "
                f"({store['fail_reason']}); not retrying for another {int(store['fail_until'] - now)}s"
            )

        print(f"🔑 Fetching new auth token (profile {profile})...")
        try:
            token, user_entity = _fetch_new_token(profile)
        except Exception as exc:
            store["fail_until"] = time.time() + _LOGIN_COOLDOWN_SECS
            store["fail_reason"] = f"{type(exc).__name__}: {exc}"
            raise AuthenticationError(
                f"login failed for profile {profile}: {exc}"
            ) from exc

        store["fail_until"] = 0.0
        store["fail_reason"] = ""
        exp = _decode_exp(token)
        if not exp:
            exp = time.time() + _FALLBACK_TTL_SECS
            print(f"⚠️  Could not decode token exp (profile {profile}); assuming {_FALLBACK_TTL_SECS // 60} minutes.")
        store["token"] = token
        store["exp"] = exp
        store["user_entity"] = user_entity
        print(f"🔑 Token refreshed (profile {profile}), valid for {int((store['exp'] - time.time()) / 60)} minutes.")

    return store["token"]


def invalidate(profile: str = "1", token: str = None) -> bool:
    """
    Drop the cached token so the next get_valid_token() performs a real login.

    Needed when the server rejects a token we still consider valid (clock skew,
    server-side revocation). Without this, a retry after a rejection would just
    replay the same rejected token.

    Pass the token that was actually rejected. It is only cleared if it is still
    the cached one: with many workers in parallel they all get rejected at about
    the same moment, and without this check each would wipe the fresh token the
    previous one just fetched, ping-ponging between login and invalidate.

    Returns True if the cached token was cleared.
    """
    with _lock:
        store = _token_store[profile]
        if token is not None and store["token"] != token:
            return False  # already replaced by another thread; keep the new one
        store["token"] = ""
        store["exp"] = 0.0
        return True


class BearerAuth(requests.auth.AuthBase):
    """
    Stamps a currently-valid token on every request at send time.

    This replaces pinning `Authorization` into session.headers once: a session
    that lives for minutes would otherwise keep sending the token it captured
    when it was created, long after that token expired.
    """

    def __init__(self, profile: str = "1"):
        self.profile = profile

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {get_valid_token(self.profile)}"
        return request


def new_session(profile: str = "1") -> requests.Session:
    """A session that always authenticates with a fresh token."""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    session.auth = BearerAuth(profile)
    return session


def get_user_entity(profile: str = "1") -> str:
    """Return the userId for the given profile. Triggers a token fetch if not yet loaded."""
    get_valid_token(profile)
    return _token_store[profile]["user_entity"]
