"""Generating a governance `Recommendation` for an agent, and reading one back.

`generate_recommendation` is the request-path glue: fresh trust evidence in,
a clamped, persisted `Recommendation` out, in one transaction — trust engine,
governance panel, and the Policy Engine's hard ceiling, wired together
exactly once (docs/lanes/vp.md, ADR-0001, ADR-0003, ADR-0004, ADR-0014).

`recommendation_out` is the read side, shared with
`app/api/v1/recommendations.py` so a row read straight back from the database
and the row this module just persisted always turn into the same API shape.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

from governance.coordinator import recommend
from governance.llm.errors import GovernanceLLMError
from governance.prompts.schema import OpinionParseError
from shared.constants import SCHEMA_VERSION, rung_of
from shared.enums import Direction, OpinionVerdict, RecommendationStatus
from shared.reason_codes import CLAWBACK_CRITICAL_ERROR, CLAWBACK_DRIFT, RECOMMENDATION_CLAMPED
from sqlalchemy.orm import Session

from app.errors import service_unavailable
from app.models import Agent, apply_policy_version
from app.models import Recommendation as RecommendationRow
from app.models.audit_log import append_entry
from app.policy.ceiling import clamp_recommendation
from app.schemas.governance import AgentOpinionOut, RecommendationOut
from app.services.trust import agent_context, compute_and_persist_trust_evaluation, jsonable


def recommendation_out(row: RecommendationRow) -> RecommendationOut:
    """A persisted row, in the API's shape. `has_dissent`/`confidence`/
    `proposed_rung` are derived from `agent_opinions` rather than stored —
    see `app/models/recommendations.py` for why."""
    opinions = [AgentOpinionOut(**opinion) for opinion in row.agent_opinions]
    confidence = (
        round(sum(o.confidence for o in opinions) / len(opinions), 4) if opinions else 0.0
    )
    # `RECOMMENDATION_CLAMPED` (shared/reason_codes.py, discussed in
    # ADR-0014) was defined, exported to the frontend, and produced by
    # nothing — docs/audits/2026-09-06-audit.md's finding 1f. Derived here,
    # not stored, for the same reason `has_dissent` below is: it is a pure
    # function of `row.clamped`, so storing it separately could only ever
    # go stale relative to the column it's derived from.
    reason_codes = [RECOMMENDATION_CLAMPED] if row.clamped else []
    return RecommendationOut(
        recommendation_id=row.id,
        agent_id=row.agent_id,
        schema_version=SCHEMA_VERSION,
        direction=row.direction,
        proposed_limit=row.proposed_limit,
        proposed_rung=rung_of(row.proposed_limit),
        rationale=row.rationale,
        opinions=opinions,
        has_dissent=any(o.verdict is OpinionVerdict.OBJECT for o in opinions),
        confidence=confidence,
        governance_mode=row.governance_mode,
        status=row.status,
        trust_evaluation_ref=row.trust_evaluation_id,
        generated_at=row.generated_at,
        clamped=row.clamped,
        clamped_from=row.clamped_from,
        reason_codes=reason_codes,
    )


def generate_recommendation(db: Session, agent: Agent) -> RecommendationOut:
    """Recompute `agent`'s `TrustEvaluation` from its real persisted decision
    history, run the governance panel over it, clamp the panel's proposal to
    what that evidence actually supports, and persist trust evaluation,
    recommendation, and audit entry — all against `db`, in the one
    transaction `app.deps.get_session` commits or rolls back as a whole (the
    same pattern `app/api/v1/decisions.py`'s decision-ingest uses).

    Governance's own `recommend()` already asserts it can never propose above
    `evaluation.recommended_limit` (governance/governance/coordinator.py,
    governance/INTEGRATION.md) — `clamp_recommendation` still runs
    unconditionally, because that guarantee is governance's, not this
    module's, and the hard ceiling does not rely on being caught (ADR-0003,
    ADR-0014).

    A `CLAWBACK` is applied immediately, in this same transaction, instead of
    being left `PENDING` — see ADR-0004: increases need human authorization,
    reductions do not, because removing authority is always the safe
    direction and a degrading agent should not keep its ceiling overnight
    waiting for someone to click approve. Deliberately writes no `approvals`
    row: `Approval.decided_by` is a foreign key to `users.id`
    (`app/models/approvals.py`) — a human-approval table by construction —
    and inventing a fake "system" user row to satisfy it would misrepresent
    what an `approvals` row means. `PolicyVersion.created_by="system"` is
    already the anticipated, documented path for exactly this
    (`app/models/policy_versions.py`'s own docstring, and `app/seed.py`'s
    hand-seeded agent-03 clawback use precisely this shape — no `Approval`
    row, `status=APPROVED` directly, `created_by="system"`). This function
    matches that established precedent rather than inventing a new one.
    """
    evaluation, trust_evaluation_id = compute_and_persist_trust_evaluation(db, agent)

    mode = os.environ.get("GOVERNANCE_MODE")
    try:
        proposal = recommend(evaluation, mode=mode, trust_evaluation_ref=trust_evaluation_id)
    except (GovernanceLLMError, OpinionParseError) as exc:
        # Both must be caught: OpinionParseError inherits ValueError, not
        # GovernanceLLMError (governance/INTEGRATION.md's own warning). Loud,
        # not a silent stub fallback — a 503 says "governance is unavailable
        # right now," which is the truth; guessing at stub reasoning instead
        # would hide that a cached-mode call had no recording to answer with.
        raise service_unavailable(
            "governance_unavailable",
            f"Governance could not produce a recommendation for {agent.id!r} in "
            f"{mode or 'stub'!r} mode: {exc}",
            {"agent_id": agent.id, "governance_mode": mode or "stub"},
        ) from exc

    final_limit, clamped, clamped_from = clamp_recommendation(
        proposal.proposed_limit, evaluation.recommended_limit
    )
    generated_at = proposal.generated_at or datetime.now(UTC)
    rec_id = f"rec-{agent.id}-{uuid.uuid4().hex[:10]}"

    is_clawback = proposal.direction is Direction.CLAWBACK
    status = RecommendationStatus.APPROVED if is_clawback else proposal.status

    row = RecommendationRow(
        id=rec_id,
        agent_id=agent.id,
        trust_evaluation_id=trust_evaluation_id,
        direction=proposal.direction,
        proposed_limit=final_limit,
        rationale=proposal.rationale,
        agent_opinions=jsonable(proposal.opinions),
        status=status,
        governance_mode=proposal.governance_mode,
        clamped=clamped,
        clamped_from=clamped_from,
        generated_at=generated_at,
    )
    db.add(row)

    policy_version_id: str | None = None
    clawback_reason_code: str | None = None
    if is_clawback:
        # The trust engine's ladder checks CRITICAL before CONFIRMED and
        # returns immediately on either (trust/trust_engine/ladder.py), so at
        # most one of these two is ever present.
        clawback_reason_code = (
            CLAWBACK_CRITICAL_ERROR
            if CLAWBACK_CRITICAL_ERROR in evaluation.reason_codes
            else CLAWBACK_DRIFT
        )
        # Two guards, not one.
        #
        # The first is the "only write when the limit actually changes" rule
        # app/api/v1/recommendations.py's approve path already applies to a
        # no-op HOLD approval: an agent already at AUTONOMY_FLOOR clawing
        # back further is still floor -> floor, and writing a redundant
        # policy version would reset decisions_since_last_change for a
        # change that didn't happen.
        #
        # The second stops the same evidence being punished twice.
        # DriftSeverity.CRITICAL is stateless — it asks only whether a
        # critical error sits in the last CRITICAL_ERROR_WINDOW acted
        # decisions (trust/trust_engine/stats/drift.py), with no memory of
        # whether a clawback already answered it. Generating a recommendation
        # is a read-shaped operation callers repeat freely: a dashboard
        # refresh, a retry, a simulator loop. Without this, two calls with no
        # new decisions between them drop two rungs for one error, which
        # contradicts ADR-0004's "exactly one rung" and could walk an agent to
        # the floor on a single mistake.
        #
        # decisions_since_last_change == 0 means the limit moved and nothing
        # has happened since, so there is no new evidence to act on.
        since_change = agent_context(db, agent).decisions_since_last_change
        already_acted_on_this_evidence = since_change == 0
        if final_limit != agent.current_limit and not already_acted_on_this_evidence:
            policy_version_id = f"pv-{uuid.uuid4().hex[:12]}"
            apply_policy_version(
                db,
                agent,
                id=policy_version_id,
                limit=final_limit,
                rung=rung_of(final_limit),
                effective_from=generated_at,
                created_by="system",
                reason=(
                    f"Automatic clawback: {'a critical error' if clawback_reason_code == CLAWBACK_CRITICAL_ERROR else 'confirmed drift'} "
                    f"({clawback_reason_code}). No human approval required for a "
                    "reduction (ADR-0004)."
                ),
            )

    append_entry(
        db,
        id=f"log-{uuid.uuid4().hex[:12]}",
        ts=generated_at,
        actor="system",
        actor_type="system",
        # Distinct from "recommendation.generated" so an auditor can tell a
        # system-applied clawback from an ordinary PENDING recommendation
        # (or a human's later "recommendation.approved") at a glance —
        # matches the event_type app/seed.py's hand-authored clawback
        # example already uses for exactly this case.
        event_type="recommendation.applied" if is_clawback else "recommendation.generated",
        entity_type="recommendation",
        entity_id=rec_id,
        payload={
            "agent_id": agent.id,
            "direction": proposal.direction.value,
            "proposed_limit": proposal.proposed_limit,
            "final_limit": final_limit,
            "clamped": clamped,
            "clamped_from": clamped_from,
            "reason_codes": (
                ([RECOMMENDATION_CLAMPED] if clamped else [])
                + ([clawback_reason_code] if clawback_reason_code else [])
            ),
            "governance_mode": proposal.governance_mode,
            "trust_evaluation_id": trust_evaluation_id,
            **({"policy_version_id": policy_version_id} if is_clawback else {}),
        },
    )

    return recommendation_out(row)
