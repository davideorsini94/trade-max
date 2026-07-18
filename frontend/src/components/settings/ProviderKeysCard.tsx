import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { apiGet, apiPost, apiPut, errorMessage } from "../../api/client";
import type {
  LlmProviderKeyInfo,
  LlmProviderName,
  LlmProvidersOut,
  LlmProvidersUpdate,
  LlmProviderTestOut,
} from "../../api/types";
import { gloss } from "../../lib/glossary";
import Card from "../common/Card";
import Spinner from "../common/Spinner";
import ErrorBox from "../common/ErrorBox";
import Badge from "../common/Badge";
import InfoTip from "../common/InfoTip";

interface ProviderKeysCardProps {
  /** Called after every successful PUT so sibling cards (models) can refresh. */
  onChanged?: () => void;
}

/** The cloud providers that carry an API-key field (rendered as key rows). */
type KeyProvider = "openrouter" | "gemini";

/** Cloud providers that carry an API-key field (rendered as key rows). */
const KEY_PROVIDER_ORDER: readonly KeyProvider[] = ["openrouter", "gemini"];
/** Order of the "Provider primario" radios (Ollama is selectable too). */
const PRIMARY_ORDER: readonly LlmProviderName[] = ["openrouter", "gemini", "ollama"];

const PROVIDER_LABELS: Record<LlmProviderName, string> = {
  openrouter: "OpenRouter",
  gemini: "Gemini",
  ollama: "Ollama (locale)",
};

/** Builds the PUT body that touches only the given cloud provider's key. */
function keyUpdate(provider: KeyProvider, value: string | null): LlmProvidersUpdate {
  return provider === "openrouter" ? { openrouter_api_key: value } : { gemini_api_key: value };
}

const inputClass =
  "w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 pr-10 text-sm text-slate-100 placeholder:text-slate-500 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50";

/** Same as inputClass but without the right padding reserved for the reveal button. */
const urlInputClass =
  "w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50";

function EyeIcon({ off }: { off: boolean }): ReactNode {
  if (off) {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M3 3l18 18M10.6 10.6a2 2 0 002.8 2.8M9.4 5.2A9.5 9.5 0 0112 5c5 0 9 4.5 9 7 0 1-.7 2.3-1.9 3.5M6.1 6.1C3.9 7.4 3 9.6 3 12c0 0 3.5 5 9 5 1 0 1.9-.2 2.7-.5"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
      <circle cx="12" cy="12" r="2.6" stroke="currentColor" strokeWidth="1.7" />
    </svg>
  );
}

