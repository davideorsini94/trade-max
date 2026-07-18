export default function DisclaimerBanner() {
  return (
    <footer className="fixed inset-x-0 bottom-0 z-40 border-t border-[var(--tm-border)] bg-[var(--tm-surface)]/95 backdrop-blur">
      <div className="mx-auto max-w-7xl px-4 py-2 text-center text-xs leading-relaxed text-slate-400 sm:px-6 lg:px-8">
        <span aria-hidden="true">⚠️</span>{" "}
        TradeMax è uno strumento sperimentale a scopo informativo. Non costituisce consulenza finanziaria. Le
        decisioni di investimento sono a tuo esclusivo rischio.
      </div>
    </footer>
  );
}
