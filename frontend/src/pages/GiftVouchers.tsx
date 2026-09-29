import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import "./Reservations.css";

import { useAuth, allowedUnits } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { formatINR, StatusChip } from "../lib/format";
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
    (stores.length === 1 ? stores[0].code : "");

  const [show, setShow] = useState<Show>("all");
  const [query, setQuery] = useState("");
  const [listing, setListing] = useState<GiftVoucherListing | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const request = useRef(0);

  const load = useCallback(
    async (which: Show, q: string) => {
      if (!store) return;
      if (!navigator.onLine) {
        setOnline(false);
        return;
      }
      const mine = ++request.current;
      setError("");
      try {
        const params: Record<string, string> = { store, show: which };
        if (q) params.q = q;
        const l = await api.get<GiftVoucherListing>(GIFT_VOUCHERS_API, { params });
        if (mine !== request.current) return;
        setLost(false);
        setListing(l.data);
      } catch (reason) {
        if (mine !== request.current) return;
        if (isConnectionLost(reason)) setLost(true);
        else setError(apiErrorMessage(reason));
      }
    },
    [store],
  );

  useEffect(() => {
    void load(show, "");
    // Only the store and the tab reload the list; a search waits for Find.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, show]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(show, query.trim());
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load, show, query]);

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
              setListing(null);
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
        <p className="warn-note" data-testid="gift-vouchers-offline" role="status">
          {GIFT_VOUCHERS_OFFLINE}
          {online && (
            <>
              {" "}
              <button
                type="button"
                className="btn"
                data-testid="gift-vouchers-retry"
                onClick={() => void load(show, query.trim())}
              >
                Try again
              </button>
            </>
          )}
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="gift-vouchers-error">
          {error}
        </p>
      )}
      {!store && <p>Choose a store to see its gift vouchers.</p>}
      {store && !listing && !offline && !error && <p>Loading gift vouchers…</p>}

      {listing && (
        <section className="card section-card" data-testid="gift-voucher-list">
          <h2 className="h3">Gift vouchers sold at {listing.store}</h2>
          {!listing.switched_on && (
            <p className="warn-note" data-testid="gift-vouchers-switched-off">
              Gift vouchers are switched off at {listing.store}: the till neither sells nor takes
              them here. The ones below stay listed, and still expire.
            </p>
          )}
          <p className="muted-cell">
            Each can be used for {listing.months} months, in parts. {listing.no_gst_note}
          </p>
          <div className="toolbar" role="tablist" aria-label="Which vouchers">
            {SHOWS.map((s) => (
              <button
                key={s.value}
                type="button"
                role="tab"
                aria-selected={show === s.value}
                className={show === s.value ? "btn btn-primary" : "btn"}
                data-testid={`gift-vouchers-show-${s.value}`}
                onClick={() => setShow(s.value)}
              >
                {s.label}
              </button>
            ))}
          </div>
          <div className="toolbar">
            <input
              className="input"
              aria-label="Find a gift voucher"
              placeholder="Voucher number"
              data-testid="gift-voucher-search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void load(show, query.trim());
              }}
            />
            <button
              type="button"
              className="btn"
              disabled={offline}
              onClick={() => void load(show, query.trim())}
            >
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
            // One card per voucher, stacked: a real layout at 375 px (PRD §5.2).
            <ul className="reservation-cards" data-testid="gift-voucher-cards">
              {listing.gift_vouchers.map((v) => (
                <VoucherCard key={v.id} voucher={v} />
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}

function VoucherCard({ voucher: v }: { voucher: GiftVoucher }) {
  const chip = stateChip(v.state);
  return (
    <li className="reservation-card" data-testid={`gift-voucher-${v.number}`}>
      <div className="reservation-card-head">
        <strong>{v.number}</strong> <StatusChip status={chip.label} tone={chip.tone} />
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
