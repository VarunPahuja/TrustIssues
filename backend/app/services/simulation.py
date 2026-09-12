"""Executing a simulation run: generate invoices, run a scripted agent over
them, and submit every resulting decision through the real ingest path
(`app.api.v1.decisions._create_decision` — never a shortcut into the
database) — recording progress and a final summary (or a recorded failure)
on the `simulation_runs` row as it goes.

Also where a clawback actually gets triggered end to end. Before this branch,
nothing in the decision-ingest path ever evaluated an agent — a clawback only
happened if a human opened the dashboard or something called
`POST /agents/{id}/recommendations` directly, so a degrading agent kept its
ceiling indefinitely otherwise (confirmed live: 400 degrading decisions left
`GET /agents/{id}/trust` reporting `direction=CLAWBACK` while
`current_limit` sat unchanged). `execute_simulation_run` now evaluates the
agent once, at the end of the run, and applies a clawback if the evidence
says so — see `_evaluate_and_maybe_clawback`'s docstring for the full
reasoning, including why this happens once per run and not once per decision.

This is also, deliberately, the only place a real deployment's own trigger
(a schedule, or ingest itself with proper rate limiting) is not yet built —
see that same docstring's note on the tradeoff this prototype made instead.

Why this does not import `simulator/`
--------------------------------------
`app/schemas/simulation.py` already establishes the rule for this exact pair
of lanes: "`SimulationPhase` mirrors `simulator.simulator.models.
SimulationPhase` by value, not by import... the simulator submits to this
API, this API does not reach into the simulator." Two more reasons specific
to this endpoint, found while reading `simulator/` before writing this file:

1. As of this writing, `simulator/simulator/models.py`'s `Invoice.invoice_id`
   defaults to a bare, unseeded `uuid4()` — a different id on every call,
   seed or no seed. Importing that model as-is would make this endpoint
   non-deterministic by construction, which is a hard requirement here
   ("same seed, same run, every time"). Fixing that bug is the simulator
   lane's own reproducibility work in flight elsewhere
   (docs/audits/2026-09-06-audit.md, PR #30 on `uk/simulator-finalise`,
   unmerged as of this branch) — this branch doesn't reach into another
   lane's open work to patch it, it just doesn't depend on the unfixed
   version.
2. `simulator/`'s CLI and runner pull in `typer`, `rich`, and their own
   `httpx.Client` — none of which the backend's runtime otherwise needs.

What this module *does* reuse from another lane is
`trust_engine.stats.wilson.wilson_lower_bound` for the run summary — not a
new coupling: `app/services/trust.py` already imports
`trust_engine.evaluate` for exactly this reason (a pure statistics library
the backend is meant to call, unlike `simulator/`, which is meant to call
the backend).
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from shared.enums import Action, Direction
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from trust_engine.stats.wilson import wilson_lower_bound

from app.models import Agent
from app.models.audit_log import append_entry
from app.models.simulation_runs import SimulationRun
from app.schemas.decision import DecisionCreate
from app.schemas.simulation import RunStatus, SimulationPhase

# Per-decision probabilities, deliberately independent of each other rather
# than derived from one combined "error rate" — degraded phase needs a
# meaningfully elevated *critical*-error rate specifically (an APPROVE whose
# ground truth is REJECT — shared.constants.CRITICAL_ERROR_DEFINITION) for
# drift detection and clawback to actually fire within a realistic window
# (trust/trust_engine/constants.py's CRITICAL_ERROR_WINDOW=20 acted
# decisions); good/recovery phases need that rate low while still allowing
# some ordinary (non-critical) mistakes.
_PHASE_PARAMS: dict[SimulationPhase, dict[str, float]] = {
    SimulationPhase.GOOD: {
        "p_ground_truth_reject": 0.20,
        "p_critical_error": 0.02,
        "p_noncritical_error": 0.06,
    },
    SimulationPhase.DEGRADED: {
        "p_ground_truth_reject": 0.20,
        "p_critical_error": 0.12,
        "p_noncritical_error": 0.15,
    },
    SimulationPhase.RECOVERY: {
        "p_ground_truth_reject": 0.20,
        "p_critical_error": 0.03,
        "p_noncritical_error": 0.08,
    },
}


class PlannedDecision:
    """One invoice's amount, ground truth, and the scripted agent's action —
    everything `DecisionCreate` needs except `agent_id`/`reason`, which the
    caller already knows."""

    __slots__ = ("action", "amount", "ground_truth", "invoice_id")

    def __init__(self, invoice_id: str, amount: int, action: Action, ground_truth: Action) -> None:
        self.invoice_id = invoice_id
        self.amount = amount
        self.action = action
        self.ground_truth = ground_truth


def generate_decision_plan(
    *,
    agent_id: str,
    phase: SimulationPhase,
    seed: int,
    count: int,
    current_limit: int,
) -> list[PlannedDecision]:
    """Pure and deterministic: the same (agent_id, phase, seed, count,
    current_limit) always produces the same list, in the same order —
    nothing here reads a clock, mints a `uuid4()`, or otherwise draws on
    anything but the seeded `random.Random` stream below.

    Seeded with a composite *string*, not the bare `seed` int, so two
    different agents (or the same agent run twice under different phases)
    given the same numeric seed don't produce the same invoices as each
    other — only a genuine repeat of the same (agent, phase, seed) does.

    `invoice_id`s are likewise deterministic and namespaced by
    (agent_id, phase, seed, index): a second run with the exact same
    (agent, phase, seed) reuses the same invoice ids on purpose, matching
    `_create_decision`'s own "an invoice is a fact recorded once" rule
    rather than fighting it — the invoices really are the same synthetic
    invoices being reprocessed, not new ones that happen to collide.
    """
    rng = random.Random(f"{agent_id}:{phase.value}:{seed}")
    params = _PHASE_PARAMS[phase]
    lo = 10
    hi = max(current_limit * 2, 1000)

    plan: list[PlannedDecision] = []
    for i in range(count):
        invoice_id = f"sim-{agent_id}-{phase.value}-{seed}-{i:05d}"
        amount = rng.randint(lo, hi)

        if rng.random() < params["p_ground_truth_reject"]:
            ground_truth = Action.REJECT
            # The only way to be "wrong" about a REJECT-worthy invoice with
            # just two possible actions is to APPROVE it — the critical
            # error by definition.
            action = Action.APPROVE if rng.random() < params["p_critical_error"] else Action.REJECT
        else:
            ground_truth = Action.APPROVE
            action = Action.REJECT if rng.random() < params["p_noncritical_error"] else Action.APPROVE

        plan.append(PlannedDecision(invoice_id, amount, action, ground_truth))

    return plan


@dataclass(frozen=True, slots=True)
class ClawbackOutcome:
    """What `_evaluate_and_maybe_clawback` found and did, for the run row to record."""

    applied: bool
    limit: int | None


def _evaluate_and_maybe_clawback(engine: Engine, agent_id: str) -> ClawbackOutcome:
    """Evaluate `agent_id` against its real, just-updated decision history and
    apply a clawback if the evidence says so — the fix for the gap this branch
    exists to close: nothing in the decision-ingest path triggered a clawback,
    so a degrading agent kept its ceiling until a human opened the dashboard or
    something called `POST /agents/{id}/recommendations` by hand.

    **Reuses `app.services.governance.generate_recommendation` — the only
    place a clawback is ever applied, and the only place the cascade guard
    (do not re-apply a clawback for evidence already acted on) lives.** This
    function does not decide whether to claw back or re-implement any part of
    that decision; it only decides *whether the panel is worth convening at
    all*, via a cheap, side-effect-free peek at the direction
    (`app.services.trust.compute_trust_evaluation`, the same function
    `compute_and_persist_trust_evaluation` itself calls — nothing new). Two
    separate sessions, deliberately: the peek must never persist anything
    (`compute_trust_evaluation` doesn't, but using a session we control and
    close either way makes that an invariant of this function, not an
    assumption about what it's called with), and the real attempt gets its own
    session so a failure inside it (see below) rolls back cleanly without
    touching whatever `execute_simulation_run` does with the run row
    afterwards.

    **Why HOLD and INCREASE never reach `generate_recommendation` here.** For
    a CLAWBACK, `generate_recommendation` both evaluates *and* applies in one
    call — that's the function's job. But for HOLD/INCREASE it *also*
    persists a new PENDING `Recommendation` row, and an increase still needs a
    human either way (ADR-0004) — auto-generating one at the end of every
    ordinary simulation run would fill the Approvals queue with a row for
    every run, actionable or not, for no benefit: nobody is waiting on an
    end-of-run INCREASE the way they would be on a real emerging trend caught
    by a scheduled evaluation. A production deployment evaluating on a
    schedule (or on ingest) might reasonably decide that differently; a
    demo/prototype whose only trigger point is "a batch of decisions just
    finished" should not turn every finished batch into approvals-queue noise.
    Skipping the call entirely when the peek says HOLD/INCREASE is what keeps
    that noise out without adding a second place that knows what a
    recommendation is for.

    Returns `ClawbackOutcome(applied=False, limit=None)` for HOLD/INCREASE,
    for a no-op clawback (already at the floor, or the cascade guard caught a
    repeat), and for a governance-layer failure while attempting one (the
    decisions this run already submitted are real and already committed;
    a governance hiccup at the very last step must not make the caller think
    the whole run failed). `applied=True` only when the agent's own
    `current_limit` demonstrably moved as a result of this call — not merely
    "governance's response claimed CLAWBACK," which is also and identically
    true of the cascade guard's own no-op replay of a past recommendation.
    """
    from app.errors import ApiError  # deferred: see module docstring's read order
    from app.services.governance import generate_recommendation
    from app.services.trust import compute_trust_evaluation

    peek_session = Session(engine)
    try:
        agent = peek_session.get(Agent, agent_id)
        peek_evaluation = compute_trust_evaluation(peek_session, agent)
    finally:
        peek_session.close()

    if peek_evaluation.direction is not Direction.CLAWBACK:
        return ClawbackOutcome(applied=False, limit=None)

    session = Session(engine)
    try:
        agent = session.get(Agent, agent_id)
        limit_before = agent.current_limit
        try:
            recommendation = generate_recommendation(session, agent)
            session.commit()
        except ApiError:
            # Governance unavailable at the last step of an otherwise-successful
            # run. The decisions already submitted are real and already
            # committed in their own transactions; losing the clawback attempt
            # must not turn a completed run into a failed one.
            session.rollback()
            return ClawbackOutcome(applied=False, limit=None)
    finally:
        session.close()

    if recommendation.proposed_limit == limit_before:
        # Either the cascade guard returned the same recommendation it did
        # last time (no new policy version written), or the agent was already
        # at AUTONOMY_FLOOR and stayed there (governance.py's own floor no-op —
        # PR #34). Either way, nothing about this agent's actual authority
        # changed because of this call.
        return ClawbackOutcome(applied=False, limit=None)
    return ClawbackOutcome(applied=True, limit=recommendation.proposed_limit)


def execute_simulation_run(run_id: str, engine: Engine) -> None:
    """The `BackgroundTasks` entry point. Runs after the `POST` response has
    already been sent, on its own thread (Starlette runs a synchronous
    background callable via `run_in_threadpool`) and its own `Session`s —
    never the request's, which is already closed by the time this executes.

    `engine` is the exact bind the triggering request's own `db: DbSessionDep`
    was using (`app.api.v1.simulation.start_simulation_run` passes
    `db.get_bind()`), not `app.deps`'s process-wide default engine. That
    default is built lazily and cached at module-global scope on first use,
    and every test in this suite replaces `get_session` via
    `app.dependency_overrides` to point at a throwaway per-test database —
    bypassing that override by reaching for the global default here would
    silently point every simulation run at the wrong database in tests (and
    at a stale one if `DATABASE_URL` ever changed between processes sharing
    one import of this module). Taking the engine as a parameter avoids that
    entirely: this function always talks to whatever database the request
    that started it was actually using.

    Every decision is submitted as its own transaction, through
    `app.api.v1.decisions._create_decision` — the exact function
    `POST /api/v1/decisions` itself calls — so, to the rest of the system, a
    simulation run looks identical to a real client submitting one decision
    at a time. `_create_decision` already serializes correctly per agent
    (the agent-row lock from PR #31's Race 1 fix); nothing here adds any
    locking or retry logic of its own, and nothing here needs to: each
    decision waits for the previous one's transaction to fully commit before
    the next one starts, so there is no concurrent access to serialize in
    the first place.

    A failure on any one decision stops the run there — the run is marked
    FAILED with however many decisions actually succeeded and why, rather
    than either silently reporting a short "completed" count with no
    explanation, or continuing to submit decisions against whatever broke.
    """
    from app.api.v1.decisions import _create_decision  # deferred: see module docstring's read order

    plan_session = Session(engine)
    try:
        run = plan_session.get(SimulationRun, run_id)
        if run is None:
            return  # the row is committed before this task is ever scheduled; not reachable in practice
        agent = plan_session.get(Agent, run.agent_id)
        phase = run.phase
        seed = run.seed
        invoice_count = run.invoice_count
        agent_id = run.agent_id
        current_limit = agent.current_limit
    finally:
        plan_session.close()

    plan = generate_decision_plan(
        agent_id=agent_id,
        phase=phase,
        seed=seed,
        count=invoice_count,
        current_limit=current_limit,
    )

    submitted = 0
    correct = 0
    failure: str | None = None

    for planned in plan:
        decision_session = Session(engine)
        try:
            body = DecisionCreate(
                invoice_id=planned.invoice_id,
                amount=planned.amount,
                action=planned.action,
                ground_truth=planned.ground_truth,
                agent_id=agent_id,
                reason=f"simulation run {run_id} ({phase.value}, seed {seed})",
            )
            _create_decision(decision_session, body)
            decision_session.commit()
        except Exception as exc:  # noqa: BLE001 — recorded on the run, not swallowed
            decision_session.rollback()
            failure = f"{type(exc).__name__}: {exc}"
            decision_session.close()
            break
        else:
            decision_session.close()

        submitted += 1
        if planned.action == planned.ground_truth:
            correct += 1

        # Visible to a poller immediately, not just once the whole run ends.
        progress_session = Session(engine)
        try:
            progress_row = progress_session.get(SimulationRun, run_id)
            progress_row.decisions_submitted = submitted
            progress_session.commit()
        finally:
            progress_session.close()

    accuracy = (correct / submitted) if submitted else None
    wilson = wilson_lower_bound(correct, submitted) if submitted else None

    # Right here: after the last decision this run will ever submit is
    # committed, before the run's own status is written as terminal — see
    # `_evaluate_and_maybe_clawback`'s docstring for what this does and does
    # not do. Guarded on `submitted > 0`: a run that submitted nothing (failed
    # on its very first decision) added no new evidence, so there is nothing
    # to re-evaluate.
    #
    # Deliberately NOT called from decision-ingest (`app/api/v1/decisions.py`)
    # on every single decision instead of once per run. That would mean a
    # full trust evaluation — `load_decision_records` plus `trust_engine.
    # evaluate` over the agent's entire history — on every write, and a
    # simulation run is exactly the workload that makes that cost visible:
    # 200 decisions would mean 200 evaluations of a history that long or
    # longer, for a result that only needs checking once the batch is done.
    # If a future caller is tempted to move this into ingest for faster
    # reaction time, the honest fix is a scheduled or ingest-triggered
    # evaluation with its own rate limiting, not deleting this comment.
    clawback = ClawbackOutcome(applied=False, limit=None)
    clawback_error: str | None = None
    if submitted > 0:
        try:
            clawback = _evaluate_and_maybe_clawback(engine, agent_id)
        except Exception as exc:  # noqa: BLE001 — recorded, never left to strand the run
            # Deliberately does not set `failure`: the decisions this run
            # submitted are real and already committed, so an unexpected bug
            # in the clawback step (distinct from the known, already-handled
            # governance-unavailable case inside `_evaluate_and_maybe_clawback`)
            # must not turn an otherwise-successful run into a FAILED one —
            # the same reasoning that function's own docstring gives for its
            # ApiError branch, extended to a failure mode it didn't anticipate.
            # Visible instead in this run's own audit entry below, so it is
            # noticed rather than silently absorbed.
            clawback_error = f"{type(exc).__name__}: {exc}"

    completed_at = datetime.now(UTC)

    final_session = Session(engine)
    try:
        final_row = final_session.get(SimulationRun, run_id)
        final_row.decisions_submitted = submitted
        final_row.completed_at = completed_at
        final_row.accuracy = accuracy
        final_row.wilson_lower_bound = wilson
        final_row.clawback_applied = clawback.applied
        final_row.clawback_limit = clawback.limit
        if failure is not None:
            final_row.status = RunStatus.FAILED
            final_row.error_message = failure
        else:
            final_row.status = RunStatus.COMPLETED
        append_entry(
            final_session,
            id=f"log-{uuid.uuid4().hex[:12]}",
            ts=completed_at,
            actor="system",
            actor_type="system",
            event_type="simulation_run.failed" if failure is not None else "simulation_run.completed",
            entity_type="simulation_run",
            entity_id=run_id,
            payload={
                "phase": phase.value,
                "seed": seed,
                "requested_count": invoice_count,
                "decisions_submitted": submitted,
                "clawback_applied": clawback.applied,
                "clawback_limit": clawback.limit,
                "accuracy": accuracy,
                "wilson_lower_bound": wilson,
                "error": failure,
                "clawback_evaluation_error": clawback_error,
            },
        )
        final_session.commit()
    finally:
        final_session.close()
