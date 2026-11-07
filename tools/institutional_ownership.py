"""Institutional ownership tools: 13F position changes, ownership by manager type, portfolio allocation."""

from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok
from schemas.institutional_ownership import (
    CikAllocationResult,
    OwnershipByManagerTypeResult,
    PositionChangesResult,
)


PositionStatus = Literal["NEW", "CLOSED", "INCREASED", "DECREASED", "UNCHANGED"]

# Without a limit, industry is ~860-1,000 rows and 300 KB against ~77 rows and 25 KB for
# sector. Prior-quarter weights are still calculated when the limit is low.
_DEFAULT_QUARTERS = {"sector": 8, "industry": 2}


def _rows(resp: Any, path: str) -> list:
    rows = _ok(resp)
    if not isinstance(rows, list):
        raise ToolError(f"Unexpected {path} response: {str(rows)[:500]}")
    return rows


@mcp.tool(annotations=ToolAnnotations(title="Get 13F Position Changes", readOnlyHint=True))
async def get_13f_position_changes(
    ctx: Context,
    cik: Annotated[
        str,
        Field(description="The filer's SEC CIK, up to 10 digits. Get it from list_options(\"institutional_managers\", q=...)."),
    ],
    position_filter: Annotated[
        list[PositionStatus],
        Field(description="Statuses to return (default NEW and CLOSED). Add INCREASED, DECREASED or UNCHANGED for "
                          "the rest of the book: a large filer's full book is thousands of rows.", min_length=1),
    ] = ["NEW", "CLOSED"],
) -> PositionChangesResult:
    """What one 13F filer bought, sold or held in its latest filed quarter, one row per position — the direct read for "what did this fund buy or exit".

    FLOW: list_options("institutional_managers", q="blackrock") gives the CIK.

    `position_filter` DEFAULTS TO NEW AND CLOSED to keep the payload down. BlackRock with
    all five statuses is 2,680 rows and 1.16 MB, so widen it only when the question needs
    the adds, trims or unchanged holdings.

    Returns {"cik", "position_filter", "count", "rows"}, largest value first. A row is
    {date, symbol, manager_name, status, shares, value, prev_shares, prev_value, share_chg,
    share_chg_pct, value_chg, value_chg_pct, accessions, sec_urls}. `accessions` and
    `sec_urls` are lists because a position can come from more than one filing; a CLOSED row
    cites the last filing that reported the position.

    DATA QUALITY: a small share of values are wrong as filed or parsed, and they are not
    corrected. An implausible value or jump may be a filer error: check `sec_urls`.

    An empty result means no rows, not necessarily no activity: `note` says so. A 422 means
    a bad CIK. Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min; counts toward plan quota.
    """
    cik = cik.strip()
    rows = _rows(
        await _send(ctx, "POST", "/13f-position-change", json={"cik": cik, "position_filter": position_filter}),
        "/13f-position-change",
    )
    note = None
    if not rows:
        note = (f"No {'/'.join(position_filter)} positions for CIK {cik} in its latest filed quarter. That is not "
                "proof of no activity: try the other statuses, or confirm the CIK with "
                "list_options(\"institutional_managers\", cik=...).")
    return PositionChangesResult(cik=cik, position_filter=position_filter, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="Get 13F Ownership by Manager Type", readOnlyHint=True))
