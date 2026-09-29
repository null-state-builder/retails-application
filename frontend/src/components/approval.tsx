// The approval bits shared between the inbox screen and every document that
// needs a second person (#70). The shape mirrors ApprovalReadSerializer.
import { useState } from "react";
import { Link } from "react-router-dom";
import { Check, ChevronRight, RotateCcw } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";

export type ApprovalStatusT = "pending" | "approved" | "rejected" | "not_required";

/** One rung of a multi-step approval route (#172). Families with no route send
 *  an empty `steps` array, so nothing below renders for them. */
export interface ApprovalStepT {
  order: number;
  label: string;
  roles: string[];
  state: "done" | "current" | "waiting" | "rejected";
  decided_by_name: string;
  decided_at: string | null;
  /** Closed from a later step (the Operations Head approving directly) rather
   *  than cleared by its own approver — said plainly, never glossed over. */
  short_circuited: boolean;
}

export interface ApprovalT {
  id: number;
  kind: string;
  kind_label: string;
  title: string;
  object_id: number;
  store: number | null;
  store_code: string;
  store_name: string;
  value_paise: number;
  status: ApprovalStatusT;
  made_by: number;
  made_by_name: string;
  requested_by: number;
  requested_by_name: string;
  requested_at: string;
  decided_by: number | null;
  decided_by_name: string;
  decided_at: string | null;
  reason: string;
  /** Zero-based position in the route; 0 and meaningless where there is none. */
  current_step?: number;
  steps?: ApprovalStepT[];
}

// kind → the document's own page. The server never knows about client routes.
const KIND_ROUTE: Record<string, string> = {
  writeoff: "/stock-count/writeoffs",
  vflip: "/stock/vflips",
  adjustment: "/stock-count/adjustments",
  transfer: "/transfer",
  stock_request: "/transfer/requests",
  return_to_brand: "/return-to-brand",
  offer: "/offers",
  booking: "/booking",
  // Store operations ticket 38: the Owner can open the debit note before deciding.
  debit_note: "/money/debit-notes",
  // Ticket 42: the Owner opens the petty cash spend and its bill before deciding.
  petty_cash: "/money/petty-cash",
  // Ticket 39: the Owner reads how far a booking goes over open-to-buy.
  open_to_buy: "/booking/open-to-buy",
};

/** Kinds with no page of their own — the approver reads them where the work is.
 *  A gap closure means nothing apart from the gaps list it came from, and a
 *  damage flag means nothing apart from the quarantine it is asking to join. */
const KIND_LIST_ROUTE: Record<string, string> = {
  gap_closure: "/transfer/in-transit",
  damage: "/stock?view=quarantine",
};

export function approvalDocPath(a: Pick<ApprovalT, "kind" | "object_id">): string | null {
  const base = KIND_ROUTE[a.kind];
  if (base) return `${base}/${a.object_id}`;
  return KIND_LIST_ROUTE[a.kind] ?? null;
}

export function fmtApprovalWhen(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Same moment, minus the year — the inbox only ever holds live requests, and
 *  the full form wraps to three lines in a table cell on a narrow pane. */
export function fmtApprovalWhenShort(iso: string): string {
  return new Date(iso).toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

const STATUS_TONE: Record<string, string> = {
  pending: "amber",
  approved: "green",
  rejected: "red",
  not_required: "grey",
};

const STATUS_LABEL: Record<string, string> = {
  pending: "Waiting",
  approved: "Approved",
  rejected: "Rejected",
  not_required: "No approval needed",
};

/** Does this approval let the document post? Approved, or small enough that
 *  the policy never asked anyone (the count-variance tolerance). */
export function isCleared(approval: ApprovalT | null | undefined): boolean {
  return approval?.status === "approved" || approval?.status === "not_required";
}

const STEP_TONE: Record<ApprovalStepT["state"], string> = {
  done: "green",
  current: "amber",
  rejected: "red",
  waiting: "grey",
};

/** The route a request is walking, one chip per step (#172).
 *
 *  Renders nothing for the families that have no route, which is most of them —
 *  a single-step approval says everything it has to say in the maker/checker
 *  block above, and a one-chip "chain" would be noise.
 *
 *  A step closed from above says so ("closed by the Operations Head"): the
 *  manager who never saw it must never appear to have approved it. */
export function ApprovalSteps({
  steps,
  compact = false,
}: {
  steps: ApprovalStepT[] | undefined;
  /** Inbox rows sit in a table cell — names drop, only the rungs show. */
  compact?: boolean;
}) {
  if (!steps || steps.length < 2) return null;
  return (
    <div
      data-testid="approval-steps"
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: 6,
        marginTop: compact ? 4 : 8,
      }}
    >
      {steps.map((step, i) => (
        <span key={step.order} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {i > 0 && (
            <ChevronRight size={12} style={{ color: "var(--muted)" }} aria-hidden />
          )}
          <span
            className={`chip chip-${STEP_TONE[step.state]}`}
            style={{ fontSize: 11.5 }}
            data-testid={`approval-step-${step.order}`}
            title={
              step.state === "done"
                ? `${step.short_circuited ? "Closed" : "Cleared"} by ${step.decided_by_name}${
                    step.decided_at ? ` on ${fmtApprovalWhen(step.decided_at)}` : ""
                  }`
                : step.label
            }
          >
            {step.state === "done" && <Check size={11} aria-hidden />}
            {step.label}
            {!compact && step.state === "done" && step.decided_by_name && (
              <span style={{ opacity: 0.8 }}>
                {" · "}
                {step.short_circuited ? "closed by " : ""}
                {step.decided_by_name}
              </span>
            )}
          </span>
        </span>
      ))}
    </div>
  );
}

