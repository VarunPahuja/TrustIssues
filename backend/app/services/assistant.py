"""Context assembly for the read-only assistant chat endpoint (`app/api/v1/assistant.py`).

Two scopes, one system prompt, and a hard rule that governs everything in this file:
**an agent-scoped conversation is built from exactly one agent's data, fetched by id,
and nothing else.** Every function below takes a single `agent_id`/`Agent` and queries
only what belongs to it — there is no code path here that loads every agent and asks
the model to pick one out. That is what "isolation: enforce, do not request" means in
practice (see the module docstring on `app/api/v1/assistant.py` for the full framing).

**This is context scoping, not access control.** Nothing here checks whether the
caller is *allowed* to see agent X — any authenticated user who can open agent X's
detail page gets agent X's assistant, exactly as they get agent X's trust chart today.
Per-agent RBAC does not exist in this system (`app/deps.py` has three roles, none of
them agent-scoped). Isolation here means "this conversation's model input contains
one agent's data and no other's," not "this user may or may not see this agent."

**Read-only, all the way down.** Every query in this module is a `select` — nothing
here writes a row, unlike `GET /agents/{id}/trust`
(`app/services/trust.py:compute_and_persist_trust_evaluation`), which computes *and
persists* a fresh evaluation on every call. The assistant reads the latest evaluation
already on record instead. That's a deliberate, visible difference from the rest of
the API: the chat feature's whole claim is "reads and explains, never changes
anything," and a hidden side-effect write would quietly weaken that claim even though
a `TrustEvaluation` snapshot is harmless data, not a policy change.
"""

from __future__ import annotations

from shared.reason_codes import describe
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, AuditLogEntry, Decision, Invoice, PolicyVersion
from app.models import Recommendation as RecommendationRow
from app.models import TrustEvaluation as TrustEvaluationRow
from app.services.doc_index import DocChunk, search
from app.services.trust import agent_context, load_decision_records

DOC_TOP_K = 5
# A full ADR section can run past a thousand characters (see e.g. ADR-0006's
# "Consequences"); five of those plus a whole agent's evidence would make for a
# needlessly expensive prompt. Trimmed per chunk rather than dropping chunks
# entirely, so a citation still names the right section even when its full text
# doesn't fit.
MAX_CHUNK_CHARS = 800
TRUST_HISTORY_LIMIT = 6
POLICY_VERSION_LIMIT = 10
RECOMMENDATION_LIMIT = 5
AUDIT_LOG_LIMIT = 10
DECISION_SAMPLE_LIMIT = 10

# Shown in both scopes, verbatim, ahead of anything retrieved — the one paragraph the
# model is never allowed to drift from regardless of what the docs or the evidence say.
ARCHITECTURE_PREAMBLE = """\
Adaptive AI Governance Platform (AAGP), one sentence: the LLM reasons and explains, \
the trust engine computes evidence (Wilson-bound proportions, two-stage drift \
detection, a weighted trust score), the Policy Engine is the only code that enforces \
a spending limit, and a human must authorize every autonomy increase — a clawback \
(confirmed drift, or a critical error) applies automatically, without a human, \
because taking authority away is the safe direction to fail in and granting it is \
not (ADR-0001, ADR-0004). Autonomy moves in fixed steps on a five-rung ladder \
(₹500 / ₹1,000 / ₹2,500 / ₹5,000 / ₹10,000), never a single leap."""

