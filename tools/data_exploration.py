"""Data exploration tools: catalogue exploration, coverage, signed drilldowns, source tracing, saved queries."""

import json
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from core import mcp, _send


@mcp.tool(annotations=ToolAnnotations(title="Explore Data Catalogue", readOnlyHint=False, destructiveHint=False))
async def explore_data_catalogue(
    ctx: Context,
    query: str,
) -> Any:
    """Explore Flexreport's data platform with an OPEN-ENDED question — designed to handle many research based or open ended questions, enabling interactive EDA.

    ===> BEFORE running this, call `list_saved_queries` and match the question against
    the saved rows — on each row's `metadata` (`summary`, `parameters`, `datasets`),
    not on the `user_request` label, skipping any row marked `stale`. A row of the
    same SHAPE — same metric, same window, only the company / filer / date / count
    differs (saved: "the past 8 quarters of sales for TGT"; asked now: "KSS's last 8
    quarters of sales") — is answered by `run_saved_query(query_tokens=<that row's
    query_placeholder>, values={...}, max_rows=500)` in seconds, the values keyed by
    `metadata.parameters`: the backend re-aims the saved queries at the new entities,
    no planning job, no draw on the 20/hour budget. An EXACT repeat replays the row's
    `queries` tokens as saved. Only when nothing saved fits, or the replay answers 422
    (the request could not be mapped onto the saved slots), run this tool.

    ===> AFTER the exploration completes and the results are shown, offer ONCE to save
    it with `save_user_query`: tell the user the request can then be re-run exactly
    (same queries, live data) OR re-asked for other companies, filers, or windows in
    seconds. Offer when the exploring is done, not mid-iteration, and never save by
    reflex.

    ===> For anything EVENT-DRIVEN (a print, a filing, a 13F move, news, "what
    happened"), call `situate(...)` first and take this tool's `query` from its plan —
    the plan names the relations that HAVE refreshed since the event and omits the
    ones that have not. Otherwise:

    ===> THE DEFAULT, FIRST-STEP tool whenever the user wants to EXPLORE the data or
    understand a topic from the data (e.g. "use Flexreport to explore...", "how are
    small caps doing — explore the data", "how have semiconductor margins trended?").
    It validates the request, plans queries against the data catalogue, runs them, and
    returns the raw result sets to render as INTERACTIVE CHARTS AND TABLES on the
    dashboard. Coverage questions are NOT this tool: for an enumeration ("which
    symbols / sectors / indicators are available?", "is X covered?") use
    `list_options` — instant, no async job; for a coverage COUNT or breakdown ("how
    many symbols are in the investor relations dataset?", "what's the count by
    sector?") use `explore_data_coverage`, the dedicated public tool. Reach for THIS
    tool when the user wants the DATA itself — values, history, trends, comparisons.

    *** RUN THIS BY ITSELF FIRST. DO NOT ALSO LAUNCH `generate_research_report` FOR THE
    SAME QUESTION. *** These two tools are SEQUENTIAL STEPS, never parallel:
      1. `explore_data_catalogue` (this tool) — the LEAN route: a validate -> plan -> run
         pipeline with no report-rendering step. Usually finishes in about a minute, but
         run time varies with the tables queried and can reach ~5 minutes. Poll it, SHOW
         the user the resulting charts/tables, and let them iterate.
      2. `generate_research_report` — the slow (~10-12 min), professional analyst-grade
         deep-dive. Reach for it ONLY LATER, AFTER the user has seen the exploration and
         EXPLICITLY asks for the full report. Firing both at once wastes a ~10-min job,
         burns the 20/hour budget, and produces confusing duplicate polling.
    There is no `delivery` argument here: results always go to the dashboard so the user
    can interact with them. The job is rate-limited to 20/hour per user server-side.

    `query` is natural language.

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll a SINGLE task
    with `get_task_status` until SUCCESS (the result is a plain dict — there is NO nested
    task to chase). Expect roughly 1-5 minutes depending on the tables queried — a task
    still PENDING after a couple of minutes is normal, so keep polling. On SUCCESS,
    `result` is:
      {"user_query": "...", "delivery": "dashboard",
       "results": {"<query name>": {"description": "...", "columns": [...],
                                     "rows": [...the FULL result set...],
                                     "row_count": N}, ...}}
    `rows` is NOT sampled — it carries every row (`row_count` matches len(rows)), so
    charts/tables reflect the whole population.

    Each entry ALSO carries `query_token`: the planned SQL sealed into an opaque token.
    It is not readable and never for display — its one use is `save_user_query`, which
    stores the tokens so `run_saved_query` can replay THIS EXACT request later in
    seconds instead of re-planning it. Saving also makes the backend author a
    PLACEHOLDER twin of each query plus a `metadata` slot schema, so the same request
    can be re-asked for another company, filer, or window by passing typed `values`
    for those slots. That is what to offer once the exploring is done (see the top of
    this docstring); a saved row also spares the 20/hour budget.

    CITE WHAT YOU PUBLISH: a result's `columns`/`rows` may carry `_source_*` columns —
    `_source_url`, `_source_filed_at`, `_source_document_id`, `_source_citations` —
    alongside a `sources` block naming each relation, its grain, and whether it is
    `traceable` per "row" or only as a "cohort". That is the provenance of the numbers,
    not padding: quote `_source_url` when the user asks where a figure came from, and for anything
    you are about to publish — or the moment they ask for the filing, the quote, or
    "how do you know" — pass `sources.relations[].relation` plus each row's
    identifying columns (the grain AND its date) to `trace_data_sources`, which
    resolves them to the source document, the
    section, or the verified quote behind the number. `get_document_section` then opens
    the exact speaker turn a transcript citation points at.

    Render each entry as a chart/table for the user. If the request can't be served from
    the platform's data, `result` instead carries a `validation_status` of "REJECTED"
    (with a reason) — relay that rather than retrying blindly.

    EVENT-DRIVEN QUESTIONS: when the question is about something that just HAPPENED
    (an earnings print, a 13F move, a downgrade, a news item), consult
    get_event_ontology(event_type=...) BEFORE writing the query and name the relations
    from its `entails.<layer>` in the query text (prefer gold / analysis relations over
    base tables). Skip any relation whose refreshed_by[].schedule[].next_run_at is
    after the event — it does not hold the event yet and the query will show the prior
    period as if it were the reaction.
    """
    return await _send(
        ctx, "POST", "/data-catalogue-exploration",
        json={"query": query},
    )


