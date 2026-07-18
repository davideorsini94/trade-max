import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../api/client";

export interface UseApiResult<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Re-runs the fetcher on demand (e.g. after the user retries or a mutation completes). */
  refetch: () => void;
}

/**
 * One-shot fetch with loading/error tracking. Re-runs whenever `deps` changes.
 * Guards against setting state after unmount or after a newer call has been
 * issued (avoids race conditions when deps change quickly).
 */
export function useApi<T>(fetcher: () => Promise<T>, deps: ReadonlyArray<unknown> = []): UseApiResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const requestSeqRef = useRef(0);
  const mountedRef = useRef(true);

  const run = useCallback(() => {
    const seq = ++requestSeqRef.current;
    setLoading(true);
    setError(null);
    fetcherRef
      .current()
      .then((result) => {
        if (mountedRef.current && seq === requestSeqRef.current) {
          setData(result);
        }
      })
      .catch((err: unknown) => {
        if (mountedRef.current && seq === requestSeqRef.current) {
          setError(errorMessage(err));
        }
      })
      .finally(() => {
        if (mountedRef.current && seq === requestSeqRef.current) {
          setLoading(false);
        }
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, refetch: run };
}
