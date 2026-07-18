import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError, apiGet, apiPost, errorMessage, isConflict } from "../../api/client";
import type {
  LlmProvidersOut,
  OllamaCatalogModel,
  OllamaLibraryOut,
  OllamaPullStatusOut,
} from "../../api/types";
import { gloss } from "../../lib/glossary";
import Card from "../common/Card";
import Spinner from "../common/Spinner";
import ErrorBox from "../common/ErrorBox";
import Badge from "../common/Badge";
import InfoTip from "../common/InfoTip";

interface OllamaModelsCardProps {
  /** Bump to re-check that Ollama is configured and reload the library. */
  refreshToken?: number;
  /** Called after a successful pull so sibling cards (models) can refresh. */
  onChanged?: () => void;
}

type PullPhase = "pulling" | "success" | "error";

interface PullState {
  status: PullPhase;
  completed_bytes: number | null;
  total_bytes: number | null;
  percent: number | null;
  detail_it: string;
}

/** Human byte size: GB with one decimal from 1 GB up, otherwise whole MB. */
function formatBytes(bytes: number | null): string {
  if (bytes === null || Number.isNaN(bytes)) return "—";
  const gb = bytes / 1e9;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  const mb = bytes / 1e6;
  return `${Math.max(0, Math.round(mb))} MB`;
}

const POLL_INTERVAL_MS = 2000;

