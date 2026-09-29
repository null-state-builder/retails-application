// The PT grid (OPS-16): the pure rules the spreadsheet-style PT editor in
// `pages/PtPrepare.tsx` runs on. Store and warehouse operations PRD §5.4 and
// GSA-T06.
//
// Nothing here decides a value. The server resolves every cell (`cells`),
// every calculation and every issue; this module only says which of the
// twenty-two KDPS columns a cell is, how a reader should read its colour,
// which rows a bulk fill touches, how a local edit becomes an E124 row edit,
// and why submit is not yet possible.

import type { operations } from "./api-schema";
import type { PtLine, PtRowIssue } from "./goodsPt";

/** E124's request body and one row edit in it, as the generated client states them. */
type RowsBody = NonNullable<
  operations["goods_v1_ptmapper_files_rows_partial_update"]["requestBody"]
>["content"]["application/json"];
export type PtRowEdit = NonNullable<RowsBody["updates"]>[number];
/** The pasted cells of one row edit (ticket 06A). */
export type CanonicalCells = NonNullable<PtRowEdit["canonical"]>;

// ---------------------------------------------------------------------------
// Columns
// ---------------------------------------------------------------------------

/** The twenty-two KDPS PT columns, in the order of the canonical PT file. */
export const KDPS_COLUMNS = [
  "SEASON",
  "BRAND",
  "COLOR",
  "GENDER",
  "SUB CATEGORY",
  "TYPE",
  "ITEM",
  "FIT",
  "SIZE",
  "BARCODE",
  "DESIGN",
  "HSN",
  "QTY",
  "MRP",
  "BASIC",
  "P RATE",
  "INPUT TAX",
  "OUTPUT TAX",
  "NAG",
  "MARGIN",
  "SUGGESTED SUB CATEGORY",
  "SUGGESTED TYPE",
] as const;

export type ColumnName = (typeof KDPS_COLUMNS)[number];

/** How a column is edited. `calculated` columns are drawn, never typed into:
 *  the server works them out from the approved profile. */
export type ColumnKind = "season" | "brand" | "attribute" | "text" | "qty" | "money" | "calculated";

export interface GridColumn {
  name: ColumnName;
  /** The key the server's resolved `cells` carry this column's value under. */
  cell: string;
  kind: ColumnKind;
  /** The vocabulary dimension of a describing column (a mapping rule's kind). */
  dimension?: string;
  /** The E124 `column_key` a person's edit of this column is sent as. Attribute
   *  columns are all sent together as `attributes`. */
  edit?: string;
  /** Shown only to someone the server lets see cost. */
  cost?: boolean;
  /** Integer-paise money, as opposed to a percentage or a count. */
  money?: boolean;
  numeric?: boolean;
}

export const GRID_COLUMNS: readonly GridColumn[] = [
  { name: "SEASON", cell: "season", kind: "season", dimension: "season", edit: "season_id" },
  { name: "BRAND", cell: "brand", kind: "brand", edit: "brand_id" },
  { name: "COLOR", cell: "colour", kind: "attribute", dimension: "colour" },
  { name: "GENDER", cell: "gender", kind: "attribute", dimension: "gender" },
  { name: "SUB CATEGORY", cell: "sub_category", kind: "attribute", dimension: "sub_category" },
  { name: "TYPE", cell: "type", kind: "attribute", dimension: "type" },
  { name: "ITEM", cell: "item", kind: "attribute", dimension: "item" },
  { name: "FIT", cell: "fit", kind: "attribute", dimension: "fit" },
  { name: "SIZE", cell: "size", kind: "attribute", dimension: "size" },
  { name: "BARCODE", cell: "alias_as_used", kind: "text", edit: "alias_as_used" },
  { name: "DESIGN", cell: "design", kind: "text", edit: "design" },
  { name: "HSN", cell: "hsn", kind: "text", edit: "hsn" },
  { name: "QTY", cell: "qty", kind: "qty", edit: "qty", numeric: true },
  { name: "MRP", cell: "mrp_paise", kind: "money", edit: "mrp_paise", money: true, numeric: true },
  {
    name: "BASIC",
    cell: "basic_paise",
    kind: "money",
    edit: "basic_paise",
    cost: true,
    money: true,
    numeric: true,
  },
  {
    name: "P RATE",
    cell: "p_rate_paise",
    kind: "calculated",
    cost: true,
    money: true,
    numeric: true,
  },
  { name: "INPUT TAX", cell: "input_tax_pct", kind: "calculated", numeric: true },
  { name: "OUTPUT TAX", cell: "output_tax_pct", kind: "calculated", numeric: true },
  { name: "NAG", cell: "nag", kind: "calculated", numeric: true },
  { name: "MARGIN", cell: "margin_pct", kind: "calculated", cost: true, numeric: true },
  { name: "SUGGESTED SUB CATEGORY", cell: "suggested_sub_category", kind: "calculated" },
  { name: "SUGGESTED TYPE", cell: "suggested_type", kind: "calculated" },
];

