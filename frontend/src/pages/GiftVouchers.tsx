import { useCallback, useEffect, useMemo, useState } from "react";

import "./Reservations.css";

import { useAuth, allowedUnits } from "../auth/AuthContext";
import { Note } from "../components/Note";
import { PageHeader } from "../components/PageHeader";
import { Section } from "../components/Section";
import { SegmentedControl } from "../components/SegmentedControl";
import { Status, type StatusTone } from "../components/Status";
import { apiErrorMessage } from "../lib/api";
import { useCachedGet } from "../lib/cachedQuery";
import { isConnectionLost } from "../lib/auditLog";
import { formatINR } from "../lib/format";
import {
  GIFT_VOUCHERS_API,
  GIFT_VOUCHERS_OFFLINE,
  balanceText,
  movementText,
  stateChip,
} from "../lib/giftVouchers";
import type { GiftVoucher, GiftVoucherListing } from "../lib/giftVouchers";
import { dayText } from "../lib/reservations";

type Show = "all" | "active" | "expired";
const SHOWS: { value: Show; label: string }[] = [
  { value: "all", label: "All" },
  { value: "active", label: "Usable" },
  { value: "expired", label: "Expired" },
];

/** The voucher's state as a status word: used up is closed, not a warning. */
const STATE_TONE: Record<string, StatusTone> = {
  active: "green",
  used_up: "grey",
  expired: "amber",
};

/** Sell > Gift vouchers (store operations ticket 19, ST-POS-4).
 *
 *  The vouchers this store sold: what each held, what is left, where it was
 *  used, and those that expired unused - what they held expires with no GST
 *  (Circular 243/37/2024). Vouchers are sold and taken at Billing; this page
 *  only reads. Online only: offline it says so. The server decides everything. */
export function GiftVouchersPage() {
  const { user, activeStore } = useAuth();
  const stores = useMemo(
    () => (user ? allowedUnits(user).filter((s) => s.store_type === "store") : []),
    [user],
  );
  const [picked, setPicked] = useState("");
  const store =
    picked ||
    (activeStore?.store_type === "store" ? activeStore.code : "") ||
    (stores.length === 1 ? (stores[0]?.code ?? "") : "");

  const [show, setShow] = useState<Show>("all");
  const [query, setQuery] = useState("");
  // The term last sent with Find; typing alone does not search.
  const [searched, setSearched] = useState("");
  const [online, setOnline] = useState(navigator.onLine);

  // The store's vouchers for the tab, from the shared cache: a revisit shows
  // the last list at once and refreshes it behind it.
  const read = useCachedGet<GiftVoucherListing>(store ? GIFT_VOUCHERS_API : null, {
    params: { store, show, q: searched },
  });
  const { reload } = read;
  // Only this store's list: another store's stays off screen while this one loads.
  const listing = read.data && read.data.store === store ? read.data : null;
  const lost = read.error !== null && isConnectionLost(read.error);
  const error = read.error !== null && !lost ? apiErrorMessage(read.error) : "";

  const load = useCallback(() => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    reload();
  }, [reload]);

  /** Find: ask for the typed term (again, if it is the one already shown). */
  const find = () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const term = query.trim();
    if (term === searched) reload();
    else setSearched(term);
  };

  useEffect(() => {
    const up = () => {
      setOnline(true);
      load();
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load]);

  const offline = !online || lost;

  return (
    <div className="page-pad">
      <PageHeader
        title="Gift vouchers"
        lead="The vouchers this store sold, what is left on each, and those that expired unused. Sell and take them at Billing."
      />
      {stores.length > 1 && (
        <label className="field">
          <span>Store</span>
          <select
            className="input"
            data-testid="gift-vouchers-store"
            value={store}
            onChange={(e) => {
              setPicked(e.target.value);
            }}
          >
            <option value="">Choose a store</option>
            {stores.map((s) => (
              <option key={s.code} value={s.code}>
                {s.code} · {s.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {offline && (
        <Note
          tone="warn"
          testId="gift-vouchers-offline"
          role="status"
          action={
            online && (
              <button
                type="button"
                className="btn btn-sm"
                data-testid="gift-vouchers-retry"
                onClick={() => load()}
              >
                Try again
              </button>
            )
          }
        >
          {GIFT_VOUCHERS_OFFLINE}
        </Note>
      )}
      {error && (
        <Note tone="error" testId="gift-vouchers-error">
          {error}
        </Note>
      )}
      {!store && <p>Choose a store to see its gift vouchers.</p>}
      {store && !listing && !offline && !error && <p>Loading gift vouchers…</p>}

      {listing && (
        <Section heading={`Gift vouchers sold at ${listing.store}`} testId="gift-voucher-list">
          {!listing.switched_on && (
            <Note tone="warn" testId="gift-vouchers-switched-off">
              Gift vouchers are switched off at {listing.store}: the till neither sells nor takes
              them here. The ones below stay listed, and still expire.
            </Note>
          )}
          <p className="muted-cell">
            Each can be used for {listing.months} months, in parts. {listing.no_gst_note}
          </p>
          <div className="toolbar">
            <SegmentedControl
              label="Which vouchers"
              options={SHOWS.map((s) => ({
                value: s.value,
                label: s.label,
                testId: `gift-vouchers-show-${s.value}`,
              }))}
              value={show}
              onChange={(next) => {
                setShow(next);
                setSearched("");
              }}
            />
            <div className="spacer" />
            <input
              className="input"
              aria-label="Find a gift voucher"
              placeholder="Voucher number"
              data-testid="gift-voucher-search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") find();
              }}
            />
            <button type="button" className="btn" disabled={offline} onClick={find}>
              Find
            </button>
          </div>
          {show === "expired" && (
            <p data-testid="gift-vouchers-expired-total">
              Expired unused: <strong>{formatINR(listing.expired_unused_paise)}</strong>. No GST is
              due on it.
            </p>
          )}
          {listing.cut && (
            <p className="muted-cell" data-testid="gift-vouchers-cut">
              Showing the newest {listing.gift_vouchers.length}. Find a number to see an older one.
            </p>
          )}
          {listing.gift_vouchers.length === 0 ? (
            <p data-testid="gift-voucher-none">No gift vouchers to show.</p>
          ) : (
            // One box per voucher in a grid that stacks at 375 px (PRD §5.2).
            <ul
              className="tiles"
              style={{ listStyle: "none", margin: 0, padding: 0 }}
              data-testid="gift-voucher-cards"
            >
              {listing.gift_vouchers.map((v) => (
                <VoucherCard key={v.id} voucher={v} />
              ))}
            </ul>
          )}
        </Section>
      )}
    </div>
  );
}

function VoucherCard({ voucher: v }: { voucher: GiftVoucher }) {
  const chip = stateChip(v.state);
  return (
    <li className="box" data-testid={`gift-voucher-${v.number}`}>
      <div>
        <strong className="mono">{v.number}</strong>{" "}
        <Status tone={STATE_TONE[v.state] ?? "grey"}>{chip.label}</Status>
      </div>
      <p>
        {formatINR(v.value_paise)} ·{" "}
        <span data-testid={`gift-voucher-balance-${v.number}`}>{balanceText(v)}</span>
      </p>
      <p className="muted-cell">
        Sold {dayText(v.issued_on)} · use by {dayText(v.valid_until)}
      </p>
      <ul className="muted-cell">
        {v.movements.map((m, i) => (
          <li key={i}>
            {movementText(m)}: {formatINR(m.amount_paise)}
            {m.store !== v.store ? ` at ${m.store}` : ""}
          </li>
        ))}
      </ul>
    </li>
  );
}
