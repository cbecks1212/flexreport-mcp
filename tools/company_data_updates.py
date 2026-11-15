"""Per-company data-update tools: earnings history, analyst estimates, analyst rating actions and
summary, insider transactions and insider activity. Until these endpoints, the same rows were
reachable only through explore_data_catalogue or a signed drilldown link."""

import json
from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok
from schemas.company_data_updates import (
    AnalystEstimatesResult,
    EarningsHistoryResult,
    InsiderActivitySummaryResult,
    InsiderTransactionsResult,
    RatingActionsResult,
    RatingSummaryResult,
)
from tools.ir_documents import _TICKER_PATTERN

InsiderSide = Literal["BUY", "SELL", "GRANT", "EXERCISE", "TAX_WITHHOLD", "GIFT_IN", "GIFT_OUT", "OTHER"]
InsiderRole = Literal["ceo", "director", "founder", "ten_pct_owner"]

Symbol = Annotated[str, Field(description="Ticker, e.g. \"CSCO\".", pattern=_TICKER_PATTERN)]


def _is_error(resp: Any) -> bool:
    return isinstance(resp, dict) and "error" in resp and set(resp) <= {"error", "detail"}


def _payload(resp: Any, path: str, hint: str) -> Any:
    """The backend's JSON, with 422 and 429 raised with a hint (401 and the rest via _ok)."""
    if _is_error(resp):
        err = str(resp.get("error", ""))
        detail = resp.get("detail")
        if detail not in (None, "") and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        suffix = f" Backend detail: {str(detail)[:1000]}" if detail not in (None, "") else ""
        if "HTTP 422" in err:
            raise ToolError(f"{path} rejected the request (HTTP 422): {hint}{suffix}")
        if "HTTP 429" in err:
            raise ToolError(
                f"{path} is rate-limited to 100 requests/minute per user (or the plan's quota is used up) and the "
                "limit was hit. Wait a minute before retrying, answer from results already fetched, or call "
                "upgrade_plan if the user hit their plan quota." + suffix
            )
    return _ok(resp)


def _list(resp: Any, path: str, hint: str) -> list:
    rows = _payload(resp, path, hint)
    if not isinstance(rows, list):
        raise ToolError(f"Unexpected {path} response: {str(rows)[:500]}")
    return rows


def _dict(resp: Any, path: str, hint: str) -> dict:
    data = _payload(resp, path, hint)
    if not isinstance(data, dict):
        raise ToolError(f"Unexpected {path} response: {str(data)[:500]}")
    return data


_SYMBOL_HINT = "check symbol (letters and digits, \".\" or \"-\" for share classes) and limit."


def _not_covered(symbol: str) -> str:
    return (f"An unknown or uncovered symbol also returns nothing (never a 404), so confirm {symbol} with "
            "list_options(\"tickers\") before reading this as an absence.")


@mcp.tool(annotations=ToolAnnotations(title="Get Earnings History", readOnlyHint=True))
async def get_earnings_history(
    ctx: Context,
    symbol: Symbol,
    limit: Annotated[
        int,
        Field(description="REPORTED quarters to return (1-20, default 8). The next scheduled report, when known, is "
                          "added on top, so up to limit + 1 rows come back.", ge=1, le=20),
    ] = 8,
) -> EarningsHistoryResult:
    """One company's quarterly earnings reports — EPS and revenue actual vs estimate, surprise, beat/miss, EPS YoY — newest first, with the next scheduled report on top. The direct read for "did X beat last quarter", "X's beat/miss record", "when does X report next".

    FLOW: pair with get_analyst_estimates for the forward consensus, company_financials for
    the statements. get_earnings_season_cohort is the cross-company view of the current
    season; predict_earnings_move sizes the next reaction.

    Returns {"symbol", "limit", "count", "rows", "note"}, newest first. A row is {symbol,
    date, status, timing, fiscal_year, fiscal_period, period_ending, eps_actual,
    eps_estimated, eps_surprise, revenue_actual, revenue_estimated, revenue_surprise,
    beat_flag, eps_yoy_change}.
    - status is "upcoming" (the next scheduled report: actuals and surprises are null) or
      "reported". timing is "before_open", "after_close" or null.
    - eps_surprise, revenue_surprise and eps_yoy_change are FRACTIONS (0.05 = +5%), not
      percents: multiply by 100 before showing a %.
    - beat_flag is "beat", "miss" or "in_line".
    - EPS is the adjusted (street) figure the estimate is made on, NOT the GAAP EPS in
      company_financials. Never compare eps_actual with company_financials' EPS.

    An unknown symbol returns no rows (never a 404): `note` is set only then. A 422 means a
    bad symbol or limit. A 401 means the sign-in expired (your MCP client re-runs OAuth).
    Requires auth (your MCP client attaches the OAuth bearer automatically). Rate-limited
    100/min per user; counts toward plan quota.
    """
    symbol = symbol.strip().upper()
    rows = _list(
        await _send(ctx, "POST", "/earnings-history", json={"symbol": symbol, "limit": limit}),
        "/earnings-history", _SYMBOL_HINT,
    )
    note = None
    if not rows:
        note = f"No earnings reports on file for {symbol}. {_not_covered(symbol)}"
    return EarningsHistoryResult(symbol=symbol, limit=limit, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="Get Analyst Estimates", readOnlyHint=True))
