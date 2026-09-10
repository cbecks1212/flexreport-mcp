"""Routing tests for situate: a question that names a realtime event type must start the
plan at that type; a question naming nothing must not be answered as if it were about
earnings; explore_data_catalogue must never be planned for a market-wide "today" question.

Fixtures are snapshots of GET /get-event-ontology (cards) and /list-realtime-event-options
(the corpus) taken 2026-09-10. Run with `python -m pytest tests/` or
`python tests/test_situate_routing.py`.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import situate  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
ONTO = json.loads((FIX / "event_ontology_cards.json").read_text())
CARDS = ONTO["event_types"]
EPISODES = ONTO["episodes"]
CORPUS = json.loads((FIX / "realtime_event_types.json").read_text())
NOW = datetime(2026, 9, 10, 15, 30, tzinfo=timezone.utc)
CLOSED = {"exchange": "NYSE", "is_open": False, "session": "closed"}
OPEN = {"exchange": "NYSE", "is_open": True, "session": "regular"}

# (question, first type the plan must start at)
NAMED_TYPE_CASES = [
    ("I'm looking for the latest investor deck's today. What are the most significant ones today", "ir_publication"),
    ("What are the most significant investor presentations today?", "ir_publication"),
    ("any new IR decks this morning", "ir_publication"),
    ("which slides came out today", "ir_publication"),
    ("any 8-Ks today?", "8k_release"),
    ("new 8k filings", "8k_release"),
    ("what did management say on the calls today", "transcript_update"),
    ("new transcripts", "transcript_update"),
    ("any 10-Qs out today", "financials_release"),
    ("what are the earnings themes this season", "earnings_themes"),
    ("who reported this morning", "eps_release"),
    ("who beat and who missed", "eps_update"),
    ("any upgrades or downgrades today", "realtime_ratings_update"),
    ("price target changes today", "realtime_ratings_update"),
    ("consensus revisions today", "financial_estimate_update"),
    ("biggest gainers today", "biggest_gainer"),
    ("what's tanking today", "biggest_loser"),
    ("biggest losers", "biggest_loser"),
    ("biggest movers right now", "biggest_mover"),
    ("which hedge funds exited positions", "13f_exited"),
    ("new 13F positions opened", "13f_new"),
    ("13F position changes", "13f_significant_position_change"),
    ("what's the news flow look like", "news_evolution"),
    ("any breaking news", "company_update"),
    ("strategy portfolio changes today", "strategy_update"),
    ("thematic basket updates", "llm_basket_update"),
    ("latest predicted returns", "stock_return_prediction_update"),
]

FAMILY_ONLY_CASES = [
    ("any earnings today?", "earnings"),
    ("how's the market trading", "market_movement"),
    ("anything on the earnings front?", "earnings"),
]

NO_MATCH_CASES = [
    "what's going on?",
    "catch me up",
    "anything interesting today",
]


def _universe(question: str, market=CLOSED):
    types = situate.question_event_types(question, CARDS)
    fam = situate.question_family(question, types, CARDS)
    block, plan, skip, guidance = situate.situate_universe(fam, CARDS, EPISODES, market, NOW, types, 12, CORPUS)
    return types, fam, block, plan, skip, guidance


def test_vocabulary_covers_the_whole_corpus():
    """Every realtime type the backend can return has words that reach it. Fails when the
    corpus grows without a vocabulary row — the gap the deck question fell through."""
    assert situate.unmapped_event_types(CARDS) == []
    corpus_types = {r["event_type"] for r in CORPUS}
    assert corpus_types <= set(situate.EVENT_VOCABULARY), corpus_types - set(situate.EVENT_VOCABULARY)
    assert set(situate.EVENT_VOCABULARY) <= set(CARDS), set(situate.EVENT_VOCABULARY) - set(CARDS)


def test_named_types_start_the_plan():
    for q, first in NAMED_TYPE_CASES:
        types, fam, block, plan, _, _ = _universe(q)
        assert types and types[0] == first, f"{q!r}: got {types}"
        assert fam == CARDS[first]["family"], f"{q!r}: family {fam}"
        assert block["shape"] == "named_types"
        assert plan[0]["tool"] == "list_realtime_events" and plan[0]["args"] == {"event_type": first}, (q, plan[0])
        assert plan[0]["optional"] is False


def test_deck_question_rotates_the_earnings_episode():
    _, _, block, plan, _, guidance = _universe(NAMED_TYPE_CASES[0][0])
    order = block["order"]
    assert order[0] == "ir_publication"
    # rest of the earnings episode follows AFTER ir_publication, wrapping round
    ep = [t for t in EPISODES["earnings"]["order"] if CARDS[t]["family"]]  # markers excluded
    i = ep.index("ir_publication")
    assert order[1:] == ep[i + 1:] + ep[:i]
    assert "eps_market_reaction" not in order
    assert [s["args"]["event_type"] for s in plan[:situate.UNIVERSE_PAYLOAD_CAP]] == order[:situate.UNIVERSE_PAYLOAD_CAP]
    assert any("Step 1 is ir_publication" in g for g in guidance)


def test_family_only_questions_walk_the_episode():
    for q, family in FAMILY_ONLY_CASES:
        types, fam, block, plan, _, _ = _universe(q)
        assert types == [], (q, types)
        assert fam == family, (q, fam)
        assert block["shape"] == "family", (q, block["shape"])
        assert plan[0]["args"]["event_type"] == block["order"][0]


def test_no_match_is_a_sweep_not_earnings():
    for q in NO_MATCH_CASES:
        types, fam, block, plan, _, guidance = _universe(q)
        assert types == [] and fam is None, (q, types, fam)
        assert block["shape"] == "sweep"
        planned = [s["args"]["event_type"] for s in plan if s["tool"] == "list_realtime_events"]
        assert planned[0] == EPISODES["earnings"]["order"][0]
        assert set(planned) >= {"company_update", "biggest_mover"}, planned
        assert len({CARDS[t]["family"] for t in planned}) >= 3, "a sweep spans families"
        assert any("market-wide sweep" in g for g in guidance)


def test_question_family_never_defaults():
    assert situate.question_family(None) is None
    assert situate.question_family("") is None
    assert situate.question_family("what's going on?") is None


def test_explore_is_skipped_never_planned():
    for q, _ in NAMED_TYPE_CASES + FAMILY_ONLY_CASES:
        _, _, _, plan, skip, guidance = _universe(q)
        assert all(s["tool"] != "explore_data_catalogue" for s in plan), q
        assert any(k["tool"] == "explore_data_catalogue" for k in skip), q
        assert not any("with explore_data_catalogue for anything older" in g for g in guidance), q
    _, _, _, _, skip, _ = _universe(NAMED_TYPE_CASES[0][0])
    why = next(k["why"] for k in skip if k["tool"] == "explore_data_catalogue")
    assert "ir_publication -> ir_documents" in why


def test_corpus_is_echoed_in_the_block():
    _, _, block, _, _, _ = _universe("what's going on?")
    types = {r["event_type"]: r for r in block["event_types"]}
    assert set(types) == {r["event_type"] for r in CORPUS}
    assert types["ir_publication"]["family"] == "earnings"
    assert "investor-relations" in types["ir_publication"]["description"]


def test_open_market_adds_movers_once():
    _, _, _, plan, _, _ = _universe("biggest movers right now", OPEN)
    movers = [s for s in plan if s["args"] == {"event_type": "biggest_mover"}]
    assert len(movers) == 1 and movers[0]["optional"] is False


def test_unknown_types_are_dropped_against_the_ontology():
    trimmed = {t: c for t, c in CARDS.items() if t != "ir_publication"}
    assert situate.question_event_types("investor decks today", trimmed) == []
    assert situate.question_event_types("investor decks today") == ["ir_publication"]


def test_multiple_named_types_keep_user_order():
    types = situate.question_event_types("8-Ks and the decks that came with them", CARDS)
    assert types == ["8k_release", "ir_publication"]
    _, _, block, plan, _, _ = _universe("8-Ks and the decks that came with them")
    assert block["order"][:2] == ["8k_release", "ir_publication"]


def test_symbol_scope_leads_with_the_named_type():
    web = {"as_of": "2026-09-10T14:00:00Z", "window_days": 7, "edges": [], "nodes": [
        {"id": "n1", "kind": "event", "type": "eps_update", "at": "2026-09-10T12:30:00Z", "headline": "beat"},
        {"id": "n2", "kind": "event", "type": "ir_publication", "at": "2026-09-10T12:00:00Z", "headline": "deck"},
        {"id": "n3", "kind": "event", "type": "8k_release", "at": "2026-09-10T11:50:00Z", "headline": "8-K"},
    ]}
    types = situate.question_event_types("OKTA's deck", CARDS)
    block, plan, _, _ = situate.situate_symbol("OKTA", web, CARDS, {}, EPISODES, 12, CLOSED, NOW, "earnings", types)
    payload = [s for s in plan if s["tool"] == "list_realtime_events"]
    assert payload[0]["args"] == {"event_type": "ir_publication", "tickers": ["OKTA"]}


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL  {name}: {e}")
    sys.exit(1 if failed else 0)
