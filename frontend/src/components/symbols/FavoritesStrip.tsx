import { useNavigate } from "react-router-dom";
import type { SymbolOut, SymbolWithQuote } from "../../api/types";
import { formatCurrency, formatPercent, signColorClass } from "../../lib/format";
import { ACTION_LABELS_IT } from "../../lib/labels";
import { gloss } from "../../lib/glossary";
import Badge from "../common/Badge";
import ConfidenceBar from "../common/ConfidenceBar";
import InfoTip from "../common/InfoTip";
import FavoriteStar from "./FavoriteStar";

interface FavoritesStripProps {
  favorites: SymbolWithQuote[];
  currency: string;
  onFavoriteToggled: (updated: SymbolOut) => void;
  onError: (message: string) => void;
}

export default function FavoritesStrip({ favorites, currency, onFavoriteToggled, onError }: FavoritesStripProps) {
  const navigate = useNavigate();

  if (favorites.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-8 text-center text-sm text-slate-400">
        Nessun titolo preferito. Aggiungi un titolo qui sotto e contrassegnalo con la stella per vederlo in
        primo piano.
      </div>
    );
  }

  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400">Preferiti</h2>
        <Badge variant="accent">★ Priorità</Badge>
        <InfoTip text={gloss("favorites_priority")} ariaLabel="Cosa significa priorità ai preferiti" />
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {favorites.map((sym) => {
          const reco = sym.last_recommendation;
          return (
            <button
              key={sym.id}
              type="button"
              onClick={() => navigate(`/symbol/${sym.ticker}`)}
              className="group flex flex-col gap-3 rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] p-4 text-left shadow-card transition hover:border-brand-600/60 hover:bg-[var(--tm-surface-2)]"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate text-base font-bold text-slate-50">{sym.ticker}</span>
                    <Badge variant="accent" className="shrink-0">★</Badge>
                  </div>
                  <p className="truncate text-xs text-slate-400">{sym.name || sym.exchange || "—"}</p>
                </div>
                <FavoriteStar
                  symbolId={sym.id}
                  isFavorite={sym.is_favorite}
                  onToggled={onFavoriteToggled}
                  onError={onError}
                />
              </div>

              <div className="flex items-baseline justify-between">
                <span className="text-xl font-semibold tabular-nums text-slate-50">
                  {sym.last_price !== null ? formatCurrency(sym.last_price, sym.currency ?? currency) : "—"}
                </span>
                <span className={`text-sm font-semibold tabular-nums ${signColorClass(sym.change_pct_1d)}`}>
                  {sym.change_pct_1d !== null ? formatPercent(sym.change_pct_1d) : "—"}
                </span>
              </div>

              <div className="border-t border-[var(--tm-border)] pt-3">
                {reco ? (
                  <div className="flex items-center justify-between gap-2">
                    <Badge
                      variant={reco.action === "BUY" ? "gain" : reco.action === "SELL" ? "loss" : "accent"}
                    >
                      {ACTION_LABELS_IT[reco.action]}
                    </Badge>
                    <ConfidenceBar value={reco.confidence} className="max-w-[7rem]" />
                  </div>
                ) : (
                  <p className="text-xs text-slate-500">Nessuna raccomandazione ancora disponibile</p>
                )}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
