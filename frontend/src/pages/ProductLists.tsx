// Product lists (/setup/products): every list a PT uses, from the KDPS master sheet.
//
// The uploader drops the KDPS PT file; only its first sheet ("Master Sheet") is
// read. They see what it would change - new values, brands and seasons to create,
// ITEM -> SUB CATEGORY / TYPE suggestions, values no longer in the sheet - adjust
// it, and submit it as one package. A different person approves it in one click.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Check, FileSpreadsheet, FileUp, RefreshCw, Send, X } from "lucide-react";

import { PageHeader } from "../components/PageHeader";
import { useAuth } from "../auth/AuthContext";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { sha256Hex } from "../lib/goodsPt";
import { Feedback, hold, useStepUp, type Page } from "../lib/goodsScreen";
import {
  MASTER_SHEET_BASE,
  STATE_LABEL,
  STATE_TONE,
  planTotals,
  submitBlockers,
  withAcknowledged,
  withLeftOut,
  withRetire,
  withRuleChoice,
  withSkipped,
  type MasterSheetDimension,
  type MasterSheetImport,
  type MasterSheetRule,
  type MasterSheetSelections,
  type MasterSheetSummary,
} from "../lib/masterSheet";
import "./Shared.css";
import "./ProductLists.css";

type Tab = "upload" | "lists" | "history";

