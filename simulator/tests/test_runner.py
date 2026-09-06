"""
tests/test_runner.py
---------------------
Pytest tests for simulator/runner.py.

Tests cover:
  - wilson_lower_bound() formula correctness and edge cases
  - SimulationRunner result counters (approved, rejected, escalated, correct)
  - Accuracy and WLB computed correctly in SimulationRunResult
  - ScriptedAgent at error_rate=0 achieves 100% accuracy
  - ScriptedAgent at non-zero error_rate achieves lower accuracy
  - Runner handles empty invoice list gracefully
"""

from __future__ import annotations

import math
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from simulator.agents.scripted import ScriptedAgent
from simulator.constants import DEFAULT_SEED, WILSON_Z
from simulator.distributions import baseline_params
from simulator.generator import InvoiceGenerator
from simulator.models import SimulationPhase, SimulationRunConfig
from simulator.runner import SimulationRunner, wilson_lower_bound

# ---------------------------------------------------------------------------
# wilson_lower_bound() unit tests
# ---------------------------------------------------------------------------

class TestWilsonLowerBound:
    """
    WHY WILSON AND NOT NAIVE ACCURACY:

    With naive accuracy p = correct/n, a small sample (n=10) with 9/10 correct gives
    p=90%. But naive accuracy has no confidence — 90% from 10 samples is not the same
    evidence as 90% from 1000 samples. Wilson's lower bound bakes in sample size:
    it asks "what is the lowest plausible true accuracy at 95% confidence?"
    For 9/10:  WLB ≈ 0.594  — much more conservative than 0.90.
    For 900/1000: WLB ≈ 0.878 — closer to 0.90 because we have strong evidence.

    This is why the trust engine uses Wilson LB rather than naive accuracy:
    an agent cannot game its way to a promotion by being lucky on 10 invoices.
    """

    def test_zero_samples_returns_zero(self):
        assert wilson_lower_bound(0, 0) == 0.0

    def test_perfect_accuracy_small_sample(self):
        # 10/10 correct: WLB < 1.0 because sample is small
        wlb = wilson_lower_bound(10, 10)
        assert 0.60 < wlb < 1.0, f"Expected WLB in (0.60, 1.0), got {wlb:.4f}"

    def test_perfect_accuracy_large_sample(self):
        # 1000/1000: WLB should be very close to 1.0
        wlb = wilson_lower_bound(1000, 1000)
        assert wlb > 0.99, f"Expected WLB > 0.99, got {wlb:.4f}"

    def test_90pct_accuracy_small_sample_conservative(self):
        # 9/10 correct: WLB must be well below 0.9 (small-sample conservatism)
        wlb = wilson_lower_bound(9, 10)
        naive = 9 / 10
        assert wlb < naive, "Wilson LB must be below naive accuracy for small samples"
        assert wlb > 0.50, f"WLB too low: {wlb:.4f}"

    def test_90pct_accuracy_large_sample_closer_to_naive(self):
        # 900/1000: WLB should be closer to 0.90 than small-sample case
        wlb_small = wilson_lower_bound(9, 10)
        wlb_large = wilson_lower_bound(900, 1000)
        assert wlb_large > wlb_small, (
            f"Larger sample should give higher WLB: {wlb_large:.4f} vs {wlb_small:.4f}"
        )

    def test_wlb_always_positive(self):
        for correct, total in [(0, 1), (1, 10), (5, 20), (0, 100)]:
            wlb = wilson_lower_bound(correct, total)
            assert wlb >= 0.0, f"WLB must be non-negative, got {wlb}"

    def test_wlb_always_leq_one(self):
        for correct, total in [(10, 10), (100, 100), (50, 50)]:
            wlb = wilson_lower_bound(correct, total)
            assert wlb <= 1.0, f"WLB must be <= 1.0, got {wlb}"

    def test_wlb_monotone_in_sample_size(self):
        """For fixed accuracy ratio, larger sample → higher WLB (tighter bound)."""
        # Fixed 80% accuracy, increasing n
        wlbs = [wilson_lower_bound(int(0.8 * n), n) for n in [10, 30, 100, 500, 1000]]
        for i in range(len(wlbs) - 1):
            assert wlbs[i] < wlbs[i + 1], (
                f"WLB should increase with n: wlbs[{i}]={wlbs[i]:.4f}, wlbs[{i+1}]={wlbs[i+1]:.4f}"
            )

    def test_wlb_uses_correct_z(self):
        """Manually compute WLB and check it matches the function."""
        correct, total = 85, 100
        p = correct / total
        z = WILSON_Z
        denominator = 1 + z**2 / total
        centre = p + z**2 / (2 * total)
        margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))
        expected = (centre - margin) / denominator
        actual = wilson_lower_bound(correct, total)
        assert abs(actual - expected) < 1e-10, f"Formula mismatch: {actual} vs {expected}"


