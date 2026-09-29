// Arrivals and counting (ticket 05): a receiver at the site where the goods
// actually are records what turned up, counts it honestly, and issues a GRN.
//
// Since OPS-17 (store and warehouse operations PRD §5.1) this is no longer a
// screen of its own. Its parts are the receiving workflow's steps: `NewArrival`
// is what **Goods arrived** opens, and `ArrivalPanel` is the delivery's Arrival
// and Count steps (`ReceiveDelivery.tsx`). The records are unchanged.
//
// What these panels refuse to fudge:
//   * an arrival is recorded at the site the goods reached, by someone who may
//     receive there. It may or may not have a booking, and it may or may not
//     have an invoice; neither is invented;
//   * several arrivals can share one transporter reference. They stay separate
//     records with their own count and GRN (GSA-T05);
//   * a count row says what condition the pieces are in. Pieces nobody can
//     identify are described in words and carry no SKU — never a nearest guess;
//   * a scan is pending work until the server acknowledges its key (design
//     §8.4). Nothing here says "recorded" before that;
//   * the GRN it issues is a quantity document. It carries no cost, no value
//     and no books effect, and the button says so before it is pressed.
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ClipboardCheck, Plus, Trash2, X } from "lucide-react";
import { Link } from "react-router-dom";

import {api, apiErrorCode, apiErrorMessage, goodsMeta} from "../lib/api";
import {
  Denied,
  Feedback,
  Field,
  masterLabel,
  PickerField,
  useGoodsFetch,
  usePagedPicker,
  useResourceDoc,
  type Page,
  type ResourceDTO,
} from "../lib/goodsScreen";
import {
  CONDITIONS,
  CONDITION_HELP,
  CONDITION_LABEL,
  conditionTotals,
  countedTotal,
  deliveryStepPath,
  NOT_GIVEN,
  orNotGiven,
  siteName,
  type ArrivalData,
  type CountSessionData,
  type DuplicateWarning,
  type Observation,
} from "../lib/goodsReceiving";
import {
  ALIAS_TYPES,
  candidateLabel,
  effectiveProfiles,
  identityForRow,
  needsChoice,
  resolutionMessage,
  type AliasContext,
  type ConfigVersionRow,
  type IdentityProfileChoice,
  type IdentityResolution,
} from "../lib/goodsIdentity";
import { useAuth } from "../auth/AuthContext";
import { formatDateTime } from "../lib/format";
import { rupeesToPaiseString } from "../lib/goodsPt";
import { threeWayOn, useThreeWay } from "./ThreeWayMatch";
import "./GoodsReceiving.css";

interface NamedMaster {
  code: string;
  name: string;
}

function useNames(url: string) {
  const list = useGoodsFetch<Page<ResourceDTO<NamedMaster>>, ResourceDTO<NamedMaster>[]>(
    url,
    (r) => r.items ?? [],
    [],
  );
  const name = (id: string | null | undefined) => {
    if (!id) return NOT_GIVEN;
    const found = list.value.find((row) => row.id === String(id));
    return found ? `${found.data.name} (${found.data.code})` : `#${id}`;
  };
  return { ...list, name };
}

// --------------------------------------------------------------------------
// Recording an arrival (E113), with the duplicate-invoice warning
// --------------------------------------------------------------------------

