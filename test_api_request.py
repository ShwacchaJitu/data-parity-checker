"""Self-check for api_request retry behaviour. Run: python test_api_request.py"""
import sys
sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252, which cannot print the retry log emoji
import api_client
from api_client import ApiError, api_request


class _Resp:
    def __init__(self, status, body=None, bad_json=False):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self._bad_json = bad_json
        self.request = type("R", (), {"headers": {}})()

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class _Session:
    """Replays a queued list of responses, one per attempt."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def run():
    api_client.time.sleep = lambda _: None  # don't actually back off

    # A 200 with an unparseable body must retry, not silently become {}.
    s = _Session([_Resp(200, bad_json=True), _Resp(200, {"history": [{"value": "Often"}]})])
    assert api_request(s, "post", "u", profile="1") == {"history": [{"value": "Often"}]}
    assert s.calls == 2, s.calls

    # Unparseable every time -> loud failure, never a silent blank.
    s = _Session([_Resp(200, bad_json=True)] * api_client._MAX_ATTEMPTS)
    try:
        api_request(s, "post", "u", profile="1")
        raise AssertionError("expected ApiError")
    except ApiError as exc:
        assert "invalid JSON" in str(exc), exc

    # 503 rides out a blip and succeeds.
    s = _Session([_Resp(503), _Resp(503), _Resp(200, {"ok": 1})])
    assert api_request(s, "post", "u", profile="1") == {"ok": 1}

    # A CDN 5xx outside _RETRY_STATUSES must retry, not return {}.
    s = _Session([_Resp(520), _Resp(200, {"ok": 1})])
    assert api_request(s, "post", "u", profile="1") == {"ok": 1}
    assert s.calls == 2, s.calls

    # A plain 4xx still means "no such record" -> {}, unchanged.
    s = _Session([_Resp(404)])
    assert api_request(s, "post", "u", profile="1") == {}
    assert s.calls == 1, s.calls

    print("all api_request checks passed")


if __name__ == "__main__":
    run()
