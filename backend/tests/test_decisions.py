from __future__ import annotations

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.decisions import _create_decision
from app.models import AuditLogEntry, Decision, Invoice, PolicyVersion
from app.models.audit_hash import GENESIS_HASH, compute_hash
from app.policy import reason_codes
from app.schemas.decision import DecisionCreate


def test_create_decision(client, admin_headers):
    resp = client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-9999",
            "amount": 750,
            "action": "APPROVE",
            "ground_truth": "APPROVE",
            "agent_id": "agent-01",
            "reason": "smoke test",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["invoice_id"] == "inv-9999"
    assert body["action"] == "APPROVE"
    assert body["decision_id"].startswith("dec-")


def test_create_decision_requires_reason(client, admin_headers):
    resp = client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-9999",
            "amount": 750,
            "action": "APPROVE",
            "ground_truth": "APPROVE",
            "agent_id": "agent-01",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"


def test_list_decisions(client, admin_headers):
    resp = client.get("/api/v1/decisions", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    agent_ids = {d["agent_id"] for d in body["items"]}
    assert agent_ids <= {"agent-01", "agent-02", "agent-03"}


def test_get_decision(client, admin_headers):
    resp = client.get("/api/v1/decisions/dec-agent03-0193", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    # the critical error behind agent-03's clawback: APPROVE where ground truth is REJECT
    assert body["action"] == "APPROVE"
    assert body["ground_truth"] == "REJECT"


def test_get_decision_not_found(client, admin_headers):
    resp = client.get("/api/v1/decisions/does-not-exist", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == "decision_not_found"


def test_posted_decision_is_actually_persisted(client, admin_headers):
    resp = client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-persist-001",
            "amount": 500,
            "action": "APPROVE",
            "ground_truth": "APPROVE",
            "agent_id": "agent-01",
            "reason": "persistence check",
        },
    )
    assert resp.status_code == 201
    decision_id = resp.json()["decision_id"]

    fetched = client.get(f"/api/v1/decisions/{decision_id}", headers=admin_headers)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["invoice_id"] == "inv-persist-001"
    assert body["amount"] == 500
    assert body["agent_id"] == "agent-01"


def test_two_identical_posts_create_two_distinct_rows(client, admin_headers):
    body = {
        "invoice_id": "inv-dup-001",
        "amount": 500,
        "action": "APPROVE",
        "ground_truth": "APPROVE",
        "agent_id": "agent-01",
        "reason": "duplicate-post check",
    }
    first = client.post("/api/v1/decisions", headers=admin_headers, json=body)
    second = client.post("/api/v1/decisions", headers=admin_headers, json=body)
    assert first.status_code == 201
    assert second.status_code == 201

    first_id = first.json()["decision_id"]
    second_id = second.json()["decision_id"]
    assert first_id != second_id
    assert first.json()["sequence"] != second.json()["sequence"]

    listing = client.get("/api/v1/decisions", headers=admin_headers)
    ids = {d["decision_id"] for d in listing.json()["items"]}
    assert {first_id, second_id} <= ids


def test_amount_over_the_limit_escalates_with_the_right_reason_code(client, admin_headers, db_engine):
    # agent-01 is seeded at rung 2 (limit 2,500) — 9,000 is well over.
    resp = client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-over-limit-001",
            "amount": 9000,
            "action": "ESCALATE",
            "ground_truth": "APPROVE",
            "agent_id": "agent-01",
            "reason": "over-limit check",
        },
    )
    assert resp.status_code == 201
    decision_id = resp.json()["decision_id"]

    with Session(db_engine) as session:
        decision = session.get(Decision, decision_id)
        assert decision.within_limit is False

        entry = (
            session.query(AuditLogEntry)
            .filter(AuditLogEntry.entity_id == decision_id, AuditLogEntry.entity_type == "decision")
            .one()
        )
        assert entry.payload["allowed"] is False
        assert entry.payload["reason_code"] == reason_codes.LIMIT_EXCEEDED


def test_amount_within_the_limit_is_allowed(client, admin_headers, db_engine):
    resp = client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-within-limit-001",
            "amount": 300,
            "action": "APPROVE",
            "ground_truth": "APPROVE",
            "agent_id": "agent-01",
            "reason": "within-limit check",
        },
    )
    assert resp.status_code == 201
    decision_id = resp.json()["decision_id"]

    with Session(db_engine) as session:
        decision = session.get(Decision, decision_id)
        assert decision.within_limit is True

        entry = (
            session.query(AuditLogEntry)
            .filter(AuditLogEntry.entity_id == decision_id, AuditLogEntry.entity_type == "decision")
            .one()
        )
        assert entry.payload["allowed"] is True
        assert entry.payload["reason_code"] == reason_codes.WITHIN_LIMIT


