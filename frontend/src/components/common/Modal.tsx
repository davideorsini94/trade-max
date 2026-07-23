import { useEffect } from "react";
import type { ReactNode } from "react";

interface ModalProps {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  /** Optional footer area (e.g. actions or a disclaimer), rendered below the body. */
  footer?: ReactNode;
}

/**
 * Generic centered modal dialog. Closes on Escape and on a click on the dark
 * overlay (but not on clicks inside the panel). Locks background scroll while
 * open and renders nothing when `open` is false. The panel matches the app's
 * Card surface (see components/common/Card.tsx).
 */
export default function Modal({ open, title, onClose, children, footer }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="w-full max-w-md rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] shadow-card"
      >
        <div className="flex items-start justify-between gap-3 border-b border-[var(--tm-border)] px-5 py-4">
          <h3 className="text-sm font-semibold tracking-wide text-slate-100">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="Chiudi"
            className="-mr-1 -mt-1 shrink-0 rounded-md px-2 py-1 text-lg leading-none text-slate-400 transition hover:bg-slate-800 hover:text-slate-200 focus:outline-none focus-visible:ring-1 focus-visible:ring-brand-500"
          >
            ✕
          </button>
        </div>
        <div className="p-5">{children}</div>
        {footer ? <div className="border-t border-[var(--tm-border)] px-5 py-4">{footer}</div> : null}
      </div>
    </div>
  );
}
