import type { AnalysisRunOut, RunStatus } from "../../api/types";
import { formatDateTimeIt } from "../../lib/format";
import { AGENT_LABELS_IT, AGENT_ORDER, RUN_STATUS_LABELS_IT, type AgentName } from "../../lib/labels";
import { gloss } from "../../lib/glossary";
import Badge, { type BadgeVariant } from "../common/Badge";
import Card from "../common/Card";
import InfoTip from "../common/InfoTip";
import Spinner from "../common/Spinner";

/** Maps each pipeline actor to its glossary key (job explanation). */
const AGENT_GLOSS_KEY: Record<AgentName, string> = {
  technical: "agent_technical",
  fundamentals: "agent_fundamentals",
  macro_news: "agent_macro",
  corporate_news: "agent_corporate",
  synthesizer: "synthesizer",
  validator: "validator",
};

/** The four analyst actors run in parallel; the tail (synth, validator) is sequential. */
const ANALYST_AGENTS: readonly AgentName[] = [
  "technical",
  "fundamentals",
  "macro_news",
  "corporate_news",
];

const STATUS_BADGE_VARIANT: Record<RunStatus, BadgeVariant> = {
  PENDING: "info",
  RUNNING: "info",
  COMPLETED: "gain",
  FAILED: "loss",
};

const HEADER_TEXT: Record<RunStatus, string> = {
  PENDING: "Analisi in corso…",
  RUNNING: "Analisi in corso…",
  COMPLETED: "Analisi completata",
  FAILED: "Analisi fallita",
};

type StepState = "done" | "failed" | "running" | "pending";

function StepIcon({ state, title }: { state: StepState; title?: string }) {
  if (state === "running") {
    return <Spinner size="sm" />;
  }
  if (state === "done") {
    return (
      <span className="text-sm font-bold text-gain" title={title} aria-label="Completato">
        ✓
      </span>
    );
  }
  if (state === "failed") {
    return (
      <span className="text-sm font-bold text-loss-light" title={title} aria-label="Non riuscito">
        ✗
      </span>
    );
  }
  return (
    <span className="text-sm text-slate-600" aria-label="In attesa">
      —
    </span>
  );
}

interface RunProgressProps {
  run: AnalysisRunOut;
}

/**
 * Prominent, navigation-resilient view of a single analysis run's progress:
 * a status header, a completed-actors progress bar and an ordered checklist of
 * the six pipeline actors plus the final recommendation, each with a check,
 * cross or live spinner. Fed by the 3s run polling on the symbol detail page.
 */
export default function RunProgress({ run }: RunProgressProps) {
  const byAgent = new Map(run.analyses.map((a) => [a.agent_name, a]));
  const isActive = run.status === "PENDING" || run.status === "RUNNING";
  const total = AGENT_ORDER.length;
  const completed = AGENT_ORDER.filter((name) => byAgent.has(name)).length;
  const pct = Math.round((completed / total) * 100);
  const allAnalystsPresent = ANALYST_AGENTS.every((name) => byAgent.has(name));

  const triggerText = run.trigger === "MANUAL" ? "manuale" : "pianificata";

  function stepState(name: AgentName): StepState {
    const analysis = byAgent.get(name);
    if (analysis) return analysis.status === "FAILED" ? "failed" : "done";
    if (!isActive) return "pending";
    // Missing while running: the four analysts spin in parallel; the tail waits
    // for its predecessor so only the actor actually working shows a spinner.
    if ((ANALYST_AGENTS as readonly string[]).includes(name)) return "running";
    if (name === "synthesizer") return allAnalystsPresent ? "running" : "pending";
    return byAgent.has("synthesizer") ? "running" : "pending"; // validator
  }

  const recoState: StepState = run.recommendation
    ? "done"
    : isActive && byAgent.has("validator") && byAgent.get("validator")?.status !== "FAILED"
      ? "running"
      : "pending";

  const barColor =
    run.status === "FAILED" ? "bg-loss" : run.status === "COMPLETED" ? "bg-gain" : "bg-brand-500";

  return (
    <Card className="border-brand-700/40">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-1.5 text-base font-semibold text-slate-50">
            {isActive ? <Spinner size="sm" /> : null}
            {HEADER_TEXT[run.status]}
            <InfoTip
              text={gloss("policy_engine")}
              ariaLabel="Come funziona l'analisi a più agenti"
            />
          </h3>
          <p className="mt-0.5 text-xs text-slate-400">
            Analisi {triggerText} · avviata il {formatDateTimeIt(run.started_at)}
          </p>
        </div>
        <Badge variant={STATUS_BADGE_VARIANT[run.status]}>{RUN_STATUS_LABELS_IT[run.status]}</Badge>
      </div>

      {run.status === "FAILED" && run.error ? (
        <p className="mt-3 rounded-md border border-loss/30 bg-loss-bg px-3 py-2 text-xs text-loss-light">
          {run.error}
        </p>
      ) : null}

      <div className="mt-4">
        <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
          <div
            className={`h-full rounded-full transition-all duration-500 ${barColor}`}
            style={{ width: `${pct}%` }}
          />
        </div>
        <p className="mt-1.5 text-xs text-slate-400">
          {completed} di {total} agenti completati
        </p>
      </div>

      <ul className="mt-4 space-y-2">
        {AGENT_ORDER.map((name) => {
          const state = stepState(name);
          const analysis = byAgent.get(name);
          const failTitle =
            state === "failed"
              ? analysis?.summary_it || "Analisi non riuscita per questo agente."
              : undefined;
          return (
            <li key={name} className="flex items-center gap-2 text-sm">
              <span className="flex w-5 shrink-0 justify-center">
                <StepIcon state={state} title={failTitle} />
              </span>
              <span className={state === "pending" ? "text-slate-500" : "text-slate-200"}>
                {AGENT_LABELS_IT[name]}
              </span>
              <InfoTip
                text={gloss(AGENT_GLOSS_KEY[name])}
                ariaLabel={`Cosa fa: ${AGENT_LABELS_IT[name]}`}
              />
            </li>
          );
        })}
        <li className="flex items-center gap-2 border-t border-[var(--tm-border)] pt-2 text-sm">
          <span className="flex w-5 shrink-0 justify-center">
            <StepIcon state={recoState} />
          </span>
          <span className={recoState === "pending" ? "text-slate-500" : "text-slate-200"}>
            Raccomandazione
          </span>
        </li>
      </ul>
    </Card>
  );
}
