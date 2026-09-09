/**
 * src/lib/demo-arc.ts
 * ---------------------
 * The ten-beat demo arc's actual logic — one function per beat, each either
 * driving the real API (mode "live") or returning a canned response from a
 * real prior run (mode "replay"). scripts/demo.ps1 is the source of truth
 * for the sequence, the API calls, and the wording; this is that same
 * sequence, callable one beat at a time from the /demo page instead of run
 * straight through from a terminal.
 *
 * BEAT 4's EVIDENCE REQUIREMENT — same reasoning as demo.ps1's beat 2:
 * a live-earned INCREASE needs >= MIN_RULED_ESCALATIONS_FOR_AGREEMENT (5,
 * trust/trust_engine/constants.py) ruled escalations, or the audit agent's
 * stub objects on the evidence gap alone and the coordinator's dissent rule
 * downgrades the INCREASE to a HOLD. The simulator's own scripted agent
 * never escalates (only ever produces APPROVE/REJECT), so beat 2 also
 * hand-crafts and rules 6 ESCALATE decisions directly against POST
 * /decisions — every one of them, not just enough to clear the count gate,
 * since the audit agent objects on ANY unruled escalation.
 *
 * READ-YOUR-WRITES GUARD — same as demo.ps1's Invoke-ApiWithRetry: a GET
 * that reads back something a previous write just committed goes through
 * `withRetry`, which treats a 404 as "not visible yet" and retries briefly
 * before giving up for real.
 */

import {
  agentsApi,
  decisionsApi,
  recommendationsApi,
  auditLogApi,
  simulationApi,
} from "./api-client";
import type {
  AgentOut,
  AgentOpinion,
  Direction,
  DriftSeverity,
  RecommendationStatus,
  Action,
} from "@/types/api";
import {
  replayBeat1,
  replayBeat2,
  replayBeat3,
  replayBeat4,
  replayBeat5,
  replayBeat6,
  replayBeat7,
  replayBeat8,
  replayBeat9,
  replayBeat10,
} from "./demo-replay-data";

export type DemoMode = "live" | "replay";

// The seed/count for beat 2's evidence-building simulation, chosen offline
// (same reasoning as demo.ps1) to produce zero critical errors anywhere in
// the run — one critical error in the last 20 acted decisions would trip
// drift.severity=CRITICAL early and turn beat 4 into a clawback instead of
// an increase.
const BEAT2_PHASE = "good" as const;
const BEAT2_INVOICE_COUNT = 120;
const BEAT2_SEED = 1;
const BEAT2_ESCALATION_COUNT = 6;
const AGENT_ID = "agent-01";

// A short, deliberate pause so replay mode still feels like something is
// happening, rather than results simply appearing — matches the pacing a
// live call would have, without pretending to be one (the REPLAY badge on
// screen already says so).
function replayDelay(ms = 500): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * A GET that reads back something a previous write just committed. Retries
 * a 404 briefly (docs/audits/2026-09-08-freeze-audit.md's read-your-writes
 * gap, ~1 in 200-800) before treating it as a real failure.
 */
async function withRetry<T>(
  fn: () => Promise<T>,
  { maxAttempts = 8, delayMs = 200 }: { maxAttempts?: number; delayMs?: number } = {}
): Promise<T> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      const status = (err as { status?: number })?.status;
      if (status === 404 && attempt < maxAttempts) {
        await sleep(delayMs);
        continue;
      }
      throw err;
    }
  }
  throw lastError;
}

/** A call that failed, in enough detail to show on stage without guessing. */
export interface BeatError {
  call: string;
  status: number | null;
  message: string;
}

export class BeatFailure extends Error {
  detail: BeatError;
  constructor(detail: BeatError) {
    super(`${detail.call} failed: ${detail.message}`);
    this.detail = detail;
  }
}

function toBeatError(call: string, err: unknown): BeatError {
  const e = err as { status?: number; message?: string };
  return {
    call,
    status: typeof e?.status === "number" ? e.status : null,
    message: e?.message ?? String(err),
  };
}

