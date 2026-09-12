"use client";
/**
 * Page 5: /simulation — Governance Simulation Control Room
 *
 * KEY CHANGES:
 *  - No GET /simulation/runs (list-all) endpoint exists — dropped history panel
 *  - POST /simulation/runs body matches SimulationRunCreate:
 *    { agent_id, invoice_count, phase, reason, seed }
 *  - phase replaces agent_type: "good" | "degraded" | "recovery"
 *  - reason is mandatory
 *  - Can poll individual run status via GET /simulation/runs/{run_id}
 *  - Added isError handling
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { agentsApi, simulationApi } from "@/lib/api-client";
import { IconSimulation } from "@/components/ui/Icons";
import type { AgentOut, SimulationPhase, SimulationRunOut } from "@/types/api";

function fmtLimit(value: number): string {
  return `₹${value.toLocaleString("en-IN")}`;
}

export default function SimulationPage() {
  const qc = useQueryClient();
  const [phase, setPhase] = useState<SimulationPhase>("good");
  const [count, setCount] = useState(100);
  const [agentId, setAgentId] = useState("agent-01");
  const [reason, setReason] = useState("");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  // Poll active run status
  const { data: activeRun, isError: activeRunError } = useQuery<SimulationRunOut>({
    queryKey: ["simulation-run", activeRunId],
    queryFn: () => simulationApi.getRun(activeRunId!),
    enabled: activeRunId != null,
    refetchInterval: 2_000,
  });

  const {
    data: agentsData,
    isLoading: agentsLoading,
    isError: agentsError,
  } = useQuery({
    queryKey: ["agents"],
    queryFn: () => agentsApi.list(),
  });

  const agents = agentsData?.items ?? [];

  const { mutate: startRun, isPending, error: startError } = useMutation({
    mutationFn: () =>
      simulationApi.start({
        agent_id: agentId,
        invoice_count: count,
        phase,
        reason: reason.trim(),
        seed: 42,
      }),
    onSuccess: (data) => {
      setActiveRunId(data.run_id);
      // Invalidate agent/trust queries so the dashboard picks up new decisions
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["agent"] });
    },
  });

  const isRunComplete = activeRun?.status === "completed" || activeRun?.status === "failed";

  return (
    <div>
      {/* Header */}
      <div className="editorial-header">
        <span className="eyebrow-label">GOVERNANCE TEST ENVIRONMENT</span>
        <h1 className="text-3xl font-black text-slate-900 tracking-tight mt-1">Simulation</h1>
        <p className="text-xs font-medium text-slate-600 max-w-xl mt-1">
          Introduce controlled distribution shifts and observe how earned autonomy responds.
        </p>
      </div>

      <div className="editorial-content space-y-8">
        {/* Configuration & Controls Panel */}
        <div className="editorial-panel p-6">
          <span className="eyebrow-label block mb-1">ENVIRONMENT CONTROLS</span>
          <h2 className="text-xl font-black text-slate-900 tracking-tight mb-4 border-b border-slate-200 pb-2">
            Simulation Setup
          </h2>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
            {/* Agent ID */}
            <div>
              <label className="eyebrow-label block mb-1">AGENT ID</label>
              <select
                id="sim-agent-select"
                value={agentId}
                onChange={e => setAgentId(e.target.value)}
                className="w-full bg-white border border-slate-300 rounded-[2px] px-3 py-2 text-xs font-bold text-slate-900 focus:outline-none focus:border-[#86BC25]"
              >
                {agents.map((agent: AgentOut) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.id} (Rung {agent.current_rung}, {fmtLimit(agent.current_limit)})
                  </option>
                ))}
              </select>
              {agentsLoading && <p className="text-[10px] text-slate-500 mt-1">Loading agents...</p>}
              {agentsError && <p className="text-[10px] text-red-700 mt-1">Couldn&apos;t load agents. Check that the backend is running.</p>}
              {!agentsLoading && !agentsError && agents.length === 0 && (
                <p className="text-[10px] text-slate-500 mt-1">No agents are available.</p>
              )}
            </div>

            {/* Phase */}
            <div>
              <label className="eyebrow-label block mb-1">SIMULATION PHASE</label>
              <select
                id="sim-phase-select"
                value={phase}
                onChange={e => setPhase(e.target.value as SimulationPhase)}
                className="w-full bg-white border border-slate-300 rounded-[2px] px-3 py-2 text-xs font-bold text-slate-900 focus:outline-none focus:border-[#86BC25]"
              >
                <option value="good">Good (Normal performance)</option>
                <option value="degraded">Degraded (Performance drop → triggers drift)</option>
                <option value="recovery">Recovery (Performance recovers)</option>
              </select>
            </div>

            {/* Invoice count */}
            <div>
              <label className="eyebrow-label block mb-1">INVOICE COUNT</label>
              <select
                id="sim-count-select"
                value={count}
                onChange={e => setCount(Number(e.target.value))}
                className="w-full bg-white border border-slate-300 rounded-[2px] px-3 py-2 text-xs font-bold text-slate-900 focus:outline-none focus:border-[#86BC25]"
              >
                <option value={50}>50 invoices</option>
                <option value={100}>100 invoices</option>
                <option value={200}>200 invoices</option>
              </select>
            </div>

            {/* Reason (mandatory) */}
            <div>
              <label className="eyebrow-label block mb-1">REASON (MANDATORY)</label>
              <input
                type="text"
                id="sim-reason-input"
                value={reason}
                onChange={e => setReason(e.target.value)}
                placeholder="Why this simulation is being run"
                className="w-full bg-white border border-slate-300 rounded-[2px] px-3 py-2 text-xs font-medium text-slate-900 focus:outline-none focus:border-[#86BC25]"
              />
            </div>
          </div>

          {startError && (
            <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-[2px]">
              <span className="text-xs text-red-700 font-bold">
                Failed to start simulation: {(startError as Error).message}
              </span>
            </div>
          )}

          {activeRunError && (
            <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-[2px]">
              <span className="text-xs text-red-700 font-bold">
                Couldn&apos;t load simulation progress. Check that the backend is running.
              </span>
            </div>
          )}

          <button
            id="sim-start-btn"
            onClick={() => startRun()}
            disabled={isPending || !reason.trim() || agentsLoading || agentsError || agents.length === 0}
            className="flex items-center justify-center gap-2 px-6 py-3 rounded-[2px] bg-[#86BC25] hover:bg-[#72a31d] text-white text-xs font-black transition-colors disabled:opacity-50"
          >
            <IconSimulation className="w-4 h-4" />
            <span>{isPending ? "SIMULATING..." : "LAUNCH SIMULATION"}</span>
          </button>
        </div>

        {/* Simulation Progress — from active run polling */}
        {activeRun && (
          <div className={`editorial-panel p-6 border-l-4 ${
            activeRun.status === "failed" ? "border-red-400 bg-red-50/30" :
            activeRun.status === "completed" ? "border-[#86BC25] bg-[#F7F8F6]" :
            "border-[#86BC25] bg-[#F7F8F6]"
          }`}>
            <div className="flex items-center justify-between mb-3">
              <span className="eyebrow-label text-slate-900">
                SIMULATION {activeRun.status.toUpperCase()}
              </span>
              <span className="font-mono text-xs text-slate-500">{activeRun.run_id}</span>
            </div>

            {!isRunComplete && (
              <div className="w-full bg-slate-200 h-2 rounded-full overflow-hidden mb-4">
                <div className="bg-[#86BC25] h-full animate-pulse w-full" />
              </div>
            )}

            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-xs">
              <div>
                <span className="eyebrow-label text-[9px] block">PHASE</span>
                <span className="font-bold text-slate-900 uppercase">{activeRun.phase}</span>
              </div>
              <div>
                <span className="eyebrow-label text-[9px] block">INVOICES</span>
                <span className="font-bold text-slate-900">{activeRun.invoice_count}</span>
              </div>
              <div>
                <span className="eyebrow-label text-[9px] block">DECISIONS SUBMITTED</span>
                <span className="font-bold text-slate-900">{activeRun.decisions_submitted}</span>
              </div>
              <div>
                <span className="eyebrow-label text-[9px] block">STATUS</span>
                <span className={`font-bold ${
                  activeRun.status === "completed" ? "text-[#5f8914]" :
                  activeRun.status === "failed" ? "text-red-700" :
                  "text-amber-800"
                }`}>
                  {activeRun.status.toUpperCase()}
                </span>
              </div>
            </div>

            {isRunComplete && activeRun.accuracy != null && (
              <div className="mt-4 pt-4 border-t border-slate-200 grid grid-cols-2 gap-4 text-xs">
                <div>
                  <span className="eyebrow-label text-[9px] block">ACCURACY</span>
                  <span className="text-base font-black text-slate-900">
                    {Math.round(activeRun.accuracy * 100)}%
                  </span>
                </div>
                {activeRun.wilson_lower_bound != null && (
                  <div>
                    <span className="eyebrow-label text-[9px] block">WILSON LOWER BOUND</span>
                    <span className="text-base font-black text-blue-700">
                      {Math.round(activeRun.wilson_lower_bound * 100)}%
                    </span>
                  </div>
                )}
              </div>
            )}

            {!isRunComplete && (
              <p className="text-xs text-slate-600 font-medium mt-3">
                Check the agent detail page for live trust evaluation updates.
              </p>
            )}
          </div>
        )}

        {/* Info panel instead of history (no list-all endpoint) */}
        {!activeRun && !isPending && (
          <div className="editorial-panel p-6">
            <span className="eyebrow-label block mb-1">HOW IT WORKS</span>
            <h2 className="text-xl font-black text-slate-900 tracking-tight mb-4 border-b border-slate-200 pb-2">
              Simulation Guide
            </h2>
            <div className="space-y-3 text-xs text-slate-600 font-medium leading-relaxed">
              <p>
                <strong className="text-slate-900">Good phase:</strong> Normal agent performance. The agent processes invoices with high accuracy, building evidence for autonomy increases.
              </p>
              <p>
                <strong className="text-slate-900">Degraded phase:</strong> Inject a performance drop. The trust engine detects drift, and the system automatically claws back autonomy — this is the demo&apos;s most dramatic beat.
              </p>
              <p>
                <strong className="text-slate-900">Recovery phase:</strong> Performance returns to normal. After enough clean decisions, the system will recommend an increase again.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
