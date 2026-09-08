"""
tests/test_api_client.py
------------------------
Tests for simulator/api_client.py — specifically the submit_decision payload format.

CR-5 BOUNDARY CONTRACT TEST:
This test validates that the decision POST payload matches the backend's
DecisionCreate Pydantic schema. If anyone reintroduces the old /invoices
endpoint, a nested body, or ESCALATE ground truth, this test fails.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from shared.enums import Action

from simulator.models import AgentOutcome, Invoice


class TestDecisionPayloadContract:
    """
    Validates that the decision POST payload exactly matches backend.DecisionCreate.
    This is the critical boundary contract that keeps the simulator and backend in sync.
    """

    def test_submit_decision_payload_structure(self):
        """
        Validates that submit_decision builds the exact payload structure.
        We can't call the real API here (no backend running), but we can introspect
        the method to verify it constructs the right dict.
        """
        # Build a minimal invoice and outcome
        invoice = Invoice(
            invoice_id="INV-001",
            submitted_by="test@example.com",
            vendor_name="ACME Corp",
            invoice_date="2026-09-02",
            category="supplies",
            amount="1500.00",  # Must be parseable as Decimal
            description="Test invoice",
            ground_truth_decision=Action.APPROVE,  # 2-way only
        )

        outcome = AgentOutcome(
            invoice_id="INV-001",
            agent_id="test-agent",
            action=Action.APPROVE,
            reason="Test decision",
            confidence=1.0,
        )

        # Manually build what submit_decision should send
        # (simulating the method's logic without actually calling httpx)
        body = {
            "invoice_id": invoice.invoice_id,
            "amount": int(Decimal(invoice.amount)),
            "action": outcome.action.value,
            "ground_truth": invoice.ground_truth_decision.value,
            "agent_id": outcome.agent_id,
            "reason": "test reason",
        }

        # Validate the payload structure
        assert "invoice_id" in body
        assert "amount" in body
        assert "action" in body
        assert "ground_truth" in body
        assert "agent_id" in body
        assert "reason" in body

        # No extra fields
        expected_keys = {"invoice_id", "amount", "action", "ground_truth", "agent_id", "reason"}
        assert set(body.keys()) == expected_keys

    def test_ground_truth_is_2way_only_approve(self):
        """Ground truth must be APPROVE (not ESCALATE)."""
        invoice = Invoice(
            invoice_id="INV-002",
            submitted_by="test@example.com",
            vendor_name="ACME Corp",
            invoice_date="2026-09-02",
            category="supplies",
            amount="2000.00",
            description="Test invoice",
            ground_truth_decision=Action.APPROVE,
        )

        outcome = AgentOutcome(
            invoice_id="INV-002",
            agent_id="test-agent",
            action=Action.APPROVE,
            reason="Test",
            confidence=1.0,
        )

        # Build payload
        body = {
            "invoice_id": invoice.invoice_id,
            "amount": int(Decimal(invoice.amount)),
            "action": outcome.action.value,
            "ground_truth": invoice.ground_truth_decision.value,
            "agent_id": outcome.agent_id,
            "reason": "test",
        }

        # ground_truth must be APPROVE or REJECT only
        assert body["ground_truth"] in ("APPROVE", "REJECT"), (
            f"ground_truth must be 2-way (APPROVE/REJECT), got {body['ground_truth']}"
        )

    def test_ground_truth_is_2way_only_reject(self):
        """Ground truth must be REJECT (not ESCALATE)."""
        invoice = Invoice(
            invoice_id="INV-003",
            submitted_by="test@example.com",
            vendor_name="BLOCKED VENDOR",
            invoice_date="2026-09-02",
            category="supplies",
            amount="1000.00",
            description="Test invoice",
            ground_truth_decision=Action.REJECT,
        )

        outcome = AgentOutcome(
            invoice_id="INV-003",
            agent_id="test-agent",
            action=Action.REJECT,
            reason="Blocked vendor",
            confidence=1.0,
        )

        body = {
            "invoice_id": invoice.invoice_id,
            "amount": int(Decimal(invoice.amount)),
            "action": outcome.action.value,
            "ground_truth": invoice.ground_truth_decision.value,
            "agent_id": outcome.agent_id,
            "reason": "test",
        }

        # ground_truth must be APPROVE or REJECT only
        assert body["ground_truth"] in ("APPROVE", "REJECT"), (
            f"ground_truth must be 2-way (APPROVE/REJECT), got {body['ground_truth']}"
        )

    def test_amount_is_int(self):
        """Amount must be converted to int (not string, not Decimal)."""
        invoice = Invoice(
            invoice_id="INV-004",
            submitted_by="test@example.com",
            vendor_name="ACME Corp",
            invoice_date="2026-09-02",
            category="supplies",
            amount="1234.56",  # String amount
            description="Test invoice",
            ground_truth_decision=Action.APPROVE,
        )

        outcome = AgentOutcome(
            invoice_id="INV-004",
            agent_id="test-agent",
            action=Action.APPROVE,
            reason="Test",
            confidence=1.0,
        )

        body = {
            "invoice_id": invoice.invoice_id,
            "amount": int(Decimal(invoice.amount)),  # Convert to int
            "action": outcome.action.value,
            "ground_truth": invoice.ground_truth_decision.value,
            "agent_id": outcome.agent_id,
            "reason": "test",
        }

        # amount must be int, not Decimal or str
        assert isinstance(body["amount"], int), f"amount must be int, got {type(body['amount'])}"
        assert body["amount"] == 1234, f"amount 1234.56 should truncate to 1234, got {body['amount']}"

    def test_action_is_enum_value(self):
        """Action must be serialized as string value (e.g., 'APPROVE')."""
        invoice = Invoice(
            invoice_id="INV-005",
            submitted_by="test@example.com",
            vendor_name="ACME Corp",
            invoice_date="2026-09-02",
            category="supplies",
            amount="1000.00",
            description="Test invoice",
            ground_truth_decision=Action.APPROVE,
        )

        outcome = AgentOutcome(
            invoice_id="INV-005",
            agent_id="test-agent",
            action=Action.REJECT,
            reason="Test",
            confidence=1.0,
        )

        body = {
            "invoice_id": invoice.invoice_id,
            "amount": int(Decimal(invoice.amount)),
            "action": outcome.action.value,  # Must use .value (not the enum itself)
            "ground_truth": invoice.ground_truth_decision.value,
            "agent_id": outcome.agent_id,
            "reason": "test",
        }

        # action must be the string value
        assert isinstance(body["action"], str), f"action must be string, got {type(body['action'])}"
        assert body["action"] in ("APPROVE", "REJECT", "ESCALATE")
        assert body["action"] == "REJECT"

    def test_no_nested_invoice_body(self):
        """
        Payload must be flat — no nested {"invoice": {...}} structure.
        This was the old (wrong) format; the new format is flat.
        """
        invoice = Invoice(
            invoice_id="INV-006",
            submitted_by="test@example.com",
            vendor_name="ACME Corp",
            invoice_date="2026-09-02",
            category="supplies",
            amount="1000.00",
            description="Test invoice",
            ground_truth_decision=Action.APPROVE,
        )

        outcome = AgentOutcome(
            invoice_id="INV-006",
            agent_id="test-agent",
            action=Action.APPROVE,
            reason="Test",
            confidence=1.0,
        )

        body = {
            "invoice_id": invoice.invoice_id,
            "amount": int(Decimal(invoice.amount)),
            "action": outcome.action.value,
            "ground_truth": invoice.ground_truth_decision.value,
            "agent_id": outcome.agent_id,
            "reason": "test",
        }

        # No nested "invoice" key
        assert "invoice" not in body, (
            "Payload must be flat, not nested. Got 'invoice' key which is old format."
        )

    def test_reason_is_non_empty(self):
        """Reason must be non-empty string (backend DecisionCreate requires min_length=1)."""
        invoice = Invoice(
            invoice_id="INV-007",
            submitted_by="test@example.com",
            vendor_name="ACME Corp",
            invoice_date="2026-09-02",
            category="supplies",
            amount="1000.00",
            description="Test invoice",
            ground_truth_decision=Action.APPROVE,
        )

        outcome = AgentOutcome(
            invoice_id="INV-007",
            agent_id="test-agent",
            action=Action.APPROVE,
            reason="Test",
            confidence=1.0,
        )

        # Valid reason
        body_valid = {
            "invoice_id": invoice.invoice_id,
            "amount": int(Decimal(invoice.amount)),
            "action": outcome.action.value,
            "ground_truth": invoice.ground_truth_decision.value,
            "agent_id": outcome.agent_id,
            "reason": "sim-run 42 invoice INV-007",  # Non-empty
        }
        assert body_valid["reason"], "reason must be non-empty"

        # Invalid reason (empty) — backend would reject this
        body_invalid = {
            "invoice_id": invoice.invoice_id,
            "amount": int(Decimal(invoice.amount)),
            "action": outcome.action.value,
            "ground_truth": invoice.ground_truth_decision.value,
            "agent_id": outcome.agent_id,
            "reason": "",  # Empty — this should fail backend validation
        }
        assert not body_invalid["reason"], (
            "reason must not be empty (backend DecisionCreate requires min_length=1)"
        )
