"use client";
/**
 * /demo — Demo Console
 * ----------------------
 * The ten-beat arc, driven live through the real API, one beat at a time,
 * presenter-paced. Replaces the terminal (scripts/demo.ps1) in front of a
 * panel — same sequence, same API calls, same wording, but click-driven
 * with real numbers rendered on screen instead of printed to a console.
 *
 * scripts/demo.ps1 is the source of truth this mirrors. src/lib/demo-arc.ts
 * holds the actual per-beat logic (live API calls, or "replay" canned
 * responses from a real run); this file is presentation only.
 */

import { useState } from "react";
import { AutonomyLadder } from "@/components/domain/AutonomyLadder";
import { OpinionCard } from "@/app/approvals/page";
import { IconDemo } from "@/components/ui/Icons";
import {
  runBeat1,
  runBeat2,
  runBeat3,
  runBeat4,
  runBeat5,
  runBeat6,
  runBeat7,
  runBeat8,
  runBeat9,
  runBeat10,
  BeatFailure,
  type BeatError,
  type BeatResult,
  type Beat1Result,
  type Beat2Result,
  type Beat3Result,
  type Beat4Result,
  type Beat5Result,
  type Beat6Result,
  type Beat7Result,
  type Beat8Result,
  type Beat9Result,
  type Beat10Result,
  type DemoMode,
} from "@/lib/demo-arc";

interface BeatDef {
  label: string;
  caption: string;
}

const BEATS: BeatDef[] = [
  {
    label: "Agent-01's starting position",
    caption:
      "Agent-01 starts this story mid-ladder, on real seed data, not at the floor — show where it actually stands right now.",
  },
  {
    label: "Build evidence",
    caption:
      "Run a simulation to build fresh evidence, then record human rulings on a handful of escalations — beat 4's INCREASE needs both.",
  },
  {
    label: "The trust evaluation",
    caption:
      "The trust evaluation: this is the intellectual core. The headline number is never the raw point estimate — it's the Wilson lower bound.",
  },
  {
    label: "Generate a recommendation",
    caption:
      "Generate a recommendation. This is the demo's highest-stakes moment: a live-earned INCREASE, no seed data, four independent opinions.",
  },
  {
    label: "Human approval",
    caption: "A human approves the recommendation — the one step ADR-0004 requires before autonomy can go up.",
  },
  {
    label: "Autonomy rises",
    caption: "Show the limit actually moved, and the new policy version chained to the one it replaced.",
  },
  {
    label: "Inject a critical error",
    caption:
      "Inject a critical error: an APPROVE the ground truth says should have been a REJECT — real money that should not have gone out.",
  },
  {
    label: "Drift detected",
    caption:
      "Show drift detection catch it — a single critical error is enough for an immediate CRITICAL severity, no waiting for a statistical trend.",
  },
  {
    label: "Automatic clawback",
    caption:
      "Generate a recommendation again. Watch: CLAWBACK applies immediately, with NO approval call — ADR-0004's asymmetry made visible.",
  },
  {
    label: "Verify the audit chain",
    caption: "Verify the audit chain — every entry hash-linked to the one before it, recomputed fresh, not just asserted.",
  },
];

type BeatStatus = "idle" | "running" | "done" | "error";

interface BeatSlot {
  status: BeatStatus;
  result: BeatResult | null;
  error: BeatError | null;
}

function emptySlots(): BeatSlot[] {
  return BEATS.map(() => ({ status: "idle", result: null, error: null }));
}

