# CONTEXT.md — Adaptive AI Governance Platform (AAGP)

This is the single source of truth for the project. If you are new to the repo — a
teammate, a mentor, a judge — start here. It is written to require zero prior
context. For the full design, glossary, and every architectural decision
explained, see `docs/SYSTEM-EXPLAINED.md`. The architecture and design
sections below (problem statement, the core design rule, the lane
ownership table, the shared contracts) don't change as the code catches up
to them and aren't re-dated as they're re-read. "The demo script" and
"Current status" are re-verified against the actual repo state as of
**2026-09-09** — where the code doesn't yet match the intent, that's stated
explicitly rather than glossed over, the same rule this file has always
followed, just re-applied on a later date than the last time someone did.

---

## Problem statement

An AI agent is given the job of approving or rejecting invoices — a task that
moves real money and where a wrong APPROVE is worse than a wrong REJECT. Nobody
wants to hand an untested agent unlimited authority on day one, and nobody wants
to permanently cap a good agent at "approve nothing over $500" once it has
proven itself.

AAGP is an **earned autonomy engine**: the agent starts with a small autonomy
limit (how large an invoice it's allowed to decide on its own) and earns the
right to a higher limit only when there is statistical evidence — not a vibe,
not a self-report — that it is performing well. If its performance degrades,
its limit is clawed back automatically. Every step of that process is meant to
be auditable: a human (or a judge) should be able to ask "why does the agent
have this limit right now?" and get a specific, evidenced answer.

## The core design rule

> **LLM reasons. Statistics provide evidence. Policy Engine enforces. Humans
> authorize.**

Four different kinds of authority are deliberately kept separate:

- **LLM reasons** — the governance agents (Gemini, via LangGraph) read the
  evidence and the situation and produce a human-readable recommendation and
  rationale. They explain; they do not decide.
- **Statistics provide evidence** — the trust engine computes Wilson-bound
  accuracy, human-agreement, drift, and a composed trust score from raw
  decision history. It is pure arithmetic: reproducible, no LLM call, no
  judgment call. It has no ability to *act* on what it computes.
- **Policy Engine enforces** — the backend's policy layer is the only thing
  that actually changes an agent's spending limit or blocks a decision. It is
  deterministic code, not a prompted model, and it is the sole gate between
  "the evidence says X" and "the agent's authority actually changes."
- **Humans authorize** — increasing an agent's autonomy (giving it *more*
  power) requires a human sign-off on the recommendation. Taking autonomy away
  (clawback) does not — see [ADR-0004](adr/0004-human-approval-required-for-autonomy-increases.md).

This separation is why the codebase is split into four independently-owned
lanes instead of one team building one service — see
[ADR-0001](adr/0001-statistical-evidence-not-llm-judgment.md).

## The four lanes

| Lane | Owner | Directory |
|---|---|---|
| Backend | Varun P. (team lead) | `backend/` |
| Trust Engine | Utkarsh | `trust/`. Wilson confidence bounds, accuracy/human-agreement metrics, critical-error weighting, recent-vs-historical drift detection, trust score composition, the autonomy ladder, cooldowns, and tests. **Hard rules**: no FastAPI, no SQLAlchemy/psycopg/DB access, no Redis, no Celery, no network calls. Pure functions wherever possible. Must emit the `TrustEvaluation` contract |
| Governance | Varun C. | `governance/`. LangGraph governance agents, Gemini integration, human-readable recommendations |
| Simulator | Adhya (porting `origin/ad/simulator-frontend` onto real contracts), then Utkarsh | `simulator/`. Synthetic invoice generation with deterministic ground truth |
| Frontend | Adhya | `frontend/`. The Next.js dashboard |

Backend owns Postgres, SQLAlchemy, Alembic, the Policy Engine, API endpoints,
JWT/RBAC, Docker Compose, and wiring the other lanes together.

Cross-lane contracts — the types every lane agrees to use — live in `shared/`
and are governed as a "treaty": see
[ADR-0005](adr/0005-shared-contracts-as-cross-lane-treaty.md) and
`CONTRIBUTING.md`. `main`'s frozen v1.1 `shared/` is the only valid contract
set — see [ADR-0010](adr/0010-main-shared-contracts-canonical.md) for why
that needed saying explicitly.

## End-to-end request flow (intended design)