async function callOrFail<T>(call: string, fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (err) {
    throw new BeatFailure(toBeatError(call, err));
  }
}

// ---------------------------------------------------------------------------
// Per-beat result shapes
// ---------------------------------------------------------------------------

export interface Beat1Result {
  kind: "beat1";
  agent: AgentOut;
}

export interface Beat2Result {
  kind: "beat2";
  runId: string;
  decisionsSubmitted: number;
  accuracy: number;
  wilsonLowerBound: number;
  escalationsRuled: number;
}

export interface Beat3Result {
  kind: "beat3";
  point: number;
  wilsonLower: number;
  trials: number;
  trustScore: number;
  gapPoints: number;
}

export interface Beat4Result {
  kind: "beat4";
  recommendationId: string;
  direction: Direction;
  status: RecommendationStatus;
  hasDissent: boolean;
  clamped: boolean;
  clampedFrom: number | null;
  proposedLimit: number;
  opinions: AgentOpinion[];
}

export interface Beat5Result {
  kind: "beat5";
  status: RecommendationStatus;
}

export interface Beat6Result {
  kind: "beat6";
  limit: number;
  rung: number;
  latestVersionId: string;
  createdBy: string;
  previousVersionId: string | null;
  previousActualId: string;
  chainedCorrectly: boolean;
}

export interface Beat7Result {
  kind: "beat7";
  decisionId: string;
  action: Action;
  groundTruth: Action;
}

export interface Beat8Result {
  kind: "beat8";
  severity: DriftSeverity;
  criticalErrorsInWindow: number;
  recentAccuracy: number | null;
  baselineAccuracy: number | null;
}

export interface Beat9Result {
  kind: "beat9";
  limitBefore: number;
  rungBefore: number;
  direction: Direction;
  status: RecommendationStatus;
  limitAfter: number;
  rungAfter: number;
}

export interface Beat10Result {
  kind: "beat10";
  chainValid: boolean;
  chainVerifiedScope: string;
  total: number;
}

export type BeatResult =
  | Beat1Result
  | Beat2Result
  | Beat3Result
  | Beat4Result
  | Beat5Result
  | Beat6Result
  | Beat7Result
  | Beat8Result
  | Beat9Result
  | Beat10Result;

// ---------------------------------------------------------------------------
// Beat 1 — agent-01's starting position
// ---------------------------------------------------------------------------

export async function runBeat1(mode: DemoMode): Promise<Beat1Result> {
  if (mode === "replay") {
    await replayDelay();
    return replayBeat1();
  }
  const agent = await callOrFail(`GET /agents/${AGENT_ID}`, () => agentsApi.get(AGENT_ID));
  return { kind: "beat1", agent };
}

// ---------------------------------------------------------------------------
// Beat 2 — build evidence: a simulation run, then 6 ruled escalations
// ---------------------------------------------------------------------------

