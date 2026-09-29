// Goods-v1 bookings, as the one Bookings screen draws them (store and warehouse
// operations PRD §12). This file holds everything that speaks the goods-v1
// booking contract - the list read, the pickers, the create command and the
// booking page with its confirm, correct, link and close actions - so
// `Bookings.tsx` keeps to the older contract and each file reaches one API
// (`lib/goodsNamespace.test.ts`), the split `GoodsApprovalRows.tsx` uses.
//
// Three things the booking page exists to keep honest:
//   * still to come is booked minus arrived plus reversed — a reversal puts the
//     pieces back on order, and the table says so per line, not only in total;
//   * size, MRP and cost are optional on a booking line. A figure nobody gave
//     reads as a dash — never a zero — and cost is drawn only for someone whose
//     grant carries it (the server leaves it out for everyone else);
//   * a confirmed booking changes only through dated, append-only corrections.
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Link2, PackagePlus, Send } from "lucide-react";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Denied,
  Feedback,
  Field,
  hold,
  masterLabel,
  PickerField,
  useGoodsFetch,
  usePagedPicker,
  useResourceDoc,
  type Page,
  type PickerRow,
  type ResourceDTO,
} from "../lib/goodsScreen";
import {
  receivedOfBooked,
  type BookingDraftLine,
  type BookingHeader,
  type BookingProgress,
} from "../lib/goodsReceiving";
import { useAuth } from "../auth/AuthContext";
import {
  BookingLinesTable,
  BookingPageHeader,
  BookingTiles,
  type BookingLineView,
} from "../components/BookingBits";
import { goodsStatus, lineTotals, paiseOf } from "../lib/bookingModel";
import { rupeesToPaise } from "../lib/format";
import { OpenToBuyPanel } from "./OpenToBuyPanel";
import "./GoodsReceiving.css";
import "./Booking.css";

type BookingSummary = PickerRow<"/goods-v1/bookings">;
type Session = ReturnType<typeof useAuth>["session"];

/** The grants that read goods-v1 bookings (the server's `BOOKING_READ_ACTIONS`). */
export const GOODS_BOOKING_READ_ACTIONS = [
  "booking.manage",
  "receive.arrival",
  "pt.prepare",
  "pt.view",
];

export function readsGoodsBookings(session: Session): boolean {
  return GOODS_BOOKING_READ_ACTIONS.some((action) => hold(session, action));
}

export function managesGoodsBookings(session: Session): boolean {
  return hold(session, "booking.manage");
}

/** The goods-v1 booking list, for the one Bookings list to merge. */
export function useGoodsBookingSummaries(enabled: boolean) {
  return useGoodsFetch<Page<BookingSummary>, BookingSummary[]>(
    enabled ? "/goods-v1/bookings?limit=100" : null,
    (r) => r.items ?? [],
    [],
  );
}

// --------------------------------------------------------------------------
// The form's goods-v1 pieces (the form itself is `Bookings.tsx`'s)
// --------------------------------------------------------------------------

export interface GoodsPickerValue {
  vendor_id: string;
  brand_id: string;
  season_id: string;
  entity_id: string;
}

/** Vendor, brand and season from the goods-v1 reference reads, and - when the
 *  store runs on goods-v1 - the company buying the goods. The ids are the same
 *  master rows the older engine books against, so one choice serves either. */
