import { useCallback, useEffect, useRef, useState } from "react";
import { ShieldCheck } from "lucide-react";

import { askTheCustomer, consentMobile, withdrawal } from "../../../till/consent";
import type {
  ConsentQuestion,
  ConsentStanding,
  ConsentState,
  TillConsentWording,
} from "../../../till/consent";
import type { TillDisplayLink } from "../../../till/customerDisplay";
import type { TillEngine } from "../../../till/engine";

/**
 * Customer consent at the counter (store operations ticket 15, ST-CMP-6).
 *
 * Shown only once a 10-digit mobile is on the bill, and only where the store
 * has consent switched on. It never blocks the bill: the phone number stays
 * optional, and nothing here is part of Save & Print.
 *
 * The customer answers on the customer display: staff press "Ask on customer
 * display" and the display asks send my bill, are you under 18, send me offers.
 * There is no button that says yes for the customer. Without a display nothing
 * is asked and both stay off. Staff can withdraw either answer, which takes
 * effect at once on this counter and reaches head office as soon as it can.
 *
 * Keyed by the number (see `ConsentCard`), so typing a different number starts
 * afresh and stops any round still open for the old one.
 */
export function ConsentCard({
  engine,
  display,
  mobile,
  wording,
  tillNumber,
  online,
  pending,
  refused,
}: {
  engine: TillEngine | null;
  display: TillDisplayLink | null;
  /** The mobile as typed on the bill. */
  mobile: string;
  wording: TillConsentWording;
  tillNumber: string;
  online: boolean;
  /** Answers on this device still to reach head office. */
  pending: number;
  /** Answers head office refused for good, kept on this device. */
  refused: number;
}) {
  const number = consentMobile(mobile);
  if (!engine || !number) return null;
  return (
    <ConsentFor
      key={number}
      engine={engine}
      display={display}
      mobile={number}
      wording={wording}
      tillNumber={tillNumber}
      online={online}
      pending={pending}
      refused={refused}
    />
  );
}

interface Round {
  stopped: boolean;
  questionId: string | null;
}

