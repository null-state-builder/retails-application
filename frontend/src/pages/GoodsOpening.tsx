// Opening manifest, variance and opening-PT screen (ticket 10, synthetic
// tenants only). A preparer loads a manifest with its physical verification;
// the owner approves it; a row that disagrees needs its own distinct variance
// approval before "Create opening PT" includes it. The opening PT itself then
// reuses the PT editor (PT Work, `/goods/pt-work?pt=`) to submit, PT Work's To
// approve queue for the checker, and the acceptance panel on Receive Goods'
// Pending for site acceptance (OPS-17: those are no longer screens of their own).
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { rupeesToPaiseString, uploadEvidence } from "../lib/goodsPt";
import { ptWorkPath } from "../lib/ptWork";
import { SohImportPanel } from "./SohImport";
import {
  Denied,
  Feedback,
  Field,
  hold,
  listState,
  readFailure,
  useAllPages,
  useGoodsFetch,
  useStepUp,
  type Page,
  type ResourceDTO,
} from "../lib/goodsScreen";

interface ManifestListItem {
  id: string;
  site_id: string;
  dataset_key: string;
  batch_key: string;
  state: "draft" | "approved";
  row_count: number;
  created_at: string;
}

interface ManifestRow {
  id: string;
  source_row_key: string;
  row: {
    qty: number;
    condition: string;
    basic_paise: string;
    mrp_paise: string;
    hsn: string;
    season_id: string;
    /** OPS-03: the loader's deliberate "this cohort cannot be established". */
    season_unknown_historical?: boolean;
    older_origin_at: string | null;
    older_origin_ref: string | null;
    identity: { sku_id: string | null };
  };
  verification: { observed_qty: number; observed_condition: string };
  matches_verification: boolean;
  variance: { accepted_qty: number; reason_code: string } | null;
  variance_request_id: string | null;
  /** The latest governed correction, when the real season was later established.
   *  The row above still says what it always said. */
  season_correction: {
    from_season_id: string;
    to_season_id: string;
    reason: string;
    recorded_at: string;
  } | null;
}

/** One season of the master, as the goods season list answers it (E031).
 *  `historical_unknown` marks the one explicit unknown historical season, so
 *  this screen can offer and name it by meaning rather than by code. */
interface SeasonOption {
  id: string;
  code: string;
  name: string;
  historical_unknown: boolean;
}

interface SeasonPayload {
  code: string;
  name: string;
  historical_unknown?: boolean;
}

/** Every season a manifest row may name. Whole, not paged: it is a handful of
 *  rows and a missing one is a row somebody cannot load.
 *
 *  100 is the largest page the list answers, and asking for more is refused
 *  outright rather than capped - which left this picker permanently empty and
 *  every row unloadable. A business with more than a hundred live seasons needs
 *  a picker that pages; none has one yet. */
function useSeasons(): SeasonOption[] {
  const { value } = useGoodsFetch<Page<ResourceDTO<SeasonPayload>>, SeasonOption[]>(
    "/goods-v1/masters/seasons?limit=100",
    (r) =>
      (r.items ?? []).map((row) => ({
        id: String(row.id),
        code: row.data.code,
        name: row.data.name,
        historical_unknown: Boolean(row.data.historical_unknown),
      })),
    [],
  );
  return value;
}

/** What a row's season should read as on screen: the season's own name, and
 *  the plain words for the unknown historical one rather than its code. */
function seasonLabel(seasons: SeasonOption[], seasonId: string): string {
  const found = seasons.find((s) => s.id === String(seasonId));
  if (!found) return seasonId ? `Season ${seasonId}` : "—";
  return found.historical_unknown ? "Unknown historical season" : found.name;
}

interface ManifestDetail {
  site_id: string;
  dataset_key: string;
  batch_key: string;
  revision: number;
  approved: boolean;
  profile_version_id: string | null;
  /** E107 pages rows at 500; `total` is the whole manifest, `items` this page.
   *  `useManifestRows` walks every page and hands the screen one document whose
   *  `items` is the whole manifest and whose `next_cursor` is therefore null. */
  rows: { items: ManifestRow[]; next_cursor: string | null; total: number };
}

interface ApprovalDTO {
  id: string;
  subject_id: string | null;
  subject_revision: number;
  reviewed_hash: string;
  requested_action: string;
}

interface OpenException {
  id: string;
  kind: string;
  subject_id: string;
  state: string;
  revision: number;
}

interface ConfigVersionSummary {
  id: string;
  state: string;
}

interface ProfileSummary {
  payload?: { family?: string };
  effective_from: string;
  versions: ConfigVersionSummary[];
}

function pinnableVersionId(config: ProfileSummary): string | null {
  const effective = config.versions.find((v) => v.state === "effective");
  return (effective ?? config.versions[0])?.id ?? null;
}

