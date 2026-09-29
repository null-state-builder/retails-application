// Stock (ticket 07): physical, valued, unvalued, available-to-sell/transfer
// quantities with holds, reservations and reasons, now or as of a past time.
//
// Four views over the same scoped rows (E173-E176): on-hand (every physical
// portion), availability (ATS/transferable), in-transit and quarantine — plus
// a same-scope summary (E172). A cost-valued view without the cost grant is
// refused as `FIELD_DENIED`, shown here as its own state rather than folded
// into the generic "denied" card, since it is a narrower thing than "you may
// not see this": the quantity view of the very same rows is still open.
import { useState } from "react";
import { AlertTriangle, ChevronRight, History, ShieldAlert } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { Denied, Feedback, Field, hold, listState, useGoodsFetch } from "../lib/goodsScreen";
import { damageRowWords, type DamageReport } from "../lib/goodsMovements";
import {
  BASES,
  BASIS_LABEL,
  LIMITATION_INTRO,
  SEASON_UNKNOWN_HISTORICAL,
  STOCK_STATES,
  STOCK_STATE_LABEL,
  eligibilityLabel,
  isWarehouseKind,
  journeyEventLabel,
  limitationLabel,
  readLimitations,
  seasonLabel,
  unvaluedQty,
  type Basis,
  type JourneyEvent,
  type StockRow,
  type StockState,
  type StockSummary,
} from "../lib/goodsStock";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { formatDateTime, formatPaiseString } from "../lib/format";
import "./GoodsAcceptance.css";

type ViewKind = "on-hand" | "availability" | "in-transit" | "quarantine";

/** E209. Damage found on this screen is recorded where it is found; the command
 *  puts the goods in quarantine under a hold at once and records the condition.
 *  The destination is not offered, because it is not the caller's to choose. */
const MARK_DAMAGED = "/goods-v1/outbound/mark-damaged";

/** OPS-05. Reporting damage opens a report a different authorised person has to
 *  confirm or reject; the quarantine view shows where that has got to. The
 *  decision itself is made on the Movements tab, never here — the person who
 *  found the damage is exactly the person who may not decide it. */
const DAMAGE_REPORTS = "/goods-v1/outbound/damage-reports";

const VIEW_PATH: Record<ViewKind, string> = {
  "on-hand": "/goods-v1/stockledger/on-hand",
  availability: "/goods-v1/stock/availability",
  "in-transit": "/goods-v1/stockledger/in-transit",
  quarantine: "/goods-v1/stockledger/quarantine",
};

const VIEW_LABEL: Record<ViewKind, string> = {
  "on-hand": "On hand",
  availability: "Availability",
  "in-transit": "In transit",
  quarantine: "Quarantine",
};

interface ReadState<T> {
  loading: boolean;
  data: T | null;
  deniedField: boolean;
  deniedAction: boolean;
  failure: string;
  /** Re-run the read. A command that changed this stock (E209) refetches the
   *  authoritative answer rather than adjusting a number locally (design §8.2). */
  reload: () => void;
}

/** A goods stock read, over the one shared fetch/error hook
 *  (`goodsScreen.useGoodsFetch`) rather than a second copy of its
 *  loading/denied/failure state machine — this thin wrapper only renames the
 *  fields to the shape this screen already reads. */
function useGoodsRead<T>(url: string | null): ReadState<T> {
  const { value, loading, denied, deniedField, failure, reload } = useGoodsFetch<T, T | null>(
    url,
    (r) => r,
    null,
  );
  return { loading, data: value, deniedField, deniedAction: denied, failure, reload };
}

function buildUrl(base: string, params: Record<string, string | undefined>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) usp.set(k, v);
  const qs = usp.toString();
  return `${base}${qs ? `?${qs}` : ""}`;
}

