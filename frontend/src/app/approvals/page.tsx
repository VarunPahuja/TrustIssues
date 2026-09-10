"use client";
/**
 * Page 3: /approvals — Governance Recommendation Review Queue
 *
 * Each row is a Recommendation from governance, not a simple HumanApproval.
 * Shows proposed changes, governance agent opinions, and dissent.
 * Mandatory reason field before approve/reject.
 *
 * KEY CHANGE: approve and reject are two separate endpoints:
 *   POST /recommendations/{id}/approve  { reason }
 *   POST /recommendations/{id}/reject   { reason }
 * The old "resolve" endpoint does not exist.
 *
 * Error handling:
 *   403 = not admin, 409 = already resolved
 */

import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { recommendationsApi } from "@/lib/api-client";
import type { Recommendation, AgentOpinion, OpinionVerdict, RecommendationStatus, Direction } from "@/types/api";

const STATUS_CLASS: Record<RecommendationStatus, string> = {
  PENDING:    "bg-amber-100 text-amber-900",
  APPROVED:   "bg-green-100 text-[#5f8914]",
  REJECTED:   "bg-red-100 text-red-700",
  SUPERSEDED: "bg-slate-100 text-slate-600",
};

const VERDICT_CLASS: Record<OpinionVerdict, string> = {
  CONCUR:  "verdict-concur",
  OBJECT:  "verdict-object",
  ABSTAIN: "verdict-abstain",
};

const DIRECTION_LABEL: Record<Direction, string> = {
  INCREASE: "↑ INCREASE",
  HOLD:     "— HOLD",
  CLAWBACK: "↓ CLAWBACK",
};

function fmtLimit(val: number): string {
  if (val >= 1000) return `₹${(val / 1000).toFixed(0)}k`;
  return `₹${val}`;
}

function fmtTimeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const h = Math.floor(diff / 3_600_000);
  const m = Math.floor((diff % 3_600_000) / 60_000);
  if (h > 24) return `${Math.floor(h / 24)}d ago`;
  if (h > 0) return `${h}h ago`;
  return `${m}m ago`;
}