/** The chosen cutoff day, as the last instant of that day in this browser's own
 *  zone. A bare `new Date("2026-09-17")` is UTC midnight, which in India is
 *  05:30 *that morning* - before the pricing profile the manifest must pin, so
 *  "today" was unusable. The end of the day is always after anything effective
 *  during it. */
function endOfDayIso(day: string): string | null {
  const [year, month, date] = day.split("-").map(Number);
  if (!year || !month || !date) return null;
  return new Date(year, month - 1, date, 23, 59, 59, 999).toISOString();
}

// ---------------------------------------------------------------------------
// New manifest
// ---------------------------------------------------------------------------

interface DraftRow {
  source_row_key: string;
  sku_id: string;
  qty: string;
  observed_qty: string;
  observed_condition: string;
  basic_rupees: string;
  mrp_rupees: string;
  hsn: string;
  season_id: string;
  older_origin_at: string;
  older_origin_ref: string;
}

function blankRow(index: number): DraftRow {
  return {
    source_row_key: `row-${index}`,
    sku_id: "",
    qty: "1",
    observed_qty: "1",
    observed_condition: "good",
    basic_rupees: "",
    mrp_rupees: "",
    hsn: "",
    season_id: "",
    older_origin_at: "",
    older_origin_ref: "",
  };
}

