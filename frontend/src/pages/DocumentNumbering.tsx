import { useCallback, useEffect, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta, typedApi } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";

type Payload = ApiRead<ApiSchemas["DocumentSeries"]>;
type Site = Payload["sites"][number];
type HeadOffice = Payload["head_office"][number];

/** What a prefix row is being edited for: a site, or head office for a GSTIN. */
interface Editing {
  key: string;
  owner: { site_id: number } | { gstin_id: number };
  revision: number;
  code: string;
}

/** Setup > Document Numbering (store operations ticket 04, ST-CMP-5).
 *
 *  Each store and each other site that sends goods has its own fixed 3-letter
 *  prefix, separate from its store code; head office has one per GSTIN, for debit
 *  notes. A prefix is unique across the company and cannot change once a number
 *  has used it. The new number format starts on a 1 April, set here; until then
 *  bills keep today's number. Everyone holding Setup reads the page; only Admin
 *  changes it, and the server refuses anybody else regardless. */
export function DocumentNumberingPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [startsFrom, setStartsFrom] = useState("");
  const [blockSize, setBlockSize] = useState("");
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await typedApi.get("/goods-v1/masters/document-series");
      const payload = response.data as Payload;
      setData(payload);
      setStartsFrom(payload.setting.new_format_from);
      setBlockSize(String(payload.setting.till_block_size));
    } catch (reason) {
      setError(apiErrorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(work: () => Promise<string>) {
    setError("");
    setSaved("");
    setBusy(true);
    try {
      setSaved(await work());
      await load();
    } catch (reason) {
      setError(apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  function savePrefix() {
    if (!editing) return;
    const target = editing;
    void run(async () => {
      const response = await api.post<{ code: string }>("/goods-v1/masters/document-series/prefixes", {
        ...goodsMeta(target.revision || undefined),
        ...target.owner,
        code: target.code.trim().toUpperCase(),
      });
      setEditing(null);
      return `Prefix ${response.data.code} is saved.`;
    });
  }

  function saveSetting() {
    if (!data) return;
    const size = Number(blockSize);
    void run(async () => {
      const response = await api.post<{ new_format_from: string }>(
        "/goods-v1/masters/document-series/setting",
        {
          ...goodsMeta(data.setting.revision || undefined),
          new_format_from: startsFrom,
          till_block_size: Number.isInteger(size) ? size : blockSize,
        },
      );
      return `Saved. The new number format starts on ${response.data.new_format_from}.`;
    });
  }

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Document Numbering" />
        {error ? <p className="warn-note">{error}</p> : <p>Loading document numbering…</p>}
      </div>
    );
  }

  const prefixCell = (key: string, row: Site | HeadOffice, owner: Editing["owner"]) =>
    editing?.key === key ? (
      <span className="toolbar">
        <input
          className="input"
          aria-label="Prefix"
          data-testid={`prefix-input-${key}`}
          maxLength={3}
          value={editing.code}
          onChange={(event) => setEditing({ ...editing, code: event.target.value })}
        />
        <button
          type="button"
          className="btn btn-primary"
          data-testid={`prefix-save-${key}`}
          disabled={busy}
          onClick={savePrefix}
        >
          Save
        </button>
        <button type="button" className="btn" onClick={() => setEditing(null)}>
          Cancel
        </button>
      </span>
    ) : (
      <span className="toolbar">
        <span className="mono" data-testid={`prefix-${key}`}>
          {row.prefix || "Not set"}
        </span>
        {row.used && <span className="chip">In use - fixed</span>}
        {data.can_change && !row.used && (
          <button
            type="button"
            className="btn"
            data-testid={`prefix-edit-${key}`}
            onClick={() => {
              setSaved("");
              setError("");
              setEditing({ key, owner, revision: row.revision, code: row.prefix });
            }}
          >
            {row.prefix ? "Change" : "Set prefix"}
          </button>
        )}
      </span>
    );

  return (
    <div className="page-pad">
      <PageHeader
        title="Document Numbering"
        lead="Each site's 3-letter prefix and the number series of every tax document. Every number stays within 16 characters."
      />
      {!data.can_change && (
        <p className="muted-cell" data-testid="numbering-read-only">
          Only Admin can change document numbering. You can see everything here.
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="numbering-error">
          {error}
        </p>
      )}
      {saved && (
        <p className="ok-note" data-testid="numbering-saved">
          {saved}
        </p>
      )}

      <section className="card section-card" data-testid="numbering-setting">
        <h2 className="h3">When the new format starts</h2>
        <p className="muted-cell">
          The number format can change only on 1 April. Until the date below, every bill keeps
          today's number. A store uses the new format only where it is switched on in Feature
          Switches.
        </p>
        <p data-testid="numbering-starts">
          {data.setting.started
            ? `The new format started on ${data.setting.new_format_from}.`
            : `The new format starts on ${data.setting.new_format_from}.`}
        </p>
        {data.can_change && (
          <div className="toolbar">
            <label className="field">
              <span>Starts on (a 1 April)</span>
              <input
                className="input"
                type="date"
                data-testid="numbering-start-date"
                min={data.today}
                value={startsFrom}
                onChange={(event) => setStartsFrom(event.target.value)}
              />
            </label>
            <label className="field">
              <span>Invoice numbers in one offline till block</span>
              <input
                className="input"
                inputMode="numeric"
                data-testid="numbering-block-size"
                value={blockSize}
                onChange={(event) => setBlockSize(event.target.value)}
              />
            </label>
            <button
              type="button"
              className="btn btn-primary"
              data-testid="numbering-setting-save"
              disabled={busy}
              onClick={saveSetting}
            >
              Save
            </button>
          </div>
        )}
      </section>

      <section className="card section-card">
        <h2 className="h3">Series</h2>
        <div className="table-wrap">
          <table className="data" data-testid="numbering-series">
            <thead>
              <tr>
                <th>Document</th>
                <th>Number</th>
                <th>Prefix of</th>
              </tr>
            </thead>
            <tbody>
              {data.series.map((series) => (
                <tr key={series.code}>
                  <td>{series.name}</td>
                  <td className="mono">{series.example.replace(/\/1$/, "/n")}</td>
                  <td>{series.owner}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card section-card">
        <h2 className="h3">Sites</h2>
        <div className="table-wrap">
          <table className="data" data-testid="numbering-sites">
            <thead>
              <tr>
                <th>Site</th>
                <th>Prefix</th>
                <th>New series</th>
              </tr>
            </thead>
            <tbody>
              {data.sites.map((site) => (
                <tr key={site.id} data-testid={`numbering-site-${site.code}`}>
                  <td>
                    {site.name} ({site.code}){site.kind === "warehouse" ? " - warehouse" : ""}
                  </td>
                  <td>{prefixCell(`site-${site.code}`, site, { site_id: site.id })}</td>
                  <td>{site.series_on ? "Switched on" : "Switched off"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card section-card">
        <h2 className="h3">Head office, one prefix per GSTIN</h2>
        <div className="table-wrap">
          <table className="data" data-testid="numbering-head-office">
            <thead>
              <tr>
                <th>GSTIN</th>
                <th>Prefix (debit notes)</th>
              </tr>
            </thead>
            <tbody>
              {data.head_office.map((row) => (
                <tr key={row.id} data-testid={`numbering-gstin-${row.gstin}`}>
                  <td>
                    {row.gstin} ({row.state_name})
                  </td>
                  <td>{prefixCell(`gstin-${row.gstin}`, row, { gstin_id: row.id })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card section-card">
        <h2 className="h3">Offline till number blocks</h2>
        {data.blocks.length === 0 ? (
          <p className="muted-cell" data-testid="numbering-no-blocks">
            No till holds a block of invoice numbers.
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="numbering-blocks">
              <thead>
                <tr>
                  <th>Counter</th>
                  <th>Month</th>
                  <th>Numbers</th>
                </tr>
              </thead>
              <tbody>
                {data.blocks.map((block) => (
                  <tr key={block.id}>
                    <td>{block.counter}</td>
                    <td>{block.month}</td>
                    <td className="mono">
                      {block.first_number} to {block.last_number}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card section-card">
        <h2 className="h3">Cancelled numbers (GSTR-1 Table 13)</h2>
        <p className="muted-cell">
          At month end, every number in an offline till's block that no bill used is recorded as
          cancelled. A number is never used again, and the gap stays visible here.
        </p>
        {data.cancelled.length === 0 ? (
          <p className="muted-cell" data-testid="numbering-no-cancelled">
            No number has been cancelled.
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="numbering-cancelled">
              <thead>
                <tr>
                  <th>Month</th>
                  <th>Prefix</th>
                  <th className="num">Count</th>
                  <th>From</th>
                  <th>To</th>
                </tr>
              </thead>
              <tbody>
                {data.cancelled.map((row) => (
                  <tr key={`${row.month}-${row.prefix}-${row.series}`}>
                    <td>{row.month}</td>
                    <td>{row.prefix}</td>
                    <td className="num">{row.count}</td>
                    <td className="mono">{row.first}</td>
                    <td className="mono">{row.last}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
