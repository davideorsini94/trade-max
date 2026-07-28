# TRADE-MAX — Blueprint di implementazione completo

Stock monitoring & advisory app. Advisory only, **nessuna esecuzione di ordini**. Local-first, single process backend, SQLite file DB, no Docker.

**Stack**: Python **3.12**, FastAPI + SQLAlchemy 2.x + SQLite + APScheduler + httpx + yfinance + feedparser + pandas; frontend React 18 + Vite 5 + TypeScript + Tailwind 3 + recharts. LLM: OpenRouter (primario) + Gemini (fallback), configurabile via `.env`.

**Convenzione lingua**: prompt di sistema degli agenti in **inglese** (resa migliore dei modelli); tutti i campi destinati all'utente (`summary_it`, `rationale_it`, `notes_it`, `report_it`) in **italiano**. Tutta la UI in italiano.

**Convenzione import**: import assoluti con radice `app` (es. `from app.models import Symbol`); il server gira dalla directory `backend/` con `uvicorn app.main:app`.

**Live updates: polling, non WebSocket.** Giustificazione: i dati cambiano su cadenze di minuti (yfinance ha ritardo ~15 min, le analisi girano ogni ore), il backend è un singolo processo con SQLite (nessun pub/sub nativo), e il polling (30–60 s) elimina gestione di connessioni, riconnessioni e stato server-side. Un hook `usePolling` generico copre tutto.

---

## 1. File tree completo

```
trade-max/
├── .gitignore
├── .env.example
├── README.md
├── requirements.txt
├── run.sh                          # avvio dev: uvicorn backend
├── backend/
│   ├── pyproject.toml              # solo [tool.pytest.ini_options] + ruff config
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                 # FastAPI app factory, CORS, static mount, lifespan (create_all + scheduler start)
│   │   ├── config.py               # Settings (pydantic-settings) da .env
│   │   ├── db.py                   # engine, SessionLocal, Base, get_db dependency
│   │   ├── models.py               # TUTTI i modelli SQLAlchemy
│   │   ├── schemas.py              # TUTTI gli schemi Pydantic + Enum
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   ├── client.py           # LLMClient: complete_json() con fallback provider
│   │   │   ├── providers.py        # OpenRouterProvider, GeminiProvider (httpx)
│   │   │   └── json_utils.py       # extract_json(), LLMOutputError
│   │   ├── data/
│   │   │   ├── __init__.py
│   │   │   ├── sources.py          # whitelist fonti curate (costanti)
│   │   │   ├── market.py           # MarketDataService (yfinance): quote, history, fundamentals, search
│   │   │   ├── indicators.py       # funzioni pure pandas: sma, ema, rsi, macd, bollinger, atr, volatility, compute_all
│   │   │   └── news.py             # NewsService (feedparser): fetch_macro(), fetch_corporate(symbol)
│   │   ├── agents/
│   │   │   ├── __init__.py         # ANALYST_AGENTS registry
│   │   │   ├── base.py             # BaseAgent, AgentContext, AgentResult
│   │   │   ├── technical.py        # TechnicalAnalystAgent
│   │   │   ├── fundamentals.py     # FundamentalsAnalystAgent
│   │   │   ├── macro_news.py       # MacroNewsAnalystAgent
│   │   │   ├── corporate_news.py   # CorporateNewsAnalystAgent
│   │   │   ├── synthesizer.py      # SynthesizerAgent
│   │   │   └── validator.py        # RiskValidatorAgent
│   │   ├── engine/
│   │   │   ├── __init__.py
│   │   │   ├── policy.py           # PolicyEngine (deterministico) + RISK_PROFILES
│   │   │   └── orchestrator.py     # run_analysis(symbol_id) end-to-end
│   │   ├── evaluation/
│   │   │   ├── __init__.py
│   │   │   ├── evaluator.py        # run_weekly_evaluation(): scoring + attribuzione per-agente
│   │   │   └── feedback.py         # generate_lessons() via LLM, get_active_lessons(agent_name)
│   │   ├── scheduler.py            # setup_scheduler(): tutti i job APScheduler, is_market_open()
│   │   └── api/
│   │       ├── __init__.py         # api_router che aggrega i router
│   │       ├── deps.py             # get_db, get_llm_client
│   │       ├── symbols.py
│   │       ├── prices.py
│   │       ├── analysis.py
│   │       ├── recommendations.py
│   │       ├── evaluations.py
│   │       ├── feedback.py
│   │       ├── settings.py
│   │       └── dashboard.py
│   └── tests/
│       ├── conftest.py             # in-memory SQLite fixture, TestClient
│       ├── test_indicators.py
│       ├── test_policy.py
│       └── test_api_symbols.py
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── tsconfig.json
    ├── tsconfig.node.json
    ├── index.html
    ├── postcss.config.js
    ├── tailwind.config.js
    └── src/
        ├── main.tsx
        ├── App.tsx                 # router + layout
        ├── index.css               # @tailwind directives + variabili tema
        ├── api/
        │   ├── client.ts           # fetch wrapper tipizzato (apiGet/apiPost/apiPut/apiDelete)
        │   └── types.ts            # tipi TS speculari agli schemi Pydantic
        ├── hooks/
        │   ├── usePolling.ts       # usePolling<T>(fetcher, intervalMs)
        │   └── useApi.ts           # fetch one-shot con loading/error
        ├── components/
        │   ├── layout/
        │   │   ├── Header.tsx
        │   │   ├── DisclaimerBanner.tsx
        │   │   └── Layout.tsx
        │   ├── common/
        │   │   ├── Badge.tsx
        │   │   ├── Card.tsx
        │   │   ├── Spinner.tsx
        │   │   ├── ErrorBox.tsx
        │   │   └── ConfidenceBar.tsx
        │   ├── symbols/
        │   │   ├── SymbolSearch.tsx
        │   │   ├── FavoriteStar.tsx
        │   │   ├── FavoritesStrip.tsx
        │   │   └── SymbolTable.tsx
        │   ├── charts/
        │   │   ├── PriceChart.tsx          # recharts ComposedChart + overlay indicatori + marker raccomandazioni
        │   │   └── AccuracyTrendChart.tsx
        │   └── recommendations/
        │       ├── RecommendationCard.tsx
        │       ├── AgentBreakdown.tsx
        │       ├── PolicyChecksList.tsx
        │       └── RecommendationTimeline.tsx
        └── pages/
            ├── DashboardPage.tsx
            ├── SymbolDetailPage.tsx
            ├── PerformancePage.tsx
            └── SettingsPage.tsx
```

`.gitignore`: `__pycache__/`, `.env`, `trademax.db*`, `node_modules/`, `frontend/dist/`, `.pytest_cache/`, `.ruff_cache/`, `.venv/`.

---

