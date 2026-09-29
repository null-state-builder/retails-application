import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeftRight,
  Banknote,
  Boxes,
  CheckCircle2,
  ClipboardList,
  GripVertical,
  Inbox,
  PackageCheck,
  Settings2,
  Sparkles,
  TrendingUp,
  X,
} from "lucide-react";

import { api } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { Money } from "../lib/format";
import { userCan } from "../shell/navConfig";
import { unitContextLabel } from "../shell/unitSwitcher";
import { StoreDashboard } from "./StoreDashboard";
import "./Home.css";

interface Summary {
  entities: number;
  gstins: number;
  stores: number;
  warehouses: number;
  brands: number;
  seasons: number;
  open_season: string | null;
}

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}
const today = new Date().toLocaleDateString("en-IN", {
  weekday: "long",
  day: "numeric",
  month: "long",
});

// Every tile is one of two things and says which: a number the ledgers can
// answer today, or a screen that is not built. It is never an invented figure.
//
// It used to be. Until this review the owner's dashboard opened on "Net sales
// today ₹18.42 L", "Owed to brands ₹42.60 L" and "Open exceptions 5" — three
// literals in this file, on a database whose trial balance was ₹0 with no
// vouchers; the store's "Items to receive: 3 shipments" was a literal too, and
// carried a "live" label. A dashboard that invents numbers is worse than one
// with none: Rule 6 says calculated numbers are not typed by hand, and a
// plausible wrong total is the one nobody goes and checks.
type CardState = "live" | "unbuilt";

interface DashboardCard {
  id: string;
  label: string;
  /** Rendered value. `null` data reads as "…" (loading), never as 0. */
  value: React.ReactNode;
  sub: string;
  icon: React.ReactNode;
  /** What the tile is for — shown in the card picker. */
  purpose: string;
  state: CardState;
  /** The screen that owns this number; clicking the tile opens it. */
  to: string;
}

const DASH_ORDER_KEY = "kdps-dashboard-card-order";
const DASH_VISIBLE_KEY = "kdps-dashboard-card-visible";

/** A count that has not arrived yet is "…", not zero — an empty ledger and an
 *  unanswered request must not look the same. */
function num(value: number | null, suffix = ""): React.ReactNode {
  return value === null ? "…" : `${value}${suffix}`;
}

function Kpi({
  id,
  icon,
  label,
  value,
  sub,
  state,
  onOpen,
  onDragStart,
  onDrop,
}: {
  id: string;
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  sub?: string;
  state: CardState;
  onOpen: () => void;
  onDragStart: () => void;
  onDrop: () => void;
}) {
  return (
    <button
      className="kpi card"
      onClick={onOpen}
      draggable
      onDragStart={onDragStart}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => { e.preventDefault(); onDrop(); }}
      data-testid={`kpi-tile-${id}`}
      data-state={state}
    >
      <div className="kpi-top">
        <span className="kpi-ic">{icon}</span>
        <span className="kpi-label">
          {label} {state === "unbuilt" && <span className="sample-tag">Not built</span>}
        </span>
        <GripVertical size={14} className="kpi-grip" />
      </div>
      <div className="kpi-value mono">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </button>
  );
}

function NetworkPanel({ s }: { s: Summary | null }) {
  const rows: [string, React.ReactNode][] = [
    ["Legal entities", s?.entities ?? "—"],
    ["State GSTINs", s?.gstins ?? "—"],
    ["Stores", s?.stores ?? "—"],
    ["Warehouses", s?.warehouses ?? "—"],
    ["Brands", s?.brands ?? "—"],
    ["Seasons", s?.seasons ?? "—"],
  ];
  return (
    <div className="card panel">
      <div className="panel-head">
        <p className="eyebrow">Live counts</p>
        <h3 className="h3">Network at a glance</h3>
      </div>
      <div className="net-grid">
        {rows.map(([k, v]) => (
          <div className="net-cell" key={k}>
            <span className="net-num mono">{v}</span>
            <span className="net-label">{k}</span>
          </div>
        ))}
      </div>
      {s?.open_season && (
        <div className="net-foot">
          Open season <span className="chip chip-green">{s.open_season}</span>
        </div>
      )}
    </div>
  );
}

