"""Performance Agent — is the improvement real, or is it a small-sample artifact?

This agent exists because a point estimate lies at low sample sizes. 10 correct
decisions out of 10 reads as "100% accurate" and means almost nothing; the Wilson
lower bound on that same evidence is around 72%, and that is the number worth
reasoning about. The trust engine already computes both — this agent's job is to say
out loud which one the recommendation is actually resting on.
"""

from __future__ import annotations

from shared.contracts import AgentOpinion, TrustEvaluation
from shared.enums import Direction, DriftSeverity, OpinionVerdict

from governance.agents.base import clamp_confidence, require_stub_mode

NAME = "performance"

# The gap between the point estimate and its lower bound *is* the uncertainty. A wide
# gap means the sample is too thin to distinguish a good agent from a lucky one.
WIDE_INTERVAL_PP = 0.15


def opine(evaluation: TrustEvaluation, mode: str) -> AgentOpinion:
    require_stub_mode(mode, NAME)

    accuracy = evaluation.accuracy
    drift = evaluation.drift
    concerns: list[str] = []

    if accuracy is None or not accuracy.has_evidence:
        return AgentOpinion(
            agent_name=NAME,
            verdict=OpinionVerdict.ABSTAIN,
            reasoning=(
                "No acted decisions to measure. The agent has produced no accuracy "
                "evidence, so there is no trend to characterise."
            ),
            concerns=("no acted decisions on record",),
            confidence=0.2,
        )

    point = accuracy.point if accuracy.point is not None else 0.0
    interval_width = point - accuracy.wilson_lower

    if interval_width >= WIDE_INTERVAL_PP:
        concerns.append(
            f"wide confidence interval: {point:.1%} observed but only "
            f"{accuracy.wilson_lower:.1%} supported at the lower bound over "
            f"{accuracy.trials} decisions"
        )

    if drift.underpowered:
        concerns.append(
            "drift test is underpowered — a real degradation this size would not "
            "reliably be detected yet"
        )

    if drift.severity is DriftSeverity.CRITICAL:
        # CRITICAL is not a statistical finding. It fires the moment a critical
        # error appears in the recent window, without running the two-proportion
        # test at all, so drop_pp and p_value are None. Describing it in the
        # language of a measured degradation printed "a drop of n/a" and claimed
        # a drop that had not happened — recent accuracy can be *higher* than
        # baseline and still be CRITICAL, because one approved invoice that
        # should have been rejected is not an averaging question.
        return AgentOpinion(
            agent_name=NAME,
            verdict=OpinionVerdict.OBJECT,
            reasoning=(
                "Drift is CRITICAL: a critical error — an approval where the "
                "correct answer was rejection — appeared in the recent window. "
                "This is decided on its own, without a statistical test, because "
                "money leaving wrongly is not something an average should be "
                "allowed to absorb."
            ),
            concerns=tuple(concerns),
            confidence=0.85,
        )

    if drift.severity is DriftSeverity.CONFIRMED:
        return AgentOpinion(
            agent_name=NAME,
            verdict=OpinionVerdict.OBJECT,
            reasoning=(
                f"Drift is CONFIRMED: recent accuracy "
                f"{_pct(drift.recent_accuracy)} against a baseline of "
                f"{_pct(drift.baseline_accuracy)}, a drop of {_pp(drift.drop_pp)} "
                f"(p={_num(drift.p_value)}). This is a measured degradation, not noise."
            ),
            concerns=tuple(concerns),
            confidence=0.85,
        )

    if drift.severity is DriftSeverity.WARNING:
        concerns.append(
            f"drift tripwire raised: recent {_pct(drift.recent_accuracy)} vs baseline "
            f"{_pct(drift.baseline_accuracy)}, not yet confirmed"
        )

    # An increase resting on a thin sample is the specific failure this agent is here to
    # catch: the point estimate looks great precisely because there is little evidence.
    if evaluation.direction is Direction.INCREASE and interval_width >= WIDE_INTERVAL_PP:
        return AgentOpinion(
            agent_name=NAME,
            verdict=OpinionVerdict.OBJECT,
            reasoning=(
                f"{accuracy.successes}/{accuracy.trials} looks like {point:.1%}, but the "
                f"Wilson lower bound is {accuracy.wilson_lower:.1%}. On this sample the "
                f"apparent improvement is not distinguishable from luck."
            ),
            concerns=tuple(concerns),
            confidence=0.7,
        )

    verdict = (
        OpinionVerdict.ABSTAIN
        if evaluation.direction is Direction.HOLD
        else OpinionVerdict.CONCUR
    )
    return AgentOpinion(
        agent_name=NAME,
        verdict=verdict,
        reasoning=(
            f"Accuracy is {accuracy.successes}/{accuracy.trials} ({point:.1%}), with a "
            f"Wilson lower bound of {accuracy.wilson_lower:.1%} over "
            f"{evaluation.acted_decisions} acted decisions. Drift status: "
            f"{drift.severity.value}. The trend is consistent with the evidence."
        ),
        concerns=tuple(concerns),
        confidence=clamp_confidence(0.75 - interval_width),
    )


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _pp(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}pp"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"
