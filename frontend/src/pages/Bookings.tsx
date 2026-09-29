// Bookings: one screen, one booking page, one form (store and warehouse
// operations PRD §12 - "the two booking screens become one destination per
// persona; the legacy screen's records stay readable through it").
//
// Every store runs on one stock system, and each system keeps its own booking
// engine underneath: the older one (`/api/bookings`, today's KDPS stores) and
// goods-v1. A person never picks between them. The list shows both as the same
// cards (`lib/bookingModel`), `/booking/<id>` opens either by the id's shape,
// and in the form the *store* decides which engine a booking goes to - each
// booking only offers stores it can reach, all on one system.
//
// This file speaks the older contract only; everything goods-v1 is reached
// through `GoodsBookings.tsx`, so each file keeps to one API. The size curve
// (ticket 40), asked for either engine's stores, is `BookingSizeCurve.tsx`.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { FileUp, Plus, Send, Sparkles, Trash2, XCircle } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { ListSearchBar } from "../components/SearchBox";
import { userCan } from "../shell/navConfig";
import { api, apiErrorMessage } from "../lib/api";
import { useDoc, useList } from "../lib/hooks";
import { formatINR, rupeesToPaise } from "../lib/format";
import {
  engineOfId,
  engineOfStore,
  filterCards,
  goodsCard,
  legacyCard,
  legacyStatus,
  lineTotals,
  type BookingEngine,
  type BookingFilter,
  type GoodsBookingRow,
  type LegacyBookingRow,
} from "../lib/bookingModel";
import { canFill, expandLine, splitWords, type SizeCurveFill } from "../lib/sizeCurve";
import { SizeCurveLineFill, SizeCurveNote, useSizeCurves } from "./BookingSizeCurve";
import {
  BookingCardLink,
  BookingFormTotals,
  BookingLinesTable,
  BookingPageHeader,
  BookingTiles,
} from "../components/BookingBits";
import {
  createGoodsBooking,
  GoodsBookingDetail,
  GoodsBookingPickers,
  goodsSeesCost,
  managesGoodsBookings,
  readsGoodsBookings,
  useGoodsBookingSummaries,
  type GoodsPickerValue,
} from "./GoodsBookings";
import { PageHeader } from "../components/PageHeader";
import "./Booking.css";

type AuthUser = ReturnType<typeof useAuth>["user"];

/** Legacy roles that never see what KDPS pays a vendor (the server's
 *  `vendors.serializers.COST_BLIND_ROLES`); the server leaves the figure out
 *  for them either way, this only decides whether the form asks. */
const COST_BLIND_ROLES = ["store_person", "warehouse"];

function legacySeesCost(user: AuthUser): boolean {
  const code = user?.role?.code;
  return Boolean(code) && !COST_BLIND_ROLES.includes(code as string);
}

interface BookingT extends LegacyBookingRow {
  season_name: string;
  close_reason: string;
  closed_at: string | null;
  closed_by_name: string | null;
  open_qty: number;
  estimated_value_paise: number | null;
  /** Absent for a login that may not see cost. */
  cost_total_paise?: number | null;
  vendor_ref: string;
  lines: BookingLineT[];
}
interface BookingLineT {
  id: number;
  style_code: string;
  size: string;
  description: string;
  booked_qty: number;
  mrp_paise: number | null;
  cost_paise?: number | null;
  received_qty: number;
  inwarded_qty: number;
  store: number | null;
  store_name: string | null;
}

// --------------------------------------------------------------------------
// The list
// --------------------------------------------------------------------------

const FILTERS: { key: BookingFilter; label: string }[] = [
  { key: "open", label: "Open" },
  { key: "done", label: "Done" },
  { key: "all", label: "All" },
];

