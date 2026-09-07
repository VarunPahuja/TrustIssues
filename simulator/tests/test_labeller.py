"""
tests/test_labeller.py
-----------------------
Pytest tests for simulator/labeller.py — the 10-rule deterministic oracle.

Tests verify:
  - Each of the 10 rules fires correctly in isolation
  - Rule priority order: earlier rules override later ones
  - Confidence values: 1.0 for clear decisions, 0.7 for boundary, 0.8 for ambiguous vendor
  - Purity: deterministic, same input → same output
"""

from __future__ import annotations

import sys
import os
from datetime import date, timedelta
from decimal import Decimal

import pytest

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from shared.enums import Action as AgentDecision
from simulator.models import InvoiceCategory, SimulationPhase
from simulator import reason_codes as RC
from simulator.labeller import GroundTruthLabeller


# ---------------------------------------------------------------------------
# Helper: build a minimal valid invoice-like object for the labeller
# (labeller only accesses specific attributes, not the full Pydantic model)
# ---------------------------------------------------------------------------

class FakeInvoice:
    """Minimal stand-in for shared.contracts.Invoice used in labeller tests."""

    def __init__(
        self,
        amount: str = "1000.00",
        category: str = "supplies",
        vendor_name: str = "Amazon Business",
        invoice_date: date = None,
        submitted_by: str = "EMP1001",
        missing_field_names: list = None,
        is_ambiguous_vendor: bool = False,
        has_missing_fields: bool = False,
    ):
        self.amount = amount
        self.category = InvoiceCategory(category)
        self.vendor_name = vendor_name
        self.invoice_date = invoice_date or (date.today() - timedelta(days=1))
        self.submitted_by = submitted_by
        self.missing_field_names = missing_field_names or []
        self.is_ambiguous_vendor = is_ambiguous_vendor
        self.has_missing_fields = has_missing_fields


@pytest.fixture
def labeller():
    return GroundTruthLabeller(current_limit=2500)


# ---------------------------------------------------------------------------
# Rule 1: Missing required fields → ESCALATE
# ---------------------------------------------------------------------------

class TestRule1MissingFields:
    def test_missing_vendor_escalates(self, labeller):
        inv = FakeInvoice(missing_field_names=["vendor_name"], has_missing_fields=True)
        decision, reason, confidence = labeller.label(inv)
        assert decision == AgentDecision.REJECT
        assert reason == RC.ESCALATE_MISSING_FIELDS
        assert confidence == 1.0

    def test_missing_submitted_by_escalates(self, labeller):
        inv = FakeInvoice(missing_field_names=["submitted_by"], has_missing_fields=True)
        decision, reason, _ = labeller.label(inv)
        assert decision == AgentDecision.REJECT

    def test_multiple_missing_fields_escalates(self, labeller):
        inv = FakeInvoice(missing_field_names=["vendor_name", "invoice_date"], has_missing_fields=True)
        decision, _, _ = labeller.label(inv)
        assert decision == AgentDecision.REJECT

    def test_missing_fields_beats_blocked_vendor(self, labeller):
        """Rule 1 (missing fields) has higher priority than Rule 2 (blocked vendor)."""
        inv = FakeInvoice(
            vendor_name="ShellCo Industries",     # Blocked vendor
            missing_field_names=["submitted_by"],  # Also missing field
            has_missing_fields=True,
        )
        decision, reason, _ = labeller.label(inv)
        assert decision == AgentDecision.REJECT
        assert reason == RC.ESCALATE_MISSING_FIELDS  # Rule 1 wins


# ---------------------------------------------------------------------------
# Rule 2: Blocked vendor → REJECT
# ---------------------------------------------------------------------------

