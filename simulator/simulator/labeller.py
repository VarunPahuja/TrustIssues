"""
simulator/labeller.py
----------------------
Deterministic ground-truth labeller.

THIS IS THE ORACLE.
The labeller is PURE PYTHON — no LLM, no randomness, no network calls.
Given an Invoice, it returns the one correct answer that a perfect policy
engine would give.  The LLM agent then tries to reach the same answer
independently; mismatches are real errors used by the trust engine.

RULE PRIORITY ORDER:
  1. Missing required fields               → ESCALATE (escalate_missing_fields)
  2. Blocked vendor                        → REJECT   (reject_blocked_vendor)
  3. Invalid category                      → REJECT   (reject_invalid_category)
  4. Negative / zero amount                → REJECT   (reject_negative_amount)
  5. Future invoice date                   → REJECT   (reject_future_date)
  6. Amount exceeds HIGH tier limit        → REJECT   (reject_exceeds_limit)
  7. Amount exceeds current tier limit     → ESCALATE (escalate_exceeds_tier)
  8. Amount in boundary zone (±5 %)       → ESCALATE (escalate_boundary_amount)
  9. Ambiguous vendor + non-trivial amount → ESCALATE (escalate_ambiguous_vendor)
 10. Everything else                       → APPROVE  (approve_within_limit)
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from shared.constants import AUTONOMY_FLOOR, AUTONOMY_LADDER
from shared.enums import Action

from simulator import reason_codes as RC
from simulator.constants import BLOCKED_VENDORS
from simulator.models import InvoiceCategory

# Threshold below which an ambiguous vendor is always approved (tiny amounts are fine)
TRIVIAL_AMOUNT_THRESHOLD_INR = 500


class GroundTruthLabeller:
    """
    Pure rule-based oracle.  No state, no LLM.

    label(invoice) → (AgentDecision, reason_code: str, confidence: float)

    confidence is 1.0 for clear-cut decisions and 0.7 for boundary cases
    (boundary cases are genuinely ambiguous even under the rules).
    """

    def __init__(self, current_limit: int = AUTONOMY_FLOOR) -> None:
        """
        autonomy_tier controls which limit table is used.
        In simulation, agents start at LOW; the trust engine may upgrade them.
        """
        self.current_limit = current_limit

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def label(
        self, invoice  # type: Invoice — avoid circular import at module level
    ) -> tuple[Action, str, float]:
        """
        Apply rules in priority order and return (decision, reason_code, confidence).
        """
        # Rule 1: Missing required fields → REJECT
        # (can't process an incomplete invoice; non-material fields are callee's choice)
        if invoice.missing_field_names:
            return Action.REJECT, RC.ESCALATE_MISSING_FIELDS, 1.0

        # Rule 2: Blocked vendor
        if invoice.vendor_name in BLOCKED_VENDORS:
            return Action.REJECT, RC.REJECT_BLOCKED_VENDOR, 1.0

        # Rule 3: Invalid category (shouldn't happen with a typed enum but belt-and-suspenders)
        try:
            InvoiceCategory(invoice.category)
        except ValueError:
            return Action.REJECT, RC.REJECT_INVALID_CATEGORY, 1.0

        # Rule 4: Parse and validate amount
        try:
            amount = Decimal(invoice.amount)
        except InvalidOperation:
            return Action.REJECT, RC.REJECT_NEGATIVE_AMOUNT, 1.0
        if amount <= 0:
            return Action.REJECT, RC.REJECT_NEGATIVE_AMOUNT, 1.0

        # Rule 5: Future date
        invoice_date = invoice.invoice_date
        if isinstance(invoice_date, str):
            invoice_date = date.fromisoformat(invoice_date)
        if invoice_date > date.today():
            return Action.REJECT, RC.REJECT_FUTURE_DATE, 1.0

        # Rule 6: Amount exceeds HIGH tier limit (globally too large)
        high_limit = AUTONOMY_LADDER[-1]
        if amount > high_limit:
            return Action.REJECT, RC.REJECT_EXCEEDS_LIMIT, 1.0

        # Rule 7: Amount exceeds current tier limit → APPROVE
        # (tier limits are system constraints, not invoice defects; resolve to underlying truth)
        current_limit = self.current_limit
        if amount > current_limit:
            return Action.APPROVE, RC.ESCALATE_EXCEEDS_TIER, 1.0

        # Rule 8: Boundary zone (±5 % of current limit) → APPROVE
        # (boundary zones are system constraints, not invoice defects; resolve to underlying truth)
        tol = 0.05
        lower_bound = current_limit * (1 - tol)
        if lower_bound <= float(amount) <= current_limit:
            # Amount is in the zone [limit * 0.95, limit] — approve, confidence 0.7
            return Action.APPROVE, RC.ESCALATE_BOUNDARY_AMOUNT, 0.7

        # Rule 9: Ambiguous vendor + non-trivial amount → APPROVE
        # (ambiguous but not blocked; resolve to underlying truth)
        if invoice.is_ambiguous_vendor and float(amount) > TRIVIAL_AMOUNT_THRESHOLD_INR:
            return Action.APPROVE, RC.ESCALATE_AMBIGUOUS_VENDOR, 0.8

        # Rule 10: Approve
        if float(amount) <= current_limit * 0.5:
            # Well below limit — low risk
            return Action.APPROVE, RC.APPROVE_LOW_RISK, 1.0

        return Action.APPROVE, RC.APPROVE_WITHIN_LIMIT, 1.0
