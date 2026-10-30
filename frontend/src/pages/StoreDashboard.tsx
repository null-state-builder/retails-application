// The store's Home (#174, D10 §2). One store's morning, top to bottom: what it
// can do next, what it sold, what is waiting on somebody, what is live in the
// shop right now, the week behind it, and - for a manager - the month.
//
// Everything here is drawn from one payload. The rule the old Home set and this
// one keeps: a tile is either a number a ledger can answer today or an honest
// empty state, never an invented figure. What is new is that a *zero* now has to
// carry the same honesty - until the till lands, "₹0 today" is not a quiet
// morning, and `sales_live` is what lets the card say so.
//
// Design system v2 (Dashboard): one lead figure, a strip, ranked lists and
// unboxed sections. The board's salesperson and brand rankings, the same-weekday
// comparison and each queue row's detail line are not in this payload, so they
// are not drawn.

import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeftRight,
  CalendarCheck,
  ChevronRight,
  PackageCheck,
  Search,
  ShoppingCart,
  Tag,
  Truck,
} from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { KpiStrip, LeadFigure } from "../components/KpiStrip";
import { Note } from "../components/Note";
import { Section } from "../components/Section";
import { Loading } from "../components/States";
import { Status } from "../components/Status";
import { apiErrorMessage } from "../lib/api";
import { useCachedGet } from "../lib/cachedQuery";
import { Money } from "../lib/format";
import { userCan, type Area } from "../shell/navConfig";
import { StoreChecklistCard } from "./StoreChecklist";
import {
  queueRows,
  shortDay,
  sparkBars,
  targetProgressPct,
  tenderRanks,
  weekSummary,
  type DashboardPayload,
} from "./storeDashboardModel";
import "./StoreDashboard.css";

// Places, so links (decision 5), drawn as icon tiles in the colour of the area
// each opens (v2.1). Red stays kept for the button that commits money or stock
// (decision 3), so New Bill is Sell's green, not red.
const QUICK_ACTIONS: { label: string; to: string; icon: ReactNode; area: Area }[] = [
  { label: "New Bill", to: "/sell", icon: <ShoppingCart size={20} />, area: "sell" },
  // Receive Goods' one way in (OPS-17): the inbox, not the legacy screen.
  { label: "Receive", to: "/goods/receive", icon: <PackageCheck size={20} />, area: "receive" },
  { label: "Transfer", to: "/transfer", icon: <ArrowLeftRight size={20} />, area: "move" },
  // D10's fourth quick action is "search across stores", and it now exists
  // (#175) - so this opens it rather than the stock the person can already see.
  // The two answer different questions, and the one a customer is standing at
  // the counter asking is this one.
  { label: "Find stock", to: "/stock/search", icon: <Search size={20} />, area: "stock" },
];

function QuickActions() {
  return (
    <nav className="quick-row" aria-label="Quick actions">
      {QUICK_ACTIONS.map((a) => (
        <Link
          key={a.label}
          to={a.to}
          className="quick-link"
          data-area={a.area}
          data-testid={`quick-${a.label.toLowerCase().replace(/ /g, "-")}`}
        >
          <span className="quick-ic" aria-hidden="true">
            {a.icon}
          </span>
          {a.label}
        </Link>
      ))}
    </nav>
  );
}

function count(n: number): string {
  return n.toLocaleString("en-IN");
}

/** "12% more than yesterday": the one comparison the payload carries. */
function VsYesterday({ pct }: { pct: number | null }) {
  if (pct === null) return null;
  if (pct === 0) return <>Level with yesterday.</>;
  return (
    <span className={pct > 0 ? "up" : "down"}>
      {Math.abs(pct).toLocaleString("en-IN")}% {pct > 0 ? "more" : "less"} than yesterday
    </span>
  );
}

