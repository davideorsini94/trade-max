import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiGet, apiPost, errorMessage, isConflict, isNotFound } from "../api/client";
import type {
  AnalysisRunOut,
  AnalyzeAccepted,
  PriceHistoryOut,
  RecommendationListOut,
  RecommendationOut,
  SettingsOut,
  SymbolOverviewOut,
  SymbolWithQuote,
} from "../api/types";
import { useApi } from "../hooks/useApi";
import { usePolling } from "../hooks/usePolling";
import Badge from "../components/common/Badge";
import Card from "../components/common/Card";
import ErrorBox from "../components/common/ErrorBox";
import Spinner from "../components/common/Spinner";
import PriceChart from "../components/charts/PriceChart";
import SymbolOverview from "../components/symbols/SymbolOverview";
import RecommendationCard from "../components/recommendations/RecommendationCard";
import AgentBreakdown from "../components/recommendations/AgentBreakdown";
import RecommendationTimeline from "../components/recommendations/RecommendationTimeline";
import RunProgress from "../components/recommendations/RunProgress";
import { parseBackendDate } from "../lib/format";

// A run that finished within this window is still shown (with its final state)
// to whoever re-enters the page: without it, a quickly-failed analysis would
// leave no visible trace after navigating away and back.
const RECENT_RUN_WINDOW_MS = 30 * 60 * 1000;

