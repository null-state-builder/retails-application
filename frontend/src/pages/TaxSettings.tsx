import { useCallback, useEffect, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta, typedApi } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { formatDateTime, formatINR, paiseToRupees, rupeesToPaise } from "../lib/format";
import { rateHundredths } from "../till/pricing";

type Payload = ApiRead<ApiSchemas["TaxSettings"]>;
type Version = Payload["versions"][number];
type Rule = Version["rules"][number];

/** A rule row as Admin is typing it: the price line in rupees, as text. A
 *  rate-schedule row (ticket 12) keeps its one rate in `rate_below`. */
interface DraftRule {
  kind: "price_line" | "flat_rate";
  hsn_prefix: string;
  name: string;
  line_rupees: string;
  rate_below: string;
  rate_above: string;
}

interface Draft {
  applies_from: string;
  rules: DraftRule[];
  unmatched_rate: string;
  /** Ticket 11: 100 rounds the invoice total to the nearest rupee; 1 does not. */
  round_total_paise: number;
  /** Ticket 13: may a store take back a bill another GSTIN issued? */
  cross_gstin_returns: boolean;
  /** Ticket 13: each annual return's filing date, as Accounts recorded it. */
  filed: FiledRow[];
  /** Ticket 14: is a gift with purchase gift stock (its input tax credit reversed)? */
  gift_with_purchase_is_gift: boolean;
  note: string;
}

/** One annual return filing date: whose GSTIN, which year (`25-26`), which day. */
interface FiledRow {
  gstin: string;
  fy: string;
  day: string;
}

function filedRows(filed: Record<string, Record<string, string>> | undefined): FiledRow[] {
  return Object.entries(filed ?? {}).flatMap(([gstin, years]) =>
    Object.entries(years).map(([fy, day]) => ({ gstin, fy, day })),
  );
}

/** Setup > Tax Settings (store operations PRD §6, ticket 03).
 *
 *  Every version, newest first: version 1 is the tax slab table every bill used
 *  before, and each save adds a new version with the date it applies from. An
 *  old version is never changed. A store is taxed by the saved versions only
 *  where its tax-settings switch is on (its CA sign-off gate closed on
 *  1 October 2026). Accounts and everyone else holding Setup read the page; only
 *  Admin saves, and the server refuses anybody else regardless. */
