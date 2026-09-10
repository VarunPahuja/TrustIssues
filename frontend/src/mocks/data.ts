/**
 * src/mocks/data.ts
 * ------------------
 * Seed data for MSW mocks — aligned to backend/openapi.json types.
 *
 * MSW is off by default — the app hits the real API.
 * This data exists for development fallback only.
 */

import type {
  AgentOut,
  PolicyVersionOut,
  DecisionRecordOut,
  Recommendation,
  TrustEvaluation,
  AuditSample,
  AuditLogEntry,
} from "@/types/api";

// ---------------------------------------------------------------------------
// Agents (AgentOut shape — id, name, current_limit, current_rung, state, context)
// ---------------------------------------------------------------------------

export const MOCK_AGENTS: AgentOut[] = [
  {
    id: "agent-01",
    name: "Invoice Approver 01",
    current_rung: 2,
    current_limit: 2500,
    state: "active",
    context: {
      current_limit: 2500,
      decisions_since_clawback: null,
      decisions_since_last_change: 12,
      state: "active",
    },
  },
  {
    id: "agent-02",
    name: "Invoice Approver 02",
    current_rung: 0,
    current_limit: 500,
    state: "probation",
    context: {
      current_limit: 500,
      decisions_since_clawback: null,
      decisions_since_last_change: 5,
      state: "probation",
    },
  },
  {
    id: "agent-03",
    name: "Invoice Approver 03",
    current_rung: 1,
    current_limit: 1000,
    state: "restricted",
    context: {
      current_limit: 1000,
      decisions_since_clawback: 8,
      decisions_since_last_change: 8,
      state: "restricted",
    },
  },
];

// ---------------------------------------------------------------------------
// Policy versions (replaces AutonomyEvent — the real endpoint data)
// ---------------------------------------------------------------------------

export const MOCK_POLICY_VERSIONS: PolicyVersionOut[] = [
  {
    id: "pv-agent01-001",
    agent_id: "agent-01",
    limit: 500,
    rung: 0,
    effective_from: "2026-08-01T09:00:00Z",
    created_by: "system",
    reason: "Agent registered at floor",
    previous_version_id: null,
  },
  {
    id: "pv-agent01-002",
    agent_id: "agent-01",
    limit: 1000,
    rung: 1,
    effective_from: "2026-08-03T18:00:00Z",
    created_by: "user-admin-01",
    reason: "Evidence cleared all six gates",
    previous_version_id: "pv-agent01-001",
  },
  {
    id: "pv-agent01-003",
    agent_id: "agent-01",
    limit: 500,
    rung: 0,
    effective_from: "2026-08-07T18:00:00Z",
    created_by: "system",
    reason: "Clawback after confirmed drift",
    previous_version_id: "pv-agent01-002",
  },
  {
    id: "pv-agent01-004",
    agent_id: "agent-01",
    limit: 1000,
    rung: 1,
    effective_from: "2026-08-10T18:00:00Z",
    created_by: "user-admin-01",
    reason: "Recovery confirmed, evidence sufficient",
    previous_version_id: "pv-agent01-003",
  },
  {
    id: "pv-agent01-005",
    agent_id: "agent-01",
    limit: 2500,
    rung: 2,
    effective_from: "2026-08-12T18:00:00Z",
    created_by: "user-admin-01",
    reason: "Sustained performance above threshold",
    previous_version_id: "pv-agent01-004",
  },
];

// ---------------------------------------------------------------------------
// Trust evaluation (full snapshot for agent-01)
// ---------------------------------------------------------------------------