interface Health {
  balanced: boolean;
  trial_balance_paise: number;
  assets_paise: number;
  liabilities_paise: number;
  leg_count: number;
  voucher_count: number;
}

/** Fetches Books Health only when the caller can see money — the same
 *  predicate that decides whether the card renders — so a role without money
 *  read never fires the request just to eat the 403 it would get back (#236). */
export function loadBooksHealth(
  canSeeMoney: boolean,
  onLoaded: (h: Health) => void,
  onDenied: () => void,
): void {
  if (!canSeeMoney) return;
  api.get("/finledger/health").then((r) => onLoaded(r.data)).catch(() => onDenied());
}

function TrialBalancePanel({ canSeeMoney }: { canSeeMoney: boolean }) {
  const [h, setH] = useState<Health | null>(null);
  const [denied, setDenied] = useState(false);
  useEffect(() => {
    // A capability that regains money access after an earlier failure must get
    // a clean retry, not stay hidden behind a stale denial.
    setDenied(false);
    loadBooksHealth(canSeeMoney, setH, () => setDenied(true));
  }, [canSeeMoney]);
  if (!canSeeMoney || denied) return null;
  const balanced = h?.balanced ?? true;
  return (
    <div className="card panel" data-testid="trial-balance-panel">
      <div className="panel-head">
        <p className="eyebrow">Books health · value ledger</p>
        <h3 className="h3">Equation of state</h3>
      </div>
      <div
        data-testid="trial-balance-state"
        style={{
          display: "flex", gap: 12, alignItems: "center", padding: "12px 14px",
          borderRadius: 12, marginBottom: 12,
          background: balanced ? "rgba(var(--green-rgb), 0.08)" : "rgba(var(--rust-rgb), 0.10)",
          color: balanced ? "var(--green)" : "var(--rust)",
        }}
      >
        {balanced ? <CheckCircle2 size={20} /> : <AlertTriangle size={20} />}
        <div style={{ display: "flex", flexDirection: "column" }}>
          <b>{balanced ? "Books tie" : "Out of balance"}</b>
          <span style={{ fontSize: 13, opacity: 0.85 }}>
            Trial balance <b className="mono"><Money paise={h?.trial_balance_paise ?? 0} /></b>
            {" "}· {h?.voucher_count ?? 0} vouchers · {h?.leg_count ?? 0} legs
          </span>
        </div>
      </div>
      <div className="net-grid">
        <div className="net-cell">
          <span className="net-num mono"><Money paise={h?.assets_paise ?? 0} short /></span>
          <span className="net-label">Stock, cash & clearing</span>
        </div>
        <div className="net-cell">
          <span className="net-num mono"><Money paise={h?.liabilities_paise ?? 0} short /></span>
          <span className="net-label">Payables, tax & contra</span>
        </div>
      </div>
    </div>
  );
}

function CollectionsBand() {
  return (
    <div className="band band-edge">
      <div className="band-head">
        <h3 className="h3">Collections</h3>
        <span className="chip chip-blue">Coming soon</span>
      </div>
      <p className="ai-line">
        This section will show cash, UPI, card clearing, deposits, and bank-audit status once the collections slice is enabled.
      </p>
    </div>
  );
}

function MorningBrief() {
  return (
    <div className="band band-ai">
      <div className="band-head">
        <h3 className="h3">Morning brief</h3>
        <span className="chip chip-purple">
          <Sparkles size={12} /> AI · preview
        </span>
      </div>
      <p className="ai-line">
        Coming soon: this area will show a suggest-only morning summary for sales, stock,
        exceptions, and follow-ups. Every operational action will still need human approval.
      </p>
      <div className="ai-fence">
        ✦ AI never writes stock, money or Tally. It opens drafts a human approves.
      </div>
    </div>
  );
}

