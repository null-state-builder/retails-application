// One form per governed configuration kind (design §5.3 ConfigPayload).
//
// Each editor writes the exact closed payload its kind declares — the server
// refuses an unknown key rather than storing free-form JSON, so there is no
// "advanced" escape hatch here either. A kind with no form yet shows its
// payload read-only and says so, rather than offering an edit that cannot work.
//
// Money is integer paise on the wire. It is typed in rupees and converted by
// digit arithmetic (`rupeesToPaise`), never by multiplying a float, and read
// back with `formatPaiseString`, where an absent amount reads "Unknown" and
// never ₹0 (change PRD §14.4).
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Plus, Trash2 } from "lucide-react";

import { formatPaiseString, paiseStringToRupees, rupeesToPaise } from "../lib/format";
import { Field } from "../lib/goodsScreen";

export type Payload = Record<string, unknown>;

export interface EditorProps {
  value: Payload;
  onChange: (next: Payload) => void;
}

/** Percentages are decimal strings with at most two places (design §3.4). */
function PercentField({
  id,
  label,
  hint,
  value,
  onChange,
}: {
  id: string;
  label: string;
  hint?: string;
  value: string;
  onChange: (next: string) => void;
}) {
  const valid = /^\d{1,2}(\.\d{1,2})?$/.test(value);
  return (
    <Field
      id={id}
      label={label}
      hint={value && !valid ? "A percentage from 0 to 99.99, at most two decimals." : hint}
    >
      <input
        id={id}
        className="input"
        inputMode="decimal"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-testid={id}
      />
    </Field>
  );
}

/** An amount in rupees, held and sent as a base-10 integer-paise string.
 *
 *  The typed text is state of its own, so a half-written "12." survives the
 *  keystroke that made it: parsing on every change and writing the parse back
 *  would delete the decimal point as it is typed. Same rule, and the same
 *  reason, as `pages/sell/billing/RupeeInput.tsx`. Only text that is actually
 *  an amount reaches the payload (`rupeesToPaise`, ADR-0004 — never
 *  `Number(x) * 100`), and an emptied box means *unknown*, not nought. */
function MoneyField({
  id,
  label,
  hint,
  paise,
  onChange,
  allowUnknown,
}: {
  id: string;
  label: string;
  hint?: string;
  paise: string | null;
  onChange: (next: string | null) => void;
  allowUnknown?: boolean;
}) {
  const [text, setText] = useState(paiseStringToRupees(paise));
  const shown = useRef(paise);

  useEffect(() => {
    // Follow the payload when something other than this box moved the amount.
    if (paise === shown.current) return;
    shown.current = paise;
    setText(paiseStringToRupees(paise));
  }, [paise]);

  return (
    <Field
      id={id}
      label={label}
      hint={
        hint ??
        (paise === null
          ? allowUnknown
            ? "Open at this end — no limit."
            : "Unknown."
          : formatPaiseString(paise))
      }
    >
      <input
        id={id}
        className="input"
        inputMode="decimal"
        value={text}
        placeholder={allowUnknown ? "No upper limit" : "0.00"}
        onChange={(e) => {
          setText(e.target.value);
          if (e.target.value.trim() === "") {
            shown.current = allowUnknown ? null : "0";
            onChange(shown.current);
            return;
          }
          // Text that is not an amount yet ("12.") stays on screen and out of
          // the payload until it is one.
          const exact = rupeesToPaise(e.target.value);
          if (exact === null) return;
          shown.current = String(exact);
          onChange(shown.current);
        }}
        data-testid={id}
      />
    </Field>
  );
}

function list<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function text(value: unknown): string {
  return typeof value === "string"
    ? value
    : value === undefined || value === null
      ? ""
      : String(value);
}

// --------------------------------------------------------------------------

export function RatesEditor({ value, onChange }: EditorProps) {
  return (
    <div className="form-grid wide-form" data-testid="rates-editor">
      <PercentField
        id="rates-transport"
        label="Transport %"
        hint="Added to BASIC to reach the layer cost."
        value={text(value.transport_pct)}
        onChange={(next) => onChange({ ...value, transport_pct: next })}
      />
      <PercentField
        id="rates-margin"
        label="Pricing margin %"
        hint="The margin the MRP is priced at — not the margin a report displays."
        value={text(value.pricing_margin_pct)}
        onChange={(next) => onChange({ ...value, pricing_margin_pct: next })}
      />
    </div>
  );
}

