import { useEffect, useState, type FormEvent } from "react";
import { apiGet, apiPut, errorMessage } from "../api/client";
import { RISK_PROFILES } from "../api/types";
import type { HealthOut, RiskProfile, SettingsOut, SettingsUpdate } from "../api/types";
import { useApi } from "../hooks/useApi";
import Card from "../components/common/Card";
import Spinner from "../components/common/Spinner";
import ErrorBox from "../components/common/ErrorBox";
import Badge from "../components/common/Badge";
import LlmModelsCard from "../components/settings/LlmModelsCard";
import { RISK_PROFILE_DESCRIPTIONS_IT, RISK_PROFILE_LABELS_IT } from "../lib/labels";
import { gloss } from "../lib/glossary";

interface SettingsFormState {
  total_budget: string;
  risk_profile: RiskProfile;
  cash_reserve_pct: string;
  max_position_pct: string;
  favorites_analysis_interval_hours: string;
  others_analysis_interval_hours: string;
}

function toFormState(settings: SettingsOut): SettingsFormState {
  return {
    total_budget: String(settings.total_budget),
    risk_profile: settings.risk_profile,
    cash_reserve_pct: String(settings.cash_reserve_pct),
    max_position_pct: String(settings.max_position_pct),
    favorites_analysis_interval_hours: String(settings.favorites_analysis_interval_hours),
    others_analysis_interval_hours: String(settings.others_analysis_interval_hours),
  };
}

const PROVIDER_LABELS: Record<string, string> = {
  openrouter: "OpenRouter",
  gemini: "Gemini",
};

