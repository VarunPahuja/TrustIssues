"""Concurrency regression tests for the two races in
docs/audits/2026-09-06-audit.md:

  Race 1 — `_create_decision`'s unlocked `max(sequence)+1` read-then-insert
  (`app/api/v1/decisions.py`), fixed by locking the agent row.
  Race 2 — `append_entry`'s unlocked read-then-append of the audit-log hash
  chain (`app/models/audit_log.py`), fixed by a Postgres advisory lock.

Both races are invisible on SQLite, which is what the rest of this test
suite runs against: SQLite has no real row-level locking and serializes an
entire file's writers regardless, so it cannot reproduce either bug and
cannot prove either fix. These tests stand up a dedicated, disposable
Postgres database instead — never the shared dev `aagp` database — so
sequence-gap and hash-chain-linearity assertions aren't polluted by
unrelated data, and are skipped outright if no Postgres instance is
reachable (`docker compose up -d db`), the same way `trust/tests/test_wilson.py`
skips its `statsmodels` cross-validation when that optional dependency isn't
installed.

Real concurrency, not sequential calls: both tests use a
`ThreadPoolExecutor`, and Starlette runs each `def` (sync) endpoint handler
in its own worker thread via `run_in_threadpool`, so concurrent `TestClient`
calls genuinely overlap at the database level — this was confirmed directly
by running both tests against the pre-fix code and observing them fail
(see the PR description for the exact before/after numbers).
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path

import psycopg2
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.deps import get_session, session_dependency_factory
from app.main import app
from app.models import Decision
from app.models.audit_log import GENESIS_HASH, AuditLogEntry, append_entry
from app.seed import seed

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _BACKEND_DIR / "alembic.ini"

_ADMIN_DATABASE_URL = os.environ.get(
    "RACE_TEST_ADMIN_DATABASE_URL",
    "postgresql://aagp:aagp_dev_password@localhost:5432/aagp",
)
_TEST_DB_NAME = "aagp_race_test"


def _postgres_reachable() -> bool:
    try:
        conn = psycopg2.connect(_ADMIN_DATABASE_URL, connect_timeout=2)
    except psycopg2.Error:
        return False
    conn.close()
    return True


requires_postgres = pytest.mark.skipif(
    not _postgres_reachable(),
    reason=(
        "Race-condition tests need a real Postgres instance — "
        "`docker compose up -d db` — since SQLite has no real row locking "
        "and cannot reproduce either race."
    ),
)


@pytest.fixture()
def postgres_engine(monkeypatch):
    """A freshly created, migrated, seeded Postgres database dedicated to
    this test module — dropped and recreated every run so sequence/chain
    assertions start from a known, clean state, and dropped again on
    teardown so nothing lingers between test sessions.
    """
    admin_conn = psycopg2.connect(_ADMIN_DATABASE_URL, connect_timeout=5)
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
    admin_conn.close()

    test_url = _ADMIN_DATABASE_URL.rsplit("/", 1)[0] + f"/{_TEST_DB_NAME}"
    monkeypatch.setenv("DATABASE_URL", test_url)
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")

    engine = create_engine(test_url)
    with Session(engine) as session:
        seed(session)
        session.commit()

    try:
        yield engine
    finally:
        engine.dispose()
        # Best-effort cleanup: never let a teardown failure here mask the
        # test's own pass/fail result.
        with contextlib.suppress(psycopg2.Error):
            admin_conn = psycopg2.connect(_ADMIN_DATABASE_URL, connect_timeout=5)
            admin_conn.autocommit = True
            with admin_conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}"')
            admin_conn.close()


@pytest.fixture()
def postgres_client(postgres_engine) -> TestClient:
    session_maker = sessionmaker(bind=postgres_engine)
    app.dependency_overrides[get_session] = session_dependency_factory(session_maker)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


@requires_postgres
def test_concurrent_decision_ingest_has_no_sequence_race(postgres_client, postgres_engine):
    """Race 1. 500 decisions for the same agent, fired with real
    concurrency. Must all succeed, and the resulting sequence numbers for
    that agent must be contiguous and duplicate-free — a gap means some
    inserts silently failed elsewhere; a duplicate means the lock didn't
    hold; a non-2xx response means the unlocked read-then-insert raced and
    the caller got a bare 500, exactly as reproduced live in
    docs/audits/2026-09-06-audit.md section 1a.
    """
    agent_id = "agent-01"
    n = 500

    def submit(i: int):
        body = {
            "invoice_id": f"race-inv-{i}",
            "amount": 10,
            "action": "APPROVE",
            "ground_truth": "APPROVE",
            "agent_id": agent_id,
            "reason": f"race-condition regression test decision {i}",
        }
        return postgres_client.post("/api/v1/decisions", json=body)

    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
        responses = list(pool.map(submit, range(n)))

    statuses = [r.status_code for r in responses]
    non_2xx = [s for s in statuses if not (200 <= s < 300)]
    assert not non_2xx, (
        f"{len(non_2xx)} of {n} concurrent decisions failed "
        f"(sample statuses: {non_2xx[:10]}) — the sequence race reproduced"
    )

    with Session(postgres_engine) as session:
        sequences = sorted(
            session.execute(
                select(Decision.sequence).where(Decision.agent_id == agent_id)
            )
            .scalars()
            .all()
        )

    assert len(sequences) == len(set(sequences)), (
        "duplicate sequence numbers were persisted for the same agent — "
        "the row lock did not serialize concurrent inserts"
    )
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences))), (
        f"gap in agent {agent_id!r}'s decision sequence: {sequences}"
    )


@requires_postgres
def test_concurrent_audit_appends_produce_no_fork(postgres_engine):
    """Race 2. Many threads, each with its own `Session`, appending to the
    audit log concurrently. The chain must remain a single unbroken line
    from `GENESIS_HASH` through every row (seed rows included) — no two
    rows may share a `prev_hash` (a fork), and every row must be reachable
    by walking the chain forward exactly once, matching the total row
    count. Reproduced live as a real, silent fork (98 duplicate `prev_hash`
    groups) in docs/audits/2026-09-06-audit.md section 1b before this fix.
    """
    n = 60

    def append_one(i: int) -> None:
        with Session(postgres_engine) as session:
            append_entry(
                session,
                id=f"race-log-{i}",
                ts=datetime.now(UTC),
                actor="race-condition-test",
                actor_type="test",
                event_type="race.test",
                entity_type="test",
                entity_id=f"race-{i}",
                payload={"i": i},
            )
            session.commit()

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(append_one, range(n)))

    with Session(postgres_engine) as session:
        rows = session.execute(select(AuditLogEntry)).scalars().all()

    prev_hashes = [row.prev_hash for row in rows]
    assert len(prev_hashes) == len(set(prev_hashes)), (
        f"hash chain forked: {len(prev_hashes) - len(set(prev_hashes))} "
        "duplicate prev_hash value(s) found — two appends chained off the "
        "same predecessor"
    )

    # Non-duplication alone isn't sufficient — walk the chain from genesis
    # and confirm every single row is reachable in exactly one place,
    # proving full linearity rather than merely the absence of an exact
    # collision.
    by_prev_hash = {row.prev_hash: row for row in rows}
    assert len(by_prev_hash) == len(rows), "prev_hash collision hidden behind dict collapse"

    cursor = GENESIS_HASH
    reached = 0
    while cursor in by_prev_hash:
        row = by_prev_hash.pop(cursor)
        cursor = row.hash
        reached += 1

    assert reached == len(rows), (
        f"chain is not fully linear from GENESIS_HASH: reached {reached} "
        f"of {len(rows)} rows before the trail broke"
    )
