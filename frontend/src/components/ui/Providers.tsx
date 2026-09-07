"use client";
/**
 * src/components/ui/Providers.tsx
 * --------------------------------
 * Root providers: TanStack Query + MSW initialisation.
 * MSW is started only when NEXT_PUBLIC_MSW_ENABLED=true (dev mode).
 * Safely guards against React StrictMode double initialization.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { getQueryClient } from "@/lib/query-client";

let mswPromise: Promise<unknown> | null = null;

function initMsw() {
  if (typeof window === "undefined") return Promise.resolve();
  // Prevent starting MSW multiple times (React StrictMode HMR, etc.)
  // Use a global flag so repeated mounts across HMR boundaries are ignored.
  // eslint-disable-next-line @typescript-eslint/ban-ts-comment
  // @ts-ignore
  if ((window as any).__MSW_STARTED) return Promise.resolve();
  if (!mswPromise) {
    mswPromise = import("@/mocks/browser").then(({ worker }) =>
      worker.start({ onUnhandledRequest: "bypass" }).then(() => {
        // eslint-disable-next-line @typescript-eslint/ban-ts-comment
        // @ts-ignore
        (window as any).__MSW_STARTED = true;
      })
    );
  }
  return mswPromise;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const queryClient = getQueryClient();
  const mswEnabled = process.env.NODE_ENV === "development" && process.env.NEXT_PUBLIC_MSW_ENABLED === "true";
  const [mswReady, setMswReady] = useState(
    !mswEnabled
  );

  useEffect(() => {
    if (!mswEnabled) return;
    initMsw().then(() => setMswReady(true));
  }, [mswEnabled]);

  if (!mswReady) return null;

  return (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  );
}
