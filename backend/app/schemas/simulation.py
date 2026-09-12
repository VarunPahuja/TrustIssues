"""Simulation-run response and request models.

No `shared/` equivalent — `simulator/simulator/models.py`'s
`SimulationRunConfig`/`SimulationRunResult` are simulator-local, not
treaty types, so this is a backend-local resource that records *that* a
run happened and summarizes its outcome, not the simulator's own internal
shape. Endpoint check (docs/lanes/vp.md): the simulator posts a decision,
the backend persists it — starting a run and reading its status back is
the operational surface around that, not a mirror of anything in
`shared/`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class SimulationPhase(str, Enum):
    """Mirrors `simulator.simulator.models.SimulationPhase` by value, not by
    import — `simulator/` is a lane the backend must not depend on
    (docs/lanes/vp.md: "Do not write code outside backend/"; the simulator
    submits to this API, this API does not reach into the simulator)."""

    GOOD = "good"
    DEGRADED = "degraded"
    RECOVERY = "recovery"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SimulationRunCreate(BaseModel):
    """Request body for `POST /api/v1/simulation/runs`.

    Only ADMIN may start a run (`app/deps.py`) — this is an operational
    action, not a governance authorization, but it still changes state
    (kicks off a batch of decisions), so `reason` is still required.
    """

    phase: SimulationPhase
    agent_id: str
    invoice_count: int = Field(gt=0, le=10_000)
    seed: int = 42
    reason: str = Field(min_length=1, description="Why this run is being started")


class SimulationRunOut(BaseModel):
    """The status/result of one simulation run."""

    model_config = ConfigDict(from_attributes=True)

    run_id: str
    status: RunStatus
    phase: SimulationPhase
    agent_id: str
    invoice_count: int
    seed: int
    started_at: datetime | None
    completed_at: datetime | None
    decisions_submitted: int
    accuracy: float | None
    wilson_lower_bound: float | None
    # Populated only when status is FAILED — the "record the failure on the
    # run" half of this endpoint's brief. None on every other status.
    error_message: str | None = None
    # Set once, at the end of the run, by
    # `app.services.simulation._evaluate_and_maybe_clawback` — whether this
    # run's own evidence caused a clawback, and to what limit. False/None on
    # a run that didn't warrant one, that hit the cascade guard, or that was
    # already at the floor: "the direction was CLAWBACK" and "this run's
    # evidence actually changed the agent's limit" are different questions,
    # and these two fields answer the second one.
    clawback_applied: bool = False
    clawback_limit: int | None = None
