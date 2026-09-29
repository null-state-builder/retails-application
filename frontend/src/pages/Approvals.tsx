// The one approvals inbox for the whole system (#70), drawn on Action Needed
// beside the goods exceptions (23 Sep 2026). `/approvals` redirects there.
//
// "The senior person opens one screen each morning and clears it" — so this is
// deliberately a single flat list across every document family, not a tab per
// module. Since 23 Sep 2026 that includes the goods approvals too (receipt and
// opening PTs, opening stock, GRN decisions, stock release, settings, product
// list), which used to wait only on their own screens (Anand: "yes, list
// them"). Those are reviewed and decided where they live, so their row says
// "Review" and goes there (`GoodsApprovalRows.tsx`, which talks to the goods
// contract so this file never has to); the older families keep their inline
// Approve and Reject.
//
// Nothing here decides anything by itself: the server owns who may approve
// what, refuses self-approval, and demands a reason on reject.
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, ShieldCheck, XCircle } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { ListSearchBar } from "../components/SearchBox";
import { TopicChip } from "../components/ActionBits";
import { useList } from "../lib/hooks";
import { Money } from "../lib/format";
import { approvalTopic, goodsApprovalView } from "../lib/actionCatalogue";
import { GoodsApprovalRow, useGoodsApprovalInbox, type ApprovalDTO } from "./GoodsApprovalRows";
import {
  ApprovalSteps,
  approvalDocPath,
  fmtApprovalWhenShort,
  type ApprovalT,
} from "../components/approval";
import "./Booking.css";
import { announceApprovalsChanged } from "../shell/Notifications";

const NO_APPROVALS: ApprovalT[] = [];

/** One line of the merged inbox: an older-family approval decided here, or a
 *  goods approval reviewed on its own screen. */
type Entry = { at: string; legacy: ApprovalT } | { at: string; goods: ApprovalDTO };

function matches(q: string, ...words: (string | null | undefined)[]): boolean {
  const needle = q.trim().toLowerCase();
  return !needle || words.some((w) => (w ?? "").toLowerCase().includes(needle));
}

/** The inbox itself. `onCount` hears how many approvals wait on this person,
 *  for Action Needed's chips; a search narrows the list, not that count.
 *  `legacy` is false for someone who holds no older approvals inbox at all, so
 *  their session never asks for one it would be refused. */
