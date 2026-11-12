"""Tests for vin_decoder. Run with: pytest

NHTSA is never contacted: each test supplies a fake NHTSA reply.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

import vin_decoder

VIN = "1HGCM82633A004352"

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


@pytest.fixture
def api():
    """Yield a function `get(path, nhtsa_reply)` that calls our API.

    `nhtsa_reply` is what the fake NHTSA returns: an httpx.Response,
    an exception to raise, or any JSON-serializable value.
    The last request sent to NHTSA is stored on `get.upstream_request`
    (None if NHTSA was not called).
    """

    def get(path, nhtsa_reply=None):
        get.upstream_request = None

        def handler(request):
            get.upstream_request = request
            if isinstance(nhtsa_reply, Exception):
                raise nhtsa_reply
            if isinstance(nhtsa_reply, httpx.Response):
                return nhtsa_reply
            return httpx.Response(200, json=nhtsa_reply)

        fake_client = httpx.AsyncClient(
            base_url=vin_decoder.NHTSA_BASE_URL,
            transport=httpx.MockTransport(handler),
        )
        vin_decoder.app.dependency_overrides[vin_decoder.get_nhtsa_client] = lambda: fake_client
        return test_client.get(path)

    with TestClient(vin_decoder.app) as test_client:
        yield get
    vin_decoder.app.dependency_overrides.clear()


def results(**changes):
    """An NHTSA reply holding GOOD_RECORD with the given fields changed."""
    return {"Results": [{**GOOD_RECORD, **changes}]}


# --- Successful decodes -----------------------------------------------------


def test_decodes_vin(api):
    response = api(f"/vin/{VIN}", results())

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
    response = api(f"/vin/{VIN.lower()}", results())

    assert response.json()["vin"] == VIN
    assert api.upstream_request.url.path.endswith(f"/DecodeVinValues/{VIN}")


def test_empty_optional_fields_become_null(api):
    body = api(f"/vin/{VIN}", results(Trim="", BodyClass=None)).json()

    assert body["trim"] is None
    assert body["bodyStyle"] is None


def test_model_year_is_passed_to_nhtsa(api):
    api(f"/vin/{VIN}?modelYear=2003", results())

    assert api.upstream_request.url.params["modelyear"] == "2003"
    assert api.upstream_request.url.params["format"] == "json"


def test_model_year_is_omitted_by_default(api):
    api(f"/vin/{VIN}", results())

    assert "modelyear" not in api.upstream_request.url.params


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
    response = api(f"/vin/{bad_vin}", results())

    assert response.status_code == 400
    assert api.upstream_request is None


def test_path_traversal_never_reaches_nhtsa(api):
    # The encoded slashes mean no route matches at all.
    response = api("/vin/..%2F..%2Fsecret", results())

    assert response.status_code == 404
    assert api.upstream_request is None


@pytest.mark.parametrize("bad_year", ["1980", "2101", "abc"])
def test_invalid_model_year_is_rejected(api, bad_year):
    response = api(f"/vin/{VIN}?modelYear={bad_year}", results())

    assert response.status_code == 422
    assert api.upstream_request is None


# --- NHTSA could not decode the VIN -----------------------------------------


@pytest.mark.parametrize("error_code", ["1", "1,400", "0,14"])
def test_unaccepted_error_code_returns_422(api, error_code):
    response = api(f"/vin/{VIN}", results(ErrorCode=error_code, ErrorText="Bad VIN"))

    assert response.status_code == 422
    assert response.json()["detail"] == "Bad VIN"


def test_accepted_error_codes_can_be_extended(api, monkeypatch):
    monkeypatch.setattr(vin_decoder, "ACCEPTED_ERROR_CODES", {"0", "14"})

    assert api(f"/vin/{VIN}", results(ErrorCode="0,14")).status_code == 200


@pytest.mark.parametrize(
    "missing",
    [{"Make": ""}, {"Model": None}, {"ModelYear": ""}, {"ModelYear": "abc"}],
)
def test_missing_required_field_returns_422(api, missing):
    assert api(f"/vin/{VIN}", results(**missing)).status_code == 422


# --- NHTSA failed or returned garbage ---------------------------------------


@pytest.mark.parametrize(
    "nhtsa_reply",
    [
        httpx.Response(500),
        httpx.Response(302, headers={"Location": "https://example.com/"}),
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
        "not-json",
        "timeout",
        "empty-results",
        "bad-record",
        "no-results-key",
        "not-an-object",
    ],
)
def test_upstream_problem_returns_502(api, nhtsa_reply):
    assert api(f"/vin/{VIN}", nhtsa_reply).status_code == 502
