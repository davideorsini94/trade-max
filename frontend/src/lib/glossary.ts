/**
 * Plain-Italian glossary for the whole UI. Every info tooltip and the
 * /glossario page reads its copy from here via gloss(key), so an explanation
 * lives in exactly one place and is reused everywhere (never duplicated inline).
 *
 * Tone: term first, then a short (1-3 sentence) friendly explanation for
 * someone with no finance background. Keep the correct financial term visible.
 */
import type { Action, Sizing } from "../api/types";

export interface GlossEntry {
  term: string;
  short: string;
}

export const GLOSSARY: Record<string, GlossEntry> = {
  // --- Azioni e strategie ---
  action_buy: {
    term: "Compra",
    short:
      "Il sistema suggerisce di acquistare il titolo, perché si aspetta che il prezzo salga. Non è un ordine automatico: la decisione finale resta tua.",
  },
  action_sell: {
    term: "Vendi",
    short:
      "Il sistema suggerisce di vendere il titolo (o di non comprarlo), perché si aspetta che il prezzo scenda.",
  },
  action_hold: {
    term: "Mantieni",
    short:
      "Il sistema suggerisce di non fare nulla per ora: né comprare né vendere, in attesa di segnali più chiari.",
  },
  sizing_all_in: {
    term: "Tutto subito",
    short:
      "Investire l'intero importo suggerito in un'unica operazione, adesso. È la strategia più aggressiva: massimo guadagno possibile ma anche massimo rischio se il momento è sfortunato.",
  },
  sizing_dca: {
    term: "Ingresso graduale (DCA)",
    short:
      "Ingresso graduale (DCA): invece di investire tutto subito, l'importo viene diviso in più tranche settimanali, per ridurre il rischio di comprare tutto in un momento sfortunato.",
  },
  sizing_partial: {
    term: "Ingresso parziale",
    short:
      "Investire solo una parte dell'importo suggerito ora, tenendo il resto da parte per un momento migliore o per limitare il rischio.",
  },
  sizing_wait: {
    term: "Attendi",
    short:
      "Non investire adesso: le condizioni non sono abbastanza favorevoli. Meglio aspettare un segnale più chiaro.",
  },
  confidence: {
    term: "Confidenza",
    short:
      "Quanto il sistema è sicuro della propria raccomandazione, da 0% a 100%. Più è alta, più i segnali analizzati puntano nella stessa direzione. Non è una garanzia di guadagno.",
  },
  signal: {
    term: "Segnale",
    short:
      "Un valore da -1 a +1 che riassume l'opinione di un analista: vicino a +1 è molto positivo (rialzista), vicino a -1 è molto negativo (ribassista), intorno a 0 è neutro.",
  },
  stance: {
    term: "Orientamento",
    short:
      "La posizione di un analista sul titolo: Rialzista (si aspetta che salga), Ribassista (si aspetta che scenda) o Neutrale (nessuna direzione chiara).",
  },

  // --- Indicatori tecnici ---
  sma50: {
    term: "Media mobile a 50 giorni",
    short:
      "Il prezzo medio del titolo negli ultimi 50 giorni, aggiornato ogni giorno. Mostra la tendenza di breve-medio periodo: se il prezzo è sopra la media, il trend recente è positivo.",
  },
  sma200: {
    term: "Media mobile a 200 giorni",
    short:
      "Il prezzo medio degli ultimi 200 giorni. Indica la tendenza di lungo periodo: molti la usano come spartiacque tra fase rialzista e ribassista.",
  },
  bollinger: {
    term: "Bande di Bollinger",
    short:
      "Due linee che formano un corridoio intorno al prezzo, in base a quanto oscilla. Quando il prezzo tocca la banda alta è considerato caro, quando tocca quella bassa è considerato conveniente.",
  },
  rsi: {
    term: "RSI",
    short:
      "Indice di forza relativa (RSI), da 0 a 100: misura quanto in fretta il prezzo è salito o sceso di recente. Sopra 70 il titolo è ipercomprato (salito molto in fretta, possibile pausa o ribasso), sotto 30 è ipervenduto.",
  },
  atr: {
    term: "ATR",
    short:
      "Ampiezza media di oscillazione (ATR): di quanto si muove in media il prezzo in una giornata. Un ATR alto indica un titolo molto mosso; si usa per calibrare stop loss e dimensione della posizione.",
  },
  volatility: {
    term: "Volatilità",
    short:
      "Quanto oscilla il prezzo di un titolo. Alta volatilità significa movimenti ampi e imprevedibili: più possibilità di guadagno ma anche più rischio.",
  },
  drawdown: {
    term: "Drawdown",
    short:
      "La perdita massima dal punto più alto a quello più basso in un certo periodo. Misura quanto un investimento potrebbe scendere nei momenti peggiori.",
  },
  macd: {
    term: "MACD",
    short:
      "Indicatore che confronta due medie mobili per cogliere i cambi di tendenza. Quando la sua linea supera la linea segnale è un indizio rialzista; quando scende sotto, ribassista.",
  },
  change_pct_1d: {
    term: "Variazione giornaliera",
    short:
      "Di quanto è cambiato il prezzo del titolo rispetto alla chiusura del giorno precedente, in percentuale. Verde se è salito, rosso se è sceso.",
  },

  // --- Scheda del titolo: quotazione ---
  open_price: {
    term: "Apertura",
    short:
      "Il prezzo a cui il titolo ha iniziato gli scambi nell'ultima giornata di borsa.",
  },
  day_range: {
    term: "Minimo e massimo di giornata",
    short:
      "Il prezzo più basso e più alto toccati dal titolo nell'ultima giornata di scambi. Dà un'idea di quanto si è mosso il prezzo in giornata.",
  },
  prev_close: {
    term: "Chiusura precedente",
    short:
      "Il prezzo a cui il titolo ha chiuso la giornata di borsa precedente. È il riferimento da cui si calcola la variazione giornaliera.",
  },
  volume: {
    term: "Volume",
    short:
      "Quante azioni sono state scambiate nell'ultima giornata. Un volume alto indica molto interesse e scambi; uno basso, poca attività.",
  },
  avg_volume: {
    term: "Volume medio (30 giorni)",
    short:
      "La media delle azioni scambiate ogni giorno nell'ultimo mese. Serve da riferimento per capire se gli scambi di oggi sono più intensi o più fiacchi del solito.",
  },
  week52_range: {
    term: "Minimo e massimo a 52 settimane",
    short:
      "Il prezzo più basso e più alto raggiunti dal titolo nell'ultimo anno. Aiuta a capire se il prezzo attuale è vicino ai suoi estremi recenti.",
  },

  // --- Scheda del titolo: valutazione ---
  pe_ratio: {
    term: "P/E (prezzo/utili)",
    short:
      "Rapporto tra il prezzo dell'azione e gli utili per azione: quanto si paga per ogni euro di utili prodotti dall'azienda. Un P/E alto indica aspettative di crescita (o un titolo caro), uno basso un titolo più conveniente (o poca fiducia).",
  },
  forward_pe: {
    term: "P/E atteso",
    short:
      "Come il P/E, ma calcolato sugli utili futuri stimati dagli analisti invece che su quelli già realizzati. Indica quanto il titolo è valutato rispetto ai guadagni previsti.",
  },
  eps: {
    term: "EPS (utile per azione)",
    short:
      "L'utile netto dell'azienda diviso per il numero di azioni: quanto guadagna la società per ogni singola azione. Più è alto, più l'azienda è redditizia per azione.",
  },
  dividend_yield: {
    term: "Rendimento da dividendo",
    short:
      "La cedola annuale (dividendo) pagata dall'azienda espressa in percentuale del prezzo dell'azione. Indica quanto rende il titolo in dividendi, al di là delle variazioni di prezzo.",
  },
  beta: {
    term: "Beta",
    short:
      "Misura quanto il titolo si muove rispetto al mercato nel suo complesso. Un beta di 1 significa che segue il mercato; sopra 1 oscilla più del mercato (più rischio), sotto 1 di meno (più stabile).",
  },
  analyst_target: {
    term: "Target degli analisti",
    short:
      "Il prezzo medio che gli analisti finanziari si aspettano per il titolo nei prossimi 12 mesi. È una stima di consenso, non una garanzia: va confrontata con il prezzo attuale.",
  },
  sector: {
    term: "Settore",
    short:
      "L'area di attività a cui appartiene l'azienda (per esempio tecnologia, sanità, energia). Aiuta a inquadrare il titolo e a confrontarlo con aziende simili.",
  },

  // --- Mercato (universo titoli) ---
  composite: {
    term: "Consigliati",
    short:
      "Ordinamento che combina fama, andamento, valore e affidabilità in un unico punteggio, per portare in alto i titoli più interessanti nel complesso.",
  },
  fame: {
    term: "Fama",
    short:
      "Quanto la società è conosciuta nel mondo, da 1 a 5 stelle: 5 stelle sono i colossi che tutti conoscono, 1 stella le aziende meno note. Non misura la qualità dell'investimento, solo la notorietà.",
  },
  trend_30d: {
    term: "Andamento a 30 giorni",
    short:
      "Di quanto è cambiato il prezzo nell'ultimo mese, in percentuale. Verde se è salito, rosso se è sceso: dà un'idea della tendenza recente del titolo.",
  },
  market_value: {
    term: "Valore (capitalizzazione di mercato)",
    short:
      "Quanto vale l'intera azienda in borsa, cioè il prezzo di un'azione moltiplicato per tutte le azioni esistenti. Si esprime in miliardi (mld): più è alto, più l'azienda è grande.",
  },
  reliability: {
    term: "Affidabilità",
    short:
      "Punteggio da 0 a 100 che unisce bassa volatilità, un trend di lungo periodo sano e grandi dimensioni dell'azienda. Più è alto, più il titolo è considerato stabile e solido; non è una garanzia di guadagno.",
  },
  market_open: {
    term: "Mercato aperto",
    short:
      "Indica se la borsa di riferimento è aperta agli scambi in questo momento. A mercato chiuso i prezzi non si aggiornano.",
  },

  // --- Rischio e protezioni ---
  stop_loss: {
    term: "Stop loss",
    short:
      "Prezzo di uscita di sicurezza: se il titolo scende fin qui, conviene vendere per limitare la perdita. Serve a proteggere il capitale quando le cose vanno male.",
  },
  take_profit: {
    term: "Take profit",
    short:
      "Prezzo obiettivo a cui conviene incassare il guadagno: se il titolo sale fin qui, si vende per mettere al sicuro il profitto.",
  },
  entry_price: {
    term: "Prezzo di ingresso",
    short: "Il prezzo indicativo a cui il sistema suggerisce di comprare (o vendere) il titolo.",
  },
  horizon_days: {
    term: "Orizzonte",
    short:
      "Per quanti giorni ci si aspetta che la raccomandazione resti valida. Indica se l'idea è di breve o di medio periodo.",
  },
  estimated_profit: {
    term: "Profitto stimato",
    short:
      "Il guadagno che il sistema ipotizza se la raccomandazione va a buon fine, in percentuale e in valore. È solo una stima, non una promessa.",
  },
  allocation_pct: {
    term: "% del budget",
    short: "La quota del tuo budget totale che il sistema suggerisce di investire in questo titolo.",
  },
  allocation_amount: {
    term: "Importo suggerito",
    short:
      "La somma di denaro che il sistema propone di investire in questo titolo. Deriva dalla percentuale del budget applicata al tuo capitale.",
  },
  dca_tranches: {
    term: "Tranche DCA",
    short:
      "In quante parti (tranche) viene diviso l'investimento quando si sceglie l'ingresso graduale. Ogni tranche viene investita in un momento diverso.",
  },
  budget: {
    term: "Budget totale",
    short:
      "Il capitale complessivo che hai deciso di destinare agli investimenti. Tutte le percentuali e gli importi suggeriti sono calcolati su questa cifra.",
  },
  cash_reserve: {
    term: "Riserva di liquidità",
    short:
      "La parte del budget che resta sempre in contanti, non investita. Serve come cuscinetto di sicurezza e per cogliere occasioni future.",
  },
  max_position: {
    term: "Massimo per posizione",
    short:
      "La quota massima del budget che può essere investita in un singolo titolo. Evita di concentrare troppo capitale su un'unica scommessa.",
  },
  risk_profile: {
    term: "Profilo di rischio",
    short:
      "Quanto rischio sei disposto ad accettare. Un profilo più prudente riduce le dimensioni delle posizioni e alza le soglie di sicurezza; uno più dinamico fa il contrario.",
  },

  // --- Attori dell'analisi ---
  agent_technical: {
    term: "Analista Tecnico",
    short:
      "Studia il grafico del prezzo: tendenze, medie mobili, forza del movimento e indicatori come RSI e MACD. Non guarda i conti dell'azienda, solo l'andamento del titolo.",
  },
  agent_fundamentals: {
    term: "Analista Fondamentale",
    short:
      "Valuta la solidità dell'azienda e se il titolo è caro o conveniente, guardando conti, utili e valutazione.",
  },
  agent_macro: {
    term: "Analista Macro",
    short:
      "Guarda il quadro generale dell'economia (tassi, inflazione, contesto dei mercati) per capire se il momento è favorevole o meno.",
  },
  agent_corporate: {
    term: "Analista News Societarie",
    short:
      "Segue le notizie sull'azienda (trimestrali, annunci, eventi) che possono muovere il prezzo nel breve periodo.",
  },
  synthesizer: {
    term: "Sintetizzatore",
    short:
      "L'attore che mette insieme le opinioni di tutti gli analisti e le trasforma in un'unica raccomandazione con azione, importo e livelli di prezzo.",
  },
  validator: {
    term: "Validatore rischio",
    short:
      "Un controllo finale che verifica la raccomandazione dal punto di vista del rischio. Può approvarla, correggerla o bloccarla se la ritiene troppo pericolosa.",
  },
  llm_model: {
    term: "Modello (LLM)",
    short:
      "Il 'cervello' di intelligenza artificiale usato dagli attori per analizzare i dati. Modelli diversi hanno costi e qualità diverse; puoi sceglierne uno economico per gli analisti e uno più potente per il sintetizzatore.",
  },

  // --- Valutazione settimanale ---
  weekly_evaluation: {
    term: "Valutazione settimanale",
    short:
      "Un controllo periodico che confronta le raccomandazioni passate con ciò che è realmente accaduto, per misurare i risultati e correggere la rotta.",
  },
  accuracy: {
    term: "Accuratezza",
    short:
      "La percentuale di raccomandazioni passate che si sono rivelate corrette. Aiuta a capire quanto fidarsi del sistema, ma i risultati passati non garantiscono quelli futuri.",
  },
  avg_signal_error: {
    term: "Errore medio segnale",
    short:
      "Di quanto, in media, il segnale di un analista si è discostato da ciò che è poi successo davvero. Più è basso, più quell'analista è stato preciso.",
  },
  n_samples: {
    term: "Campioni",
    short:
      "Quante raccomandazioni sono state usate per calcolare le metriche. Con pochi campioni i numeri sono meno affidabili.",
  },
  hypothetical_pnl: {
    term: "P&L ipotetico",
    short:
      "Il guadagno o la perdita (Profit & Loss) che avresti ottenuto seguendo tutte le raccomandazioni. È un calcolo teorico, non denaro reale.",
  },
  realized_return_7d: {
    term: "Reso realizzato",
    short:
      "Quanto ha effettivamente reso il titolo nei giorni successivi alla raccomandazione, in percentuale. Serve a verificare a posteriori se il consiglio era buono.",
  },
  outcome_score: {
    term: "Punteggio esito",
    short:
      "Un voto sintetico che misura quanto la raccomandazione si è rivelata azzeccata, considerando direzione e risultato. Più è alto, meglio è.",
  },
  lessons: {
    term: "Lezioni apprese",
    short:
      "Le cose che il sistema ha imparato dagli errori e dai successi delle settimane precedenti, e che usa per migliorare le analisi future.",
  },
  data_quality: {
    term: "Qualità dei dati",
    short:
      "Quanto sono completi e affidabili i dati usati per l'analisi. Se i dati sono scarsi o vecchi, la raccomandazione va presa con più cautela.",
  },

  // --- Regole di sicurezza ---
  policy_engine: {
    term: "Motore di policy",
    short:
      "Un insieme di regole di sicurezza automatiche che controllano ogni raccomandazione e possono modificarla o bloccarla per proteggere il capitale. Sono regole fisse, non decise dall'intelligenza artificiale.",
  },
  favorites_priority: {
    term: "Priorità ai preferiti",
    short:
      "I titoli che segni con la stella (preferiti) vengono analizzati più spesso degli altri, così le loro raccomandazioni sono sempre più aggiornate.",
  },
  validator_veto: {
    term: "Veto del validatore",
    short:
      "Quando il controllo del rischio giudica la raccomandazione troppo pericolosa e la blocca del tutto.",
  },
  validator_revise: {
    term: "Revisione del validatore",
    short:
      "Quando il controllo del rischio non blocca la raccomandazione ma la corregge, ad esempio riducendo l'importo o rendendola più prudente.",
  },
  min_confidence: {
    term: "Confidenza minima",
    short:
      "Sotto una certa soglia di confidenza la raccomandazione non porta a comprare: se il sistema non è abbastanza sicuro, si preferisce non rischiare.",
  },
  trend_filter: {
    term: "Filtro di trend",
    short:
      "Evita di comprare un titolo che è chiaramente in tendenza ribassista: meglio non remare contro la corrente del mercato.",
  },
  falling_knife: {
    term: "Rischio caduta libera",
    short:
      "Impedisce di comprare un titolo che sta crollando velocemente (il cosiddetto 'coltello che cade'), perché il prezzo potrebbe continuare a scendere.",
  },
  cooldown: {
    term: "Raffreddamento post-inversione",
    short:
      "Dopo un brusco cambio di direzione del prezzo, il sistema aspetta un po' prima di agire, per evitare di reagire a un movimento passeggero.",
  },
  volatility_sizing: {
    term: "Dimensionamento su volatilità",
    short:
      "Riduce l'importo investito quando il titolo è molto volatile (oscilla tanto): più incertezza, meno capitale a rischio.",
  },
  all_in_gate: {
    term: "Limite ingresso totale",
    short:
      "Consente di investire tutto in una volta sola solo quando le condizioni sono davvero favorevoli; altrimenti impone un ingresso più graduale.",
  },
  position_cap: {
    term: "Tetto massimo posizione",
    short:
      "Impedisce di superare la quota massima consentita per un singolo titolo, così nessuna posizione diventa troppo grande.",
  },
  cash_reserve_rule: {
    term: "Riserva di liquidità (regola)",
    short:
      "Garantisce che una parte del budget resti sempre in contanti, non investita, come margine di sicurezza.",
  },
  stop_loss_required: {
    term: "Stop loss obbligatorio",
    short:
      "Ogni raccomandazione di acquisto deve avere un prezzo di uscita di sicurezza (stop loss), così la perdita massima è sempre definita in anticipo.",
  },
  confidence_cap: {
    term: "Tetto di confidenza",
    short:
      "Impedisce alla confidenza di salire troppo: nessuna raccomandazione viene presentata come una certezza assoluta.",
  },
  hold_normalization: {
    term: "Normalizzazione MANTIENI",
    short:
      "Quando l'azione consigliata è Mantieni, azzera importi e livelli di prezzo, perché in quel caso non c'è nulla da comprare o vendere.",
  },
};

