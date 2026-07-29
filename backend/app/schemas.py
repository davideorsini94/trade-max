"""All Pydantic schemas and enums for the API layer (blueprint section 3).

Field names mirror the ORM columns (``from_attributes=True`` where a schema
maps an ORM object); JSON ``Text`` columns are parsed into ``dict``/``list``
fields by the routers before instantiation.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    # Two audience-specific notes distilled by the synthesizer (persisted inside
    # ``synthesizer_json``): guidance for someone about to invest vs. someone who
    # already holds the shares. ``None`` for older recommendations that predate them.
    advice_new_investor_it: str | None = None
    advice_holder_it: str | None = None
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
    # Horizon-aware pass (blueprint §7 addendum, part 2 of 4): same shape as
    # `accuracy`/`n_samples` above but scored once each recommendation reaches
    # its OWN horizon_days, relative to the benchmark for BUY/SELL. None/0 until
    # recommendations start maturing at their horizon (weeks after this shipped).
    accuracy_final: float | None = None
    n_samples_final: int = 0


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
    # Cumulative per-feature validation (blueprint §7 addendum, part 3 of 4):
    # None until compute_feature_stats has run at least once and found a
    # non-empty cohort. Raw pass-through of app.evaluation.features' shape; no
    # UI consumes it yet.
    feature_stats: dict | None = None


class PendingEvaluationOut(BaseModel):
    """Not-yet-scoreable recommendations: makes the learning loop visible

    before the first evaluation has enough 7-day-old data to run on.
    """

    pending_count: int
    ready_count: int
    #: Calendar-mature (7+ days old) but not yet scoreable, because the close for
    #: the target session is still missing — e.g. the 7-day mark landed on a
    #: weekend/holiday. These are NOT counted in ``ready_count``, which promises
    #: only what the next evaluation can actually score.
    awaiting_price_count: int = 0
    #: Scoreable by the SECOND, horizon-aware pass (a recommendation already
    #: scored at 7 days can still be awaiting its own horizon_days). A run does
    #: real work when this OR ``ready_count`` is non-zero.
    horizon_ready_count: int = 0
    next_evaluable_at: datetime | None


class PerformanceSummaryOut(BaseModel):
    """Rolling-window performance, the honest replacement for a single batch.

    See ``app.evaluation.summary``: the accuracy of ONE evaluation batch swung
    between 0% and 100% purely because batches hold 1-12 samples. ``accuracy`` is
    null unless the cohort clears both honesty floors (``min_n`` samples AND
    ``min_symbols`` distinct symbols); ``ci_low``/``ci_high`` and
    ``avg_outcome_score`` are present from the first sample because they degrade
    honestly instead of faking precision.
    """

    status: Literal["ok", "dati_insufficienti"]
    window_days: int
    metric_version: int
    #: Deduplicated samples (one per symbol per ISO week) and the raw count.
    n: int
    n_raw: int
    n_symbols: int
    k_correct: int
    accuracy: float | None
    ci_low: float | None
    ci_high: float | None
    #: Mean outcome_score in [-1, +1]: threshold-free, so unaffected by the ~38%
    #: of samples that sit near the correct/incorrect boundary.
    avg_outcome_score: float | None
    action_mix: dict[str, int]
    hold_only: bool
    min_n: int
    min_symbols: int
    per_agent: dict[str, dict] = {}


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


class UniverseIsinLookup(BaseModel):
    """Body of ``POST /api/universe/isin`` — the ISIN to resolve and persist."""

    isin: str


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


# --------------------------------------------------------------------------- #
# Paper-trading transactions / positions (blueprint §5.4 addendum)
# --------------------------------------------------------------------------- #
# FICTITIOUS diary of what the user says they spent/received and when — never
# a real order. Amounts are always in the SYMBOL's own currency (no FX layer
# in this app); share counts are estimated from session closes, never asked.


class TransactionCreate(BaseModel):
    """Body of ``POST /api/symbols/{symbol_id}/transactions``.

    ``amount`` on a BUY is the total cash out (fee included); on a SELL it is
    the gross sale proceeds (the fee is subtracted from the net separately).
    ``executed_at`` is a plain date — backdating is explicitly allowed (so the
    analysis can be done retroactively), future dates are rejected.
    """

    side: Literal["BUY", "SELL"]
    amount: float = Field(gt=0)
    fee_pct: float = Field(0.0, ge=0, le=100)
    executed_at: date
    note: str | None = Field(None, max_length=200)

    @field_validator("executed_at")
    @classmethod
    def _not_in_future(cls, v: date) -> date:
        if v > datetime.utcnow().date():
            raise ValueError("La data della transazione non può essere nel futuro.")
        return v


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol_id: int
    side: Literal["BUY", "SELL"]
    amount: float
    fee_pct: float
    currency: str
    executed_at: datetime
    price_ref: float | None
    quantity_est: float | None
    note: str | None
    created_at: datetime


class PositionSummaryOut(BaseModel):
    """Aggregate position for one symbol, derived from all its transactions.

    Every field marked "est" is an ESTIMATE from session closes; it is null
    (never a fabricated number) whenever ``estimates_complete`` is false.
    """

    status: Literal["OPEN", "CLOSED", "UNKNOWN"]
    currency: str
    n_transactions: int
    invested_total: float
    proceeds_net: float
    realized_cashflow: float
    estimates_complete: bool
    est_shares_open: float | None
    avg_cost_est: float | None
    last_close: float | None
    current_value_est: float | None
    total_pnl_est: float | None
    total_pnl_pct_est: float | None
    first_buy_at: datetime | None
    last_tx_at: datetime | None


class TransactionListOut(BaseModel):
    items: list[TransactionOut]
    position: PositionSummaryOut | None
