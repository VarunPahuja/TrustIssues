"use client";
/**
 * Page 2: /agents/[id] — Agent Detail & Governance Hero Page
 *
 * Uses AgentOut (from GET /agents/{id}) for basic identity, and
 * TrustEvaluation (from GET /agents/{id}/trust) for all metrics.
 * Policy versions (from GET /agents/{id}/policy-versions) for the timeline.
 * Decisions from GET /decisions filtered client-side by agent_id.
 *
 * KEY CHANGES:
 *  - trust endpoint is /trust not /trust-evaluation
 *  - autonomy history is /policy-versions not /autonomy-history
 *  - agent shape is AgentOut (id, name, current_limit, current_rung, state, context)
 *  - trust metrics come from the trust query, not from the agent
 *  - decisions from /decisions, not /agents/{id}/decisions
 */

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { agentsApi, decisionsApi } from "@/lib/api-client";
import { AutonomyTimeline } from "@/components/charts/AutonomyTimeline";
import { HorizontalThresholdGauge } from "@/components/charts/HorizontalThresholdGauge";
import { AutonomyLadder } from "@/components/domain/AutonomyLadder";
import {
  describeReasonCodes,
  AUTONOMY_LADDER,
  samplingRateOf,
} from "@/types/api";
import type { AgentState, PolicyVersionOut } from "@/types/api";

const STATE_CLASS: Record<AgentState, string> = {
  probation:  "state-badge state-probation",
  active:     "state-badge state-active",
  restricted: "state-badge state-restricted",
  suspended:  "state-badge state-suspended",
};

