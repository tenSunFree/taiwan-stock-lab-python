"""
Absolute Signal gates.

Current problem:
    institutional_net_buy_ratio_5d and revenue_yoy are currently
    converted in scoring.py via percentile_score() into candidate-pool
    relative percentiles (0-100), and text_renderer.py's
    _signal_emoji() then maps that relative percentile directly to
    🟢/🟡/🔴.

    This causes a "relative best = green" bug: a stock with an
    institutional net sell of -6.3% can still be the best value in a
    small candidate pool, get percentile 100, and be shown as 🟢 —
    even though in absolute financial terms it is a net sell.

This module is intentionally the lowest, simplest layer: it consumes
only a raw value and returns the emoji/word representing that value's
ABSOLUTE financial meaning, completely ignoring the candidate pool,
percentiles, or any other stock's value. Later steps wire this into
scoring.py and text_renderer.py.

    > 0   -> 🟢 強   (institutional: actual net buy)
    = 0   -> 🟡 普通 (flat)
    < 0   -> 🔴 偏弱 (institutional: actual net sell — must show 🔴
                      even if it happens to be the best value in the
                      candidate pool)
    None  -> ⚪ 資料不足 (must never be guessed as False/negative)

Same idea for fundamentals, gated on a 10% YoY threshold.

This step intentionally does not touch scoring.py / text_renderer.py —
existing report output, total_score, and factor_scores are completely
unaffected until later steps wire this module in.
"""

from __future__ import annotations

from dataclasses import dataclass

EMOJI_GREEN = "🟢"
EMOJI_YELLOW = "🟡"
EMOJI_RED = "🔴"
EMOJI_UNKNOWN = "⚪"

WORD_STRONG = "強"
WORD_NEUTRAL = "普通"
WORD_WEAK = "偏弱"
WORD_INSUFFICIENT = "資料不足"

# Fundamental Absolute Gate threshold: YoY >= 10% counts as "strong".
# Matches the project's existing 10% threshold used by
# fundamental_growth_sustained / eps_growth_sustained, so the same
# report never applies two different definitions of "strong growth."
FUNDAMENTAL_STRONG_YOY = 0.10


@dataclass(frozen=True)
class AbsoluteSignal:
    emoji: str
    word: str


def institutional_absolute_signal(
    institutional_net_buy_ratio_5d: float | None,
) -> AbsoluteSignal:
    """
    Institutional (籌碼) Absolute Signal — looks only at the SIGN of
    the 5-day institutional net-buy ratio, completely ignoring
    candidate-pool percentiles/relative scores.

        > 0   -> 🟢 強
        = 0   -> 🟡 普通
        < 0   -> 🔴 偏弱
        None  -> ⚪ 資料不足

    On None: app.domain.institutional_flow_builder is already designed
    so that any missing day within the fixed window makes the whole
    calculation return None (never silently backfilled with an older
    session). Mapping None -> ⚪ here is therefore a natural extension,
    with no extra freshness check needed.
    """
    if institutional_net_buy_ratio_5d is None:
        return AbsoluteSignal(emoji=EMOJI_UNKNOWN, word=WORD_INSUFFICIENT)

    if institutional_net_buy_ratio_5d > 0:
        return AbsoluteSignal(emoji=EMOJI_GREEN, word=WORD_STRONG)

    if institutional_net_buy_ratio_5d == 0:
        return AbsoluteSignal(emoji=EMOJI_YELLOW, word=WORD_NEUTRAL)

    return AbsoluteSignal(emoji=EMOJI_RED, word=WORD_WEAK)


def fundamental_absolute_signal(revenue_yoy: float | None) -> AbsoluteSignal:
    """
    Fundamental (基本面) Absolute Signal — looks only at which
    absolute range the latest monthly revenue YoY falls into,
    completely ignoring candidate-pool percentiles/relative scores.

        YoY >= 10%       -> 🟢 強
        0% <= YoY < 10%  -> 🟡 普通
        YoY < 0%         -> 🔴 偏弱
        None             -> ⚪ 資料不足
    """
    if revenue_yoy is None:
        return AbsoluteSignal(emoji=EMOJI_UNKNOWN, word=WORD_INSUFFICIENT)

    if revenue_yoy >= FUNDAMENTAL_STRONG_YOY:
        return AbsoluteSignal(emoji=EMOJI_GREEN, word=WORD_STRONG)

    if revenue_yoy >= 0:
        return AbsoluteSignal(emoji=EMOJI_YELLOW, word=WORD_NEUTRAL)

    return AbsoluteSignal(emoji=EMOJI_RED, word=WORD_WEAK)


# This rollout explicitly covers only these two factors — liquidity,
# volume_price, and risk_quality have no equally well-defined absolute
# financial meaning to gate on. Momentum already uses an absolute rule
# (bounded_momentum_score) and needs no Absolute Gate here. Later,
# text_renderer.py uses this constant to decide which factors go
# through the new logic.
ABSOLUTE_GATED_FACTORS: frozenset[str] = frozenset({"institutional", "fundamental"})
