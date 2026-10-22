from pathlib import Path

import pytest

from kalshi_bot import kalshi
from kalshi_bot.analyst import MarketEstimate
from kalshi_bot.news import build_query
from kalshi_bot.picks import build_combos, rank_singles, taker_fee

SAMPLE = Path(__file__).parent.parent / "sample_data" / "events.json"


@pytest.fixture
def candidates():
    markets = kalshi.parse_events(kalshi.load_sample_events(SAMPLE))
    return kalshi.select_candidates(markets, limit=20, max_days=30, min_volume=500)


def est(ticker, p, conf="medium"):
    return MarketEstimate(ticker=ticker, probability_yes=p, confidence=conf,
                          reasoning="test", key_evidence=[])


def test_parses_cents_and_dollar_prices(candidates):
    by_ticker = {m.ticker: m for m in candidates}
    assert by_ticker["KXFEDDECISION-26OCT-C25"].yes_ask == pytest.approx(0.64)
    assert by_ticker["KXCPIYOY-26SEP-T2.8"].yes_ask == pytest.approx(0.43)


def test_filters_near_certain_and_illiquid(candidates):
    tickers = {m.ticker for m in candidates}
    assert "KXBTCD-26OCT10-T150000" not in tickers  # 2% priced
    assert "KXBTCD-26OCT10-T120000" not in tickers  # low volume
    assert candidates[0].ticker == "KXNFLGAME-26OCT11KCBUF-KC"  # most volume first


def test_taker_fee():
    assert taker_fee(0.50) == pytest.approx(0.02)
    assert taker_fee(0.05) == pytest.approx(0.01)


def test_no_picks_when_bot_agrees_with_market(candidates):
    estimates = {m.ticker: est(m.ticker, m.implied_probability) for m in candidates}
    assert rank_singles(candidates, estimates, min_edge=0.05, max_picks=3) == []


def test_picks_yes_and_no_sides(candidates):
    estimates = {m.ticker: est(m.ticker, m.implied_probability) for m in candidates}
    estimates["KXFEDDECISION-26OCT-C25"] = est("KXFEDDECISION-26OCT-C25", 0.80, "high")
    estimates["KXCPIYOY-26SEP-T2.8"] = est("KXCPIYOY-26SEP-T2.8", 0.25)
    picks = rank_singles(candidates, estimates, min_edge=0.05, max_picks=3)
    sides = {p.market.ticker: p.side for p in picks}
    assert sides == {"KXFEDDECISION-26OCT-C25": "YES", "KXCPIYOY-26SEP-T2.8": "NO"}
    assert picks[0].market.ticker == "KXFEDDECISION-26OCT-C25"  # bigger, higher-confidence edge


def test_low_confidence_needs_double_edge(candidates):
    estimates = {"KXNFLGAME-26OCT11KCBUF-KC": est("KXNFLGAME-26OCT11KCBUF-KC", 0.57, "low")}
    assert rank_singles(candidates, estimates, min_edge=0.05, max_picks=3) == []


def test_combos_use_different_events_only(candidates):
    estimates = {
        "KXFEDDECISION-26OCT-C25": est("KXFEDDECISION-26OCT-C25", 0.85, "high"),
        "KXFEDDECISION-26OCT-H0": est("KXFEDDECISION-26OCT-H0", 0.10, "high"),
        "KXCPIYOY-26SEP-T2.8": est("KXCPIYOY-26SEP-T2.8", 0.20, "high"),
    }
    singles = rank_singles(candidates, estimates, min_edge=0.05, max_picks=5)
    combos = build_combos(singles)
    assert combos
    for c in combos:
        events = [p.market.event_ticker for p in c.legs]
        assert len(events) == len(set(events))
        assert c.edge > 0


def test_news_query_drops_filler():
    assert build_query("Will the Fed cut rates in October?") == "Fed cut rates October"
