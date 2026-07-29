import { useMemo, useState } from "react";
import { apiGet, apiPost, errorMessage, isConflict } from "../api/client";
import type {
  AgentFeedbackOut,
  EvaluationOut,
  PendingEvaluationOut,
  PerformanceSummaryOut,
  SimPortfolioOut,
} from "../api/types";
import { useApi } from "../hooks/useApi";
import Card from "../components/common/Card";
import Spinner from "../components/common/Spinner";
import ErrorBox from "../components/common/ErrorBox";
import AccuracyTrendChart from "../components/charts/AccuracyTrendChart";
import SimPortfolioCard from "../components/performance/SimPortfolioCard";
import InfoTip from "../components/common/InfoTip";
import { formatConfidence, formatDateTimeIt, formatNumber, formatPercent } from "../lib/format";
import { AGENT_LABELS_IT, AGENT_ORDER, agentLabelIt } from "../lib/labels";
import { gloss } from "../lib/glossary";

interface KpiProps {
  label: string;
  value: string;
  info?: string;
  /** Small print under the value: sample size, confidence interval, caveats. */
  sub?: string;
}

function Kpi({ label, value, info, sub }: KpiProps) {
  return (
    <div className="rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] p-4 shadow-card">
      <p className="flex items-center gap-1 text-xs uppercase tracking-wider text-slate-500">
        {label}
        {info ? <InfoTip text={info} ariaLabel={`Cosa significa: ${label}`} /> : null}
      </p>
      <p className="mt-1 text-lg font-semibold tabular-nums text-slate-100">{value}</p>
      {sub ? <p className="mt-1 text-xs leading-snug text-slate-500">{sub}</p> : null}
    </div>
  );
}

