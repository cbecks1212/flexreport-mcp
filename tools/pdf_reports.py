"""PDF report tools: cached report pulls, plan inventory, full rebuilds, research reports, PDF templates."""

import base64
from typing import Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from core import mcp, _send


# The fields that shape a CUSTOM company report — Report.OVERRIDE_FIELDS on the backend.
# Setting any of them turns /create-full-report into a custom build (~10 min, never
# saved) and the backend rejects them (422) unless `user_override` is also true.
_REPORT_SHAPING_FIELDS = (
    "financial_items", "as_reported_financial_items", "ratios", "revenue_segment",
    "technical_analysis_items", "estimate_items", "institutional_ownership",
    "include_as_report_financials", "as_reported_periods",
)


@mcp.tool(annotations=ToolAnnotations(title="Generate Stock Report", readOnlyHint=False, destructiveHint=False))
async def generate_report_for_stock(
    ctx: Context,
    ticker: str,
    user_override: bool = False,
    financial_items: Optional[list[str]] = None,
    as_reported_financial_items: Optional[list[str]] = None,
    ratios: Optional[list[str]] = None,
    revenue_segment: Optional[list[str]] = None,
    technical_analysis_items: Optional[list[str]] = None,
    estimate_items: Optional[list[str]] = None,
    institutional_ownership: Optional[list[str]] = None,
    include_as_report_financials: bool = False,
    as_reported_periods: Optional[list[str]] = None,
) -> Any:
    """FULL REBUILD of ONE ticker's report (~10 minutes). Use sparingly — never the first call.

    The fast path is `get_latest_report([ticker])`: a symbol with a fresh saved report
    plan comes back under `rendering` and is rendered into the user's template in
    ~10-20 s; any other symbol comes back with its cached PDF and a `stale` flag. This
    tool re-runs the whole pipeline — brief, query planning, SQL, formatting — so it
    is for rebuilds only, in one of two modes:

    STANDARD (ticker only, ~10 min): rebuilds the symbol's report plan from its latest
    event and SAVES it, so later `get_latest_report` calls render it in seconds. Call it
    ONLY after `get_latest_report` returned the ticker with `stale: true` or in
    `missing`, and the user wants the rebuilt report rather than the cached one — tell
    them it takes ~10 minutes before you start it. NEVER for a symbol that came back in
    `rendering` (that render already is the fresh report), and never fanned out across a
    list of names.

    CUSTOM (`user_override=true` + the shaping fields, ~10 minutes, NOT saved): a full
    build from scratch around named line items, ratios, segments, indicators,
    estimates, or holders. Bespoke asks usually do NOT belong here. The better route for
    "a report on X covering Y":
      1. EXPLORE the items the user named — `list_saved_queries` first, then
         `explore_data_catalogue` (after `situate` when the ask is event-driven);
      2. IDEATE with the user over the result sets — what to keep, cut, add, compare;
      3. OVERLAY the agreed results into their template — `get_user_template` returns
         the markup to lay the content out in with your own document tooling (no
         template saved -> offer `draft_user_template`).
    That is faster, iterative, and the user sees the data before it is set in a PDF.
    Use CUSTOM mode only when the user explicitly wants the Flexreport pipeline's own
    report rebuilt around those items and accepts the ~10 minute wait. Every field
    below shapes a custom report, and the
    backend rejects it (422) without `user_override=true` — this tool sets the switch
    for you whenever any of them is non-empty, so a shaped request is never silently
    served the standard report. Vocabularies — never guess, look each one up:
      financial_items              standardized EDGAR line items ("revenue",
                                   "operatingIncome") -> list_options("financial_items")
      ratios                       EDGAR ratios ("grossProfitMargin")
                                   -> list_options("financial_ratios")
      as_reported_financial_items  the filer's OWN tagged XBRL concepts
                                   ("us-gaap_RevenueFromContractWithCustomer...", or a
                                   company extension such as "snow_...")
                                   -> list_options("as_reported_items", ticker=...)
      revenue_segment              the filer's revenue segments ("Product revenue")
                                   -> list_options("revenue_segments", ticker=...)
      technical_analysis_items     indicators to chart ("rsi", "macd")
                                   -> list_options("technical_indicators")
      estimate_items               estimate line items to compare against actuals
      institutional_ownership      managers to feature, by SEC CIK (any width; the
                                   backend pads to 10 digits)
                                   -> list_options("institutional_managers", q="berkshire")
                                   returns {cik, name, category, aum}; pass the `cik`
                                   values, never names
      include_as_report_financials adds an AS-REPORTED statement section built from the
                                   SEC filing itself, every figure a tagged XBRL fact
                                   that deep-links to its location in the filed
                                   document (distinct from the standardized tables,
                                   whose derived figures no filer states). Line items:
                                   `as_reported_financial_items` when given, else
                                   chosen to match the narrative
      as_reported_periods          which window(s) that section shows: "quarter"
                                   (discrete three months, the default), "ytd"
                                   (cumulative), "annual" (fiscal year, from the
                                   10-K). Several render as adjacent columns. Not
                                   cosmetic: a filing states the same concept over
                                   more than one window at the SAME period end, so
                                   the basis must be explicit

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
    result[ticker]["task_id"] and poll it with `get_task_status` until SUCCESS; the
    result carries the finished report as {"pdf": "<base64>", ...}. Requires auth
    (your MCP client attaches the OAuth bearer automatically).
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
    return await _send(
        ctx, "POST", "/create-full-report", json=payload
    )


@mcp.tool(annotations=ToolAnnotations(title="Generate Research Report", readOnlyHint=False, destructiveHint=False))
async def generate_research_report(
    ctx: Context,
    query: str,
    delivery: str = "email",
) -> Any:
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

    `query` is natural language, e.g. "high-growth semis with rising estimates".
    `delivery` defaults to "email". Rate-limited to 20/hour per user server-side.

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll with
    `get_task_status` until SUCCESS, then read its `result`.
    """
    return await _send(
        ctx, "POST", "/generate-research-report",
        json={"query": query, "delivery": delivery},
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Latest Cached Reports", readOnlyHint=True))
async def get_latest_report(
    ctx: Context,
    symbols: list[str],
) -> Any:
    """Get the latest Flexreport research report(s) for one or more tickers. USE THIS BY DEFAULT.

    ===> THIS IS THE DEFAULT, CORRECT TOOL whenever a user asks for "the report",
    "research", "the latest research", "analysis", "a writeup", or "the PDF" for a
    ticker (e.g. "get me the latest research on SNOW"). It is the FAST path. For a
    symbol with a FRESH saved report plan it does not hand back a pre-built PDF: it
    pulls the plan itself — the JSON the platform saved when the symbol's latest
    material event published (brief, queries, results, format map) — and renders it
    straight into the user's template in ~10-20 s. Every other symbol comes back
    with its cached PDF. ALWAYS call this before `generate_report_for_stock`, which
    is a ~10 min full rebuild.

    If the user's intent is an OPEN-ENDED or exploratory QUESTION rather than a request
    for this existing report, do NOT use this tool — default to `explore_data_catalogue`
    (fast, interactive), and reach for `generate_research_report` only when the user
    EXPLICITLY asks for a full deep-dive writeup. A BESPOKE report ("the SNOW report,
    but built around product revenue and RPO") is not a rebuild either: explore the
    items the user named, ideate with them on the result sets, and lay the agreed
    results out in their template (`get_user_template`) with your own document tooling.

    Accepts one OR many symbols. Returns
    {"result":    [{"symbol": "AAPL", "url": "<presigned pdf url>",
                    "report": "<base64 pdf>", "generated_at": "<iso utc>",
                    "age_hours": 5.2, "latest_event_at": "<iso>", "stale": true}, ...],
     "missing":   ["XYZ", ...],
     "rendering": {"PLAY": {"task_id": "...", "status": "PENDING", "symbol": "PLAY"}}}
    Every symbol lands in exactly ONE of the three. A `result` hit carries BOTH
    representations of the same PDF: `url` is a short-lived presigned link (valid
    ~6h) — hand it to the user to download/open the document directly (and prefer it
    on clients that can't handle a large base64 blob); `report` is the inline base64
    PDF — decode it to read, render, or summarize the report's contents yourself.
    Symbols are normalized (uppercased, de-duplicated) by the backend. At most 100
    symbols per call after dedupe (413 above that) — split a longer list.

    READ ALL THREE BEFORE YOU HAND ANYTHING OVER. Nothing regenerates on its own:
      in `rendering` -> the symbol's saved plan is fresh and is being rendered into the
                        user's template (~10-20 s). Poll get_task_status(task_id); the
                        finished result carries {"pdf": "<base64>", ...} — hand the
                        user THAT. Never call generate_report_for_stock for it: the
                        render already is the fresh report.
      stale: false   -> the cached PDF reflects the symbol's latest report inputs. Serve it.
      stale: true    -> the saved plan itself is out of date (a print, a filing, a 13F
                        refresh landed after it; `latest_event_at` says when), so there
                        is nothing fresh to render. Hand over the stale PDF with that
                        caveat and offer generate_report_for_stock(ticker) — a full
                        rebuild, ~10 min. Start it when the user wants the refreshed
                        report; never fan rebuilds out across a list of names unasked.
      stale: null    -> no report inputs are known for the symbol, so freshness cannot
                        be judged. Serve the PDF and quote `generated_at` / `age_hours`.
      in `missing`   -> no plan to render and no cached PDF. generate_report_for_stock(ticker)
                        builds the first one (~10 min, saved so later calls here render
                        it in seconds) — say so before starting it — or `onboard_symbol`
                        if the ticker is not covered.
    To see which symbols have a fresh plan READY to render — by the event that
    triggered it, across the universe, before anything is pulled — use
    `list_available_reports`: it lists saved plans, not cached PDFs.

    Rate-limited to 500/hour. Requires auth (your MCP client attaches the OAuth bearer
    automatically).
    """
    return await _send(
        ctx, "POST", "/get-cached-reports", json=symbols
    )


@mcp.tool(annotations=ToolAnnotations(title="List Available Report Plans", readOnlyHint=True))
async def list_available_reports(
    ctx: Context,
    event_types: Optional[list[str]] = None,
    report_date: Optional[str] = None,
) -> Any:
    """List the symbols with a report plan SAVED on or after `report_date` (default: TODAY) —
    the real-time research get_latest_report can render right now, and whether each renders
    in seconds or needs a rebuild.

    A plan is saved the moment a material event publishes for a symbol (eps_update,
    eps_release, 8k_release, financials_release, ir_publication, transcript_update) and
    nightly for company_update. This tool is the INVENTORY of those plans — it does not
    build or return a report. Returns a list, newest plan first:
      [{"symbol": "NVDA", "event_type": "eps_release",
        "planned_at": "<iso>",   # when the plan was built
        "queued_at": "<iso>",    # the report inputs (the event) it was built from
        "fresh": true,           # see below
        "source": "redis"}, ...] # redis = built within 3 days, s3 = older durable copy
    `fresh` applies the SAME rule get_latest_report (/get-cached-reports) applies:
      fresh: true  -> get_latest_report([ticker]) renders this plan into the user's
                      template in ~10-20 s (it comes back under `rendering`). These are
                      the names to pull.
      fresh: false -> a newer event landed after the plan; get_latest_report returns the
                      stale cached PDF, and only generate_report_for_stock(ticker) — a full
                      rebuild, ~10 min — refreshes it. Say so before the user waits on it.
      not listed   -> no plan IN THE WINDOW. An empty result does not mean there is no
                      research — it means nothing was planned in the window. Widen
                      `report_date` before concluding a symbol has no plan; only then is
                      a first build a full ~10 min run (or `onboard_symbol` if the ticker
                      is not covered).

    TWO WAYS IN:
      1) Event-first — list_realtime_events(event_type=...) shows what just published; call
         this with event_types=[<that type>] to confirm which of those symbols have a plan
         and which are fresh, then get_latest_report(symbols=[...]) for the ones the user
         wants. (An event in the 12h cache with no plan here means the plan is still being
         built — check again in a minute.)
      2) Research-first — skip the events step: call this with no filter for every symbol
         planned TODAY, grouped by the event that triggered it. Plans stay renderable for 7
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
         not the whole list: the fresh ones render in seconds; a stale one comes back as
         its cached PDF. Tell the user which is which, and offer generate_report_for_stock
         rebuilds (~10 min each) only if they want to wait.
    `event_types` accepts any of the plan-earning types above (plus company_update); the
    backend rejects (422) any other type. Omit it for every type. `report_date`
    (YYYY-MM-DD, default today) is a lower bound on when the plan was built — earlier dates
    widen the window and cost more; a malformed date is a 422.

    Not `get_latest_report`: that PULLS the reports for named tickers (renders the fresh
    plans, serves the cached PDF for the rest). This tool answers "for which symbols could
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
    return await _send(ctx, "POST", "/list-available-reports", json=body)


@mcp.tool(annotations=ToolAnnotations(title="Download Report PDF", readOnlyHint=True))
async def download_pdf_from_url(
    ctx: Context,
    url: str,
    file_name: str,
) -> Any:
    """Fetch a presigned S3 PDF URL server-side and return the document as base64.

    Use this to pull the actual PDF bytes for a presigned S3 link — e.g. the `url`
    returned by `get_latest_report` — on clients that can't open the link directly and
    need the document inline. The backend fetches the URL for you (clients never have
    to reach S3) and streams the PDF back. Only S3 URLs are accepted: the host must end
    in "amazonaws.com" and contain "s3"; any other host is rejected with a 400 (SSRF
    guard). NOTE: if you already have `get_latest_report`'s `report` field (the same PDF
    inline as base64), use that directly — there's no need to call this.

    `url` is the presigned S3 link; `file_name` is the download filename to label the
    document (e.g. "AAPL.pdf"). Returns
    {"file_name": ..., "media_type": "application/pdf", "report": "<base64 pdf>"} —
    decode `report` to read, render, or save the PDF.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    pdf = await _send(
        ctx, "POST", "/download-pdf-from-url",
        json={"url": url, "file_name": file_name}, raw=True,
    )
    if isinstance(pdf, dict):  # _send returned a structured error, pass it through
        return pdf
    return {
        "file_name": file_name,
        "media_type": "application/pdf",
        "report": base64.b64encode(pdf).decode("ascii"),
    }


