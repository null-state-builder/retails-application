// Damage reviews (OPS-05; goods tickets 12A and 12B) — the second person's
// decision on a damage report, on the Movements tab of the Stock section.
//
// Three things this panel has to make plain rather than merely obey:
//   * the goods are already out of availability. A pending report is not "maybe
//     damaged, still sellable"; it is held stock waiting for somebody to say
//     what happens to it;
//   * the decision is somebody else's. Whoever reported the damage cannot
//     decide it, so the panel is drawn only for a person who may approve
//     movements, and the server refuses a self-review by name either way;
//   * rejecting is a correction, not a delete. The report stays; one linked
//     release gives the quantity back;
//   * confirming is only that decision (goods ticket 12A). It values nothing
//     and lifts nothing else: another hold, or a transfer reservation over the
//     same pieces, stays exactly where it was.
//
// Damage found while receiving is reviewed here too (ticket 05C), and a mistaken
// receiving report is rejected here like any other: the pieces go back to the
// site's receiving location as good goods.
//
// Ticket 12B: rejecting lifts the damage and nothing else - another hold, a
// reservation or a missing acceptance still stands - and the panel says so. A
// report cannot be rejected when its pieces were since valued as damaged, or
// no longer stand where it left them (its hold is gone, or they have moved).
import { useState } from "react";
import { ShieldCheck, ShieldX } from "lucide-react";

import { api, goodsMeta } from "../lib/api";
import { Denied, Field, listState, useGoodsFetch, useStepUp, type Page } from "../lib/goodsScreen";
import {
  DAMAGE_NO_REJECT,
  DAMAGE_REJECT_SCOPE,
  DAMAGE_SOURCE_LABEL,
  DAMAGE_STATE_LABEL,
  type DamageReport,
} from "../lib/goodsMovements";
import { formatDateTime } from "../lib/format";

const REPORTS = "/goods-v1/outbound/damage-reports";

export function DamageReviewsPanel({
  siteId,
  onDone,
  onError,
}: {
  siteId: string;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const list = useGoodsFetch<Page<DamageReport>, DamageReport[]>(
    `${REPORTS}?state=pending${siteId ? `&site=${siteId}` : ""}`,
    (r) => r.items ?? [],
    [],
  );

  if (list.denied) return <Denied what="damage report" />;
  const state = listState(
    { loading: list.loading, failure: list.failure, empty: list.value.length === 0 },
    "No damage report is waiting for a decision here.",
  );

  return (
    <section className="card section-card" data-testid="dmg-panel">
      <h3 className="h3">Damage reviews</h3>
      <p className="lead">
        These pieces are already held in quarantine and cannot be sold, transferred or reserved.
        Confirm the damage, or reject a mistaken report to give the quantity back. Whoever reported
        it cannot decide it.
      </p>
      <p className="muted" data-testid="dmg-confirm-scope">
        Confirming records only that the damage is real. It puts no value on the goods, and any
        other hold or transfer reservation over them stays as it is; valuing damage is a separate
        decision.
      </p>
      <p className="muted" data-testid="dmg-reject-scope">
        {DAMAGE_REJECT_SCOPE}
      </p>
      {state ?? (
        <table data-testid="dmg-list">
          <thead>
            <tr>
              <th>Reported</th>
              <th>Where it came from</th>
              <th>Reason</th>
              <th className="num">Pieces</th>
              <th>Reported by</th>
              <th>Decision</th>
            </tr>
          </thead>
          <tbody>
            {list.value.map((report) => (
              <ReviewRow
                key={report.id}
                report={report}
                onDone={(message) => {
                  list.reload();
                  onDone(message);
                }}
                onError={onError}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ReviewRow({
  report,
  onDone,
  onError,
}: {
  report: DamageReport;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const { guarded, dialog } = useStepUp();

  async function decide(decision: "confirm" | "reject") {
    setBusy(true);
    try {
      await guarded(() =>
        api.post(`${REPORTS}/${report.id}/decide`, { decision, reason, ...goodsMeta() }),
      );
      onDone(
        decision === "confirm"
          ? `${report.quantity} piece(s) confirmed damaged. They stay in quarantine.`
          : `Report rejected. The damage is lifted from ${report.quantity} piece(s); ` +
              "any other hold, reservation or acceptance still applies.",
      );
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <tr data-testid="dmg-row" data-report={report.id} data-state={report.state}>
      <td>{formatDateTime(report.reported_at)}</td>
      <td data-testid="dmg-source">{DAMAGE_SOURCE_LABEL[report.source] ?? report.source}</td>
      <td>{report.reason_code}</td>
      <td className="num" data-testid="dmg-qty">
        {report.quantity}
      </td>
      <td data-testid="dmg-reporter">{report.reported_by.name || report.reported_by.id}</td>
      <td>
        <div className="form-grid">
          <Field id={`dmg-reason-${report.id}`} label="Reason">
            <input
              id={`dmg-reason-${report.id}`}
              className="input"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              data-testid="dmg-reason"
            />
          </Field>
          <div className="toolbar">
            <button
              className="btn btn-cta"
              disabled={busy || !reason}
              onClick={() => decide("confirm")}
              data-testid="dmg-confirm"
            >
              <ShieldCheck size={14} /> Confirm damaged
            </button>
            {report.can_reject ? (
              <button
                className="btn btn-sm"
                disabled={busy || !reason}
                onClick={() => decide("reject")}
                data-testid="dmg-reject"
              >
                <ShieldX size={14} /> Reject the report
              </button>
            ) : (
              /* Tickets 05C and 12B: valued, stale or moved pieces cannot have
                 the damage lifted. Saying so beats a button that refuses. */
              <span className="muted" data-testid="dmg-no-reject">
                {DAMAGE_NO_REJECT}
              </span>
            )}
          </div>
          <p className="muted">{DAMAGE_STATE_LABEL[report.state] ?? report.state}</p>
        </div>
        {dialog}
      </td>
    </tr>
  );
}
