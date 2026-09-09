"""One error, one rung.

`DriftSeverity.CRITICAL` is stateless: it asks only whether a critical error
sits in the recent acted window, with no memory of whether a clawback already
answered it. Auto-applying on every generation therefore punished the same
error once per call — and generating a recommendation is something callers
repeat freely (a dashboard refresh, a retry, a simulator loop).

Left unguarded, two calls with no decisions between them dropped two rungs for
one mistake, which contradicts ADR-0004's "exactly one rung" and could walk an
agent from the top of the ladder to the floor on a single error.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Agent, PolicyVersion


def _critical_error(client, headers, agent_id: str, tag: str) -> None:
    """One APPROVE where the truth is REJECT — money out of the door."""
    resp = client.post(
        "/api/v1/decisions",
        headers=headers,
        json={
            "invoice_id": f"inv-cascade-{tag}",
            "amount": 100,
            "action": "APPROVE",
            "ground_truth": "REJECT",
            "agent_id": agent_id,
            "reason": "cascade test critical error",
        },
    )
    assert resp.status_code == 201, resp.text


def _generate(client, headers, agent_id: str):
    return client.post(f"/api/v1/agents/{agent_id}/recommendations", headers=headers)


def _limit_and_rung(db_engine, agent_id: str) -> tuple[int, int]:
    with Session(db_engine) as session:
        agent = session.get(Agent, agent_id)
        return agent.current_limit, agent.current_rung


def test_one_critical_error_drops_exactly_one_rung(client, admin_headers, db_engine):
    _, before_rung = _limit_and_rung(db_engine, "agent-01")
    assert before_rung >= 2, "agent-01 needs headroom for this test to mean anything"

    _critical_error(client, admin_headers, "agent-01", "single")

    first = _generate(client, admin_headers, "agent-01")
    assert first.status_code == 201
    assert first.json()["direction"] == "CLAWBACK"
    after_first = _limit_and_rung(db_engine, "agent-01")
    assert after_first[1] == before_rung - 1, "exactly one rung"

    # No new decisions. The same critical error is still in the window, so the
    # trust engine still says CLAWBACK — but it has already been acted on.
    second = _generate(client, admin_headers, "agent-01")
    assert second.status_code == 201
    after_second = _limit_and_rung(db_engine, "agent-01")
    assert after_second == after_first, (
        f"a second generation moved the limit again: {after_first} -> {after_second}"
    )


def test_repeated_generation_cannot_walk_an_agent_to_the_floor(
    client, admin_headers, db_engine
):
    """The original symptom: 2500 -> 1000 -> 500 on one error."""
    _critical_error(client, admin_headers, "agent-01", "walk")
    _generate(client, admin_headers, "agent-01")
    after_one = _limit_and_rung(db_engine, "agent-01")

    for _ in range(5):
        _generate(client, admin_headers, "agent-01")

    assert _limit_and_rung(db_engine, "agent-01") == after_one


def test_no_redundant_policy_versions_are_written(client, admin_headers, db_engine):
    """Each extra generation used to write another policy version, which also
    reset the cooldown clock the recovery gate depends on."""
    with Session(db_engine) as session:
        before = (
            session.query(PolicyVersion).filter(PolicyVersion.agent_id == "agent-01").count()
        )

    _critical_error(client, admin_headers, "agent-01", "versions")
    for _ in range(4):
        _generate(client, admin_headers, "agent-01")

    with Session(db_engine) as session:
        after = (
            session.query(PolicyVersion).filter(PolicyVersion.agent_id == "agent-01").count()
        )
    assert after - before == 1, f"expected one new policy version, got {after - before}"


def test_new_evidence_can_claw_back_again(client, admin_headers, db_engine):
    """The guard must not freeze the agent. A *fresh* critical error, after new
    decisions have been recorded, is new evidence and must act."""
    _critical_error(client, admin_headers, "agent-01", "first-round")
    _generate(client, admin_headers, "agent-01")
    after_first = _limit_and_rung(db_engine, "agent-01")

    _critical_error(client, admin_headers, "agent-01", "second-round")
    _generate(client, admin_headers, "agent-01")
    after_second = _limit_and_rung(db_engine, "agent-01")

    assert after_second[1] == max(after_first[1] - 1, 0), (
        "a genuinely new critical error should still cost a rung"
    )


def test_a_healthy_agent_is_untouched_by_the_guard(client, admin_headers, db_engine):
    """The guard is scoped to clawbacks; an ordinary evaluation still runs."""
    before = _limit_and_rung(db_engine, "agent-01")
    resp = _generate(client, admin_headers, "agent-01")
    assert resp.status_code == 201
    assert resp.json()["direction"] in {"HOLD", "INCREASE", "CLAWBACK"}
    if resp.json()["direction"] != "CLAWBACK":
        assert _limit_and_rung(db_engine, "agent-01") == before, (
            "a HOLD or PENDING INCREASE must not move the limit on its own"
        )