## 2. Schema SQLite — modelli SQLAlchemy (backend/app/models.py)

SQLAlchemy 2.0 typed style (`Mapped[...]`, `mapped_column`). `Base = DeclarativeBase` in `db.py`. Tutte le datetime **UTC naive** (convenzione: si salvano `datetime.utcnow()`; la UI converte in Europe/Rome). JSON salvato come `Text` con stringhe JSON (SQLite) — parsing negli schemi Pydantic/router.

### 2.1 `app_settings` — riga singola (id=1, creata al bootstrap)

| colonna | tipo | nullable | default |
|---|---|---|---|
| id | Integer PK | no | 1 |
| total_budget | Float | no | 10000.0 |
| budget_currency | String(8) | no | "EUR" |
| risk_profile | String(16) | no | "prudente" (`prudente\|bilanciato\|dinamico`) |
| cash_reserve_pct | Float | no | 30.0 |
| max_position_pct | Float | no | 15.0 |
| favorites_analysis_interval_hours | Integer | no | 4 |
| others_analysis_interval_hours | Integer | no | 24 |
| updated_at | DateTime | no | utcnow, onupdate utcnow |

### 2.2 `symbols`

| colonna | tipo | nullable | default |
|---|---|---|---|
| id | Integer PK autoincr | no | |
| ticker | String(20), unique, index | no | |
| name | String(120) | no | "" |
| exchange | String(40) | yes | None |
| currency | String(8) | no | "USD" |
| asset_type | String(20) | no | "EQUITY" |
| is_favorite | Boolean, index | no | False |
| favorite_added_at | DateTime | yes | None |
| is_active | Boolean | no | True |
| created_at | DateTime | no | utcnow |

Relazioni: `prices` (1-N PriceHistory, cascade delete-orphan), `recommendations` (1-N), `runs` (1-N AnalysisRun).

### 2.3 `price_history`

| colonna | tipo | nullable | default |
|---|---|---|---|
| id | Integer PK | no | |
| symbol_id | Integer FK symbols.id ondelete CASCADE, index | no | |
| ts | DateTime | no | |
| interval | String(4) | no | "1d" (`1d` o `1h`) |
| open | Float | no | |
| high | Float | no | |
| low | Float | no | |
| close | Float | no | |
| volume | Float | no | 0.0 |

Constraint: `UniqueConstraint(symbol_id, ts, interval, name="uq_price_point")`; `Index("ix_price_symbol_interval_ts", symbol_id, interval, ts)`.

### 2.4 `analysis_runs`

| colonna | tipo | nullable | default |
|---|---|---|---|
| id | Integer PK | no | |
| symbol_id | Integer FK symbols.id CASCADE, index | no | |
| status | String(12) | no | "PENDING" (`PENDING\|RUNNING\|COMPLETED\|FAILED`) |
| trigger | String(12) | no | "SCHEDULED" (`SCHEDULED\|MANUAL`) |
| llm_provider_used | String(20) | yes | None |
| error | Text | yes | None |
| started_at | DateTime | no | utcnow |
| finished_at | DateTime | yes | None |

Relazioni: `analyses` (1-N), `recommendation` (1-1, uselist=False).

### 2.5 `analyses` — un record per agente per run (inclusi synthesizer e validator)

| colonna | tipo | nullable | default |
|---|---|---|---|
| id | Integer PK | no | |
| run_id | Integer FK analysis_runs.id CASCADE, index | no | |
| symbol_id | Integer FK symbols.id CASCADE, index | no | |
| agent_name | String(30) | no | (`technical\|fundamentals\|macro_news\|corporate_news\|synthesizer\|validator`) |
| status | String(12) | no | "OK" (`OK\|FAILED`) |
| signal | Float | yes | None (−1.0..+1.0; null per validator) |
| confidence | Float | yes | None (0..1) |
| stance | String(10) | yes | None (`BULLISH\|BEARISH\|NEUTRAL`) |
| summary_it | Text | no | "" |
| output_json | Text | no | "{}" (output strutturato completo) |
| error | Text | yes | None |
| created_at | DateTime | no | utcnow |

`Index("ix_analyses_symbol_agent_created", symbol_id, agent_name, created_at)`.

### 2.6 `recommendations`

| colonna | tipo | nullable | default |
|---|---|---|---|
| id | Integer PK | no | |
| run_id | Integer FK analysis_runs.id CASCADE, unique | no | |
| symbol_id | Integer FK symbols.id CASCADE, index | no | |
| action | String(8) | no | (`BUY\|SELL\|HOLD`) |
| sizing_strategy | String(8) | no | (`ALL_IN\|DCA\|PARTIAL\|WAIT`) |
| confidence | Float | no | (0..1, post-policy) |
| allocation_pct | Float | no | 0.0 (% del budget totale) |
| allocation_amount | Float | no | 0.0 (in valuta budget) |
| dca_tranches | Integer | no | 1 |
| entry_price | Float | yes | None (close al momento della rec) |
| stop_loss_price | Float | yes | None |
| take_profit_price | Float | yes | None |
| horizon_days | Integer | no | 30 |
| estimated_profit_pct | Float | yes | None |
| estimated_profit_amount | Float | yes | None |
| rationale_it | Text | no | "" |
| synthesizer_json | Text | no | "{}" (output grezzo synthesizer) |
| validator_verdict | String(8) | no | (`APPROVE\|REVISE\|VETO`) |
| validator_notes_it | Text | no | "" |
| policy_checks_json | Text | no | "[]" (lista PolicyCheck) |
| policy_overridden | Boolean | no | False |
| original_action | String(8) | yes | None (azione pre-policy se overridden) |
| original_sizing | String(8) | yes | None |
| evaluated | Boolean, index | no | False |
| realized_return_7d | Float | yes | None (riempito dalla valutazione) |
| outcome_score | Float | yes | None (−1..+1) |
| evaluation_id | Integer FK evaluations.id SET NULL | yes | None |
| created_at | DateTime, index | no | utcnow |

`Index("ix_reco_symbol_created", symbol_id, created_at)`.

### 2.7 `evaluations` — report settimanale

| colonna | tipo | nullable | default |
|---|---|---|---|
| id | Integer PK | no | |
| period_start | DateTime | no | |
| period_end | DateTime | no | |
| status | String(12) | no | "COMPLETED" (`RUNNING\|COMPLETED\|FAILED`) |
| total_recommendations | Integer | no | 0 |
| evaluated_count | Integer | no | 0 |
| accuracy_overall | Float | yes | None (0..1) |
| avg_realized_return_pct | Float | yes | None |
| hypothetical_pnl_pct | Float | yes | None (P&L ipotetico pesato per allocation) |
| best_symbol | String(20) | yes | None |
| worst_symbol | String(20) | yes | None |
| per_agent_json | Text | no | "{}" (metriche per agente, v. §7) |
| report_it | Text | no | "" (sintesi LLM in italiano) |
| created_at | DateTime | no | utcnow |