export default function OllamaModelsCard({ refreshToken, onChanged }: OllamaModelsCardProps = {}) {
  // null = not yet determined (first load); true/false afterwards.
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [library, setLibrary] = useState<OllamaLibraryOut | null>(null);
  const [libLoading, setLibLoading] = useState(false);
  const [libError, setLibError] = useState<string | null>(null);
  const [libErrorStatus, setLibErrorStatus] = useState<number | null>(null);

  // Per-model download state so several pulls can run at once.
  const [pulls, setPulls] = useState<Record<string, PullState>>({});
  const timersRef = useRef<Record<string, number>>({});

  const loadLibrary = useCallback(() => {
    setLibLoading(true);
    setLibError(null);
    setLibErrorStatus(null);
    apiGet<OllamaLibraryOut>("/llm/ollama/library")
      .then((res) => setLibrary(res))
      .catch((err) => {
        setLibError(errorMessage(err));
        setLibErrorStatus(err instanceof ApiError ? err.status : null);
      })
      .finally(() => setLibLoading(false));
  }, []);

  // Discover whether Ollama is configured; load the library when it is. Runs on
  // mount and whenever refreshToken changes (e.g. after the URL is saved, or a
  // pull completes). Never resets `configured` to null here, so the card doesn't
  // flash out during background reloads.
  useEffect(() => {
    let alive = true;
    apiGet<LlmProvidersOut>("/llm/providers")
      .then((res) => {
        if (!alive) return;
        const ollama = res.providers.find((p) => p.provider === "ollama");
        const isConfigured = Boolean(ollama?.configured);
        setConfigured(isConfigured);
        if (isConfigured) loadLibrary();
      })
      .catch(() => {
        if (alive) setConfigured(false);
      });
    return () => {
      alive = false;
    };
  }, [refreshToken, loadLibrary]);

  const stopPolling = useCallback((model: string) => {
    const id = timersRef.current[model];
    if (id !== undefined) {
      window.clearInterval(id);
      delete timersRef.current[model];
    }
  }, []);

  const startPolling = useCallback(
    (model: string) => {
      stopPolling(model);
      const tick = () => {
        apiGet<OllamaPullStatusOut>("/llm/ollama/pull/status", { model })
          .then((res) => {
            setPulls((prev) => ({
              ...prev,
              [model]: {
                // A brief "idle" right after starting still reads as in-progress.
                status: res.status === "idle" ? "pulling" : res.status,
                completed_bytes: res.completed_bytes,
                total_bytes: res.total_bytes,
                percent: res.percent,
                detail_it: res.detail_it,
              },
            }));
            if (res.status === "success") {
              stopPolling(model);
              loadLibrary();
              onChanged?.();
            } else if (res.status === "error") {
              stopPolling(model);
            }
          })
          .catch((err) => {
            setPulls((prev) => ({
              ...prev,
              [model]: {
                status: "error",
                completed_bytes: prev[model]?.completed_bytes ?? null,
                total_bytes: prev[model]?.total_bytes ?? null,
                percent: prev[model]?.percent ?? null,
                detail_it: errorMessage(err),
              },
            }));
            stopPolling(model);
          });
      };
      tick();
      timersRef.current[model] = window.setInterval(tick, POLL_INTERVAL_MS);
    },
    [loadLibrary, onChanged, stopPolling],
  );

  const handlePull = useCallback(
    async (model: string) => {
      setPulls((prev) => ({
        ...prev,
        [model]: {
          status: "pulling",
          completed_bytes: null,
          total_bytes: null,
          percent: null,
          detail_it: "Avvio del download…",
        },
      }));
      try {
        await apiPost<{ detail?: string }>("/llm/ollama/pull", { model });
        startPolling(model);
      } catch (err) {
        if (isConflict(err)) {
          // Already downloading (started elsewhere): just follow its progress.
          startPolling(model);
          return;
        }
        setPulls((prev) => ({
          ...prev,
          [model]: {
            status: "error",
            completed_bytes: null,
            total_bytes: null,
            percent: null,
            detail_it: errorMessage(err),
          },
        }));
      }
    },
    [startPolling],
  );

  // Clear any live pollers on unmount.
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const id of Object.values(timers)) window.clearInterval(id);
    };
  }, []);

  function renderCatalogRow(m: OllamaCatalogModel): ReactNode {
    const pull = pulls[m.id];
    const isPulling = pull?.status === "pulling";
    const isError = pull?.status === "error";
    // Reflect success immediately, before the library list finishes reloading.
    const isInstalled = m.installed || pull?.status === "success";

    return (
      <li key={m.id} className="rounded-lg border border-[var(--tm-border)] p-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="font-mono text-sm text-slate-100">{m.id}</span>
              <span className="text-xs text-slate-500">{m.size_hint}</span>
            </div>
            <p className="mt-0.5 text-xs leading-relaxed text-slate-400">{m.description_it}</p>
          </div>
          <div className="shrink-0">
            {isInstalled ? (
              <Badge variant="gain">Installato</Badge>
            ) : isPulling ? (
              <Spinner size="sm" label="Scarico…" />
            ) : (
              <button
                type="button"
                onClick={() => handlePull(m.id)}
                className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-semibold text-white transition hover:bg-brand-500"
              >
                {isError ? "Riprova" : "Scarica"}
              </button>
            )}
          </div>
        </div>

        {isPulling ? (
          <div className="mt-3">
            <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
              <div
                className={`h-full rounded-full bg-brand-500 ${
                  pull.percent === null ? "animate-pulse" : "transition-all duration-500"
                }`}
                style={{ width: `${pull.percent ?? 100}%` }}
              />
            </div>
            <p className="mt-1 flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 text-xs text-slate-500">
              <span>{pull.detail_it}</span>
              <span className="font-mono">
                {pull.percent !== null ? `${Math.round(pull.percent)}% · ` : ""}
                {formatBytes(pull.completed_bytes)}
                {pull.total_bytes ? ` / ${formatBytes(pull.total_bytes)}` : ""}
              </span>
            </p>
          </div>
        ) : null}

        {isError ? <p className="mt-2 text-xs text-loss-light">{pull.detail_it}</p> : null}
      </li>
    );
  }

  // Only shown once Ollama is configured; otherwise the card renders nothing.
  if (!configured) return null;

  const title = (
    <span className="inline-flex items-center gap-1">
      Modelli Ollama
      <InfoTip text={gloss("ollama")} ariaLabel="Cos'è Ollama" />
    </span>
  );

  let body: ReactNode;
  if (libLoading && !library) {
    body = <Spinner label="Caricamento modelli Ollama…" />;
  } else if (libError && !library) {
    const message =
      libErrorStatus === 502
        ? `${libError} Controlla che Ollama sia avviato sul tuo computer.`
        : libError;
    body = <ErrorBox message={message} onRetry={loadLibrary} />;
  } else if (library) {
    body = (
      <div className="space-y-8">
        <div>
          <h4 className="text-sm font-semibold text-slate-200">Modelli installati</h4>
          {library.installed.length === 0 ? (
            <p className="mt-2 text-sm leading-relaxed text-slate-400">
              Nessun modello installato: scaricane uno dal catalogo qui sotto.
            </p>
          ) : (
            <ul className="mt-3 space-y-2">
              {library.installed.map((m) => (
                <li
                  key={m.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--tm-border)] px-3 py-2"
                >
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <span className="font-mono text-sm text-slate-100">{m.id}</span>
                    <span className="text-xs text-slate-500">{formatBytes(m.size_bytes)}</span>
                  </div>
                  <Badge variant="gain">Pronto</Badge>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <h4 className="text-sm font-semibold text-slate-200">Catalogo (da scaricare)</h4>
          <p className="mt-0.5 text-xs leading-relaxed text-slate-500">
            Modelli popolari adatti all'analisi. Il download avviene sul tuo computer e può richiedere alcuni minuti.
          </p>
          {library.catalog.length === 0 ? (
            <p className="mt-3 text-sm text-slate-400">Nessun modello nel catalogo.</p>
          ) : (
            <ul className="mt-3 space-y-2">{library.catalog.map(renderCatalogRow)}</ul>
          )}
        </div>
      </div>
    );
  } else {
    body = null;
  }

  return <Card title={title}>{body}</Card>;
}
