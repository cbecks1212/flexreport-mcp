"""Market data tools: technical indicators, intraday outliers, after-hours bars, earnings calendar, market status."""

from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok
from schemas.market_data import (
    AftermarketResult,
    EarningsAnnouncementsResult,
    EarningsMoveResult,
    IntradayOutlierResult,
    MarketOpenResult,
    TechnicalIndicatorResult,
)


@mcp.tool(annotations=ToolAnnotations(title="Get Technical Indicator Data", readOnlyHint=True))
async def get_technical_indicator_data(
    ctx: Context,
    symbol: Annotated[str, Field(description="Covered ticker, e.g. \"AAPL\" (see list_options(\"tickers\")).")],
    indicator: Annotated[str, Field(description="Indicator name from list_options(\"technical_indicators\"), e.g. \"rsi\".")],
    start_date: Annotated[str, Field(description="Inclusive start date, YYYY-MM-DD.", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    end_date: Annotated[str, Field(description="Inclusive end date, YYYY-MM-DD.", pattern=r"^\d{4}-\d{2}-\d{2}$")],
) -> TechnicalIndicatorResult:
    """Fetch a historical technical-indicator series for a symbol over a date range.

    Call `list_options("technical_indicators")` first for valid indicator names. An
    unknown symbol returns 404 and an unknown indicator returns 400 listing valid values.

    Returns `rows`, one record per trading day ordered by date (columns beyond
    date/symbol/close vary by indicator), or an empty list when there's no data in the range.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    rows = _ok(await _send(
        ctx, "GET", "/technical-indicator-endpoint",
        params={
            "symbol": symbol,
            "indicator": indicator,
            "start_date": start_date,
            "end_date": end_date,
        },
    ))
    return TechnicalIndicatorResult(rows=rows)


@mcp.tool(annotations=ToolAnnotations(title="Detect Intraday Outlier Jumps", readOnlyHint=True))
async def detect_intraday_outlier_jumps(
    ctx: Context,
    symbol: Annotated[str, Field(description="Covered ticker, e.g. \"AAPL\".")],
    frequency: Annotated[
        Literal["one_minute", "five_minute", "thirty_minute", "one_hour", "four_hour"],
        Field(description="Intraday bar size; list_options(kind=\"intraday_frequency\") lists the supported values."),
    ] = "one_minute",
    zscore_threshold: Annotated[
        float,
        Field(description="Daily-sigma cutoff for flagging a bar: higher = stricter (fewer, more extreme flags), "
                          "lower = more sensitive.", gt=0),
    ] = 2.0,
) -> IntradayOutlierResult:
    """Live look at TODAY's intraday tape, flagging outlier price jumps.

    Pulls today's intraday bars for `symbol` (US/Eastern trading day) live at the given
    `frequency` and flags each bar whose move is a statistical outlier versus the stock's
    own daily-return volatility — i.e. bars where |z-score| of the move exceeds
    `zscore_threshold`. Use it for a quick "is the stock making an abnormal intraday move
    right now?" read.

    Synchronous — returns the flagged bars directly (no task id to poll). Returns a
    404-style error if no intraday data is available yet (e.g. before the market opens or
    for an uncovered symbol).

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    return _ok(await _send(
        ctx, "GET", "/detect-intraday-outlier-jumps",
        params={"symbol": symbol, "frequency": frequency, "zscore_threshold": zscore_threshold},
    ))


@mcp.tool(annotations=ToolAnnotations(title="Get Aftermarket Data", readOnlyHint=True))
async def get_aftermarket_data(
    ctx: Context,
    symbols: Annotated[
        list[str],
        Field(description="Covered tickers, e.g. [\"AAPL\", \"MSFT\"].", min_length=1),
    ],
    start_datetime: Annotated[
        Optional[str],
        Field(description="Inclusive lower bound on bar time, ET wall-clock ISO-8601 "
                          "(\"2026-06-24T16:00:00\"). Omit for 00:00:00 today ET."),
    ] = None,
    end_datetime: Annotated[
        Optional[str],
        Field(description="Inclusive upper bound on bar time, ET wall-clock ISO-8601. "
                          "Omit for 23:59:59 today ET."),
    ] = None,
) -> AftermarketResult:
    """Query STORED after-hours 15-minute bars (open/high/low/close/volume) for symbols.

    Returns the extended-hours bars the backend has captured for `symbols` — one row per
    symbol per 15-minute bar, 16:00-20:00 ET, ordered by symbol then bar time DESC (so the
    first row per symbol is the latest bar). This is the recorded aftermarket tape, NOT a
    live feed; it is polled every 15 minutes from 16:30 ET through the evening, so a bar
    can lag the wall clock by up to ~15 minutes. For a live intraday read of the regular
    session use `detect_intraday_outlier_jumps` instead.

    Leave both datetimes off for "today's aftermarket tape". An empty `bars` list means no
    bars were captured in the window (e.g. before 16:30 ET, or on a non-trading day) — not
    an error.

    Requires auth (your MCP client attaches the OAuth bearer automatically). Rate-limited
    to 300/minute per user server-side.
    """
    body: dict[str, Any] = {"symbols": symbols}
    if start_datetime:
        body["start_datetime"] = start_datetime
    if end_datetime:
        body["end_datetime"] = end_datetime
    bars = _ok(await _send(ctx, "POST", "/get-aftermarket-data", json=body))
    return AftermarketResult(bars=bars)


@mcp.tool(annotations=ToolAnnotations(title="Predict Post-Earnings Move", readOnlyHint=True))
async def predict_earnings_move(
    ctx: Context,
    symbols: Annotated[list[str], Field(description="Tickers to score, e.g. [\"AAPL\", \"NVDA\"].", min_length=1)],
) -> EarningsMoveResult:
    """
    Predict how big a stock's move will be when it next reports earnings. For each symbol, returns
    its next earnings date (today or later) and `expected_move`: the expected absolute move of the
    reaction session as a log return (0.05 is about 5%; size only, no direction). For context,
    `hist_move_mean` / `hist_move_n` give the stock's own average reaction over its past reports and
    how many reports that covers. Symbols with no scheduled report come back in
    `no_upcoming_earnings_symbols`; uncovered tickers in `not_supported_symbols`.
    """
    return _ok(await _send(
        ctx, "POST", "/predict-earnings-announcement-move", json={"symbols" : symbols }
    ))


@mcp.tool(annotations=ToolAnnotations(title="List Earnings Announcements", readOnlyHint=True))
async def list_earnings_announcements(
    ctx: Context,
    start_date: Annotated[
        Optional[str],
        Field(description="Window start, YYYY-MM-DD; defaults to today. Other formats return 422.",
              pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    end_date: Annotated[
        Optional[str],
        Field(description="Window end, YYYY-MM-DD; defaults to today. Other formats return 422.",
              pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    symbols: Annotated[Optional[list[str]], Field(description="Limit to these tickers, e.g. [\"AAPL\",\"MSFT\"].")] = None,
    industry: Annotated[Optional[list[str]], Field(description="Industry values from list_sub_industries([...]).")] = None,
    sector: Annotated[Optional[list[str]], Field(description="Sector values from list_options(\"sectors\").")] = None,
    market_cap: Annotated[
        Optional[list[str]],
        Field(description="Market-cap buckets: \"Small-cap\", \"Medium-cap\", \"Large-cap\", \"Mega-cap\"."),
    ] = None,
) -> EarningsAnnouncementsResult:
    """List scheduled earnings announcements within a date window, Flexreport names only.

    Omit both dates for "who reports today". Results are restricted to Flexreport's
    covered universe, so symbols outside coverage are dropped silently.

    The optional filters are AND-ed together. `sector`, `industry`, and `market_cap` are
    validated against fixed enums server-side; invalid values return a 422, so source them
    from the tools named in their descriptions rather than guessing.

    Returns `announcements` (empty list when nothing matches); values are strings and
    "nan" means not available yet.

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
    announcements = _ok(await _send(
        ctx, "POST", "/list-upcoming-earnings-announcements", json=body
    ))
    return EarningsAnnouncementsResult(announcements=announcements)

@mcp.tool(annotations=ToolAnnotations(title="Check Market Open Status", readOnlyHint=True))
async def is_market_open(
    ctx: Context,
    exchange: Annotated[
        str,
        Field(description="Exchange code, e.g. \"NYSE\", \"NASDAQ\", \"LSE\" or an ISO MIC like \"XNYS\" "
                          "(not full names; case does not matter). NYSE is right for US equities."),
    ] = "NYSE",
) -> MarketOpenResult:
    """Check whether an exchange is currently open — a cheap routing helper.

    Returns `is_open`, a single boolean. An unknown exchange code is an error.

    Use it to route between the market-hours tools: when the market is CLOSED,
    extended-hours data lives in `get_aftermarket_data`;
    when OPEN, `detect_intraday_outlier_jumps` gives the live intraday read.
    No auth required.

    Unsure which exchange a ticker trades on? `get_company_snapshot` returns an
    `exchange` field whose value can be passed here as-is.
    """
    is_open = _ok(await _send(
        ctx, "GET", "/is-market-open", params={"exchange" : exchange}, require_auth=False
    ))
    return MarketOpenResult(is_open=is_open)
