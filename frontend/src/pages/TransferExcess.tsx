// Goods ticket 16: what a transfer's destination count found that nobody sent,
// and the corrective transfer that resolves it.
//
// Excess is shown beside the shipment, never inside it: each observation is
// held, unvalued, until a corrective transfer from the sender matches it. The
// sender proposes that transfer - naming its own item, with its evidence - a
// different person (the Owner) approves it through the ordinary approval, and
// the sender confirms it once, which moves nothing a second time at the
// destination. Every button here is drawn from `allowed_actions`; the server
// re-checks all of it.
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { FileCheck, Send } from "lucide-react";

import { goodsMeta } from "../lib/api";
import { Field, useGoodsFetch } from "../lib/goodsScreen";
import {
  EXCESS_DECISION_LABEL,
  correctiveProblem,
  excessWords,
  type CorrectiveConfirmationBody,
  type CorrectiveProposalBody,
  type ExcessObservation,
  type TransferDetail,
  type TransferDispatchRow,
} from "../lib/goodsTransfers";

type Runner = (path: string, body: Record<string, unknown>, message: string) => Promise<void>;

interface SenderStockRow {
  sku_id: string | null;
  description: string;
  transferable_qty: number;
}

/** Every excess observation one shipment's count recorded, and its route. */
export function ExcessTable({
  record,
  detail,
  onRun,
}: {
  record: TransferDispatchRow;
  detail: TransferDetail;
  onRun: Runner;
}) {
  const may = (action: string) => detail.allowed_actions.includes(action);
  const itemOf = (lineKey: string | null) =>
    detail.lines.find((l) => l.line_key === lineKey)?.description || lineKey || "";
  return (
    <div data-testid={`excess-${record.sequence_no}`}>
      <table>
        <caption>Goods nobody sent - held apart, unvalued</caption>
        <thead>
          <tr>
            <th>What arrived</th>
            <th className="num">Pieces</th>
            <th>Where it stands</th>
            <th>Corrective transfers</th>
          </tr>
        </thead>
        <tbody>
          {record.excess_observations.map((observation) => (
            <tr
              key={observation.lot_id}
              data-testid="excess-row"
              data-lot={observation.lot_id}
              data-wrong={String(Boolean(observation.in_place_of_line_key))}
              data-unresolved={observation.unresolved_qty}
            >
              <td>
                {observation.description}
                {observation.alias_value ? ` · code ${observation.alias_value}` : ""}
                {observation.in_place_of_line_key
                  ? ` · came instead of ${itemOf(observation.in_place_of_line_key)}`
                  : ""}
              </td>
              <td className="num">{observation.qty}</td>
              <td data-testid="excess-state">{excessWords(observation)}</td>
              <td>
                {observation.decisions.length === 0
                  ? "None yet"
                  : observation.decisions.map((decision) => (
                      <p
                        key={decision.id}
                        data-testid="excess-decision"
                        data-state={decision.state}
                      >
                        {EXCESS_DECISION_LABEL[decision.state] ?? decision.state}
                        {decision.number ? ` · ${decision.number}` : ""}
                        {decision.matched ? " · matched" : ""}
                        {decision.corrective_transfer ? (
                          <>
                            {" · "}
                            <Link
                              to={`/goods/transfers/${decision.corrective_transfer.id}`}
                              data-testid="excess-corrective-link"
                            >
                              {decision.corrective_transfer.number ?? "Corrective transfer"}
                            </Link>
                          </>
                        ) : null}
                      </p>
                    ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {may("propose_corrective")
        ? record.excess_observations
            .filter((observation) => observation.unclaimed_qty > 0)
            .map((observation) => (
              <CorrectiveForm
                key={observation.lot_id}
                detail={detail}
                observation={observation}
                onRun={onRun}
              />
            ))
        : null}
    </div>
  );
}

/** The sender proposes a corrective transfer for exact pieces of one observation. */
function CorrectiveForm({
  detail,
  observation,
  onRun,
}: {
  detail: TransferDetail;
  observation: ExcessObservation;
  onRun: Runner;
}) {
  // The sender's own eligible stock, from the same stock search a new
  // transfer is drafted from. A recorded item narrows it to that item.
  const stock = useGoodsFetch<{ items: SenderStockRow[] }, SenderStockRow[]>(
    `/goods-v1/outbound/stock-search?source_site_id=${detail.source_site_id}&limit=100` +
      (observation.sku_id ? `&sku_id=${observation.sku_id}` : ""),
    (r) => (r.items ?? []).filter((row) => row.sku_id && row.transferable_qty > 0),
    [],
  );
  const items = useMemo(() => {
    const bySku = new Map<string, { sku_id: string; description: string; available: number }>();
    for (const row of stock.value) {
      const key = String(row.sku_id);
      const item = bySku.get(key) ?? { sku_id: key, description: row.description, available: 0 };
      item.available += row.transferable_qty;
      bySku.set(key, item);
    }
    return [...bySku.values()];
  }, [stock.value]);
  const [picked, setPicked] = useState("");
  const sku = picked || observation.sku_id || "";
  const [qty, setQty] = useState(String(observation.unclaimed_qty));
  const [evidence, setEvidence] = useState("");
  const [reason, setReason] = useState("");
  const available = items.find((item) => item.sku_id === sku)?.available ?? 0;
  const problem = sku ? correctiveProblem(Number(qty), observation.unclaimed_qty, available) : "";
  const id = `corrective-${observation.lot_id}`;
  const body: CorrectiveProposalBody = {
    sku_id: sku,
    qty: Number(qty),
    source_evidence_reference: evidence.trim(),
    reason: reason.trim(),
    ...goodsMeta(),
  };
  const ready = sku && !problem && body.source_evidence_reference && body.reason;

  return (
    <div className="form-grid" data-testid="corrective-form" data-lot={observation.lot_id}>
      <h4 className="h4">Correct “{observation.description}” with a corrective transfer</h4>
      <p className="lead">
        If these goods are the sender&apos;s own, name the item and show why. The sender must hold
        that many sendable pieces of it; a different person approves the corrective transfer, and
        confirming it matches these pieces once - nothing arrives a second time. If the sender
        cannot show the stock, the goods stay held here, unvalued.
      </p>
      <Field id={`${id}-sku`} label="Which of the sender's items it is">
        <select
          id={`${id}-sku`}
          className="input"
          value={sku}
          disabled={Boolean(observation.sku_id)}
          onChange={(e) => setPicked(e.target.value)}
          data-testid="corrective-sku"
        >
          <option value="">Choose the item…</option>
          {items.map((item) => (
            <option key={item.sku_id} value={item.sku_id}>
              {item.description || item.sku_id} ({item.available} sendable)
            </option>
          ))}
        </select>
      </Field>
      <Field id={`${id}-qty`} label="Pieces">
        <input
          id={`${id}-qty`}
          className="input"
          type="number"
          min={1}
          max={observation.unclaimed_qty}
          value={qty}
          onChange={(e) => setQty(e.target.value)}
          data-testid="corrective-qty"
        />
      </Field>
      <Field
        id={`${id}-evidence`}
        label="The sender's evidence"
        hint="What shows these goods are the sender's own - a packing check, a count sheet."
      >
        <input
          id={`${id}-evidence`}
          className="input"
          value={evidence}
          aria-describedby={`${id}-evidence-hint`}
          onChange={(e) => setEvidence(e.target.value)}
          data-testid="corrective-evidence"
        />
      </Field>
      <Field id={`${id}-reason`} label="Why">
        <input
          id={`${id}-reason`}
          className="input"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          data-testid="corrective-reason"
        />
      </Field>
      {problem && <div className="warn-note">{problem}</div>}
      <button
        className="btn btn-sm"
        disabled={!ready}
        onClick={() => {
          void onRun(
            `/excess/${observation.lot_id}/corrective`,
            body,
            "Corrective transfer drafted and sent for approval. Nothing is reserved or moved until a different person approves it.",
          );
        }}
        data-testid="corrective-submit"
      >
        <Send size={14} /> Propose a corrective transfer
      </button>
    </div>
  );
}

/** On a corrective transfer: what it corrects, and - to the sender - its confirmation. */
export function CorrectivePanel({ detail, onRun }: { detail: TransferDetail; onRun: Runner }) {
  const may = (action: string) => detail.allowed_actions.includes(action);
  const decision = detail.corrective_decision;
  const [evidence, setEvidence] = useState("");
  const [note, setNote] = useState("");
  if (!detail.corrective_for) return null;
  const body: CorrectiveConfirmationBody = {
    source_evidence_reference: evidence.trim(),
    ...(note.trim() ? { note: note.trim() } : {}),
    ...goodsMeta(),
  };
  return (
    <div className="warn-note" data-testid="corrective-panel">
      <p>
        <strong>Corrective transfer</strong> for goods observed at the destination of{" "}
        <Link to={`/goods/transfers/${detail.corrective_for.id}`} data-testid="corrective-for">
          {detail.corrective_for.number ?? "the original transfer"}
        </Link>
        . It is never shipped: its goods are already there. Confirming it records its dispatch and
        arrival together and matches those observed pieces once.
      </p>
      {decision ? (
        <p data-testid="corrective-decision" data-state={decision.state}>
          {EXCESS_DECISION_LABEL[decision.state] ?? decision.state}
          {decision.number ? ` · ${decision.number}` : ""} · {decision.quantity} piece(s) · evidence{" "}
          {decision.source_evidence_reference}
          {decision.matched ? " · matched" : ""}
        </p>
      ) : null}
      {may("confirm_corrective") ? (
        <div className="form-grid" data-testid="corrective-confirm-form">
          <Field
            id="corrective-confirm-evidence"
            label="The sender's evidence that these reserved pieces are those goods"
          >
            <input
              id="corrective-confirm-evidence"
              className="input"
              value={evidence}
              onChange={(e) => setEvidence(e.target.value)}
              data-testid="corrective-confirm-evidence"
            />
          </Field>
          <Field id="corrective-confirm-note" label="Note (optional)">
            <input
              id="corrective-confirm-note"
              className="input"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              data-testid="corrective-confirm-note"
            />
          </Field>
          <button
            className="btn btn-cta"
            disabled={!body.source_evidence_reference}
            onClick={() => {
              void onRun(
                "/corrective-confirmation",
                body,
                "Confirmed. The observed goods are matched; they wait at the destination to be put away.",
              );
            }}
            data-testid="corrective-confirm"
          >
            <FileCheck size={14} /> Confirm the corrective transfer
          </button>
        </div>
      ) : null}
    </div>
  );
}
