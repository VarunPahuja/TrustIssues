"""A CLAWBACK recommendation applies immediately, in the same transaction it
was generated in — no PENDING state, no human approval call. See ADR-0004
and app/services/governance.py:generate_recommendation's docstring for the
full reasoning; this file proves the mechanics.
"""

from __future__ import annotations

from shared.constants import AUTONOMY_FLOOR, limit_of, rung_of
from shared.contracts import Recommendation
from shared.enums import Direction, RecommendationStatus
from shared.reason_codes import CLAWBACK_CRITICAL_ERROR, CLAWBACK_DRIFT
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, Approval, AuditLogEntry, PolicyVersion


def _rigged_clawback(proposed_limit: int):
    def _recommend(evaluation, mode=None, trust_evaluation_ref=None):
        return Recommendation(
            recommendation_id="rigged",
            agent_id=evaluation.agent_id,
            direction=Direction.CLAWBACK,
            proposed_limit=proposed_limit,
            proposed_rung=rung_of(proposed_limit),
            rationale="rigged for the auto-clawback test",
            opinions=(),
            has_dissent=False,
            confidence=1.0,
            governance_mode="stub",
            status=RecommendationStatus.PENDING,  # what governance proposes; the backend overrides this
            trust_evaluation_ref=trust_evaluation_ref,
            generated_at=None,
            clamped=False,
            clamped_from=None,
        )

    return _recommend


def test_clawback_applies_immediately_no_approval_needed(client, admin_headers, monkeypatch, db_engine):
    with Session(db_engine) as session:
        agent_before = session.get(Agent, "agent-01")
        rung_before = agent_before.current_rung
        approvals_before = session.query(Approval).count()

    target_limit = limit_of(rung_before - 1)
    monkeypatch.setattr("app.services.governance.recommend", _rigged_clawback(target_limit))

    resp = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)
    assert resp.status_code == 201
    body = resp.json()

    # Applied, not queued: status is already APPROVED, no human ever called
    # approve/reject.
    assert body["status"] == "APPROVED"
    assert body["direction"] == "CLAWBACK"
    assert body["proposed_limit"] == target_limit

    with Session(db_engine) as session:
        agent_after = session.get(Agent, "agent-01")
        assert agent_after.current_limit == target_limit
        assert agent_after.current_rung == rung_before - 1

        version = (
            session.query(PolicyVersion)
            .filter_by(agent_id="agent-01")
            .order_by(PolicyVersion.effective_from.desc())
            .first()
        )
        assert version.limit == target_limit
        assert version.rung == rung_before - 1
        assert version.created_by == "system"

        # No Approval row: Approval.decided_by is a foreign key to users.id —
        # a human-approval table by construction. See the docstring in
        # app/services/governance.py for why inventing a fake "system" user
        # to satisfy it would misrepresent what this table means.
        assert session.query(Approval).count() == approvals_before


def test_clawback_never_drops_below_autonomy_floor(client, admin_headers, monkeypatch, db_engine):
    with Session(db_engine) as session:
        agent = session.get(Agent, "agent-02")
        assert agent.current_limit == AUTONOMY_FLOOR, "test assumes agent-02 is seeded at the floor"
        versions_before = session.query(PolicyVersion).filter_by(agent_id="agent-02").count()

    monkeypatch.setattr("app.services.governance.recommend", _rigged_clawback(AUTONOMY_FLOOR))

    resp = client.post("/api/v1/agents/agent-02/recommendations", headers=admin_headers)
    assert resp.status_code == 201
    assert resp.json()["status"] == "APPROVED"

    with Session(db_engine) as session:
        agent_after = session.get(Agent, "agent-02")
        assert agent_after.current_limit == AUTONOMY_FLOOR
        # Already-at-floor clawing back further is floor -> floor: no real
        # change, so no redundant policy version (same "only write when the
        # limit actually changes" rule PR #33 already applies to a no-op
        # HOLD approval).
        versions_after = session.query(PolicyVersion).filter_by(agent_id="agent-02").count()
        assert versions_after == versions_before


def test_increase_and_hold_recommendations_still_require_a_human(client, admin_headers, monkeypatch):
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
    resp = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)
    assert resp.status_code == 201
    assert resp.json()["status"] == "PENDING"


