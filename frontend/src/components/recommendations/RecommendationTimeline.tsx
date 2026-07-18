import type { RecommendationOut } from "../../api/types";
import { formatDateTimeIt, formatNumber, formatPercent } from "../../lib/format";
import { ACTION_LABELS_IT, SIZING_LABELS_IT } from "../../lib/labels";
import { gloss } from "../../lib/glossary";
import Badge, { type BadgeVariant } from "../common/Badge";
import ConfidenceBar from "../common/ConfidenceBar";
import InfoTip from "../common/InfoTip";

interface RecommendationTimelineProps {
  recommendations: RecommendationOut[];
  className?: string;
}

function actionVariant(action: RecommendationOut["action"]): BadgeVariant {
  if (action === "BUY") return "gain";
  if (action === "SELL") return "loss";
  return "accent";
}

const DOT_COLOR: Record<RecommendationOut["action"], string> = {
  BUY: "#34d399",
  SELL: "#fb7185",
  HOLD: "#94a3b8",
};

export default function RecommendationTimeline({ recommendations, className = "" }: RecommendationTimelineProps) {
  if (recommendations.length === 0) {
    return (
      <div
        className={`rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-8 text-center text-sm text-slate-400 ${className}`}
      >
        Nessuna raccomandazione storica per questo titolo.
      </div>
    );
  }

  return (
    <ol className={`relative space-y-4 border-l border-[var(--tm-border)] pl-5 ${className}`}>
      {recommendations.map((reco) => (
        <li key={reco.id} className="relative">
          <span
            className="absolute -left-[1.65rem] top-1 h-2.5 w-2.5 rounded-full border-2 border-[var(--tm-bg)]"
            style={{ backgroundColor: DOT_COLOR[reco.action] }}
            aria-hidden="true"
          />
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={actionVariant(reco.action)}>{ACTION_LABELS_IT[reco.action]}</Badge>
            <span className="text-xs text-slate-500">{SIZING_LABELS_IT[reco.sizing_strategy]}</span>
            <span className="text-xs text-slate-600">·</span>
            <span className="text-xs text-slate-500">{formatDateTimeIt(reco.created_at)}</span>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-3">
            <ConfidenceBar value={reco.confidence} className="max-w-[8rem]" />
            {reco.evaluated ? (
              <div className="flex items-center gap-1.5">
                {reco.realized_return_7d !== null ? (
                  <Badge variant={reco.realized_return_7d >= 0 ? "gain" : "loss"}>
                    Reso 7g: {formatPercent(reco.realized_return_7d)}
                  </Badge>
                ) : null}
                {reco.outcome_score !== null ? (
                  <Badge variant={reco.outcome_score > 0.2 ? "gain" : "loss"}>
                    Punteggio: {formatNumber(reco.outcome_score, 2)}
                  </Badge>
                ) : null}
                {reco.realized_return_7d !== null || reco.outcome_score !== null ? (
                  <InfoTip text={gloss("realized_return_7d")} ariaLabel="Cos'è l'esito realizzato" />
                ) : null}
              </div>
            ) : (
              <span className="text-xs text-slate-500">Non ancora valutata</span>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
