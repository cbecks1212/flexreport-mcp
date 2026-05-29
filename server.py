"""FlexReport MCP server — exposes the equity backend's live events and report
artifacts as on-demand MCP tools over streamable-http.

Each tool is a thin wrapper around a public backend HTTP endpoint. The caller's
inbound `Authorization: Bearer <JWT>` is forwarded so the backend enforces auth,
plan quota, and rate limits. This service holds no credentials and does not touch
AWS/Redis/DB or import anything from the API repo.
"""

import os
from typing import Any, Literal, Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP
from starlette.responses import PlainTextResponse

from client import MissingAuthError, auth_headers, get_client

# Advertised to every client on connect (MCP `instructions`) — the hardwired
# auth playbook so any agent self-serves login without guessing.
INSTRUCTIONS = """\
FlexReport Finance — live market events and equity research as tools.

AUTHENTICATE before any data tool. The user needs a FlexReport account + a JWT:
- Existing user: call get_token(username=<email>, password=<password>) -> access_token.
- New user (no account): call register_user(email, password); the backend emails a
  confirmation link/token — have the user click the link (or paste the token to
  confirm_registration); then call get_token.
Pass the access_token as the `bearer_token` argument on every data tool
(list_realtime_events, generate_report, generate_research_report,
get_report_artifact, onboard_symbol). On HTTP 401 the token expired — call
get_token again and retry. Do NOT ask the user to paste tokens into config files.

NO AUTH NEEDED: list_report_options, list_tickers, list_sub_industries,
get_company_snapshot, get_task_status. Use list_report_options(...) to discover
valid parameter values instead of guessing.
"""

mcp = FastMCP(
    "flexreport",
    instructions=INSTRUCTIONS,
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    # Behind a load balancer (ALB): make each request self-contained instead of
    # holding a long-lived per-session SSE stream the LB would choke on, and return
    # plain JSON rather than text/event-stream. Stateless mode has no persistent
    # session, so auth is per-call `bearer_token` (from get_token), not a cache.
    stateless_http=True,
    json_response=True,
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
    """Kick off generation of a full structured research report for one ticker.

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
    """Generate a research report from a plain-English query (extensible, multi-section).

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
async def get_report_artifact(
    ctx: Context,
    symbols: list[str],
    bearer_token: Optional[str] = None,
) -> Any:
    """Bulk-fetch cached default PDF reports for a list of ticker symbols.

    Returns {"result": [{"symbol": "AAPL", "report": "<base64 pdf>"}, ...],
    "missing": ["XYZ", ...]} — `missing` lists symbols with no cached report.
    Symbols are normalized (uppercased, de-duplicated) by the backend.
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

@mcp.tool()
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


@mcp.tool()
async def confirm_registration(ctx: Context, token: str) -> Any:
    """Confirm a registration with the emailed token (step 2 of the auth flow).

    Pre-auth — no JWT required. `token` is the value emailed by `register_user`.
    """
    return await _send(ctx, "GET", f"/confirm/{token}", require_auth=False)


@mcp.tool()
async def get_token(ctx: Context, username: str, password: str) -> Any:
    """Exchange credentials for a bearer JWT (OAuth2 password flow). Pre-auth.

    THE login step. A new user calls this after confirming registration; an
    existing user calls it directly. `username` is the account email.

    Returns {"access_token": "<jwt>", "token_type": "bearer"}. Pass the
    `access_token` as the `bearer_token` argument on every authenticated tool
    (list_realtime_events, generate_report, generate_research_report,
    get_report_artifact, onboard_symbol). Re-call this and retry on a 401.

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