export function BookingsPage() {
  const { user, session } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<BookingFilter>("open");

  // An old goods-v1 address (`/goods/bookings?booking=<id>`) redirects here
  // with its query; open the booking it named.
  const asked = params.get("booking");
  useEffect(() => {
    if (asked) navigate(`/booking/${asked}`, { replace: true });
  }, [asked, navigate]);

  // Each engine is read only by someone it answers - a `booking: view` section
  // for the older one, a goods-v1 booking grant for the other.
  const readsLegacy = userCan(user, "booking", "view");
  const readsGoods = readsGoodsBookings(session);
  const legacy = useList<LegacyBookingRow>(readsLegacy ? "/bookings" : null);
  const goods = useGoodsBookingSummaries(readsGoods);
  const loading = (readsLegacy && legacy.loading) || (readsGoods && goods.loading);

  const cards = useMemo(
    () => [
      ...(readsLegacy ? legacy.data.map(legacyCard) : []),
      ...(readsGoods ? goods.value.map((row) => goodsCard(row as GoodsBookingRow)) : []),
    ],
    [readsLegacy, readsGoods, legacy.data, goods.value],
  );
  const { shown, counts } = filterCards(cards, filter, q);
  const canPlace = userCan(user, "booking", "operate") || managesGoodsBookings(session);

  return (
    <div className="page-pad" data-testid="bookings-list">
      <PageHeader
        actions={
          canPlace && (
            <Link className="btn btn-cta" to="/booking/new" data-testid="new-booking-btn">
              <Plus size={16} /> New booking
            </Link>
          )
        }
      />

      <ListSearchBar
        value={q}
        onChange={setQ}
        placeholder="Search bookings — number, brand, vendor, season, store"
        label="Search bookings"
        testId="bookings-search"
        noun="booking"
        count={shown.length}
        loading={loading}
      />

      <div className="seg bk-chips" role="tablist" aria-label="Which bookings">
        {FILTERS.map(({ key, label }) => (
          <button
            key={key}
            role="tab"
            aria-selected={filter === key}
            className={`seg-btn ${filter === key ? "active" : ""}`}
            onClick={() => setFilter(key)}
            data-testid={`bookings-filter-${key}`}
          >
            {label}
            <span className="count">{counts[key]}</span>
          </button>
        ))}
      </div>

      {goods.failure && <div className="warn-note">{goods.failure}</div>}
      {loading ? (
        <p className="lead">Loading…</p>
      ) : shown.length === 0 ? (
        <div className="card section-card" data-testid="bookings-empty">
          {q
            ? `No booking matches “${q}”. Try the number, the brand, the vendor, the season or the store.`
            : filter === "open"
              ? "No open bookings."
              : "No bookings here yet."}
        </div>
      ) : (
        <div className="card-grid" data-testid="bookings-grid">
          {shown.map((card) => (
            <BookingCardLink key={`${card.engine}:${card.id}`} card={card} />
          ))}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// The form
// --------------------------------------------------------------------------

interface DraftLine {
  /** The line's own id on this form, so an answer for it finds it wherever it moved. */
  id: string;
  style_code: string;
  size: string;
  description: string;
  qty: string;
  cost: string;
  mrp: string;
  store: string;
}
const emptyLine = (): DraftLine => ({
  id: crypto.randomUUID(),
  style_code: "",
  size: "",
  description: "",
  qty: "",
  cost: "",
  mrp: "",
  store: "",
});

interface StoreChoice {
  id: string;
  label: string;
  engine: BookingEngine;
}

/** Rupees typed into a box as integer paise; blank is "not given". */
function paiseFrom(typed: string, what: string): number | null {
  const trimmed = typed.trim();
  if (!trimmed) return null;
  const paise = rupeesToPaise(trimmed);
  if (paise === null) throw new Error(`${what} "${trimmed}" is not an amount.`);
  return paise;
}

/** Paise a line would total to while it is still being typed (bad text: none). */
function typedPaise(typed: string): number | null {
  try {
    return paiseFrom(typed, "");
  } catch {
    return null;
  }
}

export function BookingNewPage() {
  const navigate = useNavigate();
  const { user, session } = useAuth();
  const canLegacy = userCan(user, "booking", "operate");
  const canGoods = managesGoodsBookings(session);

  // The older engine's pickers, read only by someone who books there. A buyer
  // who books only on goods-v1 reads its own reference lists instead.
  const { data: vendors } = useList<{ id: number; name: string }>(canLegacy ? "/vendors" : null);
  const { data: brands } = useList<{ id: number; name: string }>(
    canLegacy ? "/masters/brands" : null,
  );
  const { data: seasons } = useList<{ id: number; code: string; name: string }>(
    canLegacy ? "/masters/seasons" : null,
  );
  const { data: legacyStores } = useList<{
    id: number;
    code: string;
    name: string;
    stock_contract?: string;
  }>(canLegacy ? "/masters/stores" : null);

  // Every store this person can book for, each tagged with the engine its
  // stock system uses. A store on goods-v1 is never offered to the older
  // engine (it could never be received there), and goods-v1 offers only its own.
  const stores = useMemo<StoreChoice[]>(() => {
    const out = new Map<string, StoreChoice>();
    if (canLegacy) {
      for (const s of legacyStores) {
        if (engineOfStore(s.stock_contract) !== "legacy") continue;
        out.set(String(s.id), {
          id: String(s.id),
          label: `${s.name} (${s.code})`,
          engine: "legacy",
        });
      }
    }
    if (canGoods) {
      for (const s of session?.sites ?? []) {
        if (engineOfStore(s.stock_contract) !== "goods") continue;
        out.set(String(s.id), {
          id: String(s.id),
          label: `${s.name} (${s.code})`,
          engine: "goods",
        });
      }
    }
    return [...out.values()].sort((a, b) => a.label.localeCompare(b.label));
  }, [canLegacy, canGoods, legacyStores, session?.sites]);

  const [storeId, setStoreId] = useState("");
  const [picked, setPicked] = useState<GoodsPickerValue>({
    vendor_id: "",
    brand_id: "",
    season_id: "",
    entity_id: "",
  });
  const [vendorRef, setVendorRef] = useState("");
  const [expected, setExpected] = useState("");
  const [label, setLabel] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([emptyLine()]);
  const [sourceFileId, setSourceFileId] = useState<number | null>(null);
  const [reading, setReading] = useState(false);
  const [aiNote, setAiNote] = useState("");
  const [warn, setWarn] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const store = stores.find((s) => s.id === storeId);
  // The store decides the engine. Before one is chosen, a person who books in
  // only one engine is already in it.
  const engine: BookingEngine | null =
    store?.engine ?? (canGoods && !canLegacy ? "goods" : canLegacy && !canGoods ? "legacy" : null);
  const showCost =
    engine === "goods"
      ? goodsSeesCost(session)
      : engine === "legacy"
        ? legacySeesCost(user)
        : goodsSeesCost(session) || legacySeesCost(user);
  const lineStores = stores.filter((s) => s.engine === (store?.engine ?? engine));

  function setLine(i: number, key: keyof DraftLine, val: string) {
    setLines((ls) => ls.map((l, idx) => (idx === i ? { ...l, [key]: val } : l)));
  }

  // Ticket 40: each line's store's size curve for this brand and season.
  const lineStoreIds = storeId ? [storeId, ...lines.map((l) => l.store || storeId)] : [];
  const curves = useSizeCurves(lineStoreIds, picked.brand_id, picked.season_id);
  const [curveLine, setCurveLine] = useState<string | null>(null);
  const [curveNote, setCurveNote] = useState("");
  const linesNow = useRef(lines);
  linesNow.current = lines;
  useEffect(() => {
    setCurveLine(null);
    setCurveNote("");
  }, [storeId, picked.brand_id, picked.season_id]);
  const storeLabel = (id: string) => stores.find((s) => s.id === id)?.label ?? "This store";
  const columns = 6 + (showCost ? 2 : 0) + (lineStores.length > 1 ? 1 : 0);

  /** Put a fill's sizes in place of the line it was asked for, if that line is
   *  still the style total that was sent; otherwise the answer is dropped. */
  function filled(
    asked: { id: string; style: string; total: number; store: string },
    fill: SizeCurveFill,
  ) {
    setCurveLine(null);
    const at = linesNow.current.findIndex((l) => l.id === asked.id);
    const line = linesNow.current[at];
    if (
      !line ||
      !canFill(line) ||
      line.style_code.trim() !== asked.style ||
      Number(line.qty) !== asked.total ||
      (line.store || storeId) !== asked.store
    ) {
      setCurveNote("The line changed before its sizes came back, so nothing was filled.");
      return;
    }
    setLines((ls) => {
      const now = ls.findIndex((l) => l.id === asked.id);
      return now < 0
        ? ls
        : expandLine(ls, now, fill.sizes, (l) => ({ ...l, id: crypto.randomUUID() }));
    });
    setCurveNote(
      `${fill.style_code}: ${splitWords(fill.sizes)}, from ${fill.category} sales in ${fill.reference_season}. Change any size or quantity before saving.`,
    );
  }

  const totals = lineTotals(
    lines
      .filter((l) => Number(l.qty) > 0)
      .map((l) => ({
        qty: Number(l.qty),
        costPaise: typedPaise(l.cost),
        mrpPaise: typedPaise(l.mrp),
      })),
  );

  async function handleFile(file: File) {
    setReading(true);
    setWarn("");
    setAiNote("");
    setError("");
    const form = new FormData();
    form.append("file", file);
    try {
      const { data } = await api.post("/bookings/draft", form);
      setSourceFileId(data.source_file_id ?? null);
      const rupees = (v: unknown) => (v === null || v === undefined ? "" : String(v));
      const got: DraftLine[] = (data.lines || []).map((l: Record<string, unknown>) => ({
        id: crypto.randomUUID(),
        style_code: String(l.style_code || ""),
        size: String(l.size || ""),
        description: String(l.description || ""),
        qty: rupees(l.quantity),
        cost: rupees(l.cost),
        mrp: rupees(l.mrp),
        store: "",
      }));
      if (got.length) setLines(got);
      // best-effort prefill of brand and season by name
      const text = (v: unknown) => String(v ?? "").toLowerCase();
      if (data.brand) {
        const b = brands.find((x) => x.name.toLowerCase().includes(text(data.brand)));
        if (b) setPicked((p) => ({ ...p, brand_id: String(b.id) }));
      }
      if (data.season) {
        const s = seasons.find(
          (x) =>
            text(data.season).includes(x.code.toLowerCase()) ||
            x.name.toLowerCase().includes(text(data.season)),
        );
        if (s) setPicked((p) => ({ ...p, season_id: String(s.id) }));
      }
      if (data.vendor_ref) setVendorRef(data.vendor_ref);
      setAiNote(
        `Read ${got.length} line(s)${data.confidence ? ` · confidence ${Math.round(data.confidence * 100)}%` : ""}. Check them before saving.`,
      );
    } catch (e: unknown) {
      const response = (e as { response?: { data?: { source_file_id?: number; detail?: string } } })
        .response;
      setSourceFileId(response?.data?.source_file_id ?? null);
      setWarn(
        response?.data?.detail || "Could not read the file — please fill the lines in by hand.",
      );
    } finally {
      setReading(false);
    }
  }

  async function save() {
    setError("");
    if (!store || !engine) {
      setError("Pick the store the goods go to.");
      return;
    }
    if (!picked.vendor_id || !picked.brand_id || !picked.season_id) {
      setError("Pick a vendor, a brand and a season.");
      return;
    }
    if (engine === "goods" && !picked.entity_id) {
      setError("Pick the company buying the goods.");
      return;
    }
    let payload: {
      style_code: string;
      size: string;
      description: string;
      qty: number;
      costPaise: number | null;
      mrpPaise: number | null;
      store: string;
    }[];
    try {
      payload = lines
        .filter((l) => l.style_code.trim() && Number(l.qty) > 0)
        .map((l) => ({
          style_code: l.style_code.trim(),
          size: l.size.trim(),
          description: l.description.trim(),
          qty: Number(l.qty),
          costPaise: showCost ? paiseFrom(l.cost, "Cost") : null,
          mrpPaise: paiseFrom(l.mrp, "MRP"),
          store: l.store,
        }));
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    if (!payload.length) {
      setError("Add at least one line with a style code and a quantity.");
      return;
    }
    setSaving(true);
    try {
      if (engine === "goods") {
        const id = await createGoodsBooking({
          ...picked,
          destination_site_id: store.id,
          ...(label.trim() ? { commercial_label: label.trim() } : {}),
          ...(vendorRef.trim() ? { vendor_ref: vendorRef.trim() } : {}),
          ...(expected ? { expected_date: expected } : {}),
          lines: payload.map((l) => ({
            style_code: l.style_code,
            qty: l.qty,
            ...(l.size ? { size: l.size } : {}),
            ...(l.description ? { description: l.description } : {}),
            ...(l.mrpPaise === null ? {} : { mrp_paise: String(l.mrpPaise) }),
            ...(l.costPaise === null ? {} : { cost_paise: String(l.costPaise) }),
            ...(l.store ? { destination_site_id: l.store } : {}),
          })),
        });
        navigate(`/booking/${id}`);
        return;
      }
      const { data } = await api.post("/bookings", {
        vendor: Number(picked.vendor_id),
        brand: Number(picked.brand_id),
        season: Number(picked.season_id),
        destination_store: Number(store.id),
        vendor_ref: vendorRef,
        source_file_id: sourceFileId,
        lines: payload.map((l) => ({
          style_code: l.style_code,
          size: l.size,
          description: l.description,
          booked_qty: l.qty,
          mrp_paise: l.mrpPaise,
          cost_paise: l.costPaise,
          store: l.store === "" ? null : Number(l.store),
        })),
      });
      // Saved as a draft; sending it to the Owner is the form's last act. If
      // that step is refused the draft is kept, and its page offers it again.
      await api.post(`/bookings/${data.id}/request-approval`, {}).catch(() => undefined);
      navigate(`/booking/${data.id}`);
    } catch (e) {
      setError(e instanceof Error && !("response" in e) ? e.message : apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page-pad" data-testid="gb-draft">
      <PageHeader title="New booking" />

      {canLegacy && (
        <div className="card section-card">
          <p className="eyebrow">Optional · The vendor's document</p>
          <h3 className="h3" style={{ marginBottom: 12 }}>
            Upload it and the lines are filled in for you
          </h3>
          <label className="dropzone" data-testid="booking-dropzone">
            <input
              type="file"
              hidden
              accept="image/*,.pdf,.xlsx,.xls,.csv"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) handleFile(file);
              }}
            />
            {reading ? (
              <span>
                <Sparkles size={18} /> Reading the document…
              </span>
            ) : (
              <span>
                <FileUp size={18} /> Drop a PDF, photo or Excel, or click to choose
              </span>
            )}
          </label>
          {aiNote && (
            <div className="ai-note" data-testid="ai-note">
              <Sparkles size={14} /> {aiNote}
            </div>
          )}
          {warn && (
            <div className="warn-note" data-testid="ai-warn">
              {warn}
            </div>
          )}
        </div>
      )}

      <div className="card section-card">
        <p className="eyebrow">The booking</p>
        <div className="form-row" style={{ marginTop: 10 }}>
          <div className="field">
            <label htmlFor="gb-destination">Store (where the goods go)</label>
            <select
              id="gb-destination"
              className="select"
              value={storeId}
              onChange={(e) => {
                setStoreId(e.target.value);
                // A line's own store must be on the same system as the booking's.
                setLines((ls) => ls.map((l) => ({ ...l, store: "" })));
              }}
              data-testid="gb-destination"
            >
              <option value="">Choose a store</option>
              {stores.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>
          {canGoods ? (
            <GoodsBookingPickers
              value={picked}
              onChange={setPicked}
              showEntity={engine === "goods"}
            />
          ) : (
            <>
              <div className="field">
                <label htmlFor="booking-vendor">Vendor</label>
                <select
                  id="booking-vendor"
                  className="select"
                  value={picked.vendor_id}
                  onChange={(e) => setPicked({ ...picked, vendor_id: e.target.value })}
                  data-testid="gb-vendor"
                >
                  <option value="">Choose a vendor</option>
                  {vendors.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="booking-brand">Brand</label>
                <select
                  id="booking-brand"
                  className="select"
                  value={picked.brand_id}
                  onChange={(e) => setPicked({ ...picked, brand_id: e.target.value })}
                  data-testid="gb-brand"
                >
                  <option value="">Choose a brand</option>
                  {brands.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="booking-season">Season</label>
                <select
                  id="booking-season"
                  className="select"
                  value={picked.season_id}
                  onChange={(e) => setPicked({ ...picked, season_id: e.target.value })}
                  data-testid="gb-season"
                >
                  <option value="">Choose a season</option>
                  {seasons.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </div>
            </>
          )}
          <div className="field">
            <label htmlFor="gb-vendor-ref">Vendor ref (PO no.)</label>
            <input
              id="gb-vendor-ref"
              className="input"
              value={vendorRef}
              onChange={(e) => setVendorRef(e.target.value)}
              placeholder="The vendor's order number"
              data-testid="gb-vendor-ref"
            />
          </div>
          {engine === "goods" && (
            <>
              <div className="field">
                <label htmlFor="gb-expected">Expected by</label>
                <input
                  id="gb-expected"
                  className="input"
                  type="date"
                  value={expected}
                  onChange={(e) => setExpected(e.target.value)}
                  data-testid="gb-expected"
                />
              </div>
              <div className="field">
                <label htmlFor="gb-label">Label (optional)</label>
                <input
                  id="gb-label"
                  className="input"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  aria-describedby="gb-label-hint"
                  data-testid="gb-label"
                />
                <span className="goods-hint" id="gb-label-hint">
                  A short name to find this booking by, such as “Diwali drop”.
                </span>
              </div>
            </>
          )}
        </div>
      </div>

      <div className="card section-card">
        <div className="toolbar" style={{ marginBottom: 10 }}>
          <div>
            <p className="eyebrow">Lines</p>
            <h3 className="h3">What is booked</h3>
          </div>
          <div className="spacer" />
          <button
            className="btn"
            onClick={() => setLines((l) => [...l, emptyLine()])}
            data-testid="gb-add-line"
          >
            <Plus size={15} /> Add line
          </button>
        </div>
        {showCost && (
          <p className="hint" style={{ marginBottom: 10 }}>
            Cost per piece is a guide only. The PT still sets the cost that counts.
          </p>
        )}
        {[...new Set(lineStoreIds)].map((id) => (
          <SizeCurveNote
            key={id}
            state={curves.states[id]}
            storeLabel={storeLabel(id)}
            onRetry={curves.retry}
          />
        ))}
        {curveNote && (
          <p className="ok-note" role="status" data-testid="gb-curve-filled">
            {curveNote}
          </p>
        )}
        <div className="lines-scroll">
          <table className="lines-table" data-testid="gb-draft-lines">
            <thead>
              <tr>
                <th style={{ width: "16%" }}>Style code</th>
                <th>Size</th>
                <th style={{ width: "20%" }}>Description</th>
                <th>Quantity</th>
                {showCost && <th>Cost per piece ₹</th>}
                <th>MRP ₹</th>
                {showCost && <th className="num">Line cost</th>}
                {lineStores.length > 1 && <th style={{ width: "14%" }}>Store</th>}
                <th />
              </tr>
            </thead>
            <tbody>
              {lines.map((l, i) => {
                const lineCost = typedPaise(l.cost);
                const lineStore = l.store || storeId;
                const curve = curves.states[lineStore];
                const fillable = curve?.kind === "ready" && canFill(l);
                return [
                  <tr key={l.id} data-testid={`gb-draft-line-${i}`}>
                    <td>
                      <input
                        aria-label={`Style code, line ${i + 1}`}
                        value={l.style_code}
                        onChange={(e) => setLine(i, "style_code", e.target.value)}
                        data-testid={`gb-style-${i}`}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`Size, line ${i + 1}`}
                        value={l.size}
                        onChange={(e) => setLine(i, "size", e.target.value)}
                        data-testid={`gb-size-${i}`}
                      />
                      {fillable && curveLine !== l.id && (
                        <button
                          type="button"
                          className="btn btn-sm"
                          aria-label={`Fill sizes from the size curve, line ${i + 1}`}
                          onClick={() => setCurveLine(l.id)}
                          data-testid={`gb-curve-${i}`}
                        >
                          Sizes
                        </button>
                      )}
                    </td>
                    <td>
                      <input
                        aria-label={`Description, line ${i + 1}`}
                        value={l.description}
                        onChange={(e) => setLine(i, "description", e.target.value)}
                        data-testid={`gb-desc-${i}`}
                      />
                    </td>
                    <td>
                      <input
                        className="num"
                        inputMode="numeric"
                        aria-label={`Quantity, line ${i + 1}`}
                        value={l.qty}
                        onChange={(e) => setLine(i, "qty", e.target.value)}
                        data-testid={`gb-qty-${i}`}
                      />
                    </td>
                    {showCost && (
                      <td>
                        <input
                          className="num"
                          inputMode="decimal"
                          aria-label={`Cost per piece in rupees, line ${i + 1}`}
                          value={l.cost}
                          onChange={(e) => setLine(i, "cost", e.target.value)}
                          data-testid={`gb-cost-${i}`}
                        />
                      </td>
                    )}
                    <td>
                      <input
                        className="num"
                        inputMode="decimal"
                        aria-label={`MRP in rupees, line ${i + 1}`}
                        value={l.mrp}
                        onChange={(e) => setLine(i, "mrp", e.target.value)}
                        data-testid={`gb-mrp-${i}`}
                      />
                    </td>
                    {showCost && (
                      <td className="num line-cost" data-testid={`gb-line-cost-${i}`}>
                        {lineCost !== null && Number(l.qty) > 0
                          ? formatINR(lineCost * Number(l.qty))
                          : "—"}
                      </td>
                    )}
                    {lineStores.length > 1 && (
                      <td>
                        <select
                          className="select"
                          aria-label={`Store, line ${i + 1}`}
                          value={l.store}
                          onChange={(e) => setLine(i, "store", e.target.value)}
                          data-testid={`gb-line-store-${i}`}
                        >
                          <option value="">Same as booking</option>
                          {lineStores.map((s) => (
                            <option key={s.id} value={s.id}>
                              {s.label}
                            </option>
                          ))}
                        </select>
                      </td>
                    )}
                    <td>
                      <button
                        className="line-del"
                        aria-label={`Remove line ${i + 1}`}
                        disabled={lines.length === 1}
                        onClick={() => setLines((ls) => ls.filter((_, idx) => idx !== i))}
                        data-testid={`gb-remove-${i}`}
                      >
                        <Trash2 size={15} />
                      </button>
                    </td>
                  </tr>,
                  curveLine === l.id && curve?.kind === "ready" && canFill(l) && (
                    <SizeCurveLineFill
                      key={`${l.id}-curve`}
                      index={i}
                      curves={curve.data}
                      styleCode={l.style_code.trim()}
                      total={Number(l.qty)}
                      colSpan={columns}
                      commands={curves.commands}
                      onClose={() => setCurveLine(null)}
                      onFilled={(fill) =>
                        filled(
                          {
                            id: l.id,
                            style: l.style_code.trim(),
                            total: Number(l.qty),
                            store: lineStore,
                          },
                          fill,
                        )
                      }
                    />
                  ),
                ];
              })}
            </tbody>
          </table>
        </div>
        <BookingFormTotals
          pieces={totals.pieces}
          costPaise={totals.costPaise}
          mrpPaise={totals.mrpPaise}
          showCost={showCost}
        />
      </div>

      {error && (
        <div
          className="warn-note"
          style={{ maxWidth: 560, marginBottom: 12 }}
          data-testid="gb-error"
        >
          {error}
        </div>
      )}
      <button
        className="btn btn-primary btn-lg"
        disabled={saving}
        onClick={save}
        data-testid="gb-save"
      >
        {saving
          ? "Saving…"
          : engine === "legacy"
            ? "Send to Owner for approval"
            : engine === "goods"
              ? "Save"
              : "Save"}
      </button>
      {engine === "goods" && (
        <p className="hint" style={{ marginTop: 8 }}>
          Saved as a draft. You confirm it on the next page.
        </p>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// The older engine's booking page
// --------------------------------------------------------------------------

/** Close early or cancel, for whoever holds the Booking *Approve* cell.
 *
 *  A season that delivered 40 of 100 pieces would otherwise stay "Part arrived"
 *  for ever and the open-order figure would count 60 pieces nobody is waiting
 *  for. The reason is mandatory: it is the answer to "why did the rest never
 *  come?" six months later. */
function EndBooking({ booking, onDone }: { booking: BookingT; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [action, setAction] = useState<"close" | "cancel">("close");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const nothingReceived = booking.received_total === 0;

  async function submit() {
    setSaving(true);
    setError("");
    try {
      await api.post(`/bookings/${booking.id}/close`, { action, reason });
      setOpen(false);
      setReason("");
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <button className="btn" onClick={() => setOpen(true)} data-testid="end-booking-btn">
        <XCircle size={15} /> Close early or cancel
      </button>
    );
  }

  return (
    <div className="card section-card" style={{ marginBottom: 18 }} data-testid="end-booking-panel">
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <div>
          <p className="eyebrow">Ending this booking</p>
          <h3 className="h3">
            {action === "cancel"
              ? "Cancel — nothing has arrived"
              : `Close early — ${booking.open_qty} piece(s) will not come`}
          </h3>
        </div>
        <div className="spacer" />
        <button
          className="btn btn-sm"
          onClick={() => setOpen(false)}
          data-testid="end-booking-cancel"
        >
          Keep it open
        </button>
      </div>
      <div className="seg" style={{ marginBottom: 10 }}>
        <button
          className={`seg-btn ${action === "close" ? "active" : ""}`}
          onClick={() => setAction("close")}
          data-testid="end-action-close"
        >
          Close early
        </button>
        <button
          className={`seg-btn ${action === "cancel" ? "active" : ""}`}
          onClick={() => setAction("cancel")}
          disabled={!nothingReceived}
          title={
            nothingReceived ? undefined : "Goods have already arrived — close it early instead"
          }
          data-testid="end-action-cancel"
        >
          Cancel
        </button>
      </div>
      <input
        className="input"
        placeholder="Why is this booking ending? (e.g. season over, vendor sent 60 pcs short)"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        data-testid="end-booking-reason"
      />
      {error && (
        <div className="warn-note" style={{ marginTop: 10 }} data-testid="end-booking-error">
          {error}
        </div>
      )}
      <button
        className="btn btn-cta"
        style={{ marginTop: 12 }}
        disabled={reason.trim().length < 3 || saving}
        onClick={submit}
        data-testid="end-booking-confirm"
      >
        {saving
          ? "Recording…"
          : action === "cancel"
            ? "Cancel this booking"
            : "Close this booking early"}
      </button>
    </div>
  );
}

/** Send a drafted booking for the Owner's signature (D11 §3). The form does this
 *  on save; this is the way again for a draft whose sending was refused. */
function SendBookingForApproval({ booking, onDone }: { booking: BookingT; onDone: () => void }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit() {
    setSaving(true);
    setError("");
    try {
      await api.post(`/bookings/${booking.id}/request-approval`, {});
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <button
        className="btn btn-cta"
        disabled={saving}
        onClick={submit}
        data-testid="booking-send-approval"
      >
        <Send size={15} /> {saving ? "Sending…" : "Send to Owner for approval"}
      </button>
      {error && (
        <span className="warn-note" data-testid="booking-send-error">
          {error}
        </span>
      )}
    </>
  );
}

function LegacyBookingDetail({ id }: { id: string }) {
  const { user } = useAuth();
  const { data: b, loading, error, reload } = useDoc<BookingT>(`/bookings/${id}`);
  // Same rung the server checks (`vendors.CanCloseBooking` = booking:approve).
  const canEnd = userCan(user, "booking", "approve");
  // The maker's rung: whoever may place a booking may send their own for signing.
  const canSend = userCan(user, "booking", "operate");
  if (error) {
    return (
      <div className="page-pad">
        <div className="card section-card" data-testid="booking-missing">
          That booking was not found.
        </div>
      </div>
    );
  }
  if (loading || !b)
    return (
      <div className="page-pad">
        <p className="lead">Loading…</p>
      </div>
    );

  const ended = b.status === "closed" || b.status === "cancelled";
  const status = legacyStatus(b.status);
  const showCost = "cost_total_paise" in b;
  const card = legacyCard(b);
  const lines = b.lines.map((l) => ({
    key: String(l.id),
    style: l.style_code,
    size: l.size,
    description: l.description,
    store: l.store_name ?? b.destination_store_name ?? "Warehouse",
    qty: l.booked_qty,
    costPaise: l.cost_paise ?? null,
    mrpPaise: l.mrp_paise,
    arrived: l.received_qty,
    toCome: ended ? 0 : Math.max(0, l.booked_qty - l.received_qty),
  }));
  return (
    <div className="page-pad" data-testid="booking-detail">
      <BookingPageHeader
        brand={b.brand_name}
        number={b.number}
        status={status}
        facts={[
          b.vendor_name,
          b.season_name,
          card.store ? `→ ${card.store}` : null,
          b.vendor_ref ? `PO ${b.vendor_ref}` : null,
        ]}
      />

      {!ended && (
        <div className="bk-actions">
          {b.status === "draft" && canSend && (
            <SendBookingForApproval booking={b} onDone={reload} />
          )}
          {/* No Receive shortcut here any more (OPS-17): the older booking engine's
              receiving screens are being retired (OPS-18), and every site now
              receives through Receive Goods' Goods arrived. A goods-v1 booking
              offers "Receive against this booking" on its own page. */}
          {canEnd && <EndBooking booking={b} onDone={reload} />}
        </div>
      )}

      {b.status === "submitted" && (
        <div className="card section-card" data-testid="booking-waiting-note">
          <p className="eyebrow">Waiting for the Owner</p>
          <p className="lead">
            It is on the Owner's Action Needed. Goods cannot be received against it until the Owner
            approves it.
          </p>
        </div>
      )}

      {ended && (
        <div className="card section-card" data-testid="booking-ended-note">
          <p className="eyebrow">{b.status === "cancelled" ? "Cancelled" : "Closed early"}</p>
          <h3 className="h3">{b.close_reason || "No reason recorded"}</h3>
          <p className="lead">
            {b.closed_by_name ? `${b.closed_by_name} · ` : ""}
            {b.closed_at ? new Date(b.closed_at).toLocaleString("en-IN") : ""}
            {b.open_qty > 0 && b.status === "closed"
              ? ` · ${b.open_qty} piece(s) did not come`
              : ""}
          </p>
        </div>
      )}

      <BookingTiles
        booked={b.booked_total}
        arrived={b.received_total}
        toCome={ended ? 0 : b.open_qty}
        costPaise={showCost ? (b.cost_total_paise ?? null) : undefined}
        mrpPaise={b.estimated_value_paise}
      />

      <BookingLinesTable lines={lines} showCost={showCost} testId="booking-detail-lines" />
    </div>
  );
}

/** `/booking/<id>`: a number opens the older engine, a uuid opens goods-v1. */
export function BookingDetailPage() {
  const { id = "" } = useParams();
  if (engineOfId(id) === "goods") {
    return (
      <div className="page-pad">
        <GoodsBookingDetail bookingId={id} />
      </div>
    );
  }
  return <LegacyBookingDetail id={id} />;
}
