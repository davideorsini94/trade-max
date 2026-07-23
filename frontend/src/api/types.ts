/**
 * TypeScript types mirroring backend/app/schemas.py (Pydantic v2) literally.
 * snake_case field names preserved; datetimes are ISO strings (UTC-naive on the
 * backend, treated as UTC by the frontend — see BLUEPRINT.md friction point #1).
 *
 * DO NOT rename fields here without updating the backend contract in
 * docs/BLUEPRINT.md section 3 — this file is a shared contract surface.
 */

// --- Enums ---

export type Action = "BUY" | "SELL" | "HOLD";
export type Sizing = "ALL_IN" | "DCA" | "PARTIAL" | "WAIT";
export type Verdict = "APPROVE" | "REVISE" | "VETO";
export type RiskProfile = "prudente" | "bilanciato" | "dinamico";
export type RunStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";
export type Stance = "BULLISH" | "BEARISH" | "NEUTRAL";

export const ACTIONS: readonly Action[] = ["BUY", "SELL", "HOLD"];
export const SIZINGS: readonly Sizing[] = ["ALL_IN", "DCA", "PARTIAL", "WAIT"];
export const RISK_PROFILES: readonly RiskProfile[] = ["prudente", "bilanciato", "dinamico"];

// --- Symbols ---

export interface SymbolCreate {
  ticker: string;
}

export interface FavoriteToggle {
  is_favorite: boolean;
}

export interface SymbolOut {
  id: number;
  ticker: string;
  name: string;
  exchange: string | null;
  currency: string;
  asset_type: string;
  is_favorite: boolean;
  is_active: boolean;
  created_at: string;
}

export interface RecommendationBrief {
  id: number;
  action: Action;
  sizing_strategy: Sizing;
  confidence: number;
  created_at: string;
}

export interface SymbolWithQuote extends SymbolOut {
  last_price: number | null;
  change_pct_1d: number | null;
  last_recommendation: RecommendationBrief | null;
}

export interface SymbolSearchResult {
  ticker: string;
  name: string;
  exchange: string | null;
  asset_type: string;
  already_added: boolean;
}

export interface SymbolOverviewOut {
  ticker: string;
  name: string;
  exchange: string | null;
  currency: string;
  asset_type: string;
  last_price: number | null;
  change_1d_abs: number | null;
  change_pct_1d: number | null;
  open: number | null;
  day_high: number | null;
  day_low: number | null;
  prev_close: number | null;
  volume: number | null;
  avg_volume_30d: number | null;
  week52_high: number | null;
  week52_low: number | null;
  market_cap: number | null;
  pe: number | null;
  forward_pe: number | null;
  eps: number | null;
  dividend_yield: number | null;
  beta: number | null;
  analyst_target: number | null;
  sector: string | null;
  industry: string | null;
  updated_at: string | null;
}

// --- Universe (Mercato) ---

/** Body for POST /api/universe/isin. The 12-char ISIN is trimmed+uppercased server-side. */
export interface IsinResolveRequest {
  isin: string;
}

export interface UniverseItemOut {
  ticker: string;
  name: string;
  exchange: string | null;
  country: string;
  sector: string;
  currency: string;
  fame_rank: number; // 1..5, 5 = più famosa
  market_cap_bn: number | null;
  last_price: number | null;
  change_pct_1d: number | null;
  change_pct_30d: number | null;
  volatility_30d_pct: number | null;
  reliability_score: number | null; // 0..100
  composite_score: number | null;
  monitored: boolean;
  is_favorite: boolean;
  symbol_id: number | null;
  updated_at: string | null;
}

export interface UniversePageOut {
  items: UniverseItemOut[];
  total: number;
  page: number;
  page_size: number;
  refreshing: boolean;
  last_refresh: string | null;
}

// --- Prices / indicators ---

