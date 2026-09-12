"use client";
/**
 * Page 4: /audit — Immutable Governance Record Table & Detail Drawer
 *
 * The audit log IS hash-chained. The backend verifies the full chain
 * and returns `chain_valid` + `chain_verified_scope` in the response.
 * The frontend renders the result — it does NOT recompute the chain.
 *
 * KEY CHANGES:
 *  - Deleted verifyChain() — business logic in the frontend is forbidden
 *  - Read chain_valid from API response instead
 *  - Fixed EVENT_TYPE_BADGE keys to use dotted format (the real event_type values)
 *  - Fixed fmtEventType to split on `.` instead of `_`
 *  - Added isError handling
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { auditLogApi } from "@/lib/api-client";
import { IconShieldAlert, IconCheckCircle } from "@/components/ui/Icons";
import type { AuditLogEntry } from "@/types/api";

/** Real event_type values are dotted: "decision.recorded", "policy_version.created", etc. */
const EVENT_TYPE_BADGE: Record<string, string> = {
  "decision.recorded":         "bg-green-100 text-[#5f8914] border-green-200",
  "policy_version.created":    "bg-amber-100 text-amber-900 border-amber-200",
  "recommendation.generated":  "bg-purple-100 text-purple-700 border-purple-200",
  "recommendation.approved":   "bg-green-100 text-[#5f8914] border-green-200",
  "recommendation.rejected":   "bg-red-100 text-red-700 border-red-200",
  "audit_sample.reviewed":     "bg-cyan-100 text-cyan-800 border-cyan-200",
};

/** Format dotted event types into readable titles: "decision.recorded" → "Decision Recorded" */
function fmtEventType(type: string): string {
  return type
    .split(".")
    .map(part => part.replace(/_/g, " "))
    .map(part => part.replace(/\b\w/g, c => c.toUpperCase()))
    .join(" · ");
}

