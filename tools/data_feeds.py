"""Data feed tools: versioned daily dataset files read back through /download-dataset."""

from typing import Annotated, Any, Optional

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok
from schemas.data_feeds import DownloadDatasetResult


# Per-page columns of a document feed (rows carrying a `document_url` and a
# `page_number`). Dropped when pages are collapsed to one row per document; every
# other column is document-level and kept as the backend sends it.
_PAGE_FIELDS = ("content", "page_number")


def _collapse_documents(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per document from its page rows, keeping first-seen (API sort) order."""
    docs: dict[Any, dict[str, Any]] = {}
    for page in pages:
        key = page.get("document_url")
        doc = docs.get(key)
        if doc is None:
            doc = {k: v for k, v in page.items() if k not in _PAGE_FIELDS}
            doc["pages_with_text"] = 0
            docs[key] = doc
        if page.get("content"):
            doc["pages_with_text"] += 1
        if (page.get("published_at") or "") > (doc.get("published_at") or ""):
            doc["published_at"] = page.get("published_at")
    return list(docs.values())


@mcp.tool(
    annotations=ToolAnnotations(title="Download Dataset Feed", readOnlyHint=True),
    # structured_output=False: with include_page_text a document feed carries a full page
    # of text per row, and structured output would send it twice (text + structuredContent).
    structured_output=False,
)
async def download_dataset(
    ctx: Context,
    dataset: Annotated[
        str,
        Field(description="A `dataset` from list_options(\"data_feeds\") (free, no rate limit), "
                          "e.g. \"earnings_volatility_predictions\"."),
    ],
    start_date: Annotated[
        str,
        Field(description="First date, inclusive, YYYY-MM-DD, on the feed's `date_field` (ET). Pick the "
                          "narrowest window that answers the request.",
              pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ],
    end_date: Annotated[
        Optional[str],
        Field(description="Last date, inclusive, YYYY-MM-DD. Omit for start_date alone (one day).",
              pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    latest_file_version: Annotated[
        bool,
        Field(description="True (default) = newest file version per date. False = every version ever "
                          "published — only for point-in-time / backtest questions (\"what did the model "
                          "say Monday morning?\")."),
    ] = True,
    bulk_download: Annotated[
        bool,
        Field(description="True ignores the dates and returns EVERY published date. Only when the user "
                          "explicitly asks for full history."),
    ] = False,
    event_types: Annotated[
        Optional[list[str]],
        Field(description="Narrow to these event types, from the feed's `event_types` in "
                          "list_options(\"data_feeds\"). Omit for all; ignored by feeds whose `event_types` is null."),
    ] = None,
    symbols: Annotated[
        Optional[list[str]],
        Field(description="Keep only these tickers, e.g. [\"SNOW\"]. Applied to the response here (the feed "
                          "itself has no symbol filter); not possible for `download_links` feeds."),
    ] = None,
    include_page_text: Annotated[
        bool,
        Field(description="Document feeds (one row per page) only. False (default) = one row per document, no "
                          "text. True = one row per page with its full `content` — only when the user wants "
                          "the text, and narrowed with symbols / document_urls."),
    ] = False,
    document_urls: Annotated[
        Optional[list[str]],
        Field(description="Document feeds only: keep only these documents (their `document_url`), e.g. to "
                          "read one deck's pages."),
    ] = None,
    max_rows: Annotated[
        int,
        Field(description="Cap on rows returned (after filtering and collapsing); the result then carries "
                          "row_count and truncated: true.", ge=1, le=100000),
    ] = 500,
) -> DownloadDatasetResult:
    """Feed of versioned daily files (earnings-move predictions/actuals, investor decks, press releases, research-report PDFs, ...) by date. Use it over explore_data_catalogue when the user wants one of these datasets for a date or date range. RATE-LIMITED TO 3 CALLS PER WEEK per user (all datasets together, failed calls included): make ONE call with the narrowest dates, never test-fire it, never re-call because a result was trimmed, and never poll it from a dashboard — fetch once and embed the rows.

    list_options("data_feeds") lists the feeds — free, no rate limit — each as
    {dataset, description, returns, date_field, event_types}: `returns` is "rows" or
    "download_links" (presigned URLs), `date_field` is the column the dates filter on,
    and `event_types` the valid filter values (null = unsupported). This tool checks
    `dataset` and `event_types` against that list before spending a download call. For
    open-ended analysis ("why did X move") keep to explore_data_catalogue.

    Sizing — always pass explicit ET dates, the narrowest that answer the request, and
    widen only when the user asks ("this month", "since launch"): the week ahead for
    forward-looking predictions; the most recent completed date for "how did it do"; one
    business day for documents (nightly files land after the 22:00 ET build, so before
    then the latest complete day is the previous business day). A range with nothing
    published returns no rows, not an error.

    Shaping, applied here to the one response:
    - Every row carries `published_at`, the UTC time its file version was published —
      quote the result's `published_at` as the data's as-of.
    - Document feeds (rows with `document_url` + `page_number`) come back ONE ROW PER
      DOCUMENT (page columns dropped, `pages_with_text` added); include_page_text=true
      returns the page rows with their full `content`.
    - `download_links` feeds return links valid 12 HOURS: hand each `url` to the user as
      a link, never store it or inline the file.

    Returns {dataset, start_date, end_date, bulk_download, latest_file_version, shape,
    rows, row_count, truncated, published_at, notes}. `notes` lists what was left out
    (pages collapsed, symbols filtered, rows truncated) — tell the user in one line.

    Requires auth (your MCP client attaches the OAuth bearer automatically). Also counts
    once against the plan's daily request quota.
    """
    end_date = end_date or start_date
    if end_date < start_date:
        raise ToolError(f"end_date {end_date} is before start_date {start_date}.")

    # Validate against the live catalogue (public, unmetered) so a bad name never
    # spends one of the 3 weekly calls.
    feeds = _ok(await _send(ctx, "GET", "/list-data-feed-options", require_auth=False))
    feed = next((f for f in feeds if f.get("dataset") == dataset), None)
    if feed is None:
        raise ToolError(f"Unknown dataset {dataset!r}; valid: {', '.join(f['dataset'] for f in feeds)}. "
                        "No download call was made.")

    notes: list[str] = []
    body: dict[str, Any] = {
        "dataset": dataset,
        "bulk_download": bulk_download,
        "latest_file_version": latest_file_version,
    }
    if bulk_download:
        start_date = end_date = None
    else:
        # Always send explicit dates: the API's default is the server's UTC date, which
        # is already tomorrow after ~20:00 ET.
        body["start_date"] = start_date
        body["end_date"] = end_date

    if event_types:
        valid_types = feed.get("event_types")
        if not valid_types:
            notes.append(f"{dataset} has no event_types filter; it was ignored.")
        elif unknown := sorted(set(event_types) - set(valid_types)):
            raise ToolError(f"Unknown event_types {unknown} for {dataset}; valid: {', '.join(valid_types)}. "
                            "No download call was made.")
        else:
            body["event_types"] = list(event_types)

    resp = await _send(ctx, "POST", "/download-dataset", json=body)
    if isinstance(resp, dict) and "HTTP 429" in str(resp.get("error", "")):
        raise ToolError(
            "download_dataset's limit is 3 calls per week per user and it has been used up (or the plan's "
            "daily quota is). Do not retry: answer from rows already fetched, use explore_data_catalogue for "
            "the same data, or call upgrade_plan if the user hit their plan quota."
        )
    rows = _ok(resp)
    if not isinstance(rows, list):
        raise ToolError(f"Unexpected /download-dataset response: {str(rows)[:500]}")

    if feed.get("returns") == "download_links":
        shape = "download_links"
        if symbols:
            notes.append("symbols cannot filter download links (each file covers every name it holds).")
    else:
        shape = "rows"
        if symbols:
            wanted = {s.strip().upper() for s in symbols}
            rows = [r for r in rows if str(r.get("symbol") or "").upper() in wanted]
            notes.append(f"Filtered to {', '.join(sorted(wanted))}.")
        if any("document_url" in r and "page_number" in r for r in rows):
            if document_urls:
                urls = set(document_urls)
                rows = [r for r in rows if r.get("document_url") in urls]
                notes.append(f"Filtered to {len(urls)} requested document(s).")
            if include_page_text:
                shape = "pages"
            else:
                shape = "documents"
                page_rows = len(rows)
                rows = _collapse_documents(rows)
                notes.append(f"Showing {len(rows)} documents ({page_rows} page rows collapsed); page text "
                             "omitted — ask for a specific document's text.")
        elif rows and (include_page_text or document_urls):
            notes.append("include_page_text / document_urls only apply to document feeds; ignored.")

    row_count = len(rows)
    truncated = row_count > max_rows
    if truncated:
        rows = rows[:max_rows]
        notes.append(f"Truncated to the first {max_rows} of {row_count} rows — narrow the dates or symbols next "
                     "time; do not re-call for the rest (3 calls/week).")
    if not row_count:
        window = "every published date" if bulk_download else f"{start_date}..{end_date}"
        notes.append(f"Nothing published for {dataset} in {window} (after filters). Not an error; don't re-call "
                     "the same window.")

    published = [str(r["published_at"]) for r in rows if r.get("published_at")]
    return DownloadDatasetResult(
        dataset=dataset,
        start_date=start_date,
        end_date=end_date,
        bulk_download=bulk_download,
        latest_file_version=latest_file_version,
        shape=shape,
        rows=rows,
        row_count=row_count,
        truncated=truncated,
        published_at=max(published) if published else None,
        notes=notes,
    )
