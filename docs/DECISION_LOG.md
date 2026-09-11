# Decision Log

Reverse-chronological. One entry per change of note (not every commit). Append
new entries at the top. See `docs/README.md` for when to update this vs. an
ADR.

---

**2026-09-11 — Utkarsh (`uk/integration-dryrun`)** — An invoice id now names
exactly one invoice, and a run that earns an increase actually asks for it. Two
defects, found from one report ("good phase suggests an increase but no
approval request appears"). **(1)** `generate_decision_plan`
(`backend/app/services/simulation.py`) built `invoice_id` from
(agent, phase, seed, index) while the *content* also depended on
`current_limit` — amounts are drawn from `randint(10, max(current_limit * 2,
1000))`, and that draw shifts every ground truth after it through the shared
`rng` stream. `_create_decision` never overwrites an existing invoice's
`amount` or `ground_truth_action` (an invoice is a fact recorded once), so a
re-run at a new limit wrote decisions whose recorded ground truth belonged to a
*different* invoice, and accuracy was scored against the wrong answer key.
Confirmed live on agent-01: after two clawbacks took it from INR 2,500 to the
floor, **all 20 invoices in its critical-error window still held amounts up to
INR 4,859 drawn at the old limit** — 20 of 20 mismatched against the plan that
produced those decisions — and a phantom critical error among them pinned drift
to `CRITICAL`, so no good run could ever clear it and the agent was stuck at
`CLAWBACK` forever. `current_limit` is now part of the id, which makes the id a
function of every input that decides the content; a repeat run at the *same*
limit still reuses its invoices, preserving the original intent.
**(2)** `_apply_any_clawback_the_run_earned` returned early for anything that
was not a `CLAWBACK`, which left the other half of ADR-0004 with no producer at
all: nothing anywhere generated an `INCREASE` recommendation, so a good run
raised the trust score until the ladder read INCREASE and no approval request
ever appeared. The only code in the project that generated one was the `/demo`
console. Renamed `_act_on_what_the_run_earned` and now handles both directions
— `generate_recommendation` already applies a `CLAWBACK` in the same
transaction and leaves an `INCREASE` `PENDING`, so handing it both produces the
asymmetry rather than bypassing it. Guarded against stacking duplicates when an
agent already has a pending recommendation, since re-running a simulation is
something a person does freely while rehearsing. **Why both were invisible:**
the backend suite runs on SQLite against a freshly seeded database where no
limit has moved yet, so the id collision cannot occur there; and the clawback
tests asserted the clawback path only. **Affects:**
`backend/tests/test_simulation_invoice_identity.py` (7 tests, including the
invariant "an id never names two different invoices" across every rung) and
three new tests in `test_simulation_clawback.py`; 300 backend tests pass. The
audit event for a failed attempt is renamed
`simulation_run.recommendation_not_generated`, since it no longer covers only
clawbacks. Verified live on a reset database: good run -> `PENDING` INCREASE to
INR 5,000 with the limit unchanged, two further identical runs adding nothing,
human approval -> INR 5,000, degraded run -> automatic CLAWBACK to INR 2,500
with no human step, and 700 of 700 stored invoices across two different limits
matching their own plan (0 mismatches, 0 amounts impossible at their own
limit). **Note for anyone reading this table:** the existing dev database had
to be reset — the corrupt invoice rows could not be repaired in place, because
the correct content for an already-written id is unknowable after the fact.

**2026-09-11 — Utkarsh (`uk/integration-dryrun`)** — The project is
deployable: backend on Render, frontend on Vercel. Three things blocked it, and
each one failed silently rather than loudly. (1) CORS was hardcoded to
`http://localhost:3000` (`backend/app/main.py`), so a deployed dashboard would
be blocked by the browser while the API answered every request correctly —
a misconfigured deployment and a backend with no data are indistinguishable
from the UI. Now `CORS_ALLOW_ORIGINS`, comma-separated, defaulting to the dev
origin and never to `*`. (2) The dashboard never sent `X-User-Role` at all
(`frontend/src/lib/api-client.ts` sent a `Bearer` token the backend ignores);
it had admin privileges purely because `current_user` defaults a header-less
request to ADMIN. That made the frontend's identity an accident of a
server-side default, invisible from the frontend code. It now sends the role
explicitly (`NEXT_PUBLIC_API_ROLE`, default `admin`), and `AUTH_DEFAULT_ROLE`
lets a deployment default anonymous callers to read-only AUDITOR instead.
(3) `DATABASE_URL` was read independently in three places
(`app/deps.py`, `app/seed.py`, `alembic/env.py`), none of which handled the
`postgres://` scheme several managed providers hand out — SQLAlchemy 2.0
dropped that alias and raises `NoSuchModuleError` naming a plugin rather than
the scheme, so the cause is hard to see. All three now read
`app.config.database_url()`, which normalises it. **Why a new `app/config.py`:**
every one of these is a value that differs between a laptop and a host, and
each needed a default that leaves local work and the existing suite behaving
exactly as before. **Deliberately not fixed:** identity is still a header the
caller chooses, so `AUTH_DEFAULT_ROLE=auditor` narrows what an
*unauthenticated* request can do and nothing about one claiming to be an admin
— pinned as `test_an_explicit_admin_header_still_wins` so the limit stays
visible rather than being mistaken for a security boundary. Real auth remains
out of scope. **Affects:** `render.yaml` and `docs/DEPLOYMENT.md` are new;
`backend/tests/test_deploy_config.py`, 20 tests. Verified live in the
deployment posture (`AUTH_DEFAULT_ROLE=auditor`): anonymous `GET /agents` 200,
anonymous `POST /simulation/runs` and `POST /decisions` both 403, the same POST
with `X-User-Role: admin` 201, preflight allowed for the configured origin and
absent for an unknown one. `backend/openapi.json` is unchanged — no endpoint
or schema moved. Free-tier caveats (the service sleeps after ~15 minutes, and
`BackgroundTasks` simulation runs can be stranded mid-flight by a cold start)
are documented rather than worked around; the recommendation is to demo
locally.

**2026-09-09 — Utkarsh (`uk/integration-dryrun`)** — The degraded simulation
phase now actually degrades. `_PHASE_PARAMS`
(`backend/app/services/simulation.py`) gave `degraded` ~86% expected accuracy
against `good`'s ~95%, and put a critical error on only 2.4% of decisions — so
the chance of one landing inside `CRITICAL_ERROR_WINDOW` (20 acted decisions)
was under half, and a degraded run usually finished with no drift, no clawback
and a trust score that had barely moved. Measured on a clean database at 150
invoices per phase: good 94.5% / trust 93.1 / NONE, degraded 89.5% / 91.9 /
NONE, recovery 93.0% / 92.8 / NONE — three phases, one story, nothing to
demonstrate. Retuned to `p_ground_truth_reject` 0.35 / `p_critical_error` 0.45
/ `p_noncritical_error` 0.30: ~65% expected accuracy and a critical error in
the recent window with probability ~0.97. Now, across all three seeded agents:
good ~93% / NONE / INCREASE, degraded ~62% / CRITICAL / CLAWBACK, recovery
~92% / NONE / INCREASE. **Why:** the parameter block's own comment already said
degraded "needs a meaningfully elevated critical-error rate for drift detection
and clawback to actually fire within a realistic window" — the numbers never
reached it. **Affects:** the dashboard's Simulation page can demonstrate
degradation for the first time (`backend/tests/test_simulation_phases.py`, 10
tests pinning the property rather than the numbers).

**2026-09-09 — Utkarsh (`uk/integration-dryrun`)** — A `201` from `POST
/api/v1/decisions` could arrive before its own row was readable.
`session_dependency_factory` commits in the `yield` dependency's teardown,
which runs after the endpoint returns, so the response could reach the client
first. Measured by polling for the row after the 201: 18 of 40 immediately
visible, 22 of 40 appearing after 28-97ms (median 51). At volume, read-back
404s were 96 of 200. `create_decision` now commits before the response is built
— still exactly one transaction per request, only its closing point moved — and
read-back 404s went to 0 of 200. **Why:** this was behind two days of
"intermittent" ruling failures: ~1% on a near-empty database and over 50% on a
populated one, because the commit outgrows the network round-trip as the table
grows. Not the dashboard competing for connections (the rate was *higher* with
the frontend stopped) and not the ruling endpoint (a plain `GET` reproduced it).
**Affects:** any client that creates a decision and immediately reads it back;
the simulator additionally now rules on escalations in a second pass per phase,
which is the more faithful model anyway.

**2026-09-09 — Utkarsh (`uk/integration-dryrun`)** — One critical error can no
longer cost every rung. `generate_recommendation` auto-applies a `CLAWBACK`
(ADR-0004), and `DriftSeverity.CRITICAL` is stateless — it asks only whether a
critical error sits in the recent acted window, with no memory of whether a
clawback already answered it. Generating a recommendation is something callers
repeat freely (a dashboard refresh, a retry, a simulator loop), so two calls
with no decisions between them dropped two rungs for one error: confirmed live
at 2500 -> 1000 -> 500. Guarded on
`agent_context(db, agent).decisions_since_last_change == 0` — a limit that just
moved with nothing recorded since is not new evidence. **Why:** ADR-0004
specifies exactly one rung per clawback. **Affects:**
`backend/tests/test_clawback_cascade.py`; Varun P.'s own auto-clawback tests
pass unchanged, and a genuinely new critical error still costs a rung.

**2026-09-09 — Utkarsh (`uk/audit-sampling`)** — Audit sampling implemented end
to end (ADR-0009). Selection at decision ingest at
`sampling_rate_of(agent.current_rung)`, inside the same transaction; `GET
/audit-samples` reads the real table with `?pending=` and `?agent_id=` filters
instead of serving `app/fixtures/audit.py`; `POST
/audit-samples/{id}/review` persists the review, appends a hash-chained entry,
emits `SAMPLE_REVIEW_DISAGREEMENT` on a `DISAGREED` verdict, and 409s a second
review. Selection is deterministic — a decision is chosen by hashing its id,
not by `random.random()`, so replaying a seeded run reproduces the same review
queue. Verified live against Postgres: agent-02 at rung 0 sampled 200 of 200,
agent-01 at rung 2 sampled 41 of 200 against an expected 0.25. Reviews
deliberately do **not** overwrite the decision's recorded ground truth: ADR-0009
describes reviewed samples eventually becoming the ground-truth source but flags
the contract gap that depends on as deferred, and substituting one for the other
would corrupt the simulator's deterministic ground truth. **Affects:**
`SAMPLE_REVIEW_DISAGREEMENT` and `SAMPLE_EVIDENCE_INSUFFICIENT` are now
reachable, so all 18 reason codes are live.

**2026-09-09 — Utkarsh (`uk/integration-dryrun`)** — Cached governance mode is
usable outside its own recordings, behind a switch. A recording is keyed by a
SHA-256 of the whole prompt, so it replays only for the exact evaluation it was
made from; every other evaluation raised `RecordingMissError` and the backend
turned that into a 503. Raising remains the default — a silent substitution
would hide that the panel was asked a question nothing had answered, which is
the failure this lane deliberately made loud, and Varun C.'s
`test_cached_mode_without_a_recording_raises_rather_than_stubbing` pins it.
`GOVERNANCE_ALLOW_STUB_FALLBACK=1` opts into falling back, and the
recommendation then reports `governance_mode="cached+stub"` and names the
substitution in its rationale, exactly as `live` already labels a fallback to
`cached`. **Affects:** `governance/tests/test_stub_fallback.py`; the rationale
sentence now names which mode actually wrote the text.

**2026-09-09 — Utkarsh (`uk/integration-dryrun`)** — `CRITICAL` drift is no
longer described as a measured degradation. The performance agent used one
sentence for `CONFIRMED` and `CRITICAL`, and `CRITICAL` has no statistics behind
it — it fires the moment a critical error appears in the recent window, without
running the two-proportion test — so `drop_pp` and `p_value` were `None` and the
rationale read "a drop of n/a (p=n/a). This is a measured degradation, not
noise." Seen live at 92.9% recent accuracy against a 71.9% baseline: the
sentence asserted a drop that had not happened, in text a reviewer is meant to
act on. Split into two branches.

**2026-09-09 — Utkarsh (`uk/dashboard-filters`)** — Two list filters the
dashboard had always sent and the API silently discarded. `list_recommendations`
had no `status` parameter, so all four approvals tabs returned every
recommendation; `list_decisions` had no `agent_id`, so the agent detail page
fetched the newest 50 across every agent and filtered in the browser, rendering
empty once another agent's run pushed one off the first page. Both now filter in
SQL, and `status` is typed as the enum so a bad value is a 422 rather than
another ignored parameter. **Why:** FastAPI drops unknown query parameters
silently, which is the worst version of this bug — the UI looks wired up, a 200
comes back, and the list never changes. **Affects:**
`backend/tests/test_list_filters.py`, 11 tests.

**2026-09-08 — Utkarsh (`uk/decision-ruling`)** — `POST
/decisions/{id}/ruling`, the write path for `human_ruling`. One of the four
trust-score components had no live source: `POST /decisions` hardcoded both
`recommended_action` and `human_ruling` to null, so `human_agreement` was always
dropped and every evaluation carried `AGREEMENT_EVIDENCE_INSUFFICIENT` and
`WEIGHTS_RENORMALISED`. `DecisionCreate` gained an optional
`recommended_action` (422 on ESCALATE, as ground truth already is) and the new
endpoint records the human's verdict — REVIEWER or ADMIN, ESCALATE-only, 409 on
a second ruling. The arc rules on its own escalations, with
`ScriptedAgent.recommend()` wrong at the same error rate as a real decision so
agreement carries signal instead of sitting at 100%. Verified live: agreement
0.892, all four components at nominal weight, neither renormalisation code
present.

**2026-09-07 — Varun P. (`vp/freeze-cleanup`)** — Approving a `HOLD`
recommendation no longer resets an agent's cooldown clock. `_record_decision`
(`backend/app/api/v1/recommendations.py`) called `apply_policy_version` on
every `APPROVED` verdict unconditionally, including a `HOLD` recommendation
whose `proposed_limit` already equals the agent's current limit
(`trust/trust_engine/ladder.py`'s `HOLD` branch always returns
`context.current_limit` unchanged) — writing a redundant `PolicyVersion` row
with the same limit but a fresh `effective_from`. Since
`app/services/trust.py:agent_context` derives `decisions_since_last_change`
from the *latest* version's `effective_from`, that no-op write reset the
cooldown clock to 0 regardless. Confirmed live: approving a rigged `HOLD`
recommendation dropped `decisions_since_last_change` from 2 to 0 without the
fix, and left it unchanged with it
(`backend/tests/test_recommendations.py::test_approving_a_hold_recommendation_does_not_reset_the_cooldown_clock`).
**Decided this was a bug, not a feature**: the cooldown
(`COOLDOWN_BETWEEN_INCREASES`) exists specifically so a lucky streak right
after a real promotion can't immediately trigger another one — it has no
relationship to whether a human clicked approve on a recommendation that
changed nothing. Resetting it on every approval, including HOLDs, meant an
agent could be made to wait out a fresh 100-decision cooldown indefinitely if
HOLD recommendations kept being generated and approved while it was
otherwise fully eligible for a real increase — actively working against the
cooldown's own stated purpose, not just redundant. Fix: only call
`apply_policy_version` when `row.proposed_limit != agent.current_limit`.
Approving a HOLD is still recorded (`approvals` row, `Recommendation.status`
flips to `APPROVED`) — only the no-op policy version write is skipped. Also
fixed a latent bug this exposed: the audit-log `event_type` was chosen from
`policy_version_id`'s truthiness rather than `verdict` directly, which would
have mislabelled a HOLD approval as `recommendation.rejected` in the audit
trail now that `policy_version_id` can be `None` on a genuine approval.
**Why:** docs/audits/2026-09-06-audit.md's freeze-cleanup item 4, read
against `trust/trust_engine/ladder.py` and
`backend/app/models/policy_versions.py` directly rather than guessed at.
**Affects:** `backend/app/api/v1/recommendations.py` only — no schema
change, no `shared/` change; `Recommendation.proposed_limit`/
`Agent.current_limit` were already both real columns being compared, not new
state.

---

**2026-09-07 — Varun P. (PR #32)** — `POST /api/v1/simulation/runs` actually
starts a run now instead of minting a fixture and returning: it creates a
`simulation_runs` row (`status=running`), commits it, then a
`BackgroundTasks` job deterministically generates `invoice_count` invoices,
runs a scripted decision over each, and submits every one through
`app.api.v1.decisions._create_decision` directly and in-process — the same
function the real HTTP endpoint calls, not a shortcut into the database.
`decisions_submitted` updates after every decision so `GET
/simulation/runs/{id}` reflects live progress; a failure mid-run marks the
row `FAILED` with an honest short count and the error, rather than either a
silently-short "completed" or an unhandled exception leaving the row stuck
at `running`. Found and fixed one real bug while writing the tests: the
background task's first draft read `app.deps`'s process-wide cached session
maker, which silently pointed every run at the wrong database under test
(every test overrides `get_session` per-test, not that global); fixed by
passing the triggering request's own DB engine into the task explicitly.
**Why:** `POST /simulation/runs` was the last major stub in the backend —
docs/audits/2026-09-06-audit.md's finding that "the Simulation Control Room
is fully non-functional against the real backend" (every `POST`'s `run_id`
404s on the very next poll, forever). **Affects:** `backend/` only. Does
**not** import `simulator/` — `app/schemas/simulation.py` already
established "mirror by value, not import" for this exact pair of lanes, and
`simulator/simulator/models.py`'s `Invoice.invoice_id` was, as of this PR,
still defaulting to an unseeded `uuid4()`, which would have made this
endpoint's required determinism impossible to guarantee. Adds migration
0004 (`simulation_runs` table). Live-verified against real Postgres: an
80-decision and a 2,000-decision run both completed correctly; decisions,
trust evaluation, and the audit chain all reflect the result.

---

**2026-09-07 — Varun P. (PR #31)** — Fixed two concurrency races in decision
ingest, both confirmed live under real Postgres load before and after.
**Race 1**: `_create_decision`'s `SELECT max(sequence)` read-then-insert
(`backend/app/api/v1/decisions.py`) raced under concurrency, colliding on
`uq_decisions_agent_sequence` and surfacing as an unhandled 500 — measured
at 78.4% of calls failing at 40-way concurrency before the fix, 0% after.
Fixed with `SELECT ... FOR UPDATE` on the agent row, serializing concurrent
decisions per agent. **Race 2**: `append_entry`'s unlocked read-then-append
of "the latest audit-log entry" let two concurrent appends chain off the
same predecessor with no exception — a silent fork, not a crash (59 forked
`prev_hash` groups out of 113 rows, measured live). Fixed with a
transaction-scoped Postgres advisory lock plus a new `audit_log.log_seq`
column (migration 0003): the lock alone wasn't sufficient, since `ts`
(caller-supplied wall-clock time) isn't guaranteed to match true insertion
order under concurrency — confirmed by the fact the fix's own test still
failed with only the lock, before `log_seq` was added. **Why:**
docs/audits/2026-09-06-audit.md sections 1a/1b, reproduced independently
before fixing. **Affects:** `backend/` only; no response-shape change
(`make openapi` confirmed byte-identical output).

---

**2026-09-07 — Utkarsh (PR #30)** — Simulator finalized. New `simulator arc`
command runs the full ten-beat demo story offline, in one command, no
backend or network needed — deterministic and byte-identical across runs
and `PYTHONHASHSEED` values. Fixed six real bugs found building it:
accuracy was counting escalations as mistakes (the trust engine already
doesn't); injected errors changed the ground-truth label instead of the
agent's action; injected errors were cancelling escalations rather than
only affecting decisions the agent was already going to act on; critical
errors could fire in the "good" phase and stall the ladder forever;
`--error-rate` wasn't actually wired to the phase; runs weren't tied to a
real `--agent-id`, so every submission 404'd. Also fixed the two
reproducibility bugs behind `simulator generate` crashing on every
invocation (`KeyError: 'APPROVE'` from a stale lowercase/`escalate` bucket
left over from PR #27's 2-way ground truth change; a `UnicodeEncodeError`
under Windows `cp1252` when piped) and two subtler ones underneath that
(unseeded `invoice_id`, hash-order-dependent `missing_field_names`).
**Why:** the Fri 4 Sept simulator-finalized deliverable
(`docs/DEADLINES.md`), and a bug that had left `main` unable to generate a
fixture at all since PR #27 landed. **Affects:** `simulator/` only.
`simulator/` still isn't in CI (a ready-to-paste block was handed to VP
separately); ruff cleanup (82 pre-existing findings) deliberately deferred.

---

**2026-09-07 — Adhya (PR #29)** — Two small but real frontend fixes on top
of PR #28. The Simulation Control Room's agent dropdown was a hardcoded
list of three agent ids/labels; it now calls `GET /agents` and renders
whatever agents actually exist, with loading and error states for when the
backend isn't reachable. `Providers.tsx`'s MSW gate compared
`NEXT_PUBLIC_MSW_ENABLED` alone; it now also requires
`NODE_ENV === "development"`, so a production build can't accidentally ship
with mocking on regardless of that one env var. **Why:** the simulation
console's agent list was already wrong the moment a fourth agent existed,
and the MSW gate was one misconfigured env var away from mocking data in
production. **Affects:** `frontend/` only, 5 files.

---

**2026-09-04 — Adhya (PR #28)** — Resolved all 10 items from the 2 Sept
frontend audit in one pass: reworked API types onto the real
`backend/openapi.json` contracts; fixed the endpoint paths for trust
evaluations, policy versions, recommendations, decisions, audit log, and
simulation; split recommendation resolution into separate approve/reject
calls with mandatory reasons (matching the real two-endpoint backend
shape); fixed the agents-list crash caused by reading fields `AgentOut`
doesn't have; wired the audit page to the backend's own `chain_valid`/
`chain_verified_scope` instead of a client-side hash-chain
reimplementation; added an application-level error boundary; added an
ESLint v9 flat config; deleted the dead `auditApi`. **Why:**
`docs/audits/2026-09-02-frontend-audit.md`'s 15-item punch list — most of
it landed in this one PR. **Affects:** `frontend/` only, 14 files.
`types/generated.ts` remains untouched and unused; the `0.85` hardcoded
threshold in `HorizontalThresholdGauge` was not part of this PR's scope and
is still open as of this entry.

---

**2026-09-02 — Adhya (PR #27)** — Two unrelated halves landed in one PR
whose title only names the frontend half. Frontend: ported the dashboard
onto the v1.1 contracts and the real 5-rung ladder (the PR body itself only
documents the simulator half — see
`docs/audits/2026-09-02-frontend-audit.md` section 1 for the gap this
left). Simulator (CR-1/2/3/5, in scope for Adhya at the time — simulator
ownership hadn't yet transferred to Utkarsh): the ground-truth labeller's
Rules 1/7/8/9 changed to emit `APPROVE`/`REJECT` only, never `ESCALATE` —
`DecisionRecord`'s ground truth is documented as always binary, and the
database's own `CHECK` constraint already enforced this at the persistence
boundary; `api_client.py`'s `submit_invoice()` replaced with
`submit_decision()`, posting a flat body to `POST /api/v1/decisions`
instead of the nonexistent `/api/v1/invoices` — this was the literal first
break in the vertical slice per `docs/audits/2026-08-31-state-audit.md`
section 6; removed the unused `google-generativeai`/`python-dotenv`
dependencies and fixed stale `AgentDecisionRecord`-referencing docstrings;
added 7 boundary-contract tests for the new payload shape. **Known,
admitted cost, deferred**: the 2-way labeller change broke pre-existing
tests that asserted the old 3-way `ESCALATE` behavior — fixed later in
PR #30. **Why:** the simulator fix closed the vertical slice's first break;
the frontend port started the 29 Aug deliverable, four days late.
**Affects:** `frontend/` (20 files) and `simulator/` (11 files) — the
simulator half is a legitimate, in-scope cross-directory change per
`docs/lanes/ad.md`'s "simulator/ + frontend/ (Adhya), through the port,"
not a boundary violation.

---

**2026-09-02 — Varun P. (PR #26)** — Closed the last broken hop: a
recommendation generated through `POST /agents/{id}/recommendations`
couldn't be approved through the API, because approve/reject still read
from fixtures. `POST /recommendations/{id}/approve` and `/reject` now write
a real `approvals` row and, on approve, apply the recommendation's
already-clamped `proposed_limit` via `apply_policy_version` —
`agents.current_limit`/`current_rung` never move any other way
(`app/models/guards.py` enforces this independently of the route code).
One transaction, one `audit_log` entry, ADMIN-only (403 for
reviewer/auditor), reason required on both paths, 409 on a non-`PENDING`
recommendation. `GET /agents/{id}/policy-versions` wired to the real table
(the last fixture-backed route on the agents router at the time). `GET
/audit-log` wired to the real table, recomputing the hash chain fresh on
every call and reporting `chain_valid`/`chain_verified_scope` rather than
just asserting immutability in a docstring. **Why:** Thu 3 Sept's approval-
workflow deliverable (`docs/DEADLINES.md`), and the specific gap
`docs/audits/2026-08-31-state-audit.md` named: the vertical slice broke at
every hop past decision ingest. **Affects:** `backend/` only. Verified live
end to end against real Postgres: decision → trust evaluation →
recommendation → approve → agent limit 2,500 → 5,000, rung 2 → 3 →
policy-versions (chained) → audit-log (`chain_valid: true`). Audit-samples
list/review and simulation runs remained fixture-backed, explicitly out of
scope here (audit-samples remains so as of this entry; simulation runs
fixed in PR #32).

---

**2026-09-01 — Varun P. (PR #25)** — Wired `GET /agents/{id}/trust` and
`POST /agents/{id}/recommendations` to the real trust engine and governance
coordinator, and to real persistence — the first time either path called
anything other than a fixture. Added the `recommendations.governance_mode`
column (migration 0002; `RecommendationOut` had required it since the
contract was written, and no column ever carried it). Fixed a foreign-key
ordering bug that was invisible on SQLite (the test suite's database) but
would have failed on Postgres. **Why:** the 31 Aug decision-ingest wiring
closed the first hop of the vertical slice; this closes the next two
(`docs/audits/2026-08-31-state-audit.md` section 6, hops 5 and 6). **Affects:**
`backend/` only. Live-verified against real Postgres.

---

**2026-09-01 — Varun P. (PR #22)** — Landed PR #16 (swappable providers,
30 Aug) and PR #21/#23 (real recordings, live mode, 1 Sept) on `main` for
real: both had merged into intermediate governance branches that never
reached `main` itself, so their content — the `LLMClient` protocol,
Gemini/Claude/OpenAI clients, the provider registry, ADR-0012, 15 committed
recordings, live mode with fallback, the `prompt_sha` staleness tripwire —
was invisible from a clean clone of `main` until this PR. No new content of
its own; recorded here only so `git log main` and this file agree on where
that content actually landed. Content already described by the existing
2026-08-30 (PR #16) and 2026-09-01 (PR #21/#23) entries below — not
re-described here to avoid two entries disagreeing over time.

---

**2026-09-01 — Varun C. (PR #21 into `vc/swappable-providers`, landed on `main`
via PR #23)** — Governance cached mode now replays real Gemini responses, and
live mode is open. Fifteen recordings committed, keyed
`agent.version.model.evidence`: `healthy_increase`, `thin_sample` and
`contested_increase` complete, three of four agents on `active_drift`;
`critical_error` and `at_ceiling` unrecorded. Live mode calls the provider with
a 25s per-agent deadline and falls back **to the recording, never to stub
text**, labelling the result `live+cached` and naming the fallen-back agents in
the rationale. The retry decision reads each error's `retryable` flag rather
than matching exception types. Three defects that only the live API could
expose, found by one probe call before the recording run: the default model
`gemini-2.5-flash` returns 404 for recently-created keys (now
`gemini-3-6-flash`); the 30s recording timeout sat inside the model's own
latency spread (9.9s/15.6s/33.3s observed, now 120s); and `record.py` documented
a `.env` file that nothing in the lane ever read. Added
`RecordingStaleError` — `Recording.prompt_sha` had described itself as a
tripwire since it was written and nothing compared it, so editing a prompt in
place replayed recordings of the older wording while looking perfectly healthy.
Added a `contested_increase` scenario because the conservatism ratchet had no
scenario in which it could fire against the real model: the 27 Aug
`has_dissent=True` figure was a stub artifact. **Why:** the 30 Aug and 3 Sept
deliverables, plus the discovery that the Gemini free tier is 20 requests per
**day**, not the ~10 per minute this lane had assumed — which makes a full
re-record cost more than a working day and turns silent staleness from a
nuisance into the likely failure. **Affects:** `governance/` only; no contract,
no `shared/`, no other lane. Cached and live both still advisory — no writes, no
policy mutation, `status` always `PENDING`. Also adds `governance/INTEGRATION.md`
for VP: `RecommendationOut.model_validate(recommendation)` validates with zero
adaptation, verified, and the full chain runs in 7.4ms with no network. Two
things still needed from the backend lane — how `governance` gets installed into
its environment, and when `recommendations.py` stops serving fixtures.

---

**2026-08-30 — Varun C. (PR #16)** — LLM provider is a per-agent setting
(`GOVERNANCE_PROVIDER`, overridable per agent with
`GOVERNANCE_PROVIDER_<AGENT>`). Claude and OpenAI clients are optional extras
with lazy imports and SDK exceptions mapped by class name, so the test suite
runs with neither package installed. `build_client()` is cached **per
provider**, not per agent, so two agents on one provider share a client and
therefore share a pacer — a rate limit belongs to the key, not the agent.
Structured output needed three schema dialects, not one: Gemini takes an
OpenAPI-3.0 subset with no `$ref`, while Claude and OpenAI take strict JSON
Schema requiring every field in `required`. **Why:** a panel that asks "is this
Gemini-specific?" should get a demonstration rather than an assurance.
**Affects:** `governance/` only. **Open:** ADR-0012 is still *Proposed* and asks
the team to rule on whether optional paid providers violate the lane brief's "do
not introduce any paid service"; the strict-reading rollback (drop the two
clients, keep the seam and the registry) is written up in the ADR. #16 merged on
CI, so that ruling is now retroactive and still owed.

---

**2026-08-29 — Varun C. (PR #15)** — Gemini client, recording store, and cached
mode end to end: a recorded response is looked up by cache key and validated
through the same `parse_opinion()` a live response would face. Raw HTTP rather
than the `google-genai` SDK, so the `responseSchema`/`responseMimeType` pair
that backs this lane's constrained-decoding claim is visible in a dict rather
than hidden behind a method; the key travels in an `x-goog-api-key` header,
never the URL, because query strings reach proxy logs. The client paces on a
**floor between calls** rather than a token bucket — a bucket permits exactly
the burst the free tier punishes. Every failure path raises: a missing recording
raises rather than falling back to stub text, and an unparseable response is
refused rather than saved. **Why:** the demo default is cached mode, and a
governance path that degrades quietly produces a demo that looks healthy and
isn't — which is the failure this whole lane exists to prevent. **Affects:**
`governance/` only. `scenarios.py` imports `trust_engine.evaluate()` at dev time
to build recording inputs; it is the lane's only trust import and is not on any
runtime path.

---

**2026-08-31 — Varun P. (PR #20)** — Wired `POST /api/v1/decisions` to real
persistence: one transaction opens the agent and its current policy
version, runs the real Policy Engine (`evaluate_decision`), persists an
`Invoice` (if new) and a `Decision` row referencing the policy version in
force, and appends a hash-chained `audit_log` entry — the first time this
endpoint did anything but mint a fixture and return. `GET /agents/{id}` and
`GET /decisions` switched from fixtures to real queries at the same time.
Added the tamper-detection test the audit-log hash chain was missing (a
test that actually mutates a persisted row and recomputes, not just hashes
from known inputs). **Why:** the Mon 31 Aug decision-ingest deliverable
(`docs/DEADLINES.md`) — `docs/audits/2026-08-31-state-audit.md` section 6
had identified this exact gap as the first break in the entire vertical
slice. **Affects:** `backend/` only. Verified against real Postgres;
`openapi.json` response shapes unchanged.

---

**2026-08-31 — Varun P.** — Branch cleanup, own branches only (`vp/*`,
`docs/*`, `chore/*`, `shared/*`; `uk/*`/`vc/*`/`ad/*` left untouched, not this
lane's to clean up). Deleted, local and remote, 12 branches verified merged
by diffing each branch's touched files against the exact merge/squash commit
that closed its PR (ancestry alone misses squash merges): `shared/v1-1-
recommendation-and-audit-sample`, `chore/infra-baseline`, `docs/reset-and-
reschedule`, `origin/docs/system-explained-merge`, `origin/chore/rename-and-
docs`, `origin/docs/resurrect-pr5`, `origin/docs/delta-audit-27aug`,
`vp/openapi-contract`, `docs/mentor-briefing`, `docs/landscape-research`,
`docs/audit-and-risk-fix`, `vp/schema-and-policy-engine` (PR #18, merged
today). Reset `vp/backend` onto `origin/main` instead of deleting it — it had
never diverged past the 17 Aug scaffold commit still shared with `uk/trust`/
`vc/governance`/`ad/simulator-frontend`, so the fix was to stop it pointing at
dead history, not to remove the branch. **Why:** all 18 PRs to date are
merged and nothing else is open in scope; an accurate branch list matters
more once `main` is close to demo-ready. **Affects:** branch list only, no
source changes; `git branch -d` (never `-D`) and `--force-with-lease` (never
`--force`) throughout, so anything genuinely unmerged would have been
refused rather than lost.

---

**2026-08-31 — Varun P. (via `vp/schema-and-policy-engine`)** — Wrote the
Policy Engine as a pure module (`backend/app/policy/`): `evaluate_decision`
(may the agent act, or must it escalate — missing/invalid policy version
fails closed, `SUSPENDED`/`RESTRICTED` escalate regardless of amount, an
amount exactly at the limit is allowed — inclusive ceiling, documented and
tested deliberately) and `clamp_recommendation` (the hard ceiling: a
proposed limit never rises above what the trust engine's evidence supports,
and the fact of clamping is always recorded, never silent). 35 tests,
including Hypothesis property tests (never allows above the limit; allowed
implies within-limit; deterministic under arbitrary input) and an
`ast`-based import-boundary test
that fails the build if the module gains a database, network, LLM, file I/O,
`os.environ`, or wall-clock dependency (ADR-0014, enforcing ADR-0003 in code
rather than by convention). Policy Engine reason codes live in
`backend/app/policy/reason_codes.py`, not `shared/reason_codes.py` — that
file's own scope is trust-evaluation reasoning, has no codes for "why did
this one decision get allowed or escalated," and `shared/` is frozen for
this branch; see ADR-0014 for the full argument and the promotion path if
the other three owners want these moved into `shared/` later. **Why:** Sat
29 Aug deliverable (docs/DEADLINES.md) — "the single most important module
in the project," per this lane's own brief. **Affects:** `backend/app/
policy/` and its tests only; does not touch `shared/`, and the Policy Engine
package itself imports nothing from `backend/app/models/` (verified by the
same import-boundary test).

**2026-08-31 — Varun P. (via `vp/schema-and-policy-engine`)** — Wrote the
persistence layer: SQLAlchemy models for every table in docs/lanes/vp.md's
schema (`backend/app/models/`), one Alembic migration
(`backend/alembic/versions/0001_initial_schema.py`, verified to apply
cleanly to an empty database and downgrade back to empty — against SQLite,
since no Postgres service exists in CI or is guaranteed on every
contributor's machine; see ADR-0013), and a deterministic seed script
(`backend/app/seed.py`, `make db-reset`) telling the same three-agent story
`app/fixtures/` already tells, using the *same* ids so a later switch from
fixture-stubbed responses to real persistence changes nothing the frontend
sees. `agents.current_limit`/`current_rung` is protected two ways: a
`CheckConstraint` generated from `shared.constants.AUTONOMY_LADDER`
(`ck_agents_rung_matches_limit`, rejects any pair not on the real ladder) and
a `before_flush` session hook (`app/models/guards.py`) that refuses to flush
an agent whose limit changed without a paired `policy_versions` row in the
same transaction — `apply_policy_version()` is the one sanctioned way to
change both together. The same hook makes `policy_versions` and `audit_log`
append-only (raises on any UPDATE or DELETE attempt) rather than relying on
nobody writing one. The hash-chain helper (`app/models/audit_hash.py`:
`sha256(prev_hash + canonical_json(payload))`) reuses the exact algorithm
`app/fixtures/audit.py` had already hand-rolled, with its own test suite
proving determinism and tamper-evidence independent of any caller — ingest
wiring (appending an entry on every mutating request) is separate work.
Wrote ADR-0013 for JSONB over normalised storage on the two evidence-snapshot
columns (`trust_evaluations.payload`, `recommendations.agent_opinions`).
**Why:** Fri 28 Aug deliverable (docs/DEADLINES.md: "`make db-reset` produces
a seeded DB"), already slipped into this weekend. **Affects:** `backend/
app/models/`, `backend/alembic/`, `backend/app/seed.py`, `backend/
pyproject.toml` (added `sqlalchemy`, `alembic`, `psycopg2-binary`,
`hypothesis`). Does not touch `shared/`, and does not wire any API endpoint
to the database — the stubs in `backend/app/api/v1/` are untouched; that is
Mon 31 Aug / Thu 3 Sept work.

---

**2026-08-27 — Varun P. (via `vp/openapi-contract`)** — Published the complete
backend HTTP contract: `backend/app/main.py`, all eighteen endpoints stubbed
against internally-consistent fixtures (three agents — one mid-ladder and
eligible for an increase, one on probation with a small sample, one clawed
back after confirmed drift), `export_openapi.py`, `backend/openapi.json`
committed. Every response model mirroring a `shared/contracts.py` dataclass
(`DecisionRecord`, `TrustEvaluation`, `DriftResult`, `ProportionResult`,
`ScoreComponent`, `Recommendation`, `AgentOpinion`, `AuditSample`,
`AgentContext`) does so field-for-field, enforced by a generalized
contract-drift test (`backend/tests/test_schema_contracts.py`) rather than
nine hand-written ones. Wrote ADR-0011 for the two decisions made once and
applied everywhere: the `items`/`total`/`page`/`page_size` pagination
envelope, and a mandatory `reason` on every mutating endpoint, including the
two (`POST /decisions`, `POST /simulation/runs`) that don't look like
governance decisions at first glance. No database models, migrations, or
SQLAlchemy anywhere in this branch — persistence is separate work
(docs/DEADLINES.md: Fri 28 Aug onward). **Why:** Tue 25 Aug deliverable,
already two days late (docs/audits/2026-08-27-delta-audit.md); the frontend
lane has been blocked on this file since 21 Aug. **Affects:** `backend/`
only. Also fixed the dev environment while touching it: `make setup` /
`scripts/setup.ps1` now install all four Python lanes (trust, simulator,
governance, backend) editable unconditionally — the old conditional
"install if it exists" logic was written when backend/governance were
empty and silently left simulator out entirely; `make test` runs pytest
across all four; `docs/ONBOARDING.md` updated to match, since "install only
`trust[dev]`" left simulator and governance tests failing on import with no
obvious cause for anyone following it literally.

**2026-08-27 — Varun (via `docs/resurrect-pr5`)** — Re-applied PR #5's commit
(`6a77eed`, "transplant Wilson table, ADR defend-it lines, layer walkthrough
into SYSTEM-EXPLAINED.md"), cherry-picked cleanly onto `main` as `7a169f6`.
**Why:** PR #5 was opened with base `docs/reset-and-reschedule` instead of
`main`. PR #4 (`docs/reset-and-reschedule` → `main`) merged at
2026-08-23T13:15:55Z; PR #5 merged its commit into `docs/reset-and-reschedule`
32 seconds later, at 13:16:27Z — by which point that branch had already done
its one job and nobody opened a follow-up PR to carry the new commit into
`main`. GitHub shows PR #5 as `MERGED`, and it was, into a branch that never
reached `main` — `origin/docs/reset-and-reschedule` still exists, still
sitting at that commit, four days later. Content was verified missing
(`git merge-base --is-ancestor 6a77eed origin/main` → false) and restorable
without conflict (`main`'s `docs/SYSTEM-EXPLAINED.md` was still byte-identical
to `6a77eed`'s parent, so nothing on `main` needed reconciling). **Affects:**
`docs/SYSTEM-EXPLAINED.md` only — the Wilson lower-bound numbers table, a
"Defend it" line on all 10 ADRs, and the plain/technical layer-by-layer
walkthrough are back; verified in `docs/audits/2026-08-27-delta-audit.md` §1
and the PR that carries this entry. `docs/RISKS.md`/`docs/CONTEXT.md`'s own
staleness (unrelated to this gap) is not addressed here.

**2026-08-26 — Utkarsh (via `uk/autonomy-ladder`, PR #7)** — Landed the autonomy
ladder and the `evaluate(decisions, context) -> TrustEvaluation` orchestrator —
the single function the backend calls. Retired the lane-local `ScoreResult`
dataclass; `compute_trust_score()` now returns a plain tuple and
`TrustEvaluation` is `trust/`'s only public result type. The ladder implements
the documented split between `eligible_for_increase` (evidence supports a raise)
and `direction` (a cooldown or post-clawback recovery window can still hold it
at `HOLD`), plus clawback-first evaluation that drops exactly one rung on
confirmed or critical drift with no cooldown of its own. 15 of the 18 scoped
reason codes are reachable and individually tested; the 3 audit-sample /
recommendation-clamping codes are out of this lane's scope (they belong to
`backend/` or a future audit-sampling module — flagged for Varun P.). 174 trust
tests pass. **Affects:** `trust/` only. No new ADR — implements decisions
already recorded in ADR-0002 / 0004 / 0006 / 0007.

**2026-08-24 — Varun C. (via `vc/langgraph-skeleton`)** — Set governance's
default mode to `stub`, not `cached`. `resolve_mode()` raises `ValueError` on
an unrecognised mode rather than falling back. **Why:** an unset
`GOVERNANCE_MODE` must never reach for a fixture directory that does not exist
yet, and must never be one typo away from a live API call. A mode that
silently degraded to stub would look like a working demo while proving
nothing. **Affects:** anything invoking `governance.recommend()`; `cached`
becomes the demo default explicitly, at the call site, once it exists (30 Aug).

**2026-08-24 — Varun C. (via `vc/langgraph-skeleton`)** — `Recommendation.
trust_evaluation_ref` is supplied by the caller, not generated in governance.
**Why:** `TrustEvaluation` carries no identity field, and the backend is the
only component that persists both sides; minting an id here would produce a
reference pointing at nothing. **Affects:** `vp/backend` — the backend must
pass its own evaluation id into `recommend()`. **Needs Varun P.'s
confirmation**, as it changes the call signature he builds against.

**2026-08-24 — Varun C. (via `vc/langgraph-skeleton`)** — Governance's audit
agent detects unruled escalations and critical-error clustering, but *not* the
per-vendor or time-windowed anomalies described in `docs/lanes/vc.md`.
**Why:** those need `DecisionRecord` history; `TrustEvaluation` carries only
aggregates. Widening the input is a cross-lane contract change and `shared/`
is frozen until 9 Sept (ADR-0005). **Affects:** scope of the audit agent;
revisit via ADR if the wider input is wanted.

**2026-08-23 — Varun (via `docs/reset-and-reschedule`)** — Named Utkarsh
backup reviewer: if Varun P. is unavailable, Utkarsh can approve and merge
anything that doesn't touch `shared/` (which still needs all four owners).
**Why:** Varun P. has interview commitments 24 Aug - 3 Sept and the team
cannot afford a lane sitting blocked on review during that window (RISKS.md
R12). **Affects:** the review process for every lane but `shared/` itself;
documented in `docs/DEADLINES.md` and `docs/lanes/uk.md`.

**2026-08-23 — Varun (via `docs/reset-and-reschedule`)** — Deleted
`infra/grafana/.gitkeep` and `infra/prometheus/.gitkeep`, and the now-empty
`infra/grafana/` and `infra/prometheus/` directories. **Why:** dead scaffold
left over from the 17 Aug initial commit for an observability stack
`docker-compose.yml` already explicitly states is cut from scope ("No Redis,
no Celery, no observability stack (Prometheus/Grafana)"). Flagged in
`docs/audits/2026-08-23-state-audit.md` §4. **Affects:** `infra/` only;
nothing referenced either directory.

**2026-08-23 — Varun (via `docs/reset-and-reschedule`)** — Committed
`docs/DEADLINES.md`, `docs/ONBOARDING.md`, `docs/SYSTEM-EXPLAINED.md`, and
`docs/lanes/{ad,uk,vc,vp}.md` to `main` for the first time. **Why:** all of
these existed only outside git before today. That is the direct, named root
cause (RISKS.md R11, ADR-0010) of ~35,600 lines being built on
`origin/ad/simulator-frontend` against an invented `shared/`, because nobody
who cloned the 17 Aug scaffold could discover the real one existed. A
document that isn't in the repository doesn't exist as far as a fresh clone —
or a fresh clone's AI assistant — is concerned. **Affects:** every lane;
`docs/ONBOARDING.md` now tells anyone joining, or anyone whose local branch
predates 23 Aug, exactly how to catch up.

**2026-08-23 — Varun (via `docs/reset-and-reschedule`)** — Merged the
stranded `origin/docs/audit-and-risk-fix` branch (the RISKS.md three-week
timeline correction and the 2026-08-21 pre-merge audit file), 2 days after it
became mergeable. **Why:** it was clean and conflict-free the entire time —
there was no technical reason for it to sit unmerged; it was simply never
picked up. **Affects:** `docs/RISKS.md`, adds
`docs/audits/2026-08-21-pre-merge-audit.md`.

**2026-08-23 — Varun (via `docs/reset-and-reschedule`)** — Wrote ADR-0010:
`main`'s frozen v1.1 `shared/` contracts are canonical; the divergent design
independently built on `origin/ad/simulator-frontend` is partially ported
(estimated 7-9 person-days per `docs/audits/2026-08-23-port-feasibility.md`),
not merged as-is and not discarded. Submission moved **12 September → 15
September 2026**; feature freeze moved **6 September → 9 September 2026**,
giving the port three extra days of runway it would not otherwise have had.
**Why:** two incompatible definitions of the core domain cannot coexist in a
system whose entire premise is a single auditable source of truth (ADR-0005);
discarding ~5,900 lines of real, working, port-feasible code costs more
(10-13 person-days) than fixing its foundation (7-9). **Affects:** `shared/`,
`simulator/`, `frontend/`; every date in `docs/DEADLINES.md` and
`docs/lanes/*.md`; see ADR-0010 for full reasoning, `docs/RISKS.md` R9 for
the risk this closes down to a scoped, owned, dated piece of work.

---

**2026-08-21 — Varun (via `chore/infra-baseline`)** — Filled in every 0-byte
root-level placeholder: `docker-compose.yml` (postgres:16 + adminer, no
Redis/Celery/observability), `.env.example`, `Makefile`
(setup/up/down/db-reset/test/test-trust/openapi/lint/fmt/dev/frontend, all
skipping gracefully when a lane has no code yet), `.github/workflows/ci.yml`
(same skip-gracefully philosophy, plus an `openapi.json` staleness check),
`.gitattributes`, `README.md`, and `scripts/*.ps1` for teammates without GNU
make. Also fixed 19 pre-existing ruff findings in `trust/` (mechanical only —
import order, `range(0, n)`, one NaN-check inconsistency between two
near-identical `_clamp01` helpers; deliberately did not run `ruff format`,
which would have restyled the author's test-constructor style far beyond
what the findings needed) so the new CI lint step doesn't fail on day one.
**Why:** nothing in the repo ran end to end before this — `docs/DEADLINES.md`
needs a working `make setup && make up` for Phase 2 to start. **Affects:**
every lane; `make setup`/`make test-trust` verified working on this machine
(after fixing a Windows backslash-path bug the first run caught); `make
up`/`down` could not be verified here — no Docker installed on this machine.

**2026-08-21 — Varun (via `shared/v1-1-recommendation-and-audit-sample`)** —
Shipped `shared/` v1.1 (`SCHEMA_VERSION` "1.0" → "1.1"), the Phase 1 deliverable
from `docs/DEADLINES.md`: added `Recommendation` + `AgentOpinion` (governance's
output contract, mirroring `TrustEvaluation`'s role for the trust lane) and
`AuditSample` (the rung-scaled post-hoc review mechanism, ADR-0009) to
`shared/contracts.py`; `RecommendationStatus`, `OpinionVerdict`, `ReviewVerdict`
to `shared/enums.py` (uppercase values, with a comment flagging — not fixing —
`AgentState`'s pre-existing lowercase inconsistency); `SAMPLING_RATE_BY_RUNG`,
`MIN_SAMPLES_FOR_ACCURACY_ESTIMATE`, `sampling_rate_of()` to
`shared/constants.py`; `SAMPLE_EVIDENCE_INSUFFICIENT`, `SAMPLE_REVIEW_DISAGREEMENT`,
`RECOMMENDATION_CLAMPED` to `shared/reason_codes.py`. Documented, as docstrings
on `TrustEvaluation`, the two ambiguities `AUDIT.md` flagged: the
`eligible_for_increase`/`direction` relationship and the
`current_limit`/`current_rung` invariant. Purely additive — no existing field or
type changed; a short list of contract gaps this surfaced (notably: no field
distinguishing ground-truth-derived vs. sample-derived accuracy on
`TrustEvaluation`) was handed back for a decision rather than added to the diff.
**Why:** `docs/DEADLINES.md` requires `shared/` to freeze at this merge with all
four lane owners' approval, and everything downstream of it (governance's
recommendation output, the backend's review queue, the trust engine's
production ground-truth source) is blocked on this contract existing first.
**Affects:** all four lanes — this is the last `shared/` change permitted
before the 6 September feature freeze per the standing rules in
`docs/DEADLINES.md`.

**2026-08-21 — Varun (via `chore/rename-and-docs`)** — Merged
`origin/uk/shared-trust-contracts` and `origin/uk/trust` into
`chore/rename-and-docs` before doing any rename/documentation work, since
those are the only two branches with real content and the trust engine cannot
be discussed, cited, or edited without them. **Why:** the repo-wide rename and
documentation bootstrap referenced files (`trust/pyproject.toml`,
`trust/trust_engine/`) that only exist on `origin/uk/trust`, and that branch
only imports cleanly once `shared/contracts.py` is populated from
`origin/uk/shared-trust-contracts`. **Affects:** this branch now has a working
trust engine + populated `shared/`; `main` itself still does not — that merge
is a separate, still-pending step (see Risk R2 in `docs/RISKS.md`).

**2026-08-21 — Varun** — Commissioned a full pre-merge audit of `uk/trust`
(`AUDIT.md`). Found the branch unmergeable as committed: its copy of
`shared/contracts.py` was still the empty placeholder, because `uk/trust` and
`uk/shared-trust-contracts` diverged independently from the same base commit
and neither was ever merged into the other. Also found the autonomy
ladder/cooldown/clawback logic — half of `uk/trust`'s ownership brief — is
entirely unimplemented (constants defined, nothing reads them), and that no
function anywhere produces the `TrustEvaluation` contract type. **Why:**
pre-merge gate before any lane starts integrating against `uk/trust`.
**Affects:** merge order and the fix checklist for `uk/trust`; directly
motivated the merge above.

**2026-08-20 15:57 (`70b3b96`) — Utkarsh Sahgal** — Pushed `origin/uk/trust`:
Wilson score interval, rate/proportion calculations (accuracy, utilization,
human agreement), two-stage drift detection, trust-score composition, and a
113-test suite. **Why:** `uk/trust`'s ownership brief — the statistical
evidence engine. **Affects:** `trust/` package. Notably does *not* implement
the autonomy ladder or cooldowns despite `trust/trust_engine/constants.py`
already defining their thresholds.

**2026-08-19 17:06 (`5721b1e`) — Utkarsh Sahgal** — Removed placeholder
`.gitkeep` files in `trust/`, replaced by real package contents. **Affects:**
`trust/` directory structure only.

**2026-08-19 17:03:58 (`f606d6a`) — Utkarsh Sahgal** — Added `trust/pyproject.toml`,
package `__init__.py` files, and `DecisionRecord` contract tests. Branched
from `main`, **not** from `uk/shared-trust-contracts` (pushed by the same
author two minutes earlier) — this is the root cause of the later divergence
resolved on 2026-08-21 above. **Affects:** `trust/` package skeleton.

**2026-08-19 17:02:09 (`444fdc0`) — Utkarsh Sahgal** — Pushed
`origin/uk/shared-trust-contracts`: populated `shared/enums.py`,
`constants.py`, `reason_codes.py`, and `contracts.py` — the cross-lane treaty
files, including `TrustEvaluation`, `DecisionRecord`, `AgentContext`,
`DriftResult`, `ProportionResult`, `ScoreComponent`. **Why:** give all four
lanes a common contract to build against (ADR-0005). **Affects:** every lane,
in principle — as of 2026-08-21, only this branch (`chore/rename-and-docs`)
and its own origin branch actually have it; `main` and the other three lane
branches do not yet.

**2026-08-17 23:56:58 (`c126543`) — Varun** — "chore: initialize project
structure": created the four-lane directory skeleton (`backend/`, `trust/`,
`governance/`, `simulator/`, `frontend/`, `shared/`, `infra/`) as empty
placeholder files (`.gitkeep`, empty `__init__.py`, 0-byte config files).
**Why:** establish the four-owner branch structure for the capstone.
**Affects:** baseline for every subsequent lane branch (`vp/backend`,
`uk/trust`, `vc/governance`, `ad/simulator-frontend` all still point at this
exact commit as of 2026-08-21, except `uk/trust`'s origin, per above).
