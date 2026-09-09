"""A CLAWBACK recommendation must be applied once per piece of evidence, not
once per call. `DriftSeverity.CRITICAL`/`CONFIRMED` are stateless — calling
`POST /agents/{id}/recommendations` twice with no new decisions in between
used to claw back twice off the same critical error (confirmed live:
2,500 -> 1,000 -> 500). See app/services/governance.py:generate_recommendation's
docstring for the guard and why it doesn't misrepresent what happened.
"""

from __future__ import annotations

from shared.constants import AUTONOMY_FLOOR, limit_of, rung_of
from shared.contracts import Recommendation
from shared.enums import Direction, RecommendationStatus
from sqlalchemy.orm import Session

from app.models import Agent, PolicyVersion


def _submit_critical_error(client, admin_headers, agent_id: str, invoice_id: str):
    return client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": invoice_id,
            "amount": 100,
            "action": "APPROVE",
            "ground_truth": "REJECT",
            "agent_id": agent_id,
            "reason": "cascade guard test",
        },
    )


def test_two_consecutive_calls_drop_only_one_rung(client, admin_headers, db_engine):
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-01").current_rung

    _submit_critical_error(client, admin_headers, "agent-01", "inv-cascade-1")

    first = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers).json()
    assert first["direction"] == "CLAWBACK"
    assert first["status"] == "APPROVED"
    assert first["proposed_limit"] == limit_of(rung_before - 1)

    with Session(db_engine) as session:
        assert session.get(Agent, "agent-01").current_rung == rung_before - 1

    second = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers).json()

    with Session(db_engine) as session:
        # Exactly one rung dropped, not two.
        assert session.get(Agent, "agent-01").current_rung == rung_before - 1

    # The second call returns the same recommendation, not a new one
    # claiming a fresh action was taken.
    assert second["recommendation_id"] == first["recommendation_id"]
    assert second["direction"] == "CLAWBACK"
    assert second["status"] == "APPROVED"
    assert second["proposed_limit"] == limit_of(rung_before - 1)


def test_third_call_still_one_rung(client, admin_headers, db_engine):
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-01").current_rung
        # Seed already gives agent-01 one system-authored policy version
        # (onboarding at the floor, app/seed.py's pv-agent01-001) — assert
        # the delta this test produces, not an absolute count.
        system_versions_before = (
            session.query(PolicyVersion)
            .filter_by(agent_id="agent-01", created_by="system")
            .count()
        )

    _submit_critical_error(client, admin_headers, "agent-01", "inv-cascade-2")

    for _ in range(3):
        client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)

    with Session(db_engine) as session:
        assert session.get(Agent, "agent-01").current_rung == rung_before - 1
        versions = (
            session.query(PolicyVersion)
            .filter_by(agent_id="agent-01", created_by="system")
            .count()
        )
        assert versions - system_versions_before == 1, (
            "three calls off one critical error must write exactly one policy version"
        )


def test_new_decisions_reactivate_the_guard_for_a_second_real_clawback(
    client, admin_headers, db_engine
):
    """The guard must not make clawback permanently one-shot: once genuinely
    new decisions arrive and drift is still active, a second clawback
    applies."""
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-01").current_rung
        # Seed already gives agent-01 one system-authored policy version
        # (onboarding at the floor, app/seed.py's pv-agent01-001) — assert
        # the delta this test produces, not an absolute count.
        system_versions_before = (
            session.query(PolicyVersion)
            .filter_by(agent_id="agent-01", created_by="system")
            .count()
        )

    _submit_critical_error(client, admin_headers, "agent-01", "inv-cascade-3a")
    first = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers).json()
    assert first["direction"] == "CLAWBACK"

    with Session(db_engine) as session:
        assert session.get(Agent, "agent-01").current_rung == rung_before - 1

    # Immediately re-calling changes nothing (the guard above already
    # covers this) — now submit a genuinely new critical error so
    # decisions_since_last_change is no longer 0, with drift still CRITICAL.
    _submit_critical_error(client, admin_headers, "agent-01", "inv-cascade-3b")

    second = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers).json()
    assert second["direction"] == "CLAWBACK"
    assert second["recommendation_id"] != first["recommendation_id"]

    with Session(db_engine) as session:
        assert session.get(Agent, "agent-01").current_rung == rung_before - 2
        versions = (
            session.query(PolicyVersion)
            .filter_by(agent_id="agent-01", created_by="system")
            .count()
        )
        assert versions - system_versions_before == 2


