"""Pick one representative announcement per stock (rule based, no LLM).

Event type, importance tier and publication time are kept separate:
  * EventType (classify.py) is a coarse title-only label.
  * Tier below is a V1 *heuristic* attached to the type, not a measured
    materiality.  A title alone cannot show how big an event is (a 財報
    title does not say whether profit collapsed), so nothing here claims
    to; "upgrade on big profit swing" needs 說明 parsing and is deferred.
  * Time is only a tie-breaker, never a score that can beat a tier.

Order within one stock: tier, then per-type priority, then the FIRST
publication time (newer wins), then article_id so results are stable.
A notice re-published for months is judged by its first date, never its
last one, and `since` drops notices first announced before the window.

Only official TWSE/TPEx notices are candidates in V1.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable

from app.news.classify import EventType, classify_title, is_subsidiary_notice
from app.news.preprocess import RepeatedNotice


class Tier(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


# V1 heuristics. Change here (and in the tests), nothing else reads them.
BASE_TIER: dict[EventType, Tier] = {
    EventType.INCIDENT_OR_CLARIFICATION: Tier.HIGH,
    EventType.CONTRACT: Tier.HIGH,
    EventType.CAPITAL_AND_DEBT: Tier.HIGH,
    EventType.FINANCIAL_WARNING: Tier.HIGH,
    # Title-only: most are routine 取得機器設備 / 處分有價證券 / 使用權資產,
    # so not HIGH. Splitting out a genuinely large deal needs 說明 amounts.
    EventType.ASSET_TRANSACTION: Tier.MEDIUM,
    EventType.FINANCIAL_REPORT: Tier.MEDIUM,
    EventType.DIVIDEND: Tier.MEDIUM,
    EventType.TREASURY_STOCK: Tier.MEDIUM,
    EventType.MONTHLY_FIGURES: Tier.MEDIUM,
    EventType.ORGANISATION_AND_INVESTMENT: Tier.MEDIUM,
    EventType.BRIEFING: Tier.MEDIUM,
    EventType.EXECUTIVE_CHANGE: Tier.MEDIUM,
    EventType.BOARD_RESOLUTION_SUMMARY: Tier.MEDIUM,
    EventType.EMPLOYEE_EQUITY: Tier.LOW,
    EventType.LOAN_GUARANTEE: Tier.LOW,
    EventType.PERSONNEL_OTHER: Tier.LOW,
    EventType.COMMITTEE: Tier.LOW,
    EventType.NON_COMPETE: Tier.LOW,
    EventType.INVITED_INVESTOR_EVENT: Tier.LOW,
    EventType.SHAREHOLDER_MEETING: Tier.LOW,
    EventType.NAME_OR_PAR_VALUE: Tier.LOW,
    EventType.SUPPLEMENT: Tier.LOW,
    EventType.OTHER: Tier.LOW,
}

# A consequence of trading, not an explanation of it: never a candidate.
EXCLUDED: frozenset[EventType] = frozenset({EventType.MARKET_ATTENTION})

# Subsidiary notices drop one tier, except types whose title already
# signals something that can reach the parent directly.
SUBSIDIARY_NOT_DOWNGRADED: frozenset[EventType] = frozenset(
    {
        EventType.INCIDENT_OR_CLARIFICATION,
        EventType.CONTRACT,
        EventType.FINANCIAL_WARNING,
    }
)

# Tie-break inside a tier (higher first). Unlisted types get 0.
TYPE_PRIORITY: dict[EventType, int] = {
    EventType.INCIDENT_OR_CLARIFICATION: 50,
    EventType.CONTRACT: 40,
    EventType.FINANCIAL_WARNING: 30,
    EventType.CAPITAL_AND_DEBT: 20,
    EventType.ASSET_TRANSACTION: 10,
    EventType.FINANCIAL_REPORT: 40,
    EventType.MONTHLY_FIGURES: 30,
    EventType.DIVIDEND: 20,
    EventType.BRIEFING: 15,
    EventType.EXECUTIVE_CHANGE: 10,
}


@dataclass(frozen=True)
class Candidate:
    notice: RepeatedNotice
    event_type: EventType
    is_subsidiary: bool
    tier: Tier
    priority: int


def assess(notice: RepeatedNotice) -> Candidate | None:
    """Type, tier and priority for one notice; None if it is not eligible."""
    article = notice.article
    if not article.is_official or article.market not in {"TWSE", "TPEX"}:
        return None
    event_type = classify_title(article.title)
    if event_type in EXCLUDED:
        return None
    tier = BASE_TIER[event_type]
    subsidiary = is_subsidiary_notice(article.title)
    if subsidiary and event_type not in SUBSIDIARY_NOT_DOWNGRADED:
        tier = Tier(max(Tier.LOW, tier - 1))
    return Candidate(
        notice=notice,
        event_type=event_type,
        is_subsidiary=subsidiary,
        tier=tier,
        priority=TYPE_PRIORITY.get(event_type, 0),
    )


def _rank_key(c: Candidate) -> tuple:
    # Higher tier, then higher priority, then the NEWER first publication.
    # Remaining ties are settled by the smaller article_id in _wins().
    return (
        c.tier,
        c.priority,
        c.notice.first_published_at,
    )


def pick_representatives(
    notices: Iterable[RepeatedNotice], *, since: dt.datetime | None = None
) -> dict[str, Candidate]:
    """One best candidate per stock_id; stocks without any are absent.

    `since` (tz-aware): ignore notices FIRST published before it, so a
    notice the source keeps re-publishing is not mistaken for news. Pass
    notices collapsed over a longer history than the window for this to be
    meaningful. first_published_at is the earliest date OBSERVED in the input,
    not necessarily the true first announcement.

    There is no upper bound here on purpose: for a historical (as-of) run
    the caller must drop announcements published after the as-of time BEFORE
    calling collapse_repeats(), otherwise future notices change the
    first/last dates and counts.
    """
    if since is not None and since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    best: dict[str, Candidate] = {}
    for notice in notices:
        if since is not None and notice.first_published_at < since:
            continue
        cand = assess(notice)
        if cand is None:
            continue
        stock = notice.article.stock_id
        current = best.get(stock)
        if current is None or _wins(cand, current):
            best[stock] = cand
    return best


def _wins(a: Candidate, b: Candidate) -> bool:
    ka, kb = _rank_key(a), _rank_key(b)
    if ka != kb:
        return ka > kb
    return a.notice.article.article_id < b.notice.article.article_id