export function GoodsBookingPickers({
  value,
  onChange,
  showEntity,
}: {
  value: GoodsPickerValue;
  onChange: (next: GoodsPickerValue) => void;
  showEntity: boolean;
}) {
  const vendors = usePagedPicker("/goods-v1/vendors", {}, value.vendor_id, (row) =>
    masterLabel(row, true),
  );
  const brands = usePagedPicker("/goods-v1/masters/brands", {}, value.brand_id, (row) =>
    masterLabel(row),
  );
  const seasons = usePagedPicker("/goods-v1/masters/seasons", {}, value.season_id, (row) =>
    masterLabel(row),
  );
  // A buyer reads the entities they may book for through their own narrow
  // reference read (E006 step 5, GSA-T05): code, name and state, nothing more.
  const entities = usePagedPicker(
    "/goods-v1/masters/entities",
    showEntity ? {} : null,
    value.entity_id,
    (row) => masterLabel(row, true),
  );

  // One company to choose from is no choice: it is picked for you.
  const onlyEntity =
    showEntity &&
    !entities.loading &&
    !entities.query &&
    !entities.hasMore &&
    entities.options.length === 1
      ? (entities.options[0]?.id ?? "")
      : "";
  useEffect(() => {
    if (onlyEntity && !value.entity_id) onChange({ ...value, entity_id: onlyEntity });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onlyEntity, value.entity_id]);

  return (
    <>
      <PickerField
        id="gb-vendor"
        label="Vendor"
        noun="vendor"
        placeholder="Choose a vendor"
        value={value.vendor_id}
        onChange={(id) => onChange({ ...value, vendor_id: id })}
        picker={vendors}
      />
      <PickerField
        id="gb-brand"
        label="Brand"
        noun="brand"
        placeholder="Choose a brand"
        value={value.brand_id}
        onChange={(id) => onChange({ ...value, brand_id: id })}
        picker={brands}
      />
      <PickerField
        id="gb-season"
        label="Season"
        noun="season"
        placeholder="Choose a season"
        value={value.season_id}
        onChange={(id) => onChange({ ...value, season_id: id })}
        picker={seasons}
      />
      {showEntity && (
        <PickerField
          id="gb-entity"
          label="Company (legal entity)"
          hint="The company buying the goods. The store must be one of its stores."
          noun="legal entity"
          placeholder="Choose a company"
          value={value.entity_id}
          onChange={(id) => onChange({ ...value, entity_id: id })}
          picker={entities}
        />
      )}
    </>
  );
}

export interface GoodsBookingInput extends GoodsPickerValue {
  destination_site_id: string;
  commercial_label?: string;
  vendor_ref?: string;
  expected_date?: string;
  lines: Omit<BookingDraftLine, "line_key">[];
}

/** Save a goods-v1 booking as an unnumbered draft; returns its id. */
export async function createGoodsBooking(input: GoodsBookingInput): Promise<string> {
  const body = {
    ...input,
    lines: input.lines.map((line) => ({ line_key: crypto.randomUUID(), ...line })),
    ...goodsMeta(),
  };
  const { data } = await api.post<{ id: string }>("/goods-v1/bookings", body);
  return data.id;
}

// --------------------------------------------------------------------------
// The booking page: progress, corrections, receipt links
// --------------------------------------------------------------------------

/** Rupees typed into a box, as the integer-paise string the wire takes. */
function paiseText(typed: string): string | null {
  const trimmed = typed.trim();
  if (!trimmed) return null;
  const paise = rupeesToPaise(trimmed);
  if (paise === null) throw new Error(`"${trimmed}" is not an amount.`);
  return String(paise);
}

