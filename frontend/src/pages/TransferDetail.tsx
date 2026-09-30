// One transfer, and every step of it as an action on the record (OPS-06).
//
// The store and warehouse operations PRD §7 is explicit that *New transfer*,
// *Dispatch* and *Receive* are actions on the transfer rather than screens of
// their own, so this is where submitting, approving, dispatching, counting,
// accepting, sending a failed delivery back and cancelling the outstanding
// balance all live.
//
// Each action is drawn only when the server says this person may take it on
// this record right now (`allowed_actions`). That is a courtesy, not the
// boundary: every command re-checks the grant, the site, the state and - for
// approval - that the approver is not the drafter.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  BadgeCheck,
  Ban,
  ClipboardList,
  FileCheck,
  MapPinCheck,
  PackageCheck,
  PackagePlus,
  Printer,
  RotateCcw,
  ScanLine,
  Send,
  Truck,
  Undo2,
} from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { OperationsPage, OperationsTable } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { formatDateTime, formatPaiseString } from "../lib/format";
import {
  Denied,
  Feedback,
  Field,
  listState,
  useGoodsFetch,
  useStepUp,
  type Page,
} from "../lib/goodsScreen";
import {
  COUNT_CONDITIONS,
  CUSTODY_HELP,
  CUSTODY_LABEL,
  dispatchStateWords,
  documentLabel,
  documentPath,
  TRANSFER_STATE_HELP,
  TRANSFER_STATE_LABEL,
  SHORTAGE_STATE_LABEL,
  acceptProblem,
  acceptableOf,
  countLabels,
  countProblem,
  eligibleOrigins,
  ewayWords,
  originLabel,
  originsOf,
  outstandingNote,
  outstandingOf,
  isHeldCustody,
  prePtSource,
  quarantineChoices,
  reconciliationWords,
  returnProblem,
  returnWords,
  scanProblem,
  scannedShipment,
  shortageProblem,
  unclaimedOf,
  unproposedPairs,
  unreturnedOf,
  wrongProblem,
  type AcceptBody,
  type AcceptReturnedBody,
  type ArrivalBody,
  type CountBody,
  type CountCondition,
  type CountExcessEntry,
  type DispatchBody,
  type DispatchPreparation,
  type DispatchScanBody,
  type EwayBody,
  type OriginShare,
  type PrePtCustodyRow,
  type QuarantineStockRow,
  type ReturnBody,
  type ShortageBody,
  type ShortageDecisionBody,
  type ShortageResolution,
  type TransferCustody,
  type TransferDraftBody,
  type TransferDraftLine,
  type TransferLine,
  type TransferDetail,
  type TransferDispatchRow,
  type TransferEventRow,
} from "../lib/goodsTransfers";
import { CorrectivePanel, ExcessTable } from "./TransferExcess";

const TRANSFERS = "/goods-v1/outbound/transfers";

/** Where arrived goods may be put away (`goods_engine.TRANSFERABLE_KINDS`). */
const PUTAWAY_KINDS = ["floor", "backstore", "bin", "zone", "fixture"];

interface SiteRef {
  id: string;
  code: string;
  name: string;
}

function siteLabel(sites: SiteRef[], id: string | null): string {
  if (!id) return "—";
  const found = sites.find((site) => String(site.id) === String(id));
  return found ? `${found.name} (${found.code})` : `#${id}`;
}

// ---------------------------------------------------------------------------
// New transfer: choose what to send out of what this site can actually send
// ---------------------------------------------------------------------------

interface StockRow {
  sku_id: string | null;
  origin_id: string | null;
  description: string;
  transferable_qty: number;
  physical_qty?: number;
  cost_value_paise?: string | null;
  ticket_value_paise?: string | null;
}

interface DraftRow {
  /** The row's own key: its SKU, or for a pre-PT row its GRN line (13E). */
  key: string;
  sku_id: string;
  description: string;
  available: number;
  qty: string;
  /** Every eligible origin this item can be sent from. */
  origins: OriginShare[];
  /** The origins chosen by hand; none chosen means the oldest first. */
  chosen: string[];
  /** Goods ticket 13D: for a quarantine transfer, the conditions and holds
   *  the pieces stand under - they travel with them. */
  held?: string;
  /** Goods ticket 13E: a pre-PT row names the GRN line its goods were counted on. */
  grn?: { grn_id: string; grn_line_key: string };
}

/** How many a row may send: from the chosen origins, or from all of them. */
function sendable(row: DraftRow): number {
  if (row.chosen.length === 0) return row.available;
  return row.origins
    .filter((share) => share.origin_id && row.chosen.includes(share.origin_id))
    .reduce((total, share) => total + share.qty, 0);
}

/** Drafting a movement from the sending site's own transferable stock.
 *
 *  Deliberately not a SKU picker over the product master: what a person may
 *  send is what is standing here, accepted, unheld and unreserved, and that is
 *  exactly the list the stock read answers. A quantity above it is refused by
 *  the server too, but there is no reason to offer it. */
