"""
simulator/agents/scripted.py
-----------------------------
ScriptedAgent — implements AgentProtocol with configurable error injection.

PURPOSE (two scenarios):
  1. "We can govern any agent, including third-party ones."
     The ScriptedAgent pretends to be a third-party black-box agent.
     It follows a simple deterministic rule set (approve/reject/escalate by
     amount thresholds) but injects a controlled fraction of wrong decisions.
     The governance layer treats it identically to the LLM agent.

  2. Fast offline testing without any LLM calls or API keys.
     Before you have a Gemini key, you can run the full simulation pipeline
     end-to-end with the ScriptedAgent to verify the runner, cache, API client,
     and fixture generation all work.

IMPORTANT:
  The ScriptedAgent's mistakes are SCRIPTED (deliberate, not genuine).
  This is different from the GeminiAgent whose mistakes are REAL.
  For the demo, the drift detector should be run against LLM agent data.
  The ScriptedAgent is a governance test bed, not the primary demo subject.
"""

from __future__ import annotations

import os
import random
import sys
from decimal import Decimal

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from shared.constants import AUTONOMY_FLOOR, AUTONOMY_LADDER
from shared.enums import Action

from simulator import reason_codes as RC
from simulator.constants import DEFAULT_SEED
from simulator.models import AgentOutcome, Invoice, InvoiceCategory


class ScriptedAgent:
    """
    A deterministic rule-following agent with a configurable error rate.

    Args:
        agent_id:    Stable identifier (used by runner and cache namespace)
        name:        Display name
        error_rate:  Fraction of decisions that will be wrong (0.0 – 1.0)
        seed:        Random seed for the error injection (reproducible)
        tier:        Autonomy tier to use for limit lookups
    """

    def __init__(
        self,
        agent_id: str = "scripted-agent-001",
        name: str = "ScriptedAgent v1",
        error_rate: float = 0.08,
        seed: int = DEFAULT_SEED,
        current_limit: int = AUTONOMY_FLOOR,
        allow_critical_errors: bool = True,
    ) -> None:
        self.agent_id = agent_id
        self.name = name
        self.error_rate = error_rate
        self.current_limit = current_limit
        # A CRITICAL error is approving an invoice that should be rejected —
        # money leaves the building. The trust engine treats a single one in the
        # recent window as an instant clawback, so a well-behaved agent must not
        # make them: its mistakes should be cautious (wrongly rejecting) instead.
        # Only the degraded phase turns these on.
        self.allow_critical_errors = allow_critical_errors
        self._rng = random.Random(seed)
        # Recommendations draw from their own stream so that adding them does
        # not shift the decision stream — the arc's existing beats stay put.
        self._rec_rng = random.Random(seed ^ 0x2EC0)

    def decide(self, invoice: Invoice) -> AgentOutcome:
        """Decide on an invoice using simple rules + optional error injection."""
        correct_decision, correct_reason = self._rule_based_decision(invoice)

        if self._rng.random() < self.error_rate:
            # Inject a wrong decision (opposite of correct)
            wrong_decision, wrong_reason = self._flip_decision(
                correct_decision, correct_reason, invoice
            )
            return AgentOutcome(
                invoice_id=invoice.invoice_id,
                agent_id=self.agent_id,
                action=wrong_decision,
                reason=wrong_reason,
                confidence=0.6,   # Lower confidence signals uncertainty
                from_cache=False,
            )

        return AgentOutcome(
            invoice_id=invoice.invoice_id,
            agent_id=self.agent_id,
            action=correct_decision,
            reason=correct_reason,
            confidence=0.9,
            from_cache=False,
        )

    def recommend(self, invoice: Invoice) -> Action:
        """What this agent would have DONE, had its limit not stopped it.

        Only meaningful for a decision it escalated. `shared.contracts.
        DecisionRecord.has_human_ruling` needs this alongside a human ruling
        before the pair counts toward human agreement, and `human_agreed`
        compares the two.

        This is the ordinary rule chain with the tier limit taken out, because
        the tier limit is the only reason the agent deferred rather than acted.
        It is wrong at the same `error_rate` as a real decision: a degraded
        agent gives degraded advice, which is exactly what makes human
        agreement fall in the degraded phase rather than sitting at 100%.
        """
        if invoice.missing_field_names:
            recommendation = Action.REJECT
        else:
            amount = Decimal(invoice.amount)
            if amount <= 0 or amount > AUTONOMY_LADDER[-1]:
                recommendation = Action.REJECT
            else:
                try:
                    InvoiceCategory(invoice.category)
                except ValueError:
                    recommendation = Action.REJECT
                else:
                    recommendation = Action.APPROVE

        if self._rec_rng.random() < self.error_rate:
            return Action.REJECT if recommendation is Action.APPROVE else Action.APPROVE
        return recommendation

    # ------------------------------------------------------------------
    # Internal decision logic
    # ------------------------------------------------------------------

    def _rule_based_decision(
        self, invoice: Invoice
    ) -> tuple[Action, str]:
        """Simple rules — similar to labeller but less sophisticated."""
        if invoice.missing_field_names:
            return Action.ESCALATE, RC.ESCALATE_MISSING_FIELDS

        amount = Decimal(invoice.amount)
        if amount <= 0:
            return Action.REJECT, RC.REJECT_NEGATIVE_AMOUNT

        try:
            InvoiceCategory(invoice.category)
        except ValueError:
            return Action.REJECT, RC.REJECT_INVALID_CATEGORY

        limit = self.current_limit
        high_limit = AUTONOMY_LADDER[-1]

        if amount > high_limit:
            return Action.REJECT, RC.REJECT_EXCEEDS_LIMIT
        if amount > limit:
            return Action.ESCALATE, RC.ESCALATE_EXCEEDS_TIER

        return Action.APPROVE, RC.APPROVE_WITHIN_LIMIT

    def _flip_decision(
        self, correct: Action, correct_reason: str, invoice: Invoice
    ) -> tuple[Action, str]:
        """Return a genuinely wrong ACTION — never a deferral.

        An injected error means the agent acted incorrectly, so it must show
        up as a wrong acted decision (lower accuracy), not as an escalation
        (which the trust engine excludes from accuracy entirely).
        """
        if correct == Action.APPROVE:
            # Wrongly reject a legitimate invoice — a non-critical error.
            return Action.REJECT, RC.REJECT_SCRIPTED_ERROR
        if correct == Action.REJECT:
            if not self.allow_critical_errors:
                # A cautious agent doesn't wrongly approve a bad invoice.
                # Skip the injection rather than manufacture a critical error.
                return Action.REJECT, RC.REJECT_SCRIPTED_ERROR
            # Wrongly approve a bad invoice — a CRITICAL error (money leaves).
            return Action.APPROVE, RC.APPROVE_SCRIPTED_ERROR
        # correct == Action.ESCALATE.
        if not self.allow_critical_errors:
            # A cautious agent still defers when it should. Injecting an error
            # here would turn a deferral into an acted decision, which changes
            # utilization rather than accuracy — not the mistake we're modelling.
            return Action.ESCALATE, correct_reason
        # A degraded agent misses the escalation trigger and acts anyway.
        if self._rng.random() < 0.5:
            return Action.APPROVE, RC.APPROVE_SCRIPTED_ERROR
        return Action.REJECT, RC.REJECT_SCRIPTED_ERROR
