"""Read-only access to Kalshi's public market data API (no account or key needed)."""

from __future__ import annotations

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass
class Market:
    ticker: str
    event_ticker: str
    event_title: str
    title: str
    category: str
    yes_bid: float | None  # prices are in dollars, 0.00 to 1.00
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume_24h: int
    open_interest: int
    close_time: datetime | None
    resolves_at: datetime | None = None  # when the outcome is known (e.g. game end); trading closes later
    rules: str = ""
    news: list[dict] = field(default_factory=list)

    @property
    def implied_probability(self) -> float | None:
        """Market's implied chance of YES: the bid/ask midpoint, else the last trade."""
        if self.yes_bid is not None and self.yes_ask is not None and self.yes_ask > 0:
            return (self.yes_bid + self.yes_ask) / 2
        return self.last_price

    @property
    def display_title(self) -> str:
        if self.title and self.title != self.event_title:
            return f"{self.event_title}: {self.title}"
        return self.event_title or self.title

    @property
    def url(self) -> str:
        return f"https://kalshi.com/markets/{self.event_ticker.lower()}"


def _price(raw: dict, name: str) -> float | None:
    """Kalshi has reported prices both as cents (int) and as dollar strings
    ("0.5600" under a *_dollars key). Accept either and return dollars."""
    dollars = raw.get(f"{name}_dollars")
    if dollars not in (None, ""):
        return float(dollars)
    cents = raw.get(name)
    if cents is None:
        return None
    return float(cents) / 100


def _int(raw: dict, name: str) -> int:
    value = raw.get(name)
    if value is None:
        value = raw.get(f"{name}_fp", 0)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_market(raw: dict, event: dict | None = None) -> Market:
    event = event or {}
    return Market(
        ticker=raw["ticker"],
        event_ticker=event.get("event_ticker") or raw.get("event_ticker", ""),
        # Without event details, fall back to the market's own question ("Will WTI be >99.99 on...?")
        event_title=event.get("title") or raw.get("title", ""),
        title=raw.get("yes_sub_title") or raw.get("subtitle") or "",
        category=event.get("category", ""),
        yes_bid=_price(raw, "yes_bid"),
        yes_ask=_price(raw, "yes_ask"),
        no_bid=_price(raw, "no_bid"),
        no_ask=_price(raw, "no_ask"),
        last_price=_price(raw, "last_price"),
        volume_24h=_int(raw, "volume_24h"),
        open_interest=_int(raw, "open_interest"),
        close_time=_time(raw.get("close_time")),
        # Sports markets keep trading open ~2 days after the game, so close_time alone
        # makes tonight's game look like it's days away. expected_expiration_time is the real date.
        resolves_at=_time(raw.get("expected_expiration_time") or raw.get("occurrence_datetime")
                          or raw.get("close_time")),
        rules=(raw.get("rules_primary") or "")[:600],
    )


def _is_open(raw: dict) -> bool:
    return raw.get("status") in (None, "active", "open")


def parse_events(events: list[dict]) -> list[Market]:
    """Markets nested inside /events responses (used for the sample data)."""
    return [_to_market(raw, event) for event in events for raw in event.get("markets", []) if _is_open(raw)]


def parse_markets(raw_markets: list[dict]) -> list[Market]:
    """Markets from the flat /markets endpoint. Add event titles later with attach_event_info."""
    return [_to_market(raw) for raw in raw_markets if _is_open(raw)]


def _get_with_retry(url: str, params: dict, timeout: int, attempts: int = 4) -> dict:
    """GET JSON, retrying timeouts and 5xx errors (Kalshi sometimes returns 504 under load)."""
    for attempt in range(attempts):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp.json()
            error: Exception = requests.HTTPError(f"{resp.status_code} from Kalshi")
        except (requests.Timeout, requests.ConnectionError) as e:
            error = e
        if attempt < attempts - 1:
            time.sleep(2 ** attempt)
    raise error


def fetch_open_markets(max_days: float, min_hours: float = 0, max_pages: int = 150,
                       timeout: int = 30) -> list[dict]:
    """Fetch open markets that could resolve between `min_hours` and `max_days` from now.
    Trading can close up to ~2 weeks after the outcome is known, so the close-time window
    is padded; select_candidates then filters on the actual resolution time.
    The /events endpoint lists far-future events first, so we query /markets by close time instead.
    Multi-leg combo markets are excluded; they have almost no volume of their own.
    If a later page keeps failing, we keep the markets already loaded rather than give up."""
    now = datetime.now(timezone.utc)
    params = {
        "status": "open",
        "limit": 200,  # bigger pages make Kalshi time out
        "mve_filter": "exclude",
        "min_close_ts": int((now + timedelta(hours=min_hours)).timestamp()),
        "max_close_ts": int((now + timedelta(days=max_days + 14)).timestamp()),
    }
    markets: list[dict] = []
    for _ in range(max_pages):
        try:
            data = _get_with_retry(f"{BASE_URL}/markets", params, timeout)
        except Exception as e:
            if not markets:
                raise
            print(f"  (Kalshi stopped responding after {len(markets)} markets: {e}. Continuing with those.)")
            break
        markets.extend(data.get("markets", []))
        if not data.get("cursor"):
            break
        params["cursor"] = data["cursor"]
    return markets