function readOrder(defaultIds: string[]): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(DASH_ORDER_KEY) || "[]") as string[];
    return [...saved.filter((id) => defaultIds.includes(id)), ...defaultIds.filter((id) => !saved.includes(id))];
  } catch {
    return defaultIds;
  }
}

/** Saved choice, else every tile that can answer today. A retired tile id (the
 *  invented ones this review deleted) falls out here rather than blanking the
 *  dashboard of anyone who had picked it. */
function readVisible(defaultIds: string[], fallback: string[]): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(DASH_VISIBLE_KEY) || "[]") as string[];
    const kept = saved.filter((id) => defaultIds.includes(id));
    return kept.length ? kept : fallback;
  } catch {
    return fallback;
  }
}

function DashboardConfigurator({
  cards,
  visible,
  onToggle,
  onClose,
}: {
  cards: DashboardCard[];
  visible: string[];
  onToggle: (id: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="modal-backdrop" data-testid="dashboard-config-modal" onClick={onClose}>
      <div className="modal dashboard-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div><p className="eyebrow">Dashboard</p><h3 className="h3">Choose cards</h3></div>
          <button className="btn btn-sm" onClick={onClose} data-testid="dashboard-config-close"><X size={14} /> Close</button>
        </div>
        <div className="dash-card-list">
          {cards.map((c) => (
            <label className="dash-card-choice" key={c.id}>
              <input type="checkbox" checked={visible.includes(c.id)} onChange={() => onToggle(c.id)} data-testid={`dashboard-card-toggle-${c.id}`} />
              <span>
                <b>{c.label}{c.state === "unbuilt" && <span className="sample-tag" style={{ marginLeft: 6 }}>Not built</span>}</b>
                <small>{c.purpose}</small>
              </span>
            </label>
          ))}
        </div>
      </div>
    </div>
  );
}

function QuickActions({ items }: { items: { label: string; to: string; icon: React.ReactNode; cta?: boolean }[] }) {
  const navigate = useNavigate();
  return (
    <div className="quick-grid">
      {items.map((a) => (
        <button
          key={a.label}
          className={`quick-btn ${a.cta ? "quick-cta" : ""}`}
          onClick={() => navigate(a.to)}
          data-testid={`quick-${a.label.toLowerCase()}`}
        >
          <span className="quick-ic">{a.icon}</span>
          {a.label}
        </button>
      ))}
    </div>
  );
}

interface OnHandSummary {
  units_on_hand: number;
  value_paise: number;
  lines: number;
}

export function Home() {
  const { user, activeStore } = useAuth();
  const navigate = useNavigate();
  const [summary, setSummary] = useState<Summary | null>(null);
  const [inbox, setInbox] = useState<number | null>(null);
  const [onHand, setOnHand] = useState<OnHandSummary | null>(null);
  const [payablePaise, setPayablePaise] = useState<number | null>(null);
  const [alertCount, setAlertCount] = useState<number | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [dragCard, setDragCard] = useState<string | null>(null);

  // The books are finance-only on the server (`IsBooksKeeper`), so only ask for
  // the payable when this person holds the rung — a 403 the tile then has to
  // explain away is a worse answer than not offering the tile.
  const canSeeMoney = userCan(user, "money", "manage");

  useEffect(() => {
    // Every number below comes from the endpoint that owns it. A failure leaves
    // the tile at "…" rather than substituting a figure.
    api.get("/masters/summary").then((r) => setSummary(r.data)).catch(() => undefined);
    api.get("/approvals/inbox").then((r) => setInbox(r.data?.length ?? 0)).catch(() => undefined);
    api.get("/stockledger/on-hand").then((r) => setOnHand(r.data.summary)).catch(() => undefined);
    api.get("/alerts").then((r) => setAlertCount(r.data?.length ?? 0)).catch(() => undefined);
    if (canSeeMoney) {
      api
        .get("/finledger/vendor/ageing")
        .then((r) => setPayablePaise(r.data.total_due_paise ?? 0))
        .catch(() => undefined);
    }
  }, [canSeeMoney]);

  const variant = user?.landing_page ?? "owner";

  const stockCard: DashboardCard = canSeeMoney
    ? {
        id: "stock_value",
        icon: <Boxes size={18} />,
        label: "Stock on hand",
        value: onHand === null ? "…" : <Money paise={onHand.value_paise} short />,
        sub: onHand ? `${onHand.units_on_hand} pcs · ${onHand.lines} SKU lines` : "From the stock ledger",
        purpose: "Stock value at book cost, from the stock ledger — the units and lines behind it are on Stock on Hand.",
        state: "live",
        to: "/stock",
      }
    : {
        id: "stock_units",
        icon: <Boxes size={18} />,
        label: "Stock on hand",
        value: num(onHand === null ? null : onHand.units_on_hand, " pcs"),
        sub: onHand ? `${onHand.lines} SKU lines` : "From the stock ledger",
        purpose: "Pieces on hand from the stock ledger. Cost is not shown — the books are finance's.",
        state: "live",
        to: "/stock",
      };

  const approvalsCard: DashboardCard = {
    id: "approvals",
    icon: <Inbox size={18} />,
    label: "Waiting for your approval",
    value: num(inbox),
    sub: "Never your own requests — nobody approves their own document",
    purpose: "Documents waiting on your decision, across every type. Opens Action Needed.",
    state: "live",
    to: "/action-needed?show=approvals",
  };

  const netSalesCard: DashboardCard = {
    id: "net_sales",
    icon: <TrendingUp size={18} />,
    label: "Net sales today",
    value: "—",
    sub: "KDPS's own POS is not built — there are no bills to total yet",
    purpose: "The day's billing. It stays blank until POS & Billing is built; nothing here is estimated.",
    state: "unbuilt",
    to: "/sell",
  };

  const exceptionsCard: DashboardCard = {
    id: "exceptions",
    icon: <AlertTriangle size={18} />,
    label: "Alerts",
    value: num(alertCount),
    sub: "Stock stuck in transit, and return windows closing",
    purpose: "In-transit ageing and closing return windows, raised by the daily alerts job. Till and fraud alerts arrive once billing is live.",
    state: "live",
    to: "/alerts",
  };

  const cardCatalog: DashboardCard[] =
    variant === "store"
      ? [
          approvalsCard,
          stockCard,
          netSalesCard,
          exceptionsCard,
        ]
      : variant === "warehouse"
        ? [
            approvalsCard,
            stockCard,
            exceptionsCard,
          ]
        : [
            ...(canSeeMoney
              ? [
                  {
                    id: "payable",
                    icon: <Banknote size={18} />,
                    label: "Owed to brands",
                    value: payablePaise === null ? "…" : <Money paise={payablePaise} short />,
                    sub: "Open vendor bills, aged on the ledger screen",
                    purpose: "What the vendor ledger still shows unpaid, FIFO-aged into 0–30 / 31–60 / 60+.",
                    state: "live" as CardState,
                    to: "/money/vendor",
                  },
                ]
              : []),
            stockCard,
            approvalsCard,
            netSalesCard,
            exceptionsCard,
          ];

  const allIds = cardCatalog.map((c) => c.id);
  const liveIds = cardCatalog.filter((c) => c.state === "live").map((c) => c.id);
  const [cardOrder, setCardOrder] = useState<string[]>(() => readOrder(allIds));
  const [visibleCards, setVisibleCards] = useState<string[]>(() => readVisible(allIds, liveIds));
  const cardsById = new Map(cardCatalog.map((c) => [c.id, c]));
  const orderedCards = cardOrder
    .map((id) => cardsById.get(id))
    .filter(Boolean)
    .concat(cardCatalog.filter((c) => !cardOrder.includes(c.id))) as DashboardCard[];
  const dashboardCards = orderedCards.filter((c) => visibleCards.includes(c.id));

  if (!user) return null;
  const name = (user.full_name || user.username).split(" ")[0];
  const ctx = unitContextLabel(activeStore);

  function toggleDashboardCard(id: string) {
    const next = visibleCards.includes(id) ? visibleCards.filter((x) => x !== id) : [...visibleCards, id];
    setVisibleCards(next);
    localStorage.setItem(DASH_VISIBLE_KEY, JSON.stringify(next));
  }

  function moveDashboardCard(target: string) {
    if (!dragCard || dragCard === target) return;
    const current = orderedCards.map((c) => c.id);
    const from = current.indexOf(dragCard);
    const to = current.indexOf(target);
    if (from < 0 || to < 0) return;
    const next = [...current];
    const [picked] = next.splice(from, 1);
    next.splice(to, 0, picked);
    setCardOrder(next);
    localStorage.setItem(DASH_ORDER_KEY, JSON.stringify(next));
  }

  function renderDashboardCards() {
    return (
      <div className="kpi-row" data-testid="dashboard-card-grid">
        {dashboardCards.map((card) => (
          <Kpi
            key={card.id}
            id={card.id}
            icon={card.icon}
            label={card.label}
            value={card.value}
            sub={card.sub}
            state={card.state}
            onOpen={() => navigate(card.to)}
            onDragStart={() => setDragCard(card.id)}
            onDrop={() => moveDashboardCard(card.id)}
          />
        ))}
      </div>
    );
  }

  return (
    <div className="page-pad home">
      <header className="home-greet">
        <h1 className="h1" data-testid="greeting">
          {greeting()}, {name}
        </h1>
        <p className="lead">
          {today} · here's your {variant === "store" || variant === "warehouse" ? "shift" : "day"} at a glance
          <span className="ctx-chip">{ctx}</span>
        </p>
        <button className="btn btn-sm dash-config-btn" onClick={() => setConfigOpen(true)} data-testid="dashboard-config-button">
          <Settings2 size={14} /> Configure dashboard
        </button>
      </header>

      {variant === "store" ? (
        // The store's Home is the D10 Dashboard (#174) - one payload, one store,
        // its own shape. The configurable-tile grid below belongs to the network
        // and warehouse views and is deliberately not drawn here: a store's
        // screen is a fixed morning routine, not a board somebody rearranges.
        <StoreDashboard />
      ) : variant === "warehouse" ? (
        <>
          <QuickActions
            items={[
              // Receive Goods' one way in (OPS-17): the inbox, whose Goods arrived
              // starts every vendor delivery.
              { label: "Receive", to: "/goods/receive", icon: <PackageCheck size={20} />, cta: true },
              { label: "Count", to: "/stock-count", icon: <ClipboardList size={20} /> },
              { label: "Transfer", to: "/transfer", icon: <ArrowLeftRight size={20} /> },
            ]}
          />
          {renderDashboardCards()}
          <div className="home-cols">
            <NetworkPanel s={summary} />
            <MorningBrief />
          </div>
        </>
      ) : (
        <>
          {renderDashboardCards()}
          <div className="home-cols">
            <div className="home-stack">
              <TrialBalancePanel canSeeMoney={canSeeMoney} />
              <CollectionsBand />
            </div>
            <div className="home-stack">
              <NetworkPanel s={summary} />
              <MorningBrief />
            </div>
          </div>
        </>
      )}
      {configOpen && <DashboardConfigurator cards={orderedCards} visible={visibleCards} onToggle={toggleDashboardCard} onClose={() => setConfigOpen(false)} />}
    </div>
  );
}
