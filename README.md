# TradeMax

Applicazione **locale** di monitoraggio azioni e supporto alle decisioni di investimento, basata su una pipeline multi-agente LLM con policy di rischio prudente e ciclo di auto-miglioramento settimanale.

> ⚠️ **TradeMax è uno strumento sperimentale a scopo informativo. Non costituisce consulenza finanziaria. Non esegue ordini. Le decisioni di investimento sono a tuo esclusivo rischio.**

## Come funziona

```
yfinance ──► prezzi + indicatori ─┐
RSS whitelist ──► notizie ────────┤
                                  ▼
             ┌────────────────────────────────────┐
             │  5 ANALISTI (in parallelo)         │
             │  • Tecnico (prezzi, RSI, MACD, …)  │
             │  • Fondamentale (P/E, debito, …)   │
             │  • Macro (Fed, BCE, stampa fin.)   │
             │  • News societarie / persone       │
             │    influenti (SEC, Yahoo, CNBC)    │
             │  • Sentiment (consensus, insider)  │
             └────────────────┬───────────────────┘
                              ▼
                     SINTETIZZATORE  (proposta: azione, sizing, stop, target)
                              ▼
                 VALIDATORE DEL RISCHIO  (avversariale, con potere di VETO)
                              ▼
             POLICY ENGINE deterministico (13 regole "safe", codice puro)
                              ▼
                       RACCOMANDAZIONE  (COMPRA / VENDI / MANTIENI + sizing)
```

- **Fonti**: solo whitelist curata (Federal Reserve, BCE, SEC EDGAR, CNBC, MarketWatch, Il Sole 24 Ore, Yahoo Finance). Nessun URL dinamico deciso dagli LLM.
- **Policy prudente** (profilo di default): veto del validatore inappellabile, confidenza minima per comprare/vendere, niente acquisti in death-cross o dopo drawdown >20%, DCA forzato con alta volatilità, max 15% del budget per posizione, riserva di liquidità minima 30%, stop-loss obbligatorio entro l'8%, "all-in" mai consentito, cooldown 72h tra segnali opposti.
- **Auto-miglioramento**: ogni domenica alle 18:00 le raccomandazioni con almeno 7 giorni di vita vengono confrontate con i rendimenti reali; l'accuratezza viene attribuita a ogni singolo agente e un "coach" LLM genera lezioni che vengono iniettate nei prompt di tutti gli agenti nelle analisi successive.
- **Portafoglio simulato**: ogni consiglio BUY apre una posizione in un libro fittizio, chiusa poi da stop-loss, take-profit, scadenza dell'orizzonte o da un consiglio SELL, usando solo le barre giornaliere già salvate; la pagina Performance ne mostra P&L, MAE/MFE e motivo di chiusura. Puoi anche registrare a mano le tue operazioni (importo, commissione, data): restano un diario fittizio — nessun ordine viene eseguito — ma sintetizzatore e validatore ne tengono conto.
- **Preferiti**: i simboli marcati con ★ sono considerati posseduti/di interesse e ottengono priorità (refresh prezzi ogni 15 min invece che ogni ora; l'analisi viene ricontrollata ogni ora a mercato aperto invece che una volta al giorno alle 16:00). L'intervallo minimo fra due analisi dello stesso titolo è 24h, regolabile da Impostazioni.
- **Mercato**: pagina con un universo curato di ~180 titoli famosi (USA, Europa, FTSE MIB) con dati reali, ordinabili per fama, andamento 30g, valore e affidabilità; da lì si aggiungono al monitoraggio o ai preferiti.
- **Per i non esperti**: ogni parametro della UI ha un tooltip "ⓘ" con la spiegazione in italiano semplice, più una pagina Glossario; anche gli agenti LLM spiegano i termini finanziari in modo divulgativo.
- **Modelli LLM configurabili**: dalla pagina Impostazioni scegli il modello per OpenRouter/Gemini/Ollama (lista letta live dalle API, per Ollama dai modelli installati sul server) e, volendo, un modello diverso per ogni singolo agente.
- **Costi ottimizzati**: payload compattati, notizie limitate, agenti saltati deterministicamente quando non hanno dati da analizzare, log dei token consumati per ogni chiamata.

## Requisiti

- Python 3.12+, Node 20+
- Una chiave [OpenRouter](https://openrouter.ai/) e/o una chiave [Gemini](https://aistudio.google.com/), oppure un server Ollama locale (`OLLAMA_BASE_URL`) (provider primario configurabile, fallback automatico)

## Avvio rapido con Docker (consigliato)

```bash
cp .env.example .env        # inserisci OPENROUTER_API_KEY e/o GEMINI_API_KEY
docker compose up -d --build
# → http://localhost:8500   (il DB persiste in ./data/trademax.db)
```

## Setup manuale (sviluppo)

```bash
# 1. backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. frontend
cd frontend && npm install && npm run build && cd ..

# 3. configurazione
cp .env.example .env
#   → inserisci OPENROUTER_API_KEY e/o GEMINI_API_KEY
```

```bash
./run.sh              # http://127.0.0.1:8000  (API + frontend buildato)
./run.sh --front      # in più: Vite dev server su http://localhost:5173 (hot reload)
```

## Test

```bash
cd backend && ../.venv/bin/pytest
```

## Struttura

| Percorso | Contenuto |
|---|---|
| `backend/app/agents/` | I 7 agenti LLM (5 analisti, sintetizzatore, validatore) |
| `backend/app/engine/` | Policy engine deterministico + orchestratore della pipeline |
| `backend/app/evaluation/` | Valutazione settimanale + generazione lezioni (feedback loop) |
| `backend/app/data/` | yfinance, indicatori tecnici (pandas), notizie RSS whitelist |
| `backend/app/scheduler.py` | Job periodici (prezzi, analisi, valutazione domenicale) |
| `backend/app/api/` | REST API (`/api/*`) |
| `frontend/` | UI React (dashboard, dettaglio simbolo, performance, impostazioni) |
| `docs/BLUEPRINT.md` | Progetto tecnico completo |

## Note e limiti (v1)

- I dati di prezzo yfinance hanno ~15 minuti di ritardo; l'orario di mercato è calcolato per singola borsa dal suffisso del ticker (senza suffisso = USA, 9:30–16:00 New York; `.MI`/`.DE`/`.PA`/… 9:00–17:30 locali; `.L` 8:00–16:30 Londra), lun–ven, senza festività.
- Il "profitto stimato" è la stima del sintetizzatore sull'orizzonte indicato, ribadita dal validatore e dalla policy: è un'ipotesi, non una promessa.
- Il database è un file SQLite (`backend/trademax.db`), creato al primo avvio.
