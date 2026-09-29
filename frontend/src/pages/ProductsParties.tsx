// Products and parties (ticket 04): styles, SKUs, aliases, source crosswalks,
// vendors, brands, seasons and sub-brands, plus the barcode lookup that shows
// every candidate and records one person's scoped choice.
//
// One area, a left list with a type switch and a search box, a detail panel on
// the right (orchestrator UX brief). Enforcement is the server's (design §4.2):
// Session display hints only decide which controls this build offers — a person
// without the grant gets the same uniform hidden-object answer from the server
// whether the record is outside their scope or does not exist (ADR-0003).
//
// Two rules this screen exists to keep visible:
//   * an omitted size is *unknown*, an explicit Free Size is a mapped
//     vocabulary value, and neither is a blank that could mean the other;
//   * a code shared by two articles shows both candidates and asks. There is
//     no merge button anywhere on this screen, because merging two identities
//     is not a thing the system can do (design §3.4).
import { useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Boxes, Pencil, Plus, Save, ScanLine, Shapes, Tags, Truck, X } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Denied,
  Feedback,
  Field,
  hold,
  listState,
  useResourceList,
  useStepUp,
  type ResourceDTO,
} from "../lib/goodsScreen";
import {
  pinnedLabel,
  useConfigurations,
  useControlledValues,
  useEffectiveIdentityProfiles,
  type EffectiveProfile,
} from "../lib/goodsConfig";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { BrandReconciliationPanel } from "./BrandReconciliation";
import "./ProductsParties.css";

// --------------------------------------------------------------------------
// Wire shapes (design §5.3 MasterPayload, §6.1 ResourceDTO<T>). Hand-typed:
// these views hand-build dict responses, and the generated client's schema for
// them describes the envelope, not this screen's own field names.
// --------------------------------------------------------------------------

export interface AttrValue {
  field_id: string;
  vocabulary_value_id?: string | null;
  supplied_text?: string | null;
  unknown?: boolean;
}

interface StyleData {
  brand_id: string;
  style_code: string;
  profile_family: string;
  attrs: AttrValue[];
}

interface SkuData {
  style_id: string;
  profile_version_id: string | null;
  attrs: AttrValue[];
}

interface AliasData {
  sku_id: string;
  issuer_key: string;
  alias_type: "barcode" | "vendor_code" | "generated";
  value: string;
  site_id: string | null;
  effective_from: string;
  effective_to: string | null;
}

interface CrosswalkData {
  kind: "vendor" | "brand" | "subbrand";
  issuer_key: string;
  source_key: string;
  target_key: string;
  config_version_id: string | null;
}

interface NamedMaster {
  code: string;
  name: string;
  parent_id?: string | null;
}

interface VendorData {
  code: string;
  name: string;
  gstin?: string | null;
  agent_ref?: string | null;
}

/** A candidate SKU behind one code (design §6.1 IdentityResolutionDTO). */
interface Candidate {
  sku_id: string;
  brand: string;
  style: string;
  size: string;
  colour?: string;
  grade?: string;
}

interface Resolution {
  result: "resolved" | "ambiguous" | "unknown";
  candidate_hash: string;
  candidates: Candidate[];
  chosen_sku_id: string | null;
  issues: { code: string; message: string }[];
}

type MasterType =
  | "brand-reconciliation"
  | "styles"
  | "skus"
  | "aliases"
  | "lookup"
  | "crosswalks"
  | "vendors"
  | "brands"
  | "seasons"
  | "subbrands";

const CROSSWALK_KINDS = ["vendor", "brand", "subbrand"] as const;
const ALIAS_TYPES = ["barcode", "vendor_code", "generated"] as const;

/** The explicit "no value given" option. It is a separate choice from every
 *  mapped value — including a Free Size — and never a blank that could be read
 *  as either (design §3.4). */
const NOT_GIVEN = "";

function nowIso(): string {
  return new Date().toISOString();
}

function stateChip(state: string): ReactNode {
  const tone = state === "effective" ? "green" : state === "pending" ? "amber" : "red";
  return <span className={`chip chip-${tone}`}>{state}</span>;
}

// --------------------------------------------------------------------------
// Attribute controls
// --------------------------------------------------------------------------

/** One governed dimension of the approved identity profile.
 *
 *  Always a choice list, never a free text box: a size typed by hand is refused
 *  by the server (`SIZE_NOT_MAPPED`), and the reason it is refused is exactly
 *  what this control exists to make obvious. "Not given" is its own option and
 *  leaves the attribute out, which is what keeps *unknown* apart from an
 *  explicit Free Size. */
function DimensionSelect({
  idPrefix,
  profileVersionId,
  dimension,
  allowed,
  required,
  value,
  onChange,
}: {
  idPrefix: string;
  profileVersionId: string;
  dimension: string;
  allowed: string[] | null;
  required: boolean;
  value: string;
  onChange: (next: string) => void;
}) {
  const { values, loading, failure } = useControlledValues(profileVersionId, dimension);
  // An empty allowed list is the server's own "no restriction" (it skips the
  // check when the profile names no values), not "nothing may be chosen".
  const restricted = allowed !== null && allowed.length > 0;
  const usable = values.filter(
    (v) => v.state === "effective" && (!restricted || allowed.includes(v.id)),
  );
  const id = `${idPrefix}-${dimension}`;
  return (
    <Field
      id={id}
      label={`${dimension}${required ? "" : " (optional)"}`}
      hint={
        failure
          ? failure
          : loading
            ? "Loading the approved values…"
            : required
              ? "This value defines the SKU, so it cannot be left out."
              : "“Not given” leaves this unknown. Free Size, where the profile allows it, is a value of its own."
      }
    >
      <select
        id={id}
        className="select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid={id}
      >
        <option value={NOT_GIVEN}>{required ? "— choose —" : "Not given"}</option>
        {usable.map((v) => (
          <option key={v.id} value={v.id}>
            {v.label}
          </option>
        ))}
      </select>
    </Field>
  );
}

interface ProfileDimension {
  name: string;
  allowed: string[] | null;
  required: boolean;
}

/** The dimensions a SKU under this profile carries, and which of them define it. */
function profileDimensions(profile: EffectiveProfile): ProfileDimension[] {
  const payload = profile.payload;
  const names = [
    ...payload.distinguishing_dimensions,
    payload.size_dimension,
    payload.colour_dimension ?? "",
    payload.grade_dimension ?? "",
  ].filter(Boolean);
  const seen = new Set<string>();
  const out: ProfileDimension[] = [];
  for (const name of names) {
    if (seen.has(name)) continue;
    seen.add(name);
    // `null` means the profile names no list for this dimension, so every
    // effective value of it is selectable; a list — even an empty one — is the
    // profile's own choice and is followed exactly.
    const allowed =
      name === payload.size_dimension
        ? payload.allowed_size_values
        : name === payload.colour_dimension
          ? payload.allowed_colour_values
          : name === payload.grade_dimension
            ? payload.allowed_grade_values
            : null;
    out.push({
      name,
      allowed,
      // Size may be omitted and stays unknown; any other distinguishing
      // dimension defines the SKU and the server refuses it unknown.
      required: payload.distinguishing_dimensions.includes(name) && name !== payload.size_dimension,
    });
  }
  return out;
}

