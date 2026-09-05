"""Flexreport MCP server — exposes the equity backend's live events and report
artifacts as on-demand MCP tools over streamable-http.

Each tool is a thin wrapper around a public backend HTTP endpoint. The caller's
inbound `Authorization: Bearer <JWT>` is forwarded so the backend enforces auth,
plan quota, and rate limits. This service holds no credentials and does not touch
AWS/Redis/DB or import anything from the API repo.
"""

import asyncio
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, get_args
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import PlainTextResponse

from client import MissingAuthError, auth_headers, get_client
import situate as situate_mod

# Non-code text (server instructions, auth playbooks) lives in instructions.json
# so the copy can be edited without touching the server logic.
_TEXT = json.loads((Path(__file__).parent / "instructions.json").read_text())

# Auth: OAuth 2.0 Resource Server, always on. The SDK serves
# /.well-known/oauth-protected-resource and returns 401 + WWW-Authenticate challenges
# pointing clients at the backend Authorization Server (issuer_url); only RS256 tokens
# validated against the backend JWKS are accepted.
from mcp.server.auth.settings import AuthSettings

from auth_verifier import (
    MCP_RESOURCE_URL,
    OAUTH_ISSUER,
    FlexReportTokenVerifier,
)

INSTRUCTIONS = _TEXT["instructions"].replace("{auth_block}", _TEXT["auth_block_oauth"])

_token_verifier = FlexReportTokenVerifier()
_auth_settings = AuthSettings(
    issuer_url=OAUTH_ISSUER,
    resource_server_url=MCP_RESOURCE_URL,
    required_scopes=[],  # backend enforces scope/plan; don't gate at the transport
)
# --- Discovery / metadata catalogues ---------------------------------------
# One tool (`list_options`) enumerates valid parameter values (event types,
# report override items, sectors, tickers, indicators, ...) instead
# of a standalone tool per catalogue. All read-only and public.

_OPTION_ENDPOINTS = {
    "event_types": "/list-realtime-event-options",
    "financial_items": "/list-financial-items",
    "financial_ratios": "/list-financial-ratios",
    "sectors": "/get-sectors",
    "institutional_investor_types": "/list-institutional-investor-types",
    "countries": "/list-countries",
    "fiscal_quarter": "/get-fiscal-quarter",
    "market_cap": "/list-marketcap-options",
    "intraday_frequency": "/list-intraday-chart-options",
    "technical_indicators": "/list-technical-indicators",
    "tickers": "/list-tickers",
    "tickers_with_names": "/list-symbols-with-names",
    # Report vocabularies scoped by a query parameter (see _OPTION_QUERY_PARAMS).
    "as_reported_items": "/list-as-reported-items",
    "revenue_segments": "/list-revenue-segments",
    "institutional_managers": "/list-institutional-managers",
}

# Catalogues that take a query parameter: a filer's own vocabularies (its tagged XBRL
# concepts, its revenue segments) need a `ticker`; the manager lookup needs a name
# fragment `q` and/or a SEC `cik`. `list_options` forwards whichever were passed and
# refuses the call when none of the kind's parameters is present.
_OPTION_QUERY_PARAMS: dict[str, tuple[str, ...]] = {
    "as_reported_items": ("ticker",),
    "revenue_segments": ("ticker",),
    "institutional_managers": ("q", "cik"),
}

# The `kind` schema advertised to MCP clients. Members must stay in lockstep
# with the dict keys above (guarded by the assert).
_OptionKind = Literal[
    "event_types",
    "financial_items",
    "financial_ratios",
    "sectors",
    "institutional_investor_types",
    "countries",
    "fiscal_quarter",
    "market_cap",
    "intraday_frequency",
    "technical_indicators",
    "tickers",
    "tickers_with_names",
    "as_reported_items",
    "revenue_segments",
    "institutional_managers",
]
assert set(get_args(_OptionKind)) == set(_OPTION_ENDPOINTS)
assert set(_OPTION_QUERY_PARAMS) <= set(_OPTION_ENDPOINTS)

# The fields that shape a CUSTOM company report — Report.OVERRIDE_FIELDS on the backend.
# Setting any of them turns /create-full-report into a full rebuild (~10 min, never
# cached) and the backend rejects them (422) unless `user_override` is also true.
_REPORT_SHAPING_FIELDS = (
    "financial_items", "as_reported_financial_items", "ratios", "revenue_segment",
    "technical_analysis_items", "estimate_items", "institutional_ownership",
    "include_as_report_financials", "as_reported_periods",
)

mcp = FastMCP(
    "flexreport",
    instructions=INSTRUCTIONS,
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    # Behind a load balancer (ALB): make each request self-contained instead of
    # holding a long-lived per-session SSE stream the LB would choke on, and return
    # plain JSON rather than text/event-stream. Stateless mode has no persistent
    # session, so auth is per-call (the validated OAuth bearer), not a cache.
    stateless_http=True,
    json_response=True,
    auth=_auth_settings,
    token_verifier=_token_verifier,
)


def _inbound_request(ctx: Context):
    """Best-effort fetch of the inbound Starlette Request from the MCP context."""
    return getattr(ctx.request_context, "request", None)


async def _send(
    ctx: Context,
    method: str,
    path: str,
    *,
    require_auth: bool = True,
    raw: bool = False,
    **kwargs: Any,
) -> Any:
    """Forward a request to the backend, returning parsed JSON or a structured error.

    Auth (handled by auth_headers): the validated inbound OAuth bearer is
    forwarded to the backend on every call.

    With `raw=True`, a 2xx response's body is returned as bytes (for binary endpoints
    like a PDF download) instead of being parsed as JSON.

    Errors (missing auth, transport failure, non-2xx) are returned as a dict with
    an "error" key rather than raised, so the agent receives a clean, readable message.
    """
    try:
        headers = auth_headers(_inbound_request(ctx), required=require_auth)
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
            msg += " — not authenticated / token expired. Your MCP client should re-run the OAuth sign-in flow."
        return {"error": msg, "detail": detail}

    if raw:
        return resp.content

    try:
        return resp.json()
    except Exception:
        return {"error": "Backend returned a non-JSON response", "detail": resp.text}


_ONTOLOGY_CACHE: dict[str, tuple[float, Any]] = {}
_ONTOLOGY_CACHE_TTL_S = 600.0


async def _ontology_graph(ctx: Context) -> Any:
    """The unfiltered event ontology (~230 KB, symbol-independent), cached in-process for
    10 minutes so `situate` costs one backend call per 10 minutes, not one per event type."""
    hit = _ONTOLOGY_CACHE.get("all")
    if hit and time.monotonic() - hit[0] < _ONTOLOGY_CACHE_TTL_S:
        return hit[1]
    res = await _send(ctx, "GET", "/get-event-ontology", params={"format": "cards"}, require_auth=False)
    if isinstance(res, dict) and "error" not in res and not res.get("degraded"):
        _ONTOLOGY_CACHE["all"] = (time.monotonic(), res)
    return res


@mcp.tool(annotations=ToolAnnotations(title="Situate: What Is Going On Right Now", readOnlyHint=True))
async def situate(
    ctx: Context,
    symbols: Optional[list[str]] = None,
    window_days: Optional[int] = None,
    question: Optional[str] = None,
) -> Any:
    """THE FIRST CALL. What is going on right now — for the market, or for the named symbols — and a ready-made PLAN of the tool calls that answer the user's question. Call it before any other flexreport tool on anything about a company, an event, or "now".

    ===> Call this FIRST for: "catch me up on X", "what happened / why", "anything
    before earnings?", "any earnings / 8-Ks / 13F moves / movers today?", "how is X
    trading", and BEFORE aiming explore_data_catalogue, list_realtime_events or a
    report tool at a company. Pass the user's question verbatim in `question` (it is
    matched by keyword to an event family, never sent to an LLM). Synchronous, public.
      situate(symbols=["OKTA"], question="catch me up on OKTA")   one or more names (max 5)
      situate(question="any earnings events today?")             no symbols = the market

    It reads the company event web (what HAPPENED to this company), the event ontology
    (what that KIND of event entails, what follows it and how soon, where its payload
    lives after the 12h realtime cache, when each table next refreshes) and market
    status, and composes them into ONE response:
      market          exchange, is_open, session
      symbols[S]      web (newest 12 nodes; full graph via get_company_event_web),
                      newest_event, episode (the DOMINANT episode in the window — its
                      anchor and each member arrived | pending | overdue with rate and
                      expected_by), cache.present / cache.expired (inferred from node
                      age vs the TTL), freshness[] (per relation: last_refresh_at,
                      next_run_at, reflects_newest_event), cards (trimmed ontology
                      cards), deepen (tools the ontology says deepen this event)
      universe        (no symbols) the family's episode order and cards
      plan[]          ORDERED tool calls: {step, tool, args, why, auth, sync, optional,
                      provenance, fallback}. `tool` is an exact tool name and `args`
                      are valid for it — pass them through unchanged.
      skip[]          calls that would return nothing or waste a job (an event type
                      that aged out of the cache, a 10-minute report) — DO NOT make these.
      guidance[]      situation-scoped rules: which tables have NOT refreshed since the
                      event, which followers are overdue, filer-scoped 13F nodes.

    HOW TO EXECUTE:
      1. Run plan[] in order. `sync=false` steps return a task_id — poll
         get_task_status. If a step errors (e.g. drilldown not-authenticated), use its
         `fallback`.
      2. Never make a call that appears in skip[]; quote its `why` if the user asks.
      3. Read guidance[] before writing: an "overdue" follower is "not recorded yet",
         not "did not happen"; a relation with reflects_newest_event=false shows the
         PRIOR period, never the reaction.
      4. Go beyond the plan only for what it did not cover — then get_company_event_web
         (full graph, wider window) and get_event_ontology (full card / relation /
         family) tell you what to call next.
    Skip this tool ONLY for pure enumeration (list_options), an explicit "get me the
    existing report" (get_latest_report), or a metric-history question with no event
    in it ("Micron EPS growth over 8 quarters" -> explore_data_catalogue).

    degraded=true means a source was unavailable (ontology not built, a web errored);
    the plan still runs on what remains — follow it, and say what was unknown.
    """
    now = datetime.now(timezone.utc)
    qfam = situate_mod.question_family(question)
    onto_task = _ontology_graph(ctx)
    market_task = _send(ctx, "GET", "/is-market-open", params={"exchange": "NYSE"}, require_auth=False)
    syms = [s.strip().upper() for s in (symbols or []) if s and s.strip()][:5]
    web_tasks = [
        _send(ctx, "GET", "/get-company-event-web",
              params={"symbol": s, **({"window_days": window_days} if window_days else {})},
              require_auth=False)
        for s in syms
    ]
    onto, market_raw, *webs = await asyncio.gather(onto_task, market_task, *web_tasks)

    market = situate_mod.parse_market(market_raw)
    degraded: list[str] = []
    if market.get("error"):
        degraded.append(f"market status unavailable: {market['error']}")
    if not isinstance(onto, dict) or "error" in onto or onto.get("degraded"):
        degraded.append("event ontology unavailable — followers, freshness and persisted_in are unknown; "
                        "plan built from the web's fetch hints only")
        cards, relations, episodes, ttl, onto_as_of = {}, {}, {}, situate_mod.DEFAULT_TTL_HOURS, None
    else:
        cards = onto.get("event_types") or {}
        relations = onto.get("relations") or {}
        episodes = onto.get("episodes") or {}
        ttl = int(onto.get("realtime_cache_ttl_hours") or situate_mod.DEFAULT_TTL_HOURS)
        onto_as_of = onto.get("as_of")

    plan: list = []
    skip: list = []
    guidance: list = []
    symbol_blocks: dict[str, Any] = {}
    universe_block = None
    if syms:
        for s, web in zip(syms, webs):
            block, p, k, g = situate_mod.situate_symbol(
                s, web, cards, relations, episodes, ttl, market, now, qfam)
            symbol_blocks[s] = block
            plan += p; skip += k; guidance += g
            if block.get("error"):
                degraded.append(f"{s}: event web errored ({block['error']})")
    else:
        fam = qfam or "earnings"
        universe_block, p, k, g = situate_mod.situate_universe(fam, cards, episodes, market, now)
        plan += p; skip += k; guidance += g

    return situate_mod.compose(
        "symbol" if syms else "universe", market, ttl, now, symbol_blocks, universe_block,
        plan, skip, guidance, degraded, onto_as_of,
    )


