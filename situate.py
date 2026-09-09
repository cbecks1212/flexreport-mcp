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

# Deterministic question -> family. Order matters (first match wins).
_QUESTION_FAMILIES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(13f|hedge fund|holders?|positions?|institution\w*)\b"), "institutional_ownership"),
    (re.compile(r"\binsider\w*\b"), "insider"),
    (re.compile(r"\b(ratings?|price target|targets?|upgrade\w*|downgrade\w*|analysts?)\b"), "analyst_activity"),
    (re.compile(r"\b(earnings|eps|print|call|guidance|transcript|8-?k|results?)\b"), "earnings"),
    (re.compile(r"\b(news|headlines?)\b"), "news"),
    (re.compile(r"\b(movers?|moving|tape|trading|intraday|why is it (up|down))\b"), "market_movement"),
]

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


def question_family(question: Optional[str]) -> Optional[str]:
    if not question:
        return None
    q = question.lower()
    for pattern, family in _QUESTION_FAMILIES:
        if pattern.search(q):
            return family
    return None


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
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return (symbols[S] block, plan steps, skip entries, guidance lines) for one symbol."""
    plan: list[dict[str, Any]] = []
    skip: list[dict[str, Any]] = []
    guidance: list[str] = []

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
    if question_fam:
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
        key=lambda t: (not (anchor and t == anchor["type"]), t not in in_episode,
                       -(parse_ts(newest_by_type[t]["at"]) or now).timestamp()),
    )[:3]
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
def situate_universe(
    family: str,
    cards: dict[str, dict[str, Any]],
    episodes: dict[str, dict[str, Any]],
    market: dict[str, Any],
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """No symbols: order the family's episode and hand back one list_realtime_events per member."""
    plan: list[dict[str, Any]] = []
    guidance: list[str] = []
    fam_types = [t for t, c in cards.items() if c.get("family") == family]
    ep_names = [ep["name"] for t in fam_types for ep in (cards[t].get("episodes") or [])]
    ep_name = max(set(ep_names), key=ep_names.count) if ep_names else None
    order = (episodes.get(ep_name) or {}).get("order") or fam_types if ep_name else fam_types
    if not order:
        order = ["eps_update", "8k_release", "biggest_mover"]
        guidance.append(f"ontology has no cards for family '{family}' — falling back to a default event order")
    for t in order[:4]:
        c = cards.get(t) or {}
        plan.append(_step("payload", "list_realtime_events", {"event_type": t},
                          f"{ep_name or family} episode member {order.index(t) + 1} of {len(order)}"
                          + (f"; persisted in {c['persisted_in']} once out of the cache" if c.get("persisted_in") else ""),
                          provenance="declared"))
    if market.get("is_open"):
        plan.append(_step("reaction", "list_realtime_events", {"event_type": "biggest_mover"},
                          "market is open — confirmed movers off 30-minute bars", provenance="declared", optional=True))
    guidance.append(f"Chain from the first non-empty result: narrow every follow-up with tickers=[...] "
                    f"to the symbols just seen. Episode order for {ep_name or family}: {' -> '.join(order)}.")
    guidance.append("An empty list means the 12h cache is cold for that type — say so; read the card's "
                    "persisted_in relation with explore_data_catalogue for anything older.")
    block = {"family": family, "episode": ep_name, "order": order,
             "cards": {t: trim_card(cards[t]) for t in order[:4] if t in cards}}
    return block, plan, [], guidance


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
