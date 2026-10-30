import { useCallback, useEffect, useRef, useState } from "react";

import { Download } from "lucide-react";

import { KpiStrip, LeadFigure } from "../components/KpiStrip";
import type { Kpi } from "../components/KpiStrip";
import { Note } from "../components/Note";
import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { Section } from "../components/Section";
import { SegmentedControl } from "../components/SegmentedControl";
import { EmptyState, Loading } from "../components/States";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { formatINR } from "../lib/format";
import {
  GROUPINGS,
  asOfText,
  cellText,
  columnsFor,
  defaultFilters,
  filterParams,
} from "../lib/salesReport";
import type { GroupBy, Row, SalesFilters } from "../lib/salesReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["SalesReport"]>;

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > Sales (store operations PRD ST-RPT-1, ticket 10).
 *
 *  Bills, pieces, value, average selling price, average bill value, units per
 *  bill, discount and target achievement, by day, store, brand, category,
 *  salesperson or tender. The figures come from the reporting copy the server
 *  keeps apart from billing, so the page says how fresh that copy is, on what
 *  basis every figure is worked out, and what it could not include. The server
 *  decides which stores, and whether cost, margin and targets are sent at all.
 *  It reads live data only: offline it says it needs a connection. */
export function SalesReportPage() {
  const [filters, setFilters] = useState<SalesFilters>(() => defaultFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: SalesFilters) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    // Half-way through changing both dates the period can run backwards; say
    // so rather than ask the server a question it will refuse.
    if (wanted.date_from && wanted.date_to && wanted.date_to < wanted.date_from) {
      setError("The period ends before it starts. Choose a To date on or after the From date.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const response = await api.get<Payload>("/reports/sales", { params: filterParams(wanted) });
      if (mine !== request.current) return;
      setLost(false);
      setData(response.data);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    } finally {
      if (mine === request.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load(filters);
  }, [filters, load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(filters);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [filters, load]);

  // The server says which groupings this viewer may have; by salesperson is the
  // team field and is never offered until it has said so.
  const offered = data?.groupings.map((g) => g.key);
  const tabs = GROUPINGS.filter((g) =>
    offered ? offered.includes(g.key) : g.key !== "salesperson",
  );

  function set<K extends keyof SalesFilters>(key: K, value: SalesFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/sales/export.xlsx", {
        params: filterParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `sales-report-${filters.group_by}-${filters.date_from}-${filters.date_to}.xlsx`;
      link.click();
      URL.revokeObjectURL(href);
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setLost(true);
        return;
      }
      setError(await blobErrorMessage(reason));
    }
  }

  const disabled = !online || busy;
  const offlineNote = (!online || lost) && (
    <Note
      tone="warn"
      role="status"
      testId="sales-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="sales-retry"
            disabled={busy}
            onClick={() => void load(filters)}
          >
            Try again
          </button>
        )
      }
    >
      {OFFLINE}
    </Note>
  );
  const errorNote = error && (
    <Note tone="error" testId="sales-error">
      {error}
    </Note>
  );
  const lead =
    "Bills, pieces, value, average selling price, average bill, units per bill, discount and target, from a copy of the bills kept apart from billing.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="Sales" lead={lead} />
        <div className="page">
          {offlineNote}
          {error ? errorNote : online && !lost && <Loading label="Loading the sales report" />}
        </div>
      </OperationsPage>
    );
  }

  const columns = columnsFor(data);
  const grouping = GROUPINGS.find((g) => g.key === data.group_by)?.label ?? "Group";
  const total = data.total as Row;

  // The period and the store, in the header where the page's own controls go
  // (the board): the filters choose what every figure below is about.
  const filterBar = (
    <div className="page-head-actions" data-testid="sales-filters">
      <label>
        <span className="sr-only">Store</span>
        <select
          className="input"
          style={{ width: "auto" }}
          data-testid="sales-filter-store"
          value={filters.store}
          disabled={disabled}
          onChange={(event) => set("store", event.target.value)}
        >
          <option value="">All my stores</option>
          {data.store_options.map((s) => (
            <option key={s.id} value={s.code}>
              {s.name} ({s.code})
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="sr-only">From</span>
        <input
          className="input"
          type="date"
          style={{ width: "auto" }}
          data-testid="sales-filter-from"
          value={filters.date_from}
          disabled={disabled}
          onChange={(event) => set("date_from", event.target.value)}
        />
      </label>
      <span className="muted-cell" aria-hidden="true">
        to
      </span>
      <label>
        <span className="sr-only">To</span>
        <input
          className="input"
          type="date"
          style={{ width: "auto" }}
          data-testid="sales-filter-to"
          value={filters.date_to}
          disabled={disabled}
          onChange={(event) => set("date_to", event.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn"
        data-testid="sales-export"
        disabled={disabled}
        onClick={() => void exportWorkbook()}
      >
        <Download size={16} aria-hidden="true" /> Export to Excel
      </button>
    </div>
  );

  // One lead figure, then at most five beside it (KpiStrip). Target, when the
  // server sends it, is said in the lead's sentence rather than as a sixth.
  const target =
    data.shows_target && total.target_pct != null && total.target_paise != null
      ? `${cellText(total.target_pct, "percent")} of the ${cellText(total.target_paise, "money")} target.`
      : "";
  const kpis: Kpi[] = [
    { label: "Bills", value: cellText(total.bills, "count"), testId: "sales-kpi-bills" },
    {
      label: "Pieces",
      value: cellText(total.pieces, "pieces"),
      detail:
        total.asp_paise == null
          ? undefined
          : `Average ${cellText(total.asp_paise, "money")} a piece`,
      testId: "sales-kpi-pieces",
    },
    {
      label: "Average bill",
      value: cellText(total.abv_paise, "money"),
      detail: "Value ÷ bills",
      testId: "sales-kpi-abv",
    },
    {
      label: "Units per bill",
      value: cellText(total.upt, "ratio"),
      detail: "Pieces ÷ bills",
      testId: "sales-kpi-upt",
    },
    {
      label: "Discount",
      value: cellText(total.disc_paise, "money"),
      detail:
        total.discount_pct == null
          ? undefined
          : `${cellText(total.discount_pct, "percent")} of MRP`,
      testId: "sales-kpi-discount",
    },
  ];

  return (
    <OperationsPage>
      <PageHeader title="Sales" lead={lead} actions={filterBar} />
      <div className="page">
        {offlineNote}
        {errorNote}
        {data.missing.length > 0 && (
          <Note tone="warn" testId="sales-missing">
            <strong>Not included</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`sales-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </Note>
        )}

        <div className="section" data-testid="sales-figures">
          <LeadFigure
            label="Value"
            value={cellText(total.value_paise, "money")}
            testId="sales-lead"
          >
            After discount, GST included, returns netted, on {cellText(total.bills, "count")} bills.{" "}
            {target}
          </LeadFigure>
          <KpiStrip items={kpis} label="The period in figures" />
        </div>

        {data.group_by === "day" && <DayChart data={data} />}

        <Section
          heading={`By ${grouping.toLowerCase()}`}
          ruled
          aside={
            <p className="muted-cell" data-testid="sales-as-of" style={{ margin: 0 }}>
              {asOfText(data.as_of)}
              {data.as_of &&
                ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
              {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
            </p>
          }
        >
          {/* The grouping swaps the whole table, so it is a SegmentedControl.
              The server says which groupings this viewer may have. */}
          <SegmentedControl<GroupBy>
            label="Group by"
            testId="sales-groupings"
            value={filters.group_by}
            disabled={disabled}
            onChange={(value) => set("group_by", value)}
            options={tabs.map((g) => ({
              value: g.key,
              label: `By ${g.label.toLowerCase()}`,
              testId: `sales-group-${g.key}`,
            }))}
          />
          {data.rows.length === 0 ? (
            <EmptyState testId="sales-empty">
              {busy ? "Loading…" : "No sales in this period at these stores."}
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="data" data-testid="sales-table">
                <thead>
                  <tr>
                    <th scope="col">{grouping}</th>
                    {columns.map((c) => (
                      <th
                        key={c.key}
                        scope="col"
                        className={`num ${PRIORITY[c.key] ?? ""}`.trim()}
                        data-testid={`sales-col-${c.key}`}
                      >
                        {c.label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((row) => (
                    <ReportRow key={row.key} row={row as Row} columns={columns} />
                  ))}
                </tbody>
                <tfoot>
                  <ReportRow row={total} columns={columns} total />
                </tfoot>
              </table>
            </div>
          )}
          <details className="muted-cell" data-testid="sales-basis">
            <summary>How these figures are worked out (formula {data.formula_version})</summary>
            <ul>
              {data.basis.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </details>
        </Section>
      </div>
    </OperationsPage>
  );
}

/** Column priority (v2 Table): the money a store is judged on always shows;
 *  the amounts behind a percentage come in on wider screens. */
const PRIORITY: Partial<Record<keyof Row, "p2" | "p3">> = {
  target_paise: "p2",
  margin_paise: "p2",
  cost_paise: "p3",
};

/** The longest period drawn as one bar a day; beyond it the table says it. */
const CHART_MAX_DAYS = 62;

/** Value billed each day of the period, from the by-day rows already loaded.
 *  A day with no row sold nothing. Navy bars on warm grey rules, no rust:
 *  rust marks only where you are (decision 4). */
function DayChart({ data }: { data: Payload }) {
  const byDay = new Map(data.rows.map((r) => [r.key, r.value_paise]));
  const days: { key: string; value: number }[] = [];
  const start = new Date(`${data.date_from}T00:00:00Z`);
  const end = new Date(`${data.date_to}T00:00:00Z`);
  for (let d = start; d <= end && days.length <= CHART_MAX_DAYS; ) {
    const key = d.toISOString().slice(0, 10);
    days.push({ key, value: byDay.get(key) ?? 0 });
    d = new Date(d.getTime() + 86_400_000);
  }
  if (days.length < 2 || days.length > CHART_MAX_DAYS) return null;
  const best = days.reduce((a, b) => (b.value > a.value ? b : a));
  if (best.value <= 0) return null;

  const W = 1000;
  const H = 200;
  const left = 56;
  const top = 12;
  const bottom = 176;
  const step = (W - left) / days.length;
  const bar = Math.max(2, step * 0.62);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * best.value);
  const y = (v: number) => bottom - (v / best.value) * (bottom - top);
  const label = (iso: string) =>
    new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-IN", {
      timeZone: "UTC",
      weekday: "short",
      day: "numeric",
      month: "short",
    });

  return (
    <Section
      heading="Every day of the period"
      ruled
      testId="sales-day-chart"
      aside={
        <span className="muted-cell">
          Best day: {label(best.key)}, {cellText(best.value, "money")}
        </span>
      }
    >
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label={`Value billed each day, ${data.date_from} to ${data.date_to}`}
        style={{ display: "block", fontFamily: "inherit" }}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={left} x2={W} y1={y(t)} y2={y(t)} stroke="var(--hairline)" />
            <text x={left - 8} y={y(t) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">
              {formatINR(Math.round(t / 100) * 100, { short: true })}
            </text>
          </g>
        ))}
        {days.map((d, i) => {
          const x = left + i * step + (step - bar) / 2;
          const sunday = new Date(`${d.key}T00:00:00Z`).getUTCDay() === 0;
          const every = days.length > 31 ? 7 : 1;
          return (
            <g key={d.key}>
              <title>{`${label(d.key)}: ${cellText(d.value, "money")}`}</title>
              {d.value > 0 && (
                <rect
                  x={x}
                  y={y(d.value)}
                  width={bar}
                  height={bottom - y(d.value)}
                  rx="2"
                  fill="var(--navy)"
                />
              )}
              {i % every === 0 && (
                <text
                  x={x + bar / 2}
                  y={bottom + 18}
                  textAnchor="middle"
                  fontSize="10.5"
                  fontWeight={sunday ? 700 : 400}
                  fill="var(--muted)"
                >
                  {Number(d.key.slice(8, 10))}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </Section>
  );
}

function ReportRow({
  row,
  columns,
  total,
}: {
  row: Row;
  columns: ReturnType<typeof columnsFor>;
  total?: boolean;
}) {
  return (
    <tr data-testid={total ? "sales-total" : "sales-row"} data-key={row.key}>
      <td>{total ? row.label : <b>{row.label}</b>}</td>
      {columns.map((c) => (
        <td
          key={c.key}
          className={`num tabular ${PRIORITY[c.key] ?? ""}`.trim()}
          data-testid={`sales-cell-${c.key}`}
        >
          {c.key === "value_paise" && !total ? (
            <b>{cellText(row[c.key], c.kind)}</b>
          ) : (
            cellText(row[c.key], c.kind)
          )}
        </td>
      ))}
    </tr>
  );
}

/** A refused download arrives as a Blob; read the server's sentence out of it. */
async function blobErrorMessage(reason: unknown): Promise<string> {
  const data = (reason as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text()) as unknown;
      return apiErrorMessage({ response: { data: parsed } });
    } catch {
      return "The export could not be made. Please try again.";
    }
  }
  return apiErrorMessage(reason);
}