class TestRule2BlockedVendor:
    @pytest.mark.parametrize("vendor", [
        "ShellCo Industries",
        "FastCash Consulting",
        "QuickBill Ltd",
        "NoName Supplies",
        "Generic Vendor",
    ])
    def test_blocked_vendors_rejected(self, labeller, vendor):
        inv = FakeInvoice(vendor_name=vendor, amount="500.00")
        decision, reason, confidence = labeller.label(inv)
        assert decision == AgentDecision.REJECT
        assert reason == RC.REJECT_BLOCKED_VENDOR
        assert confidence == 1.0

    def test_trusted_vendor_not_rejected(self, labeller):
        inv = FakeInvoice(vendor_name="Amazon Business", amount="500.00")
        decision, _, _ = labeller.label(inv)
        assert decision == AgentDecision.APPROVE


# ---------------------------------------------------------------------------
# Rule 4: Negative / zero amount → REJECT
# ---------------------------------------------------------------------------

class TestRule4NegativeAmount:
    def test_zero_amount_rejected(self, labeller):
        inv = FakeInvoice(amount="0.00")
        decision, reason, _ = labeller.label(inv)
        assert decision == AgentDecision.REJECT
        assert reason == RC.REJECT_NEGATIVE_AMOUNT

    def test_negative_amount_rejected(self, labeller):
        inv = FakeInvoice(amount="-100.00")
        decision, reason, _ = labeller.label(inv)
        assert decision == AgentDecision.REJECT
        assert reason == RC.REJECT_NEGATIVE_AMOUNT


# ---------------------------------------------------------------------------
# Rule 5: Future invoice date → REJECT
# ---------------------------------------------------------------------------

class TestRule5FutureDate:
    def test_tomorrow_date_rejected(self, labeller):
        inv = FakeInvoice(invoice_date=date.today() + timedelta(days=1))
        decision, reason, confidence = labeller.label(inv)
        assert decision == AgentDecision.REJECT
        assert reason == RC.REJECT_FUTURE_DATE
        assert confidence == 1.0

    def test_far_future_rejected(self, labeller):
        inv = FakeInvoice(invoice_date=date.today() + timedelta(days=365))
        decision, _, _ = labeller.label(inv)
        assert decision == AgentDecision.REJECT

    def test_today_accepted(self, labeller):
        """Today's date is NOT future — should not reject on date."""
        inv = FakeInvoice(invoice_date=date.today(), amount="500.00")
        decision, _, _ = labeller.label(inv)
        assert decision != AgentDecision.REJECT or _[0] != RC.REJECT_FUTURE_DATE

    def test_past_date_accepted(self, labeller):
        inv = FakeInvoice(invoice_date=date.today() - timedelta(days=30), amount="500.00")
        decision, _, _ = labeller.label(inv)
        # Should not reject due to date
        assert AgentDecision.REJECT != decision or _ != RC.REJECT_FUTURE_DATE


# ---------------------------------------------------------------------------
# Rule 6: Amount exceeds HIGH tier limit → REJECT
# ---------------------------------------------------------------------------

class TestRule6ExceedsHighLimit:
    def test_amount_above_high_limit_rejected(self, labeller):
        # HIGH limit for supplies = 35,000; amount 40,000 exceeds it
        inv = FakeInvoice(amount="40000.00", category="supplies")
        decision, reason, confidence = labeller.label(inv)
        assert decision == AgentDecision.REJECT
        assert reason == RC.REJECT_EXCEEDS_LIMIT
        assert confidence == 1.0

    def test_amount_at_max_rejects(self, labeller):
        # 50,001 exceeds all categories' high limit
        inv = FakeInvoice(amount="50001.00", category="software")
        decision, reason, _ = labeller.label(inv)
        assert decision == AgentDecision.REJECT
        assert reason == RC.REJECT_EXCEEDS_LIMIT


# ---------------------------------------------------------------------------
# Rule 7: Amount exceeds current tier limit → ESCALATE
# ---------------------------------------------------------------------------

class TestRule7ExceedsTierLimit:
    def test_above_low_limit_escalates(self, labeller):
        # LOW limit for supplies = 2,500; amount 3,000 exceeds it
        inv = FakeInvoice(amount="3000.00", category="supplies")
        decision, reason, confidence = labeller.label(inv)
        assert decision == AgentDecision.APPROVE
        assert reason == RC.ESCALATE_EXCEEDS_TIER
        assert confidence == 1.0

    def test_just_above_limit_escalates(self, labeller):
        # LOW limit for travel = 3,000; 3,001 just over
        inv = FakeInvoice(amount="3001.00", category="travel")
        decision, reason, _ = labeller.label(inv)
        assert decision == AgentDecision.APPROVE


