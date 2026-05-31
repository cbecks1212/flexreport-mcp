# flexreport-mcp

A standalone **MCP microservice** that exposes the [FlexReport](https://app.flexreportfinapi.com/api-docs)
equity backend's **live events** and **research-report artifacts** as on-demand
tools for Claude (and any MCP client).

It lets AI agents pull in real-time market events and research on demand, so they
can surface the insights that matter most to you.

It's a thin, stateless proxy over the backend's public HTTP API: it holds no
credentials and only forwards the caller's bearer JWT, so the entire contract with
the backend is an HTTP API + a JWT.

## Quick Install

Add the connector to Claude Code

```bash
claude mcp add --transport http flexreport https://mcp.flexreportfinapi.com/mcp
```

Then start Claude and just ask (e.g. *"pull the biggest movers from flexreport"*).
The agent handles login for you on the first data call — register or sign in when
prompted; you never paste a token. Add `--scope user` to make it available in every
directory. See [Auth](#auth) for details.

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

Typical agent loop: **discover** valid params (`list_report_options`) → **discover** events → **generate** a report → **poll** status → **fetch** artifact.

Auth resolution per call: explicit `bearer_token` arg → the inbound `Authorization` header.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set API_BASE_URL, MCP_HOST, MCP_PORT
set -a && source .env && set +a
python server.py              # serves streamable-http on http://MCP_HOST:MCP_PORT/mcp
```

## Auth

The server is **stateless** (load-balancer friendly), so auth rides each call.
The agent self-serves it — the playbook is hardwired into the server's MCP
`instructions`, so any connected client knows the flow:

**Agent-driven (default — no config):**
- *New user:* `register_user(email, password)` → user clicks the emailed
  confirmation link (or pastes the token to `confirm_registration`) →
  `get_token(email, password)`.
- *Existing user:* `get_token(email, password)`.
- The agent passes the returned `access_token` as the `bearer_token` arg on every
  data tool. On a 401 it re-mints and retries. No tokens in config, no restart.

**Static header (optional):**
- Configure `Authorization: Bearer <JWT>` in your MCP client and the server
  forwards it on every call — handy for a single fixed user, but the agent-driven
  path needs no config at all.

`bearer_token` (explicit) always takes precedence over the inbound header. This
service holds no credentials at rest — JWTs are forwarded per-call, never stored.

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
