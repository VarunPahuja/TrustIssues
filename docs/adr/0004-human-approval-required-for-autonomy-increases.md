# ADR-0004: Autonomy increases require human authorization; clawbacks do not

## Status

Accepted and enforced in code as of 2026-09-08 — see Consequences.

## Context

The system can recommend two very different kinds of change to an agent's
autonomy: give it *more* authority, or take authority *away*. These are not
symmetric risks. Granting more spending authority to an agent that turns out
not to deserve it is the exact failure mode the entire project exists to
prevent. Reducing an agent's authority when its performance has degraded is
the safe direction to fail in — the cost of an unnecessary clawback is
friction, not money out the door.

## Decision

**Increasing** an agent's autonomy limit requires a human to authorize the
system's recommendation before it takes effect. **Reducing** it (clawback,
whether from confirmed drift or a critical error) is applied automatically,
with no human step required.

This is visible in the contract shape itself, not just in prose: `shared/contracts.py`'s
`TrustEvaluation` has `recommended_limit` / `recommended_rung` — a
*recommendation*, not an `applied_limit` — plus a separate boolean
`eligible_for_increase`. Nothing in the contract has an equivalent
"recommended clawback, pending approval" shape; `DriftSeverity.CRITICAL`
(`shared/enums.py`) is designed as an immediate, unconditional signal — the
drift detector's own short-circuit logic (`trust/trust_engine/stats/drift.py:110-115`)
returns `CRITICAL` the instant a critical error is found in the recent window,
skipping the statistical tripwire/z-test path entirely that a milder drift
signal has to pass through. There is no equivalent "skip the checks and
apply immediately" path anywhere for an *increase*.

## Consequences

- An increase can be wrong and caught before it costs anything — a human sees
  the reason codes and the evidence and can decline it.
- A clawback can never be delayed by an unavailable reviewer — the system's
  most safety-critical action doesn't wait on a person.
- This means the "recommendation" produced by governance for an increase
  (`Direction.INCREASE`) is inert on its own — it is data for a human
  decision, not an instruction the Policy Engine executes unattended. A
  clawback recommendation (`Direction.CLAWBACK`) is the opposite: never
  inert, always applied.
- **"No human step required" is a claim about *approval*, and until
  vp/clawback-trigger it was silently misleading about *triggering*.**
  Applying a clawback with no human click still needed something to call
  `generate_recommendation` in the first place, and nothing in the
  decision-ingest path did — see "What actually triggers the evaluation"
  below for the gap and the fix. The title and the rest of this ADR were
  true about approval and read as an overclaim about the whole pipeline
  being human-free; that gap belonged in code, not left for a reader to
  notice.

### What actually triggers the evaluation (2026-09-13)

This ADR always described what happens *once* `generate_recommendation` runs
— nothing here ever said what causes it to run. Before vp/clawback-trigger,
the honest answer was "a human opens the dashboard, or something calls
`POST /agents/{id}/recommendations` directly" — meaning a degrading agent
kept its ceiling indefinitely if neither happened. Confirmed live: 400
degrading decisions left `GET /agents/{id}/trust` reporting
`direction: CLAWBACK` with `drift.severity: CRITICAL`, while
`current_limit` had not moved.

