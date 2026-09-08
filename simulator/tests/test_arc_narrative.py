"""The ten-beat demo arc, pinned.

The arc IS the deliverable: one command that shows an agent earning autonomy,
losing it to drift, and earning it back. Every number in it falls out of the
trust engine rather than being staged, which is what makes it defensible — and
also what makes it fragile, since a change anywhere upstream can quietly
reshape the story. These tests pin the shape so that a break shows up here
rather than in front of a panel.

Calibrated against the default count of 200. `default_script`'s own docstring
explains the recovery split.
"""

from __future__ import annotations

import pytest
from trust.trust_engine.evaluate import evaluate

from simulator.arc import ArcRunner, default_script

COUNT = 200


@pytest.fixture(scope="module")
def beats():
    """Run the arc exactly as `simulator arc --seed 42 --count 200` does, and
    return one row per beat."""
    runner = ArcRunner(seed=42, auto_approve=True)
    rows = []
    for beat in default_script(COUNT):
        runner.run_phase(beat.phase, beat.count)
        evaluation = evaluate(runner.records, runner.context())
        outcome = runner.resolve(evaluation)
        rows.append(
            {
                "label": beat.label,
                "direction": str(evaluation.direction).split(".")[-1],
                "trust": evaluation.trust_score,
                "drift": str(evaluation.drift.severity).split(".")[-1],
                "codes": set(evaluation.reason_codes),
                "limit": runner.limit,
                "outcome": outcome,
            }
        )
    return rows


def test_the_arc_has_six_beats(beats):
    assert len(beats) == 6


def test_it_starts_at_the_floor_without_enough_evidence(beats):
    """100% accuracy over 22 acted decisions is not yet proof of anything."""
    first = beats[0]
    assert first["direction"] == "HOLD"
    assert "INSUFFICIENT_SAMPLE" in first["codes"]
    assert first["limit"] == 500


def test_it_climbs_two_rungs_before_the_collapse(beats):
    assert beats[1]["direction"] == "INCREASE"
    assert beats[1]["limit"] == 1000
    assert beats[2]["direction"] == "INCREASE"
    assert beats[2]["limit"] == 2500


def test_degradation_triggers_an_automatic_clawback(beats):
    """A critical error in the recent window is an instant clawback, and no
    human is asked — ADR-0004. This is the beat the whole demo turns on."""
    collapse = beats[3]
    assert collapse["drift"] == "CRITICAL"
    assert collapse["direction"] == "CLAWBACK"
    assert "CLAWBACK_CRITICAL_ERROR" in collapse["codes"]
    assert collapse["limit"] == 1000, "drops exactly one rung, 2500 -> 1000"
    assert "automatically" in collapse["outcome"]


def test_recovery_is_not_instant(beats):
    """The beat that shows the threshold is a real gate: the agent is visibly
    improving and still does not get its rung back yet."""
    recovering = beats[4]
    assert recovering["direction"] == "HOLD"
    assert "TRUST_BELOW_THRESHOLD" in recovering["codes"]
    assert recovering["trust"] < 70.0
    assert recovering["limit"] == 1000, "still on the clawed-back rung"
    # Visibly recovering, not flatlining: well above the collapse.
    assert recovering["trust"] > beats[3]["trust"]


def test_it_earns_the_rung_back(beats):
    last = beats[5]
    assert last["direction"] == "INCREASE"
    assert last["limit"] == 2500
    assert last["trust"] >= 70.0


def test_human_agreement_has_data_on_every_beat(beats):
    """The arc rules on its escalations, so the agreement component is never
    dropped. If these codes come back, a quarter of the trust score has gone
    dark and every dashboard row will say so."""
    for beat in beats:
        assert "AGREEMENT_EVIDENCE_INSUFFICIENT" not in beat["codes"], beat["label"]
        assert "WEIGHTS_RENORMALISED" not in beat["codes"], beat["label"]


def test_the_arc_is_reproducible():
    """Same seed, same story — the claim the whole demo rests on."""

    def run():
        runner = ArcRunner(seed=42, auto_approve=True)
        out = []
        for beat in default_script(COUNT):
            runner.run_phase(beat.phase, beat.count)
            evaluation = evaluate(runner.records, runner.context())
            runner.resolve(evaluation)
            out.append((evaluation.trust_score, str(evaluation.direction), runner.limit))
        return out

    assert run() == run()
