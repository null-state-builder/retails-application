// The store's Home (#174, D10 §2). One store's morning, top to bottom: what it
// can do next, what it sold, what is waiting on somebody, what is live in the
// shop right now, the week behind it, and - for a manager - the month.
//
// Everything here is drawn from one payload. The rule the old Home set and this
// one keeps: a tile is either a number a ledger can answer today or an honest
// empty state, never an invented figure. What is new is that a *zero* now has to
// carry the same honesty - until the till lands, "₹0 today" is not a quiet
// morning, and `sales_live` is what lets the card say so.

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowLeftRight,
  ArrowRight,
  CalendarCheck,
  PackageCheck,
  Search,
  ShoppingCart,
  Tag,
  Truck,
} from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { api, apiErrorMessage } from "../lib/api";
import { Money } from "../lib/format";
import { userCan } from "../shell/navConfig";
import { StoreChecklistCard } from "./StoreChecklist";
import {
  queueRows,
  shortDay,
  sparkBars,
  targetProgressPct,
  type DashboardPayload,
} from "./storeDashboardModel";
// The quick-action row, `.card`/`.panel`/`.net-grid` and the two-column layout
// are Home's and index.css's; only what this screen invented lives here.
import "./StoreDashboard.css";

const QUICK_ACTIONS = [
  { label: "New Bill", to: "/sell", icon: <ShoppingCart size={20} />, cta: true },
  // Receive Goods' one way in (OPS-17): the inbox, not the legacy screen.
  { label: "Receive", to: "/goods/receive", icon: <PackageCheck size={20} /> },
  { label: "Transfer", to: "/transfer", icon: <ArrowLeftRight size={20} /> },
  // D10's fourth quick action is "search across stores", and it now exists
  // (#175) - so this opens it rather than the stock the person can already see.
  // The two answer different questions, and the one a customer is standing at
  // the counter asking is this one.
  { label: "Find stock", to: "/stock/search", icon: <Search size={20} /> },
];

function QuickActions() {
  const navigate = useNavigate();
  return (
    <div className="quick-grid">
      {QUICK_ACTIONS.map((a) => (
        <button
          key={a.label}
          className={`quick-btn ${a.cta ? "quick-cta" : ""}`}
          onClick={() => navigate(a.to)}
          data-testid={`quick-${a.label.toLowerCase().replace(/ /g, "-")}`}
        >
          <span className="quick-ic">{a.icon}</span>
          {a.label}
        </button>
      ))}
    </div>
  );
}

function TodayRow({ d }: { d: DashboardPayload }) {
  const t = d.today;
  const tiles: { id: string; label: string; value: React.ReactNode }[] = [
    { id: "net-sales", label: "Net sales", value: <Money paise={t.net_sales_paise} short /> },
    { id: "bills", label: "Bills", value: t.bills },
    { id: "avg-bill", label: "Average bill", value: <Money paise={t.avg_bill_paise} short /> },
    { id: "pieces", label: "Pieces sold", value: t.pieces },
  ];
  return (
    <div className="card panel dash-block" data-testid="today-card">
      <div className="panel-head">
        <p className="eyebrow">Today</p>
        <h3 className="h3">The day so far</h3>
      </div>
      <div className="net-grid today-grid" data-testid="today-tiles">
        {tiles.map((tile) => (
          <div className="net-cell" key={tile.id} data-testid={`today-${tile.id}`}>
            <span className="net-num mono">{tile.value}</span>
            <span className="net-label">{tile.label}</span>
          </div>
        ))}
      </div>
      <div className="collect-row" data-testid="today-collections">
        {(
          [
            ["Cash", t.collections.cash],
            ["Card", t.collections.card],
            ["UPI", t.collections.upi],
            ["Credit note", t.collections.credit_note],
          ] as const
        ).map(([label, paise]) => (
          <span key={label} className="collect-cell">
            <b className="mono">
              <Money paise={paise} short />
            </b>
            {label}
          </span>
        ))}
      </div>
      {!d.sales_live && (
        // Without this the four zeros read as a store that has sold nothing.
        <p className="warn-note" data-testid="today-not-live">
          Billing is not live here yet - these stay at nil until the Sell screen opens.
        </p>
      )}
    </div>
  );
}

function ActionQueue({ d }: { d: DashboardPayload }) {
  const navigate = useNavigate();
  const { user } = useAuth();
  // A row whose screen this person's sections do not reach is dropped rather
  // than drawn as a number that opens onto a refusal - the same rule Home holds
  // to with the payable tile.
  const rows = queueRows(d, (section, capability) => userCan(user, section, capability));
  return (
    <div className="card panel" data-testid="action-queue">
      <div className="panel-head">
        <p className="eyebrow">Needs your action</p>
        <h3 className="h3">What is waiting</h3>
      </div>
      <div className="queue-list">
        {rows.map((row) => (
          <button
            key={row.key}
            className="queue-row"
            data-testid={`queue-${row.key}`}
            data-empty={row.count === 0 ? "true" : "false"}
            onClick={() => navigate(row.to)}
          >
            <span className="queue-count mono">{row.count}</span>
            <span className="queue-label">{row.label}</span>
            <ArrowRight size={15} className="queue-go" />
          </button>
        ))}
      </div>
      {/* A queue that shortens as work is cleared is one you stop trusting to
          be the whole list, so nought rows stay and grey out instead. */}
      {rows.every((r) => r.count === 0) && (
        <p className="lead queue-clear" data-testid="queue-all-clear">
          Nothing is waiting on you right now.
        </p>
      )}
    </div>
  );
}

