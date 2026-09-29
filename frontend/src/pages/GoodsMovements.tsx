// Movements (ticket 12): move exact quantities between locations at one site,
// put goods on hold, and release a hold back into an eligible location.
//
// Three rules the screen has to make visible rather than merely obey:
//   * a move carries acceptance with the goods and never creates it, so the
//     search row's "Accepted" column travels with what you move;
//   * a hold takes effect at once, even over reserved goods, and sends them to
//     quarantine — the screen offers no destination for one;
//   * a release needs a distinct approval and cannot skip acceptance, so it is
//     drafted, submitted, and only then decided by someone else.
//
// Marking damage is on the Stock screen (`GoodsStock.tsx`), where a person
// actually finds it; it posts the same E209 command.
//
// Goods ticket 15A adds evidenced adjustments on the same screen: adjust down
// (a count correction), shrinkage (pieces lost) and found stock (adjust up).
// Each needs a reason and evidence (a note or a photo), is drafted, sent for
// approval and decided by someone else (the Owner). A removal never takes
// reserved or held pieces; found goods stay held — in quarantine when nobody
// has shown what they cost — until they are accepted.
//
// Goods ticket 15B adds returns to vendor: from a quarantine row (recorded pieces
// under a hold) or from accepted good stock, with the vendor that agreed to take
// them back and the agreement's reference. The Owner approves it, which reserves
// the exact pieces and moves nothing; the record then takes vendor pickups (with
// a reason for every piece left behind) and explicit withdrawals (`RtvPanel`).
//
// Goods ticket 15C adds write-off without disposal: from a quarantine row, with a
// reason and evidence. The Owner - a different person - approves it, which
// recognises the loss at each piece's recorded cost and moves nothing: the goods
// stay in quarantine, held and counted, until a disposal removes them. Unvalued
// (pre-PT) goods and vendor-owned goods are refused by the server.
//
// Goods ticket 15D adds disposal: from a quarantine row, the record of pieces
// actually destroyed or handed over for scrap/recycling - method, actual time,
// evidence, and for a handover who took them and any scrap proceeds. Written-off
// pieces are disposed of by naming their write-off, so their loss is never
// recognised twice. The Owner approves it; only then do exactly those pieces
// leave the books, and the rest stay in quarantine.
import { useState } from "react";
import { ArrowRight, Lock, MinusCircle, PackagePlus, Search, Truck, Unlock } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Denied,
  Feedback,
  Field,
  PickerField,
  listState,
  masterLabel,
  useGoodsFetch,
  usePagedPicker,
  useStepUp,
  type Page,
  type ResourceDTO,
} from "../lib/goodsScreen";
import {
  DISPOSAL_METHODS,
  DISPOSAL_METHOD_LABEL,
  MOVEMENT_STATE_LABEL,
  VALUE_BASIS_LABEL,
  hasSource,
  heldKeys,
  isAdjustment,
  movedQty,
  movementLabel,
  portionWords,
  releasable,
  referencePath,
  referenceWords,
  removableQty,
  returnableQty,
  rtvStateWords,
  disposalStateWords,
  disposalValueWords,
  localToIso,
  writeOffQty,
  writeOffStateWords,
  type DisposalMethod,
  type MovementData,
  type MovementReference,
  type MovementSummary,
  type StockSearchPage,
  type StockSearchRow,
} from "../lib/goodsMovements";
import { eligibilityLabel } from "../lib/goodsStock";
import { rupeesToPaiseString, uploadEvidence } from "../lib/goodsPt";
import { hold } from "../lib/goodsScreen";
import { DamageReviewsPanel } from "./DamageReviews";
import { RtvPanel, type PutawayLocation } from "./RtvPanel";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { formatDateTime, formatPaiseString } from "../lib/format";
import { causeLabel, kindLabel, resolutionFor } from "../lib/goodsExceptions";
import "./GoodsAcceptance.css";

const MOVEMENTS = "/goods-v1/outbound/movements";
const SEARCH = "/goods-v1/outbound/stock-search";
const APPROVALS = "/goods-v1/approvals";

/** One pending approval, as E169's inbox answers it. The decision quotes the
 *  request's own reviewed hash and revision back, never the movement's — the
 *  approver decides exactly what was submitted. */
interface PendingApproval {
  id: string;
  subject_id: string;
  reviewed_hash: string;
  subject_revision: number;
}

interface LocationRow {
  site_id: string;
  name: string;
  kind: string;
  system: boolean;
}

/** The location kinds `goods_movements.STORAGE_KINDS` accepts as a destination.
 *  Offering only these keeps the picker honest: a protected system location is
 *  reached by the command that owns it, never by a hand-made move. */
const STORAGE_KINDS = ["floor", "backstore", "zone", "rack", "bin", "fixture"];

type Mode =
  | "move"
  | "hold"
  | "release"
  | "adjust_down"
  | "shrinkage"
  | "found"
  | "rtv"
  | "writeoff"
  | "dispose";

const MODE_LABEL: Record<Mode, string> = {
  move: "Move to another location",
  hold: "Put on hold",
  release: "Release from hold",
  adjust_down: "Adjust down (count correction)",
  shrinkage: "Record shrinkage (pieces lost)",
  found: "Record found stock",
  rtv: "Return to vendor",
  writeoff: "Write off (goods stay here)",
  dispose: "Record a disposal (destroyed or scrapped)",
};

/** The command kind each adjustment mode sends (`goods_adjustments.KINDS`). */
const ADJUSTMENT_MODE_KIND: Partial<Record<Mode, string>> = {
  adjust_down: "adjustment_down",
  shrinkage: "shrinkage",
  found: "adjustment_up",
};

