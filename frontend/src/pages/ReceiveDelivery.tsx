// One delivery, walked end to end (OPS-04; store and warehouse operations PRD
// §5.1): arrival → count → GRN → discrepancies → PT → labels → accept and put
// away, with a rail saying which steps are behind it and which one it is on.
//
// "Folding the steps into one workflow does not merge their records." That is
// the whole design of this file: every step renders the panel its record has
// always had - `ArrivalPanel`, `GrnPanel`, `DispositionPanel`, `GrnStart`/
// `PtEditor`, `PtApprovePanel`, `LabelsPanel`, `AcceptPanel` - so the arrival,
// the count, the GRN, the discrepancy decisions, the PT versions, the
// approvals, the print jobs and the acceptance scans stay the documents they
// already were, with the same approvals, the same refusals and the same
// evidence. Nothing here writes anything of its own; it is a way in, and the
// server decides the rest.
//
// Since OPS-17 those panels have no screens of their own, so this is where a
// link to one of their records lands: `/goods/receive/grn/<id>` and
// `/goods/receive/pt/<id>` open the delivery that GRN or PT belongs to, and
// `?step=` opens it at the step the link is about. **Goods arrived**
// (`/goods/receive/new`) is the one way to start a vendor delivery.
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, Check, RefreshCw } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { formatDateTime } from "../lib/format";
import { Denied, hold, useGoodsFetch, type Page } from "../lib/goodsScreen";
import {
  RECEIVING_STEPS,
  STEP_HELP,
  STEP_LABEL,
  bookingProgress,
  deliveryStepPath,
  isReceivingStep,
  ownerRoleName,
  stepIsDone,
  stepIsReachable,
  type InboxItem,
  type ReceivingStep,
} from "../lib/goodsReceiving";
import { ArrivalPanel, NewArrival } from "./GoodsArrivals";
import { DispositionPanel, GrnPanel } from "./GoodsReceipts";
import { GrnStart, PtEditor } from "./PtPrepare";
import { PtApprovePanel } from "./PtApprovals";
import { LabelsPanel } from "./GoodsLabels";
import { AcceptPanel } from "./GoodsAccept";

/** The rail never shows "done" as a step of its own: it is the end, not a
 *  thing anybody opens. */
const RAIL: ReceivingStep[] = RECEIVING_STEPS.filter((s) => s !== "done") as ReceivingStep[];

function StepRail({
  item,
  showing,
  onShow,
}: {
  item: InboxItem;
  showing: ReceivingStep;
  onShow: (step: ReceivingStep) => void;
}) {
  return (
    <div className="page-tabs" data-testid="delivery-rail">
      {RAIL.map((step) => {
        const done = stepIsDone(item, step);
        const reachable = stepIsReachable(item, step);
        return (
          <button
            key={step}
            className={`page-tab ${step === showing ? "active" : ""}`}
            disabled={!reachable}
            aria-current={step === showing ? "page" : undefined}
            title={STEP_HELP[step]}
            onClick={() => onShow(step)}
            data-testid={`delivery-step-${step}`}
          >
            {done && <Check size={13} />} {STEP_LABEL[step]}
          </button>
        );
      })}
    </div>
  );
}

/** The step the person is looking at, drawn by the screen that owns it.
 *
 *  A step whose record does not exist yet says so rather than pretending: the
 *  PT step of a delivery with no GRN has nothing to prepare from, and saying
 *  "the GRN comes first" is more use than an empty form. */