This is the flow the architecture is built for — see Current Status below
for how much of it is real code today versus still stubbed.

1. **Simulator** generates a synthetic invoice with a deterministic correct
   answer (`ground_truth: Action`).
2. The governed agent decides `APPROVE` / `REJECT` / `ESCALATE`, constrained by
   its current autonomy limit (a rupee cap), which the **Policy Engine**
   enforces before the decision is allowed to stand.
3. The decision becomes a `DecisionRecord` (`shared/contracts.py`) — this is
   the simulator's output and the trust engine's input.
4. Escalated decisions go to a human reviewer, whose ruling fills in
   `recommended_action` / `human_ruling` on the record.
5. The **Trust Engine** consumes the agent's decision history and current
   standing (`AgentContext`) and computes accuracy, utilization, human
   agreement (all with Wilson lower bounds), critical/non-critical error
   counts, drift status, and a composed `trust_score` — packaged as a
   `TrustEvaluation`.
6. **Governance agents** read the `TrustEvaluation` and produce a
   recommendation with plain-language reasoning — but do not themselves grant
   or revoke anything.
7. The **Policy Engine** applies the recommendation: an *increase* requires a
   human to authorize it; a *clawback* (drift or critical error) is applied
   automatically.
8. The **dashboard** visualizes trust score, autonomy position, drift
   status, and the decision/audit trail.

## Shared contracts (`shared/`)

All four files are marked `TREATY FILE` in their own docstrings — changes
require sign-off from all four lane owners (see `CONTRIBUTING.md`).

### `shared/enums.py`

| Enum | Values |
|---|---|
| `Action(str, Enum)` | `APPROVE`, `REJECT`, `ESCALATE` |
| `AgentState(str, Enum)` | `probation`, `active`, `restricted`, `suspended` |
| `DriftSeverity(str, Enum)` | `NONE`, `WARNING`, `CONFIRMED`, `CRITICAL` |
| `Direction(str, Enum)` | `INCREASE`, `HOLD`, `CLAWBACK` |
| `RecommendationStatus(str, Enum)` (v1.1) | `PENDING`, `APPROVED`, `REJECTED`, `SUPERSEDED` |
| `OpinionVerdict(str, Enum)` (v1.1) | `CONCUR`, `OBJECT`, `ABSTAIN` |
| `ReviewVerdict(str, Enum)` (v1.1) | `AGREED`, `DISAGREED`, `INCONCLUSIVE` |

### `shared/constants.py`

`SCHEMA_VERSION="1.1"`, `CURRENCY="INR"`, `AUTONOMY_LADDER=(500, 1000, 2500,
5000, 10000)`, `AUTONOMY_FLOOR=500`, `MAX_RUNG=4`, `TRUST_SCORE_MIN/MAX=0.0/100.0`,
`CRITICAL_ERROR_DEFINITION` (a critical error is APPROVE-ing an invoice whose
ground truth is REJECT — money leaves the building; the reverse is an error but
not a critical one), `rung_of(limit)` / `limit_of(rung)` helpers, plus (v1.1)
`SAMPLING_RATE_BY_RUNG=(1.0, 0.50, 0.25, 0.10, 0.05)`,
`MIN_SAMPLES_FOR_ACCURACY_ESTIMATE=20`, and a `sampling_rate_of(rung)` helper
for post-hoc audit sampling (ADR-0009).

### `shared/reason_codes.py`

18 machine-readable string constants (increase-blocked reasons, increase-allowed
reasons, clawback reasons, evidence-quality notes, and — new in v1.1 —
audit-sample findings) plus a `HUMAN_READABLE` dict and a `describe(codes)`
formatter — the rule is the human-readable sentence is always generated
*from* a code, never written free-hand.

### `shared/contracts.py`