export function OpinionCard({ opinion }: { opinion: AgentOpinion }) {
  return (
    <div className={`border rounded-[2px] p-3 text-xs ${opinion.verdict === "OBJECT" ? "border-red-300 bg-red-50/50" : "border-slate-200 bg-white"}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="font-extrabold text-slate-900 capitalize">{opinion.agent_name}</span>
        <span className={`state-badge ${VERDICT_CLASS[opinion.verdict]}`}>
          {opinion.verdict}
        </span>
      </div>
      <p className="text-slate-600 font-medium leading-relaxed mb-1">{opinion.reasoning}</p>
      {opinion.concerns.length > 0 && (
        <div className="mt-2 space-y-1">
          {opinion.concerns.map((c, i) => (
            <p key={i} className="text-[11px] text-red-700 font-medium flex items-start gap-1">
              <span className="text-red-400 mt-0.5">⚠</span>
              <span>{c}</span>
            </p>
          ))}
        </div>
      )}
      <div className="mt-2 text-[10px] text-slate-400">
        Confidence: <span className="font-bold text-slate-600">{Math.round(opinion.confidence * 100)}%</span>
      </div>
    </div>
  );
}

export default function ApprovalsPage() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<"PENDING" | "APPROVED" | "REJECTED" | "all">("PENDING");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [reasonInputs, setReasonInputs] = useState<Record<string, string>>({});
  const [mutationError, setMutationError] = useState<string | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["recommendations", filter],
    queryFn: () => recommendationsApi.list(filter === "all" ? undefined : filter),
  });

  const recommendations = data?.items ?? [];

  const approveMutation = useMutation({
    mutationFn: ({ recId, reason }: { recId: string; reason: string }) =>
      recommendationsApi.approve(recId, reason),
    onSuccess: () => {
      setMutationError(null);
      qc.invalidateQueries({ queryKey: ["recommendations"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["agent"] });
    },
    onError: (error: any) => {
      if (error.status === 403) {
        setMutationError("Permission denied: only admins can approve recommendations.");
      } else if (error.status === 409) {
        setMutationError("This recommendation has already been resolved.");
        qc.invalidateQueries({ queryKey: ["recommendations"] });
      } else {
        setMutationError(error.message ?? "Failed to approve.");
      }
    },
  });

  const rejectMutation = useMutation({
    mutationFn: ({ recId, reason }: { recId: string; reason: string }) =>
      recommendationsApi.reject(recId, reason),
    onSuccess: () => {
      setMutationError(null);
      qc.invalidateQueries({ queryKey: ["recommendations"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["agent"] });
    },
    onError: (error: any) => {
      if (error.status === 403) {
        setMutationError("Permission denied: only admins can reject recommendations.");
      } else if (error.status === 409) {
        setMutationError("This recommendation has already been resolved.");
        qc.invalidateQueries({ queryKey: ["recommendations"] });
      } else {
        setMutationError(error.message ?? "Failed to reject.");
      }
    },
  });

  const handleApprove = (recId: string) => {
    const reason = reasonInputs[recId]?.trim();
    if (!reason) return;
    setMutationError(null);
    approveMutation.mutate({ recId, reason });
  };

  const handleReject = (recId: string) => {
    const reason = reasonInputs[recId]?.trim();
    if (!reason) return;
    setMutationError(null);
    rejectMutation.mutate({ recId, reason });
  };

  const isMutating = approveMutation.isPending || rejectMutation.isPending;
  const pendingCount = recommendations.filter(r => r.status === "PENDING").length;

  return (
    <div>
      {/* Header */}
      <div className="editorial-header">
        <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
          <div>
            <span className="eyebrow-label">GOVERNANCE REVIEW QUEUE</span>
            <h1 className="text-3xl font-black text-slate-900 tracking-tight mt-1">Recommendations</h1>
            <p className="text-xs font-medium text-slate-600 max-w-xl mt-1 leading-relaxed">
              Review governance recommendations with full agent opinions and dissent analysis.
            </p>
          </div>

          {/* Large Prominent Counter */}
          <div className="bg-amber-50 border border-amber-200 px-5 py-2.5 rounded-[2px] text-right">
            <span className="eyebrow-label text-[9px] text-amber-900 block">PENDING REVIEW</span>
            <span className="text-3xl font-black text-amber-900">{pendingCount}</span>
          </div>
        </div>

        {/* Clean Filter Tabs */}
        <div className="flex border-b border-slate-200 mt-6 gap-6 text-xs font-semibold font-sans">
          {(["PENDING", "APPROVED", "REJECTED", "all"] as const).map(f => (
            <button
              key={f}
              id={`filter-${f.toLowerCase()}`}
              onClick={() => setFilter(f)}
              className={`pb-3 uppercase tracking-wider text-[11px] font-bold transition-colors border-b-2 ${
                filter === f
                  ? "border-[#86BC25] text-slate-900"
                  : "border-transparent text-slate-400 hover:text-slate-900"
              }`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {/* Main Review Queue Content */}
      <div className="editorial-content space-y-4">
        {isLoading && (
          <div className="text-xs font-bold text-slate-400 uppercase tracking-widest animate-pulse">
            LOADING RECOMMENDATIONS...
          </div>
        )}

        {isError && (
          <div className="editorial-panel p-6 border-l-4 border-red-400">
            <span className="text-xs text-red-700 font-bold block">Unable to load recommendations.</span>
            <p className="text-xs text-slate-500 mt-1">Check that the backend is running.</p>
          </div>
        )}

        {mutationError && (
          <div className="editorial-panel p-4 border-l-4 border-red-400 bg-red-50">
            <span className="text-xs text-red-700 font-bold">{mutationError}</span>
          </div>
        )}

        {!isLoading && !isError && recommendations.length === 0 && (
          <div className="editorial-panel p-8 text-center text-xs text-slate-500 font-medium">
            No {filter.toLowerCase()} recommendations requiring attention.
          </div>
        )}

        {!isLoading && !isError && recommendations.map(rec => {
          const isExpanded = expandedId === rec.recommendation_id;
          const isPending = rec.status === "PENDING";
          const reason = reasonInputs[rec.recommendation_id] ?? "";

          return (
            <div
              key={rec.recommendation_id}
              className={`editorial-panel p-5 space-y-3 ${rec.has_dissent && isPending ? "border-l-4 border-red-400" : ""}`}
            >
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                {/* Main Row Information — data-driven */}
                <div className="flex items-center gap-4 flex-1">
                  <span className={`state-badge direction-${rec.direction.toLowerCase()}`}>
                    {DIRECTION_LABEL[rec.direction]}
                  </span>
                  <span className="font-mono text-sm font-black text-slate-900">{rec.agent_id}</span>
                  <span className="text-sm font-black text-slate-900">{fmtLimit(rec.proposed_limit)}</span>
                  <span className="text-xs text-slate-500">Rung {rec.proposed_rung}</span>
                  {rec.clamped && (
                    <span className="text-[10px] font-bold text-amber-800 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-[2px]">
                      CLAMPED from {fmtLimit(rec.clamped_from!)}
                    </span>
                  )}
                  {rec.has_dissent && (
                    <span className="text-[10px] font-extrabold text-red-700 bg-red-50 border border-red-200 px-2 py-0.5 rounded-[2px]">
                      ⚠ DISSENT
                    </span>
                  )}
                </div>

                <div className="flex items-center gap-4">
                  {rec.generated_at && (
                    <span className="text-xs text-slate-400 font-mono">{fmtTimeAgo(rec.generated_at)}</span>
                  )}
                  <span className={`px-2 py-0.5 text-[10px] font-extrabold uppercase rounded-[2px] ${STATUS_CLASS[rec.status]}`}>
                    {rec.status}
                  </span>
                </div>
              </div>

              {/* Rationale */}
              <p className="text-xs text-slate-600 font-medium">{rec.rationale}</p>

              {/* Approve/Reject with mandatory reason */}
              {isPending && (
                <div className="flex items-center gap-3 pt-2 border-t border-slate-200/60">
                  <input
                    type="text"
                    placeholder="Reason (mandatory)"
                    value={reason}
                    onChange={e => setReasonInputs(prev => ({ ...prev, [rec.recommendation_id]: e.target.value }))}
                    className="flex-1 px-3 py-1.5 text-xs bg-white border border-slate-300 rounded-[2px] font-medium"
                  />
                  <button
                    onClick={() => handleApprove(rec.recommendation_id)}
                    disabled={!reason.trim() || isMutating}
                    className="px-3 py-1.5 rounded-[2px] bg-[#86BC25] hover:bg-[#72a31d] text-white text-xs font-bold transition-colors disabled:opacity-50"
                  >
                    APPROVE
                  </button>
                  <button
                    onClick={() => handleReject(rec.recommendation_id)}
                    disabled={!reason.trim() || isMutating}
                    className="px-3 py-1.5 rounded-[2px] bg-red-600 hover:bg-red-700 text-white text-xs font-bold transition-colors disabled:opacity-50"
                  >
                    REJECT
                  </button>
                </div>
              )}

              {/* Expandable: Governance Agent Opinions */}
              <div className="pt-2 border-t border-slate-200/60">
                <button
                  onClick={() => setExpandedId(isExpanded ? null : rec.recommendation_id)}
                  className="text-[11px] font-bold text-slate-600 hover:text-slate-900 flex items-center gap-1"
                >
                  <span>{isExpanded ? "▲ Hide governance opinions" : `▼ ${rec.opinions.length} governance agent opinions${rec.has_dissent ? " (DISSENT)" : ""}`}</span>
                </button>

                {isExpanded && (
                  <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
                    {rec.opinions.map(opinion => (
                      <OpinionCard key={opinion.agent_name} opinion={opinion} />
                    ))}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
