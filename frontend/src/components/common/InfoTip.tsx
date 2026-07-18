import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

interface InfoTipProps {
  /** Plain-Italian explanation to show. Prefer gloss(key) so copy stays centralized. */
  text: string;
  /** Accessible label for the trigger button. Defaults to a generic Italian label. */
  ariaLabel?: string;
  className?: string;
}

interface Coords {
  top: number;
  left: number;
}

/**
 * Small accessible "i" info button that reveals an explanation on hover, focus
 * and click/tap (touch-friendly). The popover is rendered into <body> via a
 * portal and positioned with fixed coordinates so it can never be clipped by an
 * overflow/scroll container, flipping above the trigger when there is no room
 * below and clamping to the viewport on the horizontal axis. Closes on Escape
 * and on any click/tap outside the trigger and popover.
 */
export default function InfoTip({ text, ariaLabel, className = "" }: InfoTipProps) {
  const [hoverOpen, setHoverOpen] = useState(false);
  const [pinOpen, setPinOpen] = useState(false);
  const [coords, setCoords] = useState<Coords | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const tooltipId = useId();
  const open = hoverOpen || pinOpen;

  const reposition = useCallback(() => {
    const btn = buttonRef.current;
    const pop = popoverRef.current;
    if (!btn || !pop) return;
    const margin = 8;
    const gap = 6;
    const btnRect = btn.getBoundingClientRect();
    const popRect = pop.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const spaceBelow = vh - btnRect.bottom;
    const placeAbove = spaceBelow < popRect.height + gap + margin && btnRect.top > spaceBelow;
    let left = btnRect.left + btnRect.width / 2 - popRect.width / 2;
    left = Math.max(margin, Math.min(left, vw - popRect.width - margin));
    const top = placeAbove ? btnRect.top - popRect.height - gap : btnRect.bottom + gap;
    setCoords({ top: Math.max(margin, top), left });
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      setCoords(null);
      return;
    }
    reposition();
  }, [open, text, reposition]);

  useEffect(() => {
    if (!open) return;
    const onScroll = () => reposition();
    const onResize = () => reposition();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setPinOpen(false);
        setHoverOpen(false);
        buttonRef.current?.blur();
      }
    };
    const onOutside = (event: MouseEvent | TouchEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (buttonRef.current?.contains(target) || popoverRef.current?.contains(target)) return;
      setPinOpen(false);
      setHoverOpen(false);
    };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onOutside);
    document.addEventListener("touchstart", onOutside);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onOutside);
      document.removeEventListener("touchstart", onOutside);
    };
  }, [open, reposition]);

  return (
    <span className={`inline-flex align-middle ${className}`}>
      <button
        ref={buttonRef}
        type="button"
        aria-label={ariaLabel ?? "Maggiori informazioni"}
        aria-describedby={open ? tooltipId : undefined}
        onMouseEnter={() => setHoverOpen(true)}
        onMouseLeave={() => setHoverOpen(false)}
        onFocus={() => setPinOpen(true)}
        onBlur={() => setPinOpen(false)}
        onClick={() => setPinOpen(true)}
        className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full text-slate-500 transition-colors hover:text-slate-300 focus:text-slate-300 focus:outline-none focus-visible:ring-1 focus-visible:ring-brand-500"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.8" />
          <path d="M12 11v5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          <circle cx="12" cy="7.7" r="1.15" fill="currentColor" />
        </svg>
      </button>
      {open
        ? createPortal(
            <div
              ref={popoverRef}
              id={tooltipId}
              role="tooltip"
              style={{
                position: "fixed",
                top: coords ? coords.top : 0,
                left: coords ? coords.left : 0,
                opacity: coords ? 1 : 0,
                pointerEvents: coords ? "auto" : "none",
              }}
              className="z-50 max-w-xs rounded-lg border border-[var(--tm-border)] bg-[var(--tm-surface-2)] px-3 py-2 text-xs font-normal normal-case leading-relaxed tracking-normal text-slate-200 shadow-xl"
            >
              {text}
            </div>,
            document.body,
          )
        : null}
    </span>
  );
}
