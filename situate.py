"""Situate — compose the company event web + the event ontology + market status into
what is going on right now and an ordered PLAN of tool calls.

Pure functions only: no I/O, no clock reads (``now`` is passed in), so every rule here
is testable against fixtures. ``server.py`` does the fetching and calls
``situate_symbol`` / ``situate_universe`` / ``compose``.

Design notes (from the 2026-08-30 OKTA A/B):
- Agents execute ``{tool, args}`` hints found in tool results literally. That is why the
  plan names exact tools, and why ``skip`` is an explicit negative list — without it the
  web's ``fetch`` hints send agents into ``list_realtime_events`` calls for event types
  that aged out of the 12h cache and return ``[]``.
- The dominant episode is chosen by how many of its members are present in the window,
  not by the newest node: on OKTA the newest node was a tiny filer's ``13f_exited`` while
  eight earnings-episode members sat behind it.

Design notes (from the 2026-09-10 investor-deck outage):
- Routing starts from the realtime event TYPE the question names (``EVENT_VOCABULARY``
  covers the whole corpus), not from a family regex. "Most significant investor decks
  today" used to miss every family, default to earnings, and plan ir_publication fourth.
- There is no default family. A question that names nothing is a market-wide sweep.
- ``explore_data_catalogue`` is never planned in universe scope: the cache IS "today",
  and an unscoped explore over ``ir_documents`` was a 127 MB result that OOM-killed the
  proxy. It is on the skip list with the conditions under which it is allowed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_TTL_HOURS = 12
MAX_PLAN_STEPS = 8
WEB_NODE_CAP = 12
FRESHNESS_RELATION_CAP = 6

# Tools the plan may emit. Names MUST match the @mcp.tool functions in server.py; server.py
# asserts that at import so a rename fails loudly instead of emitting a dead hint.
TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "list_realtime_events": {"auth": "required", "sync": True},
    "get_signed_sql_drilldown": {"auth": "required", "sync": True},
    "explore_data_catalogue": {"auth": "required", "sync": False},
    "get_task_status": {"auth": "none", "sync": True},
    "detect_intraday_outlier_jumps": {"auth": "required", "sync": True},
    "get_aftermarket_data": {"auth": "required", "sync": True},
    "get_company_snapshot": {"auth": "none", "sync": True},
    "get_company_event_web": {"auth": "none", "sync": True},
    "get_event_ontology": {"auth": "none", "sync": True},
    "predict_earnings_move": {"auth": "required", "sync": True},
    "list_earnings_announcements": {"auth": "required", "sync": True},
    "get_latest_report": {"auth": "required", "sync": True},
    "generate_report_for_stock": {"auth": "required", "sync": False},
    "generate_research_report": {"auth": "required", "sync": False},
}

# ------------------------------------------------------------ question routing
# The realtime-event corpus: EVERY event type `list_realtime_events` can return, keyed by
# type, with the words a user reaches for when they mean it. A question that names a type
# ("investor decks today") starts the plan AT that type instead of walking an episode from
# position one. Ordered most-specific first so "13F filings" lands on the 13F types before
# "filings" could land on 8k_release. Kept in step with the live ontology by
# `unmapped_event_types` (situate reports any type the ontology knows and this table does
# not) and by tests/test_situate_routing.py (fails when the corpus grows).
EVENT_VOCABULARY: dict[str, tuple[str, ...]] = {
    "13f_new": (r"new (?:13-?f )?(?:positions?|stakes?|buys?|holdings?)", r"(?:opened|initiated|started) (?:a |new )?(?:positions?|stakes?)",
                r"(?:positions?|stakes?) (?:opened|initiated|started)", r"first[- ]time (?:positions?|buyers?|holders?)"),
    "13f_exited": (r"exit(?:ed|s)?\b", r"closed[- ]out", r"sold out", r"liquidat\w*", r"dumped", r"dropped (?:positions?|stakes?)"),
    "13f_significant_position_change": (r"13-?fs?\b", r"hedge funds?", r"institution\w*", r"holders?\b",
                                        r"(?:position|stake|holding) (?:changes?|shifts?|moves?)",
                                        r"(?:added to|trimmed|increased|reduced|cut) (?:their |its |a )?(?:positions?|stakes?)",
                                        r"whales?", r"smart money"),
    "ir_publication": (r"decks?\b", r"slides?\b", r"slide ?decks?", r"presentations?", r"investor[- ]relations", r"\bir\b",
                       r"investor (?:day|update|materials?|docs?|documents?|events?)", r"earnings materials?"),
    "8k_release": (r"8-?ks?\b", r"eight[- ]ks?\b", r"sec filings?", r"edgar\b", r"form 8"),
    "transcript_update": (r"transcripts?", r"earnings calls?", r"conference calls?", r"(?:call|management) commentary",
                          r"q ?& ?a", r"prepared remarks", r"what (?:did )?management (?:say|said)"),
    "financials_release": (r"10-?qs?\b", r"10-?ks?\b", r"quarterly financials", r"financial statements?", r"as[- ]reported",
                           r"balance sheets?", r"cash ?flow statements?", r"income statements?"),
    "earnings_themes": (r"earnings themes?", r"earnings[- ]season themes?", r"themes? (?:this|across|of the) (?:earnings )?season",
                        r"(?:emerging|fading|reinforced) themes?"),
    "eps_release": (r"earnings releases?", r"results? releases?", r"who (?:reported|is reporting|reports)", r"reporters?\b",
                    r"reported (?:earnings|results|this morning|tonight|today)"),
    "eps_update": (r"beats?\b", r"beat[- ]and[- ]raise", r"miss(?:es|ed)?\b", r"beat/miss", r"earnings tracker", r"how did .+ (?:do|report)",
                   r"earnings surprises?"),
    "news_evolution": (r"news ?flow", r"macro risks?", r"news themes?", r"what'?s (?:in|driving) the news", r"headline (?:overview|summary)",
                       r"market narrative", r"sector implications?"),
    "company_update": (r"news\b", r"headlines?", r"breaking", r"material (?:updates?|events?|developments?)", r"announce\w*",
                       r"press releases?", r"what happened (?:to|with|at)"),
    "biggest_gainer": (r"gainers?", r"winners?", r"(?:top|best) performers?", r"up the most", r"rall(?:y|ied|ying)\b", r"biggest ups?",
                       r"(?:ripping|surging|soaring|spiking)"),
    "biggest_loser": (r"losers?", r"decliners?", r"laggards?", r"down the most", r"sell[- ]?offs?", r"worst performers?",
                      r"(?:tanking|crashing|plunging|dumping|getting (?:hit|crushed))"),
    "biggest_mover": (r"movers?", r"moving\b", r"biggest moves?", r"volatil\w*", r"tape\b", r"intraday", r"why is (?:it|\w+) (?:up|down)",
                      r"(?:trading|acting) (?:today|now)", r"most active"),
    "realtime_ratings_update": (r"ratings?\b", r"upgrades?", r"downgrades?", r"initiations?", r"initiated coverage", r"price targets?",
                                r"\bpts?\b", r"analyst (?:actions?|calls?|moves?|notes?|changes?)", r"sell[- ]side", r"rated\b",
                                r"analysts?\b", r"targets?\b"),
    "financial_estimate_update": (r"estimates?", r"consensus", r"revisions?", r"revised (?:up|down|higher|lower)",
                                  r"numbers? (?:went|going|moved|moving) (?:up|down)", r"street numbers?"),
    "strategy_update": (r"strateg(?:y|ies)\b", r"\bsmid\b", r"momentum", r"multi[- ]signal", r"portfolio (?:changes?|updates?|adds?|removes?|moves?)",
                        r"adds?/removes?", r"model portfolio", r"track record"),
    "llm_basket_update": (r"baskets?", r"thematic (?:baskets?|picks?|portfolios?)", r"llm[- ]curated"),
    "stock_return_prediction_update": (r"predict\w*", r"predicted returns?", r"return scores?", r"model (?:scores?|picks?|recommendations?)",
                                       r"ml (?:scores?|picks?|models?)", r"expected returns?", r"stock picks?"),
}
# Catch-all types that step aside when a more specific sibling in the family also matched.
_GENERIC_YIELDS_TO: dict[str, tuple[str, ...]] = {
    "13f_significant_position_change": ("13f_new", "13f_exited"),
    "company_update": ("news_evolution",),
    "biggest_mover": ("biggest_gainer", "biggest_loser"),
}
_EVENT_VOCABULARY_RE: dict[str, re.Pattern[str]] = {
    t: re.compile(r"\b(?:" + "|".join(pats) + r")", re.IGNORECASE) for t, pats in EVENT_VOCABULARY.items()
}

# Deterministic question -> family for questions that name a FAMILY but no specific type
# ("any earnings today?", "what's the market doing"). One row per ontology family that has
# realtime cards, plus the families situate can still answer from durable tables. Order
# matters (first match wins). A miss returns None — never a default family.
_QUESTION_FAMILIES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(13f|hedge fund|holders?|positions?|institution\w*|ownership|stakes?)\b"), "institutional_ownership"),
    (re.compile(r"\binsider\w*\b"), "insider"),
    (re.compile(r"\b(ratings?|price target|targets?|upgrade\w*|downgrade\w*|analysts?|estimates?|consensus|revisions?)\b"), "analyst_activity"),
    (re.compile(r"\b(earnings|eps|print|calls?|guidance|transcripts?|8-?ks?|10-?[qk]s?|results?|reported|reporting|decks?|presentations?|filings?)\b"), "earnings"),
    (re.compile(r"\b(news|headlines?|announce\w*|breaking)\b"), "news"),
    (re.compile(r"\b(movers?|moving|tape|trading|intraday|gainers?|losers?|volatil\w*|why is \w+ (up|down))\b"), "market_movement"),
    (re.compile(r"\b(strateg(y|ies)|portfolios?|baskets?|picks?|smid|momentum)\b"), "strategy"),
    (re.compile(r"\b(predict\w*|forecasts?|model scores?|expected returns?)\b"), "prediction"),
]

# One entry point per episode for a market-wide sweep when the question names neither a
# type nor a family: the episode's first member is the event the rest of it follows from.
_SWEEP_ORDER: tuple[str, ...] = ("earnings", "news", "realtime_mover", "thirteen_f_cycle")

# Event families whose "reaction" is a price move worth reading off the tape.
_REACTION_FAMILIES = {"earnings", "news", "market_movement"}
# informs family -> the one relation worth a freshness row (only when a card informs it).
_INFORMS_PRIMARY = {"prices": "eod_stock_prices"}
# Plan ordering weights.
_CATEGORY_ORDER = {"payload": 0, "reaction": 1, "explore": 2, "context": 3}


# --------------------------------------------------------------------------- helpers
def parse_ts(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def question_event_types(question: Optional[str], cards: Optional[dict[str, Any]] = None) -> list[str]:
    """Every realtime event type the question names, in the order the user named them.

    Matched against EVENT_VOCABULARY (deterministic, never an LLM). When `cards` is given,
    types the live ontology does not know are dropped so the plan never emits a call
    for a type the backend would reject.
    """
    if not question:
        return []
    hits: list[tuple[int, str]] = []
    for t, pattern in _EVENT_VOCABULARY_RE.items():
        if cards is not None and t not in cards:
            continue
        m = pattern.search(question)
        if m:
            hits.append((m.start(), t))
    hits.sort()
    ordered = [t for _, t in hits]
    # a family's catch-all type yields to a more specific sibling that also matched:
    # "which hedge funds exited" is 13f_exited, not the generic position-change feed.
    for generic, siblings in _GENERIC_YIELDS_TO.items():
        if generic in ordered and any(s in ordered for s in siblings):
            ordered.remove(generic)
            ordered.append(generic)
    return ordered


def question_family(question: Optional[str], asked_types: Optional[list[str]] = None,
                    cards: Optional[dict[str, Any]] = None) -> Optional[str]:
    """The family the question is about: the ontology family of the first type it names,
    else the family its wording matches, else None. None is a real answer ("I do not
    know what this is about") — callers must NOT replace it with a default family."""
    for t in asked_types or []:
        fam = ((cards or {}).get(t) or {}).get("family")
        if fam:
            return fam
    if not question:
        return None
    q = question.lower()
    for pattern, family in _QUESTION_FAMILIES:
        if pattern.search(q):
            return family
    return None


def unmapped_event_types(cards: dict[str, Any]) -> list[str]:
    """Realtime event types the ontology knows but EVENT_VOCABULARY does not — a routing
    gap the user would hit as a wrong plan, so it is surfaced instead of hidden.
    Types with no family are episode markers (eps_market_reaction), not fetchable events."""
    return sorted(t for t, c in cards.items() if t not in EVENT_VOCABULARY and (c or {}).get("family"))


def cron_period_hours(cron: Optional[str]) -> float:
    """Rough period of a 5-field cron: enough to estimate the PREVIOUS run from next_run_at."""
    if not cron:
        return 24.0
    parts = cron.split()
    if len(parts) < 5:
        return 24.0
    minute, hour, _dom, _mon, dow = parts[:5]
    if minute.startswith("*/"):
        return int(minute[2:]) / 60.0
    if hour == "*":
        return 1.0
    if hour.startswith("*/"):
        return float(hour[2:])
    hours = hour.split(",")
    if len(hours) > 1:
        return 24.0 / len(hours)
    if dow not in ("*", "?") and len(dow.split(",")) == 1 and "-" not in dow:
        return 24.0 * 7
    return 24.0


def query_token(reference: str) -> str:
    token = reference.strip()
    if "?" in token:
        token = parse_qs(urlsplit(token).query).get("t", [token])[0]
    return token


def parse_market(raw: Any) -> dict[str, Any]:
    """Normalise the /is-market-open payload (a one-element list) to {exchange, is_open, session}."""
    rec = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else {})
    if "error" in rec:
        return {"exchange": "NYSE", "is_open": None, "session": "unknown", "error": rec.get("error")}
    is_open = rec.get("isMarketOpen")
    if is_open is None:
        is_open = rec.get("is_open")
    return {
        "exchange": rec.get("exchange") or rec.get("stockExchangeName") or "NYSE",
        "is_open": bool(is_open) if is_open is not None else None,
        "session": "regular" if is_open else ("closed" if is_open is not None else "unknown"),
        "timezone": rec.get("timezone"),
    }


