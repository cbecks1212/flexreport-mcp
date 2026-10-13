"""Output schemas for tools/strategies.py.

Each model is a tool's return type, so FastMCP advertises it as the tool's
`outputSchema` and validates every result against it. Models carrying backend
data use extra="allow" so a new backend field passes through instead of failing
validation; fields the backend can omit or null are Optional.
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class TaskAccepted(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str = Field(description="Poll with get_task_status until SUCCESS.")
    status: str = Field(description="PENDING on submission.")


class OptimizeAccepted(TaskAccepted):
    supported: list[str] = Field(default_factory=list, description="Tickers that will be scored and optimized.")
    not_supported: list[str] = Field(default_factory=list, description="Tickers outside Flexreport coverage, ignored.")


class OptimizedHolding(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    source_strategies: Optional[str] = Field(None, description="Risk tolerance the pick was tagged with, or \"user_specified\".")
    summary: Optional[str] = Field(None, description="Company summary from the snapshot.")
    conviction_level: Optional[str] = Field(None, description="Snapshot recommendation (e.g. HIGH/MEDIUM/LOW).")


class OptimizeResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = Field(description="OK, or SKIPPED / EMPTY when too few usable symbols remain.")
    reason: Optional[str] = Field(None, description="Why the run was SKIPPED.")
    holdings: list[OptimizedHolding] = Field(default_factory=list, description="The usable holdings that were optimized.")
    optimizer_results: dict[str, Any] = Field(
        default_factory=dict, description="Per optimizer (mvo, hrp, mcvar): weights plus diagnostics. Empty unless OK."
    )
    risk_profiles: Optional[dict[str, Any]] = Field(
        None, description="{profiles: [{risk_profile, method, description, diagnostics, allocation}], recommended}; "
                          "present only when status is OK."
    )
    dropped: list[Any] = Field(default_factory=list, description="In-universe tickers with insufficient price history.")
    missing: list[str] = Field(default_factory=list, description="Tickers not in the covered universe.")
    lookback_trading_days: Optional[int] = None
    max_weight: Optional[float] = Field(None, description="Per-name weight cap the optimizers applied.")


class StockPick(BaseModel):
    """Values are strings as the backend sends them."""

    model_config = ConfigDict(extra="allow")

    symbol: str
    strategy_name: Optional[str] = None
    rebalance_date: Optional[str] = None
    weight: Optional[str] = Field(None, description="Weight in the strategy basket.")
    ytd_return: Optional[str] = Field(None, description="The stock's Jan 1 -> now price return.")
    basket_ytd_return: Optional[str] = Field(None, description="Weighted YTD return of the strategy's current basket.")


class StockPicksResult(BaseModel):
    picks: list[StockPick] = Field(description="Current holdings, each strategy at its own latest rebalance.")


class StrategyStats(BaseModel):
    """Headline since-inception stats; returns are decimals (0.12 = 12%)."""

    model_config = ConfigDict(extra="allow")

    since_inception_return: Optional[float] = None
    sp_since_inception: Optional[float] = Field(None, description="S&P 500 return over the same window.")
    excess_since_inception: Optional[float] = Field(None, description="Strategy minus S&P 500.")
    annualized_return: Optional[float] = None
    max_drawdown: Optional[float] = None
    sharpe: Optional[float] = Field(None, description="Annualized, rf=0.")
    sp_sharpe: Optional[float] = None
    ytd_return: Optional[float] = None
    growth_strategy: Optional[float] = Field(None, description="What `amount` invested at inception is worth now.")
    growth_sp: Optional[float] = Field(None, description="The same `amount` in the S&P 500.")
    days_tracked: Optional[int] = None


class StrategyLeaderboardRow(StrategyStats):
    strategy_name: str
    display_name: Optional[str] = None
    book: Optional[str] = Field(None, description="Which book the row measures (mechanical or llm).")
    inception_date: Optional[str] = None


class StrategyPerformanceSummaryResult(BaseModel):
    strategies: list[StrategyLeaderboardRow] = Field(
        description="One row per book with history, sorted by excess_since_inception (best first)."
    )


class TrackRecordSummary(StrategyStats):
    last_date: Optional[str] = Field(None, description="Last day in the series, YYYY-MM-DD.")


class TrackRecordPoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str
    strategy_cumulative: Optional[float] = None
    benchmark_cumulative: Optional[float] = Field(None, description="S&P 500 cumulative return from inception.")
    excess: Optional[float] = None
    strategy_ytd: Optional[float] = None
    growth_strategy: Optional[float] = None
    growth_sp: Optional[float] = None
    sharpe_expanding: Optional[float] = Field(None, description="Expanding-window annualized Sharpe (rf=0).")
    holdings_count: Optional[int] = None


class StrategyTrackRecordResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    strategy_name: str
    book: Optional[str] = None
    inception_date: Optional[str] = None
    amount_invested: Optional[float] = None
    summary: TrackRecordSummary
    series: list[TrackRecordPoint] = Field(description="Daily points from inception, ascending by date.")


class SwapTrade(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    action: str = Field(description="ADD (newly selected) or DROP (removed).")
    event_date: str
    window_end: Optional[str] = Field(None, description="Exit date for an ADD, or the latest close.")
    still_open: Optional[bool] = Field(None, description="True when an ADD is still held.")
    name_return: Optional[float] = Field(None, description="The name's return over the window (foregone, for a DROP).")
    sp_return: Optional[float] = None
    excess: Optional[float] = None


class SwapSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    n_adds: int = 0
    n_drops: int = 0
    avg_add_excess_vs_sp: Optional[float] = None
    avg_drop_foregone_excess_vs_sp: Optional[float] = None
    net_swap_value_add: Optional[float] = None


class StrategySwapsResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    strategy_name: str
    book: Optional[str] = None
    trades: list[SwapTrade] = Field(description="Adds and drops, newest first.")
    summary: SwapSummary
