"""Turn probability estimates into ranked picks: singles and combos."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations

from .analyst import MarketEstimate
from .kalshi import Market

CONFIDENCE_WEIGHT = {"low": 0.5, "medium": 0.8, "high": 1.0}


def taker_fee(price: float) -> float:
    """Kalshi's standard taker fee for one contract: 7% of P*(1-P), rounded up to the cent.
    Check kalshi.com/fee-schedule; some markets use a different rate."""
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


@dataclass
class Pick:
    market: Market
    estimate: MarketEstimate
    side: str  # "YES" or "NO"
    price: float  # cost of one contract in dollars (pays $1 if right)
    win_probability: float  # bot's estimate that this side wins
    edge: float  # expected profit per contract after fees, in dollars

    @property
    def score(self) -> float:
        return self.edge * CONFIDENCE_WEIGHT[self.estimate.confidence]

    @property
    def return_if_right(self) -> float:
        return (1 - self.price) / self.price


@dataclass
class Combo:
    legs: list[Pick]

    @property
    def win_probability(self) -> float:
        return math.prod(p.win_probability for p in self.legs)

    @property
    def price(self) -> float:
        return math.prod(p.price for p in self.legs)

    @property
    def edge(self) -> float:
        return self.win_probability - self.price


def best_side(market: Market, est: MarketEstimate) -> Pick | None:
    q = est.probability_yes
    options = []
    if market.yes_ask:
        options.append(("YES", market.yes_ask, q))
    no_ask = market.no_ask or (round(1 - market.yes_bid, 2) if market.yes_bid else None)
    if no_ask:
        options.append(("NO", no_ask, 1 - q))
    best = None
    for side, price, win_prob in options:
        if not 0 < price < 1:
            continue
        edge = win_prob - price - taker_fee(price)
        if best is None or edge > best.edge:
            best = Pick(market, est, side, price, win_prob, edge)
    return best


def rank_singles(
    markets: list[Market],
    estimates: dict[str, MarketEstimate],
    *,
    min_edge: float,
    max_picks: int,
) -> list[Pick]:
    """Picks clear the edge bar (low-confidence picks must clear twice the bar).
    Returning zero picks is a normal, honest outcome."""
    picks = []
    for m in markets:
        est = estimates.get(m.ticker)
        if est is None:
            continue
        pick = best_side(m, est)
        if pick is None:
            continue
        bar = min_edge * (2 if est.confidence == "low" else 1)
        if pick.edge >= bar:
            picks.append(pick)
    picks.sort(key=lambda p: p.score, reverse=True)
    return picks[:max_picks]


def build_combos(singles: list[Pick], *, max_legs: int = 3, max_combos: int = 2) -> list[Combo]:
    """Combine picks from different events. Assumes the legs are independent, which
    is rough: related markets (say, two Fed questions) move together."""
    eligible = [p for p in singles if p.estimate.confidence != "low"]
    combos = []
    for size in range(2, max_legs + 1):
        for legs in combinations(eligible, size):
            if len({p.market.event_ticker for p in legs}) < size:
                continue
            combo = Combo(list(legs))
            if combo.edge > 0 and combo.win_probability >= 0.15:
                combos.append(combo)
    combos.sort(key=lambda c: c.edge, reverse=True)
    return combos[:max_combos]