function CorrectionForm({
  booking,
  sites,
  showCost,
  onDone,
}: {
  booking: ResourceDTO<BookingProgress>;
  sites: { id: string; code: string; name: string }[];
  showCost: boolean;
  onDone: () => void;
}) {
  // A booking confirmed with only its company has no store yet (GSA-T05). The
  // first store named here becomes its store for good.
  const unplaced = booking.context?.site_id == null;
  const [destination, setDestination] = useState("");
  const [reason, setReason] = useState("");
  const [lineKey, setLineKey] = useState("");
  const [qty, setQty] = useState("");
  const [description, setDescription] = useState("");
  const [cost, setCost] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setError("");
    setOk("");
    setBusy(true);
    try {
      const costPaise = showCost ? paiseText(cost) : null;
      const lineChange = {
        ...(qty ? { qty: Number(qty) } : {}),
        ...(description.trim() ? { description: description.trim() } : {}),
        ...(costPaise ? { cost_paise: costPaise } : {}),
      };
      await api.post(`/goods-v1/bookings/${booking.id}/corrections`, {
        reason_code: reason,
        effective_at: new Date().toISOString(),
        ...(note || destination
          ? {
              header_changes: {
                ...(note ? { notes: note } : {}),
                ...(destination ? { destination_site_id: destination } : {}),
              },
            }
          : {}),
        ...(lineKey && Object.keys(lineChange).length
          ? {
              line_changes: [
                {
                  original_line_key: lineKey,
                  replacement_line_key: crypto.randomUUID(),
                  ...lineChange,
                },
              ],
            }
          : {}),
        ...goodsMeta(booking.revision),
      });
      setOk("Correction recorded. The original line is kept and linked to its replacement.");
      onDone();
    } catch (e) {
      setError(e instanceof Error && !("response" in e) ? e.message : apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gr-panel" data-testid="gb-correction">
      <h4 className="gr-h4">Correct this booking</h4>
      <p className="gr-hint">
        A confirmed booking is never edited. A correction is added with its own date and reason; the
        line it replaces stays readable, and everything already arrived against it follows.
      </p>
      <Feedback error={error} ok={ok} />
      <div className="form-grid">
        <Field id="gb-c-reason" label="Reason">
          <input
            id="gb-c-reason"
            className="input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            data-testid="gb-c-reason"
          />
        </Field>
        <Field id="gb-c-line" label="Line to change">
          <select
            id="gb-c-line"
            className="select"
            value={lineKey}
            onChange={(e) => setLineKey(e.target.value)}
            data-testid="gb-c-line"
          >
            <option value="">No line change</option>
            {booking.data.lines.items.map((line) => (
              <option key={line.line_key} value={line.line_key}>
                {line.style_code ?? "Line"}
                {line.size_label ? ` · ${line.size_label}` : ""} — {line.ordered_qty} booked
              </option>
            ))}
          </select>
        </Field>
        <Field id="gb-c-qty" label="New quantity">
          <input
            id="gb-c-qty"
            className="input"
            type="number"
            min={1}
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            data-testid="gb-c-qty"
          />
        </Field>
        <Field id="gb-c-desc" label="New description">
          <input
            id="gb-c-desc"
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            data-testid="gb-c-desc"
          />
        </Field>
        {showCost && (
          <Field id="gb-c-cost" label="New cost per piece (₹)">
            <input
              id="gb-c-cost"
              className="input"
              inputMode="decimal"
              value={cost}
              onChange={(e) => setCost(e.target.value)}
              data-testid="gb-c-cost"
            />
          </Field>
        )}
        {unplaced && (
          <Field
            id="gb-c-destination"
            label="Store"
            hint="Naming it sets this booking's store for good; goods arrive against it only after that."
          >
            <select
              id="gb-c-destination"
              className="select"
              aria-describedby="gb-c-destination-hint"
              value={destination}
              onChange={(e) => setDestination(e.target.value)}
              data-testid="gb-c-destination"
            >
              <option value="">Not decided yet</option>
              {sites.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name} ({row.code})
                </option>
              ))}
            </select>
          </Field>
        )}
        <Field id="gb-c-note" label="Note">
          <input
            id="gb-c-note"
            className="input"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            data-testid="gb-c-note"
          />
        </Field>
      </div>
      <button
        className="btn btn-cta btn-sm"
        onClick={submit}
        disabled={busy || !reason}
        data-testid="gb-c-submit"
      >
        Record correction
      </button>
    </div>
  );
}

function ReceiptLinkForm({
  booking,
  onDone,
}: {
  booking: ResourceDTO<BookingProgress>;
  onDone: () => void;
}) {
  const [grnId, setGrnId] = useState("");
  const [bookingLine, setBookingLine] = useState("");
  const [grnLine, setGrnLine] = useState("");
  const [qty, setQty] = useState("");
  const [reason, setReason] = useState("RECEIVED");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setError("");
    setOk("");
    setBusy(true);
    try {
      await api.post(`/goods-v1/bookings/${booking.id}/receipt-links`, {
        grn_id: grnId,
        reason_code: reason,
        effective_at: new Date().toISOString(),
        links: [{ booking_line_key: bookingLine, grn_line_key: grnLine, qty: Number(qty) }],
        ...goodsMeta(booking.revision),
      });
      setOk("Linked. The line's arrived and still-to-come pieces have moved.");
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gr-panel" data-testid="gb-link-form">
      <h4 className="gr-h4">
        <Link2 size={14} /> Link an arrival
      </h4>
      <p className="gr-hint">
        When a GRN is issued, each counted line that matches exactly one booked line (same style and
        size) is linked at once. Anything else waits here: somebody decides which counted line
        answers which booked line, and that decision is dated. Line keys come from the GRN.
      </p>
      <Feedback error={error} ok={ok} />
      <div className="form-grid">
        <Field id="gb-l-grn" label="GRN id">
          <input
            id="gb-l-grn"
            className="input"
            value={grnId}
            onChange={(e) => setGrnId(e.target.value)}
            data-testid="gb-l-grn"
          />
        </Field>
        <Field id="gb-l-booking-line" label="Booked line">
          <select
            id="gb-l-booking-line"
            className="select"
            value={bookingLine}
            onChange={(e) => setBookingLine(e.target.value)}
            data-testid="gb-l-booking-line"
          >
            <option value="">Choose a line</option>
            {booking.data.lines.items.map((line) => (
              <option key={line.line_key} value={line.line_key}>
                {line.style_code ?? "Line"}
                {line.size_label ? ` · ${line.size_label}` : ""} — {line.outstanding_qty} still to
                come
              </option>
            ))}
          </select>
        </Field>
        <Field id="gb-l-grn-line" label="GRN line key">
          <input
            id="gb-l-grn-line"
            className="input"
            value={grnLine}
            onChange={(e) => setGrnLine(e.target.value)}
            data-testid="gb-l-grn-line"
          />
        </Field>
        <Field id="gb-l-qty" label="Quantity">
          <input
            id="gb-l-qty"
            className="input"
            type="number"
            min={1}
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            data-testid="gb-l-qty"
          />
        </Field>
        <Field id="gb-l-reason" label="Reason">
          <input
            id="gb-l-reason"
            className="input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            data-testid="gb-l-reason"
          />
        </Field>
      </div>
      <button
        className="btn btn-cta btn-sm"
        onClick={submit}
        disabled={busy || !grnId || !bookingLine || !grnLine || !qty}
        data-testid="gb-l-submit"
      >
        Link arrival
      </button>
    </div>
  );
}

