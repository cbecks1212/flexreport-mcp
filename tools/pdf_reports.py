"""PDF report tools: latest-report pulls, plan inventory, on-demand notes, research reports, PDF templates."""

import base64
from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok
from schemas.pdf_reports import (
    CachedReportsResult,
    DeleteTemplateResult,
    DownloadedPdf,
    ReportPlansResult,
    StockReportResult,
    TaskDispatch,
    TemplateTask,
    UserTemplateRecord,
)


# The fields that shape a CUSTOM company report — Report.OVERRIDE_FIELDS on the backend.
# Setting any of them steers /create-full-report's note toward those items, and the
# backend rejects them (422) unless `user_override` is also true.
_REPORT_SHAPING_FIELDS = (
    "financial_items", "as_reported_financial_items", "ratios", "revenue_segment",
    "technical_analysis_items", "estimate_items", "institutional_ownership",
    "include_as_report_financials", "as_reported_periods",
)


@mcp.tool(annotations=ToolAnnotations(title="Generate Stock Report", readOnlyHint=False, destructiveHint=False))
async def generate_report_for_stock(
    ctx: Context,
    ticker: Annotated[str, Field(description="The ONE ticker to write a new note for, e.g. \"SNOW\".")],
    user_override: Annotated[
        bool,
        Field(description="CUSTOM mode switch (steer the note toward the shaping fields; it still replaces the "
                          "symbol's report). Set automatically "
                          "whenever any shaping field is non-empty."),
    ] = False,
    financial_items: Annotated[
        Optional[list[str]],
        Field(description="Standardized EDGAR line items (\"revenue\", \"operatingIncome\") "
                          "from list_options(\"financial_items\")."),
    ] = None,
    as_reported_financial_items: Annotated[
        Optional[list[str]],
        Field(description="The filer's OWN tagged XBRL concepts (\"us-gaap_RevenueFromContractWithCustomer...\", "
                          "or a company extension such as \"snow_...\") from "
                          "list_options(\"as_reported_items\", ticker=...)."),
    ] = None,
    ratios: Annotated[
        Optional[list[str]],
        Field(description="EDGAR ratios (\"grossProfitMargin\") from list_options(\"financial_ratios\")."),
    ] = None,
    revenue_segment: Annotated[
        Optional[list[str]],
        Field(description="The filer's revenue segments (\"Product revenue\") from "
                          "list_options(\"revenue_segments\", ticker=...)."),
    ] = None,
    technical_analysis_items: Annotated[
        Optional[list[str]],
        Field(description="Indicators to chart (\"rsi\", \"macd\") from list_options(\"technical_indicators\")."),
    ] = None,
    estimate_items: Annotated[
        Optional[list[str]],
        Field(description="Estimate line items to compare against actuals."),
    ] = None,
    institutional_ownership: Annotated[
        Optional[list[str]],
        Field(description="Managers to feature, by SEC CIK (any width; the backend pads to 10 digits). Get them "
                          "from list_options(\"institutional_managers\", q=\"berkshire\") -> {cik, name, category, "
                          "aum}; pass the `cik` values, never names."),
    ] = None,
    include_as_report_financials: Annotated[
        bool,
        Field(description="Add an AS-REPORTED statement section built from the SEC filing itself: every figure a "
                          "tagged XBRL fact deep-linked to its location in the filed document (distinct from the "
                          "standardized tables, whose derived figures no filer states). Line items come from "
                          "`as_reported_financial_items` when given, else are chosen to match the narrative."),
    ] = False,
    as_reported_periods: Annotated[
        Optional[list[str]],
        Field(description="Window(s) the as-reported section shows: \"quarter\" (discrete three months, the "
                          "default), \"ytd\" (cumulative), \"annual\" (fiscal year, from the 10-K). Several render "
                          "as adjacent columns. Not cosmetic: a filing states the same concept over more than one "
                          "window at the SAME period end, so the basis must be explicit."),
    ] = None,
) -> StockReportResult:
    """Write a NEW research note for ONE ticker, now (a few minutes). Never the first call.

    The fast path is `get_latest_report([ticker])`: every material event already writes
    the symbol's report as it happens, so it usually exists and comes back in seconds
    with a `stale` flag. This tool writes a new one on demand: the platform's analyst
    researches the symbol's latest episode with its own tools (event web, intraday
    tape, technicals, situate, at most one data exploration), states the platform's
    official stance, and writes the note. The note REPLACES the symbol's report: later
    `get_latest_report` calls return it, and `report_plan` subscribers are notified when
    it lands. Two modes:

    STANDARD (ticker only): call it ONLY after `get_latest_report` returned the ticker
    with `stale: true` or in `missing`, and the user wants a current report rather than
    the cached one — tell them it takes a few minutes before you start it. If
    `latest_event_at` is only minutes old, that event's own report is probably still
    being written: wait for its `report_plan` frame (or call get_latest_report again in
    a few minutes) instead. Never fan it out across a list of names.

    CUSTOM (`user_override=true` + the shaping fields, a few minutes; like STANDARD it
    replaces the symbol's report): the same note, told to cover named line items,
    ratios, segments, indicators, estimates, or holders. Bespoke asks usually do NOT
    belong here. The better route for "a report on X covering Y":
      1. EXPLORE the items the user named — `list_saved_queries` first, then
         `explore_data_catalogue` (after `situate` when the ask is event-driven);
      2. IDEATE with the user over the result sets — what to keep, cut, add, compare;
      3. OVERLAY the agreed results into their template — `get_user_template` returns
         the markup to lay the content out in with your own document tooling (no
         template saved -> offer `draft_user_template`).
    That is faster, iterative, and the user sees the data before it is set in a PDF.
    Use CUSTOM mode only when the user explicitly wants the Flexreport note written
    around those items and accepts the few-minute wait. Every parameter
    after `user_override` shapes a custom report, and the backend rejects it (422)
    without `user_override=true` — this tool sets the switch for you whenever any of
    them is non-empty, so a shaped request is never silently served the standard
    report. Each parameter's description names its vocabulary — never guess, look
    each one up.

    Event context is SERVER-owned: the backend reads the symbol's latest platform event
    and frames the report around it. There is no thesis / change-summary / include_* /
    price-date field any more — do not send editorial steer; unknown keys are ignored.

    CHOOSING THE SHAPING FIELDS FROM THE ONTOLOGY (custom mode only — do not guess):
    read get_company_event_web(ticker) for the latest node, then
    get_event_ontology(event_type=node.type), and derive from the card's family and
    `informs`:
      earnings family          -> financial_items + estimate_items from the fundamentals
                                  and estimates families the card informs; add
                                  include_as_report_financials when the user wants the
                                  filed statement (a 10-Q / 10-K anchored event)
      institutional_ownership  -> institutional_ownership CIKs from the web node's
                                  fetch.cik (a 13F node is FILER-scoped: one filer's
                                  move across several names)
      market_movement / news   -> technical_analysis_items from the technical_indicators
                                  family (validate names via list_options)
      analyst_activity         -> estimate_items + ratios
    Omit anything the ontology gives no reason for.

    Not `generate_research_report`: that answers an open-ended or multi-company
    question (peer comps, a theme). This tool is one ticker — and in custom mode, a
    very clear list of what to cover.

    Asynchronous. Returns {"<TICKER>": {"task_id": "...", "status": "PENDING"}}: read
    result[ticker]["task_id"] and poll it with `get_task_status` until SUCCESS (a few
    minutes). The task result is the note's saved plan, NOT the PDF: on SUCCESS call
    get_latest_report([ticker]) for the document (it comes back `stale: false`). With a
    `report_plan` subscription open, the ticker's frame says the same without polling.
    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    shaping: dict[str, Any] = {
        "financial_items": financial_items,
        "as_reported_financial_items": as_reported_financial_items,
        "ratios": ratios,
        "revenue_segment": revenue_segment,
        "technical_analysis_items": technical_analysis_items,
        "estimate_items": estimate_items,
        "institutional_ownership": institutional_ownership,
        "include_as_report_financials": include_as_report_financials,
        "as_reported_periods": as_reported_periods,
    }
    assert set(shaping) == set(_REPORT_SHAPING_FIELDS)
    payload: dict[str, Any] = {"ticker": ticker}
    # Only non-empty fields travel: the backend defaults every list to [] and every flag
    # to false, and treats a set field as "custom". Any shaped request carries the
    # switch so it is never rejected with a 422 nor quietly served the standard plan.
    payload.update({k: v for k, v in shaping.items() if v})
    if user_override or len(payload) > 1:
        payload["user_override"] = True
    return _ok(await _send(
        ctx, "POST", "/create-full-report", json=payload
    ))


@mcp.tool(annotations=ToolAnnotations(title="Generate Research Report", readOnlyHint=False, destructiveHint=False))
async def generate_research_report(
    ctx: Context,
    query: Annotated[
        str,
        Field(description="The research question in natural language, e.g. \"high-growth semis with rising estimates\"."),
    ],
    delivery: Annotated[str, Field(description="Delivery channel for the finished report, e.g. \"email\" or \"dashboard\".")] = "email",
) -> TaskDispatch:
    """Answer an OPEN-ENDED or THEMATIC research QUESTION (extensible, multi-section).

    ===> THE RIGHT TOOL when the user's intent is a QUESTION rather than a request for an
    existing report — an exploratory or thesis-style ask about a ticker, or a market-wide
    theme not tied to one company (e.g. "Are large caps driving earnings season?",
    "What's the bull/bear case on NVDA?", "high-growth semis with rising estimates").
    For a plain "get me the latest report/research on <ticker>", use `get_latest_report`
    instead. This is the slow, professional DEEP-DIVE — reach for it only when the user
    EXPLICITLY asks for a full writeup, not for an ordinary exploratory question.

    If the user only wants to EXPLORE the data or "use Flexreport to explore...", do NOT
    start here — call `explore_data_catalogue` first (interactive charts/tables, usually a
    few minutes rather than ~10-12) and
    reach for THIS deep-dive only later, once the user has reviewed the exploration and
    EXPLICITLY asks for the full report. Never run both for the same question at once.

    Rate-limited to 20/hour per user server-side.

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll with
    `get_task_status` until SUCCESS, then read its `result`.
    """
    return _ok(await _send(
        ctx, "POST", "/generate-research-report",
        json={"query": query, "delivery": delivery},
    ))


@mcp.tool(
    annotations=ToolAnnotations(title="Get Latest Cached Reports", readOnlyHint=True),
    # structured_output=False: results can be very large (base64 PDFs, task results
    # like the 127 MB one behind the 2026-09-10 OOM), and structured output would
    # send them twice (text + structuredContent). The model is still the documented shape.
    structured_output=False,
)
async def get_latest_report(
    ctx: Context,
    symbols: Annotated[
        list[str],
        Field(description="One or many tickers, e.g. [\"SNOW\", \"AAPL\"]; uppercased and de-duplicated "
                          "server-side, at most 100 after dedupe (413 above that)."),
    ],
) -> CachedReportsResult:
    """Get the latest Flexreport research report(s) for one or more tickers. USE THIS BY DEFAULT.

    ===> THIS IS THE DEFAULT, CORRECT TOOL whenever a user asks for "the report",
    "research", "the latest research", "analysis", "a writeup", or "the PDF" for a
    ticker (e.g. "get me the latest research on SNOW"). It is the FAST path, and it
    only FETCHES: every material event writes the symbol's report as it happens, so
    this returns the latest document in seconds, with metadata saying how current it
    is. Nothing is built here. ALWAYS call this before `generate_report_for_stock`,
    which writes a new note (a few minutes).

    If the user's intent is an OPEN-ENDED or exploratory QUESTION rather than a request
    for this existing report, do NOT use this tool — default to `explore_data_catalogue`
    (fast, interactive), and reach for `generate_research_report` only when the user
    EXPLICITLY asks for a full deep-dive writeup. A BESPOKE report ("the SNOW report,
    but built around product revenue and RPO") is not a new note either: explore the
    items the user named, ideate with them on the result sets, and lay the agreed
    results out in their template (`get_user_template`) with your own document tooling.

    Accepts one OR many symbols. Returns
    {"result":    [{"symbol": "AAPL", "url": "<presigned pdf url>",
                    "report": "<base64 pdf>", "generated_at": "<iso utc>",
                    "age_hours": 5.2, "latest_event_at": "<iso>", "stale": true,
                    "template": "default"}, ...],
     "missing":   ["XYZ", ...],
     "stale_symbols": ["AAPL", ...],
     "rendering": {}}
    Every symbol lands in exactly ONE of `result` / `missing`; `stale_symbols` lists the
    `result` symbols flagged `stale: true`; `rendering` is always empty (kept for older
    clients). `template` is "user" when the PDF is the caller's own copy in their saved
    template, "default" for the platform layout. A `result` hit carries BOTH
    representations of the same PDF: `url` is a short-lived presigned link (valid
    ~6h) — hand it to the user to download/open the document directly (and prefer it
    on clients that can't handle a large base64 blob); `report` is the inline base64
    PDF — decode it to read, render, or summarize the report's contents yourself.
    Split a list longer than 100 symbols across calls.

    READ EACH SYMBOL'S FLAG BEFORE YOU HAND ANYTHING OVER:
      stale: false   -> the PDF reflects the symbol's latest event. Serve it.
      stale: true    -> an event landed after this PDF (`latest_event_at` says when).
                        If that was only minutes ago, the event's own report is probably
                        still being written: say so, and fetch again in a few minutes
                        (or wait for its `report_plan` frame). Otherwise hand over the
                        PDF with that caveat and offer generate_report_for_stock(ticker),
                        which writes a new note in a few minutes. Start it when the user
                        wants it; never fan it out across a list of names unasked.
      stale: null    -> no report inputs are known for the symbol, so freshness cannot
                        be judged. Serve the PDF and quote `generated_at` / `age_hours`.
      in `missing`   -> no report yet. generate_report_for_stock(ticker) writes the first
                        one (a few minutes; later calls here return it) — say so before
                        starting it — or `onboard_symbol` if the ticker is not covered.
    To see which symbols have a CURRENT report — by the event that produced it, across
    the universe, before anything is pulled — use `list_available_reports`.

    Rate-limited to 500/hour. Requires auth (your MCP client attaches the OAuth bearer
    automatically).
    """
    return _ok(await _send(
        ctx, "POST", "/get-cached-reports", json=symbols
    ))


@mcp.tool(annotations=ToolAnnotations(title="List Available Report Plans", readOnlyHint=True))
async def list_available_reports(
    ctx: Context,
    event_types: Annotated[
        Optional[list[str]],
        Field(description="Plan-earning event types to list: eps_update, eps_release, 8k_release, "
                          "financials_release, ir_publication, transcript_update, company_update. Any other "
                          "type is a 422. Omit for every type."),
    ] = None,
    report_date: Annotated[
        Optional[str],
        Field(description="Lower bound on when the plan was built, YYYY-MM-DD (default today). Earlier dates "
                          "widen the window and cost more; plans stay listed for 7 days. A malformed date "
                          "is a 422."),
    ] = None,
) -> ReportPlansResult:
    """List the symbols with a report plan SAVED on or after `report_date` (default: TODAY) —
    the real-time research get_latest_report can hand over right now, and whether each is
    current or has a newer event since.

    A plan is saved the moment a material event publishes for a symbol (eps_update,
    eps_release, 8k_release, financials_release, ir_publication, transcript_update) and
    nightly for company_update. This tool is the INVENTORY of those plans — it does not
    build or return a report. Returns `plans`, newest plan first:
      {"plans": [{"symbol": "NVDA", "event_type": "eps_release",
                  "planned_at": "<iso>",   # when the plan was built
                  "queued_at": "<iso>",    # the report inputs (the event) it was built from
                  "fresh": true,           # see below
                  "source": "redis"}, ...]}  # redis = built within 3 days, s3 = older durable copy
    `fresh` applies the SAME rule get_latest_report (/get-cached-reports) applies:
      fresh: true  -> the report get_latest_report([ticker]) returns was written from
                      this plan and reflects the latest event. These are the names to pull.
      fresh: false -> a newer event landed after the plan. Its own report may still be in
                      progress (minutes); otherwise get_latest_report returns the older PDF
                      flagged stale, and generate_report_for_stock(ticker) writes a new
                      note in a few minutes. Say so before the user waits on it.
      not listed   -> no plan IN THE WINDOW. An empty result does not mean there is no
                      research — it means nothing was planned in the window. Widen
                      `report_date` before concluding a symbol has no plan; only then is
                      a first generate_report_for_stock note (a few minutes) the answer
                      (or `onboard_symbol` if the ticker is not covered).

    TWO WAYS IN:
      1) Event-first — list_realtime_events(event_type=...) shows what just published; call
         this with event_types=[<that type>] to confirm which of those symbols have a plan
         and which are fresh, then get_latest_report(symbols=[...]) for the ones the user
         wants. (An event in the 12h cache with no plan here means the plan is still being
         built — check again in a minute.)
      2) Research-first — skip the events step: call this with no filter for every symbol
         planned TODAY, grouped by the event that triggered it. Plans stay listed for 7
         days, so pass report_date="YYYY-MM-DD" (e.g. 3 days back) when the user wants the
         running inventory rather than today's, or when today's list comes back thin.

    PICKING THE MOST PERTINENT PLANS ("pull the most relevant reports today", "what's worth
    reading right now"): the list is an inventory, not a ranking — a nightly company_update
    plan sits next to a transcript that just landed. Rank by the EVENT, not by the plan:
      a) FILTER on what published — list_realtime_events for the material types
         (transcript_update, 8k_release, ir_publication, eps_release / eps_update,
         financials_release) and for the tape (biggest_mover, biggest_loser,
         biggest_gainer). A symbol on BOTH lists — a filing or call AND a move — is
         the strongest candidate.
      b) ANALYZE the content — read the event payloads (what the 8-K discloses, what the
         call said, what the deck guides to) and keep the ones that change something;
         `situate(question=...)` and get_event_ontology say what each type means.
      c) CONFIRM with intraday price action — detect_intraday_outlier_jumps(tickers=[...])
         (and get_aftermarket_data after the close) shows whether the market treated it
         as significant; a print with no reaction ranks below a mover with a filing.
      d) MATCH to plans — call this tool with event_types=[<the types from a>] and keep
         the candidates that appear, preferring `fresh: true`; `queued_at` should be the
         event you just read.
      e) PULL the winners — get_latest_report(symbols=[...]) for the few that survived,
         not the whole list: every one comes back in seconds, flagged fresh or stale.
         Tell the user which is which, and offer generate_report_for_stock (a few minutes
         each) only if they want to wait.

    Not `get_latest_report`: that PULLS the reports for named tickers (each one's latest
    document). This tool answers "for which symbols could
    I get a fresh report?" — by event, across the universe — before anything is pulled. Not
    `generate_research_report`: that is the broad, topical, or multi-company writeup;
    the plans listed here are single-symbol, event-anchored reports.

    Synchronous, cheap (500/hour). Requires auth (your MCP client attaches the OAuth
    bearer automatically).
    """
    body: dict[str, Any] = {}
    if event_types:
        body["event_types"] = event_types
    if report_date:
        body["report_date"] = report_date
    plans = _ok(await _send(ctx, "POST", "/list-available-reports", json=body))
    return ReportPlansResult(plans=plans)


@mcp.tool(
    annotations=ToolAnnotations(title="Download Report PDF", readOnlyHint=True),
    # structured_output=False: results can be very large (base64 PDFs, task results
    # like the 127 MB one behind the 2026-09-10 OOM), and structured output would
    # send them twice (text + structuredContent). The model is still the documented shape.
    structured_output=False,
)
async def download_pdf_from_url(
    ctx: Context,
    url: Annotated[
        str,
        Field(description="Presigned S3 link, e.g. get_latest_report's `url`. The host must end in "
                          "\"amazonaws.com\" and contain \"s3\" (anything else is a 400)."),
    ],
    file_name: Annotated[str, Field(description="Download filename to label the document, e.g. \"AAPL.pdf\".")],
) -> DownloadedPdf:
    """Fetch a presigned S3 PDF URL server-side and return the document as base64.

    Use this to pull the actual PDF bytes for a presigned S3 link — e.g. the `url`
    returned by `get_latest_report` — on clients that can't open the link directly and
    need the document inline. The backend fetches the URL for you (clients never have
    to reach S3) and streams the PDF back. Only S3 URLs are accepted: the host must end
    in "amazonaws.com" and contain "s3"; any other host is rejected with a 400 (SSRF
    guard). NOTE: if you already have `get_latest_report`'s `report` field (the same PDF
    inline as base64), use that directly — there's no need to call this.

    Returns {"file_name": ..., "media_type": "application/pdf", "report": "<base64 pdf>"}
    — decode `report` to read, render, or save the PDF.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    # raw=True: bytes on success, _send's error dict otherwise (raised by _ok)
    pdf = _ok(await _send(
        ctx, "POST", "/download-pdf-from-url",
        json={"url": url, "file_name": file_name}, raw=True,
    ))
    return DownloadedPdf(
        file_name=file_name,
        media_type="application/pdf",
        report=base64.b64encode(pdf).decode("ascii"),
    )