export function NewTransferPanel({
  sourceSiteId,
  onDone,
  onCancel,
}: {
  sourceSiteId: string;
  onDone: (transferId: string) => void;
  onCancel: () => void;
}) {
  // Where this site may send to is the server's answer, not the sites in this
  // person's session: a store person reads one site, their own, and a list
  // built from that offers nowhere to send (OPS-11). The server offers every
  // other site of the same company that can receive, by name and code only.
  const offered = useGoodsFetch<{ items: SiteRef[] }, SiteRef[]>(
    `${TRANSFERS}/destinations?source_site_id=${sourceSiteId}`,
    (r) => r.items ?? [],
    [],
  );
  const [picked, setDestination] = useState("");
  const destination = picked || offered.value[0]?.id || "";
  // Goods ticket 13D: which pool to send from. Each is closed to the other on
  // the server; the screen only asks the matching question.
  const [custody, setCustody] = useState<TransferCustody>("ordinary");
  const quarantined = custody === "quarantine";
  // Goods ticket 13E: damaged goods no PT covers yet, known only by their GRN.
  const prePt = custody === "pre_pt";
  const heldPool = isHeldCustody(custody);
  const [rows, setRows] = useState<DraftRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Each origin's frozen cost and MRP, for somebody allowed to see stock value
  // (goods ticket 13A). The cost basis carries both. A person whose cost grant
  // does not reach this site is refused that basis (FIELD_DENIED) and gets the
  // same list without values - never a broken form, never a zero.
  const [valued, setValued] = useState(false);
  const stock = useGoodsFetch<{ items: StockRow[] }, StockRow[]>(
    heldPool
      ? null
      : `/goods-v1/outbound/stock-search?source_site_id=${sourceSiteId}` +
          (valued ? "&basis=cost" : ""),
    (r) => (r.items ?? []).filter((row) => row.sku_id && row.transferable_qty > 0),
    [],
  );
  // The quarantine pool is the server's own answer - exactly what submitting
  // would freeze from - not a guess over the stock rows.
  const held = useGoodsFetch<{ items: QuarantineStockRow[] }, QuarantineStockRow[]>(
    quarantined ? `${TRANSFERS}/quarantine-stock?source_site_id=${sourceSiteId}` : null,
    (r) => r.items ?? [],
    [],
  );
  const custodyPool = useGoodsFetch<{ items: PrePtCustodyRow[] }, PrePtCustodyRow[]>(
    prePt ? `${TRANSFERS}/pre-pt-custody?source_site_id=${sourceSiteId}` : null,
    (r) => r.items ?? [],
    [],
  );
  useEffect(() => {
    if (stock.deniedField) setValued(false);
  }, [stock.deniedField]);

  useEffect(() => {
    setValued(false);
  }, [sourceSiteId]);

  useEffect(() => {
    if (!prePt) return;
    setRows(
      custodyPool.value.map((row) => ({
        key: `${row.grn_id}:${row.grn_line_key}`,
        sku_id: row.sku_id ?? "",
        description: row.description,
        available: row.qty,
        qty: "",
        origins: [],
        chosen: [],
        held: `${prePtSource({ grn_number: row.grn_number, grn_id: row.grn_id })} · held: ${row.hold_kinds.join(", ")}`,
        grn: { grn_id: row.grn_id, grn_line_key: row.grn_line_key },
      })),
    );
  }, [prePt, custodyPool.value]);

  useEffect(() => {
    if (!quarantined) return;
    setRows(
      quarantineChoices(held.value).map((choice) => ({
        key: choice.sku_id,
        sku_id: choice.sku_id,
        description: choice.description,
        available: choice.available,
        qty: "",
        origins: choice.origins,
        chosen: [],
        held: `${choice.conditions.join(", ")} · held: ${choice.hold_kinds.join(", ")}`,
      })),
    );
  }, [quarantined, held.value]);

  useEffect(() => {
    if (heldPool) return;
    // One line per SKU, whatever the stock search answers. The same SKU can
    // stand at a site in more than one lot - two opening loads, a receipt and
    // a transfer back - and the search reports each separately. A transfer
    // line names a SKU and a quantity and nothing else, so two boxes for one
    // item would be two lines the server reads as one, and asking for the sum
    // is the only reading that matches what the screen says: "can be sent".
    //
    // Each item still lists the origins its pieces came from (goods ticket
    // 13A): a person may pick which ones to send from, and the server keeps
    // that choice on the draft as evidence. Choosing none means the oldest
    // eligible pieces go first, as everywhere else.
    const origins = eligibleOrigins(stock.value);
    const merged = new Map<string, DraftRow>();
    for (const row of stock.value) {
      const key = String(row.sku_id);
      const seen = merged.get(key);
      if (seen) {
        seen.available += row.transferable_qty;
      } else {
        merged.set(key, {
          key,
          sku_id: key,
          description: row.description,
          available: row.transferable_qty,
          qty: "",
          origins: origins.get(key) ?? [],
          chosen: [],
        });
      }
    }
    setRows([...merged.values()]);
  }, [heldPool, stock.value]);

  const chosen = rows.filter((row) => Number(row.qty) > 0);
  const overdrawn = chosen.some((row) => Number(row.qty) > sendable(row));

  function toggleOrigin(index: number, originId: string) {
    setRows((current) =>
      current.map((row, i) =>
        i !== index
          ? row
          : {
              ...row,
              chosen: row.chosen.includes(originId)
                ? row.chosen.filter((id) => id !== originId)
                : [...row.chosen, originId],
            },
      ),
    );
  }

  async function create() {
    setBusy(true);
    setError("");
    try {
      const body: TransferDraftBody = {
        source_site_id: String(sourceSiteId),
        destination_site_id: String(destination),
        custody,
        lines: chosen.map(
          (row): TransferDraftLine =>
            row.grn
              ? {
                  line_key: crypto.randomUUID(),
                  grn_id: row.grn.grn_id,
                  grn_line_key: row.grn.grn_line_key,
                  qty: Number(row.qty),
                }
              : {
                  line_key: crypto.randomUUID(),
                  sku_id: row.sku_id,
                  qty: Number(row.qty),
                  ...(row.chosen.length > 0 ? { origin_ids: row.chosen } : {}),
                },
        ),
        ...goodsMeta(),
      };
      const { data } = await api.post<{ id: string }>(TRANSFERS, body);
      onDone(data.id);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const source = prePt ? custodyPool : quarantined ? held : stock;
  const listing = listState(
    { loading: source.loading, failure: source.failure, empty: rows.length === 0 },
    prePt
      ? "Nothing here is damaged and waiting for a PT: a transfer of pre-PT goods takes damaged goods a GRN counted here, held in quarantine, on no PT and reserved to nobody."
      : quarantined
        ? "Nothing here is quarantined and free to move: a quarantine transfer takes recorded goods held in this site's quarantine and reserved to nobody."
        : "There is nothing here that can be sent: transferable stock is accepted, unheld and unreserved.",
  );

  return (
    <section className="card section-card" data-testid="transfer-new-panel">
      <h3 className="h3">New transfer</h3>
      <p className="lead">
        Choose where the goods are going and how many of each to send. Drafting reserves nothing — a
        different person approves the movement, and that approval is what reserves the pieces.
      </p>
      {heldPool ? (
        <p className="warn-note" data-testid="transfer-new-quarantine-note">
          {CUSTODY_HELP[custody]}
        </p>
      ) : null}
      <Feedback error={error} ok="" />
      <div className="form-grid">
        <Field id="transfer-custody" label="What to send">
          <select
            id="transfer-custody"
            className="select"
            value={custody}
            onChange={(e) => {
              setCustody(e.target.value as TransferCustody);
              setRows([]);
            }}
            data-testid="transfer-new-custody"
          >
            <option value="ordinary">Stock that can be sent</option>
            <option value="quarantine">Quarantined goods (they stay held)</option>
            <option value="pre_pt">Damaged goods not yet on a PT (they stay held)</option>
          </select>
        </Field>
        <Field id="transfer-destination" label="Send to">
          <select
            id="transfer-destination"
            className="select"
            value={destination}
            onChange={(e) => setDestination(e.target.value)}
            data-testid="transfer-destination"
          >
            {offered.value.map((site) => (
              <option key={site.id} value={site.id}>
                {site.name} ({site.code})
              </option>
            ))}
          </select>
        </Field>
      </div>
      {!offered.loading && offered.value.length === 0 ? (
        <p className="muted" data-testid="transfer-destination-none">
          {offered.failure ||
            "There is nowhere this site can send to: no other site of this company can receive goods."}
        </p>
      ) : null}
      {!heldPool && (
        <label className="check-row" data-testid="transfer-show-values">
          <input
            type="checkbox"
            checked={valued}
            onChange={(event) => setValued(event.target.checked)}
          />
          Show cost and MRP for this site
        </label>
      )}
      {listing ?? (
        <OperationsTable label="Transfer draft lines">
          <table data-testid="transfer-new-lines">
            <thead>
              <tr>
                <th>Item</th>
                <th className="num">{heldPool ? "Held here" : "Can be sent"}</th>
                <th className="num">Send</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={row.key} data-testid="transfer-new-line">
                  <td>
                    {row.description}
                    {row.held ? (
                      <div className="muted" data-testid={`transfer-new-held-${row.key}`}>
                        {row.held}
                      </div>
                    ) : null}
                    {row.origins.length > 0 && (
                      <details data-testid={`transfer-new-origins-${row.key}`}>
                        <summary>
                          {row.chosen.length === 0
                            ? `From the oldest first (${row.origins.length} origin${
                                row.origins.length === 1 ? "" : "s"
                              })`
                            : `From ${row.chosen.length} chosen origin${
                                row.chosen.length === 1 ? "" : "s"
                              }`}
                        </summary>
                        {row.origins.map((share) => (
                          <label key={share.origin_id} className="origin-choice">
                            <input
                              type="checkbox"
                              checked={row.chosen.includes(share.origin_id ?? "")}
                              onChange={() => toggleOrigin(index, share.origin_id ?? "")}
                              data-testid={`transfer-new-origin-${share.origin_id}`}
                            />{" "}
                            {originLabel(share.origin_id)} — {share.qty} can be sent
                            <OriginValue share={share} />
                          </label>
                        ))}
                      </details>
                    )}
                  </td>
                  <td className="num" data-testid={`transfer-new-available-${row.key}`}>
                    {sendable(row)}
                  </td>
                  <td className="num">
                    <input
                      className="input"
                      type="number"
                      min={0}
                      max={sendable(row)}
                      value={row.qty}
                      aria-label={`Send how many of ${row.description}`}
                      onChange={(e) =>
                        setRows((current) =>
                          current.map((r, i) => (i === index ? { ...r, qty: e.target.value } : r)),
                        )
                      }
                      data-testid={`transfer-new-qty-${row.key}`}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </OperationsTable>
      )}
      <div className="toolbar">
        <button
          className="btn btn-cta"
          disabled={busy || !destination || chosen.length === 0 || overdrawn}
          onClick={create}
          data-testid="transfer-new-create"
        >
          Draft this transfer
        </button>
        <button className="btn btn-sm" onClick={onCancel} data-testid="transfer-new-cancel">
          Cancel
        </button>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// The record
// ---------------------------------------------------------------------------

export function TransferDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const { session } = useAuth();
  const sessionSites = useMemo<SiteRef[]>(() => session?.sites ?? [], [session]);
  const [ok, setOk] = useState("");
  const [error, setError] = useState("");
  const { guarded, dialog } = useStepUp();

  const read = useGoodsFetch<TransferDetail, TransferDetail | null>(
    id ? `${TRANSFERS}/${id}` : null,
    (r) => r,
    null,
  );
  const detail = read.value;
  // The session names only the sites this person may read; the transfer names
  // both of its own ends, so the far one is never a bare `#id` (OPS-11).
  const sites = useMemo<SiteRef[]>(
    () => (detail ? [...sessionSites, detail.source_site, detail.destination_site] : sessionSites),
    [sessionSites, detail],
  );

  function done(message: string) {
    setError("");
    setOk(message);
    read.reload();
  }

  function failed(e: unknown) {
    setOk("");
    setError(apiErrorMessage(e));
  }

  async function command(path: string, body: Record<string, unknown>, message: string) {
    try {
      const approval = path === "/approve" ? detail?.approval : null;
      await guarded(() =>
        api.post(`${TRANSFERS}/${id}${path}`, {
          ...body,
          ...goodsMeta(),
          ...(approval
            ? { reviewed_hash: approval.reviewed_hash, approval_revision: approval.revision }
            : {}),
        }),
      );
      done(message);
    } catch (e) {
      failed(e);
    }
  }

  if (read.denied) return <Denied what="transfer" />;
  if (read.loading || !detail) {
    return (
      <OperationsPage>
        <PageHeader title="Transfer" />
        <p className="muted">{read.failure || "Loading…"}</p>
      </OperationsPage>
    );
  }

  const may = (action: string) => detail.allowed_actions.includes(action);

  return (
    <OperationsPage>
      <PageHeader
        title={detail.number ?? "Transfer"}
        lead={`${siteLabel(sites, detail.source_site_id)} → ${siteLabel(
          sites,
          detail.destination_site_id,
        )}`}
        actions={
          <button className="btn btn-sm" onClick={() => navigate("/goods/transfers")}>
            All transfers
          </button>
        }
      />
      <Feedback error={error} ok={ok} />
      {dialog}

      <section className="card section-card" data-testid="transfer-summary">
        <p className="eyebrow" data-testid="transfer-state">
          {TRANSFER_STATE_LABEL[detail.state] ?? detail.state}
        </p>
        <p className="lead">{TRANSFER_STATE_HELP[detail.state]}</p>
        {isHeldCustody(detail.custody) ? (
          <p className="warn-note" data-testid="transfer-custody" data-custody={detail.custody}>
            <strong>{CUSTODY_LABEL[detail.custody]}.</strong> {CUSTODY_HELP[detail.custody]}
          </p>
        ) : null}
        <dl className="kv">
          <dt>Drafted by</dt>
          <dd>{detail.drafted_by.name || detail.drafted_by.id}</dd>
          <dt>Approved by</dt>
          <dd data-testid="transfer-approver">
            {detail.approved_by?.name ?? "Nobody yet — a different person has to"}
          </dd>
          <dt>Approved</dt>
          <dd data-testid="transfer-approved">{detail.approved_qty}</dd>
          <dt>Sent so far</dt>
          <dd data-testid="transfer-dispatched">{detail.dispatched_qty}</dd>
          <dt>Cancelled before leaving</dt>
          <dd data-testid="transfer-cancelled">{detail.cancelled_qty}</dd>
          <dt>Still reserved at the sender</dt>
          <dd data-testid="transfer-reserved">{detail.reserved_qty}</dd>
          <dt>On the road</dt>
          <dd data-testid="transfer-in-transit">{detail.in_transit_qty}</dd>
          <dt>Never arrived</dt>
          <dd data-testid="transfer-short">{detail.short_qty}</dd>
          <dt>Resolved as shortage</dt>
          <dd data-testid="transfer-shortage-resolved">{detail.shortage_resolved_qty}</dd>
          <dt>Came back to the sender</dt>
          <dd data-testid="transfer-returned">{detail.returned_qty}</dd>
          <dt>Arrived that nobody sent</dt>
          <dd data-testid="transfer-excess">{detail.excess_qty}</dd>
          <dt>Balance</dt>
          <dd data-testid="transfer-balance" data-balanced={String(detail.reconciliation.balanced)}>
            {detail.reconciliation.approved} approved = {detail.reconciliation.dispatched} sent +{" "}
            {detail.reconciliation.reserved} still reserved + {detail.reconciliation.cancelled}{" "}
            cancelled
          </dd>
        </dl>

        <CorrectivePanel detail={detail} onRun={command} />
        <div className="toolbar" data-testid="transfer-actions">
          {may("submit") && (
            <button
              className="btn btn-cta"
              onClick={() =>
                command(
                  "/submit",
                  {},
                  "Sent for approval. The exact pieces are frozen; nothing is reserved until somebody approves it.",
                )
              }
              data-testid="transfer-submit"
            >
              <Send size={14} /> Send for approval
            </button>
          )}
          {detail.approval && (
            <p className="muted" data-testid="transfer-approval-route">
              Review {detail.approval.completed_steps} of {detail.approval.total_steps} complete ·{" "}
              {detail.approval.current_label}
            </p>
          )}
          {may("approve") && <ApproveForm onRun={command} />}
          {may("cancel_outstanding") && <CancelForm onRun={command} />}
        </div>
      </section>

      <section className="card section-card" data-testid="transfer-lines">
        <h3 className="h3">What is being sent</h3>
        <OperationsTable label="Transfer lines">
          <table>
            <thead>
              <tr>
                <th>Item</th>
                <th>From</th>
                <th className="num">Approved</th>
                <th className="num">Still reserved</th>
              </tr>
            </thead>
            <tbody>
              {detail.lines.map((line) => (
                <tr key={line.line_key} data-testid="transfer-line">
                  <td>{line.description || line.sku_id || "Not identified"}</td>
                  <td>
                    {line.grn_id ? (
                      <Link
                        to={`/goods/receive/grn/${line.grn_id}?step=discrepancies`}
                        data-testid="transfer-line-grn"
                      >
                        {prePtSource(line)}
                      </Link>
                    ) : (
                      <LineOrigins line={line} />
                    )}
                  </td>
                  <td className="num">{line.qty}</td>
                  <td className="num" data-testid={`transfer-outstanding-${line.line_key}`}>
                    {outstandingOf(detail, line.line_key)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </OperationsTable>
        {may("dispatch") && (
          <PreparationPanel detail={detail} onDone={done} onFail={failed} onRun={command} />
        )}
      </section>

      <section className="card section-card" data-testid="transfer-dispatches">
        <h3 className="h3">Shipments</h3>
        <p className="lead">
          One approved movement may take several shipments. Each is received as one whole shipment;
          there is no staged partial receipt inside one of them.
        </p>
        {detail.dispatches.length === 0 ? (
          <p className="muted">Nothing has left yet.</p>
        ) : (
          detail.dispatches.map((record) => (
            <ShipmentCard
              key={record.id}
              detail={detail}
              record={record}
              sites={sites}
              onRun={command}
            />
          ))
        )}
      </section>

      <section className="card section-card" data-testid="transfer-history">
        <h3 className="h3">History</h3>
        <OperationsTable label="Transfer history">
          <table>
            <thead>
              <tr>
                <th>What happened</th>
                <th>Where</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {detail.events.map((event) => (
                <tr key={event.id} data-testid="transfer-event" data-kind={event.kind}>
                  <td>{transferEventWords(event)}</td>
                  <td>{siteLabel(sites, event.site_id)}</td>
                  <td>{formatDateTime(event.actual_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </OperationsTable>
      </section>
    </OperationsPage>
  );
}

/** The unit cost and MRP an origin was frozen at, when this reader may see
 *  stock value. Nothing at all otherwise: no label, no zero. */
function OriginValue({ share }: { share: OriginShare }) {
  if (share.unit_cost_paise === undefined && share.mrp_paise === undefined) return null;
  return (
    <span className="muted" data-testid="origin-value">
      {" "}
      · cost {formatPaiseString(share.unit_cost_paise)} · MRP {formatPaiseString(share.mrp_paise)}{" "}
      each
    </span>
  );
}

/** Where a line's pieces come from. Once the plan is frozen, one row per
 *  origin - separate origins are never merged (transfers PRD §4). Before
 *  that, the origins the drafter chose, or the oldest-first rule. */
function LineOrigins({ line }: { line: TransferLine }) {
  const frozen = originsOf(line);
  if (frozen.length > 0) {
    return (
      <ul className="origin-list" data-testid="transfer-line-origins">
        {frozen.map((share) => (
          <li key={share.origin_id ?? "none"} data-testid="transfer-line-origin">
            {originLabel(share.origin_id)}: {share.qty}
            <OriginValue share={share} />
          </li>
        ))}
      </ul>
    );
  }
  const picked = line.origin_ids ?? [];
  if (picked.length > 0) {
    return (
      <span data-testid="transfer-line-origins">
        {picked.length} chosen origin{picked.length === 1 ? "" : "s"}
      </span>
    );
  }
  return <span className="muted">Oldest first</span>;
}

/** One history row in words. Only the goods ticket 12B correction is spelled
 *  out: it changes what a reader expects of a counted shipment, so it has to
 *  say that the pieces are back waiting to be accepted. */
function transferEventWords(event: TransferEventRow): string {
  if (event.kind === "returned") {
    const qty = Number(event.details.quantity ?? 0);
    const good = Number(event.details.good ?? qty);
    const damaged = Number(event.details.damaged ?? 0);
    const still = Number(event.details.still_in_transit ?? 0);
    return (
      `Came back to the sender: ${qty} piece(s) (${good} good, ${damaged} damaged)` +
      (still > 0 ? `; ${still} still unaccounted for` : "")
    );
  }
  if (event.kind === "shortage_proposed") {
    return `Shortage proposed: ${Number(event.details.quantity ?? 0)} piece(s) of shipment ${Number(
      event.details.sequence_no ?? 0,
    )} - ${String(event.details.reason ?? "")}`;
  }
  if (event.kind === "shortage_resolved" || event.kind === "shortage_rejected") {
    const qty = Number(event.details.quantity ?? 0);
    const still = Number(event.details.still_in_transit ?? 0);
    return (
      (event.kind === "shortage_resolved"
        ? `Shortage approved (${String(event.details.gap_number ?? "")}): ${qty} piece(s) resolved`
        : `Shortage rejected: ${qty} piece(s) stay in transit`) +
      (still > 0 ? `; ${still} still in transit` : "")
    );
  }
  if (event.kind === "corrective_proposed") {
    return `Corrective transfer proposed by the sender for ${Number(
      event.details.quantity ?? 0,
    )} piece(s) of excess - ${String(event.details.reason ?? "")}`;
  }
  if (event.kind === "corrective_matched") {
    return `Corrective transfer ${String(event.details.corrective_number ?? "")} matched ${Number(
      event.details.quantity ?? 0,
    )} piece(s) of excess (${String(event.details.gap_number ?? "")})`;
  }
  if (event.kind === "corrective_withdrawn") {
    return `Corrective transfer withdrawn: ${Number(event.details.quantity ?? 0)} piece(s) of excess held again - ${String(
      event.details.reason ?? "",
    )}`;
  }
  if ((event.kind === "dispatch" || event.kind === "arrival") && event.details.corrective) {
    return `Corrective ${event.kind === "dispatch" ? "dispatch" : "arrival"} recorded with the confirmation: ${Number(
      event.details.quantity ?? 0,
    )} piece(s), matched to goods already at the destination`;
  }
  if (event.kind === "accept" && event.details.returned_goods) {
    return `Returned goods put away at the sender: ${Number(event.details.quantity ?? 0)} piece(s)`;
  }
  if (event.kind !== "damage_rejected") return event.kind;
  const qty = Number(event.details.quantity ?? 0);
  const waiting = Number(event.details.waiting_to_be_accepted ?? 0);
  return (
    `Damage report rejected: ${qty} piece(s) are not damaged after all` +
    (waiting > 0 ? `; ${waiting} wait in receiving to be accepted` : "")
  );
}

type Runner = (path: string, body: Record<string, unknown>, message: string) => Promise<void>;

function ApproveForm({ onRun }: { onRun: Runner }) {
  const [reason, setReason] = useState("");
  return (
    <div className="form-grid" data-testid="transfer-approve-form">
      <Field
        id="transfer-approve-reason"
        label="Note"
        hint="Approving reserves these exact pieces at the sending site. Whoever drafted or sent this transfer cannot approve it."
      >
        <input
          id="transfer-approve-reason"
          className="input"
          value={reason}
          aria-describedby="transfer-approve-reason-hint"
          onChange={(e) => setReason(e.target.value)}
          data-testid="transfer-approve-reason"
        />
      </Field>
      <button
        className="btn btn-cta"
        onClick={() =>
          onRun(
            "/approve",
            { reason },
            "Review recorded. Stock is reserved only after every independent route step approves.",
          )
        }
        data-testid="transfer-approve"
      >
        <BadgeCheck size={14} /> Approve
      </button>
    </div>
  );
}

function CancelForm({ onRun }: { onRun: Runner }) {
  const [reason, setReason] = useState("");
  return (
    <div className="form-grid" data-testid="transfer-cancel-form">
      <Field
        id="transfer-cancel-reason"
        label="Reason for cancelling the balance"
        hint="This releases only what has not been sent. A shipment already on the road still has to be accounted for."
      >
        <input
          id="transfer-cancel-reason"
          className="input"
          value={reason}
          aria-describedby="transfer-cancel-reason-hint"
          onChange={(e) => setReason(e.target.value)}
          data-testid="transfer-cancel-reason"
        />
      </Field>
      <button
        className="btn btn-sm"
        disabled={!reason}
        onClick={() =>
          onRun(
            "/cancel-outstanding",
            { reason },
            "The undispatched balance is released. Anything already sent is untouched.",
          )
        }
        data-testid="transfer-cancel-outstanding"
      >
        <Ban size={14} /> Cancel outstanding
      </button>
    </div>
  );
}

const DISPATCH_SESSIONS = "/goods-v1/outbound/dispatch-sessions";

/** Preparing one shipment by scanning it (goods ticket 13B; design E242-E244).
 *
 *  The scans are kept on the server, so whoever holds the dispatch grant here
 *  can pick the same shipment up on another device and carry on. Scanning
 *  moves nothing. The dispatch then carries exactly what was scanned - the
 *  whole of this shipment, which may be less than the whole transfer. */
function PreparationPanel({
  detail,
  onDone,
  onFail,
  onRun,
}: {
  detail: TransferDetail;
  onDone: (message: string) => void;
  onFail: (e: unknown) => void;
  onRun: Runner;
}) {
  const open = detail.dispatch_preparation;
  const read = useGoodsFetch<DispatchPreparation, DispatchPreparation | null>(
    open ? `${DISPATCH_SESSIONS}/${open.id}` : null,
    (r) => r,
    null,
  );
  const preparation = read.value;
  const [lineKey, setLineKey] = useState("");
  const [tag, setTag] = useState("");
  const [qty, setQty] = useState("1");
  const [originId, setOriginId] = useState("");
  const [reference, setReference] = useState("");
  const [eway, setEway] = useState("");
  const [busy, setBusy] = useState(false);
  const left = detail.lines.some((line) => outstandingOf(detail, line.line_key) > 0);

  const current = preparation?.lines.find((line) => line.line_key === lineKey);
  useEffect(() => {
    if (!lineKey && preparation?.lines[0]) setLineKey(preparation.lines[0].line_key);
  }, [preparation, lineKey]);

  async function step(run: () => Promise<unknown>, message: string) {
    setBusy(true);
    try {
      await run();
      onDone(message);
      read.reload();
    } catch (e) {
      onFail(e);
    } finally {
      setBusy(false);
    }
  }

  function start(replace: boolean) {
    return step(
      () =>
        api.post(`${TRANSFERS}/${detail.id}/dispatch-sessions`, {
          ...(replace ? { replace: true } : {}),
          ...goodsMeta(),
        }),
      replace
        ? "Started again. The earlier scans are kept, but they will not be sent."
        : "Ready to scan. Nothing moves until the shipment is dispatched.",
    );
  }

  function scan() {
    if (!preparation) return;
    const body: DispatchScanBody = {
      observations: [
        {
          scan_key: crypto.randomUUID(),
          line_key: lineKey,
          qty: Number(qty),
          alias_value: tag.trim(),
          ...(originId ? { origin_id: originId } : {}),
        },
      ],
      expected_revision: preparation.revision,
      ...goodsMeta(),
    };
    return step(
      () => api.post(`${DISPATCH_SESSIONS}/${preparation.id}/scan`, body),
      `Scanned ${qty}. Nothing has left yet.`,
    ).then(() => setTag(""));
  }

  if (!left) {
    return <p className="muted">Everything approved has already been sent.</p>;
  }

  if (!open) {
    return (
      <div className="form-grid" data-testid="transfer-dispatch-form">
        <h4 className="h4">Dispatch</h4>
        <p className="lead">
          Scan the pieces going on this shipment first. A shipment may be less than the whole
          transfer; whatever is not sent stays reserved until it is sent or cancelled.
        </p>
        <button
          className="btn btn-cta"
          disabled={busy}
          onClick={() => start(false)}
          data-testid="transfer-prepare"
        >
          <ScanLine size={14} /> Prepare a shipment
        </button>
      </div>
    );
  }

  if (!preparation) return <p className="muted">{read.failure || "Loading the shipment…"}</p>;

  const shipment = scannedShipment(preparation);
  const problem = scanProblem(current, Number(qty));
  const body: DispatchBody = {
    transport: {
      ...(reference ? { reference } : {}),
      ...(eway.trim() ? { eway_reference: eway.trim() } : {}),
    },
    lines: shipment,
    dispatch_session_id: preparation.id,
    dispatch_session_revision: preparation.revision,
    dispatch_session_hash: preparation.content_hash,
    ...goodsMeta(),
  };

  return (
    <div className="form-grid" data-testid="transfer-dispatch-form">
      <h4 className="h4">Dispatch</h4>
      <p className="lead" data-testid="prep-panel">
        Scanning this shipment since {formatDateTime(preparation.opened_at)} (
        {preparation.opened_by.name || preparation.opened_by.id}). The scans are kept, so anyone
        here can carry on from another device. Nothing moves until you dispatch.
      </p>
      <OperationsTable label="Dispatch preparation">
        <table data-testid="prep-lines">
          <thead>
            <tr>
              <th>Item</th>
              <th>Tag</th>
              <th className="num">Still reserved</th>
              <th className="num">Scanned</th>
            </tr>
          </thead>
          <tbody>
            {preparation.lines.map((line) => (
              <tr key={line.line_key} data-testid="prep-line">
                <td>{line.description || line.sku_id}</td>
                <td data-testid="prep-tags">{line.alias_values.join(", ") || "—"}</td>
                <td className="num">{line.reserved_qty}</td>
                <td className="num" data-testid={`prep-scanned-${line.line_key}`}>
                  {line.scanned_qty}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </OperationsTable>

      <div className="form-grid" data-testid="prep-scan-form">
        <Field id="prep-scan-line" label="Line">
          <select
            id="prep-scan-line"
            className="select"
            value={lineKey}
            onChange={(e) => {
              setLineKey(e.target.value);
              setOriginId("");
            }}
            data-testid="prep-scan-line"
          >
            {preparation.lines.map((line) => (
              <option key={line.line_key} value={line.line_key}>
                {line.description || line.sku_id}
              </option>
            ))}
          </select>
        </Field>
        <Field id="prep-scan-tag" label="Scanned tag">
          <input
            id="prep-scan-tag"
            className="input"
            value={tag}
            onChange={(e) => setTag(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && tag.trim() && !problem) void scan();
            }}
            data-testid="prep-scan-tag"
          />
        </Field>
        <Field id="prep-scan-qty" label="Pieces">
          <input
            id="prep-scan-qty"
            className="input"
            type="number"
            min={1}
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            data-testid="prep-scan-qty"
          />
        </Field>
        {current && current.origins.length > 1 && (
          <Field id="prep-scan-origin" label="From origin">
            <select
              id="prep-scan-origin"
              className="select"
              value={originId}
              onChange={(e) => setOriginId(e.target.value)}
              data-testid="prep-scan-origin"
            >
              <option value="">Oldest first</option>
              {current.origins.map((origin) => (
                <option key={origin.origin_id} value={origin.origin_id}>
                  {originLabel(origin.origin_id)} — {origin.reserved_qty} reserved
                </option>
              ))}
            </select>
          </Field>
        )}
        {problem && tag.trim() ? <div className="warn-note">{problem}</div> : null}
        <button
          className="btn btn-sm"
          disabled={busy || !tag.trim()}
          onClick={() => void scan()}
          data-testid="prep-scan-submit"
        >
          <ScanLine size={14} /> Record scan
        </button>
      </div>

      {preparation.scans.length > 0 && (
        <OperationsTable label="Scanned transfer pieces">
          <table data-testid="prep-scans">
            <thead>
              <tr>
                <th>Scanned</th>
                <th className="num">Pieces</th>
                <th>By</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {preparation.scans.map((row) => (
                <tr key={row.scan_key} data-testid="prep-scan-row">
                  <td>
                    {row.alias_value}
                    {row.origin_id ? ` · ${originLabel(row.origin_id)}` : ""}
                  </td>
                  <td className="num">{row.qty}</td>
                  <td>{row.scanned_by.name || row.scanned_by.id}</td>
                  <td>{formatDateTime(row.actual_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </OperationsTable>
      )}

      <Field id="transfer-dispatch-ref" label="Transport reference">
        <input
          id="transfer-dispatch-ref"
          className="input"
          value={reference}
          onChange={(e) => setReference(e.target.value)}
          data-testid="transfer-dispatch-ref"
        />
      </Field>
      <Field
        id="transfer-eway-ref"
        label="E-way bill reference"
        hint="Leave empty if there is none yet. The shipment is still recorded, and the missing bill stays open as owned work until a reference is attached."
      >
        <input
          id="transfer-eway-ref"
          className="input"
          value={eway}
          aria-describedby="transfer-eway-ref-hint"
          onChange={(e) => setEway(e.target.value)}
          data-testid="transfer-eway-ref"
        />
      </Field>
      <div className="toolbar">
        <button
          className="btn btn-cta"
          disabled={busy || shipment.length === 0}
          onClick={() =>
            onRun(
              "/dispatches",
              body,
              "Dispatched. Anything still reserved stays reserved until it is sent or cancelled. If the shipment has a document, print it from the shipment below before the goods leave.",
            ).then(() => read.reload())
          }
          data-testid="transfer-dispatch"
        >
          <Truck size={14} /> Dispatch the {preparation.scanned_qty} scanned
        </button>
        <button
          className="btn btn-sm"
          disabled={busy}
          onClick={() => void start(true)}
          data-testid="prep-restart"
        >
          <RotateCcw size={14} /> Start again
        </button>
      </div>
    </div>
  );
}

function ShipmentCard({
  detail,
  record,
  sites,
  onRun,
}: {
  detail: TransferDetail;
  record: TransferDispatchRow;
  sites: SiteRef[];
  onRun: Runner;
}) {
  const may = (action: string) => detail.allowed_actions.includes(action);
  const note = outstandingNote(record);
  return (
    <div
      className="card"
      data-testid={`shipment-${record.sequence_no}`}
      data-dispatch={record.id}
      data-state={record.state}
    >
      <p className="eyebrow" data-testid="shipment-state">
        Shipment {record.sequence_no} · {dispatchStateWords(record.state, detail.custody).label}
      </p>
      <p className="lead">{dispatchStateWords(record.state, detail.custody).help}</p>
      <dl className="kv">
        <dt>From → to</dt>
        <dd>
          {siteLabel(sites, record.source_site_id)} → {siteLabel(sites, record.destination_site_id)}
        </dd>
        <dt>Left</dt>
        <dd>{formatDateTime(record.dispatched_at)}</dd>
        <dt>Pieces</dt>
        <dd data-testid="shipment-qty">{record.quantity}</dd>
        <dt>On the road now</dt>
        <dd data-testid="dispatch-in-transit">{record.in_transit_qty}</dd>
        <dt>Recorded by</dt>
        <dd>{record.recorded_by.name || record.recorded_by.id}</dd>
        <dt>Arrived</dt>
        <dd data-testid="dispatch-arrived">
          {record.arrived_at
            ? `${formatDateTime(record.arrived_at)}${
                record.arrival_recorded_by
                  ? ` (${record.arrival_recorded_by.name || record.arrival_recorded_by.id})`
                  : ""
              }`
            : "Not recorded yet"}
        </dd>
        <dt>E-way bill</dt>
        <dd data-testid="dispatch-eway">{ewayWords(record.eway)}</dd>
        <dt>Document</dt>
        <dd data-testid="shipment-document">
          {record.document ? (
            <>
              {documentLabel(record.document.kind)} {record.document.number}{" "}
              <Link
                to={documentPath(detail.id, record.id)}
                className="btn btn-sm"
                data-testid="shipment-document-print"
              >
                <Printer size={14} /> Print
              </Link>
            </>
          ) : (
            "None from the system"
          )}
        </dd>
        {record.count ? (
          <>
            <dt>Counted at the destination</dt>
            <dd data-testid="shipment-count">
              {record.count.good_total} good, {record.count.held_total} held,{" "}
              {record.count.short_total} never arrived
            </dd>
            <dt>Accounted for</dt>
            <dd
              data-testid="shipment-reconciliation"
              data-balanced={String(record.reconciliation.balanced)}
            >
              {reconciliationWords(record.reconciliation)}
            </dd>
          </>
        ) : null}
        {record.return_reason ? (
          <>
            <dt>Came back because</dt>
            <dd data-testid="shipment-return-reason">{record.return_reason}</dd>
          </>
        ) : null}
        {record.returned_qty > 0 ? (
          <>
            <dt>Back at the sender</dt>
            <dd data-testid="shipment-returned">{record.returned_qty}</dd>
            <dt>Still unaccounted for</dt>
            <dd data-testid="shipment-unresolved">{record.in_transit_qty}</dd>
          </>
        ) : null}
      </dl>
      {record.returns.length > 0 ? (
        <OperationsTable label="Return receipts at the sender">
          <table data-testid={`shipment-returns-${record.sequence_no}`}>
            <caption>Return receipts at the sender</caption>
            <thead>
              <tr>
                <th>What came back</th>
                <th>Why</th>
                <th>Evidence</th>
                <th>Recorded by</th>
                <th>Back at</th>
              </tr>
            </thead>
            <tbody>
              {record.returns.map((receipt) => (
                <tr key={receipt.id} data-testid="shipment-return-receipt">
                  <td>{returnWords(receipt)}</td>
                  <td>{receipt.reason}</td>
                  <td>{receipt.evidence_reference ?? "—"}</td>
                  <td>{receipt.recorded_by.name || receipt.recorded_by.id}</td>
                  <td>{formatDateTime(receipt.returned_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </OperationsTable>
      ) : null}
      <ul className="origin-list" data-testid={`shipment-origins-${record.sequence_no}`}>
        {record.lines.flatMap((line) =>
          line.origins.map((share) => (
            <li key={`${line.line_key}-${share.origin_id ?? "none"}`}>
              {detail.lines.find((l) => l.line_key === line.line_key)?.description || line.sku_id} ·{" "}
              {share.origin_id ? (
                <Link to={`/goods/stock/origins/${share.origin_id}`}>
                  {originLabel(share.origin_id)}
                </Link>
              ) : (
                originLabel(null)
              )}
              : {share.qty}
            </li>
          )),
        )}
      </ul>
      {note ? <p className="muted">{note}</p> : null}
      {record.shortage_resolutions.length > 0 ? (
        <ShortageTable record={record} may={may} onRun={onRun} />
      ) : null}
      {record.excess_observations.length > 0 ? (
        <ExcessTable record={record} detail={detail} onRun={onRun} />
      ) : null}
      {may("propose_shortage") && record.shortage.unclaimed > 0 ? (
        <ShortageForm record={record} detail={detail} onRun={onRun} />
      ) : null}
      {record.state === "in_transit" && !record.arrived_at && may("record_arrival") ? (
        <ArrivalForm record={record} onRun={onRun} />
      ) : null}
      {may("attach_eway") || may("verify_eway") ? (
        <EwayForm record={record} may={may} onRun={onRun} />
      ) : null}
      {record.state === "in_transit" && may("count") ? (
        <CountForm record={record} detail={detail} onRun={onRun} />
      ) : null}
      {(record.state === "in_transit" || record.state === "partly_returned") &&
      may("return_to_source") ? (
        <ReturnForm record={record} detail={detail} onRun={onRun} />
      ) : null}
      {may("accept_returned") && record.lines.some((line) => line.returned_awaiting_putaway > 0) ? (
        <AcceptReturnedForm record={record} detail={detail} sites={sites} onRun={onRun} />
      ) : null}
      {record.state === "counted" && may("accept") ? (
        <AcceptForm record={record} detail={detail} sites={sites} onRun={onRun} />
      ) : null}
    </div>
  );
}

function CountForm({
  record,
  detail,
  onRun,
}: {
  record: TransferDispatchRow;
  detail: TransferDetail;
  onRun: Runner;
}) {
  const blank = Object.fromEntries(COUNT_CONDITIONS.map((name) => [name, 0])) as Record<
    CountCondition,
    number
  >;
  const [found, setFound] = useState<Record<string, Record<CountCondition, number>>>(
    Object.fromEntries(record.lines.map((line) => [line.line_key, { ...blank }])),
  );
  // Goods ticket 16: what came in place of each line's wrong pieces, and goods
  // nobody sent at all. Each becomes its own held, unvalued observation.
  const [instead, setInstead] = useState<Record<string, { description: string; alias: string }>>(
    {},
  );
  const [extras, setExtras] = useState<{ description: string; qty: string; sku: string }[]>([]);
  const itemOf = (lineKey: string) =>
    detail.lines.find((l) => l.line_key === lineKey)?.description ||
    record.lines.find((l) => l.line_key === lineKey)?.sku_id ||
    "";
  const excess: CountExcessEntry[] = [
    ...record.lines
      .filter((line) => (found[line.line_key]?.wrong ?? 0) > 0)
      .map((line) => {
        const said = instead[line.line_key] ?? { description: "", alias: "" };
        return {
          description: said.description.trim(),
          qty: found[line.line_key]?.wrong ?? 0,
          in_place_of_line_key: line.line_key,
          ...(said.alias.trim() ? { alias_value: said.alias.trim() } : {}),
        };
      }),
    ...extras
      .filter((extra) => extra.description.trim() && Number(extra.qty) > 0)
      .map((extra) => ({
        description: extra.description.trim(),
        qty: Number(extra.qty),
        ...(extra.sku ? { sku_id: extra.sku } : {}),
      })),
  ];
  const problems = [
    ...record.lines.map((line) => countProblem(found[line.line_key] ?? blank, line.qty)),
    ...record.lines.map((line) =>
      wrongProblem(found[line.line_key]?.wrong ?? 0, instead[line.line_key]?.description ?? ""),
    ),
  ].filter(Boolean);
  const quarantined = isHeldCustody(detail.custody);
  const { label, help } = countLabels(detail.custody);

  return (
    <div className="form-grid" data-testid={`count-form-${record.sequence_no}`}>
      <h4 className="h4">Count this shipment</h4>
      <p className="lead">
        {quarantined
          ? "A shipment is counted once, whole. These are quarantined goods: everything that arrived goes to quarantine here, keeping its condition and every hold. Anything fewer than was sent is a shortage that stays visible."
          : "A shipment is counted once, whole. Record what actually arrived: anything fewer than was sent is a shortage that stays visible, and damaged or unidentified pieces go to quarantine here and stay held. Wrong goods stay short: say what came instead, and it is held apart with anything else nobody sent."}
      </p>
      {record.lines.map((line) => (
        <OperationsTable label="Received transfer pieces" key={line.line_key}>
          <table key={line.line_key} data-testid="count-line">
            <caption>
              {detail.lines.find((l) => l.line_key === line.line_key)?.description || line.sku_id} —{" "}
              {line.qty} sent
            </caption>
            <thead>
              <tr>
                {COUNT_CONDITIONS.map((name) => (
                  <th key={name} className="num" title={help[name]}>
                    {label[name]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                {COUNT_CONDITIONS.map((name) => (
                  <td key={name} className="num">
                    <input
                      className="input"
                      type="number"
                      min={0}
                      max={line.qty}
                      value={found[line.line_key]?.[name] ?? 0}
                      aria-label={`${label[name]} pieces of ${line.sku_id}`}
                      onChange={(e) =>
                        setFound((current) => ({
                          ...current,
                          [line.line_key]: {
                            ...(current[line.line_key] ?? blank),
                            [name]: Number(e.target.value || 0),
                          },
                        }))
                      }
                      data-testid={`count-${record.sequence_no}-${name}`}
                    />
                  </td>
                ))}
              </tr>
            </tbody>
          </table>
        </OperationsTable>
      ))}
      {record.lines
        .filter((line) => (found[line.line_key]?.wrong ?? 0) > 0)
        .map((line) => (
          <div className="form-grid" key={`instead-${line.line_key}`} data-testid="count-instead">
            <Field
              id={`count-instead-${record.id}-${line.line_key}`}
              label={`What came instead of ${found[line.line_key]?.wrong ?? 0} ${itemOf(line.line_key)}`}
              hint="Those pieces stay short. What came is held apart, unvalued, until the sender corrects it."
            >
              <input
                id={`count-instead-${record.id}-${line.line_key}`}
                className="input"
                value={instead[line.line_key]?.description ?? ""}
                aria-describedby={`count-instead-${record.id}-${line.line_key}-hint`}
                onChange={(e) =>
                  setInstead((current) => ({
                    ...current,
                    [line.line_key]: {
                      alias: current[line.line_key]?.alias ?? "",
                      description: e.target.value,
                    },
                  }))
                }
                data-testid={`count-${record.sequence_no}-instead`}
              />
            </Field>
            <Field
              id={`count-instead-code-${record.id}-${line.line_key}`}
              label="Code on it (optional)"
            >
              <input
                id={`count-instead-code-${record.id}-${line.line_key}`}
                className="input"
                value={instead[line.line_key]?.alias ?? ""}
                onChange={(e) =>
                  setInstead((current) => ({
                    ...current,
                    [line.line_key]: {
                      description: current[line.line_key]?.description ?? "",
                      alias: e.target.value,
                    },
                  }))
                }
                data-testid={`count-${record.sequence_no}-instead-code`}
              />
            </Field>
          </div>
        ))}
      {extras.map((extra, index) => (
        <div className="form-grid" key={`extra-${index}`} data-testid="count-extra">
          <Field id={`count-extra-${record.id}-${index}`} label="Goods nobody sent">
            <input
              id={`count-extra-${record.id}-${index}`}
              className="input"
              value={extra.description}
              onChange={(e) =>
                setExtras((current) =>
                  current.map((row, at) =>
                    at === index ? { ...row, description: e.target.value } : row,
                  ),
                )
              }
              data-testid={`count-${record.sequence_no}-extra-description`}
            />
          </Field>
          <Field id={`count-extra-qty-${record.id}-${index}`} label="Pieces">
            <input
              id={`count-extra-qty-${record.id}-${index}`}
              className="input"
              type="number"
              min={1}
              value={extra.qty}
              onChange={(e) =>
                setExtras((current) =>
                  current.map((row, at) => (at === index ? { ...row, qty: e.target.value } : row)),
                )
              }
              data-testid={`count-${record.sequence_no}-extra-qty`}
            />
          </Field>
          <Field id={`count-extra-sku-${record.id}-${index}`} label="Which item it is">
            <select
              id={`count-extra-sku-${record.id}-${index}`}
              className="input"
              value={extra.sku}
              onChange={(e) =>
                setExtras((current) =>
                  current.map((row, at) => (at === index ? { ...row, sku: e.target.value } : row)),
                )
              }
              data-testid={`count-${record.sequence_no}-extra-sku`}
            >
              <option value="">Cannot tell</option>
              {record.lines
                .filter((line) => Boolean(line.sku_id))
                .map((line) => (
                  <option key={line.line_key} value={line.sku_id}>
                    More of {itemOf(line.line_key)}
                  </option>
                ))}
            </select>
          </Field>
        </div>
      ))}
      <button
        className="btn btn-sm"
        onClick={() => setExtras((current) => [...current, { description: "", qty: "1", sku: "" }])}
        data-testid={`count-${record.sequence_no}-add-extra`}
      >
        <PackagePlus size={14} /> Goods nobody sent
      </button>
      {problems.length > 0 && <div className="warn-note">{problems[0]}</div>}
      <button
        className="btn btn-cta"
        disabled={problems.length > 0}
        onClick={() => {
          const body: CountBody = {
            lines: record.lines.map((line) => ({
              line_key: line.line_key,
              ...(found[line.line_key] ?? blank),
            })),
            ...(excess.length > 0 ? { excess } : {}),
            ...goodsMeta(),
          };
          void onRun(
            `/dispatches/${record.id}/count`,
            body,
            quarantined
              ? "Counted. The goods are in quarantine here, still held; nothing became available."
              : "Counted. Good pieces are here but not yet put away, so they are not sellable yet.",
          );
        }}
        data-testid={`count-submit-${record.sequence_no}`}
      >
        <ClipboardList size={14} /> Record this count
      </button>
    </div>
  );
}

function AcceptForm({
  record,
  detail,
  sites,
  onRun,
}: {
  record: TransferDispatchRow;
  detail: TransferDetail;
  sites: SiteRef[];
  onRun: Runner;
}) {
  const [locationId, setLocationId] = useState("");
  // The same directory the Movements screen uses, filtered the same way: goods
  // are put away in an ordinary storage location, never in a protected system
  // one. The command refuses a system location whatever this picker offered.
  const locations = useGoodsFetch<
    Page<{ id: string; data: { name: string; kind: string; system: boolean } }>,
    { id: string; name: string }[]
  >(
    `/goods-v1/masters/stores/${detail.destination_site_id}/locations?limit=100`,
    (r) =>
      (r.items ?? [])
        .filter((row) => !row.data.system && PUTAWAY_KINDS.includes(row.data.kind))
        .map((row) => ({ id: row.id, name: row.data.name })),
    [],
  );
  useEffect(() => {
    if (!locationId && locations.value[0]) setLocationId(locations.value[0].id);
  }, [locations.value, locationId]);

  // How many of each line to put away now. Acceptance may take several goes:
  // a blank box means all that is waiting.
  const [asked, setAsked] = useState<Record<string, string>>({});
  const lines = record.lines
    .map((line) => {
      const good = acceptableOf(record, line.line_key);
      const raw = asked[line.line_key];
      const qty = raw === undefined || raw === "" ? good : Number(raw);
      return { line, good, qty, problem: acceptProblem(qty, good) };
    })
    .filter((row) => row.good > 0);
  const problem = lines.find((row) => row.problem)?.problem ?? "";
  const chosen = lines.filter((row) => row.qty > 0);

  if (lines.length === 0) {
    return <p className="muted">There are no good pieces from this shipment left to put away.</p>;
  }
  const body: AcceptBody = {
    lines: chosen.map((row) => ({
      line_key: row.line.line_key,
      qty: row.qty,
      destination_location_id: locationId,
    })),
    ...goodsMeta(),
  };

  return (
    <div className="form-grid" data-testid={`accept-form-${record.sequence_no}`}>
      <h4 className="h4">Accept and put away</h4>
      <p className="lead">
        Putting the goods away at {siteLabel(sites, detail.destination_site_id)} is what makes them
        sellable here. Their original cost and identity travel with them; nothing is re-valued.
      </p>
      <Field id={`accept-location-${record.id}`} label="Put away in">
        <select
          id={`accept-location-${record.id}`}
          className="select"
          value={locationId}
          onChange={(e) => setLocationId(e.target.value)}
          data-testid={`accept-location-${record.sequence_no}`}
        >
          {locations.value.map((location) => (
            <option key={location.id} value={location.id}>
              {location.name}
            </option>
          ))}
        </select>
      </Field>
      <OperationsTable label="Pieces to put away">
        <table data-testid={`accept-lines-${record.sequence_no}`}>
          <thead>
            <tr>
              <th>Item</th>
              <th className="num">Waiting</th>
              <th className="num">Put away now</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((row) => (
              <tr key={row.line.line_key}>
                <td>
                  {detail.lines.find((l) => l.line_key === row.line.line_key)?.description ||
                    row.line.sku_id}
                </td>
                <td className="num" data-testid={`accept-waiting-${record.sequence_no}`}>
                  {row.good}
                </td>
                <td className="num">
                  <input
                    className="input"
                    type="number"
                    min={0}
                    max={row.good}
                    value={asked[row.line.line_key] ?? String(row.good)}
                    aria-label={`Put away how many of ${row.line.sku_id}`}
                    onChange={(e) =>
                      setAsked((current) => ({ ...current, [row.line.line_key]: e.target.value }))
                    }
                    data-testid={`accept-qty-${record.sequence_no}`}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </OperationsTable>
      {problem && <div className="warn-note">{problem}</div>}
      <button
        className="btn btn-cta"
        disabled={!locationId || Boolean(problem) || chosen.length === 0}
        onClick={() => {
          void onRun(
            `/dispatches/${record.id}/accept`,
            body,
            "Put away. These pieces are sellable here now; anything held stays held.",
          ).then(() => setAsked({}));
        }}
        data-testid={`accept-submit-${record.sequence_no}`}
      >
        <PackageCheck size={14} /> Accept and put away
      </button>
    </div>
  );
}

/** The shipment is physically here. That is all this records: the pieces stay
 *  in transit - not stock here, not available - until the whole shipment is
 *  counted. */
function ArrivalForm({ record, onRun }: { record: TransferDispatchRow; onRun: Runner }) {
  const [note, setNote] = useState("");
  const body: ArrivalBody = { ...(note ? { note } : {}), ...goodsMeta() };
  return (
    <div className="form-grid" data-testid={`arrival-form-${record.sequence_no}`}>
      <Field
        id={`arrival-note-${record.id}`}
        label="It has arrived"
        hint="Recording arrival makes nothing available here. The pieces stay on the road until this shipment is counted."
      >
        <input
          id={`arrival-note-${record.id}`}
          className="input"
          value={note}
          placeholder="Note (optional)"
          aria-describedby={`arrival-note-${record.id}-hint`}
          onChange={(e) => setNote(e.target.value)}
          data-testid={`arrival-note-${record.sequence_no}`}
        />
      </Field>
      <button
        className="btn btn-sm"
        onClick={() =>
          onRun(
            `/dispatches/${record.id}/arrival`,
            body,
            "Arrival recorded. Nothing is available here until the shipment is counted.",
          )
        }
        data-testid={`arrival-submit-${record.sequence_no}`}
      >
        <MapPinCheck size={14} /> Record arrival
      </button>
    </div>
  );
}

/** Attach an e-way reference late, or verify the one on file. Attaching is the
 *  sending site's; verifying is the Owner's. Neither rewrites whether a
 *  reference went with the goods. */
function EwayForm({
  record,
  may,
  onRun,
}: {
  record: TransferDispatchRow;
  may: (action: string) => boolean;
  onRun: Runner;
}) {
  const [reference, setReference] = useState("");
  const eway = record.eway;
  const attach: EwayBody = { action: "attach", reference: reference.trim(), ...goodsMeta() };
  const onFile = eway.reference;
  return (
    <div className="form-grid" data-testid={`eway-form-${record.sequence_no}`}>
      {may("attach_eway") && (
        <>
          <Field id={`eway-ref-${record.id}`} label="Attach an e-way reference">
            <input
              id={`eway-ref-${record.id}`}
              className="input"
              value={reference}
              onChange={(e) => setReference(e.target.value)}
              data-testid={`eway-ref-${record.sequence_no}`}
            />
          </Field>
          <button
            className="btn btn-sm"
            disabled={!reference.trim()}
            onClick={() =>
              onRun(
                `/dispatches/${record.id}/eway`,
                attach,
                "Attached. That it was missing when the shipment left is kept.",
              )
            }
            data-testid={`eway-attach-${record.sequence_no}`}
          >
            <FileCheck size={14} /> Attach
          </button>
        </>
      )}
      {may("verify_eway") && onFile && !eway.verified && (
        <button
          className="btn btn-sm"
          onClick={() =>
            onRun(
              `/dispatches/${record.id}/eway`,
              { action: "verify", reference: onFile },
              `Reference ${onFile} verified. This records a check, not that the movement was lawful.`,
            )
          }
          data-testid={`eway-verify-${record.sequence_no}`}
        >
          <BadgeCheck size={14} /> Verify {onFile}
        </button>
      )}
    </div>
  );
}

/** A failed delivery: record at the sender what is physically back, line by
 *  line and by condition. Only what came back is recorded - whatever is still
 *  missing stays open on the shipment until another receipt brings it. One
 *  receipt key per receipt, kept across a retry so a resend counts once. */
function ReturnForm({
  record,
  detail,
  onRun,
}: {
  record: TransferDispatchRow;
  detail: TransferDetail;
  onRun: Runner;
}) {
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [back, setBack] = useState<Record<string, { good: number; damaged: number }>>({});
  const [receiptKey, setReceiptKey] = useState(() => crypto.randomUUID());
  // A receipt that went through changes what came back; the next one is new.
  useEffect(() => {
    setReceiptKey(crypto.randomUUID());
    setBack({});
  }, [record.returned_qty]);

  const rows = record.lines.map((line) => {
    const found = back[line.line_key] ?? { good: 0, damaged: 0 };
    const unreturned = unreturnedOf(record, line.line_key);
    return {
      line,
      found,
      unreturned,
      problem: returnProblem(found.good, found.damaged, unreturned),
    };
  });
  const total = rows.reduce((sum, row) => sum + row.found.good + row.found.damaged, 0);
  const problem = rows.find((row) => row.problem)?.problem ?? "";
  const body: ReturnBody = {
    receipt_key: receiptKey,
    site_id: String(detail.source_site_id),
    reason: reason.trim(),
    ...(evidence.trim() ? { evidence_reference: evidence.trim() } : {}),
    lines: rows
      .filter((row) => row.found.good + row.found.damaged > 0)
      .map((row) => ({ line_key: row.line.line_key, ...row.found })),
    ...goodsMeta(),
  };
  const set = (lineKey: string, name: "good" | "damaged", value: number) =>
    setBack((current) => ({
      ...current,
      [lineKey]: { ...(current[lineKey] ?? { good: 0, damaged: 0 }), [name]: value },
    }));

  return (
    <div className="form-grid" data-testid={`return-form-${record.sequence_no}`}>
      <h4 className="h4">It came back</h4>
      <p className="lead">
        Record only what is physically back here. Good pieces wait in receiving until they are put
        away again; damaged ones go to quarantine for a second person to review. Anything still
        missing stays open on this shipment. Nothing is recorded as delivered.
      </p>
      {rows.map(({ line, found, unreturned }) => (
        <OperationsTable label="Transfer return pieces" key={line.line_key}>
          <table key={line.line_key} data-testid="return-line">
            <caption>
              {detail.lines.find((l) => l.line_key === line.line_key)?.description || line.sku_id} —{" "}
              {unreturned} still unaccounted for
            </caption>
            <thead>
              <tr>
                <th className="num">Back good</th>
                <th className="num">Back damaged</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                {(["good", "damaged"] as const).map((name) => (
                  <td key={name} className="num">
                    <input
                      className="input"
                      type="number"
                      min={0}
                      max={unreturned}
                      value={found[name]}
                      aria-label={`${name === "good" ? "Good" : "Damaged"} pieces back of ${line.sku_id}`}
                      onChange={(e) => set(line.line_key, name, Number(e.target.value || 0))}
                      data-testid={`return-${record.sequence_no}-${name}`}
                    />
                  </td>
                ))}
              </tr>
            </tbody>
          </table>
        </OperationsTable>
      ))}
      <Field id={`return-reason-${record.id}`} label="Why it came back">
        <input
          id={`return-reason-${record.id}`}
          className="input"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          data-testid={`return-reason-${record.sequence_no}`}
        />
      </Field>
      <Field
        id={`return-evidence-${record.id}`}
        label="Receiving evidence here (optional)"
        hint="A gate entry, the returned LR, or whatever this site keeps for goods coming in."
      >
        <input
          id={`return-evidence-${record.id}`}
          className="input"
          value={evidence}
          aria-describedby={`return-evidence-${record.id}-hint`}
          onChange={(e) => setEvidence(e.target.value)}
          data-testid={`return-evidence-${record.sequence_no}`}
        />
      </Field>
      {problem && <div className="warn-note">{problem}</div>}
      <button
        className="btn btn-sm"
        disabled={!reason.trim() || total === 0 || Boolean(problem)}
        onClick={() =>
          onRun(
            `/dispatches/${record.id}/return-to-source`,
            body,
            "Recorded back at the sender. The shipment and its departure are kept; anything still missing stays open.",
          )
        }
        data-testid={`return-submit-${record.sequence_no}`}
      >
        <Undo2 size={14} /> Record what came back
      </button>
    </div>
  );
}

/** Put a failed delivery's returned good pieces away at the sender, where
 *  they become sendable and sellable again. */
function AcceptReturnedForm({
  record,
  detail,
  sites,
  onRun,
}: {
  record: TransferDispatchRow;
  detail: TransferDetail;
  sites: SiteRef[];
  onRun: Runner;
}) {
  const [locationId, setLocationId] = useState("");
  const locations = useGoodsFetch<
    Page<{ id: string; data: { name: string; kind: string; system: boolean } }>,
    { id: string; name: string }[]
  >(
    `/goods-v1/masters/stores/${detail.source_site_id}/locations?limit=100`,
    (r) =>
      (r.items ?? [])
        .filter((row) => !row.data.system && PUTAWAY_KINDS.includes(row.data.kind))
        .map((row) => ({ id: row.id, name: row.data.name })),
    [],
  );
  useEffect(() => {
    if (!locationId && locations.value[0]) setLocationId(locations.value[0].id);
  }, [locations.value, locationId]);

  const lines = record.lines.filter((line) => line.returned_awaiting_putaway > 0);
  const body: AcceptReturnedBody = {
    lines: lines.map((line) => ({
      line_key: line.line_key,
      qty: line.returned_awaiting_putaway,
      destination_location_id: locationId,
    })),
    ...goodsMeta(),
  };
  return (
    <div className="form-grid" data-testid={`accept-returned-form-${record.sequence_no}`}>
      <h4 className="h4">Put the returned goods away</h4>
      <p className="lead">
        {lines.reduce((sum, line) => sum + line.returned_awaiting_putaway, 0)} good piece(s) are
        back in receiving at {siteLabel(sites, detail.source_site_id)}. Putting them away is what
        lets them be sent or sold again. Damaged pieces stay held.
      </p>
      <Field id={`accept-returned-location-${record.id}`} label="Put away in">
        <select
          id={`accept-returned-location-${record.id}`}
          className="select"
          value={locationId}
          onChange={(e) => setLocationId(e.target.value)}
          data-testid={`accept-returned-location-${record.sequence_no}`}
        >
          {locations.value.map((location) => (
            <option key={location.id} value={location.id}>
              {location.name}
            </option>
          ))}
        </select>
      </Field>
      <button
        className="btn btn-cta"
        disabled={!locationId}
        onClick={() =>
          onRun(
            `/dispatches/${record.id}/accept-returned`,
            body,
            "Put away at the sender. These pieces can be sent or sold again.",
          )
        }
        data-testid={`accept-returned-submit-${record.sequence_no}`}
      >
        <PackageCheck size={14} /> Accept and put away
      </button>
    </div>
  );
}

export default TransferDetailPage;

// ---------------------------------------------------------------------------
// A counted shipment's shortage (goods ticket 14)
// ---------------------------------------------------------------------------

/** The destination names the missing pieces of one counted shipment as a
 *  transit shortage, with its evidence and the follow-up it recorded with the
 *  sender or the transporter. Nothing moves: the pieces stay in transit until a
 *  different person - the Owner - approves it. */
function ShortageForm({
  record,
  detail,
  onRun,
}: {
  record: TransferDispatchRow;
  detail: TransferDetail;
  onRun: Runner;
}) {
  const [asked, setAsked] = useState<Record<string, string>>({});
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState("");
  const [followup, setFollowup] = useState("");
  const [recount, setRecount] = useState("");
  // Goods ticket 16: the short-expected half of one wrong-goods pair names
  // exactly the line and pieces the wrong goods came in place of.
  const pairs = unproposedPairs(record);
  const [pairing, setPairing] = useState("");
  const pair = pairs.find((p) => p.pairing_key === pairing);
  const rows = record.lines
    .map((line) => {
      const unclaimed = unclaimedOf(record, line.line_key);
      const raw = asked[line.line_key];
      const qty = pair
        ? pair.in_place_of_line_key === line.line_key
          ? pair.qty
          : 0
        : raw === undefined || raw === ""
          ? unclaimed
          : Number(raw);
      return { line, unclaimed, qty, problem: shortageProblem(qty, unclaimed) };
    })
    .filter((row) => row.unclaimed > 0);
  const problem = rows.find((row) => row.problem)?.problem ?? "";
  const chosen = rows.filter((row) => row.qty > 0);
  const body: ShortageBody = {
    ...(pair?.pairing_key ? { pairing_key: pair.pairing_key } : {}),
    lines: chosen.map((row) => ({ line_key: row.line.line_key, qty: row.qty })),
    reason: reason.trim(),
    evidence_reference: evidence.trim(),
    followup_note: followup.trim(),
    ...(recount.trim() ? { recount_note: recount.trim() } : {}),
    ...goodsMeta(),
  };
  const ready =
    chosen.length > 0 && !problem && body.reason && body.evidence_reference && body.followup_note;

  return (
    <div className="form-grid" data-testid={`shortage-form-${record.sequence_no}`}>
      <h4 className="h4">Resolve what never arrived</h4>
      <p className="lead">
        Propose the missing pieces as a transit shortage, with this shipment&apos;s count as the
        check. They stay in transit until a different person approves the correction; nobody at the
        sending site has to sign it off.
      </p>
      {pairs.length > 0 ? (
        <Field
          id={`shortage-pair-${record.id}`}
          label="For wrong goods (optional)"
          hint="The expected pieces the wrong goods came in place of, as their own paired decision."
        >
          <select
            id={`shortage-pair-${record.id}`}
            className="input"
            value={pairing}
            aria-describedby={`shortage-pair-${record.id}-hint`}
            onChange={(e) => setPairing(e.target.value)}
            data-testid={`shortage-pair-${record.sequence_no}`}
          >
            <option value="">Missing pieces, not wrong goods</option>
            {pairs.map((p) => (
              <option key={p.pairing_key ?? p.lot_id} value={p.pairing_key ?? ""}>
                {p.qty} replaced by “{p.description}”
              </option>
            ))}
          </select>
        </Field>
      ) : null}
      <OperationsTable label="Missing pieces to propose">
        <table>
          <thead>
            <tr>
              <th>Item</th>
              <th className="num">Missing, not yet proposed</th>
              <th className="num">Propose</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.line.line_key}>
                <td>
                  {detail.lines.find((l) => l.line_key === row.line.line_key)?.description ||
                    row.line.sku_id}
                </td>
                <td className="num" data-testid={`shortage-unclaimed-${record.sequence_no}`}>
                  {row.unclaimed}
                </td>
                <td className="num">
                  <input
                    className="input"
                    type="number"
                    min={0}
                    max={row.unclaimed}
                    value={
                      pair ? String(row.qty) : (asked[row.line.line_key] ?? String(row.unclaimed))
                    }
                    disabled={Boolean(pair)}
                    aria-label={`Missing pieces of ${row.line.sku_id} to propose`}
                    onChange={(e) =>
                      setAsked((current) => ({ ...current, [row.line.line_key]: e.target.value }))
                    }
                    data-testid={`shortage-qty-${record.sequence_no}`}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </OperationsTable>
      <Field id={`shortage-reason-${record.id}`} label="Why they are missing">
        <input
          id={`shortage-reason-${record.id}`}
          className="input"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          data-testid={`shortage-reason-${record.sequence_no}`}
        />
      </Field>
      <Field
        id={`shortage-evidence-${record.id}`}
        label="Supporting evidence"
        hint="A recount sheet, a photo, the delivery note - whatever shows the pieces never came."
      >
        <input
          id={`shortage-evidence-${record.id}`}
          className="input"
          value={evidence}
          aria-describedby={`shortage-evidence-${record.id}-hint`}
          onChange={(e) => setEvidence(e.target.value)}
          data-testid={`shortage-evidence-${record.sequence_no}`}
        />
      </Field>
      <Field
        id={`shortage-followup-${record.id}`}
        label="Follow-up with the sender or transporter"
        hint="What you asked them and what they said."
      >
        <input
          id={`shortage-followup-${record.id}`}
          className="input"
          value={followup}
          aria-describedby={`shortage-followup-${record.id}-hint`}
          onChange={(e) => setFollowup(e.target.value)}
          data-testid={`shortage-followup-${record.sequence_no}`}
        />
      </Field>
      <Field id={`shortage-recount-${record.id}`} label="What a recount found (optional)">
        <input
          id={`shortage-recount-${record.id}`}
          className="input"
          value={recount}
          onChange={(e) => setRecount(e.target.value)}
          data-testid={`shortage-recount-${record.sequence_no}`}
        />
      </Field>
      {problem && <div className="warn-note">{problem}</div>}
      <button
        className="btn btn-sm"
        disabled={!ready}
        onClick={() => {
          void onRun(
            `/dispatches/${record.id}/shortage`,
            body,
            "Shortage proposed. The pieces stay in transit until a different person decides it.",
          ).then(() => setAsked({}));
        }}
        data-testid={`shortage-submit-${record.sequence_no}`}
      >
        <ClipboardList size={14} /> Propose this shortage
      </button>
    </div>
  );
}

/** Every shortage proposal on one shipment, and - to the Owner - the decision. */
function ShortageTable({
  record,
  may,
  onRun,
}: {
  record: TransferDispatchRow;
  may: (action: string) => boolean;
  onRun: Runner;
}) {
  return (
    <OperationsTable label="Shortage corrections">
      <table data-testid={`shortages-${record.sequence_no}`}>
        <caption>Shortage corrections</caption>
        <thead>
          <tr>
            <th className="num">Pieces</th>
            <th>State</th>
            <th>Reason and evidence</th>
            <th>Follow-up</th>
            <th>Proposed by</th>
            <th>Decision</th>
          </tr>
        </thead>
        <tbody>
          {record.shortage_resolutions.map((gap) => (
            <tr key={gap.id} data-testid="shortage-row" data-state={gap.state}>
              <td className="num">{gap.quantity}</td>
              <td>
                {SHORTAGE_STATE_LABEL[gap.state] ?? gap.state}
                {gap.number ? ` · ${gap.number}` : ""}
              </td>
              <td>
                {gap.reason} · {gap.evidence_reference}
              </td>
              <td>{gap.followup_note}</td>
              <td>{gap.proposed_by?.name || gap.proposed_by?.id || "—"}</td>
              <td>
                {gap.state === "pending" && may("decide_shortage") ? (
                  <ShortageDecision gap={gap} onRun={onRun} />
                ) : gap.decided_by ? (
                  `${gap.decided_by.name || gap.decided_by.id}: ${gap.decision_reason ?? ""}`
                ) : (
                  "Waiting for a different person"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </OperationsTable>
  );
}

function ShortageDecision({ gap, onRun }: { gap: ShortageResolution; onRun: Runner }) {
  const [reason, setReason] = useState("");
  const decide = (decision: ShortageDecisionBody["decision"]) => {
    const body: ShortageDecisionBody = { decision, reason: reason.trim(), ...goodsMeta() };
    void onRun(
      `/shortages/${gap.id}/decide`,
      body,
      decision === "approve"
        ? "Approved. Those pieces are no longer in transit; the shortage is resolved."
        : "Rejected. The pieces stay in transit and can be proposed again.",
    );
  };
  return (
    <div className="form-grid" data-testid="shortage-decision">
      <input
        className="input"
        value={reason}
        aria-label="Reason for the decision"
        placeholder="Reason"
        onChange={(e) => setReason(e.target.value)}
        data-testid="shortage-decision-reason"
      />
      <div className="toolbar">
        <button
          className="btn btn-cta btn-sm"
          disabled={!reason.trim()}
          onClick={() => decide("approve")}
          data-testid="shortage-approve"
        >
          <BadgeCheck size={14} /> Approve
        </button>
        <button
          className="btn btn-sm"
          disabled={!reason.trim()}
          onClick={() => decide("reject")}
          data-testid="shortage-reject"
        >
          <Ban size={14} /> Reject
        </button>
      </div>
    </div>
  );
}