export default function PerformancePage() {
  const evaluationsQuery = useApi<EvaluationOut[]>(() => apiGet<EvaluationOut[]>("/evaluations", { limit: 12 }), []);
  // Rolling-window performance: the headline numbers come from here, NOT from the
  // latest batch (a batch can hold a single sample, where accuracy is 0% or 100%
  // by construction).
  const summaryQuery = useApi<PerformanceSummaryOut>(
    () => apiGet<PerformanceSummaryOut>("/evaluations/summary"),
    [],
  );
  const feedbackQuery = useApi<AgentFeedbackOut[]>(
    () => apiGet<AgentFeedbackOut[]>("/feedback", { active_only: true }),
    [],
  );
  const pendingQuery = useApi<PendingEvaluationOut>(() => apiGet<PendingEvaluationOut>("/evaluations/pending"), []);
  // Libro simulato del sistema: è ciò che dà alle posizioni un ciclo di vita e
  // alimenta il tetto di allocazione, quindi vive accanto alle metriche.
  const simQuery = useApi<SimPortfolioOut>(() => apiGet<SimPortfolioOut>("/sim/portfolio"), []);

  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const feedbackByAgent = useMemo(() => {
    const map = new Map<string, AgentFeedbackOut[]>();
    for (const item of feedbackQuery.data ?? []) {
      const list = map.get(item.agent_name) ?? [];
      list.push(item);
      map.set(item.agent_name, list);
    }
    for (const list of map.values()) {
      list.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    }
    return map;
  }, [feedbackQuery.data]);

  async function handleRunEvaluation() {
    if (running) return;
    setRunning(true);
    setRunError(null);
    try {
      await apiPost<EvaluationOut>("/evaluations/run");
      evaluationsQuery.refetch();
      feedbackQuery.refetch();
      pendingQuery.refetch();
      summaryQuery.refetch();
      simQuery.refetch();
    } catch (err) {
      setRunError(isConflict(err) ? "Una valutazione è già in corso." : errorMessage(err));
    } finally {
      setRunning(false);
    }
  }

  if (evaluationsQuery.loading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner size="lg" label="Caricamento performance…" />
      </div>
    );
  }

  if (evaluationsQuery.error) {
    return <ErrorBox message={evaluationsQuery.error} onRetry={evaluationsQuery.refetch} />;
  }

  const latest = evaluationsQuery.data?.[0] ?? null;
  const pending = pendingQuery.data;
  const summary = summaryQuery.data ?? null;
  const summaryWeeks = Math.round((summary?.window_days ?? 28) / 7);
  // With too few samples (or too few distinct titles) the percentage would be
  // arithmetic, not a measurement — say so instead of showing a confident 0%.
  const accuracyValue =
    summary?.status === "ok" && summary.accuracy !== null
      ? formatConfidence(summary.accuracy)
      : summary
        ? "Dati insufficienti"
        : "—";
  const accuracySub = !summary
    ? undefined
    : summary.status === "ok"
      ? `IC 95%: ${formatConfidence(summary.ci_low ?? 0)}–${formatConfidence(summary.ci_high ?? 1)} · ` +
        `${summary.n} consigli unici su ${summary.n_symbols} titoli`
      : `Finora ${summary.n} ${summary.n === 1 ? "consiglio unico" : "consigli unici"} su ` +
        `${summary.n_symbols} ${summary.n_symbols === 1 ? "titolo" : "titoli"}: ne servono almeno ` +
        `${summary.min_n} su ${summary.min_symbols} titoli diversi.`;
  // Optional on the wire (older backends omit them): treat a missing value as 0.
  const awaitingPrice = pending?.awaiting_price_count ?? 0;
  const horizonReady = pending?.horizon_ready_count ?? 0;
  const hasNoHistoryYet =
    !latest && !pending?.pending_count && !pending?.ready_count && !awaitingPrice;
  // Disable the manual run only when the status is KNOWN and both scoring passes
  // (the 7-day checkpoint and the horizon pass) have nothing they could score —
  // never while still loading or after a failed fetch, which would block a run
  // that might well be useful.
  const nothingToEvaluate =
    !pendingQuery.loading &&
    !pendingQuery.error &&
    pending != null &&
    pending.ready_count === 0 &&
    horizonReady === 0;

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold text-slate-50">Performance</h1>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-slate-400">
            Ogni settimana i consigli con almeno 7 giorni di storico vengono confrontati con
            l'andamento reale dei prezzi. Gli errori diventano lezioni che ogni agente applica
            nelle analisi successive, per rendere i consigli sempre più precisi e affidabili nel
            tempo.
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <button
            type="button"
            onClick={handleRunEvaluation}
            disabled={running || nothingToEvaluate}
            title={
              nothingToEvaluate
                ? "Nessun consiglio è pronto da valutare: servono almeno 7 giorni di storico e la chiusura di mercato del giorno di riferimento."
                : "Esegui subito la valutazione dei consigli maturi"
            }
            className="rounded-md bg-brand-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-brand-500 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {running ? <Spinner size="sm" /> : "Esegui valutazione ora"}
          </button>
          {/* Without this the button is clickable while it provably cannot score
              anything, and the run "does nothing" for no visible reason. */}
          {nothingToEvaluate && !running ? (
            <p className="max-w-xs text-right text-xs text-slate-500">
              Nessun consiglio è ancora pronto da valutare.
            </p>
          ) : null}
          {runError ? <p className="text-xs text-loss-light">{runError}</p> : null}
        </div>
      </div>

      {!pendingQuery.loading &&
      !pendingQuery.error &&
      pending &&
      (pending.pending_count > 0 || pending.ready_count > 0 || awaitingPrice > 0) ? (
        <Card title="Ciclo di apprendimento">
          <p className="text-sm leading-relaxed text-slate-300">
            {pending.ready_count > 0 ? (
              <>
                <span className="font-semibold text-slate-100">{pending.ready_count}</span>{" "}
                {pending.ready_count === 1 ? "consiglio è maturo" : "consigli sono maturi"} (7+
                giorni di storico) e{" "}
                {pending.ready_count === 1 ? "verrà incluso" : "verranno inclusi"} nella prossima
                valutazione.{" "}
              </>
            ) : null}
            {/* 7+ giorni ma senza il prezzo del giorno di riferimento: contarli tra i
                "maturi" prometterebbe una valutazione che non può ancora avvenire. */}
            {awaitingPrice > 0 ? (
              <>
                <span className="font-semibold text-slate-100">{awaitingPrice}</span>{" "}
                {awaitingPrice === 1 ? "consiglio ha" : "consigli hanno"} superato i 7 giorni ma{" "}
                {awaitingPrice === 1 ? "aspetta" : "aspettano"} la chiusura di mercato del giorno di
                riferimento (se cade in un weekend o in un festivo arriva alla riapertura):{" "}
                {awaitingPrice === 1 ? "verrà valutato" : "verranno valutati"} appena il prezzo è
                disponibile.{" "}
              </>
            ) : null}
            {pending.pending_count > 0 && pending.next_evaluable_at ? (
              <>
                Altri <span className="font-semibold text-slate-100">{pending.pending_count}</span>{" "}
                {pending.pending_count === 1 ? "consiglio sta" : "consigli stanno"} ancora
                maturando: il prossimo lotto sarà valutabile dal{" "}
                <span className="font-semibold text-slate-100">
                  {formatDateTimeIt(pending.next_evaluable_at)}
                </span>
                .
              </>
            ) : null}
          </p>
        </Card>
      ) : null}

      {hasNoHistoryYet ? (
        <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-8 text-center text-sm text-slate-400">
          Nessun consiglio ancora generato: analizza un titolo dalla sua scheda per iniziare a
          costruire lo storico su cui gli agenti impareranno.
        </div>
      ) : !latest ? (
        <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-8 text-center text-sm text-slate-400">
          Nessuna valutazione settimanale disponibile ancora: torna qui una volta maturato il
          primo lotto di consigli.
        </div>
      ) : (
        <>
          {summary?.hold_only ? (
            <div className="rounded-xl border border-[var(--tm-border)] bg-slate-900/40 px-5 py-4 text-sm leading-relaxed text-slate-300">
              Tutti i consigli valutati finora sono <span className="font-semibold text-slate-100">MANTIENI</span>:
              l'accuratezza misura soprattutto quanto il mercato è rimasto calmo, non la capacità di
              scegliere i titoli. Diventerà più informativa quando matureranno i primi consigli di
              acquisto o vendita.
            </div>
          ) : null}

          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Kpi
              label={`Accuratezza (${summaryWeeks} settimane)`}
              value={accuracyValue}
              info={gloss("accuracy")}
              sub={accuracySub}
            />
            <Kpi
              label="Punteggio medio esito"
              value={
                summary?.avg_outcome_score !== null && summary?.avg_outcome_score !== undefined
                  ? formatNumber(summary.avg_outcome_score, 2)
                  : "—"
              }
              info={gloss("outcome_score_avg")}
              sub={summary?.n ? `Su ${summary.n} consigli unici` : undefined}
            />
            <Kpi
              label="Rendimento medio realizzato"
              value={latest.avg_realized_return_pct !== null ? formatPercent(latest.avg_realized_return_pct) : "—"}
              info={gloss("realized_return_7d")}
            />
            <Kpi
              label="Valutate"
              value={`${latest.evaluated_count} / ${latest.total_recommendations}`}
              info={gloss("weekly_evaluation")}
            />
          </div>

          {latest.report_it ? (
            <Card title="Sintesi della valutazione">
              <p className="text-sm leading-relaxed text-slate-300">{latest.report_it}</p>
              <p className="mt-3 text-xs text-slate-500">
                Periodo: {formatDateTimeIt(latest.period_start)} – {formatDateTimeIt(latest.period_end)}
              </p>
              {latest.accuracy_overall !== null ? (
                <p className="mt-1 text-xs text-slate-500">
                  Accuratezza di questo singolo lotto:{" "}
                  {formatConfidence(latest.accuracy_overall)} su {latest.evaluated_count}{" "}
                  {latest.evaluated_count === 1 ? "consiglio" : "consigli"} — indicativa solo
                  insieme allo storico.
                </p>
              ) : null}
            </Card>
          ) : null}

          <SimPortfolioCard
            data={simQuery.data}
            loading={simQuery.loading}
            error={simQuery.error}
            onRetry={simQuery.refetch}
          />

          <Card title="Andamento accuratezza per agente">
            <AccuracyTrendChart perAgent={latest.per_agent} />
            <p className="mt-3 text-xs text-slate-500">
              Ogni punto è una valutazione: quelle con pochissimi consigli valutati oscillano per
              forza (con un solo campione i valori possibili sono soltanto 0% e 100%).
            </p>
          </Card>

          <Card title="Metriche per agente" padded={false}>
            <div className="tm-scroll-x overflow-hidden">
              <table className="w-full min-w-[36rem] border-collapse text-sm">
                <thead>
                  <tr className="border-b border-[var(--tm-border)] text-left text-xs uppercase tracking-wider text-slate-500">
                    <th className="px-4 py-3 font-medium">Agente</th>
                    <th className="px-4 py-3 font-medium text-right">
                      <span className="inline-flex items-center gap-1">
                        Accuratezza
                        <InfoTip text={gloss("accuracy")} ariaLabel="Cos'è l'accuratezza" />
                      </span>
                    </th>
                    <th className="px-4 py-3 font-medium text-right">
                      <span className="inline-flex items-center gap-1">
                        Errore medio segnale
                        <InfoTip text={gloss("avg_signal_error")} ariaLabel="Cos'è l'errore medio del segnale" />
                      </span>
                    </th>
                    <th className="px-4 py-3 font-medium text-right">
                      <span className="inline-flex items-center gap-1">
                        Campioni
                        <InfoTip text={gloss("n_samples")} ariaLabel="Cosa sono i campioni" />
                      </span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--tm-border)]">
                  {latest.per_agent.map((agent) => (
                    <tr key={agent.agent_name}>
                      <td className="px-4 py-3 font-medium text-slate-200">{agentLabelIt(agent.agent_name)}</td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-100">
                        {agent.accuracy !== null ? formatConfidence(agent.accuracy) : "—"}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-100">
                        {agent.avg_signal_error !== null ? formatNumber(agent.avg_signal_error, 3) : "—"}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-400">{agent.n_samples}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}

      <div>
        <h2 className="mb-3 flex items-center gap-1 text-sm font-semibold uppercase tracking-wider text-slate-400">
          Lezioni apprese
          <InfoTip text={gloss("lessons")} ariaLabel="Cosa sono le lezioni apprese" />
        </h2>
        {feedbackQuery.loading ? (
          <Spinner />
        ) : feedbackQuery.error ? (
          <ErrorBox message={feedbackQuery.error} onRetry={feedbackQuery.refetch} />
        ) : feedbackByAgent.size === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-8 text-center text-sm text-slate-400">
            Nessuna lezione attiva al momento.
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {AGENT_ORDER.filter((agent) => (feedbackByAgent.get(agent)?.length ?? 0) > 0).map((agent) => (
              <Card key={agent} title={AGENT_LABELS_IT[agent]}>
                <ul className="space-y-3">
                  {(feedbackByAgent.get(agent) ?? []).map((item) => (
                    <li key={item.id} className="border-b border-[var(--tm-border)] pb-3 last:border-0 last:pb-0">
                      <p className="text-sm leading-relaxed text-slate-300">{item.lessons_it}</p>
                      <p className="mt-1 text-xs text-slate-500">{formatDateTimeIt(item.created_at)}</p>
                    </li>
                  ))}
                </ul>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
