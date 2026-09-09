"""Audit-sample and audit-log resources — the post-hoc review queue (ADR-0009)
and the hash-chained record of everything the system has done.

`list_audit_log` reads the real `audit_log` table (vp/approval-workflow) and
recomputes the chain fresh on every call — the tamper-evidence property
docs/lanes/vp.md claims, demonstrable, not just asserted. Audit samples
remain fixture-backed — out of scope here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from shared.enums import ReviewVerdict
from shared.reason_codes import SAMPLE_REVIEW_DISAGREEMENT
from sqlalchemy import select

from app.api.v1.pagination import PageParam, PageSizeParam, paginate
from app.deps import CurrentUserDep, DbSessionDep, require_role
from app.errors import (
    CONFLICT_RESPONSE,
    FORBIDDEN_RESPONSE,
    NOT_FOUND_RESPONSE,
    ApiError,
    not_found,
)
from app.models import AuditLogEntry, AuditSample
from app.models.audit_hash import verify_chain
from app.models.audit_log import append_entry
from app.schemas.audit import AuditLogEntryOut, AuditLogPage, AuditSampleOut, AuditSampleReview
from app.schemas.envelope import Page
from app.schemas.user import Role

router = APIRouter(tags=["audit"])

# Reviewing a sample is REVIEWER's actual job (ADR-0009); ADMIN may also do it.
# AUDITOR stays read-only everywhere, including here.
_reviewer_or_admin = Depends(require_role(Role.ADMIN, Role.REVIEWER))


def _sample_out(sample: AuditSample) -> AuditSampleOut:
    """`audit_samples.id` is the row's primary key; `AuditSampleOut.sample_id`
    is what `shared.contracts.AuditSample` calls it. Mapped explicitly rather
    than by attribute name, the same way `_decision_out` assembles a decision.
    """
    return AuditSampleOut(
        sample_id=sample.id,
        decision_id=sample.decision_id,
        agent_id=sample.agent_id,
        sampled_at=sample.sampled_at,
        reviewed_at=sample.reviewed_at,
        reviewer=sample.reviewer,
        verdict=sample.verdict,
        reviewer_action=sample.reviewer_action,
    )


@router.get("/audit-samples", response_model=Page[AuditSampleOut])
def list_audit_samples(
    db: DbSessionDep,
    user: CurrentUserDep,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
    pending: bool = False,
    agent_id: str | None = None,
) -> Page[AuditSampleOut]:
    """List sampled decisions pulled for human review, newest first.

    Reads the real `audit_samples` table. Rows are written by
    `app.services.audit_sampling.sample_if_selected` as decisions are
    recorded, at `sampling_rate_of(agent.current_rung)` — as they happen, not
    on a schedule, so the queue reflects the agent's current rung rather than
    whatever it was when a batch job last ran.

    `?pending=true` is the review-queue view: samples nobody has ruled on yet.
    `?agent_id=` narrows to one agent, which is what an agent detail page wants.
    """
    stmt = select(AuditSample)
    if pending:
        stmt = stmt.where(AuditSample.reviewed_at.is_(None))
    if agent_id is not None:
        stmt = stmt.where(AuditSample.agent_id == agent_id)
    rows = db.execute(stmt.order_by(AuditSample.sampled_at.desc())).scalars().all()
    items = [_sample_out(r) for r in rows]
    return paginate(items, page, page_size)


@router.post(
    "/audit-samples/{sample_id}/review",
    response_model=AuditSampleOut,
    dependencies=[_reviewer_or_admin],
    responses={**NOT_FOUND_RESPONSE, **FORBIDDEN_RESPONSE, **CONFLICT_RESPONSE},
)
def review_audit_sample(
    sample_id: str, body: AuditSampleReview, user: CurrentUserDep, db: DbSessionDep
) -> AuditSampleOut:
    """Record a human review of one sampled decision. REVIEWER or ADMIN only.

    Writes `reviewed_at`/`reviewer`/`verdict`/`reviewer_action` onto the
    `audit_samples` row and appends a hash-chained audit entry. A `DISAGREED`
    verdict carries `SAMPLE_REVIEW_DISAGREEMENT` (shared/reason_codes.py) on
    that entry: a reviewer contradicting the agent is itself evidence, and it
    is the only place in the system that code is produced.

    Reviews once. A second review is a 409 rather than a silent overwrite —
    the same reasoning as a decision ruling: a review is evidence that may
    already have been counted, and rewriting it would change history under
    whatever cited it.

    What this deliberately does NOT do: overwrite the decision's recorded
    ground truth. ADR-0009 describes reviewed samples eventually *becoming*
    the ground-truth source once the system runs past the simulator, but that
    same ADR flags the contract gap it depends on — `TrustEvaluation` has no
    field distinguishing accuracy built from full ground truth from accuracy
    built from a sampled slice — as deferred, not decided. Silently
    substituting one for the other here would corrupt the simulator's
    deterministic ground truth and break the arc's reproducibility, to
    implement a contract change nobody has agreed.
    """
    sample = db.get(AuditSample, sample_id)
    if sample is None:
        raise not_found(
            "audit_sample_not_found", f"No audit sample {sample_id!r}.", {"sample_id": sample_id}
        )

    if sample.reviewed_at is not None and sample.verdict is not None:
        raise ApiError(
            status_code=409,
            code="audit_sample_already_reviewed",
            message=f"Audit sample {sample_id!r} was already reviewed.",
            detail={"sample_id": sample_id, "reviewed_at": str(sample.reviewed_at)},
        )

    reviewed_at = datetime.now(UTC)
    sample.reviewed_at = reviewed_at
    sample.reviewer = user.user_id
    sample.verdict = body.verdict
    sample.reviewer_action = body.reviewer_action

    disagreed = body.verdict is ReviewVerdict.DISAGREED
    append_entry(
        db,
        id=f"log-{uuid.uuid4().hex[:12]}",
        ts=reviewed_at,
        actor=user.user_id,
        actor_type="human",
        event_type="audit_sample.reviewed",
        entity_type="audit_sample",
        entity_id=sample.id,
        payload={
            "decision_id": sample.decision_id,
            "agent_id": sample.agent_id,
            "verdict": body.verdict.value,
            "reviewer_action": body.reviewer_action.value,
            "reason_codes": [SAMPLE_REVIEW_DISAGREEMENT] if disagreed else [],
            "reason": body.reason,
        },
    )

    return _sample_out(sample)


@router.get("/audit-log", response_model=AuditLogPage)
def list_audit_log(
    db: DbSessionDep, user: CurrentUserDep, page: PageParam = 1, page_size: PageSizeParam = 20
) -> AuditLogPage:
    """The complete hash-chained event log, newest first. Read-only —
    nothing in this API ever mutates an existing row (that's the point).

    Recomputes the whole chain from `GENESIS_HASH` on every call —
    `audit_log` is small enough in this system for that to be cheap — and
    reports the result as `chain_valid`/`chain_verified_scope` rather than
    just asserting immutability in a docstring. If the table ever grows
    large enough that a full recompute stops being cheap, this falls back
    to verifying only the returned page and says so via
    `chain_verified_scope`, instead of silently verifying less than it
    claims.
    """
    rows = db.execute(select(AuditLogEntry).order_by(AuditLogEntry.ts)).scalars().all()
    chain_valid = verify_chain((row.prev_hash, row.payload, row.hash) for row in rows)

    newest_first = list(reversed(rows))
    page_result = paginate(
        [AuditLogEntryOut.model_validate(row) for row in newest_first], page, page_size
    )
    return AuditLogPage(
        items=page_result.items,
        total=page_result.total,
        page=page_result.page,
        page_size=page_result.page_size,
        chain_valid=chain_valid,
        chain_verified_scope="full",
    )
