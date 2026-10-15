"""Output schemas for tools/market_data.py.

Each model is a tool's return type, so FastMCP advertises it as the tool's
`outputSchema` and validates every result against it. Row models use
extra="allow" so a new backend column passes through instead of failing
validation; fields the backend can omit or null are Optional.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class TechnicalIndicatorRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str = Field(description="Trading day, YYYY-MM-DD.")
    symbol: str = Field(description="Ticker the row belongs to.")
    close: Optional[float] = Field(None, description="Daily close on that day.")


class TechnicalIndicatorResult(BaseModel):
    rows: list[TechnicalIndicatorRow] = Field(
        description="One row per trading day, ascending by date. Beyond date/symbol/close, "
                    "columns are indicator-specific (e.g. rsi carries `rsi`, `overbought`, "
                    "`oversold`). Empty means no data in the range."
    )


class IntradayPoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str = Field(description="Bar time, ET wall-clock (\"2026-10-13 09:35:00\").")
    close: float = Field(description="Bar close.")
    cum_log_return: Optional[float] = Field(None, description="Log return from the prior daily close to this bar.")
    zscore: Optional[float] = Field(None, description="cum_log_return z-scored against the trailing 20-day daily-return distribution.")
    prior_zscore: Optional[float] = Field(None, description="Previous bar's z-score (outliers only).")
    zscore_change: Optional[float] = Field(None, description="Bar-over-bar z-score change, the size of the jump (outliers only).")


class IntradayExtreme(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str
    close: float
    zscore: float
    direction: Optional[Literal["up", "down"]] = Field(None, description="Sign of the most extreme z-score.")


class IntradayOutlierResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    zscore_threshold: float = Field(description="The |z| cutoff applied.")
    is_outlier_now: bool = Field(description="True when the latest bar is beyond the threshold.")
    prior_close: Optional[float] = Field(None, description="Prior session's close, the return baseline.")
    daily_mean_20d: Optional[float] = Field(None, description="Mean of the trailing 20 daily log returns.")
    daily_std_20d: Optional[float] = Field(None, description="Std dev of the trailing 20 daily log returns (the sigma).")
    latest: Optional[IntradayPoint] = Field(None, description="The most recent bar.")
    most_extreme: Optional[IntradayExtreme] = Field(None, description="Today's bar with the largest |z-score|.")
    outliers: list[IntradayPoint] = Field(description="Bars with |z-score| >= zscore_threshold.")
    series: list[IntradayPoint] = Field(description="Every bar today, ascending by time.")


class AftermarketBar(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str = Field(description="Ticker the bar belongs to.")
    date: str = Field(description="Bar start, ET wall-clock ISO-8601 (\"2026-10-09T16:15:00\").")
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = Field(None, description="Shares traded in the 15-minute bar.")
    last_updated: Optional[str] = Field(None, description="When the bar was captured (UTC).")


class AftermarketResult(BaseModel):
    bars: list[AftermarketBar] = Field(
        description="One row per symbol per 15-min bar, 16:00-20:00 ET, ordered by symbol then "
                    "time DESC (first row per symbol is the latest). Empty means no bars were "
                    "captured in the window."
    )


class EarningsMovePrediction(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    earnings_date: Optional[str] = Field(None, description="Next report date (today or later), YYYY-MM-DD.")
    expected_move: Optional[float] = Field(
        None, description="Expected absolute log move of the reaction session (0.05 is about 5%); size only, no direction."
    )
    hist_move_mean: Optional[float] = Field(None, description="The stock's own average absolute reaction over past reports.")
    hist_move_n: Optional[int] = Field(None, description="Number of past reports behind hist_move_mean.")


class EarningsMoveResult(BaseModel):
    predictions: list[EarningsMovePrediction] = Field(description="One prediction per covered symbol with a scheduled report.")
    not_supported_symbols: list[str] = Field(description="Requested tickers outside Flexreport coverage.")
    no_upcoming_earnings_symbols: list[str] = Field(description="Covered tickers with no scheduled report.")


class EarningsAnnouncement(BaseModel):
    """Values are strings as the backend sends them; "nan" means not available yet."""

    model_config = ConfigDict(extra="allow")

    symbol: str
    date: str = Field(description="Announcement date, YYYY-MM-DD.")
    epsActual: Optional[str] = Field(None, description="Reported EPS, or \"nan\" before the report.")
    epsEstimated: Optional[str] = Field(None, description="Consensus EPS, or \"nan\".")
    revenueActual: Optional[str] = Field(None, description="Reported revenue, or \"nan\" before the report.")
    revenueEstimated: Optional[str] = Field(None, description="Consensus revenue, or \"nan\".")
    lastUpdated: Optional[str] = None


class EarningsAnnouncementsResult(BaseModel):
    announcements: list[EarningsAnnouncement] = Field(description="Scheduled reports in the window; empty when nothing matches.")


class MarketOpenResult(BaseModel):
    is_open: bool = Field(description="True when the exchange is in its regular session right now.")
