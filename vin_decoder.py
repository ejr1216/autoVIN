"""VIN Decoder API: a thin wrapper around the NHTSA vPIC VIN decoder.

Run:   uvicorn vin_decoder:app
Try:   GET /v1/vin/1HGCM82633A004352
Docs:  GET /docs
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Annotated, Any

import httpx
import redis.asyncio as redis_client
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from redis.exceptions import RedisError

# --------------------------------------------------------------------------
# Settings. Everything you are likely to change lives in this block.
# Anything read with os.getenv can be set as an environment variable.
# --------------------------------------------------------------------------

# --- Access control ---
# Comma-separated list of API keys callers must send in the X-API-Key header,
# for example API_KEYS="key-for-team-a,key-for-team-b". Leave unset to allow
# anyone (only do that behind a gateway that already authenticates callers).
# Never write real keys in this file.
API_KEYS = frozenset(key.strip() for key in os.getenv("API_KEYS", "").split(",") if key.strip())

# Requests allowed per caller per minute. A caller is an API key, or an IP
# address when no valid key is sent. 0 turns rate limiting off.
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))

# Address of a Redis server, for example REDIS_URL="rediss://:password@host:6379/0".
# Set it when you run more than one copy of this service, so that all copies
# share one request count per caller. Leave unset to count in memory (fine
# for a single copy). The address can contain a password: never write it here.
REDIS_URL = os.getenv("REDIS_URL", "")

# --- NHTSA connection ---
NHTSA_BASE_URL = os.getenv("NHTSA_BASE_URL", "https://vpic.nhtsa.dot.gov/api/vehicles/")
NHTSA_TIMEOUT_SECONDS = float(os.getenv("NHTSA_TIMEOUT_SECONDS", "10"))

# How many extra attempts to make when NHTSA times out, drops the connection
# or answers with one of RETRY_STATUS_CODES. 0 turns retries off.
NHTSA_MAX_RETRIES = int(os.getenv("NHTSA_MAX_RETRIES", "2"))
# Wait before the first retry; the wait doubles for each later retry.
RETRY_BACKOFF_SECONDS = float(os.getenv("RETRY_BACKOFF_SECONDS", "0.5"))
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- VIN rules ---
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

# --- Response fields ---
# Optional response fields: our field name -> NHTSA field name.
# To expose another NHTSA value, add a line here and a matching
# `name: str | None = None` field on VehicleDetails below.
OPTIONAL_FIELDS = {
    "trim": "Trim",
    "body_style": "BodyClass",
}
# --------------------------------------------------------------------------


class VehicleDetails(BaseModel):
    # Python names are snake_case; the JSON output is camelCase (modelYear, bodyStyle).
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    vin: str
    model_year: int
    make: str
    model: str
    trim: str | None = None
    body_style: str | None = None


# --- Logging: one JSON object per line, tagged with the request ID ----------

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Request IDs supplied by callers are only trusted if they look harmless.
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


logger = logging.getLogger("vin_decoder")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(JsonLogFormatter())
    logger.addHandler(_handler)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False


# --- App setup --------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEYS:
        logger.warning("API_KEYS is not set: the API accepts requests from anyone.")
    # Short timeouts so a Redis outage slows requests by a second at most.
    app.state.redis = (
        redis_client.from_url(REDIS_URL, socket_timeout=1, socket_connect_timeout=1)
        if REDIS_URL
        else None
    )
    # One shared client for the whole app, so connections are reused.
    async with httpx.AsyncClient(
        base_url=NHTSA_BASE_URL,
        timeout=NHTSA_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as client:
        app.state.nhtsa_client = client
        yield
    if app.state.redis is not None:
        await app.state.redis.aclose()


app = FastAPI(title="VIN Decoder API", version="2.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Give every request an ID, log it, and add safe default headers."""
    supplied = request.headers.get("X-Request-ID", "")
    request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else uuid.uuid4().hex
    request_id_var.set(request_id)

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error", extra={"fields": {"path": request.url.path}})
        raise

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Query strings and headers are deliberately not logged (they can hold keys).
    logger.info(
        "request",
        extra={
            "fields": {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        },
    )
    return response


# --- Access control: API keys and rate limiting -----------------------------


class RateLimiter:
    """Counts requests per caller in the current minute, in memory.

    Each server process has its own counts. It is used when REDIS_URL is not
    set, and as a fallback when Redis cannot be reached.
    """

    def __init__(self) -> None:
        self._minute = 0
        self._counts: dict[str, int] = {}

    def allow(self, caller: str, limit: int) -> bool:
        minute = int(time.time() // 60)
        if minute != self._minute:
            self._minute, self._counts = minute, {}
        self._counts[caller] = self._counts.get(caller, 0) + 1
        return self._counts[caller] <= limit


rate_limiter = RateLimiter()


async def within_rate_limit(request: Request, caller: str, limit: int) -> bool:
    """Count this request and say whether the caller is still within the limit."""
    shared_store = getattr(request.app.state, "redis", None)
    if shared_store is not None:
        try:
            # One counter per caller per minute, shared by every copy of the service.
            key = f"vin-decoder:rate:{int(time.time() // 60)}:{caller}"
            async with shared_store.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, 120)  # Redis deletes old counters by itself
                count, _ = await pipe.execute()
            return count <= limit
        except (RedisError, OSError) as error:
            # Keep serving, with this process's own count, rather than fail.
            # Only the error type is logged: the message can contain the address.
            logger.warning(
                "Redis unavailable (%s); using the in-memory rate limit.", type(error).__name__
            )
    return rate_limiter.allow(caller, limit)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def protect(
    request: Request,
    api_key: Annotated[str | None, Depends(api_key_header)],
) -> None:
    """Enforce the rate limit, then the API key (when API_KEYS is set)."""
    key_is_valid = api_key is not None and any(
        hmac.compare_digest(api_key.encode(), known.encode()) for known in API_KEYS
    )
    if key_is_valid:
        caller = "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
    else:
        caller = "ip:" + (request.client.host if request.client else "unknown")

    # Rate limit first, so guessing API keys is throttled too.
    if RATE_LIMIT_PER_MINUTE > 0 and not await within_rate_limit(
        request, caller, RATE_LIMIT_PER_MINUTE
    ):
        seconds_left = 60 - int(time.time() % 60)
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Try again shortly.",
            headers={"Retry-After": str(seconds_left)},
        )

    if API_KEYS and not key_is_valid:
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")


