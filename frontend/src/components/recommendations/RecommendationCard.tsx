import type { RecommendationOut } from "../../api/types";
import { formatCurrency, formatDateTimeIt, formatNumber, formatPercent, signColorClass } from "../../lib/format";
import { ACTION_LABELS_IT, SIZING_LABELS_IT, VERDICT_LABELS_IT } from "../../lib/labels";
import { ACTION_GLOSS_KEY, SIZING_GLOSS_KEY, gloss } from "../../lib/glossary";
import Badge, { type BadgeVariant } from "../common/Badge";
import ConfidenceBar from "../common/ConfidenceBar";
import InfoTip from "../common/InfoTip";
import PolicyChecksList from "./PolicyChecksList";

interface RecommendationCardProps {
  recommendation: RecommendationOut;
  /** Valuta del titolo: prezzi di ingresso, stop loss e take profit. */
  currency?: string;
  /** Valuta del budget: importi allocati e profitto stimato in valore assoluto. */
  budgetCurrency?: string;
  className?: string;
}

function actionBadgeVariant(action: RecommendationOut["action"]): BadgeVariant {
  if (action === "BUY") return "gain";
  if (action === "SELL") return "loss";
  return "accent";
}

function verdictBadgeVariant(verdict: RecommendationOut["validator_verdict"]): BadgeVariant {
  if (verdict === "APPROVE") return "gain";
  if (verdict === "VETO") return "loss";
  return "accent";
}

