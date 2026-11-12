# VIN Decoder API

A small web API that turns a 17-character Vehicle Identification Number (VIN)
into the vehicle's year, make, model, trim and body style. It looks the VIN up
in the free, public [NHTSA vPIC](https://vpic.nhtsa.dot.gov/api/) service, so
no NHTSA account or key is needed.

It includes API-key authentication, rate limiting (optionally shared across
servers), automatic retries, JSON
logging with request IDs, a health check, a Docker image and automated tests.

## Quick start

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

```bash
uvicorn vin_decoder:app
```

Then open <http://127.0.0.1:8000/docs> for interactive documentation, or call
the API directly:

```bash
curl http://127.0.0.1:8000/v1/vin/1HGCM82633A004352
```

```json
{
  "vin": "1HGCM82633A004352",
  "modelYear": 2003,
  "make": "HONDA",
  "model": "Accord",
  "trim": "EX-V6",
  "bodyStyle": "Coupe"
}
```

Started this way the API is open to anyone who can reach it. See
[Going to production](#going-to-production) before exposing it.

## API

### `GET /v1/vin/{vin}`

| Input       | Where  | Required            | Notes                                                          |
| ----------- | ------ | ------------------- | -------------------------------------------------------------- |
| `vin`       | path   | yes                 | 17 letters and digits. The letters I, O and Q are not allowed. |
| `modelYear` | query  | no                  | 1981 to 2100. Helps NHTSA decode ambiguous VINs.               |
| `X-API-Key` | header | if `API_KEYS` is set | One of the configured keys.                                    |

| Status | Meaning                                                              |
| ------ | -------------------------------------------------------------------- |
| 200    | Decoded. `trim` and `bodyStyle` are `null` when NHTSA has none.       |
| 400    | The VIN is not 17 valid characters.                                   |
| 401    | The API key is missing or wrong.                                      |
| 422    | NHTSA could not decode the VIN, or `modelYear` is out of range.       |
| 429    | Too many requests. The `Retry-After` header says how long to wait.    |
| 502    | NHTSA is down or returned something unexpected, even after retrying.  |

### `GET /health`

Returns `{"status": "ok"}` while the service is running. It needs no API key,
is not rate limited and does not call NHTSA, so load balancers can poll it.

Every response carries an `X-Request-ID` header. Quote it when reporting a
problem; the same ID is on the matching log lines.

## Settings

All settings are environment variables. None are required.

| Variable                | Default                                    | What it does                                                                 |
| ----------------------- | ------------------------------------------ | ---------------------------------------------------------------------------- |
| `API_KEYS`              | not set (open to anyone)                   | Comma-separated keys callers must send in `X-API-Key`.                        |
| `RATE_LIMIT_PER_MINUTE` | `60`                                       | Requests allowed per caller per minute. `0` turns it off.                     |
| `REDIS_URL`             | not set (count in memory)                  | Redis address for a rate limit shared by all copies of the service.           |
| `NHTSA_MAX_RETRIES`     | `2`                                        | Extra attempts when NHTSA times out or returns 429/500/502/503/504.           |
| `RETRY_BACKOFF_SECONDS` | `0.5`                                      | Wait before the first retry. It doubles for each later retry.                 |
| `NHTSA_TIMEOUT_SECONDS` | `10`                                       | How long to wait for each NHTSA attempt.                                      |
| `NHTSA_BASE_URL`        | `https://vpic.nhtsa.dot.gov/api/vehicles/` | Where NHTSA lives. Only set this to an address you trust.                     |
| `LOG_LEVEL`             | `INFO`                                     | `DEBUG`, `INFO`, `WARNING` or `ERROR`.                                        |

With the defaults, a request can take up to about 31 seconds in the worst case
(three 10-second attempts plus waits) before returning 502. Lower
`NHTSA_TIMEOUT_SECONDS` or `NHTSA_MAX_RETRIES` if your callers cannot wait
that long.

## Going to production

**1. Create API keys.** Generate one long random key per caller, so you can
revoke one without affecting the others:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Give the keys to the service through the `API_KEYS` environment variable, using
your platform's secret store. Never put them in the code, the Dockerfile or
this repository.

**2. Run the Docker image.**

```bash
docker build -t vin-decoder .
```

```bash
docker run -p 8000:8000 -e API_KEYS="first-key,second-key" vin-decoder
```

The image runs as an unprivileged user and has a built-in health check.

**3. Put it behind HTTPS.** The service speaks plain HTTP. Run it behind a load
balancer or reverse proxy that handles HTTPS, otherwise API keys travel
unencrypted.

**4. Share the rate limit if you run more than one copy.** By default each
copy of the service counts requests in its own memory, so three copies would
allow three times the limit. To make all copies share one count, point them at
the same [Redis](https://redis.io/) server:

```bash
docker run -p 8000:8000 -e API_KEYS="first-key" -e REDIS_URL="rediss://:password@your-redis-host:6379/0" vin-decoder
```

- `REDIS_URL` contains the Redis password, so treat it as a secret like
  `API_KEYS`. Use `rediss://` (two s's) so the connection is encrypted.
- If Redis cannot be reached, the service keeps working and falls back to the
  in-memory count until Redis returns. A warning is logged each time.
- The service stores only short-lived counters in Redis, which delete
  themselves after two minutes.
- Callers without a valid key are counted by IP address. Behind a proxy, every
  caller appears to come from the proxy's address unless you tell uvicorn to
  trust it by setting `FORWARDED_ALLOW_IPS` to the proxy's IP address.

**5. Collect the logs.** The service writes one JSON object per line to
standard error, for example:

```json
{"time": "2026-11-12 15:22:23,404", "level": "INFO", "message": "request", "request_id": "5277c45e...", "method": "GET", "path": "/v1/vin/1HGCM82633A004352", "status": 200, "duration_ms": 583.0}
```

API keys and query strings are never logged. The VIN is logged as part of the
path; if your organization treats VINs as personal data, apply your usual log
retention rules.

## Changing things

Everything you are likely to change is in the settings block at the top of
`vin_decoder.py`.

**Add a field to the response.** NHTSA returns many more values than this API
exposes (see the [variable list](https://vpic.nhtsa.dot.gov/api/vehicles/getvehiclevariablelist?format=json)).
To expose one, for example the fuel type:

1. Add a line to `OPTIONAL_FIELDS`:

   ```python
   OPTIONAL_FIELDS = {
       "trim": "Trim",
       "body_style": "BodyClass",
       "fuel_type": "FuelTypePrimary",
   }
   ```

2. Add the matching field to `VehicleDetails`:

   ```python
   fuel_type: str | None = None
   ```

It appears in the JSON as `fuelType`. Adding a field does not break existing
callers.

**Accept more VINs.** By default only a completely clean decode is accepted
(NHTSA error code `0`). NHTSA often returns usable data with other codes, such
as `14` (some positions unknown). Add codes to `ACCEPTED_ERROR_CODES` to accept
them. Be careful with code `1` (check digit wrong): a mistyped VIN can decode to
the wrong vehicle.

**Make a breaking change.** Routes live under `/v1`. If you need to remove or
rename a field, add a `/v2` router next to the `v1` one in `vin_decoder.py` and
leave `/v1` working until callers have moved.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

These tests never contact NHTSA; each one supplies a fake reply. One extra test
performs a real lookup against NHTSA and needs internet access:

```bash
pytest -m live
```

GitHub runs the tests and builds the Docker image on every push and pull
request (see `.github/workflows/ci.yml`).

## Updating dependencies

Versions are pinned in `requirements.txt` and `requirements-dev.txt`.
Dependabot opens a pull request each week when a newer version exists; merge it
once the checks pass. To update by hand, change a version number, reinstall,
and run the tests.

## Security notes

- The VIN is checked against a strict pattern before it is used in the request
  to NHTSA, so callers cannot change where that request goes.
- API keys are compared in constant time, and wrong guesses count against the
  rate limit.
- The project stores no secrets. Keep keys in environment variables and never
  commit them. `.gitignore` already excludes `.env` files.
- Not included: caching of NHTSA results, and metrics and tracing.

## License

Released under the [Apache License 2.0](LICENSE). You may use, change and
redistribute this code, including commercially, as long as you keep the
license notice. It comes with no warranty.
