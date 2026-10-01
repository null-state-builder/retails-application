// Prepare PT (ticket 06; the grid since OPS-16): a preparer picks a GRN with
// uncovered quantity, fills its PT one of three ways, prepares it in a
// spreadsheet-style grid, reviews every row and submits for a distinct checker.
//
// Since OPS-17 there is no Prepare PT screen of its own: `GrnStart` and
// `PtEditor` are the delivery workflow's PT step and PT Work's To prepare tab
// (`PtWork.tsx`), the one editor in both places.
//
// The three ways to fill the grid all land in the same draft (E122/E123): start
// from the GRN's own count (E123), upload a file already in the canonical KDPS
// layout (E120 then E122 `canonical_upload`), or upload the brand's own file
// (E120 then E122 `brand_upload`, OPS-15), whose columns the one rulebook maps.
// A receipt PT is always bound to its GRN (GSA-T06).
//
// The grid (store and warehouse operations PRD §5.4) shows the twenty-two KDPS
// columns in the canonical file's order. Describing columns are dropdowns of the
// approved values; calculated columns are drawn and never typed into - the
// server works out every value, and each cell's value is the server's resolved
// one (`cells`). Each cell's colour says where its value came from. A suggestion
// is applied only by a person's click; a new item is proposed on its row and
// waits for the product-master owner; a rule is proposed from a cell and applies
// nowhere until confirmed. Fill a column and the quick fills are ordinary edits.
//
// Review is per row or for the selected rows on the current page - never all at
// once (GSA-T06). Save is explicit; leaving with unsaved edits warns; a
// concurrent edit from someone else shows a side-by-side compare rather than
// silently overwriting either version.
//
// Canonical values can be pasted (ticket 06A): a block copied from a
// spreadsheet lands from the cell it is pasted on, or by its header row, into
// the rows the count already gave this PT - SEASON, BARCODE, HSN, QTY, MRP,
// BASIC and P RATE. Pasted text waits unsaved like any edit; on Save the server
// reads it exactly as it reads an uploaded workbook, and refuses the whole save,
// cell by cell, when a barcode is not the row's item, a quantity is more than
// its counted lot has free, or an amount is not exact.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type {
  ClipboardEvent as ReactClipboardEvent,
  KeyboardEvent as ReactKeyboardEvent,
} from "react";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Download,
  RefreshCw,
  Save,
  Send,
  Upload,
  Wand2,
} from "lucide-react";

import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Denied,
  Feedback,
  Field,
  hold,
  useAllPages,
  useGoodsFetch,
  useResourceDoc,
  type Page,
  type ResourceDTO,
} from "../lib/goodsScreen";
import { apiErrorIssues, type ApiIssue } from "../lib/goodsAcceptance";
import {
  coverableQty,
  supplementCoverableQty,
  excessByLine,
  type GrnCoverage,
} from "../lib/goodsReceiving";
import {
  issueLabel,
  reviewProgress,
  rupeesToPaiseString,
  uploadEvidence,
  type PtData,
  type PtReconciliation,
  type PtRowIssue,
} from "../lib/goodsPt";
import { effectiveProfiles, type ConfigRow } from "../lib/goodsIdentity";
import { itemRejectReasonLabel } from "../lib/ptWork";
import {
  COLOUR_TIERS,
  FREE_SIZE,
  PAGE_SIZE,
  acceptedValue,
  applyFill,
  applyPaste,
  blankCount,
  buildUpdates,
  canProposeRule,
  cellTone,
  clearReviewMarks,
  columnByName,
  columnSlug,
  fillTargets,
  findChoice,
  isBlock,
  isDirty,
  isDropdown,
  isPasteColumn,
  itemStatus,
  nextPosition,
  pageCount,
  pageSlice,
  pasteUpdates,
  planPaste,
  reviewMarks,
  setEdit,
  showsCost,
  sheetChecks,
  sortIssues,
  suggestionFills,
  submitBlockers,
  visibleColumns,
  withoutEdits,
  withoutPaste,
  type AttributeEntry,
  type Choice,
  type ColumnName,
  type Edits,
  type FillScope,
  type GridColumn,
  type GridLine,
  type GridPos,
  type Pastes,
  type RowEdits,
  type SuggestionChoice,
  type Tone,
} from "../lib/ptGrid";
import { useAuth } from "../auth/AuthContext";
import { Combobox } from "../components/Combobox";
import { formatPaiseString, paiseStringToRupees } from "../lib/format";
import "./PtScreens.css";
import "./PtGrid.css";

interface ConfigVersionSummary {
  id: string;
  version: number;
  state: string;
  effective_from: string;
}

interface ConfigSummary {
  effective_from: string;
  /** The configuration's own content. A PT profile's `family` lives here, not at
   *  the top of the DTO — reading it from the wrong place named every profile
   *  "Unnamed family" and left a preparer choosing between opaque ids. */
  payload?: { family?: string };
  directions?: string[];
  /** The approved `ConfigVersion`(s) this draft produced — what a PT's
   *  `profile_version_id` actually pins is one of *these* ids, never the
   *  draft's own id (E122 step 11 reads `profile.version.pk`, and
   *  `masters.goods_config.check_pinned` looks a `ConfigVersion` up by that
   *  id, not a `ConfigDraft`). */
  versions: ConfigVersionSummary[];
}

/** The version id a PT actually pins, from an approved configuration draft:
 *  its effective version if one is live, else its first version. `null` for
 *  a draft with no version yet (not approved, or withdrawn before it took
 *  effect). */
function pinnableVersionId(config: ConfigSummary): string | null {
  const effective = config.versions.find((v) => v.state === "effective");
  return (effective ?? config.versions[0])?.id ?? null;
}

// ---------------------------------------------------------------------------
// Step 2: the chosen GRN — uncovered quantity, then start the PT
// ---------------------------------------------------------------------------

/** The evidence kind a brand's own file is uploaded as: the one upload that
 *  also takes .xls, .xlsb and .csv (Anand, 23 September 2026). */
const BRAND_FILE_KIND = "pt_brand";
const BRAND_FILE_TYPES = ".xlsx,.xls,.xlsb,.csv";

/** Start a receipt PT from one GRN: pick the pricing profile and the
 *  direction, then fill the grid one of three ways.
 *
 *  Exported so the receiving workflow's PT step offers the same start the
 *  Prepare PT screen does, on the delivery the person is already standing on.
 *  `onBack` is optional there: the workflow's own step rail is the way back. */
