import { useMemo, useState } from "react";
import { GLOSSARY, GLOSSARY_SECTIONS } from "../lib/glossary";

export default function GlossaryPage() {
  const [query, setQuery] = useState("");
  const normalized = query.trim().toLowerCase();

  const sections = useMemo(() => {
    return GLOSSARY_SECTIONS.map((section) => {
      const entries = section.keys
        .map((key) => ({ key, ...GLOSSARY[key] }))
        .filter(
          (entry) =>
            normalized === "" ||
            entry.term.toLowerCase().includes(normalized) ||
            entry.short.toLowerCase().includes(normalized),
        );
      return { title: section.title, entries };
    }).filter((section) => section.entries.length > 0);
  }, [normalized]);

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-bold text-slate-50">Glossario</h1>
        <p className="mt-1 max-w-2xl text-sm text-slate-400">
          Le parole della finanza usate in TradeMax, spiegate in modo semplice.
        </p>
      </div>

      <div>
        <label htmlFor="glossary-filter" className="sr-only">
          Cerca nel glossario
        </label>
        <input
          id="glossary-filter"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Cerca un termine…"
          className="w-full max-w-sm rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
        />
      </div>

      {sections.length === 0 ? (
        <p className="text-sm text-slate-400">Nessun termine corrisponde alla ricerca.</p>
      ) : (
        <div className="space-y-8">
          {sections.map((section) => (
            <section key={section.title}>
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">{section.title}</h2>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                {section.entries.map((entry) => (
                  <div
                    key={entry.key}
                    className="rounded-xl border border-[var(--tm-border)] bg-[var(--tm-surface)] p-4 shadow-card"
                  >
                    <p className="text-sm font-semibold text-slate-100">{entry.term}</p>
                    <p className="mt-1 text-sm leading-relaxed text-slate-400">{entry.short}</p>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