export default function SettingsPage() {
  const settingsQuery = useApi<SettingsOut>(() => apiGet<SettingsOut>("/settings"), []);
  const healthQuery = useApi<HealthOut>(() => apiGet<HealthOut>("/health"), []);

  const [form, setForm] = useState<SettingsFormState | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    if (settingsQuery.data) {
      setForm(toFormState(settingsQuery.data));
    }
  }, [settingsQuery.data]);

  function updateField<K extends keyof SettingsFormState>(key: K, value: SettingsFormState[K]) {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
    setSavedAt(null);
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!form) return;
    setSaving(true);
    setSaveError(null);
    try {
      const payload: SettingsUpdate = {
        total_budget: Number(form.total_budget),
        risk_profile: form.risk_profile,
        cash_reserve_pct: Number(form.cash_reserve_pct),
        max_position_pct: Number(form.max_position_pct),
        favorites_analysis_interval_hours: Math.round(Number(form.favorites_analysis_interval_hours)),
        others_analysis_interval_hours: Math.round(Number(form.others_analysis_interval_hours)),
      };
      const updated = await apiPut<SettingsOut>("/settings", payload);
      setForm(toFormState(updated));
      setSavedAt(Date.now());
    } catch (err) {
      setSaveError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  if (settingsQuery.loading && !form) {
    return (
      <div className="flex justify-center py-16">
        <Spinner size="lg" label="Caricamento impostazioni…" />
      </div>
    );
  }

  if (settingsQuery.error && !form) {
    return <ErrorBox message={settingsQuery.error} onRetry={settingsQuery.refetch} />;
  }

  if (!form) {
    return null;
  }

  return (
    <div className="space-y-8">
      <h1 className="text-xl font-bold text-slate-50">Impostazioni</h1>

      <Card title="Parametri di investimento">
        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label htmlFor="total_budget" className="mb-1 block text-sm font-medium text-slate-300">
              Budget totale (€)
            </label>
            <input
              id="total_budget"
              type="number"
              min={0}
              step="0.01"
              value={form.total_budget}
              onChange={(e) => updateField("total_budget", e.target.value)}
              className="w-full max-w-xs rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
            />
            <p className="mt-1.5 max-w-md text-xs leading-relaxed text-slate-500">{gloss("budget")}</p>
          </div>

          <fieldset>
            <legend className="mb-2 text-sm font-medium text-slate-300">Profilo di rischio</legend>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              {RISK_PROFILES.map((profile) => (
                <label
                  key={profile}
                  className={`cursor-pointer rounded-lg border p-3 text-sm transition ${
                    form.risk_profile === profile
                      ? "border-brand-500 bg-brand-900/30"
                      : "border-slate-700 bg-slate-900/40 hover:border-slate-600"
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="risk_profile"
                      value={profile}
                      checked={form.risk_profile === profile}
                      onChange={() => updateField("risk_profile", profile)}
                      className="accent-brand-500"
                    />
                    <span className="font-semibold text-slate-100">{RISK_PROFILE_LABELS_IT[profile]}</span>
                  </div>
                  <p className="mt-1.5 text-xs leading-relaxed text-slate-400">{RISK_PROFILE_DESCRIPTIONS_IT[profile]}</p>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label htmlFor="cash_reserve_pct" className="mb-1 block text-sm font-medium text-slate-300">
                Riserva liquidità minima (%)
              </label>
              <input
                id="cash_reserve_pct"
                type="number"
                min={10}
                max={80}
                step="1"
                value={form.cash_reserve_pct}
                onChange={(e) => updateField("cash_reserve_pct", e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              />
              <p className="mt-1.5 text-xs leading-relaxed text-slate-500">{gloss("cash_reserve")}</p>
            </div>
            <div>
              <label htmlFor="max_position_pct" className="mb-1 block text-sm font-medium text-slate-300">
                Massimo % per posizione
              </label>
              <input
                id="max_position_pct"
                type="number"
                min={1}
                max={50}
                step="1"
                value={form.max_position_pct}
                onChange={(e) => updateField("max_position_pct", e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              />
              <p className="mt-1.5 text-xs leading-relaxed text-slate-500">{gloss("max_position")}</p>
            </div>
            <div>
              <label htmlFor="favorites_interval" className="mb-1 block text-sm font-medium text-slate-300">
                Intervallo analisi preferiti (ore)
              </label>
              <input
                id="favorites_interval"
                type="number"
                min={1}
                max={24}
                step="1"
                value={form.favorites_analysis_interval_hours}
                onChange={(e) => updateField("favorites_analysis_interval_hours", e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              />
              <p className="mt-1.5 text-xs leading-relaxed text-slate-500">
                Ogni quante ore i titoli preferiti (con la stella) vengono rianalizzati in automatico. Intervalli più brevi
                tengono le raccomandazioni più aggiornate.
              </p>
            </div>
            <div>
              <label htmlFor="others_interval" className="mb-1 block text-sm font-medium text-slate-300">
                Intervallo analisi altri titoli (ore)
              </label>
              <input
                id="others_interval"
                type="number"
                min={4}
                max={168}
                step="1"
                value={form.others_analysis_interval_hours}
                onChange={(e) => updateField("others_analysis_interval_hours", e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
              />
              <p className="mt-1.5 text-xs leading-relaxed text-slate-500">
                Ogni quante ore vengono rianalizzati i titoli non preferiti. Intervalli più lunghi riducono il carico ma
                aggiornano meno spesso le loro raccomandazioni.
              </p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={saving}
              className="rounded-md bg-brand-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-brand-500 disabled:opacity-60"
            >
              {saving ? <Spinner size="sm" /> : "Salva impostazioni"}
            </button>
            {savedAt ? <span className="text-sm font-medium text-gain-light">Impostazioni salvate</span> : null}
            {saveError ? <span className="text-sm text-loss-light">{saveError}</span> : null}
          </div>
        </form>
      </Card>

      <Card title="Stato provider LLM">
        {healthQuery.loading ? (
          <Spinner />
        ) : healthQuery.error ? (
          <ErrorBox message={healthQuery.error} onRetry={healthQuery.refetch} />
        ) : (
          <ul className="space-y-3">
            {(healthQuery.data?.providers ?? []).map((provider) => (
              <li
                key={provider.provider}
                className="flex items-center justify-between gap-3 rounded-lg border border-[var(--tm-border)] px-4 py-3"
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-slate-100">
                      {PROVIDER_LABELS[provider.provider] ?? provider.provider}
                    </span>
                    {provider.is_primary ? <Badge variant="info">Primario</Badge> : null}
                  </div>
                  <p className="mt-0.5 text-xs text-slate-500">Modello: {provider.model || "—"}</p>
                </div>
                <Badge variant={provider.configured ? "gain" : "loss"}>
                  {provider.configured ? "Configurato ✓" : "Non configurato"}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <LlmModelsCard />
    </div>
  );
}
