"""All SQLAlchemy ORM models (blueprint section 2).

Conventions
-----------
* SQLAlchemy 2.0 typed style (``Mapped`` / ``mapped_column``) on the shared
  ``Base`` from ``app.db``.
* Every datetime is **UTC naive** (``datetime.utcnow()``); the frontend treats
  ISO strings without timezone as UTC and converts for display.
* Structured payloads (agent outputs, policy checks, per-agent metrics,
  lessons) are stored as JSON strings in ``Text`` columns; parsing to
  dicts/lists happens in the API layer when building response schemas.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AppSettings(Base):
    """Single-row application settings (id=1, seeded at bootstrap)."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    total_budget: Mapped[float] = mapped_column(Float, nullable=False, default=10000.0)
    budget_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="EUR")
    risk_profile: Mapped[str] = mapped_column(String(16), nullable=False, default="prudente")
    cash_reserve_pct: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)
    max_position_pct: Mapped[float] = mapped_column(Float, nullable=False, default=15.0)
    # Once a day (was every 4h). Re-analysing the same favourite 2-3x per session
    # produced near-identical recommendations with overlapping outcome windows:
    # correlated samples that the evaluation counted as independent, which both
    # inflated apparent sample size and slowed down real statistical significance
    # (34 scored recommendations covered only 7 distinct symbols). Still
    # adjustable from Impostazioni (1-24h) for anyone who wants more reactivity.
    favorites_analysis_interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24
    )
    others_analysis_interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    exchange: Mapped[str | None] = mapped_column(String(40), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    asset_type: Mapped[str] = mapped_column(String(20), nullable=False, default="EQUITY")
    is_favorite: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False)
    favorite_added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    prices: Mapped[list["PriceHistory"]] = relationship(
        back_populates="symbol", cascade="all, delete-orphan", passive_deletes=True
    )
    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="symbol", cascade="all, delete-orphan", passive_deletes=True
    )
    runs: Mapped[list["AnalysisRun"]] = relationship(
        back_populates="symbol", cascade="all, delete-orphan", passive_deletes=True
    )
    transactions: Mapped[list["UserTransaction"]] = relationship(
        back_populates="symbol", cascade="all, delete-orphan", passive_deletes=True
    )
    sim_positions: Mapped[list["SimPosition"]] = relationship(
        back_populates="symbol", cascade="all, delete-orphan", passive_deletes=True
    )


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (
        UniqueConstraint("symbol_id", "ts", "interval", name="uq_price_point"),
        Index("ix_price_symbol_interval_ts", "symbol_id", "interval", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    interval: Mapped[str] = mapped_column(String(4), nullable=False, default="1d")
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    symbol: Mapped["Symbol"] = relationship(back_populates="prices")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="PENDING")
    trigger: Mapped[str] = mapped_column(String(12), nullable=False, default="SCHEDULED")
    llm_provider_used: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    symbol: Mapped["Symbol"] = relationship(back_populates="runs")
    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )
    recommendation: Mapped["Recommendation | None"] = relationship(
        back_populates="run", uselist=False, cascade="all, delete-orphan", passive_deletes=True
    )