### 2.8 `agent_feedback` — lezioni per agente

| colonna | tipo | nullable | default |
|---|---|---|---|
| id | Integer PK | no | |
| evaluation_id | Integer FK evaluations.id CASCADE, index | no | |
| agent_name | String(30), index | no | |
| accuracy | Float | yes | None |
| avg_signal_error | Float | yes | None |
| lessons_json | Text | no | "[]" (lista di stringhe-lezione in inglese, per i prompt) |
| lessons_it | Text | no | "" (versione italiana per la UI) |
| is_active | Boolean, index | no | True |
| created_at | DateTime | no | utcnow |

### 2.9 `news_items` — cache/audit notizie (dedup per url)

| colonna | tipo | nullable | default |
|---|---|---|---|
| id | Integer PK | no | |
| source_key | String(40) | no | (chiave whitelist, es. "fed_press") |
| category | String(12) | no | (`MACRO\|CORPORATE`) |
| ticker | String(20), index | yes | None (solo CORPORATE per-symbol) |
| title | String(300) | no | |
| url | String(600), unique | no | |
| summary | Text | no | "" |
| published_at | DateTime, index | yes | None |
| fetched_at | DateTime | no | utcnow |

---

## 3. Schemi Pydantic (backend/app/schemas.py)

Tutti `BaseModel` con `model_config = ConfigDict(from_attributes=True)` dove mappano ORM.

```python
# --- Enums (str, Enum) ---
class Action(str, Enum): BUY = "BUY"; SELL = "SELL"; HOLD = "HOLD"
class Sizing(str, Enum): ALL_IN = "ALL_IN"; DCA = "DCA"; PARTIAL = "PARTIAL"; WAIT = "WAIT"
class Verdict(str, Enum): APPROVE = "APPROVE"; REVISE = "REVISE"; VETO = "VETO"
class RiskProfile(str, Enum): PRUDENTE = "prudente"; BILANCIATO = "bilanciato"; DINAMICO = "dinamico"
class RunStatus(str, Enum): PENDING = "PENDING"; RUNNING = "RUNNING"; COMPLETED = "COMPLETED"; FAILED = "FAILED"
class Stance(str, Enum): BULLISH = "BULLISH"; BEARISH = "BEARISH"; NEUTRAL = "NEUTRAL"

# --- Symbols ---
class SymbolCreate(BaseModel): ticker: str  # normalizzato .upper().strip()
class FavoriteToggle(BaseModel): is_favorite: bool
class SymbolOut(BaseModel):
    id: int; ticker: str; name: str; exchange: str | None; currency: str
    asset_type: str; is_favorite: bool; is_active: bool; created_at: datetime
class SymbolWithQuote(SymbolOut):
    last_price: float | None = None; change_pct_1d: float | None = None
    last_recommendation: "RecommendationBrief | None" = None
class SymbolSearchResult(BaseModel):
    ticker: str; name: str; exchange: str | None = None; asset_type: str = "EQUITY"; already_added: bool = False

# --- Prices / indicators ---
class PricePoint(BaseModel):
    ts: datetime; open: float; high: float; low: float; close: float; volume: float
class IndicatorSeries(BaseModel):
    # array allineati a points; null nei warm-up
    sma20: list[float | None]; sma50: list[float | None]; sma200: list[float | None]
    ema12: list[float | None]; ema26: list[float | None]
    rsi14: list[float | None]
    macd: list[float | None]; macd_signal: list[float | None]; macd_hist: list[float | None]
    bb_upper: list[float | None]; bb_mid: list[float | None]; bb_lower: list[float | None]
    atr14: list[float | None]
class PriceHistoryOut(BaseModel):
    ticker: str; interval: str; points: list[PricePoint]
    indicators: IndicatorSeries | None = None

# --- Analysis / runs ---
class AnalyzeRequest(BaseModel): trigger: Literal["MANUAL"] = "MANUAL"
class AnalyzeAccepted(BaseModel): run_id: int; status: RunStatus; message_it: str
class AnalysisOut(BaseModel):
    id: int; agent_name: str; status: str; signal: float | None; confidence: float | None
    stance: Stance | None; summary_it: str; output: dict; created_at: datetime   # output = json.loads(output_json)
class AnalysisRunOut(BaseModel):
    id: int; symbol_id: int; ticker: str; status: RunStatus; trigger: str
    llm_provider_used: str | None; error: str | None
    started_at: datetime; finished_at: datetime | None
    analyses: list[AnalysisOut] = []
    recommendation: "RecommendationOut | None" = None

# --- Recommendations ---
class PolicyCheck(BaseModel):
    rule: str          # es. "min_confidence_buy"
    passed: bool
    detail_it: str     # es. "Confidenza 0.58 < soglia 0.65: azione declassata a HOLD"
class RecommendationBrief(BaseModel):
    id: int; action: Action; sizing_strategy: Sizing; confidence: float; created_at: datetime
class RecommendationOut(BaseModel):
    id: int; run_id: int; symbol_id: int; ticker: str
    action: Action; sizing_strategy: Sizing; confidence: float
    allocation_pct: float; allocation_amount: float; dca_tranches: int
    entry_price: float | None; stop_loss_price: float | None; take_profit_price: float | None
    horizon_days: int; estimated_profit_pct: float | None; estimated_profit_amount: float | None
    rationale_it: str
    validator_verdict: Verdict; validator_notes_it: str
    policy_checks: list[PolicyCheck]; policy_overridden: bool
    original_action: Action | None; original_sizing: Sizing | None
    evaluated: bool; realized_return_7d: float | None; outcome_score: float | None
    created_at: datetime
class RecommendationListOut(BaseModel):
    items: list[RecommendationOut]; total: int

# --- Evaluations / feedback ---
class AgentMetrics(BaseModel):
    agent_name: str; accuracy: float | None; avg_signal_error: float | None
    n_samples: int; trend: list[float] = []   # accuracy ultime 8 valutazioni
class EvaluationOut(BaseModel):
    id: int; period_start: datetime; period_end: datetime; status: str
    total_recommendations: int; evaluated_count: int
    accuracy_overall: float | None; avg_realized_return_pct: float | None
    hypothetical_pnl_pct: float | None; best_symbol: str | None; worst_symbol: str | None
    per_agent: list[AgentMetrics]; report_it: str; created_at: datetime
class AgentFeedbackOut(BaseModel):
    id: int; evaluation_id: int; agent_name: str; accuracy: float | None
    lessons: list[str]; lessons_it: str; is_active: bool; created_at: datetime

# --- Settings / dashboard / health ---
class SettingsOut(BaseModel):
    total_budget: float; budget_currency: str; risk_profile: RiskProfile
    cash_reserve_pct: float; max_position_pct: float
    favorites_analysis_interval_hours: int; others_analysis_interval_hours: int
    updated_at: datetime
class SettingsUpdate(BaseModel):   # tutti opzionali, ge/le validati
    total_budget: float | None = Field(None, gt=0)
    risk_profile: RiskProfile | None = None
    cash_reserve_pct: float | None = Field(None, ge=10, le=80)
    max_position_pct: float | None = Field(None, ge=1, le=50)
    favorites_analysis_interval_hours: int | None = Field(None, ge=1, le=24)
    others_analysis_interval_hours: int | None = Field(None, ge=4, le=168)
class ProviderStatus(BaseModel):
    provider: str; configured: bool; model: str; is_primary: bool
class HealthOut(BaseModel):
    status: str; db_ok: bool; scheduler_running: bool; providers: list[ProviderStatus]
class DashboardSummary(BaseModel):
    favorites: list[SymbolWithQuote]; others: list[SymbolWithQuote]
    last_evaluation: EvaluationOut | None
    pending_runs: int; budget: SettingsOut; market_open: bool
    disclaimer_it: str   # sempre valorizzato, v. §8
```