export function GoodsMovementsPage() {
  const { session } = useAuth();
  const [params, setParams] = useSearchParams();
  const siteId = params.get("site_id") ?? "";
  const [text, setText] = useState(params.get("q") ?? "");
  const [query, setQuery] = useState(params.get("q") ?? "");
  const [picked, setPicked] = useState<StockSearchRow | null>(null);
  const [mode, setMode] = useState<Mode>("move");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [openId, setOpenId] = useState(params.get("movement") ?? "");

  const search = useGoodsFetch<StockSearchPage, StockSearchPage | null>(
    siteId
      ? `${SEARCH}?source_site_id=${siteId}${query ? `&q=${encodeURIComponent(query)}` : ""}`
      : null,
    (r) => r,
    null,
  );
  const locations = useGoodsFetch<Page<ResourceDTO<LocationRow>>, ResourceDTO<LocationRow>[]>(
    siteId ? `/goods-v1/masters/stores/${siteId}/locations?limit=100` : null,
    (r) => r.items ?? [],
    [],
  );
  const list = useGoodsFetch<Page<MovementSummary>, MovementSummary[]>(
    siteId ? `${MOVEMENTS}?site_id=${siteId}` : MOVEMENTS,
    (r) => r.items ?? [],
    [],
  );
  const open = useGoodsFetch<ResourceDTO<MovementData>, ResourceDTO<MovementData> | null>(
    openId ? `${MOVEMENTS}/${openId}` : null,
    (r) => r,
    null,
  );

  function set(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  }

  function refresh() {
    search.reload();
    list.reload();
    open.reload();
  }

  function report(e: unknown) {
    setOk("");
    setError(apiErrorMessage(e));
  }

  function choose(row: StockSearchRow, next: Mode) {
    setPicked(row);
    setMode(next);
    setError("");
    setOk("");
  }

  if (!session) return null;
  const canReview = hold(session, "movement.approve");
  const destinations = locations.value.filter(
    (row) => !row.data.system && STORAGE_KINDS.includes(row.data.kind),
  );
  // A location id is not a place anyone can find. The site's own directory is
  // already loaded for the destination picker, so every row and every frozen
  // movement line is named from it rather than showing a raw id.
  const locationNames = new Map(locations.value.map((row) => [row.id, row.data.name]));
  // Goods ticket 15B: an RTV takes quarantined pieces from the site's quarantine
  // and good stock from a storage location, so the row's location kind matters.
  const locationKinds = new Map(locations.value.map((row) => [row.id, row.data.kind]));

  return (
    <div className="stock-layout">
      <PageHeader
        title="Movements"
        lead="Move exact quantities between locations at one site, put goods on hold, release a hold, and record evidenced adjustments for someone else to approve."
      />

      <div className="form-grid">
        <Field id="mv-site" label="Site">
          <select
            id="mv-site"
            className="select"
            value={siteId}
            onChange={(e) => {
              setPicked(null);
              set("site_id", e.target.value);
            }}
            data-testid="mv-site"
          >
            <option value="">Choose a site</option>
            {session.sites.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.code})
              </option>
            ))}
          </select>
        </Field>
        <Field id="mv-search" label="Find stock">
          <input
            id="mv-search"
            className="input"
            placeholder="Style, colour or description"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                setQuery(text);
                set("q", text);
              }
            }}
            data-testid="mv-search"
          />
        </Field>
        <button
          className="btn btn-sm"
          onClick={() => {
            setQuery(text);
            set("q", text);
          }}
          data-testid="mv-search-go"
        >
          <Search size={14} /> Search
        </button>
      </div>

      <Feedback error={error} ok={ok} />

      {!siteId && <p className="muted">Choose a site to find the stock you want to move.</p>}

      {siteId && (
        <SearchResults
          search={search}
          locationNames={locationNames}
          locationKinds={locationKinds}
          onChoose={choose}
        />
      )}

      {picked && (
        <MovementForm
          row={picked}
          mode={mode}
          siteId={siteId}
          destinations={destinations}
          locationKind={picked.location_id ? locationKinds.get(picked.location_id) : undefined}
          onDone={(message, movementId) => {
            setPicked(null);
            setError("");
            setOk(message);
            if (movementId) {
              setOpenId(movementId);
              set("movement", movementId);
            }
            refresh();
          }}
          onError={report}
        />
      )}

      {/* OPS-05: the second person's decision on a damage report, beside the
          movements it concerns. Drawn only for someone who may approve a
          movement — the reporter's own screen offers no decision to make. */}
      {canReview && (
        <DamageReviewsPanel
          siteId={siteId}
          onDone={(message) => {
            setError("");
            setOk(message);
            refresh();
          }}
          onError={report}
        />
      )}

      <MovementList
        list={list}
        openId={openId}
        onOpen={(id) => {
          setOpenId(id);
          set("movement", id);
        }}
      />

      {openId && (
        <MovementDetail
          open={open}
          locationNames={locationNames}
          putawayLocations={destinations.map((row) => ({ id: row.id, name: row.data.name }))}
          onSubmitted={(message) => {
            setError("");
            setOk(message);
            refresh();
          }}
          onError={report}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stock search (E210)
// ---------------------------------------------------------------------------

function SearchResults({
  search,
  locationNames,
  locationKinds,
  onChoose,
}: {
  search: ReturnType<typeof useGoodsFetch<StockSearchPage, StockSearchPage | null>>;
  locationNames: Map<string, string>;
  locationKinds: Map<string, string>;
  onChoose: (row: StockSearchRow, mode: Mode) => void;
}) {
  if (search.denied) return <Denied what="stock" />;
  const rows = search.value?.items ?? [];
  const state = listState(
    { loading: search.loading, failure: search.failure, empty: rows.length === 0 },
    "No stock here matches that search.",
  );

  return (
    <section className="card section-card">
      <h3 className="h3">Stock at this site</h3>
      {/* The read names the basis it answered on, so the columns below are never
          read as money the caller was not shown (design §6.1's basis rule). */}
      <p className="lead" data-testid="mv-basis">
        Quantities only ({search.value?.basis ?? "quantity"} basis)
        {search.value ? ` · as of ${formatDateTime(search.value.as_of)}` : ""}
      </p>
      {state ?? (
        <div className="stock-table-wrap">
          <table data-testid="mv-search-table">
            <thead>
              <tr>
                <th>Location</th>
                <th>SKU / description</th>
                <th>Condition</th>
                <th className="num">Physical</th>
                <th className="num">Accepted</th>
                <th className="num">Held</th>
                <th className="num">Reserved</th>
                <th className="num">Sellable</th>
                <th className="num">Transferable</th>
                <th>Reasons</th>
                <th>Do</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                // A stock row is one (location, SKU, origin, condition) group,
                // so a location can carry several — accepted and unaccepted
                // pieces of the same SKU sit in quarantine side by side. The
                // grouping keys are published as data attributes so a reader,
                // and a spec, can address exactly one of them.
                <tr
                  key={index}
                  data-testid="mv-search-row"
                  data-location={row.location_id ?? ""}
                  data-origin={row.origin_id ?? ""}
                  data-accepted={row.accepted_qty}
                  data-condition={row.condition}
                >
                  <td data-testid="mv-row-location">
                    {row.location_id
                      ? (locationNames.get(row.location_id) ?? row.location_id)
                      : "—"}
                  </td>
                  <td>
                    {row.description || (row.sku_id ? row.sku_id.slice(0, 8) : "Unidentified")}
                  </td>
                  <td>{row.condition}</td>
                  <td className="num">{row.physical_qty}</td>
                  <td className="num" data-testid="mv-row-accepted">
                    {row.accepted_qty}
                  </td>
                  <td className="num" data-testid="mv-row-held">
                    {row.held_qty}
                  </td>
                  <td className="num" data-testid="mv-row-reserved">
                    {row.reserved_qty}
                  </td>
                  <td className="num" data-testid="mv-row-ats">
                    {row.ats_qty}
                  </td>
                  <td className="num" data-testid="mv-row-transferable">
                    {row.transferable_qty}
                  </td>
                  <td>
                    {row.eligibility_reasons.length > 0 ? (
                      <details>
                        <summary>{row.eligibility_reasons.length} reason(s)</summary>
                        <ul className="stock-reasons">
                          {row.eligibility_reasons.map((r) => (
                            <li key={r}>{eligibilityLabel(r)}</li>
                          ))}
                        </ul>
                      </details>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    {/* A row with nothing physically standing anywhere - all of
                        it in transit or already gone - has no source to name, so
                        it offers no action rather than a button the command
                        would refuse. */}
                    {hasSource(row) ? (
                      <div className="toolbar">
                        <button
                          className="btn btn-sm"
                          onClick={() => onChoose(row, "move")}
                          data-testid="mv-do-move"
                        >
                          <ArrowRight size={14} /> Move
                        </button>
                        <button
                          className="btn btn-sm"
                          onClick={() => onChoose(row, "hold")}
                          data-testid="mv-do-hold"
                        >
                          <Lock size={14} /> Hold
                        </button>
                        {releasable(row) && (
                          <button
                            className="btn btn-sm"
                            onClick={() => onChoose(row, "release")}
                            data-testid="mv-do-release"
                          >
                            <Unlock size={14} /> Release
                          </button>
                        )}
                        {removableQty(row) > 0 && (
                          <>
                            <button
                              className="btn btn-sm"
                              onClick={() => onChoose(row, "adjust_down")}
                              data-testid="mv-do-adjust-down"
                            >
                              <MinusCircle size={14} /> Adjust down
                            </button>
                            <button
                              className="btn btn-sm"
                              onClick={() => onChoose(row, "shrinkage")}
                              data-testid="mv-do-shrinkage"
                            >
                              <MinusCircle size={14} /> Shrinkage
                            </button>
                          </>
                        )}
                        {row.sku_id && (
                          <button
                            className="btn btn-sm"
                            onClick={() => onChoose(row, "found")}
                            data-testid="mv-do-found"
                          >
                            <PackagePlus size={14} /> Found more
                          </button>
                        )}
                        {returnableQty(
                          row,
                          row.location_id ? locationKinds.get(row.location_id) : undefined,
                        ) > 0 && (
                          <button
                            className="btn btn-sm"
                            onClick={() => onChoose(row, "rtv")}
                            data-testid="mv-do-rtv"
                          >
                            <Truck size={14} /> Return to vendor
                          </button>
                        )}
                        {writeOffQty(
                          row,
                          row.location_id ? locationKinds.get(row.location_id) : undefined,
                        ) > 0 && (
                          <button
                            className="btn btn-sm"
                            onClick={() => onChoose(row, "writeoff")}
                            data-testid="mv-do-writeoff"
                          >
                            <MinusCircle size={14} /> Write off
                          </button>
                        )}
                        {writeOffQty(
                          row,
                          row.location_id ? locationKinds.get(row.location_id) : undefined,
                        ) > 0 && (
                          <button
                            className="btn btn-sm"
                            onClick={() => onChoose(row, "dispose")}
                            data-testid="mv-do-dispose"
                          >
                            <MinusCircle size={14} /> Dispose
                          </button>
                        )}
                      </div>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// The command (E151)
// ---------------------------------------------------------------------------

function MovementForm({
  row,
  mode,
  siteId,
  destinations,
  locationKind,
  onDone,
  onError,
}: {
  row: StockSearchRow;
  mode: Mode;
  siteId: string;
  destinations: ResourceDTO<LocationRow>[];
  locationKind: string | undefined;
  onDone: (message: string, movementId?: string) => void;
  onError: (e: unknown) => void;
}) {
  const [qty, setQty] = useState(1);
  const [destination, setDestination] = useState("");
  const [reason, setReason] = useState("");
  const [holdKeys, setHoldKeys] = useState("");
  const [note, setNote] = useState("");
  const [photo, setPhoto] = useState<File | null>(null);
  const [valued, setValued] = useState(false);
  const [reference, setReference] = useState("");
  // Goods ticket 15A: a count error found after the stock was used names the
  // original GRN or PT it corrects. The picker offers the ones behind this row.
  const references = useGoodsFetch<{ items: MovementReference[] }, MovementReference[]>(
    mode === "adjust_down" && row.origin_id
      ? `${MOVEMENTS}/adjustment-references?site_id=${siteId}&origin_id=${row.origin_id}`
      : null,
    (r) => r.items ?? [],
    [],
  );
  const [busy, setBusy] = useState(false);
  // Goods ticket 15B: the vendor that agreed to take the goods back, and the
  // agreement's reference (GSA-R04). The system records it; it judges no terms.
  const [vendorId, setVendorId] = useState("");
  const [agreement, setAgreement] = useState("");
  const vendors = usePagedPicker(
    "/goods-v1/vendors",
    mode === "rtv" ? {} : null,
    vendorId,
    (vendor) => masterLabel(vendor, true),
  );

  // Goods ticket 15D: what actually happened to the goods.
  const [method, setMethod] = useState<DisposalMethod>("destruction");
  const [disposedAt, setDisposedAt] = useState(() => nowLocal());
  const [recipient, setRecipient] = useState("");
  const [proceeds, setProceeds] = useState("");
  const [slip, setSlip] = useState("");
  const [writeOffId, setWriteOffId] = useState("");
  // The site's approved write-offs a disposal may follow: only an approved one
  // carries a WOF number, so the list read narrowed to that number is exactly them.
  const writeOffs = useGoodsFetch<Page<MovementSummary>, MovementSummary[]>(
    mode === "dispose"
      ? `${MOVEMENTS}?site_id=${siteId}&q=${encodeURIComponent("/WOF/")}&limit=100`
      : null,
    (r) => (r.items ?? []).filter((wo) => wo.purpose === "writeoff" && wo.state === "official"),
    [],
  );

  const adjustmentKind = ADJUSTMENT_MODE_KIND[mode];
  const isRtv = mode === "rtv";
  const isWriteOff = mode === "writeoff";
  const isDisposal = mode === "dispose";
  const isScrap = isDisposal && method === "scrap_handover";
  // Adjustments, write-offs and disposals need evidence: a note, a photo or both
  // (a disposal may give its handover slip's reference instead).
  const needsEvidence = Boolean(adjustmentKind) || isWriteOff || isDisposal;
  const needsDestination =
    !adjustmentKind && !isRtv && !isWriteOff && !isDisposal && mode !== "hold";
  const most =
    mode === "release"
      ? row.held_qty
      : mode === "found"
        ? 999999
        : isRtv
          ? returnableQty(row, locationKind)
          : isWriteOff || isDisposal
            ? writeOffQty(row, locationKind)
            : adjustmentKind
              ? removableQty(row)
              : row.physical_qty;
  const mostLabel = mode === "found" ? "Quantity found" : `Quantity (up to ${most})`;
  const evidenced =
    !needsEvidence || note.trim() !== "" || photo !== null || (isDisposal && slip.trim() !== "");
  const agreed = !isRtv || (vendorId !== "" && agreement.trim() !== "");
  const proceedsPaise = isScrap ? rupeesToPaiseString(proceeds) : null;
  const disposalReady =
    !isDisposal ||
    (localToIso(disposedAt) !== null &&
      (!isScrap || recipient.trim() !== "") &&
      proceedsPaise !== undefined);

  async function submitRtv() {
    const evidenceIds = photo ? [await uploadEvidence(photo, { kind: "other", siteId })] : [];
    const { data } = await api.post<ResourceDTO<MovementData>>(MOVEMENTS, {
      kind: "rtv",
      site_id: siteId,
      reason_code: reason,
      vendor_id: vendorId,
      agreement_reference: agreement.trim(),
      evidence_ids: evidenceIds,
      evidence_note: note.trim() || null,
      lines: [
        {
          line_key: crypto.randomUUID(),
          sku_id: row.sku_id,
          qty,
          source_location_id: row.location_id,
          condition: row.condition,
          ...(row.origin_id ? { origin_id: row.origin_id } : {}),
        },
      ],
      ...goodsMeta(),
    });
    onDone(
      "Return drafted. Send it for approval below — the Owner approves it, and approving reserves the pieces without moving them.",
      data.id,
    );
  }

  async function submitWriteOff() {
    const evidenceIds = photo ? [await uploadEvidence(photo, { kind: "other", siteId })] : [];
    const { data } = await api.post<ResourceDTO<MovementData>>(MOVEMENTS, {
      kind: "writeoff",
      site_id: siteId,
      reason_code: reason,
      evidence_ids: evidenceIds,
      evidence_note: note.trim() || null,
      lines: [
        {
          line_key: crypto.randomUUID(),
          sku_id: row.sku_id,
          qty,
          source_location_id: row.location_id,
          condition: row.condition,
          ...(row.origin_id ? { origin_id: row.origin_id } : {}),
        },
      ],
      ...goodsMeta(),
    });
    onDone(
      "Write-off drafted. Send it for approval below — the Owner decides it, and approving moves nothing.",
      data.id,
    );
  }

  async function submitDisposal() {
    const evidenceIds = photo ? [await uploadEvidence(photo, { kind: "other", siteId })] : [];
    const { data } = await api.post<ResourceDTO<MovementData>>(MOVEMENTS, {
      kind: "disposal",
      site_id: siteId,
      reason_code: reason,
      evidence_ids: evidenceIds,
      evidence_note: note.trim() || null,
      ...(writeOffId ? { source_document_id: writeOffId } : {}),
      disposal: {
        method,
        disposed_at: localToIso(disposedAt),
        ...(isScrap ? { handed_over_to: recipient.trim() } : {}),
        ...(isScrap && proceedsPaise ? { scrap_proceeds_paise: proceedsPaise } : {}),
        ...(slip.trim() ? { evidence_reference: slip.trim() } : {}),
      },
      lines: [
        {
          line_key: crypto.randomUUID(),
          sku_id: row.sku_id,
          qty,
          source_location_id: row.location_id,
          condition: row.condition,
          ...(row.origin_id ? { origin_id: row.origin_id } : {}),
        },
      ],
      ...goodsMeta(),
    });
    onDone(
      "Disposal recorded. Send it for approval below — the goods leave the books only when the Owner approves it.",
      data.id,
    );
  }

  async function submitAdjustment() {
    const evidenceIds = photo ? [await uploadEvidence(photo, { kind: "other", siteId })] : [];
    const line: Record<string, unknown> =
      mode === "found"
        ? {
            line_key: crypto.randomUUID(),
            sku_id: row.sku_id,
            qty,
            description: row.description,
            ...(valued && row.origin_id ? { cost_evidence_origin_id: row.origin_id } : {}),
          }
        : {
            line_key: crypto.randomUUID(),
            sku_id: row.sku_id,
            qty,
            source_location_id: row.location_id,
            condition: row.condition,
            ...(row.origin_id ? { origin_id: row.origin_id } : {}),
          };
    const { data } = await api.post<ResourceDTO<MovementData>>(MOVEMENTS, {
      kind: adjustmentKind,
      site_id: siteId,
      reason_code: reason,
      evidence_ids: evidenceIds,
      evidence_note: note.trim() || null,
      ...(mode === "adjust_down" && reference ? { source_document_id: reference } : {}),
      lines: [line],
      ...goodsMeta(),
    });
    onDone(
      "Adjustment drafted. Send it for approval below — someone else (the Owner) decides it.",
      data.id,
    );
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      if (adjustmentKind) {
        await submitAdjustment();
        return;
      }
      if (isRtv) {
        await submitRtv();
        return;
      }
      if (isWriteOff) {
        await submitWriteOff();
        return;
      }
      if (isDisposal) {
        await submitDisposal();
        return;
      }
      const line: Record<string, unknown> = {
        line_key: crypto.randomUUID(),
        sku_id: row.sku_id,
        qty,
        source_location_id: row.location_id,
        condition: row.condition,
        ...(row.origin_id ? { origin_id: row.origin_id } : {}),
        ...(needsDestination ? { destination_location_id: destination } : {}),
        ...(mode === "release" ? { hold_keys: holdKeys.split(/[\s,]+/).filter(Boolean) } : {}),
      };
      const { data } = await api.post<ResourceDTO<MovementData>>(MOVEMENTS, {
        kind: mode === "move" ? "bin_move" : mode,
        site_id: siteId,
        reason_code: reason,
        evidence_ids: [],
        lines: [line],
        ...goodsMeta(),
      });
      onDone(
        mode === "release"
          ? "Release drafted. Submit it for approval below — a release is decided by someone else."
          : `Recorded as ${data.number ?? "a movement"}.`,
        data.id,
      );
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card section-card form-grid" onSubmit={submit} data-testid="mv-form">
      <h3 className="h3">{MODE_LABEL[mode]}</h3>
      <p className="lead">
        {row.description || row.sku_id || "Unidentified goods"} at {row.location_id} ·{" "}
        {row.physical_qty} physical, {row.accepted_qty} accepted, {row.held_qty} held.
      </p>
      {mode === "hold" && (
        <p className="lead" data-testid="mv-hold-note">
          Held goods go to quarantine and stop being available at once, even if a transfer has
          already reserved them.
        </p>
      )}
      {(mode === "adjust_down" || mode === "shrinkage") && (
        <p className="lead" data-testid="mv-adjust-note">
          The pieces leave this site at their own recorded cost once someone else approves. Pieces
          reserved for a transfer or under a hold are never taken.
        </p>
      )}
      {mode === "found" && (
        <p className="lead" data-testid="mv-found-note">
          Found pieces are recorded as new, held stock once someone else approves. They cannot be
          sold or sent until they are accepted, and without a recorded cost they wait in quarantine.
        </p>
      )}
      {isRtv && (
        <p className="lead" data-testid="mv-rtv-note">
          Nothing leaves when the return is approved: the pieces are reserved where they stand
          {locationKind === "quarantine" ? ", still held in quarantine," : ""} until the vendor
          collects them. Pieces a transfer has already reserved are never taken.
        </p>
      )}
      {isWriteOff && (
        <p className="lead" data-testid="mv-writeoff-note">
          A write-off records that the value of these pieces is lost, at their own recorded cost,
          once the Owner approves it. Nothing leaves: the pieces stay here in quarantine, held and
          counted, until they are actually disposed of. Reserved pieces, goods with no recorded cost
          and the brand&apos;s own (vendor-owned) goods are never written off.
        </p>
      )}
      {isDisposal && (
        <p className="lead" data-testid="mv-dispose-note">
          Record goods that were actually destroyed or handed over for scrap/recycling. Only the
          pieces you record leave, once the Owner approves; the rest stay here in quarantine. Pieces
          promised to a return to vendor must be withdrawn from it first. Written-off goods are
          disposed of by naming their write-off, so their loss is not counted twice. Donation and
          sale as damaged goods are not offered.
        </p>
      )}
      {mode === "release" && (
        <p className="lead" data-testid="mv-release-note">
          A release needs a separate approval, and only goods that were accepted here can be
          released back into an eligible location.
        </p>
      )}
      <Field id="mv-qty" label={mostLabel}>
        <input
          id="mv-qty"
          className="input"
          type="number"
          min={1}
          // An adjustment's figure is only a hint from an aggregate row: the
          // server picks the exact pieces and says why when it cannot.
          max={adjustmentKind || isRtv || isWriteOff || isDisposal ? undefined : most}
          value={qty}
          onChange={(e) => setQty(Number(e.target.value))}
          data-testid="mv-qty"
        />
      </Field>
      {needsDestination && (
        <Field id="mv-destination" label="Destination location">
          <select
            id="mv-destination"
            className="select"
            value={destination}
            onChange={(e) => setDestination(e.target.value)}
            data-testid="mv-destination"
          >
            <option value="">Choose a location</option>
            {destinations.map((loc) => (
              <option key={loc.id} value={loc.id}>
                {loc.data.name} ({loc.data.kind})
              </option>
            ))}
          </select>
        </Field>
      )}
      {mode === "release" && (
        <Field
          id="mv-hold-keys"
          label="Hold key(s) to lift"
          hint="Copy them from the hold's own movement below. Holds you do not name stay in force."
        >
          <input
            id="mv-hold-keys"
            className="input"
            value={holdKeys}
            onChange={(e) => setHoldKeys(e.target.value)}
            aria-describedby="mv-hold-keys-hint"
            data-testid="mv-hold-keys"
          />
        </Field>
      )}
      <Field id="mv-reason" label="Reason">
        <input
          id="mv-reason"
          className="input"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          data-testid="mv-reason"
        />
      </Field>
      {isRtv && (
        <>
          <PickerField
            id="mv-rtv-vendor"
            label="Vendor that agreed to take them back"
            noun="vendor"
            placeholder="Choose a vendor"
            value={vendorId}
            onChange={setVendorId}
            picker={vendors}
          />
          <Field
            id="mv-rtv-agreement"
            label="Vendor's agreement reference"
            hint="The letter, email or return authorisation number that says the vendor takes these back."
          >
            <input
              id="mv-rtv-agreement"
              className="input"
              value={agreement}
              maxLength={100}
              onChange={(e) => setAgreement(e.target.value)}
              aria-describedby="mv-rtv-agreement-hint"
              data-testid="mv-rtv-agreement"
            />
          </Field>
          <Field id="mv-evidence-note" label="Agreement note (optional)">
            <textarea
              id="mv-evidence-note"
              className="input"
              value={note}
              maxLength={1000}
              onChange={(e) => setNote(e.target.value)}
              data-testid="mv-evidence-note"
            />
          </Field>
        </>
      )}
      {isDisposal && (
        <>
          <Field id="mv-dispose-method" label="What happened to the goods">
            <select
              id="mv-dispose-method"
              className="select"
              value={method}
              onChange={(e) => setMethod(e.target.value as DisposalMethod)}
              data-testid="mv-dispose-method"
            >
              {DISPOSAL_METHODS.map((value) => (
                <option key={value} value={value}>
                  {DISPOSAL_METHOD_LABEL[value]}
                </option>
              ))}
            </select>
          </Field>
          <Field id="mv-dispose-at" label="When it actually happened">
            <input
              id="mv-dispose-at"
              className="input"
              type="datetime-local"
              value={disposedAt}
              max={nowLocal()}
              onChange={(e) => setDisposedAt(e.target.value)}
              data-testid="mv-dispose-at"
            />
          </Field>
          {isScrap && (
            <>
              <Field id="mv-dispose-recipient" label="Handed over to">
                <input
                  id="mv-dispose-recipient"
                  className="input"
                  value={recipient}
                  maxLength={120}
                  onChange={(e) => setRecipient(e.target.value)}
                  data-testid="mv-dispose-recipient"
                />
              </Field>
              <Field
                id="mv-dispose-proceeds"
                label="Scrap proceeds, ₹ (optional)"
                hint="What the scrap dealer paid. Recorded on its own; it is never set against the loss or posted."
              >
                <input
                  id="mv-dispose-proceeds"
                  className="input"
                  inputMode="decimal"
                  value={proceeds}
                  onChange={(e) => setProceeds(e.target.value)}
                  aria-describedby="mv-dispose-proceeds-hint"
                  data-testid="mv-dispose-proceeds"
                />
              </Field>
            </>
          )}
          <Field
            id="mv-dispose-slip"
            label="Handover slip or destruction note reference (optional)"
          >
            <input
              id="mv-dispose-slip"
              className="input"
              value={slip}
              maxLength={100}
              onChange={(e) => setSlip(e.target.value)}
              data-testid="mv-dispose-slip"
            />
          </Field>
          <Field
            id="mv-dispose-writeoff"
            label="Follows write-off"
            hint="Written-off goods are disposed of by naming their write-off; only its pieces are then taken."
          >
            <select
              id="mv-dispose-writeoff"
              className="select"
              value={writeOffId}
              onChange={(e) => setWriteOffId(e.target.value)}
              aria-describedby="mv-dispose-writeoff-hint"
              data-testid="mv-dispose-writeoff"
            >
              <option value="">None — the loss is recognised by this disposal</option>
              {writeOffs.value.map((wo) => (
                <option key={wo.id} value={wo.id}>
                  {wo.number ?? wo.id}
                </option>
              ))}
            </select>
          </Field>
        </>
      )}
      {mode === "adjust_down" && (
        <Field
          id="mv-reference"
          label="Corrects (original GRN or PT)"
          hint="Needed once the stock has been used, sold or reserved. Before that, a count error is corrected by reversing the PT."
        >
          <select
            id="mv-reference"
            className="select"
            value={reference}
            onChange={(e) => setReference(e.target.value)}
            aria-describedby="mv-reference-hint"
            data-testid="mv-reference"
          >
            <option value="">None</option>
            {references.value.map((ref) => (
              <option key={ref.id} value={ref.id}>
                {referenceWords(ref)}
              </option>
            ))}
          </select>
        </Field>
      )}
      {mode === "found" && row.origin_id && (
        <label className="checkbox" data-testid="mv-found-valued-label">
          <input
            type="checkbox"
            checked={valued}
            onChange={(e) => setValued(e.target.checked)}
            data-testid="mv-found-valued"
          />{" "}
          Value them at the recorded cost of this row&apos;s own receipt
        </label>
      )}
      {needsEvidence && (
        <>
          <Field
            id="mv-evidence-note"
            label="Evidence note"
            hint={
              isWriteOff
                ? "Why the value is lost. A note, a photo, or both is needed."
                : isDisposal
                  ? "How the goods were destroyed or handed over. A note, a photo or a slip reference is needed."
                  : "What was checked and found. A note, a photo, or both is needed."
            }
          >
            <textarea
              id="mv-evidence-note"
              className="input"
              value={note}
              maxLength={1000}
              onChange={(e) => setNote(e.target.value)}
              aria-describedby="mv-evidence-note-hint"
              data-testid="mv-evidence-note"
            />
          </Field>
          <Field id="mv-evidence-photo" label="Photo (optional)">
            <input
              id="mv-evidence-photo"
              type="file"
              accept="image/*"
              onChange={(e) => setPhoto(e.target.files?.[0] ?? null)}
              data-testid="mv-evidence-photo"
            />
          </Field>
        </>
      )}
      <button
        className="btn btn-cta"
        disabled={
          busy ||
          !reason ||
          !evidenced ||
          !agreed ||
          !disposalReady ||
          (needsDestination && !destination)
        }
        data-testid="mv-submit"
      >
        {MODE_LABEL[mode]}
      </button>
    </form>
  );
}

// ---------------------------------------------------------------------------
// The movement documents (E104 / E105 / E155)
// ---------------------------------------------------------------------------

function MovementList({
  list,
  openId,
  onOpen,
}: {
  list: ReturnType<typeof useGoodsFetch<Page<MovementSummary>, MovementSummary[]>>;
  openId: string;
  onOpen: (id: string) => void;
}) {
  if (list.denied) return <Denied what="movement" />;
  const state = listState(
    { loading: list.loading, failure: list.failure, empty: list.value.length === 0 },
    "No movements have been recorded here yet.",
  );
  return (
    <section className="card section-card">
      <h3 className="h3">Movements</h3>
      {state ?? (
        <table data-testid="mv-list">
          <thead>
            <tr>
              <th>Number</th>
              <th>What happened</th>
              <th>State</th>
              <th>When</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.value.map((row) => (
              <tr key={row.id} data-testid="mv-list-row">
                <td>{row.number ?? "Draft"}</td>
                <td>{movementLabel(row.purpose, row.kind)}</td>
                <td data-testid="mv-list-state">
                  {row.purpose === "rtv"
                    ? rtvStateWords(row.rtv_state, row.state)
                    : (MOVEMENT_STATE_LABEL[row.state] ?? row.state)}
                </td>
                <td>{formatDateTime(row.updated_at)}</td>
                <td>
                  <button
                    className={`btn btn-sm${row.id === openId ? " btn-active" : ""}`}
                    onClick={() => onOpen(row.id)}
                    data-testid="mv-open"
                  >
                    Open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function MovementDetail({
  open,
  locationNames,
  putawayLocations,
  onSubmitted,
  onError,
}: {
  open: ReturnType<
    typeof useGoodsFetch<ResourceDTO<MovementData>, ResourceDTO<MovementData> | null>
  >;
  locationNames: Map<string, string>;
  /** Goods ticket 15F: where returned RTV goods may be put away. */
  putawayLocations: PutawayLocation[];
  onSubmitted: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const { guarded, dialog } = useStepUp();
  const doc0 = open.value;
  const decidable = Boolean(doc0?.allowed_actions.includes("decide"));
  const inbox = useGoodsFetch<Page<PendingApproval>, PendingApproval[]>(
    decidable ? `${APPROVALS}/inbox?limit=100` : null,
    (r) => r.items ?? [],
    [],
  );
  if (open.denied) return <Denied what="movement" />;
  if (open.loading) return <span className="muted">Loading movement…</span>;
  if (open.failure) return <p className="warn-note">{open.failure}</p>;
  const doc = open.value;
  if (!doc) return null;
  const { header, lines, authority } = doc.data;
  const keys = heldKeys(lines.items);

  const waiting = inbox.value.find((row) => row.subject_id === doc.id);

  async function submitForApproval() {
    if (!doc) return;
    setBusy(true);
    try {
      await api.post(`${MOVEMENTS}/${doc.id}/submit`, {
        reviewed_hash: doc.content_hash,
        ...goodsMeta(doc.revision),
      });
      onSubmitted("Sent for approval. Someone else has to decide it.");
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  async function decide(decision: "approve" | "reject") {
    if (!waiting) return;
    setBusy(true);
    try {
      await guarded(() =>
        api.post(`${APPROVALS}/${waiting.id}/decide`, {
          decision,
          reviewed_hash: waiting.reviewed_hash,
          ...(decision === "reject" ? { reason_code: reason } : {}),
          ...goodsMeta(waiting.subject_revision),
        }),
      );
      const adjustment = isAdjustment(doc?.data.header.kind ?? "");
      const returning = doc?.data.header.kind === "rtv";
      const writingOff = doc?.data.header.kind === "writeoff";
      const disposing = doc?.data.header.kind === "disposal";
      onSubmitted(
        decision === "approve"
          ? returning
            ? "Approved. The pieces are reserved for the vendor; nothing leaves until a pickup is recorded."
            : writingOff
              ? "Approved. The value is written off; the goods stay in quarantine until they are disposed of."
              : disposing
                ? "Approved. Exactly the disposed pieces have left the books; the rest stay in quarantine."
                : adjustment
                  ? "Approved. The adjustment is recorded."
                  : "Approved. The goods are back in the location the release named."
          : returning
            ? "Turned down. The return stays a draft and nothing was reserved."
            : writingOff
              ? "Turned down. The write-off stays a draft and nothing was written off."
              : disposing
                ? "Turned down. The disposal stays a draft and nothing left the books."
                : adjustment
                  ? "Turned down. The adjustment stays a draft and nothing moved."
                  : "Turned down. The release stays a draft and nothing moved.",
      );
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card section-card" data-testid="mv-detail">
      <h3 className="h3">{doc.number ?? "Draft movement"}</h3>
      <dl className="gr-facts">
        <div>
          <dt>What happened</dt>
          <dd data-testid="mv-detail-kind">{movementLabel(null, header.kind)}</dd>
        </div>
        <div>
          <dt>Reason</dt>
          <dd data-testid="mv-detail-reason">{header.reason_code}</dd>
        </div>
        {(header.evidence_note || header.evidence_ids.length > 0) && (
          <div>
            <dt>Evidence</dt>
            <dd data-testid="mv-detail-evidence">
              {header.evidence_note}
              {header.evidence_ids.length > 0 && ` (${header.evidence_ids.length} file(s))`}
            </dd>
          </div>
        )}
        {header.source_document_id && (
          <div>
            <dt>{header.kind === "disposal" ? "Follows write-off" : "Corrects"}</dt>
            <dd data-testid="mv-detail-source-document">
              {header.kind === "disposal" ? (
                (doc.data.disposal?.write_off?.number ?? "An approved write-off")
              ) : doc.data.source_document ? (
                <Link to={referencePath(doc.data.source_document)}>
                  {referenceWords(doc.data.source_document)}
                </Link>
              ) : (
                "A GRN or PT you cannot open"
              )}
            </dd>
          </div>
        )}
        <div>
          <dt>State</dt>
          <dd>{MOVEMENT_STATE_LABEL[doc.state] ?? doc.state}</dd>
        </div>
        <div>
          <dt>Pieces shown</dt>
          <dd data-testid="mv-detail-qty">
            {movedQty(lines.items)} of {lines.total} line(s)
          </dd>
        </div>
        <div>
          <dt>Recorded by</dt>
          <dd data-testid="mv-detail-actor">{authority.actor.name || authority.actor.id}</dd>
        </div>
        <div>
          <dt>Approval</dt>
          <dd data-testid="mv-detail-approval">
            {authority.kind === "actor"
              ? "Recorded under the actor's own authority"
              : authority.approved_by
                ? `${authority.approved_by.name || authority.approved_by.id}${
                    authority.approved_at ? ` · ${formatDateTime(authority.approved_at)}` : ""
                  }`
                : authority.approval_state === "pending"
                  ? "Waiting for a separate approver"
                  : "Not yet submitted"}
          </dd>
        </div>
      </dl>

      {doc.data.exceptions && doc.data.exceptions.length > 0 && (
        <div data-testid="mv-detail-exceptions">
          <h4 className="h4">Owned work</h4>
          <ul className="stock-reasons">
            {doc.data.exceptions.map((row) => {
              const route = resolutionFor(row);
              return (
                <li key={row.id} data-testid="mv-detail-exception" data-state={row.state}>
                  {kindLabel(row.kind)} — {causeLabel(row.reason_code)} ·{" "}
                  {row.state === "open" ? "Open" : "Resolved"}
                  {row.state === "open" && route.to && (
                    <>
                      {" · "}
                      <Link to={route.to} data-testid="mv-detail-exception-link">
                        {route.label}
                      </Link>
                    </>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {header.kind === "rtv" && (
        <RtvPanel
          doc={doc}
          lineName={(key) => {
            const found = lines.items.find((row) => row.line_key === key);
            return found
              ? found.sku_id
                ? `SKU ${found.sku_id.slice(0, 8)}…`
                : key.slice(0, 8)
              : key.slice(0, 8);
          }}
          putawayLocations={putawayLocations}
          onDone={onSubmitted}
          onError={onError}
        />
      )}

      {header.kind === "writeoff" && <WriteOffPanel doc={doc} />}

      {header.kind === "disposal" && <DisposalPanel doc={doc} />}

      {keys.length > 0 && (
        <p className="lead" data-testid="mv-detail-hold-keys">
          Hold key(s): {keys.join(", ")}
        </p>
      )}

      <table data-testid="mv-detail-lines">
        <thead>
          <tr>
            <th>Source portions</th>
            <th>From</th>
            <th>To</th>
            <th>Condition</th>
            {(isAdjustment(header.kind) ||
              header.kind === "rtv" ||
              header.kind === "writeoff" ||
              header.kind === "disposal") && <th>Value</th>}
            <th className="num">Quantity</th>
          </tr>
        </thead>
        <tbody>
          {lines.items.map((line) => (
            <tr key={line.line_key} data-testid="mv-detail-line">
              <td>
                {line.portions.length > 0 ? (
                  <ul className="stock-reasons">
                    {line.portions.map((portion, index) => (
                      <li key={index}>{portionWords(portion)}</li>
                    ))}
                  </ul>
                ) : (
                  <span data-testid="mv-detail-found">
                    Found: {line.description || line.sku_id}
                    {line.found_lot_id ? ` · new lot ${line.found_lot_id.slice(0, 8)}…` : ""}
                  </span>
                )}
              </td>
              <td>
                {line.source_location_id
                  ? (locationNames.get(line.source_location_id) ?? line.source_location_id)
                  : "—"}
              </td>
              <td data-testid="mv-detail-destination">
                {line.destination_location_id
                  ? (locationNames.get(line.destination_location_id) ??
                    line.destination_location_id)
                  : header.kind === "rtv"
                    ? "To the vendor, when collected"
                    : header.kind === "writeoff"
                      ? "Stays here, in quarantine"
                      : header.kind === "disposal"
                        ? "Destroyed or handed over for scrap"
                        : "Leaves the site"}
              </td>
              <td>{line.condition}</td>
              {(isAdjustment(header.kind) ||
                header.kind === "rtv" ||
                header.kind === "writeoff" ||
                header.kind === "disposal") && (
                <td data-testid="mv-detail-value-basis">
                  {line.value_basis ? VALUE_BASIS_LABEL[line.value_basis] : "—"}
                </td>
              )}
              <td className="num">{line.qty}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {doc.allowed_actions.includes("submit") && (
        <button
          className="btn btn-cta"
          onClick={submitForApproval}
          disabled={busy}
          data-testid="mv-submit-for-approval"
        >
          Send for approval
        </button>
      )}

      {/* The distinct second person's own decision. It quotes the request's
          reviewed hash, so approving something that changed after it was
          submitted is refused rather than silently deciding the new thing. */}
      {doc.allowed_actions.includes("decide") && waiting && (
        <div className="form-grid" data-testid="mv-decide">
          <Field id="mv-decide-reason" label="Reason (needed to turn it down)">
            <input
              id="mv-decide-reason"
              className="input"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              data-testid="mv-decide-reason"
            />
          </Field>
          <button
            className="btn btn-cta"
            onClick={() => decide("approve")}
            disabled={busy}
            data-testid="mv-approve"
          >
            {header.kind === "rtv"
              ? "Approve the return"
              : header.kind === "writeoff"
                ? "Approve the write-off"
                : header.kind === "disposal"
                  ? "Approve the disposal"
                  : isAdjustment(header.kind)
                    ? "Approve the adjustment"
                    : "Approve the release"}
          </button>
          <button
            className="btn btn-sm"
            onClick={() => decide("reject")}
            disabled={busy || !reason}
            data-testid="mv-reject"
          >
            Turn it down
          </button>
        </div>
      )}
      {dialog}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Write-off without physical disposal (goods ticket 15C)
// ---------------------------------------------------------------------------

/** What a write-off recognised, and that the goods it names are still here.
 *  The value is shown only when the read carries it (the cost grant); without
 *  it the screen says nothing about money rather than showing a zero. */
function WriteOffPanel({ doc }: { doc: ResourceDTO<MovementData> }) {
  const detail = doc.data.write_off;
  return (
    <section className="card section-card" data-testid="wo-panel">
      <h4 className="h4">Write-off</h4>
      <dl className="gr-facts">
        <div>
          <dt>Status</dt>
          <dd data-testid="wo-state">{writeOffStateWords(detail, doc.state)}</dd>
        </div>
        <div>
          <dt>Pieces written off</dt>
          <dd data-testid="wo-qty">{detail?.qty ?? 0}</dd>
        </div>
        {detail?.value_paise !== undefined && (
          <div>
            <dt>Value written off (recorded cost)</dt>
            <dd data-testid="wo-value">{formatPaiseString(detail.value_paise)}</dd>
          </div>
        )}
        {detail?.state === "written_off" && (
          <>
            <div>
              <dt>Still at the site</dt>
              <dd data-testid="wo-physical">{detail.physical_qty}</dd>
            </div>
            <div>
              <dt>Still in quarantine</dt>
              <dd data-testid="wo-quarantine">{detail.quarantine_qty}</dd>
            </div>
            <div>
              <dt>Held by this write-off</dt>
              <dd data-testid="wo-held">{detail.still_held_qty}</dd>
            </div>
            <div>
              <dt>Disposed of since</dt>
              <dd data-testid="wo-disposed">{detail.disposed_qty ?? 0}</dd>
            </div>
          </>
        )}
      </dl>
      {detail?.disposals && detail.disposals.length > 0 && (
        <ul className="stock-reasons" data-testid="wo-disposals">
          {detail.disposals.map((row) => (
            <li key={row.id} data-testid="wo-disposal">
              {row.number ?? "Draft disposal"} · {row.qty} piece(s) ·{" "}
              {MOVEMENT_STATE_LABEL[row.state ?? ""] ?? row.state}
            </li>
          ))}
        </ul>
      )}
      <p className="lead" data-testid="wo-note">
        A write-off moves nothing. The goods stay counted at the site, in quarantine and
        unavailable, until a disposal records that they actually left. No accounting entry is made.
      </p>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Disposal of recorded stock (goods ticket 15D)
// ---------------------------------------------------------------------------

/** Now, as a `datetime-local` value in the browser's own time zone, to the minute. */
function nowLocal(): string {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

/** What a disposal removed, when and how, and how the loss of those pieces stands
 *  - kept apart from the physical fact. Money shows only when the read carries it
 *  (the cost grant); the scrap proceeds are their own line, never netted. */
function DisposalPanel({ doc }: { doc: ResourceDTO<MovementData> }) {
  const detail = doc.data.disposal;
  const facts = doc.data.header.disposal;
  return (
    <section className="card section-card" data-testid="dsp-panel">
      <h4 className="h4">Disposal</h4>
      <dl className="gr-facts">
        <div>
          <dt>Status</dt>
          <dd data-testid="dsp-state">{disposalStateWords(detail, doc.state)}</dd>
        </div>
        {facts && (
          <>
            <div>
              <dt>What happened</dt>
              <dd data-testid="dsp-method">{DISPOSAL_METHOD_LABEL[facts.method]}</dd>
            </div>
            <div>
              <dt>When it actually happened</dt>
              <dd data-testid="dsp-at">{formatDateTime(facts.disposed_at)}</dd>
            </div>
            {detail?.recorded_at && (
              <div>
                <dt>Recorded</dt>
                <dd data-testid="dsp-recorded-at">{formatDateTime(detail.recorded_at)}</dd>
              </div>
            )}
            {facts.handed_over_to && (
              <div>
                <dt>Handed over to</dt>
                <dd data-testid="dsp-recipient">{facts.handed_over_to}</dd>
              </div>
            )}
            {facts.evidence_reference && (
              <div>
                <dt>Slip / note reference</dt>
                <dd data-testid="dsp-reference">{facts.evidence_reference}</dd>
              </div>
            )}
          </>
        )}
        <div>
          <dt>Pieces recorded</dt>
          <dd data-testid="dsp-qty">{detail?.qty ?? 0}</dd>
        </div>
        <div>
          <dt>Pieces gone from the books</dt>
          <dd data-testid="dsp-disposed">{detail?.disposed_qty ?? 0}</dd>
        </div>
        {detail && (
          <div>
            <dt>Their loss</dt>
            <dd data-testid="dsp-value-basis">{disposalValueWords(detail)}</dd>
          </div>
        )}
        {detail?.value_paise !== undefined && (
          <div>
            <dt>Loss recognised here (recorded cost)</dt>
            <dd data-testid="dsp-value">{formatPaiseString(detail.value_paise)}</dd>
          </div>
        )}
        {detail?.scrap_proceeds_paise && (
          <div>
            <dt>Scrap proceeds (recorded only)</dt>
            <dd data-testid="dsp-proceeds">{formatPaiseString(detail.scrap_proceeds_paise)}</dd>
          </div>
        )}
      </dl>
      <p className="lead" data-testid="dsp-note">
        Only the pieces recorded here leave the site; any others stay in quarantine under their
        holds. No accounting entry is made, and scrap proceeds are never posted.
      </p>
    </section>
  );
}