export function ApprovalPill({ status }: { status: string }) {
  return (
    <span className={`chip chip-${STATUS_TONE[status] ?? "grey"} status-pill`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

function decisionLine(a: ApprovalT) {
  if (a.status === "not_required") return a.reason;
  const verb = a.status === "approved" ? "Approved" : "Rejected";
  const when = a.decided_at ? ` on ${fmtApprovalWhen(a.decided_at)}` : "";
  const why = a.reason ? ` — ${a.reason}` : "";
  return `${verb} by ${a.decided_by_name || "—"}${when}${why}`;
}

/** The made-by / approved-by / when block every wired document shows, forever.
 *
 *  Also the only way out of a rejection: a refused draft can never post and the
 *  kernel has no draft → cancelled move, so without "ask again" the maker would
 *  have to abandon the document and retype it. Asking again raises a fresh
 *  request and leaves the refusal on the record below. */
export function ApprovalTrail({
  createdByName,
  createdAt,
  approval,
  history = [],
  askAgainPath,
}: {
  createdByName: string;
  createdAt?: string;
  approval: ApprovalT | null;
  history?: ApprovalT[];
  /** API path that re-raises the request. Omit where the user may not ask. */
  askAgainPath?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function askAgain() {
    setError("");
    setBusy(true);
    try {
      await api.post(askAgainPath!);
      window.location.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
      setBusy(false);
    }
  }

  return (
    <div className="card section-card" data-testid="approval-trail">
      <p className="eyebrow">Maker · Checker</p>
      <p className="lead" style={{ marginTop: 6 }}>
        Made by <b>{createdByName || "—"}</b>
        {createdAt ? ` on ${fmtApprovalWhen(createdAt)}` : ""}
      </p>
      {/* After a rejection someone else may be the one who asked again — the
          document's maker and the request's asker are then two people, and
          saying so is the whole point of this block. Compared by id, not by
          name: two colleagues can share a display name. */}
      {approval && approval.requested_by !== approval.made_by && (
        <p className="lead">
          Asked again by <b>{approval.requested_by_name}</b> on{" "}
          {fmtApprovalWhen(approval.requested_at)}
        </p>
      )}
      {approval ? (
        <p className="lead">
          {approval.status === "pending" ? (
            <>
              Waiting for approval by a second person — clear it from the{" "}
              <Link to="/action-needed?show=approvals">Action Needed</Link>.
            </>
          ) : (
            decisionLine(approval)
          )}
        </p>
      ) : (
        <p className="lead">No approval on record.</p>
      )}
      <ApprovalPill status={approval?.status ?? "pending"} />
      <ApprovalSteps steps={approval?.steps} />

      {approval?.status === "rejected" && askAgainPath && (
        <p className="lead" style={{ marginTop: 12 }}>
          <button type="button" className="btn" disabled={busy} onClick={askAgain} data-testid="ask-again">
            <RotateCcw size={15} /> {busy ? "Sending…" : "Ask again"}
          </button>
        </p>
      )}
      {error && <div className="login-error" data-testid="ask-again-error">{error}</div>}

      {history.length > 0 && (
        <div style={{ marginTop: 12 }} data-testid="approval-history">
          <p className="eyebrow">Earlier</p>
          {history.map((h) => (
            <p key={h.id} className="lead" style={{ color: "var(--muted)", fontSize: 13 }}>
              {decisionLine(h)}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
