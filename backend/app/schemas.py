"""All Pydantic schemas and enums for the API layer (blueprint section 3).

Field names mirror the ORM columns (``from_attributes=True`` where a schema
maps an ORM object); JSON ``Text`` columns are parsed into ``dict``/``list``
fields by the routers before instantiation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Sizing(str, Enum):
    ALL_IN = "ALL_IN"
    DCA = "DCA"
    PARTIAL = "PARTIAL"
    WAIT = "WAIT"


class Verdict(str, Enum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    VETO = "VETO"


class RiskProfile(str, Enum):
    PRUDENTE = "prudente"
    BILANCIATO = "bilanciato"
    DINAMICO = "dinamico"


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Stance(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #


class SymbolCreate(BaseModel):
    ticker: str


class FavoriteToggle(BaseModel):
    is_favorite: bool


class SymbolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    name: str
    exchange: str | None
    currency: str
    asset_type: str
    is_favorite: bool
    is_active: bool
    created_at: datetime


class RecommendationBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: Action
    sizing_strategy: Sizing
    confidence: float
    created_at: datetime


class SymbolWithQuote(SymbolOut):
    last_price: float | None = None
    change_pct_1d: float | None = None
    last_recommendation: RecommendationBrief | None = None


class SymbolSearchResult(BaseModel):
    ticker: str
    name: str
    exchange: str | None = None
    asset_type: str = "EQUITY"
    already_added: bool = False


class SymbolOverviewOut(BaseModel):
    """Full quote + fundamentals snapshot for the symbol detail page.

    Price fields are derived from the stored 1d OHLCV history (refreshed first
    if stale); fundamentals come from a cached market-data pull. Every derived
    field is ``None`` when the underlying data is missing or too short.
    """

    ticker: str
    name: str
    exchange: str | None
    currency: str
    asset_type: str
    last_price: float | None
    change_1d_abs: float | None
    change_pct_1d: float | None
    open: float | None
    day_high: float | None
    day_low: float | None
    prev_close: float | None
    volume: float | None
    avg_volume_30d: float | None
    week52_high: float | None
    week52_low: float | None
    market_cap: float | None
    pe: float | None
    forward_pe: float | None
    eps: float | None
    dividend_yield: float | None
    beta: float | None
    analyst_target: float | None
    sector: str | None
    industry: str | None
    updated_at: datetime | None


# --------------------------------------------------------------------------- #
# Prices / indicators
# --------------------------------------------------------------------------- #


class PricePoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndicatorSeries(BaseModel):
    """Indicator series aligned with ``PriceHistoryOut.points`` (None in warm-up)."""

    sma20: list[float | None]
    sma50: list[float | None]
    sma200: list[float | None]
    ema12: list[float | None]
    ema26: list[float | None]
    rsi14: list[float | None]
    macd: list[float | None]
    macd_signal: list[float | None]
    macd_hist: list[float | None]
    bb_upper: list[float | None]
    bb_mid: list[float | None]
    bb_lower: list[float | None]
    atr14: list[float | None]


class PriceHistoryOut(BaseModel):
    ticker: str
    interval: str
    points: list[PricePoint]
    indicators: IndicatorSeries | None = None


# --------------------------------------------------------------------------- #
# Analysis / runs
# --------------------------------------------------------------------------- #


class AnalyzeRequest(BaseModel):
    trigger: Literal["MANUAL"] = "MANUAL"


class AnalyzeAccepted(BaseModel):
    run_id: int
    status: RunStatus
    message_it: str


class AnalysisOut(BaseModel):
    id: int
    agent_name: str
    status: str
    signal: float | None
    confidence: float | None
    stance: Stance | None
    summary_it: str
    output: dict
    created_at: datetime


# --------------------------------------------------------------------------- #
# Recommendations
# --------------------------------------------------------------------------- #


class PolicyCheck(BaseModel):
    rule: str
    passed: bool
    detail_it: str


class RecommendationOut(BaseModel):
    id: int
    run_id: int
    symbol_id: int
    ticker: str
    action: Action
    sizing_strategy: Sizing
    confidence: float
    allocation_pct: float
    allocation_amount: float
    dca_tranches: int
    entry_price: float | None
    stop_loss_price: float | None
    take_profit_price: float | None
    horizon_days: int
    estimated_profit_pct: float | None
    estimated_profit_amount: float | None
    rationale_it: str
    validator_verdict: Verdict
    validator_notes_it: str
    policy_checks: list[PolicyCheck]
    policy_overridden: bool
    original_action: Action | None
    original_sizing: Sizing | None
    evaluated: bool
    realized_return_7d: float | None
    outcome_score: float | None
    created_at: datetime


class RecommendationListOut(BaseModel):
    items: list[RecommendationOut]
    total: int


class AnalysisRunOut(BaseModel):
    id: int
    symbol_id: int
    ticker: str
    status: RunStatus
    trigger: str
    llm_provider_used: str | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    analyses: list[AnalysisOut] = []
    recommendation: RecommendationOut | None = None


# --------------------------------------------------------------------------- #
# Evaluations / feedback
# --------------------------------------------------------------------------- #


class AgentMetrics(BaseModel):
    agent_name: str
    accuracy: float | None
    avg_signal_error: float | None
    n_samples: int
    trend: list[float] = []


class EvaluationOut(BaseModel):
    id: int
    period_start: datetime
    period_end: datetime
    status: str
    total_recommendations: int
    evaluated_count: int
    accuracy_overall: float | None
    avg_realized_return_pct: float | None
    hypothetical_pnl_pct: float | None
    best_symbol: str | None
    worst_symbol: str | None
    per_agent: list[AgentMetrics]
    report_it: str
    created_at: datetime


class AgentFeedbackOut(BaseModel):
    id: int
    evaluation_id: int
    agent_name: str
    accuracy: float | None
    lessons: list[str]
    lessons_it: str
    is_active: bool
    created_at: datetime


# --------------------------------------------------------------------------- #
# Settings / dashboard / health
# --------------------------------------------------------------------------- #


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_budget: float
    budget_currency: str
    risk_profile: RiskProfile
    cash_reserve_pct: float
    max_position_pct: float
    favorites_analysis_interval_hours: int
    others_analysis_interval_hours: int
    updated_at: datetime


class SettingsUpdate(BaseModel):
    total_budget: float | None = Field(None, gt=0)
    risk_profile: RiskProfile | None = None
    cash_reserve_pct: float | None = Field(None, ge=10, le=80)
    max_position_pct: float | None = Field(None, ge=1, le=50)
    favorites_analysis_interval_hours: int | None = Field(None, ge=1, le=24)
    others_analysis_interval_hours: int | None = Field(None, ge=4, le=168)


class ProviderStatus(BaseModel):
    provider: str
    configured: bool
    model: str
    is_primary: bool


class HealthOut(BaseModel):
    status: str
    db_ok: bool
    scheduler_running: bool
    providers: list[ProviderStatus]


class DashboardSummary(BaseModel):
    favorites: list[SymbolWithQuote]
    others: list[SymbolWithQuote]
    last_evaluation: EvaluationOut | None
    pending_runs: int
    budget: SettingsOut
    market_open: bool
    disclaimer_it: str


# --------------------------------------------------------------------------- #
# Universe ("Universo titoli" — Mercato page)
# --------------------------------------------------------------------------- #


class UniverseItemOut(BaseModel):
    """One curated-universe row plus the caller's monitoring state for it."""

    model_config = ConfigDict(from_attributes=True)

    ticker: str
    name: str
    exchange: str | None
    country: str
    sector: str
    currency: str
    fame_rank: int
    market_cap_bn: float | None
    last_price: float | None
    change_pct_1d: float | None
    change_pct_30d: float | None
    volatility_30d_pct: float | None
    reliability_score: float | None
    composite_score: float | None
    monitored: bool = False
    is_favorite: bool = False
    symbol_id: int | None = None
    updated_at: datetime | None


