import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { allowedUnits, useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, apiUrl } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { Money, formatDateTime } from "../lib/format";
import {
  ORIGIN_LABEL,
  OTHER_HEAD,
  STATUS_LABEL,
  chosenStore,
  freePaise,
  needsOwner,
  roomPaise,
  whySpendWaits,
  whyTopUpWaits,
  type PettyPosition,
  type PettySpend,
  type TopUpDraft,
} from "../lib/pettyCash";
import { newUuid } from "../till/uuid";
import { RupeeInput } from "./sell/billing/RupeeInput";
import "./Shared.css";
import "./PettyCash.css";

const API = "/sell/petty-cash";
const STATUS_CHIP: Record<string, string> = {
  spent: "chip-green",
  waiting: "chip-amber",
  approved: "chip-green",
  rejected: "chip-red",
};

/** Money > Petty Cash (store operations PRD ST-MNY-3; ticket 42).
 *
 *  The store's petty cash box is kept at a float by a named custodian; head
 *  office sets both. The store tops it up from the till or with cash head office
 *  brings, and records each spend with an expense head, the amount and a photo
 *  of the bill. A spend with no photo opens an exception until the photo is
 *  added. A spend over the limit waits for the Owner in the approvals inbox.
 *
 *  Online only: everything is saved straight to head office. Offline the page
 *  says so and keeps what was typed; a save whose answer was lost is sent again
 *  under the same id, so it is never saved twice. The server decides who may do
 *  what; this page only offers what it allows. */
export function PettyCashPage() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const { user, activeStore } = useAuth();
  const units = user ? allowedUnits(user) : [];
  const site = chosenStore(Number(params.get("site_id")) || null, activeStore?.id ?? null, units);
  const [position, setPosition] = useState<PettyPosition | null>(null);
  const [opened, setOpened] = useState<PettySpend | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async () => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    try {
      let where = site;
      if (id) {
        const spend = await api.get<PettySpend>(`${API}/spends/${id}`);
        if (mine !== request.current) return;
        setOpened(spend.data);
        where = spend.data.site_id;
      } else {
        setOpened(null);
      }
      if (!where) {
        setPosition(null);
        return;
      }
      const response = await api.get<PettyPosition>(API, {
        params: { site_id: where },
      });
      if (mine !== request.current) return;
      setLost(false);
      setError("");
      setPosition(response.data);
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    }
  }, [id, site]);

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

  /** Send one write; true once the server has it. A dropped connection keeps
   *  the page as it is (and so what was typed, and its id), and says so. */
  async function send(path: string, body: object | FormData, saved: string): Promise<boolean> {
    if (!navigator.onLine) {
      setOnline(false);
      return false;
    }
    setError("");
    setDone("");
    setBusy(true);
    let ok = false;
    try {
      await api.post(path, body);
      setDone(saved);
      setLost(false);
      ok = true;
    } catch (reason) {
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
    if (ok) await load();
    return ok;
  }

  // A dropped answer does not lock the page: saving again is how it recovers.
  const live = online;
  return (
    <div className="page-pad">
      <PageHeader lead="The store's petty cash box: who holds it, what went in, and every spend with its bill." />
      {!online && (
        <p className="warn-note" data-testid="petty-offline">
          You are offline. Petty cash is saved straight to head office, so nothing can be saved
          until the connection is back. What you have typed stays here.
        </p>
      )}
      {online && lost && (
        <p className="warn-note" data-testid="petty-lost">
          The connection dropped before head office answered. What you typed is still here; press
          Save again and it is saved once.
        </p>
      )}
      {error && (
        <p className="till-alert" data-testid="petty-error">
          {error}
        </p>
      )}
      {done && (
        <p className="ok-note" data-testid="petty-done">
          {done}
        </p>
      )}

      {!id && units.length > 1 && (
        <label className="field petty-store">
          <span>Store</span>
          <select
            className="select"
            data-testid="petty-store"
            value={site ?? ""}
            onChange={(e) => setParams(e.target.value ? { site_id: e.target.value } : {})}
          >
            <option value="">Choose a store</option>
            {units.map((unit) => (
              <option key={unit.id} value={unit.id}>
                {unit.name} ({unit.code})
              </option>
            ))}
          </select>
        </label>
      )}

      {opened && <OpenedSpend spend={opened} />}

      {!position ? (
        <p className="muted-cell">{site || id ? (online ? "Loading…" : "") : "Choose a store."}</p>
      ) : (
        <>
          {!position.switched_on && (
            <p className="warn-note" data-testid="petty-switched-off">
              Petty cash is switched off for {position.store_name}. What is saved is shown below;
              nothing new can be recorded.
            </p>
          )}
          <BoxCard position={position} live={live} busy={busy} send={send} />
          {position.switched_on && position.float && (
            <div className="petty-grid">
              <SpendForm position={position} online={live} busy={busy} send={send} />
              <TopUpForm position={position} online={live} busy={busy} send={send} />
            </div>
          )}
          <SpendList position={position} live={live} busy={busy} send={send} />
          <TopUpList position={position} />
        </>
      )}
    </div>
  );
}

