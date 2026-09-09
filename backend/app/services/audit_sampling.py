"""Post-hoc audit sampling — pulling a fraction of an agent's decisions for
human review, at a rate that falls as the agent earns rungs (ADR-0009).

Two things this module is careful about.

**The rate comes from the rung, and nowhere else.** `shared.constants.
sampling_rate_of` owns the ladder `(1.0, 0.50, 0.25, 0.10, 0.05)`; this module
never second-guesses it. Review burden falling as trust is earned is the
operational half of "earned autonomy" — without it the project delivers a
bigger rupee ceiling and no relief from oversight, which ADR-0009 calls out as
the thing that would make the whole promise hollow.

**Selection is deterministic.** A decision is sampled by hashing its id, not by
drawing a random number, so replaying the same run produces the same review
queue. `random.random()` would have been shorter and would have quietly cost
the project its reproducibility claim: two runs of the same seeded simulation
would disagree about which decisions a human was asked to check. The hash is
uniformly distributed over [0, 1), so the *rate* is honest even though the
*choice* is fixed.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from shared.constants import sampling_rate_of
from sqlalchemy.orm import Session

from app.models import Agent, AuditSample, Decision

# 8 hex characters of the digest, read as an integer over its full range.
_HASH_BITS = 32
_HASH_MAX = float(1 << _HASH_BITS)


def selection_fraction(decision_id: str) -> float:
    """Where this decision falls in [0, 1), derived from its id alone.

    Stable across processes and runs: `hash()` would not be, because Python
    randomises string hashing per process.
    """
    digest = hashlib.sha256(decision_id.encode("utf-8")).hexdigest()
    return int(digest[: _HASH_BITS // 4], 16) / _HASH_MAX


def should_sample(decision_id: str, rung: int) -> bool:
    """Whether this decision is pulled for review at this agent's rung."""
    return selection_fraction(decision_id) < sampling_rate_of(rung)


def sample_if_selected(db: Session, decision: Decision, agent: Agent) -> AuditSample | None:
    """Create an `audit_samples` row for this decision, if it is selected.

    Called from decision ingest, inside that request's transaction, so a
    decision and its sample are written together or not at all: a sample
    referencing a decision that was rolled back would be a foreign key to
    nothing.

    Selection is recorded on the decision's own audit entry rather than a
    row of its own: `backend/tests/test_decisions.py` pins "exactly one
    audit_log row per decision POST", and that invariant is worth more than a
    dedicated event. The decision's entry carries `audit_sample_id`, so the
    selection is still auditable and still hash-chained.

    Escalations are never sampled. Sampling exists to check what an agent did
    *on its own authority* (ADR-0009); an escalation was already handed to a
    human, and asking a second human to review the fact that the first one was
    asked would inflate the review burden without adding evidence.
    """
    if decision.action.value == "ESCALATE":
        return None
    if not should_sample(decision.id, agent.current_rung):
        return None

    # `audit_samples.decision_id` is a foreign key, but there is no ORM
    # relationship between the two models, so SQLAlchemy's unit of work does
    # not know the sample depends on the decision and is free to flush the
    # sample first — which fails the constraint. Flushing here pins the order.
    # Still one transaction: the caller's session commits or rolls back both
    # together, so a rolled-back decision takes its sample with it.
    db.flush()

    sample = AuditSample(
        id=f"sample-{uuid.uuid4().hex[:12]}",
        decision_id=decision.id,
        agent_id=agent.id,
        sampled_at=decision.decided_at or datetime.now(UTC),
        reviewed_at=None,
        reviewer=None,
        verdict=None,
        reviewer_action=None,
    )
    db.add(sample)
    return sample