class UniversePageOut(BaseModel):
    items: list[UniverseItemOut]
    total: int
    page: int
    page_size: int
    refreshing: bool
    last_refresh: datetime | None


# --------------------------------------------------------------------------- #
# LLM model selection (per provider AND per actor)
# --------------------------------------------------------------------------- #


class LlmModelRef(BaseModel):
    """A concrete ``(provider, model)`` selection."""

    provider: str
    model: str


class LlmProviderInfo(BaseModel):
    """Configuration state of one LLM provider for the config screen."""

    provider: str
    configured: bool
    is_primary: bool
    env_default_model: str


class LlmModelInfo(BaseModel):
    """One selectable model as offered by a provider's models listing."""

    id: str
    label: str


class LlmModelsOut(BaseModel):
    """Response of ``GET /api/llm/models`` — the models a provider offers."""

    provider: str
    models: list[LlmModelInfo]
    fetched_at: datetime


class LlmConfigOut(BaseModel):
    """Response of ``GET /api/llm/config`` — current provider + preference state.

    ``default`` and each ``per_agent`` entry are ``None`` when unset (the actor
    then uses the env-configured provider/model).
    """

    providers: list[LlmProviderInfo]
    default: LlmModelRef | None
    per_agent: dict[str, LlmModelRef | None]


