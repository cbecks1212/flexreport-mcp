"""Market data tools: technical indicators, intraday outliers, after-hours bars, earnings calendar, market status."""

from typing import Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from core import mcp, _send


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


@mcp.tool(annotations=ToolAnnotations(title="Get Aftermarket Data", readOnlyHint=True))
async def get_aftermarket_data(
    ctx: Context,
    symbols: list[str],
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
) -> Any:
    """Query STORED after-hours 15-minute bars (open/high/low/close/volume) for symbols.

    Returns the extended-hours bars the backend has captured for `symbols` — one row per
    symbol per 15-minute bar, 16:00-20:00 ET, ordered by symbol then bar time DESC (so the
    first row per symbol is the latest bar). Each row carries `symbol`, `date` (the bar's
    ET wall-clock time), `open`, `high`, `low`, `close`, `volume`. This is the recorded
    aftermarket tape, NOT a live feed; it is polled every 15 minutes from 16:30 ET through
    the evening, so a bar can lag the wall clock by up to ~15 minutes. For a live intraday
    read of the regular session use `detect_intraday_outlier_jumps` instead.

    `start_datetime` and `end_datetime` bound the bar time and are ET wall-clock ISO-8601
    timestamps (e.g. "2026-06-24T16:00:00"). Both are OPTIONAL: omit them and the backend
    defaults to the start (00:00:00) and end (23:59:59) of today in ET, so leave them off
    for "today's aftermarket tape". An empty list means no bars were captured in the
    window (e.g. before 16:30 ET, or on a non-trading day) — not an error.

    Requires auth (your MCP client attaches the OAuth bearer automatically). Rate-limited
    to 300/minute per user server-side.
    """
    body: dict[str, Any] = {"symbols": symbols}
    if start_datetime:
        body["start_datetime"] = start_datetime
    if end_datetime:
        body["end_datetime"] = end_datetime
    return await _send(ctx, "POST", "/get-aftermarket-data", json=body)


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
    extended-hours data lives in `get_aftermarket_data`;
    when OPEN, `detect_intraday_outlier_jumps` gives the live intraday read.
    No auth required.

    Unsure which exchange a ticker trades on? `get_company_snapshot` returns an
    `exchange` field whose value can be passed here as-is.
    """
    return await _send(
        ctx, "GET", "/is-market-open", params={"exchange" : exchange}, require_auth=False
    )