export default function AuditPage() {
  const [page, setPage] = useState(1);
  const [selectedEntry, setSelectedEntry] = useState<AuditLogEntry | null>(null);
  const PAGE_SIZE = 25;

  const { data, isLoading, isError } = useQuery({
    queryKey: ["audit-log", page],
    queryFn: () => auditLogApi.list({ page, page_size: PAGE_SIZE }),
    refetchInterval: 10_000,
  });

  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 1;

  // chain_valid and chain_verified_scope come directly from the backend
  const chainValid = data?.chain_valid;
  const chainScope = data?.chain_verified_scope;

  return (
    <div>
      {/* Header */}
      <div className="editorial-header">
        <div className="flex items-center justify-between">
          <div>
            <span className="eyebrow-label">IMMUTABLE GOVERNANCE RECORD</span>
            <h1 className="text-3xl font-black text-slate-900 tracking-tight mt-1">Audit Trail</h1>
            <p className="text-xs font-medium text-slate-600 max-w-xl mt-1">
              Every automated decision, human intervention, and autonomy change is recorded in a hash-chained log.
            </p>
          </div>

          {/* Dynamic integrity indicator — from backend chain_valid */}
          {data && data.items.length > 0 ? (
            chainValid ? (
              <div className="flex items-center gap-2 px-3 py-1.5 bg-green-50 border border-green-200 rounded-[2px]">
                <IconCheckCircle className="w-4 h-4 text-[#5f8914]" />
                <div>
                  <span className="text-xs font-bold text-[#5f8914] block">Hash Chain: Verified</span>
                  <span className="text-[10px] text-green-700">
                    Scope: {chainScope} · {data.total} entries
                  </span>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-2 px-3 py-1.5 bg-red-50 border border-red-200 rounded-[2px]">
                <IconShieldAlert className="w-4 h-4 text-red-700" />
                <div>
                  <span className="text-xs font-bold text-red-700 block">Hash Chain: BROKEN</span>
                  <span className="text-[10px] text-red-600">Tampering detected — investigate immediately</span>
                </div>
              </div>
            )
          ) : (
            <div className="flex items-center gap-2 px-3 py-1.5 bg-slate-50 border border-slate-200 rounded-[2px]">
              <IconShieldAlert className="w-4 h-4 text-slate-400" />
              <span className="text-xs font-bold text-slate-500">No entries to verify</span>
            </div>
          )}
        </div>
      </div>

      <div className="editorial-content">
        {isError && (
          <div className="editorial-panel p-6 border-l-4 border-red-400 mb-6">
            <span className="text-xs text-red-700 font-bold block">Unable to load audit log.</span>
            <p className="text-xs text-slate-500 mt-1">Check that the backend is running.</p>
          </div>
        )}

        <div className="flex flex-col xl:flex-row gap-6">
          {/* Main Audit Data Table */}
          <div className="editorial-panel overflow-hidden flex-1">
            {isLoading ? (
              <div className="p-8 text-xs font-bold text-slate-400 uppercase tracking-widest animate-pulse">
                LOADING AUDIT LOG...
              </div>
            ) : (
              <table className="editorial-table">
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Event</th>
                    <th>Actor</th>
                    <th>Entity</th>
                    <th>Hash</th>
                    <th>Timestamp</th>
                  </tr>
                </thead>
                <tbody>
                  {data?.items.map((entry) => {
                    const isSelected = selectedEntry?.id === entry.id;
                    const badge = EVENT_TYPE_BADGE[entry.event_type] ?? "bg-slate-100 text-slate-700 border-slate-200";

                    return (
                      <tr
                        key={entry.id}
                        onClick={() => setSelectedEntry(entry)}
                        className={`cursor-pointer transition-colors ${
                          isSelected ? "bg-slate-100 font-semibold" : "hover:bg-slate-50"
                        }`}
                      >
                        <td>
                          <span className="font-mono text-xs font-bold text-slate-900">
                            {entry.id}
                          </span>
                        </td>
                        <td>
                          <span className={`inline-block px-2 py-0.5 text-[10px] font-black uppercase rounded-[2px] border ${badge}`}>
                            {fmtEventType(entry.event_type)}
                          </span>
                        </td>
                        <td>
                          <div>
                            <span className="text-xs font-semibold text-slate-800">{entry.actor}</span>
                            <span className="text-[10px] text-slate-400 ml-1">({entry.actor_type})</span>
                          </div>
                        </td>
                        <td>
                          <div>
                            <span className="text-[10px] font-bold text-slate-500 uppercase">{entry.entity_type}</span>
                            <span className="text-xs font-mono text-slate-700 ml-1">{entry.entity_id}</span>
                          </div>
                        </td>
                        <td>
                          <div className="flex items-center gap-1">
                            <span className="font-mono text-[10px] text-slate-400 truncate max-w-[80px]">
                              {entry.hash.slice(0, 12)}…
                            </span>
                          </div>
                        </td>
                        <td>
                          <span className="text-xs text-slate-500 font-medium">
                            {new Date(entry.ts).toLocaleString("en-IN", {
                              month: "short", day: "numeric",
                              hour: "2-digit", minute: "2-digit", hour12: false,
                            })}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}

            {/* Pagination Footer */}
            {totalPages > 1 && (
              <div className="p-4 border-t border-slate-200 flex items-center justify-between text-xs text-slate-600 bg-slate-50">
                <span>
                  Page {page} of {totalPages} ({data?.total.toLocaleString()} total audit entries)
                </span>
                <div className="flex gap-2">
                  <button
                    onClick={() => setPage(p => Math.max(1, p - 1))}
                    disabled={page === 1}
                    className="px-3 py-1 bg-white border border-slate-300 rounded-[2px] font-bold text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                  >
                    PREVIOUS
                  </button>
                  <button
                    onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                    disabled={page === totalPages}
                    className="px-3 py-1 bg-white border border-slate-300 rounded-[2px] font-bold text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                  >
                    NEXT
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* Right Contextual Inspection Panel */}
          {selectedEntry && (
            <div className="editorial-panel p-6 w-full xl:w-96 flex-shrink-0 space-y-4">
              <div className="flex items-center justify-between border-b border-slate-200 pb-3">
                <span className="eyebrow-label">RECORD INSPECTION</span>
                <button
                  onClick={() => setSelectedEntry(null)}
                  className="text-xs font-bold text-slate-400 hover:text-slate-900"
                >
                  ✕ CLOSE
                </button>
              </div>

              <div className="space-y-3 text-xs font-sans">
                <div>
                  <span className="eyebrow-label block text-[9px]">ENTRY ID</span>
                  <span className="font-mono font-bold text-slate-900">{selectedEntry.id}</span>
                </div>
                <div>
                  <span className="eyebrow-label block text-[9px]">EVENT TYPE</span>
                  <span className={`inline-block px-2 py-0.5 text-[10px] font-black uppercase rounded-[2px] border ${EVENT_TYPE_BADGE[selectedEntry.event_type] ?? "bg-slate-100 text-slate-700 border-slate-200"}`}>
                    {fmtEventType(selectedEntry.event_type)}
                  </span>
                </div>
                <div>
                  <span className="eyebrow-label block text-[9px]">ACTOR</span>
                  <span className="font-semibold text-slate-800">{selectedEntry.actor}</span>
                  <span className="text-[10px] text-slate-400 ml-1">({selectedEntry.actor_type})</span>
                </div>
                <div>
                  <span className="eyebrow-label block text-[9px]">ENTITY</span>
                  <span className="text-[10px] font-bold text-slate-500 uppercase">{selectedEntry.entity_type} </span>
                  <span className="font-mono font-bold text-slate-900">{selectedEntry.entity_id}</span>
                </div>
                <div>
                  <span className="eyebrow-label block text-[9px]">TIMESTAMP</span>
                  <span className="font-mono text-slate-600">{selectedEntry.ts}</span>
                </div>

                {/* Hash chain details */}
                <div className="border-t border-slate-200 pt-3 space-y-2">
                  <span className="eyebrow-label block text-[9px]">HASH CHAIN</span>
                  <div>
                    <span className="text-[9px] font-bold text-slate-500 block">PREV HASH</span>
                    <span className="font-mono text-[10px] text-slate-600 break-all leading-relaxed">
                      {selectedEntry.prev_hash}
                    </span>
                  </div>
                  <div>
                    <span className="text-[9px] font-bold text-slate-500 block">HASH</span>
                    <span className="font-mono text-[10px] text-slate-900 break-all leading-relaxed font-bold">
                      {selectedEntry.hash}
                    </span>
                  </div>
                </div>

                {/* Payload */}
                <div className="border-t border-slate-200 pt-3">
                  <span className="eyebrow-label block text-[9px] mb-1">PAYLOAD</span>
                  <pre className="bg-slate-50 border border-slate-200 rounded-[2px] p-3 text-[10px] font-mono text-slate-700 overflow-x-auto leading-relaxed">
                    {JSON.stringify(selectedEntry.payload, null, 2)}
                  </pre>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
