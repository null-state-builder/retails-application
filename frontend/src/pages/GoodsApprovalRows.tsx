// The goods approvals inbox (E169) as Action Needed draws it (23 Sep 2026): the
// fetch and one row. Kept apart from `Approvals.tsx`, which speaks the older
// approvals contract, so each file talks to one contract only
// (`lib/goodsNamespace.test.ts`); the inbox page composes the two.
//
// A goods approval is reviewed and decided on its own screen - the checker has
// to see the exact submitted revision, with step-up - so its row says "Review"
// and goes there, never deciding anything here.
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { TopicChip } from "../components/ActionBits";
import { fmtApprovalWhenShort } from "../components/approval";
import { goodsApprovalView, holdsGoodsApprovals } from "../lib/actionCatalogue";
import { useGoodsFetch, type Page } from "../lib/goodsScreen";
import type { ApprovalDTO } from "../lib/goodsPt";

export type { ApprovalDTO };

/** Pending goods approvals this person may decide, never their own. Asked only
 *  by someone who may decide one at all, so no other session sends a request
 *  that can only be refused. */
export function useGoodsApprovalInbox() {
  const { session } = useAuth();
  return useGoodsFetch<Page<ApprovalDTO>, ApprovalDTO[]>(
    holdsGoodsApprovals(session) ? "/goods-v1/approvals/inbox?limit=100" : null,
    (r) => r.items ?? [],
    [],
  );
}

export function GoodsApprovalRow({
  a,
  siteNames,
}: {
  a: ApprovalDTO;
  siteNames: ReadonlyMap<string, string>;
}) {
  const view = goodsApprovalView(a);
  const site = a.site_id ? (siteNames.get(a.site_id) ?? null) : null;
  return (
    <li className="an-approval" data-testid={`goods-approval-row-${a.id}`}>
      <div className="an-approval-main">
        <div className="an-approval-top">
          <TopicChip topic={view.topic} />
          <span className="an-approval-what">{view.label}</span>
        </div>
        <div>
          {view.number ? <span className="mono">{view.number}</span> : <span>{a.title}</span>}
          {site && <span className="an-note-where"> · {site}</span>}
        </div>
        <span className="an-meta">
          <span>Asked by {a.maker.name || "—"}</span>
          {a.requested_at && <span>{fmtApprovalWhenShort(a.requested_at)}</span>}
        </span>
      </div>
      <div className="an-approval-actions">
        <Link className="btn" to={view.to} data-testid={`goods-approval-review-${a.id}`}>
          Review <ArrowRight size={15} aria-hidden />
        </Link>
      </div>
    </li>
  );
}
