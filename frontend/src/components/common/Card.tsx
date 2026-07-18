import type { ReactNode } from "react";

interface CardProps {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  padded?: boolean;
}

export default function Card({ title, subtitle, actions, children, className = "", padded = true }: CardProps) {
  const hasHeader = Boolean(title || subtitle || actions);
  return (
    <div
      className={`rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] shadow-card ${className}`}
    >
      {hasHeader ? (
        <div className="flex items-start justify-between gap-3 border-b border-[var(--tm-border)] px-5 py-4">
          <div>
            {title ? <h3 className="text-sm font-semibold tracking-wide text-slate-100">{title}</h3> : null}
            {subtitle ? <p className="mt-0.5 text-xs text-slate-400">{subtitle}</p> : null}
          </div>
          {actions ? <div className="shrink-0">{actions}</div> : null}
        </div>
      ) : null}
      <div className={padded ? "p-5" : ""}>{children}</div>
    </div>
  );
}