function attrsFromValues(values: Record<string, string>): AttrValue[] {
  return Object.entries(values)
    .filter(([, value]) => value !== NOT_GIVEN)
    .map(([field_id, vocabulary_value_id]) => ({ field_id, vocabulary_value_id }));
}

/** How a stored attribute reads back. An entry that is present but `unknown`,
 *  and an entry that is missing altogether, both read "Not given"; neither is
 *  ever shown as an empty cell that could be mistaken for a value. */
function describeAttrs(attrs: AttrValue[], labels: Map<string, string>): string {
  const parts = attrs.map((entry) => {
    if (entry.unknown || (!entry.vocabulary_value_id && !entry.supplied_text)) {
      return `${entry.field_id}: Not given`;
    }
    const value = entry.vocabulary_value_id
      ? (labels.get(entry.vocabulary_value_id) ?? entry.vocabulary_value_id)
      : entry.supplied_text;
    return `${entry.field_id}: ${value}`;
  });
  return parts.length > 0 ? parts.join(" · ") : "No attributes";
}

// --------------------------------------------------------------------------
// A plain code/name master: vendors, brands, seasons, sub-brands (E021-E040)
// --------------------------------------------------------------------------

interface ExtraField {
  key: string;
  label: string;
  optional?: boolean;
}

/** Create/correct follows `vendor.manage`; retirement follows `master.retire`
 *  with a password confirmation — the approved permissions table, not the one
 *  broad vendor permission the old screens used (change PRD §14.5 M4, I4). */
