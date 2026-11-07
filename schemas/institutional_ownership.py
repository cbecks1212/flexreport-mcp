"""Output schemas for tools/institutional_ownership.py.

Row fields are Optional because the SDK rejects any successful result that fails its
schema. Unlisted backend keys pass through via extra="allow".
"""

from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# int stays int (share counts), rather than coming back as 1234.0.
Number = Union[int, float]


class PositionChangeRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: Optional[str] = Field(None, description="Quarter end of the filing (YYYY-MM-DD).")
    symbol: Optional[str] = None
    manager_name: Optional[str] = None
    status: Optional[str] = Field(None, description="NEW, CLOSED, INCREASED, DECREASED or UNCHANGED.")
    shares: Optional[Number] = None
    value: Optional[Number] = None
    prev_shares: Optional[Number] = None
    prev_value: Optional[Number] = None
    share_chg: Optional[Number] = None
    share_chg_pct: Optional[Number] = None
    value_chg: Optional[Number] = None
    value_chg_pct: Optional[Number] = None
    accessions: Optional[list[str]] = Field(
        None, description="SEC accession numbers; a position can come from more than one filing."
    )
    sec_urls: Optional[list[str]] = Field(
        None, description="The filings. A CLOSED row cites the last filing that reported the position."
    )


class PositionChangesResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    cik: str
    position_filter: list[str] = Field(description="The statuses requested.")
    count: int = Field(description="Rows returned.")
    rows: list[PositionChangeRow] = Field(description="One row per position in the latest filed quarter, largest value first.")
    note: Optional[str] = Field(None, description="Set only when no rows came back: what an empty result can mean.")


class OwnershipByManagerTypeRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: Optional[str] = Field(None, description="Quarter end (YYYY-MM-DD).")
    symbol: Optional[str] = None
    investor_type: Optional[str] = None
    total_shares: Optional[Number] = None
    num_investors: Optional[Number] = None
    current_period_value: Optional[Number] = None
    previous_quarter_value: Optional[Number] = None
    previous_year_value: Optional[Number] = None
    previous_quarter_change: Optional[Number] = Field(
        None, description="Percent change in VALUE, so it includes price moves. Null when the prior quarter has no row or a value of 0."
    )
    previous_year_change: Optional[Number] = Field(
        None, description="Percent change in VALUE. Null when the year-ago quarter has no row or a value of 0."
    )


class OwnershipByManagerTypeResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    count: int = Field(description="Rows returned: one per manager type per quarter, not one per quarter.")
    rows: list[OwnershipByManagerTypeRow] = Field(description="Newest quarter first, largest value first within it.")
    note: Optional[str] = Field(None, description="Set only when no rows came back: what an empty result can mean.")


class CikAllocationRow(BaseModel):
    """The group label arrives under the requested group_by, as `sector` OR `industry`.

    Neither is declared: a declared field would serialize as null in the other mode, and
    the row would appear to carry both. extra="allow" passes the real one through.
    """

    model_config = ConfigDict(extra="allow")

    date: Optional[str] = Field(None, description="Quarter end (YYYY-MM-DD).")
    cik: Optional[str] = None
    manager_name: Optional[str] = None
    positions: Optional[Number] = None
    value: Optional[Number] = None
    weight_pct: Optional[Number] = Field(None, description="Share of covered_value, not of the whole portfolio.")
    previous_quarter_weight_pct: Optional[Number] = Field(
        None, description="0 if the filer held nothing in this group in a quarter it filed; null if it did not file that quarter."
    )
    weight_chg_pts: Optional[Number] = None
    all_institution_weight_pct: Optional[Number] = Field(None, description="Null for a partial quarter.")
    covered_value: Optional[Number] = Field(None, description="Long stock positions mapped to a symbol: the base of weight_pct.")
    reported_portfolio_value: Optional[Number] = Field(None, description="Null except for the filer's latest profiled quarter.")
    coverage_pct: Optional[Number] = Field(None, description="covered_value over reported_portfolio_value. Null when that is null.")


class CikAllocationResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    cik: str
    group_by: str = Field(description="\"sector\" or \"industry\": the field each row labels its group with.")
    limit: int = Field(description="Quarters requested, after the tool's default.")
    count: int = Field(description="Rows returned: one per group per quarter.")
    rows: list[CikAllocationRow] = Field(
        description="Newest quarter first, largest value first within it. Each row also carries `sector` or `industry`, per group_by."
    )
    note: Optional[str] = Field(None, description="Set only when no rows came back: what an empty result can mean.")
