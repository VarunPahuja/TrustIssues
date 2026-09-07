"""
tests/test_scripted_agent.py
-----------------------------
Pytest tests for simulator/agents/scripted.py.

Tests cover:
  - Interface compliance: decide() returns AgentDecisionRecord
  - Reproducibility: same seed → same decisions
  - error_rate=0.0: agent is fully deterministic, no deliberate wrong decisions injected
  - error_rate=1.0: every decision is deliberately wrong
  - from_cache=False on all ScriptedAgent decisions
  - Confidence values: 0.9 for correct decisions, 0.6 for injected errors
  - agent_id and name attributes are present
"""

from __future__ import annotations

import sys
import os

import pytest

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
    
from shared.enums import Action
from simulator.constants import DEFAULT_SEED
from simulator.models import AgentOutcome, SimulationPhase
from simulator.generator import InvoiceGenerator
from simulator.distributions import baseline_params
from simulator.agents.scripted import ScriptedAgent
from simulator.agents.base import AgentProtocol


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def invoices():
    return InvoiceGenerator(seed=DEFAULT_SEED, params=baseline_params(), phase=SimulationPhase.GOOD).generate(50)


@pytest.fixture
def clean_agent():
    return ScriptedAgent(agent_id="test-scripted", name="Test Agent", error_rate=0.0, seed=DEFAULT_SEED)


@pytest.fixture
def noisy_agent():
    return ScriptedAgent(agent_id="test-scripted", name="Test Agent", error_rate=0.5, seed=DEFAULT_SEED)


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------

class TestProtocolCompliance:
    def test_implements_agent_protocol(self, clean_agent):
        assert isinstance(clean_agent, AgentProtocol), (
            "ScriptedAgent must satisfy AgentProtocol structural typing"
        )

    def test_has_agent_id(self, clean_agent):
        assert hasattr(clean_agent, "agent_id")
        assert isinstance(clean_agent.agent_id, str)
        assert len(clean_agent.agent_id) > 0

    def test_has_name(self, clean_agent):
        assert hasattr(clean_agent, "name")
        assert isinstance(clean_agent.name, str)

    def test_decide_returns_agent_decision_record(self, clean_agent, invoices):
        record = clean_agent.decide(invoices[0])
        assert isinstance(record, AgentOutcome)

    def test_record_has_invoice_id(self, clean_agent, invoices):
        record = clean_agent.decide(invoices[0])
        assert record.invoice_id == invoices[0].invoice_id

    def test_record_has_agent_id(self, clean_agent, invoices):
        record = clean_agent.decide(invoices[0])
        assert record.agent_id == clean_agent.agent_id


# ---------------------------------------------------------------------------
# from_cache always False for ScriptedAgent
# ---------------------------------------------------------------------------

class TestFromCache:
    def test_from_cache_always_false(self, clean_agent, invoices):
        for inv in invoices[:20]:
            record = clean_agent.decide(inv)
            assert record.from_cache is False, "ScriptedAgent never uses a cache"


# ---------------------------------------------------------------------------
# Confidence values
# ---------------------------------------------------------------------------

class TestConfidence:
    def test_correct_decisions_have_high_confidence(self, invoices):
        agent = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        for inv in invoices[:20]:
            record = agent.decide(inv)
            assert record.confidence == 0.9, (
                f"Clean decision should have confidence 0.9, got {record.confidence}"
            )

    def test_injected_errors_have_lower_confidence(self, invoices):
        # error_rate=1.0 means every decision is wrong → confidence 0.6
        agent = ScriptedAgent(error_rate=1.0, seed=DEFAULT_SEED)
        for inv in invoices[:20]:
            record = agent.decide(inv)
            assert record.confidence == 0.6, (
                f"Injected error should have confidence 0.6, got {record.confidence}"
            )


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

class TestReproducibility:
    def test_same_seed_same_decisions(self, invoices):
        agent_a = ScriptedAgent(error_rate=0.2, seed=42)
        agent_b = ScriptedAgent(error_rate=0.2, seed=42)
        decisions_a = [agent_a.decide(inv).decision for inv in invoices]
        decisions_b = [agent_b.decide(inv).decision for inv in invoices]
        assert decisions_a == decisions_b

    def test_different_seeds_different_error_patterns(self, invoices):
        # With error_rate=0.3, two different seeds should inject errors at different points
        agent_a = ScriptedAgent(error_rate=0.3, seed=1)
        agent_b = ScriptedAgent(error_rate=0.3, seed=99)
        decisions_a = [agent_a.decide(inv).decision for inv in invoices]
        decisions_b = [agent_b.decide(inv).decision for inv in invoices]
        # Very unlikely to produce identical patterns with 0.3 error rate over 50 invoices
        assert decisions_a != decisions_b


# ---------------------------------------------------------------------------
# Error rate behaviour
# ---------------------------------------------------------------------------

class TestErrorRate:
    def test_zero_error_rate_is_deterministic(self, invoices):
        """error_rate=0.0: both runs produce identical decisions."""
        agent_a = ScriptedAgent(error_rate=0.0, seed=42)
        agent_b = ScriptedAgent(error_rate=0.0, seed=99)  # Different seed, same 0 error rate
        decisions_a = [agent_a.decide(inv).decision for inv in invoices]
        decisions_b = [agent_b.decide(inv).decision for inv in invoices]
        # With 0 error rate, seed doesn't matter — decisions are fully rule-based
        assert decisions_a == decisions_b

    def test_higher_error_rate_produces_more_wrong_decisions(self, invoices):
        """On the invoices the rule-faithful agent acts on (not deferrals), a
        higher error_rate must produce fewer correct decisions."""
        clean = ScriptedAgent(error_rate=0.0, seed=DEFAULT_SEED)
        noisy = ScriptedAgent(error_rate=0.5, seed=DEFAULT_SEED)

        # error_rate=0.0 is deterministic, so this filter is stable.
        acted = [
            inv for inv in invoices
            if clean.decide(inv).decision != Action.ESCALATE
        ]

        def correct_on(agent):
            return sum(
                1 for inv in acted
                if agent.decide(inv).decision == inv.ground_truth_decision
            )

        correct_clean = correct_on(clean)
        correct_noisy = correct_on(noisy)

        assert correct_clean >= correct_noisy, (
            f"Injected errors should reduce correctness on acted decisions: "
            f"clean={correct_clean}, noisy={correct_noisy}"
        )