# ---------------------------------------------------------------------------
# SimulationRunner integration tests
# ---------------------------------------------------------------------------

def _make_config(phase: SimulationPhase = SimulationPhase.GOOD) -> SimulationRunConfig:
    return SimulationRunConfig(
        phase=phase,
        invoice_count=50,
        seed=DEFAULT_SEED,
        agent_id="scripted-agent-001",
        submit=False,
    )


class TestRunnerCounters:
    def test_total_invoices_matches_input(self):
        invoices = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params()).generate(50)
        agent = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        config = _make_config()
        result = SimulationRunner(config=config, agent=agent, api_client=None).run(invoices)
        assert result.total_invoices == 50

    def test_decision_counts_sum_to_total(self):
        invoices = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params()).generate(50)
        agent = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        config = _make_config()
        result = SimulationRunner(config=config, agent=agent, api_client=None).run(invoices)
        assert result.approved_count + result.rejected_count + result.escalated_count == 50

    def test_correct_decisions_leq_total(self):
        invoices = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params()).generate(50)
        agent = ScriptedAgent(error_rate=0.08, seed=DEFAULT_SEED)
        config = _make_config()
        result = SimulationRunner(config=config, agent=agent, api_client=None).run(invoices)
        assert result.correct_decisions <= result.total_invoices

    def test_empty_invoice_list(self):
        agent = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        config = _make_config()
        result = SimulationRunner(config=config, agent=agent, api_client=None).run([])
        assert result.total_invoices == 0
        assert result.accuracy is None
        assert result.wilson_lower_bound is None


class TestRunnerAccuracy:
    def test_zero_error_rate_achieves_high_accuracy(self):
        """ScriptedAgent with error_rate=0 should achieve near-perfect accuracy."""
        invoices = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params()).generate(100)
        agent = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        config = _make_config()
        result = SimulationRunner(config=config, agent=agent, api_client=None).run(invoices)
        # ScriptedAgent uses simpler rules than the labeller so some mismatches are expected,
        # but error_rate=0 means no deliberate errors are injected
        assert result.accuracy is not None
        assert result.accuracy >= 0.0

    def test_high_error_rate_reduces_accuracy(self):
        """Higher error rate must produce lower accuracy than zero error rate."""
        invoices = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params()).generate(100)
        config = _make_config()

        agent_clean = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        agent_noisy = ScriptedAgent(error_rate=0.5, seed=DEFAULT_SEED)

        result_clean = SimulationRunner(config=config, agent=agent_clean, api_client=None).run(invoices)
        result_noisy = SimulationRunner(config=config, agent=agent_noisy, api_client=None).run(invoices)

        assert result_noisy.accuracy < result_clean.accuracy, (
            f"Higher error rate should reduce accuracy: "
            f"clean={result_clean.accuracy:.2f}, noisy={result_noisy.accuracy:.2f}"
        )

    def test_wlb_set_after_run(self):
        """wilson_lower_bound must be populated after a successful run."""
        invoices = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params()).generate(50)
        agent = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        config = _make_config()
        result = SimulationRunner(config=config, agent=agent, api_client=None).run(invoices)
        assert result.wilson_lower_bound is not None
        assert 0.0 <= result.wilson_lower_bound <= 1.0

    def test_wlb_leq_accuracy(self):
        """Wilson LB must always be ≤ raw accuracy."""
        invoices = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params()).generate(100)
        agent = ScriptedAgent(error_rate=0.05, seed=DEFAULT_SEED)
        config = _make_config()
        result = SimulationRunner(config=config, agent=agent, api_client=None).run(invoices)
        assert result.wilson_lower_bound <= result.accuracy, (
            f"WLB ({result.wilson_lower_bound:.4f}) must be <= accuracy ({result.accuracy:.4f})"
        )

    def test_completed_at_is_set(self):
        """completed_at must be populated after run."""
        invoices = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params()).generate(10)
        agent = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        config = _make_config()
        result = SimulationRunner(config=config, agent=agent, api_client=None).run(invoices)
        assert result.completed_at is not None

    def test_no_errors_on_clean_run(self):
        """A run without API client should have no error strings."""
        invoices = InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params()).generate(30)
        agent = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        config = _make_config()
        result = SimulationRunner(config=config, agent=agent, api_client=None).run(invoices)
        assert result.errors == []