function ConsentFor({
  engine,
  display,
  mobile,
  wording,
  tillNumber,
  online,
  pending,
  refused,
}: {
  engine: TillEngine;
  display: TillDisplayLink | null;
  mobile: string;
  wording: TillConsentWording;
  tillNumber: string;
  online: boolean;
  pending: number;
  refused: number;
}) {
  const [standing, setStanding] = useState<{ state: ConsentState; known: boolean } | null>(null);
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const round = useRef<Round | null>(null);
  const live = useRef(true);

  const reload = useCallback(async () => {
    const got = await engine.consentStanding(mobile);
    if (live.current) setStanding(got);
  }, [engine, mobile]);

  // Re-read when the line comes or goes, and when answers leave the device.
  useEffect(() => {
    void reload();
  }, [reload, online, pending]);

  // Leaving (a new bill, a new number) stops the round and takes the open
  // question off the customer's screen, so no answer lands on the wrong person.
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      const open = round.current;
      if (!open) return;
      open.stopped = true;
      if (open.questionId) display?.withdraw(open.questionId);
    };
  }, [display]);

  async function ask() {
    if (!display || round.current) return;
    const mine: Round = { stopped: false, questionId: null };
    round.current = mine;
    setAsking(true);
    setNote("");
    let note = "";
    try {
      const answers = await askTheCustomer(
        (question) => {
          mine.questionId = question.id;
          return display.ask(question);
        },
        async (answer) => {
          await engine.recordConsent(answer);
          await reload();
        },
        { mobile, wording, tillNumber, stopped: () => mine.stopped },
      );
      note = answers.length ? "" : "Nothing was answered.";
    } catch {
      // The device could not keep an answer. Stop the round rather than leave
      // the customer answering questions that go nowhere.
      note = "This counter could not keep the answer. Ask again.";
    } finally {
      mine.stopped = true;
      if (mine.questionId) display.withdraw(mine.questionId);
      if (round.current === mine) round.current = null;
      if (live.current) {
        setAsking(false);
        setNote(note);
      }
    }
  }

  function stop() {
    const open = round.current;
    if (!open) return;
    open.stopped = true;
    if (open.questionId) display?.withdraw(open.questionId);
  }

  async function withdraw(question: ConsentQuestion) {
    await engine.recordConsent(withdrawal(mobile, question, { wording, tillNumber }));
    await reload();
    if (live.current) {
      setNote(
        question === "bill"
          ? "Withdrawn: the bill will not be sent to this number."
          : "Withdrawn: no offers will be sent to this number.",
      );
    }
  }

  const known = standing?.known ?? false;
  return (
    <section className="bill-consent" data-testid="consent-card">
      <div className="bill-customer-heading">
        <p className="eyebrow">Consent</p>
        <span>The customer answers on the display</span>
      </div>
      <dl className="bill-consent-rows">
        <ConsentRow
          label="Send my bill"
          question="bill"
          value={standing?.state.bill ?? null}
          known={known}
          loaded={standing !== null}
          onWithdraw={() => void withdraw("bill")}
        />
        <ConsentRow
          label="Send me offers"
          question="offers"
          value={standing?.state.offers ?? null}
          known={known}
          loaded={standing !== null}
          onWithdraw={() => void withdraw("offers")}
        />
      </dl>
      {display ? (
        asking ? (
          <div className="bill-consent-asking">
            <p className="muted-cell" data-testid="consent-waiting">
              Waiting for the customer to answer on the display…
            </p>
            <button type="button" className="btn" data-testid="consent-stop" onClick={stop}>
              Stop asking
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="btn bill-business-toggle"
            data-testid="consent-ask"
            onClick={() => void ask()}
          >
            <ShieldCheck size={15} /> Ask on customer display
          </button>
        )
      ) : (
        <p className="muted-cell bill-consent-note" data-testid="consent-no-display">
          Open the customer display to ask. Only the customer can say yes; until then both stay off.
        </p>
      )}
      {note && (
        <p className="muted-cell bill-consent-note" data-testid="consent-note">
          {note}
        </p>
      )}
      {refused > 0 && (
        <p className="bill-alert bill-consent-note" data-testid="consent-refused">
          Head office refused {refused === 1 ? "1 answer" : `${refused} answers`} from this counter.
          Ask the customer again if it still matters.
        </p>
      )}
      {pending > 0 && (
        <p className="muted-cell bill-consent-note" data-testid="consent-unsent">
          {pending === 1 ? "1 answer" : `${pending} answers`} waiting to reach head office.
        </p>
      )}
    </section>
  );
}

function ConsentRow({
  label,
  question,
  value,
  known,
  loaded,
  onWithdraw,
}: {
  label: string;
  question: ConsentQuestion;
  value: ConsentStanding | null;
  known: boolean;
  loaded: boolean;
  onWithdraw: () => void;
}) {
  let words: string;
  if (!loaded) words = "Checking…";
  else if (value?.given) words = "Yes";
  else if (value && value.under_18) words = "No (under 18)";
  else if (value) words = "No";
  else if (known) words = "Not asked (off)";
  else words = "Not known offline (off here)";
  // A yes can be withdrawn; so can an answer the counter cannot see offline,
  // because the customer asking to stop must never wait for the line.
  const withdrawable = loaded && (value?.given === true || (!known && value === null));
  return (
    <div className="bill-consent-row">
      <dt>{label}</dt>
      <dd data-testid={`consent-${question}`} data-given={value ? String(value.given) : "unknown"}>
        {words}
      </dd>
      {withdrawable && (
        <button
          type="button"
          className="btn btn-sm"
          data-testid={`consent-withdraw-${question}`}
          onClick={onWithdraw}
        >
          Withdraw
        </button>
      )}
    </div>
  );
}
