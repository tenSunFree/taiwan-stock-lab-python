"""
Risk policy.

Deliberately separate from CandidateBuilder: the candidate pool has
already performed "hard exclusion" (non-common-stock, incomplete
data, turnover too low). This module handles the "soft" risk that
applies once a stock has already made it into the pool — attention
stock, one-price limit-up, too many consecutive limit-up days, etc.
These stocks may still make the Top 5; they simply score lower on
risk quality.

Threshold values here are only the initial assumptions for
strategy-v1 and must be tuned against historical and out-of-sample
performance — hence everything lives in RiskPolicyConfig instead of
being hardcoded into the logic.

Tri-state inputs: is_attention/is_disposition/is_managed and
consecutive_limit_up_days are `bool | None` / `int | None`, not
plain bool/int. None means "no data source has confirmed this
status yet" — it is NOT equivalent to False/"confirmed clean". This
system currently has no wired-in attention/disposition/managed data
source (see finmind_mapper.build_stock_master's module docstring) and
no reliable historical limit-up reconstruction (see
app.domain.risk_inputs's module docstring), so these will be None for
every stock until those are wired in. assess() records which inputs
were unknown in RiskAssessment.missing_inputs, and
build_risk_quality_raw() refuses to produce a score when any are
missing — see that function's docstring for why.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskPolicyConfig:
    """
    strategy-v1 initial assumptions; must be revisited after backtesting.
    """

    maximum_consecutive_limit_up_days: int = 3
    excessive_return_5d: float = (
        0.35  # 5-day cumulative return above this is flagged as elevated risk
    )
    minimum_data_completeness: float = 0.80

    # whether the following statuses are allowed into the candidate set
    # (True = allowed but flagged/penalized, False = hard exclusion)
    allow_attention_stock: bool = True
    allow_ky_stock: bool = True
    allow_one_price_limit_up: bool = (
        True  # opened and locked at limit-up with no chance to buy in
    )


@dataclass(frozen=True)
class RiskAssessment:
    stock_id: str
    is_excluded: bool
    exclusion_reason: str | None
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    missing_inputs: tuple[str, ...] = field(default_factory=tuple)
    """Which of the tri-state inputs were None (unknown) for this
    stock, e.g. ("is_attention", "is_disposition", "is_managed",
    "consecutive_limit_up_days"). A non-empty tuple means this
    assessment is based on incomplete information — risk_flags may
    under-report real risk, and build_risk_quality_raw() will refuse
    to score it for exactly that reason."""


class RiskPolicy:
    def __init__(self, config: RiskPolicyConfig | None = None) -> None:
        self.config = config or RiskPolicyConfig()

    def assess(
        self,
        *,
        stock_id: str,
        is_attention: bool | None,
        is_disposition: bool | None,
        is_managed: bool | None,
        is_ky: bool,
        is_one_price_limit_up: bool,
        consecutive_limit_up_days: int | None,
        return_5d: float | None,
    ) -> RiskAssessment:
        # --- Hard exclusion (strategy-level, distinct from the
        # instrument-type exclusion done in CandidateBuilder) ---
        # Only an explicit True triggers exclusion — None (unknown)
        # must never be treated as if it were True OR False; it is
        # simply not evaluated as a positive match here, and is
        # recorded in missing_inputs below instead.
        if is_disposition is True:
            return RiskAssessment(
                stock_id, True, "disposition stock, excluded by strategy policy"
            )
        if is_managed is True:
            return RiskAssessment(
                stock_id,
                True,
                "full-cash-delivery / managed stock, excluded by strategy policy",
            )
        if not self.config.allow_attention_stock and is_attention is True:
            return RiskAssessment(
                stock_id, True, "attention stock, excluded by strategy policy"
            )
        if not self.config.allow_ky_stock and is_ky:
            return RiskAssessment(
                stock_id,
                True,
                "KY (foreign-registered) stock, excluded by strategy policy",
            )
        if not self.config.allow_one_price_limit_up and is_one_price_limit_up:
            return RiskAssessment(
                stock_id,
                True,
                "one-price limit-up (locked, no trading opportunity), "
                "excluded by strategy policy",
            )

        # --- Soft risk flags (kept but recorded, used to penalize the
        # risk-quality factor at the scoring stage) ---
        flags: list[str] = []

        if is_attention is True:
            flags.append("ATTENTION_STOCK")
        if is_ky:
            flags.append("KY_STOCK")
        if is_one_price_limit_up:
            flags.append("ONE_PRICE_LIMIT_UP")
        if (
            consecutive_limit_up_days is not None
            and consecutive_limit_up_days
            > self.config.maximum_consecutive_limit_up_days
        ):
            flags.append("EXCESSIVE_CONSECUTIVE_LIMIT_UP")
        if return_5d is not None and return_5d >= self.config.excessive_return_5d:
            flags.append("HIGH_FIVE_DAY_RETURN")

        # --- Track which tri-state inputs were unknown, so a later
        # "no flags raised" never gets misread as "confirmed clean" ---
        missing: list[str] = []
        if is_attention is None:
            missing.append("is_attention")
        if is_disposition is None:
            missing.append("is_disposition")
        if is_managed is None:
            missing.append("is_managed")
        if consecutive_limit_up_days is None:
            missing.append("consecutive_limit_up_days")

        return RiskAssessment(
            stock_id=stock_id,
            is_excluded=False,
            exclusion_reason=None,
            risk_flags=tuple(flags),
            missing_inputs=tuple(missing),
        )


# Penalty per flag — deliberately per-flag weighted rather than flat,
# since some flags (excessive consecutive limit-up) are riskier than
# others (a KY-registered company alone). These weights are a
# strategy-v1 initial assumption, same status as RiskPolicyConfig's
# thresholds above — must be tuned against backtesting, not treated
# as ground truth. Mirrored in config/strategy-v1.yaml's
# risk_policy.penalties section for documentation; not yet read from
# there by a config loader (same "hardcoded to match YAML by hand"
# status as MINIMUM_TURNOVER/MAXIMUM_CANDIDATES in daily_ranking.py).
RISK_FLAG_PENALTIES: dict[str, float] = {
    "ATTENTION_STOCK": 0.15,
    "KY_STOCK": 0.10,
    "ONE_PRICE_LIMIT_UP": 0.20,
    "EXCESSIVE_CONSECUTIVE_LIMIT_UP": 0.30,
    "HIGH_FIVE_DAY_RETURN": 0.25,
}


def build_risk_quality_raw(
    assessment: RiskAssessment,
    *,
    penalties: dict[str, float] = RISK_FLAG_PENALTIES,
) -> float | None:
    """
    Convert a RiskAssessment into the 0~1 risk_quality_raw input
    StockFeatures/scoring.py expects (1.0 = best risk quality, no
    flags).

    Returns None — NOT 1.0 — whenever assessment.missing_inputs is
    non-empty. An assessment built from incomplete tri-state inputs
    (attention/disposition/managed/consecutive-limit-up all unknown,
    which is currently the case for every stock — see
    RiskPolicy's module docstring) cannot honestly claim "no flags ==
    clean," because the very inputs that would have raised those
    flags were never checked. Scoring it as 1.0 would silently turn
    "we don't know" into "we're sure this is safe," which is the
    exact failure mode this project's "None over a fabricated number"
    philosophy exists to prevent (see app.domain.feature_builder).

    Unrecognized flag strings contribute zero penalty rather than
    raising, so this stays forward-compatible if RiskPolicy grows new
    flags before this table is updated — but that also means a newly
    added flag is SILENTLY unpenalized until RISK_FLAG_PENALTIES is
    updated; keep the two in sync when adding flags to
    RiskPolicy.assess().
    """
    if assessment.missing_inputs:
        return None

    penalty = sum(penalties.get(flag, 0.0) for flag in assessment.risk_flags)
    return max(0.0, 1.0 - penalty)
