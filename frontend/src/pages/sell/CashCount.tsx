import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { RefreshCw } from "lucide-react";

import { Note } from "../../components/Note";
import { OperationsPage } from "../../components/OperationsPage";
import { PageHeader } from "../../components/PageHeader";
import { Section } from "../../components/Section";
import { SegmentedControl } from "../../components/SegmentedControl";
import { Status } from "../../components/Status";
import { useAuth } from "../../auth/AuthContext";
import { api, apiErrorCode, apiErrorMessage } from "../../lib/api";
import type { ApiSchemas } from "../../lib/api";
import {
  countedPaise,
  emptyNotes,
  expectedPaise,
  pieces,
  varianceWords,
  whyCannotSave,
} from "../../lib/cashCount";
import { Money, formatDateTime } from "../../lib/format";
import { CASH_VARIANCE } from "../../till/pin";
import type { Authorisation } from "../../till/pin";
import { useTill } from "../../till/TillProvider";
import type { TillManager } from "../../till/types";
import { newUuid } from "../../till/uuid";
import { ManagerPin, useWrongPins } from "./ManagerPin";
import { RupeeInput } from "./billing/RupeeInput";
import "./Till.css";
import "./CashCount.css";

// ---------------------------------------------------------------------------
// Cash Count - the day-close Z-report count by note and coin (ticket 41, ST-MNY-2)
// ---------------------------------------------------------------------------
//
// The cashier counts the drawer note by note against the cash the server
// expects: opening + cash sales - cash refunds - deposits and handovers - petty
// cash spent + petty cash brought from head office. The drawer and the petty
// cash box are counted together (ticket 42, baseline B125). A difference needs
// a manager's own PIN, typed here and checked on
// this device (ticket 06), and the server keeps it as an owned exception - never
// a balancing entry.
//
// Online only: the count and a deposit are saved straight to head office, never
// queued, because the expected cash is only true once the server has every
// bill. Offline, or with bills still waiting to send, the Save button explains
// why it waits, and what was typed stays on the screen (and in this tab's
// session storage, so a reload keeps it too). A request that dropped before the
// answer came back is sent again under the same id, so it can never save twice.

type Position = ApiSchemas["CashPosition"];
type CountRead = ApiSchemas["CashCountRead"];

interface Draft {
  notes: Record<string, string>;
  coins: number;
  opening: number | null;
}

const EMPTY_DRAFT: Draft = { notes: {}, coins: 0, opening: null };

function draftKey(scope: string | null, day: string, userId: number | null): string | null {
  return scope && userId !== null ? `kdps-cash-count:v2:${scope}:${day}:${userId}` : null;
}

function readDraft(key: string | null): Draft {
  if (!key) return EMPTY_DRAFT;
  try {
    const raw = sessionStorage.getItem(key);
    return raw ? { ...EMPTY_DRAFT, ...(JSON.parse(raw) as Draft) } : EMPTY_DRAFT;
  } catch {
    return EMPTY_DRAFT;
  }
}

function writeDraft(key: string | null, draft: Draft | null): void {
  if (!key) return;
  try {
    if (draft) sessionStorage.setItem(key, JSON.stringify(draft));
    else sessionStorage.removeItem(key);
  } catch {
    // Storage refused (a private window): the count still lives on the screen.
  }
}

/** A refusal with no answer at all is the connection, not the server. */
function droppedConnection(e: unknown): boolean {
  return !(e as { response?: unknown })?.response;
}