function LiveInStore({ d }: { d: DashboardPayload }) {
  // Each carton opens its own transfer, which is where the Receive button is.
  const navigate = useNavigate();
  return (
    <div className="card panel" data-testid="live-card">
      <div className="panel-head">
        <p className="eyebrow">Live in store</p>
        <h3 className="h3">Right now</h3>
      </div>

      <p className="live-head">
        <Tag size={14} /> Offers running
      </p>
      {d.live.offers.length === 0 ? (
        <p className="lead live-empty" data-testid="live-offers-empty">
          No offers are running here today.
        </p>
      ) : (
        <ul className="live-list" data-testid="live-offers">
          {d.live.offers.map((o) => (
            <li key={o.id}>
              <span className="chip chip-purple">{o.brand}</span> {o.one_liner}
            </li>
          ))}
        </ul>
      )}

      <p className="live-head">
        <Truck size={14} /> Stock on the way
      </p>
      {d.live.in_transit.length === 0 ? (
        <p className="lead live-empty" data-testid="live-transit-empty">
          Nothing is on the road to this store.
        </p>
      ) : (
        <ul className="live-list" data-testid="live-transit">
          {d.live.in_transit.map((t, i) => (
            // Keyed by position as well as number: a transfer posted without one
            // would otherwise collide with the next on the empty string.
            <li key={`${t.doc_number}-${i}`}>
              <button
                className="live-link"
                data-testid={`live-transit-${t.doc_number}`}
                onClick={() => navigate(`/transfer/${t.id}`)}
              >
                <b className="mono">{t.doc_number}</b> · {t.pieces} pc
                {t.expected && <span className="live-when"> · {t.expected}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Sparkline({ d }: { d: DashboardPayload }) {
  const bars = sparkBars(d.last7);
  return (
    <div className="card panel" data-testid="sparkline-card">
      <div className="panel-head">
        <p className="eyebrow">Last 7 days</p>
        <h3 className="h3">Net sales</h3>
      </div>
      <div className="spark" data-testid="sparkline">
        {bars.map((b) => (
          <div className="spark-col" key={b.date} data-testid={`spark-${b.date}`}>
            {/* An all-nought week draws a baseline, not seven full bars: the
                strip has to have an axis before it has any values. */}
            <span className="spark-bar" style={{ height: `${b.heightPct}%` }} />
            <span className="spark-tick">{shortDay(b.date)}</span>
          </div>
        ))}
      </div>
      {bars.every((b) => b.paise === 0) && (
        <p className="lead live-empty" data-testid="sparkline-empty">
          No billing history yet.
        </p>
      )}
    </div>
  );
}

function ManagerRow({ m }: { m: NonNullable<DashboardPayload["manager"]> }) {
  const pct = targetProgressPct(m.mtd_net_paise, m.target_paise);
  return (
    <div className="card panel dash-block" data-testid="manager-row">
      <div className="panel-head">
        <p className="eyebrow">Manager</p>
        <h3 className="h3">This month</h3>
      </div>
      <div className="mtd-line">
        <span className="mtd-figure mono">
          <Money paise={m.mtd_net_paise} short />
        </span>
        <span className="mtd-of">
          of{" "}
          <b className="mono">
            <Money paise={m.target_paise} short />
          </b>{" "}
          target
        </span>
      </div>
      {pct === null ? (
        <p className="lead live-empty" data-testid="mtd-no-target">
          No target set for this month.
        </p>
      ) : (
        <div className="mtd-bar" data-testid="mtd-bar" data-pct={pct}>
          <span style={{ width: `${pct}%` }} />
        </div>
      )}
      <p className="day-close" data-testid="day-close">
        <CalendarCheck size={14} /> Day close
        <span className="sample-tag">Not built</span>
      </p>
    </div>
  );
}

export function StoreDashboard() {
  const { featuresOn } = useAuth();
  const [data, setData] = useState<DashboardPayload | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    api
      .get<DashboardPayload>("/store/dashboard")
      .then((r) => live && setData(r.data))
      .catch((e) => live && setError(apiErrorMessage(e)));
    return () => {
      live = false;
    };
  }, []);

  if (error) {
    return (
      <div className="card section-card warn-note" data-testid="dashboard-error">
        {error}
      </div>
    );
  }
  if (!data) {
    return (
      <p className="lead" data-testid="dashboard-loading">
        Loading…
      </p>
    );
  }

  return (
    <>
      <QuickActions />
      <TodayRow d={data} />
      {/* Store operations ticket 49: the day's task checklists, where switched on. */}
      {featuresOn.has("store-checklists") && <StoreChecklistCard store={data.store} />}
      {data.manager && <ManagerRow m={data.manager} />}
      <div className="home-cols">
        <div className="home-stack">
          <ActionQueue d={data} />
        </div>
        <div className="home-stack">
          <LiveInStore d={data} />
          <Sparkline d={data} />
        </div>
      </div>
    </>
  );
}
