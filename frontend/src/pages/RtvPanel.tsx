// Return to vendor (goods ticket 15B) — the RTV side of a movement's record on the
// Movements screen: where it stands, what is still waiting, why pieces were left
// behind, every pickup and withdrawal, and the two physical steps.
//
// Four rules the panel has to make plain rather than merely obey:
//   * approving an RTV reserved the pieces and moved nothing. Until the vendor
//     collects them they stay where they are, in their condition, held;
//   * a pickup is one confirmed handover. Only what was collected leaves, and
//     every piece left behind needs a reason (quarantine outcomes PRD §5);
//   * a reason is not a withdrawal. Rejected pieces stay reserved to the RTV
//     until somebody explicitly withdraws them — the panel says how many;
//   * a withdrawal releases only the reservation. Quarantined goods stay held
//     and unavailable, and every pickup keeps its evidence.
//
// Goods ticket 15F adds shipments for delivery to the vendor (`ShipmentsPanel`):
//   * sending goods is not the vendor receiving them. A shipment stays open,
//     and the return initiated, until the vendor confirms receipt or the goods
//     come back — even when nothing is left to send;
//   * the vendor's confirmation is a snapshot per line. Fewer pieces than were
//     sent is a difference somebody owns; nothing comes back into stock and
//     nothing is written off because of it;
//   * goods that actually come back are received: held goods go back on hold,
//     damage found is reported, good pieces are put away;
//   * the e-way bill is paperwork, kept apart: completing a shipment never
//     settles it.
//
// Goods ticket 15H (goods PRD §14.10 GSA-R07) closes a difference that persists
// (`ClosureForm`, `ClosureDecision`):
//   * only the Owner closes it. The site prepares the closure with a reason and
//     evidence (the vendor's letter, a photo or a note); a different person, the
//     Owner, approves or turns it down. Its follow-up date is a reminder only;
//   * approving recognises exactly the pieces the vendor never confirmed as a
//     shortfall, at their recorded cost. No credit note, payable or accounting
//     entry is made, and the return closes as partially returned — never
//     "completed".
import { useState } from "react";
import { PackageCheck, Scale, Truck, Undo2 } from "lucide-react";

import { api, goodsMeta } from "../lib/api";
import { Field, useStepUp, type ResourceDTO } from "../lib/goodsScreen";
import {
  FURTHER_PICKUP,
  SHIPMENT_STATUS_LABEL,
  acknowledgeable,
  ewayWords,
  LEFT_BEHIND_LABEL,
  LEFT_BEHIND_REASONS,
  WITHDRAWAL_LABEL,
  WITHDRAWAL_REASONS,
  leftBehindGap,
  rtvEventWords,
  rtvStateWords,
  type LeftBehindReason,
  type MovementData,
  type RtvDetail,
  type RtvAcknowledgementInput,
  type RtvClosureInput,
  type RtvPendingClosure,
  type RtvEwayInput,
  type RtvPickupInput,
  type RtvPutawayInput,
  type RtvShipment,
  type RtvShipmentInput,
  type RtvSourceReturnInput,
  type RtvWithdrawalInput,
  type WithdrawalReason,
} from "../lib/goodsMovements";
import { formatDateTime, formatPaiseString } from "../lib/format";

const MOVEMENTS = "/goods-v1/outbound/movements";
const APPROVALS = "/goods-v1/approvals";

interface ReasonRow {
  id: string;
  line_key: string;
  reason: LeftBehindReason;
  qty: number;
  remark: string;
  further: boolean;
}

/** An ordinary storage location returned goods can be put away in. */
export interface PutawayLocation {
  id: string;
  name: string;
}

