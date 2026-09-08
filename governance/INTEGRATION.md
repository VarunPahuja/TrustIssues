# Calling governance from the backend

For **VP**. The measurements below were run on `vc/recording-fixes` on 1 Sept 2026 and
are measured, not estimated. Revised 8 Sept, after #25 wired this lane in and while
#34 (auto-applied clawbacks) was still open. Governance owns this file — if something here is wrong,
it's my bug, come to me.

## The whole thing

```python
from governance.coordinator import recommend

recommendation = recommend(
    evaluation,                                   # a TrustEvaluation from trust_engine
    mode=None,                                    # None reads GOVERNANCE_MODE, default "stub"
    trust_evaluation_ref="trust-eval-agent01-002", # you supply this; see below
)
```

`recommend()` is the entire public surface of this lane. There is nothing else to call
and no object to construct first.

**It returns `shared.contracts.Recommendation`, and `RecommendationOut` already mirrors
it field-for-field with `from_attributes=True`.** So the wire step is:

```python
RecommendationOut.model_validate(recommendation)
```

I ran that against a real recommendation — it validates with **zero adaptation**. No
field renaming, no shim, no missing attribute:

```
model_validate OK
 agent_id     : agent-demo-01
 direction    : Direction.HOLD
 proposed     : 1000 rung 1
 has_dissent  : True
 opinions     : [('risk','CONCUR'), ('performance','OBJECT'),
                 ('compliance','CONCUR'), ('audit','CONCUR')]
 status       : RecommendationStatus.PENDING
 clamped      : False
```

## The full chain, timed

Decisions → `trust_engine.evaluate()` → `recommend()` → `RecommendationOut`:

```
stub    evaluate=0.3ms  recommend=5.2ms  serialise=0.1ms  -> HOLD 1000 dissent=True
cached  evaluate=0.2ms  recommend=7.4ms  serialise=0.0ms  -> HOLD 1000 dissent=True
```

**Cached mode does no network I/O** — it reads recorded model responses off disk. 7.4ms
is safe to call inside a request handler. Live mode is open now, but it makes real API
calls — see below.

## Which mode

`GOVERNANCE_MODE` is read from the environment when you don't pass `mode` explicitly.

| Mode | What it does | Needs |
|---|---|---|
| `stub` | Hand-written reasoning, no model | nothing |
| `cached` | Replays real recorded Gemini responses | recordings on disk |
| `live` | Calls the API, falls back to the recording on any failure | a key, and recordings as the safety net |

Default is `stub`, deliberately: an unset environment must never reach for a fixture
directory that may not exist, and must never be one typo away from a live API call. An
unrecognised value raises rather than falling back — a typo'd `GOVERNANCE_MODE` that
quietly ran in stub mode would look exactly like a working demo.

**For the checkpoint, `stub` is enough** and needs nothing from me. Use `cached` when you
want real model reasoning in the response.

### Live mode, if you use it

`live` calls the provider and **falls back to the recording for the same evidence** on
any failure — network down, rate limited, timed out, unparseable response. The deadline
is 25s per agent, deliberately much shorter than the recording timeout: nobody watching
a demo waits two minutes to learn the network is down.

Two things to know before you wire it to a request path:

- **It is not free and not fast.** Four agents, four API calls, paced 6s apart on the
  free tier. Budget tens of seconds, not milliseconds. `cached` is the mode for a
  request handler; `live` is for showing the thing actually works.
- **A fallback is visible in the output, deliberately.** `governance_mode` comes back as
  `"live+cached"` rather than `"live"`, and the rationale names which agents fell back.
  If you store or display `governance_mode`, expect that value — it is not a bug. A
  recommendation that claimed to be `live` when recordings answered would be exactly the
  kind of quietly-wrong result this lane exists to prevent.

If every agent's live call fails **and** there is no recording for that evidence, it
raises `RecordingMissError` naming both failures. Live mode's guarantee holds only where
a recording exists, which for the demo scenarios it does.

⚠️ **One caveat on `cached` today.** Only `healthy_increase`, `thin_sample` and
`contested_increase` are fully recorded (15 of 24 calls). The Gemini free tier turns out
to be **20 requests per day**, so the rest lands over the next day or two. A cached call
for unrecorded evidence **raises `RecordingMissError`** — deliberately, see Failures
below. If you're wiring against arbitrary evidence right now, use `stub`.

## Shapes that will bite you

These cost me time; they're written down so they don't cost you any.

- **`AgentContext` has no `agent_id`.** Only `current_limit`,
  `decisions_since_last_change`, `decisions_since_clawback`, `state`.
- **`TrustEvaluation.agent_id` comes from `decisions[0].agent_id`.** An empty decision
  list yields the string `"unknown"`, not an error. `DecisionRecord` *does* have
  `agent_id`; `AgentContext` does not.
- **`AgentState` is `PROBATION` / `ACTIVE` / `RESTRICTED` / `SUSPENDED`.** There is no
  `NORMAL`.
- **`trust_evaluation_ref` is caller-supplied and optional (`str | None`).**
  `TrustEvaluation` carries no identity of its own, and you're the only component
  persisting both sides — inventing an id in this lane would produce a reference
  pointing at nothing. Your fixtures already use the right shape
  (`trust-eval-agent01-002`), so this needs no change from either of us.

