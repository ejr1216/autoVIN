# VIN Decoder API

A small web API that turns a 17-character Vehicle Identification Number (VIN)
into the vehicle's year, make, model, trim and body style. It looks the VIN up
in the free, public [NHTSA vPIC](https://vpic.nhtsa.dot.gov/api/) service, so
no API key or account is needed.

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
curl http://127.0.0.1:8000/vin/1HGCM82633A004352
```

```json
{
  "vin": "1HGCM82633A004352",
  "modelYear": 2003,
  "make": "HONDA",
  "model": "Accord",
  "trim": "EX",
  "bodyStyle": "Coupe"
}
```

## API

### `GET /vin/{vin}`

| Input       | Where | Required | Notes                                                         |
| ----------- | ----- | -------- | ------------------------------------------------------------- |
| `vin`       | path  | yes      | 17 letters and digits. The letters I, O and Q are not allowed. |
| `modelYear` | query | no       | 1981 to 2100. Helps NHTSA decode ambiguous VINs.               |

| Status | Meaning                                                        |
| ------ | -------------------------------------------------------------- |
| 200    | Decoded. `trim` and `bodyStyle` are `null` when NHTSA has none. |
| 400    | The VIN is not 17 valid characters.                             |
| 422    | NHTSA could not decode the VIN, or `modelYear` is out of range. |
| 502    | NHTSA is down or returned something unexpected.                 |

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

It appears in the JSON as `fuelType`.

**Accept more VINs.** By default only a completely clean decode is accepted
(NHTSA error code `0`). NHTSA often returns usable data with other codes, such
as `14` (some positions unknown). Add codes to `ACCEPTED_ERROR_CODES` to accept
them. Be careful with code `1` (check digit wrong): a mistyped VIN can decode to
the wrong vehicle.

**Change the timeout or NHTSA address.** Set environment variables; no code
change is needed.

| Variable                | Default                                   |
| ----------------------- | ----------------------------------------- |
| `NHTSA_BASE_URL`        | `https://vpic.nhtsa.dot.gov/api/vehicles/` |
| `NHTSA_TIMEOUT_SECONDS` | `10`                                      |

## Tests

```bash
pytest
```

The tests never contact NHTSA; each one supplies a fake reply. Run them after
any change, including a dependency update.

## Updating dependencies

Versions are pinned in `requirements.txt`. To update, change a version number,
reinstall, and run the tests.

## Security notes

- The VIN is checked against a strict pattern before it is used in the request
  to NHTSA, so callers cannot change where that request goes.
- The project needs no secrets. If you add any (for example an API key check),
  keep them in environment variables and never commit them. `.gitignore`
  already excludes `.env` files.
- There is **no authentication and no rate limiting**. Before putting this on
  the public internet, add rate limiting (at your reverse proxy, or with a
  library such as `slowapi`) so it cannot be used to flood NHTSA, and add
  authentication if it is not meant to be public.
- Only set `NHTSA_BASE_URL` to an address you trust; the API returns whatever
  that server says.