@mcp.tool(annotations=ToolAnnotations(title="Explore Data Coverage", readOnlyHint=False, destructiveHint=False))
async def explore_data_coverage(
    ctx: Context,
    query: str,
) -> Any:
    """Answer a question about WHAT THE PLATFORM COVERS — how many symbols a dataset holds, broken down any way you like.

    ===> THE TOOL for coverage COUNTS and BREAKDOWNS: "how many symbols are covered in
    the investor relations dataset?", "what's that count by sector — how many tech
    names?", "how many companies have earnings transcripts / 13F ownership / insider
    activity / analyst estimates?", "how far back does the fundamentals history go?",
    "what's your UK coverage by sector?". It plans queries over BOTH the catalogue's
    own metadata and the datasets themselves, so it can count what no enumeration
    endpoint knows: which symbols actually carry a PARTICULAR dataset, grouped by
    sector, industry, country, market cap, or year.

    PUBLIC at the backend — no account, plan, or entitlement is needed to answer a
    coverage question, by design: a prospect evaluating the platform should be able to
    ask what it covers. (The MCP connection itself still carries the client's OAuth
    session — that gate is transport-wide, not this tool's.) Never treat a coverage
    question as plan-gated or quota-gated, and never tell the user it needs an upgrade.

    Pick between the three coverage-adjacent routes by what the answer looks like:
      - a LIST of what exists ("which sectors / indicators are available?", "is X
        covered?") -> `list_options`. Instant, no async job.
      - a COUNT or breakdown of coverage -> THIS tool.
      - the DATA itself (values, history, trends, comparisons) ->
        `explore_data_catalogue`.
    Do NOT answer a coverage count from memory, from the "2,900+ companies / ~420
    indices" headline figures, or by eyeballing the length of a `list_options`
    payload — those are universe-wide totals and say nothing about per-dataset
    coverage. Run this tool.

    There is no `delivery` argument: results always go to the dashboard so the user can
    interact with them. Rate-limited to 20/hour server-side.

    `query` is natural language — pass the coverage question as asked ("count the
    distinct symbols in the investor relations dataset, broken out by sector").

    Asynchronous: returns {"task_id": "...", "status": "PENDING"}. Poll a SINGLE task
    with `get_task_status` until SUCCESS (the result is a plain dict — there is NO
    nested task to chase). Expect roughly 1-5 minutes depending on the tables queried —
    a task still PENDING after a couple of minutes is normal, so keep polling. On
    SUCCESS, `result` is:
      {"user_query": "...", "delivery": "dashboard",
       "results": {"<query name>": {"description": "...", "columns": [...],
                                     "rows": [...the FULL result set...],
                                     "row_count": N}, ...}}
    `rows` is NOT sampled or truncated — every bucket of a breakdown comes back
    (`row_count` matches len(rows)), so the totals you report are the real ones.
    Render each entry as a chart/table for the user. If the request can't be served
    from the platform's data, `result` instead carries a `validation_status` of
    "REJECTED" (with a reason) — relay that rather than retrying blindly.
    """
    return await _send(
        ctx, "POST", "/data-coverage-exploration",
        json={"query": query}, require_auth=False,
    )


