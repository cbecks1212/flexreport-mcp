"""FlexReport MCP server — exposes the equity backend's live events and report
artifacts as on-demand MCP tools over streamable-http.

Each tool is a thin wrapper around a public backend HTTP endpoint. The caller's
inbound `Authorization: Bearer <JWT>` is forwarded so the backend enforces auth,
plan quota, and rate limits. This service holds no credentials and does not touch
AWS/Redis/DB or import anything from the API repo.
"""

import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP
from starlette.responses import PlainTextResponse

from client import MissingAuthError, auth_headers, get_client

# Non-code text (server instructions, auth playbooks) lives in instructions.json
# so the copy can be edited without touching the server logic.
_TEXT = json.loads((Path(__file__).parent / "instructions.json").read_text())

# Auth posture (env). Controls how inbound requests are authenticated:
#   legacy → no token validation; agent self-serves via get_token/register_user
#            (today's behavior). Deploying new code is a no-op until you flip this.
#   both   → OAuth Resource Server: RS256 tokens validated against the backend JWKS,
#            non-OAuth tokens passed through (legacy static-JWT users keep working),
#            password tools removed. Transition state.
#   oauth  → strict: only valid RS256 OAuth tokens accepted. End state.
AUTH_MODE = os.environ.get("AUTH_MODE", "legacy").lower()
_OAUTH_ENABLED = AUTH_MODE in ("oauth", "both")

# Mode-aware auth playbook advertised to every client on connect (MCP `instructions`).
_AUTH_BLOCK = _TEXT["auth_block_oauth"] if _OAUTH_ENABLED else _TEXT["auth_block_legacy"]

INSTRUCTIONS = _TEXT["instructions"].replace("{auth_block}", _AUTH_BLOCK)

# OAuth Resource-Server wiring (only when AUTH_MODE enables it). The SDK then serves
# /.well-known/oauth-protected-resource and returns 401 + WWW-Authenticate challenges,
# pointing clients at the backend Authorization Server (issuer_url).
_auth_settings = None
_token_verifier = None
if _OAUTH_ENABLED:
    from mcp.server.auth.settings import AuthSettings

    from auth_verifier import (
        MCP_RESOURCE_URL,
        OAUTH_ISSUER,
        FlexReportTokenVerifier,
    )

    _token_verifier = FlexReportTokenVerifier(lenient=(AUTH_MODE == "both"))
    _auth_settings = AuthSettings(
        issuer_url=OAUTH_ISSUER,
        resource_server_url=MCP_RESOURCE_URL,
        required_scopes=[],  # backend enforces scope/plan; don't gate at the transport
    )

mcp = FastMCP(
    "flexreport",
    instructions=INSTRUCTIONS,
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    # Behind a load balancer (ALB): make each request self-contained instead of
    # holding a long-lived per-session SSE stream the LB would choke on, and return
    # plain JSON rather than text/event-stream. Stateless mode has no persistent
    # session, so auth is per-call (an OAuth bearer, or legacy `bearer_token`), not a cache.
    stateless_http=True,
    json_response=True,
    auth=_auth_settings,
    token_verifier=_token_verifier,
)


def _pre_auth_tool(fn):
    """Register the legacy password/registration tools only in AUTH_MODE=legacy.

    Under OAuth these are dead (transport auth rejects the token-less connection they
    relied on) and shouldn't be advertised — sign-in moves to the browser flow.
    """
    return mcp.tool()(fn) if not _OAUTH_ENABLED else fn


def _inbound_request(ctx: Context):
    """Best-effort fetch of the inbound Starlette Request from the MCP context."""
    return getattr(ctx.request_context, "request", None)


