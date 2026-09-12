/**
 * src/lib/api-client.ts
 * ----------------------
 * Typed fetch-based API client — aligned to backend/openapi.json.
 *
 * DESIGN:
 *  - JWT token read from localStorage (prototype-acceptable tradeoff, documented)
 *  - All requests go to NEXT_PUBLIC_API_BASE_URL (env var)
 *  - MSW intercepts all fetch calls in dev when NEXT_PUBLIC_MSW_ENABLED=true
 *
 * Every endpoint, path and return type was verified against the live backend.
 * See: http://localhost:8000/docs for the interactive schema browser.
 */

import type {
  AgentOut,
  TrustEvaluation,
  PolicyVersionOut,
  Recommendation,
  AuditLogEntry,
  AuditSample,
  DecisionRecordOut,
  DecisionCreate,
  DecisionRuling,
  PaginatedResponse,
  AuditLogResponse,
  SimulationRunCreate,
  SimulationRunOut,
  AssistantChatRequest,
  AssistantChatResponse,
} from "@/types/api";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const API_V1 = `${API_BASE}/api/v1`;

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

function getAuthHeaders(): HeadersInit {
  const token =
    typeof window !== "undefined"
      ? localStorage.getItem("aag_jwt_token")
      : null;
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

// ---------------------------------------------------------------------------
// Base fetch wrappers
// ---------------------------------------------------------------------------

async function apiFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const res = await fetch(`${API_V1}${path}`, {
    ...options,
    headers: {
      ...getAuthHeaders(),
      ...options.headers,
    },
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    const error = new Error(`API ${res.status} on ${path}: ${text}`);
    (error as any).status = res.status;
    throw error;
  }

  return res.json() as Promise<T>;
}

function get<T>(path: string): Promise<T> {
  return apiFetch<T>(path);
}

function post<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Agents — GET /agents, GET /agents/{id}
// ---------------------------------------------------------------------------

export const agentsApi = {
  /** GET /agents → Page<AgentOut> */
  list: (page = 1, pageSize = 50): Promise<PaginatedResponse<AgentOut>> =>
    get(`/agents?page=${page}&page_size=${pageSize}`),

  /** GET /agents/{id} → AgentOut */
  get: (agentId: string): Promise<AgentOut> =>
    get(`/agents/${agentId}`),

  /**
   * GET /agents/{id}/trust → TrustEvaluation
   * NOTE: this endpoint computes and persists a fresh evaluation on every call.
   * Do not poll it — use getTrustHistory for the chart.
   */
  getTrust: (agentId: string): Promise<TrustEvaluation> =>
    get(`/agents/${agentId}/trust`),

  /** GET /agents/{id}/trust/history → Page<TrustEvaluation> (newest first) */
  getTrustHistory: (
    agentId: string,
    page = 1,
    pageSize = 100
  ): Promise<PaginatedResponse<TrustEvaluation>> =>
    get(`/agents/${agentId}/trust/history?page=${page}&page_size=${pageSize}`),

  /** GET /agents/{id}/policy-versions → Page<PolicyVersionOut> (newest first) */
  getPolicyVersions: (
    agentId: string,
    page = 1,
    pageSize = 100
  ): Promise<PaginatedResponse<PolicyVersionOut>> =>
    get(`/agents/${agentId}/policy-versions?page=${page}&page_size=${pageSize}`),

  /**
   * POST /agents/{id}/recommendations → RecommendationOut
   * Generates a fresh recommendation from the agent's real, current decision
   * history. A CLAWBACK comes back already `status: APPROVED` — applied in
   * the same call, no separate approve step (ADR-0004).
   */
  generateRecommendation: (agentId: string): Promise<Recommendation> =>
    post(`/agents/${agentId}/recommendations`, undefined),
};

// ---------------------------------------------------------------------------
// Decisions — GET /decisions, GET /decisions/{id}
// No /agents/{id}/decisions endpoint exists. Filter client-side.
// ---------------------------------------------------------------------------

export const decisionsApi = {
  /** GET /decisions → Page<DecisionRecordOut> */
  list: (page = 1, pageSize = 50): Promise<PaginatedResponse<DecisionRecordOut>> =>
    get(`/decisions?page=${page}&page_size=${pageSize}`),

  /** GET /decisions/{id} → DecisionRecordOut */
  get: (decisionId: string): Promise<DecisionRecordOut> =>
    get(`/decisions/${decisionId}`),

  /** POST /decisions → DecisionRecordOut. ADMIN only (defaults to ADMIN when no role header is sent). */
  create: (body: DecisionCreate): Promise<DecisionRecordOut> =>
    post("/decisions", body),

  /**
   * POST /decisions/{id}/ruling → DecisionRecordOut
   * REVIEWER or ADMIN only. Only an ESCALATE decision can be ruled on;
   * rules once — a second call is a 409.
   */
  rule: (decisionId: string, body: DecisionRuling): Promise<DecisionRecordOut> =>
    post(`/decisions/${decisionId}/ruling`, body),
};

// ---------------------------------------------------------------------------
// Recommendations (governance opinions + human authorization)
// Approve and reject are separate endpoints, not one "resolve".
// ---------------------------------------------------------------------------

export const recommendationsApi = {
  /** GET /recommendations → Page<Recommendation> */
  list: (status?: string): Promise<PaginatedResponse<Recommendation>> =>
    get(`/recommendations${status ? `?status=${status}` : ""}`),

  /** GET /recommendations/{id} → Recommendation */
  get: (recId: string): Promise<Recommendation> =>
    get(`/recommendations/${recId}`),

  /**
   * POST /recommendations/{id}/approve
   * `reason` is mandatory. Returns updated Recommendation.
   * 403 = not admin, 409 = already resolved.
   */
  approve: (recId: string, reason: string): Promise<Recommendation> =>
    post(`/recommendations/${recId}/approve`, { reason }),

  /**
   * POST /recommendations/{id}/reject
   * `reason` is mandatory. Returns updated Recommendation.
   * 403 = not admin, 409 = already resolved.
   */
  reject: (recId: string, reason: string): Promise<Recommendation> =>
    post(`/recommendations/${recId}/reject`, { reason }),
};

// ---------------------------------------------------------------------------
// Audit log (hash-chained immutable entries)
// Response includes chain_valid and chain_verified_scope.
// ---------------------------------------------------------------------------

export const auditLogApi = {
  /** GET /audit-log → AuditLogResponse (Page<AuditLogEntry> + chain_valid) */
  list: (params?: {
    page?: number;
    page_size?: number;
  }): Promise<AuditLogResponse> => {
    const qs = new URLSearchParams(
      Object.fromEntries(
        Object.entries(params ?? {})
          .filter(([, v]) => v !== undefined)
          .map(([k, v]) => [k, String(v)])
      )
    ).toString();
    return get(`/audit-log${qs ? `?${qs}` : ""}`);
  },
};

// ---------------------------------------------------------------------------
// Audit samples
// ---------------------------------------------------------------------------

export const auditSamplesApi = {
  /** GET /audit-samples → Page<AuditSample> */
  list: (agentId?: string): Promise<PaginatedResponse<AuditSample>> =>
    get(`/audit-samples${agentId ? `?agent_id=${agentId}` : ""}`),
};

// ---------------------------------------------------------------------------
// Simulation
// No GET /simulation/runs (list-all) endpoint exists.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Assistant — read-only chat, general or agent-scoped
// No tools, no mutations — see docs on POST /api/v1/assistant/chat.
// ---------------------------------------------------------------------------

export const assistantApi = {
  /** POST /assistant/chat → AssistantChatResponse */
  chat: (body: AssistantChatRequest): Promise<AssistantChatResponse> =>
    post("/assistant/chat", body),
};

export const simulationApi = {
  /**
   * POST /simulation/runs → SimulationRunOut
   * Body: { agent_id, invoice_count, phase, reason, seed? }
   */
  start: (config: SimulationRunCreate): Promise<SimulationRunOut> =>
    post("/simulation/runs", config),

  /** GET /simulation/runs/{run_id} → SimulationRunOut */
  getRun: (runId: string): Promise<SimulationRunOut> =>
    get(`/simulation/runs/${runId}`),
};
