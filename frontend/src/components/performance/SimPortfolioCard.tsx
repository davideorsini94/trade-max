import Card from "../common/Card";
import InfoTip from "../common/InfoTip";
import Spinner from "../common/Spinner";
import ErrorBox from "../common/ErrorBox";
import type { SimPortfolioOut, SimPositionOut } from "../../api/types";
import { formatConfidence, formatNumber } from "../../lib/format";
import { gloss } from "../../lib/glossary";

const CLOSE_REASON_LABELS: Record<string, string> = {
  STOP_LOSS: "Stop loss",
  TAKE_PROFIT: "Take profit",
  HORIZON: "Scadenza",
  SELL_RECO: "Vendita consigliata",
};

interface Props {
  data: SimPortfolioOut | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}

/** Arrotonda alle due cifre mostrate PRIMA di decidere segno e colore.
 *
 * Le azioni sono stimate dividendo un nozionale per un prezzo, quindi un P&L
 * nominalmente nullo arriva come 1,1e-14: senza questo arrotondamento la stessa
 * posizione appariva come "+0,00%" in verde, dando un guadagno inesistente. */
function displayValue(value: number | null): number | null {
  if (value === null || value === undefined || Number.isNaN(value)) return null;
  return Math.round(value * 100) / 100;
}

function signedPct(value: number | null): string {
  const shown = displayValue(value);
  if (shown === null) return "n/d";
  const sign = shown > 0 ? "+" : "";
  return `${sign}${formatNumber(shown, 2)}%`;
}

function toneFor(value: number | null): string {
  const shown = displayValue(value);
  if (shown === null) return "text-slate-400";
  if (shown > 0) return "text-emerald-400";
  if (shown < 0) return "text-rose-400";
  return "text-slate-300";
}

function PositionRow({ position, closed }: { position: SimPositionOut; closed: boolean }) {
  const pnl = closed ? position.realized_pnl_pct : position.unrealized_pnl_pct;
  return (
    <tr className="border-b border-[var(--tm-border)] last:border-0">
      <td className="px-4 py-2">
        <span className="font-medium text-slate-100">{position.ticker}</span>
        {position.exit_ambiguous ? (
          <span
            className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-300"
            title="In quella giornata il prezzo ha toccato sia lo stop loss sia il take profit: con i dati giornalieri non si può sapere quale sia venuto prima, quindi si considera lo stop (l'ipotesi peggiore)."
          >
            ambigua
          </span>
        ) : null}
      </td>
      <td className="px-4 py-2 text-right tabular-nums text-slate-300">
        {formatNumber(position.weight_pct, 1)}%
      </td>
      <td className="px-4 py-2 text-right tabular-nums text-slate-400">
        {position.avg_entry_price !== null ? formatNumber(position.avg_entry_price, 2) : "n/d"}
      </td>
      <td className={`px-4 py-2 text-right font-medium tabular-nums ${toneFor(pnl)}`}>
        {signedPct(pnl)}
      </td>
      {closed ? (
        <td className="px-4 py-2 text-right text-xs text-slate-400">
          {position.close_reason ? CLOSE_REASON_LABELS[position.close_reason] : "n/d"}
        </td>
      ) : (
        <td className="px-4 py-2 text-right text-xs text-slate-400">
          {position.days_open !== null ? `${position.days_open} g` : "n/d"}
        </td>
      )}
    </tr>
  );
}