export interface PricePoint {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface IndicatorSeries {
  sma20: Array<number | null>;
  sma50: Array<number | null>;
  sma200: Array<number | null>;
  ema12: Array<number | null>;
  ema26: Array<number | null>;
  rsi14: Array<number | null>;
  macd: Array<number | null>;
  macd_signal: Array<number | null>;
  macd_hist: Array<number | null>;
  bb_upper: Array<number | null>;
  bb_mid: Array<number | null>;
  bb_lower: Array<number | null>;
  atr14: Array<number | null>;
}

export interface PriceHistoryOut {
  ticker: string;
  interval: string;
  points: PricePoint[];
  indicators: IndicatorSeries | null;
}

// --- Analysis / runs ---

export interface AnalyzeRequest {
  trigger: "MANUAL";
}

export interface AnalyzeAccepted {
  run_id: number;
  status: RunStatus;
  message_it: string;
}

export interface AnalysisOut {
  id: number;
  agent_name: string;
  status: string;
  signal: number | null;
  confidence: number | null;
  stance: Stance | null;
  summary_it: string;
  output: Record<string, unknown>;
  created_at: string;
}

export interface AnalysisRunOut {
  id: number;
  symbol_id: number;
  ticker: string;
  status: RunStatus;
  trigger: string;
  llm_provider_used: string | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  analyses: AnalysisOut[];
  recommendation: RecommendationOut | null;
}

// --- Recommendations ---

export interface PolicyCheck {
  rule: string;
  passed: boolean;
  detail_it: string;
}

export interface RecommendationOut {
  id: number;
  run_id: number;
  symbol_id: number;
  ticker: string;
  action: Action;
  sizing_strategy: Sizing;
  confidence: number;
  allocation_pct: number;
  allocation_amount: number;
  dca_tranches: number;
  entry_price: number | null;
  stop_loss_price: number | null;
  take_profit_price: number | null;
  horizon_days: number;
  estimated_profit_pct: number | null;
  estimated_profit_amount: number | null;
  rationale_it: string;
  /** Consiglio per chi NON possiede ancora il titolo; null per raccomandazioni vecchie. */
  advice_new_investor_it: string | null;
  /** Consiglio per chi possiede GIÀ il titolo; null per raccomandazioni vecchie. */
  advice_holder_it: string | null;
  validator_verdict: Verdict;
  validator_notes_it: string;
  policy_checks: PolicyCheck[];
  policy_overridden: boolean;
  original_action: Action | null;
  original_sizing: Sizing | null;
  evaluated: boolean;
  realized_return_7d: number | null;
  outcome_score: number | null;
  created_at: string;
}

export interface RecommendationListOut {
  items: RecommendationOut[];
  total: number;
}

// --- Evaluations / feedback ---

export interface AgentMetrics {
  agent_name: string;
  accuracy: number | null;
  avg_signal_error: number | null;
  n_samples: number;
  trend: number[];
  // Horizon-aware pass: same shape as accuracy/n_samples above, but scored once
  // each recommendation matures at its OWN horizon_days (not the fixed 7 days).
  // Stays null/0 for weeks after this shipped, until recommendations mature.
  accuracy_final?: number | null;
  n_samples_final?: number;
}

export interface EvaluationOut {
  id: number;
  period_start: string;
  period_end: string;
  status: string;
  total_recommendations: number;
  evaluated_count: number;
  accuracy_overall: number | null;
  avg_realized_return_pct: number | null;
  hypothetical_pnl_pct: number | null;
  best_symbol: string | null;
  worst_symbol: string | null;
  per_agent: AgentMetrics[];
  report_it: string;
  created_at: string;
}

export interface PendingEvaluationOut {
  pending_count: number;
  ready_count: number;
  next_evaluable_at: string | null;
}

export interface AgentFeedbackOut {
  id: number;
  evaluation_id: number;
  agent_name: string;
  accuracy: number | null;
  lessons: string[];
  lessons_it: string;
  is_active: boolean;
  created_at: string;
}

// --- Settings / dashboard / health ---

export interface SettingsOut {
  total_budget: number;
  budget_currency: string;
  risk_profile: RiskProfile;
  cash_reserve_pct: number;
  max_position_pct: number;
  favorites_analysis_interval_hours: number;
  others_analysis_interval_hours: number;
  updated_at: string;
}

export interface SettingsUpdate {
  total_budget?: number;
  risk_profile?: RiskProfile;
  cash_reserve_pct?: number;
  max_position_pct?: number;
  favorites_analysis_interval_hours?: number;
  others_analysis_interval_hours?: number;
}

export interface ProviderStatus {
  provider: string;
  configured: boolean;
  model: string;
  is_primary: boolean;
}

export interface HealthOut {
  status: string;
  db_ok: boolean;
  scheduler_running: boolean;
  providers: ProviderStatus[];
}

export interface DashboardSummary {
  favorites: SymbolWithQuote[];
  others: SymbolWithQuote[];
  last_evaluation: EvaluationOut | null;
  pending_runs: number;
  budget: SettingsOut;
  market_open: boolean;
  disclaimer_it: string;
}

// --- LLM models config ---

export interface LlmModelOption {
  id: string;
  label: string;
}

export interface LlmModelsOut {
  provider: string;
  models: LlmModelOption[];
  fetched_at: string;
}

/** A concrete provider+model selection. */
export interface LlmModelRef {
  provider: string;
  model: string;
}

export interface LlmProviderInfo {
  provider: string;
  configured: boolean;
  is_primary: boolean;
  env_default_model: string;
}

/**
 * Per-actor model overrides. Each key mirrors an agent name (lib/labels
 * AGENT_ORDER); null means "usa il modello predefinito".
 */
export interface LlmPerAgentConfig {
  technical: LlmModelRef | null;
  fundamentals: LlmModelRef | null;
  macro_news: LlmModelRef | null;
  corporate_news: LlmModelRef | null;
  sentiment: LlmModelRef | null;
  synthesizer: LlmModelRef | null;
  validator: LlmModelRef | null;
}

export interface LlmConfigOut {
  providers: LlmProviderInfo[];
  default: LlmModelRef | null;
  per_agent: LlmPerAgentConfig;
}

/** Body for PUT /api/llm/config. null = torna al predefinito. */
export interface LlmConfigUpdate {
  default: LlmModelRef | null;
  per_agent: LlmPerAgentConfig;
}

// --- LLM provider keys (Settings) ---

export type LlmProviderName = "openrouter" | "gemini" | "ollama";

/**
 * Configured state of a single LLM provider.
 * source: "app" when the key/URL comes from the DB, "env" when only from .env,
 * null when absent. key_masked never contains the full key and is null for the
 * local Ollama provider (which has no API key). base_url is the effective Ollama
 * endpoint (null for the cloud providers).
 */
export interface LlmProviderKeyInfo {
  provider: LlmProviderName;
  configured: boolean;
  source: "app" | "env" | null;
  key_masked: string | null;
  default_model: string;
  base_url: string | null;
}

export interface LlmProvidersOut {
  primary_provider: LlmProviderName;
  fallback_enabled: boolean;
  providers: LlmProviderKeyInfo[];
}

/**
 * Body for PUT /api/llm/providers. Every field is optional:
 * absent = unchanged; a key set to null = delete the stored key (env remains as
 * fallback if present); a non-empty string = store the trimmed key.
 */
export interface LlmProvidersUpdate {
  primary_provider?: LlmProviderName;
  fallback_enabled?: boolean;
  openrouter_api_key?: string | null;
  gemini_api_key?: string | null;
  /** Ollama endpoint: absent = unchanged, null = delete DB override (env fallback), non-empty = store trimmed. */
  ollama_base_url?: string | null;
}

export interface LlmProviderTestOut {
  ok: boolean;
  detail_it: string;
}

// --- Ollama (local provider) library & downloads ---

/** A model already installed locally in Ollama (from GET {base}/api/tags). */
export interface OllamaInstalledModel {
  id: string;
  label: string;
  size_bytes: number | null;
}

/** A curated, downloadable model from the static catalog. */
export interface OllamaCatalogModel {
  id: string;
  label: string;
  description_it: string;
  size_hint: string;
  installed: boolean;
}

export interface OllamaLibraryOut {
  installed: OllamaInstalledModel[];
  catalog: OllamaCatalogModel[];
}

export interface OllamaPullStatusOut {
  model: string;
  status: "idle" | "pulling" | "success" | "error";
  completed_bytes: number | null;
  total_bytes: number | null;
  percent: number | null;
  detail_it: string;
}
