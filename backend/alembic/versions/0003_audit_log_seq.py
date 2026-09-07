"""Add audit_log.log_seq — a true insertion-order marker for the hash chain.

`append_entry` used to find "the latest entry" via `ORDER BY ts DESC LIMIT 1`.
`ts` is caller-supplied wall-clock time, not guaranteed monotonic with actual
commit order under concurrency: two appends serialized by
`_lock_chain_for_append`'s advisory lock (docs/audits/2026-09-06-audit.md
Race 2) could still each read a *different* row as "latest" if their `ts`
values happened to land out of true insertion order, which reintroduced the
fork the lock was supposed to prevent — caught by
`backend/tests/test_race_conditions.py::test_concurrent_audit_appends_produce_no_fork`
before this migration existed.

`log_seq` is assigned as `previous.log_seq + 1` from *inside* the same
advisory-lock-protected section that reads "the latest entry" — so, unlike
`ts`, it is guaranteed to reflect true serialized insertion order and nothing
else. Existing rows are backfilled from their current `ts` order: every row
inserted before this migration came from the seed script running
single-threaded, so `ts` order is a safe proxy for real insertion order for
this one-time backfill only — nothing after this migration relies on `ts`
for ordering again.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.add_column(sa.Column("log_seq", sa.BigInteger(), nullable=True))

    conn = op.get_bind()
    audit_log = sa.table(
        "audit_log",
        sa.column("id", sa.String),
        sa.column("ts", sa.DateTime),
        sa.column("log_seq", sa.BigInteger),
    )
    existing_rows = conn.execute(sa.select(audit_log.c.id).order_by(audit_log.c.ts)).fetchall()
    for seq, (row_id,) in enumerate(existing_rows, start=1):
        conn.execute(audit_log.update().where(audit_log.c.id == row_id).values(log_seq=seq))

    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.alter_column("log_seq", nullable=False)
        batch_op.create_unique_constraint("uq_audit_log_log_seq", ["log_seq"])


def downgrade() -> None:
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.drop_constraint("uq_audit_log_log_seq", type_="unique")
        batch_op.drop_column("log_seq")