# --- VIN decoding -----------------------------------------------------------


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
    """Call NHTSA (retrying temporary failures) and return its record, or raise 502."""
    params = {"format": "json"}
    if model_year is not None:
        params["modelyear"] = str(model_year)

    unavailable = HTTPException(status_code=502, detail="NHTSA VIN service is unavailable.")
    attempts = NHTSA_MAX_RETRIES + 1
    response = None

    for attempt in range(1, attempts + 1):
        if attempt > 1:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 2))
        try:
            # `vin` has already passed VIN_PATTERN, so it is safe to put in the path.
            response = await client.get(f"DecodeVinValues/{vin}", params=params)
        except httpx.TransportError as error:  # timeouts and connection problems
            problem = repr(error)
        except httpx.HTTPError as error:  # anything else is not worth retrying
            logger.warning("NHTSA request failed: %r", error)
            raise unavailable from error
        else:
            if response.status_code not in RETRY_STATUS_CODES:
                break
            problem = f"HTTP {response.status_code}"
        logger.warning("NHTSA attempt %d of %d failed: %s", attempt, attempts, problem)
    else:
        raise unavailable

    if response.status_code != 200:
        logger.warning("NHTSA answered with HTTP %d", response.status_code)
        raise unavailable

    try:
        record = response.json()["Results"][0]
        if not isinstance(record, dict):
            raise TypeError("unexpected result shape")
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


# --- Routes -----------------------------------------------------------------

# Everything under /v1 requires `protect`. For a breaking change, add a /v2
# router next to this one and leave /v1 working for existing callers.
v1 = APIRouter(prefix="/v1", dependencies=[Depends(protect)])


@v1.get(
    "/vin/{vin}",
    response_model=VehicleDetails,
    responses={
        400: {"description": "VIN is not 17 valid characters."},
        401: {"description": "Missing or invalid API key."},
        422: {"description": "VIN could not be decoded."},
        429: {"description": "Rate limit exceeded."},
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


app.include_router(v1)


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    """Liveness check for load balancers. Needs no API key and never calls NHTSA."""
    return {"status": "ok"}
