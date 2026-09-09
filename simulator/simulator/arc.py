"""
simulator/arc.py
-----------------
The ten-beat demo orchestrator.

WHAT IT DOES:
  Chains the whole demo story into one run:

    1-3  agent works at the autonomy floor, evidence accumulates
    4    the trust engine says INCREASE
    5    a human (or --auto-approve) authorises it
    6    the limit rises; the agent now approves more itself
    ...  repeat to climb rungs
    7    a degraded phase injects a real performance drop
    8    drift is detected
    9    the limit is clawed back AUTOMATICALLY (no human)
    10   a recovery phase runs; the agent becomes eligible again

TWO MODES:
  offline (default)  Decisions are fed straight into trust_engine.evaluate()
                     in-process. No backend, no database, no network. This is
                     what proves "a seeded run reproduces the arc identically
                     twice" — everything is deterministic.
  online             Decisions are POSTed to the backend and the real
                     recommendation/approval endpoints drive the ladder.

DESIGN:
  This module sits ABOVE SimulationRunner and does not modify it. runner.py
  still just runs one batch; arc.py sequences the batches and handles the
  approvals in between.

DETERMINISM:
  No wall-clock reads, no uuid4. decision_id is derived from the run id and
  the sequence number so two runs with the same seed produce byte-identical
  output.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from decimal import Decimal

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
_trust_root = os.path.join(_repo_root, "trust")
if _trust_root not in sys.path:
    sys.path.insert(0, _trust_root)

from rich.console import Console
from shared.constants import AUTONOMY_FLOOR
from shared.contracts import AgentContext, DecisionRecord
from shared.enums import Action, AgentState
from trust.trust_engine.evaluate import evaluate

from simulator.agents.scripted import ScriptedAgent
from simulator.api_client import APIClient
from simulator.constants import DEFAULT_SEED, PHASE_ERROR_RATES
from simulator.distributions import get_params
from simulator.generator import InvoiceGenerator
from simulator.models import Invoice, SimulationPhase

console = Console()


# ---------------------------------------------------------------------------
# The phase script — the shape of the demo story
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Beat:
    """One step of the arc."""
    label: str
    phase: SimulationPhase
    count: int
    resolve: bool = True   # after this batch, act on whatever the ladder says


def default_script(count: int) -> list[Beat]:
    """The ten beats: climb, collapse, claw back, recover, climb again.

    Recovery is split deliberately. The degraded phase's mistakes stay in the
    lifetime history and have to be diluted by clean decisions before the trust
    score can clear the threshold again, so 10a is given only enough runway to
    show clear improvement while still falling short: it reports
    TRUST_BELOW_THRESHOLD and holds. 10b then supplies the rest and earns the
    rung back. That "recovering, but not yet" beat is the point of splitting
    them -- it shows the threshold is a real gate rather than a formality, and
    that a clawback cannot be undone simply by waiting.

    The split is calibrated against the default count of 200, where 10a lands
    at a trust score of 69.2 against a threshold of 70.0. Widening 10a to a
    full `count * 2` pushes it to 70.3 and the beat disappears.
    """
    return [
        Beat("1-3  earning trust at the floor", SimulationPhase.GOOD, count),
        Beat("4-6  first rung earned", SimulationPhase.GOOD, count),
        Beat("6b   climbing again", SimulationPhase.GOOD, count),
        Beat("7-9  degradation injected", SimulationPhase.DEGRADED, count),
        Beat("10a  recovery begins", SimulationPhase.RECOVERY, count * 3 // 4),
        Beat("10b  recovery continues", SimulationPhase.RECOVERY, count * 3),
    ]


# ---------------------------------------------------------------------------
# ArcRunner
# ---------------------------------------------------------------------------

class ArcRunner:
    """Runs the ten-beat arc. Offline by default."""

    def __init__(
        self,
        *,
        agent_id: str = "agent-01",
        seed: int = DEFAULT_SEED,
        count: int = 200,
        auto_approve: bool = True,
        run_id: str = "arc",
        api_client: APIClient | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.seed = seed
        self.count = count
        self.auto_approve = auto_approve
        self.run_id = run_id

        # Online mode. Submission is deliberately a SIDE EFFECT: the arc still
        # evaluates from its own in-memory records, so the story it tells is
        # identical whether or not a backend is attached. That is what keeps
        # the demo reproducible while still proving the ingest path works.
        # Driving the backend's own ladder (generating recommendations and
        # letting it move the limit) is a separate step, not this one.
        self.api_client = api_client
        self.submitted: int = 0
        self.ruled: int = 0
        self.submit_failures: list[str] = []
        self.submitted_ids: list[str] = []
        self._pending_rulings: list[tuple[str, Action, int]] = []

        # Ladder state the backend would normally hold.
        self.limit: int = AUTONOMY_FLOOR
        self.state: AgentState = AgentState.PROBATION
        self.records: list[DecisionRecord] = []
        self._n_at_last_change: int = 0
        self._n_at_last_clawback: int | None = None

    # ------------------------------------------------------------------
    # Ladder state -> AgentContext (mirrors backend app/services/trust.py)
    # ------------------------------------------------------------------

    def context(self) -> AgentContext:
        n = len(self.records)
        since_clawback = (
            None if self._n_at_last_clawback is None
            else n - self._n_at_last_clawback
        )
        return AgentContext(
            current_limit=self.limit,
            decisions_since_last_change=n - self._n_at_last_change,
            decisions_since_clawback=since_clawback,
            state=self.state,
        )

    # ------------------------------------------------------------------
    # One phase: generate invoices, decide, append DecisionRecords
    # ------------------------------------------------------------------

    def run_phase(self, phase: SimulationPhase, count: int) -> None:
        error_rate = PHASE_ERROR_RATES.get(phase.value, 0.05)
        invoices = InvoiceGenerator(
            seed=self.seed, params=get_params(phase.value), phase=phase
        ).generate(count)
        agent = ScriptedAgent(
            agent_id=self.agent_id,
            seed=self.seed,
            current_limit=self.limit,       # the REAL earned limit
            error_rate=error_rate,
            # Only the degraded phase makes dangerous mistakes. In the good and
            # recovery phases the agent errs cautiously, so a single stray
            # critical error can't claw back an agent that is behaving.
            allow_critical_errors=(phase is SimulationPhase.DEGRADED),
        )
        for invoice in invoices:
            outcome = agent.decide(invoice)
            record = self._to_record(invoice, outcome, agent)
            self.records.append(record)
            if self.api_client is not None:
                self._submit(invoice, outcome, record)

        if self.api_client is not None:
            self._flush_rulings()

    def _submit(self, invoice: Invoice, outcome, record: DecisionRecord) -> None:
        """POST one decision, and its ruling if it was escalated.

        A failure is collected, never raised: one rejected decision must not
        abort a 1,500-decision arc, and the count of failures is itself the
        result we care about — a run that drops decisions is not reproducible.
        """
        try:
            response = self.api_client.submit_decision(
                invoice,
                outcome,
                self.agent_id,
                reason=f"{self.run_id} seed={self.seed} seq={record.sequence}",
                recommended_action=record.recommended_action,
            )
        except Exception as exc:  # noqa: BLE001 - a failed submit is recorded, not fatal
            self.submit_failures.append(f"{record.decision_id}: {type(exc).__name__}: {exc}")
            return

        self.submitted += 1
        decision_id = response.get("decision_id")
        if decision_id:
            self.submitted_ids.append(decision_id)

        # An escalation carries a human ruling in this arc, so it needs sending
        # too — without it the backend's own trust evaluation would drop the
        # human-agreement component even though the evidence exists locally.
        #
        # Queued, not sent now. `POST /decisions` returns 201 before the row is
        # readable: measured at a median 50ms lag on a populated database, and
        # the gap widens as the table grows. Ruling immediately meant asking
        # the backend about a decision it had just told us it created and did
        # not yet admit to having — half the rulings failed on a full arc run.
        #
        # Deferring is also the more faithful model. A human reviewing an
        # escalation is a separate act that happens afterwards, not in the same
        # breath as the agent's decision.
        if record.human_ruling is None or not decision_id:
            return
        self._pending_rulings.append((decision_id, record.human_ruling, record.sequence))

    def _flush_rulings(self) -> None:
        """Send the rulings queued during this phase."""
        pending, self._pending_rulings = self._pending_rulings, []
        for decision_id, ruling, sequence in pending:
            try:
                self.api_client.submit_ruling(
                    decision_id,
                    ruling,
                    reason=f"{self.run_id} reviewer ruling seq={sequence}",
                )
                self.ruled += 1
            except Exception as exc:  # noqa: BLE001 - a failed ruling is recorded, not fatal
                self.submit_failures.append(
                    f"{decision_id} ruling: {type(exc).__name__}: {exc}"
                )

    def _to_record(self, invoice: Invoice, outcome, agent: ScriptedAgent) -> DecisionRecord:
        seq = len(self.records)
        # An escalation is the agent deferring to a human, so it carries both
        # halves of the human-agreement evidence: what the agent would have
        # done (`recommended_action`) and what the human decided
        # (`human_ruling`). Without both, shared.contracts.DecisionRecord
        # .has_human_ruling is False and the pair is excluded from the trust
        # score entirely — which is why every beat used to report
        # AGREEMENT_EVIDENCE_INSUFFICIENT and WEIGHTS_RENORMALISED.
        #
        # The human is modelled as the reference standard: they rule the way
        # ground truth says. Agreement therefore measures whether the agent's
        # own judgement matches the reviewer's, and it falls in the degraded
        # phase because the agent's advice degrades with it.
        recommended_action = None
        human_ruling = None
        if outcome.action is Action.ESCALATE:
            recommended_action = agent.recommend(invoice)
            human_ruling = invoice.ground_truth_decision

        return DecisionRecord(
            decision_id=f"{self.run_id}-{seq:05d}",
            sequence=seq,
            invoice_id=invoice.invoice_id,
            amount=int(Decimal(invoice.amount)),
            action=outcome.action,
            ground_truth=invoice.ground_truth_decision,
            agent_id=self.agent_id,
            decided_at=None,                 # never read the clock
            recommended_action=recommended_action,
            human_ruling=human_ruling,
        )

    # ------------------------------------------------------------------
    # Act on whatever the ladder decided
    # ------------------------------------------------------------------

    def resolve(self, te) -> str:
        """Apply the ladder's direction. Returns a short outcome string."""
        if te.direction is None:
            return "no direction"

        direction = te.direction.value

        if direction == "CLAWBACK":
            # Automatic — no human, ADR-0004.
            self.limit = te.recommended_limit
            self._n_at_last_change = len(self.records)
            self._n_at_last_clawback = len(self.records)
            self.state = AgentState.RESTRICTED
            return f"CLAWBACK applied automatically -> INR {self.limit}"

        if direction == "INCREASE":
            if not self.auto_approve:
                return (
                    f"INCREASE to INR {te.recommended_limit} PENDING "
                    f"— waiting for a human to approve"
                )
            self.limit = te.recommended_limit
            self._n_at_last_change = len(self.records)
            self.state = AgentState.ACTIVE
            return f"INCREASE approved by human -> INR {self.limit}"

        return f"HOLD (eligible={te.eligible_for_increase})"

    # ------------------------------------------------------------------
    # The whole arc
    # ------------------------------------------------------------------

    def run(self, script: list[Beat] | None = None) -> None:
        script = script or default_script(self.count)

        console.print(
            f"\n[bold]Demo arc[/]  agent={self.agent_id}  seed={self.seed}  "
            f"count/phase={self.count}  auto_approve={self.auto_approve}\n"
        )
        self._print_header()

        for beat in script:
            self.run_phase(beat.phase, beat.count)
            te = evaluate(self.records, self.context())
            outcome = self.resolve(te) if beat.resolve else "-"
            self._print_row(beat, te, outcome)

        console.print(f"\n[bold]Final autonomy limit:[/] INR {self.limit}\n")

        if self.api_client is not None:
            self._print_submission_summary()

    def _print_submission_summary(self) -> None:
        """What actually reached the backend.

        The number that matters is failures: a run that silently drops
        decisions cannot be called reproducible, whatever the arc printed
        above from its own in-memory copy.
        """
        expected = len(self.records)
        escalations = sum(1 for r in self.records if r.human_ruling is not None)
        console.print("[bold]Backend submission[/]")
        console.print(f"  decisions  {self.submitted}/{expected} accepted")
        console.print(f"  rulings    {self.ruled}/{escalations} accepted")
        if self.submit_failures:
            console.print(f"  [red]failures  {len(self.submit_failures)}[/]")
            for failure in self.submit_failures[:10]:
                console.print(f"    - {failure}")
            if len(self.submit_failures) > 10:
                console.print(f"    ... and {len(self.submit_failures) - 10} more")
        else:
            console.print("  [green]failures   0 - nothing dropped[/]")
        console.print()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _print_header() -> None:
        console.print(
            f"{'beat':<34} {'n':>5} {'acted':>6} {'acc':>7} {'wLB':>7} "
            f"{'trust':>6} {'drift':>10} {'dir':>9}  outcome"
        )
        console.print("-" * 118)

    def _print_row(self, beat: Beat, te, outcome: str) -> None:
        acc = f"{te.accuracy.point:.1%}" if te.accuracy and te.accuracy.point is not None else "-"
        wlb = f"{te.accuracy.wilson_lower:.3f}" if te.accuracy else "-"
        console.print(
            f"{beat.label:<34} {te.total_decisions:>5} {te.acted_decisions:>6} "
            f"{acc:>7} {wlb:>7} {te.trust_score:>6.1f} "
            f"{te.drift.severity.value:>10} {te.direction.value:>9}  {outcome}"
        )
        console.print(f"{'':<34} reasons: {', '.join(te.reason_codes) or '-'}")