/** Returns the plain-Italian explanation for a glossary key, or "" if unknown. */
export function gloss(key: string): string {
  return GLOSSARY[key]?.short ?? "";
}

/** Maps a recommendation Action to its glossary key. */
export const ACTION_GLOSS_KEY: Record<Action, string> = {
  BUY: "action_buy",
  SELL: "action_sell",
  HOLD: "action_hold",
};

/** Maps a sizing strategy to its glossary key. */
export const SIZING_GLOSS_KEY: Record<Sizing, string> = {
  ALL_IN: "sizing_all_in",
  DCA: "sizing_dca",
  PARTIAL: "sizing_partial",
  WAIT: "sizing_wait",
};

/**
 * The policy-rule identifier used by the backend (PolicyCheck.rule) maps 1:1 to
 * a glossary key, except "cash_reserve" (the budget concept already owns that
 * key) whose rule explanation lives under "cash_reserve_rule".
 */
export function policyGlossKey(rule: string): string {
  return rule === "cash_reserve" ? "cash_reserve_rule" : rule;
}

/** Section grouping for the /glossario page; every GLOSSARY key appears once. */
export const GLOSSARY_SECTIONS: ReadonlyArray<{ title: string; keys: readonly string[] }> = [
  {
    title: "Azioni e strategie",
    keys: [
      "action_buy",
      "action_sell",
      "action_hold",
      "sizing_all_in",
      "sizing_dca",
      "sizing_partial",
      "sizing_wait",
      "confidence",
      "signal",
      "stance",
    ],
  },
  {
    title: "Indicatori tecnici",
    keys: [
      "sma50",
      "sma200",
      "bollinger",
      "rsi",
      "atr",
      "macd",
      "volatility",
      "change_pct_1d",
      "market_open",
    ],
  },
  {
    title: "Scheda del titolo",
    keys: [
      "open_price",
      "day_range",
      "prev_close",
      "volume",
      "avg_volume",
      "week52_range",
      "pe_ratio",
      "forward_pe",
      "eps",
      "dividend_yield",
      "beta",
      "analyst_target",
      "sector",
    ],
  },
  {
    title: "Mercato",
    keys: ["composite", "fame", "trend_30d", "market_value", "reliability"],
  },
  {
    title: "Rischio e protezioni",
    keys: [
      "stop_loss",
      "take_profit",
      "entry_price",
      "horizon_days",
      "estimated_profit",
      "allocation_pct",
      "allocation_amount",
      "dca_tranches",
      "budget",
      "cash_reserve",
      "max_position",
      "risk_profile",
      "drawdown",
    ],
  },
  {
    title: "Attori dell'analisi",
    keys: [
      "agent_technical",
      "agent_fundamentals",
      "agent_macro",
      "agent_corporate",
      "synthesizer",
      "validator",
      "llm_model",
    ],
  },
  {
    title: "Valutazione settimanale",
    keys: [
      "weekly_evaluation",
      "accuracy",
      "avg_signal_error",
      "n_samples",
      "hypothetical_pnl",
      "realized_return_7d",
      "outcome_score",
      "lessons",
      "data_quality",
    ],
  },
  {
    title: "Regole di sicurezza",
    keys: [
      "policy_engine",
      "favorites_priority",
      "validator_veto",
      "validator_revise",
      "min_confidence",
      "trend_filter",
      "falling_knife",
      "cooldown",
      "volatility_sizing",
      "all_in_gate",
      "position_cap",
      "cash_reserve_rule",
      "stop_loss_required",
      "confidence_cap",
      "hold_normalization",
    ],
  },
];