def _query_token(reference: str) -> str:
    """Normalise a server-minted query reference to its bare `t` token.

    Accepts either the bare token or a full signed URL (`signed_query_url`,
    `source_url`). Fernet tokens never contain "?", so a query string reliably
    marks the URL form.
    """
    token = reference.strip()
    if "?" in token:
        token = parse_qs(urlsplit(token).query).get("t", [token])[0]
    return token


def _replay_args(values: Any = None, max_rows: Any = None) -> tuple[dict[str, Any], Optional[str]]:
    """Normalise the optional replay arguments the two /query-data tools share.

    Returns (extra query params, error message). `values` travels as a JSON object
    string keyed by slot name: a dict is serialized here, and a client that already
    serialized it is passed through once it parses back to an object. `max_rows` must
    be a positive integer. Neither is ever allowed to carry SQL — they only fill the
    typed slots the backend minted into the token.
    """
    extra: dict[str, Any] = {}

    if values not in (None, "", {}):
        payload: Any = values
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return {}, ('`values` must be a JSON object of {slot name: value} built from the row\'s '
                            '`metadata.parameters` — e.g. {"symbol": "KSS", "quarters": 8}.')
        if not isinstance(payload, dict):
            return {}, ('`values` must be a JSON object keyed by slot name — e.g. '
                        '{"symbol": "KSS", "managers": [{"id": "0001454027", "name": "Verition Fund Management LLC"}]}.')
        if payload:
            extra["values"] = json.dumps(payload)

    if max_rows is not None:
        try:
            cap = int(max_rows)
        except (TypeError, ValueError):
            return {}, "`max_rows` must be a positive integer, e.g. 500."
        if cap < 1:
            return {}, "`max_rows` must be a positive integer, e.g. 500."
        extra["max_rows"] = cap

    return extra, None


@mcp.tool(annotations=ToolAnnotations(title="Run Signed Drilldown Query", readOnlyHint=True))
async def get_signed_sql_drilldown(
    ctx: Context,
    encrypted_query_token: str,
    prompt: Optional[str] = None,
    values: Optional[dict] = None,
    max_rows: Optional[int] = None,
) -> Any:
    """Fetch the rows behind an event-web drilldown from its server-minted query token.

    Some `get_company_event_web` nodes carry `signed_query_url` in their `fetch` hint —
    an opaque, tamper-proof link the backend minted for exactly that node. This tool is
    the zero-planning rung of the chain: no query to compose, the rows come back
    directly. Pass either the full `signed_query_url` or just its `t` parameter; the
    URL form is unwrapped automatically. Tokens are minted server-side only — NEVER
    construct, guess, or modify one, and never pass SQL here. A token that came from a
    SAVED request rather than the event web belongs to `run_saved_query` instead — same
    endpoint, but it replays every query in the saved row in one call.

    Returns {"format": "table", "count": N, "rows": [...]} — rows only, never the
    underlying SQL. For contextual drilldowns the NEW record is flagged
    `is_new_record: true` and the remaining rows are the prior/context records to read
    it against (the node's `signed_query_context` describes what they are).

    CITE WHAT YOU PUBLISH: the rows may carry `_source_*` columns — `_source_url`,
    `_source_filed_at`, `_source_document_id`, `_source_citations` — and the response a
    `sources` block naming each relation, its grain, and whether it is `traceable` per
    "row" or only as a "cohort". That is the provenance of the numbers, not padding:
    quote `_source_url` when the user asks where a figure came from, and for anything
    you are about to publish — or the moment they ask for the filing, the quote, or
    "how do you know" — pass `sources.relations[].relation` plus each row's
    identifying columns (the grain AND its date) to `trace_data_sources`, which
    resolves them to the source document, the
    section, or the verified quote behind the number. `get_document_section` then opens
    the exact speaker turn a transcript citation points at.

    HTTP 403 means the token is invalid or tampered — do not retry with an altered
    token; re-fetch the event web for a fresh link instead.

    `values` and `prompt` (both optional) re-aim a token that is a PLACEHOLDER TWIN at
    other entities; leave both out for a plain event-web link and the rows come back
    unchanged. `values` is the preferred form — a JSON object of {slot name: typed
    value} that fills those slots deterministically, with no language model in the
    loop. `prompt` ("the same rows for KSS") fills whatever `values` did not, from the
    wording. Either way the response carries `parameters` (what was substituted)
    alongside `rows`; echo those to the user. A 422 means the request could not be
    mapped onto the token's slots. The full slot vocabulary of a SAVED row lives in
    `list_saved_queries` -> `metadata.parameters`, and replaying a saved row is
    `run_saved_query`, not this tool.

    `max_rows` (optional, e.g. 500) caps the rows serialized back; the response then
    carries `row_count` (the full count) and `truncated: true`. Pass it on any
    drilldown that can fan out to constituents — an uncapped one can return tens of
    thousands of rows and stall the client for minutes.

    Requires auth (your MCP client attaches the OAuth bearer automatically) — unlike
    `get_company_event_web`, which is public: the web hands out the LINK to anyone, but
    following it to the underlying rows is for signed-in users. A signed-out caller gets
    a not-authenticated error, not rows; the fix is signing in, never a different token.
    """
    extra, err = _replay_args(values, max_rows)
    if err:
        return {"error": err}
    params: dict[str, Any] = {"t": _query_token(encrypted_query_token), **extra}
    if prompt and prompt.strip():
        params["prompt"] = prompt.strip()
    return await _send(ctx, "GET", "/query-data", params=params)


