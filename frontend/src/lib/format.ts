import { format, formatDistanceToNow } from "date-fns";
import { it } from "date-fns/locale";

/**
 * Backend datetimes are UTC-naive ISO strings without a timezone suffix
 * (BLUEPRINT.md friction point #1). We treat them as UTC explicitly by
 * appending "Z" when absent, then let the browser render in local time.
 */
export function parseBackendDate(iso: string): Date {
  const normalized = /[zZ]|[+-]\d{2}:\d{2}$/.test(iso) ? iso : `${iso}Z`;
  return new Date(normalized);
}

export function formatDateTimeIt(iso: string): string {
  try {
    return format(parseBackendDate(iso), "d MMM yyyy, HH:mm", { locale: it });
  } catch {
    return iso;
  }
}

export function formatDateIt(iso: string): string {
  try {
    return format(parseBackendDate(iso), "d MMM yyyy", { locale: it });
  } catch {
    return iso;
  }
}

export function formatTimeIt(iso: string): string {
  try {
    return format(parseBackendDate(iso), "HH:mm", { locale: it });
  } catch {
    return iso;
  }
}

export function formatRelativeIt(iso: string): string {
  try {
    return formatDistanceToNow(parseBackendDate(iso), { locale: it, addSuffix: true });
  } catch {
    return iso;
  }
}

export function formatCurrency(value: number, currency = "EUR"): string {
  try {
    return new Intl.NumberFormat("it-IT", {
      style: "currency",
      currency,
      maximumFractionDigits: value >= 1000 ? 0 : 2,
    }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}

export function formatNumber(value: number, decimals = 2): string {
  return new Intl.NumberFormat("it-IT", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

/** Compact it-IT number, e.g. 12_300_000 -> "12,3 Mln" (for volumes). */
export function formatCompactNumber(value: number, decimals = 1): string {
  return new Intl.NumberFormat("it-IT", {
    notation: "compact",
    maximumFractionDigits: decimals,
  }).format(value);
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: "$",
  EUR: "€",
  GBP: "£",
  JPY: "¥",
  CHF: "CHF",
  HKD: "HK$",
  CAD: "C$",
  AUD: "A$",
};

/** Short currency symbol for a currency code, falling back to the code itself. */
export function currencySymbol(code: string): string {
  return CURRENCY_SYMBOLS[code] ?? code;
}

/** Market cap in billions -> e.g. "3.450 mld $"; em-dash when unknown. */
export function formatMarketCap(bn: number | null, currency: string): string {
  if (bn === null || Number.isNaN(bn)) return "—";
  const decimals = bn >= 100 ? 0 : 1;
  return `${formatNumber(bn, decimals)} mld ${currencySymbol(currency)}`;
}

export function formatPercent(value: number, opts: { decimals?: number; withSign?: boolean } = {}): string {
  const { decimals = 2, withSign = true } = opts;
  const sign = withSign && value > 0 ? "+" : "";
  return `${sign}${formatNumber(value, decimals)}%`;
}

export function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/** Tailwind text-color class for a gain/loss/neutral numeric value. */
export function signColorClass(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "text-slate-400";
  if (value > 0) return "text-gain";
  if (value < 0) return "text-loss";
  return "text-slate-400";
}