async def get_13f_ownership_by_manager_type(
    ctx: Context,
    symbol: Annotated[str, Field(description="Covered ticker, e.g. \"AAPL\".")],
    manager_type: Annotated[
        Optional[list[str]],
        Field(description="Manager types from list_options(\"institutional_investor_types\"), e.g. [\"Hedge Fund\", "
                          "\"Pension Fund\"]. Omit for all 13.", min_length=1),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Most recent QUARTERS, not rows (1-8, default 8).", ge=1, le=8),
    ] = 8,
) -> OwnershipByManagerTypeResult:
    """Who owns a stock by manager type (hedge funds, pension funds, mutual funds...), per quarter, with quarter-over-quarter and year-over-year changes — the direct read for "are hedge funds buying X".

    Returns {"symbol", "count", "rows"}, one row per manager type per quarter, newest
    quarter first. A row is {date, symbol, investor_type, total_shares, num_investors,
    current_period_value, previous_quarter_value, previous_year_value,
    previous_quarter_change, previous_year_change}.

    READING THE NUMBERS:
    - The changes are percentages of VALUE, so they include price moves. Read them alongside
      `total_shares` before calling something buying or selling.
    - The changes are not a share of the company.
    - A change is null when the prior period has no row or a value of 0. Year-over-year only
      exists from 2025-12-31 onward, because 8 quarters are retained.
    - Quarters still partial during the 13F filing window are hidden, so the newest quarter
      can be missing until filings land.
    - A cohort that fully exits has no row, so it never shows -100%.

    DATA QUALITY: a small share of values are wrong as filed or parsed, and they are not
    corrected, so an implausible value or jump may be a filer error.

    AN EMPTY RESULT means no rows, not necessarily no holders: BRK.B is not in coverage, so
    it returns nothing even though 4,946 filers hold it. `note` is set only then. A 422 means
    an invalid manager_type or symbol. Requires auth (your MCP client attaches the OAuth
    bearer automatically). Rate-limited 100/min; counts toward plan quota.
    """
    symbol = symbol.strip().upper()
    body: dict[str, Any] = {"symbol": symbol, "limit": limit}
    if manager_type:
        body["manager_type"] = manager_type
    rows = _rows(await _send(ctx, "POST", "/13f-position-by-manager-type", json=body), "/13f-position-by-manager-type")
    note = None
    if not rows:
        note = (f"No rows for {symbol}. That is not proof of no institutional holders: the symbol may be outside "
                "coverage (BRK.B returns nothing, though thousands of filers hold it), or these manager types may "
                "hold none.")
    return OwnershipByManagerTypeResult(symbol=symbol, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="Get 13F Portfolio Allocation", readOnlyHint=True))
async def get_13f_cik_allocation(
    ctx: Context,
    cik: Annotated[
        str,
        Field(description="The filer's SEC CIK, up to 10 digits. Get it from list_options(\"institutional_managers\", q=...)."),
    ],
    group_by: Annotated[
        Literal["sector", "industry"],
        Field(description="Group by sector (default) or industry. Rows label the group under this field name."),
    ] = "sector",
    limit: Annotated[
        Optional[int],
        Field(description="The filer's most recent quarters (1-8). Defaults to 8 for sector and 2 for industry "
                          "(industry at 8 quarters is about 300 KB).", ge=1, le=8),
    ] = None,
) -> CikAllocationResult:
    """One 13F filer's portfolio weights by sector or industry per quarter, compared with all institutions — the direct read for "how is this fund positioned" and where it tilts.

    FLOW: list_options("institutional_managers", q="citadel") gives the CIK.

    Returns {"cik", "group_by", "limit", "count", "rows"}, newest quarter first. A row is
    {date, cik, manager_name, sector OR industry, positions, value, weight_pct,
    previous_quarter_weight_pct, weight_chg_pts, all_institution_weight_pct, covered_value,
    reported_portfolio_value, coverage_pct}.

    READING THE NUMBERS:
    - `weight_pct` IS A SHARE OF `covered_value`, NOT OF THE WHOLE PORTFOLIO. It covers long
      stock positions mapped to a symbol; ETFs, unmapped CUSIPs and options are excluded.
      Median coverage for Registered Investment Advisors is 41.5% of the reported portfolio.
      ALWAYS report `coverage_pct` alongside a weight.
    - `coverage_pct` and `reported_portfolio_value` are null except for the filer's latest
      profiled quarter. Citadel reads 15.94% because its reported $875B is mostly options.
    - `previous_quarter_weight_pct` is 0 if the filer held nothing in that group in a quarter
      it filed, and null if it did not file that quarter.
    - `all_institution_weight_pct` is null for a partial quarter. Tilt is `weight_pct` minus
      this value; compute it, since it is not returned.
    - A fully exited group has no row, so a quarter's `weight_chg_pts` won't sum to 0.
    - Sector and industry labels are current, not historical.

    DATA QUALITY: a small share of values are wrong as filed or parsed, and they are not
    corrected, so an implausible value or jump may be a filer error.

    A large filer's first call can take several seconds; repeats are far faster. An empty
    result means no rows for that CIK (`note` is set only then). A 422 means a bad CIK or
    limit. Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min; counts toward plan quota.
    """
    cik = cik.strip()
    quarters = limit if limit is not None else _DEFAULT_QUARTERS[group_by]
    rows = _rows(
        await _send(ctx, "POST", "/13f-cik-allocation", json={"cik": cik, "group_by": group_by, "limit": quarters}),
        "/13f-cik-allocation",
    )
    note = None
    if not rows:
        note = (f"No holdings for CIK {cik}. Confirm the CIK with list_options(\"institutional_managers\", cik=...): "
                "the filer may hold nothing that maps to a covered symbol.")
    return CikAllocationResult(cik=cik, group_by=group_by, limit=quarters, count=len(rows), rows=rows, note=note)