class LlmConfigUpdate(BaseModel):
    """Body of ``PUT /api/llm/config`` — the FULL desired preference state.

    The body always carries the complete desired state: ``default`` and all six
    ``per_agent`` entries. For every slot, a present ``{provider, model}`` upserts
    that row and ``null`` unsets it. A slot omitted from the body is treated as
    ``null`` (unset), so the persisted state always matches exactly what the body
    describes.
    """

    default: LlmModelRef | None = None
    per_agent: dict[str, LlmModelRef | None] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# LLM provider settings (provider choice + API keys, managed from Settings)
# --------------------------------------------------------------------------- #


class LlmProviderState(BaseModel):
    """The effective state of one provider for the Settings screen.

    ``source`` is ``"app"`` when the effective key/URL comes from the DB (saved in
    the app), ``"env"`` when it comes only from the ``.env`` fallback, or ``null``
    when nothing is configured anywhere. ``key_masked`` never contains the full key
    and is ``null`` for keyless providers (Ollama). ``base_url`` is the effective
    Ollama base URL (``null`` for the cloud providers).
    """

    provider: str
    configured: bool
    source: Literal["app", "env"] | None
    key_masked: str | None
    default_model: str
    base_url: str | None = None


class LlmProvidersOut(BaseModel):
    """Response of ``GET``/``PUT`` ``/api/llm/providers`` — effective provider config."""

    primary_provider: str
    fallback_enabled: bool
    providers: list[LlmProviderState]


class LlmProvidersUpdate(BaseModel):
    """Body of ``PUT /api/llm/providers`` — partial update of the provider config.

    A field ABSENT from the body leaves that setting unchanged. For the API-key
    fields, an explicit ``null`` deletes the stored key (env fallback, if any,
    remains) and a non-empty string stores the trimmed value; an empty/whitespace
    string is rejected. ``ollama_base_url`` follows the same absent/null/set
    semantics (null deletes the DB override, a non-empty value must start with
    ``http://`` or ``https://``). ``primary_provider`` (when present and non-null)
    must be one of ``openrouter`` / ``gemini`` / ``ollama``.
    """

    primary_provider: str | None = None
    fallback_enabled: bool | None = None
    openrouter_api_key: str | None = None
    gemini_api_key: str | None = None
    ollama_base_url: str | None = None


class LlmProviderTestRequest(BaseModel):
    """Body of ``POST /api/llm/providers/test`` — which provider to validate."""

    provider: str


class LlmProviderTestResult(BaseModel):
    """Result of a provider connectivity/key test (Italian user-facing detail)."""

    ok: bool
    detail_it: str


# --------------------------------------------------------------------------- #
# Ollama local models (installed + downloadable catalog + in-app download)
# --------------------------------------------------------------------------- #


class OllamaLibraryInstalled(BaseModel):
    """One model currently installed on the local Ollama server."""

    id: str
    label: str
    size_bytes: int | None = None


class OllamaCatalogEntry(BaseModel):
    """One curated, downloadable Ollama model for the Settings screen."""

    id: str
    label: str
    description_it: str
    size_hint: str
    installed: bool


class OllamaLibraryOut(BaseModel):
    """Response of ``GET /api/llm/ollama/library`` — installed + downloadable models."""

    installed: list[OllamaLibraryInstalled]
    catalog: list[OllamaCatalogEntry]


class OllamaPullRequest(BaseModel):
    """Body of ``POST /api/llm/ollama/pull`` — the model to download."""

    model: str


class OllamaPullStatusOut(BaseModel):
    """Progress of an in-app Ollama model download.

    ``status`` is ``idle`` (never pulled in this process), ``pulling``, ``success``
    or ``error``; the byte counters and ``percent`` are ``null`` until Ollama
    reports them. ``detail_it`` is an Italian human-readable status line.
    """

    model: str
    status: Literal["idle", "pulling", "success", "error"]
    completed_bytes: int | None = None
    total_bytes: int | None = None
    percent: float | None = None
    detail_it: str
