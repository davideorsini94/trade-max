import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet, apiPost, errorMessage, isConflict } from "../api/client";
import type { SymbolOut, UniverseItemOut, UniversePageOut } from "../api/types";
import Badge from "../components/common/Badge";
import ErrorBox from "../components/common/ErrorBox";
import InfoTip from "../components/common/InfoTip";
import Spinner from "../components/common/Spinner";
import FavoriteStar from "../components/symbols/FavoriteStar";
import { formatCurrency, formatMarketCap, formatPercent, formatRelativeIt, signColorClass } from "../lib/format";
import { gloss } from "../lib/glossary";

type UniverseSort = "composite" | "fame" | "trend" | "value" | "reliability";
type SortOrder = "asc" | "desc";

const SORT_OPTIONS: ReadonlyArray<{ value: UniverseSort; label: string }> = [
  { value: "composite", label: "Consigliati" },
  { value: "fame", label: "Fama" },
  { value: "trend", label: "Andamento 30g" },
  { value: "value", label: "Valore" },
  { value: "reliability", label: "Affidabilità" },
];

const PAGE_SIZES: readonly number[] = [25, 50];
const SEARCH_DEBOUNCE_MS = 300;
const POLL_INTERVAL_MS = 10000;

function PctCell({ value }: { value: number | null }) {
  if (value === null) return <span className="text-slate-500">—</span>;
  return <span className={`font-medium tabular-nums ${signColorClass(value)}`}>{formatPercent(value)}</span>;
}

function FameStars({ rank }: { rank: number }) {
  const filled = Math.max(0, Math.min(5, Math.round(rank)));
  return (
    <span
      className="inline-flex items-center gap-0.5 text-sm leading-none"
      title={`Fama ${filled} su 5`}
      aria-label={`Fama ${filled} su 5`}
    >
      {[1, 2, 3, 4, 5].map((n) => (
        <span key={n} aria-hidden="true" className={n <= filled ? "text-accent" : "text-slate-700"}>
          ★
        </span>
      ))}
    </span>
  );
}

function ReliabilityBar({ value }: { value: number | null }) {
  if (value === null) return <span className="text-slate-500">—</span>;
  const pct = Math.max(0, Math.min(100, value));
  const color = pct >= 66 ? "#34d399" : pct >= 40 ? "#f5b942" : "#fb7185";
  return (
    <div className="flex items-center gap-2">
      <div
        className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-800"
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Affidabilità"
      >
        <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: color }} />
      </div>
      <span className="w-7 shrink-0 text-right text-xs font-medium tabular-nums text-slate-300">{Math.round(pct)}</span>
    </div>
  );
}