SYSTEM_PROMPT = """\
You are the in-app assistant for the Adaptive AI Governance Platform (AAGP) — a \
help panel, not a control surface.

You are strictly read-only. You have no tools, no function calls, and no way to \
write to a database, generate a recommendation, approve or reject anything, or start \
a simulation run. If asked to do any of those, say plainly that you cannot, and name \
the UI action that does it instead — e.g. "use the Generate Recommendation button on \
the agent's page" or "Approve/Reject on the Recommendations tab requires an admin." \
Never write as though you performed an action, and never suggest a shortcut around \
human authorization.

Ground-truth discipline, and the most important rule in this prompt: a decision row \
in this system records only the agent's action, the invoice's ground truth, whether \
it was within the agent's limit, and which policy version was in force at the time. \
The governed agent decides by a fixed rule against its current spending limit — \
nothing about *why* one specific invoice was approved or rejected is ever recorded, \
for any agent. You may correctly state what an agent decided and whether it matched \
ground truth. You must never invent a reason for a specific invoice-level decision — \
that reasoning does not exist in this system, and presenting a plausible-sounding \
fabrication as fact would be the single worst thing you could do here. If asked "why \
was invoice X approved," say directly that no per-invoice rationale is recorded, then \
give what *is* knowable: the amount against the limit, the outcome, the policy \
version in force.

Cite what you use. When a claim rests on the documentation excerpts provided below, \
name the source inline, e.g. "(ADR-0006)" or "(System Explained, §5)". When a claim \
rests on the agent evidence provided below, state the numbers directly — there is no \
document to cite for a live trust score or a recommendation's own reasoning.

If this conversation is scoped to one agent, every fact under "Agent evidence" below \
belongs to that agent alone — no other agent's data was fetched for this \
conversation, and you have no way to answer about a different agent by name. Say so \
plainly rather than guessing or drawing on a different agent you might otherwise \
recall from earlier context.

Be concrete. Answer from the specific numbers and reasoning in front of you, not \
generic statements about AI governance in the abstract."""


def build_general_context(question: str) -> tuple[str, list[DocChunk]]:
    """System-wide context: the architecture preamble plus the doc index's top-k
    chunks for `question`. No agent data — this is the "ask about the system"
    scope, and it never touches the database.
    """
    chunks = search(question, k=DOC_TOP_K)
    context = f"{ARCHITECTURE_PREAMBLE}\n\n## Documentation excerpts\n\n{_render_chunks(chunks)}"
    return context, chunks


def build_agent_context(db: Session, agent: Agent, question: str) -> tuple[str, list[DocChunk]]:
    """Agent-scoped context: the same architecture preamble and doc search, plus
    everything this one agent's evidence supports — fetched by `agent.id` only.

    Nothing here loads another agent's row, decisions, evaluations, policy
    versions, recommendations, or audit entries. Every query below filters on
    `agent.id` at the database layer; there is no step where a wider result set
    is fetched and then narrowed in Python or left for the model to filter.
    """
    chunks = search(question, k=DOC_TOP_K)
    sections = [
        ARCHITECTURE_PREAMBLE,
        "## Documentation excerpts",
        _render_chunks(chunks),
        f"## Agent evidence — {agent.id} ({agent.name}) only",
        _render_agent_identity(db, agent),
        _render_current_trust(db, agent.id),
        _render_trust_history(db, agent.id),
        _render_policy_versions(db, agent.id),
        _render_recommendations(db, agent.id),
        _render_decisions(db, agent.id),
        _render_audit_log(db, agent.id),
    ]
    return "\n\n".join(sections), chunks


# --- rendering: docs -----------------------------------------------------------------


def _render_chunks(chunks: list[DocChunk]) -> str:
    if not chunks:
        return "(no matching documentation found for this question)"

    def _trim(text: str) -> str:
        return text if len(text) <= MAX_CHUNK_CHARS else text[:MAX_CHUNK_CHARS] + "…[truncated]"

    return "\n\n".join(f"### {c.doc} — {c.section}\n{_trim(c.text)}" for c in chunks)


# --- rendering: agent evidence, one query per section, always filtered by agent_id --


def _render_agent_identity(db: Session, agent: Agent) -> str:
    ctx = agent_context(db, agent)
    lines = [
        f"Id: {agent.id}",
        f"Name: {agent.name}",
        f"State: {agent.state.value}",
        f"Current limit: {agent.current_limit} (rung {agent.current_rung})",
        f"Decisions since last policy change: {ctx.decisions_since_last_change}",
    ]
    if ctx.decisions_since_clawback is not None:
        lines.append(
            f"Decisions since last clawback: {ctx.decisions_since_clawback} "
            "(clawback recovery in progress)"
        )
    return "### Identity and current standing\n" + "\n".join(lines)


