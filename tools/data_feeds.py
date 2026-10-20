"""Data feed tools: versioned daily dataset files read back through /download-dataset."""

from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, Optional
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok
from schemas.data_feeds import DownloadDatasetResult


_Dataset = Literal["earnings_volatility_predictions", "investor_decks", "press_releases", "research_reports"]
_ResearchEventType = Literal[
    "biggest_mover", "biggest_loser", "8k_release", "transcript_update",
    "financials_release", "ir_publication", "eps_release",
]

# Datasets whose rows are one per document PAGE; collapsed to one row per document
# unless the caller asks for the page text.
_DOCUMENT_DATASETS = {"investor_decks", "press_releases"}
_DOCUMENT_FIELDS = (
    "event_date", "symbol", "title", "doc_type", "fiscal_year", "fiscal_period",
    "page_count", "document_url", "source_url",
)

# Decks and research publish after the 22:00 ET end-of-day build (research zips
# observed ~23:05 ET), so a weekday's file is only safe to ask for after this hour.
_NIGHTLY_READY_HOUR_ET = 23.5
_EARNINGS_VOL_FORWARD_BUSINESS_DAYS = 5


def _add_business_days(day: date, n: int) -> date:
    """Step `n` weekdays forward (negative = back) from `day`. No holiday calendar."""
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        day += timedelta(days=step)
        if day.weekday() < 5:
            remaining -= 1
    return day


def _default_window(dataset: str, now_et: datetime) -> tuple[str, str]:
    """The narrowest window that answers an undated request (handover §6).

    Earnings vol: today through the next 5 business days (the week ahead). Decks,
    press releases, research: the single most recent business day whose nightly
    file should already be out. Always ET dates, never the API's UTC default.
    """
    today = now_et.date()
    if dataset == "earnings_volatility_predictions":
        return today.isoformat(), _add_business_days(today, _EARNINGS_VOL_FORWARD_BUSINESS_DAYS).isoformat()
    hour = now_et.hour + now_et.minute / 60
    if today.weekday() < 5 and hour >= _NIGHTLY_READY_HOUR_ET:
        day = today
    else:
        day = _add_business_days(today, -1)
    return day.isoformat(), day.isoformat()


