// Transfer → In-Transit: what is between two locations, and every gap (#71).
//
// Two honest numbers on one screen, because they are the same stock at two
// stages of the same story: pieces still on the road (fine, just not anywhere
// yet) and pieces that never arrived (somebody's problem). The gaps half is the
// warehouse/HO work — a store cannot close its own gap, and the list is scoped
// on the sender, so a store never even sees the gap it caused.
//
// Nothing here decides anything. Raising a closure asks; the approvals inbox
// answers; the server posts the resolving entries and refuses anybody entitled
// to the receiving store at either end.
import { useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ShieldCheck, Truck } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { useDoc, useList } from "../lib/hooks";
import { Money } from "../lib/format";
import { canCloseTransferGap } from "../lib/outbound-rbac";
import { isCleared } from "../components/approval";
import { PageHeader } from "../components/PageHeader";
import {
  GapStatePill,
  ReceiptExceptions,
  fmtDate,
  type TransferT,
} from "./OutboundTransfers";
import "./Booking.css";

interface InTransitRowT {
  transfer_doc_number: string;
  source_store_code: string;
  destination_store_code: string;
  sku_code: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  qty: number;
  value_paise: number;
}

interface InTransitPayloadT {
  summary: { units_in_transit: number; value_paise: number; transfers: number };
  rows: InTransitRowT[];
}

// The three real answers, in the order a senior thinks of them. The wording is
// the server's vocabulary — the reason is an instruction to the ledger, not a
// note, so it must not be paraphrased loosely here.
const GAP_REASONS = [
  {
    value: "found_later",
    label: "Found later",
    effect: "The pieces did arrive — they go into the receiving store's stock.",
  },
  {
    value: "wrongly_scanned",
    label: "Wrongly scanned at dispatch",
    effect: "They never left — they go back into the sender's stock.",
  },
  {
    value: "lost_in_transit",
    label: "Lost in transit",
    effect: "They are gone — the value is written off the books.",
  },
];

function skuLabel(r: { design: string; color: string; size: string; brand: string }): string {
  return [r.design, r.color, r.size].filter(Boolean).join(" · ") || r.brand || "—";
}

// ---------------------------------------------------------------------------
// Raise a closure — or correct the one already there
// ---------------------------------------------------------------------------

/**
 * One form for both, because they ask the identical question. Passing
 * `correcting` switches it from raising a new closure to amending the draft:
 * a transfer only ever has one closure document, and after a rejection
 * correcting that document is the only way the pieces get out of transit.
 */