export function columnByName(name: ColumnName): GridColumn {
  return GRID_COLUMNS.find((c) => c.name === name)!;
}

/** The columns to draw: all twenty-two, less the cost columns when the server
 *  sent no cost (it leaves those cells out rather than blanking them). */
export function visibleColumns(showsCost: boolean): GridColumn[] {
  return GRID_COLUMNS.filter((c) => showsCost || !c.cost);
}

/** Whether this reader sees cost: the server includes BASIC in `cells` only then. */
export function showsCost(lines: GridLine[]): boolean {
  return lines.some((line) => line.cells !== undefined && "basic_paise" in line.cells);
}

/** Describing columns that take a dropdown of approved values. */
export function isDropdown(column: GridColumn): boolean {
  return column.kind === "season" || column.kind === "brand" || column.kind === "attribute";
}

export function isEditable(column: GridColumn): boolean {
  return column.kind !== "calculated";
}

/** A stable, space-free spelling of a column for test ids and DOM ids. */
export function columnSlug(name: ColumnName): string {
  return name.replace(/\s+/g, "_");
}

// ---------------------------------------------------------------------------
// The line the grid reads
// ---------------------------------------------------------------------------

export type Origin = "file" | "rule" | "suggestion" | "person" | "none";

export interface SuggestionChoice {
  value_id: string;
  value: string;
  label: string;
  reason: string;
}

export interface AttributeEntry {
  field_id: string;
  vocabulary_value_id?: string | null;
  supplied_text?: string | null;
  unknown?: boolean;
}

/** A PT line as E099 answers it for the grid (OPS-15 intake fields, OPS-16 cells). */
export interface GridLine extends PtLine {
  origins?: Partial<Record<string, Origin>>;
  suggestions?: Partial<Record<string, { source: string; choices: SuggestionChoice[] }>>;
  describing?: { brand_id?: number | null; design?: string | null };
  match?: {
    by?: "barcode" | "describing_values" | "new_item" | "ambiguous" | "person" | null;
    candidates?: string[];
    notes?: { code: string; message: string; field?: string }[];
  };
  /** Each KDPS column's value as the server resolves it, keyed by `GridColumn.cell`. */
  cells?: Record<string, string | number | null>;
  /** The line's item is a proposal waiting for the product-master owner. */
  item_pending?: boolean;
  /** The line's item was a proposal the product-master owner rejected (OPS-17A). */
  item_rejected?: { reason_code: string } | null;
}

// ---------------------------------------------------------------------------
// Local edits
// ---------------------------------------------------------------------------

/** One row's unsaved edits, by column. The value is what that column's edit
 *  carries: a vocabulary value id, a brand or season id, typed text, integer
 *  paise text, or `null` for "clear it". `SKU` is the item a person proposed
 *  for a new-item row. */
export type RowEdits = Partial<Record<ColumnName | "SKU", string | null>>;
export type Edits = Record<string, RowEdits>;

export function setEdit(
  edits: Edits,
  lineKey: string,
  column: ColumnName | "SKU",
  value: string | null,
): Edits {
  return { ...edits, [lineKey]: { ...edits[lineKey], [column]: value } };
}

export function hasEdit(edits: Edits, lineKey: string, column: ColumnName | "SKU"): boolean {
  const row = edits[lineKey];
  return row !== undefined && column in row;
}

export function isDirty(
  edits: Edits,
  pendingReviews: Record<string, string>,
  pastes: Pastes = {},
): boolean {
  return (
    Object.values(edits).some((row) => Object.keys(row).length > 0) ||
    Object.keys(pendingReviews).length > 0 ||
    hasPastes(pastes)
  );
}

