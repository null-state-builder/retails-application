// PT approvals (ticket 06; PT Work's To approve tab since OPS-17): a checker's queue of submitted PTs, newest first,
// each opened to the exact submitted revision with its reconciliation,
// thresholds and authority — never a live re-read that could silently answer
// a different revision than what was reviewed.
//
// `ApprovalDTO` (E169/E170) names the exact revision it was submitted against
// (`subject_revision`) and the exact document it belongs to (`parent_document`,
// GSA-T04). Nothing here matches an approval by its title, and E234's
// `expected_revision` comes from the approval itself rather than from a later
// read of the PT. The viewer still reads the PT (E099) to show its rows and
// reconciliation, and a `REVISION_SUPERSEDED` banner appears the moment that
// PT has moved past the revision this approval names.
import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Clock, History, ShieldAlert } from "lucide-react";
import { Link } from "react-router-dom";

import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Denied,
  Feedback,
  hold,
  listState,
  useGoodsFetch,
  useResourceDoc,
  useStepUp,
  type Page,
} from "../lib/goodsScreen";
import {
  attemptHistory,
  isLikelySelfApproval,
  isSuperseded,
  newestFirst,
  pinnedVersions,
  reconciliationView,
  type ApprovalDTO,
  type PtData,
} from "../lib/goodsPt";
import { ptWorkPath } from "../lib/ptWork";
import { useAuth } from "../auth/AuthContext";
import { formatDateTime, formatPaiseString } from "../lib/format";
import "./PtScreens.css";

const PT_ACTIONS = new Set(["pt.approve.receipt", "pt.approve.opening", "pt.reversal.approve"]);

/** A pinned profile/rate/tax badge, named by its own `ConfigVersion` id — the
 *  id a PT's lines actually carry (`price_line` stamps
 *  `profile_version_id`/`rate_version_id`/`tax_version_id` from
 *  `profile.version.pk`, `profile.rates_version.pk`, `profile.tax_version.pk`).
 *  There is no read keyed by a version id on its own (`/goods-v1/masters/configurations/{id}`
 *  looks a *draft* up, and a version's id is never its draft's), so the badge
 *  shows what it can honestly show without guessing at that id — the pinned
 *  version's own short id, which is exact and never wrong. */
function ConfigBadge({ id, label }: { id: string | null; label: string }) {
  if (!id) return null;
  return (
    <span className="chip chip-navy" data-testid={`pt-badge-${label.toLowerCase()}`}>
      {label} #{id.slice(0, 8)}
    </span>
  );
}