export async function runBeat2(mode: DemoMode): Promise<Beat2Result> {
  if (mode === "replay") {
    await replayDelay(1200);
    return replayBeat2();
  }

  const run = await callOrFail("POST /simulation/runs", () =>
    simulationApi.start({
      agent_id: AGENT_ID,
      phase: BEAT2_PHASE,
      invoice_count: BEAT2_INVOICE_COUNT,
      seed: BEAT2_SEED,
      reason: "demo console: build evidence for a live-earned increase",
    })
  );

  let completed = null;
  for (let i = 0; i < 120; i++) {
    const status = await callOrFail(`GET /simulation/runs/${run.run_id}`, () =>
      withRetry(() => simulationApi.getRun(run.run_id))
    );
    if (status.status === "completed") {
      completed = status;
      break;
    }
    if (status.status === "failed") {
      throw new BeatFailure({
        call: `GET /simulation/runs/${run.run_id}`,
        status: null,
        message: status.error_message ?? "Simulation run failed.",
      });
    }
    await sleep(250);
  }
  if (!completed) {
    throw new BeatFailure({
      call: `GET /simulation/runs/${run.run_id}`,
      status: null,
      message: "Simulation run did not complete within 30s.",
    });
  }

  // The simulator's scripted agent never escalates (only ever produces
  // APPROVE/REJECT) — every one of these has to be hand-crafted and ruled,
  // or beat 4 comes back a dissenting HOLD instead of an INCREASE.
  for (let i = 1; i <= BEAT2_ESCALATION_COUNT; i++) {
    const decision = await callOrFail(`POST /decisions (escalation ${i})`, () =>
      decisionsApi.create({
        invoice_id: `demo-console-escalation-${i}`,
        amount: 750,
        action: "ESCALATE",
        ground_truth: "APPROVE",
        agent_id: AGENT_ID,
        recommended_action: "APPROVE",
        reason: "ambiguous vendor, escalating for human review",
      })
    );
    await callOrFail(`POST /decisions/${decision.decision_id}/ruling`, () =>
      decisionsApi.rule(decision.decision_id, {
        ruling: "APPROVE",
        reason: "reviewed the invoice; agent's recommendation was correct",
      })
    );
  }

  return {
    kind: "beat2",
    runId: run.run_id,
    decisionsSubmitted: completed.decisions_submitted,
    accuracy: completed.accuracy ?? 0,
    wilsonLowerBound: completed.wilson_lower_bound ?? 0,
    escalationsRuled: BEAT2_ESCALATION_COUNT,
  };
}

// ---------------------------------------------------------------------------
// Beat 3 — the trust evaluation: point estimate vs. Wilson lower bound
// ---------------------------------------------------------------------------

export async function runBeat3(mode: DemoMode): Promise<Beat3Result> {
  if (mode === "replay") {
    await replayDelay();
    return replayBeat3();
  }
  const trust = await callOrFail(`GET /agents/${AGENT_ID}/trust`, () =>
    withRetry(() => agentsApi.getTrust(AGENT_ID))
  );
  const acc = trust.accuracy;
  const point = acc?.point ?? 0;
  const wilsonLower = acc?.wilson_lower ?? 0;
  return {
    kind: "beat3",
    point,
    wilsonLower,
    trials: acc?.trials ?? 0,
    trustScore: trust.trust_score,
    gapPoints: (point - wilsonLower) * 100,
  };
}

// ---------------------------------------------------------------------------
// Beat 4 — generate a recommendation: the demo's highest-stakes moment
// ---------------------------------------------------------------------------

export async function runBeat4(mode: DemoMode): Promise<Beat4Result> {
  if (mode === "replay") {
    await replayDelay();
    return replayBeat4();
  }
  const rec = await callOrFail(`POST /agents/${AGENT_ID}/recommendations`, () =>
    agentsApi.generateRecommendation(AGENT_ID)
  );
  return {
    kind: "beat4",
    recommendationId: rec.recommendation_id,
    direction: rec.direction,
    status: rec.status,
    hasDissent: rec.has_dissent,
    clamped: rec.clamped,
    clampedFrom: rec.clamped_from,
    proposedLimit: rec.proposed_limit,
    opinions: rec.opinions,
  };
}

// ---------------------------------------------------------------------------
// Beat 5 — a human approves
// ---------------------------------------------------------------------------

export async function runBeat5(mode: DemoMode, recommendationId: string): Promise<Beat5Result> {
  if (mode === "replay") {
    await replayDelay();
    return replayBeat5();
  }
  const approved = await callOrFail(`POST /recommendations/${recommendationId}/approve`, () =>
    recommendationsApi.approve(
      recommendationId,
      "evidence reviewed, all four agents concur, approving the increase"
    )
  );
  return { kind: "beat5", status: approved.status };
}

// ---------------------------------------------------------------------------
// Beat 6 — the limit moved, and the policy version chains to the prior one
// ---------------------------------------------------------------------------

