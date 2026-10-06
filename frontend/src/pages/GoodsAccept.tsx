// Accept goods (ticket 07): a receiver opens (or resumes) an acceptance
// session for an official PT at the actual site, scans goods against the
// correct official line as good, damaged or wrong into a chosen location, and
// completes with leftovers still visible.
//
// Built for a phone at a dock or backstore first (GSA-T07's own UX brief):
// one big scan box that takes keyboard-wedge scanner input and Enter, a card
// per scan that starts "Pending" and turns "Recorded" only once the server
// acknowledges its scan key, and a running tally that groups only rows
// sharing line, tag, MRP, condition and location. Check-only is the default;
// combined check/putaway needs an explicit, non-system destination location.
//
// A genuine gap this screen could not build past: a plain store receiver
// (M-CSH — `receive.arrival`, `stock.accept`, `stock.view`…) holds no read
// that names which official PT is pending acceptance at their site.
// `GET /ptmapper/files` (E098) needs a `pt.view`-class action and
// `GET /alerts/exceptions` needs `exception.view`/`exception.manage` — M-CSH
// has neither, only a store manager (M-STR, which holds both) or a warehouse
// operator (C-WHO, which holds `pt.prepare`) does. So the PT picker below
// works for whoever holds one of those; everyone else — including a plain
// M-CSH receiver — falls back to typing the PT's own official version id,
// which is the one thing E139 itself actually needs. See the ticket's "Open
// for Anand" for the product question this leaves open.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, PackageCheck, RotateCcw, Undo2, WifiOff } from "lucide-react";

import { api, apiErrorCode, apiErrorMessage, goodsMeta } from "../lib/api";
import {
  Feedback,
  Field,
  listState,
  useGoodsFetch,
  useResourceList,
  useStepUp,
  type Page,
  type ResourceDTO,
} from "../lib/goodsScreen";
import { rupeesToPaiseString } from "../lib/goodsPt";
import {
  CONDITION_HELP,
  CONDITION_LABEL,
  CONDITIONS,
  apiErrorIssues,
  extraPieces,
  extraRoute,
  groupScans,
  outcomeFor,
  remainingTotal,
  type AcceptanceData,
  type PendingAcceptanceRow,
  type Condition,
  type QueuedScan,
  type ScanInput,
} from "../lib/goodsAcceptance";
import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { formatDateTime, formatPaiseString } from "../lib/format";
import { deliveryStepPath } from "../lib/goodsReceiving";
import { roleLabel } from "../lib/goodsExceptions";
import "./GoodsAcceptance.css";

/** Non-system location kinds accepted goods may be put away into
 *  (`stockledger.goods_acceptance.PUTAWAY_KINDS` = `goods_engine.TRANSFERABLE_KINDS`). */
const PUTAWAY_KINDS = new Set(["floor", "backstore", "bin", "zone", "fixture"]);

interface LocationData {
  site_id: string;
  kind: string;
  name: string;
  system: boolean;
}
type LocationRow = ResourceDTO<LocationData>;

const QUEUE_KEY = (sessionId: string) => `goods-accept-queue:${sessionId}`;
const MAX_SUPERSEDED_RETRIES = 5;

function loadQueue(sessionId: string): QueuedScan[] {
  try {
    const raw = localStorage.getItem(QUEUE_KEY(sessionId));
    return raw ? (JSON.parse(raw) as QueuedScan[]) : [];
  } catch {
    return [];
  }
}

function saveQueue(sessionId: string, queue: QueuedScan[]) {
  try {
    localStorage.setItem(QUEUE_KEY(sessionId), JSON.stringify(queue));
  } catch {
    // Best-effort only — a receiver on a phone whose storage is full or
    // disabled still gets to scan; they just lose replay-after-reload.
  }
}

/** Open or resume an acceptance session for an official PT at your own site.
 *
 *  Since OPS-17 there is no Accept goods screen of its own (store and warehouse
 *  operations PRD §5.1): the receiving workflow's last step renders this pinned
 *  to its delivery's PTs (`onlyPts` - the primary and any supplement, ticket
 *  07B), and Receive Goods' Pending renders it for the official PTs no delivery
 *  in the inbox carries - opening stock - so those are still put away from the
 *  one place receiving happens (`exceptPts`). */
