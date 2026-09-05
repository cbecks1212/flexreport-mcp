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
| `get_latest_report(symbols)` | `POST /get-cached-reports` | Get the latest pre-built cached report(s) for one or more **named** tickers, instantly, as short-lived presigned PDF download URLs + inline base64, each with a freshness tag (`generated_at`, `age_hours`, `latest_event_at`, `stale`), + a `missing` list. `stale: true` means the PDF predates the symbol's latest report inputs — regenerate with `generate_report_for_stock(ticker)` |
| `generate_report_for_stock(ticker, user_override, financial_items, as_reported_financial_items, ratios, revenue_segment, technical_analysis_items, estimate_items, institutional_ownership, include_as_report_financials, as_reported_periods)` | `POST /create-full-report` | Two modes. **Standard** (ticker only): renders the symbol's saved report plan — built by the platform's ETLs, refreshed nightly — in ~10-20 s; the call to make when the cached report is `stale` or `missing`. **Custom** (`user_override=true` + shaping lists): a bespoke full rebuild (~10 min, not cached) around the line items, ratios, as-reported concepts, segments, indicators, estimates, or manager CIKs the user named; the tool sets the switch whenever a shaping field is non-empty. Async → `{ticker: {task_id, status}}` |
| `download_pdf_from_url(url, file_name)` | `POST /download-pdf-from-url` | Fetch a presigned **S3** PDF URL server-side (SSRF-guarded) and return it inline as base64 — e.g. a `get_latest_report` `url` on clients that can't open the link (authed) |
| `explore_data_catalogue(query)` | `POST /data-catalogue-exploration` | **Default route** — fast, interactive EDA against the data platform → result sets to render as charts/tables (dashboard only, 20/hour) → `{task_id, status}` |
| `generate_research_report(query, delivery)` | `POST /generate-research-report` | **Deep dive** (~10-12 min, async) — analyst-grade writeup, only when the user explicitly asks for a full report → `{task_id, status}` |
| `get_task_status(task_id)` | `GET /task-status` | Poll an async job to `SUCCESS` and read its `result` |
| `get_stock_picks(strategy_name)` | `GET /get-stock-picks` | Latest LLM-selected stock picks for the current rebalance (optionally one strategy) |
| `list_options(kind, ticker, q, cik)` | `GET /list-realtime-event-options`, `/list-financial-items`, `/list-financial-ratios`, `/list-as-reported-items?ticker=`, `/list-revenue-segments?ticker=`, `/list-institutional-managers?q=&cik=`, `/get-sectors`, `/list-institutional-investor-types`, `/list-countries`, `/get-fiscal-quarter`, `/list-marketcap-options`, `/list-intraday-chart-options`, `/list-technical-indicators`, `/list-tickers`, `/list-symbols-with-names` | One catalogue tool: enumerate valid values for a parameter (event types, line items, ratios, a filer's own as-reported XBRL concepts and revenue segments (`ticker`), institutional managers by name fragment / CIK (`q` / `cik`), sectors, investor types, countries, fiscal quarter, market-cap buckets, intraday frequencies, technical indicators, and the ticker universe with or without company names) |
| `list_sub_industries(sectors)` | `GET /get-sub-industries` | Distinct industries within the given sector(s) |
| `get_company_snapshot(symbol)` | `GET /get-company-snapshot` | Structured snapshot: thesis, fundamentals, technicals, price targets, ownership, grades |
| `get_company_event_web(symbol, window_days, max_nodes)` | `GET /get-company-event-web` | The **why** behind the snapshot — the company's recent event **graph**: time-ordered event/`data_update` nodes with `fetch` hints and typed edges (`same_chain_run` / `lineage` / `co_occurrence`). Pair it with `get_company_snapshot`, and call it **before** chaining a targeted `list_realtime_events` / `explore_data_catalogue` / report request at one symbol |
| `situate(symbols, window_days, question)` | `GET /get-company-event-web` + `GET /get-event-ontology` + `GET /is-market-open` | **The first call.** Composes the event web (what happened to *this* company), the ontology (what that *kind* of event entails, what follows it, where its payload lives after the 12h cache, when each table next refreshes) and market status into what is going on right now plus an ordered **`plan`** of exact tool calls, a **`skip`** list of calls that would return nothing, and situation-scoped **`guidance`**. Composition is in `situate.py` (pure functions, fixture-tested); no new backend route. Public, synchronous |
| `get_event_ontology(event_type, family, relation, format)` | `GET /get-event-ontology` | The class-level **ontology** behind the event web — what an event **type** entails: the tables written when it fires (by layer), the events that usually follow (observed rate / lag), where the payload lives after the 12h realtime cache (`persisted_in`), which workflow refreshes each relation and its `next_run_at` (the freshness check), and which tool reads it. Symbol-independent, public, cacheable — call it **first** on any event-driven request and join it to a web node's `type` / `relation` / `family` |
| `get_signed_sql_drilldown(encrypted_query_token)` | `GET /query-data` | Follow a `get_company_event_web` node's `signed_query_url` (or its bare `t` token) to the rows behind it — new record flagged `is_new_record: true`, plus context rows; tokens are server-minted only, never constructed (authed) |
| `save_user_query(user_request, query_tokens)` | `POST /save-user-query` | Save an `explore_data_catalogue` result's opaque `query_token`s under the user's own wording, so the request can be replayed without re-planning it (authed, owner-scoped; **no upsert** — saving twice creates two rows, 100/min) |
| `list_saved_queries(limit)` | `GET /get-saved-queries` | The caller's saved requests, newest first — `id` (what delete takes), `user_request` (the label to match the user's ask against), `queries` (the tokens to replay). A 503 means the backend table isn't created yet, **not** "nothing saved" |
| `run_saved_query(query_tokens)` | `GET /query-data` | Replay a saved request: each token runs as-is against **live** data, synchronously, in the order given (max 25) — rows only, never SQL; seconds instead of a ~1-5 min planning job, and no draw on the 20/hour exploration budget |
| `delete_saved_query(query_id)` | `DELETE /delete-saved-query` | Delete one saved request by `id` — permanent, and the tokens go with it; the 404 deliberately covers both "no such id" and "not yours" |
| `draft_user_template(template, template_type, anchor)` | `POST /draft-user-template` | **Step 1 of 2** for a user's own PDF format: compiles an HTML page into a blueprint under several treatments and renders each as presigned **PNG previews** of the platform's specimen document — one of every content kind (title, lead, sections, bullets, KPI cards, tables, charts, figure, source line, footnote); an `add_on` at its in-report column width, a `bespoke` as full pages — plus a `draft_id`. The user signs off on how the design treats each kind of content, not on sample text. Nothing is stored — the draft lives 24h (authed, 30/min) |
| `save_user_template(draft_id, variant_id, template, template_type, anchor)` | `POST /save-user-template` | **Step 2 of 2**: pass `draft_id` + the chosen `variant_id` and the reviewed treatment is saved; or pass `template` + `template_type` (+ `anchor`) directly to skip the previews (HTML only — markdown is a 422). **One template per user — upsert**, saving replaces the previous one. Returns `{status: "SAVED", template_type, anchor, bytes, patterns, tones, warnings}` — the content kinds the design styles and its tone classes (authed, 100/min) |
| `update_user_template(template, template_type, anchor, draft_id, variant_id)` | `PUT /update-user-template` | Change the saved template — any field (omitted fields keep their stored values), or **no fields** to recompile it as stored with the current compiler. Async: `{task_id, status: "PENDING", changed, next_step}` → poll `get_task_status` for `{status: "SAVED", patterns, tones, warnings}`. 404 = nothing saved yet (save first); 422 = validation errors (authed) |
| `get_user_template()` | `GET /get-user-template` | The caller's saved template (`template_type`, `template_format`, `anchor`, sanitized `template`, timestamps). 404 = none saved; a 503 means the backend table isn't created yet, **not** "nothing saved". Also the markup to lay an agent-composed PDF out in |
| `delete_user_template()` | `DELETE /delete-user-template` | Delete the saved template — permanent, PDFs revert to the standard layouts; ownership enforced in the `WHERE` clause |
| `detect_intraday_outlier_jumps(symbol, zscore_threshold)` | `GET /detect-intraday-outlier-jumps` | Live look at today's 1-min tape; flags minutes whose move is a daily-sigma outlier (synchronous, authed) |
| `get_aftermarket_trades(symbols, start_datetime, end_datetime)` | `POST /get-aftermarket-trades` | Query **stored** extended-hours trade ticks for symbols over an ET datetime range (defaults to today, authed, 300/min) |
| `get_aftermarket_quotes(symbols, start_datetime, end_datetime)` | `POST /get-aftermarket-quotes` | Query **stored** extended-hours bid/ask quote ticks for symbols over an ET datetime range (defaults to today, authed, 300/min) |
| `onboard_symbol(symbol)` | `POST /onboard-symbol` | Request onboarding of an uncovered ticker (async, authed, 5/hour) |

