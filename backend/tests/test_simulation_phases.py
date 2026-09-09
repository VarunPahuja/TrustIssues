"""The three phases have to be distinguishable, or the feature is decorative.

`degraded` previously produced about 86% accuracy against `good`'s 95%, and put
a critical error on only 2.4% of decisions — so the chance of one landing
inside CRITICAL_ERROR_WINDOW was under half, and a "degraded" run usually
finished with no drift, no clawback and a trust score that had barely moved.
On a dashboard the three phases were indistinguishable.

These tests pin the property the phase names promise, not the exact numbers:
degraded must be clearly worse, and must reliably produce the critical errors
drift detection exists to catch.
"""

from __future__ import annotations

import pytest
from shared.enums import Action

from app.schemas.simulation import SimulationPhase
from app.services.simulation import _PHASE_PARAMS, generate_decision_plan

COUNT = 400
SEED = 42


def _plan(phase: SimulationPhase):
    return generate_decision_plan(
        agent_id="agent-01", phase=phase, seed=SEED, count=COUNT, current_limit=2500
    )


def _accuracy(plan) -> float:
    return sum(1 for d in plan if d.action is d.ground_truth) / len(plan)


def _critical_rate(plan) -> float:
    return sum(
        1 for d in plan if d.action is Action.APPROVE and d.ground_truth is Action.REJECT
    ) / len(plan)


@pytest.mark.parametrize("phase", list(SimulationPhase))
def test_every_phase_produces_a_full_plan(phase):
    assert len(_plan(phase)) == COUNT


def test_degraded_is_clearly_worse_than_good():
    """Not "a bit lower" — far enough apart to be obvious on a chart."""
    good = _accuracy(_plan(SimulationPhase.GOOD))
    degraded = _accuracy(_plan(SimulationPhase.DEGRADED))
    assert good - degraded > 0.20, f"good {good:.3f} vs degraded {degraded:.3f}"


def test_recovery_climbs_back_toward_good():
    good = _accuracy(_plan(SimulationPhase.GOOD))
    degraded = _accuracy(_plan(SimulationPhase.DEGRADED))
    recovery = _accuracy(_plan(SimulationPhase.RECOVERY))
    assert recovery > degraded + 0.15, "recovery must be a visible improvement"
    assert recovery <= good + 0.02, "and must not exceed the good phase"


def test_degraded_reliably_puts_a_critical_error_in_the_recent_window():
    """The clawback beat depends on this. A critical error has to be common
    enough that one lands inside the last CRITICAL_ERROR_WINDOW acted
    decisions, or a degraded run ends with no drift at all."""
    from trust_engine.constants import CRITICAL_ERROR_WINDOW

    rate = _critical_rate(_plan(SimulationPhase.DEGRADED))
    assert rate > 0.10, f"critical errors are too rare to trip drift: {rate:.3f}"

    # Probability at least one falls in the window, treating draws as independent.
    p_none = (1 - rate) ** CRITICAL_ERROR_WINDOW
    assert 1 - p_none > 0.90, f"only {1 - p_none:.2f} chance of drift firing"


def test_good_and_recovery_stay_safe():
    """A clawback in a good or recovery run would break the arc's story, so
    critical errors there must stay rare."""
    for phase in (SimulationPhase.GOOD, SimulationPhase.RECOVERY):
        assert _critical_rate(_plan(phase)) < 0.03, phase


def test_plans_are_reproducible():
    a = _plan(SimulationPhase.DEGRADED)
    b = _plan(SimulationPhase.DEGRADED)
    assert [(d.invoice_id, d.action, d.ground_truth, d.amount) for d in a] == [
        (d.invoice_id, d.action, d.ground_truth, d.amount) for d in b
    ]


def test_different_agents_get_different_plans():
    """The generator is seeded with agent_id too, so one seed does not give
    every agent an identical run."""
    one = generate_decision_plan(
        agent_id="agent-01", phase=SimulationPhase.GOOD, seed=SEED, count=50, current_limit=2500
    )
    two = generate_decision_plan(
        agent_id="agent-02", phase=SimulationPhase.GOOD, seed=SEED, count=50, current_limit=2500
    )
    assert [d.amount for d in one] != [d.amount for d in two]


def test_every_phase_declares_all_three_probabilities():
    """A missing key would be a KeyError deep inside a background task, where
    nobody would see it."""
    for phase, params in _PHASE_PARAMS.items():
        assert set(params) == {
            "p_ground_truth_reject",
            "p_critical_error",
            "p_noncritical_error",
        }, phase
        for name, value in params.items():
            assert 0.0 <= value <= 1.0, f"{phase} {name} = {value}"