/** Review marks waiting to be saved, less the rows that just changed: a changed
 *  row loses its review mark (GSA-T06), locally as the server does on save. */
export function clearReviewMarks(
  pending: Record<string, string>,
  changed: Iterable<string>,
): Record<string, string> {
  const next = { ...pending };
  let touched = false;
  for (const key of changed) {
    if (key in next) {
      delete next[key];
      touched = true;
    }
  }
  return touched ? next : pending;
}

export interface FieldEdit {
  column_key: string;
  value: unknown;
}

/** One row's edits as an E124 `{line_key, fields}` update. Attribute columns go
 *  together as the row's `attributes`: the line's own list with each edited
 *  dimension replaced (or dropped, when cleared). */
export function rowUpdate(
  line: GridLine,
  row: RowEdits,
): { line_key: string; fields: FieldEdit[] } {
  const fields: FieldEdit[] = [];
  let attributes: AttributeEntry[] | null = null;
  for (const [name, value] of Object.entries(row) as [ColumnName | "SKU", string | null][]) {
    if (name === "SKU") {
      fields.push({ column_key: "sku_id", value });
      continue;
    }
    const column = columnByName(name);
    if (column.kind === "attribute") {
      attributes ??= [...((line.attributes ?? []) as AttributeEntry[])];
      attributes = attributes.filter((entry) => entry.field_id !== column.dimension);
      if (value) {
        attributes.push({
          field_id: column.dimension!,
          vocabulary_value_id: value,
          unknown: false,
        });
      }
      continue;
    }
    if (!column.edit) continue;
    fields.push({ column_key: column.edit, value: editValue(column, value) });
  }
  if (attributes !== null) fields.push({ column_key: "attributes", value: attributes });
  return { line_key: line.line_key, fields };
}

function editValue(column: GridColumn, value: string | null): unknown {
  if (value === null) return null;
  const text = value.trim();
  if (column.kind === "brand") return text === "" ? null : Number(text);
  if (column.kind === "qty") {
    if (text === "") return null;
    return /^\d+$/.test(text) ? Number(text) : text;
  }
  return text === "" ? null : text;
}

/** Every row with edits, as the E124 `updates` list. */
export function buildUpdates(lines: GridLine[], edits: Edits) {
  return lines
    .filter((line) => Object.keys(edits[line.line_key] ?? {}).length > 0)
    .map((line) => rowUpdate(line, edits[line.line_key] ?? {}));
}

// ---------------------------------------------------------------------------
// Where a value came from, as a colour
// ---------------------------------------------------------------------------

/** `given`: from the file, a rule or the item (grey). `waiting`: a suggestion
 *  waits for a person (amber). `person`: a person's own choice (blue).
 *  `problem`: blank or wrong (red). `calculated`: the server's own working. */
export type Tone = "given" | "waiting" | "person" | "problem" | "calculated";

export interface ToneInput {
  column: GridColumn;
  origin?: Origin;
  /** A person changed this cell locally and has not saved yet. */
  edited: boolean;
  blank: boolean;
  /** The server named something wrong with this cell. */
  problem: boolean;
  /** Close matches are waiting on this cell. */
  suggested: boolean;
  /** A refused save named this cell's unsaved value (ticket 06A). */
  refused?: boolean;
}

export function cellTone({
  column,
  origin,
  edited,
  blank,
  problem,
  suggested,
  refused = false,
}: ToneInput): Tone {
  // A calculated column is never typed into; it is `edited` only when a person
  // pasted a check value into it (P RATE, ticket 06A), which is theirs until saved.
  if (column.kind === "calculated" && !edited) return problem ? "problem" : "calculated";
  // A local edit supersedes what the server last said about the saved cell -
  // unless the save of that very edit was refused on it.
  if (edited) return blank || refused ? "problem" : "person";
  if (problem) return "problem";
  if (origin === "suggestion" && suggested) return "waiting";
  if (blank) return "problem";
  if (origin === "person") return "person";
  return "given";
}

/** Which column a server issue is about. Anything that is about the row as a
 *  whole (its item, its coverage) has none. */
