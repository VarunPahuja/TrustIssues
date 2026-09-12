import type { Metadata } from "next";
import { Open_Sans } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/ui/Providers";
import { Sidebar } from "@/components/ui/Sidebar";
import { AssistantPanel } from "@/components/domain/AssistantPanel";

/**
 * Open Sans — loaded via Next.js font optimization.
 * Weights: 300 (light), 400 (regular), 500 (medium), 600 (semibold), 700 (bold), 800 (extrabold).
 * The font is injected as CSS variable --font-open-sans and applied as the
 * global font-family via globals.css @theme override.
 */
const openSans = Open_Sans({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700", "800"],
  variable: "--font-open-sans",
  display: "swap",
  preload: true,
});

export const metadata: Metadata = {
  title: "Earned Autonomy Engine | Deloitte AI Governance",
  description:
    "Deloitte Enterprise AI Agent Governance Platform — Earned financial autonomy with statistical evidence and automatic clawbacks.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={openSans.variable}>
      <body className="bg-[#F7F8F6] text-slate-900 antialiased">
        <Providers>
          <div className="flex min-h-screen">
            <Sidebar />
            <main className="flex-1 overflow-y-auto bg-[#F7F8F6]">{children}</main>
          </div>
          <AssistantPanel />
        </Providers>
      </body>
    </html>
  );
}