class Analysis(Base):
    """One record per actor per run (analysts, synthesizer and validator)."""

    __tablename__ = "analyses"
    __table_args__ = (
        Index("ix_analyses_symbol_agent_created", "symbol_id", "agent_name", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="OK")
    signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    stance: Mapped[str | None] = mapped_column(String(10), nullable=True)
    summary_it: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    run: Mapped["AnalysisRun"] = relationship(back_populates="analyses")


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (Index("ix_reco_symbol_created", "symbol_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True, nullable=False
    )
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    sizing_strategy: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    allocation_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    allocation_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    dca_tranches: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    estimated_profit_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_profit_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale_it: Mapped[str] = mapped_column(Text, nullable=False, default="")
    synthesizer_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    validator_verdict: Mapped[str] = mapped_column(String(8), nullable=False)
    validator_notes_it: Mapped[str] = mapped_column(Text, nullable=False, default="")
    policy_checks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    policy_overridden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    original_action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    original_sizing: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Deterministic feature snapshot captured at run time (blueprint §7 addendum):
    # a versioned JSON blob (see app.evaluation.features.build_feature_snapshot)
    # the weekly evaluation uses to test whether the newer deterministic signals
    # are predictive. Nullable because recommendations created before this column
    # existed have — and will never be back-filled with — a snapshot.
    features_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluated: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False)
    realized_return_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluation_id: Mapped[int | None] = mapped_column(
        ForeignKey("evaluations.id", ondelete="SET NULL"), nullable=True
    )
    # Second, horizon-aware evaluation checkpoint (blueprint §7 addendum, part 2
    # of 4): scored once the recommendation reaches ITS OWN horizon_days (clamped
    # 7-60), not the fixed 7-day window above, and — for BUY/SELL when a
    # benchmark return is resolvable — relative to the benchmark instead of
    # absolute. Independent of ``evaluated``/``outcome_score`` above, which keep
    # their original 7-day meaning unchanged for backward compatibility.
    evaluated_h: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    realized_return_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark_return_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    excess_return_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_score_h: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "excess" (scored vs. benchmark_return_h) or "absolute" (HOLD, or no
    # resolvable benchmark) — documents which basis outcome_score_h used.
    outcome_basis_h: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, index=True, nullable=False, default=datetime.utcnow
    )

    symbol: Mapped["Symbol"] = relationship(back_populates="recommendations")
    run: Mapped["AnalysisRun"] = relationship(back_populates="recommendation")


class Evaluation(Base):
    """Weekly evaluation report over past recommendations."""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="COMPLETED")
    total_recommendations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evaluated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accuracy_overall: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_realized_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    hypothetical_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    worst_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    per_agent_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    report_it: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Cumulative per-feature validation (blueprint §7 addendum, part 3 of 4): see
    # app.evaluation.features.compute_feature_stats. Nullable because it's
    # computed fresh on every run and older Evaluation rows never had it.
    feature_stats_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    feedback: Mapped[list["AgentFeedback"]] = relationship(
        back_populates="evaluation", cascade="all, delete-orphan", passive_deletes=True
    )


