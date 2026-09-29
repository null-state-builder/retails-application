// EOSS Planning (#318) - the sell-through-triggered markdown ladder's own
// screen (docs/05-offers/markdown-ai-research).
//
// Deliberately rule-based, not ML: a per-brand target curve says what percent
// of the buy should be sold by week N; a per-brand ladder says how deep a
// markdown goes once a style falls behind. This screen only ever *proposes* -
// every row starts and stays `pending` until a named person approves or
// rejects it, and approving is the one action that writes anything anyone
// else can see: it spins up the exact same `Offer` a brand's own discount
// would, tagged `eoss` for the month-end margin-attribution pack alone.

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CalendarRange,
  CheckCircle2,
  RefreshCw,
  Settings2,
  ShieldAlert,
  Tag,
  XCircle,
} from "lucide-react";

import { api, apiErrorMessage } from "../lib/api";
import { PageHeader } from "../components/PageHeader";
import { useList } from "../lib/hooks";
import "./OffersEoss.css";

interface SeasonRow {
  id: number;
  code: string;
  name: string;
  status: string;
}

interface BrandRow {
  id: number;
  code: string;
  name: string;
}

interface RecoRow {
  id: number;
  season_code: string;
  brand_name: string;
  brand_code: string;
  design: string;
  color: string;
  item: string;
  weeks_in_stock: number;
  on_hand_qty: number;
  sold_qty: number;
  sell_through_pct: string;
  target_pct: string;
  gap_pct: string;
  broken_size_run: boolean;
  margin_floor_pct: string | null;
  recommended_discount_pct: string;
  reason: string;
  status: "pending" | "approved" | "rejected";
  decided_discount_pct: string | null;
}

interface LadderStep {
  id?: number;
  trigger_type: "gap" | "age";
  trigger_value: string;
  discount_pct: string;
}
interface TargetPoint {
  id?: number;
  week_number: string;
  target_pct: string;
}

const TABS: { key: "" | RecoRow["status"]; label: string }[] = [
  { key: "", label: "All" },
  { key: "pending", label: "Pending" },
  { key: "approved", label: "Approved" },
  { key: "rejected", label: "Rejected" },
];

const STATUS_TONE: Record<string, string> = { pending: "amber", approved: "green", rejected: "grey" };

function num(v: string | null | undefined): number {
  return v ? parseFloat(v) : 0;
}