---

## 4. REST API (prefisso `/api`)

| # | Metodo | Path | Query/Body | Risposta | Note |
|---|---|---|---|---|---|
| 1 | GET | `/api/symbols` | `?favorites_only=bool&with_quotes=bool` | `list[SymbolWithQuote]` | ordinati: preferiti prima |
| 2 | GET | `/api/symbols/search` | `?q=str` (min 2 char) | `list[SymbolSearchResult]` | yfinance `Search`; fallback httpx `query2.finance.yahoo.com/v1/finance/search` |
| 3 | POST | `/api/symbols` | body `SymbolCreate` | `SymbolOut` (201) | valida ticker via yfinance; 404 se inesistente, 409 se duplicato; scarica subito 2 anni di storico 1d |
| 4 | DELETE | `/api/symbols/{symbol_id}` | — | 204 | cascade su prezzi/run/rec |
| 5 | POST | `/api/symbols/{symbol_id}/favorite` | body `FavoriteToggle` | `SymbolOut` | setta/azzera `favorite_added_at` |
| 6 | GET | `/api/symbols/{symbol_id}/prices` | `?interval=1d\|1h&days=int(def 180)&indicators=bool(def true)` | `PriceHistoryOut` | se dati stantii (>1 sess.) refresh sincrono da yfinance |
| 7 | POST | `/api/symbols/{symbol_id}/analyze` | body `AnalyzeRequest` | `AnalyzeAccepted` (202) | crea run PENDING, lancia orchestrator come task asyncio; 409 se run già RUNNING per il simbolo |
| 8 | GET | `/api/runs/{run_id}` | — | `AnalysisRunOut` | pagina di polling post-trigger |
| 9 | GET | `/api/symbols/{symbol_id}/recommendations/latest` | — | `RecommendationOut` (404 se nessuna) | |
| 10 | GET | `/api/symbols/{symbol_id}/recommendations` | `?limit=int(20)&offset=int(0)` | `RecommendationListOut` | desc per created_at |
| 11 | GET | `/api/recommendations/latest` | — | `list[RecommendationOut]` | ultima per ogni simbolo attivo |
| 12 | GET | `/api/evaluations` | `?limit=int(12)` | `list[EvaluationOut]` | desc |
| 13 | GET | `/api/evaluations/{evaluation_id}` | — | `EvaluationOut` | |
| 14 | POST | `/api/evaluations/run` | — | `EvaluationOut` (201) o 409 | trigger manuale del job settimanale |
| 15 | GET | `/api/feedback` | `?agent_name=str&active_only=bool(true)` | `list[AgentFeedbackOut]` | |
| 16 | GET | `/api/settings` | — | `SettingsOut` | |
| 17 | PUT | `/api/settings` | body `SettingsUpdate` | `SettingsOut` | |
| 18 | GET | `/api/dashboard/summary` | — | `DashboardSummary` | endpoint unico di polling della dashboard (30 s) |
| 19 | GET | `/api/health` | — | `HealthOut` | stato provider (chiavi configurate, mai i valori) |

Errori: sempre `{"detail": "<messaggio in italiano>"}` (HTTPException standard FastAPI).

---

## 5. Pipeline multi-agente

### 5.1 Infrastruttura LLM

`backend/app/llm/providers.py`:
```python
class BaseProvider:
    name: str
    async def chat(self, system: str, user: str, temperature: float, max_tokens: int) -> str  # raises ProviderError

class OpenRouterProvider(BaseProvider):  # POST https://openrouter.ai/api/v1/chat/completions
    # headers: Authorization Bearer, HTTP-Referer: "http://localhost", X-Title: "trade-max"
    # body: {model, messages, temperature, max_tokens, response_format:{"type":"json_object"}}
class GeminiProvider(BaseProvider):      # POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=...
    # system → systemInstruction; generationConfig: {temperature, maxOutputTokens, responseMimeType:"application/json"}
```

`backend/app/llm/client.py`:
```python
class LLMClient:
    def __init__(self, settings: Settings): ...  # costruisce provider primario+fallback da env
    async def complete_json(self, system: str, user: str, *,
                            temperature: float = 0.2, max_tokens: int = 2500) -> tuple[dict, str]:
        # ritorna (parsed_json, provider_name_usato)
        # per provider in [primario, fallback se configurato]:
        #   2 tentativi; su ProviderError(429/5xx/timeout) o LLMOutputError → provider successivo
        # esaurito tutto → raise LLMUnavailableError
```
`json_utils.extract_json(text: str) -> dict`: rimuove code fence, prende sottostringa dal primo `{` all'ultimo `}`, `json.loads`, ripara virgole finali; su fallimento `LLMOutputError`.

Default env: `LLM_PROVIDER=openrouter`, `OPENROUTER_MODEL=openai/gpt-4o-mini`, `GEMINI_MODEL=gemini-2.0-flash`, `LLM_FALLBACK_ENABLED=true`.

### 5.2 Base agente

