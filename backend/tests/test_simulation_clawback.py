"""Closing the gap this branch exists for: nothing in the decision-ingest
path used to trigger a clawback, so a degrading agent kept its ceiling until
a human opened the dashboard or something called
`POST /agents/{id}/recommendations` by hand. `execute_simulation_run` now
evaluates the agent once, at the end of the run, and applies a clawback if
the evidence says so (`app.services.simulation._evaluate_and_maybe_clawback`).

Seeds below were found by direct search over `generate_decision_plan` (pure
and deterministic — same `(agent_id, phase, seed, count)` always produces the
same plan) for a combination whose last `CRITICAL_ERROR_WINDOW=20` acted
decisions contain at least one critical error (an APPROVE whose ground truth
is REJECT) — the one condition that makes `trust_engine.stats.drift.
detect_drift` return `DriftSeverity.CRITICAL` unconditionally
(`trust/trust_engine/stats/drift.py`), which `evaluate_ladder` reads as an
immediate, unconditional `Direction.CLAWBACK` — see
`trust/trust_engine/ladder.py`. Both agents' total decision counts stay well
under `RECENT_WINDOW=50`, so `detect_drift`'s baseline/recent split never
engages either — the only way either scenario below reaches CLAWBACK is via
a critical error in the tail, never via the statistical drift path.
"""

from __future__ import annotations

import time

from shared.constants import AUTONOMY_FLOOR, limit_of, rung_of
from sqlalchemy.orm import Session

from app.models import Agent
from app.services.simulation import ClawbackOutcome, _evaluate_and_maybe_clawback


def _start_run(client, admin_headers, **overrides):
    body = {
        "phase": "good",
        "agent_id": "agent-01",
        "invoice_count": 20,
        "seed": 1,
        "reason": "clawback trigger test",
    }
    body.update(overrides)
    return client.post("/api/v1/simulation/runs", headers=admin_headers, json=body)


def _run_to_completion(client, admin_headers, run_id: str, max_attempts: int = 100) -> dict:
    """The background task runs on its own thread, not synchronously before
    `.post()` returns (confirmed by direct observation, against
    `test_simulation.py`'s own comment claiming otherwise) — every assertion
    on a run's terminal fields must poll for one first, the same as
    `test_simulation.py`'s own `_poll_until_done`."""
    body = None
    for _ in range(max_attempts):
        body = client.get(f"/api/v1/simulation/runs/{run_id}", headers=admin_headers).json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached a terminal status: {body}")


def test_degrading_run_ends_with_the_agent_clawed_back_no_separate_endpoint_call(
    client, admin_headers, db_engine
):
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-01").current_rung

    # seed=2, count=20: the plan's last 20 (of 20) acted decisions contain a
    # critical error — see module docstring.
    resp = _start_run(client, admin_headers, phase="degraded", seed=2, invoice_count=20)
    assert resp.status_code == 201
    body = _run_to_completion(client, admin_headers, resp.json()["run_id"])
    assert body["status"] == "completed"

    # No call to /agents/{id}/recommendations anywhere in this test — the run
    # itself is what applied the clawback.
    assert body["clawback_applied"] is True
    assert body["clawback_limit"] == limit_of(rung_before - 1)

    with Session(db_engine) as session:
        agent = session.get(Agent, "agent-01")
        assert agent.current_rung == rung_before - 1
        assert agent.current_limit == limit_of(rung_before - 1)


def test_clean_run_does_not_claw_back(client, admin_headers, db_engine):
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-01").current_rung

    # seed=1, count=20: zero critical errors anywhere in the plan.
    resp = _start_run(client, admin_headers, phase="good", seed=1, invoice_count=20)
    assert resp.status_code == 201
    body = _run_to_completion(client, admin_headers, resp.json()["run_id"])
    assert body["status"] == "completed"
    assert body["clawback_applied"] is False
    assert body["clawback_limit"] is None

    with Session(db_engine) as session:
        assert session.get(Agent, "agent-01").current_rung == rung_before


def test_cascade_guard_holds_across_a_second_evaluation_with_no_new_decisions(
    client, admin_headers, db_engine
):
    """A run that claws back, followed by another evaluation over the exact
    same, unchanged decision history (no new decisions submitted in
    between), must not claw back a second time — the guard
    `app.services.governance.generate_recommendation` already applies
    (PR #40) still holds when reached through this new trigger point, since
    this branch reuses that function rather than writing a second clawback
    path.
    """
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-01").current_rung

    resp = _start_run(client, admin_headers, phase="degraded", seed=2, invoice_count=20)
    body = _run_to_completion(client, admin_headers, resp.json()["run_id"])
    assert body["clawback_applied"] is True

    with Session(db_engine) as session:
        assert session.get(Agent, "agent-01").current_rung == rung_before - 1

    # Directly re-invoke the same end-of-run evaluation this run's own
    # background task called — no new decisions submitted since the clawback
    # above, exactly the "another run submitting zero new decisions"
    # scenario: decisions_since_last_change is 0 relative to the policy
    # version the first call just wrote.
    outcome = _evaluate_and_maybe_clawback(db_engine, "agent-01")
    assert outcome == ClawbackOutcome(applied=False, limit=None)

    with Session(db_engine) as session:
        # Exactly one rung dropped, not two.
        assert session.get(Agent, "agent-01").current_rung == rung_before - 1


def test_clawback_never_drops_below_autonomy_floor(client, admin_headers, db_engine):
    with Session(db_engine) as session:
        agent = session.get(Agent, "agent-02")
        assert agent.current_limit == AUTONOMY_FLOOR, "test assumes agent-02 is seeded at the floor"

    # seed=2, count=20 against agent-02: last-20 acted decisions contain a
    # critical error, same as agent-01's scenario above, found independently
    # for this agent (see module docstring).
    resp = _start_run(
        client, admin_headers, agent_id="agent-02", phase="degraded", seed=2, invoice_count=20
    )
    assert resp.status_code == 201
    body = _run_to_completion(client, admin_headers, resp.json()["run_id"])
    assert body["status"] == "completed"
    # Already at the floor: governance.py's own floor no-op (PR #34) means no
    # new policy version is written, so this run's own evaluation reports no
    # applied clawback even though the direction was CLAWBACK.
    assert body["clawback_applied"] is False
    assert body["clawback_limit"] is None

    with Session(db_engine) as session:
        agent = session.get(Agent, "agent-02")
        assert agent.current_limit == AUTONOMY_FLOOR
        assert agent.current_rung == rung_of(AUTONOMY_FLOOR) == 0


def test_run_record_reflects_no_clawback_fields_by_default(client, admin_headers):
    """Every run, clawed back or not, carries both fields in its response —
    `clawback_applied` is never missing, just `False`."""
    resp = _start_run(client, admin_headers, phase="good", seed=1, invoice_count=20)
    body = _run_to_completion(client, admin_headers, resp.json()["run_id"])
    assert "clawback_applied" in body
    assert "clawback_limit" in body
    assert body["clawback_applied"] is False
    assert body["clawback_limit"] is None
