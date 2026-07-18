import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../api/client";

export interface UsePollingOptions {
  /** When false, polling is paused and no cleanup-less interval keeps running. Default true. */
  enabled?: boolean;
  /** When true (default), fetches immediately on mount / when re-enabled instead of waiting one interval. */
  immediate?: boolean;
}

export interface UsePollingResult<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Triggers an immediate out-of-band fetch (e.g. after a mutation). */
  refresh: () => void;
}

/**
 * Generic polling hook — the single mechanism behind all live updates in
 * TradeMax (dashboard 30s, symbol detail 60s, in-progress analysis run 3s).
 * See BLUEPRINT.md section on "Live updates: polling, non WebSocket".
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  options: UsePollingOptions = {},
): UsePollingResult<T> {
  const { enabled = true, immediate = true } = options;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(immediate);

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const mountedRef = useRef(true);
  const requestSeqRef = useRef(0);

  const tick = useCallback(async () => {
    const seq = ++requestSeqRef.current;
    try {
      const result = await fetcherRef.current();
      if (mountedRef.current && seq === requestSeqRef.current) {
        setData(result);
        setError(null);
      }
    } catch (err) {
      if (mountedRef.current && seq === requestSeqRef.current) {
        setError(errorMessage(err));
      }
    } finally {
      if (mountedRef.current && seq === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      return;
    }
    if (immediate) {
      setLoading(true);
      void tick();
    }
    const id = window.setInterval(() => {
      void tick();
    }, intervalMs);
    return () => {
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, intervalMs, tick]);

  const refresh = useCallback(() => {
    setLoading(true);
    void tick();
  }, [tick]);

  return { data, error, loading, refresh };
}