export function ApprovalsList({
  onCount,
  legacy = true,
}: {
  onCount?: (n: number) => void;
  legacy?: boolean;
}) {
  const { session } = useAuth();
  const [q, setQ] = useState("");
  const older = useList<ApprovalT>(legacy ? "/approvals/inbox" : null);
  const data = legacy ? older.data : NO_APPROVALS;
  const loading = legacy && older.loading;
  const reload = older.reload;
  const goods = useGoodsApprovalInbox();
  useEffect(() => {
    if (!loading && !goods.loading) onCount?.(data.length + goods.value.length);
  }, [loading, goods.loading, data.length, goods.value.length, onCount]);

  const [rejecting, setRejecting] = useState<number | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState("");

  async function decide(id: number, action: "approve" | "reject", why = "") {
    setError("");
    setBusy(id);
    try {
      await api.post(`/approvals/${id}/decide`, { action, reason: why });
      setRejecting(null);
      setReason("");
      reload();
      announceApprovalsChanged();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  // Oldest first across both inboxes: what has waited longest is cleared first.
  const entries = useMemo<Entry[]>(() => {
    const all: Entry[] = [
      ...data
        .filter((a) => matches(q, a.kind_label, a.title, a.requested_by_name, a.store_name))
        .map((a) => ({ at: a.requested_at, legacy: a })),
      ...goods.value
        .filter((a) =>
          matches(
            q,
            goodsApprovalView(a).label,
            a.title,
            a.maker.name,
            a.parent_document?.number,
          ),
        )
        .map((a) => ({ at: a.requested_at ?? "", goods: a })),
    ];
    return all.sort((x, y) => new Date(x.at).getTime() - new Date(y.at).getTime());
  }, [data, goods.value, q]);

  const siteNames = useMemo(
    () => new Map((session?.sites ?? []).map((s) => [s.id, s.name])),
    [session],
  );
  const stillLoading = (loading && data.length === 0) || (goods.loading && goods.value.length === 0);

  return (
    <>
      {error && (
        <div className="login-error" style={{ maxWidth: 520 }} data-testid="approvals-error">
          {error}
        </div>
      )}

      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search approvals — document, type, who asked"
        label="Search approvals"
        testId="approvals-search"
        noun="approval"
        count={entries.length}
        loading={stillLoading}
      />

      {goods.failure && (
        <p className="an-quiet" data-testid="goods-approvals-error">
          Couldn't load the goods approvals just now. The rest of the list is up to date.
        </p>
      )}

      {stillLoading ? (
        <p className="lead">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="an-empty-line" data-testid="approvals-empty">
          <CheckCircle2 size={16} aria-hidden />{" "}
          {q ? `No approval matches “${q}”.` : "Nothing is waiting for your approval."}
        </p>
      ) : (
        <ul className="an-approvals" data-testid="approvals-table">
          {entries.map((entry) =>
            "legacy" in entry ? (
              <LegacyApproval
                key={`l-${entry.legacy.id}`}
                a={entry.legacy}
                rejecting={rejecting === entry.legacy.id}
                busy={busy === entry.legacy.id}
                reason={reason}
                onReason={setReason}
                onApprove={() => decide(entry.legacy.id, "approve")}
                onStartReject={() => {
                  setRejecting(entry.legacy.id);
                  setReason("");
                }}
                onConfirmReject={() => decide(entry.legacy.id, "reject", reason)}
                onCancelReject={() => {
                  setRejecting(null);
                  setReason("");
                }}
              />
            ) : (
              <GoodsApprovalRow key={`g-${entry.goods.id}`} a={entry.goods} siteNames={siteNames} />
            ),
          )}
        </ul>
      )}

      <p className="an-quiet" style={{ marginTop: 14 }}>
        <ShieldCheck size={14} aria-hidden /> Every decision is recorded on the document — made
        by, approved by, and when. Nobody approves their own request.
      </p>
    </>
  );
}

function LegacyApproval({
  a,
  rejecting,
  busy,
  reason,
  onReason,
  onApprove,
  onStartReject,
  onConfirmReject,
  onCancelReject,
}: {
  a: ApprovalT;
  rejecting: boolean;
  busy: boolean;
  reason: string;
  onReason: (value: string) => void;
  onApprove: () => void;
  onStartReject: () => void;
  onConfirmReject: () => void;
  onCancelReject: () => void;
}) {
  const path = approvalDocPath(a);
  const topic = approvalTopic(a.kind);
  return (
    <li className="an-approval" data-testid={`approval-row-${a.id}`}>
      <div className="an-approval-main">
        <div className="an-approval-top">
          {topic && <TopicChip topic={topic} />}
          <span className="an-approval-what">{a.kind_label}</span>
          {!!a.value_paise && (
            <span className="an-approval-value">
              <Money paise={a.value_paise} />
            </span>
          )}
        </div>
        <div>
          {path ? (
            <Link to={path} className="link-cell mono" data-testid={`approval-link-${a.id}`}>
              {a.title}
            </Link>
          ) : (
            <span className="mono">{a.title}</span>
          )}
          {a.store_name && <span className="an-note-where"> · {a.store_name}</span>}
        </div>
        <span className="an-meta">
          <span>Asked by {a.requested_by_name}</span>
          <span>{fmtApprovalWhenShort(a.requested_at)}</span>
        </span>
        {/* Where the family has a chain, the row says which rung it is on — an
            approver clearing a stock request needs to know whether they are the
            first pair of eyes or the last. Nothing renders for single steps. */}
        <ApprovalSteps steps={a.steps} compact />
      </div>
      <div className="an-approval-actions">
        {rejecting ? (
          <>
            <input
              className="input"
              autoFocus
              aria-label="Why are you rejecting it?"
              placeholder="Reason (required)"
              value={reason}
              onChange={(e) => onReason(e.target.value)}
              data-testid={`reject-reason-${a.id}`}
            />
            <button
              type="button"
              className="btn"
              disabled={!reason.trim() || busy}
              onClick={onConfirmReject}
              data-testid={`confirm-reject-${a.id}`}
            >
              Confirm reject
            </button>
            <button type="button" className="btn" onClick={onCancelReject}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="btn btn-cta"
              disabled={busy}
              onClick={onApprove}
              data-testid={`approve-${a.id}`}
            >
              <CheckCircle2 size={15} aria-hidden /> Approve
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={onStartReject}
              data-testid={`reject-${a.id}`}
            >
              <XCircle size={15} aria-hidden /> Reject
            </button>
          </>
        )}
      </div>
    </li>
  );
}
