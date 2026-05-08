# hdo-api

HTTP API serving CEZ HDO (hromadné dálkové ovládání) tariff schedules for [evcc](https://evcc.io).

Fetches low/high tariff time windows from the CEZ distribuce API (with CAPTCHA solving via Gemini `gemini-3.1-flash-lite`), caches them locally, and serves evcc-compatible price forecasts. Supports multiple EAN numbers — each is cached independently.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package manager)
- Your 18-digit EAN number (from your electricity bill or CEZ distribution portal)

## Run locally (without Docker)

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) (install with `curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
# Clone and enter the project
git clone <repo-url> && cd evcc-tariff

# Install dependencies
uv sync

# Start the server (cache files will be saved in the current directory)
CACHE_DIR=. uv run uvicorn main:app --port 8080
```

The server is now available at `http://localhost:8080`. Test it:

```bash
curl "http://localhost:8080/api/rates?ean=859182400000000000&vt=6.20&nt=2.80"
```

## Run with Docker

```bash
docker compose up -d
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | **Yes** | — | Google Gemini API key (used to solve the CEZ CAPTCHA via `gemini-3.1-flash-lite`) |
| `CACHE_DIR` | No | `/data` | Directory for persistent cache files |
| `LOW_TARIFF_WINDOWS` | No | — | Fallback schedule if CEZ API is unavailable, e.g. `00:00-05:00;06:00-08:30` |

## API endpoints

All endpoints accept the EAN number as a query parameter. On first request for a given EAN, the server fetches the HDO schedule from CEZ (this may take a few seconds). Subsequent requests are served from cache.

### `GET /api/rates?ean=859182400000000000&vt=6.20&nt=2.80`

Returns a JSON array of variable-length time slots for today and tomorrow, with boundaries aligned to the actual HDO switching transitions. evcc's `forecast` source accepts arbitrary slot durations.

```json
[
  {"start": "2026-05-07T00:00:00+02:00", "end": "2026-05-07T06:35:00+02:00", "value": 2.80},
  {"start": "2026-05-07T06:35:00+02:00", "end": "2026-05-07T07:35:00+02:00", "value": 6.20},
  {"start": "2026-05-07T07:35:00+02:00", "end": "2026-05-07T11:35:00+02:00", "value": 2.80}
]
```

### `GET /api/price?ean=859182400000000000&vt=6.20&nt=2.80`

Returns the current price as plain text. Use with evcc's `price` source.

### `GET /api/status`

Returns cache status per EAN for debugging.

### `DELETE /api/cache/{ean}`

Drops the cached schedule for the given EAN (in-memory and on-disk). The next request for that EAN will refetch from CEZ. Idempotent — returns `{"removed_memory": false, "removed_disk": false}` if no cache existed. Rejects non-numeric EANs with HTTP 400.

```bash
curl -X DELETE http://localhost:8080/api/cache/859182400000000000
```

## evcc configuration

```yaml
tariffs:
  grid:
    type: custom
    forecast:
      source: http
      uri: http://localhost:8080/api/rates?ean=859182400000000000&vt=6.20&nt=2.80
```

Replace `localhost` with the container name (`evcc-tariff`) if running both services in Docker.

## Data source

The schedules come from CEZ Distribuce's anonymous "časy spínání" (switching times) endpoints — the same data shown on the public portal page.

| Purpose | URL |
|---|---|
| Public portal page (browser) | `https://dip.cezdistribuce.cz/irj/portal/anonymous/casy-spinani` |
| JSON schedule endpoint (POSTed by this service) | `https://dip.cezdistribuce.cz/irj/portal/anonymous/casy-spinani?path=switch-times/signals` |
| CAPTCHA image (required by the JSON endpoint) | `https://dip.cezdistribuce.cz/irj/portal/anonymous/captcha` |
| OCR for the CAPTCHA (third-party) | `https://generativelanguage.googleapis.com` (Gemini `gemini-3.1-flash-lite`) |

The JSON endpoint expects a POST body of `{"ean": "<18 digits>", "captcha": "<4 letters>"}` and returns one record per `(signal, date)` pair. CEZ may return multiple signals per EAN (e.g. `…\|1`, `…\|2`, `…\|3`); this service unions all signal windows per date so any low-tariff slot from any relay counts as low.

## How it works

1. On startup, loads all cached HDO schedules from disk
2. On request, if cache for the given EAN is missing or older than 3 days, fetches fresh data from CEZ distribuce API
3. CAPTCHA on the CEZ API is solved by sending the image to Gemini `gemini-3.1-flash-lite` via the `google-genai` SDK (requires `GEMINI_API_KEY`)
4. Each EAN's schedule is cached to its own file and refreshed every 3 days
5. Each request maps the cached low-tariff windows to the `vt`/`nt` prices from the query parameters
