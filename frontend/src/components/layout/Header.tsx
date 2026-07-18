import { NavLink } from "react-router-dom";
import { apiGet } from "../../api/client";
import type { DashboardSummary } from "../../api/types";
import { usePolling } from "../../hooks/usePolling";
import Badge from "../common/Badge";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/mercato", label: "Mercato", end: false },
  { to: "/performance", label: "Performance", end: false },
  { to: "/settings", label: "Impostazioni", end: false },
  { to: "/glossario", label: "Glossario", end: false },
];

function navLinkClass(isActive: boolean): string {
  return [
    "rounded-md px-3 py-2 text-sm font-medium transition-colors",
    isActive ? "bg-brand-900/70 text-brand-100" : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-100",
  ].join(" ");
}

export default function Header() {
  // Self-contained: the market-open badge is relevant on every route, not just
  // the dashboard, so Header owns its own lightweight poll of the same summary
  // endpoint (30s cadence, per BLUEPRINT.md polling conventions).
  const { data } = usePolling<DashboardSummary>(() => apiGet<DashboardSummary>("/dashboard/summary"), 30000);

  return (
    <header className="sticky top-0 z-30 border-b border-[var(--tm-border)] bg-[var(--tm-bg)]/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3 sm:px-6 lg:px-8">
        <div className="flex items-center gap-6">
          <NavLink to="/" className="flex items-center gap-2 text-lg font-bold tracking-tight text-slate-50">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-brand-500 to-emerald-500 text-sm font-black text-slate-950">
              TM
            </span>
            TradeMax
          </NavLink>
          <nav className="flex items-center gap-1">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) => navLinkClass(isActive)}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
        <div className="flex items-center gap-3">
          {data ? (
            <Badge variant={data.market_open ? "gain" : "neutral"}>
              <span
                className={`h-1.5 w-1.5 rounded-full ${data.market_open ? "bg-gain tm-pulse" : "bg-slate-500"}`}
                aria-hidden="true"
              />
              {data.market_open ? "Mercato aperto" : "Mercato chiuso"}
            </Badge>
          ) : null}
        </div>
      </div>
    </header>
  );
}