_TemplateType = Literal["add_on", "bespoke"]


@mcp.tool(annotations=ToolAnnotations(title="Draft PDF Template Previews", readOnlyHint=False, destructiveHint=False))
async def draft_user_template(
    ctx: Context,
    template: Annotated[
        str,
        Field(description="The template as a complete HTML page in the intended style (max 200 KB; markdown is "
                          "a 422). Its sample content is discarded at save — only structure and styling are kept."),
    ],
    template_type: Annotated[
        _TemplateType,
        Field(description="\"add_on\" restyles ONE section of the standard report and keeps the rest; "
                          "\"bespoke\" drives the whole document."),
    ],
    anchor: Annotated[
        Optional[str],
        Field(description="REQUIRED for add_on: the section it applies to, matched as a substring against the "
                          "document's block keys (e.g. \"technical\", \"financials\", \"ownership\"). Omit for bespoke."),
    ] = None,
) -> TemplateTask:
    """Render preview images of a user's PDF template so they can approve it BY LOOKING, before anything is saved.

    THE POINT: a user wants their research in THEIR format — their masthead, colours,
    layout, branding. A saved template is a BLUEPRINT: the author's stylesheet and
    page chrome plus one markup pattern per content kind, which the backend applies
    automatically to every PDF it renders for that user. A template is a visual
    choice, so it is signed off by looking at rendered pages, never by describing
    them. This is STEP 1 OF 2: it compiles the submitted HTML into a blueprint under
    several treatments and renders each one as PNG previews of the platform's
    SPECIMEN DOCUMENT — one of every content kind (title, lead, sections, bullets,
    KPI cards, tables, charts, figure, source line, footnote). The user signs off on
    how the design treats each kind of content, not on sample text. NOTHING IS
    STORED — the draft lives in a 24-hour cache; `save_user_template` is step 2.

    THE FLOW:
      1. Author the HTML with the user (or take the page they hand you).
      2. Call this, then poll `get_task_status(task_id)` until SUCCESS. Show the user
         EVERY variant's `preview_urls` (render the images
         inline where the client can; otherwise give the links) with its `name` and
         `rationale`, side by side.
      3. The user picks one -> `save_user_template(draft_id, variant_id)`. Never save
         a treatment the user has not seen and approved. Later changes go through
         `update_user_template`.

    `template_type`:
      - "add_on"  — the previews are rendered at the 360pt column width the block
        will occupy inside a report — anything wider is clipped, not scaled — and
        the treatments restyle it to sit inside the host document (its own page
        background, masthead and disclaimer are stripped).
      - "bespoke" — the template drives the whole document: page chrome, stylesheet
        and the pattern for every content kind, previewed as full pages.

    TEMPLATE RULES (violations come back as a 422 with `detail.errors`):
      - Real CSS engine: grid, flexbox, gradients, absolute positioning all work.
        Add `break-inside: avoid` to a panel that must not split across pages.
      - Only `data:` URIs for images. External images, stylesheets and web fonts are
        BLOCKED; <script>, <link> and similar are stripped.
      - HTML only, max 200 KB; must parse as HTML.

    Asynchronous: returns {"task_id", "status": "PENDING", "next_step"}. Poll
    `get_task_status(task_id)`; the finished result is {"draft_id", "variants":
    [{"variant_id", "name", "rationale", "preview_urls": [...], "preview_pages"}],
    "expires_in_seconds", "next_step", "warnings"} (or {"error", "warnings"} when the
    draft failed). `preview_urls` are time-limited presigned PNG links, one per page.
    Read `warnings`: a treatment that "changed too much" was dropped, and an empty
    variant list means no usable adaptation survived — revise the template rather
    than retrying the same one.

    Runs a model pass plus a render per treatment, so expect minutes. Requires auth
    (your MCP client attaches the OAuth bearer automatically). Rate-limited 30/min.
    """
    if not (template or "").strip():
        raise ToolError("template is required — the HTML to preview.")
    if template_type == "add_on" and not (anchor or "").strip():
        raise ToolError("anchor is required for an add_on template — the section it applies to (e.g. 'technical').")
    return _ok(await _send(
        ctx, "POST", "/draft-user-template",
        json={
            "template": template,
            "template_type": template_type,
            "template_format": "html",
            "anchor": anchor,
        },
    ))


