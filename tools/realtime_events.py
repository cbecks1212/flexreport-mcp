"""Real-time event tools: situate, the 12h event cache, company snapshot/event web, event ontology."""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

import situate as situate_mod
from core import mcp, _send


_ONTOLOGY_CACHE: dict[str, tuple[float, Any]] = {}
_ONTOLOGY_CACHE_TTL_S = 600.0


async def _ontology_graph(ctx: Context) -> Any:
    """The unfiltered event ontology (~230 KB, symbol-independent), cached in-process for
    10 minutes so `situate` costs one backend call per 10 minutes, not one per event type."""
    hit = _ONTOLOGY_CACHE.get("all")
    if hit and time.monotonic() - hit[0] < _ONTOLOGY_CACHE_TTL_S:
        return hit[1]
    res = await _send(ctx, "GET", "/get-event-ontology", params={"format": "cards"}, require_auth=False)
    if isinstance(res, dict) and "error" not in res and not res.get("degraded"):
        _ONTOLOGY_CACHE["all"] = (time.monotonic(), res)
    return res


async def _realtime_event_corpus(ctx: Context) -> list[dict[str, Any]]:
    """The realtime-event corpus (`list_options("event_types")`: every type, its family and
    a one-line description), cached in-process like the ontology. Echoed by `situate` in
    universe scope so the agent sees every type it could ask for. [] when unavailable."""
    hit = _ONTOLOGY_CACHE.get("event_types")
    if hit and time.monotonic() - hit[0] < _ONTOLOGY_CACHE_TTL_S:
        return hit[1]
    res = await _send(ctx, "GET", "/list-realtime-event-options", require_auth=False)
    rows = res if isinstance(res, list) else []
    if rows:
        _ONTOLOGY_CACHE["event_types"] = (time.monotonic(), rows)
    return rows


