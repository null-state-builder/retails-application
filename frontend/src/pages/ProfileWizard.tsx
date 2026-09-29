// "Set up a PT profile": the guided way through the configuration a PT needs
// before it can price anything (GSA-T04).
//
// The order is forced by the rules, not by taste: a SKU identity profile may
// only name governed vocabulary, and a PT profile may only name *approved*
// rates and tax versions. So each step's versions must be approved — by
// someone other than whoever prepared them — before the next step can be
// drafted at all. The wizard shows that plainly instead of failing at the end.
//
// **The fashion layout below is a starting draft.** It is a shape to edit, not
// approved tenant configuration and not anyone's real rates or vocabulary.
// Nothing here is in force until a second person approves each version.
import { useState } from "react";
import { Check, ExternalLink, Plus, Send, Sparkles, Trash2 } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import {api, apiErrorMessage, goodsMeta} from "../lib/api";
import { Feedback, Field, hold, useStepUp } from "../lib/goodsScreen";
import type { ResourceDTO } from "../lib/goodsScreen";
import {
  allVersions,
  allowedValues,
  TENANT_SCOPE,
  useConfigurations,
  useControlledValues,
  type AllowedChoice,
  type ConfigData,
} from "../lib/goodsConfig";
import { formatPaiseString } from "../lib/format";
import { useAuth } from "../auth/AuthContext";
import type { Payload } from "./configurationPayloads";

const SIZE_DIMENSION = "size";
const COLOUR_DIMENSION = "colour";
const FASHION_FAMILY = "apparel";

/** The fashion starting draft. A shape to edit — never approved configuration. */
const FASHION_SIZES = [
  { value_key: "XS", label: "XS", sort_order: 0, retired: false },
  { value_key: "S", label: "S", sort_order: 1, retired: false },
  { value_key: "M", label: "M", sort_order: 2, retired: false },
  { value_key: "L", label: "L", sort_order: 3, retired: false },
  { value_key: "XL", label: "XL", sort_order: 4, retired: false },
  { value_key: "XXL", label: "XXL", sort_order: 5, retired: false },
  { value_key: "FREE", label: "Free Size", sort_order: 6, retired: false },
];

const FASHION_COLOURS = [
  { value_key: "BLACK", label: "Black", sort_order: 0, retired: false },
  { value_key: "WHITE", label: "White", sort_order: 1, retired: false },
  { value_key: "BLUE", label: "Blue", sort_order: 2, retired: false },
  { value_key: "RED", label: "Red", sort_order: 3, retired: false },
  { value_key: "GREEN", label: "Green", sort_order: 4, retired: false },
];

interface ColumnDraft {
  id: string;
  key: string;
  label: string;
  order: number;
  logical_type: "text" | "vocabulary" | "quantity" | "money" | "percent" | "alias";
  required: boolean;
  mode: "supplied" | "selected" | "inherited" | "looked_up" | "derived";
  dependency_keys: string[];
  rounding_scale: number;
  tolerance_minor_units: number;
  table_visible: boolean;
  export_visible: boolean;
  currency?: "INR";
  unit?: "piece";
}

function column(
  key: string,
  label: string,
  order: number,
  logical_type: ColumnDraft["logical_type"],
  mode: ColumnDraft["mode"],
  required: boolean,
): ColumnDraft {
  return {
    id: crypto.randomUUID(),
    key,
    label,
    order,
    logical_type,
    required,
    mode,
    dependency_keys: [],
    rounding_scale: logical_type === "money" ? 2 : 0,
    tolerance_minor_units: 0,
    table_visible: true,
    export_visible: true,
    ...(logical_type === "money" ? { currency: "INR" as const } : {}),
    ...(logical_type === "quantity" ? { unit: "piece" as const } : {}),
  };
}

