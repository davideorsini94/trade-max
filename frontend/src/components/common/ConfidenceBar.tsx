import { formatConfidence } from "../../lib/format";

interface ConfidenceBarProps {
  value: number; // 0..1
  showLabel?: boolean;
  className?: string;
}

function colorFor(value: number): string {
  if (value >= 0.65) return "#34d399"; // gain
  if (value >= 0.45) return "#f5b942"; // accent
  return "#fb7185"; // loss
}

export default function ConfidenceBar({ value, showLabel = true, className = "" }: ConfidenceBarProps) {
  const clamped = Math.min(1, Math.max(0, value));
  const pct = Math.round(clamped * 100);
  const color = colorFor(clamped);

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <div
        className="h-1.5 w-24 flex-1 overflow-hidden rounded-full bg-slate-800"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Confidenza"
      >
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${pct}%`, backgroundColor: color }}
        />
      </div>
      {showLabel ? (
        <span className="w-10 shrink-0 text-right text-xs font-medium tabular-nums text-slate-300">
          {formatConfidence(clamped)}
        </span>
      ) : null}
    </div>
  );
}