export function TaxSettingsPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await typedApi.get("/goods-v1/masters/tax-settings");
      setData(response.data as Payload);
    } catch (reason) {
      setError(apiErrorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save() {
    if (!data || !draft) return;
    setError("");
    setSaved("");
    const rules: Record<string, string | number>[] = [];
    for (const [index, rule] of draft.rules.entries()) {
      if (rule.kind === "flat_rate") {
        rules.push({
          kind: "flat_rate",
          hsn_prefix: rule.hsn_prefix.trim(),
          name: rule.name.trim(),
          rate: rule.rate_below.trim(),
        });
        continue;
      }
      const line = rupeesToPaise(rule.line_rupees);
      if (line === null) {
        setError(`Rule ${index + 1}: the price line must be an amount in rupees.`);
        return;
      }
      rules.push({
        kind: "price_line",
        hsn_prefix: rule.hsn_prefix.trim(),
        name: rule.name.trim(),
        threshold_paise: line,
        rate_below: rule.rate_below.trim(),
        rate_above: rule.rate_above.trim(),
      });
    }
    setBusy(true);
    try {
      const response = await api.post<{
        version: number;
        applies_from: string;
      }>("/goods-v1/masters/tax-settings/versions", {
        ...goodsMeta(data.latest_version),
        applies_from: draft.applies_from,
        rules,
        unmatched_rate: draft.unmatched_rate.trim(),
        options: {
          round_total_paise: draft.round_total_paise,
          cross_gstin_returns: draft.cross_gstin_returns,
          gift_with_purchase_is_gift: draft.gift_with_purchase_is_gift,
          annual_return_filed: draft.filed
            .filter((row) => row.gstin.trim() || row.fy.trim() || row.day.trim())
            .reduce<Record<string, Record<string, string>>>((out, row) => {
              const gstin = row.gstin.trim().toUpperCase();
              out[gstin] = { ...(out[gstin] ?? {}), [row.fy.trim()]: row.day.trim() };
              return out;
            }, {}),
        },
        note: draft.note.trim(),
      });
      setSaved(
        `Version ${response.data.version} is saved. It applies from ${response.data.applies_from}.`,
      );
      setDraft(null);
      await load();
    } catch (reason) {
      setError(apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Tax Settings" />
        {error ? <p className="warn-note">{error}</p> : <p>Loading tax settings…</p>}
      </div>
    );
  }

  return (
    <div className="page-pad">
      <PageHeader
        title="Tax Settings"
        lead="The GST rate for each HSN, kept as versions. A new version changes only bills made on or after its date."
      />
      {!data.can_change && (
        <p className="muted-cell" data-testid="tax-settings-read-only">
          Only Admin can change the tax settings. You can see every version.
        </p>
      )}
      {data.gate && (
        <p className="warn-note" data-testid="tax-settings-gate">
          Waiting for {data.gate}. Until then a real store keeps version 1, the tax slab table.
        </p>
      )}

      <section className="card section-card">
        <h2 className="h3">Stores</h2>
        {data.stores.length === 0 ? (
          <p className="muted-cell">You have no store to see.</p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="tax-settings-stores">
              <thead>
                <tr>
                  <th>Store</th>
                  <th>Taxed today by</th>
                </tr>
              </thead>
              <tbody>
                {data.stores.map((store) => (
                  <tr key={store.id} data-testid={`tax-store-${store.code}`}>
                    <td>
                      {store.name} ({store.code}){store.real ? "" : " - demo"}
                    </td>
                    <td>
                      {store.rules_on
                        ? `Version ${store.version_today}`
                        : "Version 1 (tax slabs). Tax settings are switched off here."}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {data.can_change && !draft && (
        <p>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="tax-new-version"
            onClick={() => {
              setSaved("");
              setError("");
              setDraft(startFrom(data));
            }}
          >
            New version
          </button>
        </p>
      )}

      {draft && (
        <VersionForm
          draft={draft}
          today={data.today}
          number={data.latest_version + 1}
          busy={busy}
          onChange={setDraft}
          onSave={() => void save()}
          onCancel={() => setDraft(null)}
        />
      )}
      {error && (
        <p className="warn-note" data-testid="tax-settings-error">
          {error}
        </p>
      )}
      {saved && (
        <p className="ok-note" data-testid="tax-settings-saved">
          {saved}
        </p>
      )}

      {data.versions.map((version) => (
        <VersionCard key={version.version} version={version} />
      ))}
    </div>
  );
}

function VersionCard({ version }: { version: Version }) {
  return (
    <section className="card section-card" data-testid={`tax-version-${version.version}`}>
      <h2 className="h3">
        Version {version.version}{" "}
        {version.legacy ? (
          <span className="chip">Tax slab table</span>
        ) : (
          <span className="chip chip-amber">Applies from {version.applies_from}</span>
        )}
      </h2>
      <p className="muted-cell">
        {version.legacy
          ? version.note
          : `Saved ${version.saved_at ? formatDateTime(version.saved_at) : ""}${
              version.saved_by ? ` by ${version.saved_by}` : ""
            }. ${version.note}`}
      </p>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>HSN starts with</th>
              <th>Name</th>
              <th className="num">Price line (before tax)</th>
              <th className="num">At or under</th>
              <th className="num">Over</th>
              {version.legacy && <th>From</th>}
            </tr>
          </thead>
          <tbody>
            {version.rules.map((rule: Rule, index: number) =>
              rule.kind === "flat_rate" ? (
                <tr key={`${rule.hsn_prefix}-${index}`} data-testid={`tax-flat-${rule.hsn_prefix}`}>
                  <td>{rule.hsn_prefix}</td>
                  <td>{rule.name}</td>
                  <td className="num">Rate schedule: any price</td>
                  <td className="num" colSpan={2}>
                    {rule.rate ?? rule.rate_above}%
                  </td>
                  {version.legacy && <td />}
                </tr>
              ) : (
                <tr key={`${rule.hsn_prefix}-${index}`}>
                  <td>{rule.hsn_prefix || "Any HSN"}</td>
                  <td>{rule.name}</td>
                  <td className="num">{formatINR(rule.threshold_paise)}</td>
                  <td className="num">{rule.rate_below}%</td>
                  <td className="num">{rule.rate_above}%</td>
                  {version.legacy && <td>{rule.effective_from ?? ""}</td>}
                </tr>
              ),
            )}
          </tbody>
        </table>
      </div>
      {version.unmatched_rate && (
        <p className="muted-cell">
          An item whose HSN no rule covers is charged {version.unmatched_rate}% and the bill is
          flagged.
        </p>
      )}
      <p className="muted-cell" data-testid="tax-version-rounding">
        Tax is worked out to the paisa on each line.{" "}
        {version.options.round_total_paise === 1
          ? "The invoice total is not rounded."
          : "The invoice total is rounded to the nearest rupee, with a round-off line."}
      </p>
      <p className="muted-cell" data-testid="tax-version-cross-gstin">
        {version.options.cross_gstin_returns
          ? "A store may take back a bill another GSTIN issued."
          : "A return is taken only within the GSTIN that issued the bill."}
      </p>
      <p className="muted-cell" data-testid="tax-version-gift">
        {version.options.gift_with_purchase_is_gift === false
          ? "A gift with purchase is part of the sale; only a piece given free with no gift offer is gift stock."
          : "A gift with purchase is gift stock: its input tax credit is reversed."}
      </p>
      {filedRows(version.options.annual_return_filed).length > 0 && (
        <p className="muted-cell" data-testid="tax-version-filed">
          Annual returns filed:{" "}
          {filedRows(version.options.annual_return_filed)
            .map((row) => `${row.gstin} for ${row.fy} on ${row.day}`)
            .join("; ")}
          . A credit note for a bill of that year reduces tax only up to that day (or 30 November,
          if earlier).
        </p>
      )}
    </section>
  );
}

function VersionForm({
  draft,
  today,
  number,
  busy,
  onChange,
  onSave,
  onCancel,
}: {
  draft: Draft;
  today: string;
  number: number;
  busy: boolean;
  onChange: (next: Draft) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const setRule = (index: number, patch: Partial<DraftRule>) =>
    onChange({
      ...draft,
      rules: draft.rules.map((rule, i) => (i === index ? { ...rule, ...patch } : rule)),
    });
  return (
    <section className="card section-card" data-testid="tax-version-form">
      <h2 className="h3">New version {number}</h2>
      <p className="muted-cell">
        A piece priced P, tax included and after discount, takes the lower rate when P ÷ (1 + the
        lower rate) is at or under the price line, otherwise the higher rate. A rate-schedule rule
        (accessories: belts, bags, wallets, perfumes and the like) charges one rate for its HSN
        whatever the price.
      </p>
      <div className="toolbar">
        <label className="field">
          <span>Applies from</span>
          <input
            className="input"
            type="date"
            data-testid="tax-applies-from"
            min={today}
            value={draft.applies_from}
            onChange={(event) => onChange({ ...draft, applies_from: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Rate when no rule covers an HSN (%)</span>
          <input
            className="input"
            data-testid="tax-unmatched-rate"
            value={draft.unmatched_rate}
            onChange={(event) => onChange({ ...draft, unmatched_rate: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Invoice total</span>
          <select
            className="select"
            data-testid="tax-round-total"
            value={String(draft.round_total_paise)}
            onChange={(event) =>
              onChange({ ...draft, round_total_paise: Number(event.target.value) })
            }
          >
            <option value="100">Round to the nearest rupee (round-off line)</option>
            <option value="1">Do not round</option>
          </select>
        </label>
        <label className="field">
          <span>Returns across GSTINs</span>
          <select
            className="select"
            data-testid="tax-cross-gstin"
            value={draft.cross_gstin_returns ? "yes" : "no"}
            onChange={(event) =>
              onChange({ ...draft, cross_gstin_returns: event.target.value === "yes" })
            }
          >
            <option value="no">Refused - only within the issuing GSTIN</option>
            <option value="yes">Allowed</option>
          </select>
        </label>
        <label className="field">
          <span>Gift with purchase</span>
          <select
            className="select"
            data-testid="tax-gift-with-purchase"
            value={draft.gift_with_purchase_is_gift ? "gift" : "sale"}
            onChange={(event) =>
              onChange({ ...draft, gift_with_purchase_is_gift: event.target.value === "gift" })
            }
          >
            <option value="gift">Gift stock - reverse its input tax credit</option>
            <option value="sale">Part of the sale</option>
          </select>
        </label>
      </div>
      <div className="table-wrap">
        <p className="muted-cell">
          Annual return filing dates. A credit note for a bill of that year and GSTIN reduces tax
          only up to this day, or 30 November after the year if that is earlier. After it, a return
          gets its value back with no tax reduction, and is flagged.
        </p>
        <table className="data" data-testid="tax-filed-form">
          <thead>
            <tr>
              <th>GSTIN</th>
              <th>Year (e.g. 25-26)</th>
              <th>Filed on</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {draft.filed.map((row, index) => (
              <tr key={index} data-testid={`tax-filed-${index}`}>
                <td>
                  <input
                    className="input"
                    aria-label="GSTIN"
                    data-testid={`tax-filed-${index}-gstin`}
                    value={row.gstin}
                    onChange={(event) =>
                      onChange({
                        ...draft,
                        filed: draft.filed.map((r, i) =>
                          i === index ? { ...r, gstin: event.target.value } : r,
                        ),
                      })
                    }
                  />
                </td>
                <td>
                  <input
                    className="input"
                    aria-label="Year"
                    data-testid={`tax-filed-${index}-fy`}
                    value={row.fy}
                    onChange={(event) =>
                      onChange({
                        ...draft,
                        filed: draft.filed.map((r, i) =>
                          i === index ? { ...r, fy: event.target.value } : r,
                        ),
                      })
                    }
                  />
                </td>
                <td>
                  <input
                    className="input"
                    type="date"
                    aria-label="Filed on"
                    data-testid={`tax-filed-${index}-day`}
                    max={today}
                    value={row.day}
                    onChange={(event) =>
                      onChange({
                        ...draft,
                        filed: draft.filed.map((r, i) =>
                          i === index ? { ...r, day: event.target.value } : r,
                        ),
                      })
                    }
                  />
                </td>
                <td>
                  <button
                    type="button"
                    className="btn"
                    data-testid={`tax-filed-${index}-remove`}
                    onClick={() =>
                      onChange({ ...draft, filed: draft.filed.filter((_, i) => i !== index) })
                    }
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button
          type="button"
          className="btn"
          data-testid="tax-filed-add"
          onClick={() =>
            onChange({ ...draft, filed: [...draft.filed, { gstin: "", fy: "", day: "" }] })
          }
        >
          Add a filing date
        </button>
      </div>
      <div className="table-wrap">
        <table className="data" data-testid="tax-rules-form">
          <thead>
            <tr>
              <th>Kind</th>
              <th>HSN starts with</th>
              <th>Name</th>
              <th>Price line (₹, before tax)</th>
              <th>At or under (%)</th>
              <th>Over (%)</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {draft.rules.map((rule, index) => (
              <tr key={index} data-testid={`tax-rule-${index}`}>
                <td>
                  <select
                    className="select"
                    aria-label="Kind"
                    data-testid={`tax-rule-${index}-kind`}
                    value={rule.kind}
                    onChange={(event) =>
                      setRule(index, {
                        kind: event.target.value as DraftRule["kind"],
                      })
                    }
                  >
                    <option value="price_line">Price line</option>
                    <option value="flat_rate">Rate schedule (one rate)</option>
                  </select>
                </td>
                <td>
                  <input
                    className="input"
                    aria-label="HSN starts with"
                    placeholder="Any HSN"
                    data-testid={`tax-rule-${index}-hsn`}
                    value={rule.hsn_prefix}
                    onChange={(event) => setRule(index, { hsn_prefix: event.target.value })}
                  />
                </td>
                <td>
                  <input
                    className="input"
                    aria-label="Name"
                    data-testid={`tax-rule-${index}-name`}
                    value={rule.name}
                    onChange={(event) => setRule(index, { name: event.target.value })}
                  />
                </td>
                <td>
                  <input
                    className="input"
                    aria-label="Price line"
                    data-testid={`tax-rule-${index}-line`}
                    disabled={rule.kind === "flat_rate"}
                    placeholder={rule.kind === "flat_rate" ? "Any price" : ""}
                    value={rule.kind === "flat_rate" ? "" : rule.line_rupees}
                    onChange={(event) => setRule(index, { line_rupees: event.target.value })}
                  />
                </td>
                <td>
                  <input
                    className="input"
                    aria-label={rule.kind === "flat_rate" ? "Rate" : "Rate at or under"}
                    data-testid={`tax-rule-${index}-below`}
                    value={rule.rate_below}
                    onChange={(event) => setRule(index, { rate_below: event.target.value })}
                  />
                </td>
                <td>
                  <input
                    className="input"
                    aria-label="Rate over"
                    data-testid={`tax-rule-${index}-above`}
                    disabled={rule.kind === "flat_rate"}
                    value={rule.kind === "flat_rate" ? "" : rule.rate_above}
                    onChange={(event) => setRule(index, { rate_above: event.target.value })}
                  />
                </td>
                <td>
                  <button
                    type="button"
                    className="btn"
                    disabled={draft.rules.length === 1}
                    onClick={() =>
                      onChange({
                        ...draft,
                        rules: draft.rules.filter((_, i) => i !== index),
                      })
                    }
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p>
        <button
          type="button"
          className="btn"
          data-testid="tax-add-rule"
          onClick={() =>
            onChange({
              ...draft,
              rules: [
                ...draft.rules,
                {
                  kind: "price_line",
                  hsn_prefix: "",
                  name: "",
                  line_rupees: "",
                  rate_below: "",
                  rate_above: "",
                },
              ],
            })
          }
        >
          Add a rule
        </button>
      </p>
      <label className="field">
        <span>Why this version</span>
        <input
          className="input"
          data-testid="tax-note"
          value={draft.note}
          onChange={(event) => onChange({ ...draft, note: event.target.value })}
        />
      </label>
      <p>
        <button
          type="button"
          className="btn btn-primary"
          data-testid="tax-save"
          disabled={busy}
          onClick={onSave}
        >
          {busy ? "Saving…" : `Save version ${number}`}
        </button>{" "}
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </p>
    </section>
  );
}

/** A new version starts as a copy of the newest one, so Admin changes only what
 *  is changing. From version 1, the slab table's rows (the newest one for each
 *  HSN prefix), with the highest rate they hold for an HSN no rule covers. */
function startFrom(data: Payload): Draft {
  const newest = data.versions[0];
  if (!newest) throw new Error("Cannot edit tax settings without an existing version");
  let rules: Rule[] = newest.rules;
  let unmatched = newest.unmatched_rate ?? "";
  if (newest.legacy) {
    const byPrefix = new Map<string, Rule>();
    for (const rule of newest.rules) {
      if ((rule.effective_from ?? "") > data.today) continue;
      const held = byPrefix.get(rule.hsn_prefix);
      if (!held || (rule.effective_from ?? "") >= (held.effective_from ?? "")) {
        byPrefix.set(rule.hsn_prefix, rule);
      }
    }
    rules = [...byPrefix.values()];
    unmatched = rules.reduce(
      (high, rule) =>
        rateHundredths(rule.rate_above) > rateHundredths(high) ? rule.rate_above : high,
      rules[0]?.rate_above ?? "",
    );
  }
  return {
    applies_from: data.today,
    rules: rules.map((rule) =>
      rule.kind === "flat_rate"
        ? {
            kind: "flat_rate" as const,
            hsn_prefix: rule.hsn_prefix,
            name: rule.name,
            line_rupees: "",
            rate_below: rule.rate ?? rule.rate_above,
            rate_above: "",
          }
        : {
            kind: "price_line" as const,
            hsn_prefix: rule.hsn_prefix,
            name: rule.name,
            line_rupees: paiseToRupees(rule.threshold_paise),
            rate_below: rule.rate_below,
            rate_above: rule.rate_above,
          },
    ),
    unmatched_rate: unmatched,
    round_total_paise: newest.options.round_total_paise,
    cross_gstin_returns: newest.options.cross_gstin_returns ?? false,
    gift_with_purchase_is_gift: newest.options.gift_with_purchase_is_gift ?? true,
    filed: filedRows(newest.options.annual_return_filed),
    note: "",
  };
}