export function GrnStart({
  grnId,
  onBack,
  onCreated,
}: {
  grnId: string;
  onBack?: () => void;
  onCreated: (ptId: string) => void;
}) {
  const doc = useResourceDoc<GrnCoverage>(`/goods-v1/inbound/grns/${grnId}`);
  const profiles = useGoodsFetch<Page<ResourceDTO<ConfigSummary>>, ResourceDTO<ConfigSummary>[]>(
    "/goods-v1/masters/configurations?kind=profile",
    (r) => (r.items ?? []).filter((row) => row.state === "approved" && pinnableVersionId(row.data)),
    [],
  );
  const [profileId, setProfileId] = useState("");
  const [direction, setDirection] = useState("base_to_ticket");
  const [receiptKind, setReceiptKind] = useState<"primary" | "supplement">("primary");
  const [canonicalFile, setCanonicalFile] = useState<File | null>(null);
  const [brandFile, setBrandFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    // Only when there is nothing to choose between. A tenant may have several
    // profiles in force at once - one per product family - and picking the
    // first of them for the preparer pins the wrong family's rules onto these
    // goods, which the server then refuses as IDENTITY_PROFILE_MISMATCH with
    // no way for the preparer to see why. Where there is a choice, they make it.
    if (!profileId && profiles.value.length === 1) {
      setProfileId(profiles.value[0] ? (pinnableVersionId(profiles.value[0].data) ?? "") : "");
    }
  }, [profiles.value, profileId]);

  // Ticket 07B: once a live PT already covers part of this receipt, what is left
  // to prepare is a supplement (accepted excess). Only the starting choice - the
  // preparer can still change it, and the server refuses a second primary.
  const alreadyCovered = (doc.doc?.data.lines.items ?? []).some((line) => line.covered_qty > 0);
  useEffect(() => {
    if (alreadyCovered) setReceiptKind("supplement");
  }, [alreadyCovered]);

  if (doc.denied) return <Denied what="goods receipt" />;
  if (doc.loading && !doc.doc) return <p className="muted">Loading…</p>;
  if (doc.failure) return <div className="warn-note">{doc.failure}</div>;
  if (!doc.doc) return <Denied what="goods receipt" />;

  const coverage = doc.doc.data;
  const excess = excessByLine(coverage);
  // A supplement takes accepted excess, an ordinary PT never does (ticket 07B).
  const uncoveredTotal = coverage.lines.items.reduce(
    (sum, line) =>
      sum +
      (receiptKind === "supplement"
        ? supplementCoverableQty(line)
        : coverableQty(line, excess[line.line_key] ?? 0)),
    0,
  );

  async function run(create: () => Promise<string>) {
    setBusy(true);
    setError("");
    try {
      onCreated(await create());
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function startFromCount() {
    return run(async () => {
      const { data } = await api.post<{ id: string }>(
        `/goods-v1/ptmapper/files/from-grn/${grnId}`,
        {
          receipt_kind: receiptKind,
          profile_version_id: profileId,
          direction,
          ...goodsMeta(),
        },
      );
      return data.id;
    });
  }

  function uploadAndCreate(file: File, source: "canonical_upload" | "brand_upload") {
    return run(async () => {
      const evidenceId = await uploadEvidence(file, {
        kind: source === "brand_upload" ? BRAND_FILE_KIND : "pt",
        siteId: coverage.grn_header.site_id ?? null,
      });
      const { data } = await api.post<{ id: string }>("/goods-v1/ptmapper/files", {
        purpose: "receipt",
        receipt_kind: receiptKind,
        grn_id: grnId,
        profile_version_id: profileId,
        direction,
        source,
        evidence_id: evidenceId,
        ...goodsMeta(),
      });
      return data.id;
    });
  }

  const cannotStart = busy || !profileId || uncoveredTotal === 0;

  return (
    <div className="pt-layout" data-testid="pt-grn-start">
      {onBack && (
        <div>
          <button className="btn btn-sm" onClick={onBack} data-testid="pt-grn-back">
            <ArrowLeft size={14} /> Back to To prepare
          </button>
        </div>
      )}
      <h3 className="h3">{doc.doc.number ?? `Receipt ${grnId.slice(0, 8)}`}</h3>
      <p className="pt-hint" data-testid="pt-grn-uncovered">
        <b>{uncoveredTotal}</b> piece(s) counted good are not yet on a live PT. Held pieces —
        damaged, wrong, unidentified, or an undecided excess — are never offered here.
      </p>
      {uncoveredTotal === 0 && (
        <div className="warn-note" data-testid="pt-grn-nothing-uncovered">
          Every good piece on this receipt is already covered by a live PT. There is nothing left
          for an ordinary PT to prepare.
        </div>
      )}

      <Feedback error={error} ok="" />

      <div className="card section-card pt-start-card">
        <div className="form-grid">
          <Field
            id="pt-start-profile"
            label="Pricing profile"
            hint="The product family these goods belong to. Its rates, tax and columns are what this PT is priced by."
          >
            <select
              id="pt-start-profile"
              className="select"
              value={profileId}
              aria-describedby="pt-start-profile-hint"
              onChange={(e) => setProfileId(e.target.value)}
              data-testid="pt-start-profile"
            >
              {profiles.value.length === 0 && <option value="">No approved profile</option>}
              {profiles.value.length > 1 && <option value="">Choose a profile</option>}
              {profiles.value.map((p) => (
                <option key={p.id} value={pinnableVersionId(p.data) ?? ""}>
                  {/* Named by the product family it is for, because that is the
                      thing a preparer has to match to the goods in front of
                      them. An opaque id tells them nothing. */}
                  {p.data.payload?.family ?? "Unnamed family"} (since{" "}
                  {p.data.effective_from.slice(0, 10)})
                </option>
              ))}
            </select>
          </Field>
          <Field id="pt-start-direction" label="Pricing direction">
            <select
              id="pt-start-direction"
              className="select"
              value={direction}
              onChange={(e) => setDirection(e.target.value)}
              data-testid="pt-start-direction"
            >
              <option value="base_to_ticket">BASIC → MRP (base to ticket)</option>
              <option value="ticket_to_purchase">MRP → cost (ticket to purchase)</option>
              <option value="both_supplied">Both supplied</option>
            </select>
          </Field>
          <Field
            id="pt-start-kind"
            label="Receipt kind"
            hint="A supplement covers only newly eligible uncovered quantity — it never reprices a line an earlier PT already covers."
          >
            <select
              id="pt-start-kind"
              className="select"
              value={receiptKind}
              onChange={(e) => setReceiptKind(e.target.value as "primary" | "supplement")}
              data-testid="pt-start-kind"
            >
              <option value="primary">Primary — the first PT for this receipt</option>
              <option value="supplement">Supplement — an earlier PT is already official</option>
            </select>
          </Field>
        </div>

        <div className="pt-start-actions pt-start-three">
          <div className="pt-start-path">
            <h4 className="gr-h4">Start from the count</h4>
            <p className="pt-hint">
              One row per counted lot, exactly as the GRN counted it. Fill the values in on the grid
              next.
            </p>
            <button
              className="btn btn-primary"
              disabled={cannotStart}
              onClick={startFromCount}
              data-testid="pt-start-prefill"
            >
              Start from the count
            </button>
          </div>
          <div className="pt-start-path">
            <h4 className="gr-h4">
              <Upload size={14} /> Upload a KDPS PT file
            </h4>
            <p className="pt-hint">
              A filled KDPS work sheet (one staff sheet, with or without its Master Sheet), or a
              workbook in the canonical PT layout. Its rows land in the same grid.{" "}
              <Link to="/setup/products?tab=lists">Download a blank PT file</Link>
            </p>
            <input
              type="file"
              id="pt-start-file"
              accept=".xlsx"
              aria-label="Canonical PT file"
              onChange={(e) => setCanonicalFile(e.target.files?.[0] ?? null)}
              data-testid="pt-start-file"
            />
            <button
              className="btn btn-primary"
              disabled={cannotStart || !canonicalFile}
              onClick={() => canonicalFile && uploadAndCreate(canonicalFile, "canonical_upload")}
              data-testid="pt-start-upload"
            >
              Upload and prepare
            </button>
          </div>
          <div className="pt-start-path">
            <h4 className="gr-h4">
              <Upload size={14} /> Upload the brand's own file
            </h4>
            <p className="pt-hint">
              The file as the brand sent it (.xlsx, .xls, .xlsb or .csv). Its columns are mapped to
              the KDPS columns by the approved rules; anything no rule settles waits for you.
            </p>
            <input
              type="file"
              id="pt-start-brand-file"
              accept={BRAND_FILE_TYPES}
              aria-label="Brand PT file"
              onChange={(e) => setBrandFile(e.target.files?.[0] ?? null)}
              data-testid="pt-start-brand-file"
            />
            <button
              className="btn btn-primary"
              disabled={cannotStart || !brandFile}
              onClick={() => brandFile && uploadAndCreate(brandFile, "brand_upload")}
              data-testid="pt-start-brand-upload"
            >
              Upload and map
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The grid's choices: approved vocabulary, brands and seasons
// ---------------------------------------------------------------------------

/** The describing dimensions the grid offers dropdowns for (E226), plus
 *  `season`, the vocabulary a season rule maps a brand's word to. */
const VOCAB_DIMENSIONS = [
  "season",
  "colour",
  "gender",
  "sub_category",
  "type",
  "item",
  "fit",
  "size",
] as const;

interface VocabRow {
  id: string;
  value: string;
  label: string;
  state: string;
}

interface NamedMaster {
  code: string;
  name: string;
  historical_unknown?: boolean;
}

interface GridChoices {
  /** Approved values per dimension; `null` when the profile governs none for it. */
  vocab: Record<string, Choice[] | null>;
  brands: Choice[];
  seasons: Choice[];
  failure: string;
}

function vocabUrl(dimension: string, profileVersionId: string | null): string | null {
  if (!profileVersionId) return null;
  const query = new URLSearchParams({
    dimension,
    profile_version_id: profileVersionId,
    limit: "100",
  });
  return `/goods-v1/ptmapper/controlled?${query.toString()}`;
}

function useVocab(dimension: string, profileVersionId: string | null) {
  return useAllPages<VocabRow>(vocabUrl(dimension, profileVersionId));
}

function toChoices(rows: VocabRow[]): Choice[] {
  return rows
    .filter((row) => row.state !== "retired")
    .map((row) => ({ id: row.id, value: row.value, label: row.label }));
}

function masterChoices(rows: ResourceDTO<NamedMaster>[], unknownSeason = false): Choice[] {
  return rows
    .filter((row) => row.state !== "retired" && (unknownSeason || !row.data.historical_unknown))
    .map((row) => ({ id: String(row.id), value: row.data.code, label: row.data.name }));
}

/** Everything the grid's dropdowns offer. A fixed number of reads - one per
 *  dimension - so the hooks never change between renders. Each read is whole
 *  (every page), because a dropdown missing a value is a value nobody can pick. */
function useGridChoices(profileVersionId: string | null): GridChoices {
  const season = useVocab("season", profileVersionId);
  const colour = useVocab("colour", profileVersionId);
  const gender = useVocab("gender", profileVersionId);
  const subCategory = useVocab("sub_category", profileVersionId);
  const type = useVocab("type", profileVersionId);
  const item = useVocab("item", profileVersionId);
  const fit = useVocab("fit", profileVersionId);
  const size = useVocab("size", profileVersionId);
  const brands = useAllPages<ResourceDTO<NamedMaster>>("/goods-v1/masters/brands?limit=100");
  const seasons = useAllPages<ResourceDTO<NamedMaster>>("/goods-v1/masters/seasons?limit=100");
  const reads = { season, colour, gender, sub_category: subCategory, type, item, fit, size };
  const vocab: Record<string, Choice[] | null> = {};
  for (const dimension of VOCAB_DIMENSIONS) {
    const read = reads[dimension];
    // A dimension the profile does not govern answers NOT_FOUND: no dropdown.
    vocab[dimension] = read.denied || read.failure ? null : toChoices(read.items);
  }
  return {
    vocab,
    brands: masterChoices(brands.items),
    seasons: masterChoices(seasons.items),
    failure: brands.failure || seasons.failure,
  };
}

function choicesFor(column: GridColumn, choices: GridChoices): Choice[] | null {
  if (column.kind === "brand") return choices.brands;
  if (column.kind === "season") return choices.seasons;
  if (column.kind === "attribute") return choices.vocab[column.dimension!] ?? null;
  return null;
}

// ---------------------------------------------------------------------------
// Reading a cell
// ---------------------------------------------------------------------------

/** What a cell shows: a person's unsaved value if there is one, else the
 *  server's resolved value. Never a value worked out here. */
function cellText(
  column: GridColumn,
  line: GridLine,
  row: RowEdits | undefined,
  choices: GridChoices,
  pasted?: Partial<Record<ColumnName, string>>,
): string {
  // Pasted text shows exactly as pasted until Save: the server reads it.
  const own = pasted?.[column.name];
  if (own !== undefined) return own;
  if (row && column.name in row) {
    const value = row[column.name];
    if (value === null || value === undefined) return "";
    if (isDropdown(column)) {
      return choicesFor(column, choices)?.find((c) => c.id === value)?.label ?? value;
    }
    if (column.kind === "money") return paiseStringToRupees(value);
    return value;
  }
  const raw = line.cells?.[column.cell];
  if (raw === null || raw === undefined || raw === "") return "";
  if (column.kind === "money") return paiseStringToRupees(String(raw));
  if (column.money) return formatPaiseString(String(raw));
  if (column.name === "INPUT TAX" || column.name === "OUTPUT TAX" || column.name === "MARGIN") {
    return `${raw}%`;
  }
  return String(raw);
}

const TONE_WORDS: Record<Tone, string> = {
  given: "From the file, a rule or the item",
  waiting: "A suggestion is waiting for you",
  person: "A person's choice",
  problem: "Blank or wrong",
  calculated: "Calculated by the server",
};

function GridLegend() {
  const tones: Tone[] = ["given", "waiting", "person", "problem", "calculated"];
  return (
    <div className="ptg-legend" data-testid="pt-legend" aria-label="What the colours mean">
      {tones.map((tone) => (
        <span key={tone} data-testid={`pt-legend-${tone}`}>
          <i className="ptg-swatch" data-tone={tone} /> {TONE_WORDS[tone]}
        </span>
      ))}
      <span data-testid="pt-legend-dirty">
        <i className="ptg-swatch" data-tone="dirty" /> Row with unsaved changes
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 3: the grid — prepare, review, save, submit
// ---------------------------------------------------------------------------

/** A cell a person has asked to turn into a proposed mapping rule. */
interface RuleDraft {
  lineKey: string;
  column: ColumnName;
  brandId: number | null;
  source: string;
  targetId: string;
}

/** Legacy control test ids, kept so the flows that already drive this editor
 *  keep finding the BASIC, MRP, HSN and SEASON controls where they were. */
function controlTestId(column: GridColumn, lineKey: string): string {
  switch (column.name) {
    case "BASIC":
      return `pt-cell-basic_paise-${lineKey}`;
    case "MRP":
      return `pt-cell-mrp_paise-${lineKey}`;
    case "HSN":
      return `pt-hsn-${lineKey}`;
    case "SEASON":
      return `pt-season-${lineKey}`;
    default:
      return `pt-input-${columnSlug(column.name)}-${lineKey}`;
  }
}

function normalise(text: string | null | undefined): string {
  return (text ?? "").replace(/\s+/g, " ").trim().toUpperCase();
}

/** The PT itself: its rows in the grid, their calculated values, the review
 *  marks and the submit that sends it for approval. Rendered by the Prepare PT
 *  screen and by the receiving workflow's PT step. */
export function PtEditor({ ptId, onClose }: { ptId: string; onClose?: () => void }) {
  const { session } = useAuth();
  // Row edits (E124) require exactly `pt.prepare` — never widened to
  // `pt.prepare.opening` the way the page-level gate is: an opening-only
  // holder reaching a receipt PT here must see it read-only, not an editable
  // grid the server would refuse every write from.
  const canPrepare = hold(session, "pt.prepare");
  const canProposeItems =
    hold(session, "product.master.propose") || hold(session, "product.master.manage");
  const canProposeRules = hold(session, "crosswalk.propose") || hold(session, "crosswalk.manage");
  const doc = useResourceDoc<PtData>(`/goods-v1/ptmapper/files/${ptId}`);
  const [edits, setEdits] = useState<Edits>({});
  // What a money cell's box literally shows, keyed by `${lineKey}:${column}` —
  // separate from `edits` (which holds only a value that already parsed to
  // paise). Deriving the box straight from `edits` on every keystroke would
  // snap back an intermediate "95." that has no parse yet.
  const [moneyText, setMoneyText] = useState<Record<string, string>>({});
  const [pendingReviews, setPendingReviews] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [page, setPage] = useState(0);
  const [cursor, setCursor] = useState<GridPos>({ row: 0, col: 0 });
  const focusWanted = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [pastes, setPastes] = useState<Pastes>({});
  const [compare, setCompare] = useState<Compare | null>(null);
  const [reconciliation, setReconciliation] = useState<PtReconciliation | null>(null);
  const [submitIssues, setSubmitIssues] = useState<ApiIssue[]>([]);
  const [reissueReason, setReissueReason] = useState("");
  const [fillColumn, setFillColumn] = useState<ColumnName | "">("");
  const [fillValue, setFillValue] = useState("");
  const [fillScope, setFillScope] = useState<FillScope>("blank");
  const [rule, setRule] = useState<RuleDraft | null>(null);
  const gridRef = useRef<HTMLTableElement>(null);

  // E099 pages lines at 500 (design §5.8); "every current row must be
  // reviewed before submit" (GSA-T06) means every row, not only the first
  // page a naive read would stop at, so the rest are fetched eagerly.
  const [extraLines, setExtraLines] = useState<GridLine[]>([]);
  useEffect(() => {
    let cancelled = false;
    setExtraLines([]);
    let next = doc.doc?.data.lines.next_cursor ?? null;
    if (!next) return;
    (async () => {
      const collected: GridLine[] = [];
      while (next && !cancelled) {
        const res: { data: ResourceDTO<PtData> } = await api.get<ResourceDTO<PtData>>(
          `/goods-v1/ptmapper/files/${ptId}`,
          { params: { line_cursor: next } },
        );
        collected.push(...(res.data.data.lines.items as GridLine[]));
        next = res.data.data.lines.next_cursor;
      }
      if (!cancelled) setExtraLines(collected);
    })();
    return () => {
      cancelled = true;
    };
    // Re-run whenever the loaded first page's own identity changes (a fresh
    // load, a save, a reload after conflict) — never on `extraLines` itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ptId, doc.doc?.revision, doc.doc?.content_hash, doc.doc?.data.lines.next_cursor]);

  const lines = useMemo(
    () => [...((doc.doc?.data.lines.items ?? []) as GridLine[]), ...extraLines],
    [doc.doc, extraLines],
  );
  const profileVersionId = (doc.doc?.data.header?.profile_version_id as string | undefined) ?? null;
  const choices = useGridChoices(profileVersionId);
  const dirty = isDirty(edits, pendingReviews, pastes);
  const columns = visibleColumns(showsCost(lines));
  const pages = pageCount(lines.length);
  const shownPage = Math.min(page, pages - 1);
  const pageLines = pageSlice(lines, shownPage);

  // Unsaved-navigation warning (GSA-T06): a browser-level close/refresh with
  // dirty local edits asks first, same as every other goods-v1 editor.
  useEffect(() => {
    function beforeUnload(e: BeforeUnloadEvent) {
      if (!dirty) return;
      e.preventDefault();
      e.returnValue = "";
    }
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  // Keyboard movement puts focus on the cell the cursor moved to.
  useEffect(() => {
    if (!focusWanted.current) return;
    focusWanted.current = false;
    const cell = gridRef.current?.querySelector<HTMLElement>(
      `[data-cell="${cursor.row}-${cursor.col}"]`,
    );
    const control = cell?.querySelector<HTMLElement>(
      "input[data-cell-control], [data-cell-control] input, [data-cell-control] button",
    );
    (control ?? cell)?.focus();
  }, [cursor, edits]);

  function requestClose() {
    if (dirty && !window.confirm("You have unsaved changes. Leave without saving?")) return;
    onClose?.();
  }

  // --- editing ---------------------------------------------------------------

  function edit(lineKey: string, column: ColumnName | "SKU", value: string | null) {
    setEdits((prev) => setEdit(prev, lineKey, column, value));
    setPastes((prev) => withoutPaste(prev, lineKey, column));
    setPendingReviews((prev) => clearReviewMarks(prev, [lineKey]));
  }

  function editMoney(lineKey: string, column: ColumnName, rupees: string) {
    setPastes((prev) => withoutPaste(prev, lineKey, column));
    setMoneyText((prev) => ({ ...prev, [`${lineKey}:${column}`]: rupees }));
    const paise = rupeesToPaiseString(rupees);
    if (paise === undefined) return; // not a usable amount yet — the box still shows it as typed
    edit(lineKey, column, paise);
  }

  function pick(line: GridLine, column: GridColumn, label: string) {
    if (!label) {
      edit(line.line_key, column.name, null);
    } else {
      const found = choicesFor(column, choices)?.find((c) => c.label === label);
      if (!found) return;
      edit(line.line_key, column.name, found.id);
    }
    focusWanted.current = true;
  }

  function acceptSuggestion(line: GridLine, column: GridColumn, choice: SuggestionChoice) {
    const value = acceptedValue(column, choice, choices.seasons);
    if (value === null) {
      setError(
        `No single season is named ${choice.label}. Choose the season from the list instead.`,
      );
      return;
    }
    setError("");
    edit(line.line_key, column.name, value);
  }

  // --- fill a column and quick fills -----------------------------------------

  function fillRows(column: GridColumn) {
    return lines.map((line) => ({
      key: line.line_key,
      current: cellText(column, line, edits[line.line_key], choices, pastes[line.line_key]) || null,
    }));
  }

  // --- paste (ticket 06A) -----------------------------------------------------

  function onGridPaste(e: ReactClipboardEvent<HTMLTableElement>) {
    if (!doc.doc || doc.doc.state !== "draft" || !canPrepare || compare) return;
    const text = e.clipboardData.getData("text/plain");
    const cell = (e.target as HTMLElement).closest<HTMLElement>("[data-cell]");
    if (!text || !cell?.dataset.cell) return;
    const [row, col] = cell.dataset.cell.split("-").map(Number);
    if (
      row === undefined ||
      col === undefined ||
      !Number.isInteger(row) ||
      !Number.isInteger(col) ||
      row < 0 ||
      col < 0
    )
      return;
    const column = columns[col];
    // One value pasted into a describing or calculated cell is ordinary typing.
    if (!column || (!isBlock(text) && !isPasteColumn(column.name))) return;
    e.preventDefault();
    if (doc.doc.data.header?.purpose !== "receipt") {
      setOk("");
      setError("Canonical values are pasted into a receipt PT's rows only.");
      return;
    }
    const result = planPaste(text, {
      lineKeys: lines.map((line) => line.line_key),
      startRow: shownPage * PAGE_SIZE + row,
      startCol: col,
      columns,
    });
    if (!result.ok) {
      setOk("");
      setError(result.error);
      return;
    }
    const { cells, rows, skipped } = result.plan;
    const keys = Object.keys(cells);
    setPastes((prev) => applyPaste(prev, cells));
    setEdits((prev) => withoutEdits(prev, cells));
    setMoneyText((prev) => {
      const next = { ...prev };
      for (const [key, pasted] of Object.entries(cells)) {
        for (const name of Object.keys(pasted)) delete next[`${key}:${name}`];
      }
      return next;
    });
    setPendingReviews((prev) => clearReviewMarks(prev, keys));
    setError("");
    setSubmitIssues([]);
    setOk(
      `Pasted into ${rows} row(s). Save to keep it; those rows need a fresh review.` +
        (skipped.length > 0
          ? ` Not pasted: ${skipped.join(", ")} - a paste fills SEASON, BARCODE, HSN, QTY, MRP, BASIC and P RATE only.`
          : ""),
    );
  }

  function fill(columnName: ColumnName, choice: Choice, scope: FillScope) {
    const column = columnByName(columnName);
    const keys = fillTargets(fillRows(column), choice.label, scope);
    setEdits((prev) => applyFill(prev, keys, columnName, choice.id));
    setPastes((prev) => keys.reduce((next, key) => withoutPaste(next, key, columnName), prev));
    setPendingReviews((prev) => clearReviewMarks(prev, keys));
    setOk(
      keys.length === 0
        ? `No row needed ${columnName} set to ${choice.label}.`
        : `${columnName} set to ${choice.label} on ${keys.length} row(s). Save to keep it; those rows need a fresh review.`,
    );
  }

  /** Blank SUB CATEGORY / TYPE cells take the ITEM's suggestion (the sheet's
   *  SUGGESTED columns), as ordinary edits a person saves and reviews. */
  function fillFromSuggestions() {
    const planned = suggestionFills(lines, (line, name) =>
      cellText(columnByName(name), line, edits[line.line_key], choices, pastes[line.line_key]),
    );
    const found = planned
      .map((p) => ({
        ...p,
        choice: findChoice(choicesFor(columnByName(p.column), choices) ?? [], p.value),
      }))
      .filter((p) => p.choice);
    const keys = [...new Set(found.map((p) => p.key))];
    setEdits((prev) =>
      found.reduce((next, p) => setEdit(next, p.key, p.column, p.choice!.id), prev),
    );
    setPendingReviews((prev) => clearReviewMarks(prev, keys));
    setOk(
      found.length === 0
        ? "No blank SUB CATEGORY or TYPE has a suggestion to take."
        : `Filled ${found.length} blank cell(s) from the ITEM suggestions. Save to keep them; those rows need a fresh review.`,
    );
  }

  function applyColumnFill() {
    if (!fillColumn) return;
    const column = columnByName(fillColumn);
    const found = choicesFor(column, choices)?.find((c) => c.label === fillValue);
    if (!found) return;
    fill(fillColumn, found, fillScope);
  }

  // --- save, conflict, review, submit ----------------------------------------

  async function save() {
    if (!doc.doc) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      const reviewed_rows = Object.entries(pendingReviews).map(([line_key, row_hash]) => ({
        line_key,
        row_hash,
      }));
      await api.patch(`/goods-v1/ptmapper/files/${ptId}/rows`, {
        // Typed cells first, then pasted ones: a row may have both, never in one cell.
        updates: [...buildUpdates(lines, edits), ...pasteUpdates(lines, pastes)],
        reviewed_rows,
        ...goodsMeta(doc.doc.revision),
      });
      setEdits({});
      setPastes({});
      setMoneyText({});
      setPendingReviews({});
      setSubmitIssues([]);
      setOk("Saved.");
      doc.reload();
    } catch (e) {
      if (apiErrorCode(e) === "REVISION_SUPERSEDED") {
        // Keep every local edit for the compare; read the PT as it now is.
        setCompare({ edits, pastes, before: lines });
        doc.reload();
        setError("Someone else changed this PT while you were editing. Compare below.");
      } else {
        setError(apiErrorMessage(e));
        setSubmitIssues(apiErrorIssues(e));
      }
    } finally {
      setBusy(false);
    }
  }

  function takeTheirs() {
    setEdits({});
    setPastes({});
    setMoneyText({});
    setPendingReviews({});
    setCompare(null);
  }

  function keepMine() {
    // Local edits stay; the review marks they invalidated are gone (the PT
    // moved on), so a fresh review is required before this can submit again.
    setPendingReviews({});
    setCompare(null);
  }

  function markReviewed(line: GridLine) {
    if (!line.row_hash) return;
    setPendingReviews((prev) => ({ ...prev, [line.line_key]: line.row_hash! }));
  }

  function reviewSelected() {
    setPendingReviews((prev) => ({ ...prev, ...reviewMarks(pageLines, selected, edits) }));
    setSelected({});
  }

  async function submit() {
    if (!doc.doc) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      const { data } = await api.post<{ reconciliation: PtReconciliation }>(
        `/goods-v1/ptmapper/files/${ptId}/send`,
        { reviewed_hash: doc.doc.content_hash, ...goodsMeta(doc.doc.revision) },
      );
      setSubmitIssues([]);
      setReconciliation(data.reconciliation);
      setOk("Submitted for approval.");
      doc.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
      // "Some rows are not valid yet" on its own leaves a preparer with a grid
      // and no idea which row or which field the server means. It names both.
      setSubmitIssues(apiErrorIssues(e));
    } finally {
      setBusy(false);
    }
  }

  async function reissue() {
    if (!doc.doc || !reissueReason) return;
    setBusy(true);
    setError("");
    setOk("");
    try {
      await api.post(`/goods-v1/ptmapper/files/${ptId}/reissue`, {
        corrected: { header: doc.doc.data.header, lines: doc.doc.data.lines.items },
        reason_code: reissueReason,
        ...goodsMeta(doc.doc.revision),
      });
      setOk("Reissued as a new draft under the same number. Correct it below and submit again.");
      doc.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  // --- export ----------------------------------------------------------------

  async function exportWorkbook() {
    if (!doc.doc) return;
    setError("");
    const draft = doc.doc.state === "draft" || doc.doc.state === "submitted";
    try {
      const res = await api.get<Blob>(`/goods-v1/ptmapper/files/${ptId}/export.xlsx`, {
        params: draft ? { draft: "true" } : {},
        responseType: "blob",
      });
      const href = URL.createObjectURL(res.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `${doc.doc.number ?? `PT-${ptId.slice(0, 8)}`}${draft ? "-draft" : ""}.xlsx`;
      link.click();
      URL.revokeObjectURL(href);
    } catch (e) {
      setError(apiErrorMessage(e));
    }
  }

  // --- proposing a new item and a rule --------------------------------------

  async function proposeItem(line: GridLine) {
    if (!doc.doc) return;
    const revisionId = doc.doc.context?.draft_revision_id as string | undefined;
    const brandId = line.describing?.brand_id ?? (doc.doc.context?.brand_id as number | null);
    const design = line.describing?.design ?? (line.cells?.design as string | null);
    if (Object.keys(edits[line.line_key] ?? {}).length > 0) {
      setError("Save this row's changes first, then propose its item.");
      return;
    }
    if (!revisionId || !brandId || !design) {
      setError("A new item needs its brand and design on the row before it can be proposed.");
      return;
    }
    setBusy(true);
    setError("");
    setOk("");
    try {
      // The PT profile names the product family; the identity profile in force
      // for that family is what the item is proposed under.
      const [ptProfiles, identities] = await Promise.all([
        api.get<Page<ResourceDTO<ConfigSummary>>>("/goods-v1/masters/configurations", {
          params: { kind: "profile", limit: 100 },
        }),
        api.get<Page<ConfigRow>>("/goods-v1/masters/configurations", {
          params: { kind: "identity_profile", limit: 50 },
        }),
      ]);
      const family = (ptProfiles.data.items ?? []).find((row) =>
        row.data.versions.some((v) => v.id === profileVersionId),
      )?.data.payload?.family;
      const identity = effectiveProfiles(identities.data.items ?? [], new Date()).find(
        (choice) => choice.family === family,
      );
      if (!family || !identity) {
        throw new Error("No identity profile is in force for this PT's product family.");
      }
      const styles = await api.get<Page<ResourceDTO<{ style_code: string }>>>(
        "/goods-v1/masters/styles",
        { params: { brand_id: brandId, q: design, profile_family: family, limit: 100 } },
      );
      let styleId = (styles.data.items ?? []).find(
        (row) => normalise(row.data.style_code) === normalise(design),
      )?.id;
      if (!styleId) {
        const created = await api.post<{ master: { id: string } }>("/goods-v1/masters/styles", {
          brand_id: String(brandId),
          style_code: design,
          profile_family: family,
          originating_revision_id: revisionId,
          ...goodsMeta(),
        });
        styleId = created.data.master.id;
      }
      const attrs = ((line.attributes ?? []) as AttributeEntry[])
        .filter((entry) => entry.vocabulary_value_id)
        .map((entry) => ({
          field_id: entry.field_id,
          vocabulary_value_id: entry.vocabulary_value_id,
          unknown: false,
        }));
      const sku = await api.post<{ master: { id: string } }>("/goods-v1/masters/skus", {
        style_id: styleId,
        profile_version_id: identity.id,
        attrs,
        originating_revision_id: revisionId,
        ...goodsMeta(),
      });
      edit(line.line_key, "SKU", sku.data.master.id);
      setOk(
        "Item proposed. Save to put it on the row; the row then waits for the product-master owner to confirm the item.",
      );
    } catch (e) {
      setError(e instanceof Error && !("response" in e) ? e.message : apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function openRule(line: GridLine, column: GridColumn) {
    const waiting = line.suggestions?.[column.name];
    const brandId = line.describing?.brand_id ?? (doc.doc?.context?.brand_id as number | null);
    setRule({
      lineKey: line.line_key,
      column: column.name,
      brandId: brandId ? Number(brandId) : null,
      source: waiting?.source ?? "",
      targetId: waiting?.choices[0]?.value_id ?? "",
    });
  }

  async function proposeRule() {
    if (!rule || !rule.brandId || !profileVersionId) return;
    const column = columnByName(rule.column);
    setBusy(true);
    setError("");
    setOk("");
    try {
      const { data } = await api.post<{ state: string }>("/goods-v1/masters/crosswalks", {
        kind: column.dimension,
        issuer_key: `brand:${rule.brandId}`,
        source_key: rule.source.trim(),
        target_key: rule.targetId,
        config_version_id: profileVersionId,
        ...goodsMeta(),
      });
      setOk(
        data.state === "effective"
          ? `Rule saved: ${rule.source.trim()} is read as ${ruleTargetLabel()} for this brand's files from now on.`
          : `Rule proposed: ${rule.source.trim()} as ${ruleTargetLabel()}. It applies nowhere until the product-master owner confirms it in Mapping rules.`,
      );
      setRule(null);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function ruleOptions(): Choice[] {
    if (!rule) return [];
    return choices.vocab[columnByName(rule.column).dimension!] ?? [];
  }

  function ruleTargetLabel(): string {
    return ruleOptions().find((c) => c.id === rule?.targetId)?.label ?? "";
  }

  // --- keyboard --------------------------------------------------------------

  function onCellKeyDown(e: ReactKeyboardEvent<HTMLTableCellElement>, pos: GridPos) {
    const target = e.target as HTMLElement;
    const open = Boolean(target.closest(".cbx-control.open"));
    if (open) {
      // The dropdown's own keys (arrows, Enter, Escape) are its own; Tab also
      // closes it, and the grid then moves on.
      if (e.key === "Tab") {
        const next = nextPosition(pos, "Tab", e.shiftKey, pageLines.length, columns.length);
        if (next) {
          e.preventDefault();
          focusWanted.current = true;
          setCursor(next);
        }
      } else if (e.key === "Escape") {
        focusWanted.current = true;
        setCursor({ ...pos });
      }
      return;
    }
    const input = target instanceof HTMLInputElement && target.type !== "checkbox" ? target : null;
    if (input && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      // Inside typed text the caret moves first; the cell moves at the edge.
      const atStart = input.selectionStart === 0 && input.selectionEnd === 0;
      const atEnd = input.selectionStart === input.value.length;
      if ((e.key === "ArrowLeft" && !atStart) || (e.key === "ArrowRight" && !atEnd)) return;
    }
    // Enter on a closed dropdown opens it; everywhere else it moves down.
    if (e.key === "Enter" && target.closest(".cbx")) return;
    const next = nextPosition(pos, e.key, e.shiftKey, pageLines.length, columns.length);
    if (!next) return;
    e.preventDefault();
    e.stopPropagation();
    focusWanted.current = true;
    setCursor(next);
  }

  // --- render ----------------------------------------------------------------

  if (doc.denied) return <Denied what="PT" />;
  if (doc.loading && !doc.doc) return <p className="muted">Loading…</p>;
  if (doc.failure) return <div className="warn-note">{doc.failure}</div>;
  if (!doc.doc) return <Denied what="PT" />;

  const pt = doc.doc;
  const progress = reviewProgress(lines);
  const state = pt.state;
  const readOnly = state !== "draft" || !canPrepare;
  // Reissue is offered on the server's word alone (GSA-T06): `allowed_actions`
  // knows this PT's state, this reader's scope and its purpose.
  const canReissue = pt.allowed_actions.includes("reissue");
  const blockers = submitBlockers(lines, dirty);
  const fillable = columns.filter((c) => isDropdown(c) && choicesFor(c, choices));
  const fillChoices = fillColumn ? (choicesFor(columnByName(fillColumn), choices) ?? []) : [];
  const fillBlank = fillColumn ? blankCount(fillRows(columnByName(fillColumn))) : 0;
  const colours = choices.vocab.colour ?? [];
  const sizes = choices.vocab.size ?? [];
  const freeSize = findChoice(sizes, FREE_SIZE);
  const colourBlank = blankCount(fillRows(columnByName("COLOR")));
  const sizeBlank = blankCount(fillRows(columnByName("SIZE")));
  const suggestionBlank = suggestionFills(lines, (line, name) =>
    cellText(columnByName(name), line, edits[line.line_key], choices, pastes[line.line_key]),
  ).length;
  const source = pt.data.header?.source as string | undefined;
  const allOnPage = pageLines.length > 0 && pageLines.every((line) => selected[line.line_key]);

  return (
    <div className="ptg pt-layout" data-testid="pt-editor">
      {onClose && (
        <div>
          <button className="btn btn-sm" onClick={requestClose} data-testid="pt-editor-back">
            <ArrowLeft size={14} /> Back
          </button>
        </div>
      )}
      <div className="ptg-head">
        <h3 className="h3">{pt.number ?? `PT ${ptId.slice(0, 8)}`}</h3>
        <span className={`chip chip-${state === "draft" ? "amber" : "green"}`}>{state}</span>
        {source && (
          <span className="chip" data-testid="pt-source">
            {source === "brand_upload"
              ? "From the brand's file"
              : source === "canonical_upload"
                ? "From a canonical file"
                : "From the count"}
          </span>
        )}
        {dirty && (
          <span className="chip chip-red" data-testid="pt-editor-unsaved">
            Unsaved changes
          </span>
        )}
        <span className="ptg-spacer" />
        <button className="btn btn-sm" onClick={exportWorkbook} data-testid="pt-export">
          <Download size={14} /> Export
        </button>
        <button className="btn btn-sm" onClick={() => doc.reload()} data-testid="pt-refresh">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      <Feedback error={error} ok={ok} />
      {choices.failure && <div className="warn-note">{choices.failure}</div>}

      {submitIssues.length > 0 && (
        <ul className="warn-note" data-testid="pt-submit-issues">
          {submitIssues.map((problem, index) => (
            <li key={`${problem.code}-${problem.line_key ?? index}`}>
              {/^Row \d/.test(problem.message ?? "") ? "" : rowLabel(lines, problem.line_key)}
              {problem.message ?? problem.code}
              {problem.field ? ` (${problem.field})` : ""}
            </li>
          ))}
        </ul>
      )}

      {canReissue && (
        <div className="card section-card pt-start-card" data-testid="pt-reissue-card">
          <p className="pt-hint">
            Reissue keeps this PT's number, starts a new version from its last frozen lines, and
            needs fresh review and a fresh, distinct approval before it values anything again.
          </p>
          <input
            className="input"
            placeholder="Reason for reissue"
            value={reissueReason}
            onChange={(e) => setReissueReason(e.target.value)}
            aria-label="Reason for reissue"
            data-testid="pt-reissue-reason"
          />
          <button
            className="btn btn-sm"
            disabled={busy || !reissueReason}
            onClick={reissue}
            data-testid="pt-reissue"
          >
            Reissue
          </button>
        </div>
      )}

      {compare && (
        <ComparePanel
          compare={compare}
          current={lines}
          columns={columns}
          choices={choices}
          onKeepMine={keepMine}
          onTakeTheirs={takeTheirs}
        />
      )}

      {reconciliation && <ReconciliationPanel reconciliation={reconciliation} />}

      <GridLegend />
      {!readOnly && (
        <p className="pt-hint" data-testid="pt-paste-hint">
          To paste from a spreadsheet, click the cell to start from and paste - or copy the header
          row too, and each column goes under its own name. SEASON, BARCODE, HSN, QTY, MRP, BASIC
          and P RATE are pasted; nothing is kept until you save.
        </p>
      )}

      {!readOnly && (
        <div className="ptg-tools" data-testid="pt-fill-bar">
          <Wand2 size={14} />
          <span className="ptg-tools-label">Fill a whole column</span>
          <select
            className="select"
            value={fillColumn}
            aria-label="Column to fill"
            onChange={(e) => {
              setFillColumn(e.target.value as ColumnName | "");
              setFillValue("");
            }}
            data-testid="pt-fill-column"
          >
            <option value="">Column…</option>
            {fillable.map((c) => (
              <option key={c.name} value={c.name}>
                {c.name}
              </option>
            ))}
          </select>
          {fillColumn && (
            <>
              <div className="ptg-tools-value">
                <Combobox
                  value={fillValue}
                  options={fillChoices.map((c) => c.label)}
                  onChange={setFillValue}
                  placeholder={`Approved ${fillColumn.toLowerCase()}…`}
                  testId="pt-fill-value"
                />
              </div>
              <select
                className="select"
                value={fillScope}
                aria-label="Which rows"
                onChange={(e) => setFillScope(e.target.value as FillScope)}
                data-testid="pt-fill-scope"
              >
                <option value="blank">Blank rows only ({fillBlank})</option>
                <option value="all">Every row ({lines.length})</option>
              </select>
              <button
                className="btn btn-sm btn-primary"
                disabled={!fillValue}
                onClick={applyColumnFill}
                data-testid="pt-fill-apply"
              >
                Fill
              </button>
            </>
          )}
          <span className="ptg-spacer" />
          <span className="ptg-tools-label">Quick fill blanks</span>
          {COLOUR_TIERS.map((tier) => {
            const choice = findChoice(colours, tier);
            return (
              <button
                key={tier}
                className="btn btn-sm"
                disabled={!choice || colourBlank === 0}
                title={
                  choice
                    ? `Set COLOR to ${choice.label} on the ${colourBlank} row(s) with no colour`
                    : `${tier} is not an approved colour`
                }
                onClick={() => choice && fill("COLOR", choice, "blank")}
                data-testid={`pt-quickfill-${tier}`}
              >
                {tier.charAt(0) + tier.slice(1).toLowerCase()}
              </button>
            );
          })}
          <button
            className="btn btn-sm"
            disabled={suggestionBlank === 0}
            title={`Fill ${suggestionBlank} blank SUB CATEGORY / TYPE cell(s) from the ITEM's suggestion`}
            onClick={fillFromSuggestions}
            data-testid="pt-quickfill-suggested"
          >
            From suggestions
          </button>
          <button
            className="btn btn-sm"
            disabled={!freeSize || sizeBlank === 0}
            title={
              freeSize
                ? `Set SIZE to ${freeSize.label} on the ${sizeBlank} row(s) with no size`
                : "FREE SIZE is not an approved size"
            }
            onClick={() => freeSize && fill("SIZE", freeSize, "blank")}
            data-testid="pt-quickfill-freesize"
          >
            Free size
          </button>
        </div>
      )}

      {rule && (
        <RulePanel
          rule={rule}
          options={ruleOptions()}
          brandName={choices.brands.find((b) => b.id === String(rule.brandId))?.label ?? ""}
          busy={busy}
          onChange={setRule}
          onPropose={proposeRule}
          onCancel={() => setRule(null)}
        />
      )}

      <div className="ptg-progress" data-testid="pt-progress">
        <span>
          {progress.reviewed} of {progress.total} rows reviewed
        </span>
        <progress value={progress.reviewed} max={Math.max(1, progress.total)} />
        {Object.keys(pendingReviews).length > 0 && (
          <span className="muted">
            {Object.keys(pendingReviews).length} marked — save to record the review
          </span>
        )}
      </div>

      <div className="ptg-scroll">
        <table className="ptg-grid" ref={gridRef} onPaste={onGridPaste} data-testid="pt-grid">
          <thead>
            <tr>
              <th className="ptg-fz ptg-fz-no" scope="col">
                #
              </th>
              <th className="ptg-fz ptg-fz-sel" scope="col">
                <input
                  type="checkbox"
                  aria-label="Select every row on this page"
                  checked={allOnPage}
                  disabled={readOnly}
                  onChange={(e) =>
                    setSelected((prev) => {
                      const next = { ...prev };
                      for (const line of pageLines) next[line.line_key] = e.target.checked;
                      return next;
                    })
                  }
                  data-testid="pt-select-page"
                />
              </th>
              <th className="ptg-fz ptg-fz-item" scope="col">
                Item
              </th>
              <th className="ptg-fz ptg-fz-rev" scope="col">
                Review
              </th>
              {columns.map((c) => (
                <th
                  key={c.name}
                  scope="col"
                  className={`${c.numeric ? "num " : ""}${c.kind === "calculated" ? "ptg-calc-head" : ""}`}
                  title={c.kind === "calculated" ? "Calculated by the server" : undefined}
                >
                  {c.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pageLines.map((line, rowIndex) => {
              const row = edits[line.line_key];
              const pasted = pastes[line.line_key];
              const rowDirty =
                Object.keys(row ?? {}).length > 0 || Object.keys(pasted ?? {}).length > 0;
              const issues = sortIssues(line.issues);
              // The cells a refused save (or submit) named on this row.
              const refused = sortIssues(
                submitIssues.filter((found) => found.line_key === line.line_key && found.field),
              ).cells;
              const number = shownPage * PAGE_SIZE + rowIndex + 1;
              return (
                <tr
                  key={line.line_key}
                  data-testid={`pt-row-${line.line_key}`}
                  className={rowDirty ? "ptg-row-dirty" : ""}
                >
                  <td className="ptg-fz ptg-fz-no mono">{number}</td>
                  <td className="ptg-fz ptg-fz-sel">
                    <input
                      type="checkbox"
                      aria-label={`Select row ${number}`}
                      checked={Boolean(selected[line.line_key])}
                      disabled={readOnly}
                      onChange={(e) =>
                        setSelected((prev) => ({ ...prev, [line.line_key]: e.target.checked }))
                      }
                      data-testid={`pt-select-${line.line_key}`}
                    />
                  </td>
                  <td className="ptg-fz ptg-fz-item">
                    <ItemCell
                      line={line}
                      row={row}
                      rowIssues={issues.row}
                      canPropose={!readOnly && canProposeItems}
                      busy={busy}
                      onPropose={() => proposeItem(line)}
                    />
                  </td>
                  <td className="ptg-fz ptg-fz-rev">
                    {line.reviewed && !rowDirty ? (
                      <span
                        className="chip chip-green"
                        data-testid={`pt-reviewed-${line.line_key}`}
                      >
                        Reviewed
                      </span>
                    ) : pendingReviews[line.line_key] ? (
                      <span className="chip chip-amber">Marked</span>
                    ) : (
                      <button
                        className="btn btn-sm"
                        disabled={readOnly || !line.row_hash || rowDirty}
                        title={rowDirty ? "Save this row's changes first" : undefined}
                        onClick={() => markReviewed(line)}
                        data-testid={`pt-review-${line.line_key}`}
                      >
                        Review
                      </button>
                    )}
                  </td>
                  {columns.map((column, colIndex) => (
                    <GridCell
                      key={column.name}
                      column={column}
                      line={line}
                      row={row}
                      pasted={pasted}
                      pos={{ row: rowIndex, col: colIndex }}
                      problems={[
                        ...(issues.cells[column.name] ?? []),
                        ...(refused[column.name] ?? []),
                      ]}
                      refused={Boolean(refused[column.name]?.length)}
                      choices={choices}
                      readOnly={readOnly}
                      moneyText={moneyText[`${line.line_key}:${column.name}`]}
                      canRule={!readOnly && canProposeRules && canProposeRule(column, line)}
                      onFocus={(at) =>
                        setCursor((prev) =>
                          prev.row === at.row && prev.col === at.col ? prev : at,
                        )
                      }
                      onKeyDown={onCellKeyDown}
                      onPick={(label) => pick(line, column, label)}
                      onText={(text) =>
                        column.kind === "money"
                          ? editMoney(line.line_key, column.name, text)
                          : edit(line.line_key, column.name, text)
                      }
                      onAccept={(choice) => acceptSuggestion(line, column, choice)}
                      onRule={() => openRule(line, column)}
                    />
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {pages > 1 && (
        <div className="ptg-pager" data-testid="pt-pager">
          <button
            className="btn btn-sm"
            disabled={shownPage === 0}
            onClick={() => {
              setPage(shownPage - 1);
              setSelected({});
            }}
            data-testid="pt-page-prev"
          >
            Previous
          </button>
          <span>
            Rows {shownPage * PAGE_SIZE + 1}–{Math.min(lines.length, (shownPage + 1) * PAGE_SIZE)}{" "}
            of {lines.length}
          </span>
          <button
            className="btn btn-sm"
            disabled={shownPage >= pages - 1}
            onClick={() => {
              setPage(shownPage + 1);
              setSelected({});
            }}
            data-testid="pt-page-next"
          >
            Next
          </button>
        </div>
      )}

      <div className="ptg-actions">
        <button
          className="btn btn-sm"
          disabled={readOnly}
          onClick={reviewSelected}
          data-testid="pt-review-selected"
        >
          Review selected rows on this page
        </button>
        <button
          className="btn btn-primary"
          disabled={busy || readOnly || !dirty || compare !== null}
          title={compare ? "Choose which version to keep first" : undefined}
          onClick={save}
          data-testid="pt-save"
        >
          <Save size={14} /> Save changes
        </button>
        <button
          className="btn btn-cta"
          disabled={
            busy || readOnly || blockers.length > 0 || state !== "draft" || compare !== null
          }
          onClick={submit}
          data-testid="pt-submit"
        >
          <Send size={14} /> Submit for approval
        </button>
      </div>
      {!readOnly && blockers.length > 0 && (
        <ul className="warn-note ptg-blockers" data-testid="pt-submit-blockers">
          {blockers.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function rowLabel(lines: GridLine[], lineKey: string | undefined): string {
  if (!lineKey) return "";
  const index = lines.findIndex((line) => line.line_key === lineKey);
  return index >= 0 ? `Row ${index + 1}: ` : "";
}

// ---------------------------------------------------------------------------
// One cell
// ---------------------------------------------------------------------------

function GridCell({
  column,
  line,
  row,
  pasted,
  pos,
  problems,
  refused,
  choices,
  readOnly,
  moneyText,
  canRule,
  onFocus,
  onKeyDown,
  onPick,
  onText,
  onAccept,
  onRule,
}: {
  column: GridColumn;
  line: GridLine;
  row: RowEdits | undefined;
  pasted: Partial<Record<ColumnName, string>> | undefined;
  pos: GridPos;
  problems: PtRowIssue[];
  refused: boolean;
  choices: GridChoices;
  readOnly: boolean;
  moneyText: string | undefined;
  canRule: boolean;
  onFocus: (pos: GridPos) => void;
  onKeyDown: (e: ReactKeyboardEvent<HTMLTableCellElement>, pos: GridPos) => void;
  onPick: (label: string) => void;
  onText: (text: string) => void;
  onAccept: (choice: SuggestionChoice) => void;
  onRule: () => void;
}) {
  const isPasted = pasted?.[column.name] !== undefined;
  const edited = (row !== undefined && column.name in row) || isPasted;
  const text = cellText(column, line, row, choices, pasted);
  const origin = line.origins?.[column.name];
  const waiting = line.suggestions?.[column.name];
  const offered = !edited && origin === "suggestion" ? (waiting?.choices ?? []) : [];
  const tone = cellTone({
    column,
    ...(origin ? { origin } : {}),
    edited,
    blank: text === "",
    problem: problems.length > 0,
    suggested: offered.length > 0,
    refused,
  });
  const options = isDropdown(column) ? choicesFor(column, choices) : null;
  const slug = columnSlug(column.name);
  // The KDPS work sheet's own checks: a hint, never a row issue.
  const check = edited ? undefined : sheetChecks(line)[column.name];
  const title = [
    TONE_WORDS[tone],
    ...problems.map((p) => issueLabel(p)),
    waiting?.source ? `The file says: ${waiting.source}` : "",
    check ?? "",
  ]
    .filter(Boolean)
    .join(" · ");

  let body;
  if (readOnly || column.kind === "calculated" || (isDropdown(column) && options === null)) {
    body = <span>{text || "—"}</span>;
  } else if (options) {
    body = (
      <div data-cell-control>
        <Combobox
          value={text}
          options={options.map((c) => c.label)}
          onChange={onPick}
          placeholder="—"
          size="cell"
          testId={controlTestId(column, line.line_key)}
        />
      </div>
    );
  } else {
    body = (
      <input
        data-cell-control
        className="ptg-cell-input"
        aria-label={`${column.name}, row ${pos.row + 1}`}
        inputMode={column.numeric ? "decimal" : undefined}
        value={column.kind === "money" && moneyText !== undefined && !isPasted ? moneyText : text}
        onChange={(e) => onText(e.target.value)}
        data-testid={controlTestId(column, line.line_key)}
      />
    );
  }

  return (
    <td
      className={`ptg-cell${column.numeric ? " num" : ""}${isDropdown(column) ? " ptg-wide" : ""}`}
      data-cell={`${pos.row}-${pos.col}`}
      data-tone={tone}
      data-pasted={isPasted ? "true" : undefined}
      data-check={check ? "true" : undefined}
      data-testid={`pt-cell-${slug}-${line.line_key}`}
      title={title}
      tabIndex={-1}
      onFocus={() => onFocus(pos)}
      // Capture, so an arrow on a closed dropdown moves the cursor instead of
      // opening the list; an open list keeps its own keys.
      onKeyDownCapture={(e) => onKeyDown(e, pos)}
    >
      {body}
      {check && (
        <span className="ptg-check" aria-label={check}>
          !
        </span>
      )}
      {waiting?.source && !edited && origin === "suggestion" && (
        <span className="ptg-file-text">File: {waiting.source}</span>
      )}
      {offered.length > 0 && !readOnly && (
        <span className="ptg-suggestions">
          {offered.slice(0, 3).map((choice, index) => (
            <button
              key={choice.value_id}
              type="button"
              className="ptg-chip-btn"
              title={`The file says ${waiting?.source}. Accept ${choice.label}.`}
              onClick={() => onAccept(choice)}
              data-testid={`pt-suggest-${slug}-${line.line_key}-${index}`}
            >
              {choice.label}?
            </button>
          ))}
        </span>
      )}
      {canRule && (
        <button
          type="button"
          className="ptg-link-btn"
          onClick={onRule}
          data-testid={`pt-propose-rule-${slug}-${line.line_key}`}
        >
          Propose rule
        </button>
      )}
    </td>
  );
}

// ---------------------------------------------------------------------------
// The row's item
// ---------------------------------------------------------------------------

function ItemCell({
  line,
  row,
  rowIssues,
  canPropose,
  busy,
  onPropose,
}: {
  line: GridLine;
  row: RowEdits | undefined;
  rowIssues: PtRowIssue[];
  canPropose: boolean;
  busy: boolean;
  onPropose: () => void;
}) {
  const status = itemStatus(line, row);
  return (
    <div className="ptg-item" data-testid={`pt-item-${line.line_key}`} data-status={status}>
      {status === "matched" && (
        <span className="chip chip-green" title={line.alias_as_used ?? undefined}>
          Item found
        </span>
      )}
      {status === "new_item" && (
        <>
          <span className="chip chip-amber">New item</span>
          {canPropose && (
            <button
              className="btn btn-sm"
              disabled={busy}
              onClick={onPropose}
              data-testid={`pt-propose-item-${line.line_key}`}
            >
              Propose item
            </button>
          )}
        </>
      )}
      {status === "proposed" && (
        <span className="chip chip-blue" data-testid={`pt-item-proposed-${line.line_key}`}>
          Proposed — save to use it
        </span>
      )}
      {status === "waiting" && (
        <span className="chip chip-amber" data-testid={`pt-item-waiting-${line.line_key}`}>
          New item – waiting for confirmation
        </span>
      )}
      {status === "rejected" && (
        <span className="chip chip-red" data-testid={`pt-item-rejected-${line.line_key}`}>
          New item rejected – {itemRejectReasonLabel(line.item_rejected?.reason_code)}
        </span>
      )}
      {status === "ambiguous" && <span className="chip chip-red">Several items match</span>}
      {status === "none" && <span className="chip chip-red">No item</span>}
      {rowIssues.length > 0 && (
        <details className="ptg-row-issues" data-testid={`pt-issues-${line.line_key}`}>
          <summary>{rowIssues.length} to fix</summary>
          <ul>
            {rowIssues.map((issue, index) => (
              <li key={`${issue.code}-${index}`}>{issueLabel(issue)}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Proposing a rule from a cell
// ---------------------------------------------------------------------------

function RulePanel({
  rule,
  options,
  brandName,
  busy,
  onChange,
  onPropose,
  onCancel,
}: {
  rule: RuleDraft;
  options: Choice[];
  brandName: string;
  busy: boolean;
  onChange: (rule: RuleDraft) => void;
  onPropose: () => void;
  onCancel: () => void;
}) {
  const label = options.find((c) => c.id === rule.targetId)?.label ?? "";
  return (
    <div className="ptg-tools ptg-rule" data-testid="pt-rule-panel">
      <span className="ptg-tools-label">Propose a rule</span>
      {rule.brandId ? (
        <>
          <span>When {brandName || "this brand"}'s file says</span>
          <input
            className="input"
            value={rule.source}
            aria-label="What the brand's file says"
            onChange={(e) => onChange({ ...rule, source: e.target.value })}
            data-testid="pt-rule-source"
          />
          <span>its {rule.column} is</span>
          <div className="ptg-tools-value">
            <Combobox
              value={label}
              options={options.map((c) => c.label)}
              onChange={(picked) =>
                onChange({ ...rule, targetId: options.find((c) => c.label === picked)?.id ?? "" })
              }
              placeholder={`Approved ${rule.column.toLowerCase()}…`}
              testId="pt-rule-target"
            />
          </div>
          <button
            className="btn btn-sm btn-primary"
            disabled={busy || !rule.source.trim() || !rule.targetId}
            onClick={onPropose}
            data-testid="pt-rule-submit"
          >
            Propose rule
          </button>
        </>
      ) : (
        <span>The row needs a brand before a rule can be proposed for its files.</span>
      )}
      <button className="btn btn-sm" onClick={onCancel} data-testid="pt-rule-cancel">
        Cancel
      </button>
      <span className="ptg-hint">
        A proposed rule applies nowhere until the product-master owner confirms it in Mapping rules.
        This row is not changed by it.
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// A clash with someone else's edit
// ---------------------------------------------------------------------------

/** What a clash keeps for the compare: the person's unsaved typed and pasted
 *  cells, and the rows as they were when those edits began. */
interface Compare {
  edits: Edits;
  pastes: Pastes;
  before: GridLine[];
}

function ComparePanel({
  compare,
  current,
  columns,
  choices,
  onKeepMine,
  onTakeTheirs,
}: {
  compare: Compare;
  current: GridLine[];
  columns: GridColumn[];
  choices: GridChoices;
  onKeepMine: () => void;
  onTakeTheirs: () => void;
}) {
  const rows: { key: string; number: number; column: GridColumn; mine: string; theirs: string }[] =
    [];
  compare.before.forEach((line, index) => {
    const row = compare.edits[line.line_key];
    const pasted = compare.pastes[line.line_key];
    if (!row && !pasted) return;
    const now = current.find((l) => l.line_key === line.line_key);
    for (const column of columns) {
      if (!(row && column.name in row) && pasted?.[column.name] === undefined) continue;
      rows.push({
        key: `${line.line_key}-${column.name}`,
        number: index + 1,
        column,
        mine: cellText(column, line, row, choices, pasted) || "blank",
        theirs: now ? cellText(column, now, undefined, choices) || "blank" : "row removed",
      });
    }
  });
  return (
    <div className="warn-note ptg-compare" data-testid="pt-compare">
      <AlertTriangle size={14} />
      <div>
        <p>
          This PT changed on the server while you were editing. Choose which version to keep — your
          edits stay local until you save again, and either choice needs a fresh review.
        </p>
        <table className="data">
          <thead>
            <tr>
              <th scope="col">Row</th>
              <th scope="col">Column</th>
              <th scope="col">Your value</th>
              <th scope="col">Current value</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} data-testid={`pt-compare-row-${r.key}`}>
                <td>{r.number}</td>
                <td>{r.column.name}</td>
                <td>{r.mine}</td>
                <td>{r.theirs}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="ptg-actions">
          <button className="btn btn-primary" onClick={onKeepMine} data-testid="pt-compare-mine">
            Keep mine
          </button>
          <button className="btn btn-sm" onClick={onTakeTheirs} data-testid="pt-compare-theirs">
            Take theirs
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// After submit: the reconciliation against the receipt
// ---------------------------------------------------------------------------

function ReconciliationPanel({ reconciliation }: { reconciliation: PtReconciliation }) {
  return (
    <div className="card section-card pt-reconciliation" data-testid="pt-reconciliation">
      <h4 className="gr-h4">
        <CheckCircle2 size={15} /> Reconciliation against the receipt
      </h4>
      <p className={reconciliation.passed ? "ok-note" : "warn-note"}>
        {reconciliation.passed
          ? "Every proposed line reconciles against what the receipt counted."
          : "This PT does not fully reconcile — see the gap named below."}
      </p>
      <table className="data" data-testid="pt-reconciliation-lines">
        <thead>
          <tr>
            <th>Lot</th>
            <th className="num">Counted</th>
            <th className="num">Proposed</th>
            <th className="num">Already covered</th>
            <th className="num">Still held</th>
          </tr>
        </thead>
        <tbody>
          {reconciliation.lines.map((line) => (
            <tr key={line.lot_id} data-testid={`pt-recon-${line.lot_id}`}>
              <td>{line.lot_id.slice(0, 8)}</td>
              <td className="num">{line.counted_qty}</td>
              <td className="num">{line.proposed_qty}</td>
              <td className="num">{line.already_covered_qty}</td>
              <td className="num">{line.held_uncovered_qty}</td>
            </tr>
          ))}
          <tr className="pt-recon-total">
            <td>Total</td>
            <td className="num">{reconciliation.totals.counted_qty}</td>
            <td className="num">{reconciliation.totals.proposed_qty}</td>
            <td className="num">{reconciliation.totals.already_covered_qty}</td>
            <td className="num">{reconciliation.totals.held_uncovered_qty}</td>
          </tr>
        </tbody>
      </table>
      {reconciliation.totals.proposed_qty > reconciliation.totals.counted_qty && (
        <p className="warn-note" data-testid="pt-recon-gap">
          Gap: {reconciliation.totals.proposed_qty - reconciliation.totals.counted_qty} piece(s)
          proposed above what the receipt counted.
        </p>
      )}
    </div>
  );
}
