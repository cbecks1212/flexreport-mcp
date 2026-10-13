"""Strategy tools: screening, portfolio optimization, stock picks, strategy track records."""

from typing import Annotated, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok
from schemas.strategies import (
    OptimizeAccepted,
    OptimizeResult,
    StockPicksResult,
    StrategyPerformanceSummaryResult,
    StrategySwapsResult,
    StrategyTrackRecordResult,
    TaskAccepted,
)

RiskTolerance = Annotated[
    Optional[Literal["conservative", "balanced", "aggressive"]],
    Field(description="Which risk profile is marked as recommended; omit for the optimizer default."),
]


@mcp.tool(annotations=ToolAnnotations(title="Screen Stocks", readOnlyHint=False, destructiveHint=False))
async def screen_stocks(
    ctx: Context,
    metrics: Annotated[
        Optional[dict[str, bool | float]],
        Field(description="Financial-growth criteria: metric name -> threshold (number) or required flag "
                          "(bool), e.g. {\"revenue_growth\": 0.1, \"positive_eps\": true}. Interpreted "
                          "by the screener, not enum-validated."),
    ] = None,
    sectors: Annotated[Optional[list[str]], Field(description="Sector values from list_options(\"sectors\").")] = None,
    sub_sectors: Annotated[
        Optional[list[str]], Field(description="Sub-industry values from list_sub_industries([...]).")
    ] = None,
    market_cap: Annotated[
        Optional[list[str]],
        Field(description="Market-cap buckets from list_options(\"market_cap\"), e.g. \"Small-cap\", \"Large-cap\"."),
    ] = None,
    analyst_ratings: Annotated[
        Optional[list[str]], Field(description="Analyst consensus ratings to keep, e.g. [\"Buy\", \"Strong Buy\"].")
    ] = None,
    institutional_ownership: Annotated[
        Optional[dict[str, float]],
        Field(description="Investor category -> minimum ownership, keyed by list_options(\"institutional_investor_types\") "
                          "values, e.g. {\"Hedge Fund\": 5}."),
    ] = None,
    countries: Annotated[Optional[list[str]], Field(description="Country values from list_options(\"countries\").")] = None,
    price_performance: Annotated[
        Optional[dict[str, float]],
        Field(description="Price-return criteria: window -> minimum return, e.g. {\"ytd\": 0.1}."),
    ] = None,
) -> TaskAccepted:
    """Screen stocks by financial growth, sector, sub-industry, market cap, analyst ratings, institutional ownership, country, and price performance.

    Discover valid filter values with the list tools named in each parameter's
    description before guessing.

    Rate-limited to 10/hour
    per user server-side.

    Asynchronous: returns a task id. Poll it with `get_task_status` until SUCCESS.
    """
    return _ok(await _send(
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
    ))

@mcp.tool(annotations=ToolAnnotations(title="Optimize Portfolio (Fast)", readOnlyHint=True))
async def optimize_portfolio_default(
    ctx: Context,
    symbols: Annotated[
        list[str],
        Field(description="Tickers to optimize, e.g. [\"AAPL\", \"MSFT\", \"JNJ\"] (see list_options(\"tickers\")).",
              min_length=1),
    ],
    risk_tolerance: RiskTolerance = None,
) -> OptimizeResult:
    """Build a risk-optimized portfolio from a list of tickers — fast, synchronous, no LLM.

    THE DEFAULT optimizer route. Returns the result directly and almost instantly
    (no task id, no polling). Reach for `optimize_portfolio` instead only when you
    explicitly want the slower LLM-curated variant layered on top of the optimizers.

    Symbols are validated against the covered universe; unsupported tickers come back
    in `missing`, and in-universe tickers with too little price history come back in
    `dropped`.

    No auth required (public endpoint).

    Synchronous. Returns:
      {"status": "OK",  # or "SKIPPED"/"EMPTY" when too few usable symbols remain
       "holdings": [{"symbol", "source_strategies", "summary", "conviction_level"}, ...],
       "optimizer_results": {"mvo": {...weights+diagnostics}, "hrp": {...}, "mcvar": {...}},
       "risk_profiles": {"profiles": [...], "recommended": "..."},  # present only when status == "OK"
       "dropped": [...],   # in-universe but insufficient price history
       "missing": [...]}   # not in the covered universe
    """
    return _ok(await _send(
        ctx, "POST", "/optimize-symbols-non-llm",
        json={
            "symbols": symbols,
            "risk_tolerance": risk_tolerance,
        },
        require_auth=False,
    ))


@mcp.tool(annotations=ToolAnnotations(title="Optimize Portfolio (LLM-Curated)", readOnlyHint=False, destructiveHint=False))
async def optimize_portfolio(
    ctx: Context,
    symbols: Annotated[
        list[str], Field(description="Tickers to score and optimize, e.g. [\"AAPL\", \"MSFT\", \"JNJ\"].", min_length=1)
    ],
    risk_tolerance: RiskTolerance = None,
    delivery: Annotated[
        Optional[str],
        Field(description="Result channel: \"dashboard\" (returned via get_task_status) or \"email\"."),
    ] = "dashboard",
) -> OptimizeAccepted:
    """Build a risk-optimized portfolio from a non-empty list of tickers.

    Scores `symbols` with the multi-signal scorer, LLM-curates, and risk-optimizes.

    Rate-limited to 10/hour
    per user server-side.

    Asynchronous: returns {"task_id": ..., "status": "PENDING", "supported": [...],
    "not_supported": [...]}. Poll `task_id` with `get_task_status` until SUCCESS.
    """
    return _ok(await _send(
        ctx, "POST", "/optimize-symbols",
        json={
            "symbols": symbols,
            "risk_tolerance": risk_tolerance,
            "delivery": delivery,
        },
    ))


