import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { PageHeader } from "../../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../../lib/api";
import type { ApiRead, ApiSchemas } from "../../lib/api";
import { isConnectionLost } from "../../lib/auditLog";
import {
  CONSENT_QUESTIONS,
  CUSTOMERS_OFFLINE,
  answeredHow,
  consentChip,
  correctionRequest,
  lastFourMatches,
  mergeRequest,
  moveRequest,
  newNumberOk,
  otherNumberNote,
  searchQuery,
} from "../../lib/customerRights";
import type { ConsentQuestion, CorrectionDraft } from "../../lib/customerRights";
import { Money, formatDateTime } from "../../lib/format";
import { savedSizeNote } from "../../lib/savedSizes";
import { withQuery } from "../../lib/query";
import { CUSTOMERS_API } from "./CustomerList";
import "./Customers.css";

type Customer = ApiRead<ApiSchemas["CustomerPage"]>;
type Held = ApiRead<ApiSchemas["CustomerHeld"]>;
type Listing = ApiRead<ApiSchemas["CustomerList"]>;
type Row = Listing["customers"][number];
type Right = "show" | "correct" | "erase" | "merge" | "move" | `withdraw-${ConsentQuestion}`;

/** A command's identity per right, kept across retries: if the connection drops
 *  after the server acted, pressing again replays the same command instead of
 *  acting twice. A fresh one is taken once the server has answered. */
function useCommandIds(): [(right: Right) => string, (right: Right) => void] {
  const ids = useRef(new Map<Right, string>());
  const idFor = (right: Right) => {
    let id = ids.current.get(right);
    if (!id) {
      id = crypto.randomUUID();
      ids.current.set(right, id);
    }
    return id;
  };
  return [idFor, (right) => ids.current.delete(right)];
}

/** Customers > a customer (store operations PRD §8 ST-CUS-1; ticket 16).
 *
 *  Name, number, optional GSTIN, both consents and the bills they bought on at
 *  the person's stores. The rights screen is below: at the customer's request
 *  store staff show everything held, correct it, withdraw a consent, or erase
 *  it. With the customer present they also merge another record for the same
 *  person into this one, or move it to a new number (ticket 17), where that is
 *  switched on. Bills are tax invoices and stay as printed. Each action is recorded by
 *  the server, which also decides who may act; this page only offers what it
 *  allows. Online only: offline it says so and keeps what was typed. */
