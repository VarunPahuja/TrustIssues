"""
tests/test_generator.py
------------------------
Pytest tests for simulator/generator.py.

Tests cover:
  - Reproducibility: same seed → same invoices
  - Count: generator returns exactly the requested number
  - Schema validity: all Invoice fields are populated and type-correct
  - Amount bounds: all amounts within [AMOUNT_MIN_INR, AMOUNT_MAX_INR]
  - Amount is string with 2 decimal places (never a float)
  - Ground truth always set (no None values)
  - Phase field matches the phase passed to the generator
  - Baseline distribution: low boundary fraction, low missing-field rate
  - Degraded distribution: higher boundary fraction, higher missing-field rate
  - Seed isolation: different seeds → different invoices
"""

from __future__ import annotations

import sys
import os
from decimal import Decimal, InvalidOperation

import pytest

# Allow shared/ and simulator/ to be imported from repo root
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from simulator.constants import AMOUNT_MIN_INR, AMOUNT_MAX_INR, DEFAULT_SEED
from shared.enums import Action
from simulator.models import SimulationPhase
from simulator.distributions import baseline_params, shifted_params, recovery_params
from simulator.generator import InvoiceGenerator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def baseline_gen() -> InvoiceGenerator:
    return InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params(), phase=SimulationPhase.GOOD)


@pytest.fixture
def degraded_gen() -> InvoiceGenerator:
    return InvoiceGenerator(seed=DEFAULT_SEED, params=shifted_params(), phase=SimulationPhase.DEGRADED)


@pytest.fixture
def recovery_gen() -> InvoiceGenerator:
    return InvoiceGenerator(seed=DEFAULT_SEED, params=recovery_params(), phase=SimulationPhase.RECOVERY)


@pytest.fixture
def baseline_invoices(baseline_gen):
    return baseline_gen.generate(100)


@pytest.fixture
def degraded_invoices(degraded_gen):
    return degraded_gen.generate(100)


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

class TestReproducibility:
    def test_same_seed_same_amounts(self):
        """Same seed must produce identical amount strings (amounts ARE seeded)."""
        gen_a = InvoiceGenerator(seed=42, params=baseline_params())
        gen_b = InvoiceGenerator(seed=42, params=baseline_params())
        amounts_a = [inv.amount for inv in gen_a.generate(50)]
        amounts_b = [inv.amount for inv in gen_b.generate(50)]
        assert amounts_a == amounts_b

    def test_same_seed_same_categories(self):
        """Same seed must produce identical category sequences."""
        gen_a = InvoiceGenerator(seed=99, params=baseline_params())
        gen_b = InvoiceGenerator(seed=99, params=baseline_params())
        cats_a = [inv.category for inv in gen_a.generate(50)]
        cats_b = [inv.category for inv in gen_b.generate(50)]
        assert cats_a == cats_b

    def test_same_seed_same_vendors(self):
        """Same seed must produce identical vendor sequences."""
        gen_a = InvoiceGenerator(seed=7, params=baseline_params())
        gen_b = InvoiceGenerator(seed=7, params=baseline_params())
        vendors_a = [inv.vendor_name for inv in gen_a.generate(50)]
        vendors_b = [inv.vendor_name for inv in gen_b.generate(50)]
        assert vendors_a == vendors_b

    def test_different_seeds_produce_different_amounts(self):
        """Different seeds should produce different amount sequences."""
        gen_a = InvoiceGenerator(seed=1, params=baseline_params())
        gen_b = InvoiceGenerator(seed=2, params=baseline_params())
        amounts_a = [inv.amount for inv in gen_a.generate(50)]
        amounts_b = [inv.amount for inv in gen_b.generate(50)]
        # Extremely unlikely to produce the same amounts with different seeds
        assert amounts_a != amounts_b

    def test_invoice_ids_are_uuid4_not_seeded(self):
        """
        Invoice IDs are uuid4 — intentionally NOT seeded.
        Each run produces fresh IDs to avoid collisions across runs.
        Verify uniqueness within a single batch.
        """
        gen = InvoiceGenerator(seed=42, params=baseline_params())
        invoices = gen.generate(50)
        ids = [inv.invoice_id for inv in invoices]
        assert len(ids) == len(set(ids)), "Invoice IDs must be unique within a batch"


# ---------------------------------------------------------------------------
# Count
# ---------------------------------------------------------------------------

class TestCount:
    @pytest.mark.parametrize("n", [1, 10, 50, 200])
    def test_generates_correct_count(self, n):
        gen = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params())
        invoices = gen.generate(n)
        assert len(invoices) == n

    def test_generate_zero(self):
        gen = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params())
        assert gen.generate(0) == []


# ---------------------------------------------------------------------------
# Schema validity
# ---------------------------------------------------------------------------

