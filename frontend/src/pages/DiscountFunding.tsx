import { useCallback, useEffect, useState } from "react";

import { Note } from "../components/Note";
import { PageHeader } from "../components/PageHeader";
import { UpdatedAgo } from "../components/UpdatedAgo";
import { useCachedGet } from "../lib/cachedQuery";
import { SegmentedControl } from "../components/SegmentedControl";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { FUNDING_GROUPINGS, defaultFundingFilters, fundingParams } from "../lib/discountFunding";
import type { FundingFilters, FundingGrouping } from "../lib/discountFunding";
import { alignsRight, gstCell } from "../lib/gstReport";
import type { CellKind } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["DiscountFundingReport"]>;
type Row = Payload["rows"][number];
type Column = { key: string; label: string; kind: CellKind };

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > Discount Funding (store operations PRD ST-OFR-2, ticket 25).
 *
 *  Who paid for the discounts given: the brand's share and KDPS's share, by
 *  offer, brand or store. Each discounted line's split was worked out when its
 *  bill reached head office, from the offer and the brand terms in force on the
 *  bill date; where the brand's model or share is unknown, it is counted as
 *  unknown, never guessed.
 *
 *  The figures come from the reporting copy the server keeps apart from
 *  billing; the server decides which stores, and only Accounts and Owner may
 *  read it at all. It reads live data only: offline it says it needs a
 *  connection, and comes back by itself when the line does. */
export function DiscountFundingPage() {
  const [filters, setFilters] = useState<FundingFilters>(() => defaultFundingFilters());
  const [exportError, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last export got no answer at all.
  const [exportLost, setLost] = useState(false);
  // Half-way through changing both dates the period can run backwards; say
  // so rather than ask the server a question it will refuse. The last good
  // period stays on screen.
  const backwards = Boolean(
    filters.date_from && filters.date_to && filters.date_to < filters.date_from,
  );
  const [asked, setAsked] = useState(filters);
  if (!backwards && asked !== filters) setAsked(filters);
  // Through the shared cache: a report seen before shows at once and is always
  // read again behind it. Offline, the request waits for the connection.
  const read = useCachedGet<Payload>("/reports/discount-funding", { params: fundingParams(asked) });
  useEffect(() => setError(""), [filters]);
  const data = read.data ?? null;
  const busy = read.fetching;
  const lost = exportLost || (read.error != null && isConnectionLost(read.error));
  const readError =
    read.error != null && !isConnectionLost(read.error) ? apiErrorMessage(read.error) : "";
  const error = backwards
    ? "The period ends before it starts. Choose a To date on or after the From date."
    : exportError || readError;
  useEffect(() => {
    if (read.updatedAt) setLost(false);
  }, [read.updatedAt]);

  const { reload } = read;
  const load = useCallback(() => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    reload();
  }, [reload]);

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
  }, [filters, load]);

  function set<K extends keyof FundingFilters>(key: K, value: FundingFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/discount-funding/export.xlsx", {
        params: fundingParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `discount-funding-${filters.group_by}-${filters.date_from}-${filters.date_to}.xlsx`;
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
      testId="funding-offline"
      role="status"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="funding-retry"
            disabled={busy}
            onClick={() => load()}
          >
            Try again
          </button>
        )
      }
    >
      {OFFLINE}
    </Note>
  );
  const lead =
    "How much of each discount the brand funds and how much KDPS funds, by offer, brand and store. Where the brand's model or share is unknown, it says unknown.";

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Discount Funding" lead={lead} />
        {offlineNote}
        {error ? (
          <Note tone="error" testId="funding-error">
            {error}
          </Note>
        ) : (
          online && !lost && <p>Loading the discount funding report…</p>
        )}
      </div>
    );
  }

  const columns = data.columns as Column[];

  return (
    <div className="page-pad">
      <PageHeader
        title="Discount Funding"
        lead={lead}
        actions={<UpdatedAgo updatedAt={read.updatedAt} fetching={read.fetching} />}
      />
      {offlineNote}
      <div className="toolbar" data-testid="funding-filters">
        <label className="field">
          <span>Store</span>
          <select
            className="input"
            data-testid="funding-filter-store"
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
        <label className="field">
          <span>From</span>
          <input
            className="input"
            type="date"
            data-testid="funding-filter-from"
            value={filters.date_from}
            disabled={disabled}
            onChange={(event) => set("date_from", event.target.value)}
          />
        </label>
        <label className="field">
          <span>To</span>
          <input
            className="input"
            type="date"
            data-testid="funding-filter-to"
            value={filters.date_to}
            disabled={disabled}
            onChange={(event) => set("date_to", event.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn btn-primary"
          data-testid="funding-export"
          disabled={disabled}
          onClick={() => void exportWorkbook()}
        >
          Export to Excel
        </button>
      </div>
      <SegmentedControl
        label="Group by"
        testId="funding-groupings"
        disabled={disabled}
        value={filters.group_by as FundingGrouping}
        options={FUNDING_GROUPINGS.map((v) => ({
          value: v.key as FundingGrouping,
          label: v.label,
          testId: `funding-group-${v.key}`,
        }))}
        onChange={(next) => set("group_by", next)}
      />
      <p className="muted-cell" data-testid="funding-as-of">
        {asOfText(data.as_of)}
        {data.as_of &&
          ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
        {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
      </p>
      {error && (
        <Note tone="error" testId="funding-error">
          {error}
        </Note>
      )}
      {data.missing.length > 0 && (
        <Note tone="warn" testId="funding-missing">
          <strong>Not included, or to check</strong>
          <ul>
            {data.missing.map((m) => (
              <li key={m.code} data-testid={`funding-missing-${m.code}`}>
                {m.text}
              </li>
            ))}
          </ul>
        </Note>
      )}
      {data.rows.length === 0 ? (
        <p className="muted-cell" data-testid="funding-empty">
          {busy ? "Loading…" : "No discounted lines with a split in this period at these stores."}
        </p>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="funding-table">
            <thead>
              <tr>
                {columns.map((c) => (
                  <th
                    key={c.key}
                    className={alignsRight(c.kind) ? "num" : undefined}
                    data-testid={`funding-col-${c.key}`}
                  >
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <FundingTableRow key={row.key} row={row} columns={columns} />
              ))}
            </tbody>
            <tfoot>
              <FundingTableRow row={data.total} columns={columns} total />
            </tfoot>
          </table>
        </div>
      )}
      <details className="muted-cell" data-testid="funding-basis">
        <summary>How these figures are worked out (formula {data.formula_version})</summary>
        <ul>
          {data.basis.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </details>
    </div>
  );
}

function FundingTableRow({
  row,
  columns,
  total,
}: {
  row: Row;
  columns: Column[];
  total?: boolean;
}) {
  const cells = row as unknown as Record<string, unknown>;
  return (
    <tr data-testid={total ? "funding-total" : "funding-row"} data-key={row.key}>
      {columns.map((c, index) => {
        // The total row names itself in its first cell and leaves the funder blank.
        const value = cells[c.key];
        const text = total && (value === undefined || value === "") ? "" : gstCell(value, c.kind);
        return (
          <td
            key={c.key}
            className={alignsRight(c.kind) ? "num tabular" : undefined}
            data-testid={`funding-cell-${c.key}`}
          >
            {total && index === 0 ? <strong>{text}</strong> : text}
          </td>
        );
      })}
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
