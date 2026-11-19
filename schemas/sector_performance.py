"""Output schemas for tools/sector_performance.py.

Row fields are Optional because the SDK rejects any successful result that fails its
schema. Unlisted backend keys pass through via extra="allow".
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

_MEDIAN = "Median constituent price return over {w}, in PERCENT (1.25 = +1.25%)."
_MCAP = "Market-cap-weighted price return over {w}, in PERCENT (1.25 = +1.25%)."
_VOLUME = "Share volume over the {w} window."
_VOLUME_PCT = "Change in {w} share volume vs the prior {w} window, in PERCENT (12.5 = +12.5%)."


class SectorPricePerformanceRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    sector: Optional[str] = None
    universe: Optional[str] = Field(None, description="The set of companies the row covers (follows the sp_index filter).")
    as_of: Optional[str] = Field(None, description="Date the returns are measured to.")
    n_symbols: Optional[int] = Field(None, description="Companies in the sector and universe behind the row.")
    median_return_1d: Optional[float] = Field(None, description=_MEDIAN.format(w="1 day"))
    median_return_1w: Optional[float] = Field(None, description=_MEDIAN.format(w="1 week"))
    median_return_1m: Optional[float] = Field(None, description=_MEDIAN.format(w="1 month"))
    median_return_3m: Optional[float] = Field(None, description=_MEDIAN.format(w="3 months"))
    median_return_6m: Optional[float] = Field(None, description=_MEDIAN.format(w="6 months"))
    median_return_12m: Optional[float] = Field(None, description=_MEDIAN.format(w="12 months"))
    median_return_ytd: Optional[float] = Field(None, description=_MEDIAN.format(w="the year to date"))
    mcap_weighted_return_1d: Optional[float] = Field(None, description=_MCAP.format(w="1 day"))
    mcap_weighted_return_1w: Optional[float] = Field(None, description=_MCAP.format(w="1 week"))
    mcap_weighted_return_1m: Optional[float] = Field(None, description=_MCAP.format(w="1 month"))
    mcap_weighted_return_3m: Optional[float] = Field(None, description=_MCAP.format(w="3 months"))
    mcap_weighted_return_6m: Optional[float] = Field(None, description=_MCAP.format(w="6 months"))
    mcap_weighted_return_12m: Optional[float] = Field(None, description=_MCAP.format(w="12 months"))
    mcap_weighted_return_ytd: Optional[float] = Field(None, description=_MCAP.format(w="the year to date"))


class SectorPricePerformanceResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    filters: dict[str, Any] = Field(description="The request body sent to the backend (unset filters omitted).")
    count: int = Field(description="Rows returned, one per sector.")
    rows: list[SectorPricePerformanceRow]
    note: Optional[str] = Field(None, description="Set only when no rows came back: an empty result is a valid answer.")


class SectorVolumePerformanceRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: Optional[str] = None
    sector: Optional[str] = None
    marketcap_type: Optional[str] = Field(None, description="Micro-cap | Small-cap | Medium-cap | Large-cap | Mega-cap.")
    one_week: Optional[float] = Field(None, description=_VOLUME.format(w="one-week"))
    one_month: Optional[float] = Field(None, description=_VOLUME.format(w="one-month"))
    three_months: Optional[float] = Field(None, description=_VOLUME.format(w="three-month"))
    six_months: Optional[float] = Field(None, description=_VOLUME.format(w="six-month"))
    one_year: Optional[float] = Field(None, description=_VOLUME.format(w="one-year"))
    one_week_pct: Optional[float] = Field(None, description=_VOLUME_PCT.format(w="one-week"))
    one_month_pct: Optional[float] = Field(None, description=_VOLUME_PCT.format(w="one-month"))
    three_months_pct: Optional[float] = Field(None, description=_VOLUME_PCT.format(w="three-month"))
    six_months_pct: Optional[float] = Field(None, description=_VOLUME_PCT.format(w="six-month"))
    one_year_pct: Optional[float] = Field(None, description=_VOLUME_PCT.format(w="one-year"))


class SectorVolumePerformanceResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    filters: dict[str, Any] = Field(description="The request body sent to the backend (unset filters omitted).")
    count: int = Field(description="Rows returned, one per (date, sector, marketcap_type).")
    rows: list[SectorVolumePerformanceRow]
    note: Optional[str] = Field(None, description="Set only when no rows came back: an empty result is a valid answer.")
