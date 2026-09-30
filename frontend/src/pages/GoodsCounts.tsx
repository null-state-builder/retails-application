// Stock counts at non-trading sites (goods ticket 17).
//
// Two screens: the counts at your sites (and starting one), and one count.
//
// What the screen has to make plain rather than merely obey:
//   * a count is only for a site declared non-trading. A site with tills, or
//     with no declaration, is refused - the server says why and the screen
//     shows it;
//   * starting a count freezes the whole site: no stock moves, is sent,
//     received or put away until the count ends. Damage can still be reported,
//     and goes to quarantine at once;
//   * counting is blind. Nothing a counter sees says how many there should be;
//     a scanned tag answers what the item is, never how many;
//   * a pass is submitted only with the counter's word that the whole assigned
//     area was counted, empty places included. An unfinished pass stays open to
//     continue; one left idle for a day has to be resumed on purpose;
//   * the only endings here move nothing: a count that matches the book closes,
//     and a count can be cancelled with everything counted kept. A count that
//     differs from the book is *not complete* - it stays frozen, waiting for
//     review and the Owner's approval - and the screen never says otherwise.
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ClipboardCheck, Play, RotateCcw, ScanLine, Search, Send, XCircle } from "lucide-react";

import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { formatDateTime } from "../lib/format";
import {
  Denied,
  Feedback,
  Field,
  hold,
  listState,
  useGoodsFetch,
  type Page,
} from "../lib/goodsScreen";
import {
  CONDITIONS,
  CONDITION_LABEL,
  PASS_STATE_LABEL,
  STATE_LABEL,
  candidateWords,
  deltaWords,
  itemWords,
  progressWords,
  recountSource,
  scopeWords,
  type CountCancelBody,
  type CountCloseBody,
  type CountCondition,
  type CountDetail,
  type CountLookup,
  type CountPass,
  type CountRecountBody,
  type CountScanInput,
  type CountStartBody,
  type CountSubmitBody,
  type CountSummary,
  type CountVariance,
} from "../lib/goodsCounts";
import { OperationsPage, OperationsTable } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";

const STOCKTAKES = "/goods-v1/outbound/stocktakes";
const PASSES = "/goods-v1/outbound/count-sessions";
const LOOKUP = "/goods-v1/outbound/count-lookup";

interface Issue {
  code: string;
  message: string;
}

/** A refusal's issues, when it names any - "why" the server said no. */
function refusalIssues(e: unknown): Issue[] {
  const data = (e as { response?: { data?: { details?: { issues?: Issue[] } } } })?.response?.data;
  return data?.details?.issues ?? [];
}

