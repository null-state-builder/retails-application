import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  STAGE_CHIP,
  commandIdFor,
  STAGE_LABEL,
  lineDraft,
  nextStep,
  rateText,
  reviewLines,
  type DebitNote,
  type DebitNoteList,
  type LineDraft,
  type Pending,
} from "../lib/debitNotes";
import { Money, formatDateTime } from "../lib/format";

const PAGE_API = "/goods-v1/inbound/debit-notes";
const PAGE_PATH = "/money/debit-notes";
const OFFLINE =
  "Debit notes need a connection. Nothing can be saved, sent or issued until it is back; what you typed stays here.";
const SHOWS = [
  { value: "open", label: "Open" },
  { value: "issued", label: "Issued" },
  { value: "cancelled", label: "Cancelled" },
  { value: "all", label: "All" },
] as const;
type Show = (typeof SHOWS)[number]["value"];

function pieces(note: DebitNote): number {
  return note.lines.reduce((sum, line) => sum + line.qty, 0);
}

/** Money > Debit Notes (store operations PRD ST-REC-3; ticket 38).
 *
 *  When a receiving shortage is accepted, the server drafts a debit note to the
 *  vendor at the invoice's cost. Accounts types each line's GST rate (and a cost
 *  where the invoice gave none) and asks the Owner; the Owner approves or sends
 *  it back in the approvals inbox; Accounts then issues it, and it takes head
 *  office's debit note number. An issued note is never edited, and nothing here
 *  posts to the accounts - that waits for OQ-47. The server decides who may do
 *  what; this page only offers what it allows. */
