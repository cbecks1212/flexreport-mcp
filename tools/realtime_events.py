"""Real-time event tools: situate, the 12h event cache, company snapshot/event web, event ontology."""

import asyncio
from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from core import mcp, _send, _ok, compact_output
from schemas.realtime_events import (
    CompanyEventWebResult,
    CompanySnapshotResult,
    EventOntologyResult,
    RealtimeEventsResult,
)


# Company scope: the backend's recommended lean sections and card keys (MCP_HANDOVER_situate_company.md).
# MAR on 2026-11-02: 110K characters with these defaults, 53K adding event_type +
# card_types, 220K for every section and key. `read_by` points the agent at the
# targeted tool for a refreshed table, so it stays in the defaults.
SITUATE_DEFAULT_SECTIONS = ("web", "cards", "relations", "entailed")
SITUATE_DEFAULT_KEYS = ("persisted_in", "sources", "entails", "preceded_by", "layer", "read_by", "refreshed_by")
SituateSection = Literal["web", "cards", "relations", "entailed", "informs_relations", "episodes"]


@mcp.tool(
    annotations=ToolAnnotations(title="Situate: What Is Going On Right Now", readOnlyHint=True),
    # A company situation is 30-110K characters per symbol; structured output would send it twice.
    structured_output=False,
)
@compact_output
async def situate(
    ctx: Context,
    symbols: Annotated[
        Optional[list[str]],
        Field(description="Tickers to situate, e.g. [\"MAR\"] (max 5; one backend call each, in parallel). "
                          "Omit for the whole market.", max_length=5),
    ] = None,
    event_type: Annotated[
        Optional[str],
        Field(description="WITH symbols: the realtime event type the question is about, e.g. \"financials_release\" "
                          "for a 10-Q/10-K, \"ir_publication\" for a deck, \"8k_release\". Its card is returned even "
                          "when it has no node in the window. Valid values: list_options(\"event_types\")."),
    ] = None,
    card_types: Annotated[
        Optional[list[str]],
        Field(description="WITH symbols: return cards only for these event types instead of every type present; "
                          "entailed and relations shrink with them. E.g. [\"financials_release\"] with event_type.",
              max_length=20),
    ] = None,
    window_days: Annotated[
        Optional[int],
        Field(description="WITH symbols: event-web look-back in days (default 14, max 90).", ge=1, le=90),
    ] = None,
    sections: Annotated[
        Optional[list[SituateSection]],
        Field(description="WITH symbols: sections to return (default web, cards, relations, entailed). Add episodes "
                          "for the episode definitions; informs_relations is the largest and least targeted."),
    ] = None,
    include_fetch: Annotated[
        bool,
        Field(description="WITH symbols: keep each web node's `fetch` hint (the follow-up call and, on data updates, "
                          "a signed_query_url for get_signed_sql_drilldown). false shrinks the web 2-4x."),
    ] = True,
    event_types: Annotated[
        Optional[list[str]],
        Field(description="NO symbols: the realtime event types the question is about, in the order named, e.g. "
                          "[\"ir_publication\"] for decks, [\"13f_exited\"] for hedge-fund exits. Step 1 of the "
                          "order is the first. Valid values: list_options(\"event_types\").", max_length=10),
    ] = None,
    family: Annotated[
        Optional[str],
        Field(description="NO symbols, no type named: one family whose episode is walked in order — earnings, news, "
                          "market_movement, institutional_ownership, analyst_activity, strategy, prediction, insider."),
    ] = None,
    question: Annotated[
        Optional[str],
        Field(description="NO symbols: the user's words verbatim, keyword-routed to event types / a family only when "
                          "neither event_types nor family is passed. Naming nothing is a market-wide sweep. "
                          "Ignored with symbols.", max_length=500),
    ] = None,
    top_tickers: Annotated[
        Optional[int],
        Field(description="NO symbols: members listed per event type from the realtime cache, newest first "
                          "(default 10, max 50).", ge=0, le=50),
    ] = None,
    report_inventory: Annotated[
        bool,
        Field(description="NO symbols: include fresh/stale report-plan counts per plan-earning type."),
    ] = True,
    include_keys: Annotated[
        Optional[list[str]],
        Field(description="Fields kept inside each card. With symbols the default is persisted_in, sources, "
                          "entails, preceded_by, layer, read_by, refreshed_by ([] = whole cards; keep read_by: it "
                          "names the tool that reads each table). With no symbols the default is the trimmed "
                          "market card.", max_length=30),
    ] = None,
) -> dict[str, Any]:
    """THE FIRST CALL. What is going on right now — for named companies, or for the whole market — from the backend's situation endpoints. Call it before any other flexreport tool on anything about a company, an event, or "now".

    TWO SCOPES (each parameter's description says which it belongs to):

    WITH symbols -> GET /situate-company, one call per symbol. For "catch me up on
    X", "what happened at X / why", "how did X's quarter look", and BEFORE aiming
    explore_data_catalogue, list_realtime_events or a report tool at a company. When
    the question is about one event, pass it as `event_type` AND `card_types` (halves
    the payload: the cards of every other present type are dropped):
      situate(symbols=["OKTA"])                                           catch-up
      situate(symbols=["MAR"], event_type="financials_release",
              card_types=["financials_release"])                         the 10-Q
    Returns {"situations": {SYMBOL: situation}} (a symbol that failed carries
    {"error"} instead):
      present_types   event types with a node in the window
      web             {nodes, edges}: the newest node per event type and per table
                      over window_days. Edges carry `cause`: same_chain_run (observed:
                      one chain run wrote both — a table linked this way to the event
                      REFLECTS it), lineage (declared), co_occurrence (temporal only,
                      never causation). `at` is a write time, not a business date.
                      Node `fetch` hints name the follow-up call; a signed_query_url
                      goes to get_signed_sql_drilldown verbatim.
      cards           event-type cards: persisted_in, sources, entails (tables written
                      by layer, and realtime events that follow with rate and lag),
                      preceded_by
      relations       table cards: layer, refreshed_by (schedule, next_run_at), and
                      read_by — THE TOOL THAT READS IT. Prefer the first tool there
                      over explore_data_catalogue (company_financials for the EDGAR
                      statements, get_company_snapshot, predict_earnings_move, ...).
      entailed        tables the events entail, routed to this company's EDGAR or FMP
                      side; edgar_filer / routed_out / source_routing say which side
                      (never query a routed_out table for it)
    Then: a node still inside the 12h cache -> list_realtime_events(event_type,
    tickers=[symbol]); older -> its card's persisted_in table. For a table that
    refreshed for the event, call its read_by tool first, e.g.
    company_financials(symbol, financial_type=<the table>, limit=5, line_items=[...]).
    A table whose next_run_at is after the event still shows the prior period. An
    empty web is not a quiet week: widen window_days.

    NO symbols -> GET /situate-market. For "any earnings today?", "most significant
    investor decks today", "which hedge funds exited?", "what's going on?". Name the
    types yourself when you can (`event_types`, else `family`); otherwise pass the
    user's words as `question`:
      situate(event_types=["ir_publication"])        decks today: step 1 = decks
      situate(family="earnings")                     the earnings episode in order
      situate(question="what's going on?")           a sweep: one entry per episode
    Returns {shape, routed_from, order, types, event_types, market, ...}:
      shape           named_types | family | sweep (nothing named: no default family)
      routed_from     what decided the order: `by` = explicit | question | none
      order           up to 4 event types, in reading order
      types[T]        card (trimmed ontology card: persisted_in, followers, ...),
                      cache {count, newest_at, oldest_at, tickers — or members with
                      member_kind cik | strategy | singleton}, reports {fresh, stale,
                      report_date}
      event_types     the whole realtime corpus (type, family, description)
      market          exchange, is_open, session
      degraded_sources  realtime_cache / report_inventory when one was unreachable:
                      counts are then unknown, NOT zero
    Then: list_realtime_events(event_type=T, tickers=[...]) in `order`, skipping a
    type whose cache.count is 0 (the 12h cache is cold for it — say so; it is not
    evidence nothing happened). Narrow every follow-up with tickers=[...] to the names
    just seen. For the research on the names that stand out, take the types that carry
    `reports` into list_available_reports(event_types=[...],
    report_date=<types[T].reports.report_date>), then get_latest_report. A family with no realtime types returns `message` naming
    its durable tables.

    Skip this tool ONLY for pure enumeration (list_options), an explicit "get me the
    existing report" (get_latest_report), or a metric-history question with no event
    in it ("Micron EPS over 8 quarters" -> company_financials(symbol="MU", limit=8,
    line_items=["epsDiluted"]); cross-company or non-statement history ->
    explore_data_catalogue). get_company_event_web gives a company's raw graph;
    get_event_ontology gives any full card.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    syms = list(dict.fromkeys(s.strip().upper() for s in symbols or [] if s and s.strip()))[:5]
    if not syms:
        if event_type or card_types:
            raise ToolError("`event_type` / `card_types` apply WITH symbols. For the market, pass "
                            "event_types=[...] (or family, or question).")
        return _ok(await _send(ctx, "GET", "/situate-market", params=_market_params(
            event_types, family, question, top_tickers, report_inventory, include_keys)))
    if event_types or family:
        raise ToolError("`event_types` / `family` apply with NO symbols. For a company, pass "
                        "event_type=\"...\" and card_types=[...].")

    params: dict[str, Any] = {
        "sections": list(sections) if sections else list(SITUATE_DEFAULT_SECTIONS),
        "include_fetch": "true" if include_fetch else "false",
    }
    keys = SITUATE_DEFAULT_KEYS if include_keys is None else include_keys
    if keys:
        params["include_keys"] = list(keys)
    if event_type:
        params["event_type"] = event_type.strip()
    if card_types:
        params["card_types"] = list(card_types)
    if window_days:
        params["window_days"] = window_days

    results = await asyncio.gather(*[
        _send(ctx, "GET", "/situate-company", params={"symbol": s, **params}) for s in syms])
    errors = [r for r in results if isinstance(r, dict) and "error" in r and set(r) <= {"error", "detail"}]
    if len(errors) == len(results):
        _ok(errors[0])  # every symbol failed: raise the backend error instead of returning only errors
    return {"situations": dict(zip(syms, results))}


def _market_params(event_types: Optional[list[str]], family: Optional[str], question: Optional[str],
                   top_tickers: Optional[int], report_inventory: bool,
                   include_keys: Optional[list[str]]) -> dict[str, Any]:
    """Query string for /situate-market: only what the caller set, so the backend defaults apply."""
    params: dict[str, Any] = {"report_inventory": "true" if report_inventory else "false"}
    if event_types:
        params["event_types"] = [t.strip() for t in event_types if t and t.strip()]
    if family and family.strip():
        params["family"] = family.strip()
    if question and question.strip():
        params["question"] = question.strip()
    if top_tickers is not None:
        params["top_tickers"] = top_tickers
    if include_keys:
        params["include_keys"] = list(include_keys)
    return params


@mcp.tool(annotations=ToolAnnotations(title="List Real-Time Market Events", readOnlyHint=True))
async def list_realtime_events(
    ctx: Context,
    event_type: Annotated[
        str,
        Field(description="REQUIRED realtime event type from list_options(\"event_types\"), e.g. \"eps_update\", "
                          "\"8k_release\", \"ir_publication\"."),
    ],
    tickers: Annotated[
        Optional[list[str]],
        Field(description="Narrow to these tickers; always set on follow-up calls (an unfiltered call re-pulls the whole cache)."),
    ] = None,
    sector: Annotated[Optional[list[str]], Field(description="Sector values from list_options(\"sectors\").")] = None,
    industry: Annotated[Optional[list[str]], Field(description="Industry values from list_sub_industries([...]).")] = None,
    market_cap: Annotated[
        Optional[list[str]],
        Field(description="Market-cap buckets, e.g. [\"Large-cap\", \"Mega-cap\"]."),
    ] = None,
) -> RealtimeEventsResult:
    """Pull live market events from the backend's Redis-backed cache (12h TTL).

    ===> Call `situate(...)` FIRST. With symbols, its web says which event types fired
    for the company and when — a node older than the 12h cache returns [] here; its
    payload lives in the card's `persisted_in` table instead. With no symbols, its
    `order` says which types to read and its `cache.count` per type says which are
    empty before you call (0 = cold cache, not "nothing happened").
    `list_options("event_types")` enumerates the valid type strings. `event_type` is REQUIRED and
    has no default on purpose: the type is the whole question, and a default
    would answer a different one (eps_update is one slice of the earnings
    family, not the family).

    Then CONNECT THE DOTS: fetch the type asked about and follow the situation
    across its related_events with follow-up calls narrowed by `tickers=[...]`
    to the symbols just seen — e.g. eps_update -> check the intraday tape
    (detect_intraday_outlier_jumps) -> 8k_release -> ir_publication. One hop
    answers most questions; two covers a full earnings cycle. An unfiltered
    follow-up call re-pulls the whole cache.

    Returns `events`, a list of event objects (keys vary by type). An EMPTY list
    means the cache is cold for that event type (12h TTL), not that nothing happened.

    A material event here (eps_update, eps_release, 8k_release, financials_release,
    ir_publication, transcript_update) means the symbol's report is being written from
    it (within minutes; the `report_plan` topic announces each one):
    `list_available_reports(event_types=[<this type>])` says which are already current,
    and get_latest_report returns them in seconds.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    """
    body: dict[str, Any] = {"event_type": event_type}
    if tickers:
        body["tickers"] = tickers
    if sector:
        body["sector"] = sector
    if industry:
        body["industry"] = industry
    if market_cap:
        body["market_cap"] = market_cap
    events = _ok(await _send(ctx, "POST", "/get-realtime-events", json=body))
    # report_inputs is the report-generation config (~27 KB/event against ~700 chars of
    # payload, 2026-10-23); agents never use it, so only report_inputs_key is passed on.
    for event in events if isinstance(events, list) else []:
        if isinstance(event, dict):
            event.pop("report_inputs", None)
    return RealtimeEventsResult(events=events)


@mcp.tool(annotations=ToolAnnotations(title="Get Company Snapshot", readOnlyHint=True))
async def get_company_snapshot(
    ctx: Context,
    symbol: Annotated[str, Field(description="Covered ticker, e.g. \"AAPL\".")],
) -> CompanySnapshotResult:
    """Fetch a structured, POINT-IN-TIME company snapshot — no report generation needed.

    Returns thesis/bull/bear, financial overview (Piotroski, valuation signal),
    price performance + technical indicators, price targets, institutional
    ownership, and analyst grades for `symbol`. Synchronous and cheap — prefer
    this for a quick CURRENT read instead of generating a full PDF report.

    *** POINT-IN-TIME ONLY — THIS IS NOT A TIME SERIES. *** It captures where the
    company stands right now, not how it got there. Do NOT use it to answer
    temporal / trend / "over time" questions (e.g. "how has X evolved over the
    past year", "quarter-over-quarter trend", "activity over the last 12 months",
    "since ..."). Each block is an independently-sourced current cut with its own
    as-of date (e.g. the institutional-ownership block is the latest 13F holder
    snapshot), so the blocks are NOT a comparable historical series — reading a
    trend off them, or treating two blocks' as-of dates as the same instant,
    produces spurious results. For anything time-series, historical, or
    "how did this change", use `explore_data_catalogue` instead.

    Scope caveat: the snapshot is a FIXED set of current blocks, not the full
    catalogue. Absence of a data domain HERE does NOT mean Flexreport lacks it —
    do not infer coverage gaps from this tool. Route "which symbols / values are
    available" enumerations to `list_options` (instant), and "do you have data
    on <topic>" questions that need actual data to `explore_data_catalogue`.

    *** RUN IT WITH `get_company_event_web(symbol)` — THE STANDARD PAIR. *** This
    tool gives the WHAT (where the company stands); the event web gives the WHY
    (the events that got it there, in order, and how they connect). The snapshot
    states that the thesis shifted, the valuation signal changed, or ownership
    moved; it does NOT say what caused any of it — every block here is a settled
    output with the episode behind it stripped out. So whenever the user asks
    about a company generally, or follows a snapshot with "why?" / "what changed?"
    / "what drove that?", call BOTH and read them together: quote the snapshot for
    the current position, and the event web for the sequence that explains it.
    Presenting a snapshot alone as the whole picture is the failure mode — it
    reads as a verdict with no evidence.

    Two honest limits when pairing them. (1) The event web defaults to a 7-day
    window; a driver older than that needs `window_days` widened (up to 90) before
    you can say the snapshot is unexplained. (2) The blocks here carry their own
    as-of dates — a 13F ownership block can be a quarter old, so the event that
    explains it may sit well outside any window. Absence of a matching event is
    NOT proof the snapshot moved for no reason.
    """
    return _ok(await _send(
        ctx, "GET", "/get-company-snapshot",
        params={"symbol": symbol}, require_auth=False,
    ))


@mcp.tool(annotations=ToolAnnotations(title="Get Company Event Web", readOnlyHint=True))
async def get_company_event_web(
    ctx: Context,
    symbol: Annotated[str, Field(description="Covered ticker, e.g. \"WM\".")],
    window_days: Annotated[
        Optional[int],
        Field(description="Look-back in days (default 7, max 90). Widen before calling a snapshot move unexplained.",
              ge=1, le=90),
    ] = None,
    max_nodes: Annotated[
        Optional[int],
        Field(description="Node cap (default 40, max 200).", ge=1, le=200),
    ] = None,
) -> CompanyEventWebResult:
    """Fetch the connected WEB of what recently HAPPENED to a company — the WHY behind its snapshot, and a time-ordered graph to CHAIN your next calls off.

    ===> `situate(symbols=[symbol])` already returns this web deduped to the newest
    node per event type and per table, PLUS the ontology cards and the table cards
    with read_by — start there. Call this tool directly for the RAW graph (every
    node, no dedup) or a different `window_days`, and pair every node you chain off
    with get_event_ontology(event_type=node.type): the web says WHICH event, the
    ontology says what it entails and what to do about it.

    ===> A FIRST-LINE TOOL FOR SINGLE-COMPANY QUESTIONS, not a specialist one. Reach
    for it whenever the user asks what has been going on with a name ("catch me up on
    WM", "what happened at NVDA this week", "anything I should know before
    earnings?"), asks WHY the company looks the way it does ("why is the thesis
    negative?", "what drove the downgrade?", "what changed?"), or whenever you are
    about to aim a targeted `explore_data_catalogue`, `generate_report_for_stock`, or
    `list_realtime_events` request at ONE symbol. Synchronous and cheap —
    one call turns "I'll guess which event types and dates matter" into "here are
    the exact events, their timestamps, the tables that were written, and the call
    that returns each full payload".

    *** THE STANDARD PAIR: `get_company_snapshot` + THIS. *** They answer the two
    halves of almost every company question and are usually called TOGETHER:
      - `get_company_snapshot(symbol)` -> the WHAT. Where the company stands now:
        thesis, fundamentals, technicals, ownership, grades. A settled position with
        no account of how it got there.
      - `get_company_event_web(symbol)` -> the WHY. The episode behind that position:
        the earnings print, the 8-K, the transcript update, the rating action, the
        13F refresh — in order, with edges saying how each relates.
    Read them together: the snapshot for the current position, the web for the
    sequence that explains it. A snapshot delivered alone reads as a verdict with no
    evidence; this tool is the evidence. Conversely, do NOT use this tool to state
    the company's current position — it carries headlines and pointers, not values.
    When the two disagree, say so plainly rather than smoothing it over: the blocks
    in a snapshot carry independent as-of dates, so an ownership or grade block can
    predate anything in the window (widen `window_days` before concluding a snapshot
    move is unexplained).

    Returns:
      {"symbol": ..., "as_of": ISO8601, "window_days": N,
       "nodes": [...], "edges": [...], "market_context": [...]}

    NODES — two kinds, newest first, each with a short id ("n1", "n2", ...):
      - kind="event"       — a published realtime event. Carries `type` (the event
                             type, e.g. "8k_release"), `family`, and a `fetch` hint
                             — the exact call that returns its FULL payload, e.g.
                             {"tool": "list_realtime_events", "event_type": "8k_release",
                              "related_endpoints": [...]}.
      - kind="data_update" — a write to a backing table for activity that publishes
                             NO event at all (insider filings, 13F refreshes, IPO
                             calendar entries, Companies House events, transcript and
                             fundamentals analysis). Carries `relation` (the table
                             written); most also carry a `fetch` hint (a ready-made
                             `explore_data_catalogue` query for the underlying rows),
                             and on some relations the hint additionally carries
                             `signed_query_url` — see step 2 below.
    Every node carries `at` (when the write/publish happened) and `headline` (ONE
    line, truncated at 200 chars).

    EDGES — {"from": "n3", "to": "n1", "via": <relation>, "cause": ...}, pointing
    from the EARLIER node to the LATER one. `cause` is a confidence grade — respect it:
      - "same_chain_run" — OBSERVED. One backend chain run produced both nodes.
      - "lineage"        — DECLARED. One wrote the table the other's event derives from.
      - "co_occurrence"  — TEMPORAL ONLY. Same company, same window, no known causal
                           path. Report these as "happened alongside", NEVER as caused-by.

    HOW TO CHAIN (the point of this tool):
      1. Walk the edges back from the newest node to reconstruct the episode, then
         follow each event node's `fetch` to get the detail — e.g.
         `list_realtime_events(event_type="8k_release", tickers=["WM"])`. Always narrow
         by `tickers=[symbol]`; an unfiltered call re-pulls the entire cache.
      2. When a `data_update` node's `fetch` carries `signed_query_url`, call
         `get_signed_sql_drilldown` with it FIRST — zero planning, and it returns the
         new record (`is_new_record: true`) plus the context rows to read it against.
         If it comes back not-authenticated, the link is fine — the user's sign-in
         lapsed; say so rather than altering the token.
         Otherwise use the node's `fetch` query if it has one, or its `relation` and
         `at` to write a PRECISE `explore_data_catalogue` query ("insider filings for
         WM since 2026-08-09", "13F position changes for WM in the last week")
         instead of a vague one.
      3. Feed the concrete events and dates into `explore_data_catalogue` /
         `generate_research_report` so the analysis is scoped to what actually
         happened; for the company's own report, `get_latest_report` returns the
         document written from that event.
      4. Nothing here is a full payload — a headline is a pointer, never the content.
         Do not quote a headline as if it were the event.

    `window_days` and `max_nodes` widen the look-back. Beyond the node cap, edges are capped per node (~3 same_chain_run, ~3
    co_occurrence) and `market_context` — market-wide macro/economic-calendar nodes
    that carry no ticker and so attach to no symbol — is capped at 5. A MISSING edge
    is therefore not evidence that two things are unrelated.

    Traps:
      - `at` is a WRITE/PUBLISH time, not a business date. An earnings call date or
        fiscal period end can be well before the node's timestamp — never report `at`
        as the date the underlying event occurred.
      - A "<table> refreshed for N symbols" node is universe-wide scheduled
        maintenance rolled up, not company-specific signal.
      - An empty web (`"degraded": true`, empty lists) means the graph has no rows for
        that symbol in the window — NOT that nothing happened and NOT that the symbol
        is uncovered. Say so, and fall back to `list_realtime_events` /
        `explore_data_catalogue` rather than concluding it was a quiet week.

    Requires auth (your MCP client attaches the OAuth bearer automatically).
    Rate-limited 60/min.

    Every node's `type`, `relation` and `family` are join keys into get_event_ontology:
    call get_event_ontology(event_type=node.type) to learn what that node ENTAILS (the
    tables now fresh, the events that should follow and how soon, where the payload
    lives after the 12h cache, whether downstream tables have refreshed yet) before
    chaining further calls off it. Web = what happened to THIS company; ontology = what
    that KIND of thing means. Read them together.
    """
    params: dict[str, Any] = {"symbol": symbol}
    if window_days:
        params["window_days"] = window_days
    if max_nodes:
        params["max_nodes"] = max_nodes
    return _ok(await _send(
        ctx, "GET", "/get-company-event-web",
        params=params,
    ))


@mcp.tool(annotations=ToolAnnotations(title="Get Event Ontology", readOnlyHint=True))
async def get_event_ontology(
    ctx: Context,
    event_type: Annotated[
        Optional[str],
        Field(description="One event type, e.g. \"eps_update\" (a web node's `type`). Pass exactly one filter."),
    ] = None,
    family: Annotated[
        Optional[str],
        Field(description="One family, e.g. \"earnings\" (a web node's `family`)."),
    ] = None,
    relation: Annotated[
        Optional[str],
        Field(description="One table/view, e.g. \"eod_stock_prices\" (a web node's `relation`)."),
    ] = None,
    format: Annotated[
        Literal["cards", "triples"],
        Field(description="cards (grouped, default) or triples (raw subject-predicate-object rows)."),
    ] = "cards",
) -> EventOntologyResult:
    """The platform's event ONTOLOGY — what an event TYPE means for everything else. Call it FIRST on any real-time, single-company, or event-driven request; it is the map the other calls are planned from.

    ===> For a named company, `situate(symbols=[...])` returns the cards of the event
    types in its web and the table cards they entail — start there. Call this tool
    directly for the FULL card of an event type, a relation card, a whole family, or
    the unfiltered graph.

    ===> REQUISITE for: any real-time / live-events request, any "what
    happened / why / catch me up" question, and BEFORE aiming explore_data_catalogue or
    generate_report_for_stock at a company whose situation is event-driven. Cheap,
    synchronous, public, symbol-INDEPENDENT and cacheable — fetch once per event type
    (or once unfiltered, ~230 KB) and reuse for the whole conversation.

    Pass exactly ONE filter for a small card (~5-10 KB):
      event_type="eps_update"        one event type and the relations it entails
      relation="eod_stock_prices"    one table/view and the event types that entail it
      family="earnings"              every event type and relation in a family
    No filter -> the whole class graph (21 event types, ~300 relations, 20 tools).
    Web nodes from get_company_event_web carry the join keys verbatim: pass a node's
    `type` as event_type, its `relation` as relation, its `family` as family.

    An event_type card carries:
      about                    what the event is ABOUT: symbol | filer | strategy. A 13F
                               event is a FILER's action across N names — the symbol
                               you asked about is one of them.
      persisted_in             the durable table to read once the event has aged out
                               of the realtime cache (realtime_cache_ttl_hours, 12h) —
                               list_realtime_events returns [] after that; the data is
                               still there, in this relation.
      entails.<layer>          the relations WRITTEN by the same chain run (mined, with
                               run counts) plus every view above them (pg_depend),
                               bucketed base / silver / gold / dimension / analysis.
                               These are the tables that are FRESH because this event
                               fired — name them explicitly in explore queries.
      entails.realtime_events  event types that usually FOLLOW, each with rate,
                               median_lag_hours, lift, mode (trigger = dispatched by the
                               same chain, near-certain; sequence = world-ordered,
                               independently detected, probabilistic), certainty /
                               expected_within_hours when declared, and persisted_in.
      preceded_by              the inverse — what should ALREADY exist earlier in the web.
      episodes                 named world-order sequences (earnings, realtime_mover,
                               thirteen_f_cycle, news) with this event's position — the
                               whole shape of the situation from any one member.
      produced_by[].schedule   the workflow that publishes it, its cron, and
                               next_run_at (America/New_York).
      deepened_by              the tools that deepen this event once you have its payload.
      informs                  DOMAIN causality (news -> prices, earnings -> estimates):
                               what an analyst checks next. Declared and the WEAKEST
                               edge — never present it as lineage.
      evidence                 how many of these events the platform has seen.

    A relation card carries: layer, family, cadence (update_frequency + prose),
    derives_from / feeds (pg_depend), computed_from / computes (LLM and model writers),
    refreshed_by[].schedule with next_run_at, read_by (the MCP tools that read it),
    entailed_by (event types), informs.

    THE FRESHNESS CHECK — the reason this tool exists: compare a web node's `at` with a
    downstream relation's refreshed_by[].schedule[].next_run_at. If next_run_at is AFTER
    the event, that relation does NOT reflect the event yet. Do not query it for the
    reaction — use the intraday tools (detect_intraday_outlier_jumps,
    get_aftermarket_data) or say the daily table lags.

    Every edge carries `provenance` (mined | declared | pg_depend | static:tasks.py |
    beat | catalogue | data_model), the same honesty axis as the web's `cause`: mined
    and pg_depend are facts, declared is the platform's own judgment, informs is domain
    intuition. Quote the grade when you lean on an edge. format="triples" returns raw
    subject-predicate-object rows instead of cards. degraded=true means the table has
    not been built yet, not that the type is unknown.
    """
    params: dict[str, Any] = {"format": format}
    if event_type:
        params["event_type"] = event_type
    if family:
        params["family"] = family
    if relation:
        params["relation"] = relation
    return _ok(await _send(ctx, "GET", "/get-event-ontology", params=params, require_auth=False))