`backend/app/agents/base.py`:
```python
@dataclass
class AgentContext:
    symbol: SymbolOut
    is_favorite: bool
    price_summary: dict        # ultimi valori: close, change 1d/5d/30d/90d, high/low 52w, volume medio
    indicators: dict           # ultimi valori scalari di TUTTI gli indicatori + serie compresse (ultimi 30 close)
    fundamentals: dict         # da yfinance .info/.fast_info: pe, forward_pe, eps, market_cap, dividend_yield, beta, margins, revenue_growth, debt_to_equity, analyst target
    macro_news: list[dict]     # [{source, title, summary, published_at}] max 15, ultime 72h
    corporate_news: list[dict] # idem, filtrate per ticker, max 15
    risk_profile: str
    lessons: list[str]         # lezioni attive per QUESTO agente

@dataclass
class AgentResult:
    agent_name: str; output: dict; provider: str

class BaseAgent:
    name: str                                 # override
    def build_system_prompt(self, lessons: list[str]) -> str   # template + blocco lezioni
    def build_user_prompt(self, ctx: AgentContext) -> str      # dati serializzati JSON compatto
    async def run(self, ctx: AgentContext, llm: LLMClient) -> AgentResult
    def validate_output(self, data: dict) -> dict              # clamp signal/confidence, campi obbligatori, default
```

**Iniezione feedback (identica per tutti gli agenti)** — `build_system_prompt` termina sempre con:
```
## LESSONS FROM PAST PERFORMANCE REVIEWS
Your past predictions were scored against realized market outcomes. Apply these lessons:
{numbered lessons, max 5, most recent first — or "No lessons yet." }
```
Le lezioni arrivano da `evaluation.feedback.get_active_lessons(agent_name, limit=5)`.

### 5.3 Schema JSON comune degli analisti (output atteso)

```json
{
  "stance": "BULLISH|BEARISH|NEUTRAL",
  "signal": 0.0,
  "confidence": 0.0,
  "key_points": ["..."],
  "risks": ["..."],
  "data_quality": "GOOD|PARTIAL|POOR",
  "summary_it": "2-4 frasi in italiano per l'utente"
}
```
`signal`: −1.0 (fortemente ribassista) .. +1.0 (fortemente rialzista). `confidence`: 0..1. `key_points` max 5 (inglese), `risks` max 3 (inglese).

### 5.4 I sei attori

**TechnicalAnalystAgent** (`technical.py`, name=`"technical"`)
- Input: `price_summary`, `indicators` (SMA20/50/200, EMA12/26, RSI14, MACD, Bollinger, ATR14, volatilità 30d, drawdown da max 90d, serie ultimi 30 close).
- System prompt outline: *"You are a senior technical analyst. Analyze ONLY price action and indicators provided — do not invent data. Consider trend (SMA alignment), momentum (RSI, MACD), volatility (ATR, Bollinger), support/resistance from 52w range. Be conservative: when signals conflict, lower confidence. Output strict JSON matching the schema. No prose outside JSON."* + schema §5.3 con campo extra `"trend": "UP|DOWN|SIDEWAYS"` + blocco lezioni.

**FundamentalsAnalystAgent** (`fundamentals.py`, name=`"fundamentals"`)
- Input: `fundamentals` dict.
- Prompt outline: *"You are a fundamentals analyst (value-oriented, capital preservation first). Assess valuation (P/E vs sector norms), profitability, growth, leverage, dividend. If data is missing mark data_quality PARTIAL/POOR and reduce confidence."* + schema + extra `"valuation": "CHEAP|FAIR|EXPENSIVE"` + lezioni.

**MacroNewsAnalystAgent** (`macro_news.py`, name=`"macro_news"`)
- Input: `macro_news` (feed MACRO whitelist, ultime 72h), settore/valuta del simbolo.
- Prompt outline: *"You are a macro strategist. From ONLY the provided headlines/summaries (curated reputable sources: central banks, regulators, major financial press), assess how the macro environment (rates, inflation, geopolitics, sector policy) affects THIS symbol over the next 30 days. Do not use outside knowledge of events after your training. If news is scarce, stay NEUTRAL with low confidence."* + schema + extra `"macro_drivers": ["..."]` + lezioni.

**CorporateNewsAnalystAgent** (`corporate_news.py`, name=`"corporate_news"`)
- Input: `corporate_news` (feed per-ticker: Yahoo Finance RSS del simbolo, SEC EDGAR 8-K atom, CNBC business) — copre notizie sulla società, sui grandi gruppi collegati e dichiarazioni di persone influenti (CEO, investitori noti, policymaker) presenti nelle notizie.
- Prompt outline: *"You are a corporate news analyst. Evaluate company-specific catalysts: earnings, guidance, filings, M&A, litigation, and public statements by executives or influential figures mentioned in the provided items. Weigh source reliability; regulatory filings > press. Only use provided items."* + schema + extra `"catalysts": [{"event": "...", "impact": "POSITIVE|NEGATIVE|MIXED"}]` + lezioni.

**SynthesizerAgent** (`synthesizer.py`, name=`"synthesizer"`)
- Input: i 4 output JSON degli analisti (inclusi eventuali `status:"FAILED"` — segnalati come `null`), `price_summary`, budget/risk_profile, ultima raccomandazione precedente del simbolo (azione+data, per coerenza).
- Prompt outline: *"You are the chief investment strategist of a CONSERVATIVE advisory desk. Capital preservation comes first; missing a gain is acceptable, a large loss is not. Combine the four analyst reports (technical, fundamentals, macro, corporate). Weigh agent confidence and data_quality; discount analysts flagged as unreliable in lessons. Prefer gradual entries (DCA/PARTIAL) over ALL_IN. Recommend WAIT sizing with HOLD when evidence is mixed. Set realistic stop-loss and take-profit. Output strict JSON."* + lezioni (del synthesizer) + schema:
```json
{
  "action": "BUY|SELL|HOLD",
  "sizing_strategy": "ALL_IN|DCA|PARTIAL|WAIT",
  "confidence": 0.0,
  "allocation_pct": 0.0,
  "horizon_days": 30,
  "entry_price": null, "stop_loss_price": null, "take_profit_price": null,
  "estimated_profit_pct": 0.0,
  "agent_weights": {"technical": 0.3, "fundamentals": 0.3, "macro_news": 0.2, "corporate_news": 0.2},
  "dissent": "main disagreement between analysts, or null",
  "rationale_it": "5-8 frasi in italiano: cosa fare, perché, e i rischi principali"
}
```
- Salvato in `analyses` con `signal` = mappatura action (BUY=+conf, SELL=−conf, HOLD=0).

**RiskValidatorAgent** (`validator.py`, name=`"validator"`)
- Input: proposta del synthesizer + i 4 report analisti + metriche di rischio deterministiche precalcolate (ATR/price %, drawdown 90d, beta, distanza da SMA200) + risk_profile.
- Prompt outline: *"You are an adversarial risk officer with VETO power. Your job is to find reasons the proposal could lose significant money. Challenge: overconfidence vs data quality, conflicting analysts ignored, volatility vs sizing, catalyst risk (earnings imminent), stop-loss adequacy. APPROVE only if the proposal is defensible for a conservative retail investor. Use REVISE to downgrade sizing/confidence; use VETO if downside risk is substantial or evidence is weak."* + lezioni (del validator) + schema:
```json
{
  "verdict": "APPROVE|REVISE|VETO",
  "revised_action": null,
  "revised_sizing": null,
  "confidence_adjustment": 0.0,
  "concerns": ["..."],
  "notes_it": "2-4 frasi in italiano"
}
```
(`revised_action`/`revised_sizing` valorizzati solo se REVISE; `confidence_adjustment` in −0.4..0.0, sommato alla confidence; `concerns` max 5.)

