from __future__ import annotations

from datetime import UTC, datetime

import pytest
from shared import reason_codes
from shared.constants import limit_of, rung_of
from shared.contracts import Recommendation
from shared.enums import Direction, RecommendationStatus
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.recommendations import _record_decision
from app.models import Agent, Approval, AuditLogEntry, PolicyVersion
from app.models import Recommendation as RecommendationRow
from app.models import TrustEvaluation as TrustEvaluationRow
from app.models.audit_hash import GENESIS_HASH, compute_hash
from app.schemas.user import CurrentUser, Role
from app.services.governance import generate_recommendation

_ADMIN = CurrentUser(user_id="user-admin-01", email="admin@aagp.dev", role=Role.ADMIN)


def test_list_recommendations(client, admin_headers):
    # seed.py persists three rows: rec-agent01-001 (PENDING), rec-agent03-001
    # (APPROVED clawback), rec-agent01-000 (APPROVED, an earlier increase) —
    # one more than app/fixtures/recommendations.py's two, which never
    # included the historical rec-agent01-000.
    resp = client.get("/api/v1/recommendations", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    directions = {r["direction"] for r in body["items"]}
    assert directions == {"INCREASE", "CLAWBACK"}


def test_get_recommendation(client, admin_headers):
    resp = client.get("/api/v1/recommendations/rec-agent01-001", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["direction"] == "INCREASE"
    assert len(body["opinions"]) == 4
    # the hard ceiling, visible in the response, not just in a log
    assert body["clamped"] is True
    assert body["clamped_from"] == 10000


def test_get_recommendation_not_found(client, admin_headers):
    resp = client.get("/api/v1/recommendations/does-not-exist", headers=admin_headers)
    assert resp.status_code == 404


def test_approve_recommendation_as_admin(client, admin_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve",
        headers=admin_headers,
        json={"reason": "Evidence is solid, cooldown satisfied."},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "APPROVED"


def test_approve_recommendation_requires_reason(client, admin_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve", headers=admin_headers, json={}
    )
    assert resp.status_code == 422


def test_reviewer_cannot_approve(client, reviewer_headers):
    # docs/lanes/vp.md, Thu 10 Sept security-pass check: "Reviewer cannot approve"
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve",
        headers=reviewer_headers,
        json={"reason": "trying anyway"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden"


def test_auditor_cannot_approve(client, auditor_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve",
        headers=auditor_headers,
        json={"reason": "trying anyway"},
    )
    assert resp.status_code == 403


def test_reject_recommendation_as_admin(client, admin_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/reject",
        headers=admin_headers,
        json={"reason": "Not this time."},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"


def test_cannot_decide_an_already_resolved_recommendation(client, admin_headers):
    # rec-agent03-001 is already APPROVED (an automatic clawback) in the fixture.
    resp = client.post(
        "/api/v1/recommendations/rec-agent03-001/approve",
        headers=admin_headers,
        json={"reason": "trying anyway"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "recommendation_already_resolved"


def test_cannot_reject_an_already_resolved_recommendation(client, admin_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent03-001/reject",
        headers=admin_headers,
        json={"reason": "trying anyway"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "recommendation_already_resolved"


def test_reviewer_cannot_reject(client, reviewer_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/reject",
        headers=reviewer_headers,
        json={"reason": "trying anyway"},
    )
    assert resp.status_code == 403


def test_auditor_cannot_reject(client, auditor_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/reject",
        headers=auditor_headers,
        json={"reason": "trying anyway"},
    )
    assert resp.status_code == 403


def test_reject_recommendation_requires_reason(client, admin_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/reject", headers=admin_headers, json={}
    )
    assert resp.status_code == 422


# --- the human-authorization write path: approvals + policy_versions ------------------


def test_approve_creates_an_approval_row_and_moves_the_agent(client, admin_headers, db_engine):
    with Session(db_engine) as session:
        agent_before = session.get(Agent, "agent-01")
        limit_before = agent_before.current_limit
        rung_before = agent_before.current_rung
        assert limit_before != limit_of(3)  # sanity: the approval has somewhere to move it to

    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve",
        headers=admin_headers,
        json={"reason": "Evidence is solid, cooldown satisfied."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "APPROVED"

    with Session(db_engine) as session:
        approval = (
            session.query(Approval).filter_by(recommendation_id="rec-agent01-001").one()
        )
        assert approval.decided_by == "user-admin-01"
        assert approval.verdict == RecommendationStatus.APPROVED
        assert approval.reason == "Evidence is solid, cooldown satisfied."

        agent_after = session.get(Agent, "agent-01")
        # rec-agent01-001's proposed_limit (seed.py) is limit_of(3), already
        # the post-clamp value — the agent lands exactly there.
        assert agent_after.current_limit == limit_of(3)
        assert agent_after.current_rung == rung_of(limit_of(3))
        assert agent_after.current_limit != limit_before
        assert agent_after.current_rung != rung_before


def test_approve_writes_a_policy_version_chained_to_the_prior_one(client, admin_headers, db_engine):
    with Session(db_engine) as session:
        prior = (
            session.query(PolicyVersion)
            .filter_by(agent_id="agent-01")
            .order_by(PolicyVersion.effective_from.desc())
            .first()
        )
        prior_id = prior.id

    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve",
        headers=admin_headers,
        json={"reason": "Evidence is solid, cooldown satisfied."},
    )
    assert resp.status_code == 200

    with Session(db_engine) as session:
        newest = (
            session.query(PolicyVersion)
            .filter_by(agent_id="agent-01")
            .order_by(PolicyVersion.effective_from.desc())
            .first()
        )
        assert newest.id != prior_id
        assert newest.previous_version_id == prior_id
        assert newest.limit == limit_of(3)
        assert newest.rung == rung_of(limit_of(3))
        assert newest.created_by == "user-admin-01"


def test_reject_creates_an_approval_and_changes_nothing_else(client, admin_headers, db_engine):
    with Session(db_engine) as session:
        agent_before = session.get(Agent, "agent-01")
        limit_before = agent_before.current_limit
        rung_before = agent_before.current_rung
        versions_before = session.query(PolicyVersion).filter_by(agent_id="agent-01").count()

    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/reject",
        headers=admin_headers,
        json={"reason": "Not this time."},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"

    with Session(db_engine) as session:
        approval = (
            session.query(Approval).filter_by(recommendation_id="rec-agent01-001").one()
        )
        assert approval.verdict == RecommendationStatus.REJECTED
        assert approval.reason == "Not this time."

        agent_after = session.get(Agent, "agent-01")
        assert agent_after.current_limit == limit_before
        assert agent_after.current_rung == rung_before
        versions_after = session.query(PolicyVersion).filter_by(agent_id="agent-01").count()
        assert versions_after == versions_before


def test_approve_and_reject_each_append_exactly_one_audit_entry_and_the_chain_verifies(
    client, admin_headers, db_engine
):
    with Session(db_engine) as session:
        before = session.query(AuditLogEntry).count()

    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve",
        headers=admin_headers,
        json={"reason": "Evidence is solid."},
    )
    assert resp.status_code == 200

    with Session(db_engine) as session:
        after_approve = session.query(AuditLogEntry).count()
        assert after_approve - before == 1
        approved_entry = (
            session.query(AuditLogEntry).filter_by(event_type="recommendation.approved").one()
        )
        assert approved_entry.entity_id == "rec-agent01-001"

    resp = client.post(
        "/api/v1/recommendations/rec-agent03-001/reject",
        headers=admin_headers,
        json={"reason": "trying anyway"},
    )
    # rec-agent03-001 is already resolved — this is a 409, so it must NOT
    # append an audit entry; only the approve above should have.
    assert resp.status_code == 409

    with Session(db_engine) as session:
        after_reject_attempt = session.query(AuditLogEntry).count()
        assert after_reject_attempt == after_approve

        entries = session.query(AuditLogEntry).order_by(AuditLogEntry.ts).all()
        prev_hash = GENESIS_HASH
        for entry in entries:
            assert entry.prev_hash == prev_hash
            assert entry.hash == compute_hash(prev_hash, entry.payload)
            prev_hash = entry.hash


def test_approve_lands_on_the_clamped_value_not_what_governance_proposed(
    client, admin_headers, db_engine, monkeypatch
):
    """Same rigging as the generation-side clamp test: forces a fresh
    PENDING recommendation whose stored `proposed_limit` is already the
    post-clamp value, then proves approval moves the agent there — never to
    whatever governance originally asked for."""

    def _rigged_recommend(evaluation, mode=None, trust_evaluation_ref=None):
        return Recommendation(
            recommendation_id="rigged",
            agent_id=evaluation.agent_id,
            direction=Direction.INCREASE,
            proposed_limit=999_999,
            proposed_rung=4,
            rationale="rigged for the clamp test",
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

    monkeypatch.setattr("app.services.governance.recommend", _rigged_recommend)

    gen_resp = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)
    assert gen_resp.status_code == 201
    rec = gen_resp.json()
    assert rec["clamped"] is True
    assert rec["proposed_limit"] < 999_999

    approve_resp = client.post(
        f"/api/v1/recommendations/{rec['recommendation_id']}/approve",
        headers=admin_headers,
        json={"reason": "Evidence supports this, not the rigged ask."},
    )
    assert approve_resp.status_code == 200

    with Session(db_engine) as session:
        agent = session.get(Agent, "agent-01")
        assert agent.current_limit == rec["proposed_limit"]
        assert agent.current_limit != 999_999


def test_approve_mid_transaction_failure_rolls_back_everything(db_engine):
    """Same pattern as the generation-side rollback test: force a second,
    colliding write into the same, still-uncommitted transaction
    `_record_decision` built, and prove the whole thing — approval, policy
    version, agent limit/rung, audit entry — rolls back together, not just
    the piece that collided."""
    with Session(db_engine) as session:
        before_approvals = session.query(Approval).count()
        before_versions = session.query(PolicyVersion).count()
        before_audit = session.query(AuditLogEntry).count()
        agent_before = session.get(Agent, "agent-01")
        limit_before = agent_before.current_limit
        rung_before = agent_before.current_rung

        _record_decision(
            session, _ADMIN, "rec-agent01-001", RecommendationStatus.APPROVED, "test"
        )

        # A second, colliding row (duplicate primary key, matching the same
        # forced-collision pattern test_recommendation_generation_mid_transaction_failure_rolls_back_everything
        # uses) — forces the whole not-yet-committed transaction, including
        # everything _record_decision just added, to fail on commit.
        session.add(
            RecommendationRow(
                id="rec-agent03-001",  # already exists (seed.py) — PK collision
                agent_id="agent-01",
                trust_evaluation_id="trust-eval-agent01-002",
                direction=Direction.INCREASE,
                proposed_limit=limit_of(3),
                rationale="forced collision",
                agent_opinions=[],
                status=RecommendationStatus.PENDING,
                governance_mode="stub",
                clamped=False,
                clamped_from=None,
                generated_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with Session(db_engine) as verify:
        assert verify.query(Approval).count() == before_approvals
        assert verify.query(PolicyVersion).count() == before_versions
        assert verify.query(AuditLogEntry).count() == before_audit
        agent_after = verify.get(Agent, "agent-01")
        assert agent_after.current_limit == limit_before
        assert agent_after.current_rung == rung_before
        rec = verify.get(RecommendationRow, "rec-agent01-001")
        assert rec.status == RecommendationStatus.PENDING


# --- generation: POST /agents/{agent_id}/recommendations --------------------------------


def test_generate_recommendation_persists_a_row(client, admin_headers, db_engine):
    resp = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["agent_id"] == "agent-01"
    assert body["status"] == "PENDING"  # governance can never self-approve, ADR-0004
    assert len(body["opinions"]) == 4

    with Session(db_engine) as session:
        row = session.get(RecommendationRow, body["recommendation_id"])
        assert row is not None
        assert len(row.agent_opinions) == 4
        assert row.trust_evaluation_id is not None
        assert session.get(TrustEvaluationRow, row.trust_evaluation_id) is not None


def test_generate_recommendation_unknown_agent_404s(client, admin_headers):
    resp = client.post("/api/v1/agents/does-not-exist/recommendations", headers=admin_headers)
    assert resp.status_code == 404


def test_recommendation_above_evidence_is_clamped(client, admin_headers, monkeypatch):
    """Governance's own `_aggregate` already asserts it can never propose
    above `evaluation.recommended_limit` (governance/governance/coordinator.py,
    governance/INTEGRATION.md) — this proves the *backend's* independent
    clamp still fires and is recorded, the way it would have to if that
    assertion were ever wrong. Only `recommend()`'s return value is faked;
    `clamp_recommendation` runs for real, exactly as it would against a live
    proposal (docs/lanes/vp.md, responsibility 4 — the architectural claim)."""

    def _rigged_recommend(evaluation, mode=None, trust_evaluation_ref=None):
        return Recommendation(
            recommendation_id="rigged",
            agent_id=evaluation.agent_id,
            direction=Direction.INCREASE,
            proposed_limit=999_999,  # absurdly above evaluation.recommended_limit
            proposed_rung=4,
            rationale="rigged for the clamp test",
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

    monkeypatch.setattr("app.services.governance.recommend", _rigged_recommend)

    resp = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["clamped"] is True
    assert body["clamped_from"] == 999_999
    assert body["proposed_limit"] < 999_999
    # docs/audits/2026-09-06-audit.md 1f: RECOMMENDATION_CLAMPED was defined
    # in shared/, exported to the frontend, and produced by nothing.
    assert reason_codes.RECOMMENDATION_CLAMPED in body["reason_codes"]


def test_an_unclamped_recommendation_carries_no_clamp_reason_code(client, admin_headers):
    resp = client.post("/api/v1/agents/agent-02/recommendations", headers=admin_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["clamped"] is False
    assert reason_codes.RECOMMENDATION_CLAMPED not in body["reason_codes"]


def test_every_generation_appends_exactly_one_audit_entry_and_the_chain_verifies(
    client, admin_headers, db_engine
):
    with Session(db_engine) as session:
        before = session.query(AuditLogEntry).count()

    for _ in range(3):
        resp = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)
        assert resp.status_code == 201

    with Session(db_engine) as session:
        after = session.query(AuditLogEntry).count()
        generated = (
            session.query(AuditLogEntry)
            .filter(AuditLogEntry.event_type == "recommendation.generated")
            .count()
        )
        assert after - before == 3
        assert generated == 3

        entries = session.query(AuditLogEntry).order_by(AuditLogEntry.ts).all()
        prev_hash = GENESIS_HASH
        for entry in entries:
            assert entry.prev_hash == prev_hash
            assert entry.hash == compute_hash(prev_hash, entry.payload)
            prev_hash = entry.hash


def test_cached_mode_with_no_matching_recording_returns_503_not_500(
    client, admin_headers, monkeypatch
):
    # agent-01's real, DB-derived evidence does not match any of the
    # committed demo-scenario recordings (governance/recordings/) — cached
    # mode has to miss, cleanly, for evidence nobody recorded a response to.
    monkeypatch.setenv("GOVERNANCE_MODE", "cached")
    resp = client.post("/api/v1/agents/agent-01/recommendations", headers=admin_headers)
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "governance_unavailable"
    assert "agent-01" in body["message"]


def test_recommendation_generation_mid_transaction_failure_rolls_back_everything(db_engine):
    with Session(db_engine) as session:
        before_trust = session.query(TrustEvaluationRow).count()
        before_rec = session.query(RecommendationRow).count()
        before_audit = session.query(AuditLogEntry).count()

        agent = session.get(Agent, "agent-01")
        recommendation_out = generate_recommendation(session, agent)

        # Force a second, colliding row into the same, still-uncommitted
        # transaction — proves the whole transaction (trust evaluation +
        # recommendation + audit_log, all added above) is atomic, not just
        # the first insert, the same way test_decisions.py proves it for
        # decision ingest.
        session.add(
            RecommendationRow(
                id=recommendation_out.recommendation_id,
                agent_id="agent-01",
                trust_evaluation_id=recommendation_out.trust_evaluation_ref,
                direction=recommendation_out.direction,
                proposed_limit=recommendation_out.proposed_limit,
                rationale="duplicate id, forced collision",
                agent_opinions=[],
                status=recommendation_out.status,
                governance_mode=recommendation_out.governance_mode,
                clamped=False,
                clamped_from=None,
                generated_at=recommendation_out.generated_at,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with Session(db_engine) as verify:
        assert verify.query(TrustEvaluationRow).count() == before_trust
        assert verify.query(RecommendationRow).count() == before_rec
        assert verify.query(AuditLogEntry).count() == before_audit