export const MOCK_TRUST_EVALUATION: TrustEvaluation = {
  id: "trust-eval-agent-01-060",
  agent_id: "agent-01",
  schema_version: "1.1",
  total_decisions: 400,
  acted_decisions: 380,
  escalated_decisions: 20,
  ruled_escalations: 15,
  accuracy: { successes: 384, trials: 400, point: 0.96, wilson_lower: 0.936, wilson_upper: 0.974 },
  human_agreement: { successes: 13, trials: 15, point: 0.867, wilson_lower: 0.62, wilson_upper: 0.97 },
  utilization: { successes: 380, trials: 400, point: 0.95, wilson_lower: 0.926, wilson_upper: 0.967 },
  critical_errors: 2,
  noncritical_errors: 14,
  critical_error_rate: 0.005,
  critical_errors_in_recent_window: 0,
  trust_score: 78.4,
  components: [
    { name: "accuracy", value: 0.96, nominal_weight: 0.40, effective_weight: 0.40, available: true },
    { name: "human_agreement", value: 0.867, nominal_weight: 0.20, effective_weight: 0.20, available: true },
    { name: "critical_errors", value: 0.95, nominal_weight: 0.25, effective_weight: 0.25, available: true },
    { name: "utilization", value: 0.95, nominal_weight: 0.15, effective_weight: 0.15, available: true },
  ],
  weights_renormalised: false,
  drift: {
    severity: "NONE",
    detected: false,
    recent_accuracy: 0.95,
    baseline_accuracy: 0.96,
    drop_pp: 1.0,
    z_statistic: null,
    p_value: null,
    critical_errors_in_window: 0,
    recent_n: 50,
    baseline_n: 380,
    underpowered: false,
  },
  current_limit: 2500,
  recommended_limit: 5000,
  current_rung: 2,
  recommended_rung: 3,
  direction: "INCREASE",
  state: "active",
  eligible_for_increase: true,
  decisions_since_last_change: 12,
  reason_codes: ["EVIDENCE_SUFFICIENT", "NO_DRIFT_DETECTED"],
  evaluated_at: "2026-09-02T16:31:00Z",
  config_fingerprint: "sha256:abc123",
};

// ---------------------------------------------------------------------------
// Recommendations (with governance opinions and dissent)
// ---------------------------------------------------------------------------

export const MOCK_RECOMMENDATIONS: Recommendation[] = [
  {
    recommendation_id: "rec-001",
    agent_id: "agent-01",
    schema_version: "1.1",
    direction: "INCREASE",
    proposed_limit: 5000,
    proposed_rung: 3,
    rationale: "Agent has sustained accuracy above threshold for sufficient sample size with no recent drift.",
    opinions: [
      { agent_name: "performance", verdict: "CONCUR", reasoning: "Accuracy 96% (Wilson LB 93.6%) over 400 decisions exceeds the threshold. No drift detected.", concerns: [], confidence: 0.91 },
      { agent_name: "risk", verdict: "OBJECT", reasoning: "2 critical errors in the lifetime. While none are recent, the lifetime rate warrants caution.", concerns: ["Lifetime critical error count is non-zero"], confidence: 0.68 },
      { agent_name: "compliance", verdict: "CONCUR", reasoning: "All regulatory sampling requirements met.", concerns: [], confidence: 0.85 },
      { agent_name: "audit", verdict: "CONCUR", reasoning: "Recent audit samples show consistent agreement.", concerns: [], confidence: 0.79 },
    ],
    has_dissent: true,
    confidence: 0.72,
    governance_mode: "langgraph",
    status: "PENDING",
    trust_evaluation_ref: "trust-eval-agent-01-060",
    generated_at: "2026-09-02T16:35:00Z",
    clamped: false,
    clamped_from: null,
    reason_codes: [],
  },
  {
    recommendation_id: "rec-002",
    agent_id: "agent-02",
    schema_version: "1.1",
    direction: "HOLD",
    proposed_limit: 500,
    proposed_rung: 0,
    rationale: "Agent is in probation with insufficient evidence to warrant increase.",
    opinions: [
      { agent_name: "performance", verdict: "CONCUR", reasoning: "Too few decisions to evaluate.", concerns: [], confidence: 0.65 },
      { agent_name: "risk", verdict: "CONCUR", reasoning: "Hold is appropriate during probation.", concerns: [], confidence: 0.90 },
      { agent_name: "compliance", verdict: "CONCUR", reasoning: "Minimum sample size not yet reached.", concerns: [], confidence: 0.88 },
      { agent_name: "audit", verdict: "ABSTAIN", reasoning: "No samples to review.", concerns: [], confidence: 0.40 },
    ],
    has_dissent: false,
    confidence: 0.81,
    governance_mode: "langgraph",
    status: "APPROVED",
    trust_evaluation_ref: "trust-eval-agent-02-005",
    generated_at: "2026-09-01T14:00:00Z",
    clamped: false,
    clamped_from: null,
    reason_codes: [],
  },
];

// ---------------------------------------------------------------------------
// Recent decisions (DecisionRecordOut shape — no is_escalated, is_correct, is_critical_error)
// ---------------------------------------------------------------------------

