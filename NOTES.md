# Gym Membership Invoice API

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-test.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

The health endpoint is `GET /health`.

## Run tests

```bash
pytest -q
```

## Run with Docker

```bash
docker build -t app .
docker run --rm -p 8080:8080 app
docker build --target test -t app-test .
```

## Scope

All three parts are implemented. No routes or scoring rules were intentionally skipped. State is held in process memory and starts empty on each container run. The API performs no runtime network access.

## Assumptions

- Python 3.12, FastAPI, and Uvicorn are used for the HTTP service.
- Generated member, agreement, and reconciliation-run IDs are UUID strings.
- Plan IDs and caller-supplied names are preserved exactly except member names, which are trimmed.
- Phone cleanup removes ASCII spaces, hyphens, and round brackets; other characters are rejected.
- Currency values on gateway rows are limited to AED, SAR, and BHD.
- A row with a malformed field uses `422 VALIDATION`; the specified row-rule violations use `422 INVALID_ROWS` and include every applicable row error.
- The reconciliation candidate tie-break is earliest due date and then invoice number.
- A maximum 366-day query range means the difference between `from` and `to` is at most 366 days.
