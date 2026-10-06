import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { rupeesToPaiseString, sha256Hex } from "../lib/goodsPt";
import { formatPaiseString, paiseStringToRupees } from "../lib/format";
import { Feedback, Field, hold, useStepUp, type Page, type ResourceDTO } from "../lib/goodsScreen";
import { SohReconciliationPanel } from "./SohReconciliation";

type Mapping = Record<string, string>;
interface Configuration {
  brand_mappings: Mapping;
  season_mappings: Mapping;
  size_mappings: Mapping;
  hsn_mappings: Mapping;
  row_overrides: Record<
    string,
    { exclude_reason?: string; basic_paise?: string; season_unknown_historical?: boolean }
  >;
  unknown_season_sources: string[];
  identity_profile_id: string;
  profile_version_id: string;
  cutoff_at: string;
  rate_meaning: string;
  source_store_confirmed: boolean;
  fresh_source_confirmed: boolean;
  valuation_evidence_id: string;
  reconciliation_evidence_id: string;
  external_reconciliation_note: string;
  quality_note: string;
}
interface Source {
  id: string;
  site_id: string;
  source_name: string;
  source_hash: string;
  revision: number;
  content_hash: string;
  state: string;
  allowed_actions: string[];
  data: {
    metadata: {
      row_count: number;
      stocked_rows: number;
      quantity: number;
      zero_stock_rows: number;
      sheet: string;
      source_amount_paise?: string;
      quantity_rate_paise?: string;
      source_rate_difference_paise?: string;
    };
    configuration: Partial<Configuration>;
    included_rows: number;
    included_quantity: number;
    verified_rows: number;
    batch_count: number;
    excluded_stocked_rows: number;
    approval_request_id: string | null;
    requires_inventory_reconciliation: boolean;
    stock_reconciliation_id: string | null;
    field_access: { readable_fields: string[]; writable_fields: string[] };
    batches: { index: number; manifest_id: string; quantity: number; manifest_state: string }[];
  };
}
interface SourceRow {
  barcode: string;
  brand: string;
  item_name: string;
  quantity: number;
  mrp_paise: string | null;
  source_rate_paise?: string;
  season: string;
  size: string;
  category: string;
  exclusion_reason: string;
  verification: { observed_qty?: number; observed_condition?: string };
}
interface Group {
  source_value?: string;
  source_brand?: string;
  quantity: number;
  row_count: number;
}
interface Choice {
  id: string;
  label: string;
  historical_unknown?: boolean;
}
interface ConfigData {
  versions: { id: string; state: string }[];
  payload?: { family?: string };
  kind?: string;
}

function blankConfiguration(): Configuration {
  return {
    brand_mappings: {},
    season_mappings: {},
    size_mappings: {},
    hsn_mappings: {},
    row_overrides: {},
    unknown_season_sources: [],
    identity_profile_id: "",
    profile_version_id: "",
    cutoff_at: "",
    rate_meaning: "unconfirmed",
    source_store_confirmed: false,
    fresh_source_confirmed: false,
    valuation_evidence_id: "",
    reconciliation_evidence_id: "",
    external_reconciliation_note: "",
    quality_note: "",
  };
}