@mcp.tool(annotations=ToolAnnotations(title="Save PDF Template", readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def save_user_template(
    ctx: Context,
    draft_id: Annotated[
        Optional[str],
        Field(description="Signed-off path: the draft_id from draft_user_template's finished task. Pass with variant_id."),
    ] = None,
    variant_id: Annotated[
        Optional[str],
        Field(description="Signed-off path: the variant_id the user chose from the previews. Pass with draft_id."),
    ] = None,
    template: Annotated[
        Optional[str],
        Field(description="Direct path only: the HTML template (markdown is a 422). Omit on the signed-off path — "
                          "the draft supplies it."),
    ] = None,
    template_type: Annotated[
        Optional[_TemplateType],
        Field(description="Direct path only (required there): \"add_on\" or \"bespoke\"."),
    ] = None,
    anchor: Annotated[
        Optional[str],
        Field(description="Direct path, add_on only (required there): the section it applies to, e.g. \"technical\"."),
    ] = None,
) -> TemplateTask:
    """Save the user's PDF format — one template per account, applied automatically to every PDF the backend renders for them from now on.

    STEP 2 OF 2 after `draft_user_template`. Two ways to call it:

    SIGNED-OFF (the normal path): pass `draft_id` and the `variant_id` the user chose
    from the previews. The EXACT treatment they looked at is stored — no second
    adaptation pass that could produce something they never saw. `template`,
    `template_type` and `anchor` are taken from the draft and may be omitted. A 404
    means the draft expired (24h) or the variant does not exist: draft again.

    DIRECT (no previews): pass `template` + `template_type` (+ `anchor` for an
    add_on) — only when the user explicitly wants to skip the previews. An add_on
    saved this way still gets an unreviewed harmonise pass, so prefer the signed-off
    path for anything visual. HTML only: markdown is rejected with a 422.

    ONE TEMPLATE PER USER — this is an UPSERT: saving replaces whatever was saved
    before, with no history. Call `get_user_template` first and confirm with the user
    before overwriting an existing format. To change a saved format afterwards, use
    `update_user_template` (any field, or none to recompile it as stored).

    WHAT CHANGES AFTERWARDS: the backend lays out this user's reports in the saved
    format — every event-time report gets a copy in their template (get_latest_report
    serves it, `template: "user"`), as do scheduled reports. The template is a blueprint (stylesheet, page chrome, one
    pattern per content kind) applied to the document's own content: nothing is
    filled in by hand, and no other call changes. When you compose a PDF yourself
    from Flexreport data, `get_user_template` returns the markup to lay it out in.

    Asynchronous: returns {"task_id", "status": "PENDING", "next_step"}. Poll
    `get_task_status(task_id)`; the finished result is {"status": "SAVED",
    "template_type", "anchor", "bytes", "patterns": [content kinds the design styles],
    "tones": {positive | negative | neutral | accent: css class}, "warnings"}. Tell
    the user which content kinds their design styles. A warning "keeping the
    previously compiled blueprint" means the new compile was rejected and the
    previous blueprint stays in force; the markup itself is still saved. Validation
    failures are a 422 with `detail.errors` listing each problem.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    if (draft_id is None) != (variant_id is None):
        raise ToolError("draft_id and variant_id go together — pass both (from draft_user_template) or neither.")
    if draft_id is None:
        if not (template or "").strip():
            raise ToolError("Nothing to save — pass draft_id + variant_id for a reviewed draft, or the template text itself.")
        if template_type is None:
            raise ToolError("template_type is required ('add_on' or 'bespoke') when saving a template directly.")
        if template_type == "add_on" and not (anchor or "").strip():
            raise ToolError("anchor is required for an add_on template — the section it applies to (e.g. 'technical').")

    # The backend body model requires `template` and `template_type` even on the
    # sign-off path, where the draft supplies both and overrides whatever is sent —
    # so send neutral fillers there rather than making the agent resend the markup.
    payload: dict[str, Any] = {
        "template": template or "",
        "template_type": template_type or "add_on",
        "template_format": "html",
        "anchor": anchor,
    }
    if draft_id is not None:
        payload["draft_id"] = draft_id
        payload["variant_id"] = str(variant_id)
    return _ok(await _send(ctx, "POST", "/save-user-template", json=payload))


@mcp.tool(annotations=ToolAnnotations(title="Update Saved PDF Template", readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def update_user_template(
    ctx: Context,
    template: Annotated[
        Optional[str],
        Field(description="New HTML template without previews (markdown is a 422). Omit to keep the stored one."),
    ] = None,
    template_type: Annotated[
        Optional[_TemplateType],
        Field(description="New type, \"add_on\" or \"bespoke\". Omit to keep the stored one."),
    ] = None,
    anchor: Annotated[
        Optional[str],
        Field(description="New add_on section, e.g. \"technical\". Omit to keep the stored one."),
    ] = None,
    draft_id: Annotated[
        Optional[str],
        Field(description="Visual change: the draft_id from draft_user_template's finished task. Pass with variant_id."),
    ] = None,
    variant_id: Annotated[
        Optional[str],
        Field(description="Visual change: the variant_id the user chose from the previews. Pass with draft_id."),
    ] = None,
) -> TemplateTask:
    """Change your saved PDF template — any field, or none to recompile it as stored.

    Complements `draft_user_template` (preview), `save_user_template` (create) and
    `delete_user_template` (remove). Every argument is optional; a field you omit
    keeps its stored value, and the result is compiled into a blueprint again:
      - a VISUAL change: draft the new page with `draft_user_template`, show the
        previews, then pass the chosen `draft_id` + `variant_id` here (both or
        neither);
      - a change without previews: pass `template`, `template_type` and/or
        `anchor` directly (HTML only — markdown is rejected with a 422);
      - NO arguments: recompiles the stored template with the current compiler —
        how a saved design picks up an improved compile without resubmitting it.

    Async: returns {"task_id", "status": "PENDING", "changed": [the fields that
    were updated], "next_step"} — poll `get_task_status` for {"status": "SAVED",
    "patterns", "tones", "warnings"}. A 404 means nothing is saved yet (use
    `save_user_template` first); a 422 lists validation errors in `detail.errors`.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    if (draft_id is None) != (variant_id is None):
        raise ToolError("draft_id and variant_id go together — pass both (from draft_user_template) or neither.")
    body: dict[str, Any] = {}
    if template is not None:
        body["template"] = template
        body["template_format"] = "html"
    if template_type is not None:
        body["template_type"] = template_type
    if anchor is not None:
        body["anchor"] = anchor
    if draft_id is not None:
        body["draft_id"] = draft_id
        body["variant_id"] = str(variant_id)
    return _ok(await _send(ctx, "PUT", "/update-user-template", json=body))


@mcp.tool(annotations=ToolAnnotations(title="Get Saved PDF Template", readOnlyHint=True))
async def get_user_template(ctx: Context) -> UserTemplateRecord:
    """Return the PDF template this user has saved, or a 404 if they have none.

    Call it to answer "what format am I on?", BEFORE `save_user_template` when a
    template may already exist (saving replaces it without history, so the user
    should know what they are overwriting), and whenever you are about to compose
    a PDF yourself from Flexreport data: lay the content out in this markup so the
    document matches the user's format. A 404 there is the cue to offer drafting
    one (`draft_user_template` -> previews -> `save_user_template`).

    Returns {"template_type", "template_format", "anchor", "template", "created_at",
    "updated_at"}. `template` is the sanitized markup as submitted (up to 200 KB) —
    the compiled blueprint the backend renders from is not returned. To change the
    format, draft again and pass the chosen variant to `update_user_template`
    rather than editing this text blind.

    An HTTP 404 means no template is saved — the standard layouts are in force. An
    HTTP 503 means the backend's user_templates table has not been created yet, NOT
    that nothing is saved; report that difference.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    return _ok(await _send(ctx, "GET", "/get-user-template"))


@mcp.tool(annotations=ToolAnnotations(title="Delete Saved PDF Template", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_user_template(ctx: Context) -> DeleteTemplateResult:
    """Delete the user's saved PDF template so their PDFs go back to the standard layouts.

    Permanent and there is no history: re-creating the format means drafting and
    saving again. Confirm with the user before calling. Ownership is enforced
    server-side, so only the caller's own template can be removed.

    Returns {"msg": "Template deleted. Your PDFs will use the standard layouts."}.
    A 404 means there was nothing saved to delete.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    return _ok(await _send(ctx, "DELETE", "/delete-user-template"))