def test_clawback_audit_entry_is_distinguishable_from_a_human_approval(
    client, admin_headers, monkeypatch, db_engine
):
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-01").current_rung

    monkeypatch.setattr(
        "app.services.governance.recommend", _rigged_clawback(limit_of(rung_before - 1))
    )
    resp = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)
    rec_id = resp.json()["recommendation_id"]

    with Session(db_engine) as session:
        entry = (
            session.execute(
                select(AuditLogEntry).where(
                    AuditLogEntry.entity_id == rec_id, AuditLogEntry.entity_type == "recommendation"
                )
            )
            .scalars()
            .one()
        )
        assert entry.event_type == "recommendation.applied"
        assert entry.actor == "system"
        assert entry.actor_type == "system"
        assert entry.payload["policy_version_id"] is not None
        assert (
            CLAWBACK_DRIFT in entry.payload["reason_codes"]
            or CLAWBACK_CRITICAL_ERROR in entry.payload["reason_codes"]
        )


def test_decisions_since_clawback_recovers_correctly_after_auto_apply(
    client, admin_headers, monkeypatch, db_engine
):
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-01").current_rung

    monkeypatch.setattr(
        "app.services.governance.recommend", _rigged_clawback(limit_of(rung_before - 1))
    )
    client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)

    context = client.get("/api/v1/agents/agent-01", headers=admin_headers).json()["context"]
    assert context["decisions_since_clawback"] == 0
    assert context["decisions_since_last_change"] == 0

    for i in range(3):
        client.post(
            "/api/v1/decisions",
            headers=admin_headers,
            json={
                "invoice_id": f"inv-post-clawback-{i}",
                "amount": 100,
                "action": "APPROVE",
                "ground_truth": "APPROVE",
                "agent_id": "agent-01",
                "reason": "post-clawback recovery check",
            },
        )

    context_after = client.get("/api/v1/agents/agent-01", headers=admin_headers).json()["context"]
    assert context_after["decisions_since_clawback"] == 3
    assert context_after["decisions_since_last_change"] == 3


def test_cannot_approve_or_reject_an_already_auto_applied_clawback(
    client, admin_headers, monkeypatch, db_engine
):
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-01").current_rung

    monkeypatch.setattr(
        "app.services.governance.recommend", _rigged_clawback(limit_of(rung_before - 1))
    )
    resp = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)
    rec_id = resp.json()["recommendation_id"]
    assert resp.json()["status"] == "APPROVED"

    approve = client.post(
        f"/api/v1/recommendations/{rec_id}/approve",
        headers=admin_headers,
        json={"reason": "trying anyway"},
    )
    assert approve.status_code == 409
    assert approve.json()["code"] == "recommendation_already_resolved"

    reject = client.post(
        f"/api/v1/recommendations/{rec_id}/reject",
        headers=admin_headers,
        json={"reason": "trying anyway"},
    )
    assert reject.status_code == 409
    assert reject.json()["code"] == "recommendation_already_resolved"


def test_a_real_critical_error_produces_a_genuine_auto_applied_clawback(client, admin_headers, db_engine):
    """No rigging: real decisions, real trust evaluation, real governance
    coordinator. Proves the reason-code selection
    (app/services/governance.py's CLAWBACK_CRITICAL_ERROR-vs-CLAWBACK_DRIFT
    check) against what the trust engine actually produces, not an assumed
    shape.
    """
    with Session(db_engine) as session:
        rung_before = session.get(Agent, "agent-02").current_rung

    # One critical error (APPROVE where ground truth is REJECT) is enough:
    # trust/trust_engine/stats/drift.py returns DriftSeverity.CRITICAL the
    # instant one is found in the recent window, no statistical tripwire
    # needed first (ADR-0004).
    client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-real-critical-error",
            "amount": 100,
            "action": "APPROVE",
            "ground_truth": "REJECT",
            "agent_id": "agent-02",
            "reason": "real critical error for the auto-clawback test",
        },
    )

    resp = client.post("/api/v1/agents/agent-02/recommendations", headers=admin_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["direction"] == "CLAWBACK"
    assert body["status"] == "APPROVED"

    with Session(db_engine) as session:
        agent_after = session.get(Agent, "agent-02")
        assert agent_after.current_rung == max(rung_before - 1, 0)

        entry = (
            session.execute(
                select(AuditLogEntry).where(
                    AuditLogEntry.entity_id == body["recommendation_id"],
                    AuditLogEntry.entity_type == "recommendation",
                )
            )
            .scalars()
            .one()
        )
        assert entry.payload["reason_codes"] == [CLAWBACK_CRITICAL_ERROR]
