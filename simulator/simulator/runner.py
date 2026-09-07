"""
simulator/runner.py
--------------------
SimulationRunner — orchestrates one complete simulation run.

WHAT IT DOES:
  For each invoice in the batch:
    1. Call the agent's decide() method → AgentOutcome
    2. Compare decision to invoice.ground_truth_decision → is_correct
    3. POST to the backend API (if api_client is provided)
    4. Collect stats

  At the end: compute accuracy, Wilson lower bound, return SimulationRunResult.

WHY IT'S SEPARATE FROM THE CLI:
  The runner is a plain Python class so it can be unit-tested without Typer,
  called programmatically from integration tests, or embedded in a web worker
  later.  The CLI (cli.py) is just a thin wrapper that builds the runner and
  calls it.

WILSON LOWER BOUND:
  We compute the 95 % Wilson score interval lower bound on the fly.
  This is the same formula the trust engine uses — running it here lets us
  report the expected bound in the fixture metadata so teammates can verify
  the trust engine is computing it correctly.
"""

from __future__ import annotations

import sys
import os
from datetime import UTC, datetime
from typing import Optional, Callable

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
_trust_root = os.path.join(_repo_root, "trust")
if _trust_root not in sys.path:
    sys.path.insert(0, _trust_root)

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from shared.enums import Action
from simulator.models import AgentOutcome, Invoice, SimulationRunConfig, SimulationRunResult
from trust.trust_engine.stats.wilson import wilson_lower_bound
from simulator.api_client import APIClient

console = Console()


class SimulationRunner:
    """
    Runs a batch of invoices through an agent and optionally submits to the API.

    Args:
        config:      SimulationRunConfig with phase, count, seed, agent info
        agent:       Any object implementing AgentProtocol
        api_client:  Optional APIClient. If None, invoices are processed locally
                     only (useful for fixture generation without a running backend)
        on_progress: Optional callback(completed, total) called after each invoice
    """

    def __init__(
        self,
        config: SimulationRunConfig,
        agent,  # AgentProtocol
        api_client: Optional[APIClient] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        self.config = config
        self.agent = agent
        self.api_client = api_client
        self.on_progress = on_progress

    def run(self, invoices: list[Invoice]) -> SimulationRunResult:
        """
        Process all invoices. Returns a populated SimulationRunResult.
        """
        result = SimulationRunResult(
            config=self.config,
            total_invoices=len(invoices),
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(
                f"[{self.config.phase}] Running {self.agent.name}...",
                total=len(invoices),
            )

            for i, invoice in enumerate(invoices):
                try:
                    record = self._process_one(invoice, result)
                    progress.advance(task)
                    if self.on_progress:
                        self.on_progress(i + 1, len(invoices))
                except Exception as exc:
                    result.errors.append(
                        f"Invoice {invoice.invoice_id}: {type(exc).__name__}: {exc}"
                    )
                    progress.advance(task)

        # Final statistics
        result.completed_at = datetime.now(UTC)
        if result.total_invoices > 0:
          acted = result.total_invoices - result.escalated_count
        # Final statistics
        result.completed_at = datetime.now(UTC)
        acted = result.total_invoices - result.escalated_count
        if acted > 0:
            result.accuracy = result.correct_decisions / acted
            result.wilson_lower_bound = wilson_lower_bound(result.correct_decisions, acted)
        # Cache stats (if agent supports it)
        if hasattr(self.agent, "cache_stats"):
            stats = self.agent.cache_stats
            result.cache_hits = stats.get("hits", 0)
        if hasattr(self.agent, "llm_calls"):
            result.llm_calls = self.agent.llm_calls

        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_one(
        self, invoice: Invoice, result: SimulationRunResult
    ) -> AgentOutcome:
        # Agent decides (may hit cache)
        record = self.agent.decide(invoice)

           # Score against ground truth — an escalation is neither right nor wrong
        record.is_correct = (
            None
            if record.action == Action.ESCALATE
            else record.action == invoice.ground_truth_decision
        )

        # Update counters
        if record.action == Action.APPROVE:
            result.approved_count += 1
        elif record.action == Action.REJECT:
            result.rejected_count += 1
        else:
            result.escalated_count += 1

        if record.is_correct:
            result.correct_decisions += 1

        # Submit to backend API (if configured)
        if self.api_client:
            try:
                reason = f"sim-run {self.config.seed} invoice {invoice.invoice_id}"
                self.api_client.submit_decision(invoice, record, self.config.agent_id, reason)
            except Exception as exc:
                result.errors.append(f"API submit failed for {invoice.invoice_id}: {exc}")

        return record
