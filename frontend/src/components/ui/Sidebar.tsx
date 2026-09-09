"use client";
/**
 * src/components/ui/Sidebar.tsx
 * --------------------------------
 * Deloitte White Enterprise Application Shell Sidebar.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { recommendationsApi } from "@/lib/api-client";
import {
  IconAgents,
  IconApprovals,
  IconAudit,
  IconSimulation,
  IconDemo,
} from "./Icons";

const NAV = [
  { href: "/agents",     label: "Agents",      Icon: IconAgents,     badgeKey: null },
  { href: "/approvals",  label: "Approvals",   Icon: IconApprovals,  badgeKey: "approvals" },
  { href: "/audit",      label: "Audit Trail", Icon: IconAudit,      badgeKey: null },
  { href: "/simulation", label: "Simulation",  Icon: IconSimulation, badgeKey: null },
  { href: "/demo",       label: "Demo Console", Icon: IconDemo,      badgeKey: null },
];

export function Sidebar() {
  const pathname = usePathname();

  const { data: pendingRecommendations } = useQuery({
    queryKey: ["recommendations", "PENDING"],
    queryFn: () => recommendationsApi.list("PENDING"),
    refetchInterval: 10_000,
  });

  const pendingApprovalsCount = pendingRecommendations?.items?.length ?? 0;

  return (
    <aside className="w-[240px] min-h-screen bg-white border-r border-[#E2E8F0] flex flex-col justify-between flex-shrink-0 font-sans">
      {/* Top Header & Deloitte Branding */}
      <div>
        <div className="px-5 py-6 border-b border-[#E2E8F0]">
          {/* Deloitte logo brand mark */}
          <div className="flex items-baseline gap-1 mb-1.5">
            <span className="text-xl font-black tracking-tight text-black">Deloitte</span>
            <span className="w-2 h-2 rounded-full bg-[#86BC25] inline-block" />
          </div>
          {/* Product Name in One Line */}
          <p className="text-xs font-black text-slate-900 tracking-tight whitespace-nowrap">
            Earned Autonomy Engine
          </p>
          <p className="text-[9px] font-extrabold text-slate-400 tracking-wider uppercase mt-1.5">
            AI GOVERNANCE PLATFORM
          </p>
        </div>

        {/* Sidebar Nav Links */}
        <nav className="px-3 py-6 space-y-1">
          {NAV.map(({ href, label, Icon, badgeKey }) => {
            const isActive = pathname === href || (href !== "/agents" && pathname.startsWith(href));
            return (
              <Link
                key={href}
                href={href}
                className={`flex items-center gap-3 px-3.5 py-2.5 rounded-[2px] text-xs font-bold transition-all ${
                  isActive
                    ? "bg-[#86BC25]/10 text-[#5f8914] border-l-4 border-[#86BC25]"
                    : "text-slate-600 hover:text-slate-900 hover:bg-slate-50"
                }`}
              >
                <Icon className={`w-4 h-4 ${isActive ? "text-[#5f8914]" : "text-slate-400"}`} />
                <span className="flex-1">{label}</span>
                {badgeKey === "approvals" && pendingApprovalsCount > 0 && (
                  <span className="px-1.5 py-0.5 text-[10px] font-black rounded bg-amber-100 text-amber-900">
                    {pendingApprovalsCount}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>
      </div>

      {/* Understated Environment Indicator */}
      
    </aside>
  );
}
