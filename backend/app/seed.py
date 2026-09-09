"""`make db-reset` / `python -m app.seed` (invoked by the Makefile as
`python -m backend.app.seed`): three agents telling one coherent story,
deterministic — same output every run, no wall-clock reads.

The three agents and their ids, names, and policy-version reasons match
`app/fixtures/agents.py` and `app/fixtures/policy_versions.py` exactly (same
ids, same reasons, same relative timing) so the fixture-stubbed API responses
and the real seeded database never contradict each other — deliberate, per
this branch's own instructions. `decisions.within_limit` is computed with the
real Policy Engine (`app.policy.engine.evaluate_decision`), not hand-typed,
so the seed data is provably consistent with the module that will eventually
enforce it — using it here does not couple `app/models/` to
`backend/app/policy/`; this script is application glue, free to import both,
exactly the way a future decision-ingest endpoint will.

- **agent-01** — mid-ladder (rung 2, ₹2,500), clean record, strong evidence.
- **agent-02** — on probation at the floor, small sample.
- **agent-03** — clawed back after confirmed drift (a critical error:
  approved an invoice ground truth says should have been rejected), now
  restricted and recovering.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta

from shared.constants import limit_of
from shared.contracts import (
    AgentOpinion,
    DriftResult,
    ProportionResult,
    ScoreComponent,
    TrustEvaluation,
)
from shared.enums import (
    Action,
    AgentState,
    Direction,
    DriftSeverity,
    OpinionVerdict,
    RecommendationStatus,
    ReviewVerdict,
)
from shared.reason_codes import (
    AGREEMENT_EVIDENCE_INSUFFICIENT,
    CLAWBACK_RECOVERY_PENDING,
    COOLDOWN_SATISFIED,
    EVIDENCE_SUFFICIENT,
    INSUFFICIENT_SAMPLE,
    NO_DRIFT_DETECTED,
    NO_RECENT_CRITICAL_ERRORS,
    WEIGHTS_RENORMALISED,
)
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models import (
    Agent,
    Approval,
    AuditSample,
    Base,
    Decision,
    Invoice,
    PolicyVersion,
    Recommendation,
    User,
    apply_policy_version,
)
from app.models import (
    TrustEvaluation as TrustEvaluationRow,
)
from app.models.audit_log import append_entry
from app.policy.engine import evaluate_decision
from app.policy.types import Invoice as PolicyInvoice
from app.policy.types import PolicyVersion as PolicyVersionView
from app.schemas.user import Role
from app.services.trust import jsonable as _jsonable

DEFAULT_DATABASE_URL = "postgresql://aagp:aagp_dev_password@localhost:5432/aagp"

# One fixed anchor, matching app/fixtures/*.py's own `_NOW` exactly — every
# timestamp below is relative to this, never to `datetime.now()`, so two runs
# (or a run today and a run next year) produce byte-identical rows.
_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _ago(**kwargs) -> datetime:
    return _NOW - timedelta(**kwargs)


def _as_utc(value: datetime) -> datetime:
    # SQLite (used for local dev/testing; see backend/tests/test_alembic_migration.py)
    # does not preserve tzinfo through a round trip even on a `DateTime(timezone=True)`
    # column — Postgres does. Every timestamp here is UTC by construction (`_NOW`), so
    # a naive value read back is always safe to treat as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _policy_version_in_force(versions: list[PolicyVersion], at: datetime) -> PolicyVersion:
    """The version with the latest `effective_from` at or before `at` — the
    same rule a real ingest endpoint would query with
    (`ORDER BY effective_from DESC WHERE effective_from <= :at LIMIT 1`)."""
    at = _as_utc(at)
    candidates = [v for v in versions if _as_utc(v.effective_from) <= at]
    return max(candidates, key=lambda v: _as_utc(v.effective_from))


def _seed_users(session: Session) -> None:
    # Matches the stub table in app/deps.py exactly — same ids, same emails.
    session.add_all(
        [
            User(
                id="user-admin-01",
                email="admin@aagp.dev",
                role=Role.ADMIN,
                created_at=_ago(days=60),
            ),
            User(
                id="user-reviewer-01",
                email="reviewer@aagp.dev",
                role=Role.REVIEWER,
                created_at=_ago(days=60),
            ),
            User(
                id="user-auditor-01",
                email="auditor@aagp.dev",
                role=Role.AUDITOR,
                created_at=_ago(days=60),
            ),
        ]
    )


def _seed_agent01(session: Session) -> None:
    agent = Agent(
        id="agent-01",
        name="Invoice Agent — Procurement",
        current_limit=limit_of(0),
        current_rung=0,
        state=AgentState.ACTIVE,
        created_at=_ago(days=30),
    )
    session.add(agent)
    apply_policy_version(
        session,
        agent,
        id="pv-agent01-001",
        limit=limit_of(0),
        rung=0,
        effective_from=_ago(days=30),
        created_by="system",
        reason="Agent onboarded at the autonomy floor.",
    )
    session.commit()

    agent = session.get(Agent, "agent-01")
    apply_policy_version(
        session,
        agent,
        id="pv-agent01-002",
        limit=limit_of(1),
        rung=1,
        effective_from=_ago(days=20),
        created_by="user-admin-01",
        reason="Evidence cleared all six increase gates; approved rung 0 -> 1.",
    )
    session.commit()

    agent = session.get(Agent, "agent-01")
    apply_policy_version(
        session,
        agent,
        id="pv-agent01-003",
        limit=limit_of(2),
        rung=2,
        effective_from=_ago(days=10),
        created_by="user-admin-01",
        reason="Evidence cleared all six increase gates; approved rung 1 -> 2.",
    )
    session.commit()


def _seed_agent02(session: Session) -> None:
    agent = Agent(
        id="agent-02",
        name="Invoice Agent — Facilities",
        current_limit=limit_of(0),
        current_rung=0,
        state=AgentState.PROBATION,
        created_at=_ago(days=5),
    )
    session.add(agent)
    apply_policy_version(
        session,
        agent,
        id="pv-agent02-001",
        limit=limit_of(0),
        rung=0,
        effective_from=_ago(days=5),
        created_by="system",
        reason="Agent onboarded at the autonomy floor.",
    )
    session.commit()


def _seed_agent03(session: Session) -> None:
    agent = Agent(
        id="agent-03",
        name="Invoice Agent — Marketing",
        current_limit=limit_of(0),
        current_rung=0,
        state=AgentState.ACTIVE,
        created_at=_ago(days=45),
    )
    session.add(agent)
    apply_policy_version(
        session,
        agent,
        id="pv-agent03-001",
        limit=limit_of(0),
        rung=0,
        effective_from=_ago(days=45),
        created_by="system",
        reason="Agent onboarded at the autonomy floor.",
    )
    session.commit()

    agent = session.get(Agent, "agent-03")
    apply_policy_version(
        session,
        agent,
        id="pv-agent03-002",
        limit=limit_of(1),
        rung=1,
        effective_from=_ago(days=30),
        created_by="user-admin-01",
        reason="Evidence cleared all six increase gates; approved rung 0 -> 1.",
    )
    session.commit()

    agent = session.get(Agent, "agent-03")
    apply_policy_version(
        session,
        agent,
        id="pv-agent03-003",
        limit=limit_of(2),
        rung=2,
        effective_from=_ago(days=14),
        created_by="user-admin-01",
        reason="Evidence cleared all six increase gates; approved rung 1 -> 2.",
    )
    session.commit()

    # The clawback: automatic, no human approval (ADR-0004), state -> RESTRICTED.
    agent = session.get(Agent, "agent-03")
    apply_policy_version(
        session,
        agent,
        id="pv-agent03-004",
        limit=limit_of(1),
        rung=1,
        effective_from=_ago(days=2),
        created_by="system",
        reason=(
            "Automatic clawback: confirmed drift (CLAWBACK_DRIFT). No human "
            "approval required for a reduction (ADR-0004)."
        ),
    )
    agent.state = AgentState.RESTRICTED
    session.commit()


def _seed_invoice_and_decision(
    session: Session,
    *,
    invoice_id: str,
    decision_id: str,
    sequence: int,
    agent_id: str,
    amount: int,
    vendor: str,
    category: str,
    submitted_at: datetime,
    ground_truth: Action,
    action: Action,
    decided_at: datetime,
    recommended_action: Action | None = None,
    human_ruling: Action | None = None,
) -> None:
    session.add(
        Invoice(
            id=invoice_id,
            amount=amount,
            vendor=vendor,
            category=category,
            submitted_at=submitted_at,
            ground_truth_action=ground_truth,
        )
    )

    agent = session.get(Agent, agent_id)
    versions = session.query(PolicyVersion).filter(PolicyVersion.agent_id == agent_id).all()
    version_in_force = _policy_version_in_force(versions, decided_at)

    decision_outcome = evaluate_decision(
        PolicyInvoice(invoice_id=invoice_id, amount=amount),
        PolicyVersionView(
            agent_id=agent_id,
            limit=version_in_force.limit,
            rung=version_in_force.rung,
            agent_state=agent.state,
            version_id=version_in_force.id,
        ),
    )

    session.add(
        Decision(
            id=decision_id,
            sequence=sequence,
            invoice_id=invoice_id,
            agent_id=agent_id,
            action=action,
            recommended_action=recommended_action,
            human_ruling=human_ruling,
            policy_version_id=version_in_force.id,
            within_limit=decision_outcome.within_limit,
            decided_at=decided_at,
        )
    )


def _seed_decisions(session: Session) -> None:
    _seed_invoice_and_decision(
        session,
        invoice_id="inv-1148",
        decision_id="dec-agent01-0148",
        sequence=148,
        agent_id="agent-01",
        amount=1800,
        vendor="Acme Supplies",
        category="procurement",
        submitted_at=_ago(hours=3, minutes=10),
        ground_truth=Action.APPROVE,
        action=Action.APPROVE,
        decided_at=_ago(hours=3),
    )
    _seed_invoice_and_decision(
        session,
        invoice_id="inv-1149",
        decision_id="dec-agent01-0149",
        sequence=149,
        agent_id="agent-01",
        amount=2400,
        vendor="Northwind Traders",
        category="procurement",
        submitted_at=_ago(hours=2, minutes=10),
        ground_truth=Action.APPROVE,
        action=Action.ESCALATE,
        decided_at=_ago(hours=2),
        recommended_action=Action.APPROVE,
        human_ruling=Action.APPROVE,
    )
    _seed_invoice_and_decision(
        session,
        invoice_id="inv-1150",
        decision_id="dec-agent01-0150",
        sequence=150,
        agent_id="agent-01",
        amount=900,
        vendor="Acme Supplies",
        category="procurement",
        submitted_at=_ago(hours=1, minutes=10),
        ground_truth=Action.REJECT,
        action=Action.REJECT,
        decided_at=_ago(hours=1),
    )
    session.commit()

    _seed_invoice_and_decision(
        session,
        invoice_id="inv-2012",
        decision_id="dec-agent02-0012",
        sequence=12,
        agent_id="agent-02",
        amount=420,
        vendor="CleanCo Facilities",
        category="facilities",
        submitted_at=_ago(hours=5, minutes=10),
        ground_truth=Action.APPROVE,
        action=Action.APPROVE,
        decided_at=_ago(hours=5),
    )
    _seed_invoice_and_decision(
        session,
        invoice_id="inv-2013",
        decision_id="dec-agent02-0013",
        sequence=13,
        agent_id="agent-02",
        amount=480,
        vendor="Unregistered Vendor Ltd",
        category="facilities",
        submitted_at=_ago(hours=4, minutes=10),
        ground_truth=Action.REJECT,
        action=Action.ESCALATE,
        decided_at=_ago(hours=4),
        recommended_action=Action.REJECT,
        human_ruling=Action.REJECT,
    )
    session.commit()

    # The critical error (APPROVE where ground truth is REJECT) that triggers
    # agent-03's confirmed-drift clawback — decided just before the clawback's
    # effective_from (2 days ago), while pv-agent03-003 (rung 2) is still in force.
    _seed_invoice_and_decision(
        session,
        invoice_id="inv-3193",
        decision_id="dec-agent03-0193",
        sequence=193,
        agent_id="agent-03",
        amount=2200,
        vendor="Marketing Media Co",
        category="marketing",
        submitted_at=_ago(days=2, hours=1, minutes=10),
        ground_truth=Action.REJECT,
        action=Action.APPROVE,
        decided_at=_ago(days=2, hours=1),
    )
    _seed_invoice_and_decision(
        session,
        invoice_id="inv-3194",
        decision_id="dec-agent03-0194",
        sequence=194,
        agent_id="agent-03",
        amount=950,
        vendor="Marketing Media Co",
        category="marketing",
        submitted_at=_ago(days=2, minutes=10),
        ground_truth=Action.APPROVE,
        action=Action.APPROVE,
        decided_at=_ago(days=2),
    )
    _seed_invoice_and_decision(
        session,
        invoice_id="inv-3195",
        decision_id="dec-agent03-0195",
        sequence=195,
        agent_id="agent-03",
        amount=700,
        vendor="Print & Signage Inc",
        category="marketing",
        submitted_at=_ago(hours=6, minutes=10),
        ground_truth=Action.APPROVE,
        action=Action.APPROVE,
        decided_at=_ago(hours=6),
    )
    session.commit()


# Score-component names, matching trust/trust_engine/score.py's constants.
_ACCURACY = "accuracy_wilson_lower"
_AGREEMENT = "human_agreement"
_CRITICAL_PENALTY = "critical_error_penalty"
_UTILIZATION = "autonomy_utilization"


def _seed_trust_evaluations(session: Session) -> None:
    agent01_current = TrustEvaluation(
        agent_id="agent-01",
        total_decisions=160,
        acted_decisions=150,
        escalated_decisions=10,
        ruled_escalations=8,
        accuracy=ProportionResult(141, 150, 0.94, 0.887, 0.969),
        human_agreement=ProportionResult(7, 8, 0.875, 0.529, 0.978),
        utilization=ProportionResult(150, 160, 0.9375, 0.887, 0.967),
        critical_errors=1,
        noncritical_errors=8,
        critical_error_rate=1 / 150,
        critical_errors_in_recent_window=0,
        trust_score=82.4,
        components=(
            ScoreComponent(_ACCURACY, 0.887, 0.50, 0.50, True),
            ScoreComponent(_AGREEMENT, 0.529, 0.25, 0.25, True),
            ScoreComponent(_CRITICAL_PENALTY, 0.967, 0.15, 0.15, True),
            ScoreComponent(_UTILIZATION, 0.9375, 0.10, 0.10, True),
        ),
        weights_renormalised=False,
        drift=DriftResult(
            DriftSeverity.NONE, False, 0.95, 0.93, -2.0, 0.40, 0.69, 0, 50, 100, False
        ),
        current_limit=limit_of(2),
        recommended_limit=limit_of(3),
        current_rung=2,
        recommended_rung=3,
        direction=Direction.INCREASE,
        state=AgentState.ACTIVE,
        eligible_for_increase=True,
        decisions_since_last_change=120,
        reason_codes=(
            EVIDENCE_SUFFICIENT,
            NO_DRIFT_DETECTED,
            NO_RECENT_CRITICAL_ERRORS,
            COOLDOWN_SATISFIED,
        ),
        evaluated_at=_ago(hours=1),
        config_fingerprint="seed-v1.1",
    )
    agent02_current = TrustEvaluation(
        agent_id="agent-02",
        total_decisions=14,
        acted_decisions=12,
        escalated_decisions=2,
        ruled_escalations=1,
        accuracy=ProportionResult(10, 12, 0.833, 0.554, 0.955),
        human_agreement=ProportionResult(1, 1, 1.0, 0.207, 1.0),
        utilization=ProportionResult(12, 14, 0.857, 0.601, 0.960),
        critical_errors=0,
        noncritical_errors=2,
        critical_error_rate=0.0,
        critical_errors_in_recent_window=0,
        trust_score=58.0,
        components=(
            ScoreComponent(_ACCURACY, 0.554, 0.50, 0.667, True),
            ScoreComponent(_AGREEMENT, None, 0.25, 0.0, False),
            ScoreComponent(_CRITICAL_PENALTY, 1.0, 0.15, 0.20, True),
            ScoreComponent(_UTILIZATION, 0.857, 0.10, 0.133, True),
        ),
        weights_renormalised=True,
        drift=DriftResult(DriftSeverity.NONE, False, None, None, None, None, None, 0, 12, 0, True),
        current_limit=limit_of(0),
        recommended_limit=limit_of(0),
        current_rung=0,
        recommended_rung=0,
        direction=Direction.HOLD,
        state=AgentState.PROBATION,
        eligible_for_increase=False,
        decisions_since_last_change=12,
        reason_codes=(INSUFFICIENT_SAMPLE, AGREEMENT_EVIDENCE_INSUFFICIENT, WEIGHTS_RENORMALISED),
        evaluated_at=_ago(hours=4),
        config_fingerprint="seed-v1.1",
    )
    agent03_current = TrustEvaluation(
        agent_id="agent-03",
        total_decisions=210,
        acted_decisions=195,
        escalated_decisions=15,
        ruled_escalations=12,
        accuracy=ProportionResult(165, 195, 0.846, 0.788, 0.891),
        human_agreement=ProportionResult(9, 12, 0.75, 0.469, 0.911),
        utilization=ProportionResult(195, 210, 0.929, 0.886, 0.957),
        critical_errors=4,
        noncritical_errors=11,
        critical_error_rate=4 / 195,
        critical_errors_in_recent_window=3,
        trust_score=41.2,
        components=(
            ScoreComponent(_ACCURACY, 0.788, 0.50, 0.50, True),
            ScoreComponent(_AGREEMENT, 0.469, 0.25, 0.25, True),
            ScoreComponent(_CRITICAL_PENALTY, 0.897, 0.15, 0.15, True),
            ScoreComponent(_UTILIZATION, 0.929, 0.10, 0.10, True),
        ),
        weights_renormalised=False,
        drift=DriftResult(
            DriftSeverity.CONFIRMED, True, 0.80, 0.94, 14.0, 3.21, 0.0013, 3, 50, 100, False
        ),
        current_limit=limit_of(1),
        recommended_limit=limit_of(1),
        current_rung=1,
        recommended_rung=1,
        direction=Direction.HOLD,
        state=AgentState.RESTRICTED,
        eligible_for_increase=False,
        decisions_since_last_change=5,
        reason_codes=(CLAWBACK_RECOVERY_PENDING,),
        evaluated_at=_ago(hours=2),
        config_fingerprint="seed-v1.1",
    )

    for eval_id, evaluation in (
        ("trust-eval-agent01-002", agent01_current),
        ("trust-eval-agent02-002", agent02_current),
        ("trust-eval-agent03-003", agent03_current),
    ):
        session.add(
            TrustEvaluationRow(
                id=eval_id,
                agent_id=evaluation.agent_id,
                evaluated_at=evaluation.evaluated_at,
                trust_score=evaluation.trust_score,
                recommended_limit=evaluation.recommended_limit,
                direction=evaluation.direction,
                payload=_jsonable(evaluation),
            )
        )
    session.commit()


def _seed_recommendations_and_approvals(session: Session) -> None:
    agent01_opinions = (
        AgentOpinion(
            "risk",
            OpinionVerdict.CONCUR,
            "Exposure roughly doubles at rung 3, but zero critical errors in the recent "
            "window and a lifetime rate under 1%. Acceptable increase.",
            (),
            0.82,
        ),
        AgentOpinion(
            "performance",
            OpinionVerdict.CONCUR,
            "Wilson lower bound on accuracy is 0.887 over 150 acted decisions — "
            "comfortably above threshold, and improving, not flat.",
            (),
            0.88,
        ),
        AgentOpinion(
            "compliance",
            OpinionVerdict.CONCUR,
            "Human agreement (0.875, n=8) is thin but directionally positive; no "
            "policy exceptions raised for this agent.",
            ("Human-agreement sample size is small; revisit at the next evaluation.",),
            0.65,
        ),
        AgentOpinion(
            "audit",
            OpinionVerdict.CONCUR,
            "No drift detected, no recent critical errors, cooldown satisfied.",
            (),
            0.80,
        ),
    )
    session.add(
        Recommendation(
            id="rec-agent01-001",
            agent_id="agent-01",
            trust_evaluation_id="trust-eval-agent01-002",
            direction=Direction.INCREASE,
            proposed_limit=limit_of(3),
            rationale=(
                "Trust score 82.4. Panel: 4 concur, 0 object. The panel's own headroom "
                "read rung 4; the hard ceiling clamped the ask to rung 3 (the trust "
                "engine's evidence-supported limit) before this reached a human."
            ),
            agent_opinions=_jsonable(agent01_opinions),
            status=RecommendationStatus.PENDING,
            governance_mode="stub",
            clamped=True,
            clamped_from=limit_of(4),
            generated_at=_NOW,
        )
    )

    agent03_opinions = (
        AgentOpinion(
            "risk",
            OpinionVerdict.CONCUR,
            "Clawback reduces the ceiling from 2,500 to 1,000, cutting single-decision "
            "exposure. No risk-side objection to reducing authority.",
            (),
            0.90,
        ),
        AgentOpinion(
            "performance",
            OpinionVerdict.CONCUR,
            "Recent accuracy dropped to 0.80 against a 0.94 baseline, z=3.21, p=0.0013 — "
            "a confirmed drop.",
            (),
            0.91,
        ),
        AgentOpinion(
            "compliance",
            OpinionVerdict.OBJECT,
            "Three critical errors in the recent window is itself grounds for review.",
            ("Recommend a manual audit-sample pass over agent-03's last 20 decisions.",),
            0.77,
        ),
        AgentOpinion(
            "audit",
            OpinionVerdict.CONCUR,
            "Drift severity CONFIRMED per the two-stage detector.",
            (),
            0.85,
        ),
    )
    session.add(
        Recommendation(
            id="rec-agent03-001",
            agent_id="agent-03",
            trust_evaluation_id="trust-eval-agent03-003",
            direction=Direction.CLAWBACK,
            proposed_limit=limit_of(1),
            rationale=(
                "Trust score 41.2. Panel: 3 concur, 1 object (compliance). Clawback "
                "applied automatically; no human authorization required (ADR-0004)."
            ),
            agent_opinions=_jsonable(agent03_opinions),
            status=RecommendationStatus.APPROVED,
            governance_mode="stub",
            clamped=False,
            clamped_from=None,
            generated_at=_NOW - timedelta(days=2),
        )
    )

    # A historical, already-decided recommendation for agent-01's earlier
    # rung 1 -> 2 increase (matching pv-agent01-003's own reason/timing) —
    # gives `approvals` a real row demonstrating the human-authorization path
    # ADR-0004 requires for an INCREASE, distinct from the still-PENDING
    # rec-agent01-001 above.
    session.add(
        Recommendation(
            id="rec-agent01-000",
            agent_id="agent-01",
            trust_evaluation_id="trust-eval-agent01-002",
            direction=Direction.INCREASE,
            proposed_limit=limit_of(2),
            rationale="Evidence cleared all six increase gates; proposed rung 1 -> 2.",
            agent_opinions=[],
            status=RecommendationStatus.APPROVED,
            governance_mode="stub",
            clamped=False,
            clamped_from=None,
            generated_at=_ago(days=10),
        )
    )
    session.commit()

    session.add(
        Approval(
            id="appr-agent01-001",
            recommendation_id="rec-agent01-000",
            decided_by="user-admin-01",
            verdict=RecommendationStatus.APPROVED,
            reason="Evidence cleared all six increase gates; approved rung 1 -> 2.",
            decided_at=_ago(days=10),
        )
    )
    session.commit()


def _seed_audit_samples(session: Session) -> None:
    session.add_all(
        [
            AuditSample(
                id="sample-001",
                decision_id="dec-agent01-0148",
                agent_id="agent-01",
                sampled_at=_ago(hours=3),
                reviewed_at=_ago(hours=1),
                reviewer="user-reviewer-01",
                verdict=ReviewVerdict.AGREED,
                reviewer_action=Action.APPROVE,
            ),
            AuditSample(
                id="sample-002",
                decision_id="dec-agent02-0012",
                agent_id="agent-02",
                sampled_at=_ago(hours=5),
                reviewed_at=None,
                reviewer=None,
                verdict=None,
                reviewer_action=None,
            ),
            AuditSample(
                id="sample-003",
                decision_id="dec-agent03-0193",
                agent_id="agent-03",
                sampled_at=_ago(days=2, hours=1),
                reviewed_at=_ago(days=1, hours=20),
                reviewer="user-reviewer-01",
                verdict=ReviewVerdict.DISAGREED,
                reviewer_action=Action.REJECT,
            ),
            AuditSample(
                id="sample-004",
                decision_id="dec-agent03-0195",
                agent_id="agent-03",
                sampled_at=_ago(hours=6),
                reviewed_at=None,
                reviewer=None,
                verdict=None,
                reviewer_action=None,
            ),
        ]
    )
    session.commit()


def _seed_audit_log(session: Session) -> None:
    # Same five events, same ids and timestamps, as app/fixtures/audit.py —
    # hashed for real here via app.models.audit_log.append_entry rather than
    # hand-typed, so this chain is independently, provably valid.
    entries = [
        {
            "id": "log-0001",
            "ts": _ago(days=2, hours=1, minutes=1),
            "actor": "agent-03",
            "actor_type": "agent",
            "event_type": "decision.recorded",
            "entity_type": "decision",
            "entity_id": "dec-agent03-0193",
            "payload": {"action": "APPROVE", "amount": 2200},
        },
        {
            "id": "log-0002",
            "ts": _ago(days=2),
            "actor": "system",
            "actor_type": "system",
            "event_type": "trust.evaluated",
            "entity_type": "trust_evaluation",
            "entity_id": "trust-eval-agent03-003",
            "payload": {"trust_score": 41.2, "direction": "CLAWBACK"},
        },
        {
            "id": "log-0003",
            "ts": _ago(days=2) + timedelta(minutes=1),
            "actor": "system",
            "actor_type": "system",
            "event_type": "recommendation.applied",
            "entity_type": "recommendation",
            "entity_id": "rec-agent03-001",
            "payload": {"direction": "CLAWBACK", "proposed_limit": 1000, "status": "APPROVED"},
        },
        {
            "id": "log-0004",
            "ts": _ago(days=2) + timedelta(minutes=2),
            "actor": "system",
            "actor_type": "system",
            "event_type": "policy_version.created",
            "entity_type": "policy_version",
            "entity_id": "pv-agent03-004",
            "payload": {
                "agent_id": "agent-03",
                "limit": 1000,
                "rung": 1,
                "reason": "Automatic clawback: confirmed drift.",
            },
        },
        {
            "id": "log-0005",
            "ts": _ago(hours=1),
            "actor": "user-reviewer-01",
            "actor_type": "user",
            "event_type": "audit_sample.reviewed",
            "entity_type": "audit_sample",
            "entity_id": "sample-001",
            "payload": {"verdict": "AGREED", "reviewer_action": "APPROVE"},
        },
    ]
    for entry in entries:
        append_entry(session, **entry)
        session.commit()


def seed(session: Session) -> None:
    """Populate every table with one coherent, deterministic dataset. This
    function itself still assumes an empty database — inserting into
    `users` first, with fixed ids, is what makes a second raw call blow up
    on `users_pkey`. `main()` (below) is what actually makes the CLI entry
    point idempotent, by checking first and truncating on request rather
    than by making this function tolerate partial/duplicate state."""
    _seed_users(session)
    session.commit()

    _seed_agent01(session)
    _seed_agent02(session)
    _seed_agent03(session)

    _seed_decisions(session)
    _seed_trust_evaluations(session)
    _seed_recommendations_and_approvals(session)
    _seed_audit_samples(session)
    _seed_audit_log(session)


def _already_seeded(session: Session) -> bool:
    return session.execute(select(User.id).limit(1)).first() is not None


def _truncate_all_tables(session: Session) -> None:
    """Empty every app table (never `alembic_version` — that's migration
    bookkeeping, not app data, and isn't part of `Base.metadata`) in one
    statement. `CASCADE` makes the FK order irrelevant; `RESTART IDENTITY`
    matters for any future serial/identity columns, harmless today since
    every id here is an assigned string."""
    table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    session.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the database with one coherent, deterministic demo dataset."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "If the database already has data, truncate every app table first, "
            "then seed. Without this flag, an already-seeded database is left "
            "untouched. Destructive, but scoped to this database's own app "
            "tables — never used against something the caller didn't ask for."
        ),
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = create_engine(database_url)
    with Session(engine) as session:
        if _already_seeded(session):
            if not args.reset:
                print(
                    f"{database_url} already has data (found existing users) — "
                    "nothing done. Re-run with --reset to wipe every app table "
                    "and reseed from scratch."
                )
                return
            print("Existing data found; --reset passed — truncating every app table before reseeding.")
            _truncate_all_tables(session)
        seed(session)
    print(f"Seeded {database_url}")


if __name__ == "__main__":
    main()