For a single named company, `get_company_snapshot` and `get_company_event_web` are the **standard pair** — the two halves of the same question, usually called together. The snapshot is the **what** (where the company stands now: thesis, fundamentals, technicals, ownership, grades); the event web is the **why** (the episode behind it: earnings print → 8-K → transcript update → IR publication → analyst reaction, with edges saying how each relates). A snapshot on its own is a verdict with no evidence; the web is the evidence. The web is also the cheap grounding call that makes the rest of the loop precise — each node carries the exact follow-up call, so the next `list_realtime_events`, `explore_data_catalogue`, or report request carries real event types and dates instead of guessed ones.

Typical agent loop: default to `explore_data_catalogue(query)` for open-ended/exploratory questions (fast, interactive charts/tables). Escalate only on a crystal-clear intent — `get_latest_report(symbols)` for the existing report on a named ticker, `screen_stocks(...)` to filter the universe, or `generate_research_report(query)` for an explicit deep dive (~10-12 min, async — **poll** with `get_task_status`).

Asked the same question twice? `explore_data_catalogue` re-plans its SQL every time, so once an exploration answers something the user will want again, save its `query_token`s with `save_user_query` and replay them later with `run_saved_query` — the same queries against live data, in seconds, with no planning step and no polling. `list_saved_queries` is the thing to check **before** re-running an exploration; `delete_saved_query` clears a stale row. The tokens are opaque ciphertext the backend minted: pass them through unchanged, never construct or edit one, and never show one to the user.

