"""Sector performance tools: price returns and share volume rolled up by sector — how each of the 11
sectors has moved, and whether trading activity in it is rising or falling."""

import json
from typing import Annotated, Any, Optional

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send
from schemas.sector_performance import SectorPricePerformanceResult, SectorVolumePerformanceResult
from tools.earnings_season import MarketCapType, Sector, SpIndexFilter
from tools.ir_documents import _rows

SectorsFilter = Annotated[
    Optional[list[Sector]],
    Field(description="Only these sectors, e.g. [\"Technology\", \"Energy\"]. Omit for all 11."),
]


def _sector_rows(resp: Any, path: str, hint: str) -> list:
    """The backend's list; 422 raised with `hint`, 429 / 401 / other errors raised via _rows."""
    if isinstance(resp, dict) and "HTTP 422" in str(resp.get("error", "")):
        detail = resp.get("detail")
        if detail not in (None, "") and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        raise ToolError(
            f"{path} rejected the filters (HTTP 422): {hint}"
            + (f" Backend detail: {str(detail)[:1000]}" if detail not in (None, "") else "")
        )
    return _rows(resp, path)


def _empty_note(what: str, body: dict[str, Any]) -> str:
    set_filters = {k: v for k, v in body.items() if k != "limit"}
    scope = f"the filters {json.dumps(set_filters)}" if set_filters else "the request"
    return (f"No {what} matched {scope}. This is a valid answer, not an error. Drop a filter to widen it; "
            "if no filters are set, the sector data may not have been built yet.")


@mcp.tool(annotations=ToolAnnotations(title="Get Sector Price Performance", readOnlyHint=True))
async def get_sector_price_performance(
    ctx: Context,
    sectors: SectorsFilter = None,
    sp_index: SpIndexFilter = None,
) -> SectorPricePerformanceResult:
    """Price returns by sector over 1 day, 1 week, 1 month, 3 months, 6 months, 12 months and the year to date. Each row gives both the median company's return and the market-cap-weighted return. Use it for questions like "which sectors are leading this month" or "how has Energy done YTD, S&P 500 vs the rest".

    Both filters are optional. sp_index=true covers only S&P 500 members, false covers only
    companies outside the S&P 500, and leaving it out covers all of them. Unset filters are
    left out of the request.

    Returns {"filters", "count", "rows", "note"}, one row per sector. A row is {sector,
    universe, as_of, n_symbols, median_return_1d … _ytd, mcap_weighted_return_1d … _ytd}
    (windows 1d, 1w, 1m, 3m, 6m, 12m, ytd).
    - Returns are in PERCENT, NOT fractions: 1.25 = +1.25%, -3.4 = -3.4%. Show them as they
      are; do not multiply by 100.
    - The median shows the typical company. The market-cap-weighted figure is driven by the
      largest names. When the two differ a lot, a few mega-caps are carrying (or dragging)
      the sector.
    - as_of is the date the returns are measured to. n_symbols is the number of companies
      behind the row.

    FLOW: pair with get_sector_volume_performance to see whether a move came on rising volume.
    Use get_earnings_season_cohort(sector=...) to find the companies behind a move.

    An empty result is a valid answer, not an error: `note` is set only then. A 422 means a
    filter failed validation. A 401 means the sign-in expired (your MCP client re-runs
    OAuth). A 429 means the rate limit or the plan quota was hit. Requires auth (your MCP
    client attaches the OAuth bearer automatically); counts toward plan quota.
    """
    body: dict[str, Any] = {}
    if sectors:
        body["sectors"] = list(dict.fromkeys(sectors))
    if sp_index is not None:  # False is a real filter (outside the S&P 500), so test for None
        body["sp_index"] = sp_index
    rows = _sector_rows(
        await _send(ctx, "POST", "/sector-price-performance", json=body),
        "/sector-price-performance",
        "check sectors (exact sector names) and sp_index (a boolean).",
    )
    note = _empty_note("sector price performance", body) if not rows else None
    return SectorPricePerformanceResult(filters=body, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="Get Sector Volume Performance", readOnlyHint=True))
async def get_sector_volume_performance(
    ctx: Context,
    sectors: SectorsFilter = None,
    marketcap_types: Annotated[
        Optional[list[MarketCapType]],
        Field(description="Only these market-cap buckets, e.g. [\"Small-cap\", \"Micro-cap\"]. Omit for all five."),
    ] = None,
    limit: Annotated[
        int,
        Field(description="How many of the most recent dates to return (1-252, default 1 = latest only).", ge=1, le=252),
    ] = 1,
) -> SectorVolumePerformanceResult:
    """Share volume (shares traded) for US listings, by sector and market-cap bucket, over one-week, one-month, three-month, six-month and one-year windows. Each window also shows the change against the window before it. Use it for questions like "is trading in small-cap Tech picking up" or "where is volume drying up".

    Every filter is optional. Filters given together are ANDed (e.g. sectors=["Technology"] +
    marketcap_types=["Small-cap"]). Unset filters are left out of the request. limit counts
    dates, not rows: each date has one row per sector and market-cap bucket matched.

    Returns {"filters", "count", "rows", "note"}, one row per (date, sector,
    marketcap_type). A row is {date, sector, marketcap_type, one_week, one_month,
    three_months, six_months, one_year, one_week_pct, one_month_pct, three_months_pct,
    six_months_pct, one_year_pct}.
    - one_week … one_year are share volume over that window.
    - *_pct is the PERCENT change against the window just before it of the same length (e.g.
      the last month vs the month before): 12.5 = +12.5%. Show it as it is; do not multiply
      by 100.

    FLOW: pair with get_sector_price_performance to see whether price moves came on rising
    or falling volume.

    An empty result is a valid answer, not an error: `note` is set only then. A 422 means a
    filter failed validation. A 401 means the sign-in expired (your MCP client re-runs
    OAuth). A 429 means the rate limit or the plan quota was hit. Requires auth (your MCP
    client attaches the OAuth bearer automatically); counts toward plan quota.
    """
    body: dict[str, Any] = {}
    if sectors:
        body["sectors"] = list(dict.fromkeys(sectors))
    if marketcap_types:
        body["marketcap_types"] = list(dict.fromkeys(marketcap_types))
    body["limit"] = limit
    rows = _sector_rows(
        await _send(ctx, "POST", "/sector-volume-performance", json=body),
        "/sector-volume-performance",
        "check sectors and marketcap_types (exact enum values) and limit (1-252).",
    )
    note = _empty_note("sector volume rows", body) if not rows else None
    return SectorVolumePerformanceResult(filters=body, count=len(rows), rows=rows, note=note)