function fmtLimit(v: number): string {
  return `₹${v.toLocaleString("en-IN")}`;
}

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${(v * 100).toFixed(1)}%`;
}

export default function DemoConsolePage() {
  const [mode, setMode] = useState<DemoMode>("live");
  const [stepIndex, setStepIndex] = useState(0);
  const [slots, setSlots] = useState<BeatSlot[]>(emptySlots());
  const [showResetInfo, setShowResetInfo] = useState(false);

  const slot = slots[stepIndex];
  const beat = BEATS[stepIndex];

  function setSlot(index: number, patch: Partial<BeatSlot>) {
    setSlots((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], ...patch };
      return next;
    });
  }

  function handleModeChange(next: DemoMode) {
    if (next === mode) return;
    setMode(next);
    setStepIndex(0);
    setSlots(emptySlots());
    setShowResetInfo(false);
  }

  function handleReset() {
    setStepIndex(0);
    setSlots(emptySlots());
    setShowResetInfo(true);
  }

  async function runCurrent() {
    setSlot(stepIndex, { status: "running", error: null });
    try {
      let result: BeatResult;
      switch (stepIndex) {
        case 0:
          result = await runBeat1(mode);
          break;
        case 1:
          result = await runBeat2(mode);
          break;
        case 2:
          result = await runBeat3(mode);
          break;
        case 3:
          result = await runBeat4(mode);
          break;
        case 4: {
          const beat4 = slots[3].result as Beat4Result | null;
          if (!beat4) {
            throw new BeatFailure({
              call: "POST /recommendations/{id}/approve",
              status: null,
              message: "Beat 4 hasn't produced a recommendation yet — run it first.",
            });
          }
          result = await runBeat5(mode, beat4.recommendationId);
          break;
        }
        case 5:
          result = await runBeat6(mode);
          break;
        case 6:
          result = await runBeat7(mode);
          break;
        case 7:
          result = await runBeat8(mode);
          break;
        case 8:
          result = await runBeat9(mode);
          break;
        case 9:
          result = await runBeat10(mode);
          break;
        default:
          return;
      }
      setSlot(stepIndex, { status: "done", result, error: null });
    } catch (err) {
      const detail: BeatError =
        err instanceof BeatFailure
          ? err.detail
          : { call: "unknown", status: null, message: err instanceof Error ? err.message : String(err) };
      setSlot(stepIndex, { status: "error", error: detail, result: null });
    }
  }

  const canGoNext = slot.status === "done" && stepIndex < BEATS.length - 1;
  const canGoBack = stepIndex > 0;

  return (
    <div>
      <div className="editorial-header">
        <div className="flex flex-col md:flex-row md:items-end justify-between gap-4">
          <div>
            <span className="eyebrow-label">PRESENTER MODE</span>
            <h1 className="text-3xl font-black text-slate-900 tracking-tight mt-1 flex items-center gap-2">
              <IconDemo className="w-7 h-7 text-[#5f8914]" />
              Demo Console
            </h1>
            <p className="text-xs font-medium text-slate-600 max-w-xl mt-1 leading-relaxed">
              The ten-beat arc, driven live through the real API, one beat at a time. Mirrors{" "}
              <code className="font-mono text-[11px] bg-slate-100 px-1 rounded">scripts/demo.ps1</code>.
            </p>
          </div>

          <div className="flex flex-col items-end gap-2">
            <div className="flex border border-slate-300 rounded-[2px] overflow-hidden text-xs font-bold">
              <button
                id="demo-mode-live"
                onClick={() => handleModeChange("live")}
                className={`px-3 py-1.5 transition-colors ${
                  mode === "live" ? "bg-[#86BC25] text-white" : "bg-white text-slate-600 hover:bg-slate-50"
                }`}
              >
                LIVE
              </button>
              <button
                id="demo-mode-replay"
                onClick={() => handleModeChange("replay")}
                className={`px-3 py-1.5 transition-colors border-l border-slate-300 ${
                  mode === "replay" ? "bg-amber-500 text-white" : "bg-white text-slate-600 hover:bg-slate-50"
                }`}
              >
                REPLAY
              </button>
            </div>
            <button
              id="demo-reset-btn"
              onClick={handleReset}
              className="px-3 py-1.5 rounded-[2px] border border-slate-300 bg-white hover:bg-slate-50 text-[11px] font-bold text-slate-600"
            >
              RESET CONSOLE
            </button>
          </div>
        </div>

        {mode === "replay" && (
          <div className="mt-4 px-3 py-2 bg-amber-50 border border-amber-300 rounded-[2px] inline-flex items-center gap-2">
            <span className="text-[10px] font-black uppercase tracking-wider text-amber-900 bg-amber-200 px-2 py-0.5 rounded-[2px]">
              ● RECORDING — NOT LIVE
            </span>
            <span className="text-xs text-amber-900 font-medium">
              Showing numbers from a real run captured earlier. No network calls are made in this mode.
            </span>
          </div>
        )}

        {showResetInfo && (
          <div className="mt-4 px-4 py-3 bg-red-50 border border-red-300 rounded-[2px] text-xs text-red-800 font-medium leading-relaxed">
            <strong className="font-extrabold block mb-1">The console view has been reset — the database has not.</strong>
            There is no HTTP endpoint that can restore known state (the backend exposes no reset/reseed
            route). This arc mutates real data — it moves agent-01 up a rung, then claws it back — so a
            second live run without a real reset starts from the wrong place and beat 4 may not return a
            clean INCREASE.{" "}
            <strong className="font-bold">Reset the database from the terminal before running again:</strong>{" "}
            <code className="font-mono bg-red-100 px-1 rounded">.\scripts\demo.ps1</code> (which resets
            automatically before its own run) or{" "}
            <code className="font-mono bg-red-100 px-1 rounded">make db-reset</code>.
          </div>
        )}

        {/* Progress indicator */}
        <div className="flex items-center gap-1.5 mt-6">
          {BEATS.map((b, i) => {
            const s = slots[i].status;
            const isCurrent = i === stepIndex;
            return (
              <button
                key={i}
                id={`demo-beat-dot-${i + 1}`}
                onClick={() => setStepIndex(i)}
                title={b.label}
                className={`w-7 h-7 rounded-full text-[10px] font-black flex items-center justify-center border-2 transition-all ${
                  isCurrent
                    ? "border-[#86BC25] bg-[#86BC25] text-white scale-110"
                    : s === "done"
                    ? "border-[#86BC25] bg-white text-[#5f8914]"
                    : s === "error"
                    ? "border-red-400 bg-white text-red-600"
                    : "border-slate-300 bg-white text-slate-400"
                }`}
              >
                {i + 1}
              </button>
            );
          })}
          <span className="text-[11px] font-bold text-slate-500 ml-2">
            Beat {stepIndex + 1} of {BEATS.length}
          </span>
        </div>
      </div>

      <div className="editorial-content space-y-6">
        <div className="editorial-panel p-6">
          <span className="eyebrow-label block mb-1">
            BEAT {stepIndex + 1} — {beat.label.toUpperCase()}
          </span>
          <p className="text-sm text-slate-700 font-medium leading-relaxed mb-4 max-w-3xl">{beat.caption}</p>

          <div className="flex items-center gap-3 mb-4">
            <button
              id="demo-run-beat-btn"
              onClick={runCurrent}
              disabled={slot.status === "running"}
              className="flex items-center gap-2 px-5 py-2.5 rounded-[2px] bg-[#86BC25] hover:bg-[#72a31d] text-white text-xs font-black transition-colors disabled:opacity-60"
            >
              {slot.status === "running" && (
                <span className="w-3.5 h-3.5 border-2 border-white/40 border-t-white rounded-full animate-spin" />
              )}
              {slot.status === "running"
                ? "RUNNING..."
                : slot.status === "done"
                ? "RUN AGAIN"
                : "RUN BEAT"}
            </button>
            {slot.status === "done" && (
              <span className="text-[11px] font-bold text-[#5f8914]">✓ Beat complete</span>
            )}
          </div>

          {slot.status === "error" && slot.error && (
            <div className="p-4 bg-red-50 border-l-4 border-red-400 rounded-[2px] mb-4">
              <span className="text-xs font-extrabold text-red-800 block mb-1">BEAT FAILED</span>
              <div className="text-xs text-red-700 font-mono">{slot.error.call}</div>
              {slot.error.status != null && (
                <div className="text-xs text-red-700 font-bold">status: {slot.error.status}</div>
              )}
              <div className="text-xs text-red-700 font-medium mt-1">{slot.error.message}</div>
            </div>
          )}

          {slot.status === "done" && slot.result && (
            <BeatResultView result={slot.result} />
          )}
        </div>

        <div className="flex items-center justify-between">
          <button
            id="demo-back-btn"
            onClick={() => setStepIndex((i) => Math.max(0, i - 1))}
            disabled={!canGoBack}
            className="px-4 py-2 rounded-[2px] border border-slate-300 bg-white text-xs font-bold text-slate-600 hover:bg-slate-50 disabled:opacity-40"
          >
            ← BACK
          </button>
          <button
            id="demo-next-btn"
            onClick={() => setStepIndex((i) => Math.min(BEATS.length - 1, i + 1))}
            disabled={!canGoNext}
            className="px-4 py-2 rounded-[2px] bg-slate-900 text-white text-xs font-bold hover:bg-slate-800 disabled:opacity-40"
          >
            NEXT →
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-beat result rendering
// ---------------------------------------------------------------------------

function BeatResultView({ result }: { result: BeatResult }) {
  switch (result.kind) {
    case "beat1":
      return <Beat1View r={result} />;
    case "beat2":
      return <Beat2View r={result} />;
    case "beat3":
      return <Beat3View r={result} />;
    case "beat4":
      return <Beat4View r={result} />;
    case "beat5":
      return <Beat5View r={result} />;
    case "beat6":
      return <Beat6View r={result} />;
    case "beat7":
      return <Beat7View r={result} />;
    case "beat8":
      return <Beat8View r={result} />;
    case "beat9":
      return <Beat9View r={result} />;
    case "beat10":
      return <Beat10View r={result} />;
    default:
      return null;
  }
}

function Beat1View({ r }: { r: Beat1Result }) {
  return (
    <div className="flex items-start gap-8">
      <div>
        <span className="eyebrow-label text-[9px] block">AGENT</span>
        <p className="text-lg font-black text-slate-900">{r.agent.name}</p>
        <p className="font-mono text-xs text-slate-500">{r.agent.id}</p>
        <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
          <div>
            <span className="eyebrow-label text-[9px] block">LIMIT</span>
            <span className="font-black text-slate-900 text-base">{fmtLimit(r.agent.current_limit)}</span>
          </div>
          <div>
            <span className="eyebrow-label text-[9px] block">STATE</span>
            <span className="font-bold text-slate-900 uppercase">{r.agent.state}</span>
          </div>
        </div>
      </div>
      <AutonomyLadder currentRung={r.agent.current_rung} compact />
    </div>
  );
}

function Beat2View({ r }: { r: Beat2Result }) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-4 text-xs">
        <div>
          <span className="eyebrow-label text-[9px] block">DECISIONS SUBMITTED</span>
          <span className="text-xl font-black text-slate-900">{r.decisionsSubmitted}</span>
        </div>
        <div>
          <span className="eyebrow-label text-[9px] block">ACCURACY</span>
          <span className="text-xl font-black text-slate-900">{pct(r.accuracy)}</span>
        </div>
        <div>
          <span className="eyebrow-label text-[9px] block">WILSON LOWER BOUND</span>
          <span className="text-xl font-black text-blue-700">{pct(r.wilsonLowerBound)}</span>
        </div>
      </div>
      <p className="text-xs text-slate-500 font-mono">run: {r.runId}</p>
      <div className="pt-3 border-t border-slate-200 text-xs text-slate-600 font-medium">
        Recorded human rulings on {r.escalationsRuled} escalations (
        <code className="font-mono text-[11px]">MIN_RULED_ESCALATIONS_FOR_AGREEMENT=5</code> in{" "}
        <code className="font-mono text-[11px]">trust/trust_engine/constants.py</code> — every escalation
        must be ruled, or the audit agent objects on the gap alone). All {r.escalationsRuled} agreed.
      </div>
    </div>
  );
}

function Beat3View({ r }: { r: Beat3Result }) {
  return (
    <div>
      <div className="grid grid-cols-2 gap-6 mb-4">
        <div className="p-4 bg-slate-50 border border-slate-200 rounded-[2px]">
          <span className="eyebrow-label text-[9px] block mb-1">ACCURACY POINT ESTIMATE</span>
          <span className="text-4xl font-black text-slate-500">{pct(r.point)}</span>
          <p className="text-[10px] text-slate-400 font-medium mt-1">n = {r.trials}</p>
        </div>
        <div className="p-4 bg-blue-50 border-2 border-blue-400 rounded-[2px]">
          <span className="eyebrow-label text-[9px] block mb-1 text-blue-800">
            WILSON LOWER BOUND — WHAT THE LADDER ACTUALLY TRUSTS
          </span>
          <span className="text-4xl font-black text-blue-700">{pct(r.wilsonLower)}</span>
        </div>
      </div>
      <p className="text-sm font-bold text-slate-800 mb-3">
        Gap: {r.gapPoints.toFixed(1)} percentage points of statistical caution baked into every decision
        this system makes.
      </p>
      <div>
        <span className="eyebrow-label text-[9px] block">TRUST SCORE</span>
        <span className="text-2xl font-black text-slate-900">{r.trustScore.toFixed(1)}</span>
        <span className="text-xs text-slate-500 font-medium ml-2">(needs ≥ 70.0 for an increase to be eligible)</span>
      </div>
    </div>
  );
}

function Beat4View({ r }: { r: Beat4Result }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4 flex-wrap">
        <span className={`state-badge direction-${r.direction.toLowerCase()}`}>{r.direction}</span>
        <span className="text-xs font-bold text-slate-700">status: {r.status}</span>
        <span
          className={`text-[10px] font-extrabold px-2 py-0.5 rounded-[2px] border ${
            r.hasDissent
              ? "text-red-700 bg-red-50 border-red-200"
              : "text-[#5f8914] bg-green-50 border-green-200"
          }`}
        >
          has_dissent: {String(r.hasDissent)}
        </span>
        {r.clamped && (
          <span className="text-[10px] font-bold text-amber-800 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-[2px]">
            CLAMPED from {r.clampedFrom != null ? fmtLimit(r.clampedFrom) : "?"}
          </span>
        )}
        <span className="text-xs font-bold text-slate-900">proposed: {fmtLimit(r.proposedLimit)}</span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {r.opinions.map((op) => (
          <OpinionCard key={op.agent_name} opinion={op} />
        ))}
      </div>
    </div>
  );
}

function Beat5View({ r }: { r: Beat5Result }) {
  return (
    <div>
      <span className="eyebrow-label text-[9px] block">STATUS</span>
      <span className="text-2xl font-black text-[#5f8914]">{r.status}</span>
    </div>
  );
}

function Beat6View({ r }: { r: Beat6Result }) {
  return (
    <div className="flex items-start gap-8">
      <div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs mb-3">
          <div>
            <span className="eyebrow-label text-[9px] block">NEW LIMIT</span>
            <span className="text-2xl font-black text-[#5f8914]">{fmtLimit(r.limit)}</span>
          </div>
          <div>
            <span className="eyebrow-label text-[9px] block">RUNG</span>
            <span className="text-2xl font-black text-slate-900">{r.rung}</span>
          </div>
        </div>
        <p className="text-xs text-slate-600 font-mono">
          {r.latestVersionId} (created_by={r.createdBy})
        </p>
        <p
          className={`text-xs font-bold mt-1 ${
            r.chainedCorrectly ? "text-[#5f8914]" : "text-red-700"
          }`}
        >
          previous_version_id={r.previousVersionId ?? "null"} → matches {r.previousActualId}:{" "}
          {String(r.chainedCorrectly)}
        </p>
      </div>
      <AutonomyLadder currentRung={r.rung} compact />
    </div>
  );
}

function Beat7View({ r }: { r: Beat7Result }) {
  return (
    <div className="flex items-center gap-6">
      <div>
        <span className="eyebrow-label text-[9px] block">DECISION</span>
        <span className="font-mono text-xs text-slate-700">{r.decisionId}</span>
      </div>
      <div>
        <span className="eyebrow-label text-[9px] block">ACTION</span>
        <span className="font-black text-red-700">{r.action}</span>
      </div>
      <div>
        <span className="eyebrow-label text-[9px] block">GROUND TRUTH</span>
        <span className="font-black text-slate-900">{r.groundTruth}</span>
      </div>
    </div>
  );
}

function Beat8View({ r }: { r: Beat8Result }) {
  return (
    <div className="grid grid-cols-3 gap-4">
      <div>
        <span className="eyebrow-label text-[9px] block">DRIFT SEVERITY</span>
        <span className="text-2xl font-black text-red-700">{r.severity}</span>
      </div>
      <div>
        <span className="eyebrow-label text-[9px] block">CRITICAL ERRORS IN WINDOW</span>
        <span className="text-2xl font-black text-slate-900">{r.criticalErrorsInWindow}</span>
      </div>
      <div>
        <span className="eyebrow-label text-[9px] block">RECENT vs BASELINE ACCURACY</span>
        <span className="text-lg font-black text-slate-900">
          {pct(r.recentAccuracy)} <span className="text-slate-400 font-normal text-sm">vs</span> {pct(r.baselineAccuracy)}
        </span>
      </div>
    </div>
  );
}

function Beat9View({ r }: { r: Beat9Result }) {
  return (
    <div>
      <div className="grid grid-cols-2 gap-6 mb-4">
        <div className="p-4 bg-slate-50 border border-slate-200 rounded-[2px]">
          <span className="eyebrow-label text-[9px] block mb-1">LIMIT BEFORE</span>
          <span className="text-4xl font-black text-slate-700">{fmtLimit(r.limitBefore)}</span>
          <p className="text-[10px] text-slate-400 font-medium mt-1">rung {r.rungBefore}</p>
        </div>
        <div className="p-4 bg-red-50 border-2 border-red-400 rounded-[2px]">
          <span className="eyebrow-label text-[9px] block mb-1 text-red-800">LIMIT AFTER</span>
          <span className="text-4xl font-black text-red-700">{fmtLimit(r.limitAfter)}</span>
          <p className="text-[10px] text-red-500 font-medium mt-1">rung {r.rungAfter}</p>
        </div>
      </div>
      <div className="flex items-center gap-3 mb-2">
        <span className={`state-badge direction-${r.direction.toLowerCase()}`}>{r.direction}</span>
        <span className="text-xs font-bold text-slate-700">status: {r.status}</span>
      </div>
      <p className="text-sm font-black text-red-800 bg-red-50 border-l-4 border-red-400 px-3 py-2">
        Status is already {r.status} — nobody called /approve. The reduction applied itself, automatically,
        the moment the evidence supported it.
      </p>
    </div>
  );
}

function Beat10View({ r }: { r: Beat10Result }) {
  return (
    <div className="grid grid-cols-3 gap-4">
      <div>
        <span className="eyebrow-label text-[9px] block">CHAIN VALID</span>
        <span className={`text-2xl font-black ${r.chainValid ? "text-[#5f8914]" : "text-red-700"}`}>
          {String(r.chainValid)}
        </span>
      </div>
      <div>
        <span className="eyebrow-label text-[9px] block">VERIFIED SCOPE</span>
        <span className="text-2xl font-black text-slate-900">{r.chainVerifiedScope}</span>
      </div>
      <div>
        <span className="eyebrow-label text-[9px] block">TOTAL ENTRIES</span>
        <span className="text-2xl font-black text-slate-900">{r.total}</span>
      </div>
    </div>
  );
}
