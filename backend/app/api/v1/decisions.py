"""Decision resource — the simulator's ingest path, and the read-back of what
was ingested.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status
from shared.enums import Action
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.pagination import PageParam, PageSizeParam, paginate
from app.deps import CurrentUserDep, DbSessionDep, require_role
from app.errors import (
    CONFLICT_RESPONSE,
    FORBIDDEN_RESPONSE,
    NOT_FOUND_RESPONSE,
    ApiError,
    not_found,
)
from app.models import Agent, Decision, Invoice
from app.models.audit_log import append_entry
from app.models.policy_versions import current_policy_version_for
from app.policy.engine import evaluate_decision
from app.policy.types import Invoice as PolicyInvoice
from app.policy.types import PolicyVersion as PolicyVersionView
from app.schemas.decision import DecisionCreate, DecisionRecordOut, DecisionRuling
from app.schemas.envelope import Page
from app.schemas.user import Role

router = APIRouter(prefix="/decisions", tags=["decisions"])

# Was unprotected until docs/audits/2026-09-06-audit.md's RBAC pass found it
# (item 2: "no require_role(...) dependency at all — any stub role,
# including AUDITOR, can currently POST a decision"). Gated ADMIN, matching
# the "operational action -> ADMIN operates the system" convention
# `app/api/v1/simulation.py` already establishes for the same kind of
# machine-originated, non-human-judgment action. Flagged, not just assumed:
# the three stub roles (admin/reviewer/auditor) are all *human* dashboard
# roles, and none of them obviously represents "the agent itself" or "the
# simulator" submitting its own decision — ADMIN is the closest fit today
# because the header defaults to it when absent (`app.deps.current_user`),
# which is exactly how the simulator calls this route in practice. Revisit
# when real agent/service credentials exist instead of a human role header.
_admin_only = Depends(require_role(Role.ADMIN))

# Ruling on an escalation is REVIEWER's job, by the same reasoning that makes
# reviewing an audit sample theirs (ADR-0009); ADMIN may also do it, and
# AUDITOR stays read-only.
_reviewer_or_admin = Depends(require_role(Role.ADMIN, Role.REVIEWER))


def _decision_out(decision: Decision, invoice: Invoice) -> DecisionRecordOut:
    """`amount`/`ground_truth` live on `invoices`, not `decisions`
    (`docs/lanes/vp.md`'s schema normalises them there) — assembling the
    response is always a join of the two, never one row alone."""
    return DecisionRecordOut(
        decision_id=decision.id,
        sequence=decision.sequence,
        invoice_id=decision.invoice_id,
        amount=invoice.amount,
        action=decision.action,
        ground_truth=invoice.ground_truth_action,
        agent_id=decision.agent_id,
        decided_at=decision.decided_at,
        recommended_action=decision.recommended_action,
        human_ruling=decision.human_ruling,
    )


def _create_decision(db: Session, body: DecisionCreate) -> Decision:
    """The one transaction: validate the agent and its current policy
    version exist, evaluate the invoice against the real Policy Engine,
    persist the invoice (if new) and the decision, and append a
    hash-chained `audit_log` entry — all against the same `Session`,
    committed or rolled back together by `app.deps.get_session`.

    Fails closed at every point that cannot safely resolve to a persisted
    row: an unknown agent, or an agent with no policy version on record at
    all, raises before anything is written, rather than guessing. A
    *known but internally inconsistent* policy version still persists —
    that is exactly what `evaluate_decision`'s own fail-closed escalation
    (`POLICY_VERSION_INVALID`) is for, and the row it produces is real
    evidence, not an error.
    """
    if body.ground_truth is Action.ESCALATE:
        # shared.contracts.DecisionRecord's own docstring: ground truth is
        # always APPROVE or REJECT — ESCALATE is only ever an agent action,
        # never a ground truth. invoices.ground_truth_action's CHECK
        # constraint (app/models/invoices.py) enforces the same invariant at
        # the database level; reject here with a clear 422 instead of
        # letting that constraint turn this into a 500.
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_ground_truth",
            message="ground_truth must be APPROVE or REJECT — ESCALATE is only ever an agent action.",
            detail={"ground_truth": body.ground_truth.value},
        )

    if body.recommended_action is Action.ESCALATE:
        # A recommendation is what the agent would have DONE had it been
        # allowed to act. "I recommend escalating" is not an action, and it
        # could never agree or disagree with a human ruling, so it would be
        # dead weight in the human-agreement denominator.
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_recommended_action",
            message="recommended_action must be APPROVE or REJECT, never ESCALATE.",
            detail={"recommended_action": body.recommended_action.value},
        )

    # `.with_for_update()` instead of `db.get()`: this row lock is the fix
    # for the sequence race below (docs/audits/2026-09-06-audit.md Race 1).
    # Locking the agent — a fixed, pre-existing row — serializes every
    # concurrent decision for this agent for the rest of the transaction, so
    # the `max(sequence)` read and the `Decision` insert become atomic
    # relative to each other. A unique constraint plus retry was the other
    # option; this was preferred because it guarantees zero failed inserts
    # (no retry loop, no client-visible errors) and a gap-free sequence,
    # rather than merely detecting the collision after the fact. No-op
    # outside Postgres (SQLite silently ignores `FOR UPDATE`), which is fine:
    # the test suite's SQLite fixture never runs concurrent requests against
    # the same engine.
    agent = db.execute(
        select(Agent).where(Agent.id == body.agent_id).with_for_update()
    ).scalar_one_or_none()
    if agent is None:
        raise not_found(
            "agent_not_found", f"No agent {body.agent_id!r}.", {"agent_id": body.agent_id}
        )

    policy_version = current_policy_version_for(db, agent.id)
    if policy_version is None:
        # Fail closed at the persistence boundary too: decisions.policy_version_id
        # is NOT NULL, so there is no row to reference — this can't be
        # recorded as an escalation the way an *invalid* version can be (see
        # the docstring above). A clean, explicit error beats a NOT NULL
        # constraint violation turning into a 500.
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="no_policy_version_on_record",
            message=f"Agent {agent.id!r} has no policy version on record; cannot ingest a decision.",
            detail={"agent_id": agent.id},
        )

    decided_at = datetime.now(UTC)

    invoice = db.get(Invoice, body.invoice_id)
    if invoice is None:
        # DecisionCreate mirrors shared.contracts.DecisionRecord field-for-
        # field, which carries no vendor/category — invoices.vendor/category
        # are NOT NULL and have no source in this request body. Placeholder
        # values for a first-time invoice_id; an existing invoice's own
        # fields are never overwritten on a repeat post (an invoice is a
        # fact recorded once — app/models/invoices.py's own docstring).
        # Flagged as a real contract gap in this branch's report, not
        # silently resolved.
        invoice = Invoice(
            id=body.invoice_id,
            amount=body.amount,
            vendor="unknown",
            category="unspecified",
            submitted_at=decided_at,
            ground_truth_action=body.ground_truth,
        )
        db.add(invoice)

    outcome = evaluate_decision(
        PolicyInvoice(invoice_id=body.invoice_id, amount=body.amount),
        PolicyVersionView(
            agent_id=agent.id,
            limit=policy_version.limit,
            rung=policy_version.rung,
            agent_state=agent.state,
            version_id=policy_version.id,
        ),
    )

    next_sequence = (
        db.execute(
            select(func.max(Decision.sequence)).where(Decision.agent_id == agent.id)
        ).scalar()
        or 0
    ) + 1

    decision = Decision(
        id=f"dec-{uuid.uuid4().hex[:12]}",
        sequence=next_sequence,
        invoice_id=body.invoice_id,
        agent_id=agent.id,
        action=body.action,
        recommended_action=body.recommended_action,
        # Filled in later by POST /decisions/{id}/ruling, never at ingest:
        # the agent cannot rule on its own escalation.
        human_ruling=None,
        policy_version_id=policy_version.id,
        within_limit=outcome.within_limit,
        decided_at=decided_at,
    )
    db.add(decision)

    append_entry(
        db,
        id=f"log-{uuid.uuid4().hex[:12]}",
        ts=decided_at,
        actor=agent.id,
        actor_type="agent",
        event_type="decision.recorded",
        entity_type="decision",
        entity_id=decision.id,
        payload={
            "invoice_id": body.invoice_id,
            "amount": body.amount,
            "action": body.action.value,
            "ground_truth": body.ground_truth.value,
            "recommended_action": (
                body.recommended_action.value if body.recommended_action else None
            ),
            "allowed": outcome.allowed,
            "within_limit": outcome.within_limit,
            "reason_code": outcome.reason_code,
            "reason": body.reason,
        },
    )

    return decision


@router.post(
    "",
    response_model=DecisionRecordOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_admin_only],
    responses=FORBIDDEN_RESPONSE,
)
def create_decision(
    body: DecisionCreate, user: CurrentUserDep, db: DbSessionDep
) -> DecisionRecordOut:
    """Ingest one decision from the simulator (or, eventually, a real agent).

    Validates the agent exists and has a policy version on record, evaluates
    the invoice against that policy with the real Policy Engine
    (`app.policy.engine.evaluate_decision`), and — in one transaction —
    persists the invoice (if new), the decision (referencing the exact
    policy version in force), and a hash-chained `audit_log` entry
    (docs/lanes/vp.md). `within_limit` is recorded on the decision row
    verbatim from the Policy Engine's own output; the Policy Engine's reason
    code is recorded on the audit entry, never recomputed later.
    """
    decision = _create_decision(db, body)
    invoice = db.get(Invoice, decision.invoice_id)
    return _decision_out(decision, invoice)


@router.get("", response_model=Page[DecisionRecordOut])
def list_decisions(
    db: DbSessionDep, user: CurrentUserDep, page: PageParam = 1, page_size: PageSizeParam = 20
) -> Page[DecisionRecordOut]:
    """List decisions, newest first."""
    rows = db.execute(
        select(Decision, Invoice)
        .join(Invoice, Decision.invoice_id == Invoice.id)
        .order_by(Decision.decided_at.desc())
    ).all()
    items = [_decision_out(decision, invoice) for decision, invoice in rows]
    return paginate(items, page, page_size)


@router.get("/{decision_id}", response_model=DecisionRecordOut, responses=NOT_FOUND_RESPONSE)
def get_decision(decision_id: str, user: CurrentUserDep, db: DbSessionDep) -> DecisionRecordOut:
    """Fetch one decision by id."""
    decision = db.get(Decision, decision_id)
    if decision is None:
        raise not_found(
            "decision_not_found", f"No decision {decision_id!r}.", {"decision_id": decision_id}
        )
    invoice = db.get(Invoice, decision.invoice_id)
    return _decision_out(decision, invoice)


@router.post(
    "/{decision_id}/ruling",
    response_model=DecisionRecordOut,
    dependencies=[_reviewer_or_admin],
    responses={**NOT_FOUND_RESPONSE, **FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
)
def rule_on_decision(
    decision_id: str, body: DecisionRuling, user: CurrentUserDep, db: DbSessionDep
) -> DecisionRecordOut:
    """Record a human's ruling on one escalated decision. REVIEWER or ADMIN only.

    This is the write path for `decisions.human_ruling`, and the only one:
    `POST /api/v1/decisions` deliberately leaves it null, because an agent
    cannot rule on its own escalation.

    Escalating is the agent deferring to a human, so a ruling is the answer to
    that deferral — which is why only an ESCALATE decision can be ruled on, and
    why the ruling itself must be APPROVE or REJECT. `shared.contracts.
    DecisionRecord.human_agreed` then compares the ruling against the agent's
    own `recommended_action`, and `trust_engine.stats.rates.human_agreement`
    aggregates those comparisons over ruled escalations only.

    A decision ingested without a `recommended_action` can still be ruled on —
    the ruling is a real fact worth recording — but the pair contributes
    nothing to human agreement, since there is no recommendation to compare
    against. `has_human_ruling` requires both halves.

    Rules once. A second ruling is a 409 rather than a silent overwrite: the
    audit chain records what a human decided, and decisions already evaluated
    against it must not change underneath that evidence.
    """
    if body.ruling is Action.ESCALATE:
        # The decision is already an escalation; "escalate it again" is not a
        # ruling, and it could never agree or disagree with the agent's
        # recommendation.
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_ruling",
            message="ruling must be APPROVE or REJECT — ESCALATE is not a human verdict.",
            detail={"ruling": body.ruling.value},
        )

    decision = db.get(Decision, decision_id)
    if decision is None:
        raise not_found(
            "decision_not_found", f"No decision {decision_id!r}.", {"decision_id": decision_id}
        )

    if decision.action is not Action.ESCALATE:
        raise ApiError(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="decision_not_escalated",
            message=(
                f"Decision {decision_id!r} was {decision.action.value}, not ESCALATE. "
                "Only an escalation is awaiting a human ruling."
            ),
            detail={"decision_id": decision_id, "action": decision.action.value},
        )

    if decision.human_ruling is not None:
        raise ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="decision_already_ruled",
            message=f"Decision {decision_id!r} was already ruled {decision.human_ruling.value}.",
            detail={"decision_id": decision_id, "human_ruling": decision.human_ruling.value},
        )

    decision.human_ruling = body.ruling

    append_entry(
        db,
        id=f"log-{uuid.uuid4().hex[:12]}",
        ts=datetime.now(UTC),
        actor=user.user_id,
        actor_type="human",
        event_type="decision.ruled",
        entity_type="decision",
        entity_id=decision.id,
        payload={
            "agent_id": decision.agent_id,
            "invoice_id": decision.invoice_id,
            "action": decision.action.value,
            "recommended_action": (
                decision.recommended_action.value if decision.recommended_action else None
            ),
            "human_ruling": body.ruling.value,
            # Null when the agent never recorded a recommendation, in which
            # case this ruling cannot feed human agreement (see docstring).
            "agreed": (
                decision.recommended_action is body.ruling
                if decision.recommended_action is not None
                else None
            ),
            "reason": body.reason,
        },
    )

    invoice = db.get(Invoice, decision.invoice_id)
    return _decision_out(decision, invoice)
