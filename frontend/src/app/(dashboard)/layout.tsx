"use client";
/**
 * src/app/(dashboard)/layout.tsx
 * --------------------------------
 * Dashboard layout — wraps all dashboard pages with the
 * Sidebar navigation and main content area.
 * Extracted from the original root layout.
 */

import { Sidebar } from "@/components/ui/Sidebar";

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 overflow-y-auto bg-[#F7F8F6]">{children}</main>
    </div>
  );
}