async def get_analyst_estimates(
    ctx: Context,
    symbol: Symbol,
    limit: Annotated[
        int,
        Field(description="Quarters (1-20, default 8), counted back from the furthest of the next 4 quarters it "
                          "reaches: 8 = the next 4 + the last 4 ended; 2 = the next 2.", ge=1, le=20),
    ] = 8,
) -> AnalystEstimatesResult:
    """One company's quarterly analyst consensus — EPS, revenue, EBITDA and net income averages, EPS/revenue low-high, analyst counts, consensus YoY growth — for the next 4 quarters and the quarters just ended, newest period first. The direct read for "what does the street expect from X".

    FLOW: get_earnings_history for the reported EPS/revenue surprise per quarter;
    company_financials for the GAAP statements.

    Returns {"symbol", "limit", "count", "rows", "note"}, newest period first. A row is
    {symbol, period_end, period_ended, eps_avg, eps_low, eps_high, revenue_avg, revenue_low,
    revenue_high, ebitda_avg, net_income_avg, num_analysts_eps, num_analysts_revenue,
    eps_yoy_growth, revenue_yoy_growth, est_range_is_modeled}.

    READING THE NUMBERS:
    - Actual vs estimate is worked out by the agent: line an ended quarter up with
      company_financials by period_end to the NEAREST date (they can differ by a day, e.g.
      2025-10-24 vs 2025-10-25), or use get_earnings_history for EPS/revenue surprise.
    - EPS here is adjusted; EDGAR EPS in company_financials is GAAP, so comparing the two
      directly misreads a beat as a miss (CSCO: 0.85 GAAP vs 1.03 consensus).
    - Growth columns are consensus vs the year-ago quarter's consensus, as FRACTIONS
      (0.05 = +5%).
    - eps/revenue low and high are null when est_range_is_modeled: the vendor's band there
      is synthetic.
    - There is no estimate revision history: an ended quarter shows only the final
      pre-report consensus.
    - A just-ended quarter can be period_ended=true before it is reported.

    An unknown symbol returns no rows (never a 404): `note` is set only then. A 422 means a
    bad symbol or limit. A 401 means the sign-in expired (your MCP client re-runs OAuth).
    Requires auth (your MCP client attaches the OAuth bearer automatically). Rate-limited
    100/min per user; counts toward plan quota.
    """
    symbol = symbol.strip().upper()
    rows = _list(
        await _send(ctx, "POST", "/analyst-estimates", json={"symbol": symbol, "limit": limit}),
        "/analyst-estimates", _SYMBOL_HINT,
    )
    note = None
    if not rows:
        note = f"No analyst estimates on file for {symbol}: it may have no analyst coverage. {_not_covered(symbol)}"
    return AnalystEstimatesResult(symbol=symbol, limit=limit, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="Get Analyst Rating Actions", readOnlyHint=True))