def _collapse_documents(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per document from its page rows, keeping first-seen (API sort) order."""
    docs: dict[Any, dict[str, Any]] = {}
    for page in pages:
        key = page.get("document_url") or (page.get("event_date"), page.get("symbol"), page.get("title"))
        doc = docs.get(key)
        if doc is None:
            doc = {f: page.get(f) for f in _DOCUMENT_FIELDS}
            doc["pages_with_text"] = 0
            doc["published_at"] = page.get("published_at")
            docs[key] = doc
        if page.get("content"):
            doc["pages_with_text"] += 1
        if (page.get("published_at") or "") > (doc["published_at"] or ""):
            doc["published_at"] = page.get("published_at")
    return list(docs.values())


@mcp.tool(
    annotations=ToolAnnotations(title="Download Dataset Feed", readOnlyHint=True),
    # structured_output=False: with include_page_text a deck result carries a full page
    # of text per row, and structured output would send it twice (text + structuredContent).
    structured_output=False,
)
async def download_dataset(
    ctx: Context,
    dataset: Annotated[
        _Dataset,
        Field(description="The feed: earnings_volatility_predictions, investor_decks, press_releases or "
                          "research_reports. list_options(\"data_feeds\") describes each, free of charge."),
    ],
    start_date: Annotated[
        Optional[str],
        Field(description="First date, inclusive, YYYY-MM-DD (the feed's date column: earnings_date for "
                          "earnings vol, event_date ET for the rest). Omit for the narrowest default: the week "
                          "ahead for earnings vol, the most recent business day for the others.",
              pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    end_date: Annotated[
        Optional[str],
        Field(description="Last date, inclusive, YYYY-MM-DD. Omit to take the default window (or start_date "
                          "alone, for a single day).",
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
        Optional[list[_ResearchEventType]],
        Field(description="research_reports only: narrow to these event types, e.g. [\"eps_release\"]. "
                          "Omit for all. Ignored for the other datasets."),
    ] = None,
    symbols: Annotated[
        Optional[list[str]],
        Field(description="Keep only these tickers, e.g. [\"SNOW\"]. Applied to the response here (the feed "
                          "itself has no symbol filter); not possible for research_reports links."),
    ] = None,
    include_page_text: Annotated[
        bool,
        Field(description="investor_decks / press_releases only. False (default) = one row per document, no "
                          "text. True = one row per page with its full `content` — only when the user wants "
                          "the text, and narrowed with symbols / document_urls."),
    ] = False,
    document_urls: Annotated[
        Optional[list[str]],
        Field(description="investor_decks / press_releases only: keep only these documents (their "
                          "`document_url`), e.g. to read one deck's pages."),
    ] = None,
    max_rows: Annotated[
        int,
        Field(description="Cap on rows returned (after filtering and collapsing); the result then carries "
                          "row_count and truncated: true.", ge=1, le=100000),
    ] = 500,
) -> DownloadDatasetResult:
    """Feed of versioned daily files: earnings-move predictions/actuals, investor decks, press releases and research-report PDFs, by date. Use it over explore_data_catalogue when the user wants one of these four datasets for a date or date range. RATE-LIMITED TO 3 CALLS PER WEEK per user (all datasets together, failed calls included): make ONE call with the narrowest dates, never test-fire it, never re-call because a result was trimmed, and never poll it from a dashboard — fetch once and embed the rows.

    For "what feeds are there" call list_options("data_feeds") instead — free, no
    rate limit. For open-ended analysis ("why did X move") keep to explore_data_catalogue.

    Datasets (each row also carries `published_at`, the UTC time its file version was
    published — quote it as the as-of):
    - earnings_volatility_predictions -> rows of `symbol, earnings_date, expected_move,
      hist_move_mean, hist_move_n, model_last_updated, actual_move,
      predictions_published_at`. `expected_move` / `actual_move` are absolute log moves
      of the reaction session (0.05 is about 5%); `actual_move` null = not known yet
      (BMO names settle that evening, AMC names the next evening, ~22:25 ET).
      Predictions publish ~06:00 ET Mon-Fri and cover every future earnings date scored.
      With latest_file_version=true each name appears once per date. Dates 2026-10-12..16
      were back-filled after the event (see `predictions_published_at`): never present
      them as a live track record.
    - investor_decks / press_releases -> by default ONE ROW PER DOCUMENT (`event_date,
      symbol, title, doc_type, fiscal_year, fiscal_period, page_count, document_url,
      source_url, pages_with_text, published_at`); include_page_text=true returns one row
      per page with its full `content` and `page_number`. `document_url` is the original
      PDF. Decks publish after the 22:00 ET build; coverage starts 2026-10-13.
      press_releases has only a 2026-10-14 backfill so far, so an empty result on other
      dates is expected — say so, don't retry. fiscal_year is parsed and occasionally wrong.
    - research_reports -> download links, one zip per (event_date, event_type):
      `event_date, event_type, published_at, url`. Each zip holds manifest.parquet
      (symbol, event_type, headline, occurred_at, file) and pdfs/<SYMBOL>.pdf. `url` is a
      presigned link VALID 12 HOURS: hand it to the user as a link, never store it.

    Sizing: leave the dates off for the default window (earnings vol: today through the
    next 5 business days; the others: the most recent business day whose nightly file is
    out) or pass explicit ET dates; widen only when the user asks ("this month", "since
    launch"). For "how did the predictions do", pass the most recent completed earnings
    date(s). A range with nothing published returns no rows, not an error.

    Returns {dataset, start_date, end_date, bulk_download, latest_file_version, shape,
    rows, row_count, truncated, published_at, notes}. `notes` lists what was left out
    (pages collapsed, symbols filtered, rows truncated) — tell the user in one line.

    Requires auth (your MCP client attaches the OAuth bearer automatically). Also counts
    once against the plan's daily request quota. A week of decks with page text is a
    very large payload; research links are tiny at any range.
    """
    notes: list[str] = []
    if end_date and not start_date:
        raise ToolError("Pass start_date with end_date (or start_date alone for a single day).")
    if start_date and end_date and end_date < start_date:
        raise ToolError(f"end_date {end_date} is before start_date {start_date}.")

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
        if not start_date:
            start_date, end_date = _default_window(dataset, datetime.now(ZoneInfo("America/New_York")))
        elif not end_date:
            end_date = start_date
        body["start_date"] = start_date
        body["end_date"] = end_date

    if event_types:
        if dataset == "research_reports":
            body["event_types"] = list(event_types)
        else:
            notes.append("event_types only applies to research_reports; it was ignored.")

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

    shape = "rows"
    if dataset == "research_reports":
        shape = "download_links"
        if symbols:
            notes.append("symbols cannot filter research links (each zip covers every name for its event "
                         "type); the zip's manifest.parquet lists its symbols.")
    else:
        if symbols:
            wanted = {s.strip().upper() for s in symbols}
            rows = [r for r in rows if str(r.get("symbol") or "").upper() in wanted]
            notes.append(f"Filtered to {', '.join(sorted(wanted))}.")
        if dataset in _DOCUMENT_DATASETS:
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
                if rows:
                    notes.append(f"Showing {len(rows)} documents ({page_rows} page rows collapsed); page text "
                                 "omitted — ask for a specific document's text.")
        elif include_page_text or document_urls:
            notes.append("include_page_text / document_urls only apply to investor_decks and press_releases; ignored.")

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
