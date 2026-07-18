import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiDelete, errorMessage } from "../../api/client";
import type { SymbolOut, SymbolWithQuote } from "../../api/types";
import { formatCurrency, formatPercent, signColorClass } from "../../lib/format";
import { ACTION_LABELS_IT } from "../../lib/labels";
import { gloss } from "../../lib/glossary";
import Badge from "../common/Badge";
import ConfidenceBar from "../common/ConfidenceBar";
import InfoTip from "../common/InfoTip";
import Spinner from "../common/Spinner";
import FavoriteStar from "./FavoriteStar";

interface SymbolTableProps {
  symbols: SymbolWithQuote[];
  currency: string;
  onFavoriteToggled: (updated: SymbolOut) => void;
  onDeleted: (symbolId: number) => void;
  onError: (message: string) => void;
}

export default function SymbolTable({ symbols, currency, onFavoriteToggled, onDeleted, onError }: SymbolTableProps) {
  const navigate = useNavigate();
  const [confirmingId, setConfirmingId] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  async function handleDelete(symbolId: number) {
    setDeletingId(symbolId);
    try {
      await apiDelete(`/symbols/${symbolId}`);
      onDeleted(symbolId);
    } catch (err) {
      onError(errorMessage(err));
    } finally {
      setDeletingId(null);
      setConfirmingId(null);
    }
  }

  if (symbols.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-8 text-center text-sm text-slate-400">
        Nessun altro titolo monitorato. Usa la ricerca qui sopra per aggiungerne uno.
      </div>
    );
  }

  return (
    <div className="tm-scroll-x overflow-hidden rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)]">
      <table className="w-full min-w-[46rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-[var(--tm-border)] text-left text-xs uppercase tracking-wider text-slate-500">
            <th className="px-4 py-3 font-medium">Ticker</th>
            <th className="px-4 py-3 font-medium">Nome</th>
            <th className="px-4 py-3 font-medium text-right">Prezzo</th>
            <th className="px-4 py-3 font-medium text-right">
              <span className="inline-flex items-center gap-1">
                Var. 1g
                <InfoTip text={gloss("change_pct_1d")} ariaLabel="Cos'è la variazione giornaliera" />
              </span>
            </th>
            <th className="px-4 py-3 font-medium">Ultima raccomandazione</th>
            <th className="px-4 py-3 font-medium text-center">
              <span className="inline-flex items-center gap-1">
                Preferito
                <InfoTip text={gloss("favorites_priority")} ariaLabel="Cosa significa preferito" />
              </span>
            </th>
            <th className="px-4 py-3 font-medium text-center">Elimina</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--tm-border)]">
          {symbols.map((sym) => {
            const reco = sym.last_recommendation;
            const isConfirming = confirmingId === sym.id;
            const isDeleting = deletingId === sym.id;
            return (
              <tr key={sym.id} className="transition hover:bg-slate-800/30">
                <td className="px-4 py-3">
                  <button
                    type="button"
                    onClick={() => navigate(`/symbol/${sym.ticker}`)}
                    className="font-semibold text-slate-100 hover:text-brand-300"
                  >
                    {sym.ticker}
                  </button>
                </td>
                <td className="max-w-[14rem] truncate px-4 py-3 text-slate-400">{sym.name || sym.exchange || "—"}</td>
                <td className="px-4 py-3 text-right tabular-nums text-slate-100">
                  {sym.last_price !== null ? formatCurrency(sym.last_price, sym.currency ?? currency) : "—"}
                </td>
                <td className={`px-4 py-3 text-right tabular-nums font-medium ${signColorClass(sym.change_pct_1d)}`}>
                  {sym.change_pct_1d !== null ? formatPercent(sym.change_pct_1d) : "—"}
                </td>
                <td className="px-4 py-3">
                  {reco ? (
                    <div className="flex items-center gap-2">
                      <Badge variant={reco.action === "BUY" ? "gain" : reco.action === "SELL" ? "loss" : "accent"}>
                        {ACTION_LABELS_IT[reco.action]}
                      </Badge>
                      <ConfidenceBar value={reco.confidence} className="max-w-[6rem]" showLabel={false} />
                    </div>
                  ) : (
                    <span className="text-xs text-slate-500">Nessuna</span>
                  )}
                </td>
                <td className="px-4 py-3 text-center">
                  <FavoriteStar
                    symbolId={sym.id}
                    isFavorite={sym.is_favorite}
                    onToggled={onFavoriteToggled}
                    onError={onError}
                    className="mx-auto"
                  />
                </td>
                <td className="px-4 py-3 text-center">
                  {isConfirming ? (
                    <div className="flex items-center justify-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => handleDelete(sym.id)}
                        disabled={isDeleting}
                        className="rounded-md bg-loss/90 px-2.5 py-1 text-xs font-semibold text-white transition hover:bg-loss disabled:opacity-60"
                      >
                        {isDeleting ? <Spinner size="sm" /> : "Conferma"}
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmingId(null)}
                        disabled={isDeleting}
                        className="rounded-md border border-slate-600 px-2.5 py-1 text-xs font-medium text-slate-300 transition hover:bg-slate-800"
                      >
                        Annulla
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setConfirmingId(sym.id)}
                      title="Elimina simbolo"
                      aria-label="Elimina simbolo"
                      className="mx-auto flex items-center justify-center rounded-md p-1.5 text-slate-500 transition hover:bg-loss-bg hover:text-loss-light"
                    >
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                        <path d="M4 7h16" strokeLinecap="round" />
                        <path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