const FIELD_COLUMN: Record<string, ColumnName> = {
  season_id: "SEASON",
  brand_id: "BRAND",
  alias_as_used: "BARCODE",
  design: "DESIGN",
  hsn: "HSN",
  qty: "QTY",
  mrp_paise: "MRP",
  check_mrp_paise: "MRP",
  basic_paise: "BASIC",
  check_p_rate_paise: "P RATE",
  p_rate_paise: "P RATE",
  input_tax_pct: "INPUT TAX",
  output_tax_pct: "OUTPUT TAX",
  margin_pct: "MARGIN",
};

export function issueColumn(field: string | null | undefined): ColumnName | null {
  if (!field) return null;
  // A refused pasted cell names its column by its KDPS name (ticket 06A).
  if ((KDPS_COLUMNS as readonly string[]).includes(field)) return field as ColumnName;
  const bare = field.startsWith("attrs.") ? field.slice(6) : field;
  if (bare in FIELD_COLUMN) return FIELD_COLUMN[bare] ?? null;
  const byDimension = GRID_COLUMNS.find((c) => c.kind === "attribute" && c.dimension === bare);
  return byDimension ? byDimension.name : null;
}

export interface SortedIssues {
  cells: Partial<Record<ColumnName, PtRowIssue[]>>;
  row: PtRowIssue[];
}

