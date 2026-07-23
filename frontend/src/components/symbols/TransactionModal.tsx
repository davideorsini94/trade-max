import { useEffect, useState } from "react";
import { apiPost, errorMessage } from "../../api/client";
import type { TransactionCreate, TransactionOut, TransactionSide } from "../../api/types";
import Modal from "../common/Modal";
import Spinner from "../common/Spinner";

interface TransactionModalProps {
  symbolId: number;
  ticker: string;
  /** Valuta del titolo: gli importi sono espressi in questa valuta (nessun cambio). */
  currency: string;
  side: TransactionSide;
  open: boolean;
  onClose: () => void;
  /** Chiamata dopo un inserimento riuscito (per aggiornare la posizione). */
  onSaved: () => void;
}

/** Today's date in local YYYY-MM-DD form, used as the max of the date picker. */
function todayIso(): string {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

export default function TransactionModal({
  symbolId,
  ticker,
  currency,
  side,
  open,
  onClose,
  onSaved,
}: TransactionModalProps) {
  const isBuy = side === "BUY";
  const [amount, setAmount] = useState("");
  const [feePct, setFeePct] = useState("0");
  const [executedAt, setExecutedAt] = useState(todayIso());
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset the form each time the modal is (re)opened or the side changes, so a
  // previous BUY entry never leaks into a following SELL and vice versa.
  useEffect(() => {
    if (!open) return;
    setAmount("");
    setFeePct("0");
    setExecutedAt(todayIso());
    setNote("");
    setError(null);
    setSubmitting(false);
  }, [open, side]);

  const today = todayIso();

  function validate(): string | null {
    const amountNum = Number(amount);
    if (!amount.trim() || Number.isNaN(amountNum) || amountNum <= 0) {
      return "Inserisci un importo maggiore di zero.";
    }
    const feeNum = Number(feePct);
    if (feePct.trim() === "" || Number.isNaN(feeNum) || feeNum < 0 || feeNum > 100) {
      return "La commissione deve essere compresa tra 0 e 100.";
    }
    if (!executedAt) {
      return "Inserisci la data dell'operazione.";
    }
    if (executedAt > today) {
      return "La data della transazione non può essere nel futuro.";
    }
    return null;
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting) return;
    const validationError = validate();
    if (validationError) {
      setError(validationError);
      return;
    }
    setSubmitting(true);
    setError(null);
    const body: TransactionCreate = {
      side,
      amount: Number(amount),
      fee_pct: Number(feePct),
      executed_at: executedAt,
      note: note.trim() ? note.trim() : null,
    };
    try {
      await apiPost<TransactionOut>(`/symbols/${symbolId}/transactions`, body);
      onSaved();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  const amountLabel = isBuy ? `Importo investito (${currency})` : `Controvalore della vendita (${currency})`;
  const amountHint = isBuy
    ? "Totale uscito dal conto, commissione inclusa."
    : "Importo lordo della vendita; la commissione verrà sottratta dall'incasso.";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`${isBuy ? "Registra acquisto" : "Registra vendita"} — ${ticker}`}
      footer={
        <p className="text-xs leading-relaxed text-slate-500">
          Operazione fittizia: nessun ordine reale viene eseguito. Verrà usata dalla prossima analisi.
        </p>
      }
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="tx-amount" className="mb-1 block text-xs font-medium text-slate-300">
            {amountLabel}
          </label>
          <input
            id="tx-amount"
            type="number"
            inputMode="decimal"
            min="0"
            step="0.01"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            autoFocus
            required
            className="w-full rounded-md border border-[var(--tm-border)] bg-[var(--tm-surface-2)] px-3 py-2 text-sm text-slate-100 tabular-nums outline-none transition focus:border-brand-500 focus:ring-1 focus:ring-brand-500"
          />
          <p className="mt-1 text-xs text-slate-500">{amountHint}</p>
        </div>

        <div>
          <label htmlFor="tx-fee" className="mb-1 block text-xs font-medium text-slate-300">
            Commissione (%)
          </label>
          <input
            id="tx-fee"
            type="number"
            inputMode="decimal"
            min="0"
            max="100"
            step="0.01"
            value={feePct}
            onChange={(e) => setFeePct(e.target.value)}
            className="w-full rounded-md border border-[var(--tm-border)] bg-[var(--tm-surface-2)] px-3 py-2 text-sm text-slate-100 tabular-nums outline-none transition focus:border-brand-500 focus:ring-1 focus:ring-brand-500"
          />
        </div>

        <div>
          <label htmlFor="tx-date" className="mb-1 block text-xs font-medium text-slate-300">
            Data dell'operazione
          </label>
          <input
            id="tx-date"
            type="date"
            max={today}
            value={executedAt}
            onChange={(e) => setExecutedAt(e.target.value)}
            required
            className="w-full rounded-md border border-[var(--tm-border)] bg-[var(--tm-surface-2)] px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-brand-500 focus:ring-1 focus:ring-brand-500"
          />
          <p className="mt-1 text-xs text-slate-500">Sono ammesse date passate; le azioni sono stimate dalla chiusura di quella seduta.</p>
        </div>

        <div>
          <label htmlFor="tx-note" className="mb-1 block text-xs font-medium text-slate-300">
            Nota <span className="text-slate-500">(facoltativa)</span>
          </label>
          <input
            id="tx-note"
            type="text"
            maxLength={200}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            className="w-full rounded-md border border-[var(--tm-border)] bg-[var(--tm-surface-2)] px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-brand-500 focus:ring-1 focus:ring-brand-500"
          />
        </div>

        {error ? (
          <p className="rounded-md border border-loss/30 bg-loss-bg px-3 py-2 text-xs text-loss-light" role="alert">
            {error}
          </p>
        ) : null}

        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-slate-700 px-4 py-2 text-sm font-medium text-slate-300 transition hover:bg-slate-800"
          >
            Annulla
          </button>
          <button
            type="submit"
            disabled={submitting}
            className={`inline-flex items-center gap-2 rounded-md px-4 py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-60 ${
              isBuy ? "bg-gain-dark" : "bg-loss-dark"
            }`}
          >
            {submitting ? <Spinner size="sm" /> : null}
            {isBuy ? "Registra acquisto" : "Registra vendita"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