@mcp.tool(annotations=ToolAnnotations(title="Get Stock Picks", readOnlyHint=True))
async def get_stock_picks(
    ctx: Context,
    strategy_name: Annotated[
        Optional[str],
        Field(description="Narrow to one strategy (e.g. \"momentum\", \"multi_signal\", \"fundamentals_smid\", "
                          "\"value_quality\"); omit for picks across all strategies."),
    ] = None,
) -> StockPicksResult:
    """Fetch the latest LLM-selected stock picks (current rebalance holdings).

    Returns `picks`, the holdings selected for the most recent rebalance date — the names
    the backend's strategies are currently positioned in. Each pick is a record (ticker,
    strategy, weight, rebalance date, and related fields; values are strings).
    Synchronous and read-only — no auth required.

    Each holding also carries a CURRENT-BASKET YTD view: `ytd_return` (that stock's
    Jan 1 -> now price return) and the strategy-level weighted `basket_ytd_return`.
    For how the strategies have ACTUALLY performed since inception (the realized track
    record of the book as held), use `get_strategy_performance_summary` /
    `get_strategy_track_record` instead.
    """
    params = {"strategy_name": strategy_name} if strategy_name else None
    picks = _ok(await _send(
        ctx, "GET", "/get-stock-picks", params=params, require_auth=False
    ))
    return StockPicksResult(picks=picks)


# The four strategy books plus the pooled optimized book — the valid `strategy_name`
# values for the track-record / swap-ledger tools (backend rejects anything else).
StrategyBook = Literal[
    "momentum", "multi_signal", "fundamentals_smid", "value_quality", "pooled"
]


@mcp.tool(annotations=ToolAnnotations(title="Get Strategy Performance Summary", readOnlyHint=True))
async def get_strategy_performance_summary(
    ctx: Context,
    amount: Annotated[
        float,
        Field(description="Dollar amount assumed invested at inception; scales growth_strategy / growth_sp."),
    ] = 1.0,
) -> StrategyPerformanceSummaryResult:
    """Leaderboard of since-inception performance vs the S&P 500 for all strategy books.

    THE first stop for "how are the stock picks / strategies performing?". Returns
    `strategies`, one headline row per book — the four strategies plus the pooled optimized book — sorted
    by excess return over the S&P 500: {strategy_name, display_name, inception_date,
    since_inception_return, sp_since_inception, excess_since_inception,
    annualized_return, max_drawdown, sharpe, sp_sharpe, ytd_return, growth_strategy,
    growth_sp, days_tracked}.

    growth_strategy / growth_sp = what `amount` invested at inception would be worth in
    the strategy vs the S&P 500.

    Drill into one book's full daily series with `get_strategy_track_record`, or its
    per-trade adds/drops with `get_strategy_swaps`.
    """
    rows = _ok(await _send(
        ctx, "GET", "/get-strategy-performance-summary",
        params={"amount": amount},
    ))
    return StrategyPerformanceSummaryResult(strategies=rows)


@mcp.tool(annotations=ToolAnnotations(title="Get Strategy Track Record", readOnlyHint=True))
async def get_strategy_track_record(
    ctx: Context,
    strategy_name: Annotated[
        StrategyBook, Field(description="One of the four strategies, or \"pooled\" (the pooled optimized book).")
    ],
    book: Annotated[
        Literal["llm", "mechanical"],
        Field(description="\"llm\" (LLM-curated selections) or \"mechanical\" (the raw quantitative screen, "
                          "no LLM overlay)."),
    ] = "llm",
    amount: Annotated[
        float, Field(description="Dollar amount assumed invested at inception; scales the growth-of-$ fields.")
    ] = 1.0,
) -> StrategyTrackRecordResult:
    """Since-inception daily track record vs the S&P 500 for ONE strategy book.

    Returns the realized performance of the book as actually held — chartable series
    plus headline stats: {strategy_name, book, inception_date, amount_invested,
    summary: {since_inception_return, sp_since_inception, excess_since_inception,
    annualized_return, max_drawdown, sharpe, sp_sharpe, ytd_return, growth_strategy,
    growth_sp, days_tracked, last_date},
    series: [{date, strategy_cumulative, benchmark_cumulative, excess, strategy_ytd,
    growth_strategy, growth_sp, sharpe_expanding, holdings_count}, ...]}.

    The Sharpe series is expanding-window and annualized (rf=0).

    Backend returns 404 when the chosen book has no performance history yet. For the
    cross-book leaderboard use `get_strategy_performance_summary`; for the current
    holdings themselves use `get_stock_picks`.
    """
    return _ok(await _send(
        ctx, "GET", "/get-strategy-track-record",
        params={"strategy_name": strategy_name, "book": book, "amount": amount},
    ))


@mcp.tool(annotations=ToolAnnotations(title="Get Strategy Swap Ledger", readOnlyHint=True))
async def get_strategy_swaps(
    ctx: Context,
    strategy_name: Annotated[
        StrategyBook, Field(description="One of the four strategies, or \"pooled\" (the pooled optimized book).")
    ],
) -> StrategySwapsResult:
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

    For the book-level return series use `get_strategy_track_record`.
    """
    return _ok(await _send(
        ctx, "GET", "/get-strategy-swaps",
        params={"strategy_name": strategy_name},
    ))
