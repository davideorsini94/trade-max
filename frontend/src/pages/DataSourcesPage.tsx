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
    ],
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
