"""Add simulation_runs — real persistence for POST /api/v1/simulation/runs.

Replaces the in-memory `app.fixtures.simulation.SIMULATION_RUNS` stub
(docs/audits/2026-09-06-audit.md section 2: "POST /simulation/runs... never
persists it... every run_id returned by POST 404s on the very next poll").
One row per run, updated in place as
`app.services.simulation.execute_simulation_run`'s background task makes
progress — the only table in this schema updated after insert rather than
being append-only, since a run's own status/progress is what's changing, not
a governance decision that needs a permanent history.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "simulation_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "running", "completed", "failed", name="runstatus", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.Enum(
                "good", "degraded", "recovery", name="simulationphase", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("invoice_count", sa.Integer(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decisions_submitted", sa.Integer(), nullable=False),
        sa.Column("accuracy", sa.Float(), nullable=True),
        sa.Column("wilson_lower_bound", sa.Float(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("simulation_runs")