## What governance will never do to you

Guaranteed by this lane, enforced in code, not just documented.

**These are guarantees about the `Recommendation` object `recommend()` hands back — not
about the row you persist.** You own the row. Where the two differ below, that is your
lane deciding something, working exactly as intended, and not a governance bug. The
distinction matters because both of the first two fields are ones you legitimately change
on the way to the database.

- **`status` is always `PENDING`.** Governance cannot approve its own recommendation;
  an increase needs a human (ADR-0004). Nothing here can set `APPROVED`.
  *What you persist is yours to decide:* ADR-0004 requires a human for an increase and
  deliberately does not for a clawback, so a `CLAWBACK` row written as `APPROVED` and
  applied without a human is that ADR being honoured, not this guarantee being broken.
  Governance still never asked for it.
- **`clamped` is always `False` and `clamped_from` always `None`.** Clamping is yours.
  Governance never reports itself as clamped.
  *So a persisted row with `clamped=True` is the hard ceiling working* — it means your
  `clamp_recommendation` lowered what this lane proposed. Only ever set it from your own
  clamp, never by copying these fields through.
- **`proposed_limit` never exceeds `evaluation.recommended_limit`.** There's an
  `AssertionError` guarding it. Your hard ceiling should still be enforced — this lane
  just doesn't rely on being caught.
- **No writes of any kind.** No database, no policy mutation, no limit changes. Pure
  function of the evidence you hand it (ADR-0001).

Dissent only ever ratchets toward caution: an `OBJECT` downgrades a proposed INCREASE to
HOLD. Nothing can turn a HOLD into an INCREASE or soften a CLAWBACK.

## Failures

Everything raises rather than degrading quietly, which is deliberate — a governance
path that silently falls back produces a demo that looks healthy and isn't.

| Raises | Means | Fix |
|---|---|---|
| `ValueError` | unknown `GOVERNANCE_MODE` | typo in the env var |
| `RecordingMissError` | cached mode, no recording for this evidence | record it, or use `stub` |
| `RecordingStaleError` | a prompt file was edited without a version bump | my problem, not yours — tell me |
| `OpinionParseError` | a recorded response failed validation | my problem — tell me (note: `ValueError`, not `GovernanceLLMError`) |

**Catching one exception type is not enough**, and this is the sharp edge:

- `RecordingMissError` and `RecordingStaleError` inherit `GovernanceLLMError` and carry
  `retryable = False`.
- **`OpinionParseError` inherits `ValueError`, not `GovernanceLLMError`**, and has no
  `retryable` attribute. `except GovernanceLLMError` will *not* catch it.
- `ValueError` for a bad mode is a programming error and shouldn't be caught at all —
  fix the env var.

So if you want the endpoint to survive governance failures:

```python
try:
    rec = recommend(evaluation, trust_evaluation_ref=ref)
except (GovernanceLLMError, OpinionParseError):
    rec = recommend(evaluation, mode="stub", trust_evaluation_ref=ref)
```

Nothing here is worth retrying — every failure above is a repo or disk state, not a
transient one, so a retry spends time to reach the same failure.

I'd rather fix the hierarchy than have you write that tuple. Tell me if you want
`OpinionParseError` folded under `GovernanceLLMError` and I'll do it in this lane — it's
a one-line change plus a test, and it makes your handler a single `except`.

## Both things I needed from you: done (#25, 1 Sept)

Kept rather than deleted, because the answers are the useful part now.

1. ~~**Packaging.**~~ Settled: CI does `pip install -e governance`, and
   `backend/pyproject.toml` sets `pythonpath = [".", ".."]` for the test run.
2. ~~**When you plan to wire it.**~~ Done the same day.
   `app/services/governance.py:generate_recommendation` calls `recommend()` on real
   persisted decision history and `app/api/v1/agents.py` exposes it. The shapes did line
   up — no adaptation was needed, as promised above.

**One thing left, and it is a scope question rather than a bug.** Nothing calls
`recommend()` in the demo path. `simulator/simulator/arc.py` in offline mode — the
default, and the mode that proves determinism — reads `direction` straight off the
`TrustEvaluation` and applies the ladder itself. Online mode does go through your
endpoints, but `.env` pins `GOVERNANCE_MODE=stub`, and your own
`test_cached_mode_with_no_matching_recording_returns_503_not_500` records why cached
cannot work there: real DB-derived evidence matches none of the committed demo-scenario
recordings.

So the arc demonstrates the ladder correctly and shows hand-written stub reasoning while
doing it. That is defensible — governance is advisory, so the ladder works without it —
but it means the recorded Gemini panels are not in the end-to-end flow. Closing it means
recording the evidence each of the arc's six resolve points actually produces (6 x 4 = 24
calls), and first verifying that the evidence your `compute_trust_evaluation` derives
matches what the offline arc computes. Raised at standup 9 Sept as a scope call, not
something this lane should decide alone.

## Not built yet

- **Per-vendor and time-clustered anomaly detection** in the audit agent. Needs
  `DecisionRecord` history, which `TrustEvaluation` doesn't carry. Widening that input is
  a cross-lane contract change, so it needs an ADR before any code. Flagging it because
  it's the one thing an audit-focused question at the panel will expose.
