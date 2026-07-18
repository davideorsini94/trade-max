/**
 * Italian copy shared across components: enum -> label translations and the
 * six-agent registry (mirrors backend/app/agents/__init__.py ANALYST_AGENTS
 * plus the synthesizer/validator, see BLUEPRINT.md section 5.4).
 */
import type { Action, RiskProfile, RunStatus, Sizing, Stance, Verdict } from "../api/types";

export const ACTION_LABELS_IT: Record<Action, string> = {
  BUY: "COMPRA",
  SELL: "VENDI",
  HOLD: "MANTIENI",
};

export const ACTION_LONG_LABELS_IT: Record<Action, string> = {
  BUY: "Comprare",
  SELL: "Vendere",
  HOLD: "Mantenere",
};

export const SIZING_LABELS_IT: Record<Sizing, string> = {
  ALL_IN: "Tutto subito",
  DCA: "Ingresso graduale (DCA)",
  PARTIAL: "Ingresso parziale",
  WAIT: "Attendi",
};

export const VERDICT_LABELS_IT: Record<Verdict, string> = {
  APPROVE: "Approvata",
  REVISE: "Rivista",
  VETO: "Bloccata dal validatore",
};

export const STANCE_LABELS_IT: Record<Stance, string> = {
  BULLISH: "Rialzista",
  BEARISH: "Ribassista",
  NEUTRAL: "Neutrale",
};

export const RUN_STATUS_LABELS_IT: Record<RunStatus, string> = {
  PENDING: "In coda",
  RUNNING: "In corso",
  COMPLETED: "Completata",
  FAILED: "Fallita",
};

export const RISK_PROFILE_LABELS_IT: Record<RiskProfile, string> = {
  prudente: "Prudente (consigliato)",
  bilanciato: "Bilanciato",
  dinamico: "Dinamico",
};

export const RISK_PROFILE_DESCRIPTIONS_IT: Record<RiskProfile, string> = {
  prudente:
    "Capitale al sicuro prima di tutto: posizioni fino al 15%, riserva liquidità minima 30%, soglie di confidenza più alte.",
  bilanciato:
    "Compromesso tra crescita e prudenza: posizioni fino al 20%, riserva liquidità minima 20%.",
  dinamico:
    "Più propenso al rischio: posizioni fino al 30%, riserva liquidità minima 10%, soglie di confidenza più basse.",
};

/** The six pipeline agents, in the fixed execution order used by the orchestrator. */
export const AGENT_ORDER = [
  "technical",
  "fundamentals",
  "macro_news",
  "corporate_news",
  "synthesizer",
  "validator",
] as const;

export type AgentName = (typeof AGENT_ORDER)[number];

export const AGENT_LABELS_IT: Record<AgentName, string> = {
  technical: "Analista Tecnico",
  fundamentals: "Analista Fondamentale",
  macro_news: "Analista Macro",
  corporate_news: "Analista News Societarie",
  synthesizer: "Sintetizzatore",
  validator: "Validatore Rischio",
};

export const AGENT_SHORT_DESCRIPTIONS_IT: Record<AgentName, string> = {
  technical: "Prezzo, trend e momentum",
  fundamentals: "Valutazione e solidità finanziaria",
  macro_news: "Contesto macroeconomico",
  corporate_news: "Notizie e catalizzatori societari",
  synthesizer: "Sintesi della raccomandazione",
  validator: "Controllo del rischio",
};

export function agentLabelIt(agentName: string): string {
  return AGENT_LABELS_IT[agentName as AgentName] ?? agentName;
}

/** Italian display names + descriptions for the deterministic PolicyEngine rules (BLUEPRINT.md section 6). */
export const POLICY_RULE_LABELS_IT: Record<string, string> = {
  validator_veto: "Veto del validatore",
  validator_revise: "Revisione del validatore",
  min_confidence: "Confidenza minima",
  trend_filter: "Filtro di trend",
  falling_knife: "Rischio caduta libera",
  cooldown: "Raffreddamento post-inversione",
  volatility_sizing: "Dimensionamento su volatilità",
  all_in_gate: "Limite ingresso totale",
  position_cap: "Tetto massimo posizione",
  cash_reserve: "Riserva di liquidità",
  stop_loss_required: "Stop loss obbligatorio",
  confidence_cap: "Tetto di confidenza",
  hold_normalization: "Normalizzazione MANTIENI",
};

export function policyRuleLabelIt(rule: string): string {
  return POLICY_RULE_LABELS_IT[rule] ?? rule;
}