export default function MarketPage() {
  const [sort, setSort] = useState<UniverseSort>("composite");
  const [order, setOrder] = useState<SortOrder>("desc");
  const [pageSize, setPageSize] = useState<number>(25);
  const [page, setPage] = useState<number>(1);
  const [query, setQuery] = useState<string>("");
  const [debouncedQ, setDebouncedQ] = useState<string>("");

  const [data, setData] = useState<UniversePageOut | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [refreshNote, setRefreshNote] = useState<string | null>(null);

  const [busyTicker, setBusyTicker] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  const mountedRef = useRef(true);
  const reqSeqRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Debounce the free-text search into the `q` param and reset to the first page.
  useEffect(() => {
    const id = window.setTimeout(() => {
      setDebouncedQ(query.trim());
      setPage(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [query]);

  const load = useCallback(
    async (opts: { silent?: boolean } = {}) => {
      const seq = ++reqSeqRef.current;
      if (!opts.silent) setLoading(true);
      try {
        const res = await apiGet<UniversePageOut>("/universe", {
          sort,
          order,
          page,
          page_size: pageSize,
          q: debouncedQ || undefined,
        });
        if (!mountedRef.current || seq !== reqSeqRef.current) return;
        setData(res);
        setError(null);
      } catch (err) {
        if (!mountedRef.current || seq !== reqSeqRef.current) return;
        setError(errorMessage(err));
      } finally {
        if (mountedRef.current && seq === reqSeqRef.current) setLoading(false);
      }
    },
    [sort, order, page, pageSize, debouncedQ],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while the backend is refreshing, or while prices are not yet available.
  const shouldPoll = useMemo(() => {
    if (!data) return false;
    if (data.refreshing) return true;
    return data.items.length > 0 && data.items.every((it) => it.last_price === null);
  }, [data]);

  useEffect(() => {
    if (!shouldPoll) return;
    const id = window.setInterval(() => {
      void load({ silent: true });
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [shouldPoll, load]);

  function patchItem(ticker: string, patch: Partial<UniverseItemOut>) {
    setData((prev) =>
      prev ? { ...prev, items: prev.items.map((it) => (it.ticker === ticker ? { ...it, ...patch } : it)) } : prev,
    );
  }

  async function handleRefresh() {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshNote(null);
    try {
      const res = await apiPost<{ detail?: string }>("/universe/refresh");
      setRefreshNote(res?.detail || "Aggiornamento avviato: i nuovi dati arriveranno tra poco.");
      void load({ silent: true });
    } catch (err) {
      setRefreshNote(isConflict(err) ? errorMessage(err) || "Aggiornamento già in corso." : errorMessage(err));
    } finally {
      setRefreshing(false);
    }
  }

  async function handleMonitor(item: UniverseItemOut, alsoFavorite: boolean) {
    if (busyTicker) return;
    setBusyTicker(item.ticker);
    setRowError(null);
    try {
      const created = await apiPost<SymbolOut>("/symbols", { ticker: item.ticker });
      let isFavorite = created.is_favorite;
      if (alsoFavorite && !isFavorite) {
        const updated = await apiPost<SymbolOut>(`/symbols/${created.id}/favorite`, { is_favorite: true });
        isFavorite = updated.is_favorite;
      }
      patchItem(item.ticker, { monitored: true, symbol_id: created.id, is_favorite: isFavorite });
    } catch (err) {
      setRowError(errorMessage(err));
    } finally {
      setBusyTicker(null);
    }
  }

  function changeSort(next: UniverseSort) {
    setSort(next);
    setPage(1);
  }

  function toggleOrder() {
    setOrder((prev) => (prev === "desc" ? "asc" : "desc"));
    setPage(1);
  }

  function changePageSize(next: number) {
    setPageSize(next);
    setPage(1);
  }

  function renderActions(item: UniverseItemOut) {
    const busy = busyTicker === item.ticker;
    if (item.monitored) {
      return (
        <div className="flex items-center justify-end gap-2">
          <Badge variant="info">Monitorato</Badge>
          {item.symbol_id !== null ? (
            <FavoriteStar
              symbolId={item.symbol_id}
              isFavorite={item.is_favorite}
              onToggled={(updated) => patchItem(item.ticker, { is_favorite: updated.is_favorite })}
              onError={setRowError}
              size={18}
            />
          ) : null}
          <Link
            to={`/symbol/${item.ticker}`}
            className="whitespace-nowrap text-xs font-medium text-brand-300 hover:text-brand-200"
          >
            Apri →
          </Link>
        </div>
      );
    }
    return (
      <div className="flex items-center justify-end gap-1.5">
        <button
          type="button"
          onClick={() => handleMonitor(item, false)}
          disabled={busy}
          className="rounded-md bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-brand-500 disabled:opacity-60"
        >
          {busy ? <Spinner size="sm" /> : "Monitora"}
        </button>
        <button
          type="button"
          onClick={() => handleMonitor(item, true)}
          disabled={busy}
          title="Aggiungi ai preferiti"
          aria-label="Monitora e aggiungi ai preferiti"
          className="inline-flex items-center justify-center rounded-md p-1.5 text-slate-500 transition hover:bg-slate-800 hover:text-accent disabled:opacity-50"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
            <path
              d="M12 3.5l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17.3l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3.5z"
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </div>
    );
  }

  const total = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const items = data?.items ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold text-slate-50">Mercato</h1>
        <p className="mt-1 max-w-2xl text-sm text-slate-400">
          I titoli più conosciuti al mondo, ordinati per fama, andamento, valore e affidabilità. Aggiungili al
          monitoraggio o ai preferiti.
        </p>
      </div>

      {/* Controls */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex flex-wrap items-center gap-1 rounded-lg border border-[var(--tm-border)] bg-[var(--tm-surface)] p-1">
            {SORT_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => changeSort(option.value)}
                aria-pressed={sort === option.value}
                className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                  sort === option.value
                    ? "bg-brand-900/70 text-brand-100"
                    : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-100"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={toggleOrder}
            title={order === "desc" ? "Ordine decrescente" : "Ordine crescente"}
            aria-label={order === "desc" ? "Ordine decrescente, tocca per crescente" : "Ordine crescente, tocca per decrescente"}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs font-medium text-slate-300 transition hover:bg-slate-800"
          >
            <span aria-hidden="true">{order === "desc" ? "↓" : "↑"}</span>
            {order === "desc" ? "Decrescente" : "Crescente"}
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-0 flex-1 sm:max-w-xs">
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
            <label htmlFor="market-search" className="sr-only">
              Cerca un titolo
            </label>
            <input
              id="market-search"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Cerca per ticker, nome o settore…"
              className="w-full rounded-lg border border-slate-700 bg-slate-900 py-2 pl-9 pr-3 text-sm text-slate-100 placeholder:text-slate-500 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            />
          </div>

          <div className="flex items-center gap-2">
            <label htmlFor="market-page-size" className="text-xs text-slate-500">
              Per pagina
            </label>
            <select
              id="market-page-size"
              value={pageSize}
              onChange={(event) => changePageSize(Number(event.target.value))}
              className="rounded-lg border border-slate-700 bg-slate-900 px-2 py-2 text-sm text-slate-100 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            >
              {PAGE_SIZES.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </div>

          <div className="ml-auto flex flex-col items-end gap-1">
            <button
              type="button"
              onClick={handleRefresh}
              disabled={refreshing}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-700 px-3 py-2 text-xs font-medium text-slate-300 transition hover:bg-slate-800 disabled:opacity-60"
            >
              {refreshing ? <Spinner size="sm" /> : <span aria-hidden="true">↻</span>}
              Aggiorna dati
            </button>
            {data?.last_refresh ? (
              <span className="text-[11px] text-slate-500">Ultimo aggiornamento {formatRelativeIt(data.last_refresh)}</span>
            ) : null}
          </div>
        </div>

        {refreshNote ? <p className="text-xs text-brand-200">{refreshNote}</p> : null}
      </div>

      {shouldPoll ? (
        <div className="flex items-center gap-2 rounded-lg border border-brand-700/60 bg-brand-900/30 px-4 py-2.5 text-xs text-brand-200">
          <Spinner size="sm" />
          Dati di mercato in aggiornamento…
        </div>
      ) : null}

      {rowError ? <ErrorBox message={rowError} onRetry={() => setRowError(null)} /> : null}

      {loading && !data ? (
        <div className="flex justify-center py-16">
          <Spinner size="lg" label="Caricamento mercato…" />
        </div>
      ) : error && !data ? (
        <ErrorBox message={error} onRetry={() => void load()} />
      ) : (
        <>
          {error ? <ErrorBox message={error} onRetry={() => void load()} /> : null}

          {items.length === 0 ? (
            <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-10 text-center text-sm text-slate-400">
              {debouncedQ
                ? `Nessun titolo trovato per “${debouncedQ}”.`
                : "Nessun titolo disponibile al momento."}
            </div>
          ) : (
            <>
              {/* Desktop table */}
              <div className="tm-scroll-x hidden overflow-hidden rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] md:block">
                <table className="w-full min-w-[64rem] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-[var(--tm-border)] text-left text-xs uppercase tracking-wider text-slate-500">
                      <th className="px-4 py-3 font-medium">Titolo</th>
                      <th className="px-4 py-3 text-right font-medium">Prezzo</th>
                      <th className="px-4 py-3 text-right font-medium">Var. 1g</th>
                      <th className="px-4 py-3 text-right font-medium">
                        <span className="inline-flex items-center gap-1">
                          Var. 30g
                          <InfoTip text={gloss("trend_30d")} ariaLabel="Cos'è l'andamento a 30 giorni" />
                        </span>
                      </th>
                      <th className="px-4 py-3 text-right font-medium">
                        <span className="inline-flex items-center gap-1">
                          Valore
                          <InfoTip text={gloss("market_value")} ariaLabel="Cos'è il valore di mercato" />
                        </span>
                      </th>
                      <th className="px-4 py-3 font-medium">
                        <span className="inline-flex items-center gap-1">
                          Affidabilità
                          <InfoTip text={gloss("reliability")} ariaLabel="Cos'è l'affidabilità" />
                        </span>
                      </th>
                      <th className="px-4 py-3 font-medium">
                        <span className="inline-flex items-center gap-1">
                          Fama
                          <InfoTip text={gloss("fame")} ariaLabel="Cos'è la fama" />
                        </span>
                      </th>
                      <th className="px-4 py-3 text-right font-medium">Azioni</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--tm-border)]">
                    {items.map((item) => (
                      <tr key={item.ticker} className="transition hover:bg-slate-800/30">
                        <td className="px-4 py-3">
                          <div className="font-semibold text-slate-100">{item.ticker}</div>
                          <div className="max-w-[16rem] truncate text-xs text-slate-400">{item.name || "—"}</div>
                          <div className="text-[11px] text-slate-500">{item.sector || item.country || ""}</div>
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-100">
                          {item.last_price !== null ? formatCurrency(item.last_price, item.currency) : "—"}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <PctCell value={item.change_pct_1d} />
                        </td>
                        <td className="px-4 py-3 text-right">
                          <PctCell value={item.change_pct_30d} />
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-slate-200">
                          {formatMarketCap(item.market_cap_bn, item.currency)}
                        </td>
                        <td className="px-4 py-3">
                          <ReliabilityBar value={item.reliability_score} />
                        </td>
                        <td className="px-4 py-3">
                          <FameStars rank={item.fame_rank} />
                        </td>
                        <td className="px-4 py-3">{renderActions(item)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Mobile stacked cards */}
              <div className="space-y-3 md:hidden">
                {items.map((item) => (
                  <div
                    key={item.ticker}
                    className="rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] p-4 shadow-card"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="font-semibold text-slate-100">{item.ticker}</p>
                        <p className="truncate text-xs text-slate-400">{item.name || "—"}</p>
                        <p className="text-[11px] text-slate-500">{item.sector || item.country || ""}</p>
                      </div>
                      <FameStars rank={item.fame_rank} />
                    </div>

                    <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                      <div>
                        <dt className="text-[11px] uppercase tracking-wider text-slate-500">Prezzo</dt>
                        <dd className="tabular-nums text-slate-100">
                          {item.last_price !== null ? formatCurrency(item.last_price, item.currency) : "—"}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-[11px] uppercase tracking-wider text-slate-500">Var. 1g</dt>
                        <dd>
                          <PctCell value={item.change_pct_1d} />
                        </dd>
                      </div>
                      <div>
                        <dt className="flex items-center gap-1 text-[11px] uppercase tracking-wider text-slate-500">
                          Var. 30g
                          <InfoTip text={gloss("trend_30d")} ariaLabel="Cos'è l'andamento a 30 giorni" />
                        </dt>
                        <dd>
                          <PctCell value={item.change_pct_30d} />
                        </dd>
                      </div>
                      <div>
                        <dt className="flex items-center gap-1 text-[11px] uppercase tracking-wider text-slate-500">
                          Valore
                          <InfoTip text={gloss("market_value")} ariaLabel="Cos'è il valore di mercato" />
                        </dt>
                        <dd className="tabular-nums text-slate-200">{formatMarketCap(item.market_cap_bn, item.currency)}</dd>
                      </div>
                      <div className="col-span-2">
                        <dt className="flex items-center gap-1 text-[11px] uppercase tracking-wider text-slate-500">
                          Affidabilità
                          <InfoTip text={gloss("reliability")} ariaLabel="Cos'è l'affidabilità" />
                        </dt>
                        <dd className="mt-1">
                          <ReliabilityBar value={item.reliability_score} />
                        </dd>
                      </div>
                    </dl>

                    <div className="mt-3 border-t border-[var(--tm-border)] pt-3">{renderActions(item)}</div>
                  </div>
                ))}
              </div>

              {/* Pagination footer */}
              <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
                <p className="text-xs text-slate-500">
                  Pagina {page} di {totalPages} · {total} titoli
                </p>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page <= 1 || loading}
                    className="rounded-md border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    ← Precedente
                  </button>
                  <button
                    type="button"
                    onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                    disabled={page >= totalPages || loading}
                    className="rounded-md border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    Successiva →
                  </button>
                </div>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
