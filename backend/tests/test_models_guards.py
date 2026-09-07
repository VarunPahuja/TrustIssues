"""`app/models/guards.py`: the two invariants docs/lanes/vp.md states in prose
— append-only `policy_versions`/`audit_log`, and `agents.current_limit`/
`current_rung` never changing without a paired `policy_versions` row in the
same transaction."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Agent, AuditLogEntry, Base, PolicyVersion, apply_policy_version
from app.models.guards import ImmutableRowError, PolicyVersionRequiredError

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


@pytest.fixture()
def engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def agent(engine) -> Agent:
    with Session(engine) as session:
        a = Agent(
            id="agent-01",
            name="x",
            current_limit=500,
            current_rung=0,
            state="active",
            created_at=_NOW,
        )
        session.add(a)
        session.commit()
    with Session(engine) as session:
        return session.get(Agent, "agent-01")


# --- paired limit-change guard -----------------------------------------------------


def test_direct_limit_change_without_a_policy_version_is_rejected(engine):
    with Session(engine) as session:
        a = Agent(
            id="agent-01",
            name="x",
            current_limit=500,
            current_rung=0,
            state="active",
            created_at=_NOW,
        )
        session.add(a)
        session.commit()

    with Session(engine) as session:
        a = session.get(Agent, "agent-01")
        a.current_limit = 1000
        a.current_rung = 1
        with pytest.raises(PolicyVersionRequiredError):
            session.commit()


def test_apply_policy_version_updates_agent_and_writes_a_row_together(engine):
    with Session(engine) as session:
        session.add(
            Agent(
                id="agent-01",
                name="x",
                current_limit=500,
                current_rung=0,
                state="active",
                created_at=_NOW,
            )
        )
        session.commit()

    with Session(engine) as session:
        a = session.get(Agent, "agent-01")
        version = apply_policy_version(
            session,
            a,
            id="pv-001",
            limit=1000,
            rung=1,
            effective_from=_NOW,
            created_by="user-admin-01",
            reason="test",
        )
        session.commit()
        assert version.previous_version_id is None

    with Session(engine) as session:
        a = session.get(Agent, "agent-01")
        assert (a.current_limit, a.current_rung) == (1000, 1)


def test_apply_policy_version_chains_to_the_previous_row(engine):
    with Session(engine) as session:
        a = Agent(
            id="agent-01",
            name="x",
            current_limit=500,
            current_rung=0,
            state="active",
            created_at=_NOW,
        )
        session.add(a)
        apply_policy_version(
            session,
            a,
            id="pv-001",
            limit=1000,
            rung=1,
            effective_from=_NOW,
            created_by="system",
            reason="first",
        )
        session.commit()

    with Session(engine) as session:
        a = session.get(Agent, "agent-01")
        v2 = apply_policy_version(
            session,
            a,
            id="pv-002",
            limit=2500,
            rung=2,
            effective_from=_NOW,
            created_by="system",
            reason="second",
        )
        session.commit()
        assert v2.previous_version_id == "pv-001"


def test_apply_policy_version_rejects_inconsistent_limit_and_rung(engine, agent):
    with Session(engine) as session:
        a = session.get(Agent, "agent-01")
        with pytest.raises(ValueError, match="rung"):
            apply_policy_version(
                session,
                a,
                id="pv-001",
                limit=2500,
                rung=1,
                effective_from=_NOW,
                created_by="system",
                reason="bad",
            )


def test_two_agents_changed_in_one_flush_each_need_their_own_policy_version(engine):
    with Session(engine) as session:
        session.add_all(
            [
                Agent(
                    id="agent-01",
                    name="a",
                    current_limit=500,
                    current_rung=0,
                    state="active",
                    created_at=_NOW,
                ),
                Agent(
                    id="agent-02",
                    name="b",
                    current_limit=500,
                    current_rung=0,
                    state="active",
                    created_at=_NOW,
                ),
            ]
        )
        session.commit()

    with Session(engine) as session:
        a1 = session.get(Agent, "agent-01")
        a2 = session.get(Agent, "agent-02")
        apply_policy_version(
            session,
            a1,
            id="pv-001",
            limit=1000,
            rung=1,
            effective_from=_NOW,
            created_by="system",
            reason="only agent-01 versioned",
        )
        a2.current_limit = 1000
        a2.current_rung = 1
        with pytest.raises(PolicyVersionRequiredError, match="agent-02"):
            session.commit()


# --- append-only guard --------------------------------------------------------------


def test_modifying_an_existing_policy_version_is_rejected(engine, agent):
    with Session(engine) as session:
        a = session.get(Agent, "agent-01")
        apply_policy_version(
            session,
            a,
            id="pv-001",
            limit=1000,
            rung=1,
            effective_from=_NOW,
            created_by="system",
            reason="original",
        )
        session.commit()

    with Session(engine) as session:
        pv = session.get(PolicyVersion, "pv-001")
        pv.reason = "tampered"
        with pytest.raises(ImmutableRowError):
            session.commit()


def test_deleting_a_policy_version_is_rejected(engine, agent):
    with Session(engine) as session:
        a = session.get(Agent, "agent-01")
        apply_policy_version(
            session,
            a,
            id="pv-001",
            limit=1000,
            rung=1,
            effective_from=_NOW,
            created_by="system",
            reason="original",
        )
        session.commit()

    with Session(engine) as session:
        pv = session.get(PolicyVersion, "pv-001")
        session.delete(pv)
        with pytest.raises(ImmutableRowError):
            session.commit()


def test_modifying_an_existing_audit_log_entry_is_rejected(engine):
    with Session(engine) as session:
        session.add(
            AuditLogEntry(
                id="log-001",
                ts=_NOW,
                actor="system",
                actor_type="system",
                event_type="test",
                entity_type="test",
                entity_id="x",
                payload={"a": 1},
                prev_hash="0" * 64,
                hash="1" * 64,
                log_seq=1,
            )
        )
        session.commit()

    with Session(engine) as session:
        entry = session.get(AuditLogEntry, "log-001")
        entry.actor = "someone-else"
        with pytest.raises(ImmutableRowError):
            session.commit()