function CloseGapForm({ transfer, correcting, onDone }: {
  transfer: TransferT;
  correcting?: { id: number; reason: string; note: string };
  onDone: () => void;
}) {
  const [reason, setReason] = useState(correcting?.reason ?? "");
  const [note, setNote] = useState(correcting?.note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function save() {
    setError("");
    if (!reason) {
      setError("Pick what became of the missing pieces.");
      return;
    }
    setBusy(true);
    try {
      if (correcting) {
        await api.patch(`/outbound/gap-closures/${correcting.id}`, { reason, note });
      } else {
        await api.post(`/outbound/transfers/${transfer.id}/gap-closure`, { reason, note });
      }
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const chosen = GAP_REASONS.find((r) => r.value === reason);

  return (
    <div style={{ marginTop: 14 }} data-testid={`close-gap-form-${transfer.id}`}>
      <div className="form-row">
        <div className="field">
          <label>What became of them?</label>
          <select
            className="select"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            data-testid={`gap-reason-${transfer.id}`}
          >
            <option value="">Select a reason…</option>
            {GAP_REASONS.map((r) => (
              <option key={r.value} value={r.value}>{r.label}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Note (optional)</label>
          <input
            className="input"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Who you spoke to, what they said…"
            data-testid={`gap-note-${transfer.id}`}
          />
        </div>
      </div>
      {chosen && (
        <p className="lead" style={{ marginTop: 4 }} data-testid={`gap-effect-${transfer.id}`}>
          <b>What this posts:</b> {chosen.effect}
        </p>
      )}
      {error && <div className="login-error" data-testid={`gap-error-${transfer.id}`}>{error}</div>}
      <button
        className="btn btn-primary"
        style={{ marginTop: 10 }}
        disabled={busy}
        onClick={save}
        data-testid={`gap-submit-${transfer.id}`}
      >
        <ShieldCheck size={15} />{" "}
        {busy ? "Sending…" : correcting ? "Correct and send again" : "Send for approval"}
      </button>
      <p className="lead" style={{ marginTop: 8, opacity: 0.75 }}>
        This does not close the gap yet. A second, senior person has to approve it from the{" "}
        <Link to="/action-needed?show=approvals">Action Needed</Link>, and only then do the entries post.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// One gap
// ---------------------------------------------------------------------------

function GapCard({ transfer, canClose, onChanged }: {
  transfer: TransferT;
  canClose: boolean;
  onChanged: () => void;
}) {
  const [opening, setOpening] = useState(false);
  const [correcting, setCorrecting] = useState(false);
  const closure = transfer.gap_closure;
  const [posting, setPosting] = useState(false);
  const [error, setError] = useState("");

  async function post(closureId: number) {
    setError("");
    setPosting(true);
    try {
      await api.post(`/outbound/gap-closures/${closureId}/submit`);
      onChanged();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setPosting(false);
    }
  }

  return (
    <div className="card section-card" style={{ marginBottom: 18 }} data-testid={`gap-card-${transfer.id}`}>
      <div className="toolbar" style={{ marginBottom: 8 }}>
        <div>
          <p className="eyebrow">
            <Link to={`/transfer/${transfer.id}`} className="mono">
              {transfer.doc_number || `Draft #${transfer.id}`}
            </Link>
          </p>
          <h3 className="h3">
            {transfer.source_store_code} → {transfer.destination_store_code}
          </h3>
          <p className="lead">
            <b>{transfer.qty_in_transit} piece(s)</b> sent and never scanned in
            {transfer.receipt ? ` · received ${fmtDate(transfer.receipt.receipt_date)}` : ""}
            {transfer.receipt?.received_by_name ? ` by ${transfer.receipt.received_by_name}` : ""}
          </p>
        </div>
        <div className="spacer" />
        <GapStatePill state={transfer.gap_state} />
      </div>

      {transfer.receipt?.shortfall_notes && (
        <p className="lead" data-testid={`gap-notes-${transfer.id}`}>
          “{transfer.receipt.shortfall_notes}”
        </p>
      )}
      {transfer.receipt && transfer.receipt.exceptions.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <ReceiptExceptions rows={transfer.receipt.exceptions} />
        </div>
      )}

      {closure ? (
        <div style={{ marginTop: 14 }} data-testid={`gap-pending-${transfer.id}`}>
          <p className="lead">
            Closure raised by <b>{closure.created_by_name || "—"}</b> — {closure.reason_label}.
            {isCleared(closure.approval)
              ? " Approved — post it to move the pieces."
              : closure.approval?.status === "rejected"
                ? " Turned down — correct it and send it again, or the pieces stay in transit."
                : " Waiting for a second, senior person to approve it."}
          </p>
          {error && <div className="login-error" data-testid={`gap-post-error-${transfer.id}`}>{error}</div>}
          {canClose && isCleared(closure.approval) && (
            <button
              className="btn btn-cta"
              style={{ marginTop: 8 }}
              disabled={posting}
              onClick={() => post(closure.id)}
              data-testid={`gap-post-${transfer.id}`}
            >
              <ShieldCheck size={15} /> {posting ? "Posting…" : "Post the closure"}
            </button>
          )}
          {/* Only once a checker has had their say. A pending closure must not
              offer this: the server refuses to change a document somebody is
              deciding on, and the button would only invite the attempt. */}
          {canClose && closure.approval?.status !== "pending" &&
            (correcting ? (
              <CloseGapForm
                transfer={transfer}
                correcting={{ id: closure.id, reason: closure.reason, note: closure.note || "" }}
                onDone={onChanged}
              />
            ) : (
              <button
                className="btn"
                style={{ marginTop: 8 }}
                onClick={() => setCorrecting(true)}
                data-testid={`gap-correct-${transfer.id}`}
              >
                <AlertTriangle size={15} /> Correct this closure
              </button>
            ))}
        </div>
      ) : canClose ? (
        opening ? (
          <CloseGapForm transfer={transfer} onDone={onChanged} />
        ) : (
          <button
            className="btn"
            style={{ marginTop: 12 }}
            onClick={() => setOpening(true)}
            data-testid={`gap-open-${transfer.id}`}
          >
            <AlertTriangle size={15} /> Close this gap
          </button>
        )
      ) : (
        <p className="lead" style={{ marginTop: 12, opacity: 0.75 }}>
          Only the Operations Head can close a gap.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

export function InTransitPage() {
  const { user } = useAuth();
  const canClose = canCloseTransferGap(user);
  const { data: gaps, loading: gapsLoading, reload } = useList<TransferT>(
    "/outbound/transfers/gaps",
  );
  // A summary + its rows in one object, not a bare list — so useDoc, not useList.
  const { data: transit, loading: transitLoading } = useDoc<InTransitPayloadT>(
    "/stockledger/in-transit",
  );
  const summary = transit?.summary;
  const rows = transit?.rows ?? [];

  return (
    <div className="page-pad">
      <PageHeader
        title="In transit & gaps"
        lead="Stock that has left one location and not yet arrived at another. The sender is answerable for it until the receiver scans it in — so a carton lost on the bus is visible here, not vanished."
      />

      {/* Gaps first: these are the ones somebody has to act on. */}
      <h2 className="h2" style={{ marginBottom: 12 }}>
        Gaps — sent ≠ received
      </h2>
      {gapsLoading ? (
        <p className="lead">Loading…</p>
      ) : gaps.length === 0 ? (
        <div className="card section-card" data-testid="gaps-empty">
          <p className="eyebrow"><ShieldCheck size={15} /> Nothing open</p>
          Every transfer that has been received was received in full.
        </div>
      ) : (
        gaps.map((t) => (
          <GapCard key={t.id} transfer={t} canClose={canClose} onChanged={reload} />
        ))
      )}

      {/* Then the plain in-transit position. */}
      <h2 className="h2" style={{ marginTop: 28, marginBottom: 12 }}>
        On the road
      </h2>
      {summary && (
        <div className="form-row" style={{ marginBottom: 18 }}>
          <div className="card section-card">
            <p className="eyebrow">Pieces in transit</p>
            <h3 className="h3" data-testid="transit-units">{summary.units_in_transit}</h3>
          </div>
          <div className="card section-card">
            <p className="eyebrow">Value in transit</p>
            <h3 className="h3"><Money paise={summary.value_paise} /></h3>
          </div>
          <div className="card section-card">
            <p className="eyebrow">Open transfers</p>
            <h3 className="h3">{summary.transfers}</h3>
          </div>
        </div>
      )}
      {transitLoading ? (
        <p className="lead">Loading…</p>
      ) : rows.length === 0 ? (
        <div className="card section-card" data-testid="transit-empty">
          <p className="eyebrow"><Truck size={15} /> Nothing on the road</p>
          Every dispatched piece has been accounted for.
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="transit-table">
            <thead>
              <tr>
                <th>Transfer</th>
                <th>From</th>
                <th>To</th>
                <th>SKU</th>
                <th>Item</th>
                <th className="num">Pieces</th>
                <th className="num">Value</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.transfer_doc_number}-${r.sku_code}`}>
                  <td><b className="mono">{r.transfer_doc_number}</b></td>
                  <td><b className="mono">{r.source_store_code}</b></td>
                  <td><b className="mono">{r.destination_store_code}</b></td>
                  <td className="mono">{r.sku_code}</td>
                  <td>{skuLabel(r)}</td>
                  <td className="num"><b>{r.qty}</b></td>
                  <td className="num"><Money paise={r.value_paise} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default InTransitPage;