export default function ProviderKeysCard({ onChanged }: ProviderKeysCardProps) {
  const [data, setData] = useState<LlmProvidersOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [reveal, setReveal] = useState<Record<string, boolean>>({});
  const [savingKey, setSavingKey] = useState<LlmProviderName | null>(null);
  const [removingKey, setRemovingKey] = useState<LlmProviderName | null>(null);
  const [testing, setTesting] = useState<LlmProviderName | null>(null);
  const [testResults, setTestResults] = useState<Record<string, LlmProviderTestOut>>({});
  const [rowError, setRowError] = useState<Record<string, string>>({});
  const [savedKey, setSavedKey] = useState<LlmProviderName | null>(null);

  // Ollama (local provider): a base-URL field instead of an API key.
  const [ollamaUrlDraft, setOllamaUrlDraft] = useState("");
  const [savingOllamaUrl, setSavingOllamaUrl] = useState(false);
  const [removingOllamaUrl, setRemovingOllamaUrl] = useState(false);
  const [ollamaSaved, setOllamaSaved] = useState(false);

  const [prefsSaving, setPrefsSaving] = useState(false);
  const [prefsError, setPrefsError] = useState<string | null>(null);

  const load = useCallback(() => {
    let alive = true;
    setLoading(true);
    setLoadError(null);
    apiGet<LlmProvidersOut>("/llm/providers")
      .then((res) => {
        if (alive) setData(res);
      })
      .catch((err) => {
        if (alive) setLoadError(errorMessage(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => load(), [load]);

  function clearRowFeedback(provider: LlmProviderName) {
    setRowError((prev) => {
      if (!(provider in prev)) return prev;
      const next = { ...prev };
      delete next[provider];
      return next;
    });
    setTestResults((prev) => {
      if (!(provider in prev)) return prev;
      const next = { ...prev };
      delete next[provider];
      return next;
    });
    setSavedKey((prev) => (prev === provider ? null : prev));
  }

  /** Shared PUT: applies the server response and notifies the parent. */
  async function applyUpdate(update: LlmProvidersUpdate): Promise<void> {
    const res = await apiPut<LlmProvidersOut>("/llm/providers", update);
    setData(res);
    onChanged?.();
  }

  async function handleSaveKey(provider: KeyProvider) {
    const value = (drafts[provider] ?? "").trim();
    if (!value || savingKey) return;
    clearRowFeedback(provider);
    setSavingKey(provider);
    try {
      await applyUpdate(keyUpdate(provider, value));
      setDrafts((prev) => ({ ...prev, [provider]: "" }));
      setReveal((prev) => ({ ...prev, [provider]: false }));
      setSavedKey(provider);
    } catch (err) {
      setRowError((prev) => ({ ...prev, [provider]: errorMessage(err) }));
    } finally {
      setSavingKey(null);
    }
  }

  async function handleRemoveKey(provider: KeyProvider) {
    if (removingKey) return;
    const confirmed = window.confirm(
      `Vuoi rimuovere la chiave API salvata per ${PROVIDER_LABELS[provider]}? L'app tornerà a usare l'eventuale chiave presente nel file .env, se configurata.`,
    );
    if (!confirmed) return;
    clearRowFeedback(provider);
    setRemovingKey(provider);
    try {
      await applyUpdate(keyUpdate(provider, null));
    } catch (err) {
      setRowError((prev) => ({ ...prev, [provider]: errorMessage(err) }));
    } finally {
      setRemovingKey(null);
    }
  }

  async function handleSaveOllamaUrl() {
    const value = ollamaUrlDraft.trim();
    if (!value || savingOllamaUrl) return;
    clearRowFeedback("ollama");
    setOllamaSaved(false);
    setSavingOllamaUrl(true);
    try {
      await applyUpdate({ ollama_base_url: value });
      setOllamaUrlDraft("");
      setOllamaSaved(true);
    } catch (err) {
      setRowError((prev) => ({ ...prev, ollama: errorMessage(err) }));
    } finally {
      setSavingOllamaUrl(false);
    }
  }

  async function handleRemoveOllamaUrl() {
    if (removingOllamaUrl) return;
    const confirmed = window.confirm(
      "Vuoi rimuovere l'URL di Ollama configurato nell'app? L'app tornerà a usare l'eventuale URL nel file .env (OLLAMA_BASE_URL), se presente.",
    );
    if (!confirmed) return;
    clearRowFeedback("ollama");
    setOllamaSaved(false);
    setRemovingOllamaUrl(true);
    try {
      await applyUpdate({ ollama_base_url: null });
      setOllamaUrlDraft("");
    } catch (err) {
      setRowError((prev) => ({ ...prev, ollama: errorMessage(err) }));
    } finally {
      setRemovingOllamaUrl(false);
    }
  }

  async function handleTest(provider: LlmProviderName) {
    if (testing) return;
    setSavedKey((prev) => (prev === provider ? null : prev));
    setTestResults((prev) => {
      if (!(provider in prev)) return prev;
      const next = { ...prev };
      delete next[provider];
      return next;
    });
    setTesting(provider);
    try {
      const res = await apiPost<LlmProviderTestOut>("/llm/providers/test", { provider });
      setTestResults((prev) => ({ ...prev, [provider]: res }));
    } catch (err) {
      setTestResults((prev) => ({ ...prev, [provider]: { ok: false, detail_it: errorMessage(err) } }));
    } finally {
      setTesting(null);
    }
  }

  async function handlePrimaryChange(provider: LlmProviderName) {
    if (!data || prefsSaving || data.primary_provider === provider) return;
    const prev = data;
    setPrefsError(null);
    setPrefsSaving(true);
    setData({ ...data, primary_provider: provider }); // optimistic
    try {
      await applyUpdate({ primary_provider: provider });
    } catch (err) {
      setData(prev); // rollback
      setPrefsError(errorMessage(err));
    } finally {
      setPrefsSaving(false);
    }
  }

  async function handleFallbackChange(next: boolean) {
    if (!data || prefsSaving) return;
    const prev = data;
    setPrefsError(null);
    setPrefsSaving(true);
    setData({ ...data, fallback_enabled: next }); // optimistic
    try {
      await applyUpdate({ fallback_enabled: next });
    } catch (err) {
      setData(prev); // rollback
      setPrefsError(errorMessage(err));
    } finally {
      setPrefsSaving(false);
    }
  }

  const title = (
    <span className="inline-flex items-center gap-1">
      Provider LLM
      <InfoTip text={gloss("api_key")} ariaLabel="Cos'è una chiave API" />
    </span>
  );

  function renderProviderRow(provider: KeyProvider, info: LlmProviderKeyInfo): ReactNode {
    const label = PROVIDER_LABELS[provider];
    const draft = drafts[provider] ?? "";
    const isRevealed = Boolean(reveal[provider]);
    const isSaving = savingKey === provider;
    const isRemoving = removingKey === provider;
    const isTesting = testing === provider;
    const busy = isSaving || isRemoving || isTesting;
    const test = testResults[provider];
    const err = rowError[provider];

    let statusBadge: ReactNode;
    if (info.source === "app") {
      statusBadge = <Badge variant="gain">Configurata dall'app</Badge>;
    } else if (info.source === "env") {
      statusBadge = <Badge variant="info">Configurata da .env</Badge>;
    } else {
      statusBadge = <Badge variant="neutral">Non configurata</Badge>;
    }

    return (
      <div key={provider} className="rounded-lg border border-[var(--tm-border)] p-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-sm font-semibold text-slate-100">{label}</span>
          {statusBadge}
          {info.key_masked ? (
            <span className="font-mono text-xs text-slate-500">{info.key_masked}</span>
          ) : null}
        </div>
        <p className="mt-1 text-xs text-slate-500">Modello predefinito: {info.default_model || "—"}</p>

        <div className="mt-3">
          <label htmlFor={`provider-key-${provider}`} className="sr-only">
            Nuova chiave API per {label}
          </label>
          <div className="relative">
            <input
              id={`provider-key-${provider}`}
              type={isRevealed ? "text" : "password"}
              value={draft}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              onChange={(event) => {
                const value = event.target.value;
                setDrafts((prev) => ({ ...prev, [provider]: value }));
                setSavedKey((prev) => (prev === provider ? null : prev));
              }}
              placeholder="Incolla qui la nuova chiave API…"
              className={inputClass}
            />
            <button
              type="button"
              onClick={() => setReveal((prev) => ({ ...prev, [provider]: !prev[provider] }))}
              aria-label={isRevealed ? "Nascondi la chiave" : "Mostra la chiave"}
              aria-pressed={isRevealed}
              className="absolute right-2 top-1/2 -translate-y-1/2 inline-flex h-6 w-6 items-center justify-center rounded text-slate-500 transition-colors hover:text-slate-300 focus:text-slate-300 focus:outline-none focus-visible:ring-1 focus-visible:ring-brand-500"
            >
              <EyeIcon off={isRevealed} />
            </button>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => handleSaveKey(provider)}
            disabled={busy || draft.trim().length === 0}
            className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-semibold text-white transition hover:bg-brand-500 disabled:opacity-50"
          >
            {isSaving ? <Spinner size="sm" /> : "Salva chiave"}
          </button>
          <button
            type="button"
            onClick={() => handleRemoveKey(provider)}
            disabled={busy || info.source !== "app"}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm font-medium text-slate-300 transition hover:border-slate-600 hover:text-slate-100 disabled:opacity-50"
          >
            {isRemoving ? <Spinner size="sm" /> : "Rimuovi"}
          </button>
          <button
            type="button"
            onClick={() => handleTest(provider)}
            disabled={busy || !info.configured}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm font-medium text-slate-300 transition hover:border-slate-600 hover:text-slate-100 disabled:opacity-50"
          >
            {isTesting ? <Spinner size="sm" label="Verifica…" /> : "Prova connessione"}
          </button>
          {savedKey === provider ? <span className="text-sm font-medium text-gain-light">Chiave salvata</span> : null}
        </div>

        {test ? (
          <p className={`mt-2 text-xs ${test.ok ? "text-gain-light" : "text-loss-light"}`}>
            {test.ok ? "✓ " : "✗ "}
            {test.detail_it}
          </p>
        ) : null}
        {err ? <p className="mt-2 text-xs text-loss-light">{err}</p> : null}
      </div>
    );
  }

  function renderOllamaBlock(info: LlmProviderKeyInfo): ReactNode {
    const isSaving = savingOllamaUrl;
    const isRemoving = removingOllamaUrl;
    const isTesting = testing === "ollama";
    const busy = isSaving || isRemoving || isTesting;
    const test = testResults.ollama;
    const err = rowError.ollama;

    let statusBadge: ReactNode;
    if (info.source === "app") {
      statusBadge = <Badge variant="gain">Configurato dall'app</Badge>;
    } else if (info.source === "env") {
      statusBadge = <Badge variant="info">Configurato da .env</Badge>;
    } else {
      statusBadge = <Badge variant="neutral">Non configurato</Badge>;
    }

    return (
      <div key="ollama" className="rounded-lg border border-[var(--tm-border)] p-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="inline-flex items-center gap-1 text-sm font-semibold text-slate-100">
            {PROVIDER_LABELS.ollama}
            <InfoTip text={gloss("ollama")} ariaLabel="Cos'è Ollama" />
          </span>
          {statusBadge}
        </div>
        <p className="mt-1 text-xs text-slate-500">
          URL attuale: <span className="font-mono text-slate-400">{info.base_url || "—"}</span>
        </p>

        <div className="mt-3">
          <label htmlFor="ollama-base-url" className="sr-only">
            URL di Ollama
          </label>
          <input
            id="ollama-base-url"
            type="text"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            value={ollamaUrlDraft}
            onChange={(event) => {
              setOllamaUrlDraft(event.target.value);
              setOllamaSaved(false);
            }}
            placeholder="URL di Ollama (es. http://localhost:11434)"
            className={urlInputClass}
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={handleSaveOllamaUrl}
            disabled={busy || ollamaUrlDraft.trim().length === 0}
            className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-semibold text-white transition hover:bg-brand-500 disabled:opacity-50"
          >
            {isSaving ? <Spinner size="sm" /> : "Salva URL"}
          </button>
          <button
            type="button"
            onClick={handleRemoveOllamaUrl}
            disabled={busy || info.source !== "app"}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm font-medium text-slate-300 transition hover:border-slate-600 hover:text-slate-100 disabled:opacity-50"
          >
            {isRemoving ? <Spinner size="sm" /> : "Rimuovi"}
          </button>
          <button
            type="button"
            onClick={() => handleTest("ollama")}
            disabled={busy || !info.configured}
            className="rounded-md border border-slate-700 px-3 py-1.5 text-sm font-medium text-slate-300 transition hover:border-slate-600 hover:text-slate-100 disabled:opacity-50"
          >
            {isTesting ? <Spinner size="sm" label="Verifica…" /> : "Prova connessione"}
          </button>
          {ollamaSaved ? <span className="text-sm font-medium text-gain-light">URL salvato</span> : null}
        </div>

        {test ? (
          <p className={`mt-2 text-xs ${test.ok ? "text-gain-light" : "text-loss-light"}`}>
            {test.ok ? "✓ " : "✗ "}
            {test.detail_it}
          </p>
        ) : null}
        {err ? <p className="mt-2 text-xs text-loss-light">{err}</p> : null}

        <p className="mt-3 text-xs leading-relaxed text-slate-500">
          Ollama gira sul tuo computer: gratuito e senza limiti di richieste. Se l'app gira in Docker usa
          http://host.docker.internal:11434.
        </p>
      </div>
    );
  }

  let body: ReactNode;
  if (loading) {
    body = <Spinner label="Caricamento provider…" />;
  } else if (loadError || !data) {
    body = <ErrorBox message={loadError ?? "Impossibile caricare i provider."} onRetry={load} />;
  } else {
    const infoByProvider = new Map<LlmProviderName, LlmProviderKeyInfo>();
    for (const p of data.providers) infoByProvider.set(p.provider, p);
    const ollamaInfo = infoByProvider.get("ollama") ?? null;

    body = (
      <div className="space-y-6">
        <p className="text-xs leading-relaxed text-slate-500">
          Incolla le chiavi API dei provider per abilitare l'analisi. Le chiavi restano salvate solo sul tuo computer,
          nel database locale dell'app.
        </p>

        <div className="space-y-3">
          {KEY_PROVIDER_ORDER.map((provider) => {
            const info = infoByProvider.get(provider);
            return info ? renderProviderRow(provider, info) : null;
          })}
          {ollamaInfo ? renderOllamaBlock(ollamaInfo) : null}
        </div>

        <div className="space-y-4 border-t border-[var(--tm-border)] pt-5">
          <fieldset>
            <legend className="mb-2 inline-flex items-center gap-1 text-sm font-medium text-slate-300">
              Provider primario
              <InfoTip text={gloss("llm_provider")} ariaLabel="Cos'è un provider LLM" />
            </legend>
            <div className="flex flex-wrap gap-3">
              {PRIMARY_ORDER.map((provider) => (
                <label
                  key={provider}
                  className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm transition ${
                    data.primary_provider === provider
                      ? "border-brand-500 bg-brand-900/30"
                      : "border-slate-700 bg-slate-900/40 hover:border-slate-600"
                  } ${prefsSaving ? "opacity-60" : ""}`}
                >
                  <input
                    type="radio"
                    name="primary_provider"
                    value={provider}
                    checked={data.primary_provider === provider}
                    disabled={prefsSaving}
                    onChange={() => handlePrimaryChange(provider)}
                    className="accent-brand-500"
                  />
                  <span className="font-medium text-slate-100">{PROVIDER_LABELS[provider]}</span>
                </label>
              ))}
            </div>
            <p className="mt-1.5 text-xs leading-relaxed text-slate-500">
              Il provider usato per primo per ogni analisi.
            </p>
          </fieldset>

          <label className={`flex items-start gap-2 text-sm ${prefsSaving ? "opacity-60" : ""}`}>
            <input
              type="checkbox"
              checked={data.fallback_enabled}
              disabled={prefsSaving}
              onChange={(event) => handleFallbackChange(event.target.checked)}
              className="mt-0.5 accent-brand-500"
            />
            <span>
              <span className="font-medium text-slate-200">Fallback automatico sull'altro provider</span>
              <span className="mt-0.5 block text-xs leading-relaxed text-slate-500">
                Se il provider primario non risponde, l'app riprova automaticamente con l'altro provider configurato.
              </span>
            </span>
          </label>

          <div className="flex items-center gap-3">
            {prefsSaving ? <Spinner size="sm" label="Salvataggio…" /> : null}
            {prefsError ? <span className="text-sm text-loss-light">{prefsError}</span> : null}
          </div>
        </div>
      </div>
    );
  }

  return <Card title={title}>{body}</Card>;
}