export function ProductListsPage() {
  const { session } = useAuth();
  const canDraft = hold(session, "config.draft");
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState<Tab>((params.get("tab") as Tab) || "upload");
  const [summary, setSummary] = useState<MasterSheetSummary | null>(null);
  const [current, setCurrent] = useState<MasterSheetImport | null>(null);
  const [history, setHistory] = useState<MasterSheetImport[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const stepUp = useStepUp();
  const importId = params.get("import");

  const load = useCallback(async () => {
    const [s, h] = await Promise.all([
      api.get<MasterSheetSummary>(`${MASTER_SHEET_BASE}/summary`),
      api.get<Page<MasterSheetImport>>(MASTER_SHEET_BASE),
    ]);
    setSummary(s.data);
    setHistory(h.data.items);
    const wanted = importId ?? s.data.open_import_id;
    if (wanted) {
      const one = await api.get<MasterSheetImport>(`${MASTER_SHEET_BASE}/${wanted}`);
      setCurrent(one.data);
    } else setCurrent(null);
  }, [importId]);

  useEffect(() => {
    load().catch((e) => setError(apiErrorMessage(e)));
  }, [load]);

  async function run(work: () => Promise<void>, done = "") {
    setBusy(true);
    setError("");
    setOk("");
    try {
      await work();
      if (done) setOk(done);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function show(next: Tab) {
    setTab(next);
    const p = new URLSearchParams(params);
    p.set("tab", next);
    setParams(p, { replace: true });
  }

  async function upload(file: File) {
    await run(async () => {
      const form = new FormData();
      form.append("file", file);
      form.append("command_id", crypto.randomUUID());
      form.append("contract_version", "goods-v1");
      form.append("expected_sha256", await sha256Hex(file));
      const result = await api.post<MasterSheetImport>(`${MASTER_SHEET_BASE}/upload`, form);
      setCurrent(result.data);
      setParams({ tab: "upload", import: result.data.id }, { replace: true });
      setTab("upload");
    }, "Sheet read. Review what it changes below.");
  }

  async function act(operation: string, body: Record<string, unknown> = {}, done = "") {
    if (!current) return;
    await run(async () => {
      const result = await api.post<MasterSheetImport>(
        `${MASTER_SHEET_BASE}/${current.id}/${operation}`,
        { ...goodsMeta(current.revision), ...body },
      );
      setCurrent(result.data);
      if (operation !== "selections") await load();
    }, done);
  }

  const choose = (selections: MasterSheetSelections) => act("selections", { selections });

  async function decide(decision: "approve" | "reject", reason = "") {
    if (!current?.data.approval_request_id) return;
    await run(
      async () => {
        await stepUp.guarded(() =>
          api.post(`/goods-v1/approvals/${current.data.approval_request_id}/decide`, {
            decision,
            reviewed_hash: current.content_hash,
            ...(decision === "reject" ? { reason_code: reason || "SHEET_NEEDS_CHANGES" } : {}),
            ...goodsMeta(current.revision),
          }),
        );
        // Opened by id, the approved package shows what it wrote.
        if (importId === current.id) await load();
        else setParams({ tab: "upload", import: current.id }, { replace: true });
      },
      decision === "approve"
        ? "Approved. Every list, brand and suggestion is now in use."
        : "Sent back to the uploader.",
    );
  }

  async function downloadTemplate() {
    await run(async () => {
      const response = await api.get<Blob>(`${MASTER_SHEET_BASE}/template.xlsx`, {
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = "KDPS PT FILE.xlsx";
      link.click();
      URL.revokeObjectURL(href);
    });
  }

  const open = current && (current.state === "review" || current.state === "submitted");

  return (
    <div className="page-pad product-lists">
      <PageHeader
        title="Product lists"
        lead="Season, brand, colour, gender, sub category, type, item, fit and size all come from the KDPS master sheet. Upload it here and a second person approves it."
      />
      <div className="seg" role="tablist" aria-label="Product lists">
        {(
          [
            ["upload", "Upload sheet"],
            ["lists", "Current lists"],
            ["history", "History"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={`seg-btn ${tab === key ? "active" : ""}`}
            data-testid={`pl-tab-${key}`}
            onClick={() => show(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <Feedback error={error} ok={ok} />
      {stepUp.dialog}

      {tab === "lists" && summary && (
        <CurrentLists summary={summary} busy={busy} onDownload={downloadTemplate} />
      )}
      {tab === "history" && (
        <History
          items={history}
          onOpen={(item) => {
            setParams({ tab: "upload", import: item.id }, { replace: true });
            setTab("upload");
          }}
        />
      )}
      {tab === "upload" && (
        <>
          <Steps item={current} />
          {!open && current?.state === "approved" && importId && (
            <Done
              item={current}
              onAnother={() => setParams({ tab: "upload" }, { replace: true })}
            />
          )}
          {!open &&
            !(current?.state === "approved" && importId) &&
            (canDraft ? (
              <UploadCard busy={busy} onFile={upload} />
            ) : (
              <p className="muted-cell">Only someone who manages settings can upload the sheet.</p>
            ))}
          {open && current && (
            <Review
              item={current}
              busy={busy}
              onChoose={choose}
              onRefresh={() => act("refresh", {}, "Re-checked against the lists as they are now.")}
              onWithdraw={() => act("withdraw", {}, "Upload withdrawn.")}
              onSubmit={() =>
                act(
                  "submit",
                  { reviewed_hash: current.content_hash },
                  "Sent for approval. A second person approves it from Action needed or this page.",
                )
              }
              onDecide={decide}
            />
          )}
        </>
      )}
    </div>
  );
}

function Steps({ item }: { item: MasterSheetImport | null }) {
  const at =
    !item || item.state === "withdrawn"
      ? 0
      : item.state === "review"
        ? 1
        : item.state === "submitted"
          ? 2
          : 3;
  const steps = ["Upload the sheet", "Review the changes", "Second person approves"];
  return (
    <ol className="pl-steps" aria-label="Steps">
      {steps.map((label, i) => (
        <li key={label} className={i < at ? "done" : i === at ? "now" : ""}>
          <span className="pl-step-n">{i < at ? <Check size={14} /> : i + 1}</span>
          {label}
        </li>
      ))}
    </ol>
  );
}

function UploadCard({ busy, onFile }: { busy: boolean; onFile: (file: File) => void }) {
  const [over, setOver] = useState(false);
  return (
    <section className="card section-card">
      <label
        className={`dropzone pl-drop ${over ? "over" : ""}`}
        data-testid="pl-dropzone"
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const file = e.dataTransfer.files?.[0];
          if (file && !busy) onFile(file);
        }}
      >
        <input
          type="file"
          hidden
          accept=".xlsx"
          disabled={busy}
          data-testid="pl-file"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onFile(file);
            e.target.value = "";
          }}
        />
        <FileSpreadsheet size={28} aria-hidden />
        <strong>
          {busy ? "Reading the sheet…" : "Drop the KDPS PT file here, or click to choose"}
        </strong>
        <span className="muted-cell">
          Only the first sheet, “Master Sheet”, is read. Nothing changes until it is approved.
        </span>
      </label>
    </section>
  );
}

function Review({
  item,
  busy,
  onChoose,
  onRefresh,
  onWithdraw,
  onSubmit,
  onDecide,
}: {
  item: MasterSheetImport;
  busy: boolean;
  onChoose: (s: MasterSheetSelections) => void;
  onRefresh: () => void;
  onWithdraw: () => void;
  onSubmit: () => void;
  onDecide: (decision: "approve" | "reject", reason?: string) => void;
}) {
  const plan = item.data.plan;
  const sel = item.data.selections;
  const editable = item.allowed_actions.includes("select") && item.state === "review";
  const totals = planTotals(plan);
  const blockers = submitBlockers(item);
  const [reason, setReason] = useState("");
  const looksLike = [
    ...plan.dimensions.flatMap((d) =>
      d.problems
        .filter((p) => p.code === "LOOKS_LIKE")
        .map((p) => ({ ...p, dimension: d.dimension })),
    ),
  ];

  return (
    <div className="pl-review" data-testid="pl-review">
      <section className="card section-card pl-file">
        <div>
          <p className="eyebrow">
            <FileSpreadsheet size={14} aria-hidden /> {item.source_name}
          </p>
          <p className="muted-cell">
            {item.data.rows_read} rows read from “{item.data.sheet}” · uploaded by{" "}
            {item.data.uploaded_by || "—"}
            {item.created_at ? ` · ${formatDateTime(item.created_at)}` : ""}
          </p>
        </div>
        <span className={`chip ${STATE_TONE[item.state]}`} data-testid="pl-state">
          {STATE_LABEL[item.state]}
        </span>
      </section>

      {item.data.stale && (
        <div className="warn-note pl-row" data-testid="pl-stale">
          The lists changed since this sheet was uploaded.
          {item.allowed_actions.includes("refresh") && (
            <button className="btn btn-sm" disabled={busy} onClick={onRefresh}>
              <RefreshCw size={14} /> Check again
            </button>
          )}
        </div>
      )}

      <div className="stat-grid" data-testid="pl-totals">
        <Stat value={totals.newValues} label="New list values" />
        <Stat value={totals.brands} label="Brands to create" />
        <Stat value={totals.seasons} label="Seasons to create" />
        <Stat value={totals.rules} label="ITEM suggestions to set" />
        <Stat
          value={totals.toRetire}
          label="Values to retire"
          tone={totals.toRetire ? "amber" : ""}
        />
        <Stat
          value={totals.problems}
          label="Things to check"
          tone={totals.problems ? "amber" : ""}
        />
      </div>

      {(plan.warnings.length > 0 || looksLike.length > 0) && (
        <section className="card section-card" data-testid="pl-attention">
          <h3 className="h3">Check these first</h3>
          {plan.warnings.map((w) => (
            <label key={w.id} className="pl-check">
              <input
                type="checkbox"
                checked={w.acknowledged}
                disabled={!editable || busy}
                onChange={(e) => onChoose(withAcknowledged(sel, w.id, e.target.checked))}
              />
              <span>{w.message}</span>
            </label>
          ))}
          {looksLike.map((p) => (
            <div key={`${p.dimension}-${p.text}`} className="pl-row pl-twin">
              <span>{p.message}</span>
              {editable && (
                <button
                  className="btn btn-sm"
                  disabled={busy}
                  onClick={() => onChoose(withLeftOut(sel, p.dimension, p.text, true))}
                >
                  Leave out {p.text}
                </button>
              )}
            </div>
          ))}
        </section>
      )}

      <section className="card section-card">
        <h3 className="h3">Lists</h3>
        <p className="muted-cell">
          Values already in the app stay. A value missing from the sheet is only retired if you tick
          it.
        </p>
        {plan.dimensions.map((d) => (
          <DimensionBlock
            key={d.dimension}
            d={d}
            sel={sel}
            editable={editable}
            busy={busy}
            onChoose={onChoose}
          />
        ))}
      </section>

      <BrandsBlock item={item} editable={editable} busy={busy} onChoose={onChoose} />

      {plan.seasons.to_create.length > 0 && (
        <section className="card section-card">
          <h3 className="h3">Seasons to create ({plan.seasons.to_create.length})</h3>
          <div className="chip-picker">
            {plan.seasons.to_create.map((s) => (
              <Toggle
                key={s.name}
                on={s.include}
                label={s.name}
                disabled={!editable || busy}
                onChange={(on) => onChoose(withSkipped(sel, "skip_seasons", s.name, !on))}
              />
            ))}
          </div>
        </section>
      )}

      <RulesBlock item={item} editable={editable} busy={busy} onChoose={onChoose} />

      {plan.ignored_columns.map((c) => (
        <p key={c.column} className="muted-cell" data-testid="pl-ignored">
          <strong>{c.column}</strong> ({c.values.join(", ")}): {c.reason}{" "}
          <Link to="/setup/tax-settings">Tax settings</Link>
        </p>
      ))}

      <div className="pl-actions" data-testid="pl-actions">
        {item.allowed_actions.includes("select") && item.state === "review" && (
          <>
            <div className="pl-blockers">
              {blockers.map((b) => (
                <span key={b}>{b}</span>
              ))}
            </div>
            <button className="btn" disabled={busy} onClick={onWithdraw}>
              <X size={14} /> Withdraw
            </button>
            <button
              className="btn btn-cta"
              disabled={busy || !item.allowed_actions.includes("submit")}
              onClick={onSubmit}
              data-testid="pl-submit"
            >
              <Send size={14} /> Send for approval ({plan.change_count} changes)
            </button>
          </>
        )}
        {item.state === "submitted" && item.allowed_actions.includes("select") && (
          <>
            <span className="pl-blockers">Waiting for a second person to approve.</span>
            <button className="btn" disabled={busy} onClick={onWithdraw}>
              <X size={14} /> Withdraw
            </button>
          </>
        )}
        {item.allowed_actions.includes("approve") && (
          <>
            <input
              className="input"
              placeholder="Why send it back? (only for Send back)"
              aria-label="Reason for sending back"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
            <button className="btn" disabled={busy} onClick={() => onDecide("reject", reason)}>
              Send back
            </button>
            <button
              className="btn btn-cta"
              disabled={busy}
              onClick={() => onDecide("approve")}
              data-testid="pl-approve"
            >
              <Check size={14} /> Approve all ({plan.change_count} changes)
            </button>
          </>
        )}
        {item.state === "submitted" &&
          !item.allowed_actions.includes("approve") &&
          !item.allowed_actions.includes("select") && (
            <span className="pl-blockers">
              Waiting for approval by someone who approves settings.
            </span>
          )}
      </div>
    </div>
  );
}

function Stat({ value, label, tone = "" }: { value: number; label: string; tone?: string }) {
  return (
    <div className={`card stat-card ${tone ? `pl-${tone}` : ""}`}>
      <span className="stat-value tabular">{value.toLocaleString("en-IN")}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

function Toggle({
  on,
  label,
  disabled,
  onChange,
}: {
  on: boolean;
  label: string;
  disabled: boolean;
  onChange: (on: boolean) => void;
}) {
  return (
    <button
      type="button"
      className={`chip chip-pick ${on ? "chip-green" : "pl-off"}`}
      aria-pressed={on}
      disabled={disabled}
      title={on ? "Click to leave out" : "Click to include"}
      onClick={() => onChange(!on)}
    >
      {on ? <Check size={12} aria-hidden /> : <X size={12} aria-hidden />} {label}
    </button>
  );
}

function DimensionBlock({
  d,
  sel,
  editable,
  busy,
  onChoose,
}: {
  d: MasterSheetDimension;
  sel: MasterSheetSelections;
  editable: boolean;
  busy: boolean;
  onChoose: (s: MasterSheetSelections) => void;
}) {
  const c = d.counts;
  const parts = [
    c.added ? `${c.added} new` : "",
    c.present ? `${c.present} already there` : "",
    c.back_in_use ? `${c.back_in_use} back in use` : "",
    c.not_in_sheet ? `${c.not_in_sheet} not in sheet` : "",
    c.left_out ? `${c.left_out} left out` : "",
  ].filter(Boolean);
  const other = d.problems.filter((p) => p.code !== "LOOKS_LIKE" && p.code !== "WHITESPACE_FIXED");
  return (
    <details
      className="pl-dim"
      open={d.changed && d.added.length > 0 && d.added.length <= 40}
      data-testid={`pl-dim-${d.dimension}`}
    >
      <summary>
        <strong>{d.column}</strong>
        <span className="muted-cell">{parts.join(" · ") || "no values"}</span>
        {d.changed && <span className="chip chip-blue">changes</span>}
      </summary>
      {(d.added.length > 0 || d.left_out.length > 0) && (
        <>
          <p className="pl-sub">New — click one to leave it out</p>
          <div className="chip-picker">
            {d.added.map((e) => (
              <Toggle
                key={e.text}
                on
                label={e.text}
                disabled={!editable || busy}
                onChange={() => onChoose(withLeftOut(sel, d.dimension, e.text, true))}
              />
            ))}
            {d.left_out.map((e) => (
              <Toggle
                key={e.text}
                on={false}
                label={e.text}
                disabled={!editable || busy}
                onChange={() => onChoose(withLeftOut(sel, d.dimension, e.text, false))}
              />
            ))}
          </div>
        </>
      )}
      {d.back_in_use.length > 0 && (
        <p className="pl-sub">
          Back in use (were retired): {d.back_in_use.map((v) => v.label).join(", ")}
        </p>
      )}
      {d.not_in_sheet.length > 0 && (
        <>
          <p className="pl-sub">In the app but not in the sheet — tick to retire</p>
          <div className="chip-picker">
            {d.not_in_sheet.map((v) => (
              <label key={v.value_id} className={`chip ${v.retire ? "chip-red" : ""} pl-retire`}>
                <input
                  type="checkbox"
                  checked={v.retire}
                  disabled={!editable || busy}
                  onChange={(e) => onChoose(withRetire(sel, v.value_id, e.target.checked))}
                />
                {v.label}
              </label>
            ))}
          </div>
        </>
      )}
      {other.map((p) => (
        <p key={`${p.row}-${p.code}`} className={p.blocking ? "warn-note" : "muted-cell"}>
          Row {p.row}: {p.text} — {p.message}
        </p>
      ))}
    </details>
  );
}

function BrandsBlock({
  item,
  editable,
  busy,
  onChoose,
}: {
  item: MasterSheetImport;
  editable: boolean;
  busy: boolean;
  onChoose: (s: MasterSheetSelections) => void;
}) {
  const b = item.data.plan.brands;
  const sel = item.data.selections;
  const [q, setQ] = useState("");
  const shown = useMemo(
    () => b.to_create.filter((x) => x.name.toLowerCase().includes(q.trim().toLowerCase())),
    [b.to_create, q],
  );
  if (!b.to_create.length && !b.not_in_sheet.length) {
    return (
      <p className="muted-cell">All {b.present_count} brands in the sheet are already set up.</p>
    );
  }
  return (
    <section className="card section-card" data-testid="pl-brands">
      <h3 className="h3">Brands</h3>
      <p className="muted-cell">
        {b.to_create.filter((x) => x.include).length} to create · {b.present_count} already set up.{" "}
        {b.terms_note} <Link to="/setup/products-parties?type=brands">Brands</Link>
      </p>
      {b.problems
        .filter((p) => p.code === "DUPLICATE_IN_SHEET")
        .map((p) => (
          <p key={p.row} className="muted-cell">
            Row {p.row}: {p.message}
          </p>
        ))}
      {b.to_create.length > 0 && (
        <details open={b.to_create.length <= 60}>
          <summary>New brands — click one to leave it out ({b.to_create.length})</summary>
          {b.to_create.length > 20 && (
            <input
              className="input pl-search"
              placeholder="Find a brand"
              aria-label="Find a brand"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          )}
          <div className="chip-picker pl-scroll">
            {shown.map((x) => (
              <Toggle
                key={x.name}
                on={x.include}
                label={x.name}
                disabled={!editable || busy}
                onChange={(on) => onChoose(withSkipped(sel, "skip_brands", x.name, !on))}
              />
            ))}
          </div>
        </details>
      )}
      {b.not_in_sheet.length > 0 && (
        <details>
          <summary>In the app but not in the sheet ({b.not_in_sheet.length}) — kept</summary>
          <p className="muted-cell">{b.not_in_sheet.map((x) => x.name).join(", ")}</p>
        </details>
      )}
    </section>
  );
}

function RulesBlock({
  item,
  editable,
  busy,
  onChoose,
}: {
  item: MasterSheetImport;
  editable: boolean;
  busy: boolean;
  onChoose: (s: MasterSheetSelections) => void;
}) {
  const r = item.data.plan.item_rules;
  const sel = item.data.selections;
  const [onlyChoices, setOnlyChoices] = useState(true);
  const rows: { rule: MasterSheetRule; changed: boolean }[] = [
    ...r.changed.map((rule) => ({ rule, changed: true })),
    ...r.new.map((rule) => ({ rule, changed: false })),
  ];
  const needsChoice = (rule: MasterSheetRule) =>
    (rule.sub_category?.options.length ?? 0) > 1 || (rule.type?.options.length ?? 0) > 1;
  const shown = onlyChoices ? rows.filter((x) => x.changed || needsChoice(x.rule)) : rows;
  if (!rows.length && !r.problems.length) {
    return r.same_count ? (
      <p className="muted-cell">All {r.same_count} ITEM suggestions already match the sheet.</p>
    ) : null;
  }
  return (
    <section className="card section-card" data-testid="pl-rules">
      <h3 className="h3">ITEM → SUB CATEGORY / TYPE suggestions</h3>
      <p className="muted-cell">
        The sheet's last two columns. On a PT, picking an ITEM suggests these. {rows.length} to set
        {r.same_count ? ` · ${r.same_count} already match` : ""}.
      </p>
      <label className="pl-check">
        <input
          type="checkbox"
          checked={onlyChoices}
          onChange={(e) => setOnlyChoices(e.target.checked)}
        />
        <span>Show only the ones that need a choice or change</span>
      </label>
      {shown.length > 0 && (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>ITEM</th>
                <th>SUB CATEGORY</th>
                <th>TYPE</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {shown.map(({ rule, changed }) => (
                <tr key={rule.item}>
                  <td>{rule.item}</td>
                  {(["sub_category", "type"] as const).map((dim) => {
                    const pick = rule[dim];
                    if (!pick)
                      return (
                        <td key={dim} className="muted-cell">
                          —
                        </td>
                      );
                    return (
                      <td key={dim}>
                        {pick.options.length > 1 && editable ? (
                          <select
                            className="input"
                            value={pick.value}
                            disabled={busy}
                            aria-label={`${rule.item} ${dim === "type" ? "type" : "sub category"}`}
                            onChange={(e) =>
                              onChoose(withRuleChoice(sel, rule.item, dim, e.target.value))
                            }
                          >
                            {pick.options.map((o) => (
                              <option key={o}>{o}</option>
                            ))}
                          </select>
                        ) : (
                          pick.value
                        )}
                        {pick.from && pick.from !== pick.value && (
                          <span className="muted-cell"> (was {pick.from})</span>
                        )}
                      </td>
                    );
                  })}
                  <td>
                    <span className={`chip ${changed ? "chip-amber" : "chip-blue"}`}>
                      {changed ? "Changed" : "New"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {r.problems
        .filter((p) => p.code === "HELPER_UNKNOWN")
        .map((p) => (
          <p key={`${p.row}-${p.text}`} className="muted-cell">
            Row {p.row}: {p.message}
          </p>
        ))}
    </section>
  );
}

function Done({ item, onAnother }: { item: MasterSheetImport; onAnother: () => void }) {
  const r = item.data.result as {
    config_versions?: Record<string, string>;
    brand_ids?: string[];
    season_ids?: string[];
    rule_ids?: string[];
    corrected_rule_ids?: string[];
  };
  return (
    <section className="card section-card" data-testid="pl-done">
      <h3 className="h3">
        <Check size={18} /> Approved by {item.data.approved_by || "a second person"}
      </h3>
      <ul className="pl-done-list">
        <li>{Object.keys(r.config_versions ?? {}).length} lists updated</li>
        <li>{r.brand_ids?.length ?? 0} brands created — set their terms on the Brands screen</li>
        <li>{r.season_ids?.length ?? 0} seasons created</li>
        <li>
          {(r.rule_ids?.length ?? 0) + (r.corrected_rule_ids?.length ?? 0)} ITEM suggestions set
        </li>
      </ul>
      <div className="toolbar">
        <Link className="btn" to="/setup/products-parties?type=brands">
          Brands
        </Link>
        <Link className="btn" to="/goods/pt-work">
          PT Work
        </Link>
        <button className="btn btn-cta" onClick={onAnother}>
          <FileUp size={14} /> Upload another sheet
        </button>
      </div>
    </section>
  );
}

function CurrentLists({
  summary,
  busy,
  onDownload,
}: {
  summary: MasterSheetSummary;
  busy: boolean;
  onDownload: () => void;
}) {
  const rates = summary.p_rate.in_force;
  const wrong = rates.filter((r) => !r.expected);
  return (
    <div data-testid="pl-lists">
      <section className="card section-card pl-row">
        <div>
          <h3 className="h3">KDPS PT file</h3>
          <p className="muted-cell">
            The Master Sheet with today's lists, and a blank Work Sheet with the same dropdowns and
            formulas. Fill one Work Sheet and upload it on PT Work.
          </p>
        </div>
        <button
          className="btn btn-cta"
          disabled={busy}
          onClick={onDownload}
          data-testid="pl-download"
        >
          <FileSpreadsheet size={14} /> Download PT file
        </button>
      </section>
      <div className="stat-grid">
        {summary.lists.map((l) => (
          <div key={l.dimension} className="card stat-card">
            <span className="stat-value tabular">{l.active.toLocaleString("en-IN")}</span>
            <span className="stat-label">
              {l.column}
              {l.retired ? ` · ${l.retired} retired` : ""}
            </span>
          </div>
        ))}
        <Stat value={summary.brands} label="BRAND" />
        <Stat value={summary.seasons} label="Season masters" />
        <Stat value={summary.item_rules} label="ITEM suggestions" />
      </div>
      <section className="card section-card" data-testid="pl-prate">
        <h3 className="h3">P RATE</h3>
        <p>
          KDPS buys at <strong>P RATE = BASIC × {summary.p_rate.expected_factor}</strong>.
        </p>
        {rates.length === 0 && (
          <p className="warn-note">
            No rates are in force yet. Set transport to 10% in{" "}
            <Link to="/setup/configuration">Configuration → rates</Link>.
          </p>
        )}
        {wrong.map((r) => (
          <p key={r.rates_version_id} className="warn-note">
            A rates setting in force uses BASIC × {r.factor} (transport {r.transport_pct}%). Change
            it to 10% in <Link to="/setup/configuration">Configuration → rates</Link>.
          </p>
        ))}
        {rates.length > 0 && wrong.length === 0 && (
          <p className="ok-note">
            The rates in force match: BASIC × {summary.p_rate.expected_factor}.
          </p>
        )}
      </section>
      <p className="muted-cell">
        To change one value, use <Link to="/setup/configuration">Configuration</Link>. To change
        many, upload the master sheet again.
      </p>
    </div>
  );
}

function History({
  items,
  onOpen,
}: {
  items: MasterSheetImport[];
  onOpen: (item: MasterSheetImport) => void;
}) {
  if (!items.length) return <p className="muted-cell">No sheet has been uploaded yet.</p>;
  return (
    <div className="table-wrap" data-testid="pl-history">
      <table className="data">
        <thead>
          <tr>
            <th>File</th>
            <th>Uploaded</th>
            <th>By</th>
            <th>Changes</th>
            <th>State</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td>{item.source_name}</td>
              <td>{item.created_at ? formatDateTime(item.created_at) : ""}</td>
              <td>{item.data.uploaded_by}</td>
              <td className="tabular">{item.data.plan.change_count}</td>
              <td>
                <span className={`chip ${STATE_TONE[item.state]}`}>{STATE_LABEL[item.state]}</span>
              </td>
              <td>
                <button className="btn btn-sm" onClick={() => onOpen(item)}>
                  Open
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