_KEYS_ERROR = ('`keys` must be a JSON array of row objects carrying that relation\'s GRAIN '
               'columns — e.g. [{"symbol": "SNOW", "cik": "0001273087", "date": "2026-06-30"}]. '
               'The grain is named in the result\'s `sources.relations[].grain`.')


def _trace_keys(keys: Any) -> tuple[Optional[str], Optional[str]]:
    """Normalise `keys` to the JSON array of grain objects /trace-sources expects.

    Returns (serialized keys, error message). A single row object is accepted and
    wrapped; a client that already serialized the array is passed through once it
    parses back to a list of objects. Nothing here is SQL — every value is a grain
    column read off a row this server already returned.
    """
    if keys in (None, "", [], {}):
        return None, None

    payload: Any = keys
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None, _KEYS_ERROR
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not all(isinstance(k, dict) for k in payload):
        return None, _KEYS_ERROR
    if not payload:
        return None, None

    encoded = json.dumps(payload, default=str)
    if len(encoded) > 20_000:
        return None, (f"`keys` is too large ({len(encoded)} chars; the backend limit is 20,000). "
                      "Trace the rows you are actually going to cite — a few dozen — rather "
                      "than the whole result set.")
    return encoded, None


@mcp.tool(annotations=ToolAnnotations(title="Trace Numbers To Their Sources", readOnlyHint=True))
async def trace_data_sources(
    ctx: Context,
    relation: Optional[str] = None,
    keys: Optional[list[dict]] = None,
    column: Optional[str] = None,
    encrypted_query_token: Optional[str] = None,
    prompt: Optional[str] = None,
    values: Optional[dict] = None,
    max_documents: Optional[int] = None,
    resolve_passages: bool = False,
) -> Any:
    """Resolve numbers you are holding back to the documents they were read from — the filing, the section, or the verified quote.

    The CITATION rung of the chain. `explore_data_catalogue`, `run_saved_query` and
    `get_signed_sql_drilldown` hand back rows; this says where each number CAME FROM.
    Reach for it whenever the user asks "where does that come from", "which filing",
    "can you cite that", "how do you know" — and unprompted before you publish a figure
    they are going to act on.

    TWO WAYS IN, and the FIRST is preferred:
      1. `relation` + `keys` — `relation` is a name off the result's
         `sources.relations[].relation`; `keys` is one JSON object per row you want
         traced. Pass EVERY identifying column the row carries — the reported grain
         AND the period/date — e.g. [{"symbol": "SNOW", "cik": "0001273087",
         "date": "2026-06-30"}]. Extra columns are ignored; a missing one is fatal.
         Grain alone is NOT always enough: the join behind a view can need a column
         its `grain` does not list (current_institutional_holders_v reports grain
         [symbol, cik] but joins on `date` too, and keys without it resolve nothing).
         No token and no SQL, so this is the form
         that works when you assembled a table across several calls (`run_saved_query`
         issues one call per saved token, so a token only ever describes a fragment of
         what you ended up with) or filtered the rows yourself. Trace the rows you will
         actually cite — a few dozen — not the whole result set.
      2. `encrypted_query_token` — a `trace_token` from a `sources` block, or the
         original `query_token` / `signed_query_url`, for the "I have a link and
         nothing else" case. The URL form is unwrapped automatically. Tokens are minted
         server-side only: never construct, guess or edit one, and never pass SQL here.

    THREE CITATION GRAINS come back, finest first. Which one answers depends on the
    relation and you do not choose it — one surface over several stacks:
      - `citations` carrying `quotes` — the passages an analysis row was actually
        WRITTEN from, with quotes machine-verified against the section text. The
        strongest evidence in the system; quote these verbatim.
      - `citations` of kind `transcript_section` — a link to ONE numbered speaker turn
        of an earnings call. `get_document_section` fetches that turn's text.
      - `documents` — the whole source document (SEC 13F / Form 4 / 8-K / 10-Q / 10-K,
        an earnings-call transcript, an IR PDF) a number was read from. The fallback,
        and available wherever lineage reaches a document at all.
    `providers` names which of those answered, and every relation's traceability is
    derived from the live data model — a redefined view re-derives its own on the next
    refresh, so trust this response over anything remembered.

    READ `traceable` BEFORE YOU WRITE. "row" means each row maps to one document and
    may be cited that way. "cohort" means the number is a sum over a SET of filings:
    the response carries `cohort` instead of `documents`, only the set can honestly be
    cited, and writing "per this filing, hedge funds own 4.1%" off a cohort row is the
    exact failure this field exists to prevent. "none" means the lineage reaches no
    document — say the number cannot be sourced rather than reaching for a plausible
    filing. A `reason` of "no_keys" means a column the join needs was missing
    from every key object — nearly always the period/date — not that the number is
    unsourced. Add the date and any other identifying column on the row, and re-call.

    `column` traces ONE column instead of the whole row — necessary for a YoY or
    change relation, where each value column resolves to a DIFFERENT filing (the
    result's `sources.relations[].per_column` says which columns work this way).

    `resolve_passages=True` also fetches the passage TEXT behind each citation (the
    speaker turn, or the filing section from the reader) instead of just a link. It
    costs a fetch and is capped at 20 passages, so ask for it when you intend to quote,
    not by reflex — a link is already a citation. Applies to the `relation` + `keys`
    form.

    `max_documents` caps the documents returned (server default 50, max 500).
    `values` and `prompt` apply to the TOKEN form only, and only for a token that is a
    placeholder twin: `values` is a JSON object of {slot name: typed value} that
    re-aims it deterministically, `prompt` fills from wording what `values` did not.
    Neither can change WHICH documents come back for a given set of rows.

    Requires auth (your MCP client attaches the OAuth bearer automatically). A 403 means
    the token is invalid or tampered — re-fetch the link rather than retrying an altered
    one; a 422 means neither `relation`+`keys` nor `t` arrived in a usable form.
    """
    token = _query_token(encrypted_query_token) if encrypted_query_token else None
    if not relation and not token:
        return {"error": "Pass either `relation` + `keys` (preferred — take both from the "
                         "result's `sources` block), or `encrypted_query_token`."}

    params: dict[str, Any] = {}
    if relation:
        params["relation"] = relation.strip()
        encoded, err = _trace_keys(keys)
        if err:
            return {"error": err}
        if encoded:
            params["keys"] = encoded
        if column and column.strip():
            params["column"] = column.strip()
        if resolve_passages:
            params["resolve_passages"] = True
    if token:
        params["t"] = token
        extra, err = _replay_args(values)
        if err:
            return {"error": err}
        params.update(extra)
    if prompt and prompt.strip():
        params["prompt"] = prompt.strip()
    if max_documents is not None:
        try:
            cap = int(max_documents)
        except (TypeError, ValueError):
            return {"error": "`max_documents` must be an integer between 1 and 500, e.g. 50."}
        if not 1 <= cap <= 500:
            return {"error": "`max_documents` must be between 1 and 500, e.g. 50."}
        params["max_documents"] = cap

    return await _send(ctx, "GET", "/trace-sources", params=params)