Want reports in your own format? A user supplies an HTML page — masthead, colours, layout, branding — and it is saved as a **blueprint**: the author's stylesheet and page chrome plus one markup pattern per content kind, which the backend applies automatically to every PDF it renders for that user (`generate_report_for_stock`, standard and custom, and scheduled reports). A template is structure and style applied to the document's own content: nobody fills template markup, and no other call changes. It is a visual choice, so it is approved by **looking**: `draft_user_template` compiles the page under several treatments and renders each as PNG previews of the platform's specimen document (an `add_on` restyles one section of the standard layout, named by `anchor`; a `bespoke` drives the whole document), the user picks one, and `save_user_template(draft_id, variant_id)` stores that treatment — one template per user, upserted. `update_user_template` changes any field later, or recompiles the stored design when called with no arguments; `get_user_template` shows what is in force; `delete_user_template` returns the user to the standard layouts.

There is no agent-driven PDF builder: the `build_pdf_full_width` / `build_pdf_sidebar` tools (backend `/create-pdf` and `/create-pdf-sidebar`) and the `pdf_options` catalogue were retired in favour of templates. When a user wants a document composed from Flexreport data that no backend report covers, the agent builds the PDF with its own document tooling, laying the content out in the user's saved template (`get_user_template`) when one exists — and offering to draft one (draft → preview → save, then update) when none does.

Reports are on-demand: nothing regenerates until someone asks. `get_latest_report` tags every cached PDF with `stale` — `true` when the PDF predates the symbol's latest report inputs (a print, a filing, a 13F refresh landed after `generated_at`). On `stale: true` or a `missing` symbol, the agent calls `generate_report_for_stock(ticker)` with the ticker alone: the backend renders the symbol's saved query plan (built by the internal ETLs the first time anyone asked, refreshed nightly while its inputs keep changing) in ~10-20 s and the finished PDF replaces the cached one. Only an explicit ask to cover named items makes it a **custom** report — `user_override=true` plus the shaping lists (`financial_items`, `ratios`, `as_reported_financial_items`, `revenue_segment`, `technical_analysis_items`, `estimate_items`, `institutional_ownership` CIKs, `include_as_report_financials` / `as_reported_periods`) — which is a bespoke full rebuild (~10 min) that is never cached. The backend rejects shaping lists without the switch (422), so the tool sets it whenever a shaping field is non-empty. Vocabularies come from `list_options`: `financial_items`, `financial_ratios`, `technical_indicators`, and the ticker-scoped `as_reported_items` / `revenue_segments` and the `q`-scoped `institutional_managers` (returns CIKs). Event context is server-owned — the request carries no thesis, change-summary, `include_*`, or price-date fields.

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
# Confirm the tools list loads (41 tools), then exercise:
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
