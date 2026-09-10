# ADR-0009: Post-hoc audit sampling as the ground-truth mechanism, rate tied to rung

## Status

Accepted and enforced in code as of 2026-09-09 — see Consequences.

## Context

Every accuracy, critical-error, and drift number the trust engine computes today
depends on `DecisionRecord.ground_truth` — and that field only exists because the
simulator can attach a deterministic correct answer to every synthetic invoice
(`shared/enums.py`, `Action`'s own docstring: "every synthetic invoice carries a
deterministic correct answer"). Once the system runs against real invoices, nothing
hands the trust engine a `ground_truth` at decision time — nobody knows an invoice
was wrongly approved the instant it's approved. Something has to supply correctness
after the fact, or the entire evidence chain (ADR-0001, ADR-0002) has nothing left to
measure against.

Separately, ADR-0003's Policy Engine already treats an agent's authority as something
that scales with earned trust — a fixed rupee ceiling that grows. Review burden is
just as real a cost as the rupee ceiling, and nothing about "earned autonomy" is
delivered if a trusted agent still has 100% of its decisions checked by a human; the
ceiling would be higher, but the operational payoff — less oversight required — would
never show up.

## Decision

A fraction of an agent's autonomous decisions are pulled for human review after the
fact (`AuditSample`, `shared/contracts.py`), at a rate set purely by the agent's
current rung: `SAMPLING_RATE_BY_RUNG = (1.0, 0.50, 0.25, 0.10, 0.05)`
(`shared/constants.py`) — every decision reviewed at the floor rung, down to 1-in-20
at the top rung. `sampling_rate_of(rung)` clamps out-of-range rungs the same way
`limit_of()` already does. `MIN_SAMPLES_FOR_ACCURACY_ESTIMATE = 20` is a separate
floor on *reviewed* sample count, independent of rate — a low-volume agent at the top
rung could clear "5% sampled" correctly and still have too few actual reviews to mean
anything, which is exactly what `SAMPLE_EVIDENCE_INSUFFICIENT`
(`shared/reason_codes.py`) exists to flag.

Reviewed samples (`AuditSample.verdict`, `.reviewer_action`) become the ground-truth
source that `DecisionRecord.ground_truth` was — the mechanism changes, but the trust
engine's downstream math (Wilson bounds, drift detection) does not need to know or
care which source fed it, as long as it's getting real 0/1 correctness signal.

## Consequences

### Enforcement (added 2026-09-09)

The mechanism is `app/services/audit_sampling.py`, called from decision ingest
inside the same transaction, so a decision and its sample are written together
or not at all. Three details worth recording:

- **Selection is deterministic.** A decision is chosen by hashing its id, not by
  drawing from `random.random()`. Random selection would have cost the project
  its reproducibility claim: two runs of one seeded simulation would disagree
  about which decisions a human was asked to check. The hash is uniform over
  [0, 1), so the *rate* stays honest even though the *choice* is fixed.
- **Escalations are never sampled.** Sampling checks what an agent did on its
  own authority; an escalation was already handed to a human, and reviewing the
  fact that a human was asked would inflate review burden without adding
  evidence.
- **A review does not overwrite the decision's recorded ground truth.** The
  Decision section above describes reviewed samples eventually *becoming* the
  ground-truth source, and the last bullet of this section flags the contract
  gap that depends on — `TrustEvaluation` has no field distinguishing accuracy
  built from full ground truth from accuracy built from a sampled slice — as
  unresolved. Substituting one for the other in code would corrupt the
  simulator's deterministic ground truth and break the demo arc's
  reproducibility, to implement a contract change nobody has agreed. A review
  is persisted as its own evidence, and `SAMPLE_REVIEW_DISAGREEMENT` is emitted
  on a `DISAGREED` verdict.

Verified live against Postgres: agent-02 at rung 0 sampled 200 of 200 decisions
(rate 1.00), agent-01 at rung 2 sampled 41 of 200 against an expected 0.25.
Review burden therefore falls as rungs are earned, which is the operational
half of earned autonomy this ADR exists to deliver.


- Review burden falls monotonically as an agent proves itself. This is the actual
  "ROI" the project promises: not just a bigger ceiling, but less oversight required
  to sustain it. Without a scaled rate, "earned autonomy" would be a rupee number with
  no corresponding operational relief.
- At the top rung, only 5% of decisions are ever independently checked — meaning the
  accuracy estimate feeding the trust score at the highest trust level is computed
  over a smaller `n` than the agent's raw decision volume. This is exactly why the
  Wilson lower bound (ADR-0002) matters more, not less, at high trust: the confidence
  interval correctly widens as the *reviewed* sample shrinks, even while the agent's
  raw decision count keeps growing. The design does not quietly trust a thinner slice
  of evidence as much as a thick one — the interval is what keeps that honest.
- Introduces a real, unresolved gap: `TrustEvaluation` currently has no field
  distinguishing an accuracy estimate built from full `ground_truth` (simulator) from
  one built from a 5%-reviewed sample (production) — flagged separately as a
  proposed-but-deferred contract change in the `shared/v1.1` PR description, not made
  here since it would alter an existing type.
- Whether a newly-promoted agent's *already-taken* samples get re-weighted, or only
  future decisions sample at the new rung's rate, is an open implementation question
  this ADR does not resolve — noted here so it isn't silently decided by whichever
  lane implements sampling first.

## Alternatives considered

- **Review 100% of decisions regardless of rung.** Rejected: this is the state the
  system exists to move an agent away from, not a stable end state — if every decision
  still needs a human look, the agent has earned nothing operationally, only a bigger
  number on a ceiling nobody actually relies on. It also removes the entire narrative
  the demo is built around (ADR-0004's asymmetric approval story only matters if
  oversight burden actually decreases with trust).
- **No post-hoc review at all, once the trust score clears a threshold.** Rejected:
  removes the only ground-truth mechanism the system has left once `ground_truth`
  disappears — the trust score would extrapolate forward from stale, simulator-era
  evidence indefinitely, with no way to catch an agent genuinely degrading in the real
  world. That is precisely the failure mode drift detection (ADR-0006) exists to
  catch, just with its ground-truth feed silently cut off.
- **Delayed downstream signals instead of direct review** — e.g. wait for a payment
  reversal, a vendor dispute, or a reconciliation mismatch to surface a bad decision,
  rather than proactively sampling and reviewing. Rejected as the *sole* mechanism:
  those signals are real but slow and incomplete — an overpayment may not surface for
  weeks, if ever, since the vendor who received it has no incentive to report it. A
  clawback mechanism whose entire value (ADR-0004) is reacting fast to degrading
  performance cannot be fed by a signal that slow. Downstream signals remain a
  plausible *supplementary* input for a future ADR; they cannot replace proactive
  sampling as the primary ground-truth source.
