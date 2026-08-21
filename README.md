# flexreport-mcp

A standalone **MCP microservice** that exposes the [FlexReport](https://app.flexreportfinapi.com/api-docs)
equity backend's **live events**, **research-report artifacts**, and **database of
750M+ datapoints** as on-demand tools for Claude (and any MCP client).

It supercharges AI agents with real-time market events and curated, golden-source
data spanning fundamentals, transcripts, filings, insider trades (Forms 3, 4, and 5),
ratios, macro data, IR decks pulled straight from each company's investor relations
site, and more.

## Quick Install

Add the connector to Claude Code

```bash
claude mcp add --transport http flexreport https://mcp.flexreportfinapi.com/mcp
```

Then start Claude and just ask (e.g. *"pull the biggest movers from flexreport"*).
On the first data call your MCP client runs an OAuth sign-in in your browser —
sign in or register when prompted; you never paste a token. Add `--scope user` to
make it available in every directory. See [Auth](#auth) for details.

## Use-cases

1. Analyze real-time SEC 8-K filings, while also pulling in investor relations decks and 5-minute bars to quickly identify stocks making meaningful moves: *"What are the latest 8-K releases? Highlight the most significant ones, pull the accompanying investor relations releases via FlexReport, chart 5-minute bars for any names making meaningful moves, and tell me how they align with each company's current state. I would like this in tabular format: 8-K summary, company trend, FlexReport 8-K analysis S3 link, IR deck link."*

2. Predict upcoming earnings volatility, pulling in simple and exponential moving averages and Bollinger Bands, and creating a bespoke investment memo: *"Predict tomorrow's upcoming earnings volatility, highlighting the stocks slated for the biggest moves; chart their simple and exponential moving averages and Bollinger Bands; and highlight the biggest fundamental drivers right now, putting this all in an investment memo for my team."*

3. Run thematic research on a market narrative: *"The AI trade has cooled recently, largely due to CapEx concerns and return on investment. I believe this has happened before, in the fall of 2025. Can you confirm, and what were the factors that allayed those concerns? Was it management commentary, continued demand for AI services, robust profitability and higher guidance? Please put together an in-depth report with FlexReport Finance."*

## Tools

| Tool | Backend endpoint | What it does |
|---|---|---|
| `list_realtime_events(event_type, tickers, sector, industry, market_cap)` | `POST /get-realtime-events` | Pull live events (EPS updates, transcripts, ratings, …) from the 12h cache |
| `get_latest_report(symbols)` | `POST /get-cached-reports` | Get the latest pre-built cached report(s) for one or more **named** tickers, instantly, as short-lived presigned PDF download URLs, + a `missing` list |
| `download_pdf_from_url(url, file_name)` | `POST /download-pdf-from-url` | Fetch a presigned **S3** PDF URL server-side (SSRF-guarded) and return it inline as base64 — e.g. a `get_latest_report` `url` on clients that can't open the link (authed) |
| `explore_data_catalogue(query)` | `POST /data-catalogue-exploration` | **Default route** — fast, interactive EDA against the data platform → result sets to render as charts/tables (dashboard only, 20/hour) → `{task_id, status}` |
| `generate_research_report(query, delivery)` | `POST /generate-research-report` | **Deep dive** (~10-12 min, async) — analyst-grade writeup, only when the user explicitly asks for a full report → `{task_id, status}` |
| `get_task_status(task_id)` | `GET /task-status` | Poll an async job to `SUCCESS` and read its `result` |
| `get_stock_picks(strategy_name)` | `GET /get-stock-picks` | Latest LLM-selected stock picks for the current rebalance (optionally one strategy) |
| `list_options(kind)` | `GET /list-realtime-event-options`, `/list-financial-items`, `/list-financial-ratios`, `/get-sectors`, `/list-institutional-investor-types`, `/list-countries`, `/get-fiscal-quarter`, `/list-marketcap-options`, `/list-intraday-chart-options`, `/list-pdf-tag-options`, `/list-technical-indicators`, `/list-tickers`, `/list-symbols-with-names` | One catalogue tool: enumerate valid values for a parameter (event types, ratios, sectors, investor types, countries, fiscal quarter, market-cap buckets, intraday frequencies, the PDF tag DSL, technical indicators, and the ticker universe with or without company names) |
| `list_sub_industries(sectors)` | `GET /get-sub-industries` | Distinct industries within the given sector(s) |
| `get_company_snapshot(symbol)` | `GET /get-company-snapshot` | Structured snapshot: thesis, fundamentals, technicals, price targets, ownership, grades |
| `get_company_event_web(symbol, window_days, max_nodes)` | `GET /get-company-event-web` | The **why** behind the snapshot — the company's recent event **graph**: time-ordered event/`data_update` nodes with `fetch` hints and typed edges (`same_chain_run` / `lineage` / `co_occurrence`). Pair it with `get_company_snapshot`, and call it **before** chaining a targeted `list_realtime_events` / `explore_data_catalogue` / report request at one symbol |
| `get_signed_sql_drilldown(encrypted_query_token)` | `GET /query-data` | Follow a `get_company_event_web` node's `signed_query_url` (or its bare `t` token) to the rows behind it — new record flagged `is_new_record: true`, plus context rows; tokens are server-minted only, never constructed (authed) |
| `detect_intraday_outlier_jumps(symbol, zscore_threshold)` | `GET /detect-intraday-outlier-jumps` | Live look at today's 1-min tape; flags minutes whose move is a daily-sigma outlier (synchronous, authed) |
| `get_aftermarket_trades(symbols, start_datetime, end_datetime)` | `POST /get-aftermarket-trades` | Query **stored** extended-hours trade ticks for symbols over an ET datetime range (defaults to today, authed, 300/min) |
| `get_aftermarket_quotes(symbols, start_datetime, end_datetime)` | `POST /get-aftermarket-quotes` | Query **stored** extended-hours bid/ask quote ticks for symbols over an ET datetime range (defaults to today, authed, 300/min) |
| `onboard_symbol(symbol)` | `POST /onboard-symbol` | Request onboarding of an uncovered ticker (async, authed, 5/hour) |

For a single named company, `get_company_snapshot` and `get_company_event_web` are the **standard pair** — the two halves of the same question, usually called together. The snapshot is the **what** (where the company stands now: thesis, fundamentals, technicals, ownership, grades); the event web is the **why** (the episode behind it: earnings print → 8-K → transcript update → IR publication → analyst reaction, with edges saying how each relates). A snapshot on its own is a verdict with no evidence; the web is the evidence. The web is also the cheap grounding call that makes the rest of the loop precise — each node carries the exact follow-up call, so the next `list_realtime_events`, `explore_data_catalogue`, or report request carries real event types and dates instead of guessed ones.

Typical agent loop: default to `explore_data_catalogue(query)` for open-ended/exploratory questions (fast, interactive charts/tables). Escalate only on a crystal-clear intent — `get_latest_report(symbols)` for the existing report on a named ticker, `screen_stocks(...)` to filter the universe, or `generate_research_report(query)` for an explicit deep dive (~10-12 min, async — **poll** with `get_task_status`).

> **Note:** `generate_report` (bespoke on-the-fly `POST /create-full-report`) is currently **commented out** in `server.py` — it overlapped with the routes above and caused mis-routing. The backend endpoint is unchanged; re-enable by uncommenting the tool.

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

### How it works

Sign-in is a standard browser **authorization-code + PKCE** flow, run by your MCP
client (e.g. Claude) against the FlexReport backend, which is the **Authorization
Server**. Register or sign in with an email + password, or use **Google
Sign-In** — you never paste or type a token:

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
credential-free proxy. Only valid RS256 OAuth tokens are accepted; there is no
password or static-JWT fallback.

### Config (env)

| Var | Default | Purpose |
|---|---|---|
| `OAUTH_ISSUER` | `https://app.flexreportfinapi.com` | Expected token `iss` + advertised authorization server. **Must match the backend's `iss`** — prod uses the root domain `https://flexreportfinapi.com`. |
| `OAUTH_AUDIENCE` | = `OAUTH_ISSUER` | Expected token `aud`. Set both sides to the canonical MCP URL for true audience binding. |
| `OAUTH_JWKS_URL` | `{issuer}/.well-known/jwks.json` | Where public keys are fetched (decoupled from issuer for container networking). |
| `MCP_RESOURCE_URL` | `https://mcp.flexreportfinapi.com/mcp` | This server's canonical resource identifier (the PRM `resource`). |

### Static header

Configure `Authorization: Bearer <OAuth access token>` in your MCP client and the
server validates and forwards it like any other call — useful for testing with a
token minted elsewhere. Nothing is stored at rest; tokens are forwarded per-call.

## Wire into an MCP client

`.mcp.json` (Claude Code):

```json
{
  "mcpServers": {
    "flexreport": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <YOUR_OAUTH_ACCESS_TOKEN>" }
    }
  }
}
```

## Verify with MCP Inspector

```bash
npx @modelcontextprotocol/inspector
# Connect to http://localhost:8000/mcp with header Authorization: Bearer <OAuth access token>
# Confirm the tools list loads (34 tools), then exercise:
#   list_realtime_events("eps_update")        -> events (or [])
#   get_company_event_web("NVDA")             -> event graph (or an empty/degraded web)
#   get_latest_report(["AAPL"])               -> presigned PDF url (or missing)  [named-ticker report]
#   explore_data_catalogue("MU EPS growth last 8 quarters")  -> task_id  [default exploratory route]
#   get_task_status(task_id)                  -> eventually SUCCESS
# Negative: connect with no/invalid token     -> 401 + WWW-Authenticate challenge
```

## Deploy

Build the image and run it as its own container (e.g. a separate ECS service with
its own task definition), independent of the API and Celery workers. Set
`API_BASE_URL` to the deployed backend URL.

```bash
docker build -t flexreport-mcp .
docker run -p 8000:8000 -e API_BASE_URL=https://flexreportfinapi.com flexreport-mcp
```

## Privacy

FlexReport's privacy policy — what's collected, retention windows, and the
third parties involved — is published at
[app.flexreportfinapi.com/privacy](https://app.flexreportfinapi.com/privacy)
(source: [PRIVACY.md](PRIVACY.md)).
