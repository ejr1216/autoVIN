"""VIN Decoder API: a thin wrapper around the NHTSA vPIC VIN decoder.

Run:   uvicorn vin_decoder:app
Try:   GET /vin/1HGCM82633A004352
Docs:  GET /docs
"""

import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

# --------------------------------------------------------------------------
# Settings. Everything you are likely to change lives in this block.
# The first two can be overridden with environment variables.
# --------------------------------------------------------------------------
NHTSA_BASE_URL = os.getenv("NHTSA_BASE_URL", "https://vpic.nhtsa.dot.gov/api/vehicles/")
NHTSA_TIMEOUT_SECONDS = float(os.getenv("NHTSA_TIMEOUT_SECONDS", "10"))

# 17 characters, no I, O or Q (they are not allowed in a VIN).
VIN_PATTERN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")

# 17-character VINs started with model year 1981.
MIN_MODEL_YEAR = 1981
MAX_MODEL_YEAR = 2100

# NHTSA error codes we still treat as a successful decode. "0" means a clean
# decode. NHTSA often returns usable data with other codes too, for example
# "1" (check digit wrong) or "14" (some positions unknown). Add codes here to
# accept them. Full list: https://vpic.nhtsa.dot.gov/api/ (ErrorCode variable).
ACCEPTED_ERROR_CODES = {"0"}

# Optional response fields: our field name -> NHTSA field name.
# To expose another NHTSA value, add a line here and a matching
# `name: str | None = None` field on VehicleDetails below.
OPTIONAL_FIELDS = {
    "trim": "Trim",
    "body_style": "BodyClass",
}
# --------------------------------------------------------------------------

logger = logging.getLogger("vin_decoder")


class VehicleDetails(BaseModel):
    # Python names are snake_case; the JSON output is camelCase (modelYear, bodyStyle).
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    vin: str
    model_year: int
    make: str
    model: str
    trim: str | None = None
    body_style: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared client for the whole app, so connections are reused.
    async with httpx.AsyncClient(
        base_url=NHTSA_BASE_URL,
        timeout=NHTSA_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as client:
        app.state.nhtsa_client = client
        yield


app = FastAPI(title="VIN Decoder API", version="1.1.0", lifespan=lifespan)


def get_nhtsa_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.nhtsa_client


def normalize_vin(vin: str) -> str:
    """Return the VIN upper-cased, or raise 400 if it is not a valid shape."""
    value = vin.strip().upper()
    if not VIN_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="VIN must contain 17 valid characters.")
    return value


async def fetch_nhtsa_record(
    client: httpx.AsyncClient, vin: str, model_year: int | None
) -> dict[str, Any]:
    """Call NHTSA and return the single result record, or raise 502."""
    params = {"format": "json"}
    if model_year is not None:
        params["modelyear"] = str(model_year)

    try:
        # `vin` has already passed VIN_PATTERN, so it is safe to put in the path.
        response = await client.get(f"DecodeVinValues/{vin}", params=params)
        response.raise_for_status()
        record = response.json()["Results"][0]
        if not isinstance(record, dict):
            raise TypeError("unexpected result shape")
    except httpx.HTTPError as error:
        logger.warning("NHTSA request failed: %r", error)
        raise HTTPException(status_code=502, detail="NHTSA VIN service is unavailable.") from error
    except (ValueError, KeyError, IndexError, TypeError) as error:
        logger.warning("NHTSA returned an unexpected response: %r", error)
        raise HTTPException(status_code=502, detail="NHTSA returned no decoding data.") from error

    return record


def build_vehicle_details(vin: str, record: dict[str, Any]) -> VehicleDetails:
    """Turn an NHTSA record into our response, or raise 422."""
    codes = {code.strip() for code in str(record.get("ErrorCode", "")).split(",")}
    if not codes <= ACCEPTED_ERROR_CODES:
        raise HTTPException(
            status_code=422,
            detail=record.get("ErrorText") or "VIN could not be decoded.",
        )

    year, make, model = record.get("ModelYear"), record.get("Make"), record.get("Model")
    if not (make and model and str(year).isdigit()):
        raise HTTPException(status_code=422, detail="Make, model, or year is unavailable.")

    optional = {name: record.get(source) or None for name, source in OPTIONAL_FIELDS.items()}
    return VehicleDetails(vin=vin, model_year=int(year), make=make, model=model, **optional)


@app.get(
    "/vin/{vin}",
    response_model=VehicleDetails,
    responses={
        400: {"description": "VIN is not 17 valid characters."},
        422: {"description": "VIN could not be decoded."},
        502: {"description": "NHTSA service failed or returned bad data."},
    },
)
async def decode_vin(
    vin: str,
    client: Annotated[httpx.AsyncClient, Depends(get_nhtsa_client)],
    model_year: Annotated[
        int | None, Query(alias="modelYear", ge=MIN_MODEL_YEAR, le=MAX_MODEL_YEAR)
    ] = None,
) -> VehicleDetails:
    vin_value = normalize_vin(vin)
    record = await fetch_nhtsa_record(client, vin_value, model_year)
    return build_vehicle_details(vin_value, record)
