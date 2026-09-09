"""Post-hoc audit sampling, end to end (ADR-0009).

Three things this pins: the rate really does fall as an agent earns rungs,
selection is reproducible, and a review is persisted rather than echoed back.

The rate is the operational half of "earned autonomy" — a bigger rupee ceiling
with unchanged review burden delivers none of what the project promises — so
these are claims worth failing a build over.
"""

from __future__ import annotations

import pytest
from shared.constants import SAMPLING_RATE_BY_RUNG, sampling_rate_of
from shared.enums import ReviewVerdict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditSample
from app.services.audit_sampling import selection_fraction, should_sample

# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selection_is_deterministic():
    """Same decision, same answer — every time, in every process. Random
    selection would have cost the project its reproducibility claim: two runs
    of one seeded simulation would disagree about what a human was asked to
    check."""
    ids = [f"dec-{i:08x}" for i in range(200)]
    assert [should_sample(i, 2) for i in ids] == [should_sample(i, 2) for i in ids]


def test_selection_fraction_is_uniform_enough_to_honour_the_rate():
    """The choice is fixed, but the rate still has to be true. Over many ids
    the hash must spread evenly across [0, 1), or a "25% sample" would not be
    25% of anything."""
    ids = [f"dec-{i:08x}" for i in range(4000)]
    fractions = [selection_fraction(i) for i in ids]
    assert all(0.0 <= f < 1.0 for f in fractions)
    # Each quartile should hold roughly a quarter. Generous bounds: this is a
    # smoke test for gross bias, not a statistical proof.
    for lo in (0.0, 0.25, 0.5, 0.75):
        share = sum(1 for f in fractions if lo <= f < lo + 0.25) / len(fractions)
        assert 0.2 < share < 0.3, f"quartile at {lo} held {share:.3f}"


@pytest.mark.parametrize("rung", range(len(SAMPLING_RATE_BY_RUNG)))
def test_the_sampled_share_matches_the_rung_rate(rung):
    ids = [f"dec-{i:08x}" for i in range(4000)]
    sampled = sum(1 for i in ids if should_sample(i, rung))
    expected = sampling_rate_of(rung)
    assert abs(sampled / len(ids) - expected) < 0.03, (
        f"rung {rung}: sampled {sampled / len(ids):.3f}, expected {expected}"
    )


def test_review_burden_falls_as_rungs_are_earned():
    """The whole point of ADR-0009: earning autonomy must buy less oversight,
    not just a higher ceiling."""
    ids = [f"dec-{i:08x}" for i in range(4000)]
    shares = [sum(1 for i in ids if should_sample(i, r)) for r in range(len(SAMPLING_RATE_BY_RUNG))]
    assert shares == sorted(shares, reverse=True), shares
    assert shares[0] == len(ids), "every decision is reviewed at the floor rung"
    assert shares[-1] < shares[0] // 10, "the top rung reviews a small fraction"


# ---------------------------------------------------------------------------
# Selection through real ingest
# ---------------------------------------------------------------------------


