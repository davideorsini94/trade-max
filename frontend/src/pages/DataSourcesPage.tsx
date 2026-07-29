interface SourceItem {
  name: string;
  detail?: string;
}

interface SourceSection {
  title: string;
  intro: string;
  items: SourceItem[];
  note?: string;
}

// Kept in sync by hand with backend/app/data/sources.py and the yfinance calls
// in backend/app/data/market.py — if a source changes there, update it here too.
const SECTIONS: SourceSection[] = [
  {
    title: "Prezzi, storico e volumi",
    intro:
      "Quotazioni, grafico dell'andamento, apertura/chiusura, minimi e massimi, volumi: tutto arriva da Yahoo Finance tramite la libreria yfinance, gratuita e senza chiave API.",
    items: [
      { name: "Yahoo Finance (yfinance)", detail: "prezzi in tempo quasi reale (ritardo tipico ~15 minuti), storico giornaliero e orario" },
      { name: "Indici di riferimento ed ETF settoriali SPDR", detail: "per la forza relativa l'andamento del titolo è confrontato con l'indice della sua borsa (es. S&P 500, FTSE MIB) e con l'ETF settoriale SPDR corrispondente al suo settore usato come proxy (es. XLK per la tecnologia, XLF per i finanziari), anch'essi da Yahoo Finance" },
    ],
    note:
      "I dati sono gratuiti e non garantiti: possono avere ritardi, piccole discrepanze o mancare del tutto per titoli meno seguiti.",
  },
  {
    title: "Dati fondamentali e scheda del titolo",
    intro:
      "P/E, P/E atteso, EPS, capitalizzazione, dividendo, beta, settore, industria, target degli analisti: letti dalla scheda informativa di Yahoo Finance per ogni titolo.",
    items: [
      { name: "Yahoo Finance (yfinance) — dati societari" },
      { name: "Yahoo Finance (yfinance) — stime e revisioni degli analisti", detail: "utile atteso (EPS) e crescita dei ricavi stimati per l'anno fiscale corrente e il prossimo, con la variazione del consenso negli ultimi 7/30/90 giorni e quanti analisti hanno alzato o tagliato le stime: revisioni in salita rafforzano il quadro, revisioni in discesa sono un campanello d'allarme anche quando il P/E sembra a buon mercato" },
    ],
  },
  {
    title: "Analista Sentiment (consenso, insider, istituzionali)",
    intro:
      "Consenso e revisioni degli analisti professionali, acquisti/vendite degli insider aziendali, quote in mano a investitori istituzionali, percentuale di vendite allo scoperto: anche questi arrivano da Yahoo Finance.",
    items: [{ name: "Yahoo Finance (yfinance) — raccomandazioni, insider trading, azionariato istituzionale" }],
  },
  {
    title: "Contesto di mercato (regime)",
    intro:
      "Per capire il clima generale dei mercati (non il singolo titolo), TradeMax legge da Yahoo Finance alcuni indicatori globali. Servono all'Analista Macro e al Validatore del rischio per giudicare se è un momento di calma o di tensione: quando la paura sale conviene investire meno e con stop più stretti.",
    items: [
      { name: "VIX", detail: "l'indice della paura: misura quanta turbolenza i mercati si aspettano; valori alti (sopra ~25) indicano nervosismo" },
      { name: "Rendimenti dei Treasury USA (10 anni e 3 mesi)", detail: "i tassi sui titoli di Stato americani e la loro differenza (curva dei rendimenti): quando quella a lungo termine scende sotto quella a breve la curva è \"invertita\", un classico segnale di possibile recessione" },
      { name: "Cambio EUR/USD", detail: "quanti dollari vale un euro, per il contesto valutario" },
      { name: "Oro e petrolio (WTI)", detail: "variazione a 30 giorni delle materie prime, spesso spia di inflazione e avversione al rischio" },
      { name: "Rapporto HYG/LQD", detail: "confronto tra obbligazioni ad alto rendimento e quelle di qualità: se il rapporto scende gli spread creditizi si allargano, segnale di avversione al rischio (risk-off)" },
    ],
    note:
      "Sono dati di mercato calcolati, non notizie: quando un valore manca resta semplicemente vuoto, senza inventarlo.",
  },
  {
    title: "Notizie macroeconomiche",
    intro:
      "L'Analista Macro legge solo queste fonti selezionate e verificate (nessuna ricerca libera sul web), per garantire notizie da fonti ufficiali e affidabili:",
    items: [
      { name: "Federal Reserve", detail: "comunicati ufficiali della banca centrale statunitense" },
      { name: "BCE", detail: "comunicati della Banca Centrale Europea" },
      { name: "Bank of England", detail: "comunicati della banca centrale britannica" },
      { name: "ESMA", detail: "Autorità europea degli strumenti finanziari e dei mercati (regolatore UE)" },
      { name: "US Bureau of Labor Statistics", detail: "dati ufficiali su inflazione, occupazione e indicatori economici USA" },
      { name: "CNBC Top News" },
      { name: "CNBC Economy" },
      { name: "MarketWatch" },
      { name: "Il Sole 24 Ore — Finanza & Mercati" },
      { name: "BBC News — Mondo", detail: "cronaca internazionale e conflitti" },
      { name: "Nazioni Unite", detail: "aggiornamenti ufficiali su crisi e conflitti in corso" },
      { name: "Il Sole 24 Ore — Mondo", detail: "scenario internazionale in italiano" },
      { name: "CNBC Energia", detail: "petrolio, gas ed elettricità: è il canale principale attraverso cui una guerra arriva ai mercati" },
      { name: "US EIA — Today in Energy", detail: "statistiche e analisi ufficiali dell'agenzia americana per l'energia" },
      { name: "Commissione Europea", detail: "comunicati ufficiali dell'UE, incluse le decisioni sulle sanzioni" },
    ],
    note:
      "I titoli non vengono scelti solo per data di pubblicazione: ogni notizia viene classificata in cinque temi (politica monetaria, inflazione e lavoro, geopolitica e conflitti, energia e materie prime, regolamentazione) e ciascun tema ha dei posti riservati. Serve a evitare che una fonte che pubblica molto spesso riempia lo spazio disponibile e faccia sparire, per esempio, una notizia di conflitto rilevante per i mercati.",
  },
  {
    title: "Notizie societarie",
    intro:
      "L'Analista News Societarie legge solo queste fonti per ogni singolo titolo monitorato:",
    items: [
      { name: "Yahoo Finance", detail: "notizie specifiche sul titolo" },
      { name: "SEC EDGAR", detail: "comunicazioni ufficiali obbligatorie (modulo 8-K) depositate presso l'autorità di vigilanza americana" },
      { name: "CNBC Business" },
    ],
  },
  {
    title: "Pagina Mercato (universo titoli)",
    intro:
      "L'elenco dei titoli più conosciuti mostrato in Mercato combina due cose: un elenco curato a mano (nome, borsa, settore e un punteggio di fama assegnato da noi) con dati di prezzo e affidabilità aggiornati da Yahoo Finance.",
    items: [{ name: "Elenco curato interno + Yahoo Finance (yfinance)" }],
  },
  {
    title: "Ricerca titoli e codice ISIN",
    intro: "Cercando un titolo per nome, ticker o codice ISIN, la ricerca interroga:",
    items: [{ name: "Yahoo Finance — motore di ricerca titoli" }],
  },
];