def trim_card(card: dict[str, Any]) -> dict[str, Any]:
    entails = card.get("entails") or {}
    followers = [
        {k: f.get(k) for k in ("type", "rate", "median_lag_hours", "mode", "certainty",
                               "expected_within_hours", "persisted_in") if f.get(k) is not None}
        for f in entails.get("realtime_events") or []
    ]
    return {
        "family": card.get("family"),
        "about": card.get("about"),
        "persisted_in": card.get("persisted_in"),
        "episodes": card.get("episodes"),
        "deepened_by": card.get("deepened_by"),
        "informs": card.get("informs"),
        "entails": {layer: [r.get("relation") for r in rels]
                    for layer, rels in entails.items() if layer != "realtime_events"},
        "followers": followers,
    }


def _step(category: str, tool: str, args: dict[str, Any], why: str, *, provenance: str,
          optional: bool = False, reads: Optional[list[str]] = None,
          fallback: Optional[dict[str, Any]] = None, only_if: Optional[str] = None) -> dict[str, Any]:
    reg = TOOL_REGISTRY.get(tool, {"auth": "required", "sync": True})
    s: dict[str, Any] = {
        "tool": tool, "args": args, "why": why, "auth": reg["auth"], "sync": reg["sync"],
        "optional": optional, "provenance": provenance, "_category": category,
    }
    if reads:
        s["reads"] = reads
    if fallback:
        s["fallback"] = fallback
    if only_if:
        s["only_if"] = only_if
    return s


