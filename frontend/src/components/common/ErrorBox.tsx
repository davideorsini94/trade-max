interface ErrorBoxProps {
  message: string;
  onRetry?: () => void;
  className?: string;
}

export default function ErrorBox({ message, onRetry, className = "" }: ErrorBoxProps) {
  return (
    <div
      className={`flex items-start gap-3 rounded-lg border border-loss/30 bg-loss-bg px-4 py-3 text-sm text-loss-light ${className}`}
      role="alert"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" className="mt-0.5 shrink-0" aria-hidden="true">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.8" />
        <path d="M12 8v5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        <circle cx="12" cy="16.2" r="1" fill="currentColor" />
      </svg>
      <div className="flex-1">
        <p>{message}</p>
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="mt-2 rounded-md border border-loss/40 px-3 py-1 text-xs font-medium text-loss-light transition hover:bg-loss/10"
          >
            Riprova
          </button>
        ) : null}
      </div>
    </div>
  );
}