function nowLocalInput(): string {
  const at = new Date();
  return new Date(at.getTime() - at.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

/** What a text box holds once the person has stopped typing for a moment. */
function useSettled(value: string, delayMs = 250): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}

/** The server's duplicate-invoice warning for what is typed so far (E250).
 *
 *  Asked of the server rather than matched here, because the reason the person
 *  gives has to be bound to a warning the server minted: a screen that decides
 *  for itself which arrivals look alike has nothing E113 can check the
 *  acknowledgement against. It also searches the whole receiving legal entity,
 *  which this screen's own page of arrivals is not. */
function useDuplicateWarning(entry: {
  site_id: string;
  vendor_id: string;
  invoice_number: string;
}) {
  // Settle before asking. The invoice number is typed a character at a time and
  // this read searches a whole legal entity; asking on every keystroke would
  // run that search a dozen times for one number (`SearchBox` debounces the
  // same way).
  const invoice = useSettled(entry.invoice_number.trim());
  const url =
    entry.site_id && entry.vendor_id && invoice
      ? `/goods-v1/inbound/arrivals/duplicate-warning?site_id=${encodeURIComponent(
          entry.site_id,
        )}&vendor_id=${encodeURIComponent(entry.vendor_id)}&invoice_number=${encodeURIComponent(
          invoice,
        )}`
      : null;
  const read = useGoodsFetch<DuplicateWarning, DuplicateWarning | null>(url, (r) => r, null);
  // Still typing, or the server has not answered yet. Either way what is on the
  // screen is not yet the warning for what is in the box.
  const pending = entry.invoice_number.trim() !== invoice || read.loading;
  return { ...read, warning: read.value, pending };
}

/** The booking a delivery is received against, as far as the arrival form needs
 *  it: who sent it, which brand, and where it was booked to go. */
interface BookingForArrival {
  booking_header?: {
    vendor_id?: string | number | null;
    brand_id?: string | number | null;
    destination_site_id?: string | number | null;
  };
}

/** Record what turned up: the start of every vendor delivery.
 *
 *  Rendered by Receive Goods' **Goods arrived** (OPS-17, store and warehouse
 *  operations PRD §5.1), which is the only way into receiving a vendor delivery,
 *  and by **Receive against this booking**, which opens it with the booking
 *  already chosen. A chosen booking also fills in its vendor, brand and site,
 *  because the server refuses an arrival whose vendor or brand is not the
 *  booking's; everything stays editable and nothing is recorded until the
 *  person presses Record arrival. */
export function NewArrival({
  onSaved,
  onClose,
  initialSiteId = "",
  initialBookingId = "",
}: {
  onSaved: (id: string) => void;
  onClose: () => void;
  /** The site the person was already looking at, if any. */
  initialSiteId?: string;
  /** The booking to receive against, when the person came from one. */
  initialBookingId?: string;
}) {
  const { session } = useAuth();
  const mySites = session?.sites ?? [];

  const [form, setForm] = useState({
    site_id: mySites.some((site) => String(site.id) === String(initialSiteId))
      ? String(initialSiteId)
      : "",
    vendor_id: "",
    brand_id: "",
    actual_arrival_at: nowLocalInput(),
    transporter_ref: "",
    booking_id: initialBookingId,
    invoice_number: "",
    invoice_date: "",
    brand_dispatch_date: "",
  });
  // Ticket 24 (ST-BRD-5): SOR stock ages from the brand's dispatch date, asked
  // for only at a site where SOR ageing is on - the server refuses it elsewhere.
  const asksDispatch = (session?.store_features?.["sor-ageing"] ?? []).includes(form.site_id);
  const chosenBooking = useResourceDoc<BookingForArrival>(
    form.booking_id ? `/goods-v1/bookings/${form.booking_id}` : null,
  );
  useEffect(() => {
    const header = chosenBooking.doc?.data.booking_header;
    if (!header || chosenBooking.doc?.id !== form.booking_id) return;
    const text = (value: string | number | null | undefined) =>
      value === null || value === undefined ? "" : String(value);
    const site = text(header.destination_site_id);
    setForm((current) => ({
      ...current,
      vendor_id: text(header.vendor_id) || current.vendor_id,
      brand_id: text(header.brand_id) || current.brand_id,
      // Only a site this person may receive at; otherwise they choose.
      site_id: mySites.some((row) => String(row.id) === site) ? site : current.site_id,
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chosenBooking.doc, form.booking_id]);
  const vendors = usePagedPicker("/goods-v1/vendors", {}, form.vendor_id, (row) =>
    masterLabel(row, true),
  );
  const brands = usePagedPicker("/goods-v1/masters/brands", {}, form.brand_id, (row) =>
    masterLabel(row),
  );
  // Only a confirmed booking has anything to receive against; searching and
  // paging draft bookings into this picker would offer nothing choosable.
  const bookings = usePagedPicker(
    "/goods-v1/bookings",
    { state: "confirmed" },
    form.booking_id,
    (row) => row.number ?? row.id.slice(0, 8),
  );
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const duplicate = useDuplicateWarning(form);
  const candidates = duplicate.warning?.candidates ?? [];
  const blockedByDuplicate = candidates.length > 0 && !reason.trim();

  async function save() {
    setError("");
    setBusy(true);
    try {
      const { data } = await api.post<{ id: string }>("/goods-v1/inbound/arrivals", {
        site_id: form.site_id,
        vendor_id: form.vendor_id,
        brand_id: form.brand_id,
        actual_arrival_at: new Date(form.actual_arrival_at).toISOString(),
        ...(form.transporter_ref ? { transporter_ref: form.transporter_ref } : {}),
        ...(form.booking_id ? { booking_id: form.booking_id } : {}),
        ...(form.invoice_number ? { invoice_number: form.invoice_number } : {}),
        ...(form.invoice_date ? { invoice_date: form.invoice_date } : {}),
        ...(asksDispatch && form.brand_dispatch_date
          ? { brand_dispatch_date: form.brand_dispatch_date }
          : {}),
        // Sent together or not at all: the exact warning being answered, and
        // the answer. The server refuses a hash that is no longer its current
        // warning, so an arrival recorded since this screen last looked sends
        // the person back to read the new one.
        ...(duplicate.warning?.warning_hash
          ? {
              duplicate_warning_hash: duplicate.warning.warning_hash,
              duplicate_reason: reason.trim(),
            }
          : {}),
        ...goodsMeta(),
      });
      onSaved(data.id);
    } catch (e) {
      if (apiErrorCode(e) === "DUPLICATE_ARRIVAL") duplicate.reload();
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const ready =
    form.site_id &&
    form.vendor_id &&
    form.brand_id &&
    form.actual_arrival_at &&
    !blockedByDuplicate &&
    // Never record ahead of the warning: until it has caught up with the box,
    // an empty candidate list means "not asked yet", not "nothing to say".
    !duplicate.pending;

  return (
    <div data-testid="ga-new-form">
      <div className="toolbar">
        <h3 className="h3">Record an arrival</h3>
        <div className="spacer" />
        <button className="btn btn-sm" onClick={onClose} data-testid="ga-new-close">
          <X size={14} /> Close
        </button>
      </div>
      <p className="lead">
        Record it at the site the goods actually reached, at the time they actually reached it. A
        booking and an invoice are both optional — an arrival without either is still an arrival.
      </p>
      <Feedback error={error} ok="" />
      <div className="form-grid">
        <Field id="ga-site" label="Site the goods arrived at">
          <select
            id="ga-site"
            className="select"
            value={form.site_id}
            onChange={(e) => setForm({ ...form, site_id: e.target.value })}
            data-testid="ga-site"
          >
            <option value="">Choose a site</option>
            {mySites.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name} ({row.code})
              </option>
            ))}
          </select>
        </Field>
        <PickerField
          id="ga-vendor"
          label="Vendor"
          noun="vendor"
          placeholder="Choose a vendor"
          value={form.vendor_id}
          onChange={(id) => setForm({ ...form, vendor_id: id })}
          picker={vendors}
        />
        <PickerField
          id="ga-brand"
          label="Brand"
          noun="brand"
          placeholder="Choose a brand"
          value={form.brand_id}
          onChange={(id) => setForm({ ...form, brand_id: id })}
          picker={brands}
        />
        <PickerField
          id="ga-booking"
          label="Booking"
          hint="Leave it unbooked if nobody ordered this. A buyer confirms that separately."
          noun="confirmed booking"
          placeholder="No booking"
          value={form.booking_id}
          onChange={(id) => setForm({ ...form, booking_id: id })}
          picker={bookings}
        />
        <Field id="ga-at" label="Arrived at">
          <input
            id="ga-at"
            className="input"
            type="datetime-local"
            value={form.actual_arrival_at}
            onChange={(e) => setForm({ ...form, actual_arrival_at: e.target.value })}
            data-testid="ga-at"
          />
        </Field>
        <Field
          id="ga-transporter"
          label="Transporter reference"
          hint="Shared by everything that came on the same lorry. It never merges two deliveries."
        >
          <input
            id="ga-transporter"
            className="input"
            aria-describedby="ga-transporter-hint"
            value={form.transporter_ref}
            onChange={(e) => setForm({ ...form, transporter_ref: e.target.value })}
            data-testid="ga-transporter"
          />
        </Field>
        <Field id="ga-invoice" label="Invoice number">
          <input
            id="ga-invoice"
            className="input"
            value={form.invoice_number}
            onChange={(e) => setForm({ ...form, invoice_number: e.target.value })}
            data-testid="ga-invoice"
          />
        </Field>
        <Field id="ga-invoice-date" label="Invoice date">
          <input
            id="ga-invoice-date"
            className="input"
            type="date"
            value={form.invoice_date}
            onChange={(e) => setForm({ ...form, invoice_date: e.target.value })}
            data-testid="ga-invoice-date"
          />
        </Field>
        {asksDispatch && (
          <Field
            id="ga-dispatch-date"
            label="Brand's dispatch date"
            hint="From the brand's challan or invoice. Goods on sale or return age from this day; leave it empty if the paper does not say."
          >
            <input
              id="ga-dispatch-date"
              className="input"
              type="date"
              aria-describedby="ga-dispatch-date-hint"
              max={form.actual_arrival_at.slice(0, 10)}
              value={form.brand_dispatch_date}
              onChange={(e) => setForm({ ...form, brand_dispatch_date: e.target.value })}
              data-testid="ga-dispatch-date"
            />
          </Field>
        )}
      </div>

      {candidates.length > 0 && (
        <div className="gr-warning" data-testid="ga-duplicate">
          <h4 className="gr-h4">
            <AlertTriangle size={15} /> This invoice is already recorded
          </h4>
          <p>
            {candidates.length} arrival(s) you can see already carry this vendor and invoice number
            in this company. That is normal when the rest of the cartons follow later — say why you
            are recording another one, and only count the goods actually in front of you.
          </p>
          <ul data-testid="ga-duplicate-list">
            {candidates.map((row) => (
              <li key={row.id}>
                Recorded {formatDateTime(row.recorded_at)} —{" "}
                {row.state === "counted" ? "counted" : "waiting to be counted"}
                {row.transporter_ref ? ` (${row.transporter_ref})` : ""}
              </li>
            ))}
          </ul>
          <Field
            id="ga-duplicate-reason"
            label="Why record another arrival for this invoice?"
            hint="This is a business warning, not a repeated click. Your reason is kept with the arrival, against this exact warning — if another arrival for this invoice is recorded meanwhile, you will be asked to read the warning again."
          >
            <input
              id="ga-duplicate-reason"
              className="input"
              aria-describedby="ga-duplicate-reason-hint"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              data-testid="ga-duplicate-reason"
            />
          </Field>
        </div>
      )}

      <button
        className="btn btn-cta"
        onClick={save}
        disabled={busy || !ready}
        data-testid="ga-save"
      >
        Record arrival
      </button>
      {blockedByDuplicate && (
        <p className="gr-hint" data-testid="ga-blocked">
          Give a reason above before recording this one.
        </p>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// The count: one row per observation, a condition on every row
// --------------------------------------------------------------------------

function blankRow(): Observation {
  return {
    scan_key: crypto.randomUUID(),
    sku_id: null,
    description: "",
    alias_value: null,
    condition: "good",
    qty: 1,
  };
}

/** The governed identity profile every code lookup is read under.
 *
 *  Which profile applies is a fact about the tenant, not something a receiver at
 *  a dock chooses, so this finds the one in force and says plainly when there is
 *  none rather than guessing at one (GSA-T04). */
function useIdentityProfiles() {
  const list = useGoodsFetch<
    Page<ResourceDTO<{ payload?: { family?: string }; versions?: ConfigVersionRow[] }>>,
    IdentityProfileChoice[]
  >(
    "/goods-v1/masters/configurations?kind=identity_profile&limit=50",
    (r) => effectiveProfiles(r.items ?? [], new Date()),
    [],
  );
  return list;
}

/** What one count row's scanned code turned out to be, and what the counter did
 *  about it. `chosen` is only ever set by a person: an ambiguous code has no
 *  answer until somebody picks one (GSA-T05). */
interface RowIdentity {
  looking: boolean;
  resolution: IdentityResolution | null;
  chosen: string | null;
  failure: string;
}

/** One pending row with what its identity turned out to be: a product to record
 *  on the observation, a choice to bind to the scan, or neither. */
interface SettledRow {
  row: Observation;
  sku_id: string | null;
  pick: { chosen_sku_id: string; candidate_hash: string } | null;
}

const NO_IDENTITY: RowIdentity = {
  looking: false,
  resolution: null,
  chosen: null,
  failure: "",
};

/** What the scanned code turned out to be, said in the counter's own words.
 *
 *  Three honest answers and no fourth: this is that product; this is one of
 *  these and you have to say which; nothing here matches this code. The screen
 *  never picks for the counter, and never writes a SKU nobody chose. */
function RowIdentityPanel({
  index,
  row,
  state,
  canLookUp,
  profileMissing,
  onChoose,
}: {
  index: number;
  row: Observation;
  state: RowIdentity;
  canLookUp: boolean;
  profileMissing: boolean;
  onChoose: (skuId: string) => void;
}) {
  if (profileMissing) {
    return (
      <p className="goods-hint" data-testid={`ga-identity-${index}-off`}>
        No approved identity profile, so codes cannot be looked up. Describe the goods in words;
        they are counted as unidentified.
      </p>
    );
  }
  if (!canLookUp && (row.alias_value ?? "").trim()) {
    return (
      <p className="goods-hint" data-testid={`ga-identity-${index}-waiting`}>
        Choose the product family above before this code can be looked up.
      </p>
    );
  }
  if (!canLookUp || !(row.alias_value ?? "").trim()) return null;
  if (state.looking) {
    return (
      <p className="goods-hint" data-testid={`ga-identity-${index}-looking`}>
        Checking the code…
      </p>
    );
  }
  if (state.failure) {
    return (
      <p className="warn-note" data-testid={`ga-identity-${index}-failed`}>
        {state.failure}
      </p>
    );
  }
  if (!state.resolution) return null;
  const { resolution } = state;
  if (resolution.result === "resolved") {
    const only = resolution.candidates[0];
    return (
      <p className="goods-hint" data-testid={`ga-identity-${index}-resolved`}>
        <b>Matched:</b> {only ? candidateLabel(only) : "one product"}
      </p>
    );
  }
  if (resolution.result === "unknown") {
    return (
      <p className="warn-note" data-testid={`ga-identity-${index}-unknown`}>
        {resolutionMessage(resolution)}
      </p>
    );
  }
  return (
    <fieldset className="gr-conditions" data-testid={`ga-identity-${index}-ambiguous`}>
      <legend>{resolutionMessage(resolution)}</legend>
      {resolution.candidates.map((candidate) => (
        <label key={candidate.sku_id} className="gr-condition">
          <input
            type="radio"
            name={`identity-${row.scan_key}`}
            checked={state.chosen === candidate.sku_id}
            onChange={() => onChoose(candidate.sku_id)}
            data-testid={`ga-identity-${index}-pick-${candidate.sku_id}`}
          />
          <span>
            <b>{candidateLabel(candidate)}</b>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

/** Who is holding this count, who held it before, and the way to pass it on.
 *
 *  A handover is not a correction and not a second receipt (GSA-T05): the same
 *  count carries on, with the same scans already acknowledged and the same one
 *  GRN at the end. Only somebody who may receive at this site can be given it,
 *  and the server decides that — this panel only asks. */
function HandoverPanel({
  session,
  me,
  toHumanId,
  reason,
  busy,
  onToHumanId,
  onReason,
  onHandOver,
}: {
  session: ResourceDTO<CountSessionData>;
  me: string;
  toHumanId: string;
  reason: string;
  busy: boolean;
  onToHumanId: (value: string) => void;
  onReason: (value: string) => void;
  onHandOver: () => void;
}) {
  const [open, setOpen] = useState(false);
  const mine = session.data.counter_id === me || session.data.entry_user_id === me;
  const chain = session.data.handovers ?? [];
  // There is no read here that turns a person ID into a name, so the ID is
  // shown as an ID — set in the identifier face, not buried in a sentence as
  // though it were a name (design language §"Identifiers"). "You" is the one
  // person this screen can name honestly.
  const who = (humanId: string) =>
    humanId === me ? <b>you</b> : <code className="mono">{humanId}</code>;

  return (
    <div className="gr-handover" data-testid="ga-handover">
      <p className="gr-hint" data-testid="ga-handover-owner">
        {mine
          ? "This count is yours to finish."
          : "Somebody else is holding this count. Only they can hand it on."}
      </p>

      {chain.length > 0 && (
        <ol className="gr-handover-chain" data-testid="ga-handover-chain">
          {chain.map((row) => (
            <li key={row.id}>
              {formatDateTime(row.recorded_at)} — handed from {who(row.from_human_id)} to{" "}
              {who(row.to_human_id)}, because: {row.reason_code}
            </li>
          ))}
        </ol>
      )}

      {mine &&
        (open ? (
          <div data-testid="ga-handover-form">
            <Field
              id="ga-handover-to"
              label="Who is taking it over"
              hint="Their person ID. They must already be allowed to receive goods at this site."
            >
              <input
                id="ga-handover-to"
                className="input"
                aria-describedby="ga-handover-to-hint"
                value={toHumanId}
                onChange={(e) => onToHumanId(e.target.value)}
                data-testid="ga-handover-to"
              />
            </Field>
            <Field
              id="ga-handover-reason"
              label="Why it is changing hands"
              hint="A short reason, such as SHIFT_ENDED. It is kept with the count."
            >
              <input
                id="ga-handover-reason"
                className="input"
                aria-describedby="ga-handover-reason-hint"
                value={reason}
                onChange={(e) => onReason(e.target.value)}
                data-testid="ga-handover-reason"
              />
            </Field>
            <div className="toolbar">
              <button className="btn btn-sm" onClick={() => setOpen(false)}>
                <X size={14} /> Never mind
              </button>
              <div className="spacer" />
              <button
                className="btn btn-cta"
                onClick={onHandOver}
                disabled={busy || !toHumanId.trim() || !reason.trim()}
                data-testid="ga-handover-submit"
              >
                Hand the count over
              </button>
            </div>
          </div>
        ) : (
          <button
            className="btn btn-sm"
            onClick={() => setOpen(true)}
            data-testid="ga-handover-open"
          >
            Hand this count to someone else
          </button>
        ))}
    </div>
  );
}

function CountPanel({
  arrival,
  onIssued,
}: {
  arrival: ResourceDTO<ArrivalData>;
  onIssued: (grnId: string) => void;
}) {
  const { session } = useAuth();
  const me = session?.user.human_id ?? "";
  const [sessionDoc, setSessionDoc] = useState<ResourceDTO<CountSessionData> | null>(null);
  const [pending, setPending] = useState<Observation[]>([blankRow()]);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const [remarks, setRemarks] = useState<Record<string, string>>({});
  const [handTo, setHandTo] = useState("");
  const [handReason, setHandReason] = useState("");
  const firstQty = useRef<HTMLInputElement>(null);
  const profiles = useIdentityProfiles();
  const vendors = useNames("/goods-v1/vendors?limit=100");
  const [identity, setIdentity] = useState<Record<string, RowIdentity>>({});
  const [aliasType, setAliasType] = useState<AliasContext["alias_type"]>("barcode");
  const [familyId, setFamilyId] = useState("");

  // The unfinished count this arrival already has, if any. Somebody who was
  // handed a count never saw the response that opened it, so the only way in is
  // to look it up (E249).
  const open = useGoodsFetch<
    Page<ResourceDTO<CountSessionData>>,
    ResourceDTO<CountSessionData> | null
  >(
    `/goods-v1/inbound/arrivals/${arrival.id}/sessions`,
    (r) => (r.items ?? []).find((row) => row.data.state === "open") ?? null,
    null,
  );
  const live = sessionDoc ?? open.value;

  /** Throw away what this screen is holding and read the count again.
   *
   *  Somebody else at this site may be counting into the same session, so a
   *  refusal that says the count moved on is answered by reading it, not by
   *  sending the same stale hash again. */
  function reload() {
    setSessionDoc(null);
    open.reload();
  }

  // What the server actually holds, not what this screen remembers. A resumed
  // count shows the pieces the person before recorded; nothing is "counted so
  // far" until the server has acknowledged it (design §8.4).
  const recorded = useMemo<Observation[]>(
    () =>
      (live?.data.observations ?? []).map((row) => ({
        scan_key: row.scan_key,
        sku_id: row.sku_id,
        description: row.description,
        alias_value: row.alias_value,
        condition: row.condition,
        qty: row.qty,
      })),
    [live],
  );

  const totals = useMemo(() => conditionTotals(recorded), [recorded]);
  const unchosen = useMemo(
    () =>
      pending.filter((row) => {
        const state = identity[row.scan_key];
        return needsChoice(state?.resolution ?? null, state?.chosen ?? null);
      }),
    [pending, identity],
  );
  const issuerKey = useMemo(() => {
    const vendor = vendors.value.find((row) => row.id === arrival.data.vendor_id);
    return vendor?.data.code ?? "";
  }, [vendors.value, arrival.data.vendor_id]);
  // With one family in force there is nothing to ask; with several, reading a
  // code under the wrong one answers "no product matches" and means nothing of
  // the sort, so the receiver picks the family the goods belong to.
  const profileId = profiles.value.length === 1 ? profiles.value[0].id : familyId;
  const canLookUp = Boolean(profileId && issuerKey);

  function context(): AliasContext {
    return {
      site_id: arrival.data.site_id,
      issuer_key: issuerKey,
      alias_type: aliasType,
      as_of: new Date().toISOString(),
      profile_version_id: profileId,
    };
  }

  /** Ask the server what this code means. The screen never decides: a code that
   *  matches one product is settled, one that matches several waits for a person,
   *  and one that matches nothing is counted as unidentified (E090, GSA-T05). */
  async function look(row: Observation) {
    const value = (row.alias_value ?? "").trim();
    if (!value || !canLookUp) return;
    setIdentity((was) => ({ ...was, [row.scan_key]: { ...NO_IDENTITY, looking: true } }));
    try {
      const { data } = await api.get<IdentityResolution>(
        "/goods-v1/masters/skus/lookup",
        { params: { value, ...context() } },
      );
      setIdentity((was) => ({
        ...was,
        [row.scan_key]: { ...NO_IDENTITY, resolution: data },
      }));
    } catch (e) {
      setIdentity((was) => ({
        ...was,
        [row.scan_key]: { ...NO_IDENTITY, failure: apiErrorMessage(e) },
      }));
    }
  }

  async function openSession() {
    setError("");
    setBusy(true);
    try {
      const { data } = await api.post<ResourceDTO<CountSessionData>>(
        `/goods-v1/inbound/arrivals/${arrival.id}/sessions`,
        { counter_id: me, entry_user_id: me, ...goodsMeta() },
      );
      setSessionDoc(data);
      setOk("Count started. Nothing is recorded until each row is sent.");
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function record() {
    if (!live) return;
    setError("");
    setOk("");
    setBusy(true);
    const rows = pending.filter((row) => row.qty > 0 && (row.sku_id || row.description.trim()));
    // A code that matched exactly one product IS that product; anything still
    // waiting on a person's choice is recorded with no SKU and settled by an
    // E091 pick below, so the observation stays what the counter actually saw.
    const settled: SettledRow[] = rows.map((row) => ({
      row,
      ...identityForRow(
        identity[row.scan_key]?.resolution ?? null,
        identity[row.scan_key]?.chosen ?? null,
      ),
    }));
    try {
      const { data } = await api.post<ResourceDTO<CountSessionData>>(
        `/goods-v1/inbound/count-sessions/${live.id}/observations`,
        {
          observations: settled.map(({ row, sku_id }) => ({
            scan_key: row.scan_key,
            sku_id,
            description: row.description,
            // The scope this code was read under travels with the scan, so the
            // GRN judges it the same way the counter did rather than re-judging
            // it under a wider one (design E117 step 13).
            ...(row.alias_value
              ? {
                  alias_value: row.alias_value,
                  alias_context: {
                    issuer_key: issuerKey,
                    alias_type: aliasType,
                    profile_version_id: profileId,
                  },
                }
              : {}),
            condition: row.condition,
            qty: row.qty,
          })),
          ...goodsMeta(live.revision),
        },
      );
      setSessionDoc(data);
      // Only what the server acknowledged moves out of pending (design §8.4).
      const failed = await recordPicks(settled, data);
      // A row whose product choice was refused keeps its candidates and its
      // chosen product, so the receiver can try again. Clearing it would leave
      // those goods unidentified for good and the GRN refused with no way back.
      const keep = new Set(failed.map((entry) => entry.row.scan_key));
      setPending(rows.filter((row) => keep.has(row.scan_key)).concat(blankRow()));
      setIdentity((was) =>
        Object.fromEntries(Object.entries(was).filter(([key]) => keep.has(key))),
      );
      if (failed.length === 0) {
        setOk(`${rows.length} row(s) recorded.`);
      }
      firstQty.current?.focus();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  /** Bind each chosen product to the exact scan it belongs to (E091).
   *
   *  A pick cannot be made before the scan exists — it binds to that scan — so
   *  this runs straight after the observations are acknowledged, against the ids
   *  the server just answered with. Returns the entries whose pick did not land,
   *  so the caller can keep those rows on screen to try again; leaving them
   *  unresolved is what the GRN refuses to freeze, which is the point. */
  async function recordPicks(
    settled: SettledRow[],
    doc: ResourceDTO<CountSessionData>,
  ): Promise<SettledRow[]> {
    const wanted = settled.filter((entry) => entry.pick);
    if (wanted.length === 0) return [];
    const byScanKey = new Map((doc.data.observations ?? []).map((o) => [o.scan_key, o.id]));
    const failed: SettledRow[] = [];
    const reasons: string[] = [];
    for (const entry of wanted) {
      const observationId = byScanKey.get(entry.row.scan_key);
      const named = entry.row.alias_value ?? entry.row.description;
      if (!observationId) {
        failed.push(entry);
        reasons.push(`${named}: the count did not answer with this scan`);
        continue;
      }
      try {
        await api.post("/goods-v1/masters/identity-picks", {
          context: context(),
          value: entry.row.alias_value,
          candidate_hash: entry.pick!.candidate_hash,
          chosen_sku_id: entry.pick!.chosen_sku_id,
          scan_event_id: observationId,
          ...goodsMeta(),
        });
      } catch (e) {
        failed.push(entry);
        reasons.push(`${named}: ${apiErrorMessage(e)}`);
      }
    }
    if (failed.length > 0) {
      setError(
        `The product choice was not recorded for ${reasons.join("; ")}. The rows are still here; ` +
          "choose again and record them.",
      );
    }
    return failed;
  }

  async function issue() {
    if (!live) return;
    setError("");
    setOk("");
    setBusy(true);
    try {
      const { data } = await api.post<{ id: string }>("/goods-v1/inbound/grns", {
        count_session_id: live.id,
        reviewed_hash: live.content_hash,
        ...(Object.keys(remarks).length
          ? {
              remarks: Object.entries(remarks).map(([claim_line_key, remark]) => ({
                claim_line_key,
                remark,
              })),
            }
          : {}),
        ...goodsMeta(live.revision),
      });
      onIssued(data.id);
    } catch (e) {
      setError(apiErrorMessage(e));
      // A refused issue names the claim lines whose difference still needs a
      // remark. Offering the boxes here is the only way to answer it.
      const issues = (e as { response?: { data?: { details?: { issues?: Issue[] } } } })?.response
        ?.data?.details?.issues;
      for (const problem of issues ?? []) {
        if (problem.code === "REMARK_REQUIRED" && problem.line_key) {
          setRemarks((was) => ({ ...was, [problem.line_key as string]: was[problem.line_key as string] ?? "" }));
        }
      }
    } finally {
      setBusy(false);
    }
  }

  /** Pass this unfinished count to somebody else who may receive here (E240).
   *
   *  The count does not restart: same session, same scans already acknowledged,
   *  one GRN at the end. Only who is holding it changes. */
  async function handOver() {
    if (!live) return;
    setError("");
    setOk("");
    setBusy(true);
    try {
      const { data } = await api.post<ResourceDTO<CountSessionData>>(
        `/goods-v1/inbound/count-sessions/${live.id}/handover`,
        {
          to_human_id: handTo.trim(),
          reason_code: handReason.trim(),
          reviewed_hash: live.content_hash,
          ...goodsMeta(live.revision),
        },
      );
      setSessionDoc(data);
      setHandTo("");
      setHandReason("");
      setOk("The count is now theirs. Everything counted so far stays on it.");
      open.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function setRow(index: number, patch: Partial<Observation>) {
    setPending((rows) => rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  if (open.loading) return <p className="gr-hint">Loading this arrival's count…</p>;
  if (open.denied) return <Denied what="count for this delivery" />;

  if (!live) {
    return (
      <div className="gr-panel" data-testid="ga-count-start">
        <h4 className="gr-h4">
          <ClipboardCheck size={15} /> Count these goods
        </h4>
        {open.failure ? (
          // A failed read is not "there is no count". Offering "Start the count"
          // here would invite a second count beside one that may well exist.
          <p className="warn-note" data-testid="ga-count-unknown">
            This delivery's count could not be read, so it is not known whether one is already
            under way. {open.failure}
          </p>
        ) : (
          <p className="gr-hint">
            You are recorded as both the counter and the person entering it. If you cannot finish
            it, hand it to somebody else here rather than starting a second count.
          </p>
        )}
        <Feedback error={error} ok={ok} />
        <button
          className="btn btn-cta"
          onClick={open.failure ? reload : openSession}
          disabled={busy}
          data-testid={open.failure ? "ga-count-retry" : "ga-count-open"}
        >
          {open.failure ? "Try reading it again" : "Start the count"}
        </button>
      </div>
    );
  }

  return (
    <div className="gr-panel gr-count" data-testid="ga-count">
      <h4 className="gr-h4">
        <ClipboardCheck size={15} /> Counting
      </h4>
      <Feedback error={error} ok={ok} />

      <div className="gr-totals" data-testid="ga-totals" aria-live="polite">
        {CONDITIONS.map((condition) => (
          <div key={condition} data-testid={`ga-total-${condition}`}>
            <span className="gr-total-n">{totals[condition]}</span>
            <span className="gr-total-label">{CONDITION_LABEL[condition]}</span>
          </div>
        ))}
        <div className="gr-total-all" data-testid="ga-total-all">
          <span className="gr-total-n">{countedTotal(recorded)}</span>
          <span className="gr-total-label">Counted so far</span>
        </div>
      </div>

      <HandoverPanel
        session={live}
        me={me}
        toHumanId={handTo}
        reason={handReason}
        busy={busy}
        onToHumanId={setHandTo}
        onReason={setHandReason}
        onHandOver={handOver}
      />

      {profiles.value.length > 1 && (
        <Field
          id="ga-family"
          label="What kind of product is this"
          hint="Codes are read under this product family's own rules. The wrong family answers 'no product matches' for goods that are perfectly well known."
        >
          <select
            id="ga-family"
            className="input"
            aria-describedby="ga-family-hint"
            value={familyId}
            onChange={(e) => {
              setFamilyId(e.target.value);
              setIdentity({});
            }}
            data-testid="ga-family"
          >
            <option value="">Choose a product family</option>
            {profiles.value.map((choice) => (
              <option key={choice.id} value={choice.id}>
                {choice.family}
              </option>
            ))}
          </select>
        </Field>
      )}

      <Field
        id="ga-alias-type"
        label="What kind of code is on these goods"
        hint={
          issuerKey
            ? `Codes are read as ${issuerKey}'s, because that is the vendor on this arrival.`
            : "The vendor on this arrival could not be read, so codes cannot be looked up."
        }
      >
        <select
          id="ga-alias-type"
          className="input"
          aria-describedby="ga-alias-type-hint"
          value={aliasType}
          onChange={(e) => {
            setAliasType(e.target.value as AliasContext["alias_type"]);
            setIdentity({});
          }}
          data-testid="ga-alias-type"
        >
          {ALIAS_TYPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </Field>

      {pending.map((row, index) => (
        <fieldset className="gr-row" key={row.scan_key} data-testid={`ga-row-${index}`}>
          <legend className="sr-only">Row {index + 1}</legend>
          <div className="gr-row-qty">
            <label htmlFor={`ga-qty-${index}`}>How many</label>
            <input
              id={`ga-qty-${index}`}
              ref={index === 0 ? firstQty : undefined}
              className="input gr-big"
              type="number"
              inputMode="numeric"
              min={1}
              value={row.qty}
              onChange={(e) => setRow(index, { qty: Number(e.target.value) })}
              data-testid={`ga-qty-${index}`}
            />
          </div>
          <div className="gr-row-rest">
            <Field
              id={`ga-alias-${index}`}
              label="Barcode or vendor code"
              hint={
                canLookUp
                  ? "Scan it, or type it and press Enter. Leave it empty if there is nothing to scan."
                  : "Codes cannot be looked up here, so this is only recorded as typed."
              }
            >
              <input
                id={`ga-alias-${index}`}
                className="input"
                aria-describedby={`ga-alias-${index}-hint`}
                value={row.alias_value ?? ""}
                onChange={(e) => {
                  setRow(index, { alias_value: e.target.value || null });
                  setIdentity((was) => ({ ...was, [row.scan_key]: NO_IDENTITY }));
                }}
                onBlur={() => look(row)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void look(row);
                  }
                }}
                data-testid={`ga-alias-${index}`}
              />
            </Field>
            <RowIdentityPanel
              index={index}
              row={row}
              state={identity[row.scan_key] ?? NO_IDENTITY}
              canLookUp={canLookUp}
              profileMissing={profiles.value.length === 0 && !profiles.loading}
              onChoose={(sku_id) =>
                setIdentity((was) => ({
                  ...was,
                  [row.scan_key]: { ...(was[row.scan_key] ?? NO_IDENTITY), chosen: sku_id },
                }))
              }
            />
            <Field
              id={`ga-desc-${index}`}
              label="What is it"
              hint="In words. Pieces nobody can identify are described here and carry no SKU."
            >
              <input
                id={`ga-desc-${index}`}
                className="input"
                aria-describedby={`ga-desc-${index}-hint`}
                value={row.description}
                onChange={(e) => setRow(index, { description: e.target.value })}
                data-testid={`ga-desc-${index}`}
              />
            </Field>
            <fieldset className="gr-conditions">
              <legend>Condition</legend>
              {CONDITIONS.map((condition) => (
                <label key={condition} className="gr-condition">
                  <input
                    type="radio"
                    name={`condition-${row.scan_key}`}
                    checked={row.condition === condition}
                    onChange={() => setRow(index, { condition })}
                    data-testid={`ga-condition-${index}-${condition}`}
                  />
                  <span>
                    <b>{CONDITION_LABEL[condition]}</b>
                    <small>{CONDITION_HELP[condition]}</small>
                  </span>
                </label>
              ))}
            </fieldset>
          </div>
          <button
            className="btn btn-sm"
            onClick={() => setPending((rows) => rows.filter((_, i) => i !== index))}
            disabled={pending.length === 1}
            aria-label={`Remove row ${index + 1}`}
            data-testid={`ga-row-remove-${index}`}
          >
            <Trash2 size={14} />
          </button>
        </fieldset>
      ))}

      <div className="toolbar">
        <button
          className="btn btn-sm"
          onClick={() => setPending((rows) => [...rows, blankRow()])}
          data-testid="ga-row-add"
        >
          <Plus size={14} /> Another row
        </button>
        {/* Somebody else at this site may be counting into the same session. A
            refusal that says the count moved on is only answerable by reading
            it again, so the way to do that is on the screen. */}
        <button
          className="btn btn-sm"
          onClick={reload}
          disabled={busy}
          data-testid="ga-count-reload"
        >
          Read the count again
        </button>
        <div className="spacer" />
        <button
          className="btn btn-cta"
          onClick={record}
          disabled={busy || unchosen.length > 0}
          data-testid="ga-record"
        >
          Record these rows
        </button>
      </div>
      {unchosen.length > 0 && (
        <p className="warn-note" data-testid="ga-needs-choice">
          {unchosen.length} row(s) scanned a code that matches more than one product. Choose which
          product each one is before recording them.
        </p>
      )}

      {recorded.length > 0 && (
        <div className="table-wrap">
          <table className="data" data-testid="ga-recorded">
            <caption className="sr-only">Rows the server has acknowledged</caption>
            <thead>
              <tr>
                <th className="num">Quantity</th>
                <th>What</th>
                <th>Condition</th>
              </tr>
            </thead>
            <tbody>
              {recorded.map((row) => (
                <tr key={row.scan_key}>
                  <td className="num">{row.qty}</td>
                  <td>{row.description || row.alias_value || NOT_GIVEN}</td>
                  <td>{CONDITION_LABEL[row.condition]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {Object.keys(remarks).length > 0 && (
        <div className="gr-warning" data-testid="ga-remarks">
          <h4 className="gr-h4">Every difference from the invoice needs a remark</h4>
          {Object.keys(remarks).map((key) => (
            <Field key={key} id={`ga-remark-${key}`} label={`Why the count differs on line ${key.slice(0, 8)}`}>
              <input
                id={`ga-remark-${key}`}
                className="input"
                value={remarks[key]}
                onChange={(e) => setRemarks({ ...remarks, [key]: e.target.value })}
                data-testid={`ga-remark-${key}`}
              />
            </Field>
          ))}
        </div>
      )}

      <div className="gr-issue">
        <p>
          <b>The GRN records quantity only.</b> It says how many pieces arrived and what condition
          they are in. It carries no cost, no value and no effect on the books — value is decided
          later, on a PT.
        </p>
        <button
          className="btn btn-cta"
          onClick={issue}
          disabled={busy || recorded.length === 0}
          data-testid="ga-issue"
        >
          Issue GRN
        </button>
      </div>
    </div>
  );
}

interface Issue {
  code: string;
  line_key?: string | null;
  message?: string;
}

// --------------------------------------------------------------------------
// Arrival detail: the facts, the invoice, the count
// --------------------------------------------------------------------------

interface InvoiceLineDraft {
  line_key: string;
  description: string;
  claimed_qty: number;
  /** Rupees as typed; sent only where the three-way match asks for cost. */
  cost: string;
}

function newInvoiceLine(): InvoiceLineDraft {
  return { line_key: crypto.randomUUID(), description: "", claimed_qty: 1, cost: "" };
}

function InvoicePanel({
  arrival,
  onDone,
}: {
  arrival: ResourceDTO<ArrivalData>;
  onDone: () => void;
}) {
  const [number, setNumber] = useState(arrival.data.invoice_number ?? "");
  const [date, setDate] = useState(arrival.data.invoice_date ?? "");
  const [lines, setLines] = useState<InvoiceLineDraft[]>([newInvoiceLine()]);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  // Ticket 37: the invoice's cost is asked for only where the three-way match is
  // on and the server says this login may see cost. Everyone else never sees it.
  const match = useThreeWay(threeWayOn(arrival) ? arrival.id : null);
  const askCost = match.kind === "ready" && match.data.sees_cost;
  // Until the match says whether this login may give cost, the invoice waits: a
  // line sent without its cost would read as "no cost given".
  const matchPending = threeWayOn(arrival) && match.kind === "loading";
  const badCost = askCost && lines.some((line) => rupeesToPaiseString(line.cost) === undefined);

  async function submit() {
    setError("");
    setOk("");
    setBusy(true);
    try {
      await api.post(`/goods-v1/inbound/arrivals/${arrival.id}/invoice`, {
        invoice_number: number,
        invoice_date: date,
        lines: lines.map(({ cost, ...line }) =>
          askCost ? { ...line, invoice_basic_paise: rupeesToPaiseString(cost) ?? null } : line,
        ),
        ...goodsMeta(arrival.revision),
      });
      setOk("Invoice recorded. What it claims is compared with what is counted; it is never cost.");
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gr-panel" data-testid="ga-invoice-panel">
      <h4 className="gr-h4">What the invoice claims</h4>
      <p className="gr-hint">
        Optional. Recording it lets the GRN say where the count and the invoice differ. A claim is
        never treated as cost.
        {askCost &&
          " The cost per piece you type is only compared with the booking's cost; it never becomes the stock's cost."}
      </p>
      <Feedback error={error} ok={ok} />
      <div className="form-grid">
        <Field id="ga-inv-number" label="Invoice number">
          <input
            id="ga-inv-number"
            className="input"
            value={number}
            onChange={(e) => setNumber(e.target.value)}
            data-testid="ga-inv-number"
          />
        </Field>
        <Field id="ga-inv-date" label="Invoice date">
          <input
            id="ga-inv-date"
            className="input"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            data-testid="ga-inv-date"
          />
        </Field>
      </div>
      {lines.map((line, index) => (
        <div className="form-grid" key={line.line_key}>
          <Field id={`ga-inv-desc-${index}`} label={`Line ${index + 1}: what it says`}>
            <input
              id={`ga-inv-desc-${index}`}
              className="input"
              value={line.description}
              onChange={(e) =>
                setLines((rows) =>
                  rows.map((row, i) => (i === index ? { ...row, description: e.target.value } : row)),
                )
              }
              data-testid={`ga-inv-desc-${index}`}
            />
          </Field>
          <Field id={`ga-inv-qty-${index}`} label={`Line ${index + 1}: how many claimed`}>
            <input
              id={`ga-inv-qty-${index}`}
              className="input"
              type="number"
              min={0}
              value={line.claimed_qty}
              onChange={(e) =>
                setLines((rows) =>
                  rows.map((row, i) =>
                    i === index ? { ...row, claimed_qty: Number(e.target.value) } : row,
                  ),
                )
              }
              data-testid={`ga-inv-qty-${index}`}
            />
          </Field>
          {askCost && (
            <Field
              id={`ga-inv-cost-${index}`}
              label={`Line ${index + 1}: cost per piece before tax (₹)`}
              hint={
                rupeesToPaiseString(line.cost) === undefined
                  ? "Type rupees, like 520 or 520.50."
                  : undefined
              }
            >
              <input
                id={`ga-inv-cost-${index}`}
                className="input"
                inputMode="decimal"
                value={line.cost}
                aria-describedby={`ga-inv-cost-${index}-hint`}
                onChange={(e) =>
                  setLines((rows) =>
                    rows.map((row, i) => (i === index ? { ...row, cost: e.target.value } : row)),
                  )
                }
                data-testid={`ga-inv-cost-${index}`}
              />
            </Field>
          )}
        </div>
      ))}
      <div className="toolbar">
        <button
          className="btn btn-sm"
          onClick={() =>
            setLines((rows) => [...rows, newInvoiceLine()])
          }
          data-testid="ga-inv-add"
        >
          <Plus size={14} /> Another invoice line
        </button>
        <div className="spacer" />
        <button
          className="btn btn-cta btn-sm"
          onClick={submit}
          disabled={busy || !number || !date || badCost || matchPending}
          data-testid="ga-inv-submit"
        >
          Record the invoice
        </button>
      </div>
    </div>
  );
}

/** Which part of an arrival to show. The screen shows the record whole; the
 *  receiving workflow separates the two steps it walks - the paperwork on the
 *  arrival itself, then the count that issues the GRN - so each step's panel
 *  shows only its own work while both read the same record. */
export type ArrivalSection = "all" | "arrival" | "count";

/** One arrival: what came, from whom, its invoice, and the count that turns it
 *  into a GRN. Rendered by the Arrivals screen and by the receiving workflow's
 *  Arrival and Count steps, so there is one panel and not three. */
export function ArrivalPanel({
  arrivalId,
  onClose,
  section = "all",
}: {
  arrivalId: string;
  /** Absent ⇒ no close button: the workflow closes the delivery, not the step. */
  onClose?: () => void;
  section?: ArrivalSection;
}) {
  const { session } = useAuth();
  const mySites = session?.sites ?? [];
  const doc = useResourceDoc<ArrivalData>(`/goods-v1/inbound/arrivals/${arrivalId}`);
  const vendors = useNames("/goods-v1/vendors?limit=100");
  const brands = useNames("/goods-v1/masters/brands?limit=100");
  const [issued, setIssued] = useState("");

  if (doc.denied) return <Denied what="arrival" />;
  // Only while there is nothing to show. A refresh after a write keeps the
  // record on screen — tearing the panels down mid-reload would also take the
  // message that says what just happened, and whatever was half-typed with it.
  if (doc.loading && !doc.doc) return <p className="muted">Loading…</p>;
  if (doc.failure) return <div className="warn-note">{doc.failure}</div>;
  if (!doc.doc) return <Denied what="arrival" />;

  const arrival = doc.doc;

  return (
    <div data-testid="ga-detail">
      <div className="toolbar">
        <h3 className="h3">Arrival at {siteName(mySites, arrival.data.site_id)}</h3>
        <span className={`chip chip-${arrival.state === "counted" ? "green" : "amber"}`}>
          {arrival.state === "counted" ? "Counted" : "Waiting to be counted"}
        </span>
        <div className="spacer" />
        {onClose && (
          <button className="btn btn-sm" onClick={onClose} data-testid="ga-detail-close">
            <X size={14} /> Close
          </button>
        )}
      </div>

      {section !== "count" && (
      <dl className="gr-facts" data-testid="ga-facts">
        <div>
          <dt>Vendor</dt>
          <dd>{vendors.name(arrival.data.vendor_id)}</dd>
        </div>
        <div>
          <dt>Brand</dt>
          <dd>{brands.name(arrival.data.brand_id)}</dd>
        </div>
        <div>
          <dt>Arrived</dt>
          <dd>{formatDateTime(arrival.data.actual_arrival_at)}</dd>
        </div>
        <div>
          <dt>Transporter reference</dt>
          <dd>{orNotGiven(arrival.data.transporter_ref)}</dd>
        </div>
        <div>
          <dt>Booking</dt>
          <dd>{arrival.data.booking_id ?? "Unbooked"}</dd>
        </div>
        <div>
          <dt>Invoice</dt>
          <dd>{orNotGiven(arrival.data.invoice_number)}</dd>
        </div>
        {arrival.data.duplicate_acknowledgement && (
          <div>
            <dt>Recorded despite the duplicate warning</dt>
            <dd data-testid="ga-duplicate-answer">
              {arrival.data.duplicate_acknowledgement.reason}
            </dd>
          </div>
        )}
      </dl>
      )}

      {issued ? (
        <div className="ok-note" data-testid="ga-issued">
          GRN issued.{" "}
          <Link to={deliveryStepPath("grn", issued, "grn")}>Open the GRN</Link>
        </div>
      ) : (
        <>
          {section !== "count" && <InvoicePanel arrival={arrival} onDone={doc.reload} />}
          {section !== "arrival" && <CountPanel arrival={arrival} onIssued={setIssued} />}
        </>
      )}
    </div>
  );
}
