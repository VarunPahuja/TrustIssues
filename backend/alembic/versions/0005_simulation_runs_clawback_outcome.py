"""Add simulation_runs.clawback_applied / clawback_limit.

`app.services.simulation._evaluate_and_maybe_clawback` now evaluates the
agent at the end of every simulation run and applies a clawback if the
evidence says so (vp/clawback-trigger) — these two columns are what a caller
polling `GET /api/v1/simulation/runs/{id}` reads to see whether that
happened, and to what limit.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default only at add-time, so any pre-existing row lands on
    # `false` rather than failing NOT NULL; dropped immediately after so every
    # new insert must supply it explicitly, same pattern 0002 uses for
    # recommendations.governance_mode. Batch mode for the same reason: SQLite
    # (tests) has no ALTER COLUMN at all.
    with op.batch_alter_table("simulation_runs") as batch_op:
        batch_op.add_column(
            sa.Column("clawback_applied", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("clawback_limit", sa.Integer(), nullable=True))
    with op.batch_alter_table("simulation_runs") as batch_op:
        batch_op.alter_column("clawback_applied", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("simulation_runs") as batch_op:
        batch_op.drop_column("clawback_limit")
        batch_op.drop_column("clawback_applied")
