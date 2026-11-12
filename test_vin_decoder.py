"""Tests for vin_decoder.

Run with:            pytest
Also hit real NHTSA: pytest -m live   (needs internet access)

Except for the one "live" test, NHTSA is never contacted: each test
supplies a fake NHTSA reply.
"""

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

import vin_decoder

VIN = "1HGCM82633A004352"
VIN_URL = f"/v1/vin/{VIN}"

# A clean NHTSA record. Tests copy it and change one thing.
GOOD_RECORD = {
    "ErrorCode": "0",
    "ErrorText": "0 - VIN decoded clean.",
    "ModelYear": "2003",
    "Make": "HONDA",
    "Model": "Accord",
    "Trim": "EX",
    "BodyClass": "Coupe",
}


@pytest.fixture(autouse=True)
def default_settings(monkeypatch):
    """Start every test open (no API keys), unthrottled, with instant retries."""
    monkeypatch.setattr(vin_decoder, "API_KEYS", frozenset())
    monkeypatch.setattr(vin_decoder, "RATE_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(vin_decoder, "NHTSA_MAX_RETRIES", 2)
    monkeypatch.setattr(vin_decoder, "RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(vin_decoder, "rate_limiter", vin_decoder.RateLimiter())


@pytest.fixture
def api():
    """Yield a function `get(path, nhtsa_reply, headers=None)` that calls our API.

    `nhtsa_reply` is what the fake NHTSA returns: an httpx.Response, an
    exception to raise, any JSON-serializable value, or a list of those to
    return one after another (the last one repeats).
    Every request sent to NHTSA is appended to `get.upstream_requests`.
    """

    def get(path, nhtsa_reply=None, headers=None):
        get.upstream_requests = []
        replies = list(nhtsa_reply) if isinstance(nhtsa_reply, list) else [nhtsa_reply]

        def handler(request):
            get.upstream_requests.append(request)
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, httpx.Response):
                return reply
            return httpx.Response(200, json=reply)

        fake_client = httpx.AsyncClient(
            base_url=vin_decoder.NHTSA_BASE_URL,
            transport=httpx.MockTransport(handler),
        )
        vin_decoder.app.dependency_overrides[vin_decoder.get_nhtsa_client] = lambda: fake_client
        return test_client.get(path, headers=headers)

    with TestClient(vin_decoder.app) as test_client:
        yield get
    vin_decoder.app.dependency_overrides.clear()


def results(**changes):
    """An NHTSA reply holding GOOD_RECORD with the given fields changed."""
    return {"Results": [{**GOOD_RECORD, **changes}]}


# --- Successful decodes -----------------------------------------------------


def test_decodes_vin(api):
    response = api(VIN_URL, results())

    assert response.status_code == 200
    assert response.json() == {
        "vin": VIN,
        "modelYear": 2003,
        "make": "HONDA",
        "model": "Accord",
        "trim": "EX",
        "bodyStyle": "Coupe",
    }


def test_vin_is_upper_cased(api):
    response = api(f"/v1/vin/{VIN.lower()}", results())

    assert response.json()["vin"] == VIN
    assert api.upstream_requests[0].url.path.endswith(f"/DecodeVinValues/{VIN}")


def test_empty_optional_fields_become_null(api):
    body = api(VIN_URL, results(Trim="", BodyClass=None)).json()

    assert body["trim"] is None
    assert body["bodyStyle"] is None


def test_model_year_is_passed_to_nhtsa(api):
    api(f"{VIN_URL}?modelYear=2003", results())

    assert api.upstream_requests[0].url.params["modelyear"] == "2003"
    assert api.upstream_requests[0].url.params["format"] == "json"


def test_model_year_is_omitted_by_default(api):
    api(VIN_URL, results())

    assert "modelyear" not in api.upstream_requests[0].url.params


# --- Input validation (NHTSA must not be called) ----------------------------


@pytest.mark.parametrize(
    "bad_vin",
    [
        "SHORT",
        VIN + "X",  # 18 characters
        "IHGCM82633A004352",  # contains I
        "1HGCM82633A00435O",  # contains O
        "1HGCM82633A00435Q",  # contains Q
        "1HGCM82633A00435-",  # punctuation
        "1HGCM82633A0043%3F",  # encoded "?"
    ],
)
def test_invalid_vin_is_rejected(api, bad_vin):
    response = api(f"/v1/vin/{bad_vin}", results())

    assert response.status_code == 400
    assert api.upstream_requests == []


def test_path_traversal_never_reaches_nhtsa(api):
    # The encoded slashes mean no route matches at all.
    response = api("/v1/vin/..%2F..%2Fsecret", results())

    assert response.status_code == 404
    assert api.upstream_requests == []


@pytest.mark.parametrize("bad_year", ["1980", "2101", "abc"])
def test_invalid_model_year_is_rejected(api, bad_year):
    response = api(f"{VIN_URL}?modelYear={bad_year}", results())

    assert response.status_code == 422
    assert api.upstream_requests == []


# --- NHTSA could not decode the VIN -----------------------------------------


@pytest.mark.parametrize("error_code", ["1", "1,400", "0,14"])
def test_unaccepted_error_code_returns_422(api, error_code):
    response = api(VIN_URL, results(ErrorCode=error_code, ErrorText="Bad VIN"))

    assert response.status_code == 422
    assert response.json()["detail"] == "Bad VIN"


def test_accepted_error_codes_can_be_extended(api, monkeypatch):
    monkeypatch.setattr(vin_decoder, "ACCEPTED_ERROR_CODES", {"0", "14"})

    assert api(VIN_URL, results(ErrorCode="0,14")).status_code == 200


@pytest.mark.parametrize(
    "missing",
    [{"Make": ""}, {"Model": None}, {"ModelYear": ""}, {"ModelYear": "abc"}],
)
def test_missing_required_field_returns_422(api, missing):
    assert api(VIN_URL, results(**missing)).status_code == 422


# --- NHTSA failed or returned garbage ---------------------------------------


@pytest.mark.parametrize(
    "nhtsa_reply",
    [
        httpx.Response(500),
        httpx.Response(302, headers={"Location": "https://example.com/"}),
        httpx.Response(404),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.ConnectTimeout("timed out"),
        {"Results": []},
        {"Results": ["not a record"]},
        {"unexpected": "shape"},
        ["a", "list"],
    ],
    ids=[
        "server-error",
        "redirect",
        "not-found",
        "not-json",
        "timeout",
        "empty-results",
        "bad-record",
        "no-results-key",
        "not-an-object",
    ],
)
def test_upstream_problem_returns_502(api, nhtsa_reply):
    # A one-item list so the "not-an-object" case is sent as-is, not as a sequence.
    assert api(VIN_URL, [nhtsa_reply]).status_code == 502


# --- Retries ----------------------------------------------------------------


@pytest.mark.parametrize(
    "temporary_failure",
    [httpx.Response(503), httpx.Response(429), httpx.ReadTimeout("slow"), httpx.ConnectError("down")],
    ids=["503", "429", "read-timeout", "connect-error"],
)
def test_temporary_failure_is_retried(api, temporary_failure):
    response = api(VIN_URL, [temporary_failure, temporary_failure, results()])

    assert response.status_code == 200
    assert len(api.upstream_requests) == 3


def test_gives_up_after_max_retries(api):
    response = api(VIN_URL, [httpx.Response(503)])

    assert response.status_code == 502
    assert len(api.upstream_requests) == 3  # 1 try + 2 retries


def test_retries_can_be_turned_off(api, monkeypatch):
    monkeypatch.setattr(vin_decoder, "NHTSA_MAX_RETRIES", 0)

    assert api(VIN_URL, [httpx.Response(503)]).status_code == 502
    assert len(api.upstream_requests) == 1


@pytest.mark.parametrize(
    "permanent_failure",
    [httpx.Response(404), httpx.Response(200, text="not json"), results(ErrorCode="1")],
    ids=["404", "not-json", "undecodable-vin"],
)
def test_permanent_failure_is_not_retried(api, permanent_failure):
    api(VIN_URL, [permanent_failure])

    assert len(api.upstream_requests) == 1


def test_retry_waits_get_longer(api, monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(vin_decoder, "RETRY_BACKOFF_SECONDS", 0.5)
    monkeypatch.setattr(vin_decoder.asyncio, "sleep", fake_sleep)

    api(VIN_URL, [httpx.Response(503)])

    assert waits == [0.5, 1.0]


# --- API keys ---------------------------------------------------------------


def test_no_key_needed_when_api_keys_unset(api):
    assert api(VIN_URL, results()).status_code == 200


@pytest.mark.parametrize("headers", [None, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_missing_or_wrong_key_is_rejected(api, monkeypatch, headers):
    monkeypatch.setattr(vin_decoder, "API_KEYS", frozenset({"key-one", "key-two"}))

    response = api(VIN_URL, results(), headers=headers)

    assert response.status_code == 401
    assert api.upstream_requests == []


@pytest.mark.parametrize("key", ["key-one", "key-two"])
def test_valid_key_is_accepted(api, monkeypatch, key):
    monkeypatch.setattr(vin_decoder, "API_KEYS", frozenset({"key-one", "key-two"}))

    assert api(VIN_URL, results(), headers={"X-API-Key": key}).status_code == 200


# --- Rate limiting ----------------------------------------------------------


def test_rate_limit_blocks_extra_requests(api, monkeypatch):
    monkeypatch.setattr(vin_decoder, "RATE_LIMIT_PER_MINUTE", 2)

    assert api(VIN_URL, results()).status_code == 200
    assert api(VIN_URL, results()).status_code == 200
    blocked = api(VIN_URL, results())

    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert api.upstream_requests == []


def test_each_api_key_has_its_own_limit(api, monkeypatch):
    monkeypatch.setattr(vin_decoder, "API_KEYS", frozenset({"key-one", "key-two"}))
    monkeypatch.setattr(vin_decoder, "RATE_LIMIT_PER_MINUTE", 1)

    assert api(VIN_URL, results(), headers={"X-API-Key": "key-one"}).status_code == 200
    assert api(VIN_URL, results(), headers={"X-API-Key": "key-one"}).status_code == 429
    assert api(VIN_URL, results(), headers={"X-API-Key": "key-two"}).status_code == 200


def test_key_guessing_is_rate_limited(api, monkeypatch):
    monkeypatch.setattr(vin_decoder, "API_KEYS", frozenset({"key-one"}))
    monkeypatch.setattr(vin_decoder, "RATE_LIMIT_PER_MINUTE", 2)

    statuses = [
        api(VIN_URL, results(), headers={"X-API-Key": f"guess-{n}"}).status_code for n in range(3)
    ]

    assert statuses == [401, 401, 429]


def test_limit_resets_each_minute(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(vin_decoder.time, "monotonic", lambda: now[0])
    limiter = vin_decoder.RateLimiter()

    assert limiter.allow("caller", 1) is True
    assert limiter.allow("caller", 1) is False
    now[0] = 61.0
    assert limiter.allow("caller", 1) is True


# --- Health, request IDs, logging -------------------------------------------


def test_health_needs_no_key_and_is_not_rate_limited(api, monkeypatch):
    monkeypatch.setattr(vin_decoder, "API_KEYS", frozenset({"key-one"}))
    monkeypatch.setattr(vin_decoder, "RATE_LIMIT_PER_MINUTE", 1)

    for _ in range(3):
        response = api("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    assert api.upstream_requests == []


def test_response_has_request_id_and_safe_headers(api):
    response = api(VIN_URL, results())

    assert len(response.headers["X-Request-ID"]) == 32
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_caller_request_id_is_kept(api):
    response = api(VIN_URL, results(), headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"


def test_unsafe_request_id_is_replaced(api):
    response = api(VIN_URL, results(), headers={"X-Request-ID": 'bad id"{}'})

    assert response.headers["X-Request-ID"] != 'bad id"{}'


def test_requests_are_logged_as_json_without_secrets(api, monkeypatch):
    monkeypatch.setattr(vin_decoder, "API_KEYS", frozenset({"very-secret-key"}))
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(vin_decoder.JsonLogFormatter().format(record))

    capture = Capture()
    vin_decoder.logger.addHandler(capture)
    try:
        api(VIN_URL, results(), headers={"X-API-Key": "very-secret-key", "X-Request-ID": "req-1"})
    finally:
        vin_decoder.logger.removeHandler(capture)

    entry = json.loads(lines[-1])
    assert entry["message"] == "request"
    assert entry["request_id"] == "req-1"
    assert entry["path"] == VIN_URL
    assert entry["status"] == 200
    assert "very-secret-key" not in "".join(lines)


# --- Live check against the real NHTSA service ------------------------------


@pytest.mark.live
def test_live_nhtsa_decodes_a_known_vin():
    """Not run by default. Run with: pytest -m live"""
    with TestClient(vin_decoder.app) as client:
        response = client.get(VIN_URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["make"] == "HONDA"
    assert body["model"] == "Accord"
    assert body["modelYear"] == 2003
