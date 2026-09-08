"""The arc's human-ruling evidence.

`shared.contracts.DecisionRecord.has_human_ruling` needs three things before a
decision counts toward human agreement: it must be escalated, it must carry the
agent's `recommended_action`, and it must carry a `human_ruling`. Until the arc
supplied the last two, every beat reported AGREEMENT_EVIDENCE_INSUFFICIENT and
WEIGHTS_RENORMALISED, and a quarter of the trust score never had data.
"""

from __future__ import annotations

import pytest
from shared.enums import Action

from simulator.agents.scripted import ScriptedAgent
from simulator.arc import ArcRunner, default_script
from simulator.distributions import get_params
from simulator.generator import InvoiceGenerator
from simulator.models import SimulationPhase


@pytest.fixture
def invoices():
    return InvoiceGenerator(
        seed=42, params=get_params("good"), phase=SimulationPhase.GOOD
    ).generate(60)


# ---------------------------------------------------------------------------
# ScriptedAgent.recommend
# ---------------------------------------------------------------------------


def test_recommend_never_returns_escalate(invoices):
    """A recommendation is what the agent would have DONE. 'I would escalate'
    is not an action and could never agree or disagree with a human."""
    agent = ScriptedAgent(error_rate=0.1, seed=42)
    for invoice in invoices:
        assert agent.recommend(invoice) in (Action.APPROVE, Action.REJECT)


def test_recommend_is_deterministic_for_a_seed(invoices):
    a = ScriptedAgent(error_rate=0.1, seed=42)
    b = ScriptedAgent(error_rate=0.1, seed=42)
    assert [a.recommend(i) for i in invoices] == [b.recommend(i) for i in invoices]


def test_a_flawless_agent_recommends_rejecting_incomplete_invoices():
    """With no error injection the rule chain must stand on its own: an invoice
    missing required fields cannot be approved."""
    agent = ScriptedAgent(error_rate=0.0, seed=42)
    gen = InvoiceGenerator(seed=7, params=get_params("degraded"), phase=SimulationPhase.DEGRADED)
    incomplete = [i for i in gen.generate(300) if i.missing_field_names]
    assert incomplete, "degraded phase should produce some incomplete invoices"
    for invoice in incomplete:
        assert agent.recommend(invoice) is Action.REJECT


def test_recommendations_degrade_with_the_error_rate(invoices):
    """Human agreement only carries signal if a degraded agent gives degraded
    advice. A 100% error rate must invert every recommendation."""
    clean = ScriptedAgent(error_rate=0.0, seed=42)
    broken = ScriptedAgent(error_rate=1.0, seed=42)
    for invoice in invoices:
        assert clean.recommend(invoice) is not broken.recommend(invoice)


def test_recommending_does_not_disturb_the_decision_stream(invoices):
    """Recommendations draw from their own RNG, so asking for them must not
    change what the agent decides — that is what keeps the arc's beats stable."""
    plain = ScriptedAgent(error_rate=0.1, seed=42)
    interleaved = ScriptedAgent(error_rate=0.1, seed=42)

    expected = [plain.decide(i).action for i in invoices]
    actual = []
    for invoice in invoices:
        interleaved.recommend(invoice)
        actual.append(interleaved.decide(invoice).action)
    assert actual == expected


# ---------------------------------------------------------------------------
# What the arc records
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def arc_records():
    runner = ArcRunner(seed=42, auto_approve=True)
    for beat in default_script(60):
        runner.run_phase(beat.phase, beat.count)
    return runner.records


def test_escalations_carry_both_halves_of_the_evidence(arc_records):
    escalated = [r for r in arc_records if r.is_escalated]
    assert escalated, "the arc must produce escalations to rule on"
    for record in escalated:
        assert record.recommended_action is not None
        assert record.human_ruling is not None
        assert record.has_human_ruling


def test_acted_decisions_carry_neither(arc_records):
    """Only an escalation is awaiting a human. A decision the agent acted on
    has nothing to rule on, and must not be counted as ruled."""
    for record in arc_records:
        if record.is_acted:
            assert record.recommended_action is None
            assert record.human_ruling is None
            assert not record.has_human_ruling


def test_rulings_are_never_escalate(arc_records):
    for record in arc_records:
        assert record.human_ruling is not Action.ESCALATE
        assert record.recommended_action is not Action.ESCALATE


def test_there_is_enough_evidence_for_the_agreement_component(arc_records):
    """Below MIN_RULED_ESCALATIONS_FOR_AGREEMENT the trust engine drops the
    component and renormalises the remaining weights — which is exactly the
    state this work exists to leave behind."""
    from trust.trust_engine.constants import MIN_RULED_ESCALATIONS_FOR_AGREEMENT

    ruled = [r for r in arc_records if r.has_human_ruling]
    assert len(ruled) >= MIN_RULED_ESCALATIONS_FOR_AGREEMENT


def test_agreement_is_not_unanimous(arc_records):
    """If the agent and the human never disagreed, agreement would be a
    constant 1.0 and would carry no information about the agent at all."""
    agreed = {r.human_agreed for r in arc_records if r.has_human_ruling}
    assert agreed == {True, False}