/** The canonical PT file's own headers, as a starting draft to edit. */
function fashionColumns(): ColumnDraft[] {
  return [
    column("SEASON", "Season", 0, "vocabulary", "selected", true),
    column("BRAND", "Brand", 1, "vocabulary", "looked_up", true),
    column("DESIGN", "Design", 2, "text", "supplied", true),
    column("COLOR", "Colour", 3, "vocabulary", "selected", true),
    column("SIZE", "Size", 4, "vocabulary", "selected", false),
    column("BARCODE", "Barcode", 5, "alias", "supplied", false),
    column("HSN", "HSN", 6, "text", "supplied", true),
    column("QTY", "Quantity", 7, "quantity", "supplied", true),
    column("MRP", "MRP", 8, "money", "supplied", true),
    column("BASIC", "Basic", 9, "money", "supplied", true),
    column("P RATE", "P rate", 10, "money", "derived", false),
    column("INPUT TAX", "Input tax", 11, "percent", "derived", false),
    column("OUTPUT TAX", "Output tax", 12, "percent", "derived", false),
    column("MARGIN", "Margin", 13, "percent", "derived", false),
  ];
}

const DIRECTIONS = [
  { key: "base_to_ticket", label: "From BASIC to the ticket (MRP is derived)" },
  { key: "ticket_to_purchase", label: "From the ticket to the purchase rate (MRP is given)" },
  { key: "both_supplied", label: "Both given (only the cost load is derived)" },
] as const;

// --------------------------------------------------------------------------
// The two profile payload editors, shared with the plain Configuration editor
// --------------------------------------------------------------------------

function list<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** One dimension's allowed-value list: every approved value, or a chosen few.
 *
 *  The values are read straight from the governed vocabulary (E226), with no
 *  profile named — this is how the *first* profile is chosen, and it cannot
 *  name a profile that does not exist yet.
 *
 *  "Every approved value" is what the server stores as an empty list, and it is
 *  a real choice. It is not, however, what this screen does when it could not
 *  read the vocabulary: that shows the failure and changes nothing. */
function AllowedValues({
  field,
  dimension,
  value,
  onChange,
}: {
  field: string;
  dimension: string;
  value: Payload;
  onChange: (next: Payload) => void;
}) {
  const [search, setSearch] = useState("");
  const read = useControlledValues(null, dimension || null, search);
  const [refused, setRefused] = useState("");
  const chosen = list<string>(value[field]);
  const restricted = chosen.length > 0;
  const effective = read.values.filter((v) => v.state === "effective");
  const testid = `ip-allowed-${field}`;

  function choose(next: AllowedChoice) {
    const decided = allowedValues(next, read);
    // A refused choice is said out loud. Swallowing it leaves a control that
    // looks live and does nothing.
    if ("blocked" in decided) {
      setRefused(decided.blocked);
      return;
    }
    setRefused("");
    onChange({ ...value, [field]: decided.ids });
  }

  if (!dimension) return null;
  return (
    <fieldset className="card section-card" data-testid={testid}>
      <legend>Values allowed for {dimension}</legend>
      {read.failure ? (
        <p className="warn-note" data-testid={`${testid}-failure`}>
          The approved {dimension} vocabulary could not be read, so this list cannot be chosen yet:{" "}
          {read.failure}
        </p>
      ) : (
        <>
          <label className="check-row">
            <input
              type="radio"
              name={testid}
              checked={!restricted}
              onChange={() => choose({ restricted: false, ids: [] })}
              data-testid={`${testid}-any`}
            />
            Every approved {dimension} value
          </label>
          <label className="check-row">
            <input
              type="radio"
              name={testid}
              checked={restricted}
              onChange={() =>
                choose({ restricted: true, ids: effective.slice(0, 1).map((v) => v.id) })
              }
              data-testid={`${testid}-some`}
            />
            Only the values ticked below
          </label>
          {restricted && (
            <>
              <Field id={`${testid}-search`} label={`Find a ${dimension} value`}>
                <input
                  id={`${testid}-search`}
                  className="input"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  data-testid={`${testid}-search`}
                />
              </Field>
              <p className="muted" data-testid={`${testid}-count`}>
                {chosen.length} chosen. A value ticked under another search term stays ticked even
                while this list does not show it.
              </p>
              {effective.map((v) => (
                <label className="check-row" key={v.id}>
                  <input
                    type="checkbox"
                    checked={chosen.includes(v.id)}
                    onChange={(e) =>
                      choose({
                        restricted: true,
                        ids: e.target.checked
                          ? [...chosen, v.id]
                          : chosen.filter((id) => id !== v.id),
                      })
                    }
                    data-testid={`${testid}-${v.value}`}
                  />
                  {v.label}
                </label>
              ))}
              {read.hasMore && (
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={read.showMore}
                  disabled={read.loading}
                  data-testid={`${testid}-more`}
                >
                  Show more
                </button>
              )}
            </>
          )}
          {read.loading && <p className="muted">Reading the approved {dimension} vocabulary…</p>}
          {refused && (
            <p className="goods-hint" data-testid={`${testid}-refused`}>
              {refused}
            </p>
          )}
        </>
      )}
    </fieldset>
  );
}