function Issues({ issues, testId }: { issues: Issue[]; testId: string }) {
  if (issues.length === 0) return null;
  return (
    <ul className="warn-note" data-testid={testId}>
      {issues.map((issue, index) => (
        <li key={`${issue.code}-${index}`} data-code={issue.code}>
          {issue.message}
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// The counts at your sites, and starting one
// ---------------------------------------------------------------------------

export function GoodsCountsPage() {
  const { session } = useAuth();
  const navigate = useNavigate();
  // A scheduled count due today opens this page with its site chosen (ticket 35).
  const [params] = useSearchParams();
  const [siteId, setSiteId] = useState(params.get("site") ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [issues, setIssues] = useState<Issue[]>([]);
  const list = useGoodsFetch<Page<CountSummary>, CountSummary[]>(
    STOCKTAKES,
    (r) => r.items ?? [],
    [],
  );
  if (!session) return null;
  const canCount = hold(session, "count.run");

  async function start() {
    setBusy(true);
    setError("");
    setIssues([]);
    try {
      const body: CountStartBody = {
        site_id: siteId,
        scope: { kind: "site" },
        ...goodsMeta(),
      };
      const { data } = await api.post<CountDetail>(STOCKTAKES, body);
      navigate(`/goods/counts/${data.id}`);
    } catch (e) {
      setError(apiErrorMessage(e));
      setIssues(refusalIssues(e));
    } finally {
      setBusy(false);
    }
  }

  if (list.denied)
    return (
      <OperationsPage>
        <PageHeader title="Stock counts" />
        <Denied what="stock count" />
      </OperationsPage>
    );
  const state = listState(
    { loading: list.loading, failure: list.failure, empty: list.value.length === 0 },
    "No count has been started at your sites.",
  );

  return (
    <OperationsPage className="stock-layout">
      <PageHeader
        title="Stock counts"
        lead="Blind counts at sites that do not trade. Starting one freezes the site until it ends; only a count that matches the book, or a cancelled one, ends here."
      />

      {canCount && (
        <section className="card section-card" data-testid="cnt-start">
          <h3 className="h3">Start a count</h3>
          <p className="lead">
            Only a site declared non-trading - no tills, no bills - can be counted. Starting freezes
            the whole site: no stock moves, is sent, received or put away until the count ends.
            Damage can still be reported.
          </p>
          <div className="form-grid">
            <Field id="cnt-site" label="Site">
              <select
                id="cnt-site"
                className="select"
                value={siteId}
                onChange={(e) => setSiteId(e.target.value)}
                data-testid="cnt-site"
              >
                <option value="">Choose a site</option>
                {session.sites.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.code})
                  </option>
                ))}
              </select>
            </Field>
            <button
              className="btn btn-cta"
              disabled={!siteId || busy}
              onClick={start}
              data-testid="cnt-start-go"
            >
              <Play size={14} /> Start the count
            </button>
          </div>
          <Feedback error={error} ok="" />
          <Issues issues={issues} testId="cnt-start-issues" />
        </section>
      )}

      <section className="card section-card">
        <h3 className="h3">Counts</h3>
        {state ?? (
          <OperationsTable label="Stock counts">
            <table data-testid="cnt-list">
              <thead>
                <tr>
                  <th>Count</th>
                  <th>Site</th>
                  <th>What</th>
                  <th>State</th>
                  <th>Frozen since</th>
                  <th className="num">Passes</th>
                </tr>
              </thead>
              <tbody>
                {list.value.map((row) => (
                  <tr key={row.id} data-testid="cnt-row" data-count={row.id} data-state={row.state}>
                    <td>
                      <Link to={`/goods/counts/${row.id}`} data-testid="cnt-open-link">
                        <code className="mono">{row.number ?? row.id}</code>
                      </Link>
                    </td>
                    <td>
                      {row.site.name} ({row.site.code})
                    </td>
                    <td>{scopeWords(row.scope)}</td>
                    <td>
                      {STATE_LABEL[row.state] ?? row.state}
                      {row.stale_passes > 0 && (
                        <span className="warn-note"> · {row.stale_passes} pass(es) left idle</span>
                      )}
                    </td>
                    <td>{row.frozen_at ? formatDateTime(row.frozen_at) : "-"}</td>
                    <td className="num">
                      {row.submitted_passes} submitted · {row.open_passes} counting
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </OperationsTable>
        )}
      </section>
    </OperationsPage>
  );
}

// ---------------------------------------------------------------------------
// One count
// ---------------------------------------------------------------------------

export function GoodsCountDetailPage() {
  const { id = "" } = useParams();
  const { session } = useAuth();
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const detail = useGoodsFetch<CountDetail, CountDetail | null>(
    id ? `${STOCKTAKES}/${id}` : null,
    (r) => r,
    null,
  );

  if (!session) return null;
  if (detail.denied)
    return (
      <OperationsPage>
        <PageHeader title="Stock count" />
        <Denied what="stock count" />
      </OperationsPage>
    );
  const count = detail.value;
  if (!count)
    return (
      <OperationsPage>
        <PageHeader title="Stock count" />
        <p className={detail.failure ? "warn-note" : "muted"} role="status">
          {detail.failure || "Loading…"}
        </p>
      </OperationsPage>
    );
  const words = progressWords(count.progress);

  function done(message: string) {
    setError("");
    setOk(message);
    detail.reload();
  }
  function failed(e: unknown) {
    setOk("");
    setError(apiErrorMessage(e));
  }

  return (
    <OperationsPage className="stock-layout" data-testid="cnt-detail" data-state={count.state}>
      <PageHeader
        title={`Stock count ${count.number ?? ""}`.trim()}
        lead={`${count.site.name} (${count.site.code}) · ${scopeWords(count.scope)}`}
        actions={
          <Link className="btn btn-sm" to="/goods/counts">
            All counts
          </Link>
        }
      />

      <section
        className={
          count.progress === "differences_pending"
            ? "card section-card warn-note"
            : "card section-card"
        }
        data-testid="cnt-progress"
        data-progress={count.progress}
      >
        <h3 className="h3" data-testid="cnt-progress-title">
          {words.title}
        </h3>
        <p className="lead">{words.body}</p>
        <p className="muted">
          {STATE_LABEL[count.state] ?? count.state}
          {count.frozen_at && ` · frozen since ${formatDateTime(count.frozen_at)}`} · started by{" "}
          {count.started_by.name || "-"}
        </p>
        {count.decision && (
          <p className="muted" data-testid="cnt-decision">
            Closed by {count.decision.decided_by.name || "-"} on{" "}
            {formatDateTime(count.decision.decided_at)} over {count.decision.lines} line(s), all
            matching.
          </p>
        )}
      </section>

      <Feedback error={error} ok={ok} />

      {(count.allowed_actions.includes("open_pass") ||
        count.allowed_actions.includes("continue_pass")) && (
        <MyPass count={count} onDone={done} onError={failed} />
      )}

      <PassesTable count={count} />

      {count.allowed_actions.includes("variance") && (
        // Keyed on the count's revision: a submitted pass, a recount or a close
        // is a new variance, read again rather than shown stale.
        <ReviewPanel key={count.revision} count={count} onDone={done} onError={failed} />
      )}

      {count.allowed_actions.includes("cancel") && (
        <CancelPanel count={count} onDone={done} onError={failed} />
      )}

      <section className="card section-card">
        <h3 className="h3">History</h3>
        <ol data-testid="cnt-history">
          {count.history.map((item) => (
            <li key={item.id} data-kind={item.kind}>
              {formatDateTime(item.recorded_at)} - {item.kind.replace(/_/g, " ")}
              {item.reason_code ? ` (${item.reason_code})` : ""}
            </li>
          ))}
        </ol>
      </section>
    </OperationsPage>
  );
}

function PassesTable({ count }: { count: CountDetail }) {
  return (
    <section className="card section-card">
      <h3 className="h3">Passes</h3>
      {count.passes.length === 0 ? (
        <p className="muted">Nobody has started counting yet.</p>
      ) : (
        <OperationsTable label="Count passes">
          <table data-testid="cnt-passes">
            <thead>
              <tr>
                <th>Counter</th>
                <th>Covers</th>
                <th>State</th>
                <th>Last activity</th>
                <th className="num">Counted</th>
              </tr>
            </thead>
            <tbody>
              {count.passes.map((row) => (
                <tr
                  key={row.id}
                  data-testid="cnt-pass-row"
                  data-pass={row.id}
                  data-state={row.state}
                >
                  <td>
                    {row.counter.name || "-"} · pass {row.pass_no}
                  </td>
                  <td>
                    {row.scope_label}
                    {row.reason_code ? ` (${row.reason_code})` : ""}
                  </td>
                  <td>
                    {PASS_STATE_LABEL[row.state] ?? row.state}
                    {row.stale && (
                      <span className="warn-note"> · left idle - resume to continue</span>
                    )}
                  </td>
                  <td>{row.last_activity_at ? formatDateTime(row.last_activity_at) : "-"}</td>
                  <td className="num">{row.observed_qty ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </OperationsTable>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// My pass: blind scanning, resume and the scope-complete affirmation
// ---------------------------------------------------------------------------

function MyPass({
  count,
  onDone,
  onError,
}: {
  count: CountDetail;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const passId = count.my_open_pass_id;
  const pass = useGoodsFetch<CountPass, CountPass | null>(
    passId ? `${PASSES}/${passId}` : null,
    (r) => r,
    null,
  );
  const [assign, setAssign] = useState("");
  const [busy, setBusy] = useState(false);

  async function openPass() {
    setBusy(true);
    try {
      await api.post(`${STOCKTAKES}/${count.id}/sessions`, {
        ...(assign ? { location_id: assign } : {}),
        ...goodsMeta(),
      });
      onDone("Your pass is open. Count what you find; you are not told how many to expect.");
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  if (!passId) {
    return (
      <section className="card section-card" data-testid="cnt-my-pass">
        <h3 className="h3">Count</h3>
        <p className="lead">
          Start your pass. You count what is physically there; nothing tells you how many there
          should be.
        </p>
        <div className="form-grid">
          <Field id="cnt-assign" label="Your area">
            <select
              id="cnt-assign"
              className="select"
              value={assign}
              onChange={(e) => setAssign(e.target.value)}
              data-testid="cnt-assign"
            >
              <option value="">Everything this count covers</option>
              {count.locations.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name}
                </option>
              ))}
            </select>
          </Field>
          <button className="btn btn-cta" disabled={busy} onClick={openPass} data-testid="cnt-open">
            <ScanLine size={14} /> Start counting
          </button>
        </div>
      </section>
    );
  }
  if (!pass.value) return <p className="muted">{pass.failure || "Loading your pass…"}</p>;
  return (
    <PassPanel
      pass={pass.value}
      onChanged={(message) => {
        pass.reload();
        onDone(message);
      }}
      onError={onError}
    />
  );
}

function PassPanel({
  pass,
  onChanged,
  onError,
}: {
  pass: CountPass;
  onChanged: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const [locationId, setLocationId] = useState(pass.locations[0]?.id ?? "");
  const [tag, setTag] = useState("");
  const [found, setFound] = useState<CountLookup | null>(null);
  const [skuId, setSkuId] = useState("");
  const [description, setDescription] = useState("");
  const [condition, setCondition] = useState<CountCondition>("good");
  const [qty, setQty] = useState(1);
  const [affirmed, setAffirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [issues, setIssues] = useState<Issue[]>([]);

  async function lookUp() {
    setIssues([]);
    try {
      const { data } = await api.get<CountLookup>(
        `${LOOKUP}?stocktake_id=${pass.stocktake_id}&alias_value=${encodeURIComponent(tag.trim())}`,
      );
      setFound(data);
      setSkuId(data.chosen_sku_id ?? "");
    } catch (e) {
      onError(e);
    }
  }

  async function record() {
    setBusy(true);
    setIssues([]);
    try {
      const observation: CountScanInput = {
        scan_key: crypto.randomUUID(),
        location_id: locationId,
        sku_id: skuId || null,
        description: skuId ? "" : description.trim(),
        alias_value: tag.trim() || null,
        condition,
        qty,
      };
      await api.post(`${PASSES}/${pass.id}/scan`, {
        observations: [observation],
        ...goodsMeta(pass.revision),
      });
      setTag("");
      setFound(null);
      setSkuId("");
      setDescription("");
      setQty(1);
      setCondition("good");
      setAffirmed(false);
      onChanged(`${qty} piece(s) recorded.`);
    } catch (e) {
      setIssues(refusalIssues(e));
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  async function resume() {
    setBusy(true);
    try {
      await api.post(`${PASSES}/${pass.id}/resume`, goodsMeta(pass.revision));
      onChanged("Pass resumed. Everything counted so far is still on it.");
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    setBusy(true);
    setIssues([]);
    try {
      const body: CountSubmitBody = {
        reviewed_hash: pass.observation_hash,
        scope_complete: affirmed,
        ...goodsMeta(),
        expected_revision: pass.revision,
      };
      await api.post(`${PASSES}/${pass.id}/submit`, body);
      onChanged("Pass submitted. It waits for review; you are not told whether it matches.");
    } catch (e) {
      setIssues(refusalIssues(e));
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  const describing = found?.result === "unknown" || (!found && !tag.trim());
  const canRecord =
    !busy &&
    !pass.stale &&
    Boolean(locationId) &&
    qty > 0 &&
    (Boolean(skuId) || Boolean(description.trim()));

  return (
    <section className="card section-card" data-testid="cnt-my-pass" data-pass={pass.id}>
      <h3 className="h3">Your pass {pass.pass_no}</h3>
      <p className="lead" data-testid="cnt-pass-scope">
        {pass.scope.label}
        {pass.reason_code ? ` - recount asked because: ${pass.reason_code}` : ""}
      </p>
      {pass.scope.cells.length > 0 && (
        <ul data-testid="cnt-recount-cells">
          {pass.scope.cells.map((cell) => (
            <li key={`${cell.location_id}-${cell.sku_id ?? cell.description}`}>
              {itemWords(cell)} at {cell.location_name}
            </li>
          ))}
        </ul>
      )}

      {pass.stale && (
        <div className="warn-note" data-testid="cnt-stale">
          This pass was left idle for a day. Nothing counted is lost, but it must be resumed before
          you count on or submit it.{" "}
          <button className="btn btn-sm" disabled={busy} onClick={resume} data-testid="cnt-resume">
            <RotateCcw size={14} /> Resume
          </button>
        </div>
      )}

      <div className="form-grid">
        <Field id="cnt-location" label="Where you are counting">
          <select
            id="cnt-location"
            className="select"
            value={locationId}
            onChange={(e) => setLocationId(e.target.value)}
            data-testid="cnt-location"
          >
            {pass.locations.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
        </Field>
        <Field id="cnt-tag" label="Tag or barcode">
          <input
            id="cnt-tag"
            className="input"
            value={tag}
            onChange={(e) => {
              setTag(e.target.value);
              setFound(null);
              setSkuId("");
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && tag.trim()) void lookUp();
            }}
            data-testid="cnt-tag"
          />
        </Field>
        <button
          className="btn btn-sm"
          disabled={!tag.trim()}
          onClick={lookUp}
          data-testid="cnt-lookup"
        >
          <Search size={14} /> What is it?
        </button>
      </div>

      {found && found.result === "resolved" && (
        <p className="ok-note" data-testid="cnt-item">
          {found.candidates[0] ? candidateWords(found.candidates[0]) : "No candidate found"}
        </p>
      )}
      {found && found.result === "ambiguous" && (
        <Field id="cnt-candidate" label="Which item is it?">
          <select
            id="cnt-candidate"
            className="select"
            value={skuId}
            onChange={(e) => setSkuId(e.target.value)}
            data-testid="cnt-candidate"
          >
            <option value="">Choose</option>
            {found.candidates.map((candidate) => (
              <option key={candidate.sku_id} value={candidate.sku_id}>
                {candidateWords(candidate)}
              </option>
            ))}
          </select>
        </Field>
      )}
      {describing && (
        <Field
          id="cnt-description"
          label="Describe it"
          hint={
            found?.result === "unknown"
              ? "No item is known by that tag. Describe what you see; nobody invents an item for it."
              : "No tag? Describe what you see."
          }
        >
          <input
            id="cnt-description"
            className="input"
            aria-describedby="cnt-description-hint"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            data-testid="cnt-description"
          />
        </Field>
      )}
      <div className="form-grid">
        <Field
          id="cnt-condition"
          label="Condition"
          hint="Damaged pieces are counted as damaged. Report the damage too - the count does not stop it."
        >
          <select
            id="cnt-condition"
            className="select"
            aria-describedby="cnt-condition-hint"
            value={condition}
            onChange={(e) => setCondition(e.target.value as CountCondition)}
            data-testid="cnt-condition"
          >
            {CONDITIONS.filter((c) => !skuId || c !== "unidentified").map((c) => (
              <option key={c} value={c}>
                {CONDITION_LABEL[c]}
              </option>
            ))}
          </select>
        </Field>
        <Field id="cnt-qty" label="How many">
          <input
            id="cnt-qty"
            className="input"
            type="number"
            min={1}
            value={qty}
            onChange={(e) => setQty(Number(e.target.value))}
            data-testid="cnt-qty"
          />
        </Field>
        <button
          className="btn btn-cta"
          disabled={!canRecord}
          onClick={record}
          data-testid="cnt-record"
        >
          <ClipboardCheck size={14} /> Record
        </button>
      </div>
      <Issues issues={issues} testId="cnt-scan-issues" />

      <h4 className="h4">Counted so far: {pass.observed_qty}</h4>
      {pass.observations.length > 0 && (
        <OperationsTable label="Recorded observations">
          <table data-testid="cnt-observations">
            <thead>
              <tr>
                <th>Where</th>
                <th>Item</th>
                <th>Condition</th>
                <th className="num">Pieces</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {pass.observations.map((row) => (
                <tr key={row.scan_key} data-testid="cnt-observation">
                  <td>{row.location_name}</td>
                  <td>{itemWords(row)}</td>
                  <td>{CONDITION_LABEL[row.condition as CountCondition] ?? row.condition}</td>
                  <td className="num">{row.qty}</td>
                  <td>{formatDateTime(row.actual_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </OperationsTable>
      )}

      <div className="form-grid">
        <label className="checkbox" htmlFor="cnt-affirm">
          <input
            id="cnt-affirm"
            type="checkbox"
            checked={affirmed}
            onChange={(e) => setAffirmed(e.target.checked)}
            data-testid="cnt-affirm"
          />{" "}
          I have counted the whole assigned area, including empty locations.
        </label>
        <button
          className="btn btn-cta"
          disabled={busy || pass.stale}
          onClick={submit}
          data-testid="cnt-submit"
        >
          <Send size={14} /> Submit my pass
        </button>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// The reviewer: the variance, a scoped recount and the zero-variance close
// ---------------------------------------------------------------------------

function ReviewPanel({
  count,
  onDone,
  onError,
}: {
  count: CountDetail;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const submitted = count.passes.some((p) => p.state !== "open");
  const variance = useGoodsFetch<CountVariance, CountVariance | null>(
    submitted ? `${STOCKTAKES}/${count.id}/variance?limit=500` : null,
    (r) => r,
    null,
  );
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [counter, setCounter] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  if (!submitted) {
    return (
      <section className="card section-card" data-testid="cnt-review">
        <h3 className="h3">Review</h3>
        <p className="muted">Nothing has been submitted yet.</p>
      </section>
    );
  }
  const report = variance.value;
  if (!report) return <p className="muted">{variance.failure || "Loading the variance…"}</p>;
  const lines = report.lines.items;
  const source = recountSource(lines, ticked);

  async function recount() {
    setBusy(true);
    try {
      const body: CountRecountBody = {
        line_keys: [...ticked],
        counter_id: counter,
        reason_code: reason.trim(),
        ...goodsMeta(),
        expected_revision: count.revision,
      };
      await api.post(`${STOCKTAKES}/${count.id}/recount`, body);
      setTicked(new Set());
      setReason("");
      onDone("Recount assigned. The earlier count is kept beside it.");
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  async function close() {
    if (!report) return;
    setBusy(true);
    try {
      const body: CountCloseBody = {
        reviewed_hash: report.variance_hash,
        selected_pass_ids: report.selected_pass_ids,
        ...goodsMeta(),
        expected_revision: count.revision,
      };
      await api.post(`${STOCKTAKES}/${count.id}/close`, body);
      onDone("Count closed. Counted matched the book; no stock changed and the freeze is lifted.");
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card section-card" data-testid="cnt-review">
      <h3 className="h3">Review against the book</h3>
      <p className="lead">
        The book is the site's stock at the moment the count froze it. Unscanned stock counts as
        zero found.
      </p>
      <p data-testid="cnt-review-summary" data-matches={report.matches_book ? "yes" : "no"}>
        {report.matches_book
          ? "Every line matches the book."
          : report.complete
            ? `${report.differing_lines} line(s) differ from the book. They wait for review and the Owner's approval.`
            : "Not every location is counted exactly once yet."}
      </p>
      {report.uncovered_locations.length > 0 && (
        <p className="warn-note" data-testid="cnt-uncovered">
          Not counted: {report.uncovered_locations.map((row) => row.name).join(", ")}
        </p>
      )}
      <Issues issues={report.issues as unknown as Issue[]} testId="cnt-review-issues" />
      <OperationsTable label="Count variance">
        <table data-testid="cnt-variance">
          <thead>
            <tr>
              <th>Recount</th>
              <th>Where</th>
              <th>Item</th>
              <th>Condition</th>
              <th className="num">Book</th>
              <th className="num">Counted</th>
              <th className="num">Difference</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line, index) => (
              <tr
                key={line.line_key}
                data-testid="cnt-line"
                data-line={line.line_key}
                data-delta={line.delta ?? ""}
              >
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Recount ${itemWords(line)} at ${line.location_name}`}
                    checked={ticked.has(line.line_key)}
                    disabled={line.pass_id === null}
                    onChange={(e) => {
                      const next = new Set(ticked);
                      if (e.target.checked) next.add(line.line_key);
                      else next.delete(line.line_key);
                      setTicked(next);
                    }}
                    data-testid={`cnt-line-pick-${index}`}
                  />
                </td>
                <td>{line.location_name}</td>
                <td>{itemWords(line)}</td>
                <td>{CONDITION_LABEL[line.condition as CountCondition] ?? line.condition}</td>
                <td className="num">{line.book_qty}</td>
                <td className="num">{line.observed_qty ?? "-"}</td>
                <td className="num" data-testid="cnt-line-delta">
                  {deltaWords(line.delta)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </OperationsTable>

      <h4 className="h4">Ask for a recount</h4>
      <p className="muted">
        Someone counts the ticked items again at those places, without being told the earlier count.
        The earlier count is kept; the recount is used in its place, never added to it.
      </p>
      {source.mixed && (
        <p className="warn-note" data-testid="cnt-recount-mixed">
          The ticked lines were counted in different passes. Ask for one recount per pass.
        </p>
      )}
      <div className="form-grid">
        <Field id="cnt-recount-counter" label="Who recounts">
          <select
            id="cnt-recount-counter"
            className="select"
            value={counter}
            onChange={(e) => setCounter(e.target.value)}
            data-testid="cnt-recount-counter"
          >
            <option value="">Choose</option>
            {count.counters.map((person) => (
              <option key={person.id ?? ""} value={person.id ?? ""}>
                {person.name || person.id}
              </option>
            ))}
          </select>
        </Field>
        <Field id="cnt-recount-reason" label="Why">
          <input
            id="cnt-recount-reason"
            className="input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            data-testid="cnt-recount-reason"
          />
        </Field>
        <button
          className="btn btn-sm"
          disabled={busy || ticked.size === 0 || source.mixed || !counter || !reason.trim()}
          onClick={recount}
          data-testid="cnt-recount"
        >
          <RotateCcw size={14} /> Assign the recount
        </button>
      </div>

      {count.allowed_actions.includes("close") && report.matches_book && (
        <div className="toolbar">
          <button className="btn btn-cta" disabled={busy} onClick={close} data-testid="cnt-close">
            <ClipboardCheck size={14} /> Close - counted matches the book
          </button>
        </div>
      )}
    </section>
  );
}

function CancelPanel({
  count,
  onDone,
  onError,
}: {
  count: CountDetail;
  onDone: (message: string) => void;
  onError: (e: unknown) => void;
}) {
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function cancel() {
    setBusy(true);
    try {
      const body: CountCancelBody = {
        reason_code: reason.trim(),
        ...(note.trim() ? { note: note.trim() } : {}),
        ...goodsMeta(),
        expected_revision: count.revision,
      };
      await api.post(`${STOCKTAKES}/${count.id}/cancel`, body);
      onDone(
        "Count cancelled. Everything counted is kept; no stock changed; the freeze is lifted.",
      );
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card section-card" data-testid="cnt-cancel-panel">
      <h3 className="h3">Cancel the count</h3>
      <p className="muted">
        Cancelling keeps every pass and scan, changes no stock and lifts the site's freeze.
      </p>
      <div className="form-grid">
        <Field id="cnt-cancel-reason" label="Reason">
          <input
            id="cnt-cancel-reason"
            className="input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            data-testid="cnt-cancel-reason"
          />
        </Field>
        <Field id="cnt-cancel-note" label="Note (optional)">
          <input
            id="cnt-cancel-note"
            className="input"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            data-testid="cnt-cancel-note"
          />
        </Field>
        <button
          className="btn btn-sm"
          disabled={busy || !reason.trim()}
          onClick={cancel}
          data-testid="cnt-cancel"
        >
          <XCircle size={14} /> Cancel the count
        </button>
      </div>
    </section>
  );
}
