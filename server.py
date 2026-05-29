"""FlexReport MCP server — exposes the equity backend's live events and report
artifacts as on-demand MCP tools over streamable-http.

Each tool is a thin wrapper around a public backend HTTP endpoint. The caller's
inbound `Authorization: Bearer <JWT>` is forwarded so the backend enforces auth,
plan quota, and rate limits. This service holds no credentials and does not touch
AWS/Redis/DB or import anything from the API repo.
"""

import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP

from client import MissingAuthError, auth_headers, get_client

mcp = FastMCP(
    "flexreport",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
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
    **kwargs: Any,
) -> Any:
    """Forward a request to the backend, returning parsed JSON or a structured error.

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
        return {"error": f"Backend returned HTTP {resp.status_code}", "detail": detail}

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
) -> Any:
    """Pull live earnings/market events from the backend's Redis-backed cache (12h TTL).

    `event_type` defaults to "eps_update". Other values include: eps_release,
    8k_release, earnings_transcript_update, analyst_rating_update, news_evolution,
    earnings_themes, llm_basket_update, strategy_update. The backend's
    GET /list-realtime-event-options enumerates the authoritative set.

    Optionally narrow results by `tickers`, `sector`, `industry`, or `market_cap`
    (e.g. market_cap=["Large-cap","Mega-cap"]). Returns a list of event objects,
    or an empty list when the cache is cold.
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


@mcp.tool()
async def generate_report(
    ctx: Context,
    ticker: str,
    overrides: Optional[dict] = None,
) -> Any:
    """Kick off generation of a full structured research report for one ticker.

    Only `ticker` is required; the backend applies sensible defaults for everything
    else. Pass `overrides` to customize the report (e.g.
    {"include_transcript": false, "ratios": [...], "filing_frequency": "annual"}).

    This is asynchronous. The response is keyed by ticker, e.g.
    {"AAPL": {"task_id": "...", "status": "PENDING"}}. Read result["AAPL"]["task_id"]
    and poll it with `get_task_status` until status is SUCCESS.
    """
    payload = {"ticker": ticker, **(overrides or {})}
    return await _send(ctx, "POST", "/create-full-report", json=payload)


@mcp.tool()
async def generate_research_report(
    ctx: Context,
    query: str,
    delivery: str = "email",
) -> Any:
    """Generate a research report from a plain-English query (extensible, multi-section).

    `query` is natural language, e.g. "high-growth semis with rising estimates".
    `delivery` defaults to "email". Rate-limited to 20/hour per user server-side.

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll with
    `get_task_status` until SUCCESS, then read its `result`.
    """
    return await _send(
        ctx, "POST", "/generate-research-report",
        json={"query": query, "delivery": delivery},
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
async def get_report_artifact(ctx: Context, symbols: list[str]) -> Any:
    """Bulk-fetch cached default PDF reports for a list of ticker symbols.

    Returns {"result": [{"symbol": "AAPL", "report": "<base64 pdf>"}, ...],
    "missing": ["XYZ", ...]} — `missing` lists symbols with no cached report.
    Symbols are normalized (uppercased, de-duplicated) by the backend.
    """
    return await _send(ctx, "POST", "/get-cached-reports", json=symbols)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