export function readPhysicalCsv(
  text: string,
): { barcode: string; observed_qty: number; observed_condition: string; reason: string }[] {
  const matrix: string[][] = [];
  let row: string[] = [],
    value = "",
    quoted = false;
  const clean = text.replace(/^\uFEFF/, "");
  for (let i = 0; i <= clean.length; i++) {
    const char = clean[i] ?? "\n";
    if (char === '"') {
      if (quoted && clean[i + 1] === '"') {
        value += '"';
        i++;
      } else quoted = !quoted;
    } else if (!quoted && (char === "," || char === "\n")) {
      row.push(value.trim());
      value = "";
      if (char === "\n") {
        if (row.some(Boolean)) matrix.push(row);
        row = [];
      }
    } else if (char !== "\r") value += char;
  }
  if (quoted) throw new Error("The physical-count CSV has an unclosed quote.");
  const header = matrix.shift()?.map((cell) => cell.toLowerCase());
  const required = ["barcode", "observed_qty", "observed_condition", "reason"];
  if (!header || required.some((name) => !header.includes(name)))
    throw new Error("CSV columns: barcode, observed_qty, observed_condition, reason.");
  return matrix.map((cells) => {
    const get = (name: string) => cells[header.indexOf(name)] ?? "";
    const qty = Number(get("observed_qty"));
    if (
      !get("barcode") ||
      !get("observed_qty") ||
      !Number.isSafeInteger(qty) ||
      qty < 0 ||
      !["good", "damaged", "wrong", "unidentified"].includes(get("observed_condition")) ||
      !get("reason")
    )
      throw new Error(
        "Each count needs a barcode, nonnegative whole quantity, condition and evidence/reason.",
      );
    return {
      barcode: get("barcode"),
      observed_qty: qty,
      observed_condition: get("observed_condition"),
      reason: get("reason"),
    };
  });
}

