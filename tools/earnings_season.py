"""Earnings-season tools: a cross-company view of the current earnings season — each company's latest
result (beat/miss vs estimate) and the IR decks and press releases published this season."""

import json
from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send
from schemas.earnings_season import EarningsSeasonCohortResult, IrDocumentsSeasonResult
from tools.ir_documents import _rows

# Matches the API: a ticker of 1-12 characters from A-Z, 0-9, "." and "-". Lowercase and
# surrounding whitespace are accepted here and normalized before the call.
_SEASON_TICKER_PATTERN = r"^\s*[A-Za-z0-9.\-]{1,12}\s*$"
# ISO 3166-1 alpha-2, e.g. US, GB; upper-cased before the call.
_COUNTRY_PATTERN = r"^\s*[A-Za-z]{2}\s*$"

Sector = Literal[
    "Basic Materials", "Communication Services", "Consumer Cyclical", "Consumer Defensive", "Energy",
    "Financial Services", "Healthcare", "Industrials", "Real Estate", "Technology", "Utilities",
]
MarketCapType = Literal["Micro-cap", "Small-cap", "Medium-cap", "Large-cap", "Mega-cap"]

SymbolFilter = Annotated[
    Optional[str],
    Field(description="Only this ticker, e.g. \"AAPL\" (1-12 characters: letters, digits, \".\" or \"-\").",
          pattern=_SEASON_TICKER_PATTERN),
]
SectorFilter = Annotated[Optional[Sector], Field(description="Only companies in this sector.")]
SpIndexFilter = Annotated[
    Optional[bool],
    Field(description="true = only S&P 500 members; false = only non-members. Omit for both."),
]
CountryFilter = Annotated[
    Optional[str],
    Field(description="Only companies in this country, as a 2-letter ISO code, e.g. \"US\", \"GB\".",
          pattern=_COUNTRY_PATTERN),
]
MarketCapFilter = Annotated[Optional[MarketCapType], Field(description="Only companies in this market-cap bucket.")]


def _filters(symbol: Optional[str], sector: Optional[str], sp_index: Optional[bool], country: Optional[str],
             marketcap_type: Optional[str], limit: int) -> dict[str, Any]:
    """The request body: only the filters that were set (never null), plus limit."""
    body: dict[str, Any] = {}
    if symbol is not None:
        body["symbol"] = symbol.strip().upper()
    if sector is not None:
        body["sector"] = sector
    if sp_index is not None:  # False is a real filter (non-members), so test for None
        body["sp_index"] = sp_index
    if country is not None:
        body["country"] = country.strip().upper()
    if marketcap_type is not None:
        body["marketcap_type"] = marketcap_type
    body["limit"] = limit
    return body


def _season_rows(resp: Any, path: str) -> list:
    """The backend's list; 422 raised with a hint, 429 / 401 / other errors raised via _rows."""
    if isinstance(resp, dict) and "HTTP 422" in str(resp.get("error", "")):
        detail = resp.get("detail")
        if detail not in (None, "") and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        raise ToolError(
            f"{path} rejected the filters (HTTP 422): check symbol (1-12 chars A-Z 0-9 . -), sector and "
            "marketcap_type (exact enum values), country (2-letter ISO code) and limit."
            + (f" Backend detail: {str(detail)[:1000]}" if detail not in (None, "") else "")
        )
    return _rows(resp, path)


def _empty_note(what: str, body: dict[str, Any]) -> str:
    set_filters = {k: v for k, v in body.items() if k != "limit"}
    scope = f"the filters {json.dumps(set_filters)}" if set_filters else "this earnings season"
    return (f"No {what} matched {scope}. This is a valid answer, not an error: nothing in the current "
            "earnings season fits. Loosen or drop a filter to widen it.")