function StepPanel({ item, step }: { item: InboxItem; step: ReceivingStep }) {
  const { session } = useAuth();
  const [freshPt, setFreshPt] = useState<string | null>(null);
  const ptId = freshPt ?? item.pt_id;
  const canPrepare = hold(session, "pt.prepare");

  if (item.kind === "transfer_dispatch") {
    // OPS-06 builds the dispatch records and their own steps. Until it does,
    // the inbox can list one but has nothing to open.
    return (
      <p className="muted" data-testid="delivery-transfer-pending">
        Incoming transfer dispatches are not yet opened here.
      </p>
    );
  }

  const arrivalId = item.arrival_id;
  if (!arrivalId) return <Denied what="delivery" />;

  switch (step) {
    case "arrival":
      return <ArrivalPanel arrivalId={arrivalId} section="arrival" />;
    case "count":
      return <ArrivalPanel arrivalId={arrivalId} section="count" />;
    case "grn":
      return item.grn_id ? (
        <GrnPanel grnId={item.grn_id} />
      ) : (
        <p className="muted">The count has to issue a goods receipt before there is one to read.</p>
      );
    case "discrepancies":
      return item.grn_id ? (
        <DispositionPanel grnId={item.grn_id} />
      ) : (
        <p className="muted">Nothing can be held until the goods receipt exists.</p>
      );
    case "pt_prepare":
      if (!item.grn_id) return <p className="muted">A PT is prepared from a goods receipt.</p>;
      if (ptId) return <PtEditor ptId={ptId} />;
      // A store person holds no `pt.prepare` (the Store Person role does not
      // carry it), so a delivery straight to the store waits on the warehouse
      // to prepare its PT - store and warehouse operations PRD §5.2. Saying so
      // is more use than a form the server would refuse every write from.
      return canPrepare ? (
        <GrnStart grnId={item.grn_id} onCreated={setFreshPt} />
      ) : (
        <p className="muted" data-testid="delivery-pt-elsewhere">
          PT prepared by the warehouse. The goods stay here and stay unsellable until the warehouse
          has prepared a PT for this receipt and the Owner has approved it.
        </p>
      );
    case "pt_approve":
      return ptId ? (
        <PtApprovePanel onlyPt={ptId} />
      ) : (
        <p className="muted">There is no PT to approve yet.</p>
      );
    case "labels":
      return ptId ? (
        <LabelsPanel fixed={ptId} />
      ) : (
        <p className="muted">Labels are printed from an approved PT's frozen values.</p>
      );
    case "accept": {
      // Every PT of this delivery - the primary and any supplement (ticket 07B) -
      // so the extra pieces a supplement covers are accepted from the same place.
      const pts = [...new Set([...(item.pt_ids ?? []), ...(ptId ? [ptId] : [])])];
      return pts.length > 0 ? (
        <AcceptPanel onlyPts={pts} site={item.site_id} />
      ) : (
        <p className="muted">Goods are accepted against an approved PT.</p>
      );
    }
    default:
      return <p className="muted">{STEP_HELP.done}</p>;
  }
}

/** The inbox's own name for each record a delivery can be addressed by. */
const RECORD_FILTER: Record<string, string> = { delivery: "arrival", grn: "grn", pt: "pt" };

/** The inbox read that finds this one delivery, in one tab.
 *
 *  Read through the inbox, not through a second endpoint: whatever the list
 *  says a delivery is waiting on is what this page opens, and the two can never
 *  disagree about the step or about who may see the delivery at all. A delivery,
 *  a GRN or a PT narrows the read to its one delivery on the server; anything
 *  else (a transfer dispatch, a customer return) is found by its row id. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The inbox filter a record address narrows by, or null to find the row by id
 *  (a customer return's row id is not an arrival's). */
function recordFilter(kind: string, id: string): string | null {
  return UUID.test(id) ? (RECORD_FILTER[kind] ?? null) : null;
}

function inboxUrl(kind: string, id: string, view: "pending" | "history"): string {
  const filter = recordFilter(kind, id);
  const query = new URLSearchParams({ view, limit: "100" });
  if (filter) query.set(filter, id);
  return `/goods-v1/inbound/inbox?${query.toString()}`;
}

/** One delivery's page, drawn afresh for each address: a link from one step to
 *  another record of the same delivery (the Count step's "Open the GRN") keeps
 *  the route but must not keep the step the person had open before it. */
export function ReceiveDeliveryPage() {
  const { kind = "delivery", id = "" } = useParams();
  const [params] = useSearchParams();
  return <DeliveryPage key={`${kind}/${id}/${params.get("step") ?? ""}`} />;
}

