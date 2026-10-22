"""Ask Claude to estimate the real probability of each market, using the news."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Literal

import anthropic
from pydantic import BaseModel

from .kalshi import Market

SYSTEM_PROMPT = """You are a careful prediction-market analyst. For each Kalshi market you are given \
the contract rules, the current market price, and recent headlines. Estimate the probability that \
the market resolves YES.

Guidelines:
- The market price already reflects most public information. Start from it and move away only \
when the headlines or the rules give a concrete reason. Most of the time your estimate should be \
close to the market.
- Read the resolution rules closely; mispricings often come from wording (exact thresholds, dates, \
data sources).
- Headlines can be stale, duplicated, or irrelevant. Say so when the news doesn't bear on the market.
- Use "high" confidence only when you have specific, recent evidence. Use "low" when you are mostly \
guessing.
- Return one estimate for every ticker you were given."""


class MarketEstimate(BaseModel):
    ticker: str
    probability_yes: float  # 0.0 to 1.0
    confidence: Literal["low", "medium", "high"]
    reasoning: str  # 1 to 3 sentences
    key_evidence: list[str]  # headlines or rule details that drove the estimate


class Analysis(BaseModel):
    estimates: list[MarketEstimate]


def _market_block(m: Market) -> str:
    lines = [
        f"TICKER: {m.ticker}",
        f"Market: {m.display_title}",
        f"Category: {m.category}",
        f"Resolves (expected): {m.resolves_at:%Y-%m-%d %H:%M UTC}" if m.resolves_at else "Resolves: unknown",
        f"Current price: YES ask {m.yes_ask}, YES bid {m.yes_bid} "
        f"(implied chance of YES ~{m.implied_probability:.0%})",
        f"Rules: {m.rules or 'not provided'}",
        "Recent headlines:",
    ]
    if m.news:
        lines += [f"  - [{n['published']}] {n['title']} ({n['source']})" for n in m.news]
    else:
        lines.append("  - none found")
    return "\n".join(lines)


def analyze(markets: list[Market]) -> dict[str, MarketEstimate]:
    """One Claude call for the whole batch. Returns estimates keyed by ticker."""
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"Today is {today}. Estimate each market below.\n\n" + "\n\n---\n\n".join(
        _market_block(m) for m in markets
    )

    response = client.messages.parse(
        model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": os.getenv("CLAUDE_EFFORT", "medium")},
        output_format=Analysis,
    )

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyze this batch. Try different markets.")
    if response.parsed_output is None:
        raise RuntimeError(f"Claude returned no usable analysis (stop reason: {response.stop_reason}).")

    estimates = {}
    for est in response.parsed_output.estimates:
        est.probability_yes = min(max(est.probability_yes, 0.0), 1.0)
        estimates[est.ticker] = est
    return estimates
