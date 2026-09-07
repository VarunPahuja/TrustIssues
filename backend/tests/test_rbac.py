"""Cross-cutting RBAC verification — every non-GET route under
`backend/app/api/v1/`, checked against all three stub roles, plus a read
sweep confirming AUDITOR is never blocked from a `GET`.

docs/audits/2026-09-06-audit.md's RBAC pass (freeze-cleanup item 2) read
every router by hand rather than assuming, and found two of the six
mutating endpoints had no role check at all: `POST /decisions` and
`POST /agents/{id}/recommendations`. Both are fixed alongside this file
(see `app/api/v1/decisions.py`/`agents.py` for the reasoning on why ADMIN,
specifically, and the flagged ambiguity around "who submits a decision" in
a role model built for three human dashboard roles). This file exists so
the full six-endpoint x three-role matrix is verified in one place, easy to
re-audit as a whole, rather than as one incidental assertion per resource
file.

The full mutating-endpoint role matrix, as implemented:

| Endpoint                                    | ADMIN | REVIEWER | AUDITOR |
|----------------------------------------------|-------|----------|---------|
| POST /decisions                              | yes   | 403      | 403     |
| POST /agents/{id}/recommendations            | yes   | 403      | 403     |
| POST /audit-samples/{id}/review              | yes   | yes      | 403     |
| POST /recommendations/{id}/approve           | yes   | 403      | 403     |
| POST /recommendations/{id}/reject            | yes   | 403      | 403     |
| POST /simulation/runs                        | yes   | 403      | 403     |
"""

from __future__ import annotations

_DECISION_BODY_TEMPLATE = {
    "amount": 100,
    "action": "APPROVE",
    "ground_truth": "APPROVE",
    "agent_id": "agent-01",
    "reason": "rbac check",
}


def _decision_body(invoice_id: str) -> dict:
    return {"invoice_id": invoice_id, **_DECISION_BODY_TEMPLATE}


# --- POST /decisions ---------------------------------------------------------


def test_post_decisions_reviewer_forbidden(client, reviewer_headers):
    resp = client.post(
        "/api/v1/decisions", headers=reviewer_headers, json=_decision_body("inv-rbac-1")
    )
    assert resp.status_code == 403


def test_post_decisions_auditor_forbidden(client, auditor_headers):
    resp = client.post(
        "/api/v1/decisions", headers=auditor_headers, json=_decision_body("inv-rbac-2")
    )
    assert resp.status_code == 403


def test_post_decisions_admin_allowed(client, admin_headers):
    resp = client.post(
        "/api/v1/decisions", headers=admin_headers, json=_decision_body("inv-rbac-3")
    )
    assert resp.status_code == 201


# --- POST /agents/{id}/recommendations ---------------------------------------


def test_post_agent_recommendations_reviewer_forbidden(client, reviewer_headers):
    resp = client.post("/api/v1/agents/agent-02/recommendations", headers=reviewer_headers)
    assert resp.status_code == 403


def test_post_agent_recommendations_auditor_forbidden(client, auditor_headers):
    resp = client.post("/api/v1/agents/agent-02/recommendations", headers=auditor_headers)
    assert resp.status_code == 403


def test_post_agent_recommendations_admin_allowed(client, admin_headers):
    resp = client.post("/api/v1/agents/agent-02/recommendations", headers=admin_headers)
    assert resp.status_code == 201


# --- POST /audit-samples/{id}/review — the one ADMIN-or-REVIEWER route ------


def _review_body() -> dict:
    return {"verdict": "AGREED", "reviewer_action": "APPROVE", "reason": "rbac check"}


def test_post_audit_sample_review_reviewer_allowed(client, reviewer_headers):
    # sample-002 is the fixture's pending one (app/fixtures/audit.py);
    # sample-001/003 are pre-reviewed and would 409, not prove the role check.
    resp = client.post(
        "/api/v1/audit-samples/sample-002/review", headers=reviewer_headers, json=_review_body()
    )
    assert resp.status_code == 200