`backend/app/services/simulation.py:execute_simulation_run` now evaluates the
agent once, after the run's last decision commits and before the run is
marked completed (`_evaluate_and_maybe_clawback`), reusing
`generate_recommendation` — the same function, the same cascade guard (PR
#40: do not re-apply a clawback for evidence already acted on), no second
clawback path. INCREASE/HOLD directions are deliberately never generated
from this trigger point at all (checked via a cheap, non-persisting peek at
`trust_engine`'s own direction before calling `generate_recommendation`):
an increase still needs a human either way, so auto-creating a PENDING
recommendation at the end of every ordinary run would only fill the
Approvals queue with rows nobody is waiting on, for a batch boundary that
means nothing to a real reviewer.

**Be precise about what this is and is not.** The trigger is "at the end of
a simulation run" — a prototype's natural batch boundary, not a production
mechanism. It is deliberately not "on every decision ingest": a full trust
evaluation is `load_decision_records` plus `trust_engine.evaluate` over an
agent's entire history, and running that on every single write is both slow
and wasteful at the couple hundred decisions a run submits. A real
deployment would evaluate on a schedule, or from the ingest path itself with
its own rate limiting — either is a reasonable choice this prototype simply
hasn't had to make, because it has no ingest traffic outside of simulation
runs. Stated here as a scoped limitation, not implied as more than it is.

### How this is enforced in code (2026-09-08)

`backend/app/services/governance.py:generate_recommendation` branches on
`direction` in the same transaction that persists the recommendation:

- **`INCREASE`/`HOLD`**: persisted with `status=PENDING`, exactly as before.
  Nothing acts on it until a human calls
  `POST /recommendations/{id}/approve` (`backend/app/api/v1/recommendations.py`),
  which is the only code path that may call
  `app.models.policy_versions.apply_policy_version` for one of these.
- **`CLAWBACK`**: persisted with `status=APPROVED` directly — never `PENDING`
  — and `apply_policy_version` is called immediately, in the same
  transaction, with `created_by="system"` (`PolicyVersion.created_by` is a
  free-text column exactly anticipating this, not a foreign key). Dropping
  below `shared.constants.AUTONOMY_FLOOR` is impossible by construction:
  `trust/trust_engine/ladder.py` computes the clawback's target rung as
  `max(current_rung - 1, 0)` before governance or this module ever see it, so
  the floor is enforced upstream of the auto-apply path, not by this code
  re-checking it.
- **No `approvals` row is written for an auto-applied clawback.**
  `Approval.decided_by` is a foreign key to `users.id`
  (`backend/app/models/approvals.py`) — a human-approval table by
  construction — and inventing a fake "system" user row to satisfy it would
  misrepresent what that table means. `app/seed.py`'s hand-authored
  agent-03 clawback example anticipated this exact shape (no `Approval` row,
  `status=APPROVED`, `created_by="system"`) before any live code produced it.
- **The audit-log entry is distinguishable at a glance**: `event_type=
  "recommendation.applied"` for an auto-applied clawback, vs.
  `"recommendation.generated"` for a PENDING recommendation and
  `"recommendation.approved"`/`"recommendation.rejected"` for a later human
  decision. The payload carries the specific reason code the trust engine
  produced (`CLAWBACK_DRIFT` or `CLAWBACK_CRITICAL_ERROR`,
  `shared/reason_codes.py`) and the new `policy_version_id`.
- **`app/models/guards.py`'s `before_flush` hook still governs every path**:
  it refuses any change to `agents.current_limit`/`current_rung` in a flush
  that doesn't also add a matching `PolicyVersion` — the auto-apply path
  goes through `apply_policy_version` exactly like the human-approval path,
  so this guarantee never had to be relaxed or special-cased for it.
- **An already-auto-applied clawback cannot subsequently be approved or
  rejected**: `POST /recommendations/{id}/approve`/`reject`'s existing
  `status is not PENDING -> 409` check applies unchanged, since an
  auto-applied recommendation is never `PENDING` to begin with.
- `agent_context`'s (`backend/app/services/trust.py`) `decisions_since_clawback`
  derivation — "the latest policy version's rung is lower than the one
  before it" — required no change to work correctly for an auto-applied
  clawback: it identifies the new version as a clawback purely from the
  rung comparison, with no dependency on `created_by` or any explicit flag,
  so the post-clawback recovery clock (gating a future increase via
  `CLAWBACK_RECOVERY_PENDING` until `CLEAN_DECISIONS_AFTER_CLAWBACK` clean
  decisions have passed) starts correctly the moment the version is written.

## Alternatives considered

- **Fully automatic increases**, mirroring the automatic clawback. Rejected:
  removes the one deliberate friction point in a system whose entire premise
  is that autonomy should be *earned* under scrutiny, not compounding on its
  own once a threshold is crossed — and it collapses the LLM-reasons /
  humans-authorize split from ADR-0001 into "LLM reasons and that's
  sufficient," which is the thing this architecture is built to avoid.
- **Human approval for both directions.** Rejected: gating clawback behind
  human availability turns the one thing the system is supposed to do
  reliably — respond fast to degrading performance — into something that can
  stall exactly when it matters most.
- **Automatic increases with a fast-follow human audit** (act first, review
  after). Rejected: for a system whose stated goal is defensibility "before a
  judge," being able to say "a human approved this before it happened" is
  worth more than being able to say "a human reviewed it afterward."
