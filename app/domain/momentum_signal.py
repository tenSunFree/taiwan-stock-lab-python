"""
"漲多過熱" (momentum overheated) — the single, named definition.

Previously this rule existed only as a display inference inside
app.reports.text_renderer. It now lives in the Domain so the text report,
the K-line chart marker and the Flex card all read the SAME answer (the
chart/Flex layers must never infer it from return_5d themselves — see
the LINE chart spec §21).

Rule (unchanged from the text report, just promoted):
    TRUE  — the momentum factor is in the "偏弱" band
            (score < WEAK_MOMENTUM_SCORE_THRESHOLD) AND RiskPolicy raised
            HIGH_FIVE_DAY_RETURN. Momentum is deliberately non-monotonic
            (app.domain.normalization.bounded_momentum_score): a low score
            can mean "genuinely weak" OR "already rallied too far"; the
            risk flag is what tells the two apart.
    FALSE — the momentum score exists and the rule above does not hold.
            In particular a decent/strong score is never relabelled
            overheated just because the flag fired (the flag's threshold
            is configured independently of the score's bands).
    INSUFFICIENT_DATA — no momentum score (return_5d unavailable).

Thresholds: the flag's own return threshold stays in RiskPolicyConfig
(excessive_return_5d); only the score cut-off lives here, shared with the
text report's "偏弱" boundary so the two can never disagree.
"""

from __future__ import annotations

from app.domain.signal_status import SignalStatus

WEAK_MOMENTUM_SCORE_THRESHOLD = 40.0
HIGH_FIVE_DAY_RETURN_FLAG = "HIGH_FIVE_DAY_RETURN"


def build_momentum_overheated_status(
    *, momentum_score: float | None, risk_flags: tuple[str, ...]
) -> SignalStatus:
    if momentum_score is None:
        return SignalStatus.INSUFFICIENT_DATA
    overheated = (
        momentum_score < WEAK_MOMENTUM_SCORE_THRESHOLD
        and HIGH_FIVE_DAY_RETURN_FLAG in risk_flags
    )
    return SignalStatus.TRUE if overheated else SignalStatus.FALSE