async def _send(
    ctx: Context,
    method: str,
    path: str,
    *,
    require_auth: bool = True,
    bearer_token: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Forward a request to the backend, returning parsed JSON or a structured error.

    Auth (handled by auth_headers): explicit `bearer_token` (a JWT from `get_token`)
    → the inbound Authorization header. On a 401 the message tells the agent to
    re-authenticate with `get_token`.

    Errors (missing auth, transport failure, non-2xx) are returned as a dict with
    an "error" key rather than raised, so the agent receives a clean, readable message.
    """
    try:
        headers = auth_headers(
            _inbound_request(ctx), required=require_auth, token=bearer_token
        )
    except MissingAuthError as e:
        return {"error": str(e)}

    try:
        resp = await get_client().request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as e:
        return {"error": f"Request to backend failed: {e}"}

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        msg = f"Backend returned HTTP {resp.status_code}"
        if resp.status_code == 401:
            msg += " — not authenticated / token expired. Call `get_token` and pass the result as `bearer_token`."
        return {"error": msg, "detail": detail}

    try:
        return resp.json()
    except Exception:
        return {"error": "Backend returned a non-JSON response", "detail": resp.text}


@mcp.tool()
async def list_realtime_events(
    ctx: Context,
    event_type: str = "eps_update",
    tickers: Optional[list[str]] = None,
    sector: Optional[list[str]] = None,
    industry: Optional[list[str]] = None,
    market_cap: Optional[list[str]] = None,
    bearer_token: Optional[str] = None,
) -> Any:
    """Pull live earnings/market events from the backend's Redis-backed cache (12h TTL).

    `event_type` defaults to "eps_update". Other values include: eps_release,
    8k_release, company_update, biggest_mover, earnings_transcript_update,
    analyst_rating_update, news_evolution, earnings_themes, llm_basket_update,
    strategy_update. Call `list_report_options("event_types")` for the
    authoritative, current set — do not guess.

    Optionally narrow results by `tickers`, `sector`, `industry`, or `market_cap`
    (e.g. market_cap=["Large-cap","Mega-cap"]). Returns a list of event objects,
    or an empty list when the cache is cold.

    Requires auth: pass `bearer_token` (a JWT from `get_token`); on 401 re-mint and
    retry. Omit only if the MCP client forwards an Authorization header.
    """
    body: dict[str, Any] = {"event_type": event_type}
    if tickers:
        body["tickers"] = tickers
    if sector:
        body["sector"] = sector
    if industry:
        body["industry"] = industry
    if market_cap:
        body["market_cap"] = market_cap
    return await _send(ctx, "POST", "/get-realtime-events", json=body, bearer_token=bearer_token)


@mcp.tool()
async def generate_report(
    ctx: Context,
    ticker: str,
    overrides: Optional[dict] = None,
    bearer_token: Optional[str] = None,
) -> Any:
    """Build a NEW BESPOKE report on the fly for one ticker (slow, async). LAST RESORT.

    ===> This is a LAST-RESORT tool, NOT the default. Reach for it ONLY when EITHER:
      (a) `get_latest_report` has no cached report for the ticker (it came back in
          `missing`), so there is nothing pre-built to return, OR
      (b) the user EXPLICITLY wants to CUSTOMIZE the report with specific line items,
          ratios, filing frequency, institutional-ownership cuts, or other `overrides`
          that the cached report does not already cover.

    DO NOT use this for an ordinary "get me the report / research / analysis" request —
    use `get_latest_report` instead, which returns the pre-built cached report instantly.
    For an open-ended or thematic QUESTION (about a ticker or the broader market), use
    `generate_research_report` instead. This tool kicks off a slow, asynchronous build.

    Only `ticker` is required; the backend applies sensible defaults for everything
    else. Pass `overrides` to customize the report (e.g.
    {"include_transcript": false, "ratios": [...], "filing_frequency": "annual"}).
    Discover valid override values with `list_report_options`: "financial_items"
    and "financial_ratios" for the line items/ratios, and
    "institutional_investor_types" for `overrides.institutional_ownership`.

    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header.

    This is asynchronous. The response is keyed by ticker, e.g.
    {"AAPL": {"task_id": "...", "status": "PENDING"}}. Read result["AAPL"]["task_id"]
    and poll it with `get_task_status` until status is SUCCESS.
    """
    payload = {"ticker": ticker, **(overrides or {})}
    return await _send(
        ctx, "POST", "/create-full-report", json=payload, bearer_token=bearer_token
    )


@mcp.tool()
async def generate_research_report(
    ctx: Context,
    query: str,
    delivery: str = "email",
    bearer_token: Optional[str] = None,
) -> Any:
    """Answer an OPEN-ENDED or THEMATIC research QUESTION (extensible, multi-section).

    ===> THE RIGHT TOOL when the user's intent is a QUESTION rather than a request for an
    existing report — an exploratory or thesis-style ask about a ticker, or a market-wide
    theme not tied to one company (e.g. "Are large caps driving earnings season?",
    "What's the bull/bear case on NVDA?", "high-growth semis with rising estimates").
    For a plain "get me the latest report/research on <ticker>", use `get_latest_report`
    instead; only build a bespoke/custom single-ticker report with `generate_report`.

    `query` is natural language, e.g. "high-growth semis with rising estimates".
    `delivery` defaults to "email". Rate-limited to 20/hour per user server-side.
    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header.

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll with
    `get_task_status` until SUCCESS, then read its `result`.
    """
    return await _send(
        ctx, "POST", "/generate-research-report",
        json={"query": query, "delivery": delivery}, bearer_token=bearer_token,
    )


@mcp.tool()
async def explore_data_catalogue(
    ctx: Context,
    query: str,
    bearer_token: Optional[str] = None,
) -> Any:
    """Explore FlexReport's data platform with an OPEN-ENDED question — fast, interactive EDA.

    ===> THE RIGHT TOOL for lightweight, EXPLORATORY data discovery: when the user wants
    to poke at the data, get a feel for what FlexReport's platform covers, and see the
    answer as INTERACTIVE CHARTS AND TABLES rather than a polished writeup. It validates
    the request, plans queries against the data catalogue, runs them, and returns the raw
    result sets for graphing/interpretation on the dashboard.

    How this differs from `generate_research_report`: that tool composes a professional,
    analyst-grade deep-dive (slow, ~10-12 min, email or dashboard). THIS tool is the
    quick exploratory pass — use it to scope the data and iterate on questions, then,
    once the user is satisfied with what they've found, route to `generate_research_report`
    for the full deep-dive on the question they've settled on. Examples that fit here:
    "what data do you have on semiconductor margins?", "show me revenue growth across
    large-cap software", "which sectors have the most earnings revisions lately?".

    `query` is natural language. Results are always delivered to the dashboard. The job
    is rate-limited to 20/hour per user server-side.
    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header.

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll with
    `get_task_status` until SUCCESS, then read its `result` (the query result sets to
    render as charts/tables).
    """
    return await _send(
        ctx, "POST", "/data-catalogue-exploration",
        json={"query": query}, bearer_token=bearer_token,
    )


@mcp.tool()
async def get_task_status(ctx: Context, task_id: str) -> Any:
    """Poll the status of an async job started by generate_report / generate_research_report.

    Returns {"task_id": ..., "status": ..., "result": ...}. `status` is one of
    PENDING, SUCCESS, FAILURE, RETRY. `result` is populated once status is SUCCESS.
    """
    return await _send(
        ctx, "GET", "/task-status",
        params={"task_id": task_id}, require_auth=False,
    )


@mcp.tool()
async def get_latest_report(
    ctx: Context,
    symbols: list[str],
    bearer_token: Optional[str] = None,
) -> Any:
    """Get the latest FlexReport research report(s) for one or more tickers. USE THIS BY DEFAULT.

    ===> THIS IS THE DEFAULT, CORRECT TOOL whenever a user asks for "the report",
    "research", "the latest research", "analysis", "a writeup", or "the PDF" for a
    ticker (e.g. "get me the latest research on SNOW"). It returns the pre-built,
    cached report instantly — fast and cheap. ALWAYS prefer this over generating a
    report on the fly.

    Do NOT use `generate_report` for an ordinary research request — that tool is a LAST
    RESORT (slow, async) for when this report is `missing` or the user explicitly wants a
    CUSTOMIZED report (specific line items, ratios, filing frequency, or other overrides
    the cached report does not already cover). If the user's intent is an OPEN-ENDED or
    THEMATIC QUESTION rather than a request for this existing report, use
    `generate_research_report` instead.

    Accepts one OR many symbols. Returns
    {"result": [{"symbol": "AAPL", "url": "<presigned pdf url>",
                 "report": "<base64 pdf>"}, ...],
    "missing": ["XYZ", ...]}. Each hit carries BOTH representations of the same
    PDF: `url` is a short-lived presigned link (valid ~6h) — hand it to the user
    to download/open the document directly (and prefer it on clients that can't
    handle a large base64 blob); `report` is the inline base64 PDF — decode it to
    read, render, or summarize the report's contents yourself. `missing` lists
    symbols with no cached report (for those, the user may want `generate_report`
    or `onboard_symbol`). Symbols are normalized (uppercased, de-duplicated) by
    the backend.
    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header.
    """
    return await _send(
        ctx, "POST", "/get-cached-reports", json=symbols, bearer_token=bearer_token
    )


# --- Discovery / metadata tools -------------------------------------------
# Let the agent enumerate valid parameter values (event types, report override
# items, sector/industry filters) instead of guessing. All read-only and public.

_OPTION_ENDPOINTS = {
    "event_types": "/list-realtime-event-options",
    "financial_items": "/list-financial-items",
    "financial_ratios": "/list-financial-ratios",
    "sectors": "/get-sectors",
    "institutional_investor_types": "/list-institutional-investor-types",
    "countries": "/list-countries",
    "fiscal_quarter": "/get-fiscal-quarter",
    "market_cap": "/list-marketcap-options",
}


@mcp.tool()
async def list_report_options(
    ctx: Context,
    kind: Literal[
        "event_types",
        "financial_items",
        "financial_ratios",
        "sectors",
        "institutional_investor_types",
        "countries",
        "fiscal_quarter",
        "market_cap",
    ],
) -> Any:
    """Enumerate the valid values for a parameter, straight from the backend.

    Call this BEFORE guessing a parameter value. `kind` selects which catalog:

    - "event_types"                  -> valid `event_type` for `list_realtime_events`
                                        (eps_update, company_update, biggest_mover, ...)
    - "financial_items"              -> line items usable in a report's `overrides`
    - "financial_ratios"             -> ratios usable in `overrides.ratios`
    - "sectors"                      -> valid `sector` filter values
    - "institutional_investor_types" -> valid `overrides.institutional_ownership`
                                        values for `generate_report`
    - "countries"                    -> covered countries
    - "fiscal_quarter"               -> the most recent fiscal quarter being reported
    - "market_cap"                   -> valid `market_cap` buckets (Small-cap,
                                        Medium-cap, Large-cap, Mega-cap)

    Authoritative and never stale: it reads the backend's live config, not a
    hardcoded list.
    """
    return await _send(ctx, "GET", _OPTION_ENDPOINTS[kind], require_auth=False)


@mcp.tool()
async def list_sub_industries(ctx: Context, sectors: list[str]) -> Any:
    """List the sub-industries within one or more sectors.

    `sectors` must be values from `list_report_options("sectors")`. Returns the
    distinct industries used to narrow `list_realtime_events(industry=[...])`.
    """
    return await _send(
        ctx, "GET", "/get-sub-industries",
        params={"sector": sectors}, require_auth=False,
    )


@mcp.tool()
async def list_tickers(ctx: Context, with_names: bool = False) -> Any:
    """List the ticker universe FlexReport covers.

    `with_names=False` returns bare symbols; `with_names=True` returns
    {symbol, company_name} pairs. NOTE: this is the full universe (thousands of
    names) and can be a large payload.
    """
    path = "/list-symbols-with-names" if with_names else "/list-tickers"
    return await _send(ctx, "GET", path, require_auth=False)

@mcp.tool()
async def get_company_snapshot(ctx: Context, symbol: str) -> Any:
    """Fetch a structured company snapshot — no report generation needed.

    Returns thesis/bull/bear, financial overview (Piotroski, valuation signal),
    price performance + technical indicators, price targets, institutional
    ownership, and analyst grades for `symbol`. Synchronous and cheap — prefer
    this for a quick read instead of generating a full PDF report.
    """
    return await _send(
        ctx, "GET", "/get-company-snapshot",
        params={"symbol": symbol}, require_auth=False,
    )


@mcp.tool()
async def onboard_symbol(
    ctx: Context,
    symbol: str,
    bearer_token: Optional[str] = None,
) -> Any:
    """Request onboarding of a NOT-yet-covered ticker (mutating, authenticated).

    Kicks off a 30-60 min backend workflow and emails the authenticated user when
    the first report is ready. Rate-limited to 5/hour per user. Use only when a
    symbol is missing from `list_tickers` / returns no data elsewhere.
    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header.

    Returns {"task_id": ..., "status": "PENDING"}.
    """
    return await _send(
        ctx, "POST", "/onboard-symbol",
        params={"symbol": symbol}, bearer_token=bearer_token,
    )

@mcp.tool()
async def screen_stocks(
    ctx: Context,
    metrics: Optional[dict[str, bool | float]] = None,
    sectors: Optional[list[str]] = None,
    sub_sectors: Optional[list[str]] = None,
    market_cap: Optional[list[str]] = None,
    analyst_ratings: Optional[list[str]] = None,
    institutional_ownership: Optional[dict[str, float]] = None,
    countries: Optional[list[str]] = None,
    price_performance: Optional[dict[str, float]] = None,
    bearer_token: Optional[str] = None,
) -> Any:
    """Screen stocks by financial growth, sector, sub-industry, market cap, analyst ratings, institutional ownership, country, and price performance.

    `market_cap` accepts buckets like "Small-cap", "Medium-cap", "Large-cap".
    Discover valid values with the list tools: `list_report_options` (e.g.
    kind="sectors", "institutional_investor_types") and `list_sub_industries`.

    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header. Rate-limited to 10/hour
    per user server-side.

    Asynchronous: returns a task id. Poll it with `get_task_status` until SUCCESS.
    """
    return await _send(
        ctx, "POST", "/screen-stocks",
        json={
            "metrics": metrics or {},
            "sectors": sectors,
            "sub_sectors": sub_sectors,
            "market_cap": market_cap,
            "analyst_ratings": analyst_ratings,
            "institutional_ownership": institutional_ownership,
            "countries": countries,
            "price_performance": price_performance,
        },
        bearer_token=bearer_token,
    )

@mcp.tool()
async def optimize_portfolio_default(
    ctx: Context,
    symbols: list[str],
    risk_tolerance: Optional[Literal["conservative", "balanced", "aggressive"]] = None,
    bearer_token: Optional[str] = None,
) -> Any:
    """Build a risk-optimized portfolio from a list of tickers — fast, synchronous, no LLM.

    THE DEFAULT optimizer route. Returns the result directly and almost instantly
    (no task id, no polling). Reach for `optimize_portfolio` instead only when you
    explicitly want the slower LLM-curated variant layered on top of the optimizers.

    `risk_tolerance` (conservative | balanced | aggressive) selects which risk
    profile is marked as recommended. Symbols are validated against the covered
    universe (`list_tickers`); unsupported tickers come back in `missing`, and
    in-universe tickers with too little price history come back in `dropped`.

    No auth required (public endpoint). `bearer_token` (a JWT from `get_token`) is
    still forwarded if supplied, but is optional here.

    Synchronous. Returns:
      {"status": "OK",  # or "SKIPPED"/"EMPTY" when too few usable symbols remain
       "holdings": [{"symbol", "source_strategies", "summary", "conviction_level"}, ...],
       "optimizer_results": {"mvo": {...weights+diagnostics}, "hrp": {...}, "mcvar": {...}},
       "risk_profiles": {"profiles": [...], "recommended": "..."},  # present only when status == "OK"
       "dropped": [...],   # in-universe but insufficient price history
       "missing": [...]}   # not in the covered universe
    """
    return await _send(
        ctx, "POST", "/optimize-symbols-non-llm",
        json={
            "symbols": symbols,
            "risk_tolerance": risk_tolerance,
        },
        bearer_token=bearer_token,
        require_auth=False,
    )


@mcp.tool()
async def optimize_portfolio(
    ctx: Context,
    symbols: list[str],
    risk_tolerance: Optional[Literal["conservative", "balanced", "aggressive"]] = None,
    delivery: Optional[str] = "dashboard",
    bearer_token: Optional[str] = None,
) -> Any:
    """Build a risk-optimized portfolio from a non-empty list of tickers.

    Scores `symbols` with the multi-signal scorer, LLM-curates, and risk-optimizes.
    `risk_tolerance` (conservative, balanced, or aggressive) selects which risk
    profile is recommended. `delivery` is the result channel — "dashboard"
    (returned directly) or "email".

    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header. Rate-limited to 10/hour
    per user server-side.

    Asynchronous: returns {"task_id": ..., "status": "PENDING", "supported": [...],
    "not_supported": [...]}. Poll `task_id` with `get_task_status` until SUCCESS.
    """
    return await _send(
        ctx, "POST", "/optimize-symbols",
        json={
            "symbols": symbols,
            "risk_tolerance": risk_tolerance,
            "delivery": delivery,
        },
        bearer_token=bearer_token,
    )


@mcp.tool()
async def get_stock_picks(
    ctx: Context,
    strategy_name: Optional[str] = None,
) -> Any:
    """Fetch the latest LLM-selected stock picks (current rebalance holdings).

    Returns the holdings selected for the most recent rebalance date — the names the
    backend's strategies are currently positioned in. Each pick is a record (ticker,
    strategy, weight, rebalance date, and related fields).

    `strategy_name` optionally narrows to a single strategy (e.g. one of the
    `strategy_update` baskets); omit it to get picks across all strategies. Synchronous
    and read-only — no auth required.
    """
    params = {"strategy_name": strategy_name} if strategy_name else None
    return await _send(
        ctx, "GET", "/get-stock-picks", params=params, require_auth=False
    )

@mcp.tool()
async def predict_earnings_move(
    ctx: Context,
    symbols: list[str],
    bearer_token: Optional[str] = None
)-> Any:
    """
    Predict the magnitude of a stock's move, post-earnings announcement. Returns a list of possibilities, modelling the magnitude under each scenario e.g. if stock beats and raises guidance then expect a 6% magnitude move.
    """
    return await _send(
        ctx, "POST", "/predict-earnings-announcement-move", json={"symbols" : symbols }, bearer_token=bearer_token
    )

@mcp.tool()
async def schedule_task(
    ctx: Context,
    task_name: str,
    task_type: Literal["report", "screener", "research", "events"],
    instructions: dict,
    frequency: Literal["daily", "weekly", "monthly", "quarterly", "custom"] = "weekly",
    regular_cron: Optional[str] = None,
    custom_cron: Optional[str] = None,
    bulk_subscribe: bool = False,
    bearer_token: Optional[str] = None,
) -> Any:
    """Schedule a RECURRING delivery (cron job) of a report, screen, research answer, or events.

    Use this when the user wants something delivered ON A SCHEDULE / repeatedly
    (e.g. "send me the AAPL report every Monday", "screen for cheap large-cap
    industrials monthly", "email me eps updates each morning"). For a ONE-OFF
    request, call the corresponding tool directly instead (get_latest_report /
    generate_report, screen_stocks, generate_research_report, list_realtime_events).

    `task_name` is a human label for the job (also its delete/lookup key).

    `frequency` picks a preset cron: daily (08:00), weekly (Mon 08:00),
    monthly (1st 08:00), quarterly (Jan/Apr/Jul/Oct 1st 08:00). For anything else
    set frequency="custom" and supply ONE of:
      - `regular_cron`  -> a raw 5-field cron expression ("30 6 * * 1-5"). Preferred
                           when you already know the exact cron — used verbatim.
      - `custom_cron`   -> a natural-language schedule ("every weekday at 6:30am"),
                           which the backend converts to cron via an LLM.
    Both are ignored unless frequency="custom"; if you pass both, `regular_cron`
    wins. Exactly one is required when frequency="custom".

    `task_type` selects WHAT gets delivered and the shape of `instructions`:

    - "report"   -> recurring company report. instructions REQUIRES `ticker`; same
                    optional override keys as `generate_report` (e.g.
                    {"ticker": "AAPL", "include_transcript": false, "ratios": [...]}).
    - "screener" -> recurring stock screen. instructions takes the same keys as
                    `screen_stocks`: metrics, sectors, sub_sectors, market_cap,
                    analyst_ratings, institutional_ownership, countries,
                    price_performance (e.g. {"sectors": ["Technology"],
                    "market_cap": ["Large-cap"]}).
    - "research" -> recurring open-ended/thematic research. instructions REQUIRES
                    `query`; optional `delivery` ("dashboard" or "email")
                    (e.g. {"query": "high-growth semis with rising estimates",
                    "delivery": "email"}).
    - "events"   -> recurring earnings/market events. instructions takes the same
                    keys as `list_realtime_events`: event_type (default
                    "eps_update"), tickers, sector, industry, market_cap
                    (e.g. {"event_type": "eps_update", "tickers": ["AAPL","MSFT"]}).

    Discover valid values with the list tools (`list_report_options`,
    `list_sub_industries`) rather than guessing; invalid enum values are rejected
    server-side. `bulk_subscribe=True` subscribes a wider audience instead of just
    the caller — leave it False unless the user explicitly asks.

    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header. Returns 201 on success.
    """
    if frequency == "custom" and not (regular_cron or custom_cron):
        return {"error": "When frequency='custom', supply regular_cron (a 5-field "
                         "cron expression) or custom_cron (a natural-language schedule)."}

    # Pin each task_type to its distinguishing field so the backend's
    # Union[Report, StockScreener, SearchQuery, EarningEvents] resolution can't
    # drift to the wrong branch on a thin payload.
    instr = dict(instructions or {})
    if task_type == "events":
        instr.setdefault("event_type", "eps_update")
    elif task_type == "screener":
        instr.setdefault("metrics", {})

    body: dict[str, Any] = {
        "frequency": frequency,
        "instructions": instr,
        "task_name": task_name,
        "bulk_subscribe": bulk_subscribe,
    }
    if regular_cron:
        body["regular_cron"] = regular_cron
    if custom_cron:
        body["custom_cron"] = custom_cron
    return await _send(
        ctx, "POST", "/schedule-task", json=body, bearer_token=bearer_token
    )


@mcp.tool()
async def list_scheduled_tasks(
    ctx: Context,
    bearer_token: Optional[str] = None,
) -> Any:
    """List the caller's scheduled tasks (cron jobs created via `schedule_task`).

    Returns only the authenticated user's jobs, each as
    {"name", "active", "schedule" (cron string), "args", "kwargs", "enabled",
     "last_run_at", "total_run_count"}. Use `name` as the key to remove a job with
    `delete_scheduled_task`.

    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header.
    """
    return await _send(ctx, "GET", "/get-scheduled-tasks", bearer_token=bearer_token)


@mcp.tool()
async def delete_scheduled_task(
    ctx: Context,
    task_name: str,
    bearer_token: Optional[str] = None,
) -> Any:
    """Delete a scheduled task (cron job) by its name.

    `task_name` is the `name` returned by `list_scheduled_tasks` (the same
    `task_name` used when the job was created via `schedule_task`). Returns
    {"msg": "<task_name> deleted"} on success, or an error if no such task exists.

    `bearer_token` (a JWT from `get_token`) authenticates as that user; omit it to
    use the MCP client's configured Authorization header.
    """
    return await _send(
        ctx, "DELETE", "/delete-scheduled-task",
        params={"task_name": task_name}, bearer_token=bearer_token,
    )

@mcp.tool()
async def list_earnings_announcements(
    ctx: Context,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    symbols: Optional[list[str]] = None,
    industry: Optional[list[str]] = None,
    sector: Optional[list[str]] = None,
    market_cap: Optional[list[str]] = None,
    bearer_token: Optional[str] = None,
) -> Any:
    """List scheduled earnings announcements within a date window, FlexReport names only.

    `start_date` and `end_date` are `YYYY-MM-DD` strings; both default to today when
    omitted (so omit them for "who reports today"). Anything the backend can't parse as
    a date returns a 422 — do not pass other formats. Results are restricted to FlexReport's
    covered universe, so symbols outside coverage are dropped silently.

    Optional filters, all AND-ed together:
    - `symbols`     -> limit to these tickers (e.g. ["AAPL","MSFT"]).
    - `sector`      -> values from `list_report_options("sectors")`.
    - `industry`    -> values from `list_sub_industries([...])`.
    - `market_cap`  -> buckets like "Small-cap", "Medium-cap", "Large-cap", "Mega-cap".
    `sector`, `industry`, and `market_cap` are validated against fixed enums server-side;
    invalid values return a 422, so source them from the tools above rather than guessing.

    Returns a list of announcement records (empty list when nothing matches).

    Requires auth: pass `bearer_token` (a JWT from `get_token`); on 401 re-mint and
    retry. Omit only if the MCP client forwards an Authorization header.
    """
    body: dict[str, Any] = {}
    if start_date:
        body["start_date"] = start_date
    if end_date:
        body["end_date"] = end_date
    if symbols:
        body["symbols"] = symbols
    if industry:
        body["industry"] = industry
    if sector:
        body["sector"] = sector
    if market_cap:
        body["market_cap"] = market_cap
    return await _send(
        ctx, "POST", "/list-upcoming-earnings-announcements", json=body, bearer_token=bearer_token
    )


# --- Auth / registration --------------------------------------------------
# Pre-auth flows (no JWT yet). See the server `instructions` for the full playbook.
#
#   New user:      register_user(email, password)  -> backend emails a link/token
#                  confirm_registration(token)      -> (or user clicks the link)
#                  get_token(email, password)        -> access_token
#   Existing user: get_token(email, password)        -> access_token
#
# Then pass access_token as `bearer_token` on every data tool. Stateless deployment,
# so there is no server-side session/login cache — the token rides each call.

@_pre_auth_tool
async def register_user(ctx: Context, email: str, password: str) -> Any:
    """Register a new FlexReport account (step 1 for new users).

    Pre-auth — no JWT required. The backend emails a confirmation link/token; the
    user clicks the link (or pastes the token to `confirm_registration`) to
    activate, then call `get_token` to obtain a JWT.

    Note: `password` is sent as a tool argument, so it appears in call logs.
    """
    return await _send(
        ctx, "POST", "/auth",
        json={"email": email, "password": password}, require_auth=False,
    )


@_pre_auth_tool
async def confirm_registration(ctx: Context, token: str) -> Any:
    """Confirm a registration with the emailed token (step 2 of the auth flow).

    Pre-auth — no JWT required. `token` is the value emailed by `register_user`.
    """
    return await _send(ctx, "GET", f"/confirm/{token}", require_auth=False)


@_pre_auth_tool
async def get_token(ctx: Context, username: str, password: str) -> Any:
    """Exchange credentials for a bearer JWT (OAuth2 password flow). Pre-auth.

    THE login step. A new user calls this after confirming registration; an
    existing user calls it directly. `username` is the account email.

    Returns {"access_token": "<jwt>", "token_type": "bearer"}. Pass the
    `access_token` as the `bearer_token` argument on every authenticated tool
    (list_realtime_events, get_latest_report, generate_report,
    generate_research_report, onboard_symbol). Re-call this and retry on a 401.

    Note: `password` is sent as a tool argument, so it appears in call logs.
    """
    return await _send(
        ctx, "POST", "/token",
        data={"username": username, "password": password}, require_auth=False,
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_request) -> PlainTextResponse:
    """Liveness probe for load balancers (ALB target-group health check).

    Plain 200 outside the MCP protocol — the `/mcp` path speaks MCP and won't
    return 200 to a bare GET, so point the health check here.
    """
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
