"""
simulator/distributions.py
---------------------------
Phase-specific distribution parameters.

WHY THIS FILE EXISTS:
  The three simulation phases (good / degraded / recovery) are implemented by
  passing different "knob" settings into the InvoiceGenerator.  Centralising
  those knobs here means you can tune difficulty in one place without touching
  generator logic.

DEGRADED PHASE — THE MOST IMPORTANT:
  The degraded phase is how we induce GENUINE drift in the LLM agent.
  We don't flip a fake error flag — we make the invoices harder:
    • Amounts cluster within ±5 % of policy limits (boundary cases)
    • 15–20 % of invoices have a missing required field
    • 25 % use ambiguous/unknown vendor names
    • Unusual or borderline categories appear more often
  Gemini at temperature 0 genuinely struggles with these, producing real
  accuracy degradation that the trust engine then detects.
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Allow the shared/ package to be imported without installing it as a package.
# When running from the simulator/ directory: sys.path includes the repo root.
# ---------------------------------------------------------------------------
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from dataclasses import dataclass, field

from simulator.constants import (
    AMOUNT_LOG_NORMAL_MU,
    AMOUNT_LOG_NORMAL_SIGMA,
    AMOUNT_MAX_INR,
    AMOUNT_MIN_INR,
    DEGRADED_AMBIGUOUS_VENDOR_PROB,
    DEGRADED_BOUNDARY_FRACTION,
    DEGRADED_MISSING_FIELD_PROB,
    RECOVERY_AMBIGUOUS_VENDOR_PROB,
    RECOVERY_BOUNDARY_FRACTION,
    RECOVERY_MISSING_FIELD_PROB,
)


@dataclass
class DistributionParams:
    """
    All tuneable knobs fed to InvoiceGenerator for a single phase.
    """

    # Amount distribution (log-normal)
    amount_mu: float = AMOUNT_LOG_NORMAL_MU
    amount_sigma: float = AMOUNT_LOG_NORMAL_SIGMA
    amount_min: int = AMOUNT_MIN_INR
    amount_max: int = AMOUNT_MAX_INR

    # Difficulty knobs (probabilities, 0.0 – 1.0)
    missing_field_prob: float = 0.0
    ambiguous_vendor_prob: float = 0.0
    boundary_fraction: float = 0.0   # Fraction of invoices near policy limit ±X %
    boundary_tolerance: float = 0.05  # ±5 % of limit counts as a boundary case

    # Category weighting — which categories appear how often.
    # Keys: InvoiceCategory values.  Must sum to 1.0 (normalised internally).
    category_weights: dict[str, float] = field(default_factory=lambda: {
        "travel":     0.20,
        "supplies":   0.20,
        "software":   0.20,
        "consulting": 0.20,
        "logistics":  0.20,
    })

    # Fraction of invoices that use vendors from the blocked list
    # (used in degraded to inject reject-worthy invoices)
    blocked_vendor_prob: float = 0.0

    # Label: for logging / fixture metadata
    label: str = "baseline"


# ---------------------------------------------------------------------------
# The three canonical phase configurations
# ---------------------------------------------------------------------------

def baseline_params() -> DistributionParams:
    """
    GOOD phase — clean invoices that a competent agent should get right.
    • Amounts mostly well below policy limits (sigma kept moderate)
    • No missing fields, no ambiguous vendors, no boundary clustering
    • Equal category distribution
    Expected LLM error rate: ~5–10 %
    """
    return DistributionParams(
        amount_mu=8.0,           # median ~2980 INR — comfortably below LOW limits
        amount_sigma=0.9,
        missing_field_prob=0.0,
        ambiguous_vendor_prob=0.05,
        boundary_fraction=0.05,
        blocked_vendor_prob=0.03,
        label="good",
    )


def shifted_params() -> DistributionParams:
    """
    DEGRADED phase — genuinely hard invoices that stress the LLM.
    • Amounts cluster near policy limits (±5 %) — hard to decide approve vs escalate
    • ~18 % chance of a missing required field → should escalate but LLM may guess
    • ~25 % ambiguous / unknown vendors
    • Slightly higher sigma → more spread, more edge cases
    • Category distribution skewed toward consulting + travel (higher limits, more ambiguity)
    Expected LLM error rate: >20 % (clearly worse than baseline)
    """
    return DistributionParams(
        amount_mu=8.8,           # median ~6634 INR — more invoices near LOW limits
        amount_sigma=1.3,
        missing_field_prob=DEGRADED_MISSING_FIELD_PROB,
        ambiguous_vendor_prob=DEGRADED_AMBIGUOUS_VENDOR_PROB,
        boundary_fraction=DEGRADED_BOUNDARY_FRACTION,
        blocked_vendor_prob=0.08,
        category_weights={
            "travel":     0.30,
            "consulting": 0.30,
            "software":   0.15,
            "supplies":   0.15,
            "logistics":  0.10,
        },
        label="degraded",
    )


def recovery_params() -> DistributionParams:
    """
    RECOVERY phase — difficulty relaxes back toward baseline.
    • Missing field rate drops to ~5 %
    • Ambiguous vendor rate drops to ~10 %
    • Amounts shift back toward centre
    Expected LLM error rate: ~10–15 % (improving)
    """
    return DistributionParams(
        amount_mu=8.3,
        amount_sigma=1.0,
        missing_field_prob=RECOVERY_MISSING_FIELD_PROB,
        ambiguous_vendor_prob=RECOVERY_AMBIGUOUS_VENDOR_PROB,
        boundary_fraction=RECOVERY_BOUNDARY_FRACTION,
        blocked_vendor_prob=0.05,
        label="recovery",
    )


def get_params(phase: str) -> DistributionParams:
    """Return the correct DistributionParams for a given phase string."""
    mapping = {
        "good":     baseline_params,
        "degraded": shifted_params,
        "recovery": recovery_params,
    }
    if phase not in mapping:
        raise ValueError(f"Unknown phase {phase!r}. Choose from: {list(mapping)}")
    return mapping[phase]()