export function OffersEossPage() {
  const { data: seasons } = useList<SeasonRow>("/masters/seasons");
  const { data: brands } = useList<BrandRow>("/masters/brands");
  const [season, setSeason] = useState("");
  const [tab, setTab] = useState<"" | RecoRow["status"]>("");
  const [error, setError] = useState("");
  const [genBusy, setGenBusy] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [overrides, setOverrides] = useState<Record<number, string>>({});
  const [configOpen, setConfigOpen] = useState(false);

  useEffect(() => {
    if (season || seasons.length === 0) return;
    setSeason(seasons.find((s) => s.status === "eoss")?.code ?? seasons[0].code);
  }, [seasons, season]);

  const { data: rows, loading, reload } = useList<RecoRow>(
    season ? "/offers/eoss/recommendations" : null,
    { season, status: tab },
  );

  const counts = useMemo(() => {
    const c: Record<string, number> = { "": rows.length, pending: 0, approved: 0, rejected: 0 };
    rows.forEach((r) => (c[r.status] = (c[r.status] ?? 0) + 1));
    return c;
  }, [rows]);

  async function generate() {
    setError("");
    setGenBusy(true);
    try {
      await api.post("/offers/eoss/recommendations", { season });
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setGenBusy(false);
    }
  }

  async function decide(row: RecoRow, action: "approve" | "reject") {
    setError("");
    setBusyId(row.id);
    try {
      const discount_pct = overrides[row.id] ?? row.recommended_discount_pct;
      await api.post(`/offers/eoss/recommendations/${row.id}/decide`, {
        action,
        ...(action === "approve" ? { discount_pct } : {}),
      });
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="page-pad">
      <PageHeader
        title="EOSS Planning"
        lead="Styles falling behind their season's sell-through target, with a suggested markdown depth - never applied until you say so. Approving turns a row into the exact rulebook offer your counter already knows how to price with."
        actions={
          <div className="eoss-toolbar">
            <select
              className="input"
              value={season}
              onChange={(e) => setSeason(e.target.value)}
              data-testid="eoss-season-select"
            >
              {seasons.map((s) => (
                <option key={s.code} value={s.code}>
                  {s.name} {s.status === "eoss" ? "· EOSS" : ""}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-cta"
              disabled={!season || genBusy}
              onClick={generate}
              data-testid="eoss-generate-btn"
            >
              <RefreshCw size={14} /> {genBusy ? "Recalculating…" : "Recalculate plan"}
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => setConfigOpen((v) => !v)}
              data-testid="eoss-config-toggle"
            >
              <Settings2 size={14} /> Ladder & targets
            </button>
          </div>
        }
      />

      {error && (
        <div className="login-error" style={{ maxWidth: 560 }} data-testid="eoss-error">
          {error}
        </div>
      )}

      {configOpen && <EossConfigPanel brands={brands} />}

      <div className="eoss-tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={`chip chip-pick ${tab === t.key ? "chip-navy" : "chip-grey"}`}
            onClick={() => setTab(t.key)}
            data-testid={`eoss-tab-${t.key || "all"}`}
          >
            {t.label} ({counts[t.key] ?? 0})
          </button>
        ))}
      </div>

      {loading ? (
        <p className="lead">Loading…</p>
      ) : rows.length === 0 ? (
        <div className="card section-card" data-testid="eoss-empty">
          <p className="eyebrow">
            <Tag size={15} /> Nothing here yet
          </p>
          {season
            ? "No plan has been run for this season yet — pick a season and Recalculate."
            : "Set up a season first."}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="eoss-table">
            <thead>
              <tr>
                <th>Style</th>
                <th>Weeks</th>
                <th>Sell-through vs target</th>
                <th>Discount</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const st = num(row.sell_through_pct);
                const target = num(row.target_pct);
                return (
                  <tr key={row.id} data-testid={`eoss-row-${row.id}`}>
                    <td>
                      <p className="eyebrow">
                        <Tag size={13} /> {row.brand_name || "Storewide"}
                      </p>
                      <b>{row.design}</b> · {row.color}
                      {row.broken_size_run && (
                        <span
                          className="chip chip-amber"
                          style={{ marginLeft: 8 }}
                          data-testid={`eoss-broken-${row.id}`}
                        >
                          <AlertTriangle size={11} /> Broken size run
                        </span>
                      )}
                      <span className="eoss-reason">{row.reason}</span>
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <CalendarRange size={12} style={{ marginRight: 4 }} />
                      {row.weeks_in_stock}
                    </td>
                    <td>
                      <div className="eoss-prog">
                        <div className="eoss-prog-bar">
                          <div className="eoss-prog-fill" style={{ width: `${Math.min(100, st)}%` }} />
                          <div className="eoss-prog-target" style={{ left: `${Math.min(100, target)}%` }} />
                        </div>
                        <div className="eoss-prog-meta">
                          <span>{st.toFixed(0)}% sold</span>
                          <span>target {target.toFixed(0)}%</span>
                        </div>
                      </div>
                    </td>
                    <td data-testid={`eoss-discount-${row.id}`}>
                      {row.status === "pending" ? (
                        <input
                          className="input"
                          style={{ width: 72 }}
                          type="number"
                          min={0}
                          max={90}
                          value={overrides[row.id] ?? row.recommended_discount_pct}
                          onChange={(e) =>
                            setOverrides((o) => ({ ...o, [row.id]: e.target.value }))
                          }
                          data-testid={`eoss-override-${row.id}`}
                        />
                      ) : (
                        <b>{num(row.decided_discount_pct ?? row.recommended_discount_pct).toFixed(0)}</b>
                      )}
                      %
                      {row.margin_floor_pct && (
                        <span
                          className="eoss-reason"
                          title="The deepest markdown before the price falls below unit cost"
                        >
                          <ShieldAlert size={11} /> floor {num(row.margin_floor_pct).toFixed(0)}%
                        </span>
                      )}
                    </td>
                    <td>
                      <span className={`chip chip-${STATUS_TONE[row.status]}`}>{row.status}</span>
                    </td>
                    <td>
                      {row.status === "pending" && (
                        <div style={{ display: "flex", gap: 6 }}>
                          <button
                            type="button"
                            className="btn btn-cta"
                            disabled={busyId === row.id}
                            onClick={() => decide(row, "approve")}
                            data-testid={`eoss-approve-${row.id}`}
                          >
                            <CheckCircle2 size={14} /> Approve
                          </button>
                          <button
                            type="button"
                            className="btn"
                            disabled={busyId === row.id}
                            onClick={() => decide(row, "reject")}
                            data-testid={`eoss-reject-${row.id}`}
                          >
                            <XCircle size={14} /> Reject
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Ladder & sell-through-target editor - one brand's curve at a time.
// ---------------------------------------------------------------------------

function EossConfigPanel({ brands }: { brands: BrandRow[] }) {
  const [brandCode, setBrandCode] = useState("");
  const [ladder, setLadder] = useState<LadderStep[]>([]);
  const [targets, setTargets] = useState<TargetPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  useEffect(() => {
    setLoading(true);
    setOk("");
    api
      .get("/offers/eoss/config", { params: brandCode ? { brand: brandCode } : {} })
      .then((r) => {
        setLadder(
          r.data.ladder.map((s: any) => ({
            id: s.id, trigger_type: s.trigger_type, trigger_value: s.trigger_value, discount_pct: s.discount_pct,
          })),
        );
        setTargets(
          r.data.targets.map((t: any) => ({ id: t.id, week_number: t.week_number, target_pct: t.target_pct })),
        );
      })
      .catch((e) => setError(apiErrorMessage(e)))
      .finally(() => setLoading(false));
  }, [brandCode]);

  async function save() {
    setSaving(true);
    setError("");
    setOk("");
    try {
      await api.put("/offers/eoss/config", {
        brand: brandCode || undefined,
        ladder: ladder.map((s, i) => ({ step_no: i + 1, trigger_type: s.trigger_type, trigger_value: s.trigger_value, discount_pct: s.discount_pct })),
        targets: targets.map((t) => ({ week_number: t.week_number, target_pct: t.target_pct })),
      });
      setOk("Saved.");
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card section-card eoss-config-card" data-testid="eoss-config-panel">
      <div className="toolbar" style={{ marginBottom: 4 }}>
        <p className="eyebrow">
          <Settings2 size={14} /> Markdown ladder &amp; sell-through targets
        </p>
        <div className="spacer" />
        <select
          className="input"
          value={brandCode}
          onChange={(e) => setBrandCode(e.target.value)}
          data-testid="eoss-config-brand"
        >
          <option value="">Network default</option>
          {brands.map((b) => (
            <option key={b.code} value={b.code}>
              {b.name}
            </option>
          ))}
        </select>
      </div>

      {error && <div className="login-error">{error}</div>}
      {loading ? (
        <p className="lead">Loading…</p>
      ) : (
        <div className="eoss-config-cols">
          <div>
            <p className="eyebrow">Ladder — past this, offer this much</p>
            {ladder.map((s, i) => (
              <div className="eoss-ladder-row" key={i} data-testid={`eoss-ladder-row-${i}`}>
                <select
                  className="input"
                  value={s.trigger_type}
                  onChange={(e) =>
                    setLadder((rows) => rows.map((r, j) => (j === i ? { ...r, trigger_type: e.target.value as LadderStep["trigger_type"] } : r)))
                  }
                >
                  <option value="gap">pts behind target</option>
                  <option value="age">weeks in stock</option>
                </select>
                <input
                  className="input"
                  type="number"
                  value={s.trigger_value}
                  onChange={(e) => setLadder((rows) => rows.map((r, j) => (j === i ? { ...r, trigger_value: e.target.value } : r)))}
                />
                <span>→</span>
                <input
                  className="input"
                  type="number"
                  value={s.discount_pct}
                  onChange={(e) => setLadder((rows) => rows.map((r, j) => (j === i ? { ...r, discount_pct: e.target.value } : r)))}
                />
                <span>%</span>
                <button type="button" className="btn" onClick={() => setLadder((rows) => rows.filter((_, j) => j !== i))}>
                  <XCircle size={13} />
                </button>
              </div>
            ))}
            <button
              type="button"
              className="btn"
              onClick={() => setLadder((rows) => [...rows, { trigger_type: "gap", trigger_value: "10", discount_pct: "15" }])}
              data-testid="eoss-ladder-add"
            >
              + Step
            </button>
          </div>

          <div>
            <p className="eyebrow">Sell-through target curve</p>
            {targets.map((t, i) => (
              <div className="eoss-target-row" key={i} data-testid={`eoss-target-row-${i}`}>
                <span>Week</span>
                <input
                  className="input"
                  type="number"
                  value={t.week_number}
                  onChange={(e) => setTargets((rows) => rows.map((r, j) => (j === i ? { ...r, week_number: e.target.value } : r)))}
                />
                <span>→</span>
                <input
                  className="input"
                  type="number"
                  value={t.target_pct}
                  onChange={(e) => setTargets((rows) => rows.map((r, j) => (j === i ? { ...r, target_pct: e.target.value } : r)))}
                />
                <span>%</span>
                <button type="button" className="btn" onClick={() => setTargets((rows) => rows.filter((_, j) => j !== i))}>
                  <XCircle size={13} />
                </button>
              </div>
            ))}
            <button
              type="button"
              className="btn"
              onClick={() => setTargets((rows) => [...rows, { week_number: "4", target_pct: "20" }])}
              data-testid="eoss-target-add"
            >
              + Week
            </button>
          </div>
        </div>
      )}

      <div style={{ marginTop: 16, display: "flex", gap: 10, alignItems: "center" }}>
        <button type="button" className="btn btn-cta" disabled={saving} onClick={save} data-testid="eoss-config-save">
          {saving ? "Saving…" : "Save"}
        </button>
        {ok && <span style={{ color: "var(--green)" }}>{ok}</span>}
      </div>
    </div>
  );
}

export default OffersEossPage;
