# KMA provider failure contract

A provider failure must leave weather unknown; it must not produce clear skies,
zero rain probability or a successful collection marker. The scheduler continues
other grids and retries within its existing bounded polling window.

Transport diagnostics use exception types and chained causes only: DNS_ERROR,
TLS_ERROR, CONNECT/READ/WRITE/POOL_TIMEOUT, CONNECT_ERROR, PROTOCOL_ERROR,
TIMEOUT or TRANSPORT_ERROR. HTTP failures report HTTP_<status>. A 200 response
which cannot be decoded as JSON reports NON_JSON with a fixed message and no
chained decode exception. Never log response bodies, service keys or request URLs.
The code identifies the observed failure stage; it does not prove a provider
outage or an invalid key without additional evidence.

Short forecasts collect at most five pages before storage. Every page must have
an integer totalCount (1–10 decimal digits); it must be stable across the response
pages. Missing, malformed, changed, mismatched or duplicate rows fail collection.
The API adapter returns no partial edition for storage. HTTP 429 retains exactly
one existing bounded retry; there is no new background collector or live call.

Official sources rechecked 2026-09-08:
- https://www.data.go.kr/data/15084084/openapi.do (guide 2607)
- https://www.data.go.kr/data/15059468/openapi.do (guide 241128)

The guides distinguish KST source time, forecast target time and collection time.
Nowcast is current weather only. Short/mid publication selection, expiry,
region-specific queries, overlap and missing-value behavior retain the existing
stored-forecast contract and regression tests.

Validation: `python -m pytest -q tests/test_kma_forecast_contract.py` plus the
weather/forecast/polling/OSRM suites. These are synthetic provider tests, not live
KMA collection proof. Release acceptance must combine the candidate source with
R7's bounded live collector evidence and R1's period/region UI verification.