| Type | Fields |
|---|---|
| `DecisionRecord` | `decision_id`, `sequence`, `invoice_id`, `amount`, `action`, `ground_truth`, `agent_id="agent-01"`, `decided_at=None`, `recommended_action=None`, `human_ruling=None`, plus computed properties `is_escalated` / `is_acted` / `is_correct` / `is_critical_error` / `is_noncritical_error` / `has_human_ruling` / `human_agreed` |
| `ProportionResult` | `successes`, `trials`, `point`, `wilson_lower`, `wilson_upper` |
| `ScoreComponent` | `name`, `value`, `nominal_weight`, `effective_weight`, `available` |
| `AgentContext` | `current_limit=AUTONOMY_FLOOR`, `decisions_since_last_change=0`, `decisions_since_clawback=None`, `state=PROBATION` — the agent's standing, supplied by the backend |
| `DriftResult` | `severity=NONE`, `detected=False`, `recent_accuracy`, `baseline_accuracy`, `drop_pp`, `z_statistic`, `p_value`, `critical_errors_in_window=0`, `recent_n=0`, `baseline_n=0`, `underpowered=False` |
| `TrustEvaluation` | The complete engine output: identity/versioning, decision counts, the three `ProportionResult`s, error counts, `trust_score` + `components` + `weights_renormalised`, `drift`, ladder position (`current_limit/rung`, `recommended_limit/rung`, `direction`, `eligible_for_increase`), `state`, `reason_codes`, `evaluated_at`, `config_fingerprint` |
| `AgentOpinion` (v1.1) | One governance agent's stance before opinions combine into a `Recommendation`: `agent_name`, `verdict` (`OpinionVerdict`), `reasoning`, `concerns`, `confidence` |
| `Recommendation` (v1.1) | Governance's complete output, mirroring `TrustEvaluation`'s role for the trust lane: `recommendation_id`, `agent_id`, `direction`, `proposed_limit`/`proposed_rung`, `rationale`, `opinions: tuple[AgentOpinion, ...]`, `has_dissent`, `confidence`, `governance_mode`, `status` (`RecommendationStatus`), `trust_evaluation_ref`, `clamped`/`clamped_from` |
| `AuditSample` (v1.1) | A decision pulled for post-hoc human review at the rung-scaled rate (ADR-0009): `sample_id`, `decision_id`, `agent_id`, `sampled_at`, `reviewed_at`, `reviewer`, `verdict` (`ReviewVerdict`), `reviewer_action`, plus computed `is_reviewed`/`is_pending` |