def test_post_audit_sample_review_auditor_forbidden(client, auditor_headers):
    # Role check runs before the handler body, so an already-reviewed sample
    # is fine here — this must 403 regardless of review state.
    resp = client.post(
        "/api/v1/audit-samples/sample-001/review", headers=auditor_headers, json=_review_body()
    )
    assert resp.status_code == 403


def test_post_audit_sample_review_admin_allowed(client, admin_headers):
    resp = client.post(
        "/api/v1/audit-samples/sample-004/review", headers=admin_headers, json=_review_body()
    )
    assert resp.status_code == 200


# --- POST /recommendations/{id}/approve --------------------------------------


def test_post_recommendation_approve_reviewer_forbidden(client, reviewer_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve",
        headers=reviewer_headers,
        json={"reason": "rbac check"},
    )
    assert resp.status_code == 403


def test_post_recommendation_approve_auditor_forbidden(client, auditor_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve",
        headers=auditor_headers,
        json={"reason": "rbac check"},
    )
    assert resp.status_code == 403


def test_post_recommendation_approve_admin_allowed(client, admin_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/approve",
        headers=admin_headers,
        json={"reason": "rbac check"},
    )
    assert resp.status_code == 200


# --- POST /recommendations/{id}/reject ---------------------------------------


def test_post_recommendation_reject_reviewer_forbidden(client, reviewer_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/reject",
        headers=reviewer_headers,
        json={"reason": "rbac check"},
    )
    assert resp.status_code == 403


def test_post_recommendation_reject_auditor_forbidden(client, auditor_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/reject",
        headers=auditor_headers,
        json={"reason": "rbac check"},
    )
    assert resp.status_code == 403


def test_post_recommendation_reject_admin_allowed(client, admin_headers):
    resp = client.post(
        "/api/v1/recommendations/rec-agent01-001/reject",
        headers=admin_headers,
        json={"reason": "rbac check"},
    )
    assert resp.status_code == 200


# --- POST /simulation/runs ----------------------------------------------------


def _simulation_body() -> dict:
    return {
        "phase": "good",
        "agent_id": "agent-01",
        "invoice_count": 5,
        "seed": 1,
        "reason": "rbac check",
    }


def test_post_simulation_run_reviewer_forbidden(client, reviewer_headers):
    resp = client.post(
        "/api/v1/simulation/runs", headers=reviewer_headers, json=_simulation_body()
    )
    assert resp.status_code == 403


def test_post_simulation_run_auditor_forbidden(client, auditor_headers):
    resp = client.post(
        "/api/v1/simulation/runs", headers=auditor_headers, json=_simulation_body()
    )
    assert resp.status_code == 403


def test_post_simulation_run_admin_allowed(client, admin_headers):
    resp = client.post(
        "/api/v1/simulation/runs", headers=admin_headers, json=_simulation_body()
    )
    assert resp.status_code == 201


# --- every GET route: AUDITOR reads everything, never 403 -------------------

_GET_ROUTES = [
    "/api/v1/agents",
    "/api/v1/agents/agent-01",
    "/api/v1/agents/agent-01/policy-versions",
    "/api/v1/agents/agent-01/trust",
    "/api/v1/agents/agent-01/trust/history",
    "/api/v1/decisions",
    "/api/v1/decisions/dec-agent03-0193",
    "/api/v1/recommendations",
    "/api/v1/recommendations/rec-agent01-001",
    "/api/v1/audit-samples",
    "/api/v1/audit-log",
]


def test_auditor_can_read_every_get_route(client, auditor_headers):
    for path in _GET_ROUTES:
        resp = client.get(path, headers=auditor_headers)
        assert resp.status_code == 200, (
            f"{path} -> {resp.status_code} (AUDITOR should be able to read every route)"
        )