function SimpleMasterPanel<T extends { code: string; name: string }>({
  title,
  noun,
  testid,
  listUrl,
  writeUrl,
  extras,
  search,
}: {
  title: string;
  noun: string;
  testid: string;
  listUrl: string;
  writeUrl: (id?: string) => string;
  extras: ExtraField[];
  search: string;
}) {
  const { session } = useAuth();
  const canEdit = hold(session, "vendor.manage");
  const canRetire = hold(session, "master.retire");
  const { items, loading, denied, failure, reload } = useResourceList<T>(listUrl);
  const blank = useMemo(
    () => ({ code: "", name: "", ...Object.fromEntries(extras.map((f) => [f.key, ""])) }),
    [extras],
  );
  const [form, setForm] = useState<Record<string, string>>(blank);
  const [editing, setEditing] = useState<ResourceDTO<T> | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  const needle = search.trim().toLowerCase();
  const shown = needle
    ? items.filter(
        (row) =>
          row.data.code.toLowerCase().includes(needle) ||
          row.data.name.toLowerCase().includes(needle),
      )
    : items;

  function start(row: ResourceDTO<T> | null) {
    setEditing(row);
    setForm(
      row
        ? {
            code: row.data.code,
            name: row.data.name,
            ...Object.fromEntries(
              extras.map((f) => [
                f.key,
                String((row.data as Record<string, unknown>)[f.key] ?? ""),
              ]),
            ),
          }
        : blank,
    );
    setOpen(true);
    setError("");
    setOk("");
  }

  async function save() {
    setError("");
    setOk("");
    const code = form.code?.trim();
    const name = form.name?.trim();
    if (!code || !name) {
      setError("Enter a code and name before saving.");
      return;
    }
    const payload: Record<string, string> = { code, name };
    for (const extra of extras) {
      const value = form[extra.key];
      if (value) payload[extra.key] = value;
    }
    try {
      await stepUp.guarded(() =>
        editing
          ? api.patch(writeUrl(editing.id), { ...payload, ...goodsMeta(editing.revision) })
          : api.post(writeUrl(), { ...payload, ...goodsMeta() }),
      );
      setOpen(false);
      setOk(editing ? `${noun} saved.` : `${noun} created.`);
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  async function retire(row: ResourceDTO<T>) {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post(`${writeUrl(row.id)}/retire`, {
          reason_code: "NO_LONGER_USED",
          effective_at: nowIso(),
          ...goodsMeta(row.revision),
        }),
      );
      setOk(`${row.data.name} retired.`);
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (denied) return <Denied what={noun.toLowerCase()} />;
  const state = listState(
    { loading, failure, empty: shown.length === 0 },
    `No ${title.toLowerCase()} yet.`,
  );

  return (
    <div data-testid={`${testid}-panel`}>
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">{title}</h3>
        <div className="spacer" />
        {canEdit && (
          <button className="btn btn-cta" onClick={() => start(null)} data-testid={`${testid}-new`}>
            <Plus size={15} /> New {noun.toLowerCase()}
          </button>
        )}
      </div>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {canEdit && open && (
        <div className="card section-card" data-testid={`${testid}-editor`}>
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">
              {editing ? `Edit ${noun.toLowerCase()}` : `Create ${noun.toLowerCase()}`}
            </h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid={`${testid}-close`}
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <Field id={`${testid}-code`} label="Code">
              <input
                id={`${testid}-code`}
                className="input"
                value={form.code}
                onChange={(e) => setForm({ ...form, code: e.target.value })}
                data-testid={`${testid}-code`}
              />
            </Field>
            <Field id={`${testid}-name`} label="Name">
              <input
                id={`${testid}-name`}
                className="input"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                data-testid={`${testid}-name`}
              />
            </Field>
            {extras.map((extra) => (
              <Field
                key={extra.key}
                id={`${testid}-${extra.key}`}
                label={extra.optional ? `${extra.label} (optional)` : extra.label}
              >
                <input
                  id={`${testid}-${extra.key}`}
                  className="input"
                  value={form[extra.key] ?? ""}
                  onChange={(e) => setForm({ ...form, [extra.key]: e.target.value })}
                  data-testid={`${testid}-${extra.key}`}
                />
              </Field>
            ))}
          </div>
          <button
            className="btn btn-cta"
            onClick={save}
            disabled={!form.code || !form.name}
            data-testid={`${testid}-save`}
          >
            <Save size={15} /> Save {noun.toLowerCase()}
          </button>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid={`${testid}-table`}>
          <thead>
            <tr>
              <th>Code</th>
              <th>Name</th>
              {extras.map((extra) => (
                <th key={extra.key}>{extra.label}</th>
              ))}
              <th>Status</th>
              {(canEdit || canRetire) && <th />}
            </tr>
          </thead>
          <tbody>
            {state ? (
              <tr data-testid={`${testid}-state`}>
                <td colSpan={4 + extras.length}>{state}</td>
              </tr>
            ) : (
              shown.map((row) => (
                <tr key={row.id} data-testid={`${testid}-row-${row.data.code}`}>
                  <td>
                    <b className="mono">{row.data.code}</b>
                  </td>
                  <td>{row.data.name}</td>
                  {extras.map((extra) => (
                    <td key={extra.key} className="mono" style={{ fontSize: 12.5 }}>
                      {String((row.data as Record<string, unknown>)[extra.key] ?? "") || "—"}
                    </td>
                  ))}
                  <td>{stateChip(row.state)}</td>
                  {(canEdit || canRetire) && (
                    <td>
                      {canEdit && (
                        <button
                          className="btn btn-sm"
                          onClick={() => start(row)}
                          data-testid={`${testid}-edit-${row.data.code}`}
                        >
                          <Pencil size={13} /> Edit
                        </button>
                      )}
                      {canRetire && row.state !== "retired" && (
                        <button
                          className="btn btn-sm"
                          onClick={() => retire(row)}
                          data-testid={`${testid}-retire-${row.data.code}`}
                        >
                          Retire
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Styles (E041-E045) and their SKUs
// --------------------------------------------------------------------------

function StylesPanel({
  search,
  selected,
  onSelect,
}: {
  search: string;
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const { session } = useAuth();
  const canManage = hold(session, "product.master.manage");
  const canRetire = hold(session, "master.retire");
  const styles = useResourceList<StyleData>("/goods-v1/masters/styles?limit=100");
  const brands = useResourceList<NamedMaster>("/goods-v1/masters/brands");
  const { profiles, loading: profilesLoading } = useEffectiveIdentityProfiles();
  const [form, setForm] = useState({ brand_id: "", style_code: "", profile_family: "" });
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  const families = Array.from(new Set(profiles.map((p) => p.payload.family)));
  const brandName = (id: string) =>
    brands.items.find((b) => b.id === id)?.data.name ?? `Brand ${id}`;
  const needle = search.trim().toLowerCase();
  const shown = needle
    ? styles.items.filter((row) => row.data.style_code.toLowerCase().includes(needle))
    : styles.items;

  async function create() {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post("/goods-v1/masters/styles", {
          brand_id: form.brand_id,
          style_code: form.style_code,
          profile_family: form.profile_family,
          ...goodsMeta(),
        }),
      );
      setOpen(false);
      setOk(`Style ${form.style_code} created.`);
      styles.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  async function retire(row: ResourceDTO<StyleData>) {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/styles/${row.id}/retire`, {
          reason_code: "NO_LONGER_MADE",
          effective_at: nowIso(),
          ...goodsMeta(row.revision),
        }),
      );
      setOk(`Style ${row.data.style_code} retired.`);
      styles.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (styles.denied) return <Denied what="style" />;
  if (selected) {
    const row = styles.items.find((s) => s.id === selected);
    if (row) {
      return (
        <StyleDetail
          style={row}
          brandName={brandName(row.data.brand_id)}
          profiles={profiles}
          onBack={() => onSelect(null)}
        />
      );
    }
  }

  const state = listState(
    { loading: styles.loading, failure: styles.failure, empty: shown.length === 0 },
    "No styles yet.",
  );

  return (
    <div data-testid="styles-panel">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">Styles</h3>
        <div className="spacer" />
        {canManage && (
          <button
            className="btn btn-cta"
            onClick={() => {
              setOpen(true);
              setError("");
              setOk("");
            }}
            data-testid="style-new"
          >
            <Plus size={15} /> New style
          </button>
        )}
      </div>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {canManage && open && (
        <div className="card section-card" data-testid="style-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">Create style</h3>
            <div className="spacer" />
            <button className="btn btn-sm" onClick={() => setOpen(false)} data-testid="style-close">
              <X size={14} /> Close
            </button>
          </div>
          {!profilesLoading && families.length === 0 && (
            <p className="warn-note" data-testid="style-no-profile">
              No SKU identity profile is approved and in force yet, so a style has no family to
              belong to. Set one up under Configuration first.
            </p>
          )}
          <div className="form-grid wide-form">
            <Field id="style-brand" label="Brand">
              <select
                id="style-brand"
                className="select"
                value={form.brand_id}
                onChange={(e) => setForm({ ...form, brand_id: e.target.value })}
                data-testid="style-brand"
              >
                <option value="">— choose —</option>
                {brands.items.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.data.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field id="style-code" label="Style code">
              <input
                id="style-code"
                className="input"
                value={form.style_code}
                onChange={(e) => setForm({ ...form, style_code: e.target.value })}
                data-testid="style-code"
              />
            </Field>
            <Field
              id="style-family"
              label="Product family"
              hint="The family of the approved SKU identity profile its SKUs will use."
            >
              <select
                id="style-family"
                className="select"
                value={form.profile_family}
                onChange={(e) => setForm({ ...form, profile_family: e.target.value })}
                data-testid="style-family"
              >
                <option value="">— choose —</option>
                {families.map((family) => (
                  <option key={family} value={family}>
                    {family}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <button
            className="btn btn-cta"
            onClick={create}
            disabled={!form.brand_id || !form.style_code || !form.profile_family}
            data-testid="style-save"
          >
            <Save size={15} /> Save style
          </button>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="styles-table">
          <thead>
            <tr>
              <th>Style code</th>
              <th>Brand</th>
              <th>Family</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {state ? (
              <tr data-testid="styles-state">
                <td colSpan={5}>{state}</td>
              </tr>
            ) : (
              shown.map((row) => (
                <tr key={row.id} data-testid={`style-row-${row.data.style_code}`}>
                  <td>
                    <b className="mono">{row.data.style_code}</b>
                  </td>
                  <td>{brandName(row.data.brand_id)}</td>
                  <td>{row.data.profile_family}</td>
                  <td>{stateChip(row.state)}</td>
                  <td>
                    <button
                      className="btn btn-sm"
                      onClick={() => onSelect(row.id)}
                      data-testid={`style-open-${row.data.style_code}`}
                    >
                      Open
                    </button>
                    {canRetire && row.state === "effective" && (
                      <button
                        className="btn btn-sm"
                        onClick={() => retire(row)}
                        data-testid={`style-retire-${row.data.style_code}`}
                      >
                        Retire
                      </button>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StyleDetail({
  style,
  brandName,
  profiles,
  onBack,
}: {
  style: ResourceDTO<StyleData>;
  brandName: string;
  profiles: EffectiveProfile[];
  onBack: () => void;
}) {
  const { session } = useAuth();
  const canManage = hold(session, "product.master.manage");
  const skus = useResourceList<SkuData>(`/goods-v1/masters/skus?style_id=${style.id}&limit=100`);
  const configs = useConfigurations("identity_profile");
  const [mode, setMode] = useState<"list" | "one" | "grid">("list");
  const profile = profiles.find((p) => p.payload.family === style.data.profile_family) ?? null;

  const state = listState(
    { loading: skus.loading, failure: skus.failure, empty: skus.items.length === 0 },
    "This style has no SKUs yet.",
  );

  return (
    <div data-testid="style-detail">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <button className="btn btn-sm" onClick={onBack} data-testid="style-back">
          ← All styles
        </button>
        <div className="spacer" />
      </div>
      <h3 className="h3">
        <span className="mono">{style.data.style_code}</span> · {brandName}
      </h3>
      <p className="lead">
        Family {style.data.profile_family}. {stateChip(style.state)}
      </p>
      {profile === null ? (
        <p className="warn-note" data-testid="style-detail-no-profile">
          No approved SKU identity profile is in force for this family, so no SKU can be created
          against it yet.
        </p>
      ) : (
        <p className="muted" data-testid="style-pinned-profile">
          New SKUs pin <b>{pinnedLabel(configs.items, profile.versionId) ?? profile.versionId}</b>{" "}
          of the SKU identity profile. A later version never replaces it on its own.
        </p>
      )}
      {canManage && profile && (
        <div className="toolbar" style={{ margin: "12px 0" }}>
          <button className="btn" onClick={() => setMode("one")} data-testid="sku-add-one">
            <Plus size={15} /> Add one SKU
          </button>
          <button className="btn" onClick={() => setMode("grid")} data-testid="sku-combinations">
            <Boxes size={15} /> Combinations
          </button>
          {mode !== "list" && (
            <button
              className="btn btn-sm"
              onClick={() => setMode("list")}
              data-testid="sku-form-close"
            >
              <X size={14} /> Close
            </button>
          )}
        </div>
      )}
      {mode === "one" && profile && (
        <SingleSkuForm
          styleId={style.id}
          profile={profile}
          onDone={() => {
            setMode("list");
            skus.reload();
          }}
        />
      )}
      {mode === "grid" && profile && (
        <CombinationsGrid styleId={style.id} profile={profile} onChanged={() => skus.reload()} />
      )}
      <h4 className="h4" style={{ marginTop: 16 }}>
        SKUs of this style
      </h4>
      <div className="table-wrap">
        <table className="data" data-testid="style-skus-table">
          <thead>
            <tr>
              <th>Attributes</th>
              <th>Pinned profile</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {state ? (
              <tr data-testid="style-skus-state">
                <td colSpan={3}>{state}</td>
              </tr>
            ) : (
              skus.items.map((row) => (
                <tr key={row.id} data-testid={`style-sku-${row.id}`}>
                  <td>
                    <SkuAttributeCells attrs={row.data.attrs} profile={profile} />
                  </td>
                  <td className="mono" style={{ fontSize: 12.5 }}>
                    {row.data.profile_version_id
                      ? `pinned to ${pinnedLabel(configs.items, row.data.profile_version_id) ?? row.data.profile_version_id}`
                      : "Unknown"}
                  </td>
                  <td>{stateChip(row.state)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** A SKU's stored attributes, read back with their vocabulary labels. */
function SkuAttributeCells({
  attrs,
  profile,
}: {
  attrs: AttrValue[];
  profile: EffectiveProfile | null;
}) {
  const size = useControlledValues(
    profile ? profile.versionId : null,
    profile ? profile.payload.size_dimension : null,
  );
  const colour = useControlledValues(
    profile ? profile.versionId : null,
    profile?.payload.colour_dimension ?? null,
  );
  const labels = new Map<string, string>();
  for (const value of [...size.values, ...colour.values]) labels.set(value.id, value.label);
  return <span>{describeAttrs(attrs, labels)}</span>;
}

function SingleSkuForm({
  styleId,
  profile,
  onDone,
}: {
  styleId: string;
  profile: EffectiveProfile;
  onDone: () => void;
}) {
  const dimensions = profileDimensions(profile);
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const stepUp = useStepUp();

  async function save() {
    setError("");
    try {
      await stepUp.guarded(() =>
        api.post("/goods-v1/masters/skus", {
          style_id: styleId,
          profile_version_id: profile.versionId,
          attrs: attrsFromValues(values),
          ...goodsMeta(),
        }),
      );
      onDone();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  const missing = dimensions.filter((d) => d.required && !values[d.name]);

  return (
    <div className="card section-card" data-testid="sku-single-form">
      <h4 className="h4">Add one SKU</h4>
      <p className="lead">
        These are the attributes the approved identity profile governs for this family.
      </p>
      <Feedback error={error} ok="" />
      {stepUp.dialog}
      <div className="form-grid wide-form">
        {dimensions.map((dimension) => (
          <DimensionSelect
            key={dimension.name}
            idPrefix="sku"
            profileVersionId={profile.versionId}
            dimension={dimension.name}
            allowed={dimension.allowed}
            required={dimension.required}
            value={values[dimension.name] ?? NOT_GIVEN}
            onChange={(next) => setValues({ ...values, [dimension.name]: next })}
          />
        ))}
      </div>
      <button
        className="btn btn-cta"
        onClick={save}
        disabled={missing.length > 0}
        data-testid="sku-single-save"
      >
        <Save size={15} /> Create SKU
      </button>
    </div>
  );
}

// --------------------------------------------------------------------------
// The size × colour combination grid (GSA-T04)
// --------------------------------------------------------------------------

type OutcomeKind = "existed" | "created" | "failed";

interface Outcome {
  key: string;
  label: string;
  kind: OutcomeKind;
  message: string;
}

/** Explicitly chosen combinations, reviewed, then created.
 *
 *  Nothing is inferred: the grid offers every size × colour the profile allows
 *  and the person ticks the ones that exist. Each ticked combination keeps one
 *  command identity for the life of this grid, so pressing "Retry the failures"
 *  replays exactly the commands that failed and can never create a second SKU
 *  for one that already succeeded (design §4.1 replay). */
function CombinationsGrid({
  styleId,
  profile,
  onChanged,
}: {
  styleId: string;
  profile: EffectiveProfile;
  onChanged: () => void;
}) {
  const payload = profile.payload;
  const sizes = useControlledValues(profile.versionId, payload.size_dimension);
  const colours = useControlledValues(profile.versionId, payload.colour_dimension);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [stage, setStage] = useState<"pick" | "review" | "done">("pick");
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const [busy, setBusy] = useState(false);
  const commandIds = useRef(new Map<string, string>());
  const stepUp = useStepUp();

  // As in `DimensionSelect`: an empty allowed list means the profile restricts
  // nothing, which is how the server reads it too.
  const allows = (allowed: string[], id: string) => allowed.length === 0 || allowed.includes(id);
  const sizeValues = sizes.values.filter(
    (v) => v.state === "effective" && allows(payload.allowed_size_values, v.id),
  );
  const colourValues = colours.values.filter(
    (v) => v.state === "effective" && allows(payload.allowed_colour_values, v.id),
  );

  function key(sizeId: string, colourId: string) {
    return `${sizeId}|${colourId}`;
  }

  function label(sizeId: string, colourId: string) {
    const size = sizeValues.find((v) => v.id === sizeId)?.label ?? sizeId;
    const colour = colourValues.find((v) => v.id === colourId)?.label ?? colourId;
    return `${size} · ${colour}`;
  }

  function toggle(sizeId: string, colourId: string) {
    const next = new Set(ticked);
    const id = key(sizeId, colourId);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setTicked(next);
  }

  function commandFor(id: string): string {
    const existing = commandIds.current.get(id);
    if (existing) return existing;
    const fresh = crypto.randomUUID();
    commandIds.current.set(id, fresh);
    return fresh;
  }

  async function createOne(id: string): Promise<Outcome> {
    const [sizeId, colourId] = id.split("|");
    if (!sizeId || !colourId) {
      return {
        key: id,
        label: id,
        kind: "failed",
        message: "Invalid size and colour combination.",
      };
    }
    const attrs: AttrValue[] = [{ field_id: payload.size_dimension, vocabulary_value_id: sizeId }];
    if (payload.colour_dimension) {
      attrs.push({ field_id: payload.colour_dimension, vocabulary_value_id: colourId });
    }
    try {
      await api.post("/goods-v1/masters/skus", {
        style_id: styleId,
        profile_version_id: profile.versionId,
        attrs,
        ...goodsMeta(undefined, commandFor(id)),
      });
      return { key: id, label: label(sizeId, colourId), kind: "created", message: "Created." };
    } catch (e) {
      if (apiErrorCode(e) === "MASTER_CONFLICT") {
        return {
          key: id,
          label: label(sizeId, colourId),
          kind: "existed",
          message: "This combination already exists.",
        };
      }
      return {
        key: id,
        label: label(sizeId, colourId),
        kind: "failed",
        message: apiErrorMessage(e),
      };
    }
  }

  async function run(ids: string[]) {
    setBusy(true);
    try {
      const results: Outcome[] = [];
      for (const id of ids) {
        // One at a time on purpose: each is its own command, and a failure must
        // not decide anything about the next one.
        results.push(await stepUp.guarded(() => createOne(id)));
      }
      const byKey = new Map(outcomes.map((o) => [o.key, o]));
      for (const result of results) byKey.set(result.key, result);
      setOutcomes(Array.from(byKey.values()));
      setStage("done");
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  const chosen = Array.from(ticked);
  const failed = outcomes.filter((o) => o.kind === "failed");
  const groups: { kind: OutcomeKind; title: string; testid: string }[] = [
    { kind: "existed", title: "Already existed", testid: "combo-existed" },
    { kind: "created", title: "Created", testid: "combo-created" },
    { kind: "failed", title: "Failed", testid: "combo-failed" },
  ];

  if (!payload.colour_dimension) {
    return (
      <div className="card section-card" data-testid="combo-grid">
        <p className="warn-note">
          This identity profile has no colour dimension, so there is no size × colour grid to fill.
          Add SKUs one at a time instead.
        </p>
      </div>
    );
  }

  return (
    <div className="card section-card" data-testid="combo-grid">
      <h4 className="h4">Combinations that exist</h4>
      <p className="lead">
        Tick the size and colour combinations this style is actually made in, review the list, then
        create them. Nothing is created that you have not ticked.
      </p>
      {stepUp.dialog}
      {stage === "pick" && (
        <>
          <div className="table-wrap">
            <table className="data" data-testid="combo-table">
              <thead>
                <tr>
                  <th scope="col">Size</th>
                  {colourValues.map((colour) => (
                    <th key={colour.id} scope="col">
                      {colour.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sizeValues.map((size) => (
                  <tr key={size.id}>
                    <th scope="row">{size.label}</th>
                    {colourValues.map((colour) => {
                      const id = key(size.id, colour.id);
                      const inputId = `combo-${size.value}-${colour.value}`;
                      return (
                        <td key={colour.id}>
                          <label className="pp-combo-cell" htmlFor={inputId}>
                            <input
                              id={inputId}
                              type="checkbox"
                              checked={ticked.has(id)}
                              onChange={() => toggle(size.id, colour.id)}
                              data-testid={inputId}
                            />
                            <span className="sr-only">
                              {size.label} in {colour.label}
                            </span>
                          </label>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button
            className="btn btn-cta"
            onClick={() => setStage("review")}
            disabled={chosen.length === 0}
            data-testid="combo-review"
          >
            Review {chosen.length} combination{chosen.length === 1 ? "" : "s"}
          </button>
        </>
      )}
      {stage === "review" && (
        <>
          <ul data-testid="combo-review-list">
            {chosen.map((id) => (
              <li key={id}>
                {(() => {
                  const [sizeId, colourId] = id.split("|");
                  return sizeId && colourId ? label(sizeId, colourId) : "Invalid combination";
                })()}
              </li>
            ))}
          </ul>
          <button className="btn btn-sm" onClick={() => setStage("pick")} data-testid="combo-back">
            ← Change the ticks
          </button>
          <button
            className="btn btn-cta"
            onClick={() => run(chosen)}
            disabled={busy}
            data-testid="combo-create"
          >
            <Save size={15} /> Create these {chosen.length}
          </button>
        </>
      )}
      {stage === "done" && (
        <>
          {groups.map((group) => {
            const rows = outcomes.filter((o) => o.kind === group.kind);
            return (
              <div key={group.kind} data-testid={group.testid}>
                <h5 className="h5">
                  {group.title} ({rows.length})
                </h5>
                <ul>
                  {rows.map((row) => (
                    <li key={row.key}>
                      {row.label} — {row.message}
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
          {failed.length > 0 && (
            <button
              className="btn btn-cta"
              onClick={() => run(failed.map((o) => o.key))}
              disabled={busy}
              data-testid="combo-retry"
            >
              Retry the {failed.length} that failed
            </button>
          )}
          <button className="btn btn-sm" onClick={() => setStage("pick")} data-testid="combo-again">
            Back to the grid
          </button>
        </>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// SKUs (E046-E050) and aliases (E051-E055)
// --------------------------------------------------------------------------

function SkusPanel({ search }: { search: string }) {
  const skus = useResourceList<SkuData>(
    `/goods-v1/masters/skus?limit=100${search.trim() ? `&q=${encodeURIComponent(search.trim())}` : ""}`,
  );
  const styles = useResourceList<StyleData>("/goods-v1/masters/styles?limit=100");
  const configs = useConfigurations("identity_profile");

  if (skus.denied) return <Denied what="SKU" />;
  const state = listState(
    { loading: skus.loading, failure: skus.failure, empty: skus.items.length === 0 },
    "No SKUs yet.",
  );
  const styleCode = (id: string) =>
    styles.items.find((s) => s.id === id)?.data.style_code ?? "Unknown";

  return (
    <div data-testid="skus-panel">
      <h3 className="h3">SKUs</h3>
      <p className="lead">
        A SKU is created under its style, where the approved identity profile decides what
        identifies it. Open a style to add one.
      </p>
      <div className="table-wrap">
        <table className="data" data-testid="skus-table">
          <thead>
            <tr>
              <th>Style</th>
              <th>Attributes</th>
              <th>Pinned profile</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {state ? (
              <tr data-testid="skus-state">
                <td colSpan={4}>{state}</td>
              </tr>
            ) : (
              skus.items.map((row) => (
                <tr key={row.id} data-testid={`sku-row-${row.id}`}>
                  <td className="mono">{styleCode(row.data.style_id)}</td>
                  <td>{describeAttrs(row.data.attrs, new Map())}</td>
                  <td className="mono" style={{ fontSize: 12.5 }}>
                    {row.data.profile_version_id
                      ? `pinned to ${pinnedLabel(configs.items, row.data.profile_version_id) ?? row.data.profile_version_id}`
                      : "Unknown"}
                  </td>
                  <td>{stateChip(row.state)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const blankAlias = {
  sku_id: "",
  issuer_key: "",
  alias_type: "barcode" as (typeof ALIAS_TYPES)[number],
  value: "",
  site_id: "",
};

function AliasesPanel({ search }: { search: string }) {
  const { session } = useAuth();
  const canManage = hold(session, "product.master.manage");
  const aliases = useResourceList<AliasData>(
    `/goods-v1/masters/aliases?limit=100${search.trim() ? `&q=${encodeURIComponent(search.trim())}` : ""}`,
  );
  const skus = useResourceList<SkuData>("/goods-v1/masters/skus?limit=100");
  const styles = useResourceList<StyleData>("/goods-v1/masters/styles?limit=100");
  // The sites this person may act at, straight off their session. The masters
  // site list needs `org.site.manage`, which a product master owner does not
  // hold — reading it here would be a refusal on every load, for the one
  // persona this screen exists for.
  const sites = session?.sites ?? [];
  const [form, setForm] = useState(blankAlias);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();

  const skuLabel = (id: string) => {
    const sku = skus.items.find((s) => s.id === id);
    if (!sku) return id;
    const style = styles.items.find((s) => s.id === sku.data.style_id);
    return `${style?.data.style_code ?? "?"} · ${describeAttrs(sku.data.attrs, new Map())}`;
  };

  async function create() {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post("/goods-v1/masters/aliases", {
          sku_id: form.sku_id,
          issuer_key: form.issuer_key,
          alias_type: form.alias_type,
          value: form.value,
          ...(form.site_id ? { site_id: form.site_id } : {}),
          effective_from: nowIso(),
          ...goodsMeta(),
        }),
      );
      setOpen(false);
      setOk(`Alias ${form.value} added.`);
      setForm(blankAlias);
      aliases.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (aliases.denied) return <Denied what="alias" />;
  const state = listState(
    { loading: aliases.loading, failure: aliases.failure, empty: aliases.items.length === 0 },
    "No aliases yet.",
  );

  return (
    <div data-testid="aliases-panel">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">Aliases</h3>
        <div className="spacer" />
        {canManage && (
          <button className="btn btn-cta" onClick={() => setOpen(true)} data-testid="alias-new">
            <Plus size={15} /> New alias
          </button>
        )}
      </div>
      <p className="lead">
        A barcode or vendor code that names a SKU, for an issuer, a context and a period. Two
        articles may share a code; that is resolved by asking, never by merging them.
      </p>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {canManage && open && (
        <div className="card section-card" data-testid="alias-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">Add alias</h3>
            <div className="spacer" />
            <button className="btn btn-sm" onClick={() => setOpen(false)} data-testid="alias-close">
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <Field id="alias-sku" label="SKU">
              <select
                id="alias-sku"
                className="select"
                value={form.sku_id}
                onChange={(e) => setForm({ ...form, sku_id: e.target.value })}
                data-testid="alias-sku"
              >
                <option value="">— choose —</option>
                {skus.items.map((sku) => (
                  <option key={sku.id} value={sku.id}>
                    {skuLabel(sku.id)}
                  </option>
                ))}
              </select>
            </Field>
            <Field id="alias-type" label="Kind">
              <select
                id="alias-type"
                className="select"
                value={form.alias_type}
                onChange={(e) =>
                  setForm({ ...form, alias_type: e.target.value as typeof form.alias_type })
                }
                data-testid="alias-type"
              >
                {ALIAS_TYPES.filter((t) => t !== "generated").map((t) => (
                  <option key={t} value={t}>
                    {t === "barcode" ? "Barcode" : "Vendor code"}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              id="alias-issuer"
              label="Issuer"
              hint="Who issued this code — a vendor, or your own range."
            >
              <input
                id="alias-issuer"
                className="input"
                value={form.issuer_key}
                onChange={(e) => setForm({ ...form, issuer_key: e.target.value })}
                data-testid="alias-issuer"
              />
            </Field>
            <Field id="alias-value" label="Code">
              <input
                id="alias-value"
                className="input"
                value={form.value}
                onChange={(e) => setForm({ ...form, value: e.target.value })}
                data-testid="alias-value"
              />
            </Field>
            <Field
              id="alias-site"
              label="Site (optional)"
              hint="Leave empty for a code that means the same everywhere."
            >
              <select
                id="alias-site"
                className="select"
                value={form.site_id}
                onChange={(e) => setForm({ ...form, site_id: e.target.value })}
                data-testid="alias-site"
              >
                <option value="">Everywhere</option>
                {sites.map((site) => (
                  <option key={site.id} value={site.id}>
                    {site.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <button
            className="btn btn-cta"
            onClick={create}
            disabled={!form.sku_id || !form.issuer_key || !form.value}
            data-testid="alias-save"
          >
            <Save size={15} /> Save alias
          </button>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="aliases-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Kind</th>
              <th>Issuer</th>
              <th>SKU</th>
              <th>Site</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {state ? (
              <tr data-testid="aliases-state">
                <td colSpan={6}>{state}</td>
              </tr>
            ) : (
              aliases.items.map((row) => (
                <tr key={row.id} data-testid={`alias-row-${row.data.value}`}>
                  <td>
                    <b className="mono">{row.data.value}</b>
                  </td>
                  <td>{row.data.alias_type}</td>
                  <td className="mono">{row.data.issuer_key}</td>
                  <td>{skuLabel(row.data.sku_id)}</td>
                  <td>
                    {row.data.site_id
                      ? (sites.find((s) => s.id === row.data.site_id)?.name ?? row.data.site_id)
                      : "Everywhere"}
                  </td>
                  <td>{stateChip(row.state)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Source crosswalks (E056-E060, E228, E231)
// --------------------------------------------------------------------------

function CrosswalksPanel({ search }: { search: string }) {
  const { session } = useAuth();
  const canManage = hold(session, "crosswalk.manage");
  const canPropose = hold(session, "crosswalk.propose");
  const rows = useResourceList<CrosswalkData>(
    `/goods-v1/masters/crosswalks?limit=100${search.trim() ? `&q=${encodeURIComponent(search.trim())}` : ""}`,
  );
  const { profiles } = useEffectiveIdentityProfiles();
  const vendors = useResourceList<VendorData>("/goods-v1/vendors");
  const brands = useResourceList<NamedMaster>("/goods-v1/masters/brands");
  const [form, setForm] = useState({
    kind: "vendor" as CrosswalkData["kind"],
    issuer_key: "",
    source_key: "",
    target_key: "",
  });
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();
  const configVersionId = profiles[0]?.versionId ?? "";

  const targets =
    form.kind === "brand" ? brands.items : form.kind === "vendor" ? vendors.items : [];

  async function create() {
    setError("");
    setOk("");
    try {
      await stepUp.guarded(() =>
        api.post("/goods-v1/masters/crosswalks", {
          kind: form.kind,
          issuer_key: form.issuer_key,
          source_key: form.source_key,
          target_key: form.target_key,
          config_version_id: configVersionId,
          ...goodsMeta(),
        }),
      );
      setOpen(false);
      setOk(`Mapping for ${form.source_key} saved.`);
      rows.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  if (rows.denied) return <Denied what="source mapping" />;
  const state = listState(
    { loading: rows.loading, failure: rows.failure, empty: rows.items.length === 0 },
    "No source mappings yet.",
  );

  return (
    <div data-testid="crosswalks-panel">
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <h3 className="h3">Source mappings</h3>
        <div className="spacer" />
        {(canManage || canPropose) && (
          <button
            className="btn btn-cta"
            onClick={() => setOpen(true)}
            disabled={!configVersionId}
            data-testid="crosswalk-new"
          >
            <Plus size={15} /> New mapping
          </button>
        )}
      </div>
      <p className="lead">
        What a supplier or a spreadsheet calls a vendor, brand or sub-brand, and which of ours it
        means. A mapping with no target chosen stays here, visible, until someone chooses one.
      </p>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}
      {open && (
        <div className="card section-card" data-testid="crosswalk-editor">
          <div className="toolbar" style={{ marginBottom: 12 }}>
            <h3 className="h3">New mapping</h3>
            <div className="spacer" />
            <button
              className="btn btn-sm"
              onClick={() => setOpen(false)}
              data-testid="crosswalk-close"
            >
              <X size={14} /> Close
            </button>
          </div>
          <div className="form-grid wide-form">
            <Field id="crosswalk-kind" label="Maps to">
              <select
                id="crosswalk-kind"
                className="select"
                value={form.kind}
                onChange={(e) =>
                  setForm({
                    ...form,
                    kind: e.target.value as CrosswalkData["kind"],
                    target_key: "",
                  })
                }
                data-testid="crosswalk-kind"
              >
                {CROSSWALK_KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {kind}
                  </option>
                ))}
              </select>
            </Field>
            <Field id="crosswalk-issuer" label="Issuer" hint="Whose naming this is.">
              <input
                id="crosswalk-issuer"
                className="input"
                value={form.issuer_key}
                onChange={(e) => setForm({ ...form, issuer_key: e.target.value })}
                data-testid="crosswalk-issuer"
              />
            </Field>
            <Field id="crosswalk-source" label="What they call it">
              <input
                id="crosswalk-source"
                className="input"
                value={form.source_key}
                onChange={(e) => setForm({ ...form, source_key: e.target.value })}
                data-testid="crosswalk-source"
              />
            </Field>
            <Field
              id="crosswalk-target"
              label="What we call it"
              hint="Leave unchosen to record the source key now and decide later."
            >
              <select
                id="crosswalk-target"
                className="select"
                value={form.target_key}
                onChange={(e) => setForm({ ...form, target_key: e.target.value })}
                data-testid="crosswalk-target"
              >
                <option value="">Not chosen yet</option>
                {targets.map((target) => (
                  <option key={target.id} value={target.id}>
                    {target.data.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <button
            className="btn btn-cta"
            onClick={create}
            disabled={!form.issuer_key || !form.source_key}
            data-testid="crosswalk-save"
          >
            <Save size={15} /> Save mapping
          </button>
        </div>
      )}
      <div className="table-wrap">
        <table className="data" data-testid="crosswalks-table">
          <thead>
            <tr>
              <th>Kind</th>
              <th>Issuer</th>
              <th>They call it</th>
              <th>We call it</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {state ? (
              <tr data-testid="crosswalks-state">
                <td colSpan={5}>{state}</td>
              </tr>
            ) : (
              rows.items.map((row) => (
                <tr key={row.id} data-testid={`crosswalk-row-${row.data.source_key}`}>
                  <td>{row.data.kind}</td>
                  <td className="mono">{row.data.issuer_key}</td>
                  <td>
                    <b>{row.data.source_key}</b>
                  </td>
                  <td>{row.data.target_key || <i>Not chosen yet</i>}</td>
                  <td>{stateChip(row.state)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Barcode lookup and the scoped choice (E090, E091)
// --------------------------------------------------------------------------

/** What tells two candidates apart, so the person choosing can see it. */
function differences(candidates: Candidate[]): string[] {
  const fields: (keyof Candidate)[] = ["brand", "style", "size", "colour", "grade"];
  return fields
    .filter((field) => new Set(candidates.map((c) => String(c[field] ?? "—"))).size > 1)
    .map(String);
}

function LookupPanel() {
  const { session } = useAuth();
  const profilesState = useEffectiveIdentityProfiles();
  // Same as the alias panel: where a code was read is one of this person's own
  // authorised sites, which the session already names.
  const sites = session?.sites ?? [];
  const [params] = useSearchParams();
  const subjectRevisionId = params.get("subject_revision_id");
  const scanEventId = params.get("scan_event_id");
  const [form, setForm] = useState({
    value: "",
    site_id: "",
    issuer_key: "",
    alias_type: "barcode" as (typeof ALIAS_TYPES)[number],
    profile_version_id: "",
  });
  const [result, setResult] = useState<Resolution | null>(null);
  const [asOf, setAsOf] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);

  const profileId = form.profile_version_id || (profilesState.profiles[0]?.versionId ?? "");

  async function look() {
    setError("");
    setOk("");
    setResult(null);
    setBusy(true);
    const stamp = nowIso();
    setAsOf(stamp);
    const query = new URLSearchParams({
      value: form.value,
      site_id: form.site_id,
      issuer_key: form.issuer_key,
      alias_type: form.alias_type,
      as_of: stamp,
      profile_version_id: profileId,
    });
    try {
      const { data } = await api.get<Resolution>(
        `/goods-v1/masters/skus/lookup?${query.toString()}`,
      );
      setResult(data);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function choose(candidate: Candidate) {
    if (!result) return;
    setError("");
    setOk("");
    try {
      await api.post("/goods-v1/masters/identity-picks", {
        context: {
          site_id: form.site_id,
          issuer_key: form.issuer_key,
          alias_type: form.alias_type,
          as_of: asOf,
          profile_version_id: profileId,
          ...(subjectRevisionId ? { subject_revision_id: subjectRevisionId } : {}),
        },
        value: form.value,
        candidate_hash: result.candidate_hash,
        chosen_sku_id: candidate.sku_id,
        // Named twice on purpose (E091): inside `context` it narrows the
        // re-resolution to the draft's own scope, and at the top level it is
        // the subject the choice is recorded against. The server refuses them
        // if they disagree.
        ...(subjectRevisionId ? { subject_revision_id: subjectRevisionId } : {}),
        ...(scanEventId ? { scan_event_id: scanEventId } : {}),
        ...goodsMeta(),
      });
      setOk("Your choice is recorded against this document. Nothing about the two SKUs changed.");
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  const scoped = Boolean(subjectRevisionId || scanEventId);
  const differing = result ? differences(result.candidates) : [];

  return (
    <div data-testid="lookup-panel">
      <h3 className="h3">Barcode lookup</h3>
      <p className="lead">
        Every SKU a code can mean, here and now. When a code means more than one thing, the system
        shows both and asks — it never picks one, and it never joins two articles together.
      </p>
      <Feedback error={error} ok={ok} />
      <div className="card section-card">
        <div className="form-grid wide-form">
          <Field id="lookup-value" label="Code">
            <input
              id="lookup-value"
              className="input"
              value={form.value}
              onChange={(e) => setForm({ ...form, value: e.target.value })}
              data-testid="lookup-value"
            />
          </Field>
          <Field id="lookup-site" label="Where it was read">
            <select
              id="lookup-site"
              className="select"
              value={form.site_id}
              onChange={(e) => setForm({ ...form, site_id: e.target.value })}
              data-testid="lookup-site"
            >
              <option value="">— choose —</option>
              {sites.map((site) => (
                <option key={site.id} value={site.id}>
                  {site.name}
                </option>
              ))}
            </select>
          </Field>
          <Field id="lookup-issuer" label="Issuer">
            <input
              id="lookup-issuer"
              className="input"
              value={form.issuer_key}
              onChange={(e) => setForm({ ...form, issuer_key: e.target.value })}
              data-testid="lookup-issuer"
            />
          </Field>
          <Field id="lookup-type" label="Kind of code">
            <select
              id="lookup-type"
              className="select"
              value={form.alias_type}
              onChange={(e) =>
                setForm({ ...form, alias_type: e.target.value as typeof form.alias_type })
              }
              data-testid="lookup-type"
            >
              {ALIAS_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </Field>
          <Field id="lookup-profile" label="Identity profile">
            <select
              id="lookup-profile"
              className="select"
              value={profileId}
              onChange={(e) => setForm({ ...form, profile_version_id: e.target.value })}
              data-testid="lookup-profile"
            >
              {profilesState.profiles.map((profile) => (
                <option key={profile.versionId} value={profile.versionId}>
                  {profile.payload.family} · version {profile.version}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <button
          className="btn btn-cta"
          onClick={look}
          disabled={busy || !form.value || !form.site_id || !form.issuer_key || !profileId}
          data-testid="lookup-run"
        >
          <ScanLine size={15} /> Look this code up
        </button>
      </div>
      {result && result.result === "unknown" && (
        <p className="warn-note" data-testid="lookup-unknown">
          No SKU is known by this code here and now.
        </p>
      )}
      {result && result.candidates.length > 0 && (
        <div data-testid="lookup-candidates">
          <h4 className="h4">
            {result.result === "ambiguous"
              ? `${result.candidates.length} articles share this code`
              : "One article has this code"}
          </h4>
          {differing.length > 0 && (
            <p className="lead" data-testid="lookup-differs">
              They differ by: {differing.join(", ")}.
            </p>
          )}
          <div className="pp-candidates">
            {result.candidates.map((candidate) => (
              <div
                className="card section-card"
                key={candidate.sku_id}
                data-testid={`lookup-candidate-${candidate.sku_id}`}
              >
                <dl className="pp-candidate-facts">
                  <dt>Brand</dt>
                  <dd className={differing.includes("brand") ? "pp-differs" : ""}>
                    {candidate.brand}
                  </dd>
                  <dt>Style</dt>
                  <dd className={differing.includes("style") ? "pp-differs" : ""}>
                    {candidate.style}
                  </dd>
                  <dt>Size</dt>
                  <dd className={differing.includes("size") ? "pp-differs" : ""}>
                    {candidate.size}
                  </dd>
                  {candidate.colour !== undefined && (
                    <>
                      <dt>Colour</dt>
                      <dd className={differing.includes("colour") ? "pp-differs" : ""}>
                        {candidate.colour}
                      </dd>
                    </>
                  )}
                  {candidate.grade !== undefined && (
                    <>
                      <dt>Grade</dt>
                      <dd className={differing.includes("grade") ? "pp-differs" : ""}>
                        {candidate.grade}
                      </dd>
                    </>
                  )}
                </dl>
                <button
                  className="btn btn-cta"
                  onClick={() => choose(candidate)}
                  disabled={!scoped}
                  data-testid={`lookup-choose-${candidate.sku_id}`}
                >
                  Use this one here
                </button>
              </div>
            ))}
          </div>
          <p className="muted" data-testid="lookup-scope-note">
            A choice is recorded for{" "}
            <b>{sites.find((site) => site.id === form.site_id)?.name ?? "this site"}</b>, this
            issuer and this moment, against the document being worked on. It changes nothing about
            either article, and nothing here joins them.
          </p>
          {!scoped && (
            <p className="muted" data-testid="lookup-no-subject">
              No document is open here, so there is nothing to record the choice against yet. The
              receiving and PT screens open this lookup for the line they are working on
              (`?subject_revision_id=…` or `?scan_event_id=…`), and the choice is recorded there.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// The page
// --------------------------------------------------------------------------

const NAV: { group: string; items: { type: MasterType; label: string; icon: ReactNode }[] }[] = [
  {
    group: "Products",
    items: [
      { type: "styles", label: "Styles", icon: <Shapes size={15} /> },
      { type: "skus", label: "SKUs", icon: <Boxes size={15} /> },
      { type: "aliases", label: "Aliases", icon: <Tags size={15} /> },
      { type: "lookup", label: "Barcode lookup", icon: <ScanLine size={15} /> },
    ],
  },
  {
    group: "Mapping",
    items: [
      { type: "crosswalks", label: "Source mappings", icon: <Tags size={15} /> },
      { type: "brand-reconciliation", label: "Brand identity review", icon: <Tags size={15} /> },
    ],
  },
  {
    group: "Parties",
    items: [
      { type: "vendors", label: "Vendors", icon: <Truck size={15} /> },
      { type: "brands", label: "Brands", icon: <Shapes size={15} /> },
      { type: "seasons", label: "Seasons", icon: <Shapes size={15} /> },
      { type: "subbrands", label: "Sub-brands", icon: <Shapes size={15} /> },
    ],
  },
];

export function ProductsPartiesPage() {
  const { session } = useAuth();
  const [params, setParams] = useSearchParams();
  const type = (params.get("type") as MasterType | null) ?? "styles";
  const styleId = params.get("style");
  const [search, setSearch] = useState("");

  function select(next: MasterType) {
    setParams({ type: next });
    setSearch("");
  }

  return (
    <div className="page-pad">
      <PageHeader />
      <div className="org-layout pp-layout">
        <nav className="org-nav" aria-label="Products and parties">
          <div className="org-nav-group">
            <Field id="pp-search" label="Search">
              <input
                id="pp-search"
                className="input"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Code or name"
                data-testid="pp-search"
              />
            </Field>
          </div>
          {NAV.map((group) => (
            <div className="org-nav-group" key={group.group}>
              <h4>{group.group}</h4>
              <ul className="org-nav-list">
                {group.items
                  .filter(
                    (item) =>
                      item.type !== "brand-reconciliation" ||
                      session?.display_actions.includes("access.manage"),
                  )
                  .map((item) => (
                    <li key={item.type}>
                      <button
                        className={`org-nav-item ${type === item.type ? "active" : ""}`}
                        onClick={() => select(item.type)}
                        data-testid={`pp-nav-${item.type}`}
                      >
                        {item.icon} {item.label}
                      </button>
                    </li>
                  ))}
              </ul>
            </div>
          ))}
        </nav>
        <div className="org-detail">
          {type === "styles" && (
            <StylesPanel
              search={search}
              selected={styleId}
              onSelect={(id) => setParams(id ? { type: "styles", style: id } : { type: "styles" })}
            />
          )}
          {type === "skus" && <SkusPanel search={search} />}
          {type === "aliases" && <AliasesPanel search={search} />}
          {type === "lookup" && <LookupPanel />}
          {type === "crosswalks" && <CrosswalksPanel search={search} />}
          {type === "brand-reconciliation" && <BrandReconciliationPanel />}
          {type === "vendors" && (
            <SimpleMasterPanel<VendorData>
              title="Vendors"
              noun="Vendor"
              testid="vendor"
              listUrl={"/goods-v1/vendors"}
              writeUrl={(id) => (id ? `/goods-v1/vendors/${id}` : "/goods-v1/vendors")}
              extras={[
                { key: "gstin", label: "GSTIN", optional: true },
                { key: "agent_ref", label: "Agent", optional: true },
              ]}
              search={search}
            />
          )}
          {type === "brands" && (
            <SimpleMasterPanel<NamedMaster>
              title="Brands"
              noun="Brand"
              testid="brand"
              listUrl={"/goods-v1/masters/brands"}
              writeUrl={(id) =>
                id ? `/goods-v1/masters/brands/${id}` : "/goods-v1/masters/brands"
              }
              extras={[]}
              search={search}
            />
          )}
          {type === "seasons" && (
            <SimpleMasterPanel<NamedMaster>
              title="Seasons"
              noun="Season"
              testid="season"
              listUrl={"/goods-v1/masters/seasons"}
              writeUrl={(id) =>
                id ? `/goods-v1/masters/seasons/${id}` : "/goods-v1/masters/seasons"
              }
              extras={[]}
              search={search}
            />
          )}
          {type === "subbrands" && (
            <SimpleMasterPanel<NamedMaster>
              title="Sub-brands"
              noun="Sub-brand"
              testid="subbrand"
              listUrl="/goods-v1/masters/subbrands"
              writeUrl={(id) =>
                id ? `/goods-v1/masters/subbrands/${id}` : "/goods-v1/masters/subbrands"
              }
              extras={[{ key: "parent_id", label: "Parent brand" }]}
              search={search}
            />
          )}
        </div>
      </div>
    </div>
  );
}