def attach_event_info(markets: list[Market], timeout: int = 20) -> None:
    """Fill in event title and category (one request per distinct event, a few at a time)."""

    def fetch(event_ticker: str) -> dict:
        try:
            return _get_with_retry(f"{BASE_URL}/events/{event_ticker}", {}, timeout, attempts=2).get("event", {})
        except Exception:
            return {}

    tickers = sorted({m.event_ticker for m in markets})
    with ThreadPoolExecutor(max_workers=8) as pool:
        events = dict(zip(tickers, pool.map(fetch, tickers)))
    for m in markets:
        event = events.get(m.event_ticker, {})
        m.event_title = event.get("title") or m.event_title
        m.category = event.get("category") or m.category


def load_sample_events(path: Path) -> list[dict]:
    """Bundled offline data. Sample markets store "closes_in_days" so they never go stale."""
    events = json.loads(path.read_text())["events"]
    now = datetime.now(timezone.utc)
    for event in events:
        for raw in event["markets"]:
            days = raw.pop("closes_in_days", None)
            if days is not None:
                raw["close_time"] = (now + timedelta(days=days)).isoformat()
    return events


def game_key(event_ticker: str) -> str:
    """Group markets about the same game. Kalshi puts the game's markets under different
    series (KXNFLGAME-26OCT05ATLNO, KXNFLSPREAD-26OCT05ATLNO, ...) that share a suffix.
    Short suffixes are just dates (KXWTI-26NOV03), so those keep their full ticker."""
    suffix = event_ticker.split("-", 1)[-1]
    return suffix if len(suffix) > 7 else event_ticker


def select_candidates(
    markets: list[Market],
    *,
    limit: int,
    max_days: float,
    min_volume: int,
    min_hours: float = 0,
    category: str | None = None,
    max_spread: float = 0.15,
    one_per_game: bool = False,
    max_per_category: int | None = None,
    now: datetime | None = None,
    stats: Counter | None = None,
) -> list[Market]:
    """Keep liquid markets that resolve soon and aren't already near-certain, then return
    the most actively traded ones. `one_per_game` keeps only the busiest market per game,
    and `max_per_category` stops one category (usually sports) filling every slot.
    Pass a Counter as `stats` to see why markets were skipped."""
    now = now or datetime.now(timezone.utc)
    stats = stats if stats is not None else Counter()
    eligible = []
    for m in markets:
        prob = m.implied_probability
        if prob is None or not 0.05 <= prob <= 0.95:
            stats["priced below 5% or above 95%"] += 1
        elif m.yes_bid is not None and m.yes_ask is not None and m.yes_ask - m.yes_bid > max_spread:
            stats[f"bid/ask spread wider than {max_spread:.2f}"] += 1
        elif m.volume_24h < min_volume:
            stats[f"under {min_volume} contracts traded in 24h"] += 1
        elif m.resolves_at is None or m.resolves_at < now + timedelta(hours=min_hours):
            stats[f"resolves in under {min_hours:g} hours (or already started)"] += 1
        elif m.resolves_at > now + timedelta(days=max_days):
            stats[f"resolves more than {max_days:g} days out"] += 1
        elif category and category.lower() not in m.category.lower():
            stats[f"not in category '{category}'"] += 1
        else:
            eligible.append(m)

    eligible.sort(key=lambda m: m.volume_24h, reverse=True)
    picked: list[Market] = []
    overflow: list[Market] = []  # held back by the category cap; used if slots are left over
    games: set[str] = set()
    per_category: Counter = Counter()
    for m in eligible:
        if len(picked) >= limit:
            break
        if one_per_game and game_key(m.event_ticker) in games:
            stats["another market from the same game was already picked"] += 1
            continue
        if max_per_category and per_category[m.category] >= max_per_category:
            overflow.append(m)
            continue
        picked.append(m)
        games.add(game_key(m.event_ticker))
        per_category[m.category] += 1
    for m in overflow:
        if len(picked) >= limit:
            break
        if not (one_per_game and game_key(m.event_ticker) in games):
            picked.append(m)
            games.add(game_key(m.event_ticker))
    return picked