export default function SimPortfolioCard({ data, loading, error, onRetry }: Props) {
  if (loading) {
    return (
      <Card title="Portafoglio simulato del sistema">
        <Spinner />
      </Card>
    );
  }
  if (error) {
    return (
      <Card title="Portafoglio simulato del sistema">
        <ErrorBox message={error} onRetry={onRetry} />
      </Card>
    );
  }
  if (!data) return null;

  const currencies = Object.entries(data.by_currency);
  const empty = data.n_open === 0 && data.n_closed === 0 && data.n_stale === 0;
  const stats = data.stats;

  return (
    <Card
      title={
        <span className="flex items-center gap-1">
          Portafoglio simulato del sistema
          <InfoTip text={gloss("sim_portfolio")} ariaLabel="Cos'è il portafoglio simulato" />
        </span>
      }
    >
      <p className="text-xs leading-relaxed text-slate-500">
        Non è il tuo diario e non sono soldi tuoi: è un libro fittizio che il sistema tiene da
        solo su un capitale simbolico di {formatNumber(data.base_notional, 0)} per titolo, per
        sapere quanto è già esposto quando valuta un nuovo acquisto e per imparare da come sono
        andate davvero le sue operazioni. Commissioni non simulate.
      </p>

      {empty ? (
        <div className="mt-4 rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-6 py-8 text-center text-sm text-slate-400">
          Nessuna posizione simulata: il libro si popola quando il sistema emette un consiglio di
          acquisto con un'allocazione maggiore di zero.
        </div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="rounded-lg border border-[var(--tm-border)] bg-[var(--tm-surface)] p-3">
              <p className="text-xs uppercase tracking-wider text-slate-500">Aperte</p>
              <p className="mt-1 text-lg font-semibold tabular-nums text-slate-100">{data.n_open}</p>
            </div>
            <div className="rounded-lg border border-[var(--tm-border)] bg-[var(--tm-surface)] p-3">
              <p className="text-xs uppercase tracking-wider text-slate-500">Chiuse</p>
              <p className="mt-1 text-lg font-semibold tabular-nums text-slate-100">{data.n_closed}</p>
            </div>
            <div className="rounded-lg border border-[var(--tm-border)] bg-[var(--tm-surface)] p-3">
              <p className="flex items-center gap-1 text-xs uppercase tracking-wider text-slate-500">
                Esposizione
                <InfoTip text={gloss("sim_weight")} ariaLabel="Cos'è l'esposizione" />
              </p>
              <p className="mt-1 text-lg font-semibold tabular-nums text-slate-100">
                {formatNumber(data.gross_exposure_pct, 1)}%
              </p>
            </div>
            <div className="rounded-lg border border-[var(--tm-border)] bg-[var(--tm-surface)] p-3">
              <p className="flex items-center gap-1 text-xs uppercase tracking-wider text-slate-500">
                Senza prezzi
                <InfoTip text={gloss("sim_stale")} ariaLabel="Cosa sono le posizioni senza prezzi" />
              </p>
              <p className="mt-1 text-lg font-semibold tabular-nums text-slate-100">{data.n_stale}</p>
            </div>
          </div>

          {currencies.length > 0 ? (
            <div className="mt-4 space-y-1">
              {currencies.map(([currency, agg]) => (
                <p key={currency} className="text-sm text-slate-300">
                  <span className="text-slate-500">{currency}:</span>{" "}
                  <span className={toneFor(agg.realized_pnl)}>
                    {agg.realized_pnl > 0 ? "+" : ""}
                    {formatNumber(agg.realized_pnl, 2)}
                  </span>{" "}
                  <span className="text-xs text-slate-500">realizzato</span>
                  {agg.n_open > 0 ? (
                    <>
                      {" · "}
                      {agg.unrealized_pnl !== null ? (
                        <span className={toneFor(agg.unrealized_pnl)}>
                          {agg.unrealized_pnl > 0 ? "+" : ""}
                          {formatNumber(agg.unrealized_pnl, 2)}
                        </span>
                      ) : (
                        <span className="text-slate-400">n/d</span>
                      )}{" "}
                      <span className="text-xs text-slate-500">non realizzato</span>
                    </>
                  ) : null}
                </p>
              ))}
              <p className="pt-1 text-xs text-slate-500">
                Gli importi restano separati per valuta: l'app non converte fra valute, perché non
                userebbe un tasso di cambio reale.
              </p>
            </div>
          ) : null}

          {data.open_positions.length > 0 ? (
            <div className="mt-5">
              <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-500">
                Posizioni aperte
              </h4>
              <div className="tm-scroll-x overflow-hidden rounded-lg border border-[var(--tm-border)]">
                <table className="w-full min-w-[30rem] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-[var(--tm-border)] text-left text-xs uppercase tracking-wider text-slate-500">
                      <th className="px-4 py-2 font-medium">Titolo</th>
                      <th className="px-4 py-2 text-right font-medium">Peso</th>
                      <th className="px-4 py-2 text-right font-medium">Ingresso</th>
                      <th className="px-4 py-2 text-right font-medium">P&amp;L</th>
                      <th className="px-4 py-2 text-right font-medium">Da</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.open_positions.map((position) => (
                      <PositionRow key={position.id} position={position} closed={false} />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          {data.recent_closed.length > 0 ? (
            <div className="mt-5">
              <h4 className="mb-2 flex items-center gap-1 text-xs font-medium uppercase tracking-wider text-slate-500">
                Chiusure recenti
                <InfoTip text={gloss("sim_close_reason")} ariaLabel="Cos'è il motivo di chiusura" />
              </h4>
              <div className="tm-scroll-x overflow-hidden rounded-lg border border-[var(--tm-border)]">
                <table className="w-full min-w-[30rem] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-[var(--tm-border)] text-left text-xs uppercase tracking-wider text-slate-500">
                      <th className="px-4 py-2 font-medium">Titolo</th>
                      <th className="px-4 py-2 text-right font-medium">Peso</th>
                      <th className="px-4 py-2 text-right font-medium">Ingresso</th>
                      <th className="px-4 py-2 text-right font-medium">Risultato</th>
                      <th className="px-4 py-2 text-right font-medium">Motivo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.recent_closed.map((position) => (
                      <PositionRow key={position.id} position={position} closed />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          <div className="mt-5 rounded-lg border border-[var(--tm-border)] bg-slate-900/40 p-4">
            <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-500">
              Statistiche di percorso
            </h4>
            {stats.status === "dati_insufficienti" ? (
              <p className="text-sm text-slate-400">
                Dati insufficienti: finora {stats.n}{" "}
                {stats.n === 1 ? "posizione chiusa" : "posizioni chiuse"} su {stats.n_symbols}{" "}
                {stats.n_symbols === 1 ? "titolo" : "titoli"} — ne servono almeno {stats.min_n} su{" "}
                {stats.min_symbols} titoli diversi. Con meno campioni un tasso di stop colpiti
                misurerebbe il denominatore, non la strategia.
              </p>
            ) : (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div>
                  <p className="text-xs text-slate-500">Stop colpiti</p>
                  <p className="mt-0.5 text-sm font-semibold tabular-nums text-slate-100">
                    {stats.stop_hit_rate !== null ? formatConfidence(stats.stop_hit_rate) : "n/d"}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-slate-500">Take profit</p>
                  <p className="mt-0.5 text-sm font-semibold tabular-nums text-slate-100">
                    {stats.tp_hit_rate !== null ? formatConfidence(stats.tp_hit_rate) : "n/d"}
                  </p>
                </div>
                <div>
                  <p className="flex items-center gap-1 text-xs text-slate-500">
                    Escurs. avversa
                    <InfoTip text={gloss("mae")} ariaLabel="Cos'è l'escursione avversa massima" />
                  </p>
                  <p className="mt-0.5 text-sm font-semibold tabular-nums text-rose-400">
                    {signedPct(stats.avg_mae_pct)}
                  </p>
                </div>
                <div>
                  <p className="flex items-center gap-1 text-xs text-slate-500">
                    Escurs. favorevole
                    <InfoTip text={gloss("mfe")} ariaLabel="Cos'è l'escursione favorevole massima" />
                  </p>
                  <p className="mt-0.5 text-sm font-semibold tabular-nums text-emerald-400">
                    {signedPct(stats.avg_mfe_pct)}
                  </p>
                </div>
              </div>
            )}
            {stats.n_ambiguous > 0 ? (
              <p className="mt-2 text-xs text-slate-500">
                {stats.n_ambiguous}{" "}
                {stats.n_ambiguous === 1 ? "chiusura" : "chiusure"} con stop e take profit toccati
                nella stessa giornata: l'ordine reale non è conoscibile con i dati giornalieri, e si
                è scelto lo stop (l'ipotesi peggiore).
              </p>
            ) : null}
          </div>
        </>
      )}
    </Card>
  );
}
