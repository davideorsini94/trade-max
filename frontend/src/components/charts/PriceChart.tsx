import { useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { IndicatorSeries, PricePoint, RecommendationOut } from "../../api/types";
import { formatCurrency, formatDateIt, parseBackendDate } from "../../lib/format";
import { ACTION_LABELS_IT } from "../../lib/labels";
import { gloss } from "../../lib/glossary";
import InfoTip from "../common/InfoTip";

interface PriceChartProps {
  points: PricePoint[];
  indicators: IndicatorSeries | null;
  recommendations?: RecommendationOut[];
  currency?: string;
}

interface ChartRow {
  ts: number;
  close: number;
  sma50: number | null;
  sma200: number | null;
  bbUpper: number | null;
  bbLower: number | null;
  bbRange: [number, number] | undefined;
  rsi14: number | null;
}

const ACTION_DOT_COLOR: Record<string, string> = {
  BUY: "#34d399",
  SELL: "#fb7185",
  HOLD: "#94a3b8",
};

function ToggleCheckbox({
  label,
  checked,
  onChange,
  swatchColor,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  swatchColor?: string;
}) {
  return (
    <label className="flex cursor-pointer select-none items-center gap-1.5 text-xs text-slate-300">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-3.5 w-3.5 rounded border-slate-600 bg-slate-800 accent-brand-500"
      />
      {swatchColor ? (
        <span className="h-2 w-2 rounded-full" style={{ backgroundColor: swatchColor }} aria-hidden="true" />
      ) : null}
      {label}
    </label>
  );
}

interface TooltipPayloadItem {
  dataKey?: string;
  value?: number | null;
  color?: string;
}

/**
 * Recharts' TooltipContentProps generics make a precisely-typed content
 * component brittle to version drift; accept the props loosely (as recharts
 * itself does internally) and narrow defensively below.
 */
function ChartTooltip(props: { active?: boolean; payload?: unknown; label?: unknown; currency?: string }) {
  const { active, label } = props;
  const currency = props.currency ?? "EUR";
  const payload = (props.payload ?? []) as TooltipPayloadItem[];
  if (!active || payload.length === 0 || typeof label !== "number") return null;
  const byKey = new Map(payload.map((p) => [p.dataKey, p.value]));
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900/95 px-3 py-2 text-xs shadow-xl">
      <p className="mb-1 font-semibold text-slate-200">{formatDateIt(new Date(label).toISOString())}</p>
      {byKey.has("close") ? (
        <p className="text-slate-300">Chiusura: {formatCurrency(Number(byKey.get("close")), currency)}</p>
      ) : null}
      {byKey.get("sma50") != null ? (
        <p style={{ color: "#f5b942" }}>SMA50: {formatCurrency(Number(byKey.get("sma50")), currency)}</p>
      ) : null}
      {byKey.get("sma200") != null ? (
        <p style={{ color: "#599bff" }}>SMA200: {formatCurrency(Number(byKey.get("sma200")), currency)}</p>
      ) : null}
      {byKey.get("rsi14") != null ? <p className="text-slate-300">RSI14: {Number(byKey.get("rsi14")).toFixed(1)}</p> : null}
    </div>
  );
}