### 5.5 Orchestrator (`backend/app/engine/orchestrator.py`)

```python
async def run_analysis(symbol_id: int, trigger: str = "SCHEDULED", run_id: int | None = None) -> int:
    # 1. crea/aggiorna AnalysisRun → RUNNING
    # 2. DATA: MarketDataService.refresh_prices(symbol) → upsert price_history (1d 2y, 1h 30d)
    #          indicators.compute_all(df) ; market.get_fundamentals(ticker)
    #          NewsService.fetch_macro() + fetch_corporate(ticker)  (cache news_items, TTL 30 min)
    # 3. costruisce AgentContext (lessons per ciascun agente da feedback.get_active_lessons)
    # 4. asyncio.gather sui 4 analisti (return_exceptions=True); persiste ogni analysis;
    #    se >=3 analisti FAILED → run FAILED, stop.
    # 5. SynthesizerAgent.run(...) → persiste analysis
    # 6. RiskValidatorAgent.run(proposta) → persiste analysis
    # 7. PolicyEngine.apply(proposal, verdict, market_metrics, settings, last_reco) → FinalRecommendation
    # 8. persiste Recommendation (entry_price = ultimo close; estimated_profit_amount =
    #    allocation_amount * estimated_profit_pct/100); run COMPLETED.
    # Ogni step in try/except: errore → run.status=FAILED, run.error valorizzato.
```
Concorrenza: lock `asyncio.Lock` per-symbol in memoria; l'API risponde 409 se run attiva. yfinance è sincrono: chiamarlo via `asyncio.to_thread`.

### 5.6 Whitelist fonti (`backend/app/data/sources.py`)

```python
MACRO_SOURCES = {
  "fed_press":   {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "lang": "en"},
  "ecb_press":   {"name": "BCE", "url": "https://www.ecb.europa.eu/rss/press.html", "lang": "en"},
  "cnbc_top":    {"name": "CNBC Top News", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "lang": "en"},
  "cnbc_econ":   {"name": "CNBC Economy", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258", "lang": "en"},
  "mw_top":      {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "lang": "en"},
  "sole24_fin":  {"name": "Il Sole 24 Ore Finanza", "url": "https://www.ilsole24ore.com/rss/finanza--mercati.xml", "lang": "it"},
}
CORPORATE_SOURCES = {   # template per-ticker
  "yahoo_sym":   "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
  "sec_edgar":   "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=8-K&dateb=&owner=include&count=10&output=atom",
  "cnbc_biz":    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
}
```
Solo queste fonti vengono lette; nessun URL dinamico da LLM. `NewsService` fa dedup su `news_items.url`, tiene le ultime 72h, tronca summary a 400 char. User-Agent dedicato per SEC (`trade-max/1.0 davide.orsini@promedital.it` — richiesto dalle policy SEC).

---

## 6. PolicyEngine — regole deterministiche (backend/app/engine/policy.py)

Puro codice, nessun LLM. Firma:

```python
@dataclass
class MarketMetrics:
    last_close: float; atr14: float; sma50: float; sma200: float
    rsi14: float; drawdown_90d_pct: float; volatility_30d_pct: float

def apply(proposal: dict, verdict: dict, metrics: MarketMetrics,
          settings: AppSettings, last_reco: Recommendation | None,
          open_allocation_pct: float) -> tuple[FinalReco, list[PolicyCheck]]
```

Profili di rischio (costante `RISK_PROFILES`):

| parametro | prudente (default) | bilanciato | dinamico |
|---|---|---|---|
| MAX_POSITION_PCT (cap su settings) | 15% | 20% | 30% |
| CASH_RESERVE_PCT (min) | 30% | 20% | 10% |
| MIN_CONF_BUY | 0.65 | 0.60 | 0.55 |
| MIN_CONF_SELL | 0.60 | 0.55 | 0.50 |
| ALL_IN consentito | mai | conf ≥ 0.85 e vol < 2.5% | conf ≥ 0.80 |
| ATR%_FORCE_DCA | 3.0% | 4.0% | 5.0% |
| MAX_DRAWDOWN_90D_BUY | 20% | 25% | 35% |
| COOLDOWN_OPPOSITE_H | 72h | 48h | 24h |

Regole applicate **in quest'ordine** (ognuna produce un `PolicyCheck` con esito e `detail_it`; ogni modifica setta `policy_overridden=True` e conserva `original_action`/`original_sizing`):

1. **validator_veto** — `verdict == VETO` → azione forzata a `HOLD`, sizing `WAIT`, allocation 0. Il veto è inappellabile.
2. **validator_revise** — `verdict == REVISE` → applica `revised_action`/`revised_sizing` se presenti; `confidence += confidence_adjustment` (clamp 0..1).
3. **min_confidence** — BUY con conf < MIN_CONF_BUY o SELL con conf < MIN_CONF_SELL → declassa a HOLD/WAIT.
4. **trend_filter (solo BUY)** — se `last_close < sma200` **e** `sma50 < sma200` (death cross) → HOLD/WAIT ("non comprare in trend ribassista conclamato").
5. **falling_knife (solo BUY)** — se `drawdown_90d_pct > MAX_DRAWDOWN_90D_BUY` → HOLD/WAIT.
6. **cooldown** — se `last_reco` ha azione opposta (BUY↔SELL) ed è più recente di COOLDOWN_OPPOSITE_H → HOLD/WAIT ("evitare inversioni impulsive").
7. **volatility_sizing** — se `atr14/last_close*100 > ATR%_FORCE_DCA` e sizing ∈ {ALL_IN, PARTIAL} → forza `DCA` con `dca_tranches=4` (25% a settimana).
8. **all_in_gate** — sizing ALL_IN non ammesso dal profilo → declassa a DCA (tranches=4).
9. **position_cap** — `allocation_pct = min(allocation_pct, max_position_pct del profilo, settings.max_position_pct)`.
10. **cash_reserve** — `allocation_pct = min(allocation_pct, max(0, 100 − cash_reserve_pct − open_allocation_pct))`; `open_allocation_pct` = somma delle allocation delle ultime raccomandazioni BUY attive (una per simbolo, non ancora seguite da SELL). Se risultato ≤ 0 → HOLD/WAIT ("riserva di liquidità esaurita").
11. **stop_loss_required (solo BUY)** — se `stop_loss_price` mancante o più lontano di 8% → `stop_loss = max(entry − 2*atr14, entry*0.92)`.
12. **confidence_cap** — `confidence = min(confidence, 0.95)` (mai certezza assoluta).
13. **hold_normalization** — se azione finale HOLD → sizing `WAIT`, allocation 0, tranches 1, estimated_profit nulli.