def test_every_persisted_decision_references_a_real_policy_version(client, admin_headers, db_engine):
    resp = client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-pv-ref-001",
            "amount": 200,
            "action": "APPROVE",
            "ground_truth": "APPROVE",
            "agent_id": "agent-02",
            "reason": "policy version reference check",
        },
    )
    assert resp.status_code == 201
    decision_id = resp.json()["decision_id"]

    with Session(db_engine) as session:
        decision = session.get(Decision, decision_id)
        assert decision.policy_version_id is not None
        version = session.get(PolicyVersion, decision.policy_version_id)
        assert version is not None
        assert version.agent_id == "agent-02"


def test_each_post_appends_exactly_one_audit_log_row_and_the_chain_still_verifies(
    client, admin_headers, db_engine
):
    with Session(db_engine) as session:
        before = session.query(AuditLogEntry).count()

    for i in range(3):
        resp = client.post(
            "/api/v1/decisions",
            headers=admin_headers,
            json={
                "invoice_id": f"inv-chain-{i}",
                "amount": 100 + i,
                "action": "APPROVE",
                "ground_truth": "APPROVE",
                "agent_id": "agent-01",
                "reason": "chain check",
            },
        )
        assert resp.status_code == 201

    with Session(db_engine) as session:
        after = session.query(AuditLogEntry).count()
        assert after - before == 3

        entries = session.query(AuditLogEntry).order_by(AuditLogEntry.ts).all()
        prev_hash = GENESIS_HASH
        for entry in entries:
            assert entry.prev_hash == prev_hash
            assert entry.hash == compute_hash(prev_hash, entry.payload)
            prev_hash = entry.hash


def test_unknown_agent_id_fails_cleanly_rather_than_500ing(client, admin_headers):
    resp = client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-unknown-agent-001",
            "amount": 500,
            "action": "APPROVE",
            "ground_truth": "APPROVE",
            "agent_id": "does-not-exist",
            "reason": "unknown agent check",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "agent_not_found"


def test_a_failure_mid_transaction_rolls_back_everything(db_engine):
    """Proves the whole transaction (invoice + decision + audit_log, all
    added inside `_create_decision`) is atomic — a duplicate primary key
    forced into the same, still-uncommitted transaction always fails at
    commit, regardless of dialect, and nothing from that transaction
    survives.

    The colliding row is inserted via a Core `insert()`, not a second
    `Decision(...)` ORM object sharing `decision`'s primary key: constructing
    two different Python objects for the same identity in one session's
    identity map makes SQLAlchemy itself warn ("New instance ... conflicts
    with persistent instance") *before* anything reaches the database — a
    session-bookkeeping complaint, not the database-level primary-key
    violation this test means to force, and previously present here
    (docs/audits/2026-09-06-audit.md, freeze-cleanup item 3). Core bypasses
    the identity map entirely, so the only way `session.commit()` can still
    fail is the real `uq`/primary-key constraint on `decisions.id` — the
    property actually being tested.

    The verification half only ever touches primitives captured before the
    first session closes (`decision_id`/`invoice_id`), never the `decision`
    ORM object itself — that object belongs to the closed, rolled-back
    session above, and asserting through a *different*, freshly-opened
    session is what actually proves the data is gone from the database,
    rather than merely absent from one Python object's in-memory state.

    Confirmed this test is not vacuous: temporarily removing the explicit
    `session.rollback()` call below still passes (SQLAlchemy's own
    `Session.close()` already discards an unflushed/failed transaction, so
    that call is hygiene, not what this test depends on) — but temporarily
    breaking `_create_decision` itself to commit the invoice in its own
    early transaction (simulating a genuine atomicity regression) makes
    this test fail, as it should. Both checked by hand while writing this;
    neither is asserted here as permanent test code, since the second one
    requires mutating application code to prove.
    """
    body = DecisionCreate(
        invoice_id="inv-rollback-001",
        amount=300,
        action="APPROVE",
        ground_truth="APPROVE",
        agent_id="agent-01",
        reason="rollback check",
    )
    with Session(db_engine) as session:
        decision = _create_decision(session, body)
        decision_id = decision.id
        invoice_id = decision.invoice_id

        # Unlike `session.add(...)`, a Core `insert()` executes immediately
        # against the connection rather than waiting for a flush — the
        # IntegrityError surfaces here, not at `session.commit()`.
        with pytest.raises(IntegrityError):
            session.execute(
                insert(Decision.__table__).values(
                    id=decision_id,
                    sequence=decision.sequence + 1,
                    invoice_id=decision.invoice_id,
                    agent_id=decision.agent_id,
                    action=decision.action,
                    policy_version_id=decision.policy_version_id,
                    within_limit=True,
                    decided_at=decision.decided_at,
                )
            )
        session.rollback()

    with Session(db_engine) as verify:
        assert verify.get(Invoice, invoice_id) is None
        assert verify.get(Decision, decision_id) is None