def _skip(tool: str, args: Optional[dict[str, Any]], why: str) -> dict[str, Any]:
    d: dict[str, Any] = {"tool": tool, "why": why}
    if args:
        d["args"] = args
    return d


# ------------------------------------------------------------------ symbol scope
def situate_symbol(
    symbol: str,
    web: Any,
    cards: dict[str, dict[str, Any]],
    relations: dict[str, dict[str, Any]],
    episodes: dict[str, dict[str, Any]],
    ttl_hours: int,
    market: dict[str, Any],
    now: datetime,
    question_fam: Optional[str] = None,
    asked_types: Optional[list[str]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return (symbols[S] block, plan steps, skip entries, guidance lines) for one symbol.

    `asked_types` are the realtime event types the question named (question_event_types):
    they pick the episode and lead the payload steps, so "OKTA's deck" reads ir_publication
    before whatever the episode would otherwise put first."""
    plan: list[dict[str, Any]] = []
    skip: list[dict[str, Any]] = []
    guidance: list[str] = []
    asked = [t for t in (asked_types or []) if t in cards] if cards else list(asked_types or [])

    # ---- degraded web -------------------------------------------------------------
    if not isinstance(web, dict) or "error" in web or web.get("degraded") or not web.get("nodes"):
        err = web.get("error") if isinstance(web, dict) else "no web payload"
        detail = web.get("detail") if isinstance(web, dict) else None
        block: dict[str, Any] = {"web": {"nodes": [], "edges": [], "degraded": True},
                                 "newest_event": None, "episode": None}
        if err:
            block["error"] = err
            if isinstance(detail, dict) and "unknown" in json.dumps(detail).lower():
                block["suggest"] = "onboard_symbol"
        plan.append(_step("context", "get_company_snapshot", {"symbol": symbol},
                          "no event-web rows in the window — the snapshot is the only cheap grounding",
                          provenance="declared"))
        plan.append(_step("explore", "explore_data_catalogue",
                          {"query": f"For {symbol}: the most recent 30 days of material_update_events and "
                                    f"significant_news_symbol_history, newest first"},
                          "durable fallback when the graph is empty — NOT evidence nothing happened",
                          provenance="declared", reads=["material_update_events", "significant_news_symbol_history"]))
        guidance.append(f"{symbol}: the event web has no rows in the window (or errored: {err}). "
                        "That is not evidence nothing happened — widen window_days or read the durable tables.")
        return block, plan, skip, guidance

    nodes: list[dict[str, Any]] = web["nodes"]
    events = [n for n in nodes if n.get("kind") == "event" and n.get("type")]
    updates = [n for n in nodes if n.get("kind") == "data_update" and n.get("relation")]

    # ---- cache state: inferred from node age vs the 12h TTL -------------------------
    present: list[str] = []
    expired: list[str] = []
    newest_by_type: dict[str, dict[str, Any]] = {}
    for n in events:  # nodes are newest-first
        newest_by_type.setdefault(n["type"], n)
    for t, n in newest_by_type.items():
        at = parse_ts(n.get("at"))
        age_h = (now - at).total_seconds() / 3600 if at else None
        (present if age_h is not None and age_h < ttl_hours else expired).append(t)

    # ---- dominant episode ----------------------------------------------------------
    membership: dict[str, list[str]] = {}  # episode -> member types present
    for t in newest_by_type:
        for ep in (cards.get(t) or {}).get("episodes") or []:
            membership.setdefault(ep["name"], []).append(t)
    primary_ep: Optional[str] = None
    for t in asked:  # the episode holding the type the user named wins outright
        if t in newest_by_type:
            for ep_name, members in membership.items():
                if t in members:
                    primary_ep = ep_name
                    break
        if primary_ep:
            break
    if primary_ep is None and question_fam:
        for ep_name, members in membership.items():
            if any((cards.get(t) or {}).get("family") == question_fam for t in members):
                primary_ep = ep_name
                break
    if primary_ep is None and membership:
        primary_ep = max(membership, key=lambda e: (len(membership[e]),
                                                     max(parse_ts(newest_by_type[t]["at"]) or now
                                                         for t in membership[e])))
    order: list[str] = (episodes.get(primary_ep) or {}).get("order") or [] if primary_ep else []

    # anchor = the present member EARLIEST IN EPISODE ORDER (the print itself, not
    # whichever member happened to publish a few minutes sooner); timestamp breaks ties.
    anchor: Optional[dict[str, Any]] = None
    if primary_ep:
        ep_order = (episodes.get(primary_ep) or {}).get("order") or []
        candidates = [n for n in events if n["type"] in membership[primary_ep]]
        anchor = min(
            candidates,
            key=lambda n: (ep_order.index(n["type"]) if n["type"] in ep_order else len(ep_order),
                           parse_ts(n["at"]) or now),
        ) if candidates else None
    newest_event = events[0] if events else None
    primary = anchor or newest_event
    primary_card = cards.get(primary["type"]) if primary else None
    primary_family = (primary_card or {}).get("family") or (primary or {}).get("family")
    anchor_at = parse_ts(primary["at"]) if primary else None
    anchor_age_h = (now - anchor_at).total_seconds() / 3600 if anchor_at else None

    # ---- episode members: arrived / pending / overdue -------------------------------
    followers = {f["type"]: f for f in ((primary_card or {}).get("entails") or {}).get("realtime_events") or []}
    members: list[dict[str, Any]] = []
    for t in order:
        n = newest_by_type.get(t)
        if n:
            members.append({"type": t, "status": "arrived", "at": n["at"], "node": n["id"]})
            continue
        f = followers.get(t)
        if not f or not anchor_at:
            members.append({"type": t, "status": "not_expected"})
            continue
        horizon = f.get("expected_within_hours") or (2 * (f.get("median_lag_hours") or 24))
        expected_by = anchor_at + timedelta(hours=horizon)
        status = "pending" if now < expected_by else "overdue"
        members.append({"type": t, "status": status, "rate": f.get("rate"),
                        "median_lag_hours": f.get("median_lag_hours"), "mode": f.get("mode"),
                        "expected_by": iso(expected_by)})
        if status == "overdue" and (f.get("rate") or 0) >= 0.4:
            guidance.append(f"{symbol}: {t} is OVERDUE for the {primary_ep} episode (follows "
                            f"{primary['type']} with rate {f.get('rate')}, median {f.get('median_lag_hours')}h). "
                            f"Say it is not recorded yet — not that it did not happen.")

    # ---- freshness rows --------------------------------------------------------------
    observed: dict[str, datetime] = {}
    for n in updates:
        at = parse_ts(n.get("at"))
        if at and (n["relation"] not in observed or at > observed[n["relation"]]):
            observed[n["relation"]] = at
    wanted: list[str] = []
    if primary_card:
        for layer in ("gold", "analysis", "silver"):
            for r in (primary_card.get("entails") or {}).get(layer) or []:
                if r.get("relation") not in wanted:
                    wanted.append(r["relation"])
        for fam in primary_card.get("informs") or []:
            rel = _INFORMS_PRIMARY.get(fam)
            if rel and rel not in wanted:
                wanted.append(rel)
        if primary_card.get("persisted_in") and primary_card["persisted_in"] not in wanted:
            wanted.insert(0, primary_card["persisted_in"])
    wanted = wanted[:FRESHNESS_RELATION_CAP]
    freshness: list[dict[str, Any]] = []
    fresh_relations: list[str] = []
    for rel in wanted:
        card = relations.get(rel) or {}
        row: dict[str, Any] = {"relation": rel, "layer": card.get("layer")}
        last: Optional[datetime] = observed.get(rel)
        basis = "observed" if last else None
        next_run: Optional[datetime] = None
        for rb in card.get("refreshed_by") or []:
            for sch in rb.get("schedule") or []:
                nr = parse_ts(sch.get("next_run_at"))
                if nr and (next_run is None or nr < next_run):
                    next_run = nr
                    if not last:
                        period = timedelta(hours=cron_period_hours(sch.get("cron")))
                        est = nr - period
                        # walk back past weekend/holiday gaps a naive one-period step misses
                        for _ in range(7):
                            if est <= now:
                                break
                            est -= period
                        last = min(est, now)
                        basis = "schedule"
        row["last_refresh_at"] = iso(last)
        row["next_run_at"] = iso(next_run)
        row["basis"] = basis or "unknown"
        if anchor_at and last:
            row["reflects_newest_event"] = last > anchor_at
        else:
            row["reflects_newest_event"] = None
        freshness.append(row)
        if row["reflects_newest_event"]:
            fresh_relations.append(rel)
        elif row["reflects_newest_event"] is False and primary:
            guidance.append(f"{symbol}: {rel} last refreshed ≈ {iso(last)} ({basis}) — BEFORE the "
                            f"{primary['type']} at {primary['at']}. It shows the prior period, not the reaction"
                            + (f"; next run {iso(next_run)}." if next_run else "."))

    # ---- plan: payload steps ----------------------------------------------------------
    # the episode's anchor first (the print itself), then its newest members, then the rest newest-first
    in_episode = set(membership.get(primary_ep or "", []))
    payload_types = sorted(
        newest_by_type,
        key=lambda t: (t not in asked, not (anchor and t == anchor["type"]), t not in in_episode,
                       -(parse_ts(newest_by_type[t]["at"]) or now).timestamp()),
    )[:3]
    for t in asked:
        if t not in newest_by_type:
            persisted = (cards.get(t) or {}).get("persisted_in")
            guidance.append(f"{symbol}: you asked about {t} but the event web has no {t} node in the window"
                            + (f" — its durable copy is {persisted}; widen window_days or ask for that relation by date"
                               if persisted else " — widen window_days")
                            + ". Say it is not recorded in the window, not that it did not happen.")
    explore_payloads: list[tuple[str, dict[str, Any], str]] = []
    for t in payload_types:
        n = newest_by_type[t]
        card = cards.get(t) or {}
        if t in present:
            plan.append(_step("payload", "list_realtime_events", {"event_type": t, "tickers": [symbol]},
                              f"{t} (web {n['id']}, {n['at']}) is still inside the {ttl_hours}h realtime cache",
                              provenance="cache"))
            continue
        persisted = card.get("persisted_in") or (n.get("fetch") or {}).get("relation")
        signed = None
        for u in updates:  # a data_update node for the durable relation with a signed drilldown
            if u["relation"] == persisted and (u.get("fetch") or {}).get("signed_query_url"):
                signed = u["fetch"]["signed_query_url"]
                break
        date = (parse_ts(n["at"]) or now).astimezone(ET).date().isoformat()
        explore_args = {"query": f"For {symbol}: the most recent rows of {persisted} on or after {date}"}
        if signed:
            plan.append(_step("payload", "get_signed_sql_drilldown", {"encrypted_query_token": query_token(signed)},
                              f"{t} payload (web {n['id']}). Aged out of the {ttl_hours}h cache; durable copy is "
                              f"{persisted} — this signed drilldown returns the row with zero planning",
                              provenance="mined", reads=[persisted],
                              fallback={"tool": "explore_data_catalogue", "args": explore_args}))
        elif persisted:
            explore_payloads.append((t, n, persisted))
    for t in expired:
        persisted = (cards.get(t) or {}).get("persisted_in") or (newest_by_type[t].get("fetch") or {}).get("relation")
        skip.append(_skip("list_realtime_events", {"event_type": t, "tickers": [symbol]},
                          f"{t} aged out of the {ttl_hours}h cache ({newest_by_type[t]['at']}) — returns []. "
                          + (f"Payload lives in {persisted}." if persisted else "")))

    # ---- plan: reaction step ------------------------------------------------------------
    if primary and primary_family in _REACTION_FAMILIES and anchor_age_h is not None:
        eod = next((r for r in freshness if r["relation"] == "eod_stock_prices"), None)
        if market.get("is_open"):
            plan.append(_step("reaction", "detect_intraday_outlier_jumps", {"symbol": symbol, "zscore_threshold": 2.0},
                              "market is OPEN — today's tape is not in eod_stock_prices until tonight's build",
                              provenance="declared"))
        elif anchor_age_h < 24:
            d = (anchor_at or now).astimezone(ET).date().isoformat()
            plan.append(_step("reaction", "get_aftermarket_data",
                              {"symbols": [symbol], "start_datetime": f"{d}T16:00:00", "end_datetime": f"{d}T20:00:00"},
                              f"{primary['type']} landed < 24h ago and the market is closed — the reaction is in the stored after-hours 15-minute bars",
                              provenance="declared"))
        elif eod and eod.get("reflects_newest_event"):
            guidance.append(f"{symbol}: the reaction to {primary['type']} ({primary['at']}) is already in eod_stock_prices "
                            f"(last refresh ≈ {eod['last_refresh_at']}); no intraday call needed.")

    # ---- plan: ONE combined explore job -------------------------------------------------
    # One async job, one poll loop: expired payloads without a signed drilldown, the fresh
    # entailed relations, and the daily tape travel together in a single query.
    covered = {rel for st in plan for rel in st.get("reads") or []}
    parts: list[str] = []
    explore_reads: list[str] = []
    for t, n, persisted in explore_payloads:
        if persisted in covered:
            continue
        date = (parse_ts(n["at"]) or now).astimezone(ET).date().isoformat()
        parts.append(f"the most recent rows of {persisted} on or after {date} ({t} payload)")
        explore_reads.append(persisted)
        covered.add(persisted)
    d = (anchor_at or now).astimezone(ET).date().isoformat()
    fresh_extra = [r for r in fresh_relations if r != "eod_stock_prices" and r not in covered][:5]
    if fresh_extra and primary:
        parts.append(f"the latest rows since {d} from {', '.join(fresh_extra)}")
        explore_reads += fresh_extra
    if "eod_stock_prices" in fresh_relations:
        parts.append("daily open, high, low, close, volume from eod_stock_prices for the last 15 trading days")
        explore_reads.append("eod_stock_prices")
    if parts and (primary or explore_payloads):
        q = f"For {symbol}: " + "; ".join(parts)
        plan.append(_step("explore", "explore_data_catalogue", {"query": q},
                          "ONE combined async job for everything durable: expired payloads plus the relations "
                          f"entailed by {primary['type'] if primary else 'the window'} that have refreshed since it "
                          "(stale ones are in guidance, not here). Poll its single task_id with get_task_status",
                          provenance="pg_depend", reads=explore_reads))

    # ---- plan: context ------------------------------------------------------------------
    snap_at = observed.get("company_snapshot")
    plan.append(_step("context", "get_company_snapshot", {"symbol": symbol},
                      "current position to read the episode against"
                      + (f"; snapshot regenerated {iso(snap_at)}" + (" (after the anchor event)" if anchor_at and snap_at > anchor_at else " (BEFORE the anchor event)") if snap_at else ""),
                      provenance="declared", optional=True))
    skip.append(_skip("generate_research_report", None, "~10-12 minute job; not for a catch-up question"))

    # ---- guidance -------------------------------------------------------------------------
    thirteen_f = [n for n in events if str(n["type"]).startswith("13f_")]
    if thirteen_f:
        ciks = sorted({(n.get("fetch") or {}).get("cik") for n in thirteen_f if (n.get("fetch") or {}).get("cik")})
        guidance.append(f"{symbol}: {len(thirteen_f)} 13F node(s) in the window are FILER-scoped "
                        f"(cik {', '.join(ciks) if ciks else 'n/a'}): one filer's action across several names. "
                        "Report as the filer's move, not as the company's ownership shifting.")
    if newest_event and anchor and newest_event["id"] != anchor["id"] and newest_event["type"] not in membership.get(primary_ep or "", []):
        guidance.append(f"{symbol}: newest node is {newest_event['type']} ({newest_event['at']}) but the dominant "
                        f"episode is {primary_ep} anchored on {anchor['type']} ({anchor['at']}) — lead with the episode.")

    # ---- block ----------------------------------------------------------------------------
    kept = nodes[:WEB_NODE_CAP]
    kept_ids = {n["id"] for n in kept}
    block = {
        "web": {"as_of": web.get("as_of"), "window_days": web.get("window_days"), "nodes": kept,
                "edges": [e for e in web.get("edges") or [] if e.get("from") in kept_ids and e.get("to") in kept_ids],
                "truncated": len(nodes) > WEB_NODE_CAP, "total_nodes": len(nodes)},
        "newest_event": ({k: newest_event.get(k) for k in ("id", "type", "family", "at", "headline")}
                         | {"age_hours": round((now - (parse_ts(newest_event["at"]) or now)).total_seconds() / 3600, 1)})
        if newest_event else None,
        "episode": {"name": primary_ep, "anchor": {k: anchor.get(k) for k in ("id", "type", "at")} if anchor else None,
                    "members": members} if primary_ep else None,
        "cache": {"present": present, "expired": expired,
                  "basis": f"inferred from node age vs realtime_cache_ttl_hours={ttl_hours}"},
        "freshness": freshness,
        "cards": {t: trim_card(cards[t]) for t in payload_types if t in cards},
        "deepen": [tool for tool in ((primary_card or {}).get("deepened_by") or []) if tool != "get_company_snapshot"],
    }
    return block, plan, skip, guidance


# ---------------------------------------------------------------- universe scope
UNIVERSE_PAYLOAD_CAP = 4


def _episode_for_family(family: Optional[str], cards: dict[str, dict[str, Any]],
                        episodes: dict[str, dict[str, Any]]) -> tuple[Optional[str], list[str]]:
    """(episode name, member order) for a family: the episode most of its cards belong to."""
    if not family:
        return None, []
    fam_types = [t for t, c in cards.items() if (c or {}).get("family") == family]
    ep_names = [ep["name"] for t in fam_types for ep in (cards[t].get("episodes") or [])]
    ep_name = max(set(ep_names), key=ep_names.count) if ep_names else None
    order = list((episodes.get(ep_name) or {}).get("order") or []) if ep_name else []
    return ep_name, order or fam_types


def situate_universe(
    family: Optional[str],
    cards: dict[str, dict[str, Any]],
    episodes: dict[str, dict[str, Any]],
    market: dict[str, Any],
    now: datetime,
    asked_types: Optional[list[str]] = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    corpus: Optional[list[dict[str, Any]]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """No symbols: what is going on across the market, planned from what the question NAMED.

    Three shapes, in priority order:
      1. The question names event types ("investor decks today") — step 1 is the first
         named type, the other named types follow, then the rest of that type's episode
         rotated to start after it. The user's noun is never buried at position four.
      2. It names a family but no type ("any earnings today?") — the family's episode in
         order, as before.
      3. It names neither — a market-wide SWEEP: one call per episode entry point across
         the whole realtime corpus (earnings, news, movers, 13F). There is NO default
         family; guessing "earnings" for "what's going on?" answered a different question.
    explore_data_catalogue is never planned here and is put on the skip list: the realtime
    cache IS "today", and an unscoped explore over a persisted relation is a multi-minute
    job over the whole table (the 2026-09-10 outage was a 127 MB result from exactly that).
    `corpus` (list_options("event_types") rows) is echoed in the block so the agent can see
    every type it could ask for without a second enumeration call.
    """
    plan: list[dict[str, Any]] = []
    skip: list[dict[str, Any]] = []
    guidance: list[str] = []
    asked = [t for t in (asked_types or []) if not cards or t in cards]
    if not family and asked:
        family = ((cards.get(asked[0]) or {}).get("family")) or None
    ep_name, ep_order = _episode_for_family(family, cards, episodes)

    # episode markers (eps_market_reaction: no family, no payload) are not fetchable types
    fetchable = [t for t in ep_order if not cards or (cards.get(t) or {}).get("family")]
    if asked:
        shape = "named_types"
        rotated: list[str] = []
        if asked[0] in fetchable:
            i = fetchable.index(asked[0])
            rotated = fetchable[i + 1:] + fetchable[:i]
        order = asked + [t for t in rotated if t not in asked]
    elif family and fetchable:
        shape = "family"
        order = fetchable
    elif family:
        shape = "family"
        order = []
        persisted = sorted({(c or {}).get("persisted_in") for c in cards.values()
                            if (c or {}).get("family") == family and (c or {}).get("persisted_in")})
        guidance.append(f"The ontology has no realtime event types for family '{family}' — nothing to read from the "
                        f"{ttl_hours}h cache. " + (f"Its durable relations: {', '.join(persisted)}." if persisted else
                                                    "Ask get_event_ontology(family=...) for its relations."))
    else:
        shape = "sweep"
        order = []
        sweep_episode: dict[str, str] = {}
        for ep in _SWEEP_ORDER:
            first = next((t for t in (episodes.get(ep) or {}).get("order") or []
                          if (cards.get(t) or {}).get("family")), None)
            if first and first not in order:
                order.append(first)
                sweep_episode[first] = ep
        if not order:  # ontology degraded: the corpus is still known from the vocabulary
            order = ["eps_update", "company_update", "biggest_mover", "13f_significant_position_change"]
            sweep_episode = dict(zip(order, _SWEEP_ORDER))
        guidance.append("The question named neither an event type nor a family, so this is a market-wide sweep: "
                        "one call per episode entry point. If the user meant something specific, the universe.event_types "
                        "list names every realtime type — re-run situate with that word in the question, or call "
                        "list_realtime_events for that type directly.")

    for t in order[:UNIVERSE_PAYLOAD_CAP]:
        c = cards.get(t) or {}
        if t in asked:
            why = f"the question names {t} (asked #{asked.index(t) + 1} of {len(asked)})"
        elif shape == "sweep":
            why = f"market-wide sweep: entry point of the {sweep_episode.get(t, 'realtime')} episode"
        else:
            why = f"{ep_name or family} episode member {ep_order.index(t) + 1 if t in ep_order else '?'} of {len(ep_order)}"
        if c.get("persisted_in"):
            why += f"; persisted in {c['persisted_in']} once out of the cache"
        plan.append(_step("payload", "list_realtime_events", {"event_type": t}, why, provenance="declared",
                          optional=shape != "named_types" and t != order[0]))
    if market.get("is_open") and "biggest_mover" not in order[:UNIVERSE_PAYLOAD_CAP]:
        plan.append(_step("reaction", "list_realtime_events", {"event_type": "biggest_mover"},
                          "market is open — confirmed movers off 30-minute bars", provenance="declared", optional=True))

    persisted_asked = [f"{t} -> {cards[t]['persisted_in']}" for t in asked if (cards.get(t) or {}).get("persisted_in")]
    skip.append(_skip("explore_data_catalogue", None,
                      f"not for a 'today' / 'right now' question: the {ttl_hours}h realtime cache IS today. An unscoped "
                      "explore over a persisted relation is a multi-minute job that returns the whole table. Only if the "
                      f"user asks for MORE than {ttl_hours}h of history: one explore, scoped to ONE relation, an explicit "
                      "date window, and tickers=[...] from the realtime results"
                      + (f" ({'; '.join(persisted_asked)})." if persisted_asked else ".")))
    skip.append(_skip("generate_research_report", None, "~10-12 minute job; not for a what-is-going-on question"))

    if asked:
        guidance.append(f"Step 1 is {asked[0]} because the question names it. Rank and answer from that result; the later "
                        "steps are the rest of its episode for context, not substitutes. Narrow every follow-up with "
                        "tickers=[...] to the symbols just seen.")
    elif order:
        guidance.append(f"Chain from the first non-empty result: narrow every follow-up with tickers=[...] to the symbols "
                        f"just seen. Order: {' -> '.join(order[:UNIVERSE_PAYLOAD_CAP])}.")
    guidance.append(f"An empty list means the {ttl_hours}h cache is cold for that type — say so. It is not evidence nothing "
                    "happened, and it is not a reason to dispatch explore_data_catalogue (see skip).")
    gaps = unmapped_event_types(cards) if cards else []
    if gaps:
        guidance.append(f"routing gap: the ontology has realtime types situate cannot match by keyword yet "
                        f"({', '.join(gaps)}) — call list_realtime_events for them directly if the question means one of them.")

    block: dict[str, Any] = {
        "shape": shape, "family": family, "episode": ep_name, "asked_types": asked, "order": order,
        "cards": {t: trim_card(cards[t]) for t in order[:UNIVERSE_PAYLOAD_CAP] if t in cards},
    }
    if corpus:
        block["event_types"] = [
            {"event_type": r.get("event_type"), "family": r.get("family"),
             "description": (r.get("description") or "").split(" — ")[0].split(". ")[0][:140]}
            for r in corpus if r.get("event_type")
        ]
    elif cards:
        block["event_types"] = [{"event_type": t, "family": c.get("family")} for t, c in cards.items() if (c or {}).get("family")]
    return block, plan, skip, guidance


# --------------------------------------------------------------------- compose
def compose(
    scope: str,
    market: dict[str, Any],
    ttl_hours: int,
    now: datetime,
    symbol_blocks: dict[str, dict[str, Any]],
    universe_block: Optional[dict[str, Any]],
    plan: list[dict[str, Any]],
    skip: list[dict[str, Any]],
    guidance: list[str],
    degraded_notes: list[str],
    ontology_as_of: Optional[str],
) -> dict[str, Any]:
    # dedupe on (tool, args), order by category then optional, cap, number.
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for s in sorted(plan, key=lambda s: (s["optional"], _CATEGORY_ORDER.get(s["_category"], 9))):
        key = s["tool"] + json.dumps(s["args"], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(s)
    ordered = ordered[:MAX_PLAN_STEPS]
    for i, s in enumerate(ordered, 1):
        s.pop("_category", None)
        s["step"] = i
        s = {"step": s.pop("step"), **s}
        ordered[i - 1] = s
    seen_skip: set[str] = set()
    uniq_skip = []
    for k in skip:
        key = k["tool"] + json.dumps(k.get("args"), sort_keys=True)
        if key not in seen_skip:
            seen_skip.add(key)
            uniq_skip.append(k)
    out: dict[str, Any] = {
        "as_of": iso(now),
        "scope": scope,
        "realtime_cache_ttl_hours": ttl_hours,
        "ontology_as_of": ontology_as_of,
        "degraded": bool(degraded_notes),
        "market": market,
    }
    if universe_block is not None:
        out["universe"] = universe_block
    if symbol_blocks:
        out["symbols"] = symbol_blocks
    out["plan"] = ordered
    out["skip"] = uniq_skip
    out["guidance"] = degraded_notes + guidance
    return out
