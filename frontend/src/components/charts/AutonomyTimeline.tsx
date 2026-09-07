"use client";
/**
 * src/components/charts/AutonomyTimeline.tsx
 * --------------------------------------------
 * Executive Financial & Performance Autonomy Chart.
 *
 * TWO DATA SOURCES, ONE CHART:
 *  1. PolicyVersionOut[] — limit/rung over time (stepped, left axis)
 *  2. TrustEvaluation[] — accuracy with Wilson band (right axis)
 *
 * Merged by timestamp. Policy versions define the stepped autonomy limit.
 * Trust evaluations provide the accuracy point estimate and Wilson band.
 *
 * A rung going DOWN between consecutive policy versions is a clawback —
 * marked with a red reference line.
 */

import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { PolicyVersionOut, TrustEvaluation } from "@/types/api";
import { AUTONOMY_LADDER } from "@/types/api";

interface Props {
  policyVersions: PolicyVersionOut[];
  trustHistory: TrustEvaluation[];
  height?: number;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-IN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function fmtLimit(val: number): string {
  if (val >= 1000) return `₹${(val / 1000).toFixed(0)}k`;
  return `₹${val}`;
}

interface ChartDataPoint {
  time: string;
  timeLabel: string;
  timestamp: number;
  limit: number | null;
  rung: number | null;
  accuracy: number | null;
  wilsonLower: number | null;
  wilsonUpper: number | null;
  wilsonBand: [number, number] | null;
  isClawback: boolean;
  isPromotion: boolean;
  source: "policy" | "trust" | "merged";
}

interface TimelineTooltipProps {
  active?: boolean;
  payload?: Array<{ payload?: ChartDataPoint }>;
}

function CustomTooltip({ active, payload }: TimelineTooltipProps) {
  if (!active || !payload?.length) return null;
  const d = payload[0]?.payload;
  return (
    <div className="bg-white border border-slate-300 rounded p-3 text-xs shadow-md text-slate-900 font-sans">
      <p className="font-bold text-slate-900 border-b border-slate-200 pb-1 mb-2">
        {d?.timeLabel}
      </p>
      <div className="space-y-1">
        {d?.limit != null && (
          <div className="flex justify-between gap-4">
            <span className="text-slate-600 font-medium">Autonomy Limit:</span>
            <span className="font-bold text-[#5f8914]">{fmtLimit(d.limit)} (Rung {d.rung})</span>
          </div>
        )}
        {d?.accuracy != null && (
          <div className="flex justify-between gap-4">
            <span className="text-slate-600 font-medium">Accuracy:</span>
            <span className="font-bold text-slate-900">{d.accuracy}%</span>
          </div>
        )}
        {d?.wilsonLower != null && d?.wilsonUpper != null && (
          <div className="flex justify-between gap-4">
            <span className="text-slate-600 font-medium">Wilson Band:</span>
            <span className="font-bold text-blue-700">{d.wilsonLower}% – {d.wilsonUpper}%</span>
          </div>
        )}
      </div>
      {d?.isClawback && (
        <div className="mt-2 text-[11px] text-red-700 font-bold bg-red-50 p-1 border border-red-200 rounded">
          CLAWBACK — Autonomy reduced
        </div>
      )}
      {d?.isPromotion && (
        <div className="mt-2 text-[11px] text-[#5f8914] font-bold bg-green-50 p-1 border border-green-200 rounded">
          PROMOTION — Autonomy increased
        </div>
      )}
    </div>
  );
}

export function AutonomyTimeline({ policyVersions, trustHistory, height = 400 }: Props) {
  // Reverse both to chronological order (APIs return newest-first)
  const chronPolicies = [...policyVersions].reverse();
  const chronTrust = [...trustHistory].reverse();

  // Build data points from policy versions
  const policyPoints: ChartDataPoint[] = chronPolicies.map((v, i) => {
    const prevVersion = i > 0 ? chronPolicies[i - 1] : null;
    return {
      time: v.effective_from,
      timeLabel: fmtTime(v.effective_from),
      timestamp: new Date(v.effective_from).getTime(),
      limit: v.limit,
      rung: v.rung,
      accuracy: null,
      wilsonLower: null,
      wilsonUpper: null,
      wilsonBand: null,
      isClawback: prevVersion != null && v.rung < prevVersion.rung,
      isPromotion: prevVersion != null && v.rung > prevVersion.rung,
      source: "policy" as const,
    };
  });

  // Build data points from trust history
  const trustPoints: ChartDataPoint[] = chronTrust
    .filter(t => t.evaluated_at != null)
    .map(t => {
      const acc = t.accuracy;
      return {
        time: t.evaluated_at!,
        timeLabel: fmtTime(t.evaluated_at!),
        timestamp: new Date(t.evaluated_at!).getTime(),
        limit: null,
        rung: null,
        accuracy: acc?.point != null ? Math.round(acc.point * 1000) / 10 : null,
        wilsonLower: acc != null ? Math.round(acc.wilson_lower * 1000) / 10 : null,
        wilsonUpper: acc != null ? Math.round(acc.wilson_upper * 1000) / 10 : null,
        wilsonBand: acc != null ? [Math.round(acc.wilson_lower * 1000) / 10, Math.round(acc.wilson_upper * 1000) / 10] as [number, number] : null,
        isClawback: false,
        isPromotion: false,
        source: "trust" as const,
      };
    });

  // Merge and sort by timestamp
  const allPoints = [...policyPoints, ...trustPoints].sort((a, b) => a.timestamp - b.timestamp);

  // Forward-fill limit/rung from policy points into trust points
  const data = allPoints.reduce<ChartDataPoint[]>((points, point) => {
    const previous = points[points.length - 1];
    points.push({
      ...point,
      limit: point.limit ?? previous?.limit ?? null,
      rung: point.rung ?? previous?.rung ?? null,
    });
    return points;
  }, []);

  // Find annotation events
  const promotionEvt = data.find(e => e.isPromotion);
  const clawbackEvt = data.find(e => e.isClawback);

  const promotionLabel = promotionEvt
    ? `PROMOTION → ${fmtLimit(promotionEvt.limit!)}`
    : "PROMOTION";
  const clawbackLabel = clawbackEvt
    ? `CLAWBACK → ${fmtLimit(clawbackEvt.limit!)}`
    : "CLAWBACK";

  if (data.length === 0) {
    return (
      <div className="w-full flex items-center justify-center text-xs text-slate-400 font-medium" style={{ height }}>
        No timeline data available yet.
      </div>
    );
  }

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 15, right: 60, left: 10, bottom: 15 }}>
          <CartesianGrid strokeDasharray="2 2" stroke="#e2e8f0" vertical={false} />

          <XAxis
            dataKey="timeLabel"
            tick={{ fill: "#64748b", fontSize: 10, fontFamily: "Open Sans" }}
            tickLine={false}
            axisLine={{ stroke: "#cbd5e1" }}
            interval="preserveStartEnd"
          />

          {/* Left Y-axis: Autonomy Limit (Step Function) — five-rung ladder */}
          <YAxis
            yAxisId="limit"
            orientation="left"
            tick={{ fill: "#64748b", fontSize: 10, fontFamily: "Open Sans" }}
            tickLine={false}
            axisLine={{ stroke: "#cbd5e1" }}
            tickFormatter={fmtLimit}
            ticks={[...AUTONOMY_LADDER]}
            domain={[0, 12000]}
          />

          {/* Right Y-axis: Performance Metrics (%) */}
          <YAxis
            yAxisId="pct"
            orientation="right"
            tick={{ fill: "#64748b", fontSize: 10, fontFamily: "Open Sans" }}
            tickLine={false}
            axisLine={{ stroke: "#cbd5e1" }}
            tickFormatter={v => `${v}%`}
            ticks={[50, 60, 70, 80, 85, 90, 100]}
            domain={[50, 100]}
          />

          <Tooltip content={<CustomTooltip />} />

          <Legend
            wrapperStyle={{ color: "#334155", fontSize: 11, paddingTop: 10, fontFamily: "Open Sans" }}
          />

          {/* Governance Event vertical annotations */}
          {promotionEvt && (
            <ReferenceLine
              yAxisId="limit"
              x={promotionEvt.timeLabel}
              stroke="#86BC25"
              strokeDasharray="4 2"
              strokeWidth={1.5}
              label={{
                value: promotionLabel,
                fill: "#5f8914",
                fontSize: 9,
                position: "insideTopLeft",
                fontWeight: 700,
              }}
            />
          )}

          {clawbackEvt && (
            <ReferenceLine
              yAxisId="limit"
              x={clawbackEvt.timeLabel}
              stroke="#b91c1c"
              strokeDasharray="2 2"
              strokeWidth={2}
              label={{
                value: clawbackLabel,
                fill: "#b91c1c",
                fontSize: 9,
                position: "insideTopLeft",
                fontWeight: 700,
              }}
            />
          )}

          {/* Safety Threshold Horizontal Line at 85% */}
          <ReferenceLine
            yAxisId="pct"
            y={85}
            stroke="#475569"
            strokeDasharray="3 3"
            strokeWidth={1}
            label={{ value: "Safety Threshold (85%)", fill: "#475569", fontSize: 9, position: "right" }}
          />

          {/* Autonomy Limit Step Area */}
          <Area
            yAxisId="limit"
            type="stepAfter"
            dataKey="limit"
            name="Autonomy Limit (INR)"
            stroke="#86BC25"
            fill="#86BC25"
            fillOpacity={0.1}
            strokeWidth={2}
            connectNulls
          />

          {/* Wilson Confidence Band — the key visualization */}
          <Area
            yAxisId="pct"
            type="monotone"
            dataKey="wilsonBand"
            name="Wilson 95% Confidence Band"
            stroke="none"
            fill="#3b82f6"
            fillOpacity={0.15}
            connectNulls
          />

          {/* Wilson Lower Bound Line (bottom of band) */}
          <Line
            yAxisId="pct"
            type="monotone"
            dataKey="wilsonLower"
            name="Wilson Lower Bound"
            stroke="#93c5fd"
            strokeWidth={1}
            strokeDasharray="3 3"
            dot={false}
            legendType="none"
            connectNulls
          />

          {/* Wilson Upper Bound Line (top of band) */}
          <Line
            yAxisId="pct"
            type="monotone"
            dataKey="wilsonUpper"
            name="Wilson Upper Bound"
            stroke="#93c5fd"
            strokeWidth={1}
            strokeDasharray="3 3"
            dot={false}
            legendType="none"
            connectNulls
          />

          {/* Accuracy Point Estimate Line — on top of the band */}
          <Line
            yAxisId="pct"
            type="monotone"
            dataKey="accuracy"
            name="Accuracy"
            stroke="#0f172a"
            strokeWidth={2}
            dot={false}
            connectNulls
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
