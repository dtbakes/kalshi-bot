"""Paper trading: log every estimate and pick, then score them once the markets settle.

Two CSV files in output/ (open them in Excel, Numbers or VS Code):
- estimates.csv: every market Claude looked at, with its odds and the market's odds.
  This is the real test: over many markets, is the bot more accurate than the market?
- paper_trades.csv: every pick, with a pretend stake and its profit or loss once settled.
"""

from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path

from .analyst import MarketEstimate
from .kalshi import BASE_URL, Market, _get_with_retry
from .picks import Pick, taker_fee

ESTIMATE_FIELDS = ["logged_at", "ticker", "market", "category", "resolves_at",
                   "market_prob_yes", "bot_prob_yes", "confidence", "result"]
TRADE_FIELDS = ["logged_at", "ticker", "market", "side", "price", "contracts", "cost",
                "bot_win_prob", "edge", "confidence", "result", "pnl"]


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def record_run(markets: list[Market], estimates: dict[str, MarketEstimate], picks: list[Pick],
               out_dir: Path, stake: float = 10.0) -> int:
    """Append this run's estimates and picks. A market that's already logged and still
    open isn't logged again, so running the bot twice a day doesn't double count.
    Returns how many new paper trades were added."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    est_path = out_dir / "estimates.csv"
    est_rows = _read(est_path)
    open_tickers = {r["ticker"] for r in est_rows if not r["result"]}
    for m in markets:
        est = estimates.get(m.ticker)
        if est is None or m.ticker in open_tickers:
            continue
        est_rows.append({
            "logged_at": now, "ticker": m.ticker, "market": m.display_title, "category": m.category,
            "resolves_at": m.resolves_at.isoformat() if m.resolves_at else "",
            "market_prob_yes": round(m.implied_probability, 4), "bot_prob_yes": round(est.probability_yes, 4),
            "confidence": est.confidence, "result": "",
        })
    _write(est_path, ESTIMATE_FIELDS, est_rows)

    trade_path = out_dir / "paper_trades.csv"
    trade_rows = _read(trade_path)
    open_trades = {r["ticker"] for r in trade_rows if not r["result"]}
    added = 0
    for p in picks:
        if p.market.ticker in open_trades:
            continue
        contracts = max(1, math.floor(stake / p.price))
        cost = contracts * (p.price + taker_fee(p.price))
        trade_rows.append({
            "logged_at": now, "ticker": p.market.ticker, "market": p.market.display_title,
            "side": p.side, "price": p.price, "contracts": contracts, "cost": round(cost, 2),
            "bot_win_prob": round(p.win_probability, 4), "edge": round(p.edge, 4),
            "confidence": p.estimate.confidence, "result": "", "pnl": "",
        })
        added += 1
    _write(trade_path, TRADE_FIELDS, trade_rows)
    return added


def fetch_result(ticker: str) -> str | None:
    """'yes' or 'no' once Kalshi has settled the market, otherwise None."""
    market = _get_with_retry(f"{BASE_URL}/markets/{ticker}", {}, timeout=20, attempts=2).get("market", {})
    result = (market.get("result") or "").lower()
    return result if result in ("yes", "no") else None


def settle(out_dir: Path, fetch=fetch_result) -> int:
    """Look up results for every open row. Returns how many rows newly settled."""
    est_path, trade_path = out_dir / "estimates.csv", out_dir / "paper_trades.csv"
    est_rows, trade_rows = _read(est_path), _read(trade_path)
    results: dict[str, str | None] = {}
    for ticker in {r["ticker"] for r in est_rows + trade_rows if not r["result"]}:
        try:
            results[ticker] = fetch(ticker)
        except Exception:
            results[ticker] = None  # try again next time

    settled = 0
    for r in est_rows:
        if not r["result"] and results.get(r["ticker"]):
            r["result"] = results[r["ticker"]]
            settled += 1
    for r in trade_rows:
        result = results.get(r["ticker"])
        if r["result"] or not result:
            continue
        won = r["side"].lower() == result
        r["result"] = "won" if won else "lost"
        payout = int(r["contracts"]) if won else 0
        r["pnl"] = round(payout - float(r["cost"]), 2)
        settled += 1
    if est_rows:
        _write(est_path, ESTIMATE_FIELDS, est_rows)
    if trade_rows:
        _write(trade_path, TRADE_FIELDS, trade_rows)
    return settled


def _brier(rows: list[dict], key: str) -> float:
    """Mean squared error of the YES probability: 0 is perfect, 0.25 is a coin flip."""
    return sum((float(r[key]) - (r["result"] == "yes")) ** 2 for r in rows) / len(rows)


def scoreboard(out_dir: Path) -> str:
    est_rows = _read(out_dir / "estimates.csv")
    trade_rows = _read(out_dir / "paper_trades.csv")
    lines = ["", "PAPER TRADING SCOREBOARD", "=" * 72]

    done = [r for r in trade_rows if r["result"]]
    lines.append(f"Picks: {len(trade_rows)} logged, {len(done)} settled, "
                 f"{len(trade_rows) - len(done)} still open")
    if done:
        wins = sum(r["result"] == "won" for r in done)
        pnl = sum(float(r["pnl"]) for r in done)
        staked = sum(float(r["cost"]) for r in done)
        expected = sum(float(r["bot_win_prob"]) for r in done) / len(done)
        lines += [
            f"Won {wins} of {len(done)} ({wins / len(done):.0%}); the bot expected {expected:.0%}",
            f"Fake profit/loss: ${pnl:+.2f} on ${staked:.2f} staked ({pnl / staked:+.1%})",
        ]

    scored = [r for r in est_rows if r["result"]]
    lines += ["", f"Accuracy on every market analyzed ({len(scored)} settled of {len(est_rows)}):"]
    if scored:
        bot, market = _brier(scored, "bot_prob_yes"), _brier(scored, "market_prob_yes")
        verdict = "bot is ahead" if bot < market else "market is ahead"
        lines += [
            f"  Bot error score:    {bot:.3f}",
            f"  Market error score: {market:.3f}   (lower is better; {verdict})",
            "  Calibration (when the bot said X% YES, how often was it YES?):",
        ]
        for lo in range(0, 100, 20):
            bucket = [r for r in scored if lo <= float(r["bot_prob_yes"]) * 100 < lo + 20 or
                      (lo == 80 and float(r["bot_prob_yes"]) == 1.0)]
            if bucket:
                actual = sum(r["result"] == "yes" for r in bucket) / len(bucket)
                lines.append(f"    {lo:>3}-{lo + 20}%: YES {actual:.0%} of the time ({len(bucket)} markets)")
    if len(done) < 50:
        lines.append("\nTreat this as noise until about 50 picks have settled.")
    return "\n".join(lines)
