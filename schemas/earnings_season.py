"""Output schemas for tools/earnings_season.py.

Row fields are Optional because the SDK rejects any successful result that fails its
schema. Unlisted backend keys pass through via extra="allow".
"""

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class EarningsSeasonCohortRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: Optional[str] = None
    sector: Optional[str] = None
    sp_index: Optional[bool] = Field(None, description="True = S&P 500 member.")
    country: Optional[str] = Field(None, description="ISO-2 country code.")
    marketcap_type: Optional[str] = None
    date: Optional[str] = Field(None, description="Date of the company's latest earnings result this season.")
    eps_actual: Optional[float] = None
    eps_estimated: Optional[float] = None
    revenue_actual: Optional[float] = None
    revenue_estimated: Optional[float] = None
    beat_flag: Optional[str] = Field(None, description="beat | miss | in_line.")
    eps_yoy_change: Optional[float] = Field(
        None,
        description="EPS change vs the year-ago quarter as a FRACTION, not a percent: 0.12 = +12%. Multiply by 100 "
                    "before showing a %. Null when there is no prior-year actual.",
    )
    cohort: Optional[str] = Field(None, description="sp500 | beyond_sp500_us | other.")


class EarningsSeasonCohortResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    filters: dict[str, Any] = Field(description="The request body sent to the backend (unset filters omitted).")
    count: int = Field(description="Rows returned, one per symbol.")
    rows: list[EarningsSeasonCohortRow] = Field(description="Newest result first.")
    note: Optional[str] = Field(None, description="Set only when no rows came back: an empty result is a valid answer.")


class IrSeasonDocumentRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    document_url: Optional[str] = Field(
        None, description="The stored document: pass it to get_ir_document_content(url=...). Not the same as source_url."
    )
    symbol: Optional[str] = None
    company_name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None
    sp_index: Optional[bool] = None
    marketcap_type: Optional[str] = None
    in_coverage_universe: Optional[bool] = Field(
        None, description="False = the symbol is outside the coverage universe (other tools may have little on it)."
    )
    document_date: Optional[str] = Field(None, description="The date to rank recency on.")
    published_date: Optional[str] = Field(None, description="Mostly null; use document_date instead.")
    ingested_at: Optional[str] = None
    doc_type: Optional[str] = Field(None, description="earnings_presentation | press_release.")
    title: Optional[str] = None
    source_url: Optional[str] = Field(None, description="Where the company published it (its IR site).")
    fiscal_year: Optional[Union[int, str]] = None
    fiscal_period: Optional[str] = None
    page_count: Optional[int] = None
    pages_extracted: Optional[int] = None
    has_extracted_text: Optional[bool] = Field(
        None, description="False = get_ir_document_content will likely return no pages for this document yet."
    )


class IrDocumentsSeasonResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    filters: dict[str, Any] = Field(description="The request body sent to the backend (unset filters omitted).")
    count: int = Field(description="Documents returned.")
    rows: list[IrSeasonDocumentRow] = Field(description="Newest first.")
    note: Optional[str] = Field(None, description="Set only when no rows came back: an empty result is a valid answer.")
