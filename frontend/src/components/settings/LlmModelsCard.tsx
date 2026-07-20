import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { apiGet, apiPut, errorMessage } from "../../api/client";
import type {
  LlmConfigOut,
  LlmConfigUpdate,
  LlmModelOption,
  LlmModelRef,
  LlmModelsOut,
  LlmPerAgentConfig,
} from "../../api/types";
import { AGENT_LABELS_IT, AGENT_ORDER } from "../../lib/labels";
import type { AgentName } from "../../lib/labels";
import { gloss } from "../../lib/glossary";
import Card from "../common/Card";
import Spinner from "../common/Spinner";
import ErrorBox from "../common/ErrorBox";
import InfoTip from "../common/InfoTip";

const PROVIDER_LABELS: Record<string, string> = {
  openrouter: "OpenRouter",
  gemini: "Gemini",
  ollama: "Ollama (locale)",
};

function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

const EMPTY_PER_AGENT: LlmPerAgentConfig = {
  technical: null,
  fundamentals: null,
  macro_news: null,
  corporate_news: null,
  synthesizer: null,
  validator: null,
};

/** Drops a half-filled selection (provider without model) down to null = "usa il predefinito". */
function cleanRef(ref: LlmModelRef | null): LlmModelRef | null {
  if (!ref || !ref.provider) return null;
  const model = ref.model.trim();
  return model ? { provider: ref.provider, model } : null;
}

const inputClass =
  "w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50";

interface LlmModelsCardProps {
  /** Bump this (e.g. after a provider-key change) to re-run the /llm/config load. */
  refreshToken?: number;
}