export default function SymbolDetailPage() {
  const { ticker } = useParams<{ ticker: string }>();

  const symbolsQuery = useApi<SymbolWithQuote[]>(() => apiGet<SymbolWithQuote[]>("/symbols"), []);
  const settingsQuery = useApi<SettingsOut>(() => apiGet<SettingsOut>("/settings"), []);

  const symbol = useMemo(() => {
    if (!symbolsQuery.data || !ticker) return null;
    const needle = ticker.toLowerCase();
    return symbolsQuery.data.find((s) => s.ticker.toLowerCase() === needle) ?? null;
  }, [symbolsQuery.data, ticker]);

  const symbolId = symbol?.id ?? null;

  const pricesQuery = useApi<PriceHistoryOut | null>(() => {
    if (symbolId === null) return Promise.resolve(null);
    return apiGet<PriceHistoryOut>(`/symbols/${symbolId}/prices`, { days: 180, indicators: true });
  }, [symbolId]);

  const overviewPolling = usePolling<SymbolOverviewOut | null>(
    () => {
      if (symbolId === null) return Promise.resolve(null);
      return apiGet<SymbolOverviewOut>(`/symbols/${symbolId}/overview`);
    },
    60000,
    { enabled: symbolId !== null },
  );

  const latestRecoPolling = usePolling<RecommendationOut | null>(
    async () => {
      if (symbolId === null) return null;
      try {
        return await apiGet<RecommendationOut>(`/symbols/${symbolId}/recommendations/latest`);
      } catch (err) {
        if (isNotFound(err)) return null;
        throw err;
      }
    },
    60000,
    { enabled: symbolId !== null },
  );

  const recoListPolling = usePolling<RecommendationListOut | null>(
    () => {
      if (symbolId === null) return Promise.resolve(null);
      return apiGet<RecommendationListOut>(`/symbols/${symbolId}/recommendations`, { limit: 20 });
    },
    60000,
    { enabled: symbolId !== null },
  );

  const [activeRunId, setActiveRunId] = useState<number | null>(null);
  const [runPollingEnabled, setRunPollingEnabled] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [analyzeInfo, setAnalyzeInfo] = useState<string | null>(null);

  const runPolling = usePolling<AnalysisRunOut | null>(
    () => {
      if (activeRunId === null) return Promise.resolve(null);
      return apiGet<AnalysisRunOut>(`/runs/${activeRunId}`);
    },
    3000,
    { enabled: runPollingEnabled && activeRunId !== null },
  );

  useEffect(() => {
    const status = runPolling.data?.status;
    if (status === "COMPLETED" || status === "FAILED") {
      setRunPollingEnabled(false);
      latestRecoPolling.refresh();
      recoListPolling.refresh();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runPolling.data?.status, runPolling.data?.id]);

  // Detect an in-flight run on mount / when re-entering the page: the run_id is
  // otherwise only known to the tab that clicked "Analizza ora", so navigating
  // away and back would lose all progress feedback.
  const latestRunQuery = useApi<AnalysisRunOut | null>(async () => {
    if (symbolId === null) return null;
    try {
      return await apiGet<AnalysisRunOut>(`/symbols/${symbolId}/runs/latest`);
    } catch (err) {
      if (isNotFound(err)) return null;
      throw err;
    }
  }, [symbolId]);

  // Clear any run state carried over from a previously viewed symbol before
  // resuming this symbol's own in-flight run (the component instance is reused
  // across ticker route changes).
  useEffect(() => {
    setActiveRunId(null);
    setRunPollingEnabled(false);
    setAnalyzeError(null);
    setAnalyzeInfo(null);
  }, [symbolId]);

  // Resume polling for a run that is still PENDING/RUNNING for this symbol;
  // a run that finished only minutes ago is shown too (final state, no
  // polling), so its outcome/error survives leaving and re-entering the page.
  useEffect(() => {
    const latest = latestRunQuery.data;
    if (!latest || latest.symbol_id !== symbolId || activeRunId !== null) return;
    if (latest.status === "PENDING" || latest.status === "RUNNING") {
      setActiveRunId(latest.id);
      setRunPollingEnabled(true);
      return;
    }
    const finishedAt = latest.finished_at ?? latest.started_at;
    if (Date.now() - parseBackendDate(finishedAt).getTime() < RECENT_RUN_WINDOW_MS) {
      setActiveRunId(latest.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [latestRunQuery.data, symbolId]);

  const lastRunQuery = useApi<AnalysisRunOut | null>(() => {
    if (activeRunId !== null) return Promise.resolve(null);
    const runId = latestRecoPolling.data?.run_id;
    if (!runId) return Promise.resolve(null);
    return apiGet<AnalysisRunOut>(`/runs/${runId}`);
  }, [activeRunId, latestRecoPolling.data?.run_id]);

  // The run currently being tracked: the freshest data available for activeRunId
  // (live poll, else the mount fetch that seeded the resume), matched by id so a
  // stale run from another symbol is never shown.
  const activeRun =
    activeRunId === null
      ? null
      : runPolling.data?.id === activeRunId
        ? runPolling.data
        : latestRunQuery.data?.id === activeRunId
          ? latestRunQuery.data
          : null;

  const displayedRun = activeRunId !== null ? activeRun : lastRunQuery.data;

  async function handleAnalyze() {
    if (symbolId === null || analyzing) return;
    setAnalyzing(true);
    setAnalyzeError(null);
    setAnalyzeInfo(null);
    try {
      const accepted = await apiPost<AnalyzeAccepted>(`/symbols/${symbolId}/analyze`, { trigger: "MANUAL" });
      setActiveRunId(accepted.run_id);
      setRunPollingEnabled(true);
    } catch (err) {
      if (isConflict(err)) {
        // An analysis is already running: recover its run and resume its
        // progress instead of only reporting the conflict.
        try {
          const latest = await apiGet<AnalysisRunOut>(`/symbols/${symbolId}/runs/latest`);
          if (latest.status === "PENDING" || latest.status === "RUNNING") {
            setActiveRunId(latest.id);
            setRunPollingEnabled(true);
            setAnalyzeInfo("Un'analisi era già in corso: ecco lo stato.");
          } else {
            setAnalyzeError("Un'analisi per questo titolo è già in corso.");
          }
        } catch {
          setAnalyzeError("Un'analisi per questo titolo è già in corso.");
        }
      } else {
        setAnalyzeError(errorMessage(err));
      }
    } finally {
      setAnalyzing(false);
    }
  }

  if (symbolsQuery.loading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner size="lg" label="Ricerca del titolo…" />
      </div>
    );
  }

  if (symbolsQuery.error) {
    return <ErrorBox message={symbolsQuery.error} onRetry={symbolsQuery.refetch} />;
  }

  if (!symbol) {
    return (
      <div className="space-y-4">
        <ErrorBox message={`Il titolo "${ticker ?? ""}" non è tra quelli monitorati.`} />
        <Link to="/" className="inline-block text-sm font-medium text-brand-300 hover:text-brand-200">
          ← Torna alla dashboard
        </Link>
      </div>
    );
  }

  const currency = symbol.currency;
  const activeRunInFlight = activeRun?.status === "PENDING" || activeRun?.status === "RUNNING";
  const runInProgress = (activeRunId !== null && runPollingEnabled) || activeRunInFlight;
  const runStatus = displayedRun?.status;

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold text-slate-50">{symbol.ticker}</h1>
            {symbol.is_favorite ? <Badge variant="accent">★ Preferito</Badge> : null}
          </div>
          <p className="text-sm text-slate-400">{symbol.name || symbol.exchange || "—"}</p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <button
            type="button"
            onClick={handleAnalyze}
            disabled={analyzing || runInProgress}
            className="rounded-md bg-brand-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-brand-500 disabled:opacity-60"
          >
            {analyzing ? <Spinner size="sm" /> : runInProgress ? "Analisi in corso…" : "Analizza ora"}
          </button>
          {analyzeInfo ? <p className="max-w-xs text-right text-xs text-brand-300">{analyzeInfo}</p> : null}
          {analyzeError ? <p className="max-w-xs text-right text-xs text-loss-light">{analyzeError}</p> : null}
        </div>
      </div>

      {activeRun ? <RunProgress run={activeRun} /> : null}

      {overviewPolling.data ? (
        <SymbolOverview overview={overviewPolling.data} />
      ) : overviewPolling.loading ? (
        <Card>
          <div className="flex justify-center py-8">
            <Spinner label="Caricamento dati del titolo…" />
          </div>
        </Card>
      ) : overviewPolling.error ? (
        <ErrorBox message={overviewPolling.error} />
      ) : null}

      <Card title="Andamento prezzo e indicatori">
        {pricesQuery.loading ? (
          <div className="flex justify-center py-10">
            <Spinner />
          </div>
        ) : pricesQuery.error ? (
          <ErrorBox message={pricesQuery.error} onRetry={pricesQuery.refetch} />
        ) : (
          <PriceChart
            points={pricesQuery.data?.points ?? []}
            indicators={pricesQuery.data?.indicators ?? null}
            recommendations={recoListPolling.data?.items ?? []}
            currency={currency}
          />
        )}
      </Card>

      <div>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Raccomandazione attuale</h2>
        {latestRecoPolling.error ? <ErrorBox message={latestRecoPolling.error} className="mb-3" /> : null}
        {latestRecoPolling.data ? (
          <RecommendationCard
            recommendation={latestRecoPolling.data}
            currency={currency}
            budgetCurrency={settingsQuery.data?.budget_currency ?? "EUR"}
          />
        ) : (
          <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-8 text-center text-sm text-slate-400">
            Nessuna raccomandazione ancora disponibile per questo titolo.
          </div>
        )}
      </div>

      <div>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Dettaglio per agente</h2>
        <AgentBreakdown analyses={displayedRun?.analyses ?? []} runStatus={runStatus} />
      </div>

      <div>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Storico raccomandazioni</h2>
        {recoListPolling.error ? <ErrorBox message={recoListPolling.error} className="mb-3" /> : null}
        <RecommendationTimeline recommendations={recoListPolling.data?.items ?? []} />
      </div>
    </div>
  );
}
