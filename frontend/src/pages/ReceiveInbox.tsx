// The receiving inbox (OPS-04; store and warehouse operations PRD §5.1).
//
// One list per site of everything on its way in - vendor deliveries and, once
// OPS-06 builds them, incoming transfer dispatches - each row saying which step
// it is waiting on and who owns that step. Two tabs: Pending is the work,
// History is what has been put away or closed.
//
// Since OPS-17 (PRD §5.1) this is the one way into receiving: **Goods arrived**
// on Pending starts a vendor delivery, and the separate arrivals, receipts, PT
// and acceptance screens are gone - the delivery workflow behind this list
// renders their panels as its steps. A row received against a booking says how
// much of that booking has been received, in the server's own figures. The
// records - arrival, count, GRN, discrepancy decisions, PT versions, approvals,
// print jobs, acceptance scans - stay exactly the documents they were.
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { PackagePlus, Truck } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { SearchBox } from "../components/SearchBox";
import { formatDateTime } from "../lib/format";
import { Field, hold, listState, useAllPages } from "../lib/goodsScreen";
import {
  STEP_HELP,
  STEP_LABEL,
  bookingProgress,
  ownerRoleName,
  type InboxItem,
} from "../lib/goodsReceiving";
import { AcceptPanel } from "./GoodsAccept";

/** A delivery's own address inside the workflow. One URL per record, so a row
 *  can be sent to somebody and opened where they left it. */
export function deliveryPath(item: InboxItem): string {
  const kind =
    item.kind === "transfer_dispatch"
      ? "transfer"
      : item.kind === "customer_return"
        ? "return"
        : "delivery";
  return `/goods/receive/${kind}/${encodeURIComponent(item.id)}`;
}

function matches(item: InboxItem, term: string): boolean {
  if (!term.trim()) return true;
  const hay = [
    item.reference,
    item.grn_number ?? "",
    item.pt_number ?? "",
    item.booking_number ?? "",
    STEP_LABEL[item.next_step],
  ]
    .join(" ")
    .toLowerCase();
  return hay.includes(term.trim().toLowerCase());
}

export function ReceiveInboxPage({ view = "pending" }: { view?: "pending" | "history" }) {
  const { session } = useAuth();
  const navigate = useNavigate();
  const sites = useMemo(() => session?.sites ?? [], [session]);
  const [siteId, setSiteId] = useState<string>(sites[0]?.id ?? "");
  const [term, setTerm] = useState("");

  const site = siteId || sites[0]?.id || "";
  const canReceive = hold(session, "receive.arrival");
  const inbox = useAllPages<InboxItem>(
    site ? `/goods-v1/inbound/inbox?site=${site}&view=${view}&limit=100` : null,
  );
  const rows = inbox.items.filter((item) => matches(item, term));

  const state = listState(
    { loading: inbox.loading, failure: inbox.failure, empty: rows.length === 0 },
    view === "history"
      ? "Nothing has been put away here yet."
      : "Nothing is on its way in to this site.",
  );

  return (
    <div className="page-pad">
      <PageHeader
        title={view === "history" ? "History" : "Pending"}
        lead={
          view === "history"
            ? "Deliveries that reached accepted stock or were closed, newest first."
            : "Everything on its way in to this site, and what each one is waiting on."
        }
        actions={
          view === "pending" && canReceive ? (
            // The one way to start receiving a vendor delivery (PRD §5.1).
            <Link
              className="btn btn-cta"
              to={`/goods/receive/new${site ? `?site=${encodeURIComponent(site)}` : ""}`}
              data-testid="inbox-goods-arrived"
            >
              <PackagePlus size={15} /> Goods arrived
            </Link>
          ) : undefined
        }
      />

      <div className="toolbar">
        {sites.length > 1 && (
          <Field id="inbox-site" label="Site">
            <select
              id="inbox-site"
              className="select"
              value={site}
              onChange={(e) => setSiteId(e.target.value)}
              data-testid="inbox-site"
            >
              {sites.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.code})
                </option>
              ))}
            </select>
          </Field>
        )}
        <div className="spacer" />
        <SearchBox
          value={term}
          onChange={setTerm}
          placeholder="Vendor, invoice, GRN or PT"
          label="Search this inbox"
          testId="inbox-search"
        />
      </div>

      {inbox.denied ? (
        <div className="card section-card" data-testid="inbox-denied">
          <p className="eyebrow">Not found</p>
          <h3 className="h3">There is no receiving inbox here for you</h3>
          <p className="lead">
            Either this site does not exist, or it is outside what you may see.
          </p>
        </div>
      ) : (
        (state ?? (
          <div className="table-wrap">
            <table className="data" data-testid="inbox-table">
              <caption className="sr-only">
                Deliveries on their way in to this site, each with the step it is waiting on and the
                role that owns it.
              </caption>
              <thead>
                <tr>
                  <th>What</th>
                  <th>Reference</th>
                  <th>Arrived</th>
                  <th>Next step</th>
                  <th>Waiting on</th>
                  <th>Records</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((item) => (
                  <tr
                    key={item.id}
                    data-testid={`inbox-row-${item.id}`}
                    onClick={() => navigate(deliveryPath(item))}
                    style={{ cursor: "pointer" }}
                  >
                    <td>
                      <span className="chip chip-navy">
                        {item.kind === "transfer_dispatch" ? (
                          <>
                            <Truck size={13} /> Transfer in
                          </>
                        ) : (
                          <>
                            <PackagePlus size={13} /> Vendor
                          </>
                        )}
                      </span>
                    </td>
                    <td>
                      <button
                        className="btn btn-sm"
                        onClick={(e) => {
                          e.stopPropagation();
                          navigate(deliveryPath(item));
                        }}
                        data-testid={`inbox-open-${item.id}`}
                      >
                        {item.reference}
                      </button>
                    </td>
                    <td>{formatDateTime(item.arrived_at)}</td>
                    <td data-testid={`inbox-step-${item.id}`}>
                      <span
                        className={`chip chip-${item.next_step === "done" ? "green" : "amber"}`}
                      >
                        {STEP_LABEL[item.next_step]}
                      </span>
                      <span className="muted"> {STEP_HELP[item.next_step]}</span>
                    </td>
                    <td data-testid={`inbox-owner-${item.id}`}>
                      {ownerRoleName(item.next_step_owner_role)}
                    </td>
                    <td className="muted">
                      {[item.grn_number, item.pt_number].filter(Boolean).join(" · ") || "—"}
                      {bookingProgress(item) && (
                        <div data-testid={`inbox-booking-${item.id}`}>{bookingProgress(item)}</div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}

      {/* Official PTs no delivery above carries - opening stock - are still put
          away from here, now that there is no Accept goods screen (OPS-17).
          Drawn only when there is something waiting. */}
      {view === "pending" && site && !inbox.loading && hold(session, "stock.accept") && (
        <AcceptPanel
          site={site}
          exceptPts={inbox.items.flatMap((item) => [
            ...(item.pt_ids ?? []),
            ...(item.pt_id ? [item.pt_id] : []),
          ])}
          heading="Opening stock waiting to be put away"
          quietWhenEmpty
        />
      )}
    </div>
  );
}

export function ReceiveHistoryPage() {
  return <ReceiveInboxPage view="history" />;
}

export default ReceiveInboxPage;