function ApprovalViewer({ approval, onDone }: { approval: ApprovalDTO; onDone: () => void }) {
  const { session } = useAuth();
  const humanId = session?.user.human_id ?? null;
  const canApprove = hold(session, approval.requested_action);
  // The approval names its own document (GSA-T04), so the screen opens exactly
  // that one. `subject_id` is the same value and stays as the fallback for an
  // approval whose parent could not be read.
  const documentId = approval.parent_document?.id ?? approval.subject_id;
  const pt = useResourceDoc<PtData>(`/goods-v1/ptmapper/files/${documentId}`);
  const stepUp = useStepUp();
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const [reversalReason, setReversalReason] = useState("");
  const [reissueReason, setReissueReason] = useState("");

  const ptDoc = pt.doc;
  const superseded = isSuperseded(
    approval,
    ptDoc
      ? { state: ptDoc.state, content_hash: ptDoc.content_hash, revision: ptDoc.revision }
      : null,
  );
  const selfApproval = isLikelySelfApproval(approval, humanId);
  const pinned = ptDoc ? pinnedVersions(ptDoc.data.lines.items) : null;
  const reconciliation = reconciliationView(approval.reconciliation);
  const history = ptDoc ? attemptHistory(ptDoc.data.history.items) : [];

  async function decide(decision: "approve" | "reject") {
    if (!ptDoc) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/approvals/${approval.id}/decide`, {
          decision,
          reviewed_hash: approval.reviewed_hash,
          ...(decision === "reject" ? { reason_code: "NOT_RIGHT" } : {}),
          // The revision the approval itself was submitted against, not one read
          // back from the PT at some later moment (GSA-T04).
          ...goodsMeta(approval.subject_revision),
        }),
      );
      setOk(decision === "approve" ? "Approved. Stock is valued and stays held." : "Sent back.");
      pt.reload();
      onDone();
    } catch (e) {
      if (apiErrorCode(e) === "SELF_APPROVAL") {
        setError("You cannot approve a PT you prepared or reviewed.");
      } else {
        setError(apiErrorMessage(e));
      }
    } finally {
      setBusy(false);
    }
  }

  async function requestReversal() {
    if (!ptDoc || !reversalReason) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      await api.post(`/goods-v1/ptmapper/files/${documentId}/reverse`, {
        reason_code: reversalReason,
        evidence_ids: [],
        ...goodsMeta(ptDoc.revision),
      });
      setOk("Reversal requested. It needs its own distinct approval.");
      pt.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function reissue() {
    if (!ptDoc || !reissueReason) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      await api.post(`/goods-v1/ptmapper/files/${documentId}/reissue`, {
        corrected: { header: ptDoc.data.header, lines: ptDoc.data.lines.items },
        reason_code: reissueReason,
        ...goodsMeta(ptDoc.revision),
      });
      setOk("Reissued as a new draft under the same number. Open it in Prepare PT to correct it.");
      pt.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (pt.denied) return <Denied what="PT" />;
  if (pt.loading && !ptDoc) return <p className="muted">Loading…</p>;
  if (pt.failure) return <div className="warn-note">{pt.failure}</div>;
  if (!ptDoc) return <Denied what="PT" />;

  return (
    <div className="card section-card pt-approval-viewer" data-testid="pt-approval-viewer">
      <div className="toolbar">
        <h3 className="h3">{approval.parent_document?.number ?? ptDoc.number ?? approval.title}</h3>
        <span className="chip chip-navy" data-testid="pt-approval-revision">
          Revision {approval.subject_revision}
        </span>
      </div>
      <p className="goods-hint" data-testid="pt-approval-subject">
        {/* The exact thing being decided, named by the approval itself rather
            than by whatever a later read of the PT happens to say (GSA-T04). */}
        Deciding{" "}
        {approval.parent_document
          ? `${approval.parent_document.purpose} document ${
              approval.parent_document.number ?? approval.parent_document.id.slice(0, 8)
            }`
          : approval.title}{" "}
        at revision {approval.subject_revision}.
      </p>

      {superseded && (
        <div className="warn-note" data-testid="pt-revision-superseded">
          <AlertTriangle size={14} /> REVISION_SUPERSEDED — this PT has changed since it was
          submitted.{" "}
          <Link to={ptWorkPath({ pt: documentId })} data-testid="pt-review-current">
            Review the current revision
          </Link>
        </div>
      )}

      <div className="pt-badges" data-testid="pt-approval-badges">
        <ConfigBadge id={pinned?.profileVersionId ?? null} label="Profile" />
        <ConfigBadge id={pinned?.rateVersionId ?? null} label="Rate" />
        <ConfigBadge id={pinned?.taxVersionId ?? null} label="Tax" />
      </div>

      <dl className="gr-facts">
        <div>
          <dt>Required authority</dt>
          <dd data-testid="pt-approval-roles">{approval.required_roles.join(", ") || "Unknown"}</dd>
        </div>
        <div>
          <dt>Quantity band</dt>
          <dd data-testid="pt-approval-qty">{reconciliation?.totalProposedQty ?? "Unknown"}</dd>
        </div>
        <div>
          <dt>Value band</dt>
          <dd data-testid="pt-approval-value">
            {formatPaiseString(reconciliation?.totalValuePaise ?? null)}
          </dd>
        </div>
      </dl>

      {reconciliation && !reconciliation.passed && (
        <div className="warn-note" data-testid="pt-approval-recon-failed">
          The reconciliation did not pass: {reconciliation.issues.map((i) => i.message).join("; ")}
        </div>
      )}

      <h4 className="gr-h4">
        <History size={15} /> Attempt and refusal history
      </h4>
      {history.length === 0 ? (
        <p className="muted">Nothing recorded yet.</p>
      ) : (
        <ol className="gr-history" data-testid="pt-approval-history">
          {history.map((entry) => (
            <li key={entry.id} data-testid={`pt-history-${entry.id}`}>
              <b>{entry.kind.replace(/_/g, " ")}</b> — {formatDateTime(entry.recorded_at)}
              {entry.outcome ? ` — ${entry.outcome}` : ""}
              {entry.reason_code ? ` (${entry.reason_code})` : ""}
            </li>
          ))}
        </ol>
      )}

      <Feedback error={error} ok={ok} />
      {ok.startsWith("Approved") && ptDoc && (
        // GSA-T06's own open item: "no goods-v1 stock-on-hand screen exists yet
        // to link 'valued, still held' to." Ticket 07 built the Stock screen
        // (`pages/GoodsStock.tsx`) with a stable `state` filter for exactly
        // this — held stock at the PT's own site.
        <Link
          to={`/goods/stock?site_id=${(ptDoc.context as { site_id?: string } | undefined)?.site_id ?? ""}&state=held`}
          data-testid="pt-view-held-stock"
        >
          View held stock at this site
        </Link>
      )}
      {stepUp.dialog}

      {approval.state === "pending" && (
        <div className="toolbar pt-approval-actions">
          {selfApproval ? (
            <p className="warn-note" data-testid="pt-self-approval">
              <ShieldAlert size={14} /> You cannot approve a PT you prepared or reviewed.
            </p>
          ) : (
            <button
              className="btn btn-cta"
              disabled={busy || !canApprove || superseded}
              onClick={() => decide("approve")}
              data-testid="pt-approve"
            >
              Approve
            </button>
          )}
          <button
            className="btn btn-sm"
            disabled={busy || !canApprove || superseded}
            onClick={() => decide("reject")}
            data-testid="pt-reject"
          >
            Send back
          </button>
        </div>
      )}

      {/* Both correction controls are offered on the server's word alone
          (GSA-T06). `allowed_actions` knows this PT's state, this reader's
          scope and its purpose; the route gate below is deliberately wider,
          because a preparer reaches this screen to read their own decided
          requests — so gating these on the route's own test would show a
          receipt checker a reissue, and a preparer a reversal, that each
          answer ACTION_DENIED. */}
      {ptDoc.allowed_actions.includes("reverse") && (
        <div className="pt-lifecycle-action">
          <p className="pt-hint">
            Reversal returns valued goods to unvalued custody and keeps the original count — it
            never renumbers or recreates the receipt. It needs its own distinct checker.
          </p>
          <input
            className="input"
            placeholder="Reason for reversal"
            value={reversalReason}
            onChange={(e) => setReversalReason(e.target.value)}
            aria-label="Reason for reversal"
            data-testid="pt-reverse-reason"
          />
          <button
            className="btn btn-sm"
            disabled={busy || !reversalReason}
            onClick={requestReversal}
            data-testid="pt-reverse-request"
          >
            Request reversal
          </button>
        </div>
      )}

      {ptDoc.allowed_actions.includes("reissue") && (
        <div className="pt-lifecycle-action">
          <p className="pt-hint">
            Reissue keeps this PT's number, starts a new version from its last frozen lines, and
            needs fresh review and a fresh, distinct approval before it values anything again.
          </p>
          <input
            className="input"
            placeholder="Reason for reissue"
            value={reissueReason}
            onChange={(e) => setReissueReason(e.target.value)}
            aria-label="Reason for reissue"
            data-testid="pt-reissue-reason"
          />
          <button
            className="btn btn-sm"
            disabled={busy || !reissueReason}
            onClick={reissue}
            data-testid="pt-reissue"
          >
            Reissue
          </button>
        </div>
      )}

      {ptDoc.state === "official" && (
        <div className="pt-lifecycle-action">
          <p className="pt-hint">
            Exports carry the frozen official values — never today's masters, rates or vocabulary —
            and each export writes its own audit record.
          </p>
          <div className="toolbar">
            <a
              className="btn btn-sm"
              href={`${api.defaults.baseURL}/ptmapper/files/${documentId}/export`}
              target="_blank"
              rel="noreferrer"
              data-testid="pt-export-json"
            >
              Export (frozen JSON)
            </a>
            <a
              className="btn btn-sm"
              href={`${api.defaults.baseURL}/ptmapper/files/${documentId}/export.xlsx`}
              data-testid="pt-export-xlsx"
            >
              Export (frozen XLSX)
            </a>
          </div>
        </div>
      )}

      {ptDoc.data.header.grn_id && (
        <Link
          className="btn btn-sm"
          to={ptWorkPath({ grn: ptDoc.data.header.grn_id })}
          data-testid="pt-prepare-supplement"
        >
          Prepare a supplement for this receipt
        </Link>
      )}
    </div>
  );
}

/** The PT approvals queue and the PT under review.
 *
 *  PT Work's To approve tab and the receiving workflow's PT-approval step both
 *  render this (OPS-17), so an Owner approving from the delivery they are looking at
 *  sees exactly the screen they would have seen from the menu - the same
 *  reconciliation, the same self-approval refusal, the same reviewed hash.
 *  `onlyPt` narrows both lists to one document; nothing else changes. */
export function PtApprovePanel({ onlyPt }: { onlyPt?: string }) {
  const { session } = useAuth();
  // A preparer reaches this screen too: reissuing a reversed receipt PT
  // (E131) is their own authority, not a checker's (design E131 "Granted
  // preparer; C-INV for opening"), and this is the only screen offering it.
  // Their own queue reads stay correctly scoped either way — E169 shows
  // nothing they cannot decide, and E170 already limits "recently decided"
  // to requests they made or may decide.
  const canView =
    hold(session, "pt.approve.receipt") ||
    hold(session, "pt.reversal.approve") ||
    hold(session, "pt.prepare") ||
    hold(session, "pt.prepare.opening");
  // Held directly, not derived from `queue.value`/`mine.value` membership: a
  // decide reloads both lists, and the item being viewed moves from one to
  // the other mid-flight — deriving "selected" from list membership would
  // make it briefly resolve to nothing between those two reloads, unmounting
  // the viewer (and the confirmation it just posted) before either finishes.
  const [selected, setSelected] = useState<ApprovalDTO | null>(null);
  const queue = useGoodsFetch<Page<ApprovalDTO>, ApprovalDTO[]>(
    "/goods-v1/approvals/inbox?limit=100",
    (r) => newestFirst((r.items ?? []).filter((row) => PT_ACTIONS.has(row.requested_action))),
    [],
  );
  // E170 (unlike E169) also answers a *maker's own* requests, decided or
  // still pending — the one place a preparer sees the request their own
  // submit opened, since E169's inbox is decider-only and would never offer
  // it to them at all. Not filtered to "not pending": a still-pending
  // request of their own belongs here too (self-approval shows the same
  // refusal it would once decided), so this reads "Your PT approvals", not
  // "Recently decided".
  const mine = useGoodsFetch<Page<ApprovalDTO>, ApprovalDTO[]>(
    "/goods-v1/approvals?limit=100",
    (r) => newestFirst((r.items ?? []).filter((row) => PT_ACTIONS.has(row.requested_action))),
    [],
  );

  // The workflow stands on one delivery, so it shows one delivery's approval.
  const mineFor = (rows: ApprovalDTO[]) =>
    onlyPt ? rows.filter((row) => (row.parent_document?.id ?? row.subject_id) === onlyPt) : rows;
  const waiting = mineFor(queue.value);
  const decided = mineFor(mine.value);

  useEffect(() => {
    if (!selected && waiting[0]) setSelected(waiting[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queue.value, selected]);

  // Once a decide reloads both lists, refresh the selected row's own content
  // (its `state`, in particular) from whichever list now carries it — same
  // id, so the viewer below never remounts, but a decided PT stops offering
  // the buttons for a decision it has already had.
  useEffect(() => {
    if (!selected) return;
    const fresh = [...queue.value, ...mine.value].find((row) => row.id === selected.id);
    if (fresh && fresh !== selected) setSelected(fresh);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queue.value, mine.value]);

  if (!canView) return <Denied what="PT approvals" />;
  if (queue.denied) return <Denied what="PT approvals" />;

  const state = listState(
    { loading: queue.loading, failure: queue.failure, empty: waiting.length === 0 },
    "Nothing is waiting for your decision.",
  );

  return (
    <div className="pt-layout pt-approvals-layout">
      <div className="pt-approvals-queue" data-testid="pt-approvals-queue">
        <h3 className="h3">
          <Clock size={15} /> Waiting for your decision
        </h3>
        {state ?? (
          <ul className="pt-queue-list">
            {waiting.map((row) => (
              <li key={row.id}>
                <button
                  className={`pt-queue-item${row.id === selected?.id ? " pt-queue-item-active" : ""}`}
                  onClick={() => setSelected(row)}
                  data-testid={`pt-queue-${row.id}`}
                >
                  <span>{row.title}</span>
                  <span className="muted">
                    {row.requested_at ? formatDateTime(row.requested_at) : "Unknown"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <h3 className="h3" style={{ marginTop: 18 }}>
          <CheckCircle2 size={15} /> Your PT approvals
        </h3>
        {decided.length === 0 ? (
          <p className="muted">Nothing yet.</p>
        ) : (
          <ul className="pt-queue-list">
            {decided.slice(0, 20).map((row) => (
              <li key={row.id}>
                <button
                  className={`pt-queue-item${row.id === selected?.id ? " pt-queue-item-active" : ""}`}
                  onClick={() => setSelected(row)}
                  data-testid={`pt-queue-decided-${row.id}`}
                >
                  <span>{row.title}</span>
                  <span className="muted">{row.state}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="pt-approvals-detail">
        {selected ? (
          <ApprovalViewer
            key={selected.id}
            approval={selected}
            onDone={() => {
              queue.reload();
              mine.reload();
            }}
          />
        ) : (
          <p className="muted">Pick a PT from the queue to review it.</p>
        )}
      </div>
    </div>
  );
}
