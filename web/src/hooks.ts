import { useCallback, useEffect, useRef, useState } from "react";

export interface Loaded<T> {
  data: T | null;
  error: Error | null;
  /** True only for the first load of this query; refreshes keep showing the old data. */
  loading: boolean;
  reload: () => void;
}

/**
 * Run an async loader whenever `key` changes, optionally refreshing every `pollMs` while the tab is
 * visible. A slow answer to an old query never overwrites a newer one.
 */
export function useLoader<T>(load: () => Promise<T>, key: string, pollMs = 0): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    setLoading(true);
    setData(null);
    setError(null);
  }, [key]);

  useEffect(() => {
    let current = true;
    loadRef
      .current()
      .then((value) => {
        if (!current) return;
        setData(value);
        setError(null);
      })
      .catch((e: unknown) => current && setError(e instanceof Error ? e : new Error(String(e))))
      .finally(() => current && setLoading(false));
    return () => {
      current = false;
    };
  }, [key, tick]);

  useEffect(() => {
    if (!pollMs) return;
    const id = setInterval(() => {
      if (document.visibilityState === "visible") setTick((t) => t + 1);
    }, pollMs);
    return () => clearInterval(id);
  }, [pollMs, key]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