/** A SKU identity profile: which governed dimensions identify a SKU, and which
 *  of each dimension's approved values it allows. */
export function IdentityProfileEditor({
  value,
  onChange,
}: {
  value: Payload;
  onChange: (next: Payload) => void;
}) {
  const distinguishing = list<string>(value.distinguishing_dimensions);
  return (
    <div data-testid="identity-profile-editor">
      <div className="form-grid wide-form">
        <Field id="ip-family" label="Product family" hint="Styles of this family use this profile.">
          <input
            id="ip-family"
            className="input"
            value={str(value.family)}
            onChange={(e) => onChange({ ...value, family: e.target.value })}
            data-testid="ip-family"
          />
        </Field>
        <Field id="ip-size" label="Size dimension" hint="Its vocabulary must already be approved.">
          <input
            id="ip-size"
            className="input"
            value={str(value.size_dimension)}
            onChange={(e) => onChange({ ...value, size_dimension: e.target.value })}
            data-testid="ip-size"
          />
        </Field>
        <Field id="ip-colour" label="Colour dimension (optional)">
          <input
            id="ip-colour"
            className="input"
            value={str(value.colour_dimension)}
            onChange={(e) =>
              onChange({ ...value, colour_dimension: e.target.value || null })
            }
            data-testid="ip-colour"
          />
        </Field>
        <Field
          id="ip-distinguishing"
          label="What tells two SKUs apart"
          hint="Comma separated. Size may be left out and stays unknown; anything named here cannot be."
        >
          <input
            id="ip-distinguishing"
            className="input"
            value={distinguishing.join(", ")}
            onChange={(e) =>
              onChange({
                ...value,
                distinguishing_dimensions: e.target.value
                  .split(",")
                  .map((part) => part.trim())
                  .filter(Boolean),
              })
            }
            data-testid="ip-distinguishing"
          />
        </Field>
      </div>
      <AllowedValues
        field="allowed_size_values"
        dimension={str(value.size_dimension)}
        value={value}
        onChange={onChange}
      />
      <AllowedValues
        field="allowed_colour_values"
        dimension={str(value.colour_dimension)}
        value={value}
        onChange={onChange}
      />
    </div>
  );
}

/** A PT profile: its columns, which direction its money is calculated in, and
 *  the approved rate and tax versions it names (change PRD §14.7 rule 9). */