class AgentFeedback(Base):
    """Per-agent lessons produced by an evaluation, injected into future prompts."""

    __tablename__ = "agent_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(
        ForeignKey("evaluations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_signal_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    lessons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    lessons_it: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    evaluation: Mapped["Evaluation"] = relationship(back_populates="feedback")


class NewsItem(Base):
    """Cache/audit of fetched news items, deduplicated by URL."""

    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(12), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(20), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    url: Mapped[str] = mapped_column(String(600), unique=True, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    # True when the feed carried NO usable date and ``published_at`` was filled
    # with the fetch time instead. ESMA's feed is the live example: it publishes
    # no ``pubDate`` at all, so without this flag every ESMA item looks brand new
    # and wins any recency-ordered selection — which is exactly how four ESMA
    # boilerplate items ("New Q&As available") crowded a genuine geopolitical
    # headline out of the macro payload. The timestamp is a necessary fallback
    # (the row would otherwise be unselectable), but it is INVENTED, so it is
    # declared here and never used as evidence of freshness by
    # ``app.data.news_select``.
    published_is_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class UniverseStat(Base):
    """Curated "Universo titoli" entry: static metadata for a well-known stock
    plus periodically-refreshed market stats and derived ranking scores.

    Backs the Mercato page ranking (fame / positive trend / value / reliability)
    from which the user adds symbols to monitoring/favorites. Market-derived
    fields are populated by ``app.data.universe.refresh_universe`` and may be
    ``None`` until the first successful refresh; the static fields (name,
    ``fame_rank``, ``market_cap_bn`` baseline, sector...) come from the curated
    ``app.data.universe.UNIVERSE`` list.
    """

    __tablename__ = "universe_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    exchange: Mapped[str | None] = mapped_column(String(40), nullable=True)
    country: Mapped[str] = mapped_column(String(4), nullable=False, default="US")
    sector: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    fame_rank: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    market_cap_bn: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility_30d_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    above_sma200: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reliability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LlmModelPref(Base):
    """Per-actor / default LLM provider+model override (blueprint: model selection).

    ``agent_name`` is the primary key: either the literal ``"default"`` (the
    desk-wide fallback) or one of the seven pipeline actor names (``technical``,
    ``fundamentals``, ``macro_news``, ``corporate_news``, ``sentiment``,
    ``synthesizer``, ``validator``). A missing row means "use the env-configured
    provider/model"; resolution (agent -> default -> env) lives in
    ``app.llm.prefs``.
    """

    __tablename__ = "llm_model_prefs"

    agent_name: Mapped[str] = mapped_column(String(30), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class LlmProviderSettings(Base):
    """Single-row (id=1) DB overrides for the effective LLM provider config.

    Manages, from the app's Settings page, the provider choice and the API keys
    that would otherwise live only in the ``.env`` file. Every column is nullable
    and, when non-null, overrides the corresponding env setting field by field; a
    null column falls back to env (``get_settings()``). Models are NOT stored here
    — they always come from env / the ``llm_model_prefs`` table. The merge lives
    in ``app.llm.runtime.get_effective``; API keys are secrets and are never
    logged.
    """

    __tablename__ = "llm_provider_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # null => use env LLM_PROVIDER; otherwise "openrouter" | "gemini" | "ollama".
    primary_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    openrouter_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    gemini_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # null => use env OLLAMA_BASE_URL. Not a secret (a local server URL); stored
    # here so the Ollama endpoint can be managed from the Settings page like the keys.
    ollama_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # null => use env LLM_FALLBACK_ENABLED.
    fallback_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class UserTransaction(Base):
    """Diario di trading FITTIZIO dell'utente (paper trading): nessun ordine
    reale viene mai eseguito. Registra quanto l'utente dichiara di aver
    speso/incassato e quando, così la pipeline può ragionare sul suo vero
    prezzo di carico invece che su testo generico.

    Semantica di ``amount`` (sempre nella valuta del titolo, di cui ``currency``
    è uno snapshot al momento dell'inserimento):
    * BUY : esborso TOTALE uscito dal conto, commissione inclusa.
    * SELL: controvalore LORDO della vendita; l'incasso netto è amount*(1-fee_pct/100).

    ``price_ref`` è il prezzo di chiusura della seduta usata per stimare le
    azioni (vedi ``app.engine.positions``); resta ``None`` — e con esso
    ``quantity_est`` — quando nessun prezzo è disponibile per quella data
    (degrado pulito: la transazione è comunque registrata, ma la valutazione a
    mercato non è calcolabile e non viene MAI inventata).
    """

    __tablename__ = "user_transactions"
    __table_args__ = (Index("ix_user_tx_symbol_executed", "symbol_id", "executed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True, nullable=False
    )
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # "BUY" | "SELL"
    amount: Mapped[float] = mapped_column(Float, nullable=False)  # > 0
    fee_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 0..100
    currency: Mapped[str] = mapped_column(String(8), nullable=False)  # snapshot di Symbol.currency
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False
    )  # backdating consentito
    price_ref: Mapped[float | None] = mapped_column(Float, nullable=True)  # close usato per la stima
    quantity_est: Mapped[float | None] = mapped_column(Float, nullable=True)  # azioni stimate
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    symbol: Mapped["Symbol"] = relationship(back_populates="transactions")


class SimPosition(Base):
    """Una posizione del PORTAFOGLIO SIMULATO DEL SISTEMA (blueprint §6 addendum).

    Distinta da :class:`UserTransaction` — che è il diario dell'utente e non
    viene MAI toccato da questo motore. Qui il sistema tiene il proprio libro
    fittizio: apre una posizione quando emette un BUY, la porta avanti finché
    dice HOLD, e la chiude su SELL o quando scatta stop-loss, take-profit o la
    scadenza dell'orizzonte. Serve a due scopi, in ordine di importanza:

    1. dare alle posizioni un CICLO DI VITA. Prima di questa tabella una
       posizione aperta era dedotta come "l'ultimo BUY non ancora seguito da un
       SELL": non scadeva mai, e siccome il sistema dice SELL molto raramente,
       ``open_allocation_pct`` cresceva in modo monotono fino a saturare la
       regola 10 (riserva di liquidità) e a forzare a HOLD ogni nuovo BUY.
    2. produrre statistiche di PERCORSO (escursione avversa/favorevole massima,
       quante volte lo stop è stato colpito) che il rendimento puntuale a 7
       giorni e a orizzonte non possono per costruzione vedere.

    ``planned_notional`` è espresso nella valuta del titolo: l'app non ha uno
    strato di cambio e non inventa un tasso, quindi gli aggregati monetari sono
    sempre raggruppati per valuta e mai sommati fra valute diverse.
    """

    __tablename__ = "sim_positions"
    __table_args__ = (Index("ix_sim_pos_symbol_status", "symbol_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Ancora di idempotenza: una sola posizione per consiglio BUY che l'ha
    # aperta, garantita dal vincolo di unicità. È ciò che rende la passata del
    # motore ripetibile senza creare duplicati.
    open_recommendation_id: Mapped[int] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    weight_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    planned_notional: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tranches_total: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tranches_filled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares_open: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Snapshot dei livelli decisi al momento del consiglio. ``None`` significa
    # che quel tipo di uscita non può semplicemente scattare: non viene MAI
    # inventato un livello mancante.
    stop_loss_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    opened_session: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_session: Mapped[date | None] = mapped_column(Date, nullable=True)
    # "OPEN" | "CLOSED" | "STALE" (scaduta ma senza prezzi disponibili per
    # chiuderla: si dichiara, non si inventa un prezzo di uscita).
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="OPEN", index=True)
    # "STOP_LOSS" | "TAKE_PROFIT" | "HORIZON" | "SELL_RECO"
    close_reason: Mapped[str | None] = mapped_column(String(12), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    proceeds_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True quando in una stessa barra giornaliera il minimo ha perforato lo stop
    # E il massimo ha raggiunto il take-profit: con dati giornalieri l'ordine dei
    # due eventi è inconoscibile. La regola dichiarata è "vince lo stop", e il
    # caso viene contato qui invece di essere nascosto.
    exit_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mae_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    symbol: Mapped["Symbol"] = relationship(back_populates="sim_positions")
    fills: Mapped[list["SimFill"]] = relationship(
        back_populates="position", cascade="all, delete-orphan", passive_deletes=True
    )


class SimFill(Base):
    """Registro in sola aggiunta delle esecuzioni fittizie di una posizione.

    Lo stato della posizione è interamente ricostruibile da queste righe (vedi
    ``app.engine.sim_book.rebuild_position_state``): le colonne denormalizzate
    su :class:`SimPosition` sono una comodità di lettura, non la verità.

    Il vincolo di unicità su (posizione, lato, seduta) è la seconda guardia di
    idempotenza dopo ``open_recommendation_id``: le tranche di un DCA distano
    almeno sette giorni, quindi una collisione legittima non esiste.
    """

    __tablename__ = "sim_fills"
    __table_args__ = (
        UniqueConstraint("position_id", "side", "session", name="uq_sim_fill_pos_side_session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int] = mapped_column(
        ForeignKey("sim_positions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # "BUY" | "SELL"
    # "ENTRY" | "DCA" | "STOP_LOSS" | "TAKE_PROFIT" | "HORIZON" | "SELL_RECO"
    reason: Mapped[str] = mapped_column(String(12), nullable=False)
    session: Mapped[date] = mapped_column(Date, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    shares: Mapped[float] = mapped_column(Float, nullable=False)
    notional: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    position: Mapped["SimPosition"] = relationship(back_populates="fills")
