"""Recent headlines from Google News RSS (free, no API key)."""

from __future__ import annotations

import re
import time
from urllib.parse import quote_plus

import feedparser

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# Words that make Kalshi titles long but don't help a news search.
_FILLER = re.compile(
    r"\b(will|the|be|by|on|in|of|a|an|to|before|after|above|below|or|more|less|than|"
    r"what|who|how|many|much|which|between|at|end|this|year|month|week)\b|\?",
    re.IGNORECASE,
)


def build_query(title: str, max_words: int = 8) -> str:
    words = _FILLER.sub(" ", title).split()
    return " ".join(words[:max_words])


def fetch_headlines(title: str, limit: int = 5, recent_days: int = 7) -> list[dict]:
    """Return up to `limit` recent headlines related to a market title.
    Network errors return an empty list so one bad feed doesn't stop a run."""
    query = f"{build_query(title)} when:{recent_days}d"
    feed = feedparser.parse(GOOGLE_NEWS_RSS.format(query=quote_plus(query)))
    headlines = []
    for entry in feed.entries[:limit]:
        published = entry.get("published_parsed")
        headlines.append(
            {
                "title": entry.get("title", ""),
                "source": entry.get("source", {}).get("title", ""),
                "published": time.strftime("%Y-%m-%d", published) if published else "",
                "link": entry.get("link", ""),
            }
        )
    return headlines
