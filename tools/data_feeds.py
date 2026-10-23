"""Data feed tools: Flexreport's versioned daily dataset files (/download-dataset) and the
user's own scheduled datasets (/draft-custom-dataset, /save-user-dataset, /list-user-datasets,
/delete-user-data-feed) and the rows of their published files (get_user_dataset_file)."""

import csv
import io
import json
import xml.etree.ElementTree as ET
from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok, _request_capped, MAX_RESPONSE_BYTES
from schemas.data_feeds import (
    DatasetFormat,
    DeleteUserDataFeedResult,
    DownloadDatasetResult,
    DraftCustomDatasetDispatch,
    ListUserDatasetsResult,
    SaveUserDatasetResult,
    UserDatasetFileResult,
)


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
        Field(description="A `dataset` from list_options(\"data_feeds\") (free, no rate limit)."),
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
    """Read one of Flexreport's versioned daily data feeds by date — the feeds and what each holds come from list_options("data_feeds"). Use it over explore_data_catalogue when one of those feeds answers the ask for a date or date range. RATE-LIMITED TO 3 CALLS PER WEEK per user (all datasets together, failed calls included): make ONE call with the narrowest dates, never test-fire it, never re-call because a result was trimmed, and never poll it from a dashboard — fetch once and embed the rows.

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


@mcp.tool(annotations=ToolAnnotations(title="Draft Custom Dataset", readOnlyHint=True))
async def draft_custom_dataset(
    ctx: Context,
    request: Annotated[
        str,
        Field(description="The dataset to build, in plain English — what rows, which columns, which names, what "
                          "window (e.g. \"daily close and 20-day average volume for AAPL, MSFT, NVDA over the last "
                          "3 months\"). When refining, re-send the same request."),
    ],
    previous_draft_id: Annotated[
        Optional[str],
        Field(description="draft_id of an earlier draft to refine (with `feedback`); the planner sees that "
                          "draft's queries."),
    ] = None,
    feedback: Annotated[
        Optional[str],
        Field(description="What to change about the previous draft, e.g. \"add the 50-day average\", \"weekly "
                          "instead of daily\". Only with previous_draft_id."),
    ] = None,
    sample_rows: Annotated[
        int,
        Field(description="Rows of each candidate returned as a sample (at most 5).", ge=1, le=5),
    ] = 5,
    target_format: Annotated[
        Optional[DatasetFormat],
        Field(description="The format the user plans to save in. rss/atom make the planner shape one row per "
                          "feed item (title, link, description, pub_date, guid), newest first. Omit when unknown."),
    ] = None,
) -> DraftCustomDatasetDispatch:
    """EVENT/NEWS FEEDS (filings, decks, earnings, news) — RESEARCH FIRST, then draft: 1) situate(question=<the user's words>), then get_event_ontology for the feed's event types to learn where each persists (`persisted_in`); 2) agree definitions with the user before drafting — universe, window, and what any loose term means (e.g. the proxy for "the AI trade"); 3) ONE SCOPED explore_data_catalogue census of company_event_node for the universe and window (tickers, dates, a row limit in the wording); 4) draft a request naming the table and column for each field, e.g. title from `earnings` figures, body from eight_k_timeseries_analysis.summary, link sec_link, guid company_event_node.node_id, pub_date occurred_at, ticker symbol_metadata.exchange || ':' || symbol; 5) refine with previous_draft_id + feedback, asking for ONE candidate when variants don't matter. PLAIN DATA FEEDS ("daily close and 20-day volume for AAPL, MSFT") skip all this and draft directly.

    Drafts the user's OWN dataset — a table built from Flexreport data on a schedule and published as a file (parquet, csv, tsv, json, jsonl, xlsx, xml, or an rss/atom feed) behind a fixed link — from a plain-English description. Step 1 of 2: draft (and refine) here, then save_user_dataset schedules it. Use when the user wants a custom data feed or a recurring file of their own ("build me a daily file of...", "an RSS feed of...", "a JSON feed I can poll"); for Flexreport's published feeds use download_dataset, and for a one-off look at the data use explore_data_catalogue. This is THE tool for any request worded as a "datafeed" or "feed" — never substitute schedule_task, an emailed workflow or a one-off file. Every column comes from stored data, and that includes the summaries and analysis Flexreport already stores (deck, press-release and 8-K summaries, key figures, flags, recommendations, research views), so a feed with summary columns is still a dataset; only new written analysis generated fresh each run is a schedule_task workflow (a transform step).

    Asynchronous: returns {draft_id, task_id, status: "PENDING"}. Poll task_id with
    get_task_status until SUCCESS — a draft takes about 45 s-4 min (validation, planning, a
    sample run, finalize). On SUCCESS `result` is:
      {draft_id, validation_status: "OK" | "REJECTED", reason?, suggested_alternative?,
       user_request,
       candidates: [{name, description, columns, row_count, sample_rows, query_token,
                     has_fixed_dates, suggested_date_mode}]}
    REJECTED -> relay `reason` and `suggested_alternative`; don't retry blindly.

    Show the user each candidate's description, columns, row_count and a few sample_rows
    and let them pick one. NEVER show `query_token` — it is an opaque sealed query whose
    only use is save_user_dataset — but keep the chosen candidate's for the save.
    - Samples show datetime columns as dates only; the built file keeps the full
      timestamp, so never tell the user a timestamp is missing from a sample.
    - Candidates can be near-identical: compare their descriptions, not just the samples.

    Pass `target_format` when the user has named one (always for rss/atom). A candidate
    with a column named `published_at` can't be saved (that name is reserved for the
    build time): refine it with feedback asking to rename the column.

    To refine, call again with the SAME `request`, previous_draft_id=<draft_id> and
    `feedback`; ask for "ONE candidate" when the variants don't matter. Each round is a
    new draft with its own draft_id. Ask before saving: name,
    schedule, and (if not the suggestion) date mode.

    Rate-limited to 20 drafts per hour per user. Requires auth (your MCP client attaches
    the OAuth bearer automatically).
    """
    if feedback and not previous_draft_id:
        raise ToolError("feedback refines an earlier draft: pass previous_draft_id with it.")
    body: dict[str, Any] = {"request": request, "sample_rows": sample_rows}
    if target_format:
        body["target_format"] = target_format
    if previous_draft_id:
        body["previous_draft_id"] = previous_draft_id
    if feedback:
        body["feedback"] = feedback

    resp = await _send(ctx, "POST", "/draft-custom-dataset", json=body)
    if isinstance(resp, dict) and "HTTP 429" in str(resp.get("error", "")):
        raise ToolError(
            "draft_custom_dataset's limit is 20 drafts per hour per user (or the plan's daily quota) and it has "
            "been used up. Work from a draft already returned, or call upgrade_plan if the user hit their plan quota."
        )
    return _ok(resp)


@mcp.tool(annotations=ToolAnnotations(title="Save User Dataset", readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def save_user_dataset(
    ctx: Context,
    draft_id: Annotated[str, Field(description="draft_id from draft_custom_dataset.")],
    query_token: Annotated[
        str,
        Field(description="query_token of the candidate the user chose, from that draft's get_task_status result."),
    ],
    dataset_name: Annotated[
        str,
        Field(description="Lowercase name, 3-64 chars: letters, digits, _ and -. Re-saving the same name "
                          "replaces the dataset's rules and keeps its files.",
              pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$"),
    ],
    date_mode: Annotated[
        Literal["rolling", "snapshot"],
        Field(description="The candidate's `suggested_date_mode` unless the user says otherwise. rolling = move "
                          "the query's fixed dates forward to each build date, keeping the window length; "
                          "snapshot = re-run the query exactly as saved."),
    ],
    cron: Annotated[
        str,
        Field(description="When to build, any valid 5-field cron in America/New_York, e.g. \"30 18 * * 1-5\" "
                          "(weekdays 18:30), \"*/30 * * * *\" (every 30 minutes), \"0 9-17 * * 1-5\" (hourly in "
                          "market hours)."),
    ],
    format: Annotated[
        DatasetFormat,
        Field(description="File format of each build: parquet, csv, tsv, json (an array of records), jsonl, "
                          "xlsx, xml, or rss / atom (a feed of the newest 200 rows as items; rss items carry "
                          "<company:symbol> and <category> tags for syndication partners). Use the user's "
                          "choice, else the draft's target_format, else parquet."),
    ] = "parquet",
    build_now: Annotated[
        bool,
        Field(description="Build the first file right away (default) instead of waiting for the cron."),
    ] = True,
) -> SaveUserDatasetResult:
    """Save a drafted candidate as the user's scheduled dataset (step 2 of 2, after draft_custom_dataset): its query, the schema it produces, the file format, the date mode and a cron schedule. Each build publishes a new file only when the rows changed; list_user_datasets returns the files.

    New items are reviewed before publishing, up to 40 per build. On a large first build
    the rest publish as queried and are reviewed on later builds; a review that fails
    publishes the items as queried. The review can correct text from stored sources (a
    cut-off sentence, a fiscal period missing from a title), add a one-line caveat, or hold
    an item out of the file (junk, misattributed, off-topic, materially wrong). Links,
    GUIDs, tickers and dates are never changed, and no figure is published that isn't in
    the stored data. So a file can hold fewer rows than the query returns, and an item
    already published unreviewed can later be corrected or withdrawn when its review runs.

    RSS files (format="rss"; Atom is unchanged) carry what syndication partners (e.g.
    Newstex) read:
    - `<company:symbol>EXCHANGE:TICKER</company:symbol>` per item, from a `company_symbol`
      / `ticker_exchange` column, else from `symbol` plus its listed exchange; non-US
      venue suffixes are dropped (BARC.L -> LSE:BARC).
    - Up to 10 `<category>` tags per item, from `categories`, `category`, `event_type`,
      `document_type`, `doc_type`, `sector` and `industry`.
    - One item per guid: a document listed under several tickers is one story carrying
      every ticker.
    - The channel carries the title "Flexreport Finance: <name>" and one `<copyright>`
      line; each item's description ends with a "Source: Flexreport Finance" line.
    - JSON source links are unwrapped to their URL.

    Confirm the name and schedule with the user before calling. Any valid cron is accepted
    (hourly, every 30 minutes, ranges), but one expression can't give each hour a different
    minute: for 9:30 and 20:00 offer "0,30 9,20 * * *" (also 9:00 and 20:30) or two
    datasets. Limits: 5 datasets per user, and 10 schedules per user shared with
    schedule_task workflows.

    Errors: 404 = that query_token is not a candidate of the caller's own draft; 422 = bad
    cron, a limit reached, a column named `published_at` (reserved — refine the draft to
    rename it), or the candidate no longer runs (draft it again); 503 = the dataset saved
    but its schedule didn't attach — call this again with the same args.

    Returns {dataset_id, dataset_name, saved_query_id, task_name ("ds:{email}:{name}"),
    cron, format, date_mode, schema: [{name, pg_type}], feed_fields, latest_url,
    placeholder_id_to_poll, build_task_id}.
    - `latest_url` is a FIXED link to the newest file with no sign-in (the link itself is
      the key): give it to the user as THE feed URL — for an RSS reader, a syndication
      partner's poller (rss items carry <company:symbol> and up to 10 <category>), Excel /
      Google Sheets web import, or another platform's poller. A dashboard reads the
      published file through get_user_dataset_file, never a replay of `saved_query_id`. It returns 404 until the first
      build writes a file; with build_now that usually takes minutes.
    - `feed_fields` (rss/atom only) shows which column fills each item field {title, link,
      description, pub_date, guid}; tell the user if one fell back (null).
    list_user_datasets(dataset_name=...) shows the build's last_build_status. The schedule
    appears in list_scheduled_tasks as `task_name`; delete_scheduled_task(task_name)
    stops the builds but keeps the dataset and its files; delete_user_data_feed removes
    the dataset entirely.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    return _ok(await _send(ctx, "POST", "/save-user-dataset", json={
        "draft_id": draft_id,
        "query_token": query_token,
        "dataset_name": dataset_name,
        "date_mode": date_mode,
        "cron": cron,
        "format": format,
        "build_now": build_now,
    }))


