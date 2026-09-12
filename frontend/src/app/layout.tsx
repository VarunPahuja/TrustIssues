import type { Metadata } from "next";
import { Open_Sans, Inter } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/ui/Providers";

/**
 * Open Sans — primary font for the dashboard.
 * Loaded via Next.js font optimization.
 */
const openSans = Open_Sans({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700", "800"],
  variable: "--font-open-sans",
  display: "swap",
  preload: true,
});

/**
 * Inter — used for landing page headings (tight tracking, clean feel).
 * Inspired by SentientX's typography weight/scale.
 */
const inter = Inter({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700", "800", "900"],
  variable: "--font-inter",
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
    <html lang="en" className={`${openSans.variable} ${inter.variable}`}>
      <body className="bg-[#F7F8F6] text-slate-900 antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}