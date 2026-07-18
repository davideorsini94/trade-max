import type { ReactNode } from "react";
import type { SymbolOverviewOut } from "../../api/types";
import Card from "../common/Card";
import InfoTip from "../common/InfoTip";
import {
  formatCompactNumber,
  formatCurrency,
  formatDateTimeIt,
  formatMarketCap,
  formatNumber,
  formatPercent,
  signColorClass,
} from "../../lib/format";
import { gloss } from "../../lib/glossary";

const DASH = "—";

interface SymbolOverviewProps {
  overview: SymbolOverviewOut;
}

/** A single labelled stat with an info tooltip and a value (em-dash when null). */
function StatTile({
  label,
  glossKey,
  children,
}: {
  label: string;
  glossKey: string;
  children: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-[var(--tm-border)] bg-[var(--tm-surface-2)] px-3 py-2.5">
      <div className="flex items-center gap-1 text-[11px] uppercase tracking-wider text-slate-500">
        <span>{label}</span>
        <InfoTip text={gloss(glossKey)} ariaLabel={`Cos'è: ${label}`} />
      </div>
      <div className="mt-1 text-sm font-medium tabular-nums text-slate-100">{children}</div>
    </div>
  );
}

function priceText(value: number | null): string {
  return value === null ? DASH : formatNumber(value, 2);
}

function rangeText(low: number | null, high: number | null): string {
  if (low === null || high === null) return DASH;
  return `${formatNumber(low, 2)} – ${formatNumber(high, 2)}`;
}

function volumeText(value: number | null): string {
  return value === null ? DASH : formatCompactNumber(value);
}

export default function SymbolOverview({ overview }: SymbolOverviewProps) {
  const { currency } = overview;
  const changeAbs = overview.change_1d_abs;
  const changePct = overview.change_pct_1d;
  const changeColor = signColorClass(changePct ?? changeAbs);

  const targetDelta =
    overview.analyst_target !== null && overview.last_price !== null && overview.last_price !== 0
      ? ((overview.analyst_target - overview.last_price) / overview.last_price) * 100
      : null;

  return (
    <Card>
      {/* Price hero */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <span className="text-3xl font-bold tabular-nums text-slate-50">
            {overview.last_price !== null ? formatCurrency(overview.last_price, currency) : DASH}
          </span>
          {changeAbs !== null ? (
            <span
              className={`inline-flex items-center rounded-md bg-slate-800/60 px-2 py-0.5 text-sm font-semibold tabular-nums ${changeColor}`}
            >
              {`${changeAbs >= 0 ? "+" : "-"}${formatNumber(Math.abs(changeAbs), 2)} ${currency}`}
              {changePct !== null ? ` (${formatPercent(changePct)})` : ""}
            </span>
          ) : null}
        </div>
        {overview.updated_at ? (
          <span className="text-xs text-slate-500">
            Aggiornato al {formatDateTimeIt(overview.updated_at)}
          </span>
        ) : null}
      </div>

      {/* Stat grid */}
      <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        <StatTile label="Apertura" glossKey="open_price">
          {priceText(overview.open)}
        </StatTile>
        <StatTile label="Min / Max giornata" glossKey="day_range">
          {rangeText(overview.day_low, overview.day_high)}
        </StatTile>
        <StatTile label="Chiusura precedente" glossKey="prev_close">
          {priceText(overview.prev_close)}
        </StatTile>
        <StatTile label="Volume" glossKey="volume">
          {volumeText(overview.volume)}
        </StatTile>
        <StatTile label="Volume medio 30g" glossKey="avg_volume">
          {volumeText(overview.avg_volume_30d)}
        </StatTile>
        <StatTile label="Min / Max 52 settimane" glossKey="week52_range">
          {rangeText(overview.week52_low, overview.week52_high)}
        </StatTile>
        <StatTile label="Capitalizzazione" glossKey="market_value">
          {overview.market_cap === null ? DASH : formatMarketCap(overview.market_cap / 1e9, currency)}
        </StatTile>
        <StatTile label="P/E" glossKey="pe_ratio">
          {overview.pe === null ? DASH : formatNumber(overview.pe, 2)}
        </StatTile>
        <StatTile label="P/E atteso" glossKey="forward_pe">
          {overview.forward_pe === null ? DASH : formatNumber(overview.forward_pe, 2)}
        </StatTile>
        <StatTile label="EPS" glossKey="eps">
          {overview.eps === null ? DASH : formatCurrency(overview.eps, currency)}
        </StatTile>
        <StatTile label="Dividendo" glossKey="dividend_yield">
          {overview.dividend_yield === null
            ? DASH
            : formatPercent(overview.dividend_yield, { withSign: false })}
        </StatTile>
        <StatTile label="Beta" glossKey="beta">
          {overview.beta === null ? DASH : formatNumber(overview.beta, 2)}
        </StatTile>
        <StatTile label="Target analisti" glossKey="analyst_target">
          {overview.analyst_target === null ? (
            DASH
          ) : (
            <span className="flex flex-wrap items-baseline gap-1.5">
              <span>{formatCurrency(overview.analyst_target, currency)}</span>
              {targetDelta !== null ? (
                <span className={`text-xs font-medium ${signColorClass(targetDelta)}`}>
                  ({formatPercent(targetDelta, { decimals: 1 })} dal prezzo attuale)
                </span>
              ) : null}
            </span>
          )}
        </StatTile>
        <StatTile label="Settore" glossKey="sector">
          {overview.sector === null ? (
            DASH
          ) : (
            <span className="flex flex-col">
              <span>{overview.sector}</span>
              {overview.industry ? (
                <span className="text-xs font-normal text-slate-500">{overview.industry}</span>
              ) : null}
            </span>
          )}
        </StatTile>
      </div>
    </Card>
  );
}
