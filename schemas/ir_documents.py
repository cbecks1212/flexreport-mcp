"""Output schemas for tools/ir_documents.py.

Row fields are Optional because the SDK rejects any successful result that fails its
schema. Unlisted backend keys pass through via extra="allow". The analysis's narrative
fields (key_takeaways, reported_metrics, guidance_commentary, flags) are typed Any so a
string, list or object from the backend all validate.
"""

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class IrDocumentRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    document_url: Optional[str] = Field(
        None, description="The stored document: pass it to get_ir_document_content(url=...). Not the same as source_url."
    )
    symbol: Optional[str] = None
    title: Optional[str] = None
    doc_type: Optional[str] = Field(None, description="Kind of document, e.g. a deck or presentation.")
    fiscal_year: Optional[Union[int, str]] = None
    fiscal_period: Optional[str] = None
    published_date: Optional[str] = None
    page_count: Optional[int] = None
    content_type: Optional[str] = None
    source_url: Optional[str] = Field(None, description="Where the company published it (its IR site).")
    last_updated: Optional[str] = None


class IrDocumentsResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    count: int = Field(description="Documents returned.")
    rows: list[IrDocumentRow] = Field(description="Newest first.")
    note: Optional[str] = Field(None, description="Set only when no rows came back: what an empty result can mean.")


class IrDocumentAnalysisRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: Optional[str] = None
    document_url: Optional[str] = Field(
        None, description="The deck analyzed: pass it to get_ir_document_content(url=...) to read the pages."
    )
    source_url: Optional[str] = Field(None, description="Where the company published the deck (its IR site).")
    title: Optional[str] = None
    doc_type: Optional[str] = None
    document_date: Optional[str] = None
    summary: Optional[Any] = Field(None, description="The analyst's read of the deck. Model analysis, not a quote.")
    key_takeaways: Optional[Any] = None
    reported_metrics: Optional[Any] = Field(None, description="Figures the analysis read off the deck; cite page_number_url when repeating one.")
    guidance_commentary: Optional[Any] = None
    flags: Optional[Any] = None
    cited_pages: Optional[list[int]] = Field(None, description="Page numbers the analysis relied on, in cited order.")
    page_number_url: Optional[list[str]] = Field(
        None,
        description="One document_url#page=N link per cited page, in cited order: the provenance for this analysis. "
                    "Empty when no pages were cited.",
    )
    last_updated: Optional[str] = Field(None, description="When the analysis was written.")


class IrDocumentAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    count: int = Field(description="Analyses returned: the latest analysis for each deck, one row per deck.")
    rows: list[IrDocumentAnalysisRow]
    note: Optional[str] = Field(None, description="Set only when no rows came back: what an empty result can mean.")


class IrContentPage(BaseModel):
    model_config = ConfigDict(extra="allow")

    page_number: Optional[int] = None
    page_number_url: Optional[str] = Field(None, description="document_url#page=N: the link to cite for this page.")
    char_count: Optional[int] = None
    content: Optional[str] = Field(None, description="The page's extracted text.")


class IrContentResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str = Field(description="The document_url sent to the backend.")
    count: int = Field(description="Pages returned.")
    pages: list[IrContentPage] = Field(description="One entry per page of extracted text.")
    note: Optional[str] = Field(None, description="Set only when no pages came back: what an empty result can mean.")
