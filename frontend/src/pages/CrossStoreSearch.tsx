import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { PackageSearch, Store as StoreIcon } from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { useList } from "../lib/hooks";
import { withQuery } from "../lib/query";
import { formatDateTime } from "../lib/format";
import { canWriteTransfer } from "../lib/outbound-rbac";
import { type LocationT } from "../lib/transfer-locations";
import {
  MIN_TERM,
  quotedArrivalIso,
  requestBody,
  rowsFor,
  storeIdByCode,
  totalQty,
  type AvailabilityDesignT,
  type AvailabilityResponseT,
  type AvailabilityRowT,
  type PickedRowT,
} from "../lib/cross-store";
import { SearchBox } from "../components/SearchBox";
import { PageHeader } from "../components/PageHeader";
import "./Booking.css";

// ---------------------------------------------------------------------------
// Search across every store, size by size (#175, D10 §3)
// ---------------------------------------------------------------------------
//
// A customer is standing at the counter asking for a size this store has not
// got. This screen answers "who has it?" for the whole network — quantities
// only; the endpoint carries no cost at all, by construction, so there is
// nothing money-shaped here to hide or to leak.
//
// Nothing on this screen reserves anything. "Request this" raises the ordinary
// stock request, carrying the time the counter quoted, and that ask walks its
// own approval route (own store manager → Operations Head). The piece can still
// sell where it stands in the meantime, which is why the customer is asked to
// come back rather than promised a piece.

interface StoreT {
  id: number;
  code: string;
  name: string;
}

