import type { AnalysisOut, RunStatus, Stance } from "../../api/types";
import { formatDateTimeIt } from "../../lib/format";
import {
  AGENT_LABELS_IT,
  AGENT_ORDER,
  AGENT_SHORT_DESCRIPTIONS_IT,
  STANCE_LABELS_IT,
  type AgentName,
} from "../../lib/labels";
import { gloss } from "../../lib/glossary";
import Badge, { type BadgeVariant } from "../common/Badge";
import ConfidenceBar from "../common/ConfidenceBar";
import InfoTip from "../common/InfoTip";
import Spinner from "../common/Spinner";

/** Maps each pipeline agent to its glossary key (job explanation). */
const AGENT_GLOSS_KEY: Record<AgentName, string> = {
  technical: "agent_technical",
  fundamentals: "agent_fundamentals",
  macro_news: "agent_macro",
  corporate_news: "agent_corporate",
  sentiment: "agent_sentiment",
  synthesizer: "synthesizer",
  validator: "validator",
};

interface AgentBreakdownProps {
  analyses: AnalysisOut[];
  runStatus?: RunStatus;
  className?: string;
}

function stanceVariant(stance: Stance): BadgeVariant {
  if (stance === "BULLISH") return "gain";
  if (stance === "BEARISH") return "loss";
  return "accent";
}

function SignalGauge({ value }: { value: number }) {
  const clamped = Math.min(1, Math.max(-1, value));
  const pct = Math.abs(clamped) * 50;
  const isPositive = clamped >= 0;
  const color = clamped > 0.15 ? "#34d399" : clamped < -0.15 ? "#fb7185" : "#8b95ab";
  return (
    <div className="flex items-center gap-2">
      <div className="relative h-1.5 w-24 overflow-hidden rounded-full bg-slate-800">
        <div className="absolute inset-y-0 left-1/2 w-px bg-slate-600" aria-hidden="true" />
        <div
          className="absolute inset-y-0 rounded-full"
          style={{
            width: `${pct}%`,
            left: isPositive ? "50%" : `${50 - pct}%`,
            backgroundColor: color,
          }}
        />
      </div>
      <span className="w-12 shrink-0 text-right text-xs font-medium tabular-nums text-slate-300">
        {clamped > 0 ? "+" : ""}
        {clamped.toFixed(2)}
      </span>
    </div>
  );
}

export default function AgentBreakdown({ analyses, runStatus, className = "" }: AgentBreakdownProps) {
  const byAgent = new Map(analyses.map((a) => [a.agent_name, a]));
  const isRunActive = runStatus === "PENDING" || runStatus === "RUNNING";

  return (
    <div className={`grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3 ${className}`}>
      {AGENT_ORDER.map((agentName) => {
        const analysis = byAgent.get(agentName);
        const stance = analysis?.stance ?? null;
        const signal = analysis?.signal ?? null;
        const confidence = analysis?.confidence ?? null;
        const summary = analysis?.summary_it ?? "";
        const failed = analysis?.status === "FAILED";

        return (
          <div key={agentName} className="rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] p-4 shadow-card">
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="flex items-center gap-1 text-sm font-semibold text-slate-100">
                  {AGENT_LABELS_IT[agentName]}
                  <InfoTip
                    text={gloss(AGENT_GLOSS_KEY[agentName])}
                    ariaLabel={`Cosa fa: ${AGENT_LABELS_IT[agentName]}`}
                  />
                </p>
                <p className="text-xs text-slate-500">{AGENT_SHORT_DESCRIPTIONS_IT[agentName]}</p>
              </div>
              {stance ? (
                <span className="flex items-center gap-1">
                  <Badge variant={stanceVariant(stance)}>{STANCE_LABELS_IT[stance]}</Badge>
                  <InfoTip text={gloss("stance")} ariaLabel="Cos'è l'orientamento" />
                </span>
              ) : null}
            </div>

            {analysis ? (
              <div className="mt-3 space-y-2.5">
                {failed ? (
                  <p className="text-xs font-medium text-loss-light">Analisi non riuscita per questo agente.</p>
                ) : (
                  <>
                    {signal !== null ? (
                      <div className="flex items-center justify-between gap-2">
                        <span className="flex items-center gap-1 text-[0.65rem] uppercase tracking-wider text-slate-500">
                          Segnale
                          <InfoTip text={gloss("signal")} ariaLabel="Cos'è il segnale" />
                        </span>
                        <SignalGauge value={signal} />
                      </div>
                    ) : null}
                    {confidence !== null ? (
                      <div className="flex items-center justify-between gap-2">
                        <span className="flex items-center gap-1 text-[0.65rem] uppercase tracking-wider text-slate-500">
                          Confidenza
                          <InfoTip text={gloss("confidence")} ariaLabel="Cos'è la confidenza" />
                        </span>
                        <ConfidenceBar value={confidence} />
                      </div>
                    ) : null}
                    {summary ? <p className="text-sm leading-relaxed text-slate-300">{summary}</p> : null}
                  </>
                )}
                <p className="text-[0.65rem] text-slate-500">{formatDateTimeIt(analysis.created_at)}</p>
              </div>
            ) : (
              <div className="mt-3 flex items-center gap-2 text-xs text-slate-500">
                {isRunActive ? (
                  <>
                    <Spinner size="sm" />
                    <span>In attesa dell&apos;analisi…</span>
                  </>
                ) : (
                  <span>Nessun dato disponibile.</span>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