`allocation_amount = settings.total_budget * allocation_pct / 100`. Ogni check (passato o meno) finisce in `policy_checks_json` — la UI li mostra tutti.

---

## 7. Job settimanale di valutazione + scheduler

### 7.1 Evaluator (`backend/app/evaluation/evaluator.py`)

`run_weekly_evaluation()` — trigger: cron **domenica 18:00 Europe/Rome** + endpoint manuale (#14).

1. Seleziona `recommendations` con `evaluated == False` e `created_at <= now − 7 giorni`.
2. Per ciascuna: `realized_return_7d = (close(t+7d) − entry_price) / entry_price * 100` usando il primo close 1d disponibile ≥ created_at+7d (se prezzi mancanti, refresh yfinance; se ancora mancanti, salta e resta non valutata).
3. **outcome_score** (−1..+1):
   - BUY: `clamp(realized_return_7d / 5, −1, 1)`
   - SELL: `clamp(−realized_return_7d / 5, −1, 1)`
   - HOLD: `1 − min(|realized_return_7d| / 5, 1) * 2` (premia mercato piatto)
   - "corretta" se `outcome_score > 0.2` per BUY/SELL, `> 0.6` per HOLD (soglia più alta: altrimenti "non fare nulla" risulta corretto in ogni settimana tranquilla, |ret|<2%, e l'inazione diventa il modo più facile di sembrare accurati).
4. **Attribuzione per-agente**: per ogni raccomandazione valutata, recupera le `analyses` del run; per ciascun analista: `direction_correct = sign(signal) == sign(realized_return_7d)` (signal |x|<0.15 conta come previsione "flat", corretta se |ret|<2%); `signal_error = |signal − clamp(ret/5,−1,1)|`. Aggrega: `accuracy = corrette/totali`, `avg_signal_error`, ponderati per confidence. Per il synthesizer: accuracy dell'azione finale. Per il **validator**: quando ha bloccato la proposta (VETO, `revised_action` diversa, oppure un taglio di confidenza che fa fallire `min_confidence`) è valutato sul **controfattuale** — `_outcome_score` dell'azione che il synthesizer aveva proposto: bloccare un perdente è un successo, bloccare un vincente è un suo errore, e una zona grigia (`0 < score ≤ 0.2`) non produce campione. Altrimenti segue l'azione finale come il synthesizer. Il controllo copriva solo il ramo VETO, che il validator non usava mai: risultato, 36 BUY declassati su 36 con accuracy misurata del 92% (vedi §5.4 addendum).
5. `hypothetical_pnl_pct` = media di `realized_return_7d * allocation_pct/100` sulle BUY valutate.
6. Persiste `Evaluation` con `per_agent_json` = `{agent: {accuracy, avg_signal_error, n_samples}}`, best/worst symbol.
7. Chiama `feedback.generate_lessons(evaluation)`.

### 7.2 Feedback (`backend/app/evaluation/feedback.py`)

- `generate_lessons(evaluation)`: per ogni agente (tutti e 6), una chiamata LLM con: metriche dell'agente, i 3 casi peggiori (input sintetico → previsione → esito reale), le lezioni già attive. Prompt outline: *"You are a performance coach for an AI financial analyst. Given its scored track record and worst misses, write at most 3 concrete, actionable lessons (imperative, <25 words each) to improve future analyses. Avoid generic advice; reference observed error patterns. Also provide an Italian translation. Output JSON: {"lessons": [...], "lessons_it": "..."}"*.
- Persiste `AgentFeedback`; poi disattiva (`is_active=False`) i feedback dello stesso agente più vecchi delle ultime **3** valutazioni.
- `get_active_lessons(agent_name, limit=5) -> list[str]`: lezioni dai feedback attivi, più recenti prima — usata da `BaseAgent.build_system_prompt` per **tutti** gli attori.
- Genera anche il `report_it` complessivo dell'Evaluation (una chiamata LLM riassuntiva in italiano).

### 7.3 Scheduler (`backend/app/scheduler.py`, APScheduler `AsyncIOScheduler`, timezone Europe/Rome)

`is_market_open()`: semplice — lun–ven, 15:30–22:00 Europe/Rome (sessione USA; sufficiente per v1, dichiarato nel README).

| job id | cadenza | funzione |
|---|---|---|
| `prices_favorites` | ogni 15 min, solo `is_market_open()` | refresh quote/1h per i preferiti |
| `prices_others` | ogni 60 min, solo market open | refresh per i non preferiti |
| `prices_eod` | 22:15 lun–ven | storico 1d completo per tutti |
| `analysis_favorites` | ogni `favorites_analysis_interval_hours` (def 4h), solo market open | `run_analysis` sequenziale sui preferiti |
| `analysis_others` | ogni `others_analysis_interval_hours` (def 24h), alle 16:00 | `run_analysis` sui non preferiti |
| `weekly_evaluation` | cron dom 18:00 | `run_weekly_evaluation()` |
| `news_refresh` | ogni 30 min | fetch feed MACRO in cache |

Tutti i job con `max_instances=1`, `coalesce=True`, `misfire_grace_time=3600`. Avvio/stop nel `lifespan` di FastAPI.

---

## 8. Frontend

Routing (`react-router-dom` v6): `/` Dashboard, `/symbol/:ticker` Dettaglio, `/performance` Performance, `/settings` Impostazioni. Layout comune: `Header` (logo "TradeMax", nav: "Dashboard", "Performance", "Impostazioni", badge stato mercato "Mercato aperto/chiuso") + `DisclaimerBanner` **persistente** (footer fisso): *"⚠️ TradeMax è uno strumento sperimentale a scopo informativo. Non costituisce consulenza finanziaria. Le decisioni di investimento sono a tuo esclusivo rischio."*

Polling: `usePolling<T>(fetcher, intervalMs)` — dashboard 30 s; run in corso 3 s; dettaglio simbolo 60 s.

### Pagine e dipendenze dati

**DashboardPage** — `GET /api/dashboard/summary` (poll 30 s)
- `FavoritesStrip` (in alto, prominente): card per ogni preferito con badge "★ Priorità", prezzo, variazione 1g colorata, `RecommendationBrief` (pill COMPRA/VENDI/MANTIENI + ConfidenceBar), click → dettaglio.
- `SymbolSearch`: input con debounce 300 ms → `GET /api/symbols/search?q=`; risultato con bottone "Aggiungi" → `POST /api/symbols`; poi `FavoriteStar` → `POST /api/symbols/{id}/favorite`.
- `SymbolTable` (altri titoli monitorati): ticker, nome, prezzo, var %, ultima raccomandazione, stella preferito, cestino (DELETE con conferma).
- Riquadro "Ultima valutazione settimanale" (accuracy complessiva, link a /performance) + riquadro budget ("Budget: € X · Riserva liquidità: Y%").

**SymbolDetailPage** — al mount: `GET /api/symbols` (risolve ticker→id), poi `GET /api/symbols/{id}/prices?days=180&indicators=true`, `GET .../recommendations/latest`, `GET .../recommendations?limit=20` (poll 60 s)
- `PriceChart`: ComposedChart recharts — area close, linee SMA50/SMA200, bande Bollinger (Area trasparente), toggle checkbox indicatori ("Media mobile 50", "Media mobile 200", "Bande di Bollinger", "RSI" in sotto-grafico); `ReferenceDot` per ogni raccomandazione storica (verde=BUY, rosso=SELL, grigio=HOLD) con tooltip.
- `RecommendationCard`: azione tradotta ("COMPRA/VENDI/MANTIENI"), strategia ("Tutto subito/Ingresso graduale (DCA)/Ingresso parziale/Attendi"), confidenza, importo suggerito, stop loss/target, profitto stimato, `rationale_it`, verdetto validatore ("Approvata/Rivista/Bloccata dal validatore" + note), `PolicyChecksList` (elenco regole con ✓/✗ e `detail_it`), bottone **"Analizza ora"** → `POST /api/symbols/{id}/analyze` → poll `GET /api/runs/{run_id}` ogni 3 s con stato per-agente.
- `AgentBreakdown`: 4+2 card agente (nome italiano: "Analista Tecnico", "Analista Fondamentale", "Analista Macro", "Analista News Societarie", "Sintetizzatore", "Validatore Rischio") con stance, signal gauge, confidenza, `summary_it`.
- `RecommendationTimeline`: lista cronologica con esito realizzato (`realized_return_7d`, `outcome_score`) quando disponibile.

**PerformancePage** — `GET /api/evaluations?limit=12`, `GET /api/feedback?active_only=true`
- KPI ultima valutazione (accuratezza, rendimento medio realizzato, P&L ipotetico), `report_it`.
- `AccuracyTrendChart`: LineChart accuracy per agente sulle valutazioni.
- Tabella per-agente (accuratezza, errore medio, campioni) + sezione "Lezioni apprese" (`lessons_it` per agente).
- Bottone "Esegui valutazione ora" → `POST /api/evaluations/run`.

**SettingsPage** — `GET/PUT /api/settings`, `GET /api/health`
- Form: budget totale (€), profilo di rischio (radio: "Prudente (consigliato)", "Bilanciato", "Dinamico" con descrizione delle soglie), riserva liquidità %, max % per posizione, intervalli analisi. Salva → toast "Impostazioni salvate".
- Stato provider LLM (sola lettura): "OpenRouter: configurato ✓ (primario, modello X)", "Gemini: non configurato — aggiungi GEMINI_API_KEY nel file .env". Mai mostrare chiavi.

---

## 9. Dipendenze

**requirements.txt**
```
fastapi>=0.111,<0.116
uvicorn[standard]>=0.30,<0.35
sqlalchemy>=2.0.30,<2.1
pydantic>=2.7,<3
pydantic-settings>=2.3,<3
apscheduler>=3.10,<4.0
httpx>=0.27,<0.29
yfinance>=0.2.50,<0.3
feedparser>=6.0.11,<7
pandas>=2.2,<3
numpy>=1.26,<3
python-dotenv>=1.0,<2
pytest>=8.2,<9
pytest-asyncio>=0.23,<0.26
```

**frontend/package.json**
```json
{
  "name": "trade-max-frontend", "private": true, "version": "0.1.0", "type": "module",
  "scripts": { "dev": "vite", "build": "tsc -b && vite build", "preview": "vite preview" },
  "dependencies": {
    "react": "^18.3.1", "react-dom": "^18.3.1",
    "react-router-dom": "^6.26.2",
    "recharts": "^2.12.7",
    "clsx": "^2.1.1",
    "date-fns": "^3.6.0"
  },
  "devDependencies": {
    "@types/react": "^18.3.5", "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.1",
    "typescript": "~5.5.4",
    "vite": "^5.4.6",
    "tailwindcss": "^3.4.10", "postcss": "^8.4.45", "autoprefixer": "^10.4.20"
  }
}
```
`vite.config.ts`: `server.proxy = { "/api": "http://127.0.0.1:8000" }`. Produzione: `main.py` monta `frontend/dist` come StaticFiles (html=True) dopo i router API, con catch-all che serve `index.html` per le rotte SPA.

**.env.example**
```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openai/gpt-4o-mini
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
LLM_FALLBACK_ENABLED=true
DB_PATH=./trademax.db
HOST=127.0.0.1
PORT=8000
CORS_ORIGINS=http://localhost:5173
TZ_APP=Europe/Rome
```

---

## 10. Ordine di implementazione e parallelizzazione

**Fase 0 — Backbone (sequenziale, PRIMA di tutto):**
`backend/app/config.py` → `db.py` → `models.py` → `schemas.py` → `llm/` (providers, client, json_utils). Questi file definiscono ogni interfaccia; tutto il resto li importa e basta.

**Fase 1 — Moduli indipendenti (paralleli, nessuna dipendenza incrociata oltre il backbone):**
- **A. Data layer**: `data/sources.py`, `data/market.py`, `data/indicators.py`, `data/news.py` (+ `tests/test_indicators.py`).
- **B. Agenti**: `agents/*` (base + 6 attori).
- **C. Policy + Orchestrator**: `engine/policy.py` (+ `tests/test_policy.py`), `engine/orchestrator.py`.
- **D. Valutazione + Scheduler**: `evaluation/evaluator.py`, `evaluation/feedback.py`, `scheduler.py`.
- **E. API routes**: `api/*` (+ `tests/test_api_symbols.py`, `tests/conftest.py`).
- **F. Frontend completo**: tutta la cartella `frontend/` contro il contratto API §3–§4.

**Fase 2 — Integrazione:** `main.py` (router, lifespan con scheduler, static mount, seed `app_settings` id=1), `run.sh`, `README.md`, smoke test end-to-end.

Punti di frizione noti: (1) date UTC-naive serializzate ISO senza timezone — il frontend le tratta come UTC; (2) `output_json`/`policy_checks_json` sono Text nel DB ma `dict`/`list` negli schemi — conversione nei router; (3) yfinance è sincrono — chiamarlo con `asyncio.to_thread`.