@mcp.tool(annotations=ToolAnnotations(title="List User Datasets", readOnlyHint=True))
async def list_user_datasets(
    ctx: Context,
    dataset_name: Annotated[
        Optional[str],
        Field(description="One dataset by name; all of the user's when omitted."),
    ] = None,
    start_date: Annotated[
        Optional[str],
        Field(description="First build date (YYYY-MM-DD, ET) of the files to list.", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    end_date: Annotated[
        Optional[str],
        Field(description="Last build date (YYYY-MM-DD, ET) of the files to list.", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    ] = None,
    latest_only: Annotated[
        bool,
        Field(description="True (default) = newest file per build date; false = every version."),
    ] = True,
) -> ListUserDatasetsResult:
    """List the user's own saved datasets (from save_user_dataset) with their build status and download links to the published files.

    New items are reviewed before publishing, up to 40 per build (on a large first build
    the rest publish as queried and are reviewed on later builds; a failed review publishes
    the items as queried). The review can correct text from stored sources, add a one-line
    caveat, or hold an item out; links, GUIDs, tickers and dates are never changed. So a
    file can hold fewer rows than the query returns, and an item already published
    unreviewed can later be corrected or withdrawn.

    Returns {count, datasets: [{name, user_request, output_schema, file_format,
    date_mode, cron, task_name, last_built_at, last_build_status, last_row_count,
    latest_url, files: [{run_date, published_at, url}]}]}. `latest_url` is the fixed,
    sign-in-free link to the newest file — the one to give for "what's my feed URL?" (an rss
    file's items carry <company:symbol> and <category> for syndication partners). To read a
    file's rows (a dashboard, a summary), call get_user_dataset_file. `last_build_status` is written,
    unchanged (same rows as the last file, so no new file), empty, too_large (over 500k
    rows), schema_drift (columns no longer match the saved schema — re-draft and re-save
    under the same name) or failed.

    Each file `url` is a presigned link valid 12 HOURS: hand it to the user as a link; never
    fetch the file into the chat or store the link. Not rate-limited beyond the plan
    quota. Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    if start_date and end_date and end_date < start_date:
        raise ToolError(f"end_date {end_date} is before start_date {start_date}.")
    params: dict[str, Any] = {"latest_only": latest_only}
    if dataset_name:
        params["dataset_name"] = dataset_name
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return _ok(await _send(ctx, "GET", "/list-user-datasets", params=params))


# Formats get_user_dataset_file can turn into rows; parquet and xlsx are binary and go to
# the user as a link instead.
_READABLE_FORMATS = ("json", "jsonl", "csv", "tsv", "xml", "rss", "atom")
_RSS_COMPANY_NS = "{http://purl.org/rss/1.0/modules/company}"
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _parse_dataset_file(fmt: str, body: bytes) -> tuple[list[dict[str, Any]], Optional[str]]:
    """(rows, published_at) from a published dataset file, as the backend's writers lay it out."""
    text = body.decode("utf-8")
    if fmt == "json":
        rows = json.loads(text)
    elif fmt == "jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif fmt in ("csv", "tsv"):
        rows = list(csv.DictReader(io.StringIO(text), delimiter="," if fmt == "csv" else "\t"))
    elif fmt == "xml":
        root = ET.fromstring(body)
        return [{child.tag: child.text for child in row} for row in root.findall("row")], root.get("published_at")
    elif fmt == "rss":
        channel = ET.fromstring(body).find("channel")
        rows = [{
            "title": item.findtext("title"),
            "link": item.findtext("link"),
            "description": item.findtext("description"),
            "pub_date": item.findtext("pubDate"),
            "guid": item.findtext("guid"),
            "categories": [c.text for c in item.findall("category")],
            "company_symbols": [s.text for s in item.findall(f"{_RSS_COMPANY_NS}symbol")],
        } for item in channel.findall("item")]
        return rows, channel.findtext("lastBuildDate")
    else:  # atom
        root = ET.fromstring(body)
        rows = []
        for entry in root.findall(f"{_ATOM_NS}entry"):
            link = entry.find(f"{_ATOM_NS}link")
            rows.append({
                "title": entry.findtext(f"{_ATOM_NS}title"),
                "link": link.get("href") if link is not None else None,
                "description": entry.findtext(f"{_ATOM_NS}content") or entry.findtext(f"{_ATOM_NS}summary"),
                "pub_date": entry.findtext(f"{_ATOM_NS}updated"),
                "guid": entry.findtext(f"{_ATOM_NS}id"),
                "categories": [c.get("term") for c in entry.findall(f"{_ATOM_NS}category")],
                "company_symbols": [],
            })
        return rows, root.findtext(f"{_ATOM_NS}updated")
    # json / jsonl / csv / tsv rows carry the build's published_at column
    stamps = [str(r["published_at"]) for r in rows if isinstance(r, dict) and r.get("published_at")]
    return rows, max(stamps) if stamps else None


@mcp.tool(
    annotations=ToolAnnotations(title="Get User Dataset File", readOnlyHint=True),
    # structured_output=False: a whole file of rows would be sent twice (text + structuredContent).
    structured_output=False,
)
async def get_user_dataset_file(
    ctx: Context,
    dataset_name: Annotated[str, Field(description="The dataset's `name`, as list_user_datasets shows it.")],
    version: Annotated[
        Optional[str],
        Field(description="Omit for the newest file. Else a build date (YYYY-MM-DD, its newest file) or a file's "
                          "exact `published_at` from list_user_datasets."),
    ] = None,
) -> UserDatasetFileResult:
    """Read the rows of one of the user's saved datasets AS PUBLISHED — the file behind its latest_url, or a dated version. THIS is how a dashboard or an answer reads a dataset: the published file is reviewed at publish, point-in-time, and the same bytes every consumer gets. Never replay the dataset's saved query instead (run_saved_query re-runs live SQL: different rows, no review). A claude.ai artifact can't reach flexreportfinapi.com itself, so its page calls this tool.

    Fetches the file server-side and parses json, jsonl, csv, tsv, xml, rss and atom files
    into rows; rss/atom come back one row per item {title, link, description, pub_date,
    guid, categories, company_symbols}. parquet and xlsx files can't be parsed here: give
    the user latest_url instead.

    Returns {dataset_name, format, version, published_at, row_count, rows}. Quote
    `published_at` as the data's as-of. A file over the server's size cap errors instead
    of returning — use the user's own reader on latest_url for that one.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    params: dict[str, Any] = {"dataset_name": dataset_name, "latest_only": True}
    if version:
        params.update(start_date=version[:10], end_date=version[:10], latest_only=len(version) == 10)
    listing = _ok(await _send(ctx, "GET", "/list-user-datasets", params=params))
    dataset = next((d for d in listing.get("datasets") or [] if d.get("name") == dataset_name), None)
    if dataset is None:
        raise ToolError(f"No dataset named {dataset_name!r}; list_user_datasets shows the user's datasets.")
    fmt = dataset.get("file_format")
    if fmt not in _READABLE_FORMATS:
        raise ToolError(f"{dataset_name} is published as {fmt}, which can't be parsed here; give the user its "
                        f"latest_url: {dataset.get('latest_url')}")

    if version:
        files = dataset.get("files") or []
        match = next((f for f in files if version in (f.get("published_at"), f.get("run_date"))), None)
        if match is None:
            raise ToolError(f"{dataset_name} has no file for version {version!r}; list_user_datasets(dataset_name=..., "
                            "latest_only=false) lists its versions.")
        url = match["url"]
    else:
        url = dataset.get("latest_url")
        if not url:
            raise ToolError(f"{dataset_name} has no latest_url yet.")

    # No Authorization header: latest_url is keyed by its own signature, and a presigned S3
    # link rejects a second auth mechanism.
    try:
        resp = await _request_capped("GET", url, {}, MAX_RESPONSE_BYTES)
    except Exception as e:
        raise ToolError(f"Could not fetch {dataset_name}'s file: {e}")
    if isinstance(resp, int):
        raise ToolError(f"{dataset_name}'s file is over {MAX_RESPONSE_BYTES // 1_000_000} MB, too large to return "
                        "here; give the user its latest_url for their own reader.")
    if resp.status_code == 404:
        raise ToolError(f"{dataset_name} has no published file yet (last_build_status: "
                        f"{dataset.get('last_build_status')}); the first build writes it.")
    if resp.status_code >= 400:
        raise ToolError(f"Fetching {dataset_name}'s file returned HTTP {resp.status_code}.")

    try:
        rows, published_at = _parse_dataset_file(fmt, resp.content)
    except Exception as e:
        raise ToolError(f"Could not parse {dataset_name}'s {fmt} file: {e}")
    return UserDatasetFileResult(
        dataset_name=dataset_name,
        format=fmt,
        version=version or "latest",
        published_at=published_at,
        row_count=len(rows),
        rows=rows,
    )


@mcp.tool(annotations=ToolAnnotations(title="Delete User Data Feed", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_user_data_feed(
    ctx: Context,
    dataset_name: Annotated[str, Field(description="The dataset's `name`, as list_user_datasets shows it.")],
) -> DeleteUserDataFeedResult:
    """Delete one of the user's own datasets completely: its schedule, every file it built, its fixed latest_url link, and the saved query behind it. It can't be undone — confirm with the user first, naming the dataset. To only stop the builds and keep the files, use delete_scheduled_task("ds:{email}:{name}") instead.

    Returns {msg, dataset_id, schedule_deleted, files_deleted, saved_query_deleted}.
    Errors: 404 = the user has no dataset of that name; 503 = a step failed partway —
    call again with the same name (every step is safe to repeat).

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    return _ok(await _send(ctx, "DELETE", "/delete-user-data-feed", params={"dataset_name": dataset_name}))
