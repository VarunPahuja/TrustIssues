"use client";
/**
 * Page 1: /agents — Governed AI Workforce Overview
 *
 * Renders from AgentOut — the real shape returned by GET /agents.
 * AgentOut only carries: id, name, current_limit, current_rung, state, context.
 * Trust-level fields (trust_score, accuracy, drift, etc.) live on
 * GET /agents/{id}/trust and are NOT fetched here (fast path per audit).
 */

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { agentsApi } from "@/lib/api-client";
import { AutonomyLadder } from "@/components/domain/AutonomyLadder";
import type { AgentOut, AgentState } from "@/types/api";

const STATE_CLASS: Record<AgentState, string> = {
  probation:  "state-badge state-probation",
  active:     "state-badge state-active",
  restricted: "state-badge state-restricted",
  suspended:  "state-badge state-suspended",
};

function fmtLimit(val: number): string {
  if (val >= 1000) return `₹${(val / 1000).toFixed(0)}k`;
  return `₹${val}`;
}

export default function AgentsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["agents"],
    queryFn: () => agentsApi.list(),
  });

  const agents = data?.items ?? [];
  const totalAuthority = agents.reduce((acc, a) => acc + a.current_limit, 0);
  const activeCount = agents.length;
  const attentionCount = agents.filter(a => a.state === "restricted" || a.state === "suspended").length;

  return (
    <div>
      {/* Header */}
      <div className="editorial-header">
        <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
          <div>
            <span className="eyebrow-label">GOVERNED AI WORKFORCE</span>
            <h1 className="text-3xl font-black text-slate-900 tracking-tight mt-1">Agents</h1>
            <p className="text-xs font-medium text-slate-600 max-w-xl mt-1 leading-relaxed">
              Monitor earned autonomy, statistical reliability, and human oversight across registered AI agents.
            </p>
          </div>

          {/* Right Summary Block */}
          <div className="flex items-center gap-4 bg-[#F7F8F6] border border-[#E2E8F0] px-4 py-2.5 rounded-[2px] font-sans">
            <div>
              <span className="eyebrow-label text-[9px] block">ACTIVE AGENTS</span>
              <span className="text-sm font-black text-slate-900">{activeCount}</span>
            </div>
            <div className="w-[1px] h-7 bg-slate-200" />
            <div>
              <span className="eyebrow-label text-[9px] block">TOTAL AUTHORITY</span>
              <span className="text-sm font-black text-slate-900">
                ₹{totalAuthority.toLocaleString("en-IN")}
              </span>
            </div>
            <div className="w-[1px] h-7 bg-slate-200" />
            <div>
              <span className="eyebrow-label text-[9px] block">REQUIRES ATTENTION</span>
              <span className={`text-sm font-black ${attentionCount > 0 ? "text-amber-800" : "text-slate-900"}`}>
                {attentionCount}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Main Content Area */}
      <div className="editorial-content space-y-6">
        {isLoading && (
          <div className="text-xs text-slate-500 font-bold tracking-wider uppercase animate-pulse">
            LOADING AGENT WORKFORCE REGISTRY...
          </div>
        )}

        {isError && (
          <div className="editorial-panel p-6 border-l-4 border-red-400">
            <span className="text-xs text-red-700 font-bold block">Unable to connect to governance API.</span>
            <p className="text-xs text-slate-500 mt-1">Check that the backend is running on the expected port.</p>
          </div>
        )}

        {!isLoading && !isError && agents.length === 0 && (
          <div className="editorial-panel p-8 text-center text-xs text-slate-500 font-medium">
            No agents registered yet.
          </div>
        )}

        {!isLoading && agents.map((agent: AgentOut) => (
          <div
            key={agent.id}
            className="editorial-panel p-6 hover:border-slate-300 transition-colors"
          >
            <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-6">
              {/* Left Agent identity */}
              <div className="space-y-1.5 min-w-[220px]">
                <div className="flex items-center gap-2">
                  <span className={`rung-tag rung-${agent.current_rung}`}>
                    RUNG {agent.current_rung}
                  </span>
                  <span className={STATE_CLASS[agent.state]}>
                    {agent.state.toUpperCase()}
                  </span>
                </div>
                <h2 className="text-xl font-extrabold text-slate-900 tracking-tight">
                  {agent.name}
                </h2>
                <p className="text-xs font-mono text-slate-400">{agent.id}</p>
              </div>

              {/* Autonomy Ladder Visual */}
              <div className="border-l border-slate-200 pl-6 hidden xl:block">
                <AutonomyLadder currentRung={agent.current_rung} compact />
              </div>

              {/* Editorial Metric Strip — only fields from AgentOut */}
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-6 border-t lg:border-t-0 lg:border-l border-slate-200 pt-4 lg:pt-0 lg:pl-6">
                <div>
                  <span className="eyebrow-label block text-[9px]">CURRENT AUTHORITY</span>
                  <span className="text-base font-black text-slate-900">
                    {fmtLimit(agent.current_limit)}
                  </span>
                </div>
                <div>
                  <span className="eyebrow-label block text-[9px]">RUNG</span>
                  <span className="text-base font-black text-slate-900">
                    {agent.current_rung} / 4
                  </span>
                </div>
                <div>
                  <span className="eyebrow-label block text-[9px]">STATE</span>
                  <span className={STATE_CLASS[agent.state]}>
                    {agent.state.toUpperCase()}
                  </span>
                </div>
              </div>

              {/* View Agent Action Link */}
              <div className="flex items-center justify-end">
                <Link
                  href={`/agents/${agent.id}`}
                  className="inline-flex items-center gap-1.5 px-4 py-2 rounded-[2px] bg-[#86BC25] hover:bg-[#72a31d] text-white text-xs font-bold transition-colors"
                >
                  <span>VIEW AGENT</span>
                  <span>→</span>
                </Link>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
