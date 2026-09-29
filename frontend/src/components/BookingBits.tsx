// The pieces the one Bookings screen draws the same way for both booking
// engines: the status chip, the list card, the booking page's header, tiles and
// lines table, and the form's totals. Each takes plain, already-read facts, so
// neither engine's page leaks its wire shape into the other (and neither reads
// the other's API through here).
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "./PageHeader";
import {
  arrivedPercent,
  STATUS_TONE,
  STATUS_WORD,
  type BookingCard,
  type BookingStatus,
} from "../lib/bookingModel";
import { formatINR } from "../lib/format";

/** Paise as rupees, or a dash when nobody gave the figure (never ₹0). */
export function moneyOrDash(paise: number | null | undefined): string {
  return paise === null || paise === undefined ? "—" : formatINR(paise);
}

export function BookingStatusChip({ status }: { status: BookingStatus }) {
  return (
    <span className={`chip chip-${STATUS_TONE[status]} status-pill`} data-testid="booking-status">
      {STATUS_WORD[status]}
    </span>
  );
}

/** One booking in the list, whichever engine holds it. */
export function BookingCardLink({ card }: { card: BookingCard }) {
  const pct = arrivedPercent(card.booked, card.arrived);
  const meta = [card.vendor, card.season, card.store ? `→ ${card.store}` : ""].filter(Boolean);
  return (
    <Link
      to={`/booking/${card.id}`}
      className="card bk-card"
      data-testid={`booking-card-${card.id}`}
      data-engine={card.engine}
    >
      <div className="bk-card-top">
        <span className="bk-num">{card.number ?? "No number yet"}</span>
        <BookingStatusChip status={card.status} />
      </div>
      <div className="bk-brand">{card.brand}</div>
      {meta.length > 0 && <div className="bk-meta">{meta.join(" · ")}</div>}
      <div className="bk-prog">
        <div className="bk-prog-bar">
          <div className="bk-prog-fill" style={{ width: `${pct}%` }} />
        </div>
        <div className="bk-prog-meta">
          <span data-testid="booking-card-progress">
            {card.arrived} of {card.booked} received
          </span>
          <span>{card.expected ? `Expected ${card.expected}` : `${pct}%`}</span>
        </div>
      </div>
    </Link>
  );
}

/** The booking page's header: brand as the title, then number, vendor, season
 *  and store. Whatever the booking does not name is left out, not "Not given". */
export function BookingPageHeader({
  brand,
  number,
  status,
  facts,
  actions,
}: {
  brand: string;
  number: string | null;
  status: BookingStatus;
  facts: (string | null | undefined)[];
  actions?: ReactNode;
}) {
  const named = facts.filter((fact): fact is string => Boolean(fact));
  return (
    <PageHeader
      title={brand || "Booking"}
      lead={
        <>
          <span className="bk-num" data-testid="booking-number">
            {number ?? "No number yet"}
          </span>
          {named.length > 0 && ` · ${named.join(" · ")}`}
        </>
      }
      actions={
        <>
          <BookingStatusChip status={status} />
          {actions}
        </>
      }
    />
  );
}

function Tile({ label, value, testId }: { label: string; value: ReactNode; testId?: string }) {
  return (
    <div className="card bk-tile">
      <span className="bk-tile-label">{label}</span>
      <strong className="bk-tile-value tabular" data-testid={testId}>
        {value}
      </strong>
    </div>
  );
}

/** Booked · Arrived · Still to come · Total cost · MRP value. Total cost is
 *  drawn only for someone allowed to see cost (`costPaise` undefined hides it). */
export function BookingTiles({
  booked,
  arrived,
  toCome,
  costPaise,
  mrpPaise,
  toComeTestId,
}: {
  booked: number;
  arrived: number;
  toCome: number;
  costPaise?: number | null | undefined;
  mrpPaise: number | null;
  toComeTestId?: string;
}) {
  return (
    <div className="bk-tiles" data-testid="booking-tiles">
      <Tile label="Booked" value={`${booked} pcs`} />
      <Tile label="Arrived" value={`${arrived} pcs`} />
      <Tile
        label="Still to come"
        value={`${toCome} pcs`}
        {...(toComeTestId ? { testId: toComeTestId } : {})}
      />
      {costPaise !== undefined && (
        <Tile label="Total cost" value={moneyOrDash(costPaise)} testId="booking-total-cost" />
      )}
      <Tile label="MRP value" value={moneyOrDash(mrpPaise)} testId="booking-mrp-value" />
    </div>
  );
}

/** One line on the booking page, in words (names, not codes). */
export interface BookingLineView {
  key: string;
  style: string;
  size: string;
  description: string;
  /** Where this line's goods go, when the lines do not all go to one store. */
  store?: string;
  qty: number;
  costPaise: number | null;
  mrpPaise: number | null;
  arrived: number;
  toCome: number;
}

export function BookingLinesTable({
  lines,
  showCost,
  testId,
}: {
  lines: BookingLineView[];
  showCost: boolean;
  testId: string;
}) {
  const stores = new Set(lines.map((line) => line.store ?? ""));
  const showStore = stores.size > 1;
  return (
    <div className="table-wrap">
      <table className="data" data-testid={testId}>
        <caption className="sr-only">
          Each booked line, with how much has arrived and how much is still to come.
        </caption>
        <thead>
          <tr>
            <th>Style</th>
            <th>Size</th>
            <th>Description</th>
            {showStore && <th>Store</th>}
            <th className="num">Qty</th>
            {showCost && <th className="num">Cost/pc</th>}
            <th className="num">MRP</th>
            <th className="num">Arrived</th>
            <th className="num">Still to come</th>
          </tr>
        </thead>
        <tbody>
          {lines.map((line) => (
            <tr key={line.key} data-testid={`booking-line-${line.key}`}>
              <td>
                <b className="mono">{line.style}</b>
              </td>
              <td>{line.size}</td>
              <td>{line.description}</td>
              {showStore && <td>{line.store}</td>}
              <td className="num">{line.qty}</td>
              {showCost && <td className="num">{moneyOrDash(line.costPaise)}</td>}
              <td className="num">{moneyOrDash(line.mrpPaise)}</td>
              <td className="num">{line.arrived}</td>
              <td className="num">
                <b>{line.toCome}</b>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The form's bottom line: worked out from the lines, never typed. */
export function BookingFormTotals({
  pieces,
  costPaise,
  mrpPaise,
  showCost,
}: {
  pieces: number;
  costPaise: number | null;
  mrpPaise: number | null;
  showCost: boolean;
}) {
  return (
    <div className="bk-totals" data-testid="booking-form-totals">
      <span>
        Total pieces <strong data-testid="booking-total-pieces">{pieces}</strong>
      </span>
      {showCost && (
        <span>
          Total cost <strong data-testid="booking-total-cost">{moneyOrDash(costPaise)}</strong>
        </span>
      )}
      <span>
        MRP value <strong data-testid="booking-total-mrp">{moneyOrDash(mrpPaise)}</strong>
      </span>
    </div>
  );
}
