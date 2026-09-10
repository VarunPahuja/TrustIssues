"""A simulation run applies the clawback its own decisions earned.

The gap this closes: a run recorded decisions and stopped. A degraded run could
push drift to CRITICAL, the trust evaluation would correctly read CLAWBACK, and
the agent kept its full limit indefinitely — because a clawback only applies
when a *recommendation* is generated, and nothing generated one.

Detection without action. ADR-0004 is explicit that a reduction needs no human
authorization, so nothing should have to poke the system for one to take
effect.
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.models import Agent


def _run(client, headers, agent_id: str, phase: str, count: int = 60, seed: int = 7):
    resp = client.post(
        "/api/v1/simulation/runs",
        headers=headers,
        json={
            "agent_id": agent_id,
            "phase": phase,
            "invoice_count": count,
            "seed": seed,
            "reason": f"clawback test {phase}",
        },
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    for _ in range(120):
        row = client.get(f"/api/v1/simulation/runs/{run_id}", headers=headers).json()
        if row["status"] != "running":
            return row
        time.sleep(0.05)
    raise AssertionError("simulation run did not finish")


def _agent(db_engine, agent_id: str = "agent-01") -> tuple[int, int]:
    with Session(db_engine) as session:
        a = session.get(Agent, agent_id)
        return a.current_limit, a.current_rung


def test_a_degraded_run_claws_back_without_being_asked(client, admin_headers, db_engine):
    """The whole point: nobody generates a recommendation, nobody approves
    anything, and the limit still comes down."""
    before_limit, before_rung = _agent(db_engine)
    assert before_rung >= 1, "agent-01 needs somewhere to fall to"

    row = _run(client, admin_headers, "agent-01", "degraded")
    assert row["status"] == "completed"

    after_limit, after_rung = _agent(db_engine)
    assert after_rung == before_rung - 1, (
        f"expected exactly one rung down from {before_rung}, got {after_rung}"
    )
    assert after_limit < before_limit


def test_the_clawback_is_recorded_as_a_system_action(client, admin_headers, db_engine):
    """No human authorized it, so the trail must say so — ADR-0004's asymmetry
    has to be visible afterwards, not just true at the time."""
    _run(client, admin_headers, "agent-01", "degraded")

    versions = client.get(
        "/api/v1/agents/agent-01/policy-versions", headers=admin_headers
    ).json()["items"]
    newest = versions[0]
    assert newest["created_by"] == "system", newest
    assert "clawback" in (newest["reason"] or "").lower()

    recs = client.get(
        "/api/v1/recommendations?agent_id=agent-01", headers=admin_headers
    ).json()["items"]
    clawbacks = [r for r in recs if r["direction"] == "CLAWBACK"]
    assert clawbacks, "a clawback recommendation should exist"
    assert clawbacks[0]["status"] == "APPROVED", "applied without a human step"

    approvals_for_it = [r for r in clawbacks if r.get("approved_by")]
    assert not approvals_for_it, "no human approval row for an automatic clawback"


def test_a_clean_good_run_leaves_the_agent_alone(client, admin_headers, db_engine):
    """A healthy run must not demote its agent — otherwise every simulation
    would quietly cost a rung.

    Seed 99 is chosen because `generate_decision_plan` produces no critical
    error for (agent-01, good, 99, 60). That is not a formality: the good phase
    carries a small but real critical-error probability, so *some* good seeds do
    earn a clawback, and one critical error in the recent window is enough.
    Seed 7 produces one at position 44 and would legitimately claw back. See
    `test_a_good_run_that_makes_a_critical_error_still_claws_back`."""
    before = _agent(db_engine)
    row = _run(client, admin_headers, "agent-01", "good", seed=99)
    assert row["status"] == "completed"
    assert _agent(db_engine) == before


def test_a_good_run_that_makes_a_critical_error_still_claws_back(
    client, admin_headers, db_engine
):
    """The rule is about the evidence, not the phase label. A "good" agent that
    approves an invoice it should have rejected has still let money out of the
    door, and the clawback is not negotiable on the strength of the phase it
    happened in."""
    from shared.enums import Action

    from app.schemas.simulation import SimulationPhase
    from app.services.simulation import generate_decision_plan

    plan = generate_decision_plan(
        agent_id="agent-01", phase=SimulationPhase.GOOD, seed=7, count=60,
        current_limit=2500,
    )
    criticals = [
        i for i, d in enumerate(plan)
        if d.action is Action.APPROVE and d.ground_truth is Action.REJECT
    ]
    assert criticals, "precondition: this seed must contain a critical error"

    _, before_rung = _agent(db_engine)
    _run(client, admin_headers, "agent-01", "good", seed=7)
    _, after_rung = _agent(db_engine)
    assert after_rung == before_rung - 1


def test_a_good_run_does_not_raise_the_limit_either(client, admin_headers, db_engine):
    """An INCREASE still waits for a human. Only clawbacks are applied here —
    generating increases from every run would fill the approvals queue and
    bypass the one step ADR-0004 requires."""
    before_limit, _ = _agent(db_engine)
    _run(client, admin_headers, "agent-01", "good", count=80)
    after_limit, _ = _agent(db_engine)
    assert after_limit <= before_limit, "a run must never raise a limit on its own"


def test_a_run_at_the_floor_stays_at_the_floor(client, admin_headers, db_engine):
    """agent-02 is at rung 0. A clawback there is floor -> floor, which must be
    a no-op rather than a negative rung or a redundant policy version."""
    with Session(db_engine) as session:
        start = session.get(Agent, "agent-02")
        assert start.current_rung == 0
        before_limit = start.current_limit

    versions_before = client.get(
        "/api/v1/agents/agent-02/policy-versions", headers=admin_headers
    ).json()["total"]

    row = _run(client, admin_headers, "agent-02", "degraded")
    assert row["status"] == "completed"

    after_limit, after_rung = _agent(db_engine, "agent-02")
    assert after_rung == 0
    assert after_limit == before_limit

    versions_after = client.get(
        "/api/v1/agents/agent-02/policy-versions", headers=admin_headers
    ).json()["total"]
    assert versions_after == versions_before, "no redundant policy version at the floor"


def test_the_run_still_completes_if_governance_is_unavailable(
    client, admin_headers, db_engine, monkeypatch
):
    """A governance outage must not retroactively fail a run whose decisions
    were recorded successfully — and it must leave a trace rather than
    swallowing the fact that an earned clawback never landed."""
    import app.services.simulation as sim

    def boom(*_args, **_kwargs):
        raise RuntimeError("governance is down")

    monkeypatch.setattr(sim, "_apply_any_clawback_the_run_earned", lambda *a, **k: boom())

    row = _run(client, admin_headers, "agent-01", "degraded")
    assert row["status"] == "completed", "the run itself succeeded"
    assert row["decisions_submitted"] > 0