export function CustomerPage() {
  const { id } = useParams();
  const [customer, setCustomer] = useState<Customer | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const [siteId, setSiteId] = useState<number | null>(null);
  const [held, setHeld] = useState<Held | null>(null);
  const [draft, setDraft] = useState<CorrectionDraft | null>(null);
  const [was, setWas] = useState<CorrectionDraft | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [lastFour, setLastFour] = useState("");
  const [erased, setErased] = useState(false);
  const [merging, setMerging] = useState(false);
  const [mergeTerm, setMergeTerm] = useState("");
  const [mergeFound, setMergeFound] = useState<Row[] | null>(null);
  const [mergePick, setMergePick] = useState<Row | null>(null);
  const [mergeDigits, setMergeDigits] = useState("");
  const [mergePresent, setMergePresent] = useState(false);
  const [moving, setMoving] = useState(false);
  const [newNumber, setNewNumber] = useState("");
  const [movePresent, setMovePresent] = useState(false);
  const [commandId, answered] = useCommandIds();

  const load = useCallback(async () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    try {
      const response = await api.get<Customer>(`${CUSTOMERS_API}/${id}`);
      setLost(false);
      setMissing(false);
      setCustomer(response.data);
      setSiteId((current) => current ?? response.data.stores[0]?.id ?? null);
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else if ((reason as { response?: { status?: number } }).response?.status === 404)
        setMissing(true);
      else setError(apiErrorMessage(reason));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      if (!erased) void load();
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load, erased]);

  /** Send one right. A dropped connection keeps the form and the command's
   *  identity; any answer from the server ends that command. */
  async function act<T>(right: Right, path: string, body: Record<string, unknown>): Promise<T | null> {
    if (!navigator.onLine) {
      setOnline(false);
      return null;
    }
    setBusy(true);
    setError("");
    setDone("");
    try {
      const response = await api.post<T>(`${CUSTOMERS_API}/${id}/${path}`, {
        ...goodsMeta(undefined, commandId(right)),
        ...body,
      });
      answered(right);
      setLost(false);
      return response.data;
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setLost(true);
      } else {
        answered(right);
        setError(apiErrorMessage(reason));
      }
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function show() {
    const answer = await act<Held>("show", "held", { site_id: siteId });
    if (answer) {
      setHeld(answer);
      setDone("Shown to the customer. This has been recorded.");
    }
  }

  async function correct() {
    if (!draft || !was || siteId === null) return;
    const answer = await act<Customer>("correct", "correct", correctionRequest(siteId, draft, was));
    if (answer) {
      setCustomer(answer);
      setDraft(null);
      setWas(null);
      setHeld(null);
      setDone("Corrected. Every till learns it at its next sync. Past bills keep what they printed.");
    }
  }

  async function withdraw(question: ConsentQuestion) {
    const answer = await act<Customer>(`withdraw-${question}`, "withdraw", {
      site_id: siteId,
      question,
    });
    if (answer) {
      setCustomer(answer);
      setHeld(null);
      setDone("Withdrawn. It takes effect now.");
    }
  }

  async function erase() {
    const answer = await act<{ erased: boolean }>("erase", "erase", {
      site_id: siteId,
      mobile_last4: lastFour.trim(),
    });
    if (answer?.erased) {
      setErased(true);
      setHeld(null);
    }
  }

  /** Find the other record to merge in, among the customers the person reads. */
  async function findOther() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Listing>(withQuery(CUSTOMERS_API, searchQuery(mergeTerm)));
      setLost(false);
      setMergeFound(response.data.customers.filter((row) => row.id !== Number(id)));
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }

  function closeMerge() {
    setMerging(false);
    setMergeTerm("");
    setMergeFound(null);
    setMergePick(null);
    setMergeDigits("");
    setMergePresent(false);
  }

  async function merge() {
    if (!mergePick || siteId === null) return;
    const answer = await act<Customer>(
      "merge",
      "merge",
      mergeRequest(siteId, mergePick.id, mergeDigits, mergePresent),
    );
    if (answer) {
      setCustomer(answer);
      setHeld(null);
      closeMerge();
      setDone(
        "Merged. Both records' bills are this customer's now; the other record is closed, not deleted.",
      );
    }
  }

  function closeMove() {
    setMoving(false);
    setNewNumber("");
    setMovePresent(false);
  }

  async function move() {
    if (!customer || siteId === null) return;
    const answer = await act<Customer>(
      "move",
      "move",
      moveRequest(siteId, newNumber, customer.mobile, movePresent),
    );
    if (answer) {
      setCustomer(answer);
      setHeld(null);
      closeMove();
      setDone(
        "Moved to the new number. Old bills stay theirs; the old number's consents are withdrawn, and the customer answers again on the display.",
      );
    }
  }

  const offline = !online || lost;
  const writable = !offline && !busy && siteId !== null;
  const offlineNote = offline && (
    <p className="warn-note" data-testid="customer-offline" role="status">
      {CUSTOMERS_OFFLINE}
      {online && (
        <>
          {" "}
          <button type="button" className="btn" data-testid="customer-retry" onClick={() => void load()}>
            Try again
          </button>
        </>
      )}
    </p>
  );
  const notes = (
    <>
      {error && (
        <p className="warn-note" data-testid="customer-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="customer-done">
          {done}
        </p>
      )}
    </>
  );

  if (erased) {
    return (
      <div className="page-pad">
        <PageHeader title="Customer erased" />
        <p className="ok-note" data-testid="customer-erased">
          Erased. The profile and consents are gone, here and from every till at its next sync.
          Bills stay, as the tax law needs.
        </p>
        <Link className="btn" to="/customers" data-testid="customer-back">
          Back to customers
        </Link>
      </div>
    );
  }

  if (!customer) {
    return (
      <div className="page-pad">
        <PageHeader title="Customer" />
        {offlineNote}
        {missing ? (
          <p className="warn-note" data-testid="customer-missing">
            No customer like that at your stores. <Link to="/customers">Back to customers</Link>
          </p>
        ) : error ? (
          <p className="warn-note" data-testid="customer-error">
            {error}
          </p>
        ) : (
          !offline && <p>Loading customer…</p>
        )}
      </div>
    );
  }

  return (
    <div className="page-pad">
      <PageHeader
        title={customer.name || "No name held"}
        lead={`${customer.mobile}${customer.gstin ? ` · GSTIN ${customer.gstin}` : ""}`}
      />
      <p>
        <Link to="/customers" data-testid="customer-back">
          ← Customers
        </Link>
      </p>
      {offlineNote}
      {notes}

      <section className="card section-card" data-testid="customer-profile">
        <h2 className="h3">Held about this customer</h2>
        <dl className="customer-facts">
          <dt>Name</dt>
          <dd data-testid="customer-name">{customer.name || "No name held"}</dd>
          <dt>Number</dt>
          <dd className="mono" data-testid="customer-mobile">
            {customer.mobile}
          </dd>
          <dt>GSTIN</dt>
          <dd className="mono" data-testid="customer-gstin">
            {customer.gstin || "None"}
          </dd>
          {customer.numbers.length > 0 && (
            <>
              <dt>Other numbers</dt>
              <dd data-testid="customer-numbers">
                {customer.numbers.map((n) => (
                  <div key={`${n.reason}-${n.mobile}`} data-testid={`customer-number-${n.mobile}`}>
                    <span className="mono">{n.mobile}</span>{" "}
                    <span className="muted-cell">
                      {otherNumberNote(n.reason, n.until ?? null, formatDateTime)}
                    </span>
                  </div>
                ))}
              </dd>
            </>
          )}
        </dl>
      </section>

      <section className="card section-card" data-testid="customer-consents">
        <h2 className="h3">Consents</h2>
        <table className="data">
          <tbody>
            {CONSENT_QUESTIONS.map(({ key, label }) => {
              const standing = customer.consent[key];
              const chip = consentChip(standing);
              return (
                <tr key={key} data-testid={`consent-${key}`}>
                  <td>{label}</td>
                  <td>
                    <span className={`chip ${chip.chip}`} data-testid={`consent-${key}-state`}>
                      {chip.label}
                    </span>
                    {standing && (
                      <span className="muted-cell">
                        {" "}
                        {answeredHow(standing.how)}, {formatDateTime(standing.answered_at)}
                      </span>
                    )}
                  </td>
                  <td>
                    {customer.can_act && standing?.given && (
                      <button
                        type="button"
                        className="btn"
                        disabled={!writable}
                        data-testid={`consent-${key}-withdraw`}
                        onClick={() => void withdraw(key)}
                      >
                        Withdraw
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="card section-card" data-testid="customer-bills">
        <h2 className="h3">Bills</h2>
        <p className="muted-cell">
          {customer.bills_total === 1 ? "1 bill" : `${customer.bills_total} bills`} at your stores.
          {customer.bills_total > customer.bills.length
            ? ` The newest ${customer.bills.length} are listed.`
            : ""}{" "}
          A bill is a tax invoice: it keeps the name and GSTIN printed on it.
        </p>
        {customer.bills.length > 0 && (
          <div className="table-wrap">
            <table className="data" data-testid="customer-bill-rows">
              <thead>
                <tr>
                  <th>Bill</th>
                  <th>When</th>
                  <th>Store</th>
                  <th className="num">Total</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {customer.bills.map((bill) => (
                  <tr key={bill.id} data-testid={`customer-bill-${bill.id}`}>
                    <td className="mono">{bill.doc_number || "not numbered"}</td>
                    <td>{formatDateTime(bill.billed_at)}</td>
                    <td>{bill.store_code}</td>
                    <td className="num">
                      <Money paise={bill.net_paise} />
                    </td>
                    <td>
                      {bill.exchange && <span className="chip chip-navy">Exchange</span>}
                      {bill.cancelled && <span className="chip chip-red">Cancelled</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card section-card" data-testid="customer-rights">
        <h2 className="h3">The customer's rights</h2>
        {!customer.can_act ? (
          <p className="muted-cell" data-testid="customer-read-only">
            You can read this page. Store staff act on a customer's request.
          </p>
        ) : (
          <>
            <p className="muted-cell">
              Only at the customer's own request, with the customer present. Each action is
              recorded.
            </p>
            {customer.stores.length > 1 && (
              <label className="field">
                <span>The customer is at</span>
                <select
                  className="input"
                  data-testid="customer-store"
                  value={siteId ?? ""}
                  onChange={(e) => setSiteId(Number(e.target.value))}
                >
                  {customer.stores.map((store) => (
                    <option key={store.id} value={store.id}>
                      {store.code} · {store.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <div className="toolbar">
              <button
                type="button"
                className="btn"
                disabled={!writable}
                data-testid="rights-show"
                onClick={() => void show()}
              >
                Show what is held
              </button>
              <button
                type="button"
                className="btn"
                disabled={!writable || draft !== null}
                data-testid="rights-correct"
                onClick={() => {
                  const now = { name: customer.name, gstin: customer.gstin };
                  setDraft(now);
                  setWas(now);
                }}
              >
                Correct
              </button>
              {customer.can_merge_or_move && (
                <>
                  <button
                    type="button"
                    className="btn"
                    disabled={!writable || merging}
                    data-testid="rights-merge"
                    onClick={() => setMerging(true)}
                  >
                    Merge another record
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={!writable || moving}
                    data-testid="rights-move"
                    onClick={() => setMoving(true)}
                  >
                    New number
                  </button>
                </>
              )}
              <button
                type="button"
                className="btn btn-danger"
                disabled={!writable || confirming}
                data-testid="rights-erase"
                onClick={() => setConfirming(true)}
              >
                Erase
              </button>
            </div>

            {draft && (
              <div className="card section-card" data-testid="rights-correct-form">
                <label className="field">
                  <span>Name</span>
                  <input
                    className="input"
                    maxLength={120}
                    data-testid="rights-correct-name"
                    value={draft.name}
                    onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  />
                </label>
                <label className="field">
                  <span>GSTIN (leave blank for none)</span>
                  <input
                    className="input mono"
                    maxLength={15}
                    data-testid="rights-correct-gstin"
                    value={draft.gstin}
                    onChange={(e) => setDraft({ ...draft, gstin: e.target.value })}
                  />
                </label>
                <p className="muted-cell">
                  A new number is not a correction: it moves the record (a separate step).
                </p>
                <div className="toolbar">
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={!writable}
                    data-testid="rights-correct-save"
                    onClick={() => void correct()}
                  >
                    Save correction
                  </button>
                  <button
                    type="button"
                    className="btn"
                    data-testid="rights-correct-cancel"
                    onClick={() => {
                      setDraft(null);
                      setWas(null);
                    }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {merging && (
              <div className="card section-card" data-testid="rights-merge-form">
                <p className="muted-cell">
                  Two records for the same person. This record is kept; the other one is closed,
                  never deleted, and its bills become this customer's. Only with the customer here.
                </p>
                <label className="field">
                  <span>Find the other record by number or name</span>
                  <input
                    className="input"
                    data-testid="rights-merge-search"
                    value={mergeTerm}
                    onChange={(e) => setMergeTerm(e.target.value)}
                  />
                </label>
                <div className="toolbar">
                  <button
                    type="button"
                    className="btn"
                    disabled={offline || !mergeTerm.trim()}
                    data-testid="rights-merge-find"
                    onClick={() => void findOther()}
                  >
                    Find
                  </button>
                </div>
                {mergeFound !== null && mergeFound.length === 0 && (
                  <p className="muted-cell" data-testid="rights-merge-none">
                    No other record like that at your stores.
                  </p>
                )}
                {mergeFound !== null && mergeFound.length > 0 && (
                  <table className="data" data-testid="rights-merge-found">
                    <tbody>
                      {mergeFound.map((row) => (
                        <tr key={row.id}>
                          <td className="mono">{row.mobile}</td>
                          <td>{row.name || "No name held"}</td>
                          <td>
                            <button
                              type="button"
                              className={`btn${mergePick?.id === row.id ? " btn-primary" : ""}`}
                              data-testid={`rights-merge-pick-${row.mobile}`}
                              onClick={() => {
                                setMergePick(row);
                                setMergeDigits("");
                              }}
                            >
                              {mergePick?.id === row.id ? "Picked" : "Pick"}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {mergePick && (
                  <>
                    <label className="field">
                      <span>
                        Type the last four digits of the other record's number ({mergePick.mobile})
                        to confirm
                      </span>
                      <input
                        className="input mono"
                        inputMode="numeric"
                        maxLength={4}
                        data-testid="rights-merge-digits"
                        value={mergeDigits}
                        onChange={(e) => setMergeDigits(e.target.value)}
                      />
                    </label>
                    <label className="check-row">
                      <input
                        type="checkbox"
                        data-testid="rights-merge-present"
                        checked={mergePresent}
                        onChange={(e) => setMergePresent(e.target.checked)}
                      />{" "}
                      The customer is here and asked for this
                    </label>
                  </>
                )}
                <div className="toolbar">
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={
                      !writable ||
                      !mergePick ||
                      !mergePresent ||
                      !lastFourMatches(mergePick.mobile, mergeDigits)
                    }
                    data-testid="rights-merge-confirm"
                    onClick={() => void merge()}
                  >
                    Merge into this record
                  </button>
                  <button
                    type="button"
                    className="btn"
                    data-testid="rights-merge-cancel"
                    onClick={closeMerge}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {moving && (
              <div className="card section-card" data-testid="rights-move-form">
                <p className="muted-cell">
                  The customer has a new number. The record moves to it; bills on the old number stay
                  theirs. The old number's consents are withdrawn, and the customer answers again on
                  the display. Only with the customer here.
                </p>
                <label className="field">
                  <span>New number</span>
                  <input
                    className="input mono"
                    inputMode="tel"
                    maxLength={16}
                    data-testid="rights-move-number"
                    value={newNumber}
                    onChange={(e) => setNewNumber(e.target.value)}
                  />
                </label>
                <label className="check-row">
                  <input
                    type="checkbox"
                    data-testid="rights-move-present"
                    checked={movePresent}
                    onChange={(e) => setMovePresent(e.target.checked)}
                  />{" "}
                  The customer is here and asked for this
                </label>
                <div className="toolbar">
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={!writable || !movePresent || !newNumberOk(newNumber, customer.mobile)}
                    data-testid="rights-move-save"
                    onClick={() => void move()}
                  >
                    Move to the new number
                  </button>
                  <button
                    type="button"
                    className="btn"
                    data-testid="rights-move-cancel"
                    onClick={closeMove}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {confirming && (
              <div className="card section-card" data-testid="rights-erase-form">
                <p className="warn-note">
                  Erasing removes this customer's profile and both consents, here and from every
                  till. It cannot be undone. Bills stay: the name, number and GSTIN printed on a tax
                  invoice are kept with it, as the law needs.
                </p>
                <label className="field">
                  <span>Type the last four digits of the customer's number to confirm</span>
                  <input
                    className="input mono"
                    inputMode="numeric"
                    maxLength={4}
                    data-testid="rights-erase-digits"
                    value={lastFour}
                    onChange={(e) => setLastFour(e.target.value)}
                  />
                </label>
                <div className="toolbar">
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={!writable || !lastFourMatches(customer.mobile, lastFour)}
                    data-testid="rights-erase-confirm"
                    onClick={() => void erase()}
                  >
                    Erase this customer
                  </button>
                  <button
                    type="button"
                    className="btn"
                    data-testid="rights-erase-cancel"
                    onClick={() => {
                      setConfirming(false);
                      setLastFour("");
                    }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </section>

      {held && <HeldPanel held={held} />}
    </div>
  );
}

/** Everything held, as shown to the customer. Read-only. */
function HeldPanel({ held }: { held: Held }) {
  return (
    <section className="card section-card" data-testid="rights-held">
      <h2 className="h3">Everything held about this customer</h2>
      <dl className="customer-facts">
        <dt>Name</dt>
        <dd>{held.profile.name || "None"}</dd>
        <dt>Number</dt>
        <dd className="mono">{held.profile.mobile}</dd>
        <dt>GSTIN</dt>
        <dd className="mono">{held.profile.gstin || "None"}</dd>
        {held.numbers.length > 0 && (
          <>
            <dt>Other numbers</dt>
            <dd data-testid="rights-held-numbers">
              {held.numbers.map((n) => (
                <div key={`${n.reason}-${n.mobile}`}>
                  <span className="mono">{n.mobile}</span>{" "}
                  {otherNumberNote(n.reason, n.until ?? null, formatDateTime)}
                </div>
              ))}
            </dd>
          </>
        )}
        <dt>First recorded</dt>
        <dd>{formatDateTime(held.profile.first_recorded)}</dd>
        <dt>Bills at your stores</dt>
        <dd data-testid="rights-held-bills">
          {held.bills.count}
          {held.bills.first_at && held.bills.last_at
            ? ` (${formatDateTime(held.bills.first_at)} to ${formatDateTime(held.bills.last_at)})`
            : ""}
        </dd>
        <dt>Saved sizes</dt>
        <dd data-testid="rights-held-sizes">
          {held.sizes.length === 0
            ? "None recorded"
            : held.sizes.map((row) => (
                <div key={`${row.brand}|${row.category}`}>
                  {row.brand} {row.category}: <strong>{row.size}</strong>{" "}
                  <span className="muted-cell">
                    ({savedSizeNote(row, (iso) => `on ${formatDateTime(iso)}`)})
                  </span>
                </div>
              ))}
        </dd>
        <dt>Messages sent</dt>
        <dd>None recorded</dd>
      </dl>
      <h3 className="h3">Consent answers</h3>
      {held.consent_answers.length === 0 ? (
        <p className="muted-cell">None given.</p>
      ) : (
        <table className="data" data-testid="rights-held-answers">
          <thead>
            <tr>
              <th>Question</th>
              <th>Answer</th>
              <th>Where</th>
              <th>When</th>
              <th>Wording</th>
            </tr>
          </thead>
          <tbody>
            {held.consent_answers.map((answer, index) => (
              <tr key={`${answer.answered_at}-${index}`}>
                <td>{answer.question === "bill" ? "Send my bill" : "Send me offers"}</td>
                <td>{answer.given ? "Yes" : "No"}</td>
                <td>
                  {answer.store_code}, {answeredHow(answer.how)}
                </td>
                <td>{formatDateTime(answer.answered_at)}</td>
                <td>v{answer.wording_version}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
