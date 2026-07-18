interface SpinnerProps {
  size?: "sm" | "md" | "lg";
  label?: string;
  className?: string;
}

const SIZE_PX: Record<NonNullable<SpinnerProps["size"]>, number> = {
  sm: 14,
  md: 20,
  lg: 32,
};

export default function Spinner({ size = "md", label, className = "" }: SpinnerProps) {
  const px = SIZE_PX[size];
  return (
    <span className={`inline-flex items-center gap-2 text-slate-400 ${className}`} role="status" aria-live="polite">
      <svg
        className="tm-spin"
        width={px}
        height={px}
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
        <path
          d="M21 12a9 9 0 0 0-9-9"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinecap="round"
        />
      </svg>
      {label ? <span className="text-sm">{label}</span> : null}
    </span>
  );
}