@mcp.tool(annotations=ToolAnnotations(title="Situate: What Is Going On Right Now", readOnlyHint=True))
async def situate(
    ctx: Context,
    symbols: Optional[list[str]] = None,
    window_days: Optional[int] = None,
    question: Optional[str] = None,
) -> Any:
    """THE FIRST CALL. What is going on right now — for the market, or for the named symbols — and a ready-made PLAN of the tool calls that answer the user's question. Call it before any other flexreport tool on anything about a company, an event, or "now".

    ===> Call this FIRST for: "catch me up on X", "what happened / why", "anything
    before earnings?", "any earnings / 8-Ks / 13F moves / movers today?", "how is X
    trading", and BEFORE aiming explore_data_catalogue, list_realtime_events or a
    report tool at a company. Pass the user's question verbatim in `question`: it is
    matched by keyword (never an LLM) against EVERY realtime event type — decks/
    presentations -> ir_publication, 8-Ks, transcripts, upgrades, gainers/losers, 13F
    exits, baskets, predictions, ... — and the plan STARTS at the type the user named.
    A question naming only a family ("any earnings today?") walks that family's episode;
    one naming neither ("what's going on?") sweeps every episode. Synchronous, public.
      situate(symbols=["OKTA"], question="catch me up on OKTA")   one or more names (max 5)
      situate(question="any earnings events today?")             no symbols = the market
      situate(question="most significant investor decks today")  step 1 = ir_publication

    It reads the company event web (what HAPPENED to this company), the event ontology
    (what that KIND of event entails, what follows it and how soon, where its payload
    lives after the 12h realtime cache, when each table next refreshes) and market
    status, and composes them into ONE response:
      market          exchange, is_open, session
      symbols[S]      web (newest 12 nodes; full graph via get_company_event_web),
                      newest_event, episode (the DOMINANT episode in the window — its
                      anchor and each member arrived | pending | overdue with rate and
                      expected_by), cache.present / cache.expired (inferred from node
                      age vs the TTL), freshness[] (per relation: last_refresh_at,
                      next_run_at, reflects_newest_event), cards (trimmed ontology
                      cards), deepen (tools the ontology says deepen this event)
      universe        (no symbols) shape (named_types | family | sweep), asked_types,
                      the plan order and cards, and event_types: the WHOLE realtime
                      corpus (type, family, one-line description) so you never guess
      plan[]          ORDERED tool calls: {step, tool, args, why, auth, sync, optional,
                      provenance, fallback}. `tool` is an exact tool name and `args`
                      are valid for it — pass them through unchanged.
      skip[]          calls that would return nothing or waste a job (an event type
                      that aged out of the cache, a 10-minute report) — DO NOT make these.
      suggest[]       calls to make ONLY IF their `when` arises after the plan has run
                      (explore_data_catalogue lives here, never in plan[]: it is a
                      multi-minute job, and "the user wants more than the 12h cache
                      holds" is the situation that earns it).
      guidance[]      situation-scoped rules: which tables have NOT refreshed since the
                      event, which followers are overdue, filer-scoped 13F nodes.

    HOW TO EXECUTE:
      1. Run plan[] in order. `sync=false` steps return a task_id — poll
         get_task_status.
      2. Never make a call that appears in skip[]; quote its `why` if the user asks.
         Make a suggest[] call only when its `when` is true — an empty realtime list
         is NOT that condition; say the cache is cold instead.
      3. Read guidance[] before writing: an "overdue" follower is "not recorded yet",
         not "did not happen"; a relation with reflects_newest_event=false shows the
         PRIOR period, never the reaction.
      4. Go beyond the plan only for what it did not cover — then get_company_event_web
         (full graph, wider window) and get_event_ontology (full card / relation /
         family) tell you what to call next.
    Skip this tool ONLY for pure enumeration (list_options), an explicit "get me the
    existing report" (get_latest_report), or a metric-history question with no event
    in it ("Micron EPS growth over 8 quarters" -> explore_data_catalogue).

    degraded=true means a source was unavailable (ontology not built, a web errored);
    the plan still runs on what remains — follow it, and say what was unknown.
    """
    now = datetime.now(timezone.utc)
    onto_task = _ontology_graph(ctx)
    market_task = _send(ctx, "GET", "/is-market-open", params={"exchange": "NYSE"}, require_auth=False)
    syms = [s.strip().upper() for s in (symbols or []) if s and s.strip()][:5]
    corpus_task = _realtime_event_corpus(ctx) if not syms else asyncio.sleep(0, result=[])
    web_tasks = [
        _send(ctx, "GET", "/get-company-event-web",
              params={"symbol": s, **({"window_days": window_days} if window_days else {})},
              require_auth=False)
        for s in syms
    ]
    onto, market_raw, corpus, *webs = await asyncio.gather(onto_task, market_task, corpus_task, *web_tasks)

    market = situate_mod.parse_market(market_raw)
    degraded: list[str] = []
    if market.get("error"):
        degraded.append(f"market status unavailable: {market['error']}")
    if not isinstance(onto, dict) or "error" in onto or onto.get("degraded"):
        degraded.append("event ontology unavailable — followers, freshness and persisted_in are unknown; "
                        "plan built from the web's fetch hints only")
        cards, relations, episodes, ttl, onto_as_of = {}, {}, {}, situate_mod.DEFAULT_TTL_HOURS, None
    else:
        cards = onto.get("event_types") or {}
        relations = onto.get("relations") or {}
        episodes = onto.get("episodes") or {}
        ttl = int(onto.get("realtime_cache_ttl_hours") or situate_mod.DEFAULT_TTL_HOURS)
        onto_as_of = onto.get("as_of")

    # Route from what the question NAMES: realtime event types first (checked against the
    # live ontology when it is available), then the family. A miss is None, never a default.
    qtypes = situate_mod.question_event_types(question, cards or None)
    qfam = situate_mod.question_family(question, qtypes, cards)

    plan: list = []
    skip: list = []
    guidance: list = []
    symbol_blocks: dict[str, Any] = {}
    universe_block = None
    if syms:
        for s, web in zip(syms, webs):
            block, p, k, g = situate_mod.situate_symbol(
                s, web, cards, relations, episodes, ttl, market, now, qfam, qtypes)
            symbol_blocks[s] = block
            plan += p; skip += k; guidance += g
            if block.get("error"):
                degraded.append(f"{s}: event web errored ({block['error']})")
    else:
        universe_block, p, k, g = situate_mod.situate_universe(
            qfam, cards, episodes, market, now, qtypes, ttl, corpus)
        plan += p; skip += k; guidance += g

    return situate_mod.compose(
        "symbol" if syms else "universe", market, ttl, now, symbol_blocks, universe_block,
        plan, skip, guidance, degraded, onto_as_of,
    )