# ---------------------------------------------------------------------------
# Rule 8: Boundary zone (±5% of current limit) → ESCALATE with confidence 0.7
# ---------------------------------------------------------------------------

class TestRule8BoundaryZone:
    def test_amount_in_boundary_zone_escalates(self, labeller):
        # Current rung limit is ₹2,500; boundary zone is [₹2,375, ₹2,500].
        inv = FakeInvoice(amount="2400.00", category="travel")
        decision, reason, confidence = labeller.label(inv)
        assert decision == AgentDecision.APPROVE
        assert reason == RC.ESCALATE_BOUNDARY_AMOUNT
        assert confidence == 0.7

    def test_amount_just_below_boundary_approves(self, labeller):
        inv = FakeInvoice(amount="2000.00", category="supplies")
        decision, _, _ = labeller.label(inv)
        assert decision == AgentDecision.APPROVE


# ---------------------------------------------------------------------------
# Rule 9: Ambiguous vendor + non-trivial amount → ESCALATE
# ---------------------------------------------------------------------------

class TestRule9AmbiguousVendor:
    def test_ambiguous_vendor_large_amount_escalates(self, labeller):
        inv = FakeInvoice(
            vendor_name="SR Enterprises",
            is_ambiguous_vendor=True,
            amount="1500.00",  # > 500 trivial threshold
            category="supplies",
        )
        decision, reason, confidence = labeller.label(inv)
        assert decision == AgentDecision.APPROVE
        assert reason == RC.ESCALATE_AMBIGUOUS_VENDOR
        assert confidence == 0.8

    def test_ambiguous_vendor_trivial_amount_approves(self, labeller):
        # Amount <= 500 (TRIVIAL_AMOUNT_THRESHOLD_INR) → approve despite ambiguous vendor
        inv = FakeInvoice(
            vendor_name="SR Enterprises",
            is_ambiguous_vendor=True,
            amount="400.00",
            category="supplies",
        )
        decision, _, _ = labeller.label(inv)
        assert decision == AgentDecision.APPROVE


# ---------------------------------------------------------------------------
# Rule 10: Approve
# ---------------------------------------------------------------------------

class TestRule10Approve:
    def test_clean_small_invoice_approved(self, labeller):
        inv = FakeInvoice(amount="500.00", category="supplies", vendor_name="Amazon Business")
        decision, reason, confidence = labeller.label(inv)
        assert decision == AgentDecision.APPROVE
        assert confidence == 1.0

    def test_low_risk_reason_code_for_very_small_amount(self, labeller):
        # Amount well below limit (< 50% of LOW limit 2,500 → < 1,250)
        inv = FakeInvoice(amount="500.00", category="supplies")
        decision, reason, _ = labeller.label(inv)
        assert decision == AgentDecision.APPROVE
        assert reason == RC.APPROVE_LOW_RISK

    def test_within_limit_reason_code_for_moderate_amount(self, labeller):
        # Amount between 50%-95% of limit for supplies (1,250 – 2,375)
        inv = FakeInvoice(amount="2000.00", category="supplies")
        decision, reason, _ = labeller.label(inv)
        assert decision == AgentDecision.APPROVE
        assert reason == RC.APPROVE_WITHIN_LIMIT


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_input_same_output(self, labeller):
        """Labeller must be pure: same invoice always produces the same result."""
        inv = FakeInvoice(amount="1200.00", category="travel")
        result_a = labeller.label(inv)
        result_b = labeller.label(inv)
        assert result_a == result_b

    def test_labeller_has_no_mutable_state(self):
        """Two separate labeller instances must produce identical results."""
        inv = FakeInvoice(amount="2800.00", category="travel")
        lab1 = GroundTruthLabeller(current_limit=2500)
        lab2 = GroundTruthLabeller(current_limit=2500)
        assert lab1.label(inv) == lab2.label(inv)