export function RtvPanel({
  doc,
  lineName,
  putawayLocations = [],
  onDone,
  onError,
}: {
  doc: ResourceDTO<MovementData>;
  /** Words for an RTV line — its SKU or description — from the movement's own lines. */
  lineName: (lineKey: string) => string;
  /** The site's ordinary storage locations, for putting returned goods away. */
  putawayLocations?: PutawayLocation[];
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const rtv = doc.data.rtv;
  if (!rtv) return null;
  return (
    <div data-testid="rtv-panel">
      <dl className="gr-facts">
        <div>
          <dt>Return status</dt>
          <dd data-testid="rtv-state" data-state={rtv.state ?? doc.state}>
            {rtvStateWords(rtv.state, doc.state)}
          </dd>
        </div>
        <div>
          <dt>Vendor</dt>
          <dd data-testid="rtv-vendor">{rtv.vendor ? `${rtv.vendor.name} (${rtv.vendor.code})` : "—"}</dd>
        </div>
        <div>
          <dt>Vendor&apos;s agreement</dt>
          <dd data-testid="rtv-agreement">{rtv.agreement_reference ?? "—"}</dd>
        </div>
        <div>
          <dt>Approved</dt>
          <dd data-testid="rtv-approved">{rtv.approved_qty}</dd>
        </div>
        <div>
          <dt>Collected by the vendor</dt>
          <dd data-testid="rtv-picked">{rtv.picked_up_qty}</dd>
        </div>
        <div>
          <dt>Withdrawn</dt>
          <dd data-testid="rtv-withdrawn">{rtv.withdrawn_qty}</dd>
        </div>
        <div>
          <dt>Still waiting (reserved)</dt>
          <dd data-testid="rtv-outstanding">{rtv.outstanding_qty}</dd>
        </div>
        {(rtv.shipped_qty ?? 0) > 0 && (
          <>
            <div>
              <dt>Sent to the vendor</dt>
              <dd data-testid="rtv-shipped">{rtv.shipped_qty}</dd>
            </div>
            <div>
              <dt>Confirmed by the vendor</dt>
              <dd data-testid="rtv-acknowledged">{rtv.acknowledged_qty}</dd>
            </div>
            <div>
              <dt>Came back</dt>
              <dd data-testid="rtv-returned-to-source">{rtv.returned_to_source_qty}</dd>
            </div>
            <div>
              <dt>Not yet confirmed or back</dt>
              <dd data-testid="rtv-awaiting-receipt">{rtv.awaiting_receipt_qty}</dd>
            </div>
          </>
        )}
      </dl>

      {(rtv.awaiting_withdrawal_qty ?? 0) > 0 && (
        <p className="warn-note" data-testid="rtv-awaiting-withdrawal">
          {rtv.awaiting_withdrawal_qty} piece(s) will not be collected under this return, but
          they stay reserved to it until somebody withdraws them.
        </p>
      )}

      {rtv.state && (rtv.lines?.length ?? 0) > 0 && (
        <table data-testid="rtv-lines">
          <thead>
            <tr>
              <th>Line</th>
              <th>From</th>
              <th className="num">Approved</th>
              <th className="num">Collected</th>
              <th className="num">Sent</th>
              <th className="num">Withdrawn</th>
              <th className="num">Still waiting</th>
            </tr>
          </thead>
          <tbody>
            {rtv.lines?.map((line) => (
              <tr key={line.line_key} data-testid="rtv-line">
                <td>{lineName(line.line_key ?? "")}</td>
                <td>{line.source_pool === "quarantine" ? "Quarantine" : "Stock"}</td>
                <td className="num">{line.approved_qty}</td>
                <td className="num">{line.picked_up_qty}</td>
                <td className="num">{line.shipped_qty}</td>
                <td className="num">{line.withdrawn_qty}</td>
                <td className="num" data-testid="rtv-line-outstanding">
                  {line.outstanding_qty}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {(rtv.left_behind?.length ?? 0) > 0 && (
        <div data-testid="rtv-left-behind">
          <h4 className="h4">Left behind at the last pickup</h4>
          <ul className="stock-reasons">
            {rtv.left_behind?.map((row, index) => (
              <li key={index} data-testid="rtv-left-behind-row">
                {row.qty} × {LEFT_BEHIND_LABEL[row.reason]}
                {row.remark ? ` — ${row.remark}` : ""}
                {row.further_pickup_expected ? " · another pickup expected" : " · no further pickup"}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(rtv.events?.length ?? 0) > 0 && (
        <div data-testid="rtv-events">
          <h4 className="h4">Pickups, shipments and withdrawals</h4>
          <ul className="stock-reasons">
            {rtv.events?.map((event) => (
              <li key={event.id} data-testid="rtv-event" data-kind={event.kind}>
                {event.event_at ? formatDateTime(event.event_at) : ""} · {rtvEventWords(event)}
                {event.recorded_at ? ` · recorded ${formatDateTime(event.recorded_at)}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Keyed by how many events the RTV has, so a recorded pickup or withdrawal
          starts the next one from a clean form rather than the last one's figures. */}
      {doc.allowed_actions.includes("pickup") && (
        <PickupForm
          key={`pickup-${rtv.events?.length ?? 0}`}
          doc={doc}
          rtv={rtv}
          lineName={lineName}
          onDone={onDone}
          onError={onError}
        />
      )}
      {doc.allowed_actions.includes("ship") && (
        <ShipForm
          key={`ship-${rtv.events?.length ?? 0}`}
          doc={doc}
          rtv={rtv}
          lineName={lineName}
          onDone={onDone}
          onError={onError}
        />
      )}
      {doc.allowed_actions.includes("withdraw") && (
        <WithdrawForm
          key={`withdraw-${rtv.events?.length ?? 0}`}
          doc={doc}
          rtv={rtv}
          lineName={lineName}
          onDone={onDone}
          onError={onError}
        />
      )}
      {(rtv.shipments?.length ?? 0) > 0 && (
        <div data-testid="rtv-shipments">
          <h4 className="h4">Shipments to the vendor</h4>
          {rtv.shipments?.map((shipment) => (
            <ShipmentCard
              key={shipment.id}
              doc={doc}
              shipment={shipment}
              lineName={lineName}
              putawayLocations={putawayLocations}
              onDone={onDone}
              onError={onError}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ShipForm({
  doc,
  rtv,
  lineName,
  onDone,
  onError,
}: {
  doc: ResourceDTO<MovementData>;
  rtv: RtvDetail;
  lineName: (lineKey: string) => string;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const waiting = (rtv.lines ?? []).filter((line) => (line.outstanding_qty ?? 0) > 0);
  const [sent, setSent] = useState<Record<string, number>>({});
  const [carrier, setCarrier] = useState("");
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [eway, setEway] = useState("");
  const [shippedAt, setShippedAt] = useState("");
  const [busy, setBusy] = useState(false);
  const total = Object.values(sent).reduce((sum, qty) => sum + qty, 0);
  const ready = total > 0 && carrier.trim() !== "" && (reference.trim() !== "" || note.trim() !== "");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const body: RtvShipmentInput = {
        carrier: carrier.trim(),
        ...(reference.trim() ? { evidence_reference: reference.trim() } : {}),
        ...(note.trim() ? { evidence_note: note.trim() } : {}),
        ...(eway.trim() ? { eway_reference: eway.trim() } : {}),
        ...(shippedAt ? { shipped_at: new Date(shippedAt).toISOString() } : {}),
        lines: Object.entries(sent)
          .filter(([, qty]) => qty > 0)
          .map(([line_key, qty]) => ({ line_key, qty })),
      };
      await api.post(`${MOVEMENTS}/${doc.id}/rtv-shipments`, { ...body, ...goodsMeta() });
      onDone(`Shipment recorded: ${total} piece(s) sent to the vendor, waiting for their receipt.`);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card section-card form-grid" onSubmit={submit} data-testid="rtv-ship-form">
      <h4 className="h4">
        <Truck size={16} /> Send goods to the vendor
      </h4>
      <p className="lead">
        Record only what actually left. Sending is not the vendor receiving: this return stays open
        until the vendor confirms what arrived, or the goods come back. What you do not send stays
        reserved to this return.
      </p>
      {waiting.map((line) => (
        <Field
          key={line.line_key}
          id={`rtv-send-${line.line_key}`}
          label={`Sent — ${lineName(line.line_key ?? "")} (up to ${line.outstanding_qty})`}
        >
          <input
            id={`rtv-send-${line.line_key}`}
            className="input"
            type="number"
            min={0}
            max={line.outstanding_qty}
            value={sent[line.line_key ?? ""] ?? 0}
            onChange={(e) => setSent({ ...sent, [line.line_key ?? ""]: Number(e.target.value) })}
            data-testid="rtv-send"
          />
        </Field>
      ))}
      <Field id="rtv-carrier" label="Transporter or courier">
        <input
          id="rtv-carrier"
          className="input"
          value={carrier}
          maxLength={120}
          onChange={(e) => setCarrier(e.target.value)}
          data-testid="rtv-carrier"
        />
      </Field>
      <Field
        id="rtv-ship-reference"
        label="Challan or docket number"
        hint="Evidence of the dispatch: a reference, a note, or both."
      >
        <input
          id="rtv-ship-reference"
          className="input"
          value={reference}
          maxLength={100}
          onChange={(e) => setReference(e.target.value)}
          aria-describedby="rtv-ship-reference-hint"
          data-testid="rtv-ship-reference"
        />
      </Field>
      <Field id="rtv-ship-note" label="Dispatch note (optional)">
        <textarea
          id="rtv-ship-note"
          className="input"
          value={note}
          maxLength={1000}
          onChange={(e) => setNote(e.target.value)}
          data-testid="rtv-ship-note"
        />
      </Field>
      <Field
        id="rtv-ship-eway"
        label="E-way bill number (optional)"
        hint="Without one the shipment is still recorded, and the missing e-way bill becomes work to follow up."
      >
        <input
          id="rtv-ship-eway"
          className="input"
          value={eway}
          maxLength={100}
          onChange={(e) => setEway(e.target.value)}
          aria-describedby="rtv-ship-eway-hint"
          data-testid="rtv-ship-eway"
        />
      </Field>
      <Field id="rtv-shipped-at" label="When it left (leave empty for now)">
        <input
          id="rtv-shipped-at"
          className="input"
          type="datetime-local"
          value={shippedAt}
          onChange={(e) => setShippedAt(e.target.value)}
          data-testid="rtv-shipped-at"
        />
      </Field>
      <button className="btn btn-cta" disabled={busy || !ready} data-testid="rtv-ship-submit">
        Record the shipment
      </button>
    </form>
  );
}

function ShipmentCard({
  doc,
  shipment,
  lineName,
  putawayLocations,
  onDone,
  onError,
}: {
  doc: ResourceDTO<MovementData>;
  shipment: RtvShipment;
  lineName: (lineKey: string) => string;
  putawayLocations: PutawayLocation[];
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const actions = shipment.allowed_actions ?? [];
  const path = `${MOVEMENTS}/${doc.id}/rtv-shipments/${shipment.id}`;
  // Keyed by how much has been recorded against the shipment, so each form
  // starts clean after a step rather than keeping the last one's figures.
  const step = `${shipment.acknowledgements?.length ?? 0}-${shipment.returns?.length ?? 0}-${
    shipment.putaways?.length ?? 0
  }-${shipment.pending_closure?.approval_request_id ?? ""}`;
  return (
    <div
      className="card section-card"
      data-testid="rtv-shipment"
      data-status={shipment.status}
      data-sequence={shipment.sequence_no}
    >
      <p>
        <strong>Shipment {shipment.sequence_no}</strong> ·{" "}
        {shipment.shipped_at ? formatDateTime(shipment.shipped_at) : ""} · {shipment.carrier ?? ""}
        {shipment.evidence_reference ? ` (${shipment.evidence_reference})` : ""}
      </p>
      <p data-testid="rtv-shipment-status">
        {shipment.status ? SHIPMENT_STATUS_LABEL[shipment.status] : ""}
      </p>
      <dl className="gr-facts">
        <div>
          <dt>Sent</dt>
          <dd data-testid="rtv-shipment-shipped">{shipment.shipped_qty}</dd>
        </div>
        <div>
          <dt>Confirmed by the vendor</dt>
          <dd data-testid="rtv-shipment-acknowledged">
            {shipment.acknowledged_qty ?? "not yet"}
          </dd>
        </div>
        <div>
          <dt>Came back</dt>
          <dd data-testid="rtv-shipment-returned">{shipment.returned_qty}</dd>
        </div>
        {(shipment.closed_qty ?? 0) > 0 && (
          <div>
            <dt>Closed as a shortfall</dt>
            <dd data-testid="rtv-shipment-closed">{shipment.closed_qty}</dd>
          </div>
        )}
        <div>
          <dt>Not confirmed or back</dt>
          <dd data-testid="rtv-shipment-unaccounted">{shipment.unaccounted_qty}</dd>
        </div>
        <div>
          <dt>E-way bill</dt>
          <dd data-testid="rtv-shipment-eway">{ewayWords(shipment.eway)}</dd>
        </div>
      </dl>
      {shipment.status === "short_acknowledged" && (
        <p className="warn-note" data-testid="rtv-shipment-difference">
          The vendor confirmed {shipment.acknowledged_qty} of the {shipment.shipped_qty} piece(s)
          sent. The {shipment.unaccounted_qty} missing piece(s) are a difference only the Owner
          closes; nothing has come back into stock and nothing is written off until then. Its
          follow-up date is a reminder — it never closes the difference by itself.
        </p>
      )}
      {shipment.pending_closure && (
        <ClosureDecision
          key={`decide-${step}`}
          pending={shipment.pending_closure}
          canDecide={actions.includes("decide_closure")}
          lineName={lineName}
          onDone={onDone}
          onError={onError}
        />
      )}
      {(shipment.awaiting_putaway_qty ?? 0) > 0 && (
        <p className="muted" data-testid="rtv-shipment-awaiting-putaway">
          {shipment.awaiting_putaway_qty} returned good piece(s) wait in receiving to be put away.
        </p>
      )}
      {((shipment.acknowledgements?.length ?? 0) > 0 ||
        (shipment.returns?.length ?? 0) > 0 ||
        (shipment.closures?.length ?? 0) > 0) && (
        <ul className="stock-reasons" data-testid="rtv-shipment-facts">
          {shipment.acknowledgements?.map((ack) => (
            <li key={ack.id} data-testid="rtv-shipment-ack">
              {ack.acknowledged_at ? formatDateTime(ack.acknowledged_at) : ""} · vendor confirmed{" "}
              {ack.quantity}
              {ack.recipient_reference ? ` (${ack.recipient_reference})` : ""}
              {ack.shortfall_qty ? ` · ${ack.shortfall_qty} short` : ""} · recorded by{" "}
              {ack.recorded_by?.name || "someone"}
            </li>
          ))}
          {shipment.returns?.map((back) => (
            <li key={back.id} data-testid="rtv-shipment-return">
              {back.returned_at ? formatDateTime(back.returned_at) : ""} · {back.quantity} came back
              {back.reason ? `: ${back.reason}` : ""}
              {back.damage_report_id ? " · damage reported" : ""} · recorded by{" "}
              {back.recorded_by?.name || "someone"}
            </li>
          ))}
          {shipment.closures?.map((closure) => (
            <li key={closure.id} data-testid="rtv-shipment-closure">
              {closure.closed_at ? formatDateTime(closure.closed_at) : ""} · {closure.quantity}{" "}
              closed as a shortfall
              {closure.reason ? `: ${closure.reason}` : ""}
              {closure.evidence_reference ? ` (${closure.evidence_reference})` : ""}
              {closure.value_paise !== undefined ? ` · ${valueWords(closure.value_paise)}` : ""} ·
              prepared by {closure.prepared_by?.name || "someone"}, approved by{" "}
              {closure.approved_by?.name || "someone"}
            </li>
          ))}
        </ul>
      )}
      {actions.includes("acknowledge") && (
        <AcknowledgeForm
          key={`ack-${step}`}
          path={path}
          shipment={shipment}
          lineName={lineName}
          onDone={onDone}
          onError={onError}
        />
      )}
      {actions.includes("return_to_source") && (
        <SourceReturnForm
          key={`return-${step}`}
          path={path}
          shipment={shipment}
          lineName={lineName}
          onDone={onDone}
          onError={onError}
        />
      )}
      {actions.includes("putaway_returned") && (
        <PutawayForm
          key={`putaway-${step}`}
          path={path}
          shipment={shipment}
          lineName={lineName}
          locations={putawayLocations}
          onDone={onDone}
          onError={onError}
        />
      )}
      {actions.includes("propose_closure") && !shipment.pending_closure && (
        <ClosureForm
          key={`close-${step}`}
          path={path}
          shipment={shipment}
          lineName={lineName}
          onDone={onDone}
          onError={onError}
        />
      )}
      {(actions.includes("eway_attach") || actions.includes("eway_verify")) && (
        <EwayForm
          key={`eway-${shipment.eway?.reference ?? ""}-${shipment.eway?.verified ? 1 : 0}`}
          path={path}
          shipment={shipment}
          canAttach={actions.includes("eway_attach")}
          canVerify={actions.includes("eway_verify")}
          onDone={onDone}
          onError={onError}
        />
      )}
    </div>
  );
}

interface ShipmentFormProps {
  path: string;
  shipment: RtvShipment;
  lineName: (lineKey: string) => string;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}

function AcknowledgeForm({ path, shipment, lineName, onDone, onError }: ShipmentFormProps) {
  const lines = shipment.lines ?? [];
  const [qtys, setQtys] = useState<Record<string, number>>(
    Object.fromEntries(lines.map((line) => [line.line_key ?? "", acknowledgeable(line)])),
  );
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const ready = reference.trim() !== "" || note.trim() !== "";
  const total = Object.values(qtys).reduce((sum, qty) => sum + qty, 0);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const body: RtvAcknowledgementInput = {
        reviewed_hash: shipment.state_hash ?? "",
        ...(reference.trim() ? { recipient_reference: reference.trim() } : {}),
        ...(note.trim() ? { evidence_note: note.trim() } : {}),
        lines: lines.map((line) => ({
          line_key: line.line_key ?? "",
          qty: qtys[line.line_key ?? ""] ?? 0,
        })),
      };
      await api.post(`${path}/acknowledgements`, { ...body, ...goodsMeta() });
      onDone(`Vendor confirmation recorded: ${total} piece(s) received.`);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form-grid" onSubmit={submit} data-testid="rtv-ack-form">
      <h4 className="h4">
        <PackageCheck size={16} /> Record what the vendor confirmed receiving
      </h4>
      <p className="lead">
        Enter what the vendor&apos;s receipt says for every line, even 0. A new confirmation
        replaces the last one; it is not added to it.
      </p>
      {lines.map((line) => (
        <Field
          key={line.line_key}
          id={`rtv-ack-${shipment.id}-${line.line_key}`}
          label={`Received — ${lineName(line.line_key ?? "")} (of ${acknowledgeable(line)})`}
        >
          <input
            id={`rtv-ack-${shipment.id}-${line.line_key}`}
            className="input"
            type="number"
            min={0}
            max={acknowledgeable(line)}
            value={qtys[line.line_key ?? ""] ?? 0}
            onChange={(e) => setQtys({ ...qtys, [line.line_key ?? ""]: Number(e.target.value) })}
            data-testid="rtv-ack-qty"
          />
        </Field>
      ))}
      <Field id={`rtv-ack-ref-${shipment.id}`} label="Vendor's receipt or reference">
        <input
          id={`rtv-ack-ref-${shipment.id}`}
          className="input"
          value={reference}
          maxLength={100}
          onChange={(e) => setReference(e.target.value)}
          data-testid="rtv-ack-reference"
        />
      </Field>
      <Field id={`rtv-ack-note-${shipment.id}`} label="Note (optional)">
        <input
          id={`rtv-ack-note-${shipment.id}`}
          className="input"
          value={note}
          maxLength={1000}
          onChange={(e) => setNote(e.target.value)}
          data-testid="rtv-ack-note"
        />
      </Field>
      <button className="btn btn-sm" disabled={busy || !ready} data-testid="rtv-ack-submit">
        Record the confirmation
      </button>
    </form>
  );
}

function SourceReturnForm({ path, shipment, lineName, onDone, onError }: ShipmentFormProps) {
  const lines = (shipment.lines ?? []).filter((line) => (line.unaccounted_qty ?? 0) > 0);
  const [good, setGood] = useState<Record<string, number>>({});
  const [damaged, setDamaged] = useState<Record<string, number>>({});
  const [reason, setReason] = useState("");
  const [reference, setReference] = useState("");
  const [busy, setBusy] = useState(false);
  const total =
    Object.values(good).reduce((sum, qty) => sum + qty, 0) +
    Object.values(damaged).reduce((sum, qty) => sum + qty, 0);
  const ready = total > 0 && reason.trim() !== "" && reference.trim() !== "";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const body: RtvSourceReturnInput = {
        reason: reason.trim(),
        evidence_reference: reference.trim(),
        lines: lines
          .map((line) => ({
            line_key: line.line_key ?? "",
            good: good[line.line_key ?? ""] ?? 0,
            damaged: damaged[line.line_key ?? ""] ?? 0,
          }))
          .filter((line) => line.good + line.damaged > 0),
      };
      await api.post(`${path}/returns`, { ...body, ...goodsMeta() });
      onDone(`Recorded ${total} piece(s) back at the source. The shipment's departure is kept.`);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form-grid" onSubmit={submit} data-testid="rtv-return-form">
      <h4 className="h4">
        <Undo2 size={16} /> Goods came back (delivery failed)
      </h4>
      <p className="lead">
        Record only what is physically back. Goods that left on hold go back on hold; pieces found
        damaged are reported; good pieces wait in receiving until you put them away.
      </p>
      {lines.map((line) => (
        <div key={line.line_key} className="toolbar">
          <Field
            id={`rtv-back-good-${shipment.id}-${line.line_key}`}
            label={`Back as sent — ${lineName(line.line_key ?? "")} (up to ${line.unaccounted_qty})`}
          >
            <input
              id={`rtv-back-good-${shipment.id}-${line.line_key}`}
              className="input"
              type="number"
              min={0}
              max={line.unaccounted_qty}
              value={good[line.line_key ?? ""] ?? 0}
              onChange={(e) => setGood({ ...good, [line.line_key ?? ""]: Number(e.target.value) })}
              data-testid="rtv-back-good"
            />
          </Field>
          <Field
            id={`rtv-back-damaged-${shipment.id}-${line.line_key}`}
            label="Found damaged"
          >
            <input
              id={`rtv-back-damaged-${shipment.id}-${line.line_key}`}
              className="input"
              type="number"
              min={0}
              max={line.unaccounted_qty}
              value={damaged[line.line_key ?? ""] ?? 0}
              onChange={(e) =>
                setDamaged({ ...damaged, [line.line_key ?? ""]: Number(e.target.value) })
              }
              data-testid="rtv-back-damaged"
            />
          </Field>
        </div>
      ))}
      <Field id={`rtv-back-reason-${shipment.id}`} label="Why the delivery failed">
        <input
          id={`rtv-back-reason-${shipment.id}`}
          className="input"
          value={reason}
          maxLength={500}
          onChange={(e) => setReason(e.target.value)}
          data-testid="rtv-back-reason"
        />
      </Field>
      <Field id={`rtv-back-ref-${shipment.id}`} label="Return docket or reference">
        <input
          id={`rtv-back-ref-${shipment.id}`}
          className="input"
          value={reference}
          maxLength={100}
          onChange={(e) => setReference(e.target.value)}
          data-testid="rtv-back-reference"
        />
      </Field>
      <button className="btn btn-sm" disabled={busy || !ready} data-testid="rtv-back-submit">
        Record the return
      </button>
    </form>
  );
}

function PutawayForm({
  path,
  shipment,
  lineName,
  locations,
  onDone,
  onError,
}: ShipmentFormProps & { locations: PutawayLocation[] }) {
  const lines = (shipment.lines ?? []).filter((line) => (line.awaiting_putaway_qty ?? 0) > 0);
  const [qtys, setQtys] = useState<Record<string, number>>(
    Object.fromEntries(lines.map((line) => [line.line_key ?? "", line.awaiting_putaway_qty ?? 0])),
  );
  const [location, setLocation] = useState(locations[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const total = Object.values(qtys).reduce((sum, qty) => sum + qty, 0);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const body: RtvPutawayInput = {
        lines: Object.entries(qtys)
          .filter(([, qty]) => qty > 0)
          .map(([line_key, qty]) => ({ line_key, qty, destination_location_id: location })),
      };
      await api.post(`${path}/putaways`, { ...body, ...goodsMeta() });
      onDone(`Put away ${total} returned piece(s). They are usable stock again.`);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form-grid" onSubmit={submit} data-testid="rtv-putaway-form">
      <h4 className="h4">Put returned goods away</h4>
      {lines.map((line) => (
        <Field
          key={line.line_key}
          id={`rtv-putaway-${shipment.id}-${line.line_key}`}
          label={`Put away — ${lineName(line.line_key ?? "")} (up to ${line.awaiting_putaway_qty})`}
        >
          <input
            id={`rtv-putaway-${shipment.id}-${line.line_key}`}
            className="input"
            type="number"
            min={0}
            max={line.awaiting_putaway_qty}
            value={qtys[line.line_key ?? ""] ?? 0}
            onChange={(e) => setQtys({ ...qtys, [line.line_key ?? ""]: Number(e.target.value) })}
            data-testid="rtv-putaway-qty"
          />
        </Field>
      ))}
      <Field id={`rtv-putaway-location-${shipment.id}`} label="Into">
        <select
          id={`rtv-putaway-location-${shipment.id}`}
          className="select"
          value={location}
          onChange={(e) => setLocation(e.target.value)}
          data-testid="rtv-putaway-location"
        >
          {locations.map((loc) => (
            <option key={loc.id} value={loc.id}>
              {loc.name}
            </option>
          ))}
        </select>
      </Field>
      <button
        className="btn btn-sm"
        disabled={busy || total === 0 || !location}
        data-testid="rtv-putaway-submit"
      >
        Put away
      </button>
    </form>
  );
}

/** A recorded-cost value in words; `null` is unknown (pre-PT custody), never 0. */
function valueWords(value: string | null | undefined): string {
  return value === null || value === undefined
    ? "value unknown"
    : `${formatPaiseString(value)} at recorded cost`;
}

/** Goods ticket 15H: the site prepares the Owner's closure of a persistent difference. */
function ClosureForm({ path, shipment, onDone, onError }: ShipmentFormProps) {
  const [reason, setReason] = useState("");
  const [letter, setLetter] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const ready = reason.trim() !== "" && (letter.trim() !== "" || note.trim() !== "");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const body: RtvClosureInput = {
        reason: reason.trim(),
        reviewed_hash: shipment.state_hash ?? "",
        ...(letter.trim() ? { evidence_reference: letter.trim() } : {}),
        ...(note.trim() ? { evidence_note: note.trim() } : {}),
      };
      await api.post(`${path}/shortfall-closures`, { ...body, ...goodsMeta() });
      onDone(
        `Closure sent to the Owner: ${shipment.unaccounted_qty} missing piece(s). Nothing changes until the Owner approves it.`,
      );
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form-grid" onSubmit={submit} data-testid="rtv-closure-form">
      <h4 className="h4">
        <Scale size={16} /> Ask the Owner to close the difference
      </h4>
      <p className="lead">
        If the vendor will not confirm the {shipment.unaccounted_qty} missing piece(s), the Owner
        can close them as a shortfall at their recorded cost. Give the reason and the vendor&apos;s
        letter, or a note. No credit note or payment is recorded.
      </p>
      <Field id={`rtv-close-reason-${shipment.id}`} label="Why the difference should be closed">
        <input
          id={`rtv-close-reason-${shipment.id}`}
          className="input"
          value={reason}
          maxLength={500}
          onChange={(e) => setReason(e.target.value)}
          data-testid="rtv-closure-reason"
        />
      </Field>
      <Field id={`rtv-close-letter-${shipment.id}`} label="Vendor's letter or reference">
        <input
          id={`rtv-close-letter-${shipment.id}`}
          className="input"
          value={letter}
          maxLength={100}
          onChange={(e) => setLetter(e.target.value)}
          data-testid="rtv-closure-reference"
        />
      </Field>
      <Field id={`rtv-close-note-${shipment.id}`} label="Note (if there is no letter)">
        <input
          id={`rtv-close-note-${shipment.id}`}
          className="input"
          value={note}
          maxLength={1000}
          onChange={(e) => setNote(e.target.value)}
          data-testid="rtv-closure-note"
        />
      </Field>
      <button className="btn btn-sm" disabled={busy || !ready} data-testid="rtv-closure-submit">
        Send to the Owner
      </button>
    </form>
  );
}

/** Goods ticket 15H: the prepared closure, and the Owner's decision on it. */
function ClosureDecision({
  pending,
  canDecide,
  lineName,
  onDone,
  onError,
}: {
  pending: RtvPendingClosure;
  canDecide: boolean;
  lineName: (lineKey: string) => string;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const { guarded, dialog } = useStepUp();

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    try {
      await guarded(() =>
        api.post(`${APPROVALS}/${pending.approval_request_id}/decide`, {
          decision,
          reviewed_hash: pending.reviewed_hash,
          ...(decision === "reject" ? { reason_code: reason.trim() } : {}),
          ...goodsMeta(pending.revision),
        }),
      );
      onDone(
        decision === "approve"
          ? `Difference closed: ${pending.quantity} piece(s) recognised as a shortfall.`
          : "Turned down. The difference stays open.",
      );
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card section-card" data-testid="rtv-closure-pending">
      <p>
        <strong>Waiting for the Owner:</strong> close {pending.quantity} missing piece(s) as a
        shortfall
        {pending.value_paise !== undefined ? ` (${valueWords(pending.value_paise)})` : ""}.
      </p>
      <p className="muted">
        {pending.reason}
        {pending.evidence_reference ? ` · letter ${pending.evidence_reference}` : ""}
        {pending.evidence_note ? ` · ${pending.evidence_note}` : ""} · prepared by{" "}
        {pending.prepared_by?.name || "someone"}
        {pending.lines && pending.lines.length > 1
          ? ` · ${pending.lines.map((line) => `${lineName(line.line_key ?? "")}: ${line.qty}`).join(", ")}`
          : ""}
      </p>
      {canDecide && (
        <div className="toolbar">
          <button
            type="button"
            className="btn btn-cta btn-sm"
            disabled={busy}
            onClick={() => void decide("approve")}
            data-testid="rtv-closure-approve"
          >
            Approve — close as a shortfall
          </button>
          <input
            className="input"
            aria-label="Why it is turned down"
            placeholder="Why it is turned down"
            value={reason}
            maxLength={60}
            onChange={(e) => setReason(e.target.value)}
            data-testid="rtv-closure-reject-reason"
          />
          <button
            type="button"
            className="btn btn-sm"
            disabled={busy || reason.trim() === ""}
            onClick={() => void decide("reject")}
            data-testid="rtv-closure-reject"
          >
            Turn down
          </button>
        </div>
      )}
      {dialog}
    </div>
  );
}

function EwayForm({
  path,
  shipment,
  canAttach,
  canVerify,
  onDone,
  onError,
}: {
  path: string;
  shipment: RtvShipment;
  canAttach: boolean;
  canVerify: boolean;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const [reference, setReference] = useState(shipment.eway?.reference ?? "");
  const [busy, setBusy] = useState(false);

  async function send(action: RtvEwayInput["action"]) {
    setBusy(true);
    try {
      const body: RtvEwayInput = { action, reference: reference.trim() };
      await api.post(`${path}/eway`, { ...body, ...goodsMeta() });
      onDone(action === "attach" ? "E-way bill number attached." : "E-way bill verified.");
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="toolbar" data-testid="rtv-eway-form">
      <input
        className="input"
        aria-label="E-way bill number"
        value={reference}
        maxLength={100}
        onChange={(e) => setReference(e.target.value)}
        data-testid="rtv-eway-reference"
      />
      {canAttach && (
        <button
          type="button"
          className="btn btn-sm"
          disabled={busy || reference.trim() === ""}
          onClick={() => void send("attach")}
          data-testid="rtv-eway-attach"
        >
          Attach e-way bill
        </button>
      )}
      {canVerify && (
        <button
          type="button"
          className="btn btn-sm"
          disabled={busy || reference.trim() === ""}
          onClick={() => void send("verify")}
          data-testid="rtv-eway-verify"
        >
          Verify e-way bill
        </button>
      )}
    </div>
  );
}

function PickupForm({
  doc,
  rtv,
  lineName,
  onDone,
  onError,
}: {
  doc: ResourceDTO<MovementData>;
  rtv: RtvDetail;
  lineName: (lineKey: string) => string;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const waiting = (rtv.lines ?? []).filter((line) => (line.outstanding_qty ?? 0) > 0);
  const outstanding = Object.fromEntries(
    waiting.map((line) => [line.line_key ?? "", line.outstanding_qty ?? 0]),
  );
  const [taken, setTaken] = useState<Record<string, number>>({});
  const [collectedBy, setCollectedBy] = useState("");
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [pickedAt, setPickedAt] = useState("");
  const [reasons, setReasons] = useState<ReasonRow[]>([]);
  const [busy, setBusy] = useState(false);

  const gap = leftBehindGap(outstanding, taken, reasons);
  const total = Object.values(taken).reduce((sum, qty) => sum + qty, 0);
  const evidenced = reference.trim() !== "" || note.trim() !== "";
  const remarksOk = reasons.every((row) => row.reason !== "other" || row.remark.trim() !== "");
  const ready =
    total > 0 && collectedBy.trim() !== "" && evidenced && Object.keys(gap).length === 0 && remarksOk;

  function setReason(id: string, patch: Partial<ReasonRow>) {
    setReasons((rows) => rows.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const body: RtvPickupInput = {
        collected_by: collectedBy.trim(),
        ...(reference.trim() ? { evidence_reference: reference.trim() } : {}),
        ...(note.trim() ? { evidence_note: note.trim() } : {}),
        ...(pickedAt ? { picked_up_at: new Date(pickedAt).toISOString() } : {}),
        lines: Object.entries(taken)
          .filter(([, qty]) => qty > 0)
          .map(([line_key, qty]) => ({ line_key, qty })),
        left_behind: reasons.map((row) => ({
          line_key: row.line_key,
          reason: row.reason,
          qty: row.qty,
          ...(row.reason === "other"
            ? { remark: row.remark.trim(), further_pickup_expected: row.further }
            : row.remark.trim()
              ? { remark: row.remark.trim() }
              : {}),
        })),
      };
      await api.post(`${MOVEMENTS}/${doc.id}/rtv-pickups`, { ...body, ...goodsMeta() });
      onDone(`Pickup recorded: ${total} piece(s) handed over to the vendor.`);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card section-card form-grid" onSubmit={submit} data-testid="rtv-pickup-form">
      <h4 className="h4">
        <PackageCheck size={16} /> Record a vendor pickup
      </h4>
      <p className="lead">
        Record only what the vendor actually took. Pieces left behind stay reserved to this return,
        and each one needs a reason.
      </p>
      {waiting.map((line) => (
        <Field
          key={line.line_key}
          id={`rtv-take-${line.line_key}`}
          label={`Collected — ${lineName(line.line_key ?? "")} (up to ${line.outstanding_qty})`}
        >
          <input
            id={`rtv-take-${line.line_key}`}
            className="input"
            type="number"
            min={0}
            max={line.outstanding_qty}
            value={taken[line.line_key ?? ""] ?? 0}
            onChange={(e) => setTaken({ ...taken, [line.line_key ?? ""]: Number(e.target.value) })}
            data-testid="rtv-take"
          />
        </Field>
      ))}
      <Field id="rtv-collected-by" label="Collected by (vendor or representative)">
        <input
          id="rtv-collected-by"
          className="input"
          value={collectedBy}
          maxLength={120}
          onChange={(e) => setCollectedBy(e.target.value)}
          data-testid="rtv-collected-by"
        />
      </Field>
      <Field
        id="rtv-reference"
        label="Pickup slip or challan number"
        hint="Evidence of the handover: a reference, a note, or both."
      >
        <input
          id="rtv-reference"
          className="input"
          value={reference}
          maxLength={100}
          onChange={(e) => setReference(e.target.value)}
          aria-describedby="rtv-reference-hint"
          data-testid="rtv-reference"
        />
      </Field>
      <Field id="rtv-note" label="Handover note (optional)">
        <textarea
          id="rtv-note"
          className="input"
          value={note}
          maxLength={1000}
          onChange={(e) => setNote(e.target.value)}
          data-testid="rtv-note"
        />
      </Field>
      <Field id="rtv-picked-at" label="When it was collected (leave empty for now)">
        <input
          id="rtv-picked-at"
          className="input"
          type="datetime-local"
          value={pickedAt}
          onChange={(e) => setPickedAt(e.target.value)}
          data-testid="rtv-picked-at"
        />
      </Field>

      <div data-testid="rtv-reasons">
        <h4 className="h4">Why pieces were left behind</h4>
        {reasons.map((row) => (
          <div key={row.id} className="toolbar" data-testid="rtv-reason-row">
            <select
              className="select"
              aria-label="Line"
              value={row.line_key}
              onChange={(e) => setReason(row.id, { line_key: e.target.value })}
              data-testid="rtv-reason-line"
            >
              {waiting.map((line) => (
                <option key={line.line_key} value={line.line_key}>
                  {lineName(line.line_key ?? "")}
                </option>
              ))}
            </select>
            <select
              className="select"
              aria-label="Reason"
              value={row.reason}
              onChange={(e) => setReason(row.id, { reason: e.target.value as LeftBehindReason })}
              data-testid="rtv-reason"
            >
              {LEFT_BEHIND_REASONS.map((reason) => (
                <option key={reason} value={reason}>
                  {LEFT_BEHIND_LABEL[reason]}
                </option>
              ))}
            </select>
            <input
              className="input"
              type="number"
              min={1}
              aria-label="Pieces"
              value={row.qty}
              onChange={(e) => setReason(row.id, { qty: Number(e.target.value) })}
              data-testid="rtv-reason-qty"
            />
            <input
              className="input"
              aria-label="Remark"
              placeholder={row.reason === "other" ? "Remark (needed)" : "Remark (optional)"}
              value={row.remark}
              maxLength={500}
              onChange={(e) => setReason(row.id, { remark: e.target.value })}
              data-testid="rtv-reason-remark"
            />
            {FURTHER_PICKUP[row.reason] === null ? (
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={row.further}
                  onChange={(e) => setReason(row.id, { further: e.target.checked })}
                  data-testid="rtv-reason-further"
                />{" "}
                Another pickup expected
              </label>
            ) : (
              <span className="muted">
                {FURTHER_PICKUP[row.reason] ? "Another pickup expected" : "No further pickup"}
              </span>
            )}
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setReasons((rows) => rows.filter((r) => r.id !== row.id))}
            >
              Remove
            </button>
          </div>
        ))}
        {waiting.length > 0 && (
          <button
            type="button"
            className="btn btn-sm"
            onClick={() =>
              setReasons((rows) => [
                ...rows,
                {
                  id: crypto.randomUUID(),
                  line_key: waiting[0].line_key ?? "",
                  reason: "vendor_rejected",
                  qty: 1,
                  remark: "",
                  further: false,
                },
              ])
            }
            data-testid="rtv-add-reason"
          >
            Add a reason
          </button>
        )}
        {Object.entries(gap).map(([key, missing]) => (
          <p key={key} className="muted" data-testid="rtv-reason-gap">
            {lineName(key)}:{" "}
            {missing > 0
              ? `${missing} piece(s) left behind still need a reason.`
              : `the reasons name ${-missing} piece(s) more than are left behind.`}
          </p>
        ))}
      </div>

      <button className="btn btn-cta" disabled={busy || !ready} data-testid="rtv-pickup-submit">
        Record the pickup
      </button>
    </form>
  );
}

function WithdrawForm({
  doc,
  rtv,
  lineName,
  onDone,
  onError,
}: {
  doc: ResourceDTO<MovementData>;
  rtv: RtvDetail;
  lineName: (lineKey: string) => string;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const waiting = (rtv.lines ?? []).filter((line) => (line.outstanding_qty ?? 0) > 0);
  const [reason, setReason] = useState<WithdrawalReason>("vendor_rejected");
  const [remark, setRemark] = useState("");
  const [whole, setWhole] = useState(true);
  const [qtys, setQtys] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const partTotal = Object.values(qtys).reduce((sum, qty) => sum + qty, 0);
  const ready = (reason !== "other" || remark.trim() !== "") && (whole || partTotal > 0);
  const nothingLeft = (rtv.picked_up_qty ?? 0) === 0;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const body: RtvWithdrawalInput = {
        reason,
        ...(remark.trim() ? { remark: remark.trim() } : {}),
        ...(whole
          ? {}
          : {
              lines: Object.entries(qtys)
                .filter(([, qty]) => qty > 0)
                .map(([line_key, qty]) => ({ line_key, qty })),
            }),
      };
      await api.post(`${MOVEMENTS}/${doc.id}/rtv-withdrawals`, { ...body, ...goodsMeta() });
      onDone(
        whole
          ? nothingLeft
            ? "Return cancelled. Nothing left; the goods stay where they are, still held."
            : "Balance withdrawn. The return is closed as partially returned."
          : "Withdrawn. Those pieces stay where they are, still held.",
      );
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card section-card form-grid" onSubmit={submit} data-testid="rtv-withdraw-form">
      <h4 className="h4">
        <Undo2 size={16} /> Withdraw from this return
      </h4>
      <p className="lead">
        Withdrawing releases only the reservation. Nothing moves: quarantined goods stay in
        quarantine and on hold, and pickups already recorded keep their evidence.
      </p>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={whole}
          onChange={(e) => setWhole(e.target.checked)}
          data-testid="rtv-withdraw-whole"
        />{" "}
        Withdraw everything still waiting ({rtv.outstanding_qty})
      </label>
      {!whole &&
        waiting.map((line) => (
          <Field
            key={line.line_key}
            id={`rtv-withdraw-${line.line_key}`}
            label={`Withdraw — ${lineName(line.line_key ?? "")} (up to ${line.outstanding_qty})`}
          >
            <input
              id={`rtv-withdraw-${line.line_key}`}
              className="input"
              type="number"
              min={0}
              max={line.outstanding_qty}
              value={qtys[line.line_key ?? ""] ?? 0}
              onChange={(e) => setQtys({ ...qtys, [line.line_key ?? ""]: Number(e.target.value) })}
              data-testid="rtv-withdraw-qty"
            />
          </Field>
        ))}
      <Field id="rtv-withdraw-reason" label="Reason">
        <select
          id="rtv-withdraw-reason"
          className="select"
          value={reason}
          onChange={(e) => setReason(e.target.value as WithdrawalReason)}
          data-testid="rtv-withdraw-reason"
        >
          {WITHDRAWAL_REASONS.map((value) => (
            <option key={value} value={value}>
              {WITHDRAWAL_LABEL[value]}
            </option>
          ))}
        </select>
      </Field>
      <Field id="rtv-withdraw-remark" label={reason === "other" ? "Remark (needed)" : "Remark (optional)"}>
        <input
          id="rtv-withdraw-remark"
          className="input"
          value={remark}
          maxLength={500}
          onChange={(e) => setRemark(e.target.value)}
          data-testid="rtv-withdraw-remark"
        />
      </Field>
      <button className="btn btn-sm" disabled={busy || !ready} data-testid="rtv-withdraw-submit">
        Withdraw
      </button>
    </form>
  );
}