@mcp.tool(annotations=ToolAnnotations(title="List Real-Time Market Events", readOnlyHint=True))
async def list_realtime_events(
    ctx: Context,
    event_type: str,
    tickers: Optional[list[str]] = None,
    sector: Optional[list[str]] = None,
    industry: Optional[list[str]] = None,
    market_cap: Optional[list[str]] = None,
) -> Any:
    """Pull live market events from the backend's Redis-backed cache (12h TTL).

    ===> Call `situate(...)` FIRST (symbols=[...] for named companies, none for the
    market): its `cache.present` / `cache.expired` say which types this tool can
    still return, its `plan` carries the exact calls to make, and its `skip` list
    names the ones that would return [] — an event older than the 12h cache lives
    in the ontology card's `persisted_in` relation, not here. Never call this for a
    type situate marked expired. `list_options("event_types")` remains the
    enumeration of valid type strings and descriptions. `event_type` is REQUIRED and
    has no default on purpose: the type is the whole question, and a default
    would answer a different one (eps_update is one slice of the earnings
    family, not the family).

    Then CONNECT THE DOTS: fetch the type asked about and follow the situation
    across its related_events with follow-up calls narrowed by `tickers=[...]`
    to the symbols just seen — e.g. eps_update -> check the intraday tape
    (detect_intraday_outlier_jumps) -> 8k_release -> ir_publication. One hop
    answers most questions; two covers a full earnings cycle. An unfiltered
    follow-up call re-pulls the whole cache.

    Optionally narrow results by `tickers`, `sector`, `industry`, or `market_cap`
    (e.g. market_cap=["Large-cap","Mega-cap"]). Returns a list of event objects.
    An EMPTY list means the cache is cold for that event type (12h TTL), not
    that nothing happened.

    A material event here (eps_update, eps_release, 8k_release, financials_release,
    ir_publication, transcript_update) means a report plan was saved for the symbol:
    `list_available_reports(event_types=[<this type>])` says which of them
    get_latest_report renders fresh in seconds.

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
    return await _send(ctx, "POST", "/get-realtime-events", json=body)


@mcp.tool(annotations=ToolAnnotations(title="Get Company Snapshot", readOnlyHint=True))
async def get_company_snapshot(ctx: Context, symbol: str) -> Any:
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
    return await _send(
        ctx, "GET", "/get-company-snapshot",
        params={"symbol": symbol}, require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Company Event Web", readOnlyHint=True))
async def get_company_event_web(
    ctx: Context,
    symbol: str,
    window_days: Optional[int] = None,
    max_nodes: Optional[int] = None,
) -> Any:
    """Fetch the connected WEB of what recently HAPPENED to a company — the WHY behind its snapshot, and a time-ordered graph to CHAIN your next calls off.

    ===> `situate(symbols=[symbol])` already returns this web (newest 12 nodes) PLUS
    the ontology cards, cache state and an ordered plan — start there. Call this
    tool directly for the FULL graph or a wider `window_days`, and pair every node
    you chain off with get_event_ontology(event_type=node.type): the web says WHICH
    event, the ontology says what it entails and what to do about it.

    ===> A FIRST-LINE TOOL FOR SINGLE-COMPANY QUESTIONS, not a specialist one. Reach
    for it whenever the user asks what has been going on with a name ("catch me up on
    WM", "what happened at NVDA this week", "anything I should know before
    earnings?"), asks WHY the company looks the way it does ("why is the thesis
    negative?", "what drove the downgrade?", "what changed?"), or whenever you are
    about to aim a targeted `explore_data_catalogue`, `generate_report_for_stock`, or
    `list_realtime_events` request at ONE symbol. Synchronous, cheap, and public —
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
         That follow-up needs the user SIGNED IN (this tool does not); if it comes back
         not-authenticated, the link is fine — say sign-in is needed for the rows and
         fall back to an `explore_data_catalogue` query on the node's `relation`.
         Otherwise use the node's `fetch` query if it has one, or its `relation` and
         `at` to write a PRECISE `explore_data_catalogue` query ("insider filings for
         WM since 2026-08-09", "13F position changes for WM in the last week")
         instead of a vague one.
      3. Feed the concrete events and dates into `explore_data_catalogue` /
         `generate_research_report` so the analysis is scoped to what actually
         happened; for the company's own report, `get_latest_report` renders the plan
         saved from that event.
      4. Nothing here is a full payload — a headline is a pointer, never the content.
         Do not quote a headline as if it were the event.

    `window_days` (default 7, max 90) and `max_nodes` (default 40, max 200) widen the
    look-back. Beyond the node cap, edges are capped per node (~3 same_chain_run, ~3
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

    PUBLIC at the backend — no account, plan, or entitlement is needed, the same
    posture as `get_company_snapshot`. (The MCP connection itself still carries the
    client's OAuth session; that gate is transport-wide, not this tool's.)
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
    return await _send(
        ctx, "GET", "/get-company-event-web",
        params=params, require_auth=False,
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Event Ontology", readOnlyHint=True))
async def get_event_ontology(
    ctx: Context,
    event_type: Optional[str] = None,
    family: Optional[str] = None,
    relation: Optional[str] = None,
    format: Literal["cards", "triples"] = "cards",
) -> Any:
    """The platform's event ONTOLOGY — what an event TYPE means for everything else. Call it FIRST on any real-time, single-company, or event-driven request; it is the map the other calls are planned from.

    ===> `situate(...)` composes this with the event web and the cache state and
    hands you a plan — start there. Call this tool directly for the FULL card of an
    event type, a relation card, a whole family, or the unfiltered graph, when the
    plan did not cover what you need next.

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
    return await _send(ctx, "GET", "/get-event-ontology", params=params, require_auth=False)