def _rigged_clawback(proposed_limit: int):
    def _recommend(evaluation, mode=None, trust_evaluation_ref=None):
        return Recommendation(
            recommendation_id="rigged",
            agent_id=evaluation.agent_id,
            direction=Direction.CLAWBACK,
            proposed_limit=proposed_limit,
            proposed_rung=rung_of(proposed_limit),
            rationale="rigged for the cascade-guard test",
            opinions=(),
            has_dissent=False,
            confidence=1.0,
            governance_mode="stub",
            status=RecommendationStatus.PENDING,
            trust_evaluation_ref=trust_evaluation_ref,
            generated_at=None,
            clamped=False,
            clamped_from=None,
        )

    return _recommend


def test_floor_no_op_case_from_pr34_still_behaves(client, admin_headers, monkeypatch, db_engine):
    """PR #34's own guarantee — clawing back at the floor writes no
    redundant policy version — must be unaffected by this one."""
    with Session(db_engine) as session:
        agent = session.get(Agent, "agent-02")
        assert agent.current_limit == AUTONOMY_FLOOR, "test assumes agent-02 is seeded at the floor"
        versions_before = session.query(PolicyVersion).filter_by(agent_id="agent-02").count()

    monkeypatch.setattr("app.services.governance.recommend", _rigged_clawback(AUTONOMY_FLOOR))

    resp = client.post("/api/v1/agents/agent-02/recommendations", headers=admin_headers)
    assert resp.status_code == 201
    assert resp.json()["status"] == "APPROVED"

    with Session(db_engine) as session:
        assert session.get(Agent, "agent-02").current_limit == AUTONOMY_FLOOR
        versions_after = session.query(PolicyVersion).filter_by(agent_id="agent-02").count()
        assert versions_after == versions_before


def test_increase_and_hold_are_unaffected_by_the_guard(client, admin_headers, monkeypatch):
    """The guard only ever inspects direction == CLAWBACK; INCREASE and
    HOLD must behave exactly as before, called any number of times."""

    def _rigged_hold(evaluation, mode=None, trust_evaluation_ref=None):
        return Recommendation(
            recommendation_id="rigged",
            agent_id=evaluation.agent_id,
            direction=Direction.HOLD,
            proposed_limit=evaluation.current_limit,
            proposed_rung=rung_of(evaluation.current_limit),
            rationale="rigged HOLD",
            opinions=(),
            has_dissent=False,
            confidence=1.0,
            governance_mode="stub",
            status=RecommendationStatus.PENDING,
            trust_evaluation_ref=trust_evaluation_ref,
            generated_at=None,
            clamped=False,
            clamped_from=None,
        )

    monkeypatch.setattr("app.services.governance.recommend", _rigged_hold)
    first = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers).json()
    second = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers).json()
    assert first["status"] == "PENDING"
    assert second["status"] == "PENDING"
    # Unlike the CLAWBACK guard, HOLD calls are independent — each is a
    # fresh, real recommendation, not short-circuited to the previous one.
    assert first["recommendation_id"] != second["recommendation_id"]

    resp = client.post("/api/v1/agents/agent-02/recommendations", headers=admin_headers)
    assert resp.json()["direction"] in ("INCREASE", "HOLD")
    assert resp.json()["status"] == "PENDING"
