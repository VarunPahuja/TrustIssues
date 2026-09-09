"""Query filters the dashboard depends on.

Both of these were already being sent by the frontend and silently discarded
by FastAPI, which is the worst version of this bug: the UI looks wired up, the
request goes out, a 200 comes back, and the list never changes. Nothing fails
loudly enough to notice until someone clicks the tabs in front of an audience.
"""

from __future__ import annotations

import pytest


def _post_decision(client, headers, agent_id: str, i: int) -> str:
    resp = client.post(
        "/api/v1/decisions",
        headers=headers,
        json={
            "invoice_id": f"inv-filter-{agent_id}-{i}",
            "amount": 100,
            "action": "APPROVE",
            "ground_truth": "APPROVE",
            "agent_id": agent_id,
            "reason": "list filter test",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["decision_id"]


# ---------------------------------------------------------------------------
# GET /decisions?agent_id=
# ---------------------------------------------------------------------------


def test_decisions_can_be_filtered_by_agent(client, admin_headers):
    for i in range(3):
        _post_decision(client, admin_headers, "agent-01", i)
        _post_decision(client, admin_headers, "agent-02", i)

    resp = client.get("/api/v1/decisions?agent_id=agent-02", headers=admin_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "agent-02 has decisions"
    assert {d["agent_id"] for d in items} == {"agent-02"}


def test_an_agents_decisions_survive_another_agents_run(client, admin_headers):
    """The bug this fixes: the agent detail page fetched the newest 50
    decisions across every agent and filtered in the browser, so a burst of
    activity on one agent pushed another's off the page entirely and its panel
    rendered empty."""
    for i in range(3):
        _post_decision(client, admin_headers, "agent-01", 100 + i)

    # A much larger burst on a different agent, enough to fill any first page.
    for i in range(60):
        _post_decision(client, admin_headers, "agent-02", 200 + i)

    unfiltered = client.get("/api/v1/decisions?page_size=50", headers=admin_headers).json()
    assert not any(d["agent_id"] == "agent-01" for d in unfiltered["items"]), (
        "precondition: agent-01 really is off the first unfiltered page"
    )

    filtered = client.get("/api/v1/decisions?agent_id=agent-01", headers=admin_headers).json()
    assert filtered["items"], "agent-01's decisions are still reachable"
    assert {d["agent_id"] for d in filtered["items"]} == {"agent-01"}


def test_decisions_without_a_filter_still_returns_every_agent(client, admin_headers):
    _post_decision(client, admin_headers, "agent-01", 300)
    _post_decision(client, admin_headers, "agent-02", 300)
    resp = client.get("/api/v1/decisions?page_size=200", headers=admin_headers)
    assert len({d["agent_id"] for d in resp.json()["items"]}) > 1


def test_an_unknown_agent_filter_returns_an_empty_page_not_an_error(client, admin_headers):
    """An empty result is the honest answer to "this agent has no decisions".
    A 404 would conflate "no such agent" with "nothing to show"."""
    resp = client.get("/api/v1/decisions?agent_id=agent-does-not-exist", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# GET /recommendations?status=
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["PENDING", "APPROVED", "REJECTED"])
def test_recommendations_can_be_filtered_by_status(client, admin_headers, status):
    resp = client.get(f"/api/v1/recommendations?status={status}", headers=admin_headers)
    assert resp.status_code == 200
    assert {r["status"] for r in resp.json()["items"]} <= {status}


def test_the_status_tabs_do_not_all_show_the_same_list(client, admin_headers):
    """The actual reported symptom: all four approvals tabs rendered
    identically, because the parameter was being discarded."""
    everything = client.get("/api/v1/recommendations", headers=admin_headers).json()
    pending = client.get("/api/v1/recommendations?status=PENDING", headers=admin_headers).json()
    approved = client.get("/api/v1/recommendations?status=APPROVED", headers=admin_headers).json()

    assert everything["total"] >= pending["total"] + approved["total"]
    pending_ids = {r["recommendation_id"] for r in pending["items"]}
    approved_ids = {r["recommendation_id"] for r in approved["items"]}
    assert pending_ids.isdisjoint(approved_ids), "a recommendation is in exactly one tab"


def test_an_unknown_status_is_rejected_rather_than_ignored(client, admin_headers):
    """Silently ignoring a bad value is how this bug survived in the first
    place. An enum-typed parameter fails loudly instead."""
    resp = client.get("/api/v1/recommendations?status=BANANA", headers=admin_headers)
    assert resp.status_code == 422


def test_recommendations_can_be_filtered_by_agent(client, admin_headers):
    resp = client.get("/api/v1/recommendations?agent_id=agent-03", headers=admin_headers)
    assert resp.status_code == 200
    assert {r["agent_id"] for r in resp.json()["items"]} <= {"agent-03"}


def test_status_and_agent_filters_combine(client, admin_headers):
    resp = client.get(
        "/api/v1/recommendations?status=PENDING&agent_id=agent-03", headers=admin_headers
    )
    assert resp.status_code == 200
    for r in resp.json()["items"]:
        assert r["status"] == "PENDING"
        assert r["agent_id"] == "agent-03"