export const MOCK_DECISIONS: DecisionRecordOut[] = [
  { decision_id: "d1", sequence: 396, invoice_id: "inv-001", amount: 450, agent_id: "agent-01", action: "APPROVE", ground_truth: "APPROVE", decided_at: "2026-09-02T09:00:00Z", recommended_action: null, human_ruling: null },
  { decision_id: "d2", sequence: 397, invoice_id: "inv-002", amount: 1200, agent_id: "agent-01", action: "ESCALATE", ground_truth: "APPROVE", decided_at: "2026-09-02T09:05:00Z", recommended_action: "APPROVE", human_ruling: "APPROVE" },
  { decision_id: "d3", sequence: 398, invoice_id: "inv-003", amount: 380, agent_id: "agent-01", action: "APPROVE", ground_truth: "APPROVE", decided_at: "2026-09-02T09:10:00Z", recommended_action: null, human_ruling: null },
  { decision_id: "d4", sequence: 399, invoice_id: "inv-004", amount: 950, agent_id: "agent-01", action: "APPROVE", ground_truth: "REJECT", decided_at: "2026-09-02T09:15:00Z", recommended_action: null, human_ruling: null },
  { decision_id: "d5", sequence: 400, invoice_id: "inv-005", amount: 2100, agent_id: "agent-01", action: "REJECT", ground_truth: "REJECT", decided_at: "2026-09-02T09:20:00Z", recommended_action: null, human_ruling: null },
];

// ---------------------------------------------------------------------------
// Audit samples
// ---------------------------------------------------------------------------

export const MOCK_AUDIT_SAMPLES: AuditSample[] = [
  { sample_id: "smp-001", decision_id: "d1", agent_id: "agent-01", sampled_at: "2026-09-02T09:01:00Z", reviewed_at: "2026-09-02T10:00:00Z", reviewer: "reviewer@company.com", verdict: "AGREED", reviewer_action: "APPROVE" },
  { sample_id: "smp-002", decision_id: "d3", agent_id: "agent-01", sampled_at: "2026-09-02T09:11:00Z", reviewed_at: null, reviewer: null, verdict: null, reviewer_action: null },
];

// ---------------------------------------------------------------------------
// Audit log (hash-chained, using dotted event_type values)
// ---------------------------------------------------------------------------

export const MOCK_AUDIT_LOG: AuditLogEntry[] = [
  {
    id: "al-001", ts: "2026-08-01T09:00:00Z",
    actor: "system", actor_type: "system", event_type: "policy_version.created",
    entity_type: "agent", entity_id: "agent-01",
    payload: { name: "Invoice Approver 01", initial_limit: 500, initial_rung: 0 },
    prev_hash: "0000000000000000000000000000000000000000000000000000000000000000",
    hash: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
  },
  {
    id: "al-002", ts: "2026-08-01T10:00:00Z",
    actor: "agent-01", actor_type: "agent", event_type: "decision.recorded",
    entity_type: "decision", entity_id: "d-batch-001",
    payload: { action: "APPROVE", amount: 450, invoice_id: "inv-001" },
    prev_hash: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
    hash: "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
  },
  {
    id: "al-003", ts: "2026-08-03T18:00:00Z",
    actor: "trust-engine", actor_type: "system", event_type: "policy_version.created",
    entity_type: "policy_version", entity_id: "pv-agent01-002",
    payload: { direction: "INCREASE", from_rung: 0, to_rung: 1, from_limit: 500, to_limit: 1000 },
    prev_hash: "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
    hash: "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  },
  {
    id: "al-004", ts: "2026-08-07T18:00:00Z",
    actor: "trust-engine", actor_type: "system", event_type: "policy_version.created",
    entity_type: "policy_version", entity_id: "pv-agent01-003",
    payload: { direction: "CLAWBACK", from_rung: 1, to_rung: 0, from_limit: 1000, to_limit: 500 },
    prev_hash: "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
    hash: "d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
  },
  {
    id: "al-005", ts: "2026-08-08T10:00:00Z",
    actor: "governance", actor_type: "system", event_type: "recommendation.generated",
    entity_type: "recommendation", entity_id: "rec-001",
    payload: { direction: "INCREASE", proposed_limit: 5000, proposed_rung: 3, has_dissent: true },
    prev_hash: "d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
    hash: "e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
  },
  {
    id: "al-006", ts: "2026-09-02T14:00:00Z",
    actor: "user-admin-01", actor_type: "human", event_type: "recommendation.approved",
    entity_type: "recommendation", entity_id: "rec-002",
    payload: { reason: "Evidence sufficient, no drift" },
    prev_hash: "e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
    hash: "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1",
  },
  {
    id: "al-007", ts: "2026-09-02T15:00:00Z",
    actor: "reviewer@company.com", actor_type: "human", event_type: "audit_sample.reviewed",
    entity_type: "audit_sample", entity_id: "smp-001",
    payload: { decision_id: "d1", verdict: "AGREED", reviewer_action: "APPROVE" },
    prev_hash: "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1",
    hash: "a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8",
  },
];