_TemplateType = Literal["add_on", "bespoke"]


@mcp.tool(annotations=ToolAnnotations(title="Draft PDF Template Previews", readOnlyHint=False, destructiveHint=False))
async def draft_user_template(
    ctx: Context,
    template: str,
    template_type: _TemplateType,
    anchor: Optional[str] = None,
) -> Any:
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
      2. Call this. Show the user EVERY variant's `preview_urls` (render the images
         inline where the client can; otherwise give the links) with its `name` and
         `rationale`, side by side.
      3. The user picks one -> `save_user_template(draft_id, variant_id)`. Never save
         a treatment the user has not seen and approved. Later changes go through
         `update_user_template`.

    `template_type`:
      - "add_on"  — restyles ONE section of the standard report layout and keeps the
        rest. `anchor` is REQUIRED: the section it applies to, matched as a substring
        against the document's block keys (e.g. "technical", "financials",
        "ownership"). The previews are rendered at the 360pt column width the block
        will occupy inside a report — anything wider is clipped, not scaled — and
        the treatments restyle it to sit inside the host document (its own page
        background, masthead and disclaimer are stripped).
      - "bespoke" — the template drives the whole document: page chrome, stylesheet
        and the pattern for every content kind, previewed as full pages.

    HTML ONLY: a template is a page design, so the backend rejects markdown (422).

    TEMPLATE RULES (violations come back as a 422 with `detail.errors`):
      - Real CSS engine: grid, flexbox, gradients, absolute positioning all work.
        Add `break-inside: avoid` to a panel that must not split across pages.
      - Only `data:` URIs for images. External images, stylesheets and web fonts are
        BLOCKED; <script>, <link> and similar are stripped.
      - Max 200 KB; must parse as HTML.
      - Write the sample as a complete page in the intended style; its content is
        discarded at save — only the structure and styling are kept.

    Returns {"draft_id", "variants": [{"variant_id", "name", "rationale",
    "preview_urls": [...], "preview_pages"}], "expires_in_seconds", "next_step",
    "warnings"}. `preview_urls` are time-limited presigned PNG links, one per page.
    Read `warnings`: a treatment that "changed too much" was dropped, and an empty
    variant list (HTTP 502) means no usable adaptation survived — revise the
    template rather than retrying the same one.

    Runs a model pass plus a render, so expect several seconds. Requires auth (your
    MCP client attaches the OAuth bearer automatically). Rate-limited 30/min.
    """
    if not (template or "").strip():
        return {"error": "template is required — the HTML to preview."}
    if template_type == "add_on" and not (anchor or "").strip():
        return {"error": "anchor is required for an add_on template — the section it applies to (e.g. 'technical')."}
    return await _send(
        ctx, "POST", "/draft-user-template",
        json={
            "template": template,
            "template_type": template_type,
            "template_format": "html",
            "anchor": anchor,
        },
    )


@mcp.tool(annotations=ToolAnnotations(title="Save PDF Template", readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def save_user_template(
    ctx: Context,
    draft_id: Optional[str] = None,
    variant_id: Optional[str] = None,
    template: Optional[str] = None,
    template_type: Optional[_TemplateType] = None,
    anchor: Optional[str] = None,
) -> Any:
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

    WHAT CHANGES AFTERWARDS: the backend renders every PDF it builds for this user —
    `get_latest_report` renders of fresh plans, `generate_report_for_stock` rebuilds
    (standard and custom) and scheduled reports — through the saved format. The template is a blueprint (stylesheet, page chrome, one
    pattern per content kind) applied to the document's own content: nothing is
    filled in by hand, and no other call changes. When you compose a PDF yourself
    from Flexreport data, `get_user_template` returns the markup to lay it out in.

    Returns {"status": "SAVED", "template_type", "anchor", "bytes", "patterns":
    [content kinds the design styles], "tones": {positive | negative | neutral |
    accent: css class}, "warnings"}. Tell the user which content kinds their design
    styles. A warning "keeping the previously compiled blueprint" means the new
    compile was rejected and the previous blueprint stays in force; the markup
    itself is still saved. Validation failures are a 422 with `detail.errors`
    listing each problem.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    if (draft_id is None) != (variant_id is None):
        return {"error": "draft_id and variant_id go together — pass both (from draft_user_template) or neither."}
    if draft_id is None:
        if not (template or "").strip():
            return {"error": "Nothing to save — pass draft_id + variant_id for a reviewed draft, or the template text itself."}
        if template_type is None:
            return {"error": "template_type is required ('add_on' or 'bespoke') when saving a template directly."}
        if template_type == "add_on" and not (anchor or "").strip():
            return {"error": "anchor is required for an add_on template — the section it applies to (e.g. 'technical')."}

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
    return await _send(ctx, "POST", "/save-user-template", json=payload)


@mcp.tool(annotations=ToolAnnotations(title="Update Saved PDF Template", readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def update_user_template(
    ctx: Context,
    template: Optional[str] = None,
    template_type: Optional[_TemplateType] = None,
    anchor: Optional[str] = None,
    draft_id: Optional[str] = None,
    variant_id: Optional[str] = None,
) -> Any:
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
        return {"error": "draft_id and variant_id go together — pass both (from draft_user_template) or neither."}
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
    return await _send(ctx, "PUT", "/update-user-template", json=body)


@mcp.tool(annotations=ToolAnnotations(title="Get Saved PDF Template", readOnlyHint=True))
async def get_user_template(ctx: Context) -> Any:
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
    return await _send(ctx, "GET", "/get-user-template")


@mcp.tool(annotations=ToolAnnotations(title="Delete Saved PDF Template", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_user_template(ctx: Context) -> Any:
    """Delete the user's saved PDF template so their PDFs go back to the standard layouts.

    Permanent and there is no history: re-creating the format means drafting and
    saving again. Confirm with the user before calling. Ownership is enforced
    server-side, so only the caller's own template can be removed.

    Returns {"msg": "Template deleted. Your PDFs will use the standard layouts."}.
    A 404 means there was nothing saved to delete.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    return await _send(ctx, "DELETE", "/delete-user-template")