export async function runBeat6(mode: DemoMode): Promise<Beat6Result> {
  if (mode === "replay") {
    await replayDelay();
    return replayBeat6();
  }
  const agent = await callOrFail(`GET /agents/${AGENT_ID}`, () =>
    withRetry(() => agentsApi.get(AGENT_ID))
  );
  const versions = await callOrFail(`GET /agents/${AGENT_ID}/policy-versions`, () =>
    withRetry(() => agentsApi.getPolicyVersions(AGENT_ID, 1, 2))
  );
  const [latest, previous] = versions.items;
  return {
    kind: "beat6",
    limit: agent.current_limit,
    rung: agent.current_rung,
    latestVersionId: latest?.id ?? "",
    createdBy: latest?.created_by ?? "",
    previousVersionId: latest?.previous_version_id ?? null,
    previousActualId: previous?.id ?? "",
    chainedCorrectly: !!latest && !!previous && latest.previous_version_id === previous.id,
  };
}

// ---------------------------------------------------------------------------
// Beat 7 — inject a critical error
// ---------------------------------------------------------------------------

export async function runBeat7(mode: DemoMode): Promise<Beat7Result> {
  if (mode === "replay") {
    await replayDelay();
    return replayBeat7();
  }
  const decision = await callOrFail("POST /decisions (critical error)", () =>
    decisionsApi.create({
      invoice_id: "demo-console-critical-error-1",
      amount: 900,
      action: "APPROVE",
      ground_truth: "REJECT",
      agent_id: AGENT_ID,
      reason: "demo console: inject a critical error to trigger drift detection",
    })
  );
  return {
    kind: "beat7",
    decisionId: decision.decision_id,
    action: decision.action,
    groundTruth: decision.ground_truth,
  };
}

// ---------------------------------------------------------------------------
// Beat 8 — drift detection catches it
// ---------------------------------------------------------------------------

export async function runBeat8(mode: DemoMode): Promise<Beat8Result> {
  if (mode === "replay") {
    await replayDelay();
    return replayBeat8();
  }
  const trust = await callOrFail(`GET /agents/${AGENT_ID}/trust`, () =>
    withRetry(() => agentsApi.getTrust(AGENT_ID))
  );
  return {
    kind: "beat8",
    severity: trust.drift.severity,
    criticalErrorsInWindow: trust.drift.critical_errors_in_window,
    recentAccuracy: trust.drift.recent_accuracy,
    baselineAccuracy: trust.drift.baseline_accuracy,
  };
}

// ---------------------------------------------------------------------------
// Beat 9 — the clawback: automatic, no approval call
// ---------------------------------------------------------------------------

export async function runBeat9(mode: DemoMode): Promise<Beat9Result> {
  if (mode === "replay") {
    await replayDelay();
    return replayBeat9();
  }
  const before = await callOrFail(`GET /agents/${AGENT_ID} (before)`, () =>
    withRetry(() => agentsApi.get(AGENT_ID))
  );
  const rec = await callOrFail(`POST /agents/${AGENT_ID}/recommendations`, () =>
    agentsApi.generateRecommendation(AGENT_ID)
  );
  const after = await callOrFail(`GET /agents/${AGENT_ID} (after)`, () =>
    withRetry(() => agentsApi.get(AGENT_ID))
  );
  return {
    kind: "beat9",
    limitBefore: before.current_limit,
    rungBefore: before.current_rung,
    direction: rec.direction,
    status: rec.status,
    limitAfter: after.current_limit,
    rungAfter: after.current_rung,
  };
}

// ---------------------------------------------------------------------------
// Beat 10 — verify the audit chain
// ---------------------------------------------------------------------------

export async function runBeat10(mode: DemoMode): Promise<Beat10Result> {
  if (mode === "replay") {
    await replayDelay();
    return replayBeat10();
  }
  const log = await callOrFail("GET /audit-log", () =>
    withRetry(() => auditLogApi.list({ page: 1, page_size: 100 }))
  );
  return {
    kind: "beat10",
    chainValid: log.chain_valid,
    chainVerifiedScope: log.chain_verified_scope,
    total: log.total,
  };
}
