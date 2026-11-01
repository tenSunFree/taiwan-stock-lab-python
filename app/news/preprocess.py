"""
Python-side preprocessing BEFORE any LLM call.

window filter -> exact dedup -> title normalisation -> similarity
clustering => *candidate* groups (the LLM analyzer confirms them; if it
fails, every article becomes its own event: prefer under-merging).
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.news.models import TAIPEI_TZ, RawArticle

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|ref$)", re.I)


def normalize_title(title: str, *, stock_id: str = "", stock_name: str = "") -> str:
    text = unicodedata.normalize("NFKC", title).lower()
    for token in (stock_name, stock_id):
        if token:
            text = text.replace(unicodedata.normalize("NFKC", token).lower(), "")
    return _PUNCT.sub("", text)


def canonical_url(url: str) -> str:
    """Lower-case host, drop fragment / tracking params / trailing slash."""
    p = urlsplit(url.strip())
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k))
    )
    return urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", query, "")
    )


def trading_days_window_start(
    as_of: dt.date, n_trading_days: int, is_trading_day: Callable[[dt.date], bool]
) -> dt.date:
    """Earliest date inside the last n trading days (as_of counts if it
    is a trading day)."""
    if n_trading_days < 1:
        raise ValueError("n_trading_days must be >= 1")
    day, found = as_of, 0
    for _ in range(366):
        if is_trading_day(day):
            found += 1
            if found == n_trading_days:
                return day
        day -= dt.timedelta(days=1)
    raise RuntimeError("not enough trading days within a year")


def filter_window(
    articles: Iterable[RawArticle], *, start: dt.date, end: dt.date
) -> list[RawArticle]:
    """Uses published_at (never fetched_at); inclusive both ends."""
    return [
        a
        for a in articles
        if start <= a.published_at.astimezone(TAIPEI_TZ).date() <= end
    ]


def dedup_exact(articles: Iterable[RawArticle]) -> list[RawArticle]:
    """
    Official open data has no per-item URL, so it dedups by article_id
    only (a shared source_url must never merge distinct announcements).
    Media news dedups by article_id OR canonical original_url.
    """
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    out: list[RawArticle] = []
    for a in sorted(articles, key=lambda x: x.published_at):
        if a.article_id in seen_ids:
            continue
        if a.original_url:
            cu = canonical_url(a.original_url)
            if cu in seen_urls:
                continue
            seen_urls.add(cu)
        seen_ids.add(a.article_id)
        out.append(a)
    return out


def cluster_candidates(
    articles: list[RawArticle],
    *,
    threshold: float = 0.60,
    stock_names: dict[str, str] | None = None,
) -> list[list[RawArticle]]:
    """Group same-stock articles with similar normalised titles
    (transitive, union-find)."""
    stock_names = stock_names or {}
    parent = list(range(len(articles)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    norm = [
        normalize_title(
            a.title, stock_id=a.stock_id, stock_name=stock_names.get(a.stock_id, "")
        )
        for a in articles
    ]
    for i in range(len(articles)):
        for j in range(i + 1, len(articles)):
            if articles[i].stock_id != articles[j].stock_id:
                continue
            x, y = norm[i], norm[j]
            if not x or not y:
                continue
            contained = (x in y or y in x) and min(len(x), len(y)) >= 4
            if contained or SequenceMatcher(None, x, y).ratio() >= threshold:
                parent[find(i)] = find(j)

    groups: dict[int, list[RawArticle]] = {}
    for idx, art in enumerate(articles):
        groups.setdefault(find(idx), []).append(art)
    return [sorted(g, key=lambda x: x.published_at) for g in groups.values()]