interface Slab {
  lower_paise: string;
  upper_paise: string | null;
  lower_inclusive: boolean;
  upper_inclusive: boolean;
  input_pct: string;
  output_pct: string;
}

interface HsnRule {
  hsn: string;
  effective_from: string;
  effective_to?: string | null;
  slabs: Slab[];
}

const BLANK_SLAB: Slab = {
  lower_paise: "0",
  upper_paise: null,
  lower_inclusive: true,
  upper_inclusive: false,
  input_pct: "0.00",
  output_pct: "0.00",
};

export function TaxRatesEditor({ value, onChange }: EditorProps) {
  const rules = list<HsnRule>(value.hsn_rules);

  function setRules(next: HsnRule[]) {
    onChange({ ...value, currency: "INR", hsn_rules: next });
  }

  function setRule(index: number, patch: Partial<HsnRule>) {
    setRules(rules.map((rule, i) => (i === index ? { ...rule, ...patch } : rule)));
  }

  function setSlab(ruleIndex: number, slabIndex: number, patch: Partial<Slab>) {
    const rule = rules[ruleIndex];
    if (!rule) return;
    setRule(ruleIndex, {
      slabs: rule.slabs.map((slab, i) => (i === slabIndex ? { ...slab, ...patch } : slab)),
    });
  }

  return (
    <div data-testid="tax-editor">
      <p className="lead">
        One rule per HSN and period. Rules for one HSN may touch but never overlap, and neither may
        the slabs inside a rule.
      </p>
      {rules.map((rule, ruleIndex) => (
        <div className="card section-card" key={ruleIndex} data-testid={`tax-rule-${ruleIndex}`}>
          <div className="form-grid wide-form">
            <Field id={`tax-hsn-${ruleIndex}`} label="HSN">
              <input
                id={`tax-hsn-${ruleIndex}`}
                className="input"
                value={rule.hsn}
                onChange={(e) => setRule(ruleIndex, { hsn: e.target.value })}
                data-testid={`tax-hsn-${ruleIndex}`}
              />
            </Field>
            <Field
              id={`tax-from-${ruleIndex}`}
              label="In force from"
              hint="A rule with no time zone never applies, so this carries yours."
            >
              <input
                id={`tax-from-${ruleIndex}`}
                className="input"
                type="datetime-local"
                value={rule.effective_from.slice(0, 16)}
                onChange={(e) =>
                  setRule(ruleIndex, {
                    effective_from: new Date(e.target.value).toISOString(),
                  })
                }
                data-testid={`tax-from-${ruleIndex}`}
              />
            </Field>
          </div>
          <h5 className="h5">Slabs</h5>
          {rule.slabs.map((slab, slabIndex) => (
            <div className="form-grid wide-form" key={slabIndex}>
              <MoneyField
                id={`tax-lower-${ruleIndex}-${slabIndex}`}
                label="From (MRP)"
                paise={slab.lower_paise}
                onChange={(next) => setSlab(ruleIndex, slabIndex, { lower_paise: next ?? "0" })}
              />
              <MoneyField
                id={`tax-upper-${ruleIndex}-${slabIndex}`}
                label="Up to (MRP)"
                paise={slab.upper_paise}
                allowUnknown
                onChange={(next) => setSlab(ruleIndex, slabIndex, { upper_paise: next })}
              />
              <PercentField
                id={`tax-input-${ruleIndex}-${slabIndex}`}
                label="Input %"
                value={slab.input_pct}
                onChange={(next) => setSlab(ruleIndex, slabIndex, { input_pct: next })}
              />
              <PercentField
                id={`tax-output-${ruleIndex}-${slabIndex}`}
                label="Output %"
                value={slab.output_pct}
                onChange={(next) => setSlab(ruleIndex, slabIndex, { output_pct: next })}
              />
              <button
                className="btn btn-sm"
                onClick={() =>
                  setRule(ruleIndex, { slabs: rule.slabs.filter((_, i) => i !== slabIndex) })
                }
                data-testid={`tax-slab-remove-${ruleIndex}-${slabIndex}`}
              >
                <Trash2 size={13} /> Remove slab
              </button>
            </div>
          ))}
          <button
            className="btn btn-sm"
            onClick={() => setRule(ruleIndex, { slabs: [...rule.slabs, { ...BLANK_SLAB }] })}
            data-testid={`tax-slab-add-${ruleIndex}`}
          >
            <Plus size={13} /> Add slab
          </button>
          <button
            className="btn btn-sm"
            onClick={() => setRules(rules.filter((_, i) => i !== ruleIndex))}
            data-testid={`tax-rule-remove-${ruleIndex}`}
          >
            <Trash2 size={13} /> Remove rule
          </button>
        </div>
      ))}
      <button
        className="btn"
        onClick={() =>
          setRules([
            ...rules,
            {
              hsn: "",
              effective_from: new Date().toISOString(),
              slabs: [{ ...BLANK_SLAB }],
            },
          ])
        }
        data-testid="tax-rule-add"
      >
        <Plus size={15} /> Add an HSN rule
      </button>
    </div>
  );
}