export default function CrossStoreSearch() {
  const { user } = useAuth();
  const [q, setQ] = useState("");
  const [data, setData] = useState<AvailabilityResponseT | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [picked, setPicked] = useState<PickedRowT | null>(null);

  const term = q.trim();
  const searchable = term.length >= MIN_TERM;

  useEffect(() => {
    if (!searchable) {
      setData(null);
      setError("");
      return;
    }
    let live = true;
    setLoading(true);
    setError("");
    api
      .get(withQuery("/stock/availability", { q: term }))
      .then((r) => live && setData(r.data))
      .catch((e) => live && setError(apiErrorMessage(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [term, searchable]);

  // Picking a new row closes whatever panel was open, so the form on screen
  // always belongs to the row above it.
  function pick(design: AvailabilityDesignT, row: AvailabilityRowT) {
    setPicked((current) =>
      current?.row.sku_code === row.sku_code && current.row.store === row.store
        ? null
        : { design, row },
    );
  }

  return (
    <div className="page-pad">
      <PageHeader lead="Every store's stock, size by size. Quantities only — another store's cost stays theirs." />

      <div className="filter-bar" data-testid="availability-search-bar">
        <SearchBox
          value={q}
          onChange={setQ}
          placeholder="Scan the tag, or type a design number or item name"
          label="Search stock across every store"
          testId="availability-search"
        />
      </div>

      {!searchable && (
        <div className="card section-card" data-testid="availability-hint">
          <p className="lead">
            Type at least {MIN_TERM} characters — or scan the tag — to see who has this piece.
          </p>
        </div>
      )}

      {searchable && loading && <p className="lead">Searching every store…</p>}

      {searchable && !loading && error && (
        <div className="warn-note" data-testid="availability-error">
          {error}
        </div>
      )}

      {searchable && !loading && !error && data && (
        <>
          {data.truncated && (
            <div className="warn-note" data-testid="availability-truncated">
              Showing the first {data.results.length} styles only. Narrow the search — scan the tag
              or type the full design number.
            </div>
          )}

          {data.results.length === 0 ? (
            <div className="card section-card" data-testid="availability-empty">
              <p className="lead">Nobody in the network is holding anything matching “{term}”.</p>
            </div>
          ) : (
            data.results.map((design) => (
              <DesignCard
                key={`${design.brand}/${design.design}`}
                design={design}
                picked={picked}
                onPick={pick}
                canRequest={canWriteTransfer(user)}
                onDone={() => setPicked(null)}
              />
            ))
          )}
        </>
      )}
    </div>
  );
}

function DesignCard({
  design,
  picked,
  onPick,
  canRequest,
  onDone,
}: {
  design: AvailabilityDesignT;
  picked: PickedRowT | null;
  onPick: (design: AvailabilityDesignT, row: AvailabilityRowT) => void;
  canRequest: boolean;
  onDone: () => void;
}) {
  const rows = rowsFor(design);
  // Two brands may both number a style "1001", so the key carries both — the
  // server groups on the pair and this list has to agree with it.
  const testId = `availability-design-${design.brand}-${design.design}`;

  return (
    <div className="card section-card" data-testid={testId}>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <div>
          <p className="eyebrow">{design.brand || "—"}</p>
          <h3 className="h3">
            <span className="mono">{design.design || "—"}</span>
            {design.item ? ` · ${design.item}` : ""}
          </h3>
        </div>
        <div className="spacer" />
        <span className="chip chip-navy" data-testid={`${testId}-total`}>
          {totalQty(design)} pcs in the network
        </span>
      </div>

      <table className="lines-table" data-testid={`${testId}-rows`}>
        <thead>
          <tr>
            <th>Size</th>
            <th>Where</th>
            <th>Colour</th>
            <th className="num">Qty</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => {
            const isPicked =
              picked?.row.sku_code === row.sku_code && picked.row.store === row.store;
            return [
              <tr key={`${row.store}-${row.sku_code}`} data-testid={`${testId}-row-${i}`}>
                <td>
                  <b>{row.size || "—"}</b>
                </td>
                <td>
                  <StoreIcon size={13} /> <b className="mono">{row.store}</b>{" "}
                  <span className="lead">{row.store_name}</span>
                </td>
                <td>{row.color || "—"}</td>
                <td className="num">{row.qty}</td>
                <td>
                  {canRequest && (
                    <button
                      type="button"
                      className="btn"
                      onClick={() => onPick(design, row)}
                      data-testid={`${testId}-request-${i}`}
                    >
                      <PackageSearch size={14} /> {isPicked ? "Cancel" : "Request this"}
                    </button>
                  )}
                </td>
              </tr>,
              isPicked ? (
                <tr key="request-panel">
                  <td colSpan={5}>
                    <RequestPanel design={design} row={row} onDone={onDone} />
                  </td>
                </tr>
              ) : null,
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

/** The ask itself: how many, and the time the counter is about to quote.
 *
 *  Deliberately not a modal — a store person is talking to a customer while they
 *  fill this in, and the row they picked has to stay on screen above it. */
function RequestPanel({
  design,
  row,
  onDone,
}: {
  design: AvailabilityDesignT;
  row: AvailabilityRowT;
  onDone: () => void;
}) {
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data: stores } = useList<StoreT>("/masters/stores");
  const { data: locations } = useList<LocationT>("/masters/locations");

  const storeLocked = user?.scope_type === "store" && (user?.stores?.length ?? 0) >= 1;
  const lockedStore = storeLocked ? user!.stores[0] : null;

  const [requestingId, setRequestingId] = useState(lockedStore ? String(lockedStore.id) : "");
  const [qty, setQty] = useState("1");
  const [arrival, setArrival] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const fulfillingId = storeIdByCode(locations, row.store);
  const quoted = quotedArrivalIso(arrival);

  async function raise() {
    setError("");
    if (!requestingId) {
      setError("Which store is asking?");
      return;
    }
    if (fulfillingId === null) {
      setError(`${row.store} is not somewhere you can raise a request against.`);
      return;
    }
    if (Number(requestingId) === fulfillingId) {
      setError("That is this store's own stock — no request needed.");
      return;
    }
    const pieces = Number(qty);
    if (!Number.isInteger(pieces) || pieces < 1) {
      setError("Ask for at least one piece.");
      return;
    }
    setSaving(true);
    try {
      const { data } = await api.post(
        "/outbound/stock-requests",
        requestBody({
          requestingStoreId: Number(requestingId),
          fulfillingStoreId: fulfillingId,
          design,
          row,
          qty: pieces,
          expectedArrivalLocal: arrival,
          notes,
        }),
      );
      onDone();
      navigate(`/transfer/requests/${data.id}`);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div data-testid="availability-request-panel">
      <p className="eyebrow">
        Ask {row.store} for {design.design} · {row.size} · {row.color || "—"}
      </p>
      <div className="form-row" style={{ marginTop: 10 }}>
        <div className="field">
          <label>Requesting store</label>
          {storeLocked && lockedStore ? (
            <div className="store-lock" data-testid="availability-requesting-locked">
              {lockedStore.code} · {lockedStore.name}
            </div>
          ) : (
            <select
              className="select"
              value={requestingId}
              onChange={(e) => setRequestingId(e.target.value)}
              data-testid="availability-requesting-select"
            >
              <option value="">Select store…</option>
              {stores.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.code} · {s.name}
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="field">
          <label>Pieces</label>
          <input
            className="input"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            data-testid="availability-qty"
          />
        </div>
        <div className="field">
          <label>Tell the customer to come back</label>
          <input
            className="input"
            type="datetime-local"
            value={arrival}
            onChange={(e) => setArrival(e.target.value)}
            data-testid="availability-arrival"
          />
          {/* The picker renders in the browser's locale, so a person typing
              08/02 for the eighth of February can hand a US-formatted control
              the second of August and never know. Reading the instant back the
              way India writes it is the only warning they get. */}
          {quoted && (
            <p className="lead" data-testid="availability-arrival-echo">
              {formatDateTime(quoted)}
            </p>
          )}
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label>Notes</label>
          <input
            className="input"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Customer waiting for this size"
            data-testid="availability-notes"
          />
        </div>
      </div>

      <p className="lead" data-testid="availability-no-hold-note">
        Nothing is held for you — the piece can still sell where it is. The time above is what you
        quote the customer, and the ask goes to your manager and the Operations Head.
      </p>

      {error && (
        <div className="login-error" style={{ maxWidth: 480 }} data-testid="availability-request-error">
          {error}
        </div>
      )}

      <button
        className="btn btn-cta"
        disabled={saving}
        onClick={raise}
        data-testid="availability-raise-request"
      >
        <PackageSearch size={15} /> {saving ? "Raising…" : "Raise request"}
      </button>
    </div>
  );
}
