"""`POST /api/v1/decisions/{id}/ruling` — the write path for `human_ruling`.

`human_agreement` is one of the four components of the trust score, and until
this endpoint existed nothing could populate it outside seed data: `POST
/decisions` hardcoded both `recommended_action` and `human_ruling` to null.
These tests pin the two halves of the evidence
(`shared.contracts.DecisionRecord.has_human_ruling` needs an escalation, a
recommendation and a ruling) and the rules that keep the pair meaningful.
"""

from __future__ import annotations

import pytest


def _escalate(client, headers, invoice_id: str, recommended: str | None = "APPROVE") -> str:
    """Ingest one ESCALATE decision and return its id."""
    body = {
        "invoice_id": invoice_id,
        "amount": 4000,
        "action": "ESCALATE",
        "ground_truth": "APPROVE",
        "agent_id": "agent-01",
        "reason": "above the agent's limit",
    }
    if recommended is not None:
        body["recommended_action"] = recommended
    resp = client.post("/api/v1/decisions", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["decision_id"]


# ---------------------------------------------------------------------------
# Ingest now carries the agent's recommendation
# ---------------------------------------------------------------------------


def test_ingest_persists_recommended_action(client, admin_headers):
    decision_id = _escalate(client, admin_headers, "inv-rule-001", recommended="APPROVE")
    resp = client.get(f"/api/v1/decisions/{decision_id}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["recommended_action"] == "APPROVE"


def test_ingest_defaults_recommended_action_to_null(client, admin_headers):
    decision_id = _escalate(client, admin_headers, "inv-rule-002", recommended=None)
    resp = client.get(f"/api/v1/decisions/{decision_id}", headers=admin_headers)
    assert resp.json()["recommended_action"] is None


def test_ingest_rejects_a_recommendation_to_escalate(client, admin_headers):
    resp = client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-rule-003",
            "amount": 4000,
            "action": "ESCALATE",
            "ground_truth": "APPROVE",
            "agent_id": "agent-01",
            "recommended_action": "ESCALATE",
            "reason": "nonsense recommendation",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_recommended_action"


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_reviewer_can_rule_on_an_escalation(client, admin_headers, reviewer_headers):
    decision_id = _escalate(client, admin_headers, "inv-rule-010")
    resp = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "APPROVE", "reason": "vendor is on the trusted list"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["human_ruling"] == "APPROVE"
    assert body["recommended_action"] == "APPROVE"
    assert body["action"] == "ESCALATE"


def test_admin_can_rule_too(client, admin_headers):
    decision_id = _escalate(client, admin_headers, "inv-rule-011")
    resp = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=admin_headers,
        json={"ruling": "REJECT", "reason": "duplicate invoice"},
    )
    assert resp.status_code == 200
    assert resp.json()["human_ruling"] == "REJECT"


def test_a_ruling_survives_a_reread(client, admin_headers, reviewer_headers):
    """The ruling is persisted, not just echoed back in the response."""
    decision_id = _escalate(client, admin_headers, "inv-rule-012")
    client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "REJECT", "reason": "over budget"},
    )
    resp = client.get(f"/api/v1/decisions/{decision_id}", headers=admin_headers)
    assert resp.json()["human_ruling"] == "REJECT"


def test_disagreement_is_recorded_as_faithfully_as_agreement(
    client, admin_headers, reviewer_headers
):
    """A human overruling the agent is the evidence human_agreement exists to
    capture — it must persist exactly as an agreement does."""
    decision_id = _escalate(client, admin_headers, "inv-rule-013", recommended="APPROVE")
    resp = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "REJECT", "reason": "the agent missed the blocked vendor"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["recommended_action"] == "APPROVE"
    assert body["human_ruling"] == "REJECT"


# ---------------------------------------------------------------------------
# The rules that keep a ruling meaningful
# ---------------------------------------------------------------------------