@mcp.tool(annotations=ToolAnnotations(title="List Real-Time Market Events", readOnlyHint=True))
async def list_realtime_events(
    ctx: Context,
    event_type: str,
    tickers: Optional[list[str]] = None,
    sector: Optional[list[str]] = None,
    industry: Optional[list[str]] = None,
    market_cap: Optional[list[str]] = None,
) -> Any:
    """Pull live market events from the backend's Redis-backed cache (12h TTL).

    ===> Call `situate(...)` FIRST (symbols=[...] for named companies, none for the
    market): its `cache.present` / `cache.expired` say which types this tool can
    still return, its `plan` carries the exact calls to make, and its `skip` list
    names the ones that would return [] — an event older than the 12h cache lives
    in the ontology card's `persisted_in` relation, not here. Never call this for a
    type situate marked expired. `list_options("event_types")` remains the
    enumeration of valid type strings and descriptions. `event_type` is REQUIRED and
    has no default on purpose: the type is the whole question, and a default
    would answer a different one (eps_update is one slice of the earnings
    family, not the family).

    Then CONNECT THE DOTS: fetch the type asked about and follow the situation
    across its related_events with follow-up calls narrowed by `tickers=[...]`
    to the symbols just seen — e.g. eps_update -> check the intraday tape
    (detect_intraday_outlier_jumps) -> 8k_release -> ir_publication. One hop
    answers most questions; two covers a full earnings cycle. An unfiltered
    follow-up call re-pulls the whole cache.

    Optionally narrow results by `tickers`, `sector`, `industry`, or `market_cap`
    (e.g. market_cap=["Large-cap","Mega-cap"]). Returns a list of event objects.
    An EMPTY list means the cache is cold for that event type (12h TTL), not
    that nothing happened.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
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
    return await _send(ctx, "POST", "/get-realtime-events", json=body)



@mcp.tool(annotations=ToolAnnotations(title="Generate Stock Report", readOnlyHint=False, destructiveHint=False))
async def generate_report_for_stock(
    ctx: Context,
    ticker: str,
    user_override: bool = False,
    financial_items: Optional[list[str]] = None,
    as_reported_financial_items: Optional[list[str]] = None,
    ratios: Optional[list[str]] = None,
    revenue_segment: Optional[list[str]] = None,
    technical_analysis_items: Optional[list[str]] = None,
    estimate_items: Optional[list[str]] = None,
    institutional_ownership: Optional[list[str]] = None,
    include_as_report_financials: bool = False,
    as_reported_periods: Optional[list[str]] = None,
) -> Any:
    """Build a report for ONE ticker. Two modes — pick by what the user asked for.

    STANDARD (ticker only, ~10-20 seconds): renders the symbol's saved report plan — the
    query plan the platform's ETLs built the first time anyone asked for the symbol and
    refresh nightly while its inputs keep changing. Call it this way when
    `get_latest_report` came back `stale: true` for the ticker (the cached PDF predates
    the symbol's latest report inputs — a print, a filing, a 13F refresh) or listed it
    in `missing`. The finished PDF replaces the cached one. If the saved plan is itself
    stale or absent, the backend rebuilds it (~10 min) and saves it for next time — so
    poll the task rather than assume a fixed runtime.

    CUSTOM (`user_override=true` + the shaping fields, ~10 minutes, NOT cached): a full
    rebuild from scratch around what the user named. Use it ONLY when the user
    EXPLICITLY wants the report to cover specific line items, ratios, segments,
    indicators, estimates, or holders. Every field below shapes a custom report, and the
    backend rejects it (422) without `user_override=true` — this tool sets the switch
    for you whenever any of them is non-empty, so a shaped request is never silently
    served the standard report. Vocabularies — never guess, look each one up:
      financial_items              standardized EDGAR line items ("revenue",
                                   "operatingIncome") -> list_options("financial_items")
      ratios                       EDGAR ratios ("grossProfitMargin")
                                   -> list_options("financial_ratios")
      as_reported_financial_items  the filer's OWN tagged XBRL concepts
                                   ("us-gaap_RevenueFromContractWithCustomer...", or a
                                   company extension such as "snow_...")
                                   -> list_options("as_reported_items", ticker=...)
      revenue_segment              the filer's revenue segments ("Product revenue")
                                   -> list_options("revenue_segments", ticker=...)
      technical_analysis_items     indicators to chart ("rsi", "macd")
                                   -> list_options("technical_indicators")
      estimate_items               estimate line items to compare against actuals
      institutional_ownership      managers to feature, by SEC CIK (any width; the
                                   backend pads to 10 digits)
                                   -> list_options("institutional_managers", q="berkshire")
                                   returns {cik, name, category, aum}; pass the `cik`
                                   values, never names
      include_as_report_financials adds an AS-REPORTED statement section built from the
                                   SEC filing itself, every figure a tagged XBRL fact
                                   that deep-links to its location in the filed
                                   document (distinct from the standardized tables,
                                   whose derived figures no filer states). Line items:
                                   `as_reported_financial_items` when given, else
                                   chosen to match the narrative
      as_reported_periods          which window(s) that section shows: "quarter"
                                   (discrete three months, the default), "ytd"
                                   (cumulative), "annual" (fiscal year, from the
                                   10-K). Several render as adjacent columns. Not
                                   cosmetic: a filing states the same concept over
                                   more than one window at the SAME period end, so
                                   the basis must be explicit

    Event context is SERVER-owned: the backend reads the symbol's latest platform event
    and frames the report around it. There is no thesis / change-summary / include_* /
    price-date field any more — do not send editorial steer; unknown keys are ignored.

    CHOOSING THE SHAPING FIELDS FROM THE ONTOLOGY (custom mode only — do not guess):
    read get_company_event_web(ticker) for the latest node, then
    get_event_ontology(event_type=node.type), and derive from the card's family and
    `informs`:
      earnings family          -> financial_items + estimate_items from the fundamentals
                                  and estimates families the card informs; add
                                  include_as_report_financials when the user wants the
                                  filed statement (a 10-Q / 10-K anchored event)
      institutional_ownership  -> institutional_ownership CIKs from the web node's
                                  fetch.cik (a 13F node is FILER-scoped: one filer's
                                  move across several names)
      market_movement / news   -> technical_analysis_items from the technical_indicators
                                  family (validate names via list_options)
      analyst_activity         -> estimate_items + ratios
    Omit anything the ontology gives no reason for.

    Not `generate_research_report`: that answers an open-ended or multi-company
    question (peer comps, a theme). This tool is one ticker — and in custom mode, a
    very clear list of what to cover.

    Asynchronous. Returns {"<TICKER>": {"task_id": "...", "status": "PENDING"}}: read
    result[ticker]["task_id"] and poll it with `get_task_status` until SUCCESS; the
    result carries the finished report as {"pdf": "<base64>", ...}. Requires auth
    (your MCP client attaches the OAuth bearer automatically).
    """
    shaping: dict[str, Any] = {
        "financial_items": financial_items,
        "as_reported_financial_items": as_reported_financial_items,
        "ratios": ratios,
        "revenue_segment": revenue_segment,
        "technical_analysis_items": technical_analysis_items,
        "estimate_items": estimate_items,
        "institutional_ownership": institutional_ownership,
        "include_as_report_financials": include_as_report_financials,
        "as_reported_periods": as_reported_periods,
    }
    assert set(shaping) == set(_REPORT_SHAPING_FIELDS)
    payload: dict[str, Any] = {"ticker": ticker}
    # Only non-empty fields travel: the backend defaults every list to [] and every flag
    # to false, and treats a set field as "custom". Any shaped request carries the
    # switch so it is never rejected with a 422 nor quietly served the standard plan.
    payload.update({k: v for k, v in shaping.items() if v})
    if user_override or len(payload) > 1:
        payload["user_override"] = True
    return await _send(
        ctx, "POST", "/create-full-report", json=payload
    )


@mcp.tool(annotations=ToolAnnotations(title="Generate Research Report", readOnlyHint=False, destructiveHint=False))
async def generate_research_report(
    ctx: Context,
    query: str,
    delivery: str = "email",
) -> Any:
    """Answer an OPEN-ENDED or THEMATIC research QUESTION (extensible, multi-section).

    ===> THE RIGHT TOOL when the user's intent is a QUESTION rather than a request for an
    existing report — an exploratory or thesis-style ask about a ticker, or a market-wide
    theme not tied to one company (e.g. "Are large caps driving earnings season?",
    "What's the bull/bear case on NVDA?", "high-growth semis with rising estimates").
    For a plain "get me the latest report/research on <ticker>", use `get_latest_report`
    instead. This is the slow, professional DEEP-DIVE — reach for it only when the user
    EXPLICITLY asks for a full writeup, not for an ordinary exploratory question.

    If the user only wants to EXPLORE the data or "use Flexreport to explore...", do NOT
    start here — call `explore_data_catalogue` first (interactive charts/tables, usually a
    few minutes rather than ~10-12) and
    reach for THIS deep-dive only later, once the user has reviewed the exploration and
    EXPLICITLY asks for the full report. Never run both for the same question at once.

    `query` is natural language, e.g. "high-growth semis with rising estimates".
    `delivery` defaults to "email". Rate-limited to 20/hour per user server-side.

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll with
    `get_task_status` until SUCCESS, then read its `result`.
    """
    return await _send(
        ctx, "POST", "/generate-research-report",
        json={"query": query, "delivery": delivery},
    )


@mcp.tool(annotations=ToolAnnotations(title="Explore Data Catalogue", readOnlyHint=False, destructiveHint=False))
async def explore_data_catalogue(
    ctx: Context,
    query: str,
) -> Any:
    """Explore Flexreport's data platform with an OPEN-ENDED question — designed to handle many research based or open ended questions, enabling interactive EDA.

    ===> For anything EVENT-DRIVEN (a print, a filing, a 13F move, news, "what
    happened"), call `situate(...)` first and take this tool's `query` from its plan —
    the plan names the relations that HAVE refreshed since the event and omits the
    ones that have not. Otherwise:

    ===> THE DEFAULT, FIRST-STEP tool whenever the user wants to EXPLORE the data or
    understand a topic from the data (e.g. "use Flexreport to explore...", "how are
    small caps doing — explore the data", "how have semiconductor margins trended?").
    It validates the request, plans queries against the data catalogue, runs them, and
    returns the raw result sets to render as INTERACTIVE CHARTS AND TABLES on the
    dashboard. Coverage questions are NOT this tool: for an enumeration ("which
    symbols / sectors / indicators are available?", "is X covered?") use
    `list_options` — instant, no async job; for a coverage COUNT or breakdown ("how
    many symbols are in the investor relations dataset?", "what's the count by
    sector?") use `explore_data_coverage`, the dedicated public tool. Reach for THIS
    tool when the user wants the DATA itself — values, history, trends, comparisons.

    *** RUN THIS BY ITSELF FIRST. DO NOT ALSO LAUNCH `generate_research_report` FOR THE
    SAME QUESTION. *** These two tools are SEQUENTIAL STEPS, never parallel:
      1. `explore_data_catalogue` (this tool) — the LEAN route: a validate -> plan -> run
         pipeline with no report-rendering step. Usually finishes in about a minute, but
         run time varies with the tables queried and can reach ~5 minutes. Poll it, SHOW
         the user the resulting charts/tables, and let them iterate.
      2. `generate_research_report` — the slow (~10-12 min), professional analyst-grade
         deep-dive. Reach for it ONLY LATER, AFTER the user has seen the exploration and
         EXPLICITLY asks for the full report. Firing both at once wastes a ~10-min job,
         burns the 20/hour budget, and produces confusing duplicate polling.
    There is no `delivery` argument here: results always go to the dashboard so the user
    can interact with them. The job is rate-limited to 20/hour per user server-side.

    `query` is natural language.

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll a SINGLE task
    with `get_task_status` until SUCCESS (the result is a plain dict — there is NO nested
    task to chase). Expect roughly 1-5 minutes depending on the tables queried — a task
    still PENDING after a couple of minutes is normal, so keep polling. On SUCCESS,
    `result` is:
      {"user_query": "...", "delivery": "dashboard",
       "results": {"<query name>": {"description": "...", "columns": [...],
                                     "rows": [...the FULL result set...],
                                     "row_count": N}, ...}}
    `rows` is NOT sampled — it carries every row (`row_count` matches len(rows)), so
    charts/tables reflect the whole population.

    Each entry ALSO carries `query_token`: the planned SQL sealed into an opaque token.
    It is not readable and never for display — its one use is `save_user_query`, which
    stores the tokens so `run_saved_query` can replay THIS EXACT request later in
    seconds instead of re-planning it. Offer that when the user signals they will want
    the question again (a recurring check, a dashboard they watch); a saved row also
    spares the 20/hour budget. And before running this tool for something the user has
    asked before, check `list_saved_queries` — the answer may already be one fast
    replay away.

    Render each entry as a chart/table for the user. If the request can't be served from
    the platform's data, `result` instead carries a `validation_status` of "REJECTED"
    (with a reason) — relay that rather than retrying blindly.

    EVENT-DRIVEN QUESTIONS: when the question is about something that just HAPPENED
    (an earnings print, a 13F move, a downgrade, a news item), consult
    get_event_ontology(event_type=...) BEFORE writing the query and name the relations
    from its `entails.<layer>` in the query text (prefer gold / analysis relations over
    base tables). Skip any relation whose refreshed_by[].schedule[].next_run_at is
    after the event — it does not hold the event yet and the query will show the prior
    period as if it were the reaction.
    """
    return await _send(
        ctx, "POST", "/data-catalogue-exploration",
        json={"query": query},
    )


@mcp.tool(annotations=ToolAnnotations(title="Explore Data Coverage", readOnlyHint=False, destructiveHint=False))
async def explore_data_coverage(
    ctx: Context,
    query: str,
) -> Any:
    """Answer a question about WHAT THE PLATFORM COVERS — how many symbols a dataset holds, broken down any way you like.

    ===> THE TOOL for coverage COUNTS and BREAKDOWNS: "how many symbols are covered in
    the investor relations dataset?", "what's that count by sector — how many tech
    names?", "how many companies have earnings transcripts / 13F ownership / insider
    activity / analyst estimates?", "how far back does the fundamentals history go?",
    "what's your UK coverage by sector?". It plans queries over BOTH the catalogue's
    own metadata and the datasets themselves, so it can count what no enumeration
    endpoint knows: which symbols actually carry a PARTICULAR dataset, grouped by
    sector, industry, country, market cap, or year.

    PUBLIC at the backend — no account, plan, or entitlement is needed to answer a
    coverage question, by design: a prospect evaluating the platform should be able to
    ask what it covers. (The MCP connection itself still carries the client's OAuth
    session — that gate is transport-wide, not this tool's.) Never treat a coverage
    question as plan-gated or quota-gated, and never tell the user it needs an upgrade.

    Pick between the three coverage-adjacent routes by what the answer looks like:
      - a LIST of what exists ("which sectors / indicators are available?", "is X
        covered?") -> `list_options`. Instant, no async job.
      - a COUNT or breakdown of coverage -> THIS tool.
      - the DATA itself (values, history, trends, comparisons) ->
        `explore_data_catalogue`.
    Do NOT answer a coverage count from memory, from the "2,900+ companies / ~420
    indices" headline figures, or by eyeballing the length of a `list_options`
    payload — those are universe-wide totals and say nothing about per-dataset
    coverage. Run this tool.

    There is no `delivery` argument: results always go to the dashboard so the user can
    interact with them. Rate-limited to 20/hour server-side.

    `query` is natural language — pass the coverage question as asked ("count the
    distinct symbols in the investor relations dataset, broken out by sector").

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll a SINGLE task
    with `get_task_status` until SUCCESS (the result is a plain dict — there is NO
    nested task to chase). Expect roughly 1-5 minutes depending on the tables queried —
    a task still PENDING after a couple of minutes is normal, so keep polling. On
    SUCCESS, `result` is:
      {"user_query": "...", "delivery": "dashboard",
       "results": {"<query name>": {"description": "...", "columns": [...],
                                     "rows": [...the FULL result set...],
                                     "row_count": N}, ...}}
    `rows` is NOT sampled or truncated — every bucket of a breakdown comes back
    (`row_count` matches len(rows)), so the totals you report are the real ones.
    Render each entry as a chart/table for the user. If the request can't be served
    from the platform's data, `result` instead carries a `validation_status` of
    "REJECTED" (with a reason) — relay that rather than retrying blindly.
    """
    return await _send(
        ctx, "POST", "/data-coverage-exploration",
        json={"query": query}, require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Async Task Status", readOnlyHint=True))
async def get_task_status(ctx: Context, task_id: str) -> Any:
    """Poll the status of an async job (generate_research_report, explore_data_catalogue, explore_data_coverage, screen_stocks, ...).

    Returns {"task_id": ..., "status": ..., "result": ...}. `status` is one of
    PENDING, SUCCESS, FAILURE, RETRY. `result` is populated once status is SUCCESS.
    """
    return await _send(
        ctx, "GET", "/task-status",
        params={"task_id": task_id}, require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Latest Cached Reports", readOnlyHint=True))
async def get_latest_report(
    ctx: Context,
    symbols: list[str],
) -> Any:
    """Get the latest Flexreport research report(s) for one or more tickers. USE THIS BY DEFAULT.

    ===> THIS IS THE DEFAULT, CORRECT TOOL whenever a user asks for "the report",
    "research", "the latest research", "analysis", "a writeup", or "the PDF" for a
    ticker (e.g. "get me the latest research on SNOW"). It returns the pre-built,
    cached report instantly — fast and cheap. ALWAYS prefer this over generating a
    report on the fly.

    If the user's intent is an OPEN-ENDED or exploratory QUESTION rather than a request
    for this existing report, do NOT use this tool — default to `explore_data_catalogue`
    (fast, interactive), and reach for `generate_research_report` only when the user
    EXPLICITLY asks for a full deep-dive writeup.

    Accepts one OR many symbols. Returns
    {"result": [{"symbol": "AAPL", "url": "<presigned pdf url>",
                 "report": "<base64 pdf>", "generated_at": "<iso utc>",
                 "age_hours": 5.2, "latest_event_at": "<iso>", "stale": false}, ...],
    "missing": ["XYZ", ...]}. Each hit carries BOTH representations of the same
    PDF: `url` is a short-lived presigned link (valid ~6h) — hand it to the user
    to download/open the document directly (and prefer it on clients that can't
    handle a large base64 blob); `report` is the inline base64 PDF — decode it to
    read, render, or summarize the report's contents yourself. Symbols are
    normalized (uppercased, de-duplicated) by the backend.

    FRESHNESS — read `stale` BEFORE you hand a report over. Nothing regenerates on
    its own under the on-demand model; the flag tells you when to ask for a rebuild:
      stale: false -> the PDF reflects the symbol's latest report inputs. Serve it.
      stale: true  -> the PDF predates the symbol's latest report inputs — a print, a
                      filing, a 13F refresh landed after `generated_at`
                      (`latest_event_at` says when). Call
                      generate_report_for_stock(ticker) — ticker ONLY, no shaping
                      fields — which renders the symbol's saved plan in ~10-20 s;
                      poll its task_id with `get_task_status` and hand the user THAT
                      report. Say the cached copy was out of date and is being
                      refreshed; offer the stale one only if they cannot wait.
      stale: null  -> no report inputs are known for the symbol, so freshness cannot
                      be judged. Serve the PDF and quote `generated_at` / `age_hours`.
      in `missing` -> no cached report at all. generate_report_for_stock(ticker)
                      builds one (a symbol's FIRST build is a full ~10 min run and
                      is saved for next time), or `onboard_symbol` if the ticker is
                      not covered.
    """
    return await _send(
        ctx, "POST", "/get-cached-reports", json=symbols
    )


@mcp.tool(annotations=ToolAnnotations(title="Download Report PDF", readOnlyHint=True))
async def download_pdf_from_url(
    ctx: Context,
    url: str,
    file_name: str,
) -> Any:
    """Fetch a presigned S3 PDF URL server-side and return the document as base64.

    Use this to pull the actual PDF bytes for a presigned S3 link — e.g. the `url`
    returned by `get_latest_report` — on clients that can't open the link directly and
    need the document inline. The backend fetches the URL for you (clients never have
    to reach S3) and streams the PDF back. Only S3 URLs are accepted: the host must end
    in "amazonaws.com" and contain "s3"; any other host is rejected with a 400 (SSRF
    guard). NOTE: if you already have `get_latest_report`'s `report` field (the same PDF
    inline as base64), use that directly — there's no need to call this.

    `url` is the presigned S3 link; `file_name` is the download filename to label the
    document (e.g. "AAPL.pdf"). Returns
    {"file_name": ..., "media_type": "application/pdf", "report": "<base64 pdf>"} —
    decode `report` to read, render, or save the PDF.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    pdf = await _send(
        ctx, "POST", "/download-pdf-from-url",
        json={"url": url, "file_name": file_name}, raw=True,
    )
    if isinstance(pdf, dict):  # _send returned a structured error, pass it through
        return pdf
    return {
        "file_name": file_name,
        "media_type": "application/pdf",
        "report": base64.b64encode(pdf).decode("ascii"),
    }


@mcp.tool(annotations=ToolAnnotations(title="List Valid Parameter Options", readOnlyHint=True))
async def list_options(
    ctx: Context,
    kind: _OptionKind,
    ticker: Optional[str] = None,
    q: Optional[str] = None,
    cik: Optional[str] = None,
) -> Any:
    """Enumerate the valid values for a parameter, straight from the backend.

    Call this BEFORE guessing a parameter value. `kind` selects which catalog; three
    kinds are scoped by a query parameter (`ticker`, or `q` / `cik`) and refuse the call
    without it:

    - "event_types"                  -> valid `event_type` for `list_realtime_events`
                                        (eps_update, company_update, biggest_mover, ...)
    - "financial_items"              -> standardized EDGAR line items ("revenue",
                                        "operatingIncome") for a CUSTOM report's
                                        `financial_items` (`generate_report_for_stock`,
                                        or a scheduled "create-full-report" step)
    - "financial_ratios"             -> EDGAR ratios ("grossProfitMargin") for that
                                        report's `ratios`
    - "as_reported_items"            -> NEEDS `ticker`. The filer's OWN tagged XBRL
                                        concepts from its 10-Q / 10-K filings — {concept,
                                        statement_type, label, standard_concept,
                                        latest_period, facts} — for a custom report's
                                        `as_reported_financial_items`
    - "revenue_segments"             -> NEEDS `ticker`. The filer's revenue segments as
                                        tagged in its filings — {product_label,
                                        product_member, product_axis, concept,
                                        latest_period, facts} — for a custom report's
                                        `revenue_segment` (pass the product_label).
                                        A name lookup only: never sum members across it
    - "institutional_managers"       -> NEEDS `q` (name fragment, e.g. "berkshire")
                                        and/or `cik` (SEC CIK, any width). Managers as
                                        {cik, name, category, aum}, largest AUM first —
                                        pass the `cik` values as a custom report's
                                        `institutional_ownership`, never the names
    - "sectors"                      -> valid `sector` filter values
    - "institutional_investor_types" -> investor CATEGORIES (not managers) — the keys
                                        `screen_stocks(institutional_ownership=...)`
                                        filters on
    - "countries"                    -> covered countries
    - "fiscal_quarter"               -> the most recent fiscal quarter being reported
    - "market_cap"                   -> valid `market_cap` buckets (Small-cap,
                                        Medium-cap, Large-cap, Mega-cap)
    - "intraday_frequency"           -> supported intraday chart frequencies
    - "technical_indicators"         -> indicator names accepted by
                                        `get_technical_indicator_data` (rsi, macd,
                                        sma_50, ...); an unknown indicator there
                                        returns a 400 listing this set
    - "tickers"                      -> the full covered symbol universe as bare
                                        symbols — equities PLUS ~420 market indices
                                        as caret-prefixed symbols (^GSPC, ^VIX, ...).
                                        NOTE: thousands of names — a large payload
    - "tickers_with_names"           -> {symbol, company_name} pairs for the company
                                        universe ONLY — market indices are NOT in
                                        this list, only in "tickers" (an even larger
                                        payload)

    Authoritative and never stale: it reads the backend's live config, not a
    hardcoded list. Public — no auth required.
    """
    wanted = _OPTION_QUERY_PARAMS.get(kind, ())
    given = {"ticker": ticker, "q": q, "cik": cik}
    params = {name: given[name] for name in wanted if given[name]}
    if wanted and not params:
        return {"error": f'list_options("{kind}") needs {" and/or ".join(wanted)} — '
                         "pass it as a keyword argument."}
    return await _send(
        ctx, "GET", _OPTION_ENDPOINTS[kind],
        params=params or None, require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="List Sub-Industries", readOnlyHint=True))
async def list_sub_industries(ctx: Context, sectors: list[str]) -> Any:
    """List the sub-industries within one or more sectors.

    `sectors` must be values from `list_options("sectors")`. Returns the
    distinct industries used to narrow `list_realtime_events(industry=[...])`.
    """
    return await _send(
        ctx, "GET", "/get-sub-industries",
        params={"sector": sectors}, require_auth=False,
    )


# --- User PDF templates ----------------------------------------------------
# THE way a user gets reports in their own format. A saved template is a
# BLUEPRINT — the author's stylesheet and page chrome plus one markup pattern
# per content kind (title, lead, sections, bullets, KPI cards, tables, charts,
# figure, source line, footnote) — which the backend applies automatically to
# every PDF it renders for that user (`generate_report_for_stock`, scheduled
# reports). Nobody fills template markup: a template is structure and style;
# the document supplies the content. A template is a visual choice, so the
# flow is draft (rendered previews) -> the user looks and picks -> save the
# treatment they approved -> `update_user_template` for later changes.
#
# There is no agent-driven PDF builder here any more: the build_pdf_* tools
# over /create-pdf and /create-pdf-sidebar (and the pdf_options catalogue that
# described their tag DSL) were retired in favour of templates. When a user
# wants a document composed from Flexreport data that no backend report
# covers, the agent builds the PDF with its own document tooling and, if the
# user has a saved template (`get_user_template`), lays the content out in
# that format; if none is saved, it offers to draft one.

_TemplateType = Literal["add_on", "bespoke"]


@mcp.tool(annotations=ToolAnnotations(title="Draft PDF Template Previews", readOnlyHint=False, destructiveHint=False))
async def draft_user_template(
    ctx: Context,
    template: str,
    template_type: _TemplateType,
    anchor: Optional[str] = None,
) -> Any:
    """Render preview images of a user's PDF template so they can approve it BY LOOKING, before anything is saved.

    THE POINT: a user wants their research in THEIR format — their masthead, colours,
    layout, branding. A saved template is a BLUEPRINT: the author's stylesheet and
    page chrome plus one markup pattern per content kind, which the backend applies
    automatically to every PDF it renders for that user. A template is a visual
    choice, so it is signed off by looking at rendered pages, never by describing
    them. This is STEP 1 OF 2: it compiles the submitted HTML into a blueprint under
    several treatments and renders each one as PNG previews of the platform's
    SPECIMEN DOCUMENT — one of every content kind (title, lead, sections, bullets,
    KPI cards, tables, charts, figure, source line, footnote). The user signs off on
    how the design treats each kind of content, not on sample text. NOTHING IS
    STORED — the draft lives in a 24-hour cache; `save_user_template` is step 2.

    THE FLOW:
      1. Author the HTML with the user (or take the page they hand you).
      2. Call this. Show the user EVERY variant's `preview_urls` (render the images
         inline where the client can; otherwise give the links) with its `name` and
         `rationale`, side by side.
      3. The user picks one -> `save_user_template(draft_id, variant_id)`. Never save
         a treatment the user has not seen and approved. Later changes go through
         `update_user_template`.

    `template_type`:
      - "add_on"  — restyles ONE section of the standard report layout and keeps the
        rest. `anchor` is REQUIRED: the section it applies to, matched as a substring
        against the document's block keys (e.g. "technical", "financials",
        "ownership"). The previews are rendered at the 360pt column width the block
        will occupy inside a report — anything wider is clipped, not scaled — and
        the treatments restyle it to sit inside the host document (its own page
        background, masthead and disclaimer are stripped).
      - "bespoke" — the template drives the whole document: page chrome, stylesheet
        and the pattern for every content kind, previewed as full pages.

    HTML ONLY: a template is a page design, so the backend rejects markdown (422).

    TEMPLATE RULES (violations come back as a 422 with `detail.errors`):
      - Real CSS engine: grid, flexbox, gradients, absolute positioning all work.
        Add `break-inside: avoid` to a panel that must not split across pages.
      - Only `data:` URIs for images. External images, stylesheets and web fonts are
        BLOCKED; <script>, <link> and similar are stripped.
      - Max 200 KB; must parse as HTML.
      - Write the sample as a complete page in the intended style; its content is
        discarded at save — only the structure and styling are kept.

    Returns {"draft_id", "variants": [{"variant_id", "name", "rationale",
    "preview_urls": [...], "preview_pages"}], "expires_in_seconds", "next_step",
    "warnings"}. `preview_urls` are time-limited presigned PNG links, one per page.
    Read `warnings`: a treatment that "changed too much" was dropped, and an empty
    variant list (HTTP 502) means no usable adaptation survived — revise the
    template rather than retrying the same one.

    Runs a model pass plus a render, so expect several seconds. Requires auth (your
    MCP client attaches the OAuth bearer automatically). Rate-limited 30/min.
    """
    if not (template or "").strip():
        return {"error": "template is required — the HTML to preview."}
    if template_type == "add_on" and not (anchor or "").strip():
        return {"error": "anchor is required for an add_on template — the section it applies to (e.g. 'technical')."}
    return await _send(
        ctx, "POST", "/draft-user-template",
        json={
            "template": template,
            "template_type": template_type,
            "template_format": "html",
            "anchor": anchor,
        },
    )


@mcp.tool(annotations=ToolAnnotations(title="Save PDF Template", readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def save_user_template(
    ctx: Context,
    draft_id: Optional[str] = None,
    variant_id: Optional[str] = None,
    template: Optional[str] = None,
    template_type: Optional[_TemplateType] = None,
    anchor: Optional[str] = None,
) -> Any:
    """Save the user's PDF format — one template per account, applied automatically to every PDF the backend renders for them from now on.

    STEP 2 OF 2 after `draft_user_template`. Two ways to call it:

    SIGNED-OFF (the normal path): pass `draft_id` and the `variant_id` the user chose
    from the previews. The EXACT treatment they looked at is stored — no second
    adaptation pass that could produce something they never saw. `template`,
    `template_type` and `anchor` are taken from the draft and may be omitted. A 404
    means the draft expired (24h) or the variant does not exist: draft again.

    DIRECT (no previews): pass `template` + `template_type` (+ `anchor` for an
    add_on) — only when the user explicitly wants to skip the previews. An add_on
    saved this way still gets an unreviewed harmonise pass, so prefer the signed-off
    path for anything visual. HTML only: markdown is rejected with a 422.

    ONE TEMPLATE PER USER — this is an UPSERT: saving replaces whatever was saved
    before, with no history. Call `get_user_template` first and confirm with the user
    before overwriting an existing format. To change a saved format afterwards, use
    `update_user_template` (any field, or none to recompile it as stored).

    WHAT CHANGES AFTERWARDS: the backend renders every PDF it builds for this user —
    `generate_report_for_stock` (standard and custom) and scheduled reports — through
    the saved format. The template is a blueprint (stylesheet, page chrome, one
    pattern per content kind) applied to the document's own content: nothing is
    filled in by hand, and no other call changes. When you compose a PDF yourself
    from Flexreport data, `get_user_template` returns the markup to lay it out in.

    Returns {"status": "SAVED", "template_type", "anchor", "bytes", "patterns":
    [content kinds the design styles], "tones": {positive | negative | neutral |
    accent: css class}, "warnings"}. Tell the user which content kinds their design
    styles. A warning "keeping the previously compiled blueprint" means the new
    compile was rejected and the previous blueprint stays in force; the markup
    itself is still saved. Validation failures are a 422 with `detail.errors`
    listing each problem.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    if (draft_id is None) != (variant_id is None):
        return {"error": "draft_id and variant_id go together — pass both (from draft_user_template) or neither."}
    if draft_id is None:
        if not (template or "").strip():
            return {"error": "Nothing to save — pass draft_id + variant_id for a reviewed draft, or the template text itself."}
        if template_type is None:
            return {"error": "template_type is required ('add_on' or 'bespoke') when saving a template directly."}
        if template_type == "add_on" and not (anchor or "").strip():
            return {"error": "anchor is required for an add_on template — the section it applies to (e.g. 'technical')."}

    # The backend body model requires `template` and `template_type` even on the
    # sign-off path, where the draft supplies both and overrides whatever is sent —
    # so send neutral fillers there rather than making the agent resend the markup.
    payload: dict[str, Any] = {
        "template": template or "",
        "template_type": template_type or "add_on",
        "template_format": "html",
        "anchor": anchor,
    }
    if draft_id is not None:
        payload["draft_id"] = draft_id
        payload["variant_id"] = str(variant_id)
    return await _send(ctx, "POST", "/save-user-template", json=payload)


@mcp.tool(annotations=ToolAnnotations(title="Update Saved PDF Template", readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def update_user_template(
    ctx: Context,
    template: Optional[str] = None,
    template_type: Optional[_TemplateType] = None,
    anchor: Optional[str] = None,
    draft_id: Optional[str] = None,
    variant_id: Optional[str] = None,
) -> Any:
    """Change your saved PDF template — any field, or none to recompile it as stored.

    Complements `draft_user_template` (preview), `save_user_template` (create) and
    `delete_user_template` (remove). Every argument is optional; a field you omit
    keeps its stored value, and the result is compiled into a blueprint again:
      - a VISUAL change: draft the new page with `draft_user_template`, show the
        previews, then pass the chosen `draft_id` + `variant_id` here (both or
        neither);
      - a change without previews: pass `template`, `template_type` and/or
        `anchor` directly (HTML only — markdown is rejected with a 422);
      - NO arguments: recompiles the stored template with the current compiler —
        how a saved design picks up an improved compile without resubmitting it.

    Async: returns {"task_id", "status": "PENDING", "changed": [the fields that
    were updated], "next_step"} — poll `get_task_status` for {"status": "SAVED",
    "patterns", "tones", "warnings"}. A 404 means nothing is saved yet (use
    `save_user_template` first); a 422 lists validation errors in `detail.errors`.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    if (draft_id is None) != (variant_id is None):
        return {"error": "draft_id and variant_id go together — pass both (from draft_user_template) or neither."}
    body: dict[str, Any] = {}
    if template is not None:
        body["template"] = template
        body["template_format"] = "html"
    if template_type is not None:
        body["template_type"] = template_type
    if anchor is not None:
        body["anchor"] = anchor
    if draft_id is not None:
        body["draft_id"] = draft_id
        body["variant_id"] = str(variant_id)
    return await _send(ctx, "PUT", "/update-user-template", json=body)


@mcp.tool(annotations=ToolAnnotations(title="Get Saved PDF Template", readOnlyHint=True))
async def get_user_template(ctx: Context) -> Any:
    """Return the PDF template this user has saved, or a 404 if they have none.

    Call it to answer "what format am I on?", BEFORE `save_user_template` when a
    template may already exist (saving replaces it without history, so the user
    should know what they are overwriting), and whenever you are about to compose
    a PDF yourself from Flexreport data: lay the content out in this markup so the
    document matches the user's format. A 404 there is the cue to offer drafting
    one (`draft_user_template` -> previews -> `save_user_template`).

    Returns {"template_type", "template_format", "anchor", "template", "created_at",
    "updated_at"}. `template` is the sanitized markup as submitted (up to 200 KB) —
    the compiled blueprint the backend renders from is not returned. To change the
    format, draft again and pass the chosen variant to `update_user_template`
    rather than editing this text blind.

    An HTTP 404 means no template is saved — the standard layouts are in force. An
    HTTP 503 means the backend's user_templates table has not been created yet, NOT
    that nothing is saved; report that difference.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    return await _send(ctx, "GET", "/get-user-template")


@mcp.tool(annotations=ToolAnnotations(title="Delete Saved PDF Template", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_user_template(ctx: Context) -> Any:
    """Delete the user's saved PDF template so their PDFs go back to the standard layouts.

    Permanent and there is no history: re-creating the format means drafting and
    saving again. Confirm with the user before calling. Ownership is enforced
    server-side, so only the caller's own template can be removed.

    Returns {"msg": "Template deleted. Your PDFs will use the standard layouts."}.
    A 404 means there was nothing saved to delete.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    return await _send(ctx, "DELETE", "/delete-user-template")


@mcp.tool(annotations=ToolAnnotations(title="Get Company Snapshot", readOnlyHint=True))
async def get_company_snapshot(ctx: Context, symbol: str) -> Any:
    """Fetch a structured, POINT-IN-TIME company snapshot — no report generation needed.

    Returns thesis/bull/bear, financial overview (Piotroski, valuation signal),
    price performance + technical indicators, price targets, institutional
    ownership, and analyst grades for `symbol`. Synchronous and cheap — prefer
    this for a quick CURRENT read instead of generating a full PDF report.

    *** POINT-IN-TIME ONLY — THIS IS NOT A TIME SERIES. *** It captures where the
    company stands right now, not how it got there. Do NOT use it to answer
    temporal / trend / "over time" questions (e.g. "how has X evolved over the
    past year", "quarter-over-quarter trend", "activity over the last 12 months",
    "since ..."). Each block is an independently-sourced current cut with its own
    as-of date (e.g. the institutional-ownership block is the latest 13F holder
    snapshot), so the blocks are NOT a comparable historical series — reading a
    trend off them, or treating two blocks' as-of dates as the same instant,
    produces spurious results. For anything time-series, historical, or
    "how did this change", use `explore_data_catalogue` instead.

    Scope caveat: the snapshot is a FIXED set of current blocks, not the full
    catalogue. Absence of a data domain HERE does NOT mean Flexreport lacks it —
    do not infer coverage gaps from this tool. Route "which symbols / values are
    available" enumerations to `list_options` (instant), and "do you have data
    on <topic>" questions that need actual data to `explore_data_catalogue`.

    *** RUN IT WITH `get_company_event_web(symbol)` — THE STANDARD PAIR. *** This
    tool gives the WHAT (where the company stands); the event web gives the WHY
    (the events that got it there, in order, and how they connect). The snapshot
    states that the thesis shifted, the valuation signal changed, or ownership
    moved; it does NOT say what caused any of it — every block here is a settled
    output with the episode behind it stripped out. So whenever the user asks
    about a company generally, or follows a snapshot with "why?" / "what changed?"
    / "what drove that?", call BOTH and read them together: quote the snapshot for
    the current position, and the event web for the sequence that explains it.
    Presenting a snapshot alone as the whole picture is the failure mode — it
    reads as a verdict with no evidence.

    Two honest limits when pairing them. (1) The event web defaults to a 7-day
    window; a driver older than that needs `window_days` widened (up to 90) before
    you can say the snapshot is unexplained. (2) The blocks here carry their own
    as-of dates — a 13F ownership block can be a quarter old, so the event that
    explains it may sit well outside any window. Absence of a matching event is
    NOT proof the snapshot moved for no reason.
    """
    return await _send(
        ctx, "GET", "/get-company-snapshot",
        params={"symbol": symbol}, require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Company Event Web", readOnlyHint=True))
async def get_company_event_web(
    ctx: Context,
    symbol: str,
    window_days: Optional[int] = None,
    max_nodes: Optional[int] = None,
) -> Any:
    """Fetch the connected WEB of what recently HAPPENED to a company — the WHY behind its snapshot, and a time-ordered graph to CHAIN your next calls off.

    ===> `situate(symbols=[symbol])` already returns this web (newest 12 nodes) PLUS
    the ontology cards, cache state and an ordered plan — start there. Call this
    tool directly for the FULL graph or a wider `window_days`, and pair every node
    you chain off with get_event_ontology(event_type=node.type): the web says WHICH
    event, the ontology says what it entails and what to do about it.

    ===> A FIRST-LINE TOOL FOR SINGLE-COMPANY QUESTIONS, not a specialist one. Reach
    for it whenever the user asks what has been going on with a name ("catch me up on
    WM", "what happened at NVDA this week", "anything I should know before
    earnings?"), asks WHY the company looks the way it does ("why is the thesis
    negative?", "what drove the downgrade?", "what changed?"), or whenever you are
    about to aim a targeted `explore_data_catalogue`, `generate_report_for_stock`, or
    `list_realtime_events` request at ONE symbol. Synchronous, cheap, and public —
    one call turns "I'll guess which event types and dates matter" into "here are
    the exact events, their timestamps, the tables that were written, and the call
    that returns each full payload".

    *** THE STANDARD PAIR: `get_company_snapshot` + THIS. *** They answer the two
    halves of almost every company question and are usually called TOGETHER:
      - `get_company_snapshot(symbol)` -> the WHAT. Where the company stands now:
        thesis, fundamentals, technicals, ownership, grades. A settled position with
        no account of how it got there.
      - `get_company_event_web(symbol)` -> the WHY. The episode behind that position:
        the earnings print, the 8-K, the transcript update, the rating action, the
        13F refresh — in order, with edges saying how each relates.
    Read them together: the snapshot for the current position, the web for the
    sequence that explains it. A snapshot delivered alone reads as a verdict with no
    evidence; this tool is the evidence. Conversely, do NOT use this tool to state
    the company's current position — it carries headlines and pointers, not values.
    When the two disagree, say so plainly rather than smoothing it over: the blocks
    in a snapshot carry independent as-of dates, so an ownership or grade block can
    predate anything in the window (widen `window_days` before concluding a snapshot
    move is unexplained).

    Returns:
      {"symbol": ..., "as_of": ISO8601, "window_days": N,
       "nodes": [...], "edges": [...], "market_context": [...]}

    NODES — two kinds, newest first, each with a short id ("n1", "n2", ...):
      - kind="event"       — a published realtime event. Carries `type` (the event
                             type, e.g. "8k_release"), `family`, and a `fetch` hint
                             — the exact call that returns its FULL payload, e.g.
                             {"tool": "list_realtime_events", "event_type": "8k_release",
                              "related_endpoints": [...]}.
      - kind="data_update" — a write to a backing table for activity that publishes
                             NO event at all (insider filings, 13F refreshes, IPO
                             calendar entries, Companies House events, transcript and
                             fundamentals analysis). Carries `relation` (the table
                             written); most also carry a `fetch` hint (a ready-made
                             `explore_data_catalogue` query for the underlying rows),
                             and on some relations the hint additionally carries
                             `signed_query_url` — see step 2 below.
    Every node carries `at` (when the write/publish happened) and `headline` (ONE
    line, truncated at 200 chars).

    EDGES — {"from": "n3", "to": "n1", "via": <relation>, "cause": ...}, pointing
    from the EARLIER node to the LATER one. `cause` is a confidence grade — respect it:
      - "same_chain_run" — OBSERVED. One backend chain run produced both nodes.
      - "lineage"        — DECLARED. One wrote the table the other's event derives from.
      - "co_occurrence"  — TEMPORAL ONLY. Same company, same window, no known causal
                           path. Report these as "happened alongside", NEVER as caused-by.

    HOW TO CHAIN (the point of this tool):
      1. Walk the edges back from the newest node to reconstruct the episode, then
         follow each event node's `fetch` to get the detail — e.g.
         `list_realtime_events(event_type="8k_release", tickers=["WM"])`. Always narrow
         by `tickers=[symbol]`; an unfiltered call re-pulls the entire cache.
      2. When a `data_update` node's `fetch` carries `signed_query_url`, call
         `get_signed_sql_drilldown` with it FIRST — zero planning, and it returns the
         new record (`is_new_record: true`) plus the context rows to read it against.
         That follow-up needs the user SIGNED IN (this tool does not); if it comes back
         not-authenticated, the link is fine — say sign-in is needed for the rows and
         fall back to an `explore_data_catalogue` query on the node's `relation`.
         Otherwise use the node's `fetch` query if it has one, or its `relation` and
         `at` to write a PRECISE `explore_data_catalogue` query ("insider filings for
         WM since 2026-08-09", "13F position changes for WM in the last week")
         instead of a vague one.
      3. Feed the concrete events and dates into `generate_report_for_stock` /
         `generate_research_report` so the report is scoped to what actually happened.
      4. Nothing here is a full payload — a headline is a pointer, never the content.
         Do not quote a headline as if it were the event.

    `window_days` (default 7, max 90) and `max_nodes` (default 40, max 200) widen the
    look-back. Beyond the node cap, edges are capped per node (~3 same_chain_run, ~3
    co_occurrence) and `market_context` — market-wide macro/economic-calendar nodes
    that carry no ticker and so attach to no symbol — is capped at 5. A MISSING edge
    is therefore not evidence that two things are unrelated.

    Traps:
      - `at` is a WRITE/PUBLISH time, not a business date. An earnings call date or
        fiscal period end can be well before the node's timestamp — never report `at`
        as the date the underlying event occurred.
      - A "<table> refreshed for N symbols" node is universe-wide scheduled
        maintenance rolled up, not company-specific signal.
      - An empty web (`"degraded": true`, empty lists) means the graph has no rows for
        that symbol in the window — NOT that nothing happened and NOT that the symbol
        is uncovered. Say so, and fall back to `list_realtime_events` /
        `explore_data_catalogue` rather than concluding it was a quiet week.

    PUBLIC at the backend — no account, plan, or entitlement is needed, the same
    posture as `get_company_snapshot`. (The MCP connection itself still carries the
    client's OAuth session; that gate is transport-wide, not this tool's.)
    Rate-limited 60/min.

    Every node's `type`, `relation` and `family` are join keys into get_event_ontology:
    call get_event_ontology(event_type=node.type) to learn what that node ENTAILS (the
    tables now fresh, the events that should follow and how soon, where the payload
    lives after the 12h cache, whether downstream tables have refreshed yet) before
    chaining further calls off it. Web = what happened to THIS company; ontology = what
    that KIND of thing means. Read them together.
    """
    params: dict[str, Any] = {"symbol": symbol}
    if window_days:
        params["window_days"] = window_days
    if max_nodes:
        params["max_nodes"] = max_nodes
    return await _send(
        ctx, "GET", "/get-company-event-web",
        params=params, require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Event Ontology", readOnlyHint=True))
async def get_event_ontology(
    ctx: Context,
    event_type: Optional[str] = None,
    family: Optional[str] = None,
    relation: Optional[str] = None,
    format: Literal["cards", "triples"] = "cards",
) -> Any:
    """The platform's event ONTOLOGY — what an event TYPE means for everything else. Call it FIRST on any real-time, single-company, or event-driven request; it is the map the other calls are planned from.

    ===> `situate(...)` composes this with the event web and the cache state and
    hands you a plan — start there. Call this tool directly for the FULL card of an
    event type, a relation card, a whole family, or the unfiltered graph, when the
    plan did not cover what you need next.

    ===> REQUISITE for: any real-time / live-events request, any "what
    happened / why / catch me up" question, and BEFORE aiming explore_data_catalogue or
    generate_report_for_stock at a company whose situation is event-driven. Cheap,
    synchronous, public, symbol-INDEPENDENT and cacheable — fetch once per event type
    (or once unfiltered, ~230 KB) and reuse for the whole conversation.

    Pass exactly ONE filter for a small card (~5-10 KB):
      event_type="eps_update"        one event type and the relations it entails
      relation="eod_stock_prices"    one table/view and the event types that entail it
      family="earnings"              every event type and relation in a family
    No filter -> the whole class graph (21 event types, ~300 relations, 20 tools).
    Web nodes from get_company_event_web carry the join keys verbatim: pass a node's
    `type` as event_type, its `relation` as relation, its `family` as family.

    An event_type card carries:
      about                    what the event is ABOUT: symbol | filer | strategy. A 13F
                               event is a FILER's action across N names — the symbol
                               you asked about is one of them.
      persisted_in             the durable table to read once the event has aged out
                               of the realtime cache (realtime_cache_ttl_hours, 12h) —
                               list_realtime_events returns [] after that; the data is
                               still there, in this relation.
      entails.<layer>          the relations WRITTEN by the same chain run (mined, with
                               run counts) plus every view above them (pg_depend),
                               bucketed base / silver / gold / dimension / analysis.
                               These are the tables that are FRESH because this event
                               fired — name them explicitly in explore queries.
      entails.realtime_events  event types that usually FOLLOW, each with rate,
                               median_lag_hours, lift, mode (trigger = dispatched by the
                               same chain, near-certain; sequence = world-ordered,
                               independently detected, probabilistic), certainty /
                               expected_within_hours when declared, and persisted_in.
      preceded_by              the inverse — what should ALREADY exist earlier in the web.
      episodes                 named world-order sequences (earnings, realtime_mover,
                               thirteen_f_cycle, news) with this event's position — the
                               whole shape of the situation from any one member.
      produced_by[].schedule   the workflow that publishes it, its cron, and
                               next_run_at (America/New_York).
      deepened_by              the tools that deepen this event once you have its payload.
      informs                  DOMAIN causality (news -> prices, earnings -> estimates):
                               what an analyst checks next. Declared and the WEAKEST
                               edge — never present it as lineage.
      evidence                 how many of these events the platform has seen.

    A relation card carries: layer, family, cadence (update_frequency + prose),
    derives_from / feeds (pg_depend), computed_from / computes (LLM and model writers),
    refreshed_by[].schedule with next_run_at, read_by (the MCP tools that read it),
    entailed_by (event types), informs.

    THE FRESHNESS CHECK — the reason this tool exists: compare a web node's `at` with a
    downstream relation's refreshed_by[].schedule[].next_run_at. If next_run_at is AFTER
    the event, that relation does NOT reflect the event yet. Do not query it for the
    reaction — use the intraday tools (detect_intraday_outlier_jumps,
    get_aftermarket_quotes / get_aftermarket_trades) or say the daily table lags.

    Every edge carries `provenance` (mined | declared | pg_depend | static:tasks.py |
    beat | catalogue | data_model), the same honesty axis as the web's `cause`: mined
    and pg_depend are facts, declared is the platform's own judgment, informs is domain
    intuition. Quote the grade when you lean on an edge. format="triples" returns raw
    subject-predicate-object rows instead of cards. degraded=true means the table has
    not been built yet, not that the type is unknown.
    """
    params: dict[str, Any] = {"format": format}
    if event_type:
        params["event_type"] = event_type
    if family:
        params["family"] = family
    if relation:
        params["relation"] = relation
    return await _send(ctx, "GET", "/get-event-ontology", params=params, require_auth=False)


def _query_token(reference: str) -> str:
    """Normalise a server-minted query reference to its bare `t` token.

    Accepts either the bare token or a full signed URL (`signed_query_url`,
    `source_url`). Fernet tokens never contain "?", so a query string reliably
    marks the URL form.
    """
    token = reference.strip()
    if "?" in token:
        token = parse_qs(urlsplit(token).query).get("t", [token])[0]
    return token


@mcp.tool(annotations=ToolAnnotations(title="Run Signed Drilldown Query", readOnlyHint=True))
async def get_signed_sql_drilldown(ctx: Context, encrypted_query_token: str) -> Any:
    """Fetch the rows behind an event-web drilldown from its server-minted query token.

    Some `get_company_event_web` nodes carry `signed_query_url` in their `fetch` hint —
    an opaque, tamper-proof link the backend minted for exactly that node. This tool is
    the zero-planning rung of the chain: no query to compose, the rows come back
    directly. Pass either the full `signed_query_url` or just its `t` parameter; the
    URL form is unwrapped automatically. Tokens are minted server-side only — NEVER
    construct, guess, or modify one, and never pass SQL here. A token that came from a
    SAVED request rather than the event web belongs to `run_saved_query` instead — same
    endpoint, but it replays every query in the saved row in one call.

    Returns {"format": "table", "count": N, "rows": [...]} — rows only, never the
    underlying SQL. For contextual drilldowns the NEW record is flagged
    `is_new_record: true` and the remaining rows are the prior/context records to read
    it against (the node's `signed_query_context` describes what they are).

    HTTP 403 means the token is invalid or tampered — do not retry with an altered
    token; re-fetch the event web for a fresh link instead.

    Requires auth (your MCP client attaches the OAuth bearer automatically) — unlike
    `get_company_event_web`, which is public: the web hands out the LINK to anyone, but
    following it to the underlying rows is for signed-in users. A signed-out caller gets
    a not-authenticated error, not rows; the fix is signing in, never a different token.
    """
    return await _send(
        ctx, "GET", "/query-data",
        params={"t": _query_token(encrypted_query_token)},
    )


@mcp.tool(annotations=ToolAnnotations(title="Save Data Request For Replay", readOnlyHint=False, destructiveHint=False))
async def save_user_query(
    ctx: Context,
    user_request: str,
    query_tokens: list[str],
) -> Any:
    """Save the queries an exploration already planned, so the same request can be replayed instantly.

    THE POINT: `explore_data_catalogue` is a full validate -> plan -> run pipeline that
    re-authors the SQL from scratch on every call (~1-5 min, 20/hour). For a question
    the user will ask AGAIN — a recurring check, a dashboard they watch, "my usual
    semis screen" — save the tokens that exploration just produced. `run_saved_query`
    then re-runs exactly those queries against live data in seconds, with no planning
    step and no async job to poll.

    WHEN TO CALL: after an exploration the user says (or clearly implies) they will
    want again. Do NOT save every exploration by reflex, and do not save a one-off.

    `user_request` — the natural-language request being saved, in the user's own words.
    It is the ONLY human-readable label on the saved row, so make it specific enough to
    recognise later ("weekly EPS-revision check on my semis basket", not "the query").

    `query_tokens` — the `query_token` values from the exploration result's `results`
    entries, one per named query, passed through EXACTLY as returned. Each is opaque
    ciphertext the server minted around the planned SQL; a full `signed_query_url` /
    `source_url` is accepted too and unwrapped to its token. NEVER write, guess, or
    edit a token, and never pass SQL here — a token this backend did not mint cannot be
    decrypted and fails with 403 at replay time, long after the exploration is gone. A
    result entry carrying no `query_token` has nothing to save; skip it.

    Owner-scoped: the row belongs to the calling user and is invisible to everyone
    else. There is NO upsert (unlike `schedule_task`): saving the same request twice
    creates a SECOND row. Check `list_saved_queries` first and `delete_saved_query` the
    stale one rather than piling up near-duplicates.

    Returns {"msg": "Successfully saved query: <user_request>"}. The row `id` is not in
    the response — `list_saved_queries` has it, and it is what `delete_saved_query`
    takes.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    tokens = [_query_token(t) for t in query_tokens if t and t.strip()]
    if not tokens:
        return {"error": "query_tokens is required — pass the `query_token` values from the exploration result."}
    return await _send(
        ctx, "POST", "/save-user-query",
        json={"user_request": user_request, "queries": tokens},
    )


@mcp.tool(annotations=ToolAnnotations(title="List Saved Data Requests", readOnlyHint=True))
async def list_saved_queries(
    ctx: Context,
    limit: int = 50,
) -> Any:
    """List the data requests you have saved with `save_user_query`, newest first.

    The entry point to the replay flow: check HERE before re-running
    `explore_data_catalogue` for a question the user has asked before — a matching
    saved row means the answer is seconds away via `run_saved_query` instead of a
    ~1-5 minute planning job.

    Returns {"count": N, "saved_queries": [{"id", "user_request", "queries",
    "created_at", "updated_at"}, ...]}, the calling user's rows only:
      - `user_request` is the natural-language label to match against what the user
        just asked. Match on MEANING, not string equality — but a row that is merely
        close is a DIFFERENT question; confirm with the user before replaying it.
      - `queries` is the list of opaque query tokens — hand it straight to
        `run_saved_query`. Never show a token to the user: it is ciphertext, not
        content, and says nothing about what the query does.
      - `id` is what `delete_saved_query` takes.

    `limit` is 1-500 (default 50). Duplicate `user_request` values are possible —
    saving does not upsert.

    An HTTP 503 here means the backend's saved-queries table has not been created yet,
    NOT that the user has nothing saved — report that difference rather than an empty
    list.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    return await _send(ctx, "GET", "/get-saved-queries", params={"limit": limit})


@mcp.tool(annotations=ToolAnnotations(title="Run Saved Data Request", readOnlyHint=True))
async def run_saved_query(
    ctx: Context,
    query_tokens: list[str],
) -> Any:
    """Re-run a saved request's queries immediately against live data — no planning, no polling.

    The payoff of `save_user_query`: pass a saved row's `queries` array from
    `list_saved_queries` and each stored query runs as-is, returning fresh rows
    synchronously. Use this INSTEAD of `explore_data_catalogue` whenever a saved row
    already covers the question — same SQL, current data, seconds instead of minutes,
    and it does not draw on the 20/hour exploration budget.

    `query_tokens` — the saved row's `queries` list, verbatim (a full signed URL is
    accepted and unwrapped). Max 25 per call. Tokens are server-minted only: never
    construct or edit one, and never pass SQL. For a token that came from a
    `get_company_event_web` node rather than a saved row, use
    `get_signed_sql_drilldown` — same endpoint, single node, drilldown framing.

    Returns {"queries_run": N, "results": [{"index": i, "format": "table",
    "count": <row count>, "rows": [...]}, ...]}, in the SAME ORDER as `query_tokens`.
    Rows only — the underlying SQL is never returned, by design. Describe the results
    from the saved `user_request` and the columns, never by asserting what the query
    does internally.

    Each token runs independently: one failure does not stop the rest, and that entry
    carries an "error" key instead of rows. A 403 means that token is invalid or
    tampered — do not retry it altered; the saved row is dead, so re-run
    `explore_data_catalogue`, `save_user_query` a fresh row, and `delete_saved_query`
    the old one.

    A replay returning DIFFERENT rows than when it was saved is normal — it hits live
    data. Report what comes back now; never reconcile it against remembered numbers.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    tokens = [_query_token(t) for t in query_tokens if t and t.strip()]
    if not tokens:
        return {"error": "query_tokens is required — pass the `queries` array from list_saved_queries."}
    if len(tokens) > 25:
        return {"error": f"Too many query tokens ({len(tokens)}); a saved request holds a handful. Max 25 per call."}

    # Sequential, not fanned out: a saved request holds a few queries, each one is a
    # metered backend call, and keeping the order means `results[i]` always lines up
    # with `query_tokens[i]`. A failed token yields its error entry and the rest run.
    results: list[dict[str, Any]] = []
    for i, token in enumerate(tokens):
        payload = await _send(ctx, "GET", "/query-data", params={"t": token})
        results.append({"index": i, **(payload if isinstance(payload, dict) else {"result": payload})})
    return {"queries_run": len(results), "results": results}


@mcp.tool(annotations=ToolAnnotations(title="Delete Saved Data Request", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_saved_query(
    ctx: Context,
    query_id: int,
) -> Any:
    """Delete one of your saved data requests by id.

    `query_id` is the `id` from `list_saved_queries` — NOT a position in that list and
    not the `user_request`. Deletion is permanent and touches no data, but the query
    tokens go with the row and cannot be reconstructed: re-creating it means running
    `explore_data_catalogue` again and re-saving. When more than one row looks like a
    match, confirm WHICH one with the user before calling.

    Ownership is enforced server-side. A 404 ("No saved query with id N") means the id
    does not exist OR is not yours — the backend deliberately does not distinguish the
    two, so never tell the user the row belongs to someone else.

    Returns {"msg": "Deleted saved query <id>"}.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    return await _send(
        ctx, "DELETE", "/delete-saved-query",
        params={"query_id": query_id},
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Technical Indicator Data", readOnlyHint=True))
async def get_technical_indicator_data(
    ctx: Context,
    symbol: str,
    indicator: str,
    start_date: str,
    end_date: str,
) -> Any:
    """Fetch a historical technical-indicator series for a symbol over a date range.

    `indicator` must be one of the names from `list_options("technical_indicators")`
    (call it first). `start_date` and `end_date` are inclusive and must be "YYYY-MM-DD".
    `symbol` is validated against the covered universe (`list_options("tickers")`); an unknown
    symbol returns 404 and an unknown indicator returns 400 listing valid values.

    Returns a list of daily records (each row's columns vary by indicator), ordered by
    date, or an empty list when there's no data in the range.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    return await _send(
        ctx, "GET", "/technical-indicator-endpoint",
        params={
            "symbol": symbol,
            "indicator": indicator,
            "start_date": start_date,
            "end_date": end_date,
        },
    )


@mcp.tool(annotations=ToolAnnotations(title="Detect Intraday Outlier Jumps", readOnlyHint=True))
async def detect_intraday_outlier_jumps(
    ctx: Context,
    symbol: str,
    frequency: Literal[
        "one_minute", "five_minute", "thirty_minute", "one_hour", "four_hour"
    ] = "one_minute",
    zscore_threshold: float = 2.0,
) -> Any:
    """Live look at TODAY's intraday tape, flagging outlier price jumps.

    Pulls today's intraday bars for `symbol` (US/Eastern trading day) live at the given
    `frequency` and flags each bar whose move is a statistical outlier versus the stock's
    own daily-return volatility — i.e. bars where |z-score| of the move exceeds
    `zscore_threshold`. Use it for a quick "is the stock making an abnormal intraday move
    right now?" read. `list_options(kind="intraday_frequency")` lists the supported
    frequencies straight from the backend.

    `zscore_threshold` (default 2.0) is the daily-sigma cutoff: higher = stricter (fewer,
    more extreme flags), lower = more sensitive. Synchronous — returns the flagged bars
    directly (no task id to poll). Returns a 404-style error if no intraday data is
    available yet (e.g. before the market opens or for an uncovered symbol).

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    return await _send(
        ctx, "GET", "/detect-intraday-outlier-jumps",
        params={"symbol": symbol, "frequency": frequency, "zscore_threshold": zscore_threshold},
    )


async def _query_aftermarket(
    ctx: Context,
    path: str,
    symbols: list[str],
    start_datetime: Optional[str],
    end_datetime: Optional[str],
) -> Any:
    """Build the AftermarketQuery body and forward it to a stored-data endpoint.

    `start_datetime`/`end_datetime` are only included when supplied so the backend
    applies its defaults (start/end of today in ET) for whichever bound is omitted.
    """
    body: dict[str, Any] = {"symbols": symbols}
    if start_datetime:
        body["start_datetime"] = start_datetime
    if end_datetime:
        body["end_datetime"] = end_datetime
    return await _send(ctx, "POST", path, json=body)


@mcp.tool(annotations=ToolAnnotations(title="Get Aftermarket Trades", readOnlyHint=True))
async def get_aftermarket_trades(
    ctx: Context,
    symbols: list[str],
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
) -> Any:
    """Query STORED aftermarket (extended-hours) TRADE data for symbols over a datetime range.

    Returns the trade ticks the backend has ingested for `symbols`, filtered on
    `ingested_at` between `start_datetime` and `end_datetime` (inclusive). Use it to
    pull the recorded extended-hours tape — i.e. read back already-captured aftermarket
    prints, NOT a live feed. For a live intraday read of the regular session use
    `detect_intraday_outlier_jumps` instead.

    `start_datetime` and `end_datetime` are ET wall-clock ISO-8601 timestamps
    (e.g. "2026-06-24T16:00:00"). Both are OPTIONAL: omit them and the backend
    defaults to the start (00:00:00) and end (23:59:59) of today in ET, so leave
    them off for "today's aftermarket trades".

    Requires auth (your MCP client attaches the OAuth bearer automatically). Rate-limited
    to 300/minute per user server-side.
    """
    return await _query_aftermarket(
        ctx, "/get-aftermarket-trades", symbols, start_datetime, end_datetime
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Aftermarket Quotes", readOnlyHint=True))
async def get_aftermarket_quotes(
    ctx: Context,
    symbols: list[str],
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
) -> Any:
    """Query STORED aftermarket (extended-hours) QUOTE data for symbols over a datetime range.

    Returns the bid/ask quote ticks the backend has ingested for `symbols`, filtered on
    `ingested_at` between `start_datetime` and `end_datetime` (inclusive). Use it to
    pull the recorded extended-hours quotes — i.e. read back already-captured aftermarket
    bid/ask data, NOT a live feed. The trade-print counterpart is `get_aftermarket_trades`.

    `start_datetime` and `end_datetime` are ET wall-clock ISO-8601 timestamps
    (e.g. "2026-06-24T16:00:00"). Both are OPTIONAL: omit them and the backend
    defaults to the start (00:00:00) and end (23:59:59) of today in ET, so leave
    them off for "today's aftermarket quotes".

    Requires auth (your MCP client attaches the OAuth bearer automatically). Rate-limited
    to 300/minute per user server-side.
    """
    return await _query_aftermarket(
        ctx, "/get-aftermarket-quotes", symbols, start_datetime, end_datetime
    )


@mcp.tool(annotations=ToolAnnotations(title="Onboard New Symbol", readOnlyHint=False, destructiveHint=False))
async def onboard_symbol(
    ctx: Context,
    symbol: str,
) -> Any:
    """Request onboarding of a NOT-yet-covered ticker (mutating, authenticated).

    Kicks off a 30-60 min backend workflow and emails the authenticated user when
    the first report is ready. Rate-limited to 5/hour per user. Use only when a
    symbol is missing from `list_options("tickers")` / returns no data elsewhere.


    Returns {"task_id": ..., "status": "PENDING"}.
    """
    return await _send(
        ctx, "POST", "/onboard-symbol",
        params={"symbol": symbol},
    )

@mcp.tool(annotations=ToolAnnotations(title="Screen Stocks", readOnlyHint=False, destructiveHint=False))
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
) -> Any:
    """Screen stocks by financial growth, sector, sub-industry, market cap, analyst ratings, institutional ownership, country, and price performance.

    `market_cap` accepts buckets like "Small-cap", "Medium-cap", "Large-cap".
    Discover valid values with the list tools: `list_options` (e.g.
    kind="sectors", "institutional_investor_types") and `list_sub_industries`.

    Rate-limited to 10/hour
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
    )

@mcp.tool(annotations=ToolAnnotations(title="Optimize Portfolio (Fast)", readOnlyHint=True))
async def optimize_portfolio_default(
    ctx: Context,
    symbols: list[str],
    risk_tolerance: Optional[Literal["conservative", "balanced", "aggressive"]] = None,
) -> Any:
    """Build a risk-optimized portfolio from a list of tickers — fast, synchronous, no LLM.

    THE DEFAULT optimizer route. Returns the result directly and almost instantly
    (no task id, no polling). Reach for `optimize_portfolio` instead only when you
    explicitly want the slower LLM-curated variant layered on top of the optimizers.

    `risk_tolerance` (conservative | balanced | aggressive) selects which risk
    profile is marked as recommended. Symbols are validated against the covered
    universe (`list_options("tickers")`); unsupported tickers come back in `missing`, and
    in-universe tickers with too little price history come back in `dropped`.

    No auth required (public endpoint).

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
        require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="Optimize Portfolio (LLM-Curated)", readOnlyHint=False, destructiveHint=False))
async def optimize_portfolio(
    ctx: Context,
    symbols: list[str],
    risk_tolerance: Optional[Literal["conservative", "balanced", "aggressive"]] = None,
    delivery: Optional[str] = "dashboard",
) -> Any:
    """Build a risk-optimized portfolio from a non-empty list of tickers.

    Scores `symbols` with the multi-signal scorer, LLM-curates, and risk-optimizes.
    `risk_tolerance` (conservative, balanced, or aggressive) selects which risk
    profile is recommended. `delivery` is the result channel — "dashboard"
    (returned directly) or "email".

    Rate-limited to 10/hour
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
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Stock Picks", readOnlyHint=True))
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

    Each holding also carries a CURRENT-BASKET YTD view: `ytd_return` (that stock's
    Jan 1 -> now price return) and the strategy-level weighted `basket_ytd_return`.
    For how the strategies have ACTUALLY performed since inception (the realized track
    record of the book as held), use `get_strategy_performance_summary` /
    `get_strategy_track_record` instead.
    """
    params = {"strategy_name": strategy_name} if strategy_name else None
    return await _send(
        ctx, "GET", "/get-stock-picks", params=params, require_auth=False
    )


# The four strategy books plus the pooled optimized book — the valid `strategy_name`
# values for the track-record / swap-ledger tools (backend rejects anything else).
StrategyBook = Literal[
    "momentum", "multi_signal", "fundamentals_smid", "value_quality", "pooled"
]


@mcp.tool(annotations=ToolAnnotations(title="Get Strategy Performance Summary", readOnlyHint=True))
async def get_strategy_performance_summary(
    ctx: Context,
    amount: float = 1.0,
) -> Any:
    """Leaderboard of since-inception performance vs the S&P 500 for all strategy books.

    THE first stop for "how are the stock picks / strategies performing?". Returns one
    headline row per book — the four strategies plus the pooled optimized book — sorted
    by excess return over the S&P 500: {strategy_name, display_name, inception_date,
    since_inception_return, sp_since_inception, excess_since_inception,
    annualized_return, max_drawdown, sharpe, sp_sharpe, ytd_return, growth_strategy,
    growth_sp, days_tracked}.

    `amount` scales the growth-of-$ fields (growth_strategy / growth_sp = what `amount`
    invested at inception would be worth in the strategy vs the S&P 500).

    Drill into one book's full daily series with `get_strategy_track_record`, or its
    per-trade adds/drops with `get_strategy_swaps`.
    """
    return await _send(
        ctx, "GET", "/get-strategy-performance-summary",
        params={"amount": amount},
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Strategy Track Record", readOnlyHint=True))
async def get_strategy_track_record(
    ctx: Context,
    strategy_name: StrategyBook,
    book: Literal["llm", "mechanical"] = "llm",
    amount: float = 1.0,
) -> Any:
    """Since-inception daily track record vs the S&P 500 for ONE strategy book.

    Returns the realized performance of the book as actually held — chartable series
    plus headline stats: {strategy_name, book, inception_date, amount_invested,
    summary: {since_inception_return, sp_since_inception, excess_since_inception,
    annualized_return, max_drawdown, sharpe, sp_sharpe, ytd_return, growth_strategy,
    growth_sp, days_tracked, last_date},
    series: [{date, strategy_cumulative, benchmark_cumulative, excess, strategy_ytd,
    growth_strategy, growth_sp, sharpe_expanding, holdings_count}, ...]}.

    `strategy_name` is one of the four strategies or "pooled" (the pooled optimized
    book). `book` selects the variant: "llm" (LLM-curated selections, the default) or
    "mechanical" (the raw quantitative screen, no LLM overlay). `amount` scales the
    growth-of-$ fields. The Sharpe series is expanding-window and annualized (rf=0).

    Backend returns 404 when the chosen book has no performance history yet. For the
    cross-book leaderboard use `get_strategy_performance_summary`; for the current
    holdings themselves use `get_stock_picks`.
    """
    return await _send(
        ctx, "GET", "/get-strategy-track-record",
        params={"strategy_name": strategy_name, "book": book, "amount": amount},
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Strategy Swap Ledger", readOnlyHint=True))
async def get_strategy_swaps(
    ctx: Context,
    strategy_name: StrategyBook,
) -> Any:
    """Per-trade swap ledger (adds/drops) vs the S&P 500 for ONE strategy book.

    Answers "were the individual trades good?": across consecutive weekly rebalances,
    an ADD is a name newly selected and a DROP a name removed. Each ADD is measured
    from entry to its exit (or the latest close if still held); each DROP is the
    FOREGONE return from its exit to the latest close. Both are compared to the S&P
    500 over the same window.

    Returns {strategy_name,
    trades: [{symbol, action, event_date, window_end, still_open, name_return,
    sp_return, excess}, ...] (newest first),
    summary: {n_adds, n_drops, avg_add_excess_vs_sp, avg_drop_foregone_excess_vs_sp,
    net_swap_value_add}}.

    `strategy_name` is one of the four strategies or "pooled". For the book-level
    return series use `get_strategy_track_record`.
    """
    return await _send(
        ctx, "GET", "/get-strategy-swaps",
        params={"strategy_name": strategy_name},
    )

@mcp.tool(annotations=ToolAnnotations(title="Predict Post-Earnings Move", readOnlyHint=True))
async def predict_earnings_move(
    ctx: Context,
    symbols: list[str],
)-> Any:
    """
    Predict the magnitude of a stock's move, post-earnings announcement. Returns a list of possibilities, modelling the magnitude under each scenario e.g. if stock beats and raises guidance then expect a 6% magnitude move.
    """
    return await _send(
        ctx, "POST", "/predict-earnings-announcement-move", json={"symbols" : symbols }
    )

@mcp.tool(annotations=ToolAnnotations(title="Schedule Recurring Workflow", readOnlyHint=False, destructiveHint=False))
async def schedule_task(
    ctx: Context,
    name: str,
    steps: list[dict],
    regular_cron: str,
    delivery: Literal["email", "dashboard"] = "email",
    description: Optional[str] = None,
) -> Any:
    """Save a multi-step WORKFLOW (upsert by `name`) and attach a recurring cron
    schedule for its delivery.

    Use this when the user wants a sequence of platform calls delivered ON A
    SCHEDULE / repeatedly (e.g. "every weekday morning pull 8-K releases, screen
    for cheap large-caps, and email me the overlaps as a table"). For a ONE-OFF
    request, call the corresponding tools directly instead.

    `name` is a lowercase slug (pattern ^[a-z0-9][a-z0-9_-]{0,63} — no spaces,
    colons, or uppercase) and the upsert key: re-saving the same name updates
    the definition and re-attaches the schedule from `regular_cron`.

    `steps` is an ordered list of 1-8 dicts of two kinds:

    - fetch     -> {"kind": "fetch", "endpoint": "...", "params": {...},
                    "label": "..."}: call an allowlisted backend endpoint
                    (kebab-case path, no leading slash).
    - transform -> {"kind": "transform", "instruction": "...", "label": "..."}:
                    apply a natural-language instruction (<=2000 chars) to the
                    accumulated results of ALL prior steps (e.g. "intersect the
                    two event sets by symbol and build a table").

    The FIRST step must be a fetch. `label` is optional (unique lowercase slug;
    defaults to step_{i}) and keys that step's result. The LAST step's output is
    what gets delivered, so end table/summary workflows with a transform.

    Allowlisted fetch endpoints (anything else is rejected with a 422 at save
    time; `params` takes the same keys as the matching tool):

    - POST, JSON-body params: "get-realtime-events" (`list_realtime_events`),
      "list-upcoming-earnings-announcements" (`list_earnings_announcements`),
      "predict-earnings-announcement-move" ({"symbols": [...]}),
      "data-catalogue-exploration" ({"query": "..."}).
    - GET, scalar query params only (no lists/dicts): "get-company-snapshot"
      (requires {"symbol": "..."}), "get-strategy-performance-summary",
      "get-strategy-track-record", "get-strategy-swaps", "get-stock-picks",
      "list-tickers", "list-realtime-event-options", "get-sectors",
      "get-sub-industries".
    - EXPENSIVE — max 2 per workflow, and the cron must use a literal minute and
      at most 4 literal hours (no sub-hourly / "*" fields): "screen-stocks"
      (`screen_stocks`), "generate-research-report" ({"query": "..."}),
      "create-full-report" ({"ticker": "..."} for the symbol's standard report;
      add "user_override": true plus the shaping lists `generate_report_for_stock`
      takes — financial_items, ratios, as_reported_financial_items, revenue_segment,
      technical_analysis_items, estimate_items, institutional_ownership CIKs — for a
      custom one; the backend rejects the lists without the switch),
      "optimize-symbols", "optimize-portfolio", "list-optimized-stock-picks".

    `delivery` is "email" (default) or "dashboard".

    `regular_cron` is a raw 5-field cron expression controlling when the
    workflow fires (e.g. "30 6 * * 1-5" = weekdays 06:30), validated
    server-side. Each user may hold at most 10 active schedules.

    Returns 201 (created) or 200 (updated) with `workflow_id` and `task_name`
    ("wf:{email}:{name}"), the key that `list_scheduled_tasks` shows and
    `delete_scheduled_task` takes.
    """

    workflow: dict[str, Any] = {
        "name": name,
        "steps": steps,
        "delivery": delivery,
    }
    if description:
        workflow["description"] = description

    body: dict[str, Any] = {
        "workflow": workflow,
        "schedule": {"regular_cron": regular_cron},
    }

    return await _send(
        ctx, "POST", "/save-user-workflow", json=body
    )


@mcp.tool(annotations=ToolAnnotations(title="List Scheduled Tasks", readOnlyHint=True))
async def list_scheduled_tasks(
    ctx: Context,
) -> Any:
    """List the caller's scheduled tasks (cron jobs created via `schedule_task`).

    Returns only the authenticated user's jobs, each as
    {"name", "active", "schedule" (cron string), "args", "kwargs", "enabled",
     "last_run_at", "total_run_count"}. Workflow schedules are named
    "wf:{email}:{workflow-name}". Use `name` as the key to remove a job with
    `delete_scheduled_task`.
    """
    return await _send(ctx, "GET", "/get-scheduled-tasks")


@mcp.tool(annotations=ToolAnnotations(title="Delete Scheduled Task", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_scheduled_task(
    ctx: Context,
    task_name: str,
) -> Any:
    """Delete a scheduled task (cron job) by its name.

    `task_name` is the `name` returned by `list_scheduled_tasks` — for workflow
    schedules that is the "wf:{email}:{workflow-name}" value `schedule_task`
    returns as `task_name`, NOT the bare workflow name. Deleting removes only
    the cron schedule; the saved workflow definition is kept and can be
    re-scheduled by re-saving it via `schedule_task`. Returns
    {"msg": "<task_name> deleted"} on success, or an error if no such task exists.
    """
    return await _send(
        ctx, "DELETE", "/delete-scheduled-task",
        params={"task_name": task_name},
    )

@mcp.tool(annotations=ToolAnnotations(title="List Earnings Announcements", readOnlyHint=True))
async def list_earnings_announcements(
    ctx: Context,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    symbols: Optional[list[str]] = None,
    industry: Optional[list[str]] = None,
    sector: Optional[list[str]] = None,
    market_cap: Optional[list[str]] = None,
) -> Any:
    """List scheduled earnings announcements within a date window, Flexreport names only.

    `start_date` and `end_date` are `YYYY-MM-DD` strings; both default to today when
    omitted (so omit them for "who reports today"). Anything the backend can't parse as
    a date returns a 422 — do not pass other formats. Results are restricted to Flexreport's
    covered universe, so symbols outside coverage are dropped silently.

    Optional filters, all AND-ed together:
    - `symbols`     -> limit to these tickers (e.g. ["AAPL","MSFT"]).
    - `sector`      -> values from `list_options("sectors")`.
    - `industry`    -> values from `list_sub_industries([...])`.
    - `market_cap`  -> buckets like "Small-cap", "Medium-cap", "Large-cap", "Mega-cap".
    `sector`, `industry`, and `market_cap` are validated against fixed enums server-side;
    invalid values return a 422, so source them from the tools above rather than guessing.

    Returns a list of announcement records (empty list when nothing matches).

    Requires auth (your MCP client attaches the OAuth bearer automatically).
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
        ctx, "POST", "/list-upcoming-earnings-announcements", json=body
    )

@mcp.tool(annotations=ToolAnnotations(title="Check Market Open Status", readOnlyHint=True))
async def is_market_open(ctx: Context, exchange: str = "NYSE") -> Any:
    """Check whether an exchange is currently open — a cheap routing helper.

    `exchange` is a CASE-SENSITIVE exchange code (e.g. "NYSE", "NASDAQ", "LSE");
    defaults to NYSE, the right choice for US equities. An unknown or wrong-case
    code returns a 400 "Exchange not found." error — use uppercase codes, not
    full names.

    Returns a one-element list; the answer is its `isMarketOpen` boolean. The
    record also carries the session hours (`openingHour`/`closingHour` with UTC
    offsets), `timezone`, and any additional-session bounds.

    Use it to route between the market-hours tools: when the market is CLOSED,
    extended-hours data lives in `get_aftermarket_quotes`/`get_aftermarket_trades`;
    when OPEN, `detect_intraday_outlier_jumps` gives the live intraday read.
    No auth required.

    Unsure which exchange a ticker trades on? `get_company_snapshot` returns an
    `exchange` field whose value can be passed here as-is.
    """
    return await _send(
        ctx, "GET", "/is-market-open", params={"exchange" : exchange}, require_auth=False
    )


# --- Billing / plan upgrades ----------------------------------------------
# Both tools return a browser LINK only — the user completes payment/management
# in Stripe. This service never touches card or account details; the caller's
# forwarded bearer identifies the user, so neither tool takes any arguments.

@mcp.tool(annotations=ToolAnnotations(title="Upgrade Plan", readOnlyHint=False, destructiveHint=False))
async def upgrade_plan(ctx: Context) -> str:
    """Get a secure link for the user to upgrade or change their Flexreport Finance plan (e.g. to raise their request limit). Returns a checkout link — the user completes payment in their browser. Call this whenever the user asks to upgrade, subscribe, pay for, or change their plan, or when they've hit a usage/quota limit."""
    # /payment/upgrade-link is quota-exempt, so this is safe even after a 429.
    # _send forwards the caller's inbound bearer token.
    data = await _send(ctx, "GET", "/payment/upgrade-link")
    if not isinstance(data, dict) or data.get("error") or not data.get("url"):
        return ("Sorry, I couldn't generate an upgrade link just now. "
                "Please try again in a moment.")

    url = data["url"]
    u = data.get("usage") or {}
    lines = []
    if u.get("limit") is not None:  # omit the usage line when unlimited / usage null
        lines.append(
            f"You're currently on the **{u.get('plan', 'free')}** plan — "
            f"{u.get('used')}/{u.get('limit')} requests used ({u.get('period')})."
        )
    lines.append(f"👉 [Upgrade your plan]({url})")
    lines.append(
        "Open the link to choose a plan and complete checkout. Your new limit "
        "activates as soon as payment succeeds — no need to re-authenticate here."
    )
    return "\n\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(title="Manage Billing", readOnlyHint=False, destructiveHint=False))
async def manage_billing(ctx: Context) -> str:
    """Get a link to the Stripe billing portal where the user can view invoices, update their card, or cancel their subscription. Use when the user asks to manage, change, or cancel their billing/subscription."""
    # _send forwards the caller's inbound bearer token.
    data = await _send(ctx, "GET", "/payment/portal-link")
    if isinstance(data, dict) and data.get("url") and not data.get("error"):
        return f"👉 [Manage your billing]({data['url']})"

    err = data.get("error", "") if isinstance(data, dict) else ""
    if "HTTP 404" in err:  # no active subscription yet
        return ("You don't have an active paid subscription yet. "
                "Use upgrade_plan to subscribe.")
    return ("Sorry, I couldn't open the billing portal just now. "
            "Please try again in a moment.")




def _check_plan_registry() -> None:
    """Every tool situate's plan may emit must exist here — a rename fails at import,
    not as a dead hint in an agent's hands."""
    try:
        registered = {t.name for t in mcp._tool_manager.list_tools()}
    except Exception:  # SDK internals moved — do not block startup on the check
        return
    missing = sorted(set(situate_mod.TOOL_REGISTRY) - registered)
    if missing:
        raise RuntimeError(f"situate.TOOL_REGISTRY names tools that are not registered: {missing}")


_check_plan_registry()


@mcp.custom_route("/health", methods=["GET"])
async def health(_request) -> PlainTextResponse:
    """Liveness probe for load balancers (ALB target-group health check).

    Plain 200 outside the MCP protocol — the `/mcp` path speaks MCP and won't
    return 200 to a bare GET, so point the health check here.
    """
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