function TodayRow({ d }: { d: DashboardPayload }) {
  const t = d.today;
  const tenders = tenderRanks(t.collections);
  return (
    <Section heading="The day so far" testId="today-card">
      <div className="today-split">
        <div className="today-figures" data-testid="today-tiles">
          <LeadFigure
            label="Net sales"
            value={<Money paise={t.net_sales_paise} />}
            testId="today-net-sales"
          >
            <VsYesterday pct={t.vs_yesterday_pct} />
          </LeadFigure>
          <KpiStrip
            label="Today's figures"
            items={[
              { label: "Bills", value: count(t.bills), testId: "today-bills" },
              {
                label: "Average bill",
                value: <Money paise={t.avg_bill_paise} />,
                testId: "today-avg-bill",
              },
              {
                label: "Pieces sold",
                value: count(t.pieces),
                detail:
                  t.bills > 0
                    ? `${(t.pieces / t.bills).toLocaleString("en-IN", { maximumFractionDigits: 1 })} a bill`
                    : undefined,
                testId: "today-pieces",
              },
            ]}
          />
        </div>
        <Section heading="By tender" level={3} className="today-tenders" testId="today-collections">
          <ol className="ranked">
            {tenders.map((r, i) => (
              <li key={r.label}>
                <span className="ranked-r">{i + 1}</span>
                <span className="ranked-name">
                  {r.label}
                  <span className="ranked-bar" aria-hidden="true">
                    <span style={{ width: `${r.barPct}%` }} />
                  </span>
                </span>
                <span className="ranked-v">
                  <Money paise={r.paise} />
                  {r.sharePct !== null && <span className="ranked-share">{r.sharePct}%</span>}
                </span>
              </li>
            ))}
          </ol>
        </Section>
      </div>
      {!d.sales_live && (
        // Without this the four zeros read as a store that has sold nothing.
        <Note tone="warn" testId="today-not-live">
          Billing is not live here yet - these stay at nil until the Sell screen opens.
        </Note>
      )}
    </Section>
  );
}

function ActionQueue({ d }: { d: DashboardPayload }) {
  const { user } = useAuth();
  // A row whose screen this person's sections do not reach is dropped rather
  // than drawn as a number that opens onto a refusal - the same rule Home holds
  // to with the payable tile.
  const rows = queueRows(d, (section, capability) => userCan(user, section, capability));
  return (
    <Section
      heading="Needs you"
      testId="action-queue"
      aside={
        <Link className="link" to="/action-needed">
          All on Action needed
        </Link>
      }
    >
      <ul className="queue-list">
        {rows.map((row) => (
          <li key={row.key}>
            <Link
              to={row.to}
              className="queue-row"
              data-testid={`queue-${row.key}`}
              data-empty={row.count === 0 ? "true" : "false"}
            >
              <span className="queue-label">{row.label}</span>
              <span className="queue-count tabular">{count(row.count)}</span>
              <ChevronRight size={16} className="queue-go" aria-hidden="true" />
            </Link>
          </li>
        ))}
      </ul>
      {/* A queue that shortens as work is cleared is one you stop trusting to
          be the whole list, so nought rows stay and grey out instead. */}
      {rows.every((r) => r.count === 0) && (
        <p className="queue-clear" data-testid="queue-all-clear">
          Nothing is waiting on you right now.
        </p>
      )}
    </Section>
  );
}

