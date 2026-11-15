"""Output schemas for tools/company_data_updates.py.

Row fields are Optional because the SDK rejects any successful result that fails its
schema. Unlisted backend keys pass through via extra="allow".
"""

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# int stays int (share counts, analyst counts), rather than coming back as 1234.0.
Number = Union[int, float]


# ── get_earnings_history ─────────────────────────────────────────────────────

class EarningsHistoryRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: Optional[str] = None
    date: Optional[str] = Field(None, description="Report date (YYYY-MM-DD).")
    status: Optional[str] = Field(None, description="upcoming | reported. At most one upcoming row, on top.")
    timing: Optional[str] = Field(None, description="before_open | after_close, or null when not known.")
    fiscal_year: Optional[Union[int, str]] = None
    fiscal_period: Optional[str] = None
    period_ending: Optional[str] = Field(None, description="Fiscal quarter end (YYYY-MM-DD).")
    eps_actual: Optional[Number] = Field(None, description="ADJUSTED (street) EPS, not the GAAP EPS in company_financials.")
    eps_estimated: Optional[Number] = Field(None, description="Consensus adjusted EPS before the report.")
    eps_surprise: Optional[Number] = Field(None, description="A FRACTION: 0.05 = +5% vs the estimate.")
    revenue_actual: Optional[Number] = None
    revenue_estimated: Optional[Number] = None
    revenue_surprise: Optional[Number] = Field(None, description="A FRACTION: 0.05 = +5% vs the estimate.")
    beat_flag: Optional[str] = Field(None, description="beat | miss | in_line.")
    eps_yoy_change: Optional[Number] = Field(None, description="EPS vs the year-ago quarter as a FRACTION: 0.05 = +5%.")


class EarningsHistoryResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    limit: int = Field(description="REPORTED quarters requested; the next scheduled report can add one row on top.")
    count: int = Field(description="Rows returned, including any upcoming row.")
    rows: list[EarningsHistoryRow] = Field(description="Newest first.")
    note: Optional[str] = Field(None, description="Set only when no rows came back: what an empty result can mean.")


# ── get_analyst_estimates ────────────────────────────────────────────────────

class AnalystEstimateRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: Optional[str] = None
    period_end: Optional[str] = Field(
        None, description="Fiscal quarter end (YYYY-MM-DD). Can differ by a day from company_financials' date: match to the nearest."
    )
    period_ended: Optional[bool] = Field(
        None, description="True once the quarter has ended, which can be before it is reported."
    )
    eps_avg: Optional[Number] = Field(None, description="Consensus ADJUSTED EPS, not GAAP.")
    eps_low: Optional[Number] = Field(None, description="Null when est_range_is_modeled.")
    eps_high: Optional[Number] = Field(None, description="Null when est_range_is_modeled.")
    revenue_avg: Optional[Number] = None
    revenue_low: Optional[Number] = Field(None, description="Null when est_range_is_modeled.")
    revenue_high: Optional[Number] = Field(None, description="Null when est_range_is_modeled.")
    ebitda_avg: Optional[Number] = None
    net_income_avg: Optional[Number] = None
    num_analysts_eps: Optional[Number] = None
    num_analysts_revenue: Optional[Number] = None
    eps_yoy_growth: Optional[Number] = Field(
        None, description="Consensus vs the year-ago quarter's consensus, as a FRACTION: 0.05 = +5%."
    )
    revenue_yoy_growth: Optional[Number] = Field(
        None, description="Consensus vs the year-ago quarter's consensus, as a FRACTION: 0.05 = +5%."
    )
    est_range_is_modeled: Optional[bool] = Field(
        None, description="True = the vendor's low/high band is synthetic, so the low/high columns are null."
    )


class AnalystEstimatesResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    limit: int = Field(description="Quarters requested, counted back from the furthest of the next 4 quarters.")
    count: int = Field(description="Rows returned, one per quarter.")
    rows: list[AnalystEstimateRow] = Field(description="Newest period first.")
    note: Optional[str] = Field(None, description="Set only when no rows came back: what an empty result can mean.")


# ── get_analyst_rating_actions ───────────────────────────────────────────────

class RatingActionRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: Optional[str] = None
    rating_company: Optional[str] = Field(None, description="The broker / research firm.")
    prev_rating: Optional[str] = Field(None, description="In the firm's own words.")
    new_rating: Optional[str] = Field(None, description="In the firm's own words (Overweight, Buy, Outperform...).")
    action: Optional[str] = Field(None, description="upgrade | downgrade | maintain.")
    news_title: Optional[str] = Field(None, description="Null for most actions: only each firm's latest sourced article is on file.")
    publisher: Optional[str] = None
    news_url: Optional[str] = None


class RatingActionsResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    count: int = Field(description="Actions returned.")
    rows: list[RatingActionRow] = Field(description="Newest first.")
    note: Optional[str] = Field(None, description="Set only when no rows came back: what an empty result can mean.")


# ── get_analyst_rating_summary ───────────────────────────────────────────────

