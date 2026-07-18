import type { PolicyCheck } from "../../api/types";
import { policyRuleLabelIt } from "../../lib/labels";
import { gloss, policyGlossKey } from "../../lib/glossary";
import InfoTip from "../common/InfoTip";

interface PolicyChecksListProps {
  checks: PolicyCheck[];
  className?: string;
}

export default function PolicyChecksList({ checks, className = "" }: PolicyChecksListProps) {
  if (checks.length === 0) {
    return <p className={`text-xs text-slate-500 ${className}`}>Nessun controllo di policy registrato.</p>;
  }

  return (
    <ul className={`space-y-2 ${className}`}>
      {checks.map((check, idx) => {
        const info = gloss(policyGlossKey(check.rule));
        return (
          <li key={`${check.rule}-${idx}`} className="flex items-start gap-2.5 text-sm">
            <span
              aria-hidden="true"
              className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[0.65rem] font-bold ${
                check.passed ? "bg-gain-bg text-gain-light" : "bg-loss-bg text-loss-light"
              }`}
            >
              {check.passed ? "✓" : "✗"}
            </span>
            <div className="min-w-0">
              <p className="flex items-center gap-1 font-medium text-slate-200">
                {policyRuleLabelIt(check.rule)}
                {info ? <InfoTip text={info} ariaLabel={`Cos'è: ${policyRuleLabelIt(check.rule)}`} /> : null}
              </p>
              <p className="text-xs text-slate-500">{check.detail_it}</p>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
