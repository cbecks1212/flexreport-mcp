# flexreport-mcp

A standalone **MCP microservice** that exposes the [FlexReport](https://app.flexreportfinapi.com/api-docs)
equity backend's **live events** and **research-report artifacts** as on-demand
tools for Claude (and any MCP client).

It lets AI agents pull in real-time market events and research on demand, so they
can surface the insights that matter most to you.

## Quick Install

Add the connector to Claude Code

```bash
claude mcp add --transport http flexreport https://mcp.flexreportfinapi.com/mcp
```

Then start Claude and just ask (e.g. *"pull the biggest movers from flexreport"*).
On the first data call your MCP client runs an OAuth sign-in in your browser —
sign in or register when prompted; you never paste a token. Add `--scope user` to
make it available in every directory. See [Auth](#auth) for details.

## Tools

| Tool | Backend endpoint | What it does |
|---|---|---|
| `list_realtime_events(event_type, tickers, sector, industry, market_cap)` | `POST /get-realtime-events` | Pull live events (EPS updates, transcripts, ratings, …) from the 12h cache |
| `get_latest_report(symbols)` | `POST /get-cached-reports` | Get the latest pre-built cached report(s) for one or more **named** tickers, instantly, as short-lived presigned PDF download URLs, + a `missing` list |
| `explore_data_catalogue(query)` | `POST /data-catalogue-exploration` | **Default route** — fast, interactive EDA against the data platform → result sets to render as charts/tables (dashboard only, 20/hour) → `{task_id, status}` |
| `generate_research_report(query, delivery)` | `POST /generate-research-report` | **Deep dive** (~10-12 min, async) — analyst-grade writeup, only when the user explicitly asks for a full report → `{task_id, status}` |
| `get_task_status(task_id)` | `GET /task-status` | Poll an async job to `SUCCESS` and read its `result` |
| `get_stock_picks(strategy_name)` | `GET /get-stock-picks` | Latest LLM-selected stock picks for the current rebalance (optionally one strategy) |
| `list_report_options(kind)` | `GET /list-realtime-event-options`, `/list-financial-items`, `/list-financial-ratios`, `/get-sectors`, `/list-institutional-investor-types`, `/list-countries`, `/get-fiscal-quarter` | Enumerate valid values for a parameter (event types, ratios, sectors, investor types, countries, fiscal quarter) |
| `list_sub_industries(sectors)` | `GET /get-sub-industries` | Distinct industries within the given sector(s) |
| `list_tickers(with_names)` | `GET /list-tickers` or `/list-symbols-with-names` | The covered ticker universe (optionally with company names) |
| `get_company_snapshot(symbol)` | `GET /get-company-snapshot` | Structured snapshot: thesis, fundamentals, technicals, price targets, ownership, grades |
| `detect_intraday_outlier_jumps(symbol, zscore_threshold)` | `GET /detect-intraday-outlier-jumps` | Live look at today's 1-min tape; flags minutes whose move is a daily-sigma outlier (synchronous, authed) |
| `get_aftermarket_trades(symbols, start_datetime, end_datetime)` | `POST /get-aftermarket-trades` | Query **stored** extended-hours trade ticks for symbols over an ET datetime range (defaults to today, authed, 300/min) |
| `get_aftermarket_quotes(symbols, start_datetime, end_datetime)` | `POST /get-aftermarket-quotes` | Query **stored** extended-hours bid/ask quote ticks for symbols over an ET datetime range (defaults to today, authed, 300/min) |
| `onboard_symbol(symbol)` | `POST /onboard-symbol` | Request onboarding of an uncovered ticker (async, authed, 5/hour) |
| `register_user(email, password)` | `POST /auth` | Register for an API key; backend emails a confirmation token (pre-auth, **`AUTH_MODE=legacy` only**) |
| `confirm_registration(token)` | `GET /confirm/{token}` | Confirm a registration with the emailed token (pre-auth, **`AUTH_MODE=legacy` only**) |
| `get_token(username, password)` | `POST /token` | Exchange credentials for a bearer JWT (OAuth2 password flow, pre-auth, **`AUTH_MODE=legacy` only**) |

Typical agent loop: default to `explore_data_catalogue(query)` for open-ended/exploratory questions (fast, interactive charts/tables). Escalate only on a crystal-clear intent — `get_latest_report(symbols)` for the existing report on a named ticker, `screen_stocks(...)` to filter the universe, or `generate_research_report(query)` for an explicit deep dive (~10-12 min, async — **poll** with `get_task_status`).

> **Note:** `generate_report` (bespoke on-the-fly `POST /create-full-report`) is currently **commented out** in `server.py` — it overlapped with the routes above and caused mis-routing. The backend endpoint is unchanged; re-enable by uncommenting the tool.

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

The server is an **OAuth 2.0 Resource Server** and **stateless** (load-balancer
friendly), so auth rides each call. It holds **no credentials and no signing
secret** — it validates the inbound bearer token and forwards it to the backend,
which enforces scope, plan, and quota.

### How it works (OAuth)

Sign-in is a standard browser **authorization-code + PKCE** flow, run by your MCP
client (e.g. Claude) against the FlexReport backend, which is the **Authorization
Server**. You never paste or type a token:

1. On a request without a valid token the server returns `401` with a
   `WWW-Authenticate` challenge and serves Protected Resource Metadata at
   `/.well-known/oauth-protected-resource`, pointing the client at the backend AS.
2. The client opens your browser; you sign in / consent and it receives an RS256
   access token issued by the backend.
3. The server validates that token on **every** call — signature via the
   backend's **JWKS** (RS256 public key) plus `aud`, `iss`, and `exp` — then
   forwards it to the backend. Invalid or expired → a clean `401` and the client
   re-runs the flow.

The server never sees your password and never holds the signing key — it stays a
credential-free proxy.

### Modes — `AUTH_MODE` (env)

| `AUTH_MODE` | Behavior |
|---|---|
| `legacy` | No token validation. The agent self-serves credentials via the `register_user` / `confirm_registration` / `get_token` tools (password passed as a tool arg). Original behavior; those pre-auth tools are registered **only** in this mode. |
| `both` | **OAuth Resource Server (prod today).** RS256 OAuth tokens are validated against the backend JWKS; tokens that fail RS256 are passed through as opaque, so legacy static-JWT users keep working while the backend re-validates them. Password tools removed. Transition state. |
| `oauth` | Strict end state. Only valid RS256 OAuth tokens are accepted. |

### Config (env)

| Var | Default | Purpose |
|---|---|---|
| `AUTH_MODE` | `legacy` | Selects the posture above |
| `OAUTH_ISSUER` | `https://app.flexreportfinapi.com` | Expected token `iss` + advertised authorization server. **Must match the backend's `iss`** — prod uses the root domain `https://flexreportfinapi.com`. |
| `OAUTH_AUDIENCE` | = `OAUTH_ISSUER` | Expected token `aud`. Set both sides to the canonical MCP URL for true audience binding. |
| `OAUTH_JWKS_URL` | `{issuer}/.well-known/jwks.json` | Where public keys are fetched (decoupled from issuer for container networking). |
| `MCP_RESOURCE_URL` | `https://mcp.flexreportfinapi.com/mcp` | This server's canonical resource identifier (the PRM `resource`). |

### Static header (any mode)

Configure `Authorization: Bearer <token>` in your MCP client and the server
forwards it verbatim — an OAuth access token under `both`/`oauth`, or a legacy
JWT under `legacy`. An explicit `bearer_token` tool arg always takes precedence
over the inbound header. Nothing is stored at rest; tokens are forwarded per-call.

### Legacy agent-driven path (`AUTH_MODE=legacy` only)

The original flow, kept for backward compatibility and being retired in favor of
OAuth (it sends the password as a tool argument, so it lands in call logs):

- *New user:* `register_user(email, password)` → click the emailed link (or paste
  the token to `confirm_registration`) → `get_token(email, password)`.
- *Existing user:* `get_token(email, password)`.
- The agent passes the returned `access_token` as the `bearer_token` arg on every
  data tool and re-mints on a `401`.

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
# Confirm the tools list loads (count varies by AUTH_MODE — legacy adds the 3 pre-auth tools), then exercise:
#   list_realtime_events("eps_update")        -> events (or [])
#   get_latest_report(["AAPL"])               -> presigned PDF url (or missing)  [named-ticker report]
#   explore_data_catalogue("MU EPS growth last 8 quarters")  -> task_id  [default exploratory route]
#   get_task_status(task_id)                  -> eventually SUCCESS
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
