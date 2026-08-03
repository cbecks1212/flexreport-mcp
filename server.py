"""Flexreport MCP server — exposes the equity backend's live events and report
artifacts as on-demand MCP tools over streamable-http.

Each tool is a thin wrapper around a public backend HTTP endpoint. The caller's
inbound `Authorization: Bearer <JWT>` is forwarded so the backend enforces auth,
plan quota, and rate limits. This service holds no credentials and does not touch
AWS/Redis/DB or import anything from the API repo.
"""

import base64
import json
import os
from pathlib import Path
from typing import Any, Literal, Optional, get_args

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import PlainTextResponse

from client import MissingAuthError, auth_headers, get_client

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
# report override items, sectors, tickers, PDF tags, indicators, ...) instead
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
    "pdf_options": "/list-pdf-tag-options",
    "technical_indicators": "/list-technical-indicators",
    "tickers": "/list-tickers",
    "tickers_with_names": "/list-symbols-with-names",
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
    "pdf_options",
    "technical_indicators",
    "tickers",
    "tickers_with_names",
]
assert set(get_args(_OptionKind)) == set(_OPTION_ENDPOINTS)

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


@mcp.tool(annotations=ToolAnnotations(title="List Real-Time Market Events", readOnlyHint=True))
async def list_realtime_events(
    ctx: Context,
    event_type: str = "eps_update",
    tickers: Optional[list[str]] = None,
    sector: Optional[list[str]] = None,
    industry: Optional[list[str]] = None,
    market_cap: Optional[list[str]] = None,
) -> Any:
    """Pull live market events from the backend's Redis-backed cache (12h TTL).

    On ANY real-time/events request, call `list_options("event_types")` FIRST —
    it returns every event type with its family, description, and the
    related_events that fire around the same situation. Work from that
    catalogue, never a remembered list of types. `event_type` defaults to
    "eps_update", which is only one slice of the earnings family.

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



@mcp.tool(annotations=ToolAnnotations(title="Generate Bespoke Stock Report", readOnlyHint=False, destructiveHint=False))
async def generate_report_for_stock(
    ctx: Context,
    ticker: str,
    overrides: Optional[dict] = None,
) -> Any:
    """Build a NEW BESPOKE report on the fly for one ticker (slow, async; takes ~10 minutes).

    ===> This is an on-demand tool to create a fresh report on a company for a user. The suggested use is:
      (a) `get_latest_report` has no cached report for the ticker (it came back in
          `missing`), so there is nothing pre-built to return, OR the cached report is stale OR
      (b) the user EXPLICITLY wants the report to EMPHASIZE specific line items, ratios,
          estimates, or technical indicators that the cached report does not already
          highlight.

    Use the cached first, then this second. This should not be confused with generate_research_report, which should be used for more open-ended questions or when doing things like peer comps. generate_report_for_stock is dedicated to one ticker where the user's intent is very clear e.g. has an explicit set of metrics to focus on.

    Only `ticker` is required; the backend composes the report dynamically around the
    company's latest platform events and applies sensible defaults. The `overrides`
    keys the backend honors (all optional — any other key is accepted but IGNORED):
      - "financial_items", "ratios", "estimate_items" (lists) — must-cover EMPHASIS
        items woven into the report's coverage. Discover valid values with
        `list_options`: "financial_items" and "financial_ratios".
      - "technical_analysis_items" (list) — indicators to CHART; valid values via
        `list_options("technical_indicators")`.
      - "investment_thesis_impact", "significant_changes", "change_summary" (strings) —
        editorial steer: frames the report as a company update built around these points.
      - "use_api_financials" (bool) — source YoY financials live from the upstream data
        provider instead of the platform's cached view (fresher, slower).
      - "include_financials" / "include_ownership" (bools) — nudge the report's framing
        (financials-release vs ownership-change) when no recent platform event exists.
    Do NOT pass "include_transcript", "filing_frequency", "institutional_ownership", or
    price-date fields — the dynamic report builder ignores them.



    This is asynchronous. The response is keyed by ticker, e.g.
    {"AAPL": {"task_id": "...", "status": "PENDING"}}. Read result["AAPL"]["task_id"]
    and poll it with `get_task_status` until status is SUCCESS; the result carries the
    finished report as {"pdf": "<base64>", ...}.
    """
    payload = {"ticker": ticker, **(overrides or {})}
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

    ===> THE DEFAULT, FIRST-STEP tool whenever the user wants to EXPLORE the data or
    understand a topic from the data (e.g. "use Flexreport to explore...", "how are
    small caps doing — explore the data", "how have semiconductor margins trended?").
    It validates the request, plans queries against the data catalogue, runs them, and
    returns the raw result sets to render as INTERACTIVE CHARTS AND TABLES on the
    dashboard. For pure coverage/enumeration questions ("which symbols / sectors /
    indicators are available?", "is X covered?") prefer `list_options` instead — it
    answers instantly from the live catalogs, no async job needed.

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
    Render each entry as a chart/table for the user. If the request can't be served from
    the platform's data, `result` instead carries a `validation_status` of "REJECTED"
    (with a reason) — relay that rather than retrying blindly.
    """
    return await _send(
        ctx, "POST", "/data-catalogue-exploration",
        json={"query": query},
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Async Task Status", readOnlyHint=True))
async def get_task_status(ctx: Context, task_id: str) -> Any:
    """Poll the status of an async job (generate_research_report, explore_data_catalogue, screen_stocks, ...).

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
                 "report": "<base64 pdf>"}, ...],
    "missing": ["XYZ", ...]}. Each hit carries BOTH representations of the same
    PDF: `url` is a short-lived presigned link (valid ~12h) — hand it to the user
    to download/open the document directly (and prefer it on clients that can't
    handle a large base64 blob); `report` is the inline base64 PDF — decode it to
    read, render, or summarize the report's contents yourself. `missing` lists
    symbols with no cached report (for those, the user may want `onboard_symbol`
    to add coverage). Symbols are normalized (uppercased, de-duplicated) by
    the backend.

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
async def list_options(ctx: Context, kind: _OptionKind) -> Any:
    """Enumerate the valid values for a parameter, straight from the backend.

    Call this BEFORE guessing a parameter value. `kind` selects which catalog:

    - "event_types"                  -> valid `event_type` for `list_realtime_events`
                                        (eps_update, company_update, biggest_mover, ...)
    - "financial_items"              -> line items usable in a scheduled report's
                                        `overrides` (schedule_task task_type="report")
    - "financial_ratios"             -> ratios usable in those `overrides.ratios`
    - "sectors"                      -> valid `sector` filter values
    - "institutional_investor_types" -> valid `overrides.institutional_ownership`
                                        values for a scheduled report
    - "countries"                    -> covered countries
    - "fiscal_quarter"               -> the most recent fiscal quarter being reported
    - "market_cap"                   -> valid `market_cap` buckets (Small-cap,
                                        Medium-cap, Large-cap, Mega-cap)
    - "intraday_frequency"           -> supported intraday chart frequencies
    - "pdf_options"                  -> the complete `document_structure` tag DSL for
                                        `build_pdf_full_width` / `build_pdf_sidebar`:
                                        every block tag, its payload shape with a worked
                                        example, usage rules, and a full example document.
                                        Call before your FIRST build — do not guess
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
    return await _send(ctx, "GET", _OPTION_ENDPOINTS[kind], require_auth=False)


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


@mcp.tool(annotations=ToolAnnotations(title="Build Full-Width PDF", readOnlyHint=False, destructiveHint=False))
async def build_pdf_full_width(
    ctx: Context,
    document_structure: dict,
    font: str = "Times-Roman",
    font_size: int = 10,
) -> Any:
    """Render a tagged `document_structure` into a full-width PDF and return a link.

    THE DEFAULT PDF BUILDER — reach for this first. `document_structure` must
    follow the tag DSL from `list_options("pdf_options")` (call that first). The page is a
    single letter-size column: no sidebar rail, `_sidebar`-tagged blocks flow
    inline with everything else, and tag suffixes are optional (bare legacy tags
    like paragraph/table/line_chart are accepted). Blocks render in key insertion
    order and paginate normally. Best for memos, narratives, and any report whose
    content reads as one flowing column.

    Use `build_pdf_sidebar` INSTEAD only when the format genuinely benefits from a
    fixed left rail of at-a-glance metadata (e.g. an equity research note with
    rating, price target, and key stats beside the body). Decide from the user's
    request and the desired format.

    For charts, PREFER DATA OVER PIXELS: send raw series through the native chart
    tags (line/bar, or a 'multi_panel' item under line_chart_two_main for stacked
    price/RSI/MACD-style panels) and let the backend render them — a few KB of
    JSON instead of a large base64 image. Only use a figure tag for an image you
    already hold verbatim: hand-built base64 risks corruption in transit, and an
    unreadable image degrades to blank space instead of failing the build.

    `font`/`font_size` set the base body style (e.g. "Times-Roman", 10). Returns
    {"s3_presigned_url": "<time-limited S3 link to the PDF>", "warnings": [...]}.
    Hand the user the URL (it expires), or pass it to `download_pdf_from_url` to
    pull the bytes inline on clients that can't open the link. CHECK `warnings`: a
    non-empty list means one or more blocks (usually a figure/image) were dropped
    or rendered degraded.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    return await _send(
        ctx, "POST", "/create-pdf",
        json={"document_structure": document_structure, "font": font, "font_size": font_size},
    )


@mcp.tool(annotations=ToolAnnotations(title="Build Sidebar PDF", readOnlyHint=False, destructiveHint=False))
async def build_pdf_sidebar(
    ctx: Context,
    document_structure: dict,
    font: str = "Times-Roman",
    font_size: int = 10,
) -> Any:
    """Render a tagged `document_structure` into a sidebar-layout PDF and return a link.

    Same tag DSL and engine as `build_pdf_full_width` (call `list_options("pdf_options")`
    first) but the report adds a fixed grey left rail drawn on every page: put
    `_sidebar`-tagged blocks in the rail (rating, price target, key stats — keep
    it short; the rail does NOT paginate, so one page height of content max, or
    the overflow is dropped) and `_main`-tagged blocks in the paginated main column.

    Choose this over the default `build_pdf_full_width` only when the format
    genuinely calls for an at-a-glance metadata rail — a classic equity research
    note is the canonical case. For most documents (memos, narratives,
    single-column reports), prefer `build_pdf_full_width`. Decide from the user's
    request and the desired format.

    For charts, PREFER DATA OVER PIXELS: send raw series through the native chart
    tags (line/bar, or a 'multi_panel' item under line_chart_two_main) rather than
    embedding hand-built base64 images via figure tags — large base64 risks
    corruption in transit, and an unreadable image degrades to blank space.

    `font`/`font_size` set the base body style (e.g. "Times-Roman", 10). Returns
    {"s3_presigned_url": "<time-limited S3 link to the PDF>", "warnings": [...]}.
    Hand the user the URL (it expires), or pass it to `download_pdf_from_url` to
    pull the bytes inline on clients that can't open the link. CHECK `warnings`: a
    non-empty list means one or more blocks were dropped or rendered degraded.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    return await _send(
        ctx, "POST", "/create-pdf-sidebar",
        json={"document_structure": document_structure, "font": font, "font_size": font_size},
    )


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
    """
    return await _send(
        ctx, "GET", "/get-company-snapshot",
        params={"symbol": symbol}, require_auth=False,
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

@mcp.tool(annotations=ToolAnnotations(title="Schedule Recurring Task", readOnlyHint=False, destructiveHint=False))
async def schedule_task(
    ctx: Context,
    task_name: str,
    task_type: Literal["report", "screener", "research", "events"],
    instructions: dict,
    frequency: Literal["daily", "weekly", "monthly", "quarterly", "custom"] = "weekly",
    regular_cron: Optional[str] = None,
    custom_cron: Optional[str] = None,
    bulk_subscribe: bool = False,
) -> Any:
    """Schedule a RECURRING delivery (cron job) of a report, screen, research answer, or events.

    Use this when the user wants something delivered ON A SCHEDULE / repeatedly
    (e.g. "send me the AAPL report every Monday", "screen for cheap large-cap
    industrials monthly", "email me eps updates each morning"). For a ONE-OFF
    request, call the corresponding tool directly instead (get_latest_report,
    screen_stocks, generate_research_report, explore_data_catalogue,
    list_realtime_events).

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

    - "report"   -> recurring company report. instructions REQUIRES `ticker`; optional
                    override keys customize it — line items, ratios, filing frequency,
                    institutional-ownership cuts (discover valid values with
                    `list_options`), e.g.
                    {"ticker": "AAPL", "include_transcript": false, "ratios": [...]}.
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

    Discover valid values with the list tools (`list_options`,
    `list_sub_industries`) rather than guessing; invalid enum values are rejected
    server-side. `bulk_subscribe=True` subscribes a wider audience instead of just
    the caller — leave it False unless the user explicitly asks.

    Returns 201 on success.
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
        ctx, "POST", "/schedule-task", json=body
    )


@mcp.tool(annotations=ToolAnnotations(title="List Scheduled Tasks", readOnlyHint=True))
async def list_scheduled_tasks(
    ctx: Context,
) -> Any:
    """List the caller's scheduled tasks (cron jobs created via `schedule_task`).

    Returns only the authenticated user's jobs, each as
    {"name", "active", "schedule" (cron string), "args", "kwargs", "enabled",
     "last_run_at", "total_run_count"}. Use `name` as the key to remove a job with
    `delete_scheduled_task`.


    """
    return await _send(ctx, "GET", "/get-scheduled-tasks")


@mcp.tool(annotations=ToolAnnotations(title="Delete Scheduled Task", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_scheduled_task(
    ctx: Context,
    task_name: str,
) -> Any:
    """Delete a scheduled task (cron job) by its name.

    `task_name` is the `name` returned by `list_scheduled_tasks` (the same
    `task_name` used when the job was created via `schedule_task`). Returns
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




@mcp.custom_route("/health", methods=["GET"])
async def health(_request) -> PlainTextResponse:
    """Liveness probe for load balancers (ALB target-group health check).

    Plain 200 outside the MCP protocol — the `/mcp` path speaks MCP and won't
    return 200 to a bare GET, so point the health check here.
    """
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
