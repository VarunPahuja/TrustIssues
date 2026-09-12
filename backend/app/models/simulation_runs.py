"""`simulation_runs` — one row per `POST /api/v1/simulation/runs`.

Unlike `decisions`/`policy_versions`, this table is not append-only in the
"never update" sense: the same row transitions `RUNNING -> COMPLETED` (or
`RUNNING -> FAILED`) as `app.services.simulation.execute_simulation_run`'s
background task makes progress, and `decisions_submitted` is updated after
every decision so `GET /api/v1/simulation/runs/{id}` reflects live progress
while it runs, not just the final summary. Nothing on this row changes after
it reaches `COMPLETED`/`FAILED`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import enum_column
from app.schemas.simulation import RunStatus, SimulationPhase


class SimulationRun(Base):
    __tablename__ = "simulation_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[RunStatus] = mapped_column(enum_column(RunStatus), nullable=False)
    phase: Mapped[SimulationPhase] = mapped_column(enum_column(SimulationPhase), nullable=False)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    invoice_count: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decisions_submitted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    wilson_lower_bound: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Populated only on a failed run — "record the failure on the run rather
    # than silently continuing with a short count" (this branch's own brief).
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set by `app.services.simulation._evaluate_and_maybe_clawback`, at the end
    # of the run, after the last decision commits. `clawback_applied` is False
    # for every run that didn't warrant one, that hit the cascade guard (no
    # new evidence since the last clawback), or that was already at
    # AUTONOMY_FLOOR — "the direction said CLAWBACK" and "this run's evidence
    # actually moved the agent's limit" are different questions, and this
    # column answers the second one. `clawback_limit` is the resulting limit,
    # populated only when `clawback_applied` is True.
    clawback_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    clawback_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
