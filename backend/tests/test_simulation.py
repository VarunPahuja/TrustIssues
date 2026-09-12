from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from app.models import Decision


def _start_run(client, admin_headers, **overrides):
    body = {
        "phase": "good",
        "agent_id": "agent-01",
        "invoice_count": 20,
        "seed": 7,
        "reason": "test run",
    }
    body.update(overrides)
    return client.post("/api/v1/simulation/runs", headers=admin_headers, json=body)


def _poll_until_done(client, admin_headers, run_id, max_attempts=20):
    """`TestClient` runs `BackgroundTasks` to completion before `.post()`
    returns, so in practice one `GET` is always enough here — polling with a
    bound anyway matches how a real, asynchronous deployment behaves, which
    is the behavior this test is actually meant to prove out."""
    for _ in range(max_attempts):
        resp = client.get(f"/api/v1/simulation/runs/{run_id}", headers=admin_headers)
        body = resp.json()
        if body["status"] in ("completed", "failed"):
            return body
    raise AssertionError(f"run {run_id} never reached a terminal status: {body}")


def test_start_simulation_run_as_admin(client, admin_headers):
    resp = _start_run(client, admin_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["run_id"].startswith("run-")
    assert body["status"] in ("running", "completed")  # may finish before the response is read


def test_start_simulation_run_requires_reason(client, admin_headers):
    resp = client.post(
        "/api/v1/simulation/runs",
        headers=admin_headers,
        json={"phase": "good", "agent_id": "agent-01", "invoice_count": 20, "seed": 7},
    )
    assert resp.status_code == 422


def test_start_simulation_run_unknown_agent(client, admin_headers):
    resp = _start_run(client, admin_headers, agent_id="agent-does-not-exist")
    assert resp.status_code == 404


def test_reviewer_cannot_start_a_run(client, reviewer_headers):
    resp = client.post(
        "/api/v1/simulation/runs",
        headers=reviewer_headers,
        json={"phase": "good", "agent_id": "agent-01", "invoice_count": 20, "seed": 7, "reason": "trying anyway"},
    )
    assert resp.status_code == 403


def test_get_simulation_run_not_found(client, admin_headers):
    resp = client.get("/api/v1/simulation/runs/does-not-exist", headers=admin_headers)
    assert resp.status_code == 404


def test_run_completes_with_the_requested_decision_count(client, admin_headers, db_engine):
    resp = _start_run(client, admin_headers, invoice_count=25, seed=11)
    run_id = resp.json()["run_id"]

    final = _poll_until_done(client, admin_headers, run_id)
    assert final["status"] == "completed"
    assert final["decisions_submitted"] == 25
    assert final["error_message"] is None

    from sqlalchemy.orm import Session

    with Session(db_engine) as session:
        count = len(
            session.execute(
                select(Decision).where(Decision.agent_id == "agent-01")
            ).scalars().all()
        )
    assert count >= 25  # >= : agent-01 may already carry seeded decisions


def test_run_reaches_completed_with_plausible_summary_stats(client, admin_headers):
    resp = _start_run(client, admin_headers, phase="good", invoice_count=60, seed=3)
    run_id = resp.json()["run_id"]

    final = _poll_until_done(client, admin_headers, run_id)
    assert final["status"] == "completed"
    assert final["accuracy"] is not None
    assert final["wilson_lower_bound"] is not None
    # Good phase is tuned for high, not perfect, accuracy — see
    # app/services/simulation.py's _PHASE_PARAMS.
    assert 0.5 < final["accuracy"] <= 1.0
    # Wilson lower bound is always <= the raw point estimate.
    assert final["wilson_lower_bound"] <= final["accuracy"]


def test_same_seed_produces_identical_decision_sequences(client, admin_headers, db_engine):
    """Two runs, same (agent, phase, seed, count), run back to back for the
    *same* agent. `generate_decision_plan` is seeded by
    `(agent_id, phase, seed)` deliberately (see its docstring) precisely so
    that two *different* agents given the same numeric seed do NOT collide —
    determinism is a property of repeating the exact same run, not of the
    bare seed value alone.

    seed=99 used to work here, but since vp/clawback-trigger it happens to
    put a critical error in run A's last `CRITICAL_ERROR_WINDOW` decisions —
    run A then claws back agent-01 (correctly; see
    test_simulation_clawback.py), which changes `agent.current_limit`
    *before* run B's own plan is generated. `generate_decision_plan` takes
    `current_limit` as an explicit argument specifically because it sets the
    invoice-amount range (see its own docstring) — a different limit is a
    different amount range, which perturbs every draw after it from the same
    seeded `random.Random` stream, and run B stops matching run A. That is
    not a determinism bug in `generate_decision_plan` itself (each run is
    still exactly reproducible given its own inputs) — it is this test's
    premise ("two back-to-back same-seed runs see the same current_limit")
    no longer holding for every seed, now that a run can change the very
    limit the next one's plan depends on. seed=1 has no critical error
    anywhere in a 15-decision degraded plan for agent-01 (checked directly
    against `generate_decision_plan`), so neither run claws back and the
    premise holds again.
    """
    resp_a = _start_run(client, admin_headers, agent_id="agent-01", phase="degraded", seed=1, invoice_count=15)
    run_a = _poll_until_done(client, admin_headers, resp_a.json()["run_id"])

    resp_b = _start_run(client, admin_headers, agent_id="agent-01", phase="degraded", seed=1, invoice_count=15)
    run_b = _poll_until_done(client, admin_headers, resp_b.json()["run_id"])

    assert run_a["decisions_submitted"] == run_b["decisions_submitted"] == 15
    assert run_a["accuracy"] == run_b["accuracy"]
    assert run_a["wilson_lower_bound"] == run_b["wilson_lower_bound"]

    # Stronger than matching summary stats: the two runs' *own* decisions
    # (the ones each run just created, identified by sequence) carry
    # identical action/ground-truth pairs in identical order.
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models import Decision, Invoice

    with Session(db_engine) as session:
        rows = session.execute(
            select(Decision, Invoice)
            .join(Invoice, Decision.invoice_id == Invoice.id)
            .where(Decision.agent_id == "agent-01")
            .order_by(Decision.sequence)
        ).all()
    tail_a = [(d.action, i.ground_truth_action, i.amount) for d, i in rows[-30:-15]]
    tail_b = [(d.action, i.ground_truth_action, i.amount) for d, i in rows[-15:]]
    assert tail_a == tail_b


def test_a_failure_mid_run_is_recorded_not_swallowed(client, admin_headers):
    """Force the third decision to raise, and confirm the run is marked
    FAILED with the failure recorded and a short, honest count — not a
    silently short "completed" run and not an unhandled exception that
    leaves the row stuck at RUNNING forever."""
    real_create_decision = None
    call_count = {"n": 0}

    def _flaky_create_decision(db, body):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated ingest failure")
        return real_create_decision(db, body)

    from app.api.v1 import decisions as decisions_module

    real_create_decision = decisions_module._create_decision

    with patch("app.api.v1.decisions._create_decision", side_effect=_flaky_create_decision):
        resp = _start_run(client, admin_headers, invoice_count=10, seed=5)
        run_id = resp.json()["run_id"]
        final = _poll_until_done(client, admin_headers, run_id)

    assert final["status"] == "failed"
    assert final["decisions_submitted"] == 2
    assert final["error_message"] is not None
    assert "simulated ingest failure" in final["error_message"]