interface StatProps {
  label: string;
  value: string;
  valueClassName?: string;
  /** Plain-Italian explanation shown via an info tooltip next to the label. */
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

export default function RecommendationCard({
  recommendation: reco,
  currency = "EUR",
  budgetCurrency = "EUR",
  className = "",
}: RecommendationCardProps) {
  return (
    <div className={`rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] p-5 shadow-card ${className}`}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-1.5">
            <Badge variant={actionBadgeVariant(reco.action)} className="text-sm">
              {ACTION_LABELS_IT[reco.action]}
            </Badge>
            <InfoTip text={gloss(ACTION_GLOSS_KEY[reco.action])} ariaLabel="Cosa significa questa azione" />
            <span className="ml-1 text-xs text-slate-400">{SIZING_LABELS_IT[reco.sizing_strategy]}</span>
            <InfoTip text={gloss(SIZING_GLOSS_KEY[reco.sizing_strategy])} ariaLabel="Cos'è questa strategia" />
          </div>
          {reco.policy_overridden && reco.original_action ? (
            <p className="mt-1.5 max-w-md text-xs text-slate-500">
              Azione originale del sintetizzatore: {ACTION_LABELS_IT[reco.original_action]}
              {reco.original_sizing ? ` · ${SIZING_LABELS_IT[reco.original_sizing]}` : ""} — rivista dal motore di policy.
            </p>
          ) : null}
          <p className="mt-1.5 text-xs text-slate-500">{formatDateTimeIt(reco.created_at)}</p>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[0.65rem] uppercase tracking-wider text-slate-500">Confidenza</span>
          <InfoTip text={gloss("confidence")} ariaLabel="Cos'è la confidenza" />
          <ConfidenceBar value={reco.confidence} className="w-28" />
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 border-t border-[var(--tm-border)] pt-4 sm:grid-cols-3">
        <Stat
          label="Importo suggerito"
          value={formatCurrency(reco.allocation_amount, budgetCurrency)}
          info={gloss("allocation_amount")}
        />
        <Stat
          label="% del budget"
          value={formatPercent(reco.allocation_pct, { withSign: false })}
          info={gloss("allocation_pct")}
        />
        <Stat label="Tranche DCA" value={String(reco.dca_tranches)} info={gloss("dca_tranches")} />
        <Stat
          label="Prezzo di ingresso"
          value={reco.entry_price !== null ? formatCurrency(reco.entry_price, currency) : "—"}
          info={gloss("entry_price")}
        />
        <Stat
          label="Stop loss"
          value={reco.stop_loss_price !== null ? formatCurrency(reco.stop_loss_price, currency) : "—"}
          info={gloss("stop_loss")}
        />
        <Stat
          label="Take profit"
          value={reco.take_profit_price !== null ? formatCurrency(reco.take_profit_price, currency) : "—"}
          info={gloss("take_profit")}
        />
        <Stat label="Orizzonte" value={`${reco.horizon_days} giorni`} info={gloss("horizon_days")} />
        <Stat
          label="Profitto stimato"
          value={reco.estimated_profit_pct !== null ? formatPercent(reco.estimated_profit_pct) : "—"}
          valueClassName={signColorClass(reco.estimated_profit_pct)}
          info={gloss("estimated_profit")}
        />
        <Stat
          label="Profitto stimato (importo)"
          value={reco.estimated_profit_amount !== null ? formatCurrency(reco.estimated_profit_amount, budgetCurrency) : "—"}
          valueClassName={signColorClass(reco.estimated_profit_amount)}
          info={gloss("estimated_profit")}
        />
      </div>

      {reco.rationale_it ? (
        <p className="mt-4 border-t border-[var(--tm-border)] pt-4 text-sm leading-relaxed text-slate-300">
          {reco.rationale_it}
        </p>
      ) : null}

      {reco.advice_new_investor_it || reco.advice_holder_it ? (
        <div className="mt-4 grid grid-cols-1 gap-3 border-t border-[var(--tm-border)] pt-4 sm:grid-cols-2">
          {reco.advice_new_investor_it ? (
            <div className="rounded-lg border border-[var(--tm-border)] bg-[var(--tm-surface-2)] p-3">
              <p className="mb-1.5 text-[0.65rem] font-semibold uppercase tracking-wider text-slate-500">
                Se stai valutando di entrare
              </p>
              <p className="text-sm leading-relaxed text-slate-300">{reco.advice_new_investor_it}</p>
            </div>
          ) : null}
          {reco.advice_holder_it ? (
            <div className="rounded-lg border border-[var(--tm-border)] bg-[var(--tm-surface-2)] p-3">
              <p className="mb-1.5 text-[0.65rem] font-semibold uppercase tracking-wider text-slate-500">
                Se possiedi già il titolo
              </p>
              <p className="text-sm leading-relaxed text-slate-300">{reco.advice_holder_it}</p>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="mt-4 border-t border-[var(--tm-border)] pt-4">
        <div className="flex items-center gap-2">
          <Badge variant={verdictBadgeVariant(reco.validator_verdict)}>{VERDICT_LABELS_IT[reco.validator_verdict]}</Badge>
          <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">Validatore rischio</span>
          <InfoTip text={gloss("validator")} ariaLabel="Cos'è il validatore rischio" />
        </div>
        {reco.validator_notes_it ? <p className="mt-2 text-sm text-slate-400">{reco.validator_notes_it}</p> : null}
      </div>

      <div className="mt-4 border-t border-[var(--tm-border)] pt-4">
        <h4 className="mb-2 flex items-center gap-1 text-xs font-semibold uppercase tracking-wider text-slate-500">
          Controlli di policy
          <InfoTip text={gloss("policy_engine")} ariaLabel="Cos'è il motore di policy" />
        </h4>
        <PolicyChecksList checks={reco.policy_checks} />
      </div>

      {reco.evaluated ? (
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-[var(--tm-border)] pt-4 text-xs">
          <span className="text-slate-500">Esito a 7 giorni:</span>
          {reco.realized_return_7d !== null ? (
            <span className="inline-flex items-center gap-1">
              <Badge variant={reco.realized_return_7d >= 0 ? "gain" : "loss"}>Reso: {formatPercent(reco.realized_return_7d)}</Badge>
              <InfoTip text={gloss("realized_return_7d")} ariaLabel="Cos'è il reso realizzato" />
            </span>
          ) : null}
          {reco.outcome_score !== null ? (
            <span className="inline-flex items-center gap-1">
              <Badge variant={reco.outcome_score > 0.2 ? "gain" : "loss"}>Punteggio: {formatNumber(reco.outcome_score, 2)}</Badge>
              <InfoTip text={gloss("outcome_score")} ariaLabel="Cos'è il punteggio esito" />
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