/** A store's words: its name, else nothing (never an id). */
function nameOfSite(sites: { id: string; name: string }[], id: string | null | undefined): string {
  if (!id) return "";
  return sites.find((site) => String(site.id) === String(id))?.name ?? "";
}

function lineViews(
  booking: ResourceDTO<BookingProgress>,
  sites: { id: string; name: string }[],
): BookingLineView[] {
  const header: BookingHeader = booking.data.booking_header;
  const headerStore = booking.data.names?.site ?? nameOfSite(sites, header.destination_site_id);
  return booking.data.lines.items.map((line) => ({
    key: line.line_key,
    style: line.style_code ?? "",
    // Colour left the form, but a line booked with one still says so.
    size: [line.size_label, line.colour_label].filter(Boolean).join(" · "),
    description: line.description ?? "",
    store: line.destination_site_id
      ? nameOfSite(sites, line.destination_site_id) || headerStore
      : headerStore,
    qty: line.ordered_qty,
    costPaise: paiseOf(line.cost_paise),
    mrpPaise: paiseOf(line.mrp_paise),
    arrived: line.received_qty - line.reversed_qty,
    toCome: line.outstanding_qty,
  }));
}

/** One goods-v1 booking, drawn as the one Bookings screen draws every booking. */
export function GoodsBookingDetail({ bookingId }: { bookingId: string }) {
  const { session } = useAuth();
  const canManage = managesGoodsBookings(session);
  const doc = useResourceDoc<BookingProgress>(`/goods-v1/bookings/${bookingId}`);
  const mySites = session?.sites ?? [];
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const [showing, setShowing] = useState<"" | "correct" | "link">("");
  /** Bumped when a confirmation is refused, so the open-to-buy panel reads again. */
  const [otbNonce, setOtbNonce] = useState(0);

  const items = doc.doc?.data.lines.items ?? [];
  // The server leaves cost out altogether for a reader whose grant lacks it.
  const showCost = items.length > 0 && items.every((line) => "cost_paise" in line);
  const views = useMemo(() => (doc.doc ? lineViews(doc.doc, mySites) : []), [doc.doc, mySites]);

  async function confirm() {
    if (!doc.doc) return;
    setError("");
    setOk("");
    setBusy(true);
    try {
      await api.post(`/goods-v1/bookings/${doc.doc.id}/request-approval`, {
        reviewed_hash: doc.doc.content_hash,
        ...goodsMeta(doc.doc.revision),
      });
      setOk("Confirmed. The booking has its number and its lines are fixed.");
      doc.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
      setOtbNonce((n) => n + 1);
    } finally {
      setBusy(false);
    }
  }

  async function close(action: "short_close" | "cancel") {
    if (!doc.doc) return;
    setError("");
    setOk("");
    setBusy(true);
    try {
      await api.post(`/goods-v1/bookings/${doc.doc.id}/close`, {
        action,
        reason_code: action === "cancel" ? "CANCELLED" : "SHORT_CLOSED",
        ...goodsMeta(doc.doc.revision),
      });
      setOk(
        action === "cancel"
          ? "Cancelled. Nothing had arrived against it."
          : "Closed early. What was booked and what arrived are both kept.",
      );
      doc.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (doc.denied) return <Denied what="booking" />;
  // Only while there is nothing to show. A refresh after a write keeps the
  // record on screen — tearing the panels down mid-reload would also take the
  // message that says what just happened, and whatever was half-typed with it.
  if (doc.loading && !doc.doc) return <p className="muted">Loading…</p>;
  if (doc.failure) return <div className="warn-note">{doc.failure}</div>;
  if (!doc.doc) return <Denied what="booking" />;

  const booking = doc.doc;
  const header = booking.data.booking_header;
  const names = booking.data.names ?? {};
  const allowed = booking.allowed_actions ?? [];
  const totals = lineTotals(views);
  const arrived = views.reduce((sum, line) => sum + line.arrived, 0);
  const toCome = views.reduce((sum, line) => sum + line.toCome, 0);
  const status = goodsStatus(booking.state, totals.pieces, arrived);
  // Receiving against a booking is Receive Goods' grant, not Booking's, and only
  // a confirmed, open booking that names where it goes has anything to receive
  // (the arrival command refuses anything else).
  const canReceiveHere =
    hold(session, "receive.arrival") &&
    booking.state === "confirmed" &&
    Boolean(header.destination_site_id);

  return (
    <div data-testid="gb-detail">
      <BookingPageHeader
        brand={names.brand ?? ""}
        number={booking.number ?? null}
        status={status}
        facts={[
          names.vendor,
          names.season,
          names.site ? `→ ${names.site}` : "Store not decided yet",
          header.vendor_ref ? `PO ${header.vendor_ref}` : null,
          header.expected_date ? `Expected ${header.expected_date}` : null,
          header.commercial_label,
        ]}
      />
      <Feedback error={error} ok={ok} />

      {booking.state !== "draft" && (
        <p className="lead" data-testid="gb-received">
          {receivedOfBooked(booking.number, arrived, totals.pieces)}
        </p>
      )}

      {canReceiveHere && (
        <div className="bk-actions" data-testid="gb-receive-actions">
          {/* The same arrival step Goods arrived opens, with this booking
              already chosen (OPS-17, PRD §5.1). */}
          <Link
            className="btn btn-cta"
            to={`/goods/receive/new?booking=${encodeURIComponent(booking.id)}`}
            data-testid="gb-receive"
          >
            <PackagePlus size={15} /> Receive against this booking
          </Link>
        </div>
      )}

      <BookingTiles
        booked={totals.pieces}
        arrived={arrived}
        toCome={toCome}
        costPaise={showCost ? totals.costPaise : undefined}
        mrpPaise={totals.mrpPaise}
        toComeTestId="gb-outstanding-total"
      />

      {canManage && booking.state === "draft" && booking.field_access?.writable_fields.includes("cost") && (
        <OpenToBuyPanel
          bookingId={booking.id}
          revision={booking.revision}
          reviewedHash={booking.content_hash}
          nonce={otbNonce}
        />
      )}

      {canManage && allowed.length > 0 && (
        <div className="bk-actions" data-testid="gb-actions">
          {allowed.includes("bookings.request_approval") && (
            <button
              className="btn btn-cta"
              onClick={confirm}
              disabled={busy}
              data-testid="gb-confirm"
            >
              <Send size={15} /> Confirm booking
            </button>
          )}
          {allowed.includes("bookings.corrections") && (
            <button
              className="btn"
              onClick={() => setShowing(showing === "correct" ? "" : "correct")}
              data-testid="gb-correct"
            >
              Correct
            </button>
          )}
          {allowed.includes("bookings.receipt_links") && (
            <button
              className="btn"
              onClick={() => setShowing(showing === "link" ? "" : "link")}
              data-testid="gb-link"
            >
              <Link2 size={15} /> Link arrival
            </button>
          )}
          {allowed.includes("bookings.close") && (
            <>
              <button
                className="btn"
                onClick={() => close("short_close")}
                disabled={busy}
                data-testid="gb-short-close"
              >
                Close early
              </button>
              <button
                className="btn"
                onClick={() => close("cancel")}
                disabled={busy}
                data-testid="gb-cancel"
              >
                Cancel
              </button>
            </>
          )}
        </div>
      )}

      {/* Left open after a success, so the message that says what happened
          stays with the form that caused it. */}
      {showing === "correct" && (
        <CorrectionForm booking={booking} sites={mySites} showCost={showCost} onDone={doc.reload} />
      )}
      {showing === "link" && <ReceiptLinkForm booking={booking} onDone={doc.reload} />}

      <BookingLinesTable lines={views} showCost={showCost} testId="gb-lines" />
      <p className="gr-hint">
        Still to come is what was booked, less what has arrived, plus anything a counter-GRN put
        back on order.
      </p>
    </div>
  );
}
