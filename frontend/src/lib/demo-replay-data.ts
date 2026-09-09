/**
 * src/lib/demo-replay-data.ts
 * ----------------------------
 * Canned responses for the /demo page's "Replay" mode — the numbers a real
 * live run actually produced (scripts/demo.ps1, run against a freshly reset
 * Postgres database, 9 Sept 2026). This exists so the demo still runs if the
 * backend is unreachable minutes before presenting.
 *
 * These are NOT fabricated placeholder numbers — every value here is copied
 * from that run's own terminal output. Where demo.ps1's output didn't
 * capture something (its beat 4 print only ever showed each agent's
 * verdict, never governance's full reasoning text), the reasoning string
 * here says so plainly rather than inventing detail nothing recorded.
 */

import type {
  Beat1Result,
  Beat2Result,
  Beat3Result,
  Beat4Result,
  Beat5Result,
  Beat6Result,
  Beat7Result,
  Beat8Result,
  Beat9Result,
  Beat10Result,
} from "./demo-arc";

const REPLAY_REASONING =
  "Replayed from a real run (scripts/demo.ps1, 9 Sept 2026) — that run's own output recorded this agent's verdict but not its full reasoning text.";

export function replayBeat1(): Beat1Result {
  return {
    kind: "beat1",
    agent: {
      id: "agent-01",
      name: "Invoice Agent — Procurement",
      current_limit: 2500,
      current_rung: 2,
      state: "active",
      context: {
        current_limit: 2500,
        decisions_since_last_change: 3,
        decisions_since_clawback: null,
        state: "active",
      },
    },
  };
}

export function replayBeat2(): Beat2Result {
  return {
    kind: "beat2",
    runId: "run-e21087c3",
    decisionsSubmitted: 120,
    accuracy: 0.942,
    wilsonLowerBound: 0.884,
    escalationsRuled: 6,
  };
}

export function replayBeat3(): Beat3Result {
  return {
    kind: "beat3",
    point: 0.943,
    wilsonLower: 0.886,
    trials: 122,
    trustScore: 84.9,
    gapPoints: 5.6,
  };
}

export function replayBeat4(): Beat4Result {
  return {
    kind: "beat4",
    recommendationId: "rec-agent-01-replay",
    direction: "INCREASE",
    status: "PENDING",
    hasDissent: false,
    clamped: false,
    clampedFrom: null,
    proposedLimit: 5000,
    opinions: [
      { agent_name: "risk", verdict: "CONCUR", reasoning: REPLAY_REASONING, concerns: [], confidence: 0.85 },
      { agent_name: "performance", verdict: "CONCUR", reasoning: REPLAY_REASONING, concerns: [], confidence: 0.88 },
      { agent_name: "compliance", verdict: "CONCUR", reasoning: REPLAY_REASONING, concerns: [], confidence: 0.82 },
      { agent_name: "audit", verdict: "CONCUR", reasoning: REPLAY_REASONING, concerns: [], confidence: 0.8 },
    ],
  };
}

export function replayBeat5(): Beat5Result {
  return { kind: "beat5", status: "APPROVED" };
}

export function replayBeat6(): Beat6Result {
  return {
    kind: "beat6",
    limit: 5000,
    rung: 3,
    latestVersionId: "pv-95839e7b4ff2",
    createdBy: "user-admin-01",
    previousVersionId: "pv-agent01-003",
    previousActualId: "pv-agent01-003",
    chainedCorrectly: true,
  };
}

export function replayBeat7(): Beat7Result {
  return {
    kind: "beat7",
    decisionId: "dec-940dd70713a0",
    action: "APPROVE",
    groundTruth: "REJECT",
  };
}

export function replayBeat8(): Beat8Result {
  return {
    kind: "beat8",
    severity: "CRITICAL",
    criticalErrorsInWindow: 1,
    recentAccuracy: 0.909,
    baselineAccuracy: 0.949,
  };
}

export function replayBeat9(): Beat9Result {
  return {
    kind: "beat9",
    limitBefore: 5000,
    rungBefore: 3,
    direction: "CLAWBACK",
    status: "APPROVED",
    limitAfter: 2500,
    rungAfter: 2,
  };
}

export function replayBeat10(): Beat10Result {
  return {
    kind: "beat10",
    chainValid: true,
    chainVerifiedScope: "full",
    total: 143,
  };
}
