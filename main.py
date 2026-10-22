"""Kalshi picks bot: scans open markets, reads the news, and suggests trades.

Research tool only. It never places orders.

    python main.py                  # full run (needs ANTHROPIC_API_KEY in .env)
    python main.py --no-ai          # just list candidate markets and their news
    python main.py --sample         # use bundled sample data instead of live Kalshi data
    python main.py --category economics --markets 8
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from kalshi_bot import kalshi, news, tracker
from kalshi_bot.picks import Combo, Pick, build_combos, rank_singles

ROOT = Path(__file__).parent
DISCLAIMER = (
    "Research only, not financial advice. These are an AI's estimates and prediction "
    "markets are usually priced efficiently. Never risk money you can't afford to lose."
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Suggest Kalshi trades from market data and news.")
    p.add_argument("--markets", type=int, default=12, help="how many markets to analyze (default 12)")
    p.add_argument("--category", help="only markets whose category contains this, e.g. economics")
    p.add_argument("--days", type=float, default=3,
                   help="only markets that resolve within N days (default 3)")
    p.add_argument("--min-hours", type=float, default=12,
                   help="skip markets resolving sooner than this, so there's time to research (default 12)")
    p.add_argument("--min-volume", type=int, default=100, help="minimum 24h volume in contracts (default 100)")
    p.add_argument("--min-edge", type=float, default=0.05,
                   help="minimum expected profit per $1 contract after fees (default 0.05)")
    p.add_argument("--max-picks", type=int, default=3, help="most single picks to show (default 3)")
    p.add_argument("--no-ai", action="store_true", help="skip Claude; show candidates and news only")
    p.add_argument("--sample", action="store_true", help="use sample_data/events.json instead of the live API")
    p.add_argument("--save", action="store_true", help="also write the results to output/ as JSON")
    p.add_argument("--scoreboard", action="store_true",
                   help="check results of past picks and show the paper-trading scoreboard")
    p.add_argument("--stake", type=float, default=10.0, help="pretend dollars per paper trade (default 10)")
    return p.parse_args()


def fmt_pick(i: int, pick: Pick) -> str:
    m, est = pick.market, pick.estimate
    return "\n".join([
        f"{i}. BUY {pick.side} @ ${pick.price:.2f}  |  {m.display_title}",
        f"   Market says {m.implied_probability:.0%} YES, bot says {est.probability_yes:.0%} YES "
        f"({est.confidence} confidence)",
        f"   Expected profit per contract: ${pick.edge:+.2f} after fees  |  "
        f"pays {pick.return_if_right:.0%} if right",
        f"   Why: {est.reasoning}",
        f"   {m.url}",
    ])


def fmt_combo(i: int, combo: Combo) -> str:
    legs = "\n".join(f"     - {p.side} on {p.market.display_title} (${p.price:.2f})" for p in combo.legs)
    return (
        f"{i}. {len(combo.legs)}-leg combo  |  est. win chance {combo.win_probability:.0%}, "
        f"est. cost ${combo.price:.2f} per $1 payout\n{legs}\n"
        f"   Assumes the legs are independent. Check Kalshi's actual combo price before trading."
    )


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()

    if args.scoreboard:
        print("Checking Kalshi for results...")
        settled = tracker.settle(ROOT / "output")
        print(f"{settled} results updated.")
        print(tracker.scoreboard(ROOT / "output"))
        return 0

    print("Fetching open Kalshi markets...")
    if args.sample:
        markets = kalshi.parse_events(kalshi.load_sample_events(ROOT / "sample_data" / "events.json"))
    else:
        try:
            markets = kalshi.parse_markets(kalshi.fetch_open_markets(max_days=args.days, min_hours=args.min_hours))
        except Exception as e:  # network or API problem
            print(f"Couldn't reach Kalshi ({e}). Try again, or use --sample to test offline.")
            return 1

    stats: Counter = Counter()
    filters = dict(max_days=args.days, min_hours=args.min_hours, min_volume=args.min_volume)
    if args.sample:
        candidates = kalshi.select_candidates(
            markets, limit=args.markets, category=args.category, one_per_game=True,
            stats=stats, **filters)
    else:
        # Event titles and categories need one request per event, so look them up only for
        # the busiest games, then pick a mix: one market per game and at most a third of
        # the slots from any one category (otherwise sports fills everything).
        pool = kalshi.select_candidates(markets, limit=150, one_per_game=True, stats=stats, **filters)
        kalshi.attach_event_info(pool)
        candidates = kalshi.select_candidates(
            pool, limit=args.markets, category=args.category, one_per_game=True,
            max_per_category=None if args.category else math.ceil(args.markets / 3),
            stats=Counter(), **filters)

    print(f"Found {len(markets)} open markets; looking for ones that resolve "
          f"{args.min_hours:g} hours to {args.days:g} days from now. "
          f"Analyzing the {len(candidates)} most active that fit your filters.")
    if not candidates:
        print("Nothing matched. Markets were skipped because they were:")
        for reason, count in stats.most_common():
            print(f"  {count:>6}  {reason}")
        print("Try a larger --days, a smaller --min-hours or --min-volume, or no --category.")
        return 0

    print("Reading the news...")
    for m in candidates:
        try:
            m.news = news.fetch_headlines(m.display_title)
        except Exception:
            m.news = []

    if args.no_ai:
        for m in candidates:
            print(f"\n- {m.display_title}  [{m.ticker}]  ~{m.implied_probability:.0%} YES, "
                  f"24h volume {m.volume_24h}")
            for n in m.news[:3]:
                print(f"    {n['published']}  {n['title']}")
        return 0

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY isn't set. Add it to a .env file (see .env.example), or run with --no-ai.")
        return 1

    from kalshi_bot.analyst import analyze  # imported here so --no-ai works without the SDK set up

    print(f"Asking Claude to estimate {len(candidates)} markets (this can take a minute)...")
    estimates = analyze(candidates)
    singles = rank_singles(candidates, estimates, min_edge=args.min_edge, max_picks=args.max_picks)
    combos = build_combos(singles)

    print("\n" + "=" * 72)
    print(f"KALSHI PICKS  ·  {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 72)
    if not singles:
        print("\nNo recommended trades right now. Nothing cleared the edge bar, which is the honest")
        print("answer most of the time. Try again later or lower --min-edge.")
    else:
        print("\nSINGLE TRADES")
        for i, pick in enumerate(singles, 1):
            print("\n" + fmt_pick(i, pick))
        if combos:
            print("\nCOMBOS")
            for i, combo in enumerate(combos, 1):
                print("\n" + fmt_combo(i, combo))
    print(f"\n{DISCLAIMER}\n")

    if not args.sample:
        added = tracker.record_run(candidates, estimates, singles, ROOT / "output", stake=args.stake)
        print(f"Paper trading: logged {len(estimates)} estimates and {added} new picks to output/. "
              f"Run `python3 main.py --scoreboard` after they settle.")

    if args.save:
        out = ROOT / "output"
        out.mkdir(exist_ok=True)
        path = out / f"picks-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.write_text(json.dumps({
            "generated_at": datetime.now().isoformat(),
            "singles": [
                {"ticker": p.market.ticker, "market": p.market.display_title, "side": p.side,
                 "price": p.price, "bot_probability_yes": p.estimate.probability_yes,
                 "edge": round(p.edge, 4), "confidence": p.estimate.confidence,
                 "reasoning": p.estimate.reasoning, "evidence": p.estimate.key_evidence}
                for p in singles
            ],
            "combos": [
                {"legs": [f"{p.side} {p.market.ticker}" for p in c.legs],
                 "win_probability": round(c.win_probability, 4), "price": round(c.price, 4)}
                for c in combos
            ],
            "all_estimates": {t: e.model_dump() for t, e in estimates.items()},
        }, indent=2))
        print(f"Saved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
