"""`audit_log` — docs/lanes/vp.md schema: id, ts, actor, actor_type,
event_type, entity_type, entity_id, payload (JSONB), prev_hash, hash.

Append-only, same enforcement as `policy_versions`
(`app/models/guards.py`'s `before_flush` hook raises on any attempt to modify
or delete an existing row). `entity_type`/`entity_id` are a deliberately
untyped polymorphic reference — this table logs events against decisions,
policy versions, recommendations, audit samples, and more, and adding a real
foreign key per entity type would mean a new column and a new migration every
time a new kind of event needs logging. Verifying the chain (recomputing each
row's `hash` from `prev_hash` and its own `payload` via `app.models.audit_hash
.compute_hash`) is left as a read-side operation for whoever needs it, not
something this model does on every read.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.models.audit_hash import GENESIS_HASH, compute_hash
from app.models.base import Base
from app.models.types import JSONBType

# Arbitrary but fixed key for the audit-log chain's Postgres advisory lock
# (see `_lock_chain_for_append` below). The number itself has no meaning —
# it only needs to be distinct from any other advisory lock this codebase
# might one day take, and to never change, since two processes must agree
# on it to actually serialize against each other.
_AUDIT_LOG_CHAIN_LOCK_KEY = 726_351_408


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    actor_type: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONBType, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String, nullable=False)
    hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # True insertion-order marker, assigned only from inside
    # `_lock_chain_for_append`'s locked section — see that function's
    # docstring and migration 0003 for why `ts` (caller-supplied wall-clock
    # time) cannot safely serve this purpose under concurrency.
    log_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)


def _lock_chain_for_append(session: Session) -> None:
    """Serialize appends so "the latest entry" can never move between the
    read below and the write that follows it
    (docs/audits/2026-09-06-audit.md Race 2: two concurrent appends reading
    the same latest row and both chaining off it, forking the hash chain
    without raising anything — silent, and worse than Race 1 precisely
    because nothing detects it).

    A transaction-scoped Postgres advisory lock (`pg_advisory_xact_lock`),
    not a row lock, because there is no fixed row to lock — the "latest"
    row is a moving target, and `SELECT ... FOR UPDATE ORDER BY ts DESC
    LIMIT 1` does not correctly serialize a query whose result set itself
    changes between attempts (the classic phantom-row problem: a blocked
    transaction resumes locking the row it originally selected, not
    whatever became latest while it waited). A single fixed advisory-lock
    key sidesteps that entirely by making every append acquire the exact
    same lock before doing anything else, which forces appends into a
    strict, unbroken queue — ordering, not merely non-collision, which is
    the property this audit trail actually needs.

    The lock is released automatically when the surrounding transaction
    commits or rolls back, exactly matching `app.deps.get_session`'s
    commit-on-success/rollback-on-exception semantics — no separate
    cleanup needed, and no way to leak the lock past the request that took
    it. No-op outside Postgres: `pg_advisory_xact_lock` doesn't exist on
    SQLite, and the test suite's SQLite fixture never runs concurrent
    requests against the same engine, so there is nothing to serialize
    there.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(select(func.pg_advisory_xact_lock(_AUDIT_LOG_CHAIN_LOCK_KEY)))


def append_entry(
    session: Session,
    *,
    id: str,
    ts: datetime,
    actor: str,
    actor_type: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict,
) -> AuditLogEntry:
    """Append the next row of the chain, hashing it against whatever the
    latest row in `session` currently is (or `GENESIS_HASH` for the first row
    ever). A convenience for tests and the seed script — full ingest wiring
    (appending an entry as part of every mutating request) is separate work.
    """
    _lock_chain_for_append(session)
    # `ORDER BY log_seq DESC`, not `ts DESC`: `ts` is caller-supplied and not
    # guaranteed monotonic with true insertion order under concurrency (see
    # `_lock_chain_for_append`'s docstring and migration 0003) — `log_seq` is
    # assigned below, from inside this same locked section, specifically so
    # this read is always correct.
    previous = (
        session.execute(select(AuditLogEntry).order_by(AuditLogEntry.log_seq.desc()))
        .scalars()
        .first()
    )
    prev_hash = previous.hash if previous is not None else GENESIS_HASH
    next_log_seq = (previous.log_seq if previous is not None else 0) + 1
    entry = AuditLogEntry(
        id=id,
        ts=ts,
        actor=actor,
        actor_type=actor_type,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload,
        prev_hash=prev_hash,
        hash=compute_hash(prev_hash, payload),
        log_seq=next_log_seq,
    )
    session.add(entry)
    return entry
