import type { ReactNode } from "react";

export type BadgeVariant = "gain" | "loss" | "accent" | "neutral" | "info" | "outline";

interface BadgeProps {
  children: ReactNode;
  variant?: BadgeVariant;
  className?: string;
  title?: string;
}

const VARIANT_CLASSES: Record<BadgeVariant, string> = {
  gain: "bg-gain-bg text-gain-light border-gain/30",
  loss: "bg-loss-bg text-loss-light border-loss/30",
  accent: "bg-accent-bg text-accent-light border-accent/30",
  neutral: "bg-slate-800/60 text-slate-300 border-slate-700",
  info: "bg-brand-900/60 text-brand-200 border-brand-700/60",
  outline: "bg-transparent text-slate-300 border-slate-600",
};

export default function Badge({ children, variant = "neutral", className = "", title }: BadgeProps) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-semibold ${VARIANT_CLASSES[variant]} ${className}`}
    >
      {children}
    </span>
  );
}