@mcp.tool(annotations=ToolAnnotations(title="Get Source Document Section", readOnlyHint=True))
async def get_document_section(
    ctx: Context,
    symbol: str,
    year: int,
    quarter: int,
    n: int,
    name: str = "earnings_transcript",
) -> Any:
    """Open ONE numbered section of a source document — the exact earnings-call speaker turn a citation points at.

    The other end of a `trace_data_sources` citation, and the finest grain the platform
    serves: not the whole call, one turn of it. Use it to read the passage behind a
    number before quoting it, and to quote a speaker verbatim with the citation intact.

    Every argument comes off the citation you are following — a `transcript_section`
    citation's URL is `/query-section?name=...&symbol=...&year=...&quarter=...&n=...`,
    so read the four values out of it rather than deriving them. If you pulled the row
    from `earnings_call_transcript_sections` yourself you already hold all four
    (`symbol`, `year`, `quarter`, `section_no`); no text search is needed or wanted.

    *** `year` and `quarter` are the FISCAL period of the CALL, never the calendar
    quarter a report covers. *** For most covered companies those differ — the call
    everyone calls "Q2 2026" is stored as FY2026 Q4 for one name and FY2027 Q1 for
    another. Take the period from the citation, the transcript record, or `call_date`.
    A 404 here ("no section N for SYM YEARqQ") is almost always this, not a missing
    call: re-check the period before concluding the transcript is not covered.

    `name` is the whitelisted section source; `earnings_transcript` (the default) is
    the one that exists today. `n` is 1-based within the document.

    Returns {"name", "symbol", "year", "quarter", "n", "format": "paragraph",
    "count", "rows": [{symbol, year, quarter, call_date, section_no, speaker,
    content}], "highlights": [...], "total_sections": N, "transcript_url": ...}.
    `content` is the turn's text and `speaker` is who said it — attribute the quote to
    that speaker, never to "the company". `total_sections` bounds `n`, so neighbouring
    turns (n-1, n+1) are how you read a passage in context when it starts mid-answer.
    `highlights` are quotes the platform's own analysis pipeline verified against this
    section; they mark evidence inside it but are NOT necessarily the passage you are
    citing — verify your own quote against `content`. `transcript_url` is frequently
    null, so offer "view the full call" only when it is present.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"error": "`symbol` is required — the ticker the cited call belongs to."}
    return await _send(ctx, "GET", "/query-section", params={
        "name": (name or "earnings_transcript").strip(),
        "symbol": symbol, "year": year, "quarter": quarter, "n": n,
    })


@mcp.tool(annotations=ToolAnnotations(title="Save Data Request For Replay", readOnlyHint=False, destructiveHint=False))
async def save_user_query(
    ctx: Context,
    user_request: str,
    query_tokens: list[str],
) -> Any:
    """Save the queries an exploration already planned, so the request can be replayed instantly — exactly, or re-asked for other entities.

    THE POINT: `explore_data_catalogue` is a full validate -> plan -> run pipeline that
    re-authors the SQL from scratch on every call (~1-5 min, 20/hour). For a question
    the user will ask AGAIN — a recurring check, a dashboard they watch, "my usual
    semis screen" — save the tokens that exploration just produced. `run_saved_query`
    then re-runs exactly those queries against live data in seconds, with no planning
    step and no async job to poll. The backend ALSO authors a PLACEHOLDER twin of each
    saved query (its ticker, CIK list, date, interval, and count lifted into typed
    slots) plus a `metadata` slot schema naming them, so the same request can later be
    re-asked for a different company, filer, or window:
    `run_saved_query(query_placeholder, values={"symbol": "KSS"})`.

    WHEN TO CALL: once an exploration is done and shown, offer it — say the request can
    be re-run exactly or with different values — and save when the user says yes or
    clearly implies they will want it again. Do NOT save every exploration by reflex,
    and do not save a one-off.

    `user_request` — the natural-language request being saved, in the user's own words.
    It is the ONLY human-readable label on the saved row, so make it specific enough to
    recognise later ("weekly EPS-revision check on my semis basket", not "the query").

    `query_tokens` — the `query_token` values from the exploration result's `results`
    entries, one per named query, passed through EXACTLY as returned. Each is opaque
    ciphertext the server minted around the planned SQL; a full `signed_query_url` /
    `source_url` is accepted too and unwrapped to its token. NEVER write, guess, or
    edit a token, and never pass SQL here — a token this backend did not mint cannot be
    decrypted and fails with 403 at replay time, long after the exploration is gone. A
    result entry carrying no `query_token` has nothing to save; skip it.

    Owner-scoped: the row belongs to the calling user and is invisible to everyone
    else. There is NO upsert (unlike `schedule_task`): saving the same request twice
    creates a SECOND row. Check `list_saved_queries` first and `delete_saved_query` the
    stale one rather than piling up near-duplicates.

    Returns {"msg": "Successfully saved query: <user_request>", "id": <row id>,
    "placeholder_status": "PENDING" | "NOT_DISPATCHED"}. `id` is the delete key.
    PENDING means the placeholder twins AND the row's `metadata` (its slot schema,
    summary, and datasets) are being authored — roughly 10 s to 5 minutes, depending on
    the size of the queries. Until they land, `query_placeholder` and `metadata` are
    null in `list_saved_queries`, and an exact replay via `queries` works meanwhile.
    NOT_DISPATCHED means no twins will be authored for this row: it can be replayed
    exactly but not re-aimed.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    tokens = [_query_token(t) for t in query_tokens if t and t.strip()]
    if not tokens:
        return {"error": "query_tokens is required — pass the `query_token` values from the exploration result."}
    return await _send(
        ctx, "POST", "/save-user-query",
        json={"user_request": user_request, "queries": tokens},
    )