export default function CashCountPage() {
  const { engine, till } = useTill();
  const { user } = useAuth();
  const [position, setPosition] = useState<Position | null>(null);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await api.get<Position>("/sell/cash-count");
      setPosition(response.data);
      setLoadError("");
    } catch (e) {
      setLoadError(
        droppedConnection(e)
          ? "The expected cash cannot be read while the connection is down. It reads again when you are back online."
          : apiErrorMessage(e),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  const online = till?.online ?? navigator.onLine;
  useEffect(() => {
    if (online) void load();
  }, [load, online]);

  return (
    <OperationsPage>
      <PageHeader
        lead="Count the drawer by note and coin at day close, against the cash the system expects."
        actions={
          <button
            type="button"
            className="btn"
            data-testid="cash-reload"
            disabled={loading || !online}
            onClick={() => void load()}
          >
            <RefreshCw size={15} className={loading ? "till-spin" : ""} />
            Read again
          </button>
        }
      />

      <div className="cash-body">
        {!online && (
          <Note tone="warn" testId="cash-offline">
            You are offline. The count and deposits are saved straight to head office, so they wait
            until the connection is back. What you have typed stays here.
          </Note>
        )}
        {loadError && (
          <Note tone="error" testId="cash-load-error">
            {loadError}
          </Note>
        )}

        {position && (
          <>
            {!position.switched_on && (
              <Note tone="warn" testId="cash-switched-off">
                Cash count is switched off for this store. Counts already saved are shown below;
                nothing new can be counted or recorded.
              </Note>
            )}
            {!position.switched_on ? null : position.counted ? (
              <>
                <SavedCount count={position.counted} />
                {(position.bills > 0 ||
                  Number(position.movements_paise) !== 0 ||
                  Number(position.petty_cash_paise) !== 0 ||
                  Number(position.petty_top_ups_paise) !== 0) && (
                  <Note tone="warn" role="status" testId="cash-after-close">
                    New activity arrived after this saved count: {position.bills} uncounted bill(s).
                    It belongs to the next count; the saved count and its evidence stay unchanged.
                    The drawer expected for the next count is{" "}
                    <Money paise={expectedPaise(position, null) ?? 0} />. Today’s saved count does
                    not settle this later activity.
                  </Note>
                )}
              </>
            ) : (
              <CountForm
                position={position}
                pending={till?.pending ?? 0}
                online={online}
                hasCounter={Boolean(engine && till)}
                tillNumber={till?.device?.identity.counter_id ?? ""}
                cashierId={user?.id ?? null}
                onSaved={() => void load()}
                onStale={() => void load()}
              />
            )}
            <div className="cash-lower">
              {position.switched_on && (
                <Movements position={position} online={online} onSaved={() => void load()} />
              )}
              <RecentCounts counts={position.recent} />
            </div>
          </>
        )}
      </div>
    </OperationsPage>
  );
}

// --- the count --------------------------------------------------------------------

function CountForm({
  position,
  pending,
  online,
  hasCounter,
  tillNumber,
  cashierId,
  onSaved,
  onStale,
}: {
  position: Position;
  pending: number;
  online: boolean;
  hasCounter: boolean;
  tillNumber: string;
  cashierId: number | null;
  onSaved: () => void;
  onStale: () => void;
}) {
  const { engine, till } = useTill();
  const denominations = position.denominations;
  // The engine opens only a server-derived tenant/site/device namespace.
  // Preserve unidentified old drafts without reading or copying them here.
  const key = draftKey(engine?.db.name ?? null, position.business_day, cashierId);
  const [draft, setDraft] = useState<Draft>(() => readDraft(key));
  const [draftFor, setDraftFor] = useState(key);
  if (draftFor !== key) {
    // A new day, or another person at the counter: their own draft, not this one.
    setDraftFor(key);
    setDraft(readDraft(key));
  }
  const [managers, setManagers] = useState<TillManager[]>([]);
  const [asking, setAsking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const countId = useRef(newUuid());
  const pins = useWrongPins();

  // The managers this counter could check a PIN for, from its own database, so
  // the PIN works exactly as it does on a bill (ticket 06).
  useEffect(() => {
    if (!engine) return;
    void engine.db.managers.toArray().then(setManagers);
  }, [engine]);

  useEffect(() => {
    if (draftFor === key) writeDraft(key, draft);
  }, [key, draftFor, draft]);

  const notes = useMemo(() => {
    const out = emptyNotes(denominations);
    for (const face of denominations) out[String(face)] = pieces(draft.notes[face] ?? "") ?? 0;
    return out;
  }, [denominations, draft.notes]);
  const badNote = denominations.some((face) => pieces(draft.notes[face] ?? "") === null);
  const counted = countedPaise(denominations, notes, draft.coins);
  const expected = expectedPaise(position, draft.opening);
  const variance = expected === null ? null : counted - expected;
  const blocked = whyCannotSave({ online, pending, hasCounter });

  async function submit(authorisation: Authorisation | null, pin = "") {
    if (expected === null) return;
    setSaving(true);
    setMessage("");
    try {
      await api.post("/sell/cash-counts", {
        id: countId.current,
        notes,
        coins_paise: draft.coins,
        opening_paise: position.opening_declared ? draft.opening : null,
        expected_paise: expected,
        approved_by: authorisation?.user_id ?? null,
        // Checked again by the server, because this is online; never kept.
        manager_pin: pin,
        till_number: tillNumber,
      });
      writeDraft(key, null);
      countId.current = newUuid();
      onSaved();
    } catch (e) {
      const code = apiErrorCode(e);
      if (droppedConnection(e)) {
        // Kept: the same id goes again, so a count that did land is not saved twice.
        setMessage(
          "Not saved: the connection dropped. Your count is still here. Press Save again when you are back online.",
        );
      } else {
        countId.current = newUuid();
        setMessage(apiErrorMessage(e));
        if (code === "ALREADY_COUNTED") writeDraft(key, null);
        if (code === "CASH_CHANGED" || code === "ALREADY_COUNTED") onStale();
      }
    } finally {
      setSaving(false);
    }
  }

  function save() {
    if (variance === null || badNote) return;
    if (variance !== 0) {
      if (till?.onlineAlpha) {
        setMessage(
          "This variance needs a recorded independent approval. Your count draft is retained; arrange an authorised review before closing it.",
        );
        return;
      }
      setAsking(true);
      return;
    }
    void submit(null);
  }

  if (!key || !hasCounter) {
    return (
      <Note tone="warn" role="status" testId="cash-counter-required">
        Waiting for this store's authorised counter. Open Till &amp; Sync to pair it before entering
        a cash count.
      </Note>
    );
  }

  return (
    <Section
      heading={`Today's count · ${position.business_day}`}
      className="cash-count"
      testId="cash-count-form"
    >
      <div className="cash-grid">
        <Section heading="Expected cash" level={3}>
          <div className="cash-rows">
            <Row
              label={
                position.opening_declared
                  ? "Opening float (first count at this store)"
                  : `Opening (counted ${position.previous?.business_day ?? ""})`
              }
            >
              {position.opening_declared ? (
                <span className="cash-money">
                  <RupeeInput
                    testId="cash-opening"
                    label="Opening float"
                    paise={draft.opening ?? 0}
                    locked={saving}
                    placeholder="0"
                    onChange={(paise) => setDraft((d) => ({ ...d, opening: paise }))}
                  />
                </span>
              ) : (
                <Money paise={position.opening_paise ?? 0} />
              )}
            </Row>
            <Row label={`Cash sales (${position.bills} bills)`} testId="cash-sales">
              + <Money paise={position.cash_sales_paise} />
            </Row>
            <Row label="Cash refunds">
              − <Money paise={position.cash_refunds_paise} />
            </Row>
            <Row label="Deposits and handovers" testId="cash-movements-total">
              − <Money paise={position.movements_paise} />
            </Row>
            <Row label="Petty cash spent" testId="cash-petty-spent">
              − <Money paise={position.petty_cash_paise} />
            </Row>
            {position.petty_top_ups_paise > 0 && (
              <Row label="Petty cash from head office" testId="cash-petty-brought">
                + <Money paise={position.petty_top_ups_paise} />
              </Row>
            )}
            <Row label="Expected in the drawer and petty cash box" testId="cash-expected" strong>
              {expected === null ? "Type the opening float" : <Money paise={expected} />}
            </Row>
          </div>
          <p className="cash-text">
            Count the petty cash box together with the drawer. Since{" "}
            {formatDateTime(position.window_from)}. Card <Money paise={position.tenders.card} /> and
            UPI <Money paise={position.tenders.upi} /> are not in the drawer.
          </p>
        </Section>

        <Section heading="Counted, by note and coin" level={3}>
          <div className="cash-rows">
            {denominations.map((face) => {
              const text = draft.notes[face] ?? "";
              const n = pieces(text);
              return (
                <div className="cash-note-row" key={face}>
                  <label htmlFor={`cash-note-${face}`}>₹{face} ×</label>
                  <input
                    id={`cash-note-${face}`}
                    className="input mono cash-pieces"
                    inputMode="numeric"
                    data-testid={`cash-note-${face}`}
                    disabled={saving}
                    value={text}
                    placeholder="0"
                    onChange={(e) =>
                      setDraft((d) => ({ ...d, notes: { ...d.notes, [face]: e.target.value } }))
                    }
                  />
                  <span className="cash-line">
                    {n === null ? "whole notes only" : <Money paise={n * face * 100} />}
                  </span>
                </div>
              );
            })}
            <div className="cash-note-row">
              <label htmlFor="cash-coins">Coins ₹</label>
              <RupeeInput
                testId="cash-coins"
                label="Coins"
                paise={draft.coins}
                locked={saving}
                placeholder="0"
                onChange={(paise) => setDraft((d) => ({ ...d, coins: paise ?? 0 }))}
              />
              <span className="cash-line">
                <Money paise={draft.coins} />
              </span>
            </div>
            <Row label="Counted" testId="cash-counted" strong>
              <Money paise={counted} />
            </Row>
          </div>
        </Section>

        <Section heading="Difference" level={3} className="cash-difference">
          {variance === null ? (
            <p className="cash-text">Type the opening float to see the difference.</p>
          ) : (
            <>
              <span className={`cash-variance-figure${variance === 0 ? " cash-variance-nil" : ""}`}>
                {variance > 0 ? "+" : variance < 0 ? "−" : ""}
                <Money paise={Math.abs(variance)} />
              </span>
              <Status tone={variance === 0 ? "green" : "amber"}>
                {variance === 0 ? "Cash matches" : variance < 0 ? "Short" : "Over"}
              </Status>
              <p className="cash-text" data-testid="cash-variance" data-variance={variance}>
                {variance === 0 ? (
                  <>The count matches the expected cash.</>
                ) : (
                  <>
                    Cash {varianceWords(variance)} by <Money paise={Math.abs(variance)} />.
                    {till?.onlineAlpha
                      ? " Keep this draft for recorded independent approval before closing it."
                      : " A manager of this store confirms it with their own PIN, and it goes to the store manager as an exception to explain."}{" "}
                    Nothing is booked to balance it.
                  </>
                )}
              </p>
            </>
          )}

          {blocked && (
            <Note tone="warn" testId="cash-blocked">
              {blocked}
            </Note>
          )}
          {message && (
            <Note tone="error" testId="cash-save-error">
              {message}
            </Note>
          )}
          <button
            type="button"
            className="btn btn-cta btn-lg btn-block"
            data-testid="cash-save"
            disabled={Boolean(blocked) || saving || variance === null || badNote}
            onClick={save}
          >
            {saving
              ? "Saving…"
              : variance
                ? till?.onlineAlpha
                  ? "Arrange independent review"
                  : "Confirm with manager PIN and save"
                : "Save the count"}
          </button>
        </Section>
      </div>

      {asking && variance !== null && (
        <ManagerPin
          managers={managers}
          asks={[
            {
              kind: CASH_VARIANCE,
              ref: position.business_day,
              paise: Math.abs(variance),
              label: `Cash ${varianceWords(variance)}, ${position.business_day}`,
            },
          ]}
          cashierId={cashierId}
          lead="This count needs a manager to confirm it:"
          footnote="The manager types it themselves. Their name and the time are saved with the count."
          wrong={pins.wrong}
          onWrong={pins.wasWrong}
          onClose={() => setAsking(false)}
          onAuthorised={(authorisation, pin) => {
            pins.clear();
            setAsking(false);
            void submit(authorisation, pin);
          }}
        />
      )}
    </Section>
  );
}

function SavedCount({ count }: { count: CountRead }) {
  return (
    <Section
      heading={`Today's cash is counted · ${count.business_day}`}
      aside={<Status tone="green">Counted</Status>}
      testId="cash-counted-today"
    >
      <div className="cash-rows cash-saved">
        <Row label="Expected">
          <Money paise={count.expected_paise} />
        </Row>
        <Row label="Counted">
          <Money paise={count.counted_paise} />
        </Row>
        <Row label="Difference" testId="cash-saved-variance" strong>
          {count.variance_paise === 0 ? (
            "None"
          ) : (
            <>
              {varianceWords(count.variance_paise)} <Money paise={Math.abs(count.variance_paise)} />
            </>
          )}
        </Row>
        <Row label="Counted by">
          {count.counted_by_name} · {formatDateTime(count.counted_at)}
        </Row>
        {count.approved_by !== null && (
          <Row label="Confirmed by" testId="cash-saved-approver">
            {count.approved_by_name}
          </Row>
        )}
      </div>
    </Section>
  );
}

// --- deposits and handovers ----------------------------------------------------------

function Movements({
  position,
  online,
  onSaved,
}: {
  position: Position;
  online: boolean;
  onSaved: () => void;
}) {
  const [kind, setKind] = useState<"deposit" | "handover">("deposit");
  const [amount, setAmount] = useState(0);
  const [receivedBy, setReceivedBy] = useState("");
  const [reference, setReference] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const moveId = useRef(newUuid());
  const deposit = kind === "deposit";

  async function record() {
    setSaving(true);
    setMessage("");
    try {
      await api.post("/sell/cash-movements", {
        id: moveId.current,
        kind,
        amount_paise: amount,
        received_by: receivedBy,
        reference,
      });
      moveId.current = newUuid();
      setAmount(0);
      setReceivedBy("");
      setReference("");
      onSaved();
    } catch (e) {
      if (droppedConnection(e)) {
        setMessage(
          "Not recorded: the connection dropped. Press Record again when you are back online.",
        );
      } else {
        moveId.current = newUuid();
        setMessage(apiErrorMessage(e));
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <Section heading="Cash taken out of the drawer" testId="cash-movements">
      <div className="cash-move-form">
        <SegmentedControl
          label="Kind"
          className="cash-kind"
          value={kind}
          options={[
            { value: "deposit", label: "Bank deposit", testId: "cash-move-deposit" },
            { value: "handover", label: "Handover", testId: "cash-move-handover" },
          ]}
          onChange={setKind}
        />
        <div className="cash-move-fields">
          <div className="field cash-money">
            <label htmlFor="cash-move-amount">Amount ₹</label>
            <RupeeInput
              testId="cash-move-amount"
              label="Amount"
              paise={amount}
              locked={saving}
              placeholder="0"
              onChange={(paise) => setAmount(paise ?? 0)}
            />
          </div>
          <div className="field">
            <label htmlFor="cash-move-to">{deposit ? "Bank" : "Received by"}</label>
            <input
              id="cash-move-to"
              className="input"
              data-testid="cash-move-to"
              value={receivedBy}
              disabled={saving}
              onChange={(e) => setReceivedBy(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="cash-move-ref">
              {deposit ? "Deposit slip number" : "Receipt number"}
            </label>
            <input
              id="cash-move-ref"
              className="input"
              data-testid="cash-move-ref"
              value={reference}
              disabled={saving}
              onChange={(e) => setReference(e.target.value)}
            />
          </div>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="cash-move-record"
            disabled={!online || saving || amount <= 0 || !receivedBy.trim() || !reference.trim()}
            onClick={() => void record()}
          >
            {saving ? "Recording…" : "Record"}
          </button>
        </div>
      </div>
      <p className="cash-text">
        A bank deposit or a handover to head office. Both sides are recorded: who took it out, and
        who received it with the slip or receipt number. It comes off the expected cash of the next
        count.
      </p>
      {message && (
        <Note tone="error" testId="cash-move-error">
          {message}
        </Note>
      )}
      {position.movements.length > 0 && (
        <ul className="cash-move-list" data-testid="cash-move-list">
          {position.movements.map((m) => (
            <li key={m.id}>
              <span>
                <b>{m.kind === "deposit" ? "Deposit" : "Handover"}</b> · {m.given_by_name} →{" "}
                {m.received_by} · {m.reference}
              </span>
              <b>
                <Money paise={m.amount_paise} />
              </b>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

// --- history -------------------------------------------------------------------------

function RecentCounts({ counts }: { counts: CountRead[] }) {
  if (!counts.length) return null;
  return (
    <Section heading="Recent counts" testId="cash-recent">
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Day</th>
              <th className="num">Expected</th>
              <th className="num">Counted</th>
              <th className="num">Difference</th>
              <th>Confirmed by</th>
            </tr>
          </thead>
          <tbody>
            {counts.map((c) => (
              <tr key={c.id}>
                <td>{c.business_day}</td>
                <td className="num">
                  <Money paise={c.expected_paise} />
                </td>
                <td className="num">
                  <Money paise={c.counted_paise} />
                </td>
                <td className="num">
                  {c.variance_paise === 0 ? (
                    "—"
                  ) : (
                    <Status tone="amber">
                      <Money paise={c.variance_paise} />
                    </Status>
                  )}
                </td>
                <td>{c.approved_by_name || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Row({
  label,
  children,
  testId,
  strong,
}: {
  label: string;
  children: ReactNode;
  testId?: string;
  strong?: boolean;
}) {
  return (
    <div className={`till-row${strong ? " cash-strong" : ""}`}>
      <span className="till-row-label">{label}</span>
      <span className="till-row-value" data-testid={testId}>
        {children}
      </span>
    </div>
  );
}