def _post_decision(client, headers, i: int, action: str = "APPROVE") -> str:
    resp = client.post(
        "/api/v1/decisions",
        headers=headers,
        json={
            "invoice_id": f"inv-sampling-{action}-{i}",
            "amount": 100,
            "action": action,
            "ground_truth": "APPROVE",
            "agent_id": "agent-01",
            "reason": "audit sampling test",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["decision_id"]


def test_ingest_creates_samples_for_some_decisions(client, admin_headers, db_engine):
    ids = [_post_decision(client, admin_headers, i) for i in range(40)]
    with Session(db_engine) as session:
        rows = (
            session.execute(select(AuditSample).where(AuditSample.decision_id.in_(ids)))
            .scalars()
            .all()
        )
    assert rows, "agent-01 is not at the top rung; some decisions must be sampled"
    assert len(rows) < len(ids), "and not all of them"
    for row in rows:
        assert row.reviewed_at is None, "a fresh sample is pending"


def test_escalations_are_never_sampled(client, admin_headers, db_engine):
    """An escalation was already handed to a human. Reviewing the fact that a
    human was asked adds burden and no evidence."""
    ids = [_post_decision(client, admin_headers, i, action="ESCALATE") for i in range(40)]
    with Session(db_engine) as session:
        rows = (
            session.execute(select(AuditSample).where(AuditSample.decision_id.in_(ids)))
            .scalars()
            .all()
        )
    assert rows == []


def test_the_decision_audit_entry_records_whether_it_was_sampled(client, admin_headers):
    ids = [_post_decision(client, admin_headers, i) for i in range(40)]
    log = client.get("/api/v1/audit-log", headers=admin_headers).json()
    entries = {
        e["entity_id"]: e for e in log["items"] if e["event_type"] == "decision.recorded"
    }
    seen = [entries[d]["payload"]["audit_sample_id"] for d in ids if d in entries]
    assert seen, "the decisions should appear in the audit log"
    assert any(v is not None for v in seen), "at least one sampled"
    assert any(v is None for v in seen), "at least one not sampled"


# ---------------------------------------------------------------------------
# The review queue
# ---------------------------------------------------------------------------


def test_pending_filter_returns_only_unreviewed(client, admin_headers):
    resp = client.get("/api/v1/audit-samples?pending=true", headers=admin_headers)
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["reviewed_at"] is None
        assert item["verdict"] is None


def test_agent_filter_narrows_the_queue(client, admin_headers):
    resp = client.get("/api/v1/audit-samples?agent_id=agent-03", headers=admin_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "agent-03 has seeded samples"
    assert {i["agent_id"] for i in items} == {"agent-03"}


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def test_a_review_is_persisted_not_echoed(client, admin_headers, reviewer_headers):
    """The stub this replaces returned a modified copy and wrote nothing. The
    read-back is the whole test."""
    resp = client.post(
        "/api/v1/audit-samples/sample-002/review",
        headers=reviewer_headers,
        json={"verdict": "AGREED", "reviewer_action": "APPROVE", "reason": "checked, agent was right"},
    )
    assert resp.status_code == 200, resp.text

    listing = client.get("/api/v1/audit-samples?agent_id=agent-02", headers=admin_headers).json()
    reviewed = [i for i in listing["items"] if i["sample_id"] == "sample-002"]
    assert len(reviewed) == 1
    assert reviewed[0]["verdict"] == "AGREED"
    assert reviewed[0]["reviewed_at"] is not None
    assert reviewed[0]["reviewer"] == "user-reviewer-01"


def test_reviewing_twice_is_a_conflict(client, reviewer_headers):
    body = {"verdict": "AGREED", "reviewer_action": "APPROVE", "reason": "first"}
    first = client.post(
        "/api/v1/audit-samples/sample-004/review", headers=reviewer_headers, json=body
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/audit-samples/sample-004/review",
        headers=reviewer_headers,
        json={**body, "reason": "second"},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "audit_sample_already_reviewed"


def test_a_disagreeing_review_emits_the_reason_code(client, admin_headers, reviewer_headers):
    """`SAMPLE_REVIEW_DISAGREEMENT` had no producer anywhere in the codebase
    before this. A reviewer contradicting the agent is evidence, and this is
    where it enters the record."""
    resp = client.post(
        "/api/v1/audit-samples/sample-002/review",
        headers=reviewer_headers,
        json={
            "verdict": "DISAGREED",
            "reviewer_action": "REJECT",
            "reason": "vendor was on the blocked list",
        },
    )
    assert resp.status_code == 200

    log = client.get("/api/v1/audit-log", headers=admin_headers).json()
    entries = [
        e
        for e in log["items"]
        if e["event_type"] == "audit_sample.reviewed" and e["entity_id"] == "sample-002"
    ]
    assert len(entries) == 1
    payload = entries[0]["payload"]
    assert payload["reason_codes"] == ["SAMPLE_REVIEW_DISAGREEMENT"]
    assert payload["verdict"] == "DISAGREED"
    assert payload["reason"] == "vendor was on the blocked list"


def test_an_agreeing_review_emits_no_reason_code(client, admin_headers, reviewer_headers):
    client.post(
        "/api/v1/audit-samples/sample-004/review",
        headers=reviewer_headers,
        json={"verdict": "AGREED", "reviewer_action": "APPROVE", "reason": "fine"},
    )
    log = client.get("/api/v1/audit-log", headers=admin_headers).json()
    entry = next(
        e
        for e in log["items"]
        if e["event_type"] == "audit_sample.reviewed" and e["entity_id"] == "sample-004"
    )
    assert entry["payload"]["reason_codes"] == []


def test_the_audit_chain_still_verifies_after_a_review(client, admin_headers, reviewer_headers):
    client.post(
        "/api/v1/audit-samples/sample-002/review",
        headers=reviewer_headers,
        json={"verdict": "AGREED", "reviewer_action": "APPROVE", "reason": "chain check"},
    )
    log = client.get("/api/v1/audit-log", headers=admin_headers).json()
    assert log["chain_valid"] is True


def test_reviewing_an_unknown_sample_is_404(client, reviewer_headers):
    resp = client.post(
        "/api/v1/audit-samples/does-not-exist/review",
        headers=reviewer_headers,
        json={"verdict": "AGREED", "reviewer_action": "APPROVE", "reason": "nothing there"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "audit_sample_not_found"


def test_auditor_cannot_review(client, auditor_headers):
    resp = client.post(
        "/api/v1/audit-samples/sample-002/review",
        headers=auditor_headers,
        json={"verdict": "AGREED", "reviewer_action": "APPROVE", "reason": "not my call"},
    )
    assert resp.status_code == 403


def test_verdict_and_action_are_validated(client, reviewer_headers):
    resp = client.post(
        "/api/v1/audit-samples/sample-002/review",
        headers=reviewer_headers,
        json={"verdict": "MAYBE", "reviewer_action": "APPROVE", "reason": "not a verdict"},
    )
    assert resp.status_code == 422


def test_review_requires_a_reason(client, reviewer_headers):
    resp = client.post(
        "/api/v1/audit-samples/sample-002/review",
        headers=reviewer_headers,
        json={"verdict": "AGREED", "reviewer_action": "APPROVE"},
    )
    assert resp.status_code == 422


def test_only_disagreement_emits_the_reason_code(client, admin_headers, reviewer_headers):
    """There are three verdicts, not two. INCONCLUSIVE means the reviewer could
    not tell — that is not the agent being contradicted, so it must not carry
    SAMPLE_REVIEW_DISAGREEMENT. Pinning this because the endpoint branches on
    DISAGREED alone, and a future fourth verdict would silently fall on the
    quiet side of that branch."""
    resp = client.post(
        "/api/v1/audit-samples/sample-002/review",
        headers=reviewer_headers,
        json={
            "verdict": "INCONCLUSIVE",
            "reviewer_action": "ESCALATE",
            "reason": "invoice image was unreadable",
        },
    )
    assert resp.status_code == 200, resp.text

    log = client.get("/api/v1/audit-log", headers=admin_headers).json()
    entry = next(
        e
        for e in log["items"]
        if e["event_type"] == "audit_sample.reviewed" and e["entity_id"] == "sample-002"
    )
    assert entry["payload"]["verdict"] == "INCONCLUSIVE"
    assert entry["payload"]["reason_codes"] == []


def test_the_verdict_enum_has_not_grown_unnoticed():
    """If a verdict is added, the reason-code branch above needs revisiting."""
    assert {v.value for v in ReviewVerdict} == {"AGREED", "DISAGREED", "INCONCLUSIVE"}
