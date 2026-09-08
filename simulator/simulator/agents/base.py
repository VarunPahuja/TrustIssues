"""
simulator/agents/base.py
-------------------------
The AgentProtocol that every agent must implement.

WHY A PROTOCOL (not ABC):
  Python's typing.Protocol is structural — any class with a `decide` method
  satisfying the signature is automatically compatible, even if it doesn't
  explicitly inherit from AgentProtocol.  This makes it trivial to wrap a
  third-party agent behind the interface without modifying it.

INTERFACE CONTRACT:
  decide(invoice: Invoice) → AgentOutcome
    • Must return a fully populated AgentOutcome
    • May call an LLM, use rules, or replay a fixture — doesn't matter
    • Must be callable from a single thread (no async for simplicity)
    • The runner catches all exceptions and records them as errors
"""

from __future__ import annotations

import os
import sys
from typing import Protocol, runtime_checkable

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from simulator.models import AgentOutcome, Invoice


@runtime_checkable
class AgentProtocol(Protocol):
    """
    Structural protocol for any invoice-processing agent.

    Both GeminiAgent and ScriptedAgent implement this interface.
    The runner only depends on this protocol, never on concrete types.
    """

    agent_id: str   # Stable identifier, used as the cache namespace
    name: str       # Human-readable display name

    def decide(self, invoice: Invoice) -> AgentOutcome:
        """
        Process a single invoice and return a decision record.

        Args:
            invoice: The Invoice object (with ground truth set by the labeller,
                     but the agent must NOT look at ground_truth_* fields).

        Returns:
            AgentOutcome with action, reason, confidence, and cache metadata.
        """
        ...