export function GoodsStockPage() {
  const { session } = useAuth();
  const [params, setParams] = useSearchParams();
  const siteId = params.get("site_id") ?? "";
  const state = (params.get("state") as StockState | "") || "";
  const basis = (params.get("basis") as Basis) || "quantity";
  const asOf = params.get("as_of") ?? "";
  // An unrecognised `?view=` (a stale link, a typo) falls back to "on-hand"
  // rather than building a request against `VIEW_PATH[undefined]`.
  const rawView = params.get("view");
  const view = (rawView && rawView in VIEW_PATH ? rawView : "on-hand") as ViewKind;

  function set(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  }

  // `?as_of=` is free text in the URL — a malformed value must show as an
  // ordinary validation problem (the server's own INVALID_REQUEST), never
  // crash the screen on `Invalid Date.toISOString()`.
  const asOfDate = asOf ? new Date(asOf) : null;
  const asOfIso = asOfDate && !Number.isNaN(asOfDate.getTime()) ? asOfDate.toISOString() : undefined;
  const season = params.get("season") ?? "";
  const query = {
    site_id: siteId || undefined,
    state: state || undefined,
    basis,
    as_of: asOfIso,
    season: season || undefined,
  };
  const rows = useGoodsRead<{ items: StockRow[]; next_cursor: string | null; as_of: string }>(
    buildUrl(VIEW_PATH[view], query),
  );
  const summary = useGoodsRead<StockSummary>(
    // The same season filter as the rows, so the totals are the totals of what
    // is on screen rather than of a wider scope.
    buildUrl("/goods-v1/stockledger/summary", {
      site_id: siteId || undefined,
      basis,
      as_of: asOfIso,
      season: season || undefined,
    }),
  );

  // OPS-05: on the quarantine view, a damaged row says what happened to the
  // report that put it there — still waiting, confirmed, or rejected — and who
  // decided it. Read only on that view: the other three are not about holds,
  // and a read nobody looks at is a read nobody should be making.
  const reviews = useGoodsFetch<{ items: DamageReport[] }, DamageReport[]>(
    view === "quarantine" ? buildUrl(DAMAGE_REPORTS, { site: siteId || undefined }) : null,
    (r) => r.items ?? [],
    [],
  );

  const [damageError, setDamageError] = useState("");
  const [damageOk, setDamageOk] = useState("");
  const canMarkDamaged = hold(session, "movement.draft");

  async function markDamaged(row: StockRow, qty: number, reason: string) {
    setDamageError("");
    setDamageOk("");
    try {
      await api.post(MARK_DAMAGED, {
        kind: "hold",
        site_id: row.site_id,
        reason_code: reason,
        evidence_ids: [],
        lines: [
          {
            line_key: crypto.randomUUID(),
            sku_id: row.sku_id,
            qty,
            source_location_id: row.location_id,
            condition: row.condition,
            ...(row.origin_id ? { origin_id: row.origin_id } : {}),
          },
        ],
        ...goodsMeta(),
      });
      setDamageOk(
        `${qty} piece(s) marked damaged and held in quarantine. ` +
          "Someone else has to confirm or reject the report, on the Movements tab.",
      );
      rows.reload();
      summary.reload();
      reviews.reload();
    } catch (e) {
      setDamageError(apiErrorMessage(e));
    }
  }

  // R27: the *server* says which parts of its answer it could not replay. The
  // screen renders that list and never works it out from the `as_of` it sent:
  // a timestamp the server clamped back to a live read has nothing to declare,
  // and claiming otherwise would warn about a limitation that does not apply.
  const limitations = readLimitations(summary, rows);
  // `session.sites[].type` is the one source of a site's real kind (R28) —
  // never guessed back from a row's own quantities, which a fully held
  // warehouse row carries none of either way.
  const siteKinds = new Map((session?.sites ?? []).map((s) => [s.id, s.type]));

  if (!session) return null;

  return (
    <div className="stock-layout">
      <PageHeader title="Stock" lead="Physical, valued and available quantities, with holds, reservations and reasons." />

      <div className="form-grid">
        <Field id="stock-site" label="Site">
          <select
            id="stock-site"
            className="select"
            value={siteId}
            onChange={(e) => set("site_id", e.target.value)}
            data-testid="stock-site"
          >
            <option value="">All sites in scope</option>
            {session.sites.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.code})
              </option>
            ))}
          </select>
        </Field>
        <Field id="stock-state" label="State">
          <select
            id="stock-state"
            className="select"
            value={state}
            onChange={(e) => set("state", e.target.value)}
            data-testid="stock-state"
          >
            <option value="">Any</option>
            {STOCK_STATES.map((s) => (
              <option key={s} value={s}>
                {STOCK_STATE_LABEL[s]}
              </option>
            ))}
          </select>
        </Field>
        <Field id="stock-basis" label="Basis">
          <select
            id="stock-basis"
            className="select"
            value={basis}
            onChange={(e) => set("basis", e.target.value)}
            data-testid="stock-basis"
          >
            {BASES.map((b) => (
              <option key={b} value={b}>
                {BASIS_LABEL[b]}
              </option>
            ))}
          </select>
        </Field>
        {/* OPS-03: one filter, by meaning. Opening stock whose buying cohort
            could not be established is a first-class thing to go looking for,
            and a season id in a URL would not survive a reseeded master. */}
        <Field id="stock-season" label="Season">
          <select
            id="stock-season"
            className="select"
            value={season}
            onChange={(e) => set("season", e.target.value)}
            data-testid="stock-season"
          >
            <option value="">Any</option>
            <option value={SEASON_UNKNOWN_HISTORICAL}>Unknown historical season</option>
          </select>
        </Field>
        <Field id="stock-as-of" label="As of">
          <input
            id="stock-as-of"
            className="input"
            type="datetime-local"
            value={asOf}
            onChange={(e) => set("as_of", e.target.value)}
            data-testid="stock-as-of"
          />
        </Field>
      </div>

      {limitations.length > 0 && (
        <div className="stock-limitation" data-testid="stock-limitations">
          <p className="stock-limitation-intro">
            <AlertTriangle size={14} /> {LIMITATION_INTRO}
          </p>
          <ul className="stock-limitation-list">
            {limitations.map((code) => (
              <li key={code} data-testid={`stock-limitation-${code}`}>
                {limitationLabel(code)}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="toolbar">
        {(Object.keys(VIEW_PATH) as ViewKind[]).map((v) => (
          <button
            key={v}
            className={`btn btn-sm${v === view ? " btn-active" : ""}`}
            onClick={() => set("view", v)}
            data-testid={`stock-view-${v}`}
          >
            {VIEW_LABEL[v]}
          </button>
        ))}
      </div>

      <SummaryPanel summary={summary} />

      <Feedback error={damageError} ok={damageOk} />

      <StockTable
        rows={rows}
        basis={basis}
        view={view}
        siteKinds={siteKinds}
        reviews={reviews.value}
        onMarkDamaged={canMarkDamaged ? markDamaged : null}
      />
    </div>
  );
}

function SummaryPanel({ summary }: { summary: ReadState<StockSummary> }) {
  if (summary.loading) return <span className="muted">Loading summary…</span>;
  if (summary.deniedField) {
    return (
      <p className="warn-note" data-testid="stock-summary-field-denied">
        Value totals need the cost field grant. Showing without them.
      </p>
    );
  }
  if (summary.deniedAction || !summary.data) return null;
  const t = summary.data.totals;
  return (
    <dl className="gr-facts" data-testid="stock-summary">
      <div>
        <dt>Physical</dt>
        <dd>{t.physical_qty}</dd>
      </div>
      <div>
        <dt>Valued</dt>
        <dd>{t.valued_qty}</dd>
      </div>
      <div>
        <dt>Unvalued</dt>
        <dd>{t.unvalued_qty}</dd>
      </div>
      <div>
        <dt>Available to sell</dt>
        <dd>{t.ats_qty}</dd>
      </div>
      <div>
        <dt>Available to transfer</dt>
        <dd>{t.transferable_qty}</dd>
      </div>
      <div>
        <dt>In transit</dt>
        <dd>{t.transit_qty}</dd>
      </div>
      {summary.data.basis !== "quantity" && (
        <div>
          <dt>Value</dt>
          <dd data-testid="stock-summary-value">
            {t.value_paise !== undefined ? formatPaiseString(t.value_paise) : "Unknown"}
            {t.value_completeness && t.value_completeness !== "complete" ? ` (${t.value_completeness})` : ""}
          </dd>
        </div>
      )}
    </dl>
  );
}

function StockTable({
  rows,
  basis,
  view,
  siteKinds,
  reviews,
  onMarkDamaged,
}: {
  rows: ReadState<{ items: StockRow[]; next_cursor: string | null; as_of: string }>;
  basis: Basis;
  view: ViewKind;
  siteKinds: Map<string, string>;
  reviews: DamageReport[];
  onMarkDamaged: ((row: StockRow, qty: number, reason: string) => Promise<void>) | null;
}) {
  if (rows.deniedField) {
    return (
      <div className="card section-card" data-testid="stock-field-denied">
        <p className="eyebrow">Not shown</p>
        <h3 className="h3">Value needs the cost grant</h3>
        <p className="lead">
          Requesting {BASIS_LABEL[basis].toLowerCase()} here is refused without the cost field
          grant. Switch the basis to "Quantity only" to see these rows without value.
        </p>
      </div>
    );
  }
  if (rows.deniedAction) return <Denied what="stock" />;

  const emptyText =
    view === "in-transit"
      ? "Nothing is in transit for this scope."
      : view === "quarantine"
        ? "Nothing is quarantined or held for this scope."
        : "No stock in this scope.";

  const state = listState({ loading: rows.loading, failure: rows.failure, empty: (rows.data?.items.length ?? 0) === 0 }, emptyText);
  if (state) return state;

  const items = rows.data?.items ?? [];
  return (
    <div className="stock-table-wrap">
      <table data-testid="stock-table">
        <thead>
          <tr>
            <th>Location</th>
            <th>SKU / description</th>
            <th>Season</th>
            <th>Condition</th>
            <th className="num">Physical</th>
            <th className="num">Valued</th>
            <th className="num">Unvalued</th>
            <th className="num">Accepted</th>
            <th className="num">{view === "in-transit" ? "In transit" : "ATS / Transfer"}</th>
            <th className="num">Held</th>
            <th className="num">Reserved</th>
            {basis !== "quantity" && <th className="num">Value</th>}
            <th>Reasons</th>
            {view === "quarantine" && <th>Damage review</th>}
            {onMarkDamaged && view !== "in-transit" && <th>Found damaged?</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((row, i) => (
            <StockRowView
              key={i}
              row={row}
              basis={basis}
              view={view}
              isWarehouse={isWarehouseKind(row.site_id ? siteKinds.get(row.site_id) : undefined)}
              review={view === "quarantine" ? damageRowWords(row, reviews) : null}
              onMarkDamaged={view === "in-transit" ? null : onMarkDamaged}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StockRowView({
  row,
  basis,
  view,
  isWarehouse,
  review,
  onMarkDamaged,
}: {
  row: StockRow;
  basis: Basis;
  view: ViewKind;
  isWarehouse: boolean;
  /** The damage report speaking for this row, in words, on the quarantine view. */
  review: string | null;
  onMarkDamaged: ((row: StockRow, qty: number, reason: string) => Promise<void>) | null;
}) {
  const valueKey = basis === "cost" ? row.cost_value_paise : basis === "ticket" ? row.ticket_value_paise : undefined;
  const available = view === "in-transit" ? row.physical_qty : isWarehouse ? row.transferable_qty : row.ats_qty;
  return (
    <tr>
      <td>
        {view === "in-transit"
          ? `${row.source_site_id ?? "?"} → ${row.destination_site_id ?? "?"}`
          : row.location_id ?? "—"}
      </td>
      <td>
        {row.description || (row.sku_id ? row.sku_id.slice(0, 8) : "Unidentified")}
        {row.origin_id && (
          <Link to={`/goods/stock/origins/${row.origin_id}`} className="chip chip-navy" data-testid="stock-journey-link">
            Journey <ChevronRight size={12} />
          </Link>
        )}
      </td>
      {/* The season these goods are in now. A later governed correction is
          what shows; the season the row opened under stays beside it, because
          a correction adds a fact and never rewrites one (OPS-03). */}
      <td data-testid="stock-season-label">
        {seasonLabel(row)}
        {row.season_original && (
          <span className="muted"> (was {row.season_original.season_label || "—"})</span>
        )}
      </td>
      <td>{row.condition}</td>
      <td className="num">{row.physical_qty}</td>
      <td className="num">{row.valued_qty}</td>
      <td className="num">{unvaluedQty(row)}</td>
      {/* Approval values goods; only acceptance makes them usable, and the two
          numbers differ for as long as a receipt sits unchecked. A stock screen
          that showed only "valued" would read as though it were already on the
          floor (change PRD §4.1 outcome 1). */}
      <td className="num" data-testid="stock-accepted-qty">
        {row.accepted_qty}
      </td>
      <td className="num" data-testid="stock-available-qty">
        {available}
      </td>
      <td className="num">{row.held_qty}</td>
      <td className="num">{row.reserved_qty}</td>
      {basis !== "quantity" && <td className="num">{formatPaiseString(valueKey ?? null)}</td>}
      <td>
        {row.eligibility_reasons.length > 0 ? (
          <details>
            <summary>{row.eligibility_reasons.length} reason(s)</summary>
            <ul className="stock-reasons">
              {row.eligibility_reasons.map((r) => (
                <li key={r}>{eligibilityLabel(r)}</li>
              ))}
            </ul>
          </details>
        ) : (
          "—"
        )}
      </td>
      {view === "quarantine" && (
        <td data-testid="stock-damage-review">
          {review ?? (row.condition === "damaged" ? "No report on record" : "—")}
        </td>
      )}
      {onMarkDamaged && (
        <td>
          <MarkDamaged row={row} onMark={onMarkDamaged} />
        </td>
      )}
    </tr>
  );
}

/** E209 from where the damage is actually found. No destination is asked for:
 *  damaged goods go to quarantine under a hold, and a location supplied by the
 *  client cannot override that. */
function MarkDamaged({
  row,
  onMark,
}: {
  row: StockRow;
  onMark: (row: StockRow, qty: number, reason: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [qty, setQty] = useState(1);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const movable = row.physical_qty - row.held_qty;

  if (!row.location_id || !row.site_id || movable < 1) return <span className="muted">—</span>;
  if (!open) {
    return (
      <button className="btn btn-sm" onClick={() => setOpen(true)} data-testid="stock-mark-damaged">
        <ShieldAlert size={14} /> Mark damaged
      </button>
    );
  }
  return (
    <div className="form-grid" data-testid="stock-damage-form">
      <input
        className="input"
        type="number"
        min={1}
        max={movable}
        value={qty}
        onChange={(e) => setQty(Number(e.target.value))}
        aria-label={`How many pieces are damaged (up to ${movable})`}
        data-testid="stock-damage-qty"
      />
      <input
        className="input"
        placeholder="What is wrong with them"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        aria-label="Reason"
        data-testid="stock-damage-reason"
      />
      <button
        className="btn btn-cta"
        disabled={busy || !reason}
        onClick={async () => {
          setBusy(true);
          await onMark(row, qty, reason);
          setBusy(false);
          setOpen(false);
          setReason("");
        }}
        data-testid="stock-damage-confirm"
      >
        Hold in quarantine
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Origin journey (E181)
// ---------------------------------------------------------------------------

export function GoodsOriginJourneyPage() {
  const { id } = useParams<{ id: string }>();
  const journey = useGoodsRead<{ items: JourneyEvent[]; next_cursor: string | null }>(
    id ? `/goods-v1/stockledger/origins/${id}/journey` : null,
  );

  return (
    <div className="stock-layout">
      <PageHeader title="Origin journey" lead="The published events on this origin, in order." />
      {journey.deniedAction && <Denied what="origin" />}
      {journey.failure && <p className="warn-note">{journey.failure}</p>}
      {journey.loading && <span className="muted">Loading…</span>}
      {!journey.loading && !journey.deniedAction && !journey.failure && (journey.data?.items.length ?? 0) === 0 && (
        <span className="muted">No events found for this origin.</span>
      )}
      {journey.data && journey.data.items.length > 0 && (
        <ol className="journey-timeline" data-testid="journey-timeline">
          {journey.data.items.map((event) => (
            <li key={event.event_id} data-testid={`journey-event-${event.event_id}`}>
              <History size={14} className="journey-icon" />
              <span className="journey-kind">{journeyEventLabel(event.event_kind)}</span>
              <div className="journey-meta">
                {formatDateTime(event.event_at)} · qty {event.qty}
                {event.number ? ` · ${event.number}` : ""}
                {event.source_site_id || event.destination_site_id
                  ? ` · ${event.source_site_id ?? "?"} → ${event.destination_site_id ?? "?"}`
                  : ""}
                {event.value_paise !== undefined ? ` · ${formatPaiseString(event.value_paise)}` : ""}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