type Send = (path: string, body: object | FormData, saved: string) => Promise<boolean>;

function Row({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div className="petty-row">
      <span className="petty-row-label">{label}</span>
      <span className="petty-row-value" data-testid={testId}>
        {children}
      </span>
    </div>
  );
}

// --- the box ---------------------------------------------------------------------

function BoxCard({
  position,
  live,
  busy,
  send,
}: {
  position: PettyPosition;
  live: boolean;
  busy: boolean;
  send: Send;
}) {
  const box = position.float;
  const [floatPaise, setFloatPaise] = useState<number>(box?.float_paise ?? 0);
  const [custodian, setCustodian] = useState<string>(box ? String(box.custodian) : "");
  const change = useRef(newUuid());

  async function save() {
    const ok = await send(
      `${API}/float`,
      {
        id: change.current,
        site_id: position.site_id,
        float_paise: floatPaise,
        custodian: Number(custodian),
        expected_revision: box?.revision ?? 0,
      },
      "The float and custodian are saved.",
    );
    if (ok) change.current = newUuid();
  }

  return (
    <section className="card section-card" data-testid="petty-box">
      <h3 className="section-title">
        Petty cash box · {position.store_name} ({position.store})
      </h3>
      {box ? (
        <>
          <Row label="Float" testId="petty-float">
            <Money paise={box.float_paise} />
          </Row>
          <Row label="Custodian" testId="petty-custodian">
            {box.custodian_name}
          </Row>
          <Row label="In the box now" testId="petty-balance">
            <Money paise={position.balance_paise} />
          </Row>
          {position.waiting_paise > 0 && (
            <Row label="Waiting for the Owner" testId="petty-waiting">
              <Money paise={position.waiting_paise} />
            </Row>
          )}
          {position.missing_bills > 0 && (
            <p className="warn-note" data-testid="petty-missing-bills">
              {position.missing_bills === 1
                ? "1 spend has no bill photo yet. Add it below."
                : `${position.missing_bills} spends have no bill photo yet. Add them below.`}
            </p>
          )}
        </>
      ) : (
        <p className="muted-cell" data-testid="petty-not-set">
          Head office has not set this store's petty cash float and custodian yet.
        </p>
      )}

      {position.may_set_float && position.switched_on && (
        <div className="petty-float-form" data-testid="petty-float-form">
          <label className="field">
            <span>Float</span>
            <RupeeInput
              testId="petty-float-amount"
              label="Float"
              paise={floatPaise}
              locked={busy}
              placeholder="0"
              onChange={(paise) => setFloatPaise(paise ?? 0)}
            />
          </label>
          <label className="field">
            <span>Custodian</span>
            <select
              className="select"
              data-testid="petty-float-custodian"
              value={custodian}
              disabled={busy}
              onChange={(e) => setCustodian(e.target.value)}
            >
              <option value="">Choose who holds the box</option>
              {position.custodians.map((person) => (
                <option key={person.id} value={person.id}>
                  {person.name}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="btn"
            data-testid="petty-float-save"
            disabled={!live || busy || floatPaise <= 0 || !custodian}
            onClick={() => void save()}
          >
            {box ? "Change the float" : "Set the float"}
          </button>
        </div>
      )}
    </section>
  );
}

// --- a spend ---------------------------------------------------------------------

function SpendForm({
  position,
  online,
  busy,
  send,
}: {
  position: PettyPosition;
  online: boolean;
  busy: boolean;
  send: Send;
}) {
  const [head, setHead] = useState("");
  const [amountPaise, setAmountPaise] = useState(0);
  const [note, setNote] = useState("");
  const [bill, setBill] = useState<File | null>(null);
  const [fileKey, setFileKey] = useState(0);
  const spendId = useRef(newUuid());
  const blocked = whySpendWaits({ head, amountPaise, note }, position, online);
  const owner = needsOwner(amountPaise, position.approval_above_paise);

  async function save() {
    const form = new FormData();
    form.append("id", spendId.current);
    form.append("site_id", String(position.site_id));
    form.append("head", head);
    form.append("amount_paise", String(amountPaise));
    form.append("note", note);
    if (bill) form.append("bill", bill);
    const ok = await send(
      `${API}/spends`,
      form,
      owner ? "Saved. It waits for the Owner in the approvals inbox." : "The spend is saved.",
    );
    if (ok) {
      spendId.current = newUuid();
      setHead("");
      setAmountPaise(0);
      setNote("");
      setBill(null);
      setFileKey((k) => k + 1);
    }
  }

  return (
    <section className="card section-card" data-testid="petty-spend-form">
      <h3 className="section-title">Record a spend</h3>
      <label className="field">
        <span>Expense head</span>
        <select
          className="select"
          data-testid="petty-spend-head"
          value={head}
          disabled={busy}
          onChange={(e) => setHead(e.target.value)}
        >
          <option value="">Choose a head</option>
          {position.heads.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Amount</span>
        <RupeeInput
          testId="petty-spend-amount"
          label="Amount"
          paise={amountPaise}
          locked={busy}
          placeholder="0"
          onChange={(paise) => setAmountPaise(paise ?? 0)}
        />
      </label>
      <label className="field">
        <span>What it was for{head === OTHER_HEAD ? "" : " (optional)"}</span>
        <input
          className="input"
          data-testid="petty-spend-note"
          maxLength={200}
          value={note}
          disabled={busy}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <label className="field">
        <span>Bill photo</span>
        <input
          key={fileKey}
          type="file"
          accept="image/jpeg,image/png,application/pdf"
          capture="environment"
          data-testid="petty-spend-bill"
          disabled={busy}
          onChange={(e) => setBill(e.target.files?.[0] ?? null)}
        />
      </label>
      <p className="muted-cell">
        The box can pay <Money paise={freePaise(position)} />.
      </p>
      {owner && (
        <p className="warn-note" data-testid="petty-spend-owner">
          Over <Money paise={position.approval_above_paise} />: this waits for the Owner's approval
          in the approvals inbox. Pay it only once it is approved.
        </p>
      )}
      {!bill && amountPaise > 0 && (
        <p className="warn-note" data-testid="petty-spend-no-bill">
          No bill photo: it can be saved, but it stays an open exception for the store manager until
          the photo is added.
        </p>
      )}
      {blocked && amountPaise > 0 && (
        <p className="muted-cell" data-testid="petty-spend-blocked">
          {blocked}
        </p>
      )}
      <button
        type="button"
        className="btn btn-cta"
        data-testid="petty-spend-save"
        disabled={Boolean(blocked) || busy}
        onClick={() => void save()}
      >
        {busy ? "Saving…" : "Save the spend"}
      </button>
    </section>
  );
}

// --- a top-up --------------------------------------------------------------------

function TopUpForm({
  position,
  online,
  busy,
  send,
}: {
  position: PettyPosition;
  online: boolean;
  busy: boolean;
  send: Send;
}) {
  const [draft, setDraft] = useState<TopUpDraft>({
    origin: "till",
    amountPaise: 0,
    givenBy: "",
    reference: "",
  });
  const topUpId = useRef(newUuid());
  const blocked = whyTopUpWaits(draft, position, online);
  const fromHo = draft.origin === "head_office";

  async function save() {
    const ok = await send(
      `${API}/top-ups`,
      {
        id: topUpId.current,
        site_id: position.site_id,
        origin: draft.origin,
        amount_paise: draft.amountPaise,
        given_by_name: fromHo ? draft.givenBy : "",
        reference: fromHo ? draft.reference : "",
      },
      "The top-up is saved.",
    );
    if (ok) {
      topUpId.current = newUuid();
      setDraft({
        origin: draft.origin,
        amountPaise: 0,
        givenBy: "",
        reference: "",
      });
    }
  }

  return (
    <section className="card section-card" data-testid="petty-top-up-form">
      <h3 className="section-title">Top up the box</h3>
      <label className="field">
        <span>Where the cash comes from</span>
        <select
          className="select"
          data-testid="petty-top-up-origin"
          value={draft.origin}
          disabled={busy}
          onChange={(e) =>
            setDraft({
              ...draft,
              origin: e.target.value as TopUpDraft["origin"],
            })
          }
        >
          <option value="till">{ORIGIN_LABEL.till}</option>
          <option value="head_office">{ORIGIN_LABEL.head_office}</option>
        </select>
      </label>
      <label className="field">
        <span>Amount</span>
        <RupeeInput
          testId="petty-top-up-amount"
          label="Amount"
          paise={draft.amountPaise}
          locked={busy}
          placeholder="0"
          onChange={(paise) => setDraft({ ...draft, amountPaise: paise ?? 0 })}
        />
      </label>
      {fromHo && (
        <>
          <label className="field">
            <span>Brought by</span>
            <input
              className="input"
              data-testid="petty-top-up-given-by"
              maxLength={120}
              value={draft.givenBy}
              disabled={busy}
              onChange={(e) => setDraft({ ...draft, givenBy: e.target.value })}
            />
          </label>
          <label className="field">
            <span>Head office voucher or receipt number</span>
            <input
              className="input"
              data-testid="petty-top-up-reference"
              maxLength={64}
              value={draft.reference}
              disabled={busy}
              onChange={(e) => setDraft({ ...draft, reference: e.target.value })}
            />
          </label>
        </>
      )}
      <p className="muted-cell">
        The box can take <Money paise={roomPaise(position)} /> more before it reaches its float.
        {position.float && <> {position.float.custodian_name} receives it.</>}
      </p>
      {blocked && draft.amountPaise > 0 && (
        <p className="muted-cell" data-testid="petty-top-up-blocked">
          {blocked}
        </p>
      )}
      <button
        type="button"
        className="btn"
        data-testid="petty-top-up-save"
        disabled={Boolean(blocked) || busy}
        onClick={() => void save()}
      >
        Save the top-up
      </button>
    </section>
  );
}

// --- what has happened -----------------------------------------------------------

function BillCell({
  spend,
  canAdd,
  busy,
  send,
}: {
  spend: PettySpend;
  canAdd: boolean;
  busy: boolean;
  send: Send;
}) {
  if (spend.bill_url) {
    return (
      <a
        href={apiUrl(spend.bill_url.replace(/^\/api/, ""))}
        target="_blank"
        rel="noreferrer"
        data-testid={`petty-bill-${spend.id}`}
      >
        View bill
      </a>
    );
  }
  if (!canAdd) return <span className="chip chip-amber">No bill</span>;
  return (
    <label className="btn btn-sm" data-testid={`petty-add-bill-${spend.id}`}>
      Add bill photo
      <input
        type="file"
        accept="image/jpeg,image/png,application/pdf"
        capture="environment"
        hidden
        disabled={busy}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (!file) return;
          const form = new FormData();
          form.append("bill", file);
          void send(`${API}/spends/${spend.id}/bill`, form, "The bill photo is added.");
        }}
      />
    </label>
  );
}

function SpendList({
  position,
  live,
  busy,
  send,
}: {
  position: PettyPosition;
  live: boolean;
  busy: boolean;
  send: Send;
}) {
  const canAdd = live && position.switched_on;
  return (
    <section className="card section-card" data-testid="petty-spends">
      <h3 className="section-title">Spends</h3>
      {position.spends.length === 0 ? (
        <p className="muted-cell">No spend is recorded yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>When</th>
                <th>Head</th>
                <th>For</th>
                <th className="num">Amount</th>
                <th>Where it stands</th>
                <th>Bill</th>
                <th>Recorded by</th>
              </tr>
            </thead>
            <tbody>
              {position.spends.map((spend) => (
                <tr key={spend.id} data-testid={`petty-spend-${spend.id}`}>
                  <td>{formatDateTime(spend.recorded_at)}</td>
                  <td>{spend.head}</td>
                  <td>{spend.note}</td>
                  <td className="num">
                    <Money paise={spend.amount_paise} />
                  </td>
                  <td>
                    <span className={`chip ${STATUS_CHIP[spend.status] ?? ""}`}>
                      {STATUS_LABEL[spend.status] ?? spend.status}
                    </span>
                    {spend.reject_reason && <div className="muted-cell">{spend.reject_reason}</div>}
                  </td>
                  <td>
                    <BillCell spend={spend} canAdd={canAdd} busy={busy} send={send} />
                  </td>
                  <td>{spend.recorded_by_name}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function TopUpList({ position }: { position: PettyPosition }) {
  return (
    <section className="card section-card" data-testid="petty-top-ups">
      <h3 className="section-title">Top-ups</h3>
      {position.top_ups.length === 0 ? (
        <p className="muted-cell">No top-up is recorded yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>When</th>
                <th>From</th>
                <th className="num">Amount</th>
                <th>Given by</th>
                <th>Received by</th>
              </tr>
            </thead>
            <tbody>
              {position.top_ups.map((row) => (
                <tr key={row.id}>
                  <td>{formatDateTime(row.recorded_at)}</td>
                  <td>{ORIGIN_LABEL[row.origin] ?? row.origin}</td>
                  <td className="num">
                    <Money paise={row.amount_paise} />
                  </td>
                  <td>
                    {row.origin === "till"
                      ? `${row.recorded_by_name} (the till)`
                      : `${row.given_by_name} · ${row.reference}`}
                  </td>
                  <td>{row.custodian_name}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/** The one spend the Owner opened from the approvals inbox. */
function OpenedSpend({ spend }: { spend: PettySpend }) {
  return (
    <section className="card section-card" data-testid="petty-opened">
      <h3 className="section-title">
        {spend.head} · <Money paise={spend.amount_paise} /> · {spend.store}
      </h3>
      <Row label="Where it stands" testId="petty-opened-status">
        {STATUS_LABEL[spend.status] ?? spend.status}
      </Row>
      <Row label="For">{spend.note || "-"}</Row>
      <Row label="Recorded">
        {spend.recorded_by_name}, {formatDateTime(spend.recorded_at)}
      </Row>
      <Row label="Custodian">{spend.custodian_name}</Row>
      <Row label="Bill photo">
        {spend.bill_url ? (
          <a href={apiUrl(spend.bill_url.replace(/^\/api/, ""))} target="_blank" rel="noreferrer">
            View bill
          </a>
        ) : (
          "Not added yet"
        )}
      </Row>
      {spend.status === "waiting" && (
        <p className="muted-cell">
          Approve or reject it in the <Link to="/approvals">approvals inbox</Link>.
        </p>
      )}
    </section>
  );
}
