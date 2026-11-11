"""Investor-relations document tools: list a company's IR decks, read the analyst's take on them, read a deck's pages."""

import re
from typing import Annotated, Any

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok
from schemas.ir_documents import IrContentResult, IrDocumentAnalysisResult, IrDocumentsResult


# A ticker: letters and digits, with "." or "-" for share classes (BRK.B, BF-B). Surrounding
# whitespace is allowed here and stripped before the call.
_TICKER_PATTERN = r"^\s*[A-Za-z0-9][A-Za-z0-9.\-]{0,14}\s*$"
# Matches the API: an http(s) URL with no whitespace, at most 2048 characters.
_URL_PATTERN = r"^https?://\S+$"
_URL_MAX_LENGTH = 2048
# page_number_url is document_url + "#page=N"; the backend keys content on document_url.
_PAGE_FRAGMENT = re.compile(r"#page=\d+$")


def _rows(resp: Any, path: str) -> list:
    """The backend's list, with 429 (and, via _ok, 401 and every other error) raised as a ToolError."""
    if isinstance(resp, dict) and "HTTP 429" in str(resp.get("error", "")):
        detail = resp.get("detail")
        raise ToolError(
            f"{path} is rate-limited to 100 requests/minute per user (or the plan's quota is used up) and the "
            "limit was hit. Wait a minute before retrying, answer from results already fetched, or call "
            "upgrade_plan if the user hit their plan quota."
            + (f" Backend detail: {str(detail)[:500]}" if detail not in (None, "") else "")
        )
    rows = _ok(resp)
    if not isinstance(rows, list):
        raise ToolError(f"Unexpected {path} response: {str(rows)[:500]}")
    return rows


@mcp.tool(annotations=ToolAnnotations(title="List IR Documents", readOnlyHint=True))
async def list_ir_documents(
    ctx: Context,
    symbol: Annotated[str, Field(description="Covered ticker, e.g. \"AVNW\".", pattern=_TICKER_PATTERN)],
    limit: Annotated[
        int,
        Field(description="Most recent documents to return (1-100, default 10).", ge=1, le=100),
    ] = 10,
) -> IrDocumentsResult:
    """A company's investor-relations documents (decks, presentations), newest first — the inventory step before reading what a deck said.

    FLOW: list_ir_documents -> get_ir_document_analysis (the analyst's read, with the pages
    it cites) -> get_ir_document_content(url=document_url) for the page text.

    Returns {"symbol", "count", "rows", "note"}, newest first. A row is {document_url,
    symbol, title, doc_type, fiscal_year, fiscal_period, published_date, page_count,
    content_type, source_url, last_updated}. `document_url` is the stored copy that
    get_ir_document_content takes; `source_url` is where the company published it. They are
    not interchangeable.

    An empty result means no stored documents, not necessarily that the company publishes
    none: `note` is set only then. A 422 means a bad symbol or limit. A 401 means the
    sign-in expired (your MCP client re-runs OAuth). Requires auth (your MCP client attaches
    the OAuth bearer automatically). Rate-limited 100/min per user; counts toward plan quota.
    """
    symbol = symbol.strip().upper()
    rows = _rows(await _send(ctx, "POST", "/ir-document", json={"symbol": symbol, "limit": limit}), "/ir-document")
    note = None
    if not rows:
        note = (f"No IR documents stored for {symbol}. That is not proof the company publishes none: the symbol "
                "may be outside coverage or its decks not yet collected.")
    return IrDocumentsResult(symbol=symbol, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="Get IR Document Analysis", readOnlyHint=True))
async def get_ir_document_analysis(
    ctx: Context,
    symbol: Annotated[str, Field(description="Covered ticker, e.g. \"AVNW\".", pattern=_TICKER_PATTERN)],
    limit: Annotated[
        int,
        Field(description="Most recent decks to return an analysis for (1-50, default 5).", ge=1, le=50),
    ] = 5,
) -> IrDocumentAnalysisResult:
    """The analyst's read of a company's most recent IR decks, the latest analysis per deck, with links to the exact pages it relied on — the direct read for "what did X's investor deck say".

    Returns {"symbol", "count", "rows", "note"}, one row per deck. A row is {symbol,
    document_url, source_url, title, doc_type, document_date, summary, key_takeaways,
    reported_metrics, guidance_commentary, flags, cited_pages, page_number_url,
    last_updated}.

    These fields are a model's analysis of the deck, not quotes, so attribute them to the
    analysis.

    PROVENANCE: `cited_pages` lists the page numbers the analysis relied on, and
    `page_number_url` gives a `document_url#page=N` link for each, in the same cited order.
    Those links are the provenance for the analysis: WHEN YOU REPEAT A FIGURE OR CLAIM FROM
    IT, CITE THE page_number_url link it came from. Both lists are empty when no pages were
    cited, so say that analysis is uncited. To check a figure against the deck, call
    get_ir_document_content(url=document_url) and read the matching `page_number`.

    An empty result means no analyzed decks, not necessarily no decks: try
    list_ir_documents. `note` is set only then. A 422 means a bad symbol or limit. A 401
    means the sign-in expired (your MCP client re-runs OAuth). Requires auth (your MCP
    client attaches the OAuth bearer automatically). Rate-limited 100/min per user; counts
    toward plan quota.
    """
    symbol = symbol.strip().upper()
    rows = _rows(
        await _send(ctx, "POST", "/ir-document-analysis", json={"symbol": symbol, "limit": limit}),
        "/ir-document-analysis",
    )
    note = None
    if not rows:
        note = (f"No analyzed IR decks for {symbol}. That is not proof it has no decks: list_ir_documents shows "
                "what is stored, and get_ir_document_content reads one directly.")
    return IrDocumentAnalysisResult(symbol=symbol, count=len(rows), rows=rows, note=note)


@mcp.tool(annotations=ToolAnnotations(title="Get IR Document Content", readOnlyHint=True))
async def get_ir_document_content(
    ctx: Context,
    url: Annotated[
        str,
        Field(description="A document_url returned by list_ir_documents or get_ir_document_analysis (an http(s) URL "
                          "with no whitespace, at most 2048 characters). Not the source_url.",
              pattern=_URL_PATTERN, max_length=_URL_MAX_LENGTH),
    ],
) -> IrContentResult:
    """The extracted text of one IR document, page by page — use it to open the pages an analysis cites, or to read a deck directly.

    FLOW: take `document_url` from list_ir_documents or get_ir_document_analysis. A
    `page_number_url` (document_url#page=N) works too: the `#page=N` is dropped before the
    call and the whole document comes back, so read the page whose `page_number` is N.

    Returns {"url", "count", "pages", "note"}. A page is {page_number, page_number_url,
    char_count, content}. Cite a page's `page_number_url` when you repeat anything from its
    `content`.

    An empty result usually means the url is not a stored document_url (a `source_url`
    will not match): `note` is set only then. A 422 means the url failed validation. A 401
    means the sign-in expired (your MCP client re-runs OAuth). Requires auth (your MCP
    client attaches the OAuth bearer automatically). Rate-limited 100/min per user; counts
    toward plan quota.
    """
    url = _PAGE_FRAGMENT.sub("", url)
    pages = _rows(await _send(ctx, "POST", "/ir-content", json={"url": url}), "/ir-content")
    note = None
    if not pages:
        note = ("No extracted pages for this url. Pass a document_url exactly as list_ir_documents or "
                "get_ir_document_analysis returned it, not the source_url.")
    return IrContentResult(url=url, count=len(pages), pages=pages, note=note)
