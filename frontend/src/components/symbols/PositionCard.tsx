import { useState } from "react";
import { apiDelete, errorMessage } from "../../api/client";
import type { PositionSummaryOut, TransactionListOut } from "../../api/types";
import { formatCurrency, formatDateIt, formatNumber, formatPercent, signColorClass } from "../../lib/format";
import Badge, { type BadgeVariant } from "../common/Badge";
import ErrorBox from "../common/ErrorBox";
import InfoTip from "../common/InfoTip";
import Spinner from "../common/Spinner";

interface PositionCardProps {
  data: TransactionListOut | null;
  /** Valuta del titolo (gli importi delle transazioni sono in questa valuta). */
  currency: string;
  loading: boolean;
  error: string | null;
  /** Richiamata dopo un'eliminazione riuscita per rinfrescare i dati. */
  onChanged: () => void;
}

interface StatProps {
  label: string;
  value: string;
  valueClassName?: string;
  info?: string;
}

function Stat({ label, value, valueClassName = "text-slate-100", info }: StatProps) {
  return (
    <div>
      <p className="flex items-center gap-1 text-[0.65rem] uppercase tracking-wider text-slate-500">
        {label}
        {info ? <InfoTip text={info} ariaLabel={`Cosa significa: ${label}`} /> : null}
      </p>
      <p className={`text-sm font-semibold tabular-nums ${valueClassName}`}>{value}</p>
    </div>
  );
}

const STATUS_META: Record<PositionSummaryOut["status"], { label: string; variant: BadgeVariant }> = {
  OPEN: { label: "Posizione aperta", variant: "accent" },
  CLOSED: { label: "Posizione chiusa", variant: "neutral" },
  UNKNOWN: { label: "Stima incompleta", variant: "outline" },
};

export default function PositionCard({ data, currency, loading, error, onChanged }: PositionCardProps) {
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function handleDelete(id: number) {
    if (deletingId !== null) return;
    setDeletingId(id);
    setDeleteError(null);
    try {
      await apiDelete(`/transactions/${id}`);
      onChanged();
    } catch (err) {
      setDeleteError(errorMessage(err));
    } finally {
      setDeletingId(null);
    }
  }

  if (loading && !data) {
    return (
      <div className="rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] p-5 shadow-card">
        <div className="flex justify-center py-6">
          <Spinner label="Caricamento della posizione…" />
        </div>
      </div>
    );
  }

  if (error) {
    return <ErrorBox message={error} />;
  }

  const items = data?.items ?? [];
  const position = data?.position ?? null;

  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-8 text-center text-sm text-slate-400">
        Nessuna operazione registrata. Usa Compra/Vendi per simulare una posizione: verrà usata dalle prossime analisi.
      </div>
    );
  }

  const posCurrency = position?.currency ?? currency;
  const statusMeta = position ? STATUS_META[position.status] : null;

  return (
    <div className="rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] p-5 shadow-card">
      {position ? (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              {statusMeta ? <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge> : null}
              <span className="text-xs text-slate-500">
                {position.n_transactions} {position.n_transactions === 1 ? "operazione" : "operazioni"}
              </span>
            </div>
            <span className="text-[0.65rem] uppercase tracking-wider text-slate-500">Importi in {posCurrency}</span>
          </div>

          <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 border-t border-[var(--tm-border)] pt-4 sm:grid-cols-3">
            <Stat label="Investito totale" value={formatCurrency(position.invested_total, posCurrency)} />
            <Stat label="Incassato netto" value={formatCurrency(position.proceeds_net, posCurrency)} />
            <Stat
              label="Cashflow realizzato"
              value={formatCurrency(position.realized_cashflow, posCurrency)}
              valueClassName={signColorClass(position.realized_cashflow)}
            />
            <Stat
              label="Prezzo medio di carico (est.)"
              value={position.avg_cost_est !== null ? formatCurrency(position.avg_cost_est, posCurrency) : "—"}
            />
            <Stat
              label="Azioni aperte (est.)"
              value={position.est_shares_open !== null ? formatNumber(position.est_shares_open, 4) : "—"}
            />
            <Stat
              label="Valore attuale (est.)"
              value={position.current_value_est !== null ? formatCurrency(position.current_value_est, posCurrency) : "—"}
            />
            <Stat
              label="P&L totale (est.)"
              value={
                position.total_pnl_est !== null
                  ? `${formatCurrency(position.total_pnl_est, posCurrency)}${
                      position.total_pnl_pct_est !== null
                        ? ` (${formatPercent(position.total_pnl_pct_est)})`
                        : ""
                    }`
                  : "—"
              }
              valueClassName={signColorClass(position.total_pnl_est)}
            />
          </div>

          {!position.estimates_complete ? (
            <p className="mt-4 rounded-md border border-accent/30 bg-accent-bg px-3 py-2 text-xs text-accent-light">
              Prezzo di chiusura non disponibile per alcune date: il P&L a mercato non è calcolabile e non viene stimato.
              Il cashflow realizzato resta valido.
            </p>
          ) : null}
        </>
      ) : null}

      {deleteError ? <ErrorBox message={deleteError} className="mt-4" /> : null}

      <div className="mt-4 border-t border-[var(--tm-border)] pt-4">
        <div className="tm-scroll-x -mx-1 px-1">
          <table className="w-full min-w-[36rem] text-left text-sm">
            <thead>
              <tr className="text-[0.65rem] uppercase tracking-wider text-slate-500">
                <th className="pb-2 pr-3 font-medium">Data</th>
                <th className="pb-2 pr-3 font-medium">Tipo</th>
                <th className="pb-2 pr-3 text-right font-medium">Importo</th>
                <th className="pb-2 pr-3 text-right font-medium">Comm.</th>
                <th className="pb-2 pr-3 text-right font-medium">Azioni (est.)</th>
                <th className="pb-2 pr-3 font-medium">Nota</th>
                <th className="pb-2 font-medium" aria-label="Azioni" />
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--tm-border)]">
              {items.map((tx) => (
                <tr key={tx.id} className="text-slate-300">
                  <td className="py-2 pr-3 whitespace-nowrap tabular-nums">{formatDateIt(tx.executed_at)}</td>
                  <td className="py-2 pr-3">
                    <Badge variant={tx.side === "BUY" ? "gain" : "loss"}>
                      {tx.side === "BUY" ? "Compra" : "Vendi"}
                    </Badge>
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">{formatCurrency(tx.amount, tx.currency)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {formatPercent(tx.fee_pct, { withSign: false })}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {tx.quantity_est !== null ? formatNumber(tx.quantity_est, 4) : "—"}
                  </td>
                  <td className="max-w-[12rem] truncate py-2 pr-3 text-slate-400" title={tx.note ?? undefined}>
                    {tx.note ?? "—"}
                  </td>
                  <td className="py-2 text-right">
                    <button
                      type="button"
                      onClick={() => handleDelete(tx.id)}
                      disabled={deletingId === tx.id}
                      title="Elimina operazione"
                      aria-label="Elimina operazione"
                      className="rounded-md px-2 py-1 text-slate-500 transition hover:bg-loss/10 hover:text-loss-light disabled:opacity-50"
                    >
                      {deletingId === tx.id ? <Spinner size="sm" /> : <span aria-hidden="true">🗑</span>}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