def test_cannot_rule_on_a_decision_the_agent_acted_on(client, admin_headers, reviewer_headers):
    resp = client.post(
        "/api/v1/decisions",
        headers=admin_headers,
        json={
            "invoice_id": "inv-rule-020",
            "amount": 300,
            "action": "APPROVE",
            "ground_truth": "APPROVE",
            "agent_id": "agent-01",
            "reason": "within limit",
        },
    )
    decision_id = resp.json()["decision_id"]

    resp = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "APPROVE", "reason": "second-guessing an acted decision"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "decision_not_escalated"


def test_cannot_rule_escalate(client, admin_headers, reviewer_headers):
    decision_id = _escalate(client, admin_headers, "inv-rule-021")
    resp = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "ESCALATE", "reason": "passing it back"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_ruling"


def test_ruling_twice_is_a_conflict(client, admin_headers, reviewer_headers):
    """A ruling is evidence the trust engine may already have scored. A second
    one is refused rather than silently overwriting the first."""
    decision_id = _escalate(client, admin_headers, "inv-rule-022")
    first = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "APPROVE", "reason": "looks fine"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "REJECT", "reason": "changed my mind"},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "decision_already_ruled"

    # The first ruling stands.
    resp = client.get(f"/api/v1/decisions/{decision_id}", headers=admin_headers)
    assert resp.json()["human_ruling"] == "APPROVE"


def test_ruling_requires_a_reason(client, admin_headers, reviewer_headers):
    decision_id = _escalate(client, admin_headers, "inv-rule-023")
    resp = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "APPROVE"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


def test_ruling_an_unknown_decision_is_404(client, reviewer_headers):
    resp = client.post(
        "/api/v1/decisions/does-not-exist/ruling",
        headers=reviewer_headers,
        json={"ruling": "APPROVE", "reason": "nothing to rule on"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "decision_not_found"


def test_a_ruling_without_a_recommendation_is_allowed_but_cannot_agree(
    client, admin_headers, reviewer_headers
):
    """The ruling is a real fact worth recording. It just cannot feed human
    agreement, because there is no recommendation to compare it against —
    `has_human_ruling` needs both halves."""
    decision_id = _escalate(client, admin_headers, "inv-rule-024", recommended=None)
    resp = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "APPROVE", "reason": "fine, though the agent said nothing"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["human_ruling"] == "APPROVE"
    assert body["recommended_action"] is None


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


def test_auditor_cannot_rule(client, admin_headers, auditor_headers):
    """AUDITOR is read-only everywhere, including here."""
    decision_id = _escalate(client, admin_headers, "inv-rule-030")
    resp = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=auditor_headers,
        json={"ruling": "APPROVE", "reason": "not my call to make"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recommended", "ruling", "expected_agreed"),
    [("APPROVE", "APPROVE", True), ("APPROVE", "REJECT", False)],
)
def test_ruling_appends_an_audit_entry(
    client, admin_headers, reviewer_headers, recommended, ruling, expected_agreed
):
    invoice_id = f"inv-rule-audit-{recommended}-{ruling}"
    decision_id = _escalate(client, admin_headers, invoice_id, recommended=recommended)
    resp = client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": ruling, "reason": "recorded for the chain"},
    )
    assert resp.status_code == 200

    log = client.get("/api/v1/audit-log", headers=admin_headers).json()
    entries = [
        e
        for e in log["items"]
        if e["event_type"] == "decision.ruled" and e["entity_id"] == decision_id
    ]
    assert len(entries) == 1, "exactly one audit entry per ruling"
    payload = entries[0]["payload"]
    assert payload["human_ruling"] == ruling
    assert payload["recommended_action"] == recommended
    assert payload["agreed"] is expected_agreed
    assert payload["reason"] == "recorded for the chain"


def test_the_audit_chain_still_verifies_after_a_ruling(client, admin_headers, reviewer_headers):
    decision_id = _escalate(client, admin_headers, "inv-rule-040")
    client.post(
        f"/api/v1/decisions/{decision_id}/ruling",
        headers=reviewer_headers,
        json={"ruling": "APPROVE", "reason": "chain integrity check"},
    )
    log = client.get("/api/v1/audit-log", headers=admin_headers).json()
    assert log["chain_valid"] is True
