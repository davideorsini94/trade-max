import { useEffect, useRef, useState } from "react";
import { apiGet, apiPost, errorMessage } from "../../api/client";
import type { SymbolOut, SymbolSearchResult } from "../../api/types";
import Spinner from "../common/Spinner";
import FavoriteStar from "./FavoriteStar";

interface SymbolSearchProps {
  /** Notifies the parent (DashboardPage) so lists can be refreshed immediately. */
  onAdded?: (symbol: SymbolOut) => void;
  className?: string;
}

interface ResultState extends SymbolSearchResult {
  added?: SymbolOut;
}

const DEBOUNCE_MS = 300;
const MIN_QUERY_LENGTH = 2;

export default function SymbolSearch({ onAdded, className = "" }: SymbolSearchProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ResultState[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [addingTicker, setAddingTicker] = useState<string | null>(null);

  const debounceRef = useRef<number | null>(null);
  const requestSeqRef = useRef(0);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    function handleOutsideClick(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleOutsideClick);
    return () => document.removeEventListener("mousedown", handleOutsideClick);
  }, []);

  useEffect(() => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    const trimmed = query.trim();
    if (trimmed.length < MIN_QUERY_LENGTH) {
      setResults([]);
      setLoading(false);
      setError(null);
      return;
    }
    setLoading(true);
    debounceRef.current = window.setTimeout(() => {
      const seq = ++requestSeqRef.current;
      apiGet<SymbolSearchResult[]>("/symbols/search", { q: trimmed })
        .then((found) => {
          if (seq !== requestSeqRef.current) return;
          setResults(found.map((r) => ({ ...r })));
          setError(null);
          setOpen(true);
        })
        .catch((err: unknown) => {
          if (seq !== requestSeqRef.current) return;
          setError(errorMessage(err));
          setResults([]);
        })
        .finally(() => {
          if (seq === requestSeqRef.current) setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
    };
  }, [query]);

  async function handleAdd(result: ResultState) {
    setAddingTicker(result.ticker);
    setError(null);
    try {
      const created = await apiPost<SymbolOut>("/symbols", { ticker: result.ticker });
      setResults((prev) =>
        prev.map((r) => (r.ticker === result.ticker ? { ...r, added: created, already_added: true } : r)),
      );
      onAdded?.(created);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setAddingTicker(null);
    }
  }

  return (
    <div ref={containerRef} className={`relative ${className}`}>
      <div className="relative">
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-500"
          aria-hidden="true"
        >
          <circle cx="11" cy="11" r="7" />
          <path d="M21 21l-4.3-4.3" strokeLinecap="round" />
        </svg>
        <input
          type="text"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onFocus={() => results.length > 0 && setOpen(true)}
          placeholder="Cerca un titolo per ticker o nome (es. AAPL, Enel)…"
          className="w-full rounded-lg border border-slate-700 bg-slate-900 py-2.5 pl-9 pr-9 text-sm text-slate-100 placeholder:text-slate-500 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
        />
        {loading ? (
          <span className="absolute right-3 top-1/2 -translate-y-1/2">
            <Spinner size="sm" />
          </span>
        ) : null}
      </div>

      {error ? <p className="mt-2 text-xs text-loss-light">{error}</p> : null}

      {open && results.length > 0 ? (
        <ul className="absolute z-20 mt-2 max-h-80 w-full min-w-[22rem] divide-y divide-slate-800 overflow-y-auto rounded-lg border border-slate-700 bg-slate-900 shadow-xl">
          {results.map((r) => (
            <li key={r.ticker} className="flex items-center justify-between gap-3 px-4 py-2.5">
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-slate-100">
                  {r.ticker}
                  {r.exchange ? <span className="ml-1.5 text-xs font-normal text-slate-500">{r.exchange}</span> : null}
                </p>
                <p className="truncate text-xs text-slate-400">{r.name || "—"}</p>
              </div>
              <div className="shrink-0">
                {r.added ? (
                  <FavoriteStar
                    symbolId={r.added.id}
                    isFavorite={r.added.is_favorite}
                    onToggled={(updated) =>
                      setResults((prev) => prev.map((x) => (x.ticker === r.ticker ? { ...x, added: updated } : x)))
                    }
                  />
                ) : r.already_added ? (
                  <span className="text-xs font-medium text-slate-500">Già aggiunto</span>
                ) : (
                  <button
                    type="button"
                    onClick={() => handleAdd(r)}
                    disabled={addingTicker === r.ticker}
                    className="rounded-md bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-brand-500 disabled:opacity-60"
                  >
                    {addingTicker === r.ticker ? <Spinner size="sm" /> : "Aggiungi"}
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      ) : null}

      {open && !loading && query.trim().length >= MIN_QUERY_LENGTH && results.length === 0 && !error ? (
        <div className="absolute z-20 mt-2 w-full rounded-lg border border-slate-700 bg-slate-900 px-4 py-3 text-sm text-slate-400 shadow-xl">
          Nessun titolo trovato per &ldquo;{query.trim()}&rdquo;.
        </div>
      ) : null}
    </div>
  );
}