export default function DataSourcesPage() {
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-bold text-slate-50">Fonti dei dati</h1>
        <p className="mt-1 max-w-2xl text-sm leading-relaxed text-slate-400">
          Da dove arrivano le informazioni che TradeMax usa per analizzare i titoli e generare i
          consigli. Nessun dato viene inventato: ogni agente lavora solo con ciò che legge da
          queste fonti, e quando i dati mancano lo dichiara apertamente (qualità dei dati "scarsa"
          o analisi saltata).
        </p>
      </div>

      <div className="space-y-6">
        {SECTIONS.map((section) => (
          <section
            key={section.title}
            className="rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] p-5 shadow-card"
          >
            <h2 className="text-sm font-semibold text-slate-100">{section.title}</h2>
            <p className="mt-1.5 text-sm leading-relaxed text-slate-400">{section.intro}</p>
            <ul className="mt-3 space-y-1.5">
              {section.items.map((item) => (
                <li key={item.name} className="flex flex-wrap items-baseline gap-x-1.5 text-sm">
                  <span className="font-medium text-slate-200">{item.name}</span>
                  {item.detail ? <span className="text-slate-500">— {item.detail}</span> : null}
                </li>
              ))}
            </ul>
            {section.note ? <p className="mt-3 text-xs text-slate-500">{section.note}</p> : null}
          </section>
        ))}
      </div>

      <p className="max-w-2xl text-xs leading-relaxed text-slate-500">
        Tutte le fonti di notizie sono un elenco fisso deciso dall'applicazione: nessun agente può
        cercare o inventare fonti diverse da quelle elencate qui sopra.
      </p>
    </div>
  );
}