export default function LlmModelsCard({ refreshToken }: LlmModelsCardProps = {}) {
  const [config, setConfig] = useState<LlmConfigOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [defaultRef, setDefaultRef] = useState<LlmModelRef | null>(null);
  const [perAgent, setPerAgent] = useState<LlmPerAgentConfig>(EMPTY_PER_AGENT);

  const [modelsByProvider, setModelsByProvider] = useState<Record<string, LlmModelOption[]>>({});
  const [modelsLoading, setModelsLoading] = useState<Record<string, boolean>>({});
  const [modelsError, setModelsError] = useState<Record<string, string>>({});
  const requestedRef = useRef<Set<string>>(new Set());

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const ensureModels = useCallback((provider: string) => {
    if (!provider || requestedRef.current.has(provider)) return;
    requestedRef.current.add(provider);
    setModelsLoading((prev) => ({ ...prev, [provider]: true }));
    setModelsError((prev) => {
      if (!(provider in prev)) return prev;
      const next = { ...prev };
      delete next[provider];
      return next;
    });
    apiGet<LlmModelsOut>("/llm/models", { provider })
      .then((res) => {
        setModelsByProvider((prev) => ({ ...prev, [provider]: res.models }));
      })
      .catch((err) => {
        setModelsError((prev) => ({ ...prev, [provider]: errorMessage(err) }));
        // Allow a later retry (e.g. after the key is added) to re-request this provider.
        requestedRef.current.delete(provider);
      })
      .finally(() => {
        setModelsLoading((prev) => ({ ...prev, [provider]: false }));
      });
  }, []);

  const loadConfig = useCallback(() => {
    let alive = true;
    setLoading(true);
    setLoadError(null);
    apiGet<LlmConfigOut>("/llm/config")
      .then((cfg) => {
        if (!alive) return;
        setConfig(cfg);
        setDefaultRef(cfg.default);
        setPerAgent(cfg.per_agent);
        // Preload models for every provider already referenced, so datalists and
        // current values are populated without the user opening each field first.
        const provs = new Set<string>();
        if (cfg.default?.provider) provs.add(cfg.default.provider);
        for (const key of AGENT_ORDER) {
          const ref = cfg.per_agent[key];
          if (ref?.provider) provs.add(ref.provider);
        }
        provs.forEach((p) => ensureModels(p));
      })
      .catch((err) => {
        if (!alive) return;
        setLoadError(errorMessage(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [ensureModels]);

  useEffect(() => {
    // A refresh (provider key saved, or an Ollama model pulled) can change which
    // models exist per provider; drop the per-provider fetch cache so the model
    // options — including newly installed Ollama models — reload fresh.
    requestedRef.current = new Set();
    const cleanup = loadConfig();
    return cleanup;
  }, [loadConfig, refreshToken]);

  const configuredProviders = (config?.providers ?? []).filter((p) => p.configured);
  const primaryProvider = config?.providers.find((p) => p.is_primary) ?? null;
  const effectiveDefaultText =
    defaultRef && defaultRef.model
      ? `${providerLabel(defaultRef.provider)} · ${defaultRef.model}`
      : primaryProvider
        ? `da .env: ${primaryProvider.env_default_model}`
        : "predefinito";

  function changeDefaultProvider(provider: string) {
    setSaved(false);
    if (!provider) {
      setDefaultRef(null);
      return;
    }
    ensureModels(provider);
    setDefaultRef((prev) => ({ provider, model: prev && prev.provider === provider ? prev.model : "" }));
  }

  function changeDefaultModel(model: string) {
    setSaved(false);
    setDefaultRef((prev) => (prev ? { ...prev, model } : prev));
  }

  function changeAgentProvider(agent: AgentName, provider: string) {
    setSaved(false);
    if (!provider) {
      setPerAgent((prev) => ({ ...prev, [agent]: null }) as LlmPerAgentConfig);
      return;
    }
    ensureModels(provider);
    setPerAgent((prev) => {
      const cur = prev[agent];
      const next: LlmModelRef = { provider, model: cur && cur.provider === provider ? cur.model : "" };
      return { ...prev, [agent]: next } as LlmPerAgentConfig;
    });
  }

  function changeAgentModel(agent: AgentName, model: string) {
    setSaved(false);
    setPerAgent((prev) => {
      const cur = prev[agent];
      if (!cur) return prev;
      return { ...prev, [agent]: { ...cur, model } } as LlmPerAgentConfig;
    });
  }

  async function handleSave() {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      const payload: LlmConfigUpdate = {
        default: cleanRef(defaultRef),
        per_agent: {
          technical: cleanRef(perAgent.technical),
          fundamentals: cleanRef(perAgent.fundamentals),
          macro_news: cleanRef(perAgent.macro_news),
          corporate_news: cleanRef(perAgent.corporate_news),
          synthesizer: cleanRef(perAgent.synthesizer),
          validator: cleanRef(perAgent.validator),
        },
      };
      const updated = await apiPut<LlmConfigOut>("/llm/config", payload);
      setConfig(updated);
      setDefaultRef(updated.default);
      setPerAgent(updated.per_agent);
      setSaved(true);
    } catch (err) {
      setSaveError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  // Reusable searchable model combobox: <input> backed by a per-provider <datalist>.
  // Defined as a plain render helper (invoked as a function, not <Component/>) so the
  // <input> keeps a stable identity across renders and never loses focus while typing.
  function renderModelField({
    id,
    provider,
    value,
    onChange,
  }: {
    id: string;
    provider: string;
    value: string;
    onChange: (model: string) => void;
  }): ReactNode {
    const disabled = !provider;
    const isLoading = provider ? Boolean(modelsLoading[provider]) : false;
    const err = provider ? modelsError[provider] : undefined;
    const hasList = Boolean(provider && modelsByProvider[provider]);
    return (
      <div>
        <div className="relative">
          <input
            id={id}
            type="text"
            list={hasList ? `llm-models-${provider}` : undefined}
            value={value}
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
            placeholder={disabled ? "Scegli prima un provider" : "Cerca o digita un modello…"}
            className={inputClass}
          />
          {isLoading ? (
            <span className="absolute right-2 top-1/2 -translate-y-1/2">
              <Spinner size="sm" />
            </span>
          ) : null}
        </div>
        {err ? (
          <p className="mt-1 text-xs text-loss-light">{err} Puoi comunque digitare l'ID di un modello.</p>
        ) : null}
      </div>
    );
  }

  function renderProviderSelect({
    id,
    value,
    onChange,
    emptyLabel,
  }: {
    id: string;
    value: string;
    onChange: (provider: string) => void;
    emptyLabel: string;
  }): ReactNode {
    return (
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
      >
        <option value="">{emptyLabel}</option>
        {configuredProviders.map((p) => (
          <option key={p.provider} value={p.provider}>
            {providerLabel(p.provider)}
          </option>
        ))}
      </select>
    );
  }

  const title = (
    <span className="inline-flex items-center gap-1">
      Modelli LLM
      <InfoTip text={gloss("llm_model")} ariaLabel="Cos'è un modello LLM" />
    </span>
  );

  let body: ReactNode;
  if (loading) {
    body = <Spinner label="Caricamento modelli…" />;
  } else if (loadError) {
    body = <ErrorBox message={loadError} onRetry={loadConfig} />;
  } else if (configuredProviders.length === 0) {
    body = (
      <p className="text-sm leading-relaxed text-slate-400">
        Salva una chiave API nella sezione «Provider LLM» qui sopra per scegliere i modelli.
      </p>
    );
  } else {
    body = (
      <div className="space-y-8">
        {/* Hidden datalists, one per provider whose models are loaded. */}
        {Object.entries(modelsByProvider).map(([prov, models]) => (
          <datalist key={prov} id={`llm-models-${prov}`}>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </datalist>
        ))}

        {/* Default model */}
        <div>
          <h4 className="text-sm font-semibold text-slate-200">Modello predefinito</h4>
          <p className="mt-0.5 text-xs text-slate-500">
            Usato da tutti gli agenti che non hanno un modello dedicato qui sotto.
          </p>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label htmlFor="llm-default-provider" className="mb-1 block text-xs font-medium text-slate-400">
                Provider
              </label>
              {renderProviderSelect({
                id: "llm-default-provider",
                value: defaultRef?.provider ?? "",
                onChange: changeDefaultProvider,
                emptyLabel: "Usa il valore da .env",
              })}
            </div>
            <div>
              <label htmlFor="llm-default-model" className="mb-1 block text-xs font-medium text-slate-400">
                Modello
              </label>
              {renderModelField({
                id: "llm-default-model",
                provider: defaultRef?.provider ?? "",
                value: defaultRef?.model ?? "",
                onChange: changeDefaultModel,
              })}
            </div>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
            {primaryProvider ? (
              <span className="text-xs text-slate-500">Da .env: {primaryProvider.env_default_model}</span>
            ) : null}
            <button
              type="button"
              onClick={() => {
                setDefaultRef(null);
                setSaved(false);
              }}
              className="text-xs font-medium text-brand-300 transition hover:text-brand-200"
            >
              Ripristina predefinito (.env)
            </button>
          </div>
        </div>

        {/* Per-agent models */}
        <div>
          <h4 className="text-sm font-semibold text-slate-200">Modello per agente</h4>
          <p className="mt-0.5 text-xs text-slate-500">
            Scegli un modello dedicato a un singolo agente, oppure lascia «Predefinito» per usare quello sopra.
          </p>
          <div className="mt-3 space-y-4">
            {AGENT_ORDER.map((agent) => {
              const ref = perAgent[agent];
              return (
                <div key={agent} className="grid grid-cols-1 gap-2 sm:grid-cols-[10rem_1fr_1fr] sm:items-start sm:gap-3">
                  <div className="pt-2 text-sm font-medium text-slate-200">{AGENT_LABELS_IT[agent]}</div>
                  <div>
                    <label htmlFor={`llm-${agent}-provider`} className="sr-only">
                      Provider per {AGENT_LABELS_IT[agent]}
                    </label>
                    {renderProviderSelect({
                      id: `llm-${agent}-provider`,
                      value: ref?.provider ?? "",
                      onChange: (provider) => changeAgentProvider(agent, provider),
                      emptyLabel: "Predefinito",
                    })}
                  </div>
                  <div>
                    <label htmlFor={`llm-${agent}-model`} className="sr-only">
                      Modello per {AGENT_LABELS_IT[agent]}
                    </label>
                    {renderModelField({
                      id: `llm-${agent}-model`,
                      provider: ref?.provider ?? "",
                      value: ref?.model ?? "",
                      onChange: (model) => changeAgentModel(agent, model),
                    })}
                    {!ref ? (
                      <p className="mt-1 text-xs text-slate-500">Usa il modello predefinito ({effectiveDefaultText}).</p>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={handleSave}
            disabled={saving}
            className="rounded-md bg-brand-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-brand-500 disabled:opacity-60"
          >
            {saving ? <Spinner size="sm" /> : "Salva modelli"}
          </button>
          {saved ? <span className="text-sm font-medium text-gain-light">Modelli salvati</span> : null}
          {saveError ? <span className="text-sm text-loss-light">{saveError}</span> : null}
        </div>
      </div>
    );
  }

  return <Card title={title}>{body}</Card>;
}