async def get_analyst_rating_actions(
    ctx: Context,
    symbol: Symbol,
    limit: Annotated[int, Field(description="Actions to return (1-200, default 25).", ge=1, le=200)] = 25,
) -> RatingActionsResult:
    """One company's analyst rating actions — upgrades, downgrades and maintains, firm by firm — newest first. The direct read for "who downgraded X", "recent analyst actions on X".

    FLOW: get_analyst_rating_summary for where each firm stands now and the consensus;
    get_analyst_estimates for the numbers behind the ratings.

    Returns {"symbol", "count", "rows", "note"}, newest first. A row is {date,
    rating_company, prev_rating, new_rating, action, news_title, publisher, news_url}.
    - action is "upgrade", "downgrade" or "maintain".
    - Ratings are in each firm's own words (Overweight, Buy, Outperform...): do not map them
      onto one scale without saying so.
    - news_* are null for most actions: only each firm's latest sourced article is on file.

    An unknown symbol returns no rows (never a 404): `note` is set only then. A 422 means a
    bad symbol or limit. A 401 means the sign-in expired (your MCP client re-runs OAuth).
    Requires auth (your MCP client attaches the OAuth bearer automatically). Rate-limited
    100/min per user; counts toward plan quota.
    """
    symbol = symbol.strip().upper()
    rows = _list(
        await _send(ctx, "POST", "/analyst-rating-actions", json={"symbol": symbol, "limit": limit}),
        "/analyst-rating-actions", _SYMBOL_HINT,
    )
    note = None
    if not rows:
        note = f"No analyst rating actions on file for {symbol}: it may have no analyst coverage. {_not_covered(symbol)}"
    return RatingActionsResult(symbol=symbol, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="Get Analyst Rating Summary", readOnlyHint=True))
async def get_analyst_rating_summary(
    ctx: Context,
    symbol: Symbol,
    limit: Annotated[
        int,
        Field(description="Firms to list (1-100, default 25), most recently active first.", ge=1, le=100),
    ] = 25,
) -> RatingSummaryResult:
    """Where the analysts stand on one company now — the consensus rating, how many firms cover it, each firm's current rating and latest action, and a written analysis. The direct read for "what's the street's rating on X".

    FLOW: get_analyst_rating_actions for each firm's action history; get_analyst_estimates
    for the consensus numbers.

    Returns {"symbol", "consensus_rating", "firm_count", "analysis", "firms", "note"}.
    - firms is [{rating_company, rating, prev_rating, action, date}], most recently active
      first; ratings are in each firm's own words.
    - firm_count covers every firm that acted in the year up to the latest action, even when
      limit cuts the list.
    - analysis is {date, consensus_rating, summary} or null. It can be months old (CSCO's is
      dated 2025-11-12): report its date, never present it as current.

    An unknown symbol returns empty lists (never a 404): `note` is set only then. A 422
    means a bad symbol or limit. A 401 means the sign-in expired (your MCP client re-runs
    OAuth). Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min per user; counts toward plan quota.
    """
    symbol = symbol.strip().upper()
    data = _dict(
        await _send(ctx, "POST", "/analyst-rating-summary", json={"symbol": symbol, "limit": limit}),
        "/analyst-rating-summary", _SYMBOL_HINT,
    )
    data = {**data, "symbol": data.get("symbol") or symbol, "firms": data.get("firms") or []}
    if not data["firms"] and not data.get("analysis") and not data.get("consensus_rating"):
        data["note"] = f"No analyst ratings on file for {symbol}: it may have no analyst coverage. {_not_covered(symbol)}"
    return RatingSummaryResult(**data)


