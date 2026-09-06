"""
simulator/generator.py
-----------------------
Generates synthetic invoices with fully deterministic, seeded randomness.

KEY DESIGN DECISIONS:
  1. Seeded random.Random instance — same seed always produces the same invoices.
     This means fixtures are reproducible and diffs are meaningful.
  2. Amount distribution is log-normal so it matches real expense data
     (most invoices are small; a long tail of large ones).
  3. The labeller is called INSIDE the generator so every Invoice object
     leaves the generator with ground truth already set — the LLM agent then
     decides independently and its answer is compared to that ground truth.
  4. Phase difficulty is controlled entirely through DistributionParams;
     the generator itself has no phase-specific logic.
"""

from __future__ import annotations

import math
import random
import sys
import os
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from uuid import UUID

# Allow shared/ to be imported without installing
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from simulator.constants import (
    AMOUNT_MAX_INR,
    AMOUNT_MIN_INR,
    BLOCKED_VENDORS,
    DEFAULT_SEED,
    REQUIRED_INVOICE_FIELDS,
)
from simulator.models import Invoice, InvoiceCategory, SimulationPhase
from simulator.distributions import DistributionParams
from simulator.labeller import GroundTruthLabeller


# ---------------------------------------------------------------------------
# Vendor name pools
# ---------------------------------------------------------------------------

TRUSTED_VENDORS: list[str] = [
    "Infosys Ltd", "TCS Technologies", "Wipro Services",
    "MakeMyTrip Corporate", "OYO Business Travel", "IRCTC Rail",
    "Amazon Business", "Flipkart Wholesale", "Staples India",
    "Microsoft India", "Adobe Systems India", "Zoho Corporation",
    "Deloitte India", "KPMG Advisory", "Ernst & Young LLP",
    "Blue Dart Express", "FedEx India", "Delhivery Ltd",
    "Taj Hotels Corporate", "ITC Hotels Business",
]

AMBIGUOUS_VENDORS: list[str] = [
    "SR Enterprises", "M/s Global Solutions", "Innovate Corp",
    "Tech Dynamics", "Prime Services", "Allied Industries",
    "NextGen Consulting", "Pinnacle Systems", "Alpha Logistics",
    "Horizon Tech", "Vertex Solutions", "Apex Trading Co",
    "Metro Supplies", "Capital Services", "Blue Ocean Tech",
]

BLOCKED_VENDOR_LIST: list[str] = sorted(BLOCKED_VENDORS)

# ---------------------------------------------------------------------------
# Employee / department pools
# ---------------------------------------------------------------------------

EMPLOYEES: list[str] = [
    f"EMP{i:04d}" for i in range(1001, 1051)
]

DEPARTMENTS: list[str] = [
    "Engineering", "Marketing", "Sales", "HR", "Finance",
    "Operations", "Legal", "Product", "Design", "Research",
]

COST_CENTRES: list[str] = [
    f"CC-{d[:3].upper()}-{n:02d}"
    for d, n in zip(DEPARTMENTS, range(10, 20))
]

PO_PREFIXES: list[str] = ["PO", "PR", "ORD"]

DESCRIPTIONS: dict[str, list[str]] = {
    "travel": [
        "Business travel to client site",
        "Conference attendance",
        "Team offsite travel",
        "Airport transfers and accommodation",
    ],
    "supplies": [
        "Office stationery and consumables",
        "Printer cartridges and paper",
        "Desk accessories and ergonomic equipment",
        "Cleaning supplies for office",
    ],
    "software": [
        "Annual SaaS subscription renewal",
        "Development tool licence",
        "Cloud storage subscription",
        "Security software licence",
    ],
    "consulting": [
        "Strategy consulting engagement",
        "Legal advisory services",
        "HR consulting and recruitment",
        "Financial advisory services",
    ],
    "logistics": [
        "Freight and courier charges",
        "Warehouse storage fees",
        "Last-mile delivery costs",
        "Customs and clearance charges",
    ],
}