def _latest_trust_evaluation(db: Session, agent_id: str) -> TrustEvaluationRow | None:
    return (
        db.execute(
            select(TrustEvaluationRow)
            .where(TrustEvaluationRow.agent_id == agent_id)
            .order_by(TrustEvaluationRow.evaluated_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _render_current_trust(db: Session, agent_id: str) -> str:
    row = _latest_trust_evaluation(db, agent_id)
    if row is None:
        return "### Current trust evaluation\nNo trust evaluation has been computed for this agent yet."

    p = row.payload
    accuracy = p.get("accuracy")
    agreement = p.get("human_agreement")
    utilization = p.get("utilization")
    drift = p.get("drift") or {}

    lines = [
        f"Trust score: {row.trust_score:.1f}",
        f"Direction: {p.get('direction')} (eligible for increase: {p.get('eligible_for_increase')})",
        (
            f"Current limit / recommended limit: {p.get('current_limit')} / "
            f"{p.get('recommended_limit')} (rung {p.get('current_rung')} / "
            f"{p.get('recommended_rung')})"
        ),
        (
            f"Total decisions: {p.get('total_decisions')} "
            f"(acted {p.get('acted_decisions')}, escalated {p.get('escalated_decisions')}, "
            f"ruled escalations {p.get('ruled_escalations')})"
        ),
    ]
    if accuracy:
        lines.append(
            f"Accuracy: {accuracy['successes']}/{accuracy['trials']} "
            f"(point {accuracy['point']}, Wilson lower bound {accuracy['wilson_lower']:.3f} — "
            "this is the number the ladder actually reads, not the raw point accuracy; "
            "see ADR-0002)"
        )
    if agreement and agreement.get("trials"):
        lines.append(
            f"Human agreement on ruled escalations: {agreement['successes']}/{agreement['trials']} "
            f"(Wilson lower bound {agreement['wilson_lower']:.3f})"
        )
    if utilization:
        lines.append(
            f"Autonomy utilization (acted vs. total): {utilization['successes']}/{utilization['trials']}"
        )
    lines.append(
        f"Critical errors: {p.get('critical_errors')} lifetime, "
        f"{p.get('critical_errors_in_recent_window')} in the recent window "
        f"(rate {p.get('critical_error_rate'):.4f}); noncritical errors: {p.get('noncritical_errors')}"
    )
    lines.append(
        f"Drift: {drift.get('severity')} (detected={drift.get('detected')}, "
        f"underpowered={drift.get('underpowered')}, "
        f"recent accuracy {drift.get('recent_accuracy')}, baseline accuracy {drift.get('baseline_accuracy')}, "
        f"z={drift.get('z_statistic')}, p={drift.get('p_value')})"
    )
    if p.get("weights_renormalised"):
        lines.append("Score weights were renormalised (a component had no evidence yet).")
    reason_codes = p.get("reason_codes") or []
    if reason_codes:
        lines.append(f"Reason codes: {', '.join(reason_codes)} — {describe(list(reason_codes))}")
    # Deliberately no "evaluated at" timestamp here, for the same reason
    # governance/governance/prompts/evidence.py's render_evidence excludes one from
    # what it hashes for a cache key: GET /agents/{id}/trust recomputes and persists a
    # fresh TrustEvaluation on every call (app/services/trust.py), so two calls against
    # an unchanged decision history produce identical substance but a different
    # `evaluated_at` — including it here would bust the cached-mode recording for this
    # agent every time anyone merely reloads its dashboard page, for no evidentiary
    # reason. See `_render_trust_history` for why the same reasoning excludes the
    # current row from that section too, rather than just its timestamp.
    return "### Current trust evaluation\n" + "\n".join(lines)


def _render_trust_history(db: Session, agent_id: str) -> str:
    """Prior evaluations, most recent first, for trend questions — deliberately
    without a timestamp on any row, and deliberately collapsing consecutive
    identical ones into a single line.

    `GET /agents/{id}/trust` recomputes and persists a fresh row on *every*
    call, unconditionally (`app/services/trust.py`) — every dashboard page load
    adds one, whether or not the underlying decisions changed. Two consequences
    follow, and both are handled here rather than left for a reader to notice
    the hard way: identical evidence produces identical `trust_score`/
    `direction`/`recommended_limit` but a new `evaluated_at` each time, so (a)
    printing that timestamp would bust the cached-mode recording every time
    anyone merely reloads the page (the same reasoning `_render_current_trust`
    already applies to the single most recent row — extended here to *every*
    row, since any of them could be one of these repeat, no-op recomputations,
    not just the latest), and (b) a run of otherwise-identical rows is
    duplicate noise for a trend question, not signal. Collapsing consecutive
    duplicates and dropping the timestamp fixes both: what remains is exactly
    the sequence of *distinct* states this agent has actually passed through.
    The single most recent row is excluded entirely — it is already rendered
    in full, above, as "Current trust evaluation."
    """
    rows = (
        db.execute(
            select(TrustEvaluationRow)
            .where(TrustEvaluationRow.agent_id == agent_id)
            .order_by(TrustEvaluationRow.evaluated_at.desc())
        )
        .scalars()
        .all()
    )[1:]  # drop the current row — already shown in full above

    distinct: list[TrustEvaluationRow] = []
    for row in rows:
        if not distinct or (
            (distinct[-1].trust_score, distinct[-1].direction, distinct[-1].recommended_limit)
            != (row.trust_score, row.direction, row.recommended_limit)
        ):
            distinct.append(row)
    distinct = distinct[:TRUST_HISTORY_LIMIT]

    if not distinct:
        return "### Trust history\nNo earlier evaluations on record."
    lines = [
        f"- trust score {row.trust_score:.1f}, direction {row.direction.value}, "
        f"recommended limit {row.recommended_limit}"
        for row in distinct
    ]
    return "### Trust history, most recent first (repeat identical evaluations collapsed)\n" + "\n".join(
        lines
    )


def _render_policy_versions(db: Session, agent_id: str) -> str:
    rows = (
        db.execute(
            select(PolicyVersion)
            .where(PolicyVersion.agent_id == agent_id)
            .order_by(PolicyVersion.effective_from.desc())
            .limit(POLICY_VERSION_LIMIT)
        )
        .scalars()
        .all()
    )
    if not rows:
        return "### Policy version history\nNo policy versions recorded."
    lines = [
        f"- {row.effective_from}: rung {row.rung} (limit {row.limit}), set by {row.created_by} — "
        f"{row.reason}"
        for row in rows
    ]
    return "### Policy version history (most recent first, chained)\n" + "\n".join(lines)


def _render_recommendations(db: Session, agent_id: str) -> str:
    rows = (
        db.execute(
            select(RecommendationRow)
            .where(RecommendationRow.agent_id == agent_id)
            .order_by(RecommendationRow.generated_at.desc())
            .limit(RECOMMENDATION_LIMIT)
        )
        .scalars()
        .all()
    )
    if not rows:
        return "### Recommendations\nNo recommendations generated for this agent yet."

    blocks = []
    for row in rows:
        header = (
            f"- {row.generated_at}: {row.direction.value} to limit {row.proposed_limit}, "
            f"status {row.status.value}, mode {row.governance_mode}"
            + (f", clamped from {row.clamped_from}" if row.clamped else "")
        )
        rationale = f"  Rationale: {row.rationale}"
        opinions = "\n".join(
            f"  - [{o['agent_name']}] {o['verdict']} (confidence {o['confidence']:.2f}): "
            f"{o['reasoning']}"
            + (f" Concerns: {'; '.join(o['concerns'])}" if o.get("concerns") else "")
            for o in row.agent_opinions
        )
        blocks.append("\n".join([header, rationale, opinions] if opinions else [header, rationale]))

    return "### Recommendations, with full governance panel opinions (most recent first)\n" + "\n\n".join(
        blocks
    )


def _render_decisions(db: Session, agent_id: str) -> str:
    """Decision summary statistics, then a readable sample of recent decisions —
    reuses `DecisionRecord`'s own correctness properties for the stats
    (`app.services.trust.load_decision_records`) so "correct" is computed the
    same way here as everywhere else in the system, then a small direct query
    for the sample so it can show `within_limit` and the policy version in
    force, which `DecisionRecord` doesn't carry.
    """
    # Deliberately no vendor/category here, the same reasoning
    # `governance/governance/prompts/evidence.py` states explicitly for its own evidence
    # block: invoice vendor names and categories arrive from outside the company and are
    # attacker-controlled free text (indirect prompt injection: a supplier writes
    # "ignore previous instructions..." into a memo field a human then reads through an
    # LLM). Everything rendered below is system-generated — an id, an amount, an enum, a
    # boolean, a policy-version id — never free text a supplier chose.
    records = load_decision_records(db, agent_id)
    if not records:
        return "### Decisions\nNo decisions recorded for this agent yet."

    acted = [r for r in records if r.is_acted]
    correct = sum(1 for r in acted if r.is_correct)
    critical = sum(1 for r in records if r.is_critical_error)
    noncritical = sum(1 for r in records if r.is_noncritical_error)
    by_action: dict[str, int] = {}
    for r in records:
        by_action[r.action.value] = by_action.get(r.action.value, 0) + 1

    action_breakdown = ", ".join(f"{v} {k}" for k, v in by_action.items())
    summary = [
        f"Total: {len(records)} decisions — {action_breakdown}",
        (
            f"Of {len(acted)} acted decisions, {correct} matched ground truth "
            f"({critical} critical errors — approved when ground truth was reject; "
            f"{noncritical} noncritical errors)."
        ),
    ]

    sample_rows = (
        db.execute(
            select(Decision, Invoice)
            .join(Invoice, Decision.invoice_id == Invoice.id)
            .where(Decision.agent_id == agent_id)
            .order_by(Decision.sequence.desc())
            .limit(DECISION_SAMPLE_LIMIT)
        )
        .all()
    )
    sample_lines = [
        f"- #{d.sequence} {d.decided_at}: invoice {d.invoice_id} (₹{inv.amount}), "
        f"action {d.action.value}, ground truth {inv.ground_truth_action.value}, "
        f"within_limit={d.within_limit}, policy_version={d.policy_version_id}"
        + (f", human_ruling={d.human_ruling.value}" if d.human_ruling else "")
        for d, inv in sample_rows
    ]

    return (
        "### Decision summary and a sample of recent decisions\n"
        + "\n".join(summary)
        + "\n\nNo per-invoice reasoning is recorded — only the action, ground truth, "
        "within_limit, and policy version shown below. Do not infer or invent why "
        "any single decision went the way it did.\n\n"
        + "Recent decisions (most recent first):\n"
        + "\n".join(sample_lines)
    )


def _render_audit_log(db: Session, agent_id: str) -> str:
    """Audit log entries for this agent only.

    `audit_log` has no `agent_id` column of its own — it is a polymorphic event
    log keyed by `(entity_type, entity_id)` (`app/models/audit_log.py`) — so
    membership is decided here, in Python, against two closed sets computed
    from `agent_id` first: this agent's own decision ids, and a direct
    `payload["agent_id"]` match for every other event type this system emits
    (recommendations, policy versions, audit-sample reviews all carry it). This
    is still per-agent filtering, not "load everything and let the model
    filter": no audit_log row makes it into the rendered text, or the prompt,
    unless one of these two checks matches this specific agent before either
    ever reaches the LLM.
    """
    decision_ids = set(
        db.execute(select(Decision.id).where(Decision.agent_id == agent_id)).scalars().all()
    )
    rows = db.execute(select(AuditLogEntry).order_by(AuditLogEntry.log_seq.desc())).scalars().all()

    matched = []
    for row in rows:
        belongs = row.payload.get("agent_id") == agent_id or (
            row.entity_type == "decision" and row.entity_id in decision_ids
        )
        if belongs:
            matched.append(row)
        if len(matched) >= AUDIT_LOG_LIMIT:
            break

    if not matched:
        return "### Audit log\nNo audit log entries recorded for this agent."

    lines = [f"- {row.ts} [{row.event_type}] by {row.actor} ({row.actor_type})" for row in matched]
    return "### Audit log (most recent first)\n" + "\n".join(lines)