function LiveInStore({ d }: { d: DashboardPayload }) {
  return (
    <Section heading="Live in store" testId="live-card">
      <div className="live-group">
        <p className="live-head">
          <Tag size={14} aria-hidden="true" /> Offers running
          {d.live.offers.length > 0 && <>, {count(d.live.offers.length)}</>}
        </p>
        {d.live.offers.length === 0 ? (
          <p className="live-empty" data-testid="live-offers-empty">
            No offers are running here today.
          </p>
        ) : (
          <ul className="live-list" data-testid="live-offers">
            {d.live.offers.map((o) => (
              <li key={o.id}>
                <Status tone="blue">{o.brand}</Status>
                <span>{o.one_liner}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="live-group">
        <p className="live-head">
          <Truck size={14} aria-hidden="true" /> Stock on the way
        </p>
        {d.live.in_transit.length === 0 ? (
          <p className="live-empty" data-testid="live-transit-empty">
            Nothing is on the road to this store.
          </p>
        ) : (
          // Each carton opens its own transfer, which is where Receive is.
          <ul className="live-list" data-testid="live-transit">
            {d.live.in_transit.map((t, i) => (
              // Keyed by position as well as number: a transfer posted without one
              // would otherwise collide with the next on the empty string.
              <li key={`${t.doc_number}-${i}`}>
                <Link
                  className="link"
                  to={`/transfer/${t.id}`}
                  data-testid={`live-transit-${t.doc_number}`}
                >
                  <span className="mono">{t.doc_number}</span>
                </Link>
                <span className="live-when">
                  {count(t.pieces)} pc{t.expected && <>, {t.expected}</>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}

function Sparkline({ d }: { d: DashboardPayload }) {
  const bars = sparkBars(d.last7);
  const week = weekSummary(d.last7);
  return (
    <Section
      heading="Last 7 days"
      ruled
      testId="sparkline-card"
      aside={
        <span className="spark-total">
          <Money paise={week.totalPaise} />
        </span>
      }
    >
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
      {week.best ? (
        <p className="spark-note">
          Best day {shortDay(week.best.date)},{" "}
          <b className="tabular">
            <Money paise={week.best.paise} />
          </b>
        </p>
      ) : (
        <p className="spark-note" data-testid="sparkline-empty">
          No billing history yet.
        </p>
      )}
    </Section>
  );
}

function ManagerRow({ m }: { m: NonNullable<DashboardPayload["manager"]> }) {
  const { user } = useAuth();
  const pct = targetProgressPct(m.mtd_net_paise, m.target_paise);
  // Day close is a counter's screen: offered only to a one-store login that
  // runs the till, the same rule as its menu line.
  const canCloseHere = (user?.stores?.length ?? 0) === 1 && userCan(user, "sell", "operate");
  return (
    <Section heading="This month" ruled testId="manager-row">
      <LeadFigure label="Net sales this month" value={<Money paise={m.mtd_net_paise} />}>
        of{" "}
        <b>
          <Money paise={m.target_paise} />
        </b>{" "}
        target
      </LeadFigure>
      {pct === null ? (
        <p className="live-empty" data-testid="mtd-no-target">
          No target set for this month.
        </p>
      ) : (
        <div className="mtd-bar" data-testid="mtd-bar" data-pct={pct} aria-hidden="true">
          <span style={{ width: `${pct}%` }} />
        </div>
      )}
      <p className="day-close" data-testid="day-close" data-state={m.day_close.state}>
        <CalendarCheck size={14} aria-hidden="true" /> Day close
        {m.day_close.state === "closed" && <Status tone="green">Closed</Status>}
        {m.day_close.state === "open" && (
          <>
            <Status tone="amber">Open</Status>
            {canCloseHere && (
              <Link to="/sell/day-close" data-testid="day-close-link">
                Close the day
              </Link>
            )}
          </>
        )}
        {m.day_close.state === "off" && <Status>Not in use</Status>}
      </p>
    </Section>
  );
}

export function StoreDashboard({
  onLoaded,
}: {
  /** Home writes its heading sentence from the same payload, so it is not read twice. */
  onLoaded?: ((d: DashboardPayload) => void) | undefined;
} = {}) {
  const { featuresOn } = useAuth();
  // From the shared cache: a revisit shows the store's day at once and
  // refreshes it behind it (fresh for 10 s, like the other counts).
  const read = useCachedGet<DashboardPayload>("/store/dashboard", { kind: "doc" });
  const data = read.data ?? null;
  // A failed refresh behind a day already shown leaves the day on screen.
  const error = !data && read.error !== null ? apiErrorMessage(read.error) : "";
  const loaded = useRef(onLoaded);
  loaded.current = onLoaded;

  useEffect(() => {
    if (data) loaded.current?.(data);
  }, [data]);

  if (error) {
    return (
      <Note tone="error" testId="dashboard-error">
        {error}
      </Note>
    );
  }
  if (!data) {
    return <Loading lines={4} label="Loading the store's day" testId="dashboard-loading" />;
  }

  return (
    <div className="store-dash">
      <QuickActions />
      <TodayRow d={data} />
      {/* Store operations ticket 49: the day's task checklists, where switched on. */}
      {featuresOn.has("store-checklists") && <StoreChecklistCard store={data.store} />}
      {data.manager && <ManagerRow m={data.manager} />}
      <div className="dash-cols">
        <ActionQueue d={data} />
        <div className="dash-side">
          <LiveInStore d={data} />
          <Sparkline d={data} />
        </div>
      </div>
    </div>
  );
}