# ---------------------------------------------------------------------------
# InvoiceGenerator
# ---------------------------------------------------------------------------

class InvoiceGenerator:
    """
    Creates seeded, reproducible Invoice objects according to DistributionParams.

    Usage:
        params = baseline_params()
        gen = InvoiceGenerator(seed=42, params=params, phase=SimulationPhase.GOOD)
        invoices = gen.generate(100)
    """

    def __init__(
        self,
        seed: int = DEFAULT_SEED,
        params: Optional[DistributionParams] = None,
        phase: SimulationPhase = SimulationPhase.GOOD,
    ) -> None:
        from simulator.distributions import baseline_params
        self.rng = random.Random(seed)
        # Invoice IDs draw from their own seeded stream. Keeping them off self.rng
        # means adding IDs does not shift the content stream, so fixture contents
        # stay byte-identical while the IDs themselves become reproducible.
        self._id_rng = random.Random(seed ^ 0x1D_C0FFEE)
        self.params = params or baseline_params()
        self.phase = phase
        self.labeller = GroundTruthLabeller()
        self._invoice_counter = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, count: int) -> list[Invoice]:
        """Generate `count` Invoice objects with ground truth already labelled."""
        return [self._make_invoice() for _ in range(count)]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_invoice(self) -> Invoice:
        self._invoice_counter += 1

        category = self._pick_category()
        amount_inr = self._pick_amount(category)
        vendor, is_ambiguous, is_blocked = self._pick_vendor()
        invoice_date = self._pick_date()

        # Determine which required fields to drop (degraded phase hardness)
        missing, present = self._apply_missing_fields()

        submitted_by = self.rng.choice(EMPLOYEES)
        department = self.rng.choice(DEPARTMENTS) if "department" not in missing else None
        cost_centre = self.rng.choice(COST_CENTRES) if "cost_centre" not in missing else None
        po_number: Optional[str] = None
        if self.rng.random() < 0.6 and "purchase_order" not in missing:
            prefix = self.rng.choice(PO_PREFIXES)
            po_number = f"{prefix}-{self.rng.randint(10000, 99999)}"

        description = self.rng.choice(DESCRIPTIONS[category.value])

        is_boundary = self._is_boundary_amount(amount_inr, category)

        # Build a partial invoice dict for the labeller
        # (some fields intentionally absent to match missing_field_names)
        invoice_kwargs: dict = dict(
            submitted_by=submitted_by if "submitted_by" not in missing else "MISSING",
            vendor_name=vendor if "vendor_name" not in missing else "MISSING",
            invoice_date=invoice_date if "invoice_date" not in missing else date(1900, 1, 1),
            category=category,
            amount=self._format_amount(amount_inr),
            description=description,
            department=department,
            cost_centre=cost_centre,
            purchase_order=po_number,
            phase=self.phase,
            is_boundary_case=is_boundary,
            is_ambiguous_vendor=is_ambiguous,
            has_missing_fields=bool(missing),
            missing_field_names=sorted(missing),
            # Ground truth will be filled below
            ground_truth_decision=None,  # type: ignore[arg-type]
            ground_truth_reason="",
            ground_truth_confidence=1.0,
        )

        # Temporarily create a bare invoice to pass to labeller
        # We'll set the GT fields right after
        invoice = Invoice.model_construct(**invoice_kwargs)

        gt_decision, gt_reason, gt_confidence = self.labeller.label(invoice)
        invoice.ground_truth_decision = gt_decision
        invoice.ground_truth_reason = gt_reason
        invoice.ground_truth_confidence = gt_confidence

        # Now do a full validation pass
        return Invoice(
            invoice_id=self._next_invoice_id(),
            submitted_by=invoice_kwargs["submitted_by"],
            vendor_name=vendor,           # Always store real vendor name in GT
            invoice_date=invoice_date,    # Always store real date
            category=category,
            amount=invoice_kwargs["amount"],
            description=description,
            department=department,
            cost_centre=cost_centre,
            purchase_order=po_number,
            phase=self.phase,
            is_boundary_case=is_boundary,
            is_ambiguous_vendor=is_ambiguous,
            has_missing_fields=bool(missing),
            missing_field_names=sorted(missing),
            ground_truth_decision=gt_decision,
            ground_truth_reason=gt_reason,
            ground_truth_confidence=gt_confidence,
        )

    def _next_invoice_id(self) -> str:
        """A UUID4-shaped id drawn from the seeded stream, so fixtures reproduce."""
        return str(UUID(int=self._id_rng.getrandbits(128), version=4))

    def _pick_category(self) -> InvoiceCategory:
        weights = self.params.category_weights
        categories = list(weights.keys())
        probs = list(weights.values())
        chosen = self.rng.choices(categories, weights=probs, k=1)[0]
        return InvoiceCategory(chosen)

    def _pick_amount(self, category: InvoiceCategory) -> int:
        """
        Log-normal draw clamped to [AMOUNT_MIN_INR, AMOUNT_MAX_INR].
        In degraded phase, some fraction of amounts are nudged into the
        boundary zone (±boundary_tolerance of the category policy limit).
        """
        # Should this invoice be a boundary case?
        if self.rng.random() < self.params.boundary_fraction:
            limit = 500
            tol = self.params.boundary_tolerance
            # Uniformly sample within ±tol of the limit
            lo = int(limit * (1 - tol))
            hi = int(limit * (1 + tol))
            return max(AMOUNT_MIN_INR, min(AMOUNT_MAX_INR, self.rng.randint(lo, hi)))

        # Normal log-normal draw
        log_val = self.rng.gauss(self.params.amount_mu, self.params.amount_sigma)
        amount = int(math.exp(log_val))
        return max(AMOUNT_MIN_INR, min(AMOUNT_MAX_INR, amount))

    def _pick_vendor(self) -> tuple[str, bool, bool]:
        """
        Returns (vendor_name, is_ambiguous, is_blocked).
        """
        roll = self.rng.random()

        if roll < self.params.blocked_vendor_prob:
            return self.rng.choice(BLOCKED_VENDOR_LIST), False, True

        if roll < self.params.blocked_vendor_prob + self.params.ambiguous_vendor_prob:
            return self.rng.choice(AMBIGUOUS_VENDORS), True, False

        return self.rng.choice(TRUSTED_VENDORS), False, False

    def _pick_date(self) -> date:
        """Pick a date within the past 90 days."""
        today = date.today()
        days_back = self.rng.randint(0, 90)
        return today - timedelta(days=days_back)

    def _apply_missing_fields(self) -> tuple[set[str], set[str]]:
        """
        Decide which required fields to omit.
        Returns (missing_field_names, present_field_names).
        Never omits 'amount' or 'category' (they're required for labelling).
        """
        droppable = [f for f in REQUIRED_INVOICE_FIELDS if f not in ("amount", "category")]
        missing: set[str] = set()

        if self.rng.random() < self.params.missing_field_prob:
            # Drop 1 or 2 fields
            n_drop = self.rng.randint(1, min(2, len(droppable)))
            missing = set(self.rng.sample(droppable, n_drop))

        present = set(REQUIRED_INVOICE_FIELDS) - missing
        return missing, present

    def _is_boundary_amount(self, amount: int, category: InvoiceCategory) -> bool:
        """Check if the amount falls within boundary_tolerance of the LOW tier limit."""
        limit = 500
        tol = self.params.boundary_tolerance
        return abs(amount - limit) / limit <= tol

    @staticmethod
    def _format_amount(amount_inr: int) -> str:
        """Return amount as a string with 2 decimal places (no float involved)."""
        d = Decimal(amount_inr).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return str(d)