async function allPages<T>(path: string): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;
  do {
    const response: { data: Page<T> } = await api.get(
      `${path}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    );
    items.push(...response.data.items);
    cursor = response.data.next_cursor;
  } while (cursor);
  return items;
}

export function SohImportPanel({ siteId }: { siteId: string }) {
  const { session } = useAuth();
  const stage = hold(session, "opening.import.stage");
  const prepare = hold(session, "pt.prepare.opening");
  const approve = hold(session, "opening.manifest.approve");
  const [sources, setSources] = useState<Source[]>([]);
  const [selected, setSelected] = useState<Source | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [counts, setCounts] = useState<File | null>(null);
  const [valuationFile, setValuationFile] = useState<File | null>(null);
  const [reconciliationFile, setReconciliationFile] = useState<File | null>(null);
  const [withdrawReason, setWithdrawReason] = useState("");
  const [rows, setRows] = useState<SourceRow[]>([]);
  const [rowCursor, setRowCursor] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [includeCatalogue, setIncludeCatalogue] = useState(false);
  const [basicValues, setBasicValues] = useState<Record<string, string>>({});
  const [config, setConfig] = useState<Configuration>(blankConfiguration);
  const [groups, setGroups] = useState<Record<string, Group[]>>({});
  const [choices, setChoices] = useState<Record<string, Choice[]>>({});
  const [tab, setTab] = useState("source");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();
  const base = "/goods-v1/ptmapper/soh-imports";
  const canPrepare = selected?.data.field_access.writable_fields.includes("cost") ?? false;
  const canVerify = selected?.allowed_actions.includes("verify") ?? false;
  const canReadFinancial =
    selected?.data.field_access.readable_fields.includes("financial") ?? false;

  async function refresh(sourceId?: string) {
    const list = await api.get<Page<Source>>(`${base}?site_id=${siteId}`);
    setSources(list.data.items);
    if (sourceId) {
      const detail = await api.get<Source>(`${base}/${sourceId}`);
      setSelected(detail.data);
      return detail.data;
    }
    return null;
  }

  useEffect(() => {
    let active = true;
    setSelected(null);
    setSources([]);
    setRows([]);
    setGroups({});
    setChoices({});
    setConfig(blankConfiguration());
    api
      .get<Page<Source>>(`${base}?site_id=${siteId}`)
      .then((r) => {
        if (active) setSources(r.data.items);
      })
      .catch((e: unknown) => {
        if (active) setError(apiErrorMessage(e));
      });
    return () => {
      active = false;
    };
  }, [siteId]);

  useEffect(() => {
    if (!selected) return;
    let active = true;
    const id = selected.id;
    api
      .get<{ items: SourceRow[]; next_cursor: string | null }>(
        `${base}/${id}/rows?stocked=${includeCatalogue ? "0" : "1"}&q=${encodeURIComponent(search)}`,
      )
      .then((r) => {
        if (active) {
          setRows(r.data.items);
          setRowCursor(r.data.next_cursor);
        }
      })
      .catch((e: unknown) => {
        if (active) setError(apiErrorMessage(e));
      });
    return () => {
      active = false;
    };
  }, [selected, search, includeCatalogue]);

  async function open(source: Source) {
    setSelected(source);
    setConfig({ ...blankConfiguration(), ...source.data.configuration });
    setError("");
    setBasicValues(
      Object.fromEntries(
        Object.entries(source.data.configuration.row_overrides ?? {})
          .filter(([, value]) => value.basic_paise !== undefined)
          .map(([barcode, value]) => [barcode, paiseStringToRupees(value.basic_paise)]),
      ),
    );
    if (prepare || approve) {
      try {
        const found: Record<string, Group[]> = {};
        for (const kind of ["brand", "season", "size", "category"])
          found[kind] = (
            await api.get<{ items: Group[] }>(`${base}/${source.id}/rows?stocked=0&group=${kind}`)
          ).data.items;
        setGroups(found);
        const brandRows = await allPages<ResourceDTO<{ name: string; code: string }>>(
          "/goods-v1/masters/brands?limit=100",
        );
        const seasonRows = await allPages<
          ResourceDTO<{ name: string; historical_unknown: boolean }>
        >("/goods-v1/masters/seasons?limit=100");
        const configRows = await allPages<ResourceDTO<ConfigData>>(
          "/goods-v1/masters/configurations?limit=100",
        );
        const vocabulary = await api.get<{ items: (Choice & { dimension: string })[] }>(
          `${base}/${source.id}/rows?group=vocabulary`,
        );
        setChoices({
          brands: brandRows.map((r) => ({ id: r.id, label: `${r.data.name} (${r.data.code})` })),
          seasons: seasonRows.map((r) => ({
            id: r.id,
            label: r.data.name,
            historical_unknown: r.data.historical_unknown,
          })),
          identity: configRows
            .filter((r) => r.data.kind === "identity_profile")
            .flatMap((r) =>
              r.data.versions
                .filter((v) => v.state === "effective")
                .map((v) => ({ id: v.id, label: r.data.payload?.family ?? v.id })),
            ),
          profiles: configRows
            .filter((r) => r.data.kind === "profile")
            .flatMap((r) =>
              r.data.versions
                .filter((v) => v.state === "effective")
                .map((v) => ({ id: v.id, label: r.data.payload?.family ?? v.id })),
            ),
          sizes: vocabulary.data.items.filter((value) => value.dimension === "size"),
        });
      } catch (e) {
        setError(apiErrorMessage(e));
      }
    }
  }

  async function run(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    setOk("");
    try {
      await work();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function mutate(operation: string, body: Record<string, unknown> = {}) {
    if (!selected) return;
    const result = await api.post<Source>(`${base}/${selected.id}/${operation}`, {
      ...body,
      ...goodsMeta(selected.revision),
    });
    setSelected(result.data);
    setOk("Saved against the current source revision.");
    return result.data;
  }

  function pick(
    field: "brand_mappings" | "season_mappings" | "size_mappings" | "hsn_mappings",
    key: string,
    value: string,
  ) {
    setConfig((old) => ({ ...old, [field]: { ...old[field], [key]: value } }));
  }

  function reviewedConfiguration() {
    const rowOverrides = { ...config.row_overrides };
    for (const [barcode, text] of Object.entries(basicValues)) {
      const paise = rupeesToPaiseString(text);
      if (paise === undefined || paise === null || BigInt(paise) <= 0n)
        throw new Error(
          `Barcode ${barcode}: record a positive basic value in INR with at most two decimal places.`,
        );
      rowOverrides[barcode] = { ...rowOverrides[barcode], basic_paise: paise };
    }
    const reviewed: Partial<Configuration> = { ...config, row_overrides: rowOverrides };
    // Hidden saved financial declarations stay pinned on the server. Blank
    // projected inputs must not erase them while editing a mapping or profile.
    if (!canReadFinancial) {
      if (!reviewed.quality_note?.trim()) delete reviewed.quality_note;
      if (!reviewed.external_reconciliation_note?.trim())
        delete reviewed.external_reconciliation_note;
    }
    return reviewed;
  }

  async function uploadSupporting(
    file: File,
    field: "valuation_evidence_id" | "reconciliation_evidence_id",
  ) {
    const form = new FormData();
    form.append("file", file);
    form.append("command_id", crypto.randomUUID());
    form.append("contract_version", "goods-v1");
    form.append("expected_sha256", await sha256Hex(file));
    form.append("kind", "other");
    form.append(
      "scope",
      JSON.stringify({
        scope_kind: "sites",
        site_ids: [siteId],
        brand_ids: [],
        sensitive_fields: ["cost", "financial"],
      }),
    );
    const uploaded = await api.post<ResourceDTO<Record<string, unknown>>>(
      "/goods-v1/files/uploads",
      form,
    );
    setConfig((old) => ({ ...old, [field]: uploaded.data.id }));
    setOk("Protected supporting evidence uploaded and linked to this store.");
  }

  function mappingTable(
    kind: string,
    field: "brand_mappings" | "season_mappings" | "size_mappings" | "hsn_mappings",
    options?: Choice[],
  ) {
    return (
      <div
        role="region"
        aria-label={`SOH ${kind} mappings`}
        tabIndex={0}
        style={{ maxHeight: 360, overflow: "auto" }}
      >
        <table className="table">
          <thead>
            <tr>
              <th>Source {kind}</th>
              <th>Rows / quantity</th>
              <th>Reviewed target</th>
            </tr>
          </thead>
          <tbody>
            {(groups[kind] ?? []).map((group) => {
              const key = group.source_brand ?? group.source_value ?? "";
              return (
                <tr key={key}>
                  <td>{key || "Blank in source"}</td>
                  <td>
                    {group.row_count} / {group.quantity}
                  </td>
                  <td>
                    {options ? (
                      <select
                        className="input"
                        value={config[field][key] ?? ""}
                        onChange={(e) => pick(field, key, e.target.value)}
                      >
                        <option value="">Choose explicitly</option>
                        {options.map((option) => (
                          <option key={option.id} value={option.id}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        className="input"
                        value={config[field][key] ?? ""}
                        onChange={(e) => pick(field, key, e.target.value)}
                        placeholder={
                          field === "hsn_mappings"
                            ? "Confirmed HSN"
                            : "Approved vocabulary value ID"
                        }
                      />
                    )}
                    {field === "season_mappings" &&
                      choices.seasons?.find((s) => s.id === config[field][key])
                        ?.historical_unknown && (
                        <label>
                          <input
                            type="checkbox"
                            checked={config.unknown_season_sources.includes(key)}
                            onChange={(e) =>
                              setConfig((old) => ({
                                ...old,
                                unknown_season_sources: e.target.checked
                                  ? [...old.unknown_season_sources, key]
                                  : old.unknown_season_sources.filter((v) => v !== key),
                              }))
                            }
                          />{" "}
                          I reviewed that no real season can be established for these rows.
                        </label>
                      )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  if (!stage && !prepare && !approve) return null;
  return (
    <section className="card section-card" data-testid="soh-import">
      <h3 className="h3">Switch an existing store to KDPS</h3>
      <p className="lead">
        Upload current stock on hand, review its identities and values, record physical
        verification, then approve and accept the opening stock. Uploading alone creates no
        inventory.
      </p>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {!selected ? (
        <>
          {stage && (
            <div className="form-grid">
              <Field id="soh-file" label="SOH source (.xlsx)">
                <input
                  id="soh-file"
                  type="file"
                  accept=".xlsx"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                />
              </Field>
              <button
                className="btn btn-cta"
                disabled={busy || !file}
                onClick={() =>
                  run(async () => {
                    if (!file) return;
                    const form = new FormData();
                    form.append("file", file);
                    form.append("site_id", siteId);
                    form.append("command_id", crypto.randomUUID());
                    form.append("contract_version", "goods-v1");
                    form.append("expected_sha256", await sha256Hex(file));
                    const result = await api.post<Source>(`${base}/upload`, form);
                    await refresh(result.data.id);
                    await open(result.data);
                  })
                }
              >
                Upload and inspect source
              </button>
            </div>
          )}
          <div className="table-wrap" role="region" aria-label="SOH sources" tabIndex={0}>
            <table className="table">
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Rows</th>
                  <th>Quantity</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {sources.map((source) => (
                  <tr key={source.id}>
                    <td>
                      <button className="btn btn-sm" onClick={() => open(source)}>
                        {source.source_name}
                      </button>
                    </td>
                    <td>{source.data.metadata.row_count}</td>
                    <td>{source.data.metadata.quantity}</td>
                    <td>{source.state}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <>
          <div className="toolbar">
            <button
              className="btn btn-sm"
              onClick={() => {
                setSelected(null);
                void refresh();
              }}
            >
              Sources
            </button>
            <b>{selected.source_name}</b>
            <span className="chip">
              {selected.state} · revision {selected.revision}
            </span>
            <button
              className="btn btn-sm"
              onClick={() =>
                run(async () => {
                  await refresh(selected.id);
                })
              }
            >
              Refresh
            </button>
          </div>
          <p>
            {selected.data.metadata.row_count.toLocaleString()} source rows ·{" "}
            {selected.data.metadata.quantity.toLocaleString()} units ·{" "}
            {selected.data.metadata.zero_stock_rows.toLocaleString()} zero-stock catalogue rows.{" "}
            {selected.data.excluded_stocked_rows} stocked rows excluded with reasons.{" "}
            {selected.data.verified_rows}/{selected.data.included_rows} included rows physically
            verified.
          </p>
          <p className="muted-cell">Source SHA-256: {selected.source_hash}</p>
          {selected.allowed_actions.includes("claim") && (
            <button
              className="btn btn-cta"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await mutate("claim");
                })
              }
            >
              Start valuation preparation
            </button>
          )}
          <div className="toolbar">
            {(selected.data.field_access.readable_fields.includes("cost")
              ? ["source", "mapping", "controls", "physical", "opening"]
              : ["source", "physical", "opening"]
            ).map((name) => (
              <button
                className={`btn btn-sm ${tab === name ? "btn-cta" : ""}`}
                key={name}
                onClick={() => setTab(name)}
              >
                {name}
              </button>
            ))}
          </div>
          {tab === "source" && (
            <>
              <label>
                <input
                  type="checkbox"
                  checked={includeCatalogue}
                  onChange={(e) => setIncludeCatalogue(e.target.checked)}
                />{" "}
                Include zero-stock catalogue and negative rows retained for review
              </label>
              <input
                className="input"
                placeholder="Search barcode or source brand"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <div className="table-wrap" role="region" aria-label="SOH source rows" tabIndex={0}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>Barcode / source item</th>
                      <th>Brand / season</th>
                      <th>SOH</th>
                      <th>MRP</th>
                      {selected.data.field_access.readable_fields.includes("cost") && (
                        <th>Source Rate (meaning requires review)</th>
                      )}
                      <th>Physical / exclusion</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.barcode}>
                        <td>
                          {row.barcode}
                          <div>{row.item_name}</div>
                        </td>
                        <td>
                          {row.brand}
                          <div>{row.season || "Season unavailable"}</div>
                        </td>
                        <td>{row.quantity}</td>
                        <td>{formatPaiseString(row.mrp_paise)}</td>
                        {selected.data.field_access.readable_fields.includes("cost") && (
                          <td>
                            {formatPaiseString(row.source_rate_paise)}
                            {canPrepare &&
                              config.rate_meaning === "reviewed_row_values" &&
                              row.quantity > 0 &&
                              !selected.data.batches.length && (
                                <label>
                                  Reviewed basic unit value (INR)
                                  <input
                                    className="input"
                                    aria-label={`Reviewed basic unit value for ${row.barcode}`}
                                    inputMode="decimal"
                                    value={basicValues[row.barcode] ?? ""}
                                    onChange={(e) =>
                                      setBasicValues((old) => ({
                                        ...old,
                                        [row.barcode]: e.target.value,
                                      }))
                                    }
                                  />
                                </label>
                              )}
                          </td>
                        )}
                        <td>
                          {row.verification.observed_qty ?? "Unverified"}{" "}
                          {row.verification.observed_condition}
                          <div>{row.exclusion_reason}</div>
                          {canPrepare && row.quantity > 0 && !selected.data.batches.length && (
                            <input
                              className="input"
                              placeholder="Reviewed exclusion reason, if needed"
                              value={config.row_overrides[row.barcode]?.exclude_reason ?? ""}
                              onChange={(e) =>
                                setConfig((old) => ({
                                  ...old,
                                  row_overrides: {
                                    ...old.row_overrides,
                                    [row.barcode]: {
                                      ...old.row_overrides[row.barcode],
                                      exclude_reason: e.target.value,
                                    },
                                  },
                                }))
                              }
                            />
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {rowCursor && (
                <button
                  className="btn btn-sm"
                  onClick={() =>
                    run(async () => {
                      const r = await api.get<{ items: SourceRow[]; next_cursor: string | null }>(
                        `${base}/${selected.id}/rows?stocked=${includeCatalogue ? "0" : "1"}&q=${encodeURIComponent(search)}&cursor=${encodeURIComponent(rowCursor)}`,
                      );
                      setRows(r.data.items);
                      setRowCursor(r.data.next_cursor);
                    })
                  }
                >
                  Next rows
                </button>
              )}
            </>
          )}
          {tab === "mapping" && (
            <>
              <p>
                Display labels are source evidence. Choose stable tenant records explicitly; no
                label match establishes authority. Create missing governed masters in{" "}
                <Link to="/setup/products-parties">Products & parties</Link>.
              </p>
              {mappingTable("brand", "brand_mappings", choices.brands)}
              {mappingTable("season", "season_mappings", choices.seasons)}
              {mappingTable("category", "hsn_mappings")}
              {mappingTable("size", "size_mappings", choices.sizes)}
            </>
          )}
          {tab === "controls" && (
            <div className="form-grid">
              <Field id="soh-cutoff" label="Actual source cutoff (date, time and timezone)">
                <input
                  id="soh-cutoff"
                  className="input"
                  placeholder="2026-09-30T08:00:00+05:30"
                  value={config.cutoff_at}
                  onChange={(e) => setConfig({ ...config, cutoff_at: e.target.value })}
                />
              </Field>
              <Field id="soh-identity-profile" label="Approved identity profile">
                <select
                  id="soh-identity-profile"
                  className="input"
                  value={config.identity_profile_id}
                  onChange={(e) => setConfig({ ...config, identity_profile_id: e.target.value })}
                >
                  <option value="">Choose profile</option>
                  {choices.identity?.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field id="soh-value-profile" label="Approved opening pricing profile">
                <select
                  id="soh-value-profile"
                  className="input"
                  value={config.profile_version_id}
                  onChange={(e) => setConfig({ ...config, profile_version_id: e.target.value })}
                >
                  <option value="">Choose profile</option>
                  {choices.profiles?.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field id="soh-rate" label="Confirmed Rate meaning">
                <select
                  id="soh-rate"
                  className="input"
                  value={config.rate_meaning}
                  onChange={(e) => setConfig({ ...config, rate_meaning: e.target.value })}
                >
                  <option value="unconfirmed">Unconfirmed — activation blocked</option>
                  <option value="basic_ex_tax">Evidenced basic value before input tax</option>
                  <option value="reviewed_row_values">Individually reviewed basic values</option>
                </select>
              </Field>
              <Field id="soh-valuation-evidence" label="Valuation evidence">
                <input
                  id="soh-valuation-evidence"
                  type="file"
                  onChange={(e) => setValuationFile(e.target.files?.[0] ?? null)}
                />
                <button
                  className="btn btn-sm"
                  disabled={busy || !valuationFile || !canPrepare}
                  onClick={() =>
                    run(async () => {
                      if (valuationFile && canPrepare)
                        await uploadSupporting(valuationFile, "valuation_evidence_id");
                    })
                  }
                >
                  Upload valuation evidence
                </button>
                <p className="muted-cell">
                  {config.valuation_evidence_id ||
                    "Required: evidence of Rate meaning or independently reviewed row values."}
                </p>
              </Field>
              <Field id="soh-reconciliation-evidence" label="External reconciliation evidence">
                <input
                  id="soh-reconciliation-evidence"
                  type="file"
                  onChange={(e) => setReconciliationFile(e.target.files?.[0] ?? null)}
                />
                <button
                  className="btn btn-sm"
                  disabled={busy || !reconciliationFile || !canPrepare}
                  onClick={() =>
                    run(async () => {
                      if (reconciliationFile && canPrepare)
                        await uploadSupporting(reconciliationFile, "reconciliation_evidence_id");
                    })
                  }
                >
                  Upload reconciliation evidence
                </button>
                <p className="muted-cell">
                  {config.reconciliation_evidence_id ||
                    "Required: external books and explained stock/value differences."}
                </p>
              </Field>
              {selected.data.metadata.source_rate_difference_paise !== undefined && (
                <p>
                  Source Amount: {formatPaiseString(selected.data.metadata.source_amount_paise)}.
                  Quantity × Rate: {formatPaiseString(selected.data.metadata.quantity_rate_paise)}.{" "}
                  Source Amount minus quantity × Rate:{" "}
                  {formatPaiseString(selected.data.metadata.source_rate_difference_paise)}. Explain
                  the difference in the reviewed reconciliation.
                </p>
              )}
              {!canReadFinancial && (
                <p className="muted-cell">
                  Financial source totals and saved reconciliation declarations require scoped
                  financial review. Blank statements preserve the saved declarations. New statements
                  are author-provided evidence for independent Owner review.
                </p>
              )}
              <Field id="soh-quality" label="Source quality and mapping review">
                <textarea
                  id="soh-quality"
                  className="input"
                  disabled={!canPrepare}
                  placeholder={
                    !canReadFinancial
                      ? "Optional new evidence statement; existing saved statement is restricted."
                      : undefined
                  }
                  value={config.quality_note}
                  onChange={(e) => setConfig({ ...config, quality_note: e.target.value })}
                />
              </Field>
              <Field
                id="soh-reconciliation"
                label="Explained quantity/value differences and external-books reconciliation"
              >
                <textarea
                  id="soh-reconciliation"
                  className="input"
                  disabled={!canPrepare}
                  placeholder={
                    !canReadFinancial
                      ? "Optional new reconciliation declaration; existing saved declaration is restricted."
                      : undefined
                  }
                  value={config.external_reconciliation_note}
                  onChange={(e) =>
                    setConfig({ ...config, external_reconciliation_note: e.target.value })
                  }
                />
              </Field>
              <label>
                <input
                  type="checkbox"
                  checked={config.source_store_confirmed}
                  onChange={(e) =>
                    setConfig({ ...config, source_store_confirmed: e.target.checked })
                  }
                />{" "}
                The source belongs to this store; the filename alone is insufficient.
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={config.fresh_source_confirmed}
                  onChange={(e) =>
                    setConfig({ ...config, fresh_source_confirmed: e.target.checked })
                  }
                />{" "}
                The cutover source is fresh and all subsequent movements are accounted for.
              </label>
            </div>
          )}
          {(tab === "mapping" || tab === "controls") &&
            canPrepare &&
            !selected.data.batches.length && (
              <button
                className="btn btn-cta"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    await mutate("prepare", { configuration: reviewedConfiguration() });
                  })
                }
              >
                Save reviewed mappings and valuation controls
              </button>
            )}
          {tab === "physical" && (
            <>
              <p>
                Upload actual physical observations as CSV:{" "}
                <code>barcode,observed_qty,observed_condition,reason</code>. Conditions: good,
                damaged, wrong, unidentified. Software stock is not a physical count.
              </p>
              <input
                type="file"
                accept=".csv"
                onChange={(e) => setCounts(e.target.files?.[0] ?? null)}
              />
              {canVerify && (
                <button
                  className="btn btn-cta"
                  disabled={busy || !counts || !!selected.data.batches.length}
                  onClick={() =>
                    run(async () => {
                      if (!counts) return;
                      const observations = readPhysicalCsv(await counts.text());
                      let current = selected;
                      for (let start = 0; start < observations.length; start += 5000) {
                        const r = await api.post<Source>(`${base}/${current.id}/verify`, {
                          observations: observations.slice(start, start + 5000),
                          ...goodsMeta(current.revision),
                        });
                        current = r.data;
                        setSelected(current);
                      }
                      setOk(`Recorded ${observations.length} actual physical observations.`);
                    })
                  }
                >
                  Record physical verification
                </button>
              )}
            </>
          )}
          {tab === "opening" && (
            <>
              <SohReconciliationPanel
                key={selected.id}
                source={selected}
                onChanged={() => refresh(selected.id)}
              />
              {selected.allowed_actions.includes("submit") &&
                ["review", "uploaded"].includes(selected.state) && (
                  <button
                    className="btn btn-cta"
                    disabled={busy}
                    onClick={() =>
                      run(async () => {
                        await mutate("submit");
                      })
                    }
                  >
                    Submit exact source for independent Owner review
                  </button>
                )}
              {selected.allowed_actions.includes("approve") &&
                selected.state === "submitted" &&
                selected.data.approval_request_id && (
                  <button
                    className="btn btn-cta"
                    disabled={busy}
                    onClick={() =>
                      run(async () => {
                        await stepUp.guarded(() =>
                          api.post(
                            `/goods-v1/approvals/${selected.data.approval_request_id}/decide`,
                            {
                              decision: "approve",
                              reviewed_hash: selected.content_hash,
                              ...goodsMeta(selected.revision),
                            },
                          ),
                        );
                        await refresh(selected.id);
                      })
                    }
                  >
                    Approve reviewed source
                  </button>
                )}
              {selected.allowed_actions.includes("apply") &&
                ["approved", "applied"].includes(selected.state) &&
                Array.from({ length: selected.data.batch_count }, (_, i) => i + 1)
                  .filter((i) => !selected.data.batches.some((b) => b.index === i))
                  .map((index) => (
                    <button
                      key={index}
                      className="btn btn-cta"
                      disabled={busy}
                      onClick={() =>
                        run(async () => {
                          await mutate("apply", { batch_index: index });
                        })
                      }
                    >
                      Create governed opening batch {index}
                    </button>
                  ))}
              <p>
                Each batch retains its normal manifest approval, opening PT approval and separate
                physical acceptance. Stock becomes sellable after every approved batch reconciles
                and store readiness is activated.
              </p>
              {selected.data.batches.map((batch) => (
                <p key={batch.index}>
                  {selected.data.field_access.readable_fields.includes("cost") ? (
                    <Link to={`/goods/opening?manifest=${batch.manifest_id}`}>
                      Batch {batch.index}: {batch.quantity} source units · manifest{" "}
                      {batch.manifest_state}
                    </Link>
                  ) : (
                    <>
                      Batch {batch.index}: {batch.quantity} source units · manifest{" "}
                      {batch.manifest_state}
                    </>
                  )}
                </p>
              ))}
              <Link to="/goods/receive">Continue physical acceptance</Link> ·{" "}
              <Link to="/setup/organisation">Store readiness</Link>
              {selected.allowed_actions.includes("withdraw") &&
                !selected.data.batches.length &&
                selected.state !== "withdrawn" && (
                  <div>
                    <input
                      className="input"
                      aria-label="Source withdrawal reason"
                      placeholder="Reason this source is superseded or withdrawn"
                      value={withdrawReason}
                      onChange={(e) => setWithdrawReason(e.target.value)}
                    />
                    <button
                      className="btn btn-sm"
                      disabled={busy || !withdrawReason.trim()}
                      onClick={() =>
                        run(async () => {
                          await stepUp.guarded(() =>
                            mutate("withdraw", { reason: withdrawReason }),
                          );
                        })
                      }
                    >
                      Withdraw inactive source
                    </button>
                  </div>
                )}
            </>
          )}
        </>
      )}
    </section>
  );
}
