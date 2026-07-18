import { useState } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api/client";
import type { DashboardSummary } from "../api/types";
import { usePolling } from "../hooks/usePolling";
import Card from "../components/common/Card";
import Spinner from "../components/common/Spinner";
import ErrorBox from "../components/common/ErrorBox";
import FavoritesStrip from "../components/symbols/FavoritesStrip";
import SymbolSearch from "../components/symbols/SymbolSearch";
import SymbolTable from "../components/symbols/SymbolTable";
import InfoTip from "../components/common/InfoTip";
import { formatConfidence, formatCurrency } from "../lib/format";
import { RISK_PROFILE_LABELS_IT } from "../lib/labels";
import { gloss } from "../lib/glossary";

export default function DashboardPage() {
  const { data, error, loading, refresh } = usePolling<DashboardSummary>(
    () => apiGet<DashboardSummary>("/dashboard/summary"),
    30000,
  );
  const [actionError, setActionError] = useState<string | null>(null);

  function handleMutated() {
    setActionError(null);
    refresh();
  }

  function handleError(message: string) {
    setActionError(message);
  }

  if (loading && !data) {
    return (
      <div className="flex justify-center py-16">
        <Spinner size="lg" label="Caricamento dashboard…" />
      </div>
    );
  }

  if (error && !data) {
    return <ErrorBox message={error} onRetry={refresh} />;
  }

  if (!data) {
    return null;
  }

  const currency = data.budget.budget_currency;

  return (
    <div className="space-y-8">
      {actionError ? <ErrorBox message={actionError} onRetry={() => setActionError(null)} /> : null}
      {error ? <ErrorBox message={error} /> : null}

      <FavoritesStrip
        favorites={data.favorites}
        currency={currency}
        onFavoriteToggled={handleMutated}
        onError={handleError}
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <div>
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Aggiungi un titolo</h2>
            <SymbolSearch onAdded={handleMutated} />
          </div>
          <div>
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Altri titoli monitorati</h2>
            <SymbolTable
              symbols={data.others}
              currency={currency}
              onFavoriteToggled={handleMutated}
              onDeleted={handleMutated}
              onError={handleError}
            />
          </div>
        </div>

        <div className="space-y-4">
          <Card
            title={
              <span className="inline-flex items-center gap-1">
                Budget
                <InfoTip text={gloss("budget")} ariaLabel="Cos'è il budget totale" />
              </span>
            }
          >
            <p className="flex flex-wrap items-center gap-x-1 text-lg font-semibold text-slate-100">
              <span>Budget: {formatCurrency(data.budget.total_budget, currency)}</span>
              <span className="text-slate-500">·</span>
              <span>Riserva liquidità: {Math.round(data.budget.cash_reserve_pct)}%</span>
              <InfoTip text={gloss("cash_reserve")} ariaLabel="Cos'è la riserva di liquidità" />
            </p>
            <p className="mt-2 flex items-center gap-1 text-xs text-slate-500">
              Profilo di rischio: <span className="text-slate-300">{RISK_PROFILE_LABELS_IT[data.budget.risk_profile]}</span>
              <InfoTip text={gloss("risk_profile")} ariaLabel="Cos'è il profilo di rischio" />
            </p>
          </Card>

          <Card
            title={
              <span className="inline-flex items-center gap-1">
                Ultima valutazione settimanale
                <InfoTip text={gloss("weekly_evaluation")} ariaLabel="Cos'è la valutazione settimanale" />
              </span>
            }
          >
            <div className="space-y-2">
              {data.last_evaluation ? (
                <>
                  <p className="flex items-center gap-1 text-sm text-slate-300">
                    Accuratezza:{" "}
                    <span className="font-semibold text-slate-100">
                      {data.last_evaluation.accuracy_overall !== null
                        ? formatConfidence(data.last_evaluation.accuracy_overall)
                        : "—"}
                    </span>
                    <InfoTip text={gloss("accuracy")} ariaLabel="Cos'è l'accuratezza" />
                  </p>
                  <p className="text-xs text-slate-500">
                    {data.last_evaluation.evaluated_count} di {data.last_evaluation.total_recommendations} raccomandazioni
                    valutate
                  </p>
                </>
              ) : (
                <p className="text-sm text-slate-400">Nessuna valutazione settimanale ancora disponibile.</p>
              )}
              <Link to="/performance" className="inline-block text-sm font-medium text-brand-300 hover:text-brand-200">
                Vai alla pagina Performance →
              </Link>
            </div>
          </Card>

          {data.pending_runs > 0 ? (
            <Card title="Analisi in corso">
              <p className="text-sm text-slate-400">{data.pending_runs} analisi in corso al momento.</p>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}