function NewManifestForm({
  siteId,
  onCreated,
}: {
  siteId: string;
  onCreated: (id: string) => void;
}) {
  const [batchKey, setBatchKey] = useState("");
  const [datasetKey, setDatasetKey] = useState("synthetic-batch");
  const [cutoffAt, setCutoffAt] = useState(() => new Date().toISOString().slice(0, 10));
  const [evidence, setEvidence] = useState<File | null>(null);
  const [rows, setRows] = useState<DraftRow[]>([blankRow(1)]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const seasons = useSeasons();
  const profiles = useGoodsFetch<Page<ResourceDTO<ProfileSummary>>, ResourceDTO<ProfileSummary>[]>(
    "/goods-v1/masters/configurations?kind=profile",
    (r) => (r.items ?? []).filter((row) => row.state === "approved" && pinnableVersionId(row.data)),
    [],
  );
  const [profileId, setProfileId] = useState("");

  useEffect(() => {
    if (!profileId && profiles.value.length === 1) {
      setProfileId(profiles.value[0] ? (pinnableVersionId(profiles.value[0].data) ?? "") : "");
    }
  }, [profiles.value, profileId]);

  function updateRow(index: number, patch: Partial<DraftRow>) {
    setRows((prev) => prev.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  async function submit() {
    if (!evidence) {
      setError("Choose the manifest's source evidence file.");
      return;
    }
    if (!profileId) {
      setError("Choose a pricing profile.");
      return;
    }
    const cutoff = endOfDayIso(cutoffAt);
    if (!cutoff) {
      setError("Choose the cutoff date.");
      return;
    }
    const badMoney = rows.findIndex(
      (r) =>
        rupeesToPaiseString(r.basic_rupees) == null || rupeesToPaiseString(r.mrp_rupees) == null,
    );
    if (badMoney >= 0) {
      setError(`Row ${badMoney + 1} needs a basic value and an MRP in rupees.`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const evidenceId = await uploadEvidence(evidence, { kind: "other", siteId });
      const { data } = await api.post<{ id: string }>("/goods-v1/ptmapper/opening-manifests", {
        site_id: siteId,
        batch_key: batchKey,
        dataset_key: datasetKey,
        cutoff_at: cutoff,
        source_evidence_id: evidenceId,
        profile_version_id: profileId,
        rows: rows.map((r) => ({
          row: {
            source_row_key: r.source_row_key,
            site_id: siteId,
            condition: "good",
            // A distinguishing raw_alias per row, so two rows of the same SKU
            // are not read as one duplicated physical identity (design §5.3):
            // real KDPS tag capture is stage 4, so the row's own key stands
            // in for the tag text this synthetic entry has none of.
            identity: { sku_id: r.sku_id, attributes: [], raw_alias: r.source_row_key },
            qty: Number(r.qty),
            basic_paise: rupeesToPaiseString(r.basic_rupees) ?? null,
            mrp_paise: rupeesToPaiseString(r.mrp_rupees) ?? null,
            hsn: r.hsn,
            season_id: r.season_id,
            // The declaration travels with the season, never instead of it:
            // the server refuses the unknown historical season undeclared,
            // and refuses the declaration over any other season (OPS-03).
            season_unknown_historical: seasons.some(
              (s) => s.id === r.season_id && s.historical_unknown,
            ),
            // R-INV-010 retains "older origins where known". Blank stays null -
            // unknown, never invented - and opens its own investigation.
            older_origin_at: endOfDayIso(r.older_origin_at),
            older_origin_ref: r.older_origin_ref || null,
          },
          verification: {
            observed_qty: Number(r.observed_qty),
            observed_condition: r.observed_condition,
          },
        })),
        ...goodsMeta(),
      });
      onCreated(data.id);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card section-card" data-testid="opening-new-manifest">
      <h3 className="h3">New opening manifest</h3>
      <Feedback error={error} ok="" />
      <div className="form-grid">
        <Field id="opening-batch" label="Batch key">
          <input
            id="opening-batch"
            className="input"
            value={batchKey}
            onChange={(e) => setBatchKey(e.target.value)}
            data-testid="opening-batch-key"
          />
        </Field>
        <Field id="opening-dataset" label="Dataset key">
          <input
            id="opening-dataset"
            className="input"
            value={datasetKey}
            onChange={(e) => setDatasetKey(e.target.value)}
            data-testid="opening-dataset-key"
          />
        </Field>
        <Field id="opening-cutoff" label="Cutoff date">
          <input
            id="opening-cutoff"
            type="date"
            className="input"
            value={cutoffAt}
            onChange={(e) => setCutoffAt(e.target.value)}
            data-testid="opening-cutoff-at"
          />
        </Field>
        <Field id="opening-evidence" label="Source evidence">
          <input
            id="opening-evidence"
            type="file"
            onChange={(e) => setEvidence(e.target.files?.[0] ?? null)}
            data-testid="opening-evidence-file"
          />
        </Field>
        <Field id="opening-profile" label="Pricing profile">
          <select
            id="opening-profile"
            className="input"
            value={profileId}
            onChange={(e) => setProfileId(e.target.value)}
            data-testid="opening-profile-select"
          >
            {profiles.value.length === 0 && <option value="">No approved profile</option>}
            {profiles.value.length > 1 && <option value="">Choose a profile</option>}
            {profiles.value.map((p) => (
              <option key={p.id} value={pinnableVersionId(p.data) ?? ""}>
                {p.data.payload?.family ?? "Unnamed family"} (since{" "}
                {p.data.effective_from.slice(0, 10)})
              </option>
            ))}
          </select>
        </Field>
      </div>

      <h4 className="h4">Rows</h4>
      {rows.map((r, i) => (
        <div className="form-grid" key={i} data-testid={`opening-row-${i}`}>
          <Field id={`row-${i}-key`} label="Source row key">
            <input
              id={`row-${i}-key`}
              className="input"
              value={r.source_row_key}
              onChange={(e) => updateRow(i, { source_row_key: e.target.value })}
              data-testid={`opening-row-${i}-key`}
            />
          </Field>
          <Field id={`row-${i}-sku`} label="SKU ID">
            <input
              id={`row-${i}-sku`}
              className="input"
              value={r.sku_id}
              onChange={(e) => updateRow(i, { sku_id: e.target.value })}
              data-testid={`opening-row-${i}-sku`}
            />
          </Field>
          <Field id={`row-${i}-qty`} label="Quantity">
            <input
              id={`row-${i}-qty`}
              type="number"
              className="input"
              value={r.qty}
              onChange={(e) => updateRow(i, { qty: e.target.value })}
              data-testid={`opening-row-${i}-qty`}
            />
          </Field>
          <Field id={`row-${i}-observed-qty`} label="Observed quantity">
            <input
              id={`row-${i}-observed-qty`}
              type="number"
              className="input"
              value={r.observed_qty}
              onChange={(e) => updateRow(i, { observed_qty: e.target.value })}
              data-testid={`opening-row-${i}-observed-qty`}
            />
          </Field>
          <Field id={`row-${i}-basic`} label="Basic (₹)">
            <input
              id={`row-${i}-basic`}
              className="input"
              value={r.basic_rupees}
              onChange={(e) => updateRow(i, { basic_rupees: e.target.value })}
              data-testid={`opening-row-${i}-basic`}
            />
          </Field>
          <Field id={`row-${i}-mrp`} label="MRP (₹)">
            <input
              id={`row-${i}-mrp`}
              className="input"
              value={r.mrp_rupees}
              onChange={(e) => updateRow(i, { mrp_rupees: e.target.value })}
              data-testid={`opening-row-${i}-mrp`}
            />
          </Field>
          <Field id={`row-${i}-hsn`} label="HSN">
            <input
              id={`row-${i}-hsn`}
              className="input"
              value={r.hsn}
              onChange={(e) => updateRow(i, { hsn: e.target.value })}
              data-testid={`opening-row-${i}-hsn`}
            />
          </Field>
          {/* The unknown historical season is one of the choices here, not a
              separate tick-box: the person picks a cohort or says there is
              none, and either way it is a deliberate choice, never a default
              (store and warehouse operations PRD §4). */}
          <Field id={`row-${i}-season`} label="Season">
            <select
              id={`row-${i}-season`}
              className="input"
              value={r.season_id}
              onChange={(e) => updateRow(i, { season_id: e.target.value })}
              data-testid={`opening-row-${i}-season`}
            >
              <option value="">Choose a season</option>
              {seasons.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.historical_unknown ? "Unknown historical season" : `${s.name} (${s.code})`}
                </option>
              ))}
            </select>
          </Field>
          <Field id={`row-${i}-older-at`} label="Older origin date (optional)">
            <input
              id={`row-${i}-older-at`}
              type="date"
              className="input"
              value={r.older_origin_at}
              onChange={(e) => updateRow(i, { older_origin_at: e.target.value })}
              data-testid={`opening-row-${i}-older-at`}
            />
          </Field>
          <Field id={`row-${i}-older-ref`} label="Older origin reference (optional)">
            <input
              id={`row-${i}-older-ref`}
              className="input"
              value={r.older_origin_ref}
              onChange={(e) => updateRow(i, { older_origin_ref: e.target.value })}
              data-testid={`opening-row-${i}-older-ref`}
            />
          </Field>
        </div>
      ))}
      <button
        className="btn btn-sm"
        onClick={() => setRows((prev) => [...prev, blankRow(prev.length + 1)])}
        data-testid="opening-add-row"
      >
        + Add row
      </button>

      <div className="form-actions">
        <button
          className="btn btn-cta"
          onClick={submit}
          disabled={busy || !batchKey}
          data-testid="opening-create-manifest"
        >
          Create manifest
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Older origin: the owner's dedicated "unavailable" closure (E241, GSA-T10)
// ---------------------------------------------------------------------------

/** GSA-T10's dedicated C-OWN action. It closes the investigation into a missing
 *  historical origin - with a reason and supporting evidence - and leaves the
 *  origin itself honestly unknown. The exception centre routes here for it, so
 *  the control has to live on the row, not only behind the API. */
function OriginUnavailableRow({
  row,
  siteId,
  exception,
  reviewedHash,
  reload,
}: {
  row: ManifestRow;
  siteId: string;
  exception: OpenException;
  reviewedHash: string;
  reload: () => void;
}) {
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const stepUp = useStepUp();

  async function confirm() {
    if (!evidence) {
      setError("Attach the evidence of what was searched.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const evidenceId = await uploadEvidence(evidence, { kind: "other", siteId });
      await stepUp.guarded(() =>
        api.post(`/goods-v1/exceptions/${exception.id}/confirm-origin-unavailable`, {
          manifest_row_id: row.id,
          reason_code: reason || "NO_HISTORICAL_RECORD",
          evidence_ids: [evidenceId],
          reviewed_hash: reviewedHash,
          ...goodsMeta(exception.revision),
        }),
      );
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card section-card" data-testid={`opening-origin-${row.source_row_key}`}>
      {stepUp.dialog}
      <h4 className="h4">Row {row.source_row_key}: older origin unknown</h4>
      <p className="muted">
        Confirming closes the investigation only. The origin stays unknown - it is never filled in.
      </p>
      <Feedback error={error} ok="" />
      <div className="form-grid">
        <Field id={`origin-${row.id}-reason`} label="Reason code">
          <input
            id={`origin-${row.id}-reason`}
            className="input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="NO_HISTORICAL_RECORD"
            data-testid={`opening-origin-${row.source_row_key}-reason`}
          />
        </Field>
        <Field id={`origin-${row.id}-evidence`} label="Supporting evidence">
          <input
            id={`origin-${row.id}-evidence`}
            type="file"
            onChange={(e) => setEvidence(e.target.files?.[0] ?? null)}
            data-testid={`opening-origin-${row.source_row_key}-evidence`}
          />
        </Field>
      </div>
      <div className="form-actions">
        <button
          className="btn btn-cta"
          onClick={confirm}
          disabled={busy}
          data-testid={`opening-origin-${row.source_row_key}-confirm`}
        >
          Confirm origin unavailable
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Reading a whole manifest (ticket 10A)
// ---------------------------------------------------------------------------

/** Pages of rows this walk will follow before it gives up and says so. E107
 *  answers 500 rows a page and a manifest holds at most 5,000, so ten pages
 *  reads the largest manifest there can be; twenty is the hard stop. */
const MAX_ROW_PAGES = 20;

/** How many times the walk starts over when the manifest is revised under it.
 *  A revision is somebody's deliberate edit, not a loop - three is generous. */
const MAX_RESTARTS = 3;

/** A draft the operator has typed for one row, kept by `source_row_key`. */
interface VarianceDraft {
  acceptedQty: string;
  reason: string;
}

/** What a row's variance fields say before anyone types: the quantity physical
 *  verification actually observed, which is the answer in almost every case. */
function blankDraft(row: ManifestRow): VarianceDraft {
  return { acceptedQty: String(row.verification.observed_qty), reason: "" };
}

/** Every row of one opening manifest, read through E107's `row_cursor`.
 *
 *  Reading only the first page hid rows 501 and after: they could not be
 *  reviewed, could not take a variance decision, and were still opened by the
 *  opening PT, which is built from the manifest server-side and never saw the
 *  screen's short list. So this walks the cursor to the end and hands the
 *  screen one document carrying every row.
 *
 *  The pages are separate reads, so a manifest revised part-way through would
 *  otherwise be stitched together out of two different manifests. Each page
 *  quotes the manifest's `revision` and `content_hash`; the moment either
 *  differs from the first page's, everything read so far belongs to a manifest
 *  that no longer exists and the walk starts again from the beginning.
 *
 *  `superseded` says the rows on screen are not the rows that were there
 *  before - either the walk restarted, or a row the operator could see last
 *  time is gone. Their typed drafts are theirs and are kept (they live in the
 *  detail view, keyed by `source_row_key`, not by row id); what is void is any
 *  decision already resting on a row that no longer exists, and the server
 *  refuses those itself on the revision it was given. */
export function useManifestRows(manifestId: string) {
  const [doc, setDoc] = useState<ResourceDTO<ManifestDetail> | null>(null);
  const [loading, setLoading] = useState(true);
  const [denied, setDenied] = useState(false);
  const [failure, setFailure] = useState("");
  const [superseded, setSuperseded] = useState(false);
  const [tick, setTick] = useState(0);
  const generation = useRef(0);
  const seen = useRef<{ manifestId: string; rowIds: Set<string> } | null>(null);

  useEffect(() => {
    const mine = ++generation.current;
    const base = `/goods-v1/ptmapper/opening-manifests/${manifestId}`;
    setLoading(true);
    setDenied(false);
    setFailure("");

    /** One walk of every page. `null` means the manifest changed under it. */
    async function walk(): Promise<ResourceDTO<ManifestDetail> | null> {
      let first: ResourceDTO<ManifestDetail> | null = null;
      let cursor: string | null = null;
      const rows: ManifestRow[] = [];
      for (let read = 0; read < MAX_ROW_PAGES; read += 1) {
        const query: string = cursor ? `?row_cursor=${encodeURIComponent(cursor)}` : "";
        const { data } = await api.get<ResourceDTO<ManifestDetail>>(`${base}${query}`);
        if (first && (data.revision !== first.revision || data.content_hash !== first.content_hash))
          return null;
        first = first ?? data;
        rows.push(...(data.data.rows.items ?? []));
        cursor = data.data.rows.next_cursor;
        if (!cursor) {
          // The manifest's own count of itself, not this walk's. If they differ
          // the walk did not see the manifest whole, and saying "N rows, all
          // reviewed here" over a short list is the one thing this must not do.
          const total = data.data.rows.total;
          if (rows.length !== total) {
            throw new Error(`Read ${rows.length} of this manifest's ${total} rows. Reload.`);
          }
          return {
            ...first,
            data: { ...first.data, rows: { items: rows, next_cursor: null, total } },
          };
        }
      }
      throw new Error("This manifest has more rows than this screen can read.");
    }

    void (async () => {
      try {
        let restarted = false;
        for (let attempt = 0; attempt < MAX_RESTARTS; attempt += 1) {
          const whole = await walk();
          if (mine !== generation.current) return;
          if (whole === null) {
            restarted = true;
            continue;
          }
          const rowIds = new Set(whole.data.rows.items.map((row) => row.id));
          // Only a row that went missing from *this* manifest says it was
          // revised; the rows of the last manifest looked at say nothing.
          const before = seen.current?.manifestId === manifestId ? seen.current.rowIds : null;
          const lost = before !== null && [...before].some((id) => !rowIds.has(id));
          seen.current = { manifestId, rowIds };
          setDoc(whole);
          setSuperseded(restarted || lost);
          setLoading(false);
          return;
        }
        if (mine !== generation.current) return;
        setFailure("The manifest kept changing while it was read. Reload to see it whole.");
        setLoading(false);
      } catch (e) {
        if (mine !== generation.current) return;
        const outcome = readFailure(e);
        setDenied(outcome.denied);
        setFailure(outcome.failure);
        setLoading(false);
      }
    })();
    return () => {
      generation.current += 1;
    };
  }, [manifestId, tick]);

  return { doc, loading, denied, failure, superseded, reload: () => setTick((t) => t + 1) };
}

// ---------------------------------------------------------------------------
// Manifest detail: approve, propose/approve variance, create opening PT
// ---------------------------------------------------------------------------

function VarianceRow({
  row,
  manifestId,
  revision,
  inbox,
  draft,
  onDraft,
  reload,
  reloadInbox,
}: {
  row: ManifestRow;
  manifestId: string;
  revision: number;
  inbox: ApprovalDTO[];
  /** What the operator has typed for this row, held by the detail view so a
   *  reload - or a revision that replaces every row - does not erase it. */
  draft: VarianceDraft;
  onDraft: (patch: Partial<VarianceDraft>) => void;
  reload: () => void;
  reloadInbox: () => void;
}) {
  const { session } = useAuth();
  const { acceptedQty, reason } = draft;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const stepUp = useStepUp();

  async function propose() {
    setBusy(true);
    setError("");
    try {
      await api.post(`/goods-v1/ptmapper/opening-manifests/${manifestId}/variances`, {
        manifest_row_id: row.id,
        accepted_qty: Number(acceptedQty),
        reason_code: reason || "PHYSICAL_COUNT_DIFFERS",
        evidence_ids: [],
        ...goodsMeta(revision),
      });
      reload();
      reloadInbox();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function decide(decision: "approve" | "reject") {
    const request = inbox.find((a) => a.id === row.variance_request_id);
    if (!request) {
      setError("This request isn't loaded yet - reloading.");
      reloadInbox();
      return;
    }
    setBusy(true);
    setError("");
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/approvals/${request.id}/decide`, {
          decision,
          reviewed_hash: request.reviewed_hash,
          ...goodsMeta(request.subject_revision),
        }),
      );
      reload();
      reloadInbox();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (row.matches_verification) return null;

  return (
    <div className="card section-card" data-testid={`opening-variance-${row.source_row_key}`}>
      {stepUp.dialog}
      <p className="lead">
        Row {row.source_row_key}: manifest says {row.row.qty}, verification found{" "}
        {row.verification.observed_qty} ({row.verification.observed_condition}).
      </p>
      <Feedback error={error} ok="" />
      {row.variance ? (
        <p className="ok-note">Variance accepted at {row.variance.accepted_qty}.</p>
      ) : row.variance_request_id ? (
        <div className="form-actions">
          <button
            className="btn btn-cta"
            onClick={() => decide("approve")}
            disabled={busy}
            data-testid={`opening-variance-approve-${row.source_row_key}`}
          >
            Approve variance
          </button>
          <button className="btn btn-sm" onClick={() => decide("reject")} disabled={busy}>
            Reject
          </button>
        </div>
      ) : hold(session, "pt.prepare.opening") ? (
        <div className="form-grid">
          <Field id={`variance-qty-${row.id}`} label="Accepted quantity">
            <input
              id={`variance-qty-${row.id}`}
              type="number"
              className="input"
              value={acceptedQty}
              onChange={(e) => onDraft({ acceptedQty: e.target.value })}
              data-testid={`opening-variance-qty-${row.source_row_key}`}
            />
          </Field>
          <Field id={`variance-reason-${row.id}`} label="Reason">
            <input
              id={`variance-reason-${row.id}`}
              className="input"
              value={reason}
              onChange={(e) => onDraft({ reason: e.target.value })}
              data-testid={`opening-variance-reason-${row.source_row_key}`}
            />
          </Field>
          <button
            className="btn btn-cta"
            onClick={propose}
            disabled={busy}
            data-testid={`opening-variance-propose-${row.source_row_key}`}
          >
            Propose variance
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function ManifestDetailView({
  manifestId,
  onBack,
}: {
  manifestId: string;
  onBack: () => void;
}) {
  const { session } = useAuth();
  const navigate = useNavigate();
  const { doc, loading, denied, failure, superseded, reload } = useManifestRows(manifestId);
  // E169's inbox answers flat ApprovalDTO items (approvals.goods_services.approval_dto),
  // never ResourceDTO-wrapped like a document read - so this reads the pages directly
  // rather than through `useResourceList`, which would look for a `.data` this has none of.
  //
  // Every page of it, not the first: a manifest of any size can have more rows
  // awaiting a variance decision than one inbox page holds, and a request this
  // screen cannot see is a row the operator cannot decide.
  const {
    items: inbox,
    denied: inboxDenied,
    failure: inboxFailure,
    reload: reloadInbox,
  } = useAllPages<ApprovalDTO>("/goods-v1/approvals/inbox?limit=100");
  // The owner's origin-unavailable closure (E241) acts on the exception, not the
  // manifest, so the row needs its own open investigation to act on - and, for
  // the same reason as the inbox, every page of them. Scoped to this manifest's
  // own site (E185 takes `site_id`), so a busy tenant's other sites do not have
  // to be paged through to find one row's investigation.
  const {
    items: exceptions,
    denied: exceptionsDenied,
    failure: exceptionsFailure,
    reload: reloadExceptions,
  } = useAllPages<OpenException>(
    doc ? `/goods-v1/exceptions?limit=100&site_id=${doc.data.site_id}` : null,
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Keyed by `source_row_key`, which survives a revision, and not by row id,
  // which does not: a manifest revised under the operator gives every row a new
  // id, and typing that took a while to do should outlive that.
  const [drafts, setDrafts] = useState<Record<string, VarianceDraft>>({});
  const stepUp = useStepUp();
  const seasons = useSeasons();

  if (loading) return <span className="muted">Loading…</span>;
  // A real failure is named before the generic "not found" card: a manifest
  // being revised faster than it can be read is not a manifest the reader may
  // not see, and telling them so would send them to the wrong person.
  if (failure) return <span className="warn-note">{failure}</span>;
  if (denied || !doc) return <Denied what="opening manifest" />;

  const manifest = doc.data;
  const manifestApproval = inbox.find(
    (a) => a.subject_id === manifestId && a.requested_action === "opening.manifest.approve",
  );

  async function decideManifest(decision: "approve" | "reject") {
    if (!manifestApproval) return;
    setBusy(true);
    setError("");
    try {
      await stepUp.guarded(() =>
        api.post(`/goods-v1/approvals/${manifestApproval.id}/decide`, {
          decision,
          reviewed_hash: manifestApproval.reviewed_hash,
          ...goodsMeta(manifestApproval.subject_revision),
        }),
      );
      reload();
      reloadInbox();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function createOpeningPt() {
    if (!manifest.profile_version_id) {
      setError("This manifest's approved version has no pinned pricing profile.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const { data } = await api.post<{ id: string }>(
        `/goods-v1/ptmapper/files/from-manifest/${manifestId}`,
        { profile_version_id: manifest.profile_version_id, ...goodsMeta() },
      );
      navigate(ptWorkPath({ pt: data.id }));
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const rows = manifest.rows.items;
  const unresolved = rows.filter((r) => !r.matches_verification && !r.variance);

  return (
    <div className="card section-card" data-testid="opening-manifest-detail">
      {stepUp.dialog}
      <button className="btn btn-sm" onClick={onBack}>
        ← Manifests
      </button>
      <h3 className="h3">
        {manifest.batch_key} · {doc.state}
      </h3>
      <Feedback error={error} ok="" />
      {superseded && (
        <p className="warn-note" data-testid="opening-superseded">
          This manifest was revised while it was on screen. These are its current rows; any variance
          proposed against the rows it replaced no longer counts. What you had typed has been kept.
        </p>
      )}
      <p className="muted" data-testid="opening-row-total">
        {rows.length === 1 ? "1 row" : `${rows.length} rows`}, all reviewed here.
      </p>
      {/* Without this the approvals simply do not appear, and a screen with no
          approve button reads as "nothing to decide" rather than "this could
          not be read" - the ticket's own failure mode on the error path. */}
      {(inboxDenied || inboxFailure) && (
        <p className="warn-note" data-testid="opening-inbox-unread">
          The approvals waiting on this manifest could not be read, so no decision is offered here.{" "}
          {inboxFailure}
        </p>
      )}
      {(exceptionsDenied || exceptionsFailure) && (
        <p className="warn-note" data-testid="opening-exceptions-unread">
          Open investigations could not be read, so a row with an unknown older origin shows no
          closure here. {exceptionsFailure}
        </p>
      )}
      {manifestApproval && (
        <div className="form-actions">
          <button
            className="btn btn-cta"
            onClick={() => decideManifest("approve")}
            disabled={busy}
            data-testid="opening-approve-manifest"
          >
            Approve manifest
          </button>
          <button className="btn btn-sm" onClick={() => decideManifest("reject")} disabled={busy}>
            Reject
          </button>
        </div>
      )}
      <table className="table">
        <thead>
          <tr>
            <th>Row</th>
            <th>SKU</th>
            <th>Season</th>
            <th>Qty</th>
            <th>Verified</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} data-testid={`opening-row-${row.source_row_key}-summary`}>
              <td>{row.source_row_key}</td>
              <td>{row.row.identity.sku_id}</td>
              {/* The season in force, and - when a correction established the
                  real cohort later - the one this row was loaded under, which
                  stays part of the evidence (OPS-03). */}
              <td data-testid={`opening-row-${row.source_row_key}-season-label`}>
                {seasonLabel(seasons, row.season_correction?.to_season_id ?? row.row.season_id)}
                {row.season_correction && (
                  <span className="muted">
                    {" "}
                    (was {seasonLabel(seasons, row.season_correction.from_season_id)})
                  </span>
                )}
              </td>
              <td>{row.row.qty}</td>
              <td>
                {row.verification.observed_qty} ({row.verification.observed_condition})
              </td>
              <td>
                {row.matches_verification
                  ? "matches"
                  : row.variance
                    ? "variance approved"
                    : "needs variance"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.map((row) => {
        const investigation = exceptions.find(
          (e) =>
            e.kind === "opening_origin_unavailable" &&
            e.subject_id === `manifest_row:${row.id}` &&
            e.state === "open",
        );
        return investigation ? (
          <OriginUnavailableRow
            key={`origin-${row.id}`}
            row={row}
            siteId={manifest.site_id}
            exception={investigation}
            reviewedHash={doc.content_hash}
            reload={() => {
              reload();
              reloadExceptions();
            }}
          />
        ) : null;
      })}
      {rows
        .filter((r) => !r.matches_verification)
        .map((row) => (
          <VarianceRow
            key={row.id}
            row={row}
            manifestId={manifestId}
            revision={manifest.revision}
            inbox={inbox}
            draft={drafts[row.source_row_key] ?? blankDraft(row)}
            onDraft={(patch) =>
              setDrafts((prev) => ({
                ...prev,
                [row.source_row_key]: {
                  ...(prev[row.source_row_key] ?? blankDraft(row)),
                  ...patch,
                },
              }))
            }
            reload={reload}
            reloadInbox={reloadInbox}
          />
        ))}
      {manifest.approved && hold(session, "pt.prepare.opening") && (
        <div className="form-actions">
          <button
            className="btn btn-cta"
            onClick={createOpeningPt}
            disabled={busy || unresolved.length > 0}
            data-testid="opening-create-pt"
          >
            Create opening PT
          </button>
          {unresolved.length > 0 && (
            <p className="muted">
              {unresolved.length} row(s) still need a variance decision before the opening PT can
              include them.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

export function GoodsOpeningPage() {
  const { session } = useAuth();
  const sites = session?.sites ?? [];
  const [siteId, setSiteId] = useState("");
  const [params, setParams] = useSearchParams();
  const manifestId = params.get("manifest");
  const [showForm, setShowForm] = useState(false);
  const canPrepare = hold(session, "pt.prepare.opening");
  const mayInspectManifest =
    canPrepare ||
    ["opening.manifest.approve", "opening.variance.approve", "pt.approve.opening"].some((action) =>
      hold(session, action),
    );

  useEffect(() => {
    if (!siteId && sites[0]) setSiteId(sites[0].id);
  }, [sites, siteId]);

  // E106's list answers flat OpeningManifestSummaryDTO items, not ResourceDTO-wrapped.
  const {
    value: manifestPage,
    loading: manifestsLoading,
    failure: manifestsFailure,
  } = useGoodsFetch<
    Page<ManifestListItem> & { capabilities?: { manual_manifest: boolean } },
    { items: ManifestListItem[]; manual: boolean }
  >(
    siteId ? `/goods-v1/ptmapper/opening-manifests?site_id=${siteId}&limit=100` : null,
    (r) => ({ items: r.items ?? [], manual: r.capabilities?.manual_manifest === true }),
    { items: [], manual: false },
  );
  const manifests = manifestPage.items;

  return (
    <div className="page-pad">
      <PageHeader
        title="Opening stock"
        lead="Reviewed source stock, opening manifests, variances and physical acceptance."
      />
      {manifestId && mayInspectManifest ? (
        // Keyed, so opening a second manifest starts clean rather than
        // carrying the first one's typed variance drafts across to it.
        <ManifestDetailView key={manifestId} manifestId={manifestId} onBack={() => setParams({})} />
      ) : (
        <>
          <div className="form-grid">
            <Field id="opening-site" label="Site">
              <select
                id="opening-site"
                className="input"
                value={siteId}
                onChange={(e) => setSiteId(e.target.value)}
                data-testid="opening-site-select"
              >
                {sites.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          {siteId && <SohImportPanel key={siteId} siteId={siteId} />}
          {canPrepare && manifestPage.manual && !showForm && (
            <button
              className="btn btn-cta"
              onClick={() => setShowForm(true)}
              data-testid="opening-new"
            >
              + New opening manifest
            </button>
          )}
          {showForm && manifestPage.manual && siteId && (
            <NewManifestForm
              siteId={siteId}
              onCreated={(id) => {
                setShowForm(false);
                setParams({ manifest: id });
              }}
            />
          )}
          <table className="table" data-testid="opening-manifest-list">
            <thead>
              <tr>
                <th>Batch</th>
                <th>Dataset</th>
                <th>Rows</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {(() => {
                const empty = listState(
                  {
                    loading: manifestsLoading,
                    failure: manifestsFailure,
                    empty: manifests.length === 0,
                  },
                  "No opening manifests yet.",
                );
                return empty ? (
                  <tr>
                    <td colSpan={4}>{empty}</td>
                  </tr>
                ) : null;
              })()}
              {manifests.map((row) => (
                <tr
                  key={row.id}
                  className={mayInspectManifest ? "row-clickable" : ""}
                  onClick={() => {
                    if (mayInspectManifest) setParams({ manifest: row.id });
                  }}
                  data-testid={`opening-manifest-row-${row.batch_key}`}
                >
                  <td>{row.batch_key}</td>
                  <td>{row.dataset_key}</td>
                  <td>{row.row_count}</td>
                  <td>{row.state}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export default GoodsOpeningPage;