@mcp.tool(annotations=ToolAnnotations(title="List Saved Data Requests", readOnlyHint=True))
async def list_saved_queries(
    ctx: Context,
    limit: int = 50,
) -> Any:
    """List the data requests you have saved with `save_user_query`, newest first.

    The entry point to the replay flow: check HERE FIRST, before running
    `explore_data_catalogue` for any exploration question — a saved row that matches
    the question, exactly OR in shape, means the answer is seconds away via
    `run_saved_query` instead of a ~1-5 minute planning job.

    Returns {"count": N, "saved_queries": [{"id", "user_request", "queries",
    "query_placeholder", "metadata", "created_at", "updated_at"}, ...]}, the calling
    user's rows only.

    MATCH ON `metadata`, NOT ON THE LABEL. `metadata` describes what the row actually
    asks. It is authored after the save and is null until the placeholder task has run
    (roughly 10 s to 5 minutes, depending on the size of the queries):
      `summary`        one line with the slots in braces, e.g. "13F positions of
                       {managers} in {symbol}, {date_prior} vs {date_latest}: shares,
                       value, weight, QoQ change"
      `parameters`     [{name, type, description, example}] — the slot schema, one
                       vocabulary across every query in the row. These names are the
                       keys of `run_saved_query(values=...)`.
      `entities`       {slot: resolved example} — what the row was saved for
      `datasets`       the public dataset names the row reads (the same names
                       `explore_data_coverage` reports)
      `topics`         classifier topics for the saved request
      `result_schema`  [{index, summary, slots, columns}] — per query, what its result
                       set holds
      `stale`          true when a query no longer plans (checked nightly)
      `run_count`, `last_run_at`
    Match the user's question against `summary` + `parameters` + `datasets`, NOT
    against `user_request` — that is only the label the user typed while saving. SKIP
    any row marked `stale: true`. For a row whose `metadata` has not landed yet, fall
    back to the label and replay `queries` exactly.

    Having picked a row:
      - EXACT repeat (same entities, same window) -> `run_saved_query(query_tokens=
        <the row's `queries`>)`; it replays as saved.
      - SAME SHAPE, other values (saved "the past 8 quarters of sales for TGT"; asked
        "KSS's last 8 quarters of sales") -> `run_saved_query(query_tokens=<the row's
        `query_placeholder`>, values={...})`, the values keyed by
        `metadata.parameters`; add a `prompt` only for slots you cannot type.
      - Pick WHICH of the row's queries to run from `result_schema`: the ones that
        answer the question, leaving the `_constituents` companion out — it is a
        7,000-35,000 row payload. Pass `max_rows` (e.g. 500) whatever you run.
      - TELL THE USER which saved row is being reused (its `summary` and `entities`)
        and which values were substituted (the replay's `parameters`).
    A row that is merely close in TOPIC but asks a different thing is a DIFFERENT
    question; confirm with the user before replaying it.

    The other fields:
      - `queries` is the list of opaque query tokens for an exact replay.
      - `query_placeholder` is the list of placeholder-twin tokens, index-aligned with
        `queries`, or null while the placeholder task has not run yet (replay
        `queries` exactly or list again shortly). Never show a token of either kind to
        the user: it is ciphertext, not content, and says nothing about what the query
        does.
      - `user_request` is the user's own label — worth quoting when you say which row
        you reused, but not what to decide on.
      - `id` is what `delete_saved_query` takes.

    `limit` is 1-500 (default 50). Duplicate `user_request` values are possible —
    saving does not upsert.

    An HTTP 503 here means the backend's saved-queries table has not been created yet,
    NOT that the user has nothing saved — report that difference rather than an empty
    list.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    return await _send(ctx, "GET", "/get-saved-queries", params={"limit": limit})


@mcp.tool(annotations=ToolAnnotations(title="Run Saved Data Request", readOnlyHint=True))
async def run_saved_query(
    ctx: Context,
    query_tokens: list[str],
    prompt: Optional[str] = None,
    values: Optional[dict] = None,
    max_rows: Optional[int] = None,
) -> Any:
    """Re-run a saved request's queries against live data — exactly, or re-aimed at other entities with typed `values`. No planning, no polling.

    The payoff of `save_user_query`: pass a saved row's `queries` array from
    `list_saved_queries` and each stored query runs as-is, returning fresh rows
    synchronously. Use this INSTEAD of `explore_data_catalogue` whenever a saved row
    already covers the question — same SQL, current data, seconds instead of minutes,
    and it does not draw on the 20/hour exploration budget.

    `query_tokens` — the saved row's `queries` list for an exact replay, or its
    `query_placeholder` list when re-aiming with `values` / `prompt` — verbatim (a
    full signed URL is accepted and unwrapped). Max 25 per call. Tokens are
    server-minted only: never construct or edit one, and never pass SQL. For a token
    that came from a `get_company_event_web` node rather than a saved row, use
    `get_signed_sql_drilldown` — same endpoint, single node, drilldown framing.

    `values` — THE PREFERRED WAY to re-aim a saved row: a JSON object of {slot name:
    typed value}, its keys taken from that row's `metadata.parameters` in
    `list_saved_queries`. Slots filled here are substituted deterministically, with no
    language model in the loop (~0.5-1.2 s per query). Reach for it whenever you
    already know the entities — you usually do, having resolved the tickers or CIKs
    before calling — and keep `prompt` for what you cannot type.
      - Entity-set slots (type `cik_rows` / `symbol_rows`) take a LIST of {"id",
        "name"} objects: [{"id": "0001454027", "name": "Verition Fund Management LLC"}].
        A name-only entry ({"id": "", "name": "Walleye Capital"}) is resolved
        server-side against the filer registry; a name it cannot resolve comes back as
        a 422, never as a guess.
      - Pass the SAME dict to every token of a row: each twin picks out the slots it
        uses, and keys no twin uses come back as `ignored_values`.
      - A slot left out falls back to `prompt`, and then to the value the row was
        saved with (reported as `defaulted_parameters`).

    `prompt` — natural-language fill for whatever `values` did not cover, e.g. "the
    past 8 quarters of sales for KSS". Pass it with the row's `query_placeholder`
    tokens; a plain `queries` token has no slots and replays unchanged. The backend
    maps the wording onto the row's typed slots. A 422 means it could not — fall back
    to `explore_data_catalogue` for that question. A prompt-only fill can also DRIFT (a
    slot the wording never mentions, a date say, can come back re-aimed), which is the
    second reason to prefer `values`: never infer the substituted values from the
    wording, read them out of `parameters` and say them back to the user.

    `max_rows` (e.g. 500) — PASS IT ON EVERY REPLAY. Uncapped, a saved row's
    `_constituents` companion query returns 7,000-35,000 rows (3-8 MB) and the client
    stalls for minutes. A sliced result carries `row_count` (the full count) and
    `truncated: true`; say the user is seeing a slice of N rows.

    Returns {"queries_run": N, "results": [{"index": i, "format": "table",
    "count": <rows returned>, "rows": [...], ...}, ...]}, in the SAME ORDER as
    `query_tokens`. A re-aimed replay adds, per result:
      `parameters`            the values actually substituted (resolved ids and
                              canonical names)
      `defaulted_parameters`  slots that kept the saved example ("prior quarter kept
                              as 2026-03-31")
      `ignored_values`        keys in `values` this twin does not use
    Surface all three: which values were substituted, which kept the saved example,
    which were ignored. Rows only — the underlying SQL is never returned, by design.
    Describe the results from the row's `metadata.summary`, the substituted
    parameters, and the columns, never by asserting what the query does internally.

    CITE WHAT YOU PUBLISH: each entry in `results` may carry `_source_*` columns —
    `_source_url`, `_source_filed_at`, `_source_document_id`, `_source_citations` —
    alongside a `sources` block naming each relation, its grain, and whether it is
    `traceable` per "row" or only as a "cohort". That is the provenance of the numbers,
    not padding: quote `_source_url` when the user asks where a figure came from, and for anything
    you are about to publish — or the moment they ask for the filing, the quote, or
    "how do you know" — pass `sources.relations[].relation` plus each row's
    identifying columns (the grain AND its date) to `trace_data_sources`, which
    resolves them to the source document, the
    section, or the verified quote behind the number. `get_document_section` then opens
    the exact speaker turn a transcript citation points at.

    Each token runs independently: one failure does not stop the rest, and that entry
    carries an "error" key instead of rows. A 403 means that token is invalid or
    tampered — do not retry it altered; the saved row is dead, so re-run
    `explore_data_catalogue`, `save_user_query` a fresh row, and `delete_saved_query`
    the old one. A 422 on a re-aimed replay is the mapping failure above (unknown
    entity, wrong shape), not a dead row.

    A replay returning DIFFERENT rows than when it was saved is normal — it hits live
    data. Report what comes back now; never reconcile it against remembered numbers.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    tokens = [_query_token(t) for t in query_tokens if t and t.strip()]
    if not tokens:
        return {"error": "query_tokens is required — pass the `queries` (or `query_placeholder`) array from list_saved_queries."}
    if len(tokens) > 25:
        return {"error": f"Too many query tokens ({len(tokens)}); a saved request holds a handful. Max 25 per call."}
    extra, err = _replay_args(values, max_rows)
    if err:
        return {"error": err}
    prompt = prompt.strip() if prompt else None

    # Sequential, not fanned out: a saved request holds a few queries, each one is a
    # metered backend call, and keeping the order means `results[i]` always lines up
    # with `query_tokens[i]`. A failed token yields its error entry and the rest run.
    # `values`, `prompt` and `max_rows` ride along on every call: each placeholder twin
    # takes the slots it uses (reporting the rest as `ignored_values`), and a plain
    # token ignores the fill arguments entirely.
    results: list[dict[str, Any]] = []
    for i, token in enumerate(tokens):
        params: dict[str, Any] = {"t": token, **({"prompt": prompt} if prompt else {}), **extra}
        payload = await _send(ctx, "GET", "/query-data", params=params)
        results.append({"index": i, **(payload if isinstance(payload, dict) else {"result": payload})})
    return {"queries_run": len(results), "results": results}


@mcp.tool(annotations=ToolAnnotations(title="Delete Saved Data Request", readOnlyHint=False, destructiveHint=True, idempotentHint=True))
async def delete_saved_query(
    ctx: Context,
    query_id: int,
) -> Any:
    """Delete one of your saved data requests by id.

    `query_id` is the `id` from `list_saved_queries` — NOT a position in that list and
    not the `user_request`. Deletion is permanent and touches no data, but the query
    tokens go with the row and cannot be reconstructed: re-creating it means running
    `explore_data_catalogue` again and re-saving. When more than one row looks like a
    match, confirm WHICH one with the user before calling.

    Ownership is enforced server-side. A 404 ("No saved query with id N") means the id
    does not exist OR is not yours — the backend deliberately does not distinguish the
    two, so never tell the user the row belongs to someone else.

    Returns {"msg": "Deleted saved query <id>"}.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 100/min.
    """
    return await _send(
        ctx, "DELETE", "/delete-saved-query",
        params={"query_id": query_id},
    )