class RatingAnalysis(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: Optional[str] = Field(None, description="When the analysis was written; can be months old.")
    consensus_rating: Optional[str] = None
    summary: Optional[str] = None


class RatingFirmRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    rating_company: Optional[str] = None
    rating: Optional[str] = Field(None, description="The firm's current rating, in its own words.")
    prev_rating: Optional[str] = None
    action: Optional[str] = Field(None, description="upgrade | downgrade | maintain.")
    date: Optional[str] = Field(None, description="The firm's latest action.")


class RatingSummaryResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    consensus_rating: Optional[str] = None
    firm_count: Optional[int] = Field(
        None, description="Every firm that acted in the year up to the latest action, even when limit cuts `firms`."
    )
    analysis: Optional[RatingAnalysis] = Field(None, description="Null when there is none. Report its date: it can be months old.")
    firms: list[RatingFirmRow] = Field(default_factory=list, description="Most recently active first.")
    note: Optional[str] = Field(None, description="Set only when nothing came back: what an empty result can mean.")


# ── get_insider_transactions ─────────────────────────────────────────────────

class InsiderTransactionRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    transaction_date: Optional[str] = None
    filing_date: Optional[str] = None
    insider_name: Optional[str] = None
    position: Optional[str] = None
    is_ceo: Optional[bool] = None
    is_director: Optional[bool] = None
    is_founder: Optional[bool] = None
    is_ten_pct_owner: Optional[bool] = None
    side: Optional[str] = Field(None, description="BUY | SELL | GRANT | EXERCISE | TAX_WITHHOLD | GIFT_IN | GIFT_OUT | OTHER.")
    transaction_code: Optional[str] = Field(None, description="The Form 4 transaction code.")
    shares: Optional[Number] = None
    filing_price: Optional[Number] = Field(None, description="The price on the filing (may be a weighted average: see footnotes).")
    close_price: Optional[Number] = Field(None, description="That day's market close.")
    mkt_value: Optional[Number] = Field(
        None, description="shares x close_price for every side: for GRANT/EXERCISE/TAX_WITHHOLD it is market value, not money paid."
    )
    shares_owned_after: Optional[Number] = None
    is_10b5_1: Optional[bool] = Field(None, description="True = traded under a 10b5-1 plan.")
    direct_indirect: Optional[str] = None
    security: Optional[str] = None
    accession: Optional[str] = None
    sec_url: Optional[str] = Field(None, description="The SEC filing.")
    footnotes: Optional[Any] = Field(
        None, description="Weighted-average price ranges and 10b5-1 plan adoption dates; about 1 KB a row."
    )


class InsiderTransactionsResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    side: list[str] = Field(description="The sides requested.")
    role: Optional[list[str]] = Field(None, description="The roles requested (ANY of), or null for every senior insider.")
    count: int = Field(description="Transactions returned.")
    rows: list[InsiderTransactionRow] = Field(description="Newest first.")
    note: Optional[str] = Field(None, description="Set only when no rows came back: what an empty result can mean.")


# ── get_insider_activity_summary ─────────────────────────────────────────────

class InsiderQuarterRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    quarter: Optional[str] = None
    buy_count: Optional[Number] = None
    sell_count: Optional[Number] = None
    buy_shares: Optional[Number] = None
    sell_shares: Optional[Number] = None
    buy_value: Optional[Number] = Field(None, description="Open-market buys, valued at market close.")
    sell_value: Optional[Number] = Field(None, description="Open-market sells, valued at market close.")
    net_value: Optional[Number] = Field(None, description="buy_value minus sell_value.")
    net_buy_ratio: Optional[Number] = Field(None, description="-1 (all selling) to +1 (all buying).")
    n_insiders: Optional[Number] = None
    n_buyers: Optional[Number] = None
    n_sellers: Optional[Number] = None
    ceo_bought: Optional[bool] = None
    ceo_sold: Optional[bool] = None
    dir_buy_count: Optional[Number] = None
    dir_sell_count: Optional[Number] = None
    grant_shares: Optional[Number] = None
    grant_count: Optional[Number] = None
    first_txn_date: Optional[str] = None
    last_txn_date: Optional[str] = None


class InsiderRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    insider_name: Optional[str] = None
    position: Optional[str] = None
    is_ceo: Optional[bool] = None
    is_director: Optional[bool] = None
    is_founder: Optional[bool] = None
    is_ten_pct_owner: Optional[bool] = None
    activity: Optional[str] = None
    buy_count: Optional[Number] = None
    sell_count: Optional[Number] = None
    buy_shares: Optional[Number] = None
    sell_shares: Optional[Number] = None
    buy_value: Optional[Number] = None
    sell_value: Optional[Number] = None
    net_value: Optional[Number] = None
    net_buy_ratio: Optional[Number] = Field(None, description="-1 (all selling) to +1 (all buying).")
    grant_shares: Optional[Number] = None
    first_txn_date: Optional[str] = None
    last_txn_date: Optional[str] = None


class InsiderActivitySummaryResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    quarters: list[InsiderQuarterRow] = Field(default_factory=list, description="Newest quarter first.")
    insiders: list[InsiderRow] = Field(
        default_factory=list, description="The last 6 months, largest net dollar value first."
    )
    note: Optional[str] = Field(None, description="Set only when a list came back empty: what that can mean.")