@mcp.tool(annotations=ToolAnnotations(title="Get Insider Transactions", readOnlyHint=True))
async def get_insider_transactions(
    ctx: Context,
    symbol: Symbol,
    side: Annotated[
        list[InsiderSide],
        Field(description="Transaction sides to return (default BUY and SELL: open-market trades). Add GRANT, "
                          "EXERCISE, TAX_WITHHOLD, GIFT_IN, GIFT_OUT or OTHER for the rest.", min_length=1),
    ] = ["BUY", "SELL"],
    role: Annotated[
        Optional[list[InsiderRole]],
        Field(description="Only insiders with ANY of these roles, e.g. [\"ceo\"]. Omit for every senior insider.",
              min_length=1),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Transactions to return (1-500, default 50). Rows are ~1 KB each: keep it modest.",
              ge=1, le=500),
    ] = 50,
) -> InsiderTransactionsResult:
    """One company's individual insider (Form 4) transactions — who bought or sold, how many shares, at what price, under a 10b5-1 plan or not — newest first. The direct read for "has X's CEO been selling", "insider buys at X".

    Senior insiders only, last 6 months.

    FLOW: get_insider_activity_summary first for the quarter-by-quarter picture and who
    the net buyers/sellers are; then this for the trades behind a quarter. `sec_url` is the
    filing.

    Returns {"symbol", "side", "role", "count", "rows", "note"}, newest first. A row is
    {transaction_date, filing_date, insider_name, position, is_ceo, is_director, is_founder,
    is_ten_pct_owner, side, transaction_code, shares, filing_price, close_price, mkt_value,
    shares_owned_after, is_10b5_1, direct_indirect, security, accession, sec_url, footnotes}.

    READING THE NUMBERS:
    - mkt_value is shares x that day's close for every side: for GRANT/EXERCISE/TAX_WITHHOLD
      it is market value, not money paid.
    - footnotes carry weighted-average price ranges and 10b5-1 plan adoption dates; they are
      ~1 KB a row, so keep limit modest (CSCO default = 49 rows, 53 KB).

    An empty result means no matching trades by senior insiders in 6 months, or an unknown
    symbol (never a 404): `note` is set only then. A 422 means a bad symbol, side, role or
    limit. A 401 means the sign-in expired (your MCP client re-runs OAuth). Requires auth
    (your MCP client attaches the OAuth bearer automatically). Rate-limited 100/min per
    user; counts toward plan quota.
    """
    symbol = symbol.strip().upper()
    body: dict[str, Any] = {"symbol": symbol, "side": side, "limit": limit}
    if role:
        body["role"] = role
    rows = _list(
        await _send(ctx, "POST", "/insider-transactions", json=body),
        "/insider-transactions",
        "check symbol, side (BUY, SELL, GRANT, EXERCISE, TAX_WITHHOLD, GIFT_IN, GIFT_OUT, OTHER), role (ceo, "
        "director, founder, ten_pct_owner) and limit.",
    )
    note = None
    if not rows:
        who = f" by a {'/'.join(role)}" if role else ""
        note = (f"No {'/'.join(side)} transactions{who} at {symbol} by senior insiders in the last 6 months. Widen "
                "`side` (grants, exercises and tax withholding are left out by default) or drop `role`. "
                f"{_not_covered(symbol)}")
    return InsiderTransactionsResult(symbol=symbol, side=side, role=role, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="Get Insider Activity Summary", readOnlyHint=True))
async def get_insider_activity_summary(
    ctx: Context,
    symbol: Symbol,
    limit: Annotated[int, Field(description="Quarters to return (1-36, default 8).", ge=1, le=36)] = 8,
) -> InsiderActivitySummaryResult:
    """One company's insider buying and selling rolled up by quarter and by insider — counts, shares, dollar value, net, net-buy ratio, CEO/director activity, grants. The direct read for "are insiders buying or selling X", "who is selling".

    FLOW: get_insider_transactions for the individual trades behind a quarter (pass `role`
    to narrow to the CEO or directors).

    Returns {"symbol", "quarters", "insiders", "note"}.
    - quarters: [{quarter, buy_count, sell_count, buy_shares, sell_shares, buy_value,
      sell_value, net_value, net_buy_ratio, n_insiders, n_buyers, n_sellers, ceo_bought,
      ceo_sold, dir_buy_count, dir_sell_count, grant_shares, grant_count, first_txn_date,
      last_txn_date}], newest quarter first.
    - insiders: [{insider_name, position, is_ceo, is_director, is_founder, is_ten_pct_owner,
      activity, buy_count, sell_count, buy_shares, sell_shares, buy_value, sell_value,
      net_value, net_buy_ratio, grant_shares, first_txn_date, last_txn_date}].
      insiders covers the last 6 months, largest net dollar value first.

    READING THE NUMBERS: Buy/sell/net are open-market trades only, valued at market close;
    net_buy_ratio runs -1 (all selling) to +1 (all buying). Grants are counted separately
    (grant_shares, grant_count) and are not in buy/sell/net.

    An unknown symbol returns empty lists (never a 404): `note` is set only when a list is
    empty. A 422 means a bad symbol or limit. A 401 means the sign-in expired (your MCP
    client re-runs OAuth). Requires auth (your MCP client attaches the OAuth bearer
    automatically). Rate-limited 100/min per user; counts toward plan quota.
    """
    symbol = symbol.strip().upper()
    data = _dict(
        await _send(ctx, "POST", "/insider-activity-summary", json={"symbol": symbol, "limit": limit}),
        "/insider-activity-summary", _SYMBOL_HINT,
    )
    data = {**data, "symbol": data.get("symbol") or symbol,
            "quarters": data.get("quarters") or [], "insiders": data.get("insiders") or []}
    if not data["quarters"] and not data["insiders"]:
        data["note"] = f"No insider activity on file for {symbol}. {_not_covered(symbol)}"
    elif not data["insiders"]:
        data["note"] = (f"No senior-insider activity at {symbol} in the last 6 months, so `insiders` is empty; "
                        "`quarters` reaches further back.")
    return InsiderActivitySummaryResult(**data)
