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

from datetime import datetime

from sqlalchemy import (
    Boolean,
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
    favorites_analysis_interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4
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