function fmtLimit(val: number): string {
  if (val >= 1000) return `₹${(val / 1000).toFixed(val % 1000 === 0 ? 0 : 1)}k`;
  return `₹${val.toLocaleString("en-IN")}`;
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

/** Detect clawbacks by comparing consecutive policy versions (rung going down). */
function buildEventTimeline(versions: PolicyVersionOut[]) {
  // versions are newest-first from the API — reverse for chronological order
  const chronological = [...versions].reverse();

  return chronological.map((v, i) => {
    const prevVersion = i > 0 ? chronological[i - 1] : null;
    const isClawback = prevVersion != null && v.rung < prevVersion.rung;
    const isPromotion = prevVersion != null && v.rung > prevVersion.rung;

    let color = "bg-slate-400";
    let title = "";
    let description = "";

    if (isClawback) {
      color = "bg-red-600";
      title = `Automatic clawback → ${fmtLimit(v.limit)}`;
      description = `Autonomy reduced to rung ${v.rung}. ${v.reason}`;
    } else if (isPromotion) {
      color = "bg-[#86BC25]";
      title = `Promotion granted → ${fmtLimit(v.limit)}`;
      description = `Earned rung ${v.rung}. ${v.reason}`;
    } else if (i === 0) {
      color = "bg-blue-500";
      title = `Initial policy → ${fmtLimit(v.limit)}`;
      description = `Starting at rung ${v.rung}. ${v.reason}`;
    } else {
      title = `Policy change → ${fmtLimit(v.limit)}`;
      description = v.reason;
    }

    return { time: fmtTime(v.effective_from), color, title, description };
  });
}

export default function AgentDetailPage() {
  const { id } = useParams<{ id: string }>();

  const { data: agent, isLoading: agentLoading, isError: agentError } = useQuery({
    queryKey: ["agent", id],
    queryFn: () => agentsApi.get(id),
  });

  const { data: policyVersionsData, isError: policyVersionsError } = useQuery({
    queryKey: ["agent-policy-versions", id],
    queryFn: () => agentsApi.getPolicyVersions(id),
  });

  const { data: trustEval, isError: trustEvalError } = useQuery({
    queryKey: ["agent-trust", id],
    queryFn: () => agentsApi.getTrust(id),
    // Don't poll — each call computes and persists a new evaluation
    refetchInterval: false,
    staleTime: 60_000,
  });

  const { data: trustHistoryData, isError: trustHistoryError } = useQuery({
    queryKey: ["agent-trust-history", id],
    queryFn: () => agentsApi.getTrustHistory(id),
  });

  const { data: decisionsData, isError: decisionsError } = useQuery({
    queryKey: ["agent-decisions", id],
    queryFn: () => decisionsApi.list(),
  });

  if (agentLoading) {
    return (
      <div className="editorial-content text-xs font-bold text-slate-400 uppercase tracking-widest animate-pulse">
        LOADING GOVERNANCE AGENT PROFILE...
      </div>
    );
  }

  if (agentError || !agent) {
    return (
      <div className="editorial-content">
        <div className="editorial-panel p-6 border-l-4 border-red-400">
          <span className="text-xs font-bold text-red-700 uppercase tracking-widest block">
            {agentError ? "FAILED TO LOAD AGENT" : "GOVERNANCE RECORD NOT FOUND."}
          </span>
          <p className="text-xs text-slate-500 mt-1">Check that the backend is running and the agent ID is valid.</p>
        </div>
      </div>
    );
  }

  if (policyVersionsError || trustEvalError || trustHistoryError || decisionsError) {
    return (
      <div className="editorial-content">
        <div className="editorial-panel p-6 border-l-4 border-red-400">
          <span className="text-xs font-bold text-red-700 uppercase tracking-widest block">
            FAILED TO LOAD COMPLETE AGENT DATA
          </span>
          <p className="text-xs text-slate-500 mt-1">Check that the backend is running and try again.</p>
        </div>
      </div>
    );
  }

  const policyVersions = policyVersionsData?.items ?? [];
  const trustHistory = trustHistoryData?.items ?? [];
  const hasClawback = policyVersions.some((v, i) => {
    const chronological = [...policyVersions].reverse();
    const idx = chronological.indexOf(v);
    return idx > 0 && v.rung < chronological[idx - 1].rung;
  });
  const eventTimeline = buildEventTimeline(policyVersions);
  const samplingRate = samplingRateOf(agent.current_rung);

  // Filter decisions for this agent (client-side, since no per-agent endpoint exists)
  const agentDecisions = (decisionsData?.items ?? []).filter(d => d.agent_id === id);

  return (
    <div>
      {/* Editorial Hero Header */}
      <div className="editorial-header">
        <div className="flex flex-col md:flex-row md:items-start justify-between gap-6">
          <div>
            <span className="eyebrow-label">AGENT GOVERNANCE</span>
            <h1 className="text-3xl font-black text-slate-900 tracking-tight mt-1">
              {agent.name.split(" ")[0]}
            </h1>
            <div className="flex items-center gap-3 text-xs text-slate-500 font-mono mt-1">
              <span>{agent.id}</span>
            </div>
            <div className="flex items-center gap-2 mt-2">
              <span className={STATE_CLASS[agent.state]}>
                {agent.state.toUpperCase()}
              </span>
              <span className={`rung-tag rung-${agent.current_rung}`}>
                RUNG {agent.current_rung}
              </span>
              {trustEval && trustEval.drift.severity !== "NONE" && (
                <span className={`state-badge drift-${trustEval.drift.severity.toLowerCase()}`}>
                  DRIFT: {trustEval.drift.severity}
                </span>
              )}
            </div>
          </div>

          {/* Right Hero Autonomy Metric */}
          <div className="text-right">
            <span className="eyebrow-label block mb-1">CURRENT AUTONOMY</span>
            <div className="flex items-center justify-end gap-2">
              <span className="w-1.5 h-8 bg-[#86BC25] rounded-full inline-block" />
              <p className="text-4xl font-black text-slate-900 tracking-tight">
                ₹{agent.current_limit.toLocaleString("en-IN")}
              </p>
            </div>
            <div className="flex items-center justify-end gap-2 mt-1.5">
              {trustEval && trustEval.direction !== "HOLD" && (
                <span className={`state-badge direction-${trustEval.direction.toLowerCase()}`}>
                  {trustEval.direction}
                </span>
              )}
              {trustEval?.eligible_for_increase && (
                <span className="state-badge state-active">
                  ELIGIBLE FOR INCREASE
                </span>
              )}
              {hasClawback && (
                <div className="flex items-center gap-1.5 text-xs text-red-700 font-bold bg-red-50 border border-red-200 px-2 py-0.5 rounded-[2px]">
                  <span className="w-1.5 h-1.5 rounded-full bg-red-600 inline-block" />
                  <span>AUTONOMY CLAWED BACK</span>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="editorial-content space-y-8">
        {/* Horizontal Performance Band (Strip) — from TrustEvaluation */}
        <div className="editorial-panel grid grid-cols-2 sm:grid-cols-6 divide-y sm:divide-y-0 sm:divide-x divide-slate-200">
          <div className="metric-strip-item">
            <span className="eyebrow-label text-[9px]">TRUST SCORE</span>
            <span className="text-2xl font-black text-slate-900">
              {trustEval ? trustEval.trust_score.toFixed(1) : "—"}
            </span>
          </div>
          <div className="metric-strip-item">
            <span className="eyebrow-label text-[9px]">TOTAL DECISIONS</span>
            <span className="text-2xl font-black text-slate-900">
              {trustEval ? trustEval.total_decisions.toLocaleString() : "—"}
            </span>
          </div>
          <div className="metric-strip-item">
            <span className="eyebrow-label text-[9px]">ACCURACY</span>
            <span className="text-2xl font-black text-slate-900">
              {trustEval?.accuracy?.point != null ? `${Math.round(trustEval.accuracy.point * 100)}%` : "—"}
            </span>
          </div>
          <div className="metric-strip-item">
            <span className="eyebrow-label text-[9px]">WILSON BAND</span>
            <span className="text-2xl font-black text-blue-700">
              {trustEval?.accuracy != null
                ? `${Math.round(trustEval.accuracy.wilson_lower * 100)}–${Math.round(trustEval.accuracy.wilson_upper * 100)}%`
                : "—"}
            </span>
          </div>
          <div className="metric-strip-item">
            <span className="eyebrow-label text-[9px]">SAMPLING RATE</span>
            <span className="text-2xl font-black text-slate-900">{Math.round(samplingRate * 100)}%</span>
          </div>
          <div className="metric-strip-item">
            <span className="eyebrow-label text-[9px]">DIRECTION</span>
            <span className="text-2xl font-black text-slate-900">
              {trustEval ? trustEval.direction : "—"}
            </span>
          </div>
        </div>

        {/* Hero Visual — How Autonomy Changed (Timeline Chart) */}
        <div className="editorial-panel p-6">
          <div className="border-b border-slate-200 pb-4 mb-4">
            <span className="eyebrow-label">GOVERNANCE TRAJECTORY</span>
            <h2 className="text-xl font-black text-slate-900 tracking-tight mt-0.5">
              How autonomy changed
            </h2>
            <p className="text-xs text-slate-500 font-medium mt-1">
              The shaded band narrows as evidence accumulates — that narrowing unlocks higher rungs.
            </p>
          </div>

          <AutonomyTimeline
            policyVersions={policyVersions}
            trustHistory={trustHistory}
            height={380}
          />
        </div>

        {/* Two-Column: Why Autonomy Changed & Horizontal Threshold Visualization */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Explanation Panel: Why Autonomy Changed — data-driven from reason codes */}
          <div className="editorial-panel p-6">
            <span className="eyebrow-label block mb-1">GOVERNANCE DIAGNOSTICS</span>
            <h3 className="text-lg font-black text-slate-900 mb-4 border-b border-slate-200 pb-2">
              Why autonomy changed
            </h3>

            <div className="space-y-4 text-xs font-sans">
              {trustEval && trustEval.reason_codes.length > 0 ? (
                <>
                  <div className={`${trustEval.direction === "CLAWBACK" ? "bg-red-50/80 border-red-200" : trustEval.direction === "INCREASE" ? "bg-green-50/80 border-green-200" : "bg-slate-50 border-slate-200"} border rounded-[2px] p-4`}>
                    <div className="flex items-center justify-between mb-2">
                      <span className={`font-extrabold text-xs ${trustEval.direction === "CLAWBACK" ? "text-red-900" : trustEval.direction === "INCREASE" ? "text-green-900" : "text-slate-900"}`}>
                        {trustEval.direction}
                      </span>
                      <span className={`state-badge direction-${trustEval.direction.toLowerCase()}`}>
                        {trustEval.direction}
                      </span>
                    </div>
                    <p className="text-slate-700 font-medium leading-relaxed">
                      {describeReasonCodes(trustEval.reason_codes)}
                    </p>
                  </div>

                  <div className="grid grid-cols-2 gap-4 text-xs">
                    <div className="bg-slate-50 border border-slate-200 p-3 rounded-[2px]">
                      <span className="eyebrow-label text-[9px] block">CURRENT LIMIT</span>
                      <span className="text-base font-black text-slate-900">{fmtLimit(trustEval.current_limit)}</span>
                    </div>
                    <div className="bg-slate-50 border border-slate-200 p-3 rounded-[2px]">
                      <span className="eyebrow-label text-[9px] block">RECOMMENDED</span>
                      <span className="text-base font-black text-slate-900">{fmtLimit(trustEval.recommended_limit)}</span>
                    </div>
                    <div className="bg-slate-50 border border-slate-200 p-3 rounded-[2px]">
                      <span className="eyebrow-label text-[9px] block">ELIGIBLE</span>
                      <span className={`text-base font-black ${trustEval.eligible_for_increase ? "text-[#5f8914]" : "text-slate-500"}`}>
                        {trustEval.eligible_for_increase ? "YES" : "NO"}
                      </span>
                    </div>
                    <div className="bg-slate-50 border border-slate-200 p-3 rounded-[2px]">
                      <span className="eyebrow-label text-[9px] block">SINCE LAST CHANGE</span>
                      <span className="text-base font-black text-slate-900">{trustEval.decisions_since_last_change}</span>
                    </div>
                  </div>

                  {/* Reason codes as individual tags */}
                  <div className="flex flex-wrap gap-1.5 pt-2">
                    {trustEval.reason_codes.map(code => (
                      <span key={code} className="px-2 py-0.5 text-[10px] font-bold font-mono bg-slate-100 text-slate-700 rounded border border-slate-200">
                        {code}
                      </span>
                    ))}
                  </div>
                </>
              ) : (
                <p className="text-slate-500">No diagnostics available.</p>
              )}
            </div>
          </div>

          {/* Horizontal Reliability Visual */}
          <div className="editorial-panel p-6 flex flex-col justify-between">
            <div>
              <span className="eyebrow-label block mb-1">STATISTICAL EVIDENCE</span>
              <h3 className="text-lg font-black text-slate-900 mb-4 border-b border-slate-200 pb-2">
                Reliability Position
              </h3>

              {trustEval?.accuracy != null && trustEval.accuracy.point != null && (
                <HorizontalThresholdGauge
                  accuracy={trustEval.accuracy.point}
                  wilsonLB={trustEval.accuracy.wilson_lower}
                />
              )}
            </div>

            {/* Trust score component breakdown */}
            {trustEval && trustEval.components.length > 0 && (
              <div className="mt-4">
                <span className="eyebrow-label block text-[9px] mb-2">SCORE COMPONENTS</span>
                <div className="space-y-1.5">
                  {trustEval.components.map(comp => (
                    <div key={comp.name} className="flex items-center justify-between text-xs bg-slate-50 border border-slate-200 px-3 py-1.5 rounded-[2px]">
                      <span className="font-semibold text-slate-700 capitalize">{comp.name.replace(/_/g, " ")}</span>
                      <div className="flex items-center gap-3">
                        <span className="text-slate-500 text-[10px]">w={comp.effective_weight.toFixed(2)}</span>
                        <span className="font-bold text-slate-900">
                          {comp.value != null ? `${Math.round(comp.value * 100)}%` : "—"}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
                {trustEval.weights_renormalised && (
                  <p className="text-[10px] text-amber-700 font-medium mt-1">
                    ⚠ Weights renormalised (some components unavailable)
                  </p>
                )}
              </div>
            )}

            {/* Drift section */}
            {trustEval && trustEval.drift.detected && (
              <div className="mt-4 bg-amber-50 border border-amber-200 p-3 rounded-[2px]">
                <span className="eyebrow-label block text-[9px] text-amber-900 mb-1">DRIFT DETECTED</span>
                <div className="flex items-center gap-4 text-xs">
                  <div>
                    <span className="text-slate-600">Recent: </span>
                    <span className="font-bold text-slate-900">
                      {trustEval.drift.recent_accuracy != null ? `${Math.round(trustEval.drift.recent_accuracy * 100)}%` : "—"}
                    </span>
                  </div>
                  <span className="text-slate-300">vs</span>
                  <div>
                    <span className="text-slate-600">Baseline: </span>
                    <span className="font-bold text-slate-900">
                      {trustEval.drift.baseline_accuracy != null ? `${Math.round(trustEval.drift.baseline_accuracy * 100)}%` : "—"}
                    </span>
                  </div>
                  <span className={`state-badge drift-${trustEval.drift.severity.toLowerCase()}`}>
                    {trustEval.drift.severity}
                  </span>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Autonomy Ladder + Sampling Rate */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="editorial-panel p-6">
            <AutonomyLadder currentRung={agent.current_rung} />
          </div>
          <div className="editorial-panel p-6 lg:col-span-2">
            <span className="eyebrow-label block mb-1">AUDIT SAMPLING</span>
            <h3 className="text-lg font-black text-slate-900 mb-4 border-b border-slate-200 pb-2">
              Review burden by rung
            </h3>
            <p className="text-xs text-slate-600 mb-3">
              As an agent climbs the ladder, its autonomous decisions are sampled at a lower rate. This shrinking review burden is the system&apos;s ROI.
            </p>
            <div className="grid grid-cols-5 gap-2">
              {AUTONOMY_LADDER.map((limit, rung) => (
                <div
                  key={rung}
                  className={`text-center p-3 rounded-[2px] border ${rung === agent.current_rung ? `rung-tag rung-${rung}` : "bg-slate-50 border-slate-200"}`}
                >
                  <span className="block text-[10px] font-bold text-slate-600">{fmtLimit(limit)}</span>
                  <span className="block text-lg font-black text-slate-900 mt-1">{Math.round(samplingRateOf(rung) * 100)}%</span>
                  <span className="block text-[9px] text-slate-500">sampled</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Governance Event Timeline — data-driven from PolicyVersionOut[] */}
        {eventTimeline.length > 0 && (
          <div className="editorial-panel p-6">
            <span className="eyebrow-label block mb-1">CHRONOLOGICAL AUDIT</span>
            <h3 className="text-lg font-black text-slate-900 mb-4 border-b border-slate-200 pb-2">
              Governance Event History
            </h3>

            <div className="relative border-l-2 border-slate-200 ml-4 space-y-6 pl-6 py-2 text-xs font-sans">
              {eventTimeline.map((evt, i) => (
                <div key={i} className="relative">
                  <div className={`absolute -left-[29px] w-2.5 h-2.5 rounded-full ${evt.color}`} />
                  <span className="font-mono text-slate-400 text-[11px] block">{evt.time}</span>
                  <p className="font-bold text-slate-900 text-xs">{evt.title}</p>
                  <p className="text-slate-500">{evt.description}</p>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Recent Decisions */}
        <div className="editorial-panel p-6">
          <span className="eyebrow-label block mb-1">DECISION LOG</span>
          <h3 className="text-lg font-black text-slate-900 mb-4 border-b border-slate-200 pb-2">
            Recent Decisions
          </h3>
          {agentDecisions.length > 0 ? (
            <div className="space-y-2">
              {agentDecisions.slice(0, 20).map(r => {
                const time = r.decided_at
                  ? new Date(r.decided_at).toLocaleString("en-IN", {
                      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
                    })
                  : "—";
                return (
                  <div key={r.decision_id} className="bg-white border border-slate-200 rounded-[4px] p-3 flex items-center justify-between gap-3 text-xs hover:bg-slate-50 transition-colors">
                    <div className="flex items-center gap-3 min-w-0">
                      <span className={`px-2 py-0.5 font-bold uppercase rounded-[3px] border ${
                        r.action === "APPROVE" ? "bg-[#86BC25]/10 text-[#5f8914] border-[#86BC25]/30" :
                        r.action === "REJECT" ? "bg-red-50 text-red-700 border-red-200" :
                        "bg-amber-50 text-amber-800 border-amber-200"
                      }`}>
                        {r.action}
                      </span>
                      <span className="font-mono font-medium text-slate-900 mr-2">{r.invoice_id}</span>
                      <span className="font-bold text-slate-700">₹{r.amount.toLocaleString("en-IN")}</span>
                    </div>
                    <div className="flex items-center gap-4 text-slate-500 flex-shrink-0">
                      <span>{time}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-slate-500 text-xs">No decision records found for this agent.</p>
          )}
        </div>
      </div>
    </div>
  );
}
