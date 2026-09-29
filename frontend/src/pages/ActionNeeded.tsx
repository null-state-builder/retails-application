// Action Needed: everything waiting on you, on one screen (Anand, 23 Sep 2026).
//
// Home used to carry Approvals, Alerts and Exceptions as three lines. They are
// split now by one question - "do I have to do something?" - so the two lists
// that answer yes live here: approvals waiting for your decision, and goods
// exceptions waiting for their fix. Heads-ups that need nothing live on Alerts.
//
// Nothing is merged underneath. Each block keeps its own data, its own buttons
// and its own gate; this screen only puts them side by side. The old
// `/approvals` and `/goods/exceptions` addresses redirect here with `?show=`.
//
// Every item on it carries a topic chip and says, in plain words, what happened
// and what to do (`lib/actionCatalogue.ts`), for people who just need the
// work done (Anand, 23 Sep 2026).
import { useCallback, useState } from "react";
import { Navigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { holdsGoodsApprovals } from "../lib/actionCatalogue";
import { ApprovalsList } from "./Approvals";
import { canSeeExceptions as holdsExceptions, ExceptionsPanel } from "./GoodsExceptions";

type Show = "all" | "approvals" | "exceptions";

export function ActionNeededPage() {
  const { user, session } = useAuth();
  const [params, setParams] = useSearchParams();
  const [approvalCount, setApprovalCount] = useState<number | null>(null);
  const [exceptionCount, setExceptionCount] = useState<number | null>(null);
  const onApprovals = useCallback((n: number) => setApprovalCount(n), []);
  const onExceptions = useCallback((n: number) => setExceptionCount(n), []);

  // The exceptions screen's old notifications tab is on Alerts now.
  if (params.get("tab") === "alerts") return <Navigate to="/alerts" replace />;

  // Each block keeps the gate its old screen had: approvals behind Home, the
  // exceptions behind their goods grants. A person may hold either or both.
  // Someone who decides goods approvals sees their own goods inbox here too,
  // even without the older inbox (it only ever lists what they may decide).
  const canSeeLegacyApprovals =
    !!user?.is_superuser || (user?.capabilities?.home ?? "none") !== "none";
  const canSeeApprovals = canSeeLegacyApprovals || holdsGoodsApprovals(session);
  const canSeeExceptions = holdsExceptions(session);
  const asked = params.get("show");
  const show: Show =
    (asked === "approvals" && canSeeApprovals) || (asked === "exceptions" && canSeeExceptions)
      ? asked
      : "all";

  function choose(next: Show) {
    const p = new URLSearchParams(params);
    if (next === "all") p.delete("show");
    else p.set("show", next);
    setParams(p, { replace: true });
  }

  const chips: { key: Show; label: string; n?: number | null }[] = [
    { key: "all", label: "All" },
    ...(canSeeApprovals
      ? [{ key: "approvals" as const, label: "Approvals", n: approvalCount }]
      : []),
    ...(canSeeExceptions
      ? [{ key: "exceptions" as const, label: "Exceptions", n: exceptionCount }]
      : []),
  ];

  return (
    <div className="page-pad">
      <PageHeader lead="Your to-do list. Approve what's waiting for you, and fix what went wrong. News that needs nothing from you is on Alerts." />

      {chips.length > 2 && (
        <div className="toolbar" role="tablist" aria-label="What to show">
          {chips.map((c) => (
            <button
              key={c.key}
              type="button"
              role="tab"
              aria-selected={show === c.key}
              className={`btn btn-sm${show === c.key ? " btn-active" : ""}`}
              onClick={() => choose(c.key)}
              data-testid={`action-show-${c.key}`}
            >
              {c.label}
              {c.n != null ? ` (${c.n})` : ""}
            </button>
          ))}
        </div>
      )}

      {/* Both blocks stay mounted, only hidden, so each chip keeps its count. */}
      {canSeeApprovals && (
        <section data-testid="action-approvals" hidden={show === "exceptions"}>
          <div className="an-section-head">
            <h2 className="h3">Approvals</h2>
            <span className="an-section-hint">Waiting for your yes or no</span>
          </div>
          <ApprovalsList onCount={onApprovals} legacy={canSeeLegacyApprovals} />
        </section>
      )}

      {canSeeExceptions && session && (
        <section
          data-testid="action-exceptions"
          hidden={show === "approvals"}
          className={show === "all" && canSeeApprovals ? "an-section" : undefined}
        >
          <div className="an-section-head">
            <h2 className="h3">Exceptions</h2>
            <span className="an-section-hint">Problems to fix. Each card says what to do.</span>
          </div>
          <ExceptionsPanel session={session} onCount={onExceptions} />
        </section>
      )}
    </div>
  );
}

export default ActionNeededPage;