export function AcceptPanel({
  onlyPts,
  site,
  exceptPts,
  heading = "Waiting to be accepted here",
  quietWhenEmpty = false,
}: {
  /** Show only these PTs' work - the delivery the workflow is standing on. */
  onlyPts?: string[];
  /** The site the caller already knows. Absent ⇒ the person picks one. */
  site?: string;
  /** Leave these PTs out: the inbox already lists their deliveries. */
  exceptPts?: string[];
  heading?: string;
  /** Draw nothing at all when nothing is waiting, rather than an empty card. */
  quietWhenEmpty?: boolean;
}) {
  const { session } = useAuth();
  const sites = session?.sites ?? [];
  const [siteId, setSiteId] = useState<string>(site ?? "");
  const [openDoc, setOpenDoc] = useState<ResourceDTO<AcceptanceData> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const stepUp = useStepUp();

  useEffect(() => {
    if (site) {
      setSiteId(site);
      return;
    }
    if (!siteId && sites[0]) setSiteId(sites[0].id);
  }, [sites, siteId, site]);

  // E248: the work waiting for this person, found with `stock.accept` alone.
  // Before this read the only way to find an official PT was to browse PTs,
  // which meant holding authority over every PT in scope just to be told which
  // two pieces to put away (GSA-T07).
  const waiting = useGoodsFetch<Page<PendingAcceptanceRow>, PendingAcceptanceRow[]>(
    siteId ? `/goods-v1/stockledger/pending-acceptance?site_id=${siteId}&limit=100` : null,
    (r) => r.items ?? [],
    [],
  );

  async function open(sourceVersionId: string) {
    if (!siteId || !sourceVersionId) return;
    setError("");
    setBusy(true);
    try {
      const { data } = await stepUp.guarded(() =>
        api.post<ResourceDTO<AcceptanceData>>("/goods-v1/stockledger/acceptance-sessions", {
          site_id: Number(siteId),
          source_version_id: sourceVersionId,
          ...goodsMeta(),
        }),
      );
      setOpenDoc(data);
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (!session) return null;
  // The workflow stands on one delivery, so it shows one delivery's work.
  const waitingHere = onlyPts
    ? waiting.value.filter((row) => onlyPts.includes(row.pt_id))
    : waiting.value.filter((row) => !(exceptPts ?? []).includes(row.pt_id));
  if (quietWhenEmpty && !openDoc && (waiting.loading || waitingHere.length === 0)) return null;

  if (openDoc) {
    return (
      <div className="accept-layout">
        <PageHeader
          title="Accept goods"
          lead={`Official PT ${openDoc.number ?? openDoc.id.slice(0, 8)} at ${
            sites.find((s) => s.id === siteId)?.name ?? siteId
          }`}
        />
        <button className="btn btn-sm" data-testid="accept-back" onClick={() => setOpenDoc(null)}>
          <RotateCcw size={14} /> Choose a different PT
        </button>
        <SessionPanel doc={openDoc} onDoc={setOpenDoc} siteId={siteId} stepUp={stepUp} />
      </div>
    );
  }

  return (
    <div className="accept-layout">
      {!onlyPts && (
        <PageHeader
          title="Accept goods"
          lead="Open or resume an acceptance session for an official PT at your site."
        />
      )}
      {stepUp.dialog}
      <Feedback error={error} ok="" />
      <div className="form-grid" hidden={Boolean(site)}>
        <Field id="accept-site" label="Site">
          <select
            id="accept-site"
            className="select"
            value={siteId}
            onChange={(e) => setSiteId(e.target.value)}
            data-testid="accept-site"
          >
            {sites.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.code})
              </option>
            ))}
          </select>
        </Field>
      </div>

      <div className="card section-card" data-testid="accept-waiting">
        <h3 className="h3">{heading}</h3>
        {listState(
          { loading: waiting.loading, failure: waiting.failure, empty: waitingHere.length === 0 },
          "Nothing at this site is waiting to be accepted.",
        ) ?? (
          <ul className="acc-pt-list" data-testid="accept-pt-list">
            {waitingHere.map((row) => (
              <li key={row.official_version_id}>
                <span>
                  <b>{row.pt_number ?? row.pt_id.slice(0, 8)}</b> —{" "}
                  {row.remaining_qty > 0
                    ? `${row.remaining_qty} piece(s) still to accept`
                    : "All pieces accepted; finish the open session"}
                </span>
                <button
                  className="btn btn-sm"
                  disabled={busy}
                  onClick={() => open(row.official_version_id)}
                  data-testid={`accept-open-${row.pt_id}`}
                >
                  {row.remaining_qty > 0 ? "Open" : "Finish session"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The open session: pending lines, the scan box, the running tally
// ---------------------------------------------------------------------------

function SessionPanel({
  doc,
  onDoc,
  siteId,
  stepUp,
}: {
  doc: ResourceDTO<AcceptanceData>;
  onDoc: (doc: ResourceDTO<AcceptanceData>) => void;
  siteId: string;
  stepUp: ReturnType<typeof useStepUp>;
}) {
  // The queue lives in a ref, not only in state: several scans submitted in
  // quick succession (a receiver scanning fast) must never race each other
  // into overlapping `flush` calls sharing one stale `expected_revision` —
  // `sending` as plain React state is not enough of a mutex, since two
  // synchronous calls can both read it before either's `setSending(true)`
  // has rendered. `queueRef`/`sendingRef` give `flush` something to check and
  // set *now*, not next render; `queueVersion` only exists to make the ref's
  // changes visible to `groupScans`/JSX below.
  const queueRef = useRef<QueuedScan[]>(loadQueue(doc.id));
  const [queueVersion, setQueueVersion] = useState(0);
  const sendingRef = useRef(false);
  // The revision `flush` sends is tracked here, updated the instant a scan
  // response lands — never read off the `doc` *prop*, which only carries a
  // fresh revision after `onDoc` triggers a render one tick later, by which
  // time a queued next scan may already have fired.
  const revisionRef = useRef(doc.revision);
  // A bound on how many times a REVISION_SUPERSEDED retry chases a moving
  // target before giving up and surfacing it as a real error — someone else
  // scanning this session at exactly the same rate is a vanishingly unlikely
  // way to spend a whole minute, so this is a safety valve, not a normal path.
  const supersededStreakRef = useRef(0);
  const [online, setOnline] = useState(navigator.onLine);
  const [error, setError] = useState("");
  const [completeError, setCompleteError] = useState("");
  const [confirmingComplete, setConfirmingComplete] = useState(false);
  const [completed, setCompleted] = useState(doc.data.state === "completed");
  // Ticket 07B: the scan that turned out to be more than this PT covers, and
  // whether the way back is on screen (opened by that refusal, or by hand).
  const [extra, setExtra] = useState<FoundExtra | null>(null);
  const [showExtra, setShowExtra] = useState(false);

  useEffect(() => {
    revisionRef.current = doc.revision;
  }, [doc.revision]);

  useEffect(() => {
    function goOnline() {
      setOnline(true);
      void flush();
    }
    function goOffline() {
      setOnline(false);
    }
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", goOffline);
    // Scans restored from localStorage (a reload after a scan that never got
    // an answer) are already "pending" — resend them now rather than waiting
    // for a future reconnect that, if the connection never actually dropped,
    // is never coming.
    if (navigator.onLine) void flush();
    return () => {
      window.removeEventListener("online", goOnline);
      window.removeEventListener("offline", goOffline);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const locations = useResourceList<LocationData>(
    `/goods-v1/masters/stores/${siteId}/locations?limit=100`,
  );
  const putawayLocations = locations.items.filter(
    (l) => !l.data.system && PUTAWAY_KINDS.has(l.data.kind),
  );

  const remaining = remainingTotal(doc.data.lines.items);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on queueVersion, the ref's own change signal
  const groups = useMemo(() => groupScans(queueRef.current), [queueVersion]);
  const canScan = doc.allowed_actions.includes("scan") && !completed;
  const canComplete = doc.allowed_actions.includes("complete") && !completed;

  function commitQueue(next: QueuedScan[]) {
    queueRef.current = next;
    saveQueue(doc.id, next);
    setQueueVersion((v) => v + 1);
  }

  async function flush() {
    if (sendingRef.current || !navigator.onLine) return;
    const toSend = queueRef.current.filter((s) => s.status === "pending");
    if (toSend.length === 0) return;
    sendingRef.current = true;
    setError("");
    try {
      const { data } = await stepUp.guarded(() =>
        api.post<ResourceDTO<AcceptanceData>>(
          `/goods-v1/stockledger/acceptance-sessions/${doc.id}/scan`,
          {
            observations: toSend.map((s) => s.input),
            ...goodsMeta(revisionRef.current),
          },
        ),
      );
      revisionRef.current = data.revision;
      supersededStreakRef.current = 0;
      onDoc(data);
      const acknowledged = new Set(data.data.acknowledged_scan_keys);
      commitQueue(
        queueRef.current.map((s) =>
          acknowledged.has(s.key)
            ? { key: s.key, input: s.input, lineLabel: s.lineLabel, status: "recorded" }
            : s,
        ),
      );
    } catch (e) {
      const code = apiErrorCode(e);
      if (!(e as { response?: unknown })?.response) {
        // A network failure, not a refusal — the scans stay pending and the
        // offline banner explains why (R13: resend the same keys later).
        // Nothing here is a deterministic "no", so retrying costs nothing.
        setOnline(navigator.onLine);
        return;
      }
      if (code === "REVISION_SUPERSEDED" && supersededStreakRef.current < MAX_SUPERSEDED_RETRIES) {
        // The one server-side refusal this screen treats as transient: the
        // session moved under us, not that any scan itself was wrong.
        // `toSend` stays "pending" — the `finally` block below retries it
        // with the freshly-read revision, and a successful retry clears this
        // message the moment it starts (the next flush's own `setError("")`).
        // Bounded above so a genuinely stuck conflict surfaces as an error
        // instead of retrying forever.
        supersededStreakRef.current += 1;
        try {
          const { data: fresh } = await api.get<ResourceDTO<AcceptanceData>>(
            `/goods-v1/stockledger/acceptance-sessions/${doc.id}`,
          );
          revisionRef.current = fresh.revision;
          onDoc(fresh);
        } catch {
          // fall through to the message below
        }
        setError("Someone else scanned into this session. Reloaded — try again.");
        return;
      }
      // Every other refusal the server actually answered (TAG_MISMATCH,
      // ACCEPTANCE_INVALID, IDENTITY_UNRESOLVED, ACTION_DENIED, a 5xx) is
      // deterministic: retrying the exact same payload would only refuse it
      // again, forever, since the `finally` block below re-flushes whenever
      // anything is still "pending". So every entry in this batch is
      // resolved to "error" here — the ones a per-scan issue names get that
      // issue's own message, and any that don't (a request-level refusal
      // with no per-observation issue) get the refusal's own message —
      // never left "pending" to loop.
      const issues = apiErrorIssues(e);
      const byIndex = new Map(
        issues
          .map((issue) => {
            const m = /^observations\[(\d+)\]/.exec(issue.field ?? "");
            return m ? ([Number(m[1]), issue.message] as const) : null;
          })
          .filter((v): v is readonly [number, string] => v !== null),
      );
      const fallbackMessage = apiErrorMessage(e);
      // More pieces than this PT covers is not a mistake to dismiss and rescan:
      // it goes back to the goods receipt. Open the way back on the first one.
      const [firstExtra] = [...extraPieces(issues).keys()];
      const extraScan = firstExtra === undefined ? undefined : toSend[firstExtra];
      if (extraScan) {
        setExtra({
          alias: extraScan.input.alias_value,
          qty: extraScan.input.qty,
          lineId: extraScan.input.official_line_id,
        });
        setShowExtra(true);
      }
      commitQueue(
        queueRef.current.map((s) => {
          const index = toSend.findIndex((t) => t.key === s.key);
          if (index < 0) return s;
          return { ...s, status: "error", error: byIndex.get(index) ?? fallbackMessage };
        }),
      );
      setError(fallbackMessage);
    } finally {
      sendingRef.current = false;
      // A scan added while this request was in flight is still pending —
      // pick it up now rather than waiting for the next scan or reconnect.
      // Nothing here can loop forever: the only statuses left "pending" by
      // the catch above are a network failure or REVISION_SUPERSEDED, both
      // genuinely worth retrying, and every deterministic refusal was just
      // resolved to "error" and so no longer matches this check.
      if (queueRef.current.some((s) => s.status === "pending") && navigator.onLine) {
        void flush();
      }
    }
  }

  function enqueue(input: ScanInput, lineLabel: string) {
    commitQueue([
      ...queueRef.current,
      { key: input.scan_key, input, lineLabel, status: "pending" },
    ]);
    void flush();
  }

  function removeErrored(key: string) {
    commitQueue(queueRef.current.filter((s) => s.key !== key));
  }

  async function complete(confirm: boolean) {
    setCompleteError("");
    // The same one transient refusal `flush` recognises, for the same reason:
    // the session moved on, not that completing it is wrong. It moves on with
    // *every scan*, including this receiver's own - so without this retry the
    // person who scanned the whole session is the one told somebody else did,
    // and Complete refuses for as long as they keep pressing it.
    for (let attempt = 0; attempt <= MAX_SUPERSEDED_RETRIES; attempt += 1) {
      try {
        const { data } = await stepUp.guarded(() =>
          api.post<ResourceDTO<AcceptanceData>>(
            `/goods-v1/stockledger/acceptance-sessions/${doc.id}/complete`,
            { confirm_complete: confirm, ...goodsMeta(revisionRef.current) },
          ),
        );
        revisionRef.current = data.revision;
        onDoc(data);
        setCompleted(true);
        setConfirmingComplete(false);
        try {
          localStorage.removeItem(QUEUE_KEY(doc.id));
        } catch {
          // best-effort
        }
        return;
      } catch (e) {
        if (apiErrorCode(e) !== "REVISION_SUPERSEDED" || attempt === MAX_SUPERSEDED_RETRIES) {
          setCompleteError(apiErrorMessage(e));
          return;
        }
        try {
          const { data: fresh } = await api.get<ResourceDTO<AcceptanceData>>(
            `/goods-v1/stockledger/acceptance-sessions/${doc.id}`,
          );
          revisionRef.current = fresh.revision;
          onDoc(fresh);
        } catch {
          setCompleteError(apiErrorMessage(e));
          return;
        }
      }
    }
  }

  return (
    <div>
      {stepUp.dialog}
      {!online && (
        <div className="acc-offline" data-testid="accept-offline">
          <WifiOff size={16} /> Not sent yet, will resend.
        </div>
      )}
      <Feedback error={error} ok="" />
      {completed && (
        <div className="ok-note" data-testid="accept-completed">
          Session completed.{" "}
          {remaining > 0 ? `${remaining} piece(s) stay open, not written off.` : ""}
        </div>
      )}

      <h3 className="h3">Pending official lines</h3>
      {listState(
        { loading: false, failure: "", empty: doc.data.lines.items.length === 0 },
        "This session has no lines.",
      ) ?? (
        <table className="acc-lines" data-testid="accept-lines">
          <thead>
            <tr>
              <th>Tag</th>
              <th className="num">MRP</th>
              <th className="num">Expected</th>
              <th className="num">Checked</th>
              <th className="num">Accepted</th>
              <th className="num">Damaged</th>
              <th className="num">Remaining</th>
            </tr>
          </thead>
          <tbody>
            {doc.data.lines.items.map((line) => (
              <tr key={line.official_line_id} data-testid={`accept-line-${line.official_line_id}`}>
                <td>{line.alias_as_used ?? "—"}</td>
                <td className="num">{formatPaiseString(line.mrp_paise)}</td>
                <td className="num">{line.expected_qty}</td>
                <td className="num">{line.checked_qty}</td>
                <td className="num">{line.accepted_qty}</td>
                <td className="num">{line.damaged_qty}</td>
                <td className="num">{line.remaining_qty}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {canScan && (
        <ScanForm
          lines={doc.data.lines.items}
          locations={putawayLocations}
          locationsFailure={locations.failure}
          onScan={enqueue}
        />
      )}

      <h3 className="h3">Scanned so far</h3>
      {listState(
        { loading: false, failure: "", empty: groups.length === 0 },
        "Nothing scanned yet.",
      ) ?? (
        <table className="acc-tally" data-testid="accept-tally">
          <thead>
            <tr>
              <th>Line</th>
              <th>Tag</th>
              <th className="num">MRP</th>
              <th>Condition</th>
              <th className="num">Qty</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.key} data-testid={`accept-group-${g.key}`}>
                <td>{g.lineLabel}</td>
                <td>{g.alias}</td>
                <td className="num">{formatPaiseString(g.mrp)}</td>
                <td>{CONDITION_LABEL[g.condition]}</td>
                <td className="num">{g.qty}</td>
                <td>
                  {g.recordedQty > 0 && (
                    <span className="chip chip-green">{g.recordedQty} recorded</span>
                  )}
                  {g.pendingQty > 0 && (
                    <span className="chip chip-amber">{g.pendingQty} pending</span>
                  )}
                  {g.errorQty > 0 && <span className="chip chip-red">{g.errorQty} failed</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {queueRef.current.some((s) => s.status === "error") && (
        <div className="acc-errors" data-testid="accept-errors">
          {queueRef.current
            .filter((s) => s.status === "error")
            .map((s) => (
              <p key={s.key}>
                <AlertTriangle size={14} /> {s.input.alias_value}: {s.error}{" "}
                <button
                  className="btn btn-sm"
                  onClick={() => removeErrored(s.key)}
                  data-testid={`accept-dismiss-${s.key}`}
                >
                  Dismiss
                </button>
              </p>
            ))}
        </div>
      )}

      {showExtra ? (
        <ExtraPiecesPanel
          doc={doc}
          found={extra}
          onDoc={onDoc}
          stepUp={stepUp}
          onClose={() => {
            setShowExtra(false);
            setExtra(null);
          }}
        />
      ) : (
        <button
          className="btn btn-sm"
          onClick={() => setShowExtra(true)}
          data-testid="accept-extra-open"
        >
          <Undo2 size={14} /> Found more pieces than this PT?
        </button>
      )}

      {canComplete && (
        <div className="card section-card">
          <h3 className="h3">Complete session</h3>
          <Feedback error={completeError} ok="" />
          {remaining > 0 ? (
            confirmingComplete ? (
              <div>
                <p className="lead">
                  {remaining} piece(s) are still not accepted. They stay visible and are not written
                  off.
                </p>
                <ul>
                  {doc.data.lines.items
                    .filter((l) => l.remaining_qty > 0)
                    .map((l) => (
                      <li key={l.official_line_id}>
                        {l.alias_as_used ?? l.line_key}: {l.remaining_qty} left
                      </li>
                    ))}
                </ul>
                <button
                  className="btn btn-cta"
                  onClick={() => complete(true)}
                  data-testid="accept-complete-confirm"
                >
                  <PackageCheck size={14} /> Complete anyway
                </button>
                <button className="btn btn-sm" onClick={() => setConfirmingComplete(false)}>
                  Cancel
                </button>
              </div>
            ) : (
              <button
                className="btn btn-cta"
                onClick={() => setConfirmingComplete(true)}
                data-testid="accept-complete"
              >
                Complete
              </button>
            )
          ) : (
            <button
              className="btn btn-cta"
              onClick={() => complete(false)}
              data-testid="accept-complete"
            >
              <PackageCheck size={14} /> Complete
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Ticket 07B: more pieces than the PT covers - the way back, or the handoff
// ---------------------------------------------------------------------------

interface FoundExtra {
  alias: string;
  qty: number;
  lineId: string | null;
}

/** Extra pieces are never accepted here and never change the PT (GSA-T07).
 *
 *  They go back the governed way: the goods receipt's count (counter-GRN), a
 *  decision on the extra (`accept_excess`, approved by someone else) and a
 *  supplement PT, whose pieces then wait here on their own. Whoever may start
 *  that correction is shown the route and a link to the goods receipt; anyone
 *  else hands the pieces over (E251) to the role that owns it. Which of the two
 *  a person sees is the server's `correction.can_correct`, and a link grants
 *  nothing - the receipt is only named to someone who may read it. */
function ExtraPiecesPanel({
  doc,
  found,
  onDoc,
  stepUp,
  onClose,
}: {
  doc: ResourceDTO<AcceptanceData>;
  found: FoundExtra | null;
  onDoc: (doc: ResourceDTO<AcceptanceData>) => void;
  stepUp: ReturnType<typeof useStepUp>;
  onClose: () => void;
}) {
  const correction = doc.data.correction;
  const route = extraRoute(correction);
  const canHandOver = doc.allowed_actions.includes("report_extra");
  const [alias, setAlias] = useState(found?.alias ?? "");
  const [qty, setQty] = useState(found?.qty ?? 1);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");

  async function handOver() {
    setError("");
    setOk("");
    if (!alias.trim()) {
      setError("Scan or type the tag of the extra pieces.");
      return;
    }
    setBusy(true);
    try {
      const { data } = await stepUp.guarded(() =>
        api.post<ResourceDTO<AcceptanceData>>(
          `/goods-v1/stockledger/acceptance-sessions/${doc.id}/extra`,
          {
            alias_value: alias.trim(),
            qty,
            official_line_id: found?.lineId ?? null,
            note: note.trim() || null,
            ...goodsMeta(),
          },
        ),
      );
      onDoc(data);
      setNote("");
      setOk("Handed over. Keep the pieces apart until they are dealt with.");
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const handoff = correction?.handoff ?? null;

  return (
    <div className="card section-card acc-extra" data-testid="accept-extra">
      <h3 className="h3">
        <AlertTriangle size={16} /> More pieces than this PT covers
      </h3>
      <p className="lead">
        {found
          ? `${found.qty} piece(s) with tag ${found.alias} were not accepted, and the PT has not changed. `
          : ""}
        Extra pieces are never accepted here. Keep them apart: they go back to the goods
        receipt&apos;s count, someone decides the extra, and a supplement PT covers them. Then they
        wait here to be accepted on their own.
      </p>

      {correction && (
        <dl className="acc-extra-refs" data-testid="accept-extra-refs">
          <div>
            <dt>Goods receipt</dt>
            <dd>{correction.grn_number ?? "Not shown to you"}</dd>
          </div>
          <div>
            <dt>This PT</dt>
            <dd>
              {correction.pt_number ?? correction.pt_id.slice(0, 8)}
              {correction.receipt_kind === "supplement" ? " (supplement)" : ""}
            </dd>
          </div>
        </dl>
      )}

      {route === "none" && (
        <p className="muted" data-testid="accept-extra-none">
          This PT has no goods receipt to correct. Tell the person who prepared it.
        </p>
      )}

      {route === "correct" && correction && (
        <ol className="acc-extra-steps" data-testid="accept-extra-steps">
          <li>
            Correct the count on the goods receipt with a counter-GRN. Someone else approves it.{" "}
            {correction.grn_id && (
              <Link
                className="btn btn-sm"
                to={deliveryStepPath("grn", correction.grn_id, "grn")}
                data-testid="accept-extra-grn"
              >
                Open {correction.grn_number ?? "the goods receipt"}
              </Link>
            )}
          </li>
          <li>
            The extra is decided on the receipt&apos;s Discrepancies step (&quot;Accept the
            extra&quot;), and a second person approves that decision.
          </li>
          <li>
            The warehouse prepares a supplement PT for those pieces only; it is approved separately.
          </li>
          <li>The extra pieces then wait here, on the supplement, to be accepted.</li>
        </ol>
      )}

      {(route === "hand_over" || route === "handed_over") && (
        <div data-testid="accept-extra-handoff">
          {route === "handed_over" && handoff ? (
            <p className="ok-note" data-testid="accept-extra-handed-over">
              Handed over {formatDateTime(handoff.opened_at)}. {roleLabel(handoff.owner_role)} owns
              it until the extra on this goods receipt is decided.
            </p>
          ) : (
            <p className="lead">
              You cannot correct the goods receipt yourself. Hand the pieces over: whoever owns the
              correction is told, and it stays theirs until it is dealt with.
            </p>
          )}
          <Feedback error={error} ok={ok} />
          <div className="form-grid">
            <Field id="acc-extra-alias" label="Tag on the extra pieces">
              <input
                id="acc-extra-alias"
                className="input"
                value={alias}
                onChange={(e) => setAlias(e.target.value)}
                data-testid="accept-extra-alias"
              />
            </Field>
            <Field id="acc-extra-qty" label="How many">
              <input
                id="acc-extra-qty"
                className="input"
                type="number"
                min={1}
                value={qty}
                onChange={(e) => setQty(Math.max(1, Number(e.target.value) || 1))}
                data-testid="accept-extra-qty"
              />
            </Field>
            <Field id="acc-extra-note" label="Note (optional)">
              <input
                id="acc-extra-note"
                className="input"
                value={note}
                maxLength={500}
                onChange={(e) => setNote(e.target.value)}
                data-testid="accept-extra-note"
              />
            </Field>
          </div>
          <button
            className="btn btn-cta btn-sm"
            disabled={busy || !canHandOver}
            onClick={handOver}
            data-testid="accept-extra-hand-over"
          >
            {route === "handed_over" ? "Add these pieces to the handoff" : "Hand over"}
          </button>
        </div>
      )}

      <button className="btn btn-sm" onClick={onClose} data-testid="accept-extra-close">
        Close
      </button>
    </div>
  );
}

function ScanForm({
  lines,
  locations,
  locationsFailure,
  onScan,
}: {
  lines: AcceptanceData["lines"]["items"];
  locations: LocationRow[];
  locationsFailure: string;
  onScan: (input: ScanInput, lineLabel: string) => void;
}) {
  const [officialLineId, setOfficialLineId] = useState("");
  const [alias, setAlias] = useState("");
  const [mrpText, setMrpText] = useState("");
  const [condition, setCondition] = useState<Condition>("good");
  const [putaway, setPutaway] = useState(false);
  const [locationId, setLocationId] = useState("");
  const [qty, setQty] = useState(1);
  const [validation, setValidation] = useState("");
  const aliasBox = useRef<HTMLInputElement>(null);

  const selectedLine = lines.find((l) => l.official_line_id === officialLineId) ?? null;

  useEffect(() => {
    if (selectedLine) {
      if (!alias) setAlias(selectedLine.alias_as_used ?? "");
      if (!mrpText && selectedLine.mrp_paise) setMrpText(selectedLine.mrp_paise);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [officialLineId]);

  function reset() {
    setQty(1);
    setValidation("");
    // A keyboard-wedge scanner types into whatever the box already holds —
    // it does not select-all first — so a stale alias here means the next
    // scan concatenates onto it instead of replacing it. Always cleared to
    // empty, never re-prefilled, so the next scan's keystrokes land clean.
    setAlias("");
    // MRP is read off the printed tag and typed, not scanned repeatedly —
    // it stays at the chosen line's own value (or whatever was last typed)
    // so a run of good scans against the same line need not retype it.
    aliasBox.current?.focus();
  }

  function submit() {
    setValidation("");
    if (!alias.trim()) {
      setValidation("Scan or type the tag first.");
      return;
    }
    const outcome = outcomeFor(condition, putaway);
    let mrp: string | null = null;
    if (outcome !== "wrong") {
      const parsed = rupeesToPaiseString(mrpText);
      if (parsed === undefined || parsed === null) {
        setValidation("Enter the ticket MRP printed on the tag.");
        return;
      }
      mrp = parsed;
    }
    let location: string | null = null;
    if (outcome === "accepted_good") {
      if (!locationId) {
        setValidation("Choose where these pieces go.");
        return;
      }
      location = locationId;
    }
    const input: ScanInput = {
      scan_key: crypto.randomUUID(),
      outcome,
      condition,
      qty,
      alias_value: alias.trim(),
      observed_ticket_mrp_paise: mrp,
      official_line_id: officialLineId || null,
      // Never sent. A physical Code 128 carries only the frozen printable alias,
      // so there is no evidence id on the tag for a receiver to read and none
      // for this screen to type on their behalf; the server derives it from the
      // official line's own frozen source or print job (GSA-T07).
      label_evidence_id: null,
      chosen_sku_id: null,
      location_id: location,
      actual_at: new Date().toISOString(),
    };
    onScan(input, selectedLine?.alias_as_used ?? alias.trim());
    reset();
  }

  return (
    <fieldset className="card section-card acc-scan-form" data-testid="accept-scan-form">
      <legend>Scan</legend>
      <div className="form-grid">
        <Field
          id="acc-line"
          label="Official line (optional — leave blank to let the tag resolve it)"
        >
          <select
            id="acc-line"
            className="select"
            value={officialLineId}
            onChange={(e) => setOfficialLineId(e.target.value)}
            data-testid="accept-line-select"
          >
            <option value="">Resolve from the scanned tag</option>
            {lines
              .filter((l) => l.remaining_qty > 0)
              .map((l) => (
                <option key={l.official_line_id} value={l.official_line_id}>
                  {l.alias_as_used ?? l.line_key} — {l.remaining_qty} remaining
                </option>
              ))}
          </select>
        </Field>
      </div>

      <div className="form-grid">
        <Field id="acc-alias" label="Scanned tag text">
          <input
            id="acc-alias"
            ref={aliasBox}
            className="input acc-big"
            autoFocus
            value={alias}
            onChange={(e) => setAlias(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
            data-testid="accept-alias"
          />
        </Field>
      </div>

      <fieldset className="acc-conditions" role="radiogroup" aria-label="Condition">
        <legend>Condition</legend>
        {CONDITIONS.map((c) => (
          <label key={c} className="acc-condition">
            <input
              type="radio"
              name="acc-condition"
              checked={condition === c}
              onChange={() => setCondition(c)}
              data-testid={`accept-condition-${c}`}
            />
            <span>
              {CONDITION_LABEL[c]}
              <small>{CONDITION_HELP[c]}</small>
            </span>
          </label>
        ))}
      </fieldset>

      {condition !== "wrong" && (
        <div className="form-grid">
          <Field id="acc-mrp" label="Ticket MRP">
            <input
              id="acc-mrp"
              className="input"
              inputMode="decimal"
              value={mrpText}
              onChange={(e) => setMrpText(e.target.value)}
              data-testid="accept-mrp"
            />
          </Field>
        </div>
      )}

      {condition === "good" && (
        <div className="form-grid">
          <label className="acc-putaway-toggle">
            <input
              id="acc-putaway"
              type="checkbox"
              checked={putaway}
              onChange={(e) => {
                setPutaway(e.target.checked);
                if (!e.target.checked) setLocationId("");
              }}
              data-testid="accept-putaway-toggle"
            />{" "}
            Check and put away
          </label>
          {putaway && (
            <Field id="acc-location" label="Destination location">
              <select
                id="acc-location"
                className="select"
                value={locationId}
                onChange={(e) => setLocationId(e.target.value)}
                data-testid="accept-location"
              >
                <option value="">Choose a location</option>
                {locations.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.data.name} ({l.data.kind})
                  </option>
                ))}
              </select>
              {locationsFailure && <span className="warn-note">{locationsFailure}</span>}
            </Field>
          )}
        </div>
      )}
      {condition === "damaged" && (
        <p className="lead" data-testid="accept-damaged-note">
          Goes to quarantine, damage hold.
        </p>
      )}

      <div className="form-grid">
        <Field id="acc-qty" label="Quantity">
          <input
            id="acc-qty"
            className="input acc-big"
            type="number"
            min={1}
            value={qty}
            onChange={(e) => setQty(Math.max(1, Number(e.target.value) || 1))}
            data-testid="accept-qty"
          />
        </Field>
        <button className="btn btn-cta" onClick={submit} data-testid="accept-submit-scan">
          Add scan
        </button>
      </div>
      {validation && <span className="warn-note">{validation}</span>}
    </fieldset>
  );
}