function DeliveryPage() {
  const { kind = "delivery", id = "" } = useParams();
  const [params] = useSearchParams();
  const asked = params.get("step");
  const [showing, setShowing] = useState<ReceivingStep | null>(null);

  const pick = (rows: InboxItem[]) =>
    (recordFilter(kind, id) ? rows[0] : rows.find((row) => row.id === id)) ?? null;
  const found = useGoodsFetch<Page<InboxItem>, InboxItem | null>(
    inboxUrl(kind, id, "pending"),
    (r) => pick(r.items ?? []),
    null,
  );
  const history = useGoodsFetch<Page<InboxItem>, InboxItem | null>(
    inboxUrl(kind, id, "history"),
    (r) => pick(r.items ?? []),
    null,
  );
  const item = found.value ?? history.value;

  if (found.denied) return <Denied what="delivery" />;
  if ((found.loading || history.loading) && !item)
    return <p className="page-pad muted">Loading…</p>;
  if (found.failure) return <div className="page-pad warn-note">{found.failure}</div>;
  if (!item) {
    // An official PT that belongs to no delivery - opening stock - is still put
    // away from Receive Goods: its acceptance link opens its acceptance alone.
    // So is an approved found-stock adjustment (goods ticket 15A), whose
    // acceptance-remaining work links here the same way.
    if (kind === "pt" && asked === "accept") {
      return (
        <div className="page-pad">
          <PageHeader
            title="Put away"
            lead="This record belongs to no delivery in the inbox - opening stock, or found stock recorded by an adjustment - so only its acceptance is shown."
            actions={
              <Link className="btn btn-sm" to="/goods/receive" data-testid="delivery-back">
                <ArrowLeft size={14} /> Back to the inbox
              </Link>
            }
          />
          <div className="card section-card" data-testid="delivery-panel-pt">
            <AcceptPanel onlyPts={[id]} />
          </div>
        </div>
      );
    }
    return <Denied what="delivery" />;
  }

  // The step a person chose on the rail, else the one the link named, else the
  // one the delivery is waiting on - each only while it can actually be opened.
  const wanted = showing ?? (isReceivingStep(asked) ? asked : null);
  const step = wanted && stepIsReachable(item, wanted) ? wanted : item.next_step;
  const panelStep: ReceivingStep = step === "done" ? "accept" : step;
  const booking = bookingProgress(item);
  const recordKind = item.kind === "transfer_dispatch" ? "transfer" : "delivery";

  return (
    <div className="page-pad">
      <PageHeader
        title={item.reference}
        lead={
          <>
            Arrived {formatDateTime(item.arrived_at)}. Next step:{" "}
            <b>{STEP_LABEL[item.next_step]}</b> — {ownerRoleName(item.next_step_owner_role)}.
            {booking && (
              <>
                {" "}
                <span className="chip chip-navy" data-testid="delivery-booking">
                  {booking}
                </span>
              </>
            )}
          </>
        }
        actions={
          <>
            <button
              className="btn btn-sm"
              onClick={() => {
                found.reload();
                history.reload();
              }}
              title="Read the delivery again, after a step has been done"
              data-testid="delivery-refresh"
            >
              <RefreshCw size={14} /> Refresh
            </button>
            <Link className="btn btn-sm" to="/goods/receive" data-testid="delivery-back">
              <ArrowLeft size={14} /> Back to the inbox
            </Link>
          </>
        }
      />
      <StepRail item={item} showing={panelStep} onShow={setShowing} />
      <p className="lead" data-testid="delivery-step-help">
        {STEP_HELP[step]}
      </p>
      {/* The record's own kind, whichever address opened it; the record itself
          says which it is. */}
      <div className="card section-card" data-testid={`delivery-panel-${recordKind}`}>
        <StepPanel item={item} step={panelStep} />
      </div>
    </div>
  );
}

/** **Goods arrived** (OPS-17, store and warehouse operations PRD §5.1): the one
 *  way to start receiving a vendor delivery. It records the arrival and opens
 *  the new delivery at its Arrival step. From a booking's **Receive against
 *  this booking** it opens with that booking already chosen (`?booking=`), and
 *  from the inbox with the site the person was looking at (`?site=`). */
export function ReceiveNewPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { session } = useAuth();
  if (!hold(session, "receive.arrival")) return <Denied what="way to record an arrival" />;
  return (
    <div className="page-pad">
      <PageHeader
        title="Goods arrived"
        lead="Record what turned up. The delivery then walks its steps from the inbox."
        actions={
          <Link className="btn btn-sm" to="/goods/receive" data-testid="delivery-back">
            <ArrowLeft size={14} /> Back to the inbox
          </Link>
        }
      />
      <div className="card section-card">
        <NewArrival
          initialSiteId={params.get("site") ?? ""}
          initialBookingId={params.get("booking") ?? ""}
          onSaved={(arrivalId) => navigate(deliveryStepPath("delivery", arrivalId, "arrival"))}
          onClose={() => navigate("/goods/receive")}
        />
      </div>
    </div>
  );
}

export default ReceiveDeliveryPage;