export function DebitNotesPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [show, setShow] = useState<Show>("open");
  const [list, setList] = useState<DebitNoteList | null>(null);
  const [note, setNote] = useState<DebitNote | null>(null);
  const [drafts, setDrafts] = useState<Record<string, LineDraft>>({});
  const [reviewNote, setReviewNote] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  /** The press whose answer was lost, if any (`commandIdFor`). */
  const pending = useRef<Pending | null>(null);
  const request = useRef(0);

  const shown = useRef<{ id: number; revision: number } | null>(null);
  /** Show `fresh`. What was typed is kept when it is the same note at the same
   *  revision - a reload after a dropped connection never loses it - and reset
   *  when the note moved on. */
  const openNote = useCallback((fresh: DebitNote | null) => {
    const same =
      fresh !== null &&
      shown.current !== null &&
      shown.current.id === fresh.id &&
      shown.current.revision === fresh.revision;
    shown.current = fresh ? { id: fresh.id, revision: fresh.revision } : null;
    setNote(fresh);
    if (same) return;
    setDrafts(
      fresh ? Object.fromEntries(fresh.lines.map((line) => [line.key, lineDraft(line)])) : {},
    );
    setReviewNote(fresh?.review_note ?? "");
    setCancelReason("");
  }, []);

  const load = useCallback(async () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    try {
      const [l, n] = await Promise.all([
        api.get<DebitNoteList>(PAGE_API, { params: { show } }),
        id ? api.get<DebitNote>(`${PAGE_API}/${id}`) : Promise.resolve(null),
      ]);
      if (mine !== request.current) return;
      setLost(false);
      setList(l.data);
      openNote(n ? n.data : null);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }, [id, show, openNote]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load();
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load]);

  /** Send one step. A dropped connection keeps what was typed and the command's
   *  identity for the same press (`commandIdFor`); any answer from the server
   *  ends that command. */
  async function send(step: string, body: Record<string, unknown>, said: string) {
    if (!note) return;
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setBusy(true);
    setError("");
    setDone("");
    const attempt = { noteId: note.id, revision: note.revision, step, body: JSON.stringify(body) };
    const commandId = commandIdFor(pending.current, attempt, () => crypto.randomUUID());
    pending.current = { ...attempt, commandId };
    try {
      const answer = await api.post<DebitNote>(`${PAGE_API}/${note.id}/${step}`, {
        ...goodsMeta(note.revision, commandId),
        ...body,
      });
      pending.current = null;
      setDone(said);
      openNote(answer.data);
      const fresh = await api.get<DebitNoteList>(PAGE_API, { params: { show } });
      setList(fresh.data);
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setLost(true);
      } else {
        pending.current = null;
        setError(apiErrorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  const offline = !online || lost;
  const writable = !offline && !busy;
  const allowed = new Set(note?.allowed_actions ?? []);
  const review = note ? reviewLines(note.lines, drafts) : { lines: [], bad: [] };
  const noteChanged = note ? reviewNote.trim() !== note.review_note : false;
  const dirty = review.lines.length > 0 || noteChanged;

  return (
    <div className="page-pad">
      <PageHeader
        title="Debit Notes"
        lead="Debit notes to vendors for pieces invoiced but never received. Accounts reviews and issues them; the Owner approves them in the approvals inbox."
      />
      {offline && (
        <p className="warn-note" data-testid="dn-offline" role="status">
          {OFFLINE}
          {online && (
            <>
              {" "}
              <button
                type="button"
                className="btn"
                data-testid="dn-retry"
                onClick={() => void load()}
              >
                Try again
              </button>
            </>
          )}
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="dn-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="dn-done">
          {done}
        </p>
      )}

      {note && (
        <section className="card section-card" data-testid="dn-detail">
          <div className="toolbar">
            <h2 className="h3">{note.number ?? `Draft debit note ${note.id}`}</h2>
            <span className={`chip ${STAGE_CHIP[note.stage]}`} data-testid="dn-stage">
              {STAGE_LABEL[note.stage]}
            </span>
            <Link className="btn" to={PAGE_PATH} data-testid="dn-close">
              Back to the list
            </Link>
          </div>
          <p data-testid="dn-next">{nextStep(note)}</p>
          {!note.switched_on && note.stage !== "issued" && note.stage !== "cancelled" && (
            <p className="warn-note" data-testid="dn-switched-off">
              Debit notes are switched off at {note.site.name}. The note stays here, but nothing new
              can be done to it until Admin switches them on again in Setup, Feature Switches. It
              can still be cancelled.
            </p>
          )}
          <dl className="facts" data-testid="dn-facts">
            <dt>To the vendor</dt>
            <dd>
              {note.vendor.name}
              {note.vendor.gstin ? ` · GSTIN ${note.vendor.gstin}` : ""}
            </dd>
            <dt>Issued under</dt>
            <dd>GSTIN {note.gstin}</dd>
            <dt>Received at</dt>
            <dd>
              {note.site.code} · {note.site.name}
            </dd>
            <dt>GRN</dt>
            <dd data-testid="dn-grn">
              <Link to={`/goods/receive/grn/${note.grn.id}?step=grn`}>
                {note.grn.number || "Open the GRN"}
              </Link>
            </dd>
            <dt>Vendor's invoice</dt>
            <dd data-testid="dn-claim">
              {note.claim.invoice_number || "No number"}
              {note.claim.invoice_date ? ` of ${note.claim.invoice_date}` : ""} (claim version{" "}
              {note.claim.revision})
            </dd>
            {note.issued_on && (
              <>
                <dt>Issued</dt>
                <dd>
                  {note.issued_on} by {note.issued_by}
                </dd>
              </>
            )}
            {note.stage === "cancelled" && (
              <>
                <dt>Cancelled</dt>
                <dd data-testid="dn-cancel-reason">
                  {note.cancel_reason} ({note.cancelled_by})
                </dd>
              </>
            )}
          </dl>

          <div className="table-wrap">
            <table className="data" data-testid="dn-lines">
              <thead>
                <tr>
                  <th>Item</th>
                  <th className="num">Pieces short</th>
                  <th className="num">Cost per piece</th>
                  <th>GST rate</th>
                  <th className="num">Taxable value</th>
                  <th className="num">Tax</th>
                  <th className="num">Total</th>
                </tr>
              </thead>
              <tbody>
                {note.lines.map((line, index) => {
                  const draft = drafts[line.key] ?? lineDraft(line);
                  const editing = allowed.has("review") && writable;
                  return (
                    <tr key={line.key} data-testid={`dn-line-${index}`}>
                      <td>
                        {line.description}
                        {line.style_code && <div className="muted-cell">{line.style_code}</div>}
                      </td>
                      <td className="num" data-testid="dn-line-qty">
                        {line.qty}
                      </td>
                      <td className="num" data-testid="dn-line-cost">
                        {line.cost_from === "invoice" || !editing ? (
                          line.unit_cost_paise === null ? (
                            <span className="chip chip-amber">Not known</span>
                          ) : (
                            <>
                              <Money paise={Number(line.unit_cost_paise)} />
                              <div className="muted-cell">
                                {line.cost_from === "invoice"
                                  ? "From the invoice"
                                  : "Typed by Accounts"}
                              </div>
                            </>
                          )
                        ) : (
                          <input
                            className="input"
                            inputMode="decimal"
                            aria-label={`Cost per piece in rupees, ${line.description}`}
                            data-testid={`dn-cost-${index}`}
                            value={draft.cost_rupees}
                            onChange={(e) =>
                              setDrafts({
                                ...drafts,
                                [line.key]: { ...draft, cost_rupees: e.target.value },
                              })
                            }
                          />
                        )}
                      </td>
                      <td data-testid="dn-line-rate">
                        {editing ? (
                          <select
                            className="select"
                            aria-label={`GST rate, ${line.description}`}
                            data-testid={`dn-rate-${index}`}
                            value={draft.gst_rate}
                            onChange={(e) =>
                              setDrafts({
                                ...drafts,
                                [line.key]: { ...draft, gst_rate: e.target.value },
                              })
                            }
                          >
                            <option value="">Not set</option>
                            {(list?.gst_rates ?? []).map((rate) => (
                              <option key={rate} value={rate}>
                                {rate}%
                              </option>
                            ))}
                          </select>
                        ) : (
                          rateText(line.gst_rate)
                        )}
                      </td>
                      <td className="num">
                        {line.taxable_paise === null ? (
                          "-"
                        ) : (
                          <Money paise={Number(line.taxable_paise)} />
                        )}
                      </td>
                      <td className="num">
                        {line.tax_paise === null ? "-" : <Money paise={Number(line.tax_paise)} />}
                      </td>
                      <td className="num">
                        {line.total_paise === null ? (
                          "-"
                        ) : (
                          <Money paise={Number(line.total_paise)} />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              <tfoot>
                <tr>
                  <th colSpan={4}>Total</th>
                  <th className="num" data-testid="dn-taxable">
                    <Money paise={Number(note.taxable_paise)} />
                  </th>
                  <th className="num" data-testid="dn-tax">
                    <Money paise={Number(note.tax_paise)} />
                  </th>
                  <th className="num" data-testid="dn-total">
                    <Money paise={Number(note.total_paise)} />
                  </th>
                </tr>
              </tfoot>
            </table>
          </div>
          <p className="muted-cell" data-testid="dn-split">
            {note.split.kind === "intra" && (
              <>
                Within one state: CGST <Money paise={Number(note.split.cgst_paise ?? 0)} />, SGST{" "}
                <Money paise={Number(note.split.sgst_paise ?? 0)} />.
              </>
            )}
            {note.split.kind === "inter" && (
              <>
                Between states: IGST <Money paise={Number(note.split.igst_paise ?? 0)} />.
              </>
            )}
            {note.split.kind === "unknown" &&
              "The vendor's state is not recorded, so the IGST or CGST and SGST split is not shown."}{" "}
            Not posted to the accounts: that waits for OQ-47.
          </p>

          {note.missing.length > 0 && (
            <ul className="warn-note" data-testid="dn-missing">
              {note.missing.map((m, i) => (
                <li key={`${m.code}-${m.line_key ?? i}`}>{m.message}</li>
              ))}
            </ul>
          )}

          {note.approval && (
            <p className="muted-cell" data-testid="dn-approval">
              Asked by {note.approval.requested_by} on {formatDateTime(note.approval.requested_at)}.
              {note.approval.status === "pending" &&
                " Waiting for the Owner in the approvals inbox."}
              {note.approval.status === "approved" &&
                ` Approved by ${note.approval.decided_by}${note.approval.decided_at ? ` on ${formatDateTime(note.approval.decided_at)}` : ""}.`}
              {note.approval.status === "rejected" &&
                ` Sent back by ${note.approval.decided_by}: ${note.approval.reason}`}
            </p>
          )}

          {allowed.has("review") && (
            <label className="field">
              <span>Accounts' note</span>
              <input
                className="input"
                maxLength={240}
                data-testid="dn-review-note"
                disabled={!writable}
                value={reviewNote}
                onChange={(e) => setReviewNote(e.target.value)}
              />
            </label>
          )}
          {review.bad.length > 0 && (
            <p className="warn-note" data-testid="dn-bad-cost">
              A cost per piece is rupees, for example 260 or 260.50.
            </p>
          )}

          <div className="toolbar">
            {allowed.has("review") && (
              <button
                type="button"
                className="btn"
                data-testid="dn-save"
                disabled={!writable || !dirty || review.bad.length > 0}
                onClick={() =>
                  void send(
                    "review",
                    { lines: review.lines, ...(noteChanged ? { note: reviewNote.trim() } : {}) },
                    "Saved.",
                  )
                }
              >
                Save
              </button>
            )}
            {allowed.has("request_approval") && (
              <button
                type="button"
                className="btn btn-primary"
                data-testid="dn-ask"
                disabled={!writable || dirty}
                title={dirty ? "Save your changes first." : undefined}
                onClick={() =>
                  void send(
                    "request-approval",
                    {},
                    "Sent to the Owner. It waits in the approvals inbox.",
                  )
                }
              >
                Ask the Owner to approve
              </button>
            )}
            {allowed.has("issue") && (
              <button
                type="button"
                className="btn btn-primary"
                data-testid="dn-issue"
                disabled={!writable}
                onClick={() => void send("issue", {}, "Issued. It has its debit note number.")}
              >
                Issue the debit note
              </button>
            )}
          </div>
          {allowed.has("cancel") && (
            <div className="toolbar">
              <input
                className="input"
                maxLength={240}
                placeholder="Why it is not being issued"
                aria-label="Why the note is cancelled"
                data-testid="dn-cancel-reason-input"
                disabled={!writable}
                value={cancelReason}
                onChange={(e) => setCancelReason(e.target.value)}
              />
              <button
                type="button"
                className="btn"
                data-testid="dn-cancel"
                disabled={!writable || !cancelReason.trim()}
                onClick={() =>
                  void send(
                    "cancel",
                    { reason: cancelReason.trim() },
                    "Cancelled. It was never numbered.",
                  )
                }
              >
                Cancel the note
              </button>
            </div>
          )}
        </section>
      )}

      <section className="card section-card">
        <div className="toolbar">
          <h2 className="h3">Notes</h2>
          <label className="field">
            <span>Show</span>
            <select
              className="select"
              data-testid="dn-show"
              value={show}
              onChange={(e) => setShow(e.target.value as Show)}
            >
              {SHOWS.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        {!list ? (
          <p className="muted-cell">{offline ? "" : "Loading…"}</p>
        ) : list.notes.length === 0 ? (
          <p className="muted-cell" data-testid="dn-empty">
            No debit notes here. One is drafted when a receiving shortage is accepted.
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="dn-list">
              <thead>
                <tr>
                  <th>Note</th>
                  <th>Vendor</th>
                  <th>Received at</th>
                  <th>GRN</th>
                  <th className="num">Pieces short</th>
                  <th className="num">Total</th>
                  <th>Stage</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.notes.map((row) => (
                  <tr key={row.id} data-testid={`dn-row-${row.id}`}>
                    <td>{row.number ?? `Draft ${row.id}`}</td>
                    <td>{row.vendor.name}</td>
                    <td>{row.site.code}</td>
                    <td>{row.grn.number}</td>
                    <td className="num">{pieces(row)}</td>
                    <td className="num">
                      <Money paise={Number(row.total_paise)} />
                    </td>
                    <td>
                      <span className={`chip ${STAGE_CHIP[row.stage]}`}>
                        {STAGE_LABEL[row.stage]}
                      </span>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn"
                        data-testid={`dn-open-${row.id}`}
                        onClick={() => {
                          setError("");
                          setDone("");
                          navigate(`${PAGE_PATH}/${row.id}`);
                        }}
                      >
                        Open
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