class TestSchemaValidity:
    def test_all_required_fields_present(self, baseline_invoices):
        """Every invoice must have non-None values for core fields."""
        for inv in baseline_invoices:
            assert inv.invoice_id is not None and inv.invoice_id != ""
            assert inv.submitted_by is not None
            assert inv.vendor_name is not None and inv.vendor_name != ""
            assert inv.invoice_date is not None
            assert inv.category is not None
            assert inv.amount is not None

    def test_ground_truth_always_set(self, baseline_invoices):
        """ground_truth_decision must never be None."""
        for inv in baseline_invoices:
            assert inv.ground_truth_decision is not None
            assert isinstance(inv.ground_truth_decision, Action)

    def test_ground_truth_reason_non_empty(self, baseline_invoices):
        """ground_truth_reason must be a non-empty string."""
        for inv in baseline_invoices:
            assert isinstance(inv.ground_truth_reason, str)
            assert len(inv.ground_truth_reason) > 0

    def test_ground_truth_confidence_in_range(self, baseline_invoices):
        """ground_truth_confidence must be in [0.0, 1.0]."""
        for inv in baseline_invoices:
            assert 0.0 <= inv.ground_truth_confidence <= 1.0

    def test_phase_field_matches_constructor(self):
        """Invoice.phase must match the phase the generator was initialised with."""
        for phase in [SimulationPhase.GOOD, SimulationPhase.DEGRADED, SimulationPhase.RECOVERY]:
            gen = InvoiceGenerator(seed=DEFAULT_SEED, phase=phase)
            invoices = gen.generate(20)
            for inv in invoices:
                assert inv.phase == phase

    def test_invoice_ids_are_unique(self, baseline_invoices):
        """All invoice IDs within a single run must be unique."""
        ids = [inv.invoice_id for inv in baseline_invoices]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Amount validity
# ---------------------------------------------------------------------------

class TestAmounts:
    def test_amount_is_string(self, baseline_invoices):
        """Amount must be a string (contract requirement — never float)."""
        for inv in baseline_invoices:
            assert isinstance(inv.amount, str), f"Amount must be str, got {type(inv.amount)}"

    def test_amount_has_two_decimal_places(self, baseline_invoices):
        """Amount string must parse to a Decimal with 2dp."""
        for inv in baseline_invoices:
            try:
                d = Decimal(inv.amount)
            except InvalidOperation:
                pytest.fail(f"Amount {inv.amount!r} is not a valid decimal")
            # Check exactly 2 decimal places
            assert "." in inv.amount
            assert len(inv.amount.split(".")[1]) == 2

    def test_amounts_within_bounds(self, baseline_invoices):
        """All amounts must be within [AMOUNT_MIN_INR, AMOUNT_MAX_INR]."""
        for inv in baseline_invoices:
            amount = int(Decimal(inv.amount))
            assert AMOUNT_MIN_INR <= amount <= AMOUNT_MAX_INR, (
                f"Amount {amount} out of [{AMOUNT_MIN_INR}, {AMOUNT_MAX_INR}]"
            )

    def test_amounts_positive(self, baseline_invoices):
        """All amounts must be positive."""
        for inv in baseline_invoices:
            assert Decimal(inv.amount) > 0


# ---------------------------------------------------------------------------
# Distribution properties
# ---------------------------------------------------------------------------

class TestDistributionProperties:
    def test_baseline_low_missing_field_rate(self, baseline_invoices):
        """Baseline phase: nearly no missing fields (< 5%)."""
        missing_count = sum(1 for inv in baseline_invoices if inv.has_missing_fields)
        rate = missing_count / len(baseline_invoices)
        assert rate < 0.05, f"Baseline missing-field rate too high: {rate:.1%}"

    def test_degraded_higher_missing_field_rate(self, degraded_invoices):
        """Degraded phase: significantly more missing fields (> 10%)."""
        missing_count = sum(1 for inv in degraded_invoices if inv.has_missing_fields)
        rate = missing_count / len(degraded_invoices)
        assert rate > 0.10, f"Degraded missing-field rate too low: {rate:.1%}"

    def test_degraded_has_more_missing_fields_than_baseline(self, baseline_invoices, degraded_invoices):
        """Degraded distribution must have materially more missing fields than baseline."""
        baseline_rate = sum(1 for i in baseline_invoices if i.has_missing_fields) / len(baseline_invoices)
        degraded_rate = sum(1 for i in degraded_invoices if i.has_missing_fields) / len(degraded_invoices)
        assert degraded_rate > baseline_rate, (
            f"Degraded ({degraded_rate:.1%}) should exceed baseline ({baseline_rate:.1%})"
        )

    def test_baseline_low_ambiguous_vendor_rate(self, baseline_invoices):
        """Baseline phase: low ambiguous vendor rate (< 15%)."""
        ambig = sum(1 for inv in baseline_invoices if inv.is_ambiguous_vendor)
        rate = ambig / len(baseline_invoices)
        assert rate < 0.15, f"Baseline ambiguous-vendor rate too high: {rate:.1%}"

    def test_degraded_higher_ambiguous_vendor_rate(self, degraded_invoices):
        """Degraded phase: higher ambiguous vendor rate (> 15%)."""
        ambig = sum(1 for inv in degraded_invoices if inv.is_ambiguous_vendor)
        rate = ambig / len(degraded_invoices)
        assert rate > 0.15, f"Degraded ambiguous-vendor rate too low: {rate:.1%}"

    def test_categories_cover_all_five(self, baseline_invoices):
        """All 5 invoice categories should appear in a batch of 100."""
        categories = {inv.category.value for inv in baseline_invoices}
        assert categories == {"travel", "supplies", "software", "consulting", "logistics"}

    def test_ground_truth_decisions_include_approve_and_reject(self, baseline_invoices):
        """A batch of baseline invoices should include both APPROVE and REJECT answers."""
        decisions = {inv.ground_truth_decision for inv in baseline_invoices}
        assert Action.APPROVE in decisions
        assert Action.REJECT in decisions