`DecisionRecord.ground_truth` is documented (in `Action`'s own docstring) as
existing because "every synthetic invoice carries a deterministic correct
answer" — i.e. this field is simulator-sourced by design. How (or whether) the
pipeline is meant to generalize to real, non-synthetic invoices — where ground
truth isn't known at decision time — is an open question, not yet decided.

## The demo script

As of **2026-09-09**, most of the wiring below exists in real code (see
Current Status) — this is no longer aspirational for any of beats 1-7; see
`docs/audits/2026-09-08-freeze-audit.md` section 3 for the live-verified
detail behind every beat, not just this summary.

1. Start an agent at the autonomy floor (₹500). **Real** — seeded agents
   exist at the floor; `POST /api/v1/decisions` and
   `POST /api/v1/simulation/runs` both work against a real agent row.
2. Run the simulator against a batch of synthetic invoices; show the agent
   deciding within its limit. **Real** — `POST /api/v1/simulation/runs`
   generates invoices, runs a scripted agent, and submits every decision
   through the real ingest path; the Policy Engine enforces the limit on
   each one.
3. Show the trust score climbing as evidence accumulates, with the Wilson
   lower bound (not the raw accuracy) driving the number. **Real** —
   `GET /agents/{id}/trust` computes this from real persisted decisions
   every call.
4. Show the system recommend an autonomy increase, with the reason codes
   that justify it, and require a human click to authorize it. **Real, end
   to end, as of 2026-09-08.** `POST /decisions/{decision_id}/ruling` (PR
   #35) is the real write path for `human_ruling` on an escalated decision
   — the gap that used to block this beat. Live-verified: with enough
   correct decisions and escalations ruled on (past
   `MIN_RULED_ESCALATIONS_FOR_AGREEMENT=5`), `human_agreement` stops
   reading "insufficient evidence," the audit agent stops objecting on that
   basis, and `POST /agents/{id}/recommendations` returns a genuine
   `direction: INCREASE, status: PENDING, has_dissent: false` with all four
   governance agents concurring — not a seed-data recommendation.
   `POST /recommendations/{id}/approve` then applies it for real
   (`current_limit`/`current_rung` change confirmed via `apply_policy_version`).
   Operationally: a demo operator needs to drive a handful of ruled
   escalations alongside the plain decision stream for this evidence gap
   to close in real time — easy to forget mid-demo, worth rehearsing.
5. Inject a critical error (an APPROVE that should have been a REJECT) and
   show the automatic clawback to the floor, with no human step required.
   **Real, as of 2026-09-08.** `generate_recommendation`
   (`backend/app/services/governance.py`) applies a `CLAWBACK` recommendation
   immediately, in the same transaction it's generated in —
   `apply_policy_version` runs with `created_by="system"`, status is
   `APPROVED` on write, and no `approvals` row exists for it (`Approval.
   decided_by` is a foreign-key-to-`users.id` human-approval table by
   construction, so it deliberately doesn't get one — see ADR-0004's
   Consequences for the full reasoning). `POST /recommendations/{id}/approve`
   is never called. Live-verified: injecting a real critical error and
   calling `POST /agents/{id}/recommendations` drops the agent's rung with
   no further API call.
6. Inject a subtler, sustained accuracy drop (not a single critical error) and
   show drift detection catch it — first as a WARNING tripwire, then
   CONFIRMED once the two-proportion z-test has enough samples to back it.
   **Real** — live-verified: injecting critical-error decisions produced
   `drift.severity: "CRITICAL"` and a real `CLAWBACK_CRITICAL_ERROR` reason
   code.
7. Walk through the audit trail: every recommendation and every autonomy
   change should be explainable via its `reason_codes`, not just "the number
   went up." **Mostly real** — 16 of the 18 codes in `shared/reason_codes.py`
   are reachable in the live system as of this branch (`RECOMMENDATION_CLAMPED`
   was added 2026-09-07; previously 15). The remaining two —
   `SAMPLE_EVIDENCE_INSUFFICIENT` and `SAMPLE_REVIEW_DISAGREEMENT` — stay
   unreachable because `POST /audit-samples/{id}/review` is still a stub
   that doesn't persist a review or feed the trust engine.

## Explicit non-goals

- No real bank/ERP/payment integration — everything runs against the
  simulator's synthetic invoices.
- No production-grade auth beyond a JWT/RBAC scaffold — this is a capstone
  prototype, not a hardened multi-tenant system.
- No general-purpose document understanding — invoice "correctness" is a
  deterministic label the simulator assigns, not something the LLM is
  validated against independently.
- No multi-tenant support, no high-availability/scale target — single agent,
  single demo environment.
- No claim that this generalizes beyond invoice approval without further
  design work (see the `ground_truth` open question above).

## Current status

Reality as of **2026-09-09**, not aspiration — every count below was
re-verified by actually running each lane's test suite while writing this,
not copied from a prior pass (`docs/audits/2026-09-08-freeze-audit.md`
found this table's previous numbers stale in four places — see that
audit's section 6 for exactly which). Full detail in that audit,
`docs/audits/2026-09-06-audit.md`, and in `docs/DECISION_LOG.md`'s entries
for PRs #20 through #32; this table is the summary.

| Lane | What exists on `main` | What's stubbed | What's absent |
|---|---|---|---|
| `shared/` (treaty files) | **Merged 2026-08-21, frozen v1.1, unchanged since** — confirmed empty diff against every branch, including both PRs open as of this writing. All four files: enums, constants, 18 reason codes, contracts | — | Nothing — the freeze has held completely |
| Trust Engine (`trust/`) | **Complete.** Wilson score interval, accuracy/utilization/human-agreement proportions, error breakdown, two-stage drift detection, trust-score composition, the full autonomy ladder (six increase gates, two clawback triggers, exactly one rung per evaluation), cooldowns and clawback-recovery logic — all pure functions, all 18 reason codes correctly categorized (though see the Backend row for which are actually *reachable* live). **174 tests, all passing** | — | Nothing outstanding in this lane |
| Backend (`backend/`) | **The most complete lane.** Real persistence throughout: decision ingest (Policy Engine, hash-chained audit log), trust evaluation, governance-recommendation generation with the hard-ceiling clamp (`RECOMMENDATION_CLAMPED` emitted, not just defined), human approve/reject with real `policy_versions` writes, and `POST /simulation/runs` actually starts and runs a simulation rather than minting a fixture. A `CLAWBACK` recommendation now applies itself immediately and automatically — status `APPROVED` on write, `apply_policy_version` called with `created_by="system"`, no human call, no `approvals` row — matching ADR-0004 and this file's own design rule for the first time (fixed 2026-09-08; see "The demo script," beat 5, and ADR-0004's Consequences for the full mechanism). Two concurrency races found live under real Postgres load were fixed (an agent-row lock for the decision-sequence race, an advisory lock plus a new ordering column for a silent audit-chain fork). A cascade bug in the auto-clawback (repeat calls to the recommendations endpoint clawing back twice off one piece of evidence) was found and fixed — the guard is `decisions_since_last_change == 0`, the same "don't act twice on the same evidence" rule the floor no-op already used. `POST /decisions/{decision_id}/ruling` is now the real write path for a decision's `human_ruling`, which is what unblocked a clean, live-driven `INCREASE` (see "The demo script," beat 4). RBAC is real (not a stub-in-name-only): every one of the 7 non-`GET` routes now has a role check, including the ruling endpoint (REVIEWER or ADMIN). **212 tests, all passing** | `POST /audit-samples/{id}/review` still validates and returns a copy without persisting or feeding the trust engine — the one deliberately out-of-scope item several passes in a row haven't touched, since audit-sample persistence is a larger, separate piece of work | Nothing else outstanding in this lane as of this table |
| Governance (`governance/`) | **Complete for the demo's needs.** The LangGraph workflow, prompt layer, real Gemini HTTP client with an on-disk recording store, and swappable providers (Gemini/Claude/OpenAI via `GOVERNANCE_PROVIDER`) are all real and merged. `cached` mode replays **28 real, committed recordings** (`governance/recordings/`, not empty — this was the single biggest gap the 2026-08-31 pass found, since closed; cached mode now covers all six scenarios). `live` mode is implemented with a per-agent timeout and fallback to the recording, not to stub text. **235 tests, all passing** | — | An Azure OpenAI client — the team's stated pivot to "Azure primary, Gemini fallback" has never been implemented or recorded in ADR-0012 (still `Status: Proposed`) or here; a repo-wide search for "azure" outside audit files returns zero hits. The three-provider design that *did* ship (Gemini/Claude/OpenAI) works today regardless of how that question resolves |
| Simulator (`simulator/`) | **Finalized on `main`.** Ported onto the frozen contracts, the ground-truth labeller fixed to 2-way (APPROVE/REJECT, no ESCALATE), and a `simulator arc` command that runs the full ten-beat demo story offline — deterministic, byte-identical across runs and `PYTHONHASHSEED` values, no backend or network needed. **123 tests, all passing, `ruff check simulator/` clean (0 findings)** | — | Online mode (posting a live-generated arc through the real backend, as opposed to the in-memory `arc` command or the backend's own `POST /simulation/runs`) is not this lane's concern now — Utkarsh owns the CLI, the backend generates its own invoices for `POST /simulation/runs` deliberately without importing this lane's code (see that endpoint's own docstring for why) |
| Frontend (`frontend/`) | Rewritten onto the real contracts (5-rung ladder, real endpoint paths, real approve/reject calls, the audit page reading the backend's own `chain_valid` instead of recomputing it client-side). Build and typecheck both clean | `HorizontalThresholdGauge`'s accuracy-health verdict is still computed client-side against a hardcoded `0.85`, not sourced from the API — the one confirmed remaining "business logic in the frontend" violation from the 2026-09-02 and 2026-09-06 audits | The Simulation Control Room correctly implements its documented contract, but was non-functional against the real backend until `POST /simulation/runs` actually persisted a run (fixed 2026-09-07, same day as this table) |

**Bottom line:** the vertical slice the 2026-08-31 audit found completely
disconnected end to end is now real, live-verified, and (for decision
ingest specifically) safe under concurrent load. What remains open as of
the 9 September freeze is narrower and more specific than "wire it up":
audit-sample persistence (the only remaining dead reason codes trace back
to it) and the frontend's one remaining client-side threshold — both the
clawback/human-approval mismatch and the human-ruling gap this file used
to flag here are fixed (see "The demo script," beats 4 and 5). See
`docs/audits/2026-09-08-freeze-audit.md` for the fullest, most current
picture, including per-person outstanding work going into the freeze, and
`docs/audits/2026-09-06-audit.md` for the state two days earlier.
