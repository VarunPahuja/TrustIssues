"use client";
/**
 * src/app/error.tsx
 * -------------------
 * App-root error boundary. Shows a designed error page instead of a blank
 * white screen when a render crash occurs.
 *
 * Next.js automatically wraps the nearest error.tsx around the page's
 * content. This one sits at the app root, so it catches everything.
 */

import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Log the error for debugging — in production this would go to a service
    console.error("[GlobalError]", error);
  }, [error]);

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] px-6">
      <div className="max-w-md w-full text-center space-y-6">
        <div className="mx-auto w-12 h-12 rounded-full bg-red-100 flex items-center justify-center">
          <span className="text-red-700 text-xl font-black">!</span>
        </div>

        <div>
          <span className="eyebrow-label text-red-700 block mb-1">SYSTEM ERROR</span>
          <h1 className="text-2xl font-black text-slate-900 tracking-tight">
            Something went wrong
          </h1>
          <p className="text-xs text-slate-600 font-medium mt-2 leading-relaxed">
            The dashboard encountered an unexpected error. This is likely a temporary issue.
          </p>
        </div>

        {error.message && (
          <div className="bg-red-50 border border-red-200 rounded-[2px] p-4 text-left">
            <span className="text-[9px] font-bold text-red-700 uppercase block mb-1">Error Details</span>
            <pre className="text-[11px] font-mono text-red-800 whitespace-pre-wrap break-all">
              {error.message}
            </pre>
          </div>
        )}

        <button
          onClick={reset}
          className="inline-flex items-center gap-2 px-6 py-2.5 rounded-[2px] bg-[#86BC25] hover:bg-[#72a31d] text-white text-xs font-black transition-colors"
        >
          <span>TRY AGAIN</span>
          <span>↻</span>
        </button>

        <p className="text-[10px] text-slate-400 font-medium">
          If this persists, check that the backend is running and refresh the page.
        </p>
      </div>
    </div>
  );
}