@mcp.tool(annotations=ToolAnnotations(title="Get Earnings Season Cohort", readOnlyHint=True))
async def get_earnings_season_cohort(
    ctx: Context,
    symbol: SymbolFilter = None,
    sector: SectorFilter = None,
    sp_index: SpIndexFilter = None,
    country: CountryFilter = None,
    marketcap_type: MarketCapFilter = None,
    limit: Annotated[int, Field(description="Rows to return (1-1000, default 100).", ge=1, le=1000)] = 100,
) -> EarningsSeasonCohortResult:
    """Each covered company's latest earnings result in the current earnings season — beat/miss, EPS and revenue vs estimate, EPS YoY — one row per symbol, newest first. The cross-company view: "who missed", "how are small-cap healthcare names doing".

    Every filter is optional; filters given together are ANDed (e.g. sector="Healthcare" +
    marketcap_type="Small-cap"). Unset filters are left out of the request.

    Returns {"filters", "count", "rows", "note"}. A row is {symbol, sector, sp_index,
    country, marketcap_type, date, eps_actual, eps_estimated, revenue_actual,
    revenue_estimated, beat_flag, eps_yoy_change, cohort}.
    - beat_flag is "beat", "miss" or "in_line".
    - eps_yoy_change is a FRACTION, NOT a percent: 0.12 = +12%, -0.3 = -30%. Multiply by 100
      before showing it as a %. Null when there is no prior-year actual.
    - cohort is "sp500" (S&P 500 member), "beyond_sp500_us" (US, outside the S&P 500) or
      "other" (non-US).

    FLOW: filter here (e.g. beat_flag "miss" rows) -> list_ir_documents_season(symbol=...) for
    those names' decks and press releases -> get_ir_document_analysis /
    get_ir_document_content(url=document_url).

    An empty result is a valid answer (nothing matches the filters), not an error: `note` is
    set only then. A 422 means a filter failed validation. A 401 means the sign-in expired
    (your MCP client re-runs OAuth). A 429 means the 100 requests/minute per-user limit (or
    the plan quota) was hit. Requires auth (your MCP client attaches the OAuth bearer
    automatically); counts toward plan quota.
    """
    body = _filters(symbol, sector, sp_index, country, marketcap_type, limit)
    rows = _season_rows(
        await _send(ctx, "POST", "/earnings-season-cohort", json=body), "/earnings-season-cohort"
    )
    note = _empty_note("earnings results", body) if not rows else None
    return EarningsSeasonCohortResult(filters=body, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="List IR Documents This Season", readOnlyHint=True))
async def list_ir_documents_season(
    ctx: Context,
    symbol: SymbolFilter = None,
    sector: SectorFilter = None,
    sp_index: SpIndexFilter = None,
    country: CountryFilter = None,
    marketcap_type: MarketCapFilter = None,
    limit: Annotated[int, Field(description="Documents to return (1-500, default 50).", ge=1, le=500)] = 50,
) -> IrDocumentsSeasonResult:
    """Investor-relations documents (earnings presentations and press releases) published or ingested this earnings season, across companies, newest first — "which decks came out this season", or the decks for names picked from get_earnings_season_cohort.

    Every filter is optional; filters given together are ANDed. Unset filters are left out of
    the request.

    Returns {"filters", "count", "rows", "note"}. A row is {document_url, symbol,
    company_name, sector, industry, country, sp_index, marketcap_type, in_coverage_universe,
    document_date, published_date, ingested_at, doc_type, title, source_url, fiscal_year,
    fiscal_period, page_count, pages_extracted, has_extracted_text}.
    - Symbols are NOT limited to the coverage universe: in_coverage_universe = false marks
      companies outside it (other tools may have little or nothing on them).
    - Rank recency on document_date. published_date is mostly null.
    - doc_type is "earnings_presentation" or "press_release".
    - document_url feeds get_ir_document_content(url=document_url); source_url is the
      company's IR-site link and is not interchangeable. has_extracted_text = false means
      there is no page text to read yet.

    FLOW: get_earnings_season_cohort -> list_ir_documents_season -> get_ir_document_analysis
    (the analyst's read) / get_ir_document_content(url=document_url) (the pages).

    An empty result is a valid answer (nothing matches the filters), not an error: `note` is
    set only then. A 422 means a filter failed validation. A 401 means the sign-in expired
    (your MCP client re-runs OAuth). A 429 means the 100 requests/minute per-user limit (or
    the plan quota) was hit. Requires auth (your MCP client attaches the OAuth bearer
    automatically); counts toward plan quota.
    """
    body = _filters(symbol, sector, sp_index, country, marketcap_type, limit)
    rows = _season_rows(await _send(ctx, "POST", "/ir-documents-season", json=body), "/ir-documents-season")
    note = _empty_note("IR documents", body) if not rows else None
    return IrDocumentsSeasonResult(filters=body, count=len(rows), rows=rows, note=note)
