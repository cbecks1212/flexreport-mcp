# flexreport-mcp

A standalone **MCP microservice** that exposes the [FlexReport](https://flexreportfinapi.com)
equity backend's **live earnings events** and **research-report artifacts** as on-demand
tools for Claude (and any MCP client).

It is a thin, stateless proxy over the backend's public HTTP API. It holds **no
credentials** and never touches AWS/Redis/DB — it forwards the caller's bearer JWT
so the backend handles auth, plan quota, and rate limits. No code is shared with the
API repo; the only contract is the HTTP API + a JWT.

## Tools

| Tool | Backend endpoint | What it does |
|---|---|---|
| `list_realtime_events(event_type, tickers, sector, industry, market_cap)` | `POST /get-realtime-events` | Pull live events (EPS updates, transcripts, ratings, …) from the 12h cache |
| `generate_report(ticker, overrides)` | `POST /create-full-report` | Start a structured report job for a ticker → `{ticker: {task_id, status}}` |
| `generate_research_report(query, delivery)` | `POST /generate-research-report` | Start a report job from a plain-English query → `{task_id, status}` |
| `get_task_status(task_id)` | `GET /task-status` | Poll an async job to `SUCCESS` and read its `result` |
| `get_report_artifact(symbols)` | `POST /get-cached-reports` | Bulk-fetch cached PDFs (base64) + a `missing` list |
| `list_report_options(kind)` | `GET /list-realtime-event-options`, `/list-financial-items`, `/list-financial-ratios`, `/get-sectors`, `/list-institutional-investor-types`, `/list-countries`, `/get-fiscal-quarter` | Enumerate valid values for a parameter (event types, ratios, sectors, investor types, countries, fiscal quarter) |
| `list_sub_industries(sectors)` | `GET /get-sub-industries` | Distinct industries within the given sector(s) |
| `list_tickers(with_names)` | `GET /list-tickers` or `/list-symbols-with-names` | The covered ticker universe (optionally with company names) |
| `get_company_snapshot(symbol)` | `GET /get-company-snapshot` | Structured snapshot: thesis, fundamentals, technicals, price targets, ownership, grades |
| `onboard_symbol(symbol)` | `POST /onboard-symbol` | Request onboarding of an uncovered ticker (async, authed, 5/hour) |
| `register_user(email, password)` | `POST /auth` | Register for an API key; backend emails a confirmation token (pre-auth) |
| `confirm_registration(token)` | `GET /confirm/{token}` | Confirm a registration with the emailed token (pre-auth) |
| `get_token(username, password)` | `POST /token` | Exchange credentials for a bearer JWT (OAuth2 password flow, pre-auth) |
| `login(username, password)` | `POST /token` | Authenticate and **cache** the JWT for this session — auto-applied to later calls (pre-auth) |
| `logout()` | — | Clear the session's cached JWT |

Typical agent loop: **discover** valid params (`list_report_options`) → **discover** events → **generate** a report → **poll** status → **fetch** artifact.

Auth resolution per call: explicit `bearer_token` arg → the session's cached JWT (from `login`) → the inbound `Authorization` header.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set API_BASE_URL, MCP_HOST, MCP_PORT
set -a && source .env && set +a
python server.py              # serves streamable-http on http://MCP_HOST:MCP_PORT/mcp
```

## Auth

Three ways to authenticate, all backed by the backend's `POST /token` (OAuth2
password flow). Pick one — no Authorization header is required in `.mcp.json`
unless you choose the static option.

**A. `login` tool (recommended — agent-driven, no config):**
- *New user:* `register_user(email, password)` → `confirm_registration(token)` →
  `login(email, password)`.
- *Existing user:* `login(email, password)`.
- The JWT is cached **per MCP session** (keyed by the `Mcp-Session-Id` header) and
  auto-applied to every subsequent authed call — no `bearer_token`, no header in
  `.mcp.json`. Concurrent sessions are isolated. Call `logout` to clear it; the
  cache is in-memory and lost on restart. A backend 401 evicts it automatically.

**B. `bearer_token` arg (explicit, per-call):**
- `get_token(email, password)` → pass the returned `access_token` as the
  `bearer_token` arg to `generate_report` / `get_report_artifact` / etc. Overrides
  both the cache and the header.

**C. Static header (single user):**
- Configure `Authorization: Bearer <JWT>` in your MCP client; the server forwards
  it on every call. Simplest, but the manual injection the other two avoid.

This service still holds no credentials at rest — JWTs are forwarded per-call,
never stored.

## Wire into an MCP client

`.mcp.json` (Claude Code):

```json
{
  "mcpServers": {
    "flexreport": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <YOUR_JWT>" }
    }
  }
}
```

## Verify with MCP Inspector

```bash
npx @modelcontextprotocol/inspector
# Connect to http://localhost:8000/mcp with header Authorization: Bearer <JWT>
# Confirm 5 tools list, then exercise:
#   list_realtime_events("eps_update")        -> events (or [])
#   generate_report("AAPL")                   -> read ["AAPL"]["task_id"]
#   get_task_status(task_id)                  -> eventually SUCCESS
#   get_report_artifact(["AAPL"])             -> base64 (or missing)
# Negative: call any JWT tool with no token   -> clean {"error": ...}, no crash
```

## Deploy

Build the image and run it as its own container (e.g. a separate ECS service with
its own task definition), independent of the API and Celery workers. Set
`API_BASE_URL` to the deployed backend URL.

```bash
docker build -t flexreport-mcp .
docker run -p 8000:8000 -e API_BASE_URL=https://flexreportfinapi.com flexreport-mcp
```