export function PtProfileEditor({
  value,
  onChange,
}: {
  value: Payload;
  onChange: (next: Payload) => void;
}) {
  const rates = useConfigurations("rates");
  const taxes = useConfigurations("tax_rates");
  const columns = list<ColumnDraft>(value.columns);
  const directions = list<string>(value.directions);
  const effectiveRates = allVersions(rates.items).filter((v) => v.version.state === "effective");
  const effectiveTaxes = allVersions(taxes.items).filter((v) => v.version.state === "effective");

  function setColumns(next: ColumnDraft[]) {
    onChange({ ...value, columns: next.map((c, i) => ({ ...c, order: i })) });
  }

  return (
    <div data-testid="pt-profile-editor">
      <div className="form-grid wide-form">
        <Field id="pp-family" label="Product family">
          <input
            id="pp-family"
            className="input"
            value={str(value.family)}
            onChange={(e) => onChange({ ...value, family: e.target.value })}
            data-testid="pp-family"
          />
        </Field>
        <Field id="pp-rates" label="Rates version">
          <select
            id="pp-rates"
            className="select"
            value={str(value.rates_version_id)}
            onChange={(e) => onChange({ ...value, rates_version_id: e.target.value })}
            data-testid="pp-rates"
          >
            <option value="">— choose —</option>
            {effectiveRates.map((entry) => (
              <option key={entry.version.id} value={entry.version.id}>
                version {entry.version.version}
              </option>
            ))}
          </select>
        </Field>
        <Field id="pp-tax" label="Tax version">
          <select
            id="pp-tax"
            className="select"
            value={str(value.tax_version_id)}
            onChange={(e) => onChange({ ...value, tax_version_id: e.target.value })}
            data-testid="pp-tax"
          >
            <option value="">— choose —</option>
            {effectiveTaxes.map((entry) => (
              <option key={entry.version.id} value={entry.version.id}>
                version {entry.version.version}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <fieldset className="cfg-directions">
        <legend>How the money is worked out</legend>
        {DIRECTIONS.map((direction) => (
          <label key={direction.key} htmlFor={`pp-dir-${direction.key}`} className="cfg-check">
            <input
              id={`pp-dir-${direction.key}`}
              type="checkbox"
              checked={directions.includes(direction.key)}
              onChange={(e) =>
                onChange({
                  ...value,
                  directions: e.target.checked
                    ? [...directions, direction.key]
                    : directions.filter((d) => d !== direction.key),
                })
              }
              data-testid={`pp-dir-${direction.key}`}
            />
            <span>{direction.label}</span>
          </label>
        ))}
      </fieldset>
      <label htmlFor="pp-override" className="cfg-check">
        <input
          id="pp-override"
          type="checkbox"
          checked={Boolean(value.allow_row_override)}
          onChange={(e) => onChange({ ...value, allow_row_override: e.target.checked })}
          data-testid="pp-override"
        />
        <span>A single row may override the calculated value, with a reason</span>
      </label>
      <h5 className="h5">Columns</h5>
      <div className="table-wrap">
        <table className="data" data-testid="pp-columns">
          <thead>
            <tr>
              <th>Column</th>
              <th>Shown as</th>
              <th>Kind</th>
              <th>Where it comes from</th>
              <th>Required</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {columns.map((col, index) => (
              <tr key={col.id} data-testid={`pp-column-${col.key}`}>
                <td className="mono">{col.key}</td>
                <td>
                  <input
                    aria-label={`Label for ${col.key}`}
                    className="input"
                    value={col.label}
                    onChange={(e) =>
                      setColumns(
                        columns.map((c, i) => (i === index ? { ...c, label: e.target.value } : c)),
                      )
                    }
                    data-testid={`pp-column-label-${col.key}`}
                  />
                </td>
                <td>{col.logical_type}</td>
                <td>{col.mode}</td>
                <td>
                  <input
                    aria-label={`${col.key} is required`}
                    type="checkbox"
                    checked={col.required}
                    onChange={(e) =>
                      setColumns(
                        columns.map((c, i) =>
                          i === index ? { ...c, required: e.target.checked } : c,
                        ),
                      )
                    }
                    data-testid={`pp-column-required-${col.key}`}
                  />
                </td>
                <td>
                  <button
                    className="btn btn-sm"
                    onClick={() => setColumns(columns.filter((_, i) => i !== index))}
                    data-testid={`pp-column-remove-${col.key}`}
                  >
                    <Trash2 size={13} /> Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button
        className="btn btn-sm"
        onClick={() =>
          setColumns([
            ...columns,
            column(`COLUMN ${columns.length + 1}`, "New column", columns.length, "text", "supplied", false),
          ])
        }
        data-testid="pp-column-add"
      >
        <Plus size={13} /> Add a column
      </button>
    </div>
  );
}

// --------------------------------------------------------------------------
// The wizard
// --------------------------------------------------------------------------

type ItemState = "missing" | "draft" | "submitted" | "in_force";


function stateOf(line: ResourceDTO<ConfigData> | undefined): ItemState {
  if (!line) return "missing";
  if ((line.data.versions ?? []).some((v) => v.state === "effective")) return "in_force";
  if (line.state === "submitted") return "submitted";
  return "draft";
}

/** The line this step is about: the one already in force if there is one, and
 *  otherwise the newest draft.
 *
 *  A kind can have several lines — an abandoned draft beside the one that was
 *  approved — and the newest is not the one in force. Showing that one would
 *  tell a person their setup is unfinished when it is done, and send them to
 *  submit a rival version that approval would refuse as an overlap. */
function pick(
  lines: ResourceDTO<ConfigData>[],
  match: (line: ResourceDTO<ConfigData>) => boolean,
): ResourceDTO<ConfigData> | undefined {
  const ours = lines.filter(match);
  return ours.find((l) => (l.data.versions ?? []).some((v) => v.state === "effective")) ?? ours[0];
}

function stateWords(state: ItemState): string {
  return state === "missing"
    ? "Not started"
    : state === "draft"
      ? "Draft saved"
      : state === "submitted"
        ? "Waiting for someone else to approve it"
        : "In force";
}

/** One step item: what it is, the state it is really in, and what can be done
 *  to it next.
 *
 *  A top-level component on purpose. Declared inside `ProfileWizard` it would be
 *  a new component type on every render — and this screen re-renders as each of
 *  its five configuration reads settles, so every button would be torn out and
 *  rebuilt under the hands of whoever was reaching for it. */
function WizardItem({
  testid,
  title,
  kind,
  line,
  state,
  loading,
  starting,
  canDraft,
  busy,
  blocked,
  onCreate,
  onOpen,
  onSubmit,
}: {
  testid: string;
  title: string;
  kind: string;
  line: ResourceDTO<ConfigData> | undefined;
  state: ItemState;
  loading: boolean;
  starting: () => Payload;
  canDraft: boolean;
  busy: boolean;
  blocked?: string;
  onCreate: (kind: string, payload: Payload) => void;
  onOpen: (kind: string, id: string) => void;
  onSubmit: (line: ResourceDTO<ConfigData>) => void;
}) {
  return (
    <div className="cfg-wizard-item" data-testid={`wiz-${testid}`}>
      <div>
        <b>{title}</b>
        <div className="muted" data-testid={`wiz-${testid}-state`}>
          {/* "Not started" while the read is still in flight would be a lie,
              and the one a person acts on by starting a second line. */}
          {loading ? "Loading…" : stateWords(state)}
          {state === "in_force" && (
            <>
              {" · "}
              version {(line?.data.versions ?? []).find((v) => v.state === "effective")?.version}
            </>
          )}
        </div>
        {blocked && (
          <div className="goods-hint" data-testid={`wiz-${testid}-blocked`}>
            {blocked}
          </div>
        )}
      </div>
      <div className="spacer" />
      {canDraft && !loading && !line && !blocked && (
        <button
          className="btn btn-sm"
          onClick={() => onCreate(kind, starting())}
          disabled={busy}
          data-testid={`wiz-${testid}-create`}
        >
          <Plus size={13} /> Create starting draft
        </button>
      )}
      {line && !loading && (
        <button
          className="btn btn-sm"
          onClick={() => onOpen(kind, line.id)}
          data-testid={`wiz-${testid}-open`}
        >
          <ExternalLink size={13} /> Open
        </button>
      )}
      {canDraft && !loading && line && state === "draft" && (
        <button
          className="btn btn-sm"
          onClick={() => onSubmit(line)}
          disabled={busy}
          data-testid={`wiz-${testid}-submit`}
        >
          <Send size={13} /> Send for approval
        </button>
      )}
      {state === "in_force" && <Check size={16} aria-label="in force" />}
    </div>
  );
}

export function ProfileWizard() {
  const { session } = useAuth();
  const canDraft = hold(session, "config.draft");
  const [, setParams] = useSearchParams();
  const vocabularies = useConfigurations("vocabulary");
  const identity = useConfigurations("identity_profile");
  const rates = useConfigurations("rates");
  const taxes = useConfigurations("tax_rates");
  const profiles = useConfigurations("profile");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const stepUp = useStepUp();

  const sizeLine = pick(vocabularies.items, (l) => l.data.payload.dimension === SIZE_DIMENSION);
  const colourLine = pick(vocabularies.items, (l) => l.data.payload.dimension === COLOUR_DIMENSION);
  const identityLine = pick(identity.items, (l) => l.data.payload.family === FASHION_FAMILY);
  const ratesLine = pick(rates.items, () => true);
  const taxLine = pick(taxes.items, () => true);
  const profileLine = pick(profiles.items, (l) => l.data.payload.family === FASHION_FAMILY);

  // One signal for the whole wizard: every step reads a different list, and a
  // step that says "Not started" while its read is still in flight invites a
  // second line for something that already exists.
  const loading =
    vocabularies.loading ||
    identity.loading ||
    rates.loading ||
    taxes.loading ||
    profiles.loading;
  const ratesVersion = allVersions(rates.items).find((v) => v.version.state === "effective");
  const taxVersion = allVersions(taxes.items).find((v) => v.version.state === "effective");

  async function createDraft(kind: string, payload: Payload, reload: () => void) {
    setError("");
    setOk("");
    setBusy(true);
    try {
      await stepUp.guarded(() =>
        api.post("/goods-v1/masters/configurations", {
          kind,
          scope: TENANT_SCOPE,
          effective_from: new Date().toISOString(),
          payload,
          ...goodsMeta(),
        }),
      );
      setOk("Starting draft saved. Edit it, then send it for approval.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function submitDraft(line: ResourceDTO<ConfigData>, reload: () => void) {
    setError("");
    setOk("");
    setBusy(true);
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/masters/configurations/${line.id}/submit`, {
          reviewed_hash: line.content_hash,
          ...goodsMeta(line.revision),
        }),
      );
      setOk("Sent for approval. Someone other than you has to approve it.");
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const vocabularyReady =
    stateOf(sizeLine) === "in_force" && stateOf(colourLine) === "in_force";
  // The starting draft carries empty allowed-value lists, which the server
  // reads as "this profile restricts nothing". That is a real choice, so it may
  // not be made by a person who was never shown the values: until both reads
  // answer, the draft cannot be started at all.
  const sizeValues = useControlledValues(null, vocabularyReady ? SIZE_DIMENSION : null);
  const colourValues = useControlledValues(null, vocabularyReady ? COLOUR_DIMENSION : null);
  const valuesUnread = [sizeValues, colourValues].find((r) => r.failure || r.loading);
  const identityReady = stateOf(identityLine) === "in_force";
  const moneyReady = Boolean(ratesVersion && taxVersion);

  return (
    <div data-testid="config-wizard" data-loading={loading ? "true" : "false"}>
      <h3 className="h3">
        <Sparkles size={15} /> Set up a PT profile
      </h3>
      <p className="warn-note" data-testid="wiz-draft-warning">
        The fashion layout offered here is a <b>starting draft</b> — a shape to edit, not approved
        tenant configuration and not anyone's real rates or vocabulary. Nothing takes effect until a
        second person approves each version.
      </p>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}

      <section className="card section-card" data-testid="wiz-step-vocabulary">
        <h4 className="h4">1 · Vocabulary</h4>
        <p className="lead">
          The closed lists a SKU is described by. A size list carries an explicit Free Size value, so
          "free size" is a size and not a missing one.
        </p>
        <WizardItem
          testid="size"
          title="Size vocabulary"
          kind="vocabulary"
          line={sizeLine}
          loading={loading}
          state={stateOf(sizeLine)}
          canDraft={canDraft}
          busy={busy}
          onCreate={(kind, payload) => createDraft(kind, payload, vocabularies.reload)}
          onOpen={(kind, id) => setParams({ kind, draft: id })}
          onSubmit={(line) => submitDraft(line, vocabularies.reload)}
          starting={() => ({
            dimension: SIZE_DIMENSION,
            values: FASHION_SIZES,
            effective_from: new Date().toISOString(),
          })}
        />
        <WizardItem
          testid="colour"
          title="Colour vocabulary"
          kind="vocabulary"
          line={colourLine}
          loading={loading}
          state={stateOf(colourLine)}
          canDraft={canDraft}
          busy={busy}
          onCreate={(kind, payload) => createDraft(kind, payload, vocabularies.reload)}
          onOpen={(kind, id) => setParams({ kind, draft: id })}
          onSubmit={(line) => submitDraft(line, vocabularies.reload)}
          starting={() => ({
            dimension: COLOUR_DIMENSION,
            values: FASHION_COLOURS,
            effective_from: new Date().toISOString(),
          })}
        />
      </section>

      <section className="card section-card" data-testid="wiz-step-identity">
        <h4 className="h4">2 · What identifies a SKU</h4>
        <p className="lead">
          The starting draft allows every approved size and colour. Open it to narrow either list
          to the values this family actually uses — that choice is part of the draft, and the
          approver sees it.
        </p>
        <WizardItem
          testid="identity"
          title={`SKU identity profile · ${FASHION_FAMILY}`}
          kind="identity_profile"
          line={identityLine}
          loading={loading}
          state={stateOf(identityLine)}
          canDraft={canDraft}
          busy={busy}
          onCreate={(kind, payload) => createDraft(kind, payload, identity.reload)}
          onOpen={(kind, id) => setParams({ kind, draft: id })}
          onSubmit={(line) => submitDraft(line, identity.reload)}
          blocked={
            !vocabularyReady
              ? "Both vocabularies have to be approved and in force first — a profile may only name governed vocabulary."
              : valuesUnread?.failure
                ? `The approved values could not be read, and this draft would allow every one of them: ${valuesUnread.failure}`
                : valuesUnread?.loading
                  ? "Reading the approved values this profile would allow…"
                  : undefined
          }
          starting={() => ({
            family: FASHION_FAMILY,
            distinguishing_dimensions: [COLOUR_DIMENSION],
            size_dimension: SIZE_DIMENSION,
            colour_dimension: COLOUR_DIMENSION,
            grade_dimension: null,
            allowed_size_values: [],
            allowed_colour_values: [],
            allowed_grade_values: [],
          })}
        />
      </section>

      <section className="card section-card" data-testid="wiz-step-money">
        <h4 className="h4">3 · Rates and tax</h4>
        <WizardItem
          testid="rates"
          title="Rates"
          kind="rates"
          line={ratesLine}
          loading={loading}
          state={stateOf(ratesLine)}
          canDraft={canDraft}
          busy={busy}
          onCreate={(kind, payload) => createDraft(kind, payload, rates.reload)}
          onOpen={(kind, id) => setParams({ kind, draft: id })}
          onSubmit={(line) => submitDraft(line, rates.reload)}
          starting={() => ({ transport_pct: "0.00", pricing_margin_pct: "0.00" })}
        />
        <WizardItem
          testid="tax"
          title="Tax rules"
          kind="tax_rates"
          line={taxLine}
          loading={loading}
          state={stateOf(taxLine)}
          canDraft={canDraft}
          busy={busy}
          onCreate={(kind, payload) => createDraft(kind, payload, taxes.reload)}
          onOpen={(kind, id) => setParams({ kind, draft: id })}
          onSubmit={(line) => submitDraft(line, taxes.reload)}
          starting={() => ({
            currency: "INR",
            hsn_rules: [
              {
                hsn: "6109",
                effective_from: new Date().toISOString(),
                slabs: [
                  {
                    lower_paise: "0",
                    upper_paise: "100000",
                    lower_inclusive: true,
                    upper_inclusive: true,
                    input_pct: "5.00",
                    output_pct: "5.00",
                  },
                  {
                    lower_paise: "100000",
                    upper_paise: null,
                    lower_inclusive: false,
                    upper_inclusive: false,
                    input_pct: "12.00",
                    output_pct: "12.00",
                  },
                ],
              },
            ],
          })}
        />
      </section>

      <section className="card section-card" data-testid="wiz-step-columns">
        <h4 className="h4">4 · PT columns and direction</h4>
        <WizardItem
          testid="profile"
          title={`PT profile · ${FASHION_FAMILY}`}
          kind="profile"
          line={profileLine}
          loading={loading}
          state={stateOf(profileLine)}
          canDraft={canDraft}
          busy={busy}
          onCreate={(kind, payload) => createDraft(kind, payload, profiles.reload)}
          onOpen={(kind, id) => setParams({ kind, draft: id })}
          onSubmit={(line) => submitDraft(line, profiles.reload)}
          blocked={
            identityReady && moneyReady
              ? undefined
              : "A PT profile may only name approved rates and tax versions, and its family needs an approved identity profile."
          }
          starting={() => ({
            family: FASHION_FAMILY,
            columns: fashionColumns(),
            directions: ["base_to_ticket", "both_supplied"],
            allow_row_override: false,
            rates_version_id: ratesVersion?.version.id ?? "",
            tax_version_id: taxVersion?.version.id ?? "",
            opening_rules: { season_required: true, both_supplied: true },
          })}
        />
      </section>

      <WizardPreview
        profileLine={profileLine}
        ratesLine={ratesLine}
        taxLine={taxLine}
        identityLine={identityLine}
      />

      <section className="card section-card" data-testid="wiz-step-submit">
        <h4 className="h4">6 · Send it for approval</h4>
        {profileLine ? (
          <p className="lead" data-testid="wiz-submit-state">
            The PT profile is {stateWords(stateOf(profileLine)).toLowerCase()}. Open it to change a
            backdated start or review what it affects.
          </p>
        ) : (
          <p className="muted">There is no PT profile draft to send yet.</p>
        )}
      </section>
    </div>
  );
}

function WizardPreview({
  profileLine,
  ratesLine,
  taxLine,
  identityLine,
}: {
  profileLine: ResourceDTO<ConfigData> | undefined;
  ratesLine: ResourceDTO<ConfigData> | undefined;
  taxLine: ResourceDTO<ConfigData> | undefined;
  identityLine: ResourceDTO<ConfigData> | undefined;
}) {
  const columns = list<ColumnDraft>(profileLine?.data.payload.columns);
  const directions = list<string>(profileLine?.data.payload.directions);
  const rulesRaw = list<{ hsn: string; slabs: { lower_paise: string; upper_paise: string | null; input_pct: string; output_pct: string }[] }>(
    taxLine?.data.payload.hsn_rules,
  );
  const transport = str(ratesLine?.data.payload.transport_pct);
  const margin = str(ratesLine?.data.payload.pricing_margin_pct);

  return (
    <section className="card section-card" data-testid="wiz-step-preview">
      <h4 className="h4">5 · What this will do</h4>
      {!profileLine ? (
        <p className="muted" data-testid="wiz-preview-empty">
          Nothing to preview until there is a PT profile draft.
        </p>
      ) : (
        <>
          <p className="lead">
            SKUs of family <b>{str(identityLine?.data.payload.family) || "—"}</b>. Money is worked
            out{" "}
            {directions.length === 0
              ? "— no direction chosen yet"
              : directions
                  .map((d) => DIRECTIONS.find((entry) => entry.key === d)?.label ?? d)
                  .join("; ")}
            .
          </p>
          <p className="muted" data-testid="wiz-preview-rates">
            Transport {transport || "Unknown"}% · pricing margin {margin || "Unknown"}%
          </p>
          <div className="table-wrap">
            <table className="data" data-testid="wiz-preview-columns">
              <thead>
                <tr>
                  <th>Column</th>
                  <th>Kind</th>
                  <th>Where it comes from</th>
                  <th>Required</th>
                </tr>
              </thead>
              <tbody>
                {columns.map((col) => (
                  <tr key={col.id}>
                    <td className="mono">{col.label}</td>
                    <td>{col.logical_type}</td>
                    <td>{col.mode}</td>
                    <td>{col.required ? "Yes" : "No"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h5 className="h5">Tax slabs</h5>
          {rulesRaw.length === 0 ? (
            <p className="muted" data-testid="wiz-preview-no-tax">
              No HSN rule yet, so nothing can be priced.
            </p>
          ) : (
            <div className="table-wrap">
              <table className="data" data-testid="wiz-preview-tax">
                <thead>
                  <tr>
                    <th>HSN</th>
                    <th>From</th>
                    <th>Up to</th>
                    <th>Input</th>
                    <th>Output</th>
                  </tr>
                </thead>
                <tbody>
                  {rulesRaw.flatMap((rule, ruleIndex) =>
                    rule.slabs.map((slab, slabIndex) => (
                      <tr key={`${ruleIndex}-${slabIndex}`}>
                        <td className="mono">{rule.hsn}</td>
                        <td>{formatPaiseString(slab.lower_paise)}</td>
                        <td>
                          {/* A slab with no upper bound is open at the top —
                              that is a fact, not an unknown amount. */}
                          {slab.upper_paise === null
                            ? "No upper limit"
                            : formatPaiseString(slab.upper_paise)}
                        </td>
                        <td>{slab.input_pct}%</td>
                        <td>{slab.output_pct}%</td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}