interface VocabValue {
  value_key: string;
  label: string;
  sort_order: number;
  retired: boolean;
}

export function VocabularyEditor({ value, onChange }: EditorProps) {
  const values = list<VocabValue>(value.values);

  function setValues(next: VocabValue[]) {
    onChange({ ...value, values: next });
  }

  return (
    <div data-testid="vocabulary-editor">
      <div className="form-grid wide-form">
        <Field
          id="vocab-dimension"
          label="Dimension"
          hint="One vocabulary owns a dimension. Two in force for the same one is refused."
        >
          <input
            id="vocab-dimension"
            className="input"
            value={text(value.dimension)}
            onChange={(e) => onChange({ ...value, dimension: e.target.value })}
            data-testid="vocab-dimension"
          />
        </Field>
      </div>
      <p className="lead">
        A published value key can never be dropped — retire it instead, so everything that already
        used it still reads.
      </p>
      <div className="table-wrap">
        <table className="data" data-testid="vocab-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Label</th>
              <th>Order</th>
              <th>Retired</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {values.map((entry, index) => (
              <tr key={index}>
                <td>
                  <input
                    aria-label={`Value key ${index + 1}`}
                    className="input"
                    value={entry.value_key}
                    onChange={(e) =>
                      setValues(
                        values.map((v, i) =>
                          i === index ? { ...v, value_key: e.target.value } : v,
                        ),
                      )
                    }
                    data-testid={`vocab-key-${index}`}
                  />
                </td>
                <td>
                  <input
                    aria-label={`Label for value ${index + 1}`}
                    className="input"
                    value={entry.label}
                    onChange={(e) =>
                      setValues(
                        values.map((v, i) => (i === index ? { ...v, label: e.target.value } : v)),
                      )
                    }
                    data-testid={`vocab-label-${index}`}
                  />
                </td>
                <td>
                  <input
                    aria-label={`Sort order for value ${index + 1}`}
                    className="input"
                    type="number"
                    value={entry.sort_order}
                    onChange={(e) =>
                      setValues(
                        values.map((v, i) =>
                          i === index ? { ...v, sort_order: Number(e.target.value) } : v,
                        ),
                      )
                    }
                    data-testid={`vocab-order-${index}`}
                  />
                </td>
                <td>
                  <input
                    aria-label={`Retire value ${index + 1}`}
                    type="checkbox"
                    checked={entry.retired}
                    onChange={(e) =>
                      setValues(
                        values.map((v, i) =>
                          i === index ? { ...v, retired: e.target.checked } : v,
                        ),
                      )
                    }
                    data-testid={`vocab-retired-${index}`}
                  />
                </td>
                <td>
                  <button
                    className="btn btn-sm"
                    onClick={() => setValues(values.filter((_, i) => i !== index))}
                    data-testid={`vocab-remove-${index}`}
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
        className="btn"
        onClick={() =>
          setValues([
            ...values,
            { value_key: "", label: "", sort_order: values.length, retired: false },
          ])
        }
        data-testid="vocab-add"
      >
        <Plus size={15} /> Add a value
      </button>
    </div>
  );
}

interface ReasonCode {
  code: string;
  label: string;
  retired: boolean;
}

export function ReasonsEditor({ value, onChange }: EditorProps) {
  const codes = list<ReasonCode>(value.codes);
  return (
    <div data-testid="reasons-editor">
      <div className="form-grid wide-form">
        <Field
          id="reasons-action"
          label="Action"
          hint="The registered action these reasons belong to."
        >
          <input
            id="reasons-action"
            className="input"
            value={text(value.action)}
            onChange={(e) => onChange({ ...value, action: e.target.value })}
            data-testid="reasons-action"
          />
        </Field>
      </div>
      <div className="table-wrap">
        <table className="data" data-testid="reasons-table">
          <thead>
            <tr>
              <th>Code</th>
              <th>What it says</th>
              <th>Retired</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {codes.map((entry, index) => (
              <tr key={index}>
                <td>
                  <input
                    aria-label={`Reason code ${index + 1}`}
                    className="input"
                    value={entry.code}
                    onChange={(e) =>
                      onChange({
                        ...value,
                        codes: codes.map((c, i) =>
                          i === index ? { ...c, code: e.target.value } : c,
                        ),
                      })
                    }
                    data-testid={`reason-code-${index}`}
                  />
                </td>
                <td>
                  <input
                    aria-label={`Reason label ${index + 1}`}
                    className="input"
                    value={entry.label}
                    onChange={(e) =>
                      onChange({
                        ...value,
                        codes: codes.map((c, i) =>
                          i === index ? { ...c, label: e.target.value } : c,
                        ),
                      })
                    }
                    data-testid={`reason-label-${index}`}
                  />
                </td>
                <td>
                  <input
                    aria-label={`Retire reason ${index + 1}`}
                    type="checkbox"
                    checked={entry.retired}
                    onChange={(e) =>
                      onChange({
                        ...value,
                        codes: codes.map((c, i) =>
                          i === index ? { ...c, retired: e.target.checked } : c,
                        ),
                      })
                    }
                    data-testid={`reason-retired-${index}`}
                  />
                </td>
                <td>
                  <button
                    className="btn btn-sm"
                    onClick={() =>
                      onChange({ ...value, codes: codes.filter((_, i) => i !== index) })
                    }
                    data-testid={`reason-remove-${index}`}
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
        className="btn"
        onClick={() =>
          onChange({ ...value, codes: [...codes, { code: "", label: "", retired: false }] })
        }
        data-testid="reason-add"
      >
        <Plus size={15} /> Add a reason
      </button>
    </div>
  );
}

export function SeriesEditor({
  value,
  onChange,
  entities,
}: EditorProps & { entities: { id: string; name: string }[] }) {
  return (
    <div className="form-grid wide-form" data-testid="series-editor">
      <Field id="series-entity" label="Legal entity">
        <select
          id="series-entity"
          className="select"
          value={text(value.entity_id)}
          onChange={(e) => onChange({ ...value, entity_id: e.target.value })}
          data-testid="series-entity"
        >
          <option value="">— choose —</option>
          {entities.map((entity) => (
            <option key={entity.id} value={entity.id}>
              {entity.name}
            </option>
          ))}
        </select>
      </Field>
      <Field id="series-type" label="Document type">
        <input
          id="series-type"
          className="input"
          value={text(value.type)}
          onChange={(e) => onChange({ ...value, type: e.target.value })}
          data-testid="series-type"
        />
      </Field>
      <Field id="series-fy" label="Financial year" hint="Written like 26-27.">
        <input
          id="series-fy"
          className="input"
          value={text(value.fy)}
          onChange={(e) => onChange({ ...value, fy: e.target.value })}
          data-testid="series-fy"
        />
      </Field>
      <Field
        id="series-block"
        label="Numbers reserved at a time"
        hint="How many numbers the series claims in one go."
      >
        <input
          id="series-block"
          className="input"
          type="number"
          value={Number(value.ceiling_block_size ?? 1000)}
          onChange={(e) => onChange({ ...value, ceiling_block_size: Number(e.target.value) })}
          data-testid="series-block"
        />
      </Field>
    </div>
  );
}

export function LabelEditor({ value, onChange }: EditorProps) {
  const fields = list<string>(value.fields);
  return (
    <div data-testid="label-editor">
      <div className="form-grid wide-form">
        <Field id="label-width" label="Width (mm)">
          <input
            id="label-width"
            className="input"
            type="number"
            value={Number(value.width_mm ?? 38)}
            onChange={(e) => onChange({ ...value, width_mm: Number(e.target.value) })}
            data-testid="label-width"
          />
        </Field>
        <Field id="label-height" label="Height (mm)">
          <input
            id="label-height"
            className="input"
            type="number"
            value={Number(value.height_mm ?? 25)}
            onChange={(e) => onChange({ ...value, height_mm: Number(e.target.value) })}
            data-testid="label-height"
          />
        </Field>
        <Field id="label-dpi" label="Printer DPI">
          <select
            id="label-dpi"
            className="select"
            value={Number(value.printer_dpi ?? 203)}
            onChange={(e) => onChange({ ...value, printer_dpi: Number(e.target.value) })}
            data-testid="label-dpi"
          >
            {[203, 300, 600].map((dpi) => (
              <option key={dpi} value={dpi}>
                {dpi}
              </option>
            ))}
          </select>
        </Field>
        <Field
          id="label-module"
          label="Narrowest bar (dots)"
          hint="A layout that would need a thinner bar than this is refused, never shrunk."
        >
          <input
            id="label-module"
            className="input"
            type="number"
            value={Number(value.min_module_dots ?? 2)}
            onChange={(e) => onChange({ ...value, min_module_dots: Number(e.target.value) })}
            data-testid="label-module"
          />
        </Field>
        <Field id="label-copies" label="Most copies in one job">
          <input
            id="label-copies"
            className="input"
            type="number"
            value={Number(value.copies_limit ?? 100)}
            onChange={(e) => onChange({ ...value, copies_limit: Number(e.target.value) })}
            data-testid="label-copies"
          />
        </Field>
        <Field id="label-payload" label="Longest code the symbol holds">
          <input
            id="label-payload"
            className="input"
            type="number"
            value={Number(value.max_payload_characters ?? 32)}
            onChange={(e) => onChange({ ...value, max_payload_characters: Number(e.target.value) })}
            data-testid="label-payload"
          />
        </Field>
        <Field
          id="label-fields"
          label="What the label shows"
          hint="Comma separated, in the order they print."
        >
          <input
            id="label-fields"
            className="input"
            value={fields.join(", ")}
            onChange={(e) =>
              onChange({
                ...value,
                fields: e.target.value
                  .split(",")
                  .map((part) => part.trim())
                  .filter(Boolean),
              })
            }
            data-testid="label-fields"
          />
        </Field>
      </div>
    </div>
  );
}

export function BarcodeRangeEditor({ value, onChange }: EditorProps) {
  return (
    <div className="form-grid wide-form" data-testid="range-editor">
      <Field id="range-issuer" label="Issuer">
        <input
          id="range-issuer"
          className="input"
          value={text(value.issuer)}
          onChange={(e) => onChange({ ...value, issuer: e.target.value })}
          data-testid="range-issuer"
        />
      </Field>
      <Field id="range-prefix" label="Prefix">
        <input
          id="range-prefix"
          className="input"
          value={text(value.prefix)}
          onChange={(e) => onChange({ ...value, prefix: e.target.value })}
          data-testid="range-prefix"
        />
      </Field>
      <Field id="range-start" label="First number">
        <input
          id="range-start"
          className="input"
          type="number"
          value={Number(value.start ?? 0)}
          onChange={(e) => onChange({ ...value, start: Number(e.target.value) })}
          data-testid="range-start"
        />
      </Field>
      <Field id="range-end" label="Last number">
        <input
          id="range-end"
          className="input"
          type="number"
          value={Number(value.end ?? 0)}
          onChange={(e) => onChange({ ...value, end: Number(e.target.value) })}
          data-testid="range-end"
        />
      </Field>
    </div>
  );
}

/** A kind with no form on this screen yet. Its payload is shown as it is, and
 *  the screen says plainly that it cannot be edited here — better than an edit
 *  box that would be refused. */
export function ReadOnlyPayload({ value, kind }: { value: Payload; kind: string }): ReactNode {
  return (
    <div data-testid="payload-readonly">
      <p className="warn-note">
        This screen has no form for {kind} configuration yet, so it is shown as it stands and cannot
        be changed here.
      </p>
      <pre className="mono" style={{ overflowX: "auto", fontSize: 12 }}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}
