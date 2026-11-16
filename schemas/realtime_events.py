"""Output schemas for tools/realtime_events.py.

Each model is a tool's return type, so FastMCP advertises it as the tool's
`outputSchema` and validates every result against it. Models use extra="allow"
so a new backend key passes through instead of failing validation. Event records
and ontology cards are polymorphic, so only their stable keys are typed; the rest
are open dicts. `situate` returns unstructured (its payload would be sent twice).
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── list_realtime_events ───────────────────────────────────────────────────
class RealtimeEvent(BaseModel):
    """Payload keys vary by event_type; only the common ones are typed."""

    model_config = ConfigDict(extra="allow")

    symbol: Optional[str] = Field(None, description="Ticker (absent on market-wide types such as news_evolution).")
    event_type: Optional[str] = None


class RealtimeEventsResult(BaseModel):
    events: list[RealtimeEvent] = Field(
        description="Cached events of the requested type. Empty means the 12h cache is cold for that type, "
                    "not that nothing happened."
    )


# ── get_company_snapshot ───────────────────────────────────────────────────
class _Block(BaseModel):
    model_config = ConfigDict(extra="allow")


class SnapshotCompanySummary(_Block):
    summary: Optional[str] = None
    core_investment_thesis: Optional[str] = None
    bull_case: Optional[str] = None
    bear_case: Optional[str] = None
    active_coverage: Optional[bool] = Field(None, description="False when no snapshot narrative exists for the symbol.")


class SnapshotFinancialOverview(_Block):
    variant_perception: Optional[Any] = None
    piotroski_score: Optional[float] = None
    valuation_signal: Optional[Any] = None


class SnapshotPriceOverview(_Block):
    summary: Optional[str] = None
    price_performance: Optional[dict[str, Any]] = Field(None, description="Returns over 1M / 3M / 6M / 12M.")
    technical_indicators: Optional[dict[str, Any]] = Field(None, description="Latest SMA/EMA/RSI/MACD/Bollinger readings and signals.")


class SnapshotRecentNews(_Block):
    articles: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Curated articles, rank ascending: rank, published_date, title, article_text, url, publisher, rationale.",
    )
    active_coverage: Optional[bool] = None


class SnapshotOwnership(_Block):
    summary: Optional[str] = None
    ownership_breakdown: list[dict[str, Any]] = Field(
        default_factory=list, description="Top holders from the latest 13F snapshot, by holder rank."
    )


class SnapshotGrades(_Block):
    summary: Optional[str] = None
    consensus_rating: Optional[Any] = Field(None, description="Street consensus rating, e.g. Buy.")
    analyst_ratings: list[dict[str, Any]] = Field(default_factory=list, description="Up to 20 most recent rating actions.")


class CompanySnapshotResult(_Block):
    company_summary: Optional[SnapshotCompanySummary] = Field(None, description="Thesis, bull and bear case.")
    financial_overview: Optional[SnapshotFinancialOverview] = None
    price_overview: Optional[SnapshotPriceOverview] = None
    price_targets: Optional[dict[str, Any]] = Field(
        None, description="Decommissioned pending a model revamp: values are null and active_coverage is false."
    )
    recent_news: Optional[SnapshotRecentNews] = None
    institutional_ownership: Optional[SnapshotOwnership] = None
    stock_grades_overview: Optional[SnapshotGrades] = None


# ── get_event_ontology ─────────────────────────────────────────────────────
class EventOntologyResult(BaseModel):
    """Cards format fills event_types/relations/episodes (plus families, tools); triples fills edges."""

    model_config = ConfigDict(extra="allow")

    as_of: Optional[str] = None
    realtime_cache_ttl_hours: Optional[int] = None
    degraded: Optional[bool] = Field(None, description="True when the ontology table is not built yet, not that the type is unknown.")
    event_types: Optional[dict[str, Any]] = Field(None, description="Event-type cards, keyed by type (cards format).")
    relations: Optional[dict[str, Any]] = Field(None, description="Relation cards, keyed by table/view (cards format).")
    episodes: Optional[dict[str, Any]] = None
    edges: Optional[list[dict[str, Any]]] = Field(None, description="Subject-predicate-object rows (triples format).")