export function sortIssues(issues: PtRowIssue[] | undefined): SortedIssues {
  const out: SortedIssues = { cells: {}, row: [] };
  for (const found of issues ?? []) {
    const column = issueColumn(found.field);
    if (column) (out.cells[column] ??= []).push(found);
    else out.row.push(found);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Filling a column
// ---------------------------------------------------------------------------

export type FillScope = "blank" | "all";

/** What one row shows in the column being filled: its value as read (a label),
 *  blank when it has none. */
export interface FillRow {
  key: string;
  current: string | null;
}

function blankText(value: string | null | undefined): boolean {
  return value === null || value === undefined || String(value).trim() === "";
}

/** The rows a fill changes: every row or blank rows only, never a row that
 *  already shows the value (it would lose its review mark for nothing). */
export function fillTargets(rows: FillRow[], target: string, scope: FillScope): string[] {
  const wanted = target.trim().toUpperCase();
  return rows
    .filter((row) => scope === "all" || blankText(row.current))
    .filter((row) => (row.current ?? "").trim().toUpperCase() !== wanted)
    .map((row) => row.key);
}

export function applyFill(edits: Edits, keys: string[], column: ColumnName, value: string): Edits {
  let next = edits;
  for (const key of keys) next = setEdit(next, key, column, value);
  return next;
}

export function blankCount(rows: FillRow[]): number {
  return rows.filter((row) => blankText(row.current)).length;
}

// ---------------------------------------------------------------------------
// Vocabulary choices and quick fills
// ---------------------------------------------------------------------------

export interface Choice {
  id: string;
  /** The value's key (a vocabulary value key, a brand or season code). */
  value: string;
  label: string;
}

function squash(text: string): string {
  return text.replace(/[\s_-]+/g, "").toUpperCase();
}

/** The approved choice a quick fill names, by key or label, ignoring case and
 *  spacing (`FREE SIZE`, `Free Size`, `FREESIZE`); none when the approved list
 *  does not hold it - a quick fill never invents a value. */
export function findChoice(choices: Choice[], word: string): Choice | undefined {
  const wanted = squash(word);
  return choices.find((c) => squash(c.value) === wanted || squash(c.label) === wanted);
}

/** The price-tier colours and the size a quick fill offers (the old PT Files
 *  screen's quick fills, carried into the grid). */
export const COLOUR_TIERS = ["PREMIUM", "MEDIUM", "ECONOMY"] as const;
export const FREE_SIZE = "FREE SIZE";

/** A suggestion accepted on a cell: the edit value it becomes. A vocabulary or
 *  brand suggestion is its own id; a season suggestion names a vocabulary
 *  season, and a PT line holds a Season master row, so it becomes the one
 *  season row with that code or name - or nothing, when that is not exactly one. */
export function acceptedValue(
  column: GridColumn,
  choice: SuggestionChoice,
  seasons: Choice[],
): string | null {
  if (column.kind !== "season") return choice.value_id;
  const names = new Set([choice.value, choice.label].map((t) => t.trim().toUpperCase()));
  const found = seasons.filter(
    (s) => names.has(s.value.trim().toUpperCase()) || names.has(s.label.trim().toUpperCase()),
  );
  return found.length === 1 ? (found[0]?.id ?? null) : null;
}

/** Columns a mapping rule can be proposed from: the ones read straight from the
 *  brand file's own column. ITEM, SUB CATEGORY and TYPE are read from words in
 *  the description, whose rules are keywords, not a cell's own text. */
export const RULE_COLUMNS: readonly ColumnName[] = ["SEASON", "COLOR", "GENDER", "FIT", "SIZE"];

/** A rule may be proposed from a cell whose file text matched no rule: a
 *  suggestion waiting, or a blank a person has not filled, on a brand-file row. */
export function canProposeRule(column: GridColumn, line: GridLine): boolean {
  if (!RULE_COLUMNS.includes(column.name) || !line.origins) return false;
  const origin = line.origins[column.name];
  return origin === "suggestion" || origin === "none" || origin === undefined;
}

// ---------------------------------------------------------------------------
// The row's item and the submit gate
// ---------------------------------------------------------------------------

export type ItemStatus =
  | "matched"
  | "new_item"
  | "proposed"
  | "waiting"
  | "rejected"
  | "ambiguous"
  | "none";

export function itemStatus(line: GridLine, row?: RowEdits): ItemStatus {
  if (row && "SKU" in row && row.SKU) return "proposed";
  if (line.item_pending) return "waiting";
  if (line.item_rejected) return "rejected";
  if (line.sku_id) return "matched";
  if (line.match?.by === "new_item") return "new_item";
  if (line.match?.by === "ambiguous") return "ambiguous";
  return "none";
}

/** Why submit cannot run yet, in the preparer's words. Empty means the server
 *  may be asked; it still decides (reconciliation, prices, every row issue). */
export function submitBlockers(lines: GridLine[], dirty: boolean): string[] {
  const out: string[] = [];
  if (dirty) out.push("Save your changes first: submit sends the saved PT.");
  if (lines.length === 0) out.push("The PT has no rows.");
  const count = (test: (line: GridLine) => boolean) => lines.filter(test).length;
  const unreviewed = count((line) => !line.reviewed);
  if (unreviewed > 0) out.push(`${unreviewed} row(s) still to review.`);
  const proposeable = count((line) => itemStatus(line) === "new_item");
  if (proposeable > 0) {
    out.push(`${proposeable} new item(s) to propose: press Propose item on the row.`);
  }
  const waiting = count((line) => itemStatus(line) === "waiting");
  if (waiting > 0) {
    out.push(
      `${waiting} new item(s) waiting for the product-master owner's confirmation. The PT can be submitted once they are confirmed.`,
    );
  }
  const rejected = count((line) => itemStatus(line) === "rejected");
  if (rejected > 0) {
    out.push(
      `${rejected} new item(s) rejected by the product-master owner: give the row another item.`,
    );
  }
  const unnamed = count((line) => ["ambiguous", "none"].includes(itemStatus(line)));
  if (unnamed > 0) out.push(`${unnamed} row(s) name no item yet.`);
  return out;
}

// ---------------------------------------------------------------------------
// Review on the current page
// ---------------------------------------------------------------------------

export const PAGE_SIZE = 50;

export function pageCount(total: number, size = PAGE_SIZE): number {
  return Math.max(1, Math.ceil(total / size));
}

export function pageSlice<T>(rows: T[], page: number, size = PAGE_SIZE): T[] {
  return rows.slice(page * size, page * size + size);
}

/** The review marks "Review selected rows on this page" records: selected rows
 *  of this page only, never one with unsaved edits (its hash is about to
 *  change), never one already reviewed, never one the server gave no hash. */
export function reviewMarks(
  pageLines: GridLine[],
  selected: Record<string, boolean>,
  edits: Edits,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of pageLines) {
    if (!selected[line.line_key] || line.reviewed || !line.row_hash) continue;
    if (Object.keys(edits[line.line_key] ?? {}).length > 0) continue;
    out[line.line_key] = line.row_hash;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Keyboard
// ---------------------------------------------------------------------------

export interface GridPos {
  row: number;
  col: number;
}

/** Where a key moves the cursor, as in a spreadsheet: arrows move one cell;
 *  Tab and Shift+Tab move across, wrapping to the next or previous row; Enter
 *  and Shift+Enter move down and up. `null` for a key the grid leaves alone. */
export function nextPosition(
  pos: GridPos,
  key: string,
  shift: boolean,
  rows: number,
  cols: number,
): GridPos | null {
  if (rows === 0 || cols === 0) return null;
  const clampRow = (r: number) => Math.min(rows - 1, Math.max(0, r));
  const clampCol = (c: number) => Math.min(cols - 1, Math.max(0, c));
  switch (key) {
    case "ArrowUp":
      return { row: clampRow(pos.row - 1), col: pos.col };
    case "ArrowDown":
      return { row: clampRow(pos.row + 1), col: pos.col };
    case "ArrowLeft":
      return { row: pos.row, col: clampCol(pos.col - 1) };
    case "ArrowRight":
      return { row: pos.row, col: clampCol(pos.col + 1) };
    case "Enter":
      return { row: clampRow(pos.row + (shift ? -1 : 1)), col: pos.col };
    case "Tab": {
      if (shift) {
        if (pos.col > 0) return { row: pos.row, col: pos.col - 1 };
        return pos.row > 0 ? { row: pos.row - 1, col: cols - 1 } : pos;
      }
      if (pos.col < cols - 1) return { row: pos.row, col: pos.col + 1 };
      return pos.row < rows - 1 ? { row: pos.row + 1, col: 0 } : pos;
    }
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// Pasting canonical values (ticket 06A)
// ---------------------------------------------------------------------------

/** The columns a paste fills: the seven a canonical PT workbook is read from.
 *  The server reads the pasted text exactly as it reads an uploaded workbook's
 *  cells; this module only works out which row and column each cell lands on. */
export const PASTE_COLUMNS: readonly ColumnName[] = [
  "SEASON",
  "BARCODE",
  "HSN",
  "QTY",
  "MRP",
  "BASIC",
  "P RATE",
];

/** Pasted cells not yet saved: the text as pasted, by row and column. */
export type Pastes = Record<string, Partial<Record<ColumnName, string>>>;

export function hasPastes(pastes: Pastes): boolean {
  return Object.values(pastes).some((row) => Object.keys(row).length > 0);
}

export function isPasteColumn(name: ColumnName): boolean {
  return PASTE_COLUMNS.includes(name);
}

/** Spreadsheet clipboard text as rows of cells: tabs between cells, a line per
 *  row, the one trailing line break a spreadsheet adds dropped. */
export function parseClipboard(text: string): string[][] {
  const rows = text.replace(/\r\n?/g, "\n").split("\n");
  if (rows.length > 1 && rows[rows.length - 1] === "") rows.pop();
  return rows.map((row) => row.split("\t"));
}

/** Whether clipboard text is a block (several cells) rather than one value. */
export function isBlock(text: string): boolean {
  return /[\t\n]/.test(text.replace(/\r?\n$/, ""));
}

function kdpsName(text: string): ColumnName | null {
  const wanted = text.replace(/\s+/g, " ").trim().toUpperCase();
  return (KDPS_COLUMNS as readonly string[]).includes(wanted) ? (wanted as ColumnName) : null;
}

/** A first row made only of KDPS column names is a header: the columns are
 *  then taken from it, wherever the paste started. */
function headerOf(row: string[]): (ColumnName | null)[] | null {
  const filled = row.filter((cell) => cell.trim() !== "");
  if (filled.length === 0 || !filled.every((cell) => kdpsName(cell))) return null;
  return row.map((cell) => (cell.trim() === "" ? null : kdpsName(cell)));
}

export interface PastePlan {
  cells: Pastes;
  rows: number;
  /** Columns that held text a paste does not fill (a describing value, a
   *  calculated one, or cost the reader cannot see). */
  skipped: ColumnName[];
}

export type PasteResult = { ok: true; plan: PastePlan } | { ok: false; error: string };

export interface PasteTarget {
  /** Every row of the PT, in the order drawn (all pages). */
  lineKeys: string[];
  /** The row the paste starts on, counted over all pages from 0. */
  startRow: number;
  /** The column the paste starts on, an index into `columns`. */
  startCol: number;
  /** The columns drawn, as `visibleColumns` gives them. */
  columns: GridColumn[];
}

/** Where each pasted cell lands, as a spreadsheet would put it: from the cell
 *  the paste started on, down the rows and across the columns drawn - or, when
 *  the first line is a header of KDPS column names, into those columns. A paste
 *  fills the PT's own rows and never adds one, so a block taller than the rows
 *  below is refused whole rather than cut short. */
export function planPaste(text: string, target: PasteTarget): PasteResult {
  const grid = parseClipboard(text);
  const header = headerOf(grid[0] ?? []);
  const body = header ? grid.slice(1) : grid;
  if (body.length === 0 || body.every((row) => row.every((cell) => cell.trim() === ""))) {
    return { ok: false, error: "The paste holds no values." };
  }
  const drawn = new Set(target.columns.map((c) => c.name));
  let names: (ColumnName | null)[];
  if (header) {
    names = header;
  } else {
    const width = Math.max(...body.map((row) => row.length));
    const room = target.columns.length - target.startCol;
    if (width > room) {
      const from = target.columns[target.startCol]?.name ?? "here";
      return {
        ok: false,
        error: `The paste is ${width} columns wide, but the grid has only ${room} from ${from} on.`,
      };
    }
    names = target.columns.slice(target.startCol, target.startCol + width).map((c) => c.name);
  }
  const below = target.lineKeys.length - target.startRow;
  if (body.length > below) {
    return {
      ok: false,
      error:
        `The paste has ${body.length} rows, but there are only ${below} from row ` +
        `${target.startRow + 1} down. A paste fills the rows already on this PT - one per ` +
        "counted lot - and never adds a row.",
    };
  }
  const cells: Pastes = {};
  const skipped = new Set<ColumnName>();
  body.forEach((row, i) => {
    const key = target.lineKeys[target.startRow + i];
    if (!key) return;
    row.forEach((raw, j) => {
      const name = names[j];
      if (!name) return;
      const value = raw.trim();
      if (!isPasteColumn(name) || !drawn.has(name)) {
        if (value !== "") skipped.add(name);
        return;
      }
      (cells[key] ??= {})[name] = value;
    });
  });
  if (!hasPastes(cells)) {
    return {
      ok: false,
      error: `Nothing in the paste lands on a column a paste fills (${PASTE_COLUMNS.join(", ")}).`,
    };
  }
  return {
    ok: true,
    plan: {
      cells,
      rows: Object.keys(cells).length,
      skipped: KDPS_COLUMNS.filter((name) => skipped.has(name)),
    },
  };
}

/** Pasted cells laid over the ones already waiting. */
export function applyPaste(pastes: Pastes, cells: Pastes): Pastes {
  const next = { ...pastes };
  for (const [key, row] of Object.entries(cells)) next[key] = { ...next[key], ...row };
  return next;
}

/** One cell's pasted text dropped: a person typed or picked over it. */
export function withoutPaste(pastes: Pastes, lineKey: string, column: ColumnName | "SKU"): Pastes {
  const row = pastes[lineKey];
  if (!row || column === "SKU" || !(column in row)) return pastes;
  const rest = { ...row };
  delete rest[column];
  return { ...pastes, [lineKey]: rest };
}

/** Typed edits of the cells a paste just filled dropped: the paste is newer. */
export function withoutEdits(edits: Edits, cells: Pastes): Edits {
  let next = edits;
  for (const [key, row] of Object.entries(cells)) {
    const typed = next[key];
    if (!typed) continue;
    const rest = { ...typed };
    for (const name of Object.keys(row)) delete rest[name as ColumnName];
    next = { ...next, [key]: rest };
  }
  return next;
}

/** Every pasted row as an E124 `{line_key, canonical}` update. `planPaste`
 *  only ever records `PASTE_COLUMNS`, the keys `CanonicalCells` names. */
export function pasteUpdates(lines: GridLine[], pastes: Pastes): PtRowEdit[] {
  return lines
    .filter((line) => Object.keys(pastes[line.line_key] ?? {}).length > 0)
    .map((line) => ({
      line_key: line.line_key,
      canonical: { ...pastes[line.line_key] } as CanonicalCells,
    }));
}