export default function PriceChart({ points, indicators, recommendations = [], currency = "USD" }: PriceChartProps) {
  const [showSma50, setShowSma50] = useState(true);
  const [showSma200, setShowSma200] = useState(true);
  const [showBollinger, setShowBollinger] = useState(false);
  const [showRsi, setShowRsi] = useState(true);

  const data = useMemo<ChartRow[]>(() => {
    return points.map((p, idx) => {
      const bbUpper = indicators?.bb_upper?.[idx] ?? null;
      const bbLower = indicators?.bb_lower?.[idx] ?? null;
      return {
        ts: parseBackendDate(p.ts).getTime(),
        close: p.close,
        sma50: indicators?.sma50?.[idx] ?? null,
        sma200: indicators?.sma200?.[idx] ?? null,
        bbUpper,
        bbLower,
        bbRange: bbUpper !== null && bbLower !== null ? ([bbLower, bbUpper] as [number, number]) : undefined,
        rsi14: indicators?.rsi14?.[idx] ?? null,
      };
    });
  }, [points, indicators]);

  // Snap each recommendation to the nearest plotted bar: an intraday
  // timestamp never matches a daily bar exactly, and the newest
  // recommendation is usually more recent than the last close — exact
  // matching/range filtering would drop it from the chart.
  const markers = useMemo(() => {
    if (data.length === 0) return [];
    return recommendations.map((rec) => {
      const ts = parseBackendDate(rec.created_at).getTime();
      let nearest = data[0];
      for (const row of data) {
        if (Math.abs(row.ts - ts) < Math.abs(nearest.ts - ts)) nearest = row;
      }
      return { rec, ts: nearest.ts, fallbackY: nearest.close };
    });
  }, [recommendations, data]);

  const dateTickFormatter = (value: number) => formatDateIt(new Date(value).toISOString());
  const priceTickFormatter = (value: number) => formatCurrency(value, currency);

  if (points.length === 0) {
    return (
      <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-slate-700 text-sm text-slate-500">
        Nessun dato storico disponibile per questo titolo.
      </div>
    );
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex items-center gap-1">
          <ToggleCheckbox label="Media mobile 50" checked={showSma50} onChange={setShowSma50} swatchColor="#f5b942" />
          <InfoTip text={gloss("sma50")} ariaLabel="Cos'è la media mobile a 50 giorni" />
        </div>
        <div className="flex items-center gap-1">
          <ToggleCheckbox label="Media mobile 200" checked={showSma200} onChange={setShowSma200} swatchColor="#599bff" />
          <InfoTip text={gloss("sma200")} ariaLabel="Cos'è la media mobile a 200 giorni" />
        </div>
        <div className="flex items-center gap-1">
          <ToggleCheckbox label="Bande di Bollinger" checked={showBollinger} onChange={setShowBollinger} swatchColor="#a78bfa" />
          <InfoTip text={gloss("bollinger")} ariaLabel="Cosa sono le bande di Bollinger" />
        </div>
        <div className="flex items-center gap-1">
          <ToggleCheckbox label="RSI" checked={showRsi} onChange={setShowRsi} swatchColor="#34d399" />
          <InfoTip text={gloss("rsi")} ariaLabel="Cos'è l'RSI" />
        </div>
      </div>

      <ResponsiveContainer width="100%" height={360}>
        <ComposedChart data={data} syncId="tm-price-rsi" margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="tm-close-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#3478f6" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#3478f6" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#232c40" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="ts"
            type="number"
            domain={["dataMin", "dataMax"]}
            tickFormatter={dateTickFormatter}
            stroke="#8b95ab"
            tick={{ fontSize: 11 }}
            minTickGap={40}
          />
          <YAxis
            domain={["auto", "auto"]}
            tickFormatter={priceTickFormatter}
            stroke="#8b95ab"
            tick={{ fontSize: 11 }}
            width={70}
          />
          <Tooltip content={<ChartTooltip currency={currency} />} />
          {showBollinger ? (
            <Area
              dataKey="bbRange"
              stroke="none"
              fill="#a78bfa"
              fillOpacity={0.12}
              isAnimationActive={false}
              name="Bande di Bollinger"
              legendType="none"
            />
          ) : null}
          <Area
            type="monotone"
            dataKey="close"
            stroke="#3478f6"
            fill="url(#tm-close-fill)"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
            name="Chiusura"
          />
          {showSma50 ? (
            <Line
              type="monotone"
              dataKey="sma50"
              stroke="#f5b942"
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
              name="Media mobile 50"
              connectNulls
            />
          ) : null}
          {showSma200 ? (
            <Line
              type="monotone"
              dataKey="sma200"
              stroke="#599bff"
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
              name="Media mobile 200"
              connectNulls
            />
          ) : null}
          {markers.map((m) => {
            const dotColor = ACTION_DOT_COLOR[m.rec.action];
            const tooltipText = `${ACTION_LABELS_IT[m.rec.action]} · ${formatDateIt(m.rec.created_at)}`;
            return (
              <ReferenceDot
                key={m.rec.id}
                x={m.ts}
                y={m.rec.entry_price ?? m.fallbackY}
                r={5}
                isFront
                shape={(shapeProps: { cx?: number; cy?: number }) => (
                  <g>
                    <circle
                      cx={shapeProps.cx}
                      cy={shapeProps.cy}
                      r={5}
                      fill={dotColor}
                      stroke="#0b0f1a"
                      strokeWidth={1.5}
                    />
                    <title>{tooltipText}</title>
                  </g>
                )}
              />
            );
          })}
          <Legend wrapperStyle={{ fontSize: 12, color: "#8b95ab" }} />
        </ComposedChart>
      </ResponsiveContainer>

      {showRsi ? (
        <ResponsiveContainer width="100%" height={130}>
          <ComposedChart data={data} syncId="tm-price-rsi" margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="#232c40" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="ts"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={dateTickFormatter}
              stroke="#8b95ab"
              tick={{ fontSize: 11 }}
              minTickGap={40}
            />
            <YAxis domain={[0, 100]} ticks={[30, 50, 70]} stroke="#8b95ab" tick={{ fontSize: 11 }} width={70} />
            <Tooltip content={<ChartTooltip currency={currency} />} />
            <ReferenceLine y={70} stroke="#fb7185" strokeDasharray="4 4" />
            <ReferenceLine y={30} stroke="#34d399" strokeDasharray="4 4" />
            <Line
              type="monotone"
              dataKey="rsi14"
              stroke="#34d399"
              strokeWidth={1.5}
              dot={false}
              isAnimationActive={false}
              name="RSI14"
              connectNulls
            />
          </ComposedChart>
        </ResponsiveContainer>
      ) : null}

      {!indicators ? (
        <p className="mt-2 text-xs text-slate-500">Indicatori tecnici non disponibili per questo intervallo.</p>
      ) : null}
    </div>
  );
}
