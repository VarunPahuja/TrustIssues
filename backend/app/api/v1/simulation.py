"""Simulation-run resource — start a batch, check its status."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.deps import CurrentUserDep, DbSessionDep, require_role
from app.errors import FORBIDDEN_RESPONSE, NOT_FOUND_RESPONSE, not_found
from app.models import Agent
from app.models.audit_log import append_entry
from app.models.simulation_runs import SimulationRun
from app.schemas.simulation import RunStatus, SimulationRunCreate, SimulationRunOut
from app.schemas.user import Role
from app.services.simulation import execute_simulation_run

router = APIRouter(prefix="/simulation", tags=["simulation"])

# Starting a run is an operational action, not a governance authorization, but
# it still changes state, so it's still gated (ADMIN operates the system).
_admin_only = Depends(require_role(Role.ADMIN))


def _run_out(run: SimulationRun) -> SimulationRunOut:
    return SimulationRunOut(
        run_id=run.id,
        status=run.status,
        phase=run.phase,
        agent_id=run.agent_id,
        invoice_count=run.invoice_count,
        seed=run.seed,
        started_at=run.started_at,
        completed_at=run.completed_at,
        decisions_submitted=run.decisions_submitted,
        accuracy=run.accuracy,
        wilson_lower_bound=run.wilson_lower_bound,
        error_message=run.error_message,
        clawback_applied=run.clawback_applied,
        clawback_limit=run.clawback_limit,
    )


@router.post(
    "/runs",
    response_model=SimulationRunOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_admin_only],
    responses=FORBIDDEN_RESPONSE | NOT_FOUND_RESPONSE,
)
def start_simulation_run(
    body: SimulationRunCreate,
    background_tasks: BackgroundTasks,
    user: CurrentUserDep,
    db: DbSessionDep,
) -> SimulationRunOut:
    """Start a real simulation run.

    Creates the run row with `status=running`, then schedules
    `app.services.simulation.execute_simulation_run` as a `BackgroundTasks`
    job: it generates `invoice_count` synthetic invoices for `phase` from
    `seed`, runs a scripted agent over them, and submits every resulting
    decision through the same ingest path `POST /api/v1/decisions` itself
    uses (`app.api.v1.decisions._create_decision`, called directly and
    in-process — no self-HTTP-call, same function either way, so this is
    not a shortcut into the database).

    The run row is committed here, explicitly, before the background task
    is scheduled — deliberately not left to `DbSessionDep`'s usual
    commit-at-end-of-request — so the background task's own session is
    guaranteed to find the row already durable, regardless of exactly how
    FastAPI orders background-task execution relative to a dependency's
    post-`yield` teardown code.
    """
    agent = db.get(Agent, body.agent_id)
    if agent is None:
        raise not_found(
            "agent_not_found", f"No agent {body.agent_id!r}.", {"agent_id": body.agent_id}
        )

    started_at = datetime.now(UTC)
    run = SimulationRun(
        id=f"run-{uuid.uuid4().hex[:8]}",
        status=RunStatus.RUNNING,
        phase=body.phase,
        agent_id=body.agent_id,
        invoice_count=body.invoice_count,
        seed=body.seed,
        reason=body.reason,
        started_at=started_at,
        completed_at=None,
        decisions_submitted=0,
        accuracy=None,
        wilson_lower_bound=None,
        error_message=None,
    )
    db.add(run)
    append_entry(
        db,
        id=f"log-{uuid.uuid4().hex[:12]}",
        ts=started_at,
        actor=user.user_id,
        actor_type="user",
        event_type="simulation_run.started",
        entity_type="simulation_run",
        entity_id=run.id,
        payload={
            "phase": body.phase.value,
            "agent_id": body.agent_id,
            "invoice_count": body.invoice_count,
            "seed": body.seed,
            "reason": body.reason,
        },
    )
    db.commit()

    # `db.get_bind()`, not the process-wide default engine: see
    # `execute_simulation_run`'s docstring for why the background task must
    # use the same bind this request's own session did.
    background_tasks.add_task(execute_simulation_run, run.id, db.get_bind())
    return _run_out(run)


@router.get("/runs/{run_id}", response_model=SimulationRunOut, responses=NOT_FOUND_RESPONSE)
def get_simulation_run(run_id: str, user: CurrentUserDep, db: DbSessionDep) -> SimulationRunOut:
    """Poll a run's status and, once complete, its summary accuracy/Wilson
    lower bound over the decisions it submitted. `decisions_submitted`
    updates after every decision while the run is still in progress.
    """
    run = db.get(SimulationRun, run_id)
    if run is None:
        raise not_found(
            "simulation_run_not_found", f"No simulation run {run_id!r}.", {"run_id": run_id}
        )
    return _run_out(run)
