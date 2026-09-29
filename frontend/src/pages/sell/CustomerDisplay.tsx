import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { apiErrorMessage, typedApi } from "../../lib/api";
import { Money } from "../../lib/format";
import { DISPLAY_START, DisplayScreenLink, openChannel } from "../../till/customerDisplay";
import type { DisplayState, DisplayView } from "../../till/customerDisplay";
import "./CustomerDisplay.css";

// ---------------------------------------------------------------------------
// The customer display (store operations ticket 09, ST-POS-6)
// ---------------------------------------------------------------------------
//
// The second window the till opens, turned to face the customer. It has no
// menu and no staff controls. It asks the server exactly one thing, once, as
// it opens: may this signed-in login show this store's display (a till login
// for that store, with the switch on)? After that it only listens to the till
// beside it (`till/customerDisplay.ts`), so it keeps working with no internet.
//
// What it can show is whatever `readTillMessage` lets through: the lines,
// offers, savings, total, the UPI amount and code, a thank-you, and a question
// for the customer. Nothing else reaches this window.

type Opening =
  | { state: "checking" }
  | { state: "refused"; reason: string }
  | { state: "open"; storeCode: string };

export default function CustomerDisplayPage() {
  const [params] = useSearchParams();
  const store = params.get("store") ?? "";
  const [opening, setOpening] = useState<Opening>({ state: "checking" });

  useEffect(() => {
    let live = true;
    if (!store) {
      setOpening({ state: "refused", reason: "Open the customer display from the till." });
      return;
    }
    if (!navigator.onLine) {
      setOpening({
        state: "refused",
        reason:
          "The customer display needs the internet to open. Connect, then open it again " +
          "from the till. Once open, it keeps working if the connection drops.",
      });
      return;
    }
    typedApi
      .get("/sell/customer-display", { params: { store } })
      .then(({ data }) => {
        if (!live) return;
        if (data.store_code !== store) {
          setOpening({ state: "refused", reason: "Open the customer display from the till." });
          return;
        }
        setOpening({ state: "open", storeCode: data.store_code });
      })
      .catch((error) => {
        if (!live) return;
        const status = (error as { response?: { status?: number } })?.response?.status;
        setOpening({
          state: "refused",
          reason:
            status === 401
              ? "Sign in at the till first, then open the customer display from there."
              : status
                ? apiErrorMessage(error)
                : "Could not reach head office to open the customer display. Connect, then " +
                  "open it again from the till.",
        });
      });
    return () => {
      live = false;
    };
  }, [store]);

  if (opening.state === "checking") {
    return <main className="cdisplay" data-testid="display-checking" />;
  }
  if (opening.state === "refused") {
    return (
      <main className="cdisplay">
        <p className="cdisplay-refused" data-testid="display-refused">
          {opening.reason}
        </p>
      </main>
    );
  }
  return <Following storeCode={opening.storeCode} />;
}

function Following({ storeCode }: { storeCode: string }) {
  const [state, setState] = useState<DisplayState>(DISPLAY_START);
  const link = useRef<DisplayScreenLink | null>(null);
  const [noChannel, setNoChannel] = useState(false);

  useEffect(() => {
    const channel = openChannel(storeCode);
    if (!channel) {
      setNoChannel(true);
      return;
    }
    const opened = new DisplayScreenLink(channel, setState);
    link.current = opened;
    return () => {
      link.current = null;
      opened.close();
    };
  }, [storeCode]);

  if (noChannel) {
    return (
      <main className="cdisplay">
        <p className="cdisplay-refused" data-testid="display-refused">
          This browser cannot show a customer display. Use an up-to-date Chrome or Edge.
        </p>
      </main>
    );
  }

  const question = state.question;
  return (
    <main className="cdisplay" data-testid="display" data-screen={state.view.screen}>
      <Screen view={state.view} />
      {question && (
        <div
          className="cdisplay-question"
          role="dialog"
          aria-modal="true"
          data-testid="display-question"
        >
          <p className="cdisplay-question-text">{question.text}</p>
          <div className="cdisplay-question-choices">
            {question.choices.map((choice) => (
              <button
                key={choice.value}
                type="button"
                className="cdisplay-choice"
                data-testid={`display-answer-${choice.value}`}
                onClick={() => link.current?.answer(question.id, choice.value)}
              >
                {choice.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </main>
  );
}

function Screen({ view }: { view: DisplayView }) {
  if (view.screen === "welcome") {
    return (
      <section className="cdisplay-centre" data-testid="display-welcome">
        <h1>Welcome</h1>
        <p>Your bill will appear here as it is made.</p>
      </section>
    );
  }
  if (view.screen === "thanks") {
    return (
      <section className="cdisplay-centre" data-testid="display-thanks">
        <h1>Thank you for shopping with us</h1>
        <p>Please visit again.</p>
      </section>
    );
  }
  return (
    <section className="cdisplay-bill" data-testid="display-bill">
      <table className="cdisplay-lines">
        <thead>
          <tr>
            <th>Item</th>
            <th className="num">Qty</th>
            <th className="num">Price</th>
            <th className="num">Amount</th>
          </tr>
        </thead>
        <tbody>
          {view.lines.map((line, index) => (
            <tr key={index} data-testid="display-line">
              <td>
                <span className="cdisplay-desc">{line.description}</span>
                {line.offers.map((offer) => (
                  <span key={offer} className="cdisplay-chip">
                    {offer}
                  </span>
                ))}
                {line.saved_paise > 0 && (
                  <span className="cdisplay-saved">
                    You save <Money paise={line.saved_paise} />
                  </span>
                )}
              </td>
              <td className="num">{line.qty}</td>
              <td className="num">
                <Money paise={line.mrp_paise} />
              </td>
              <td className="num">
                <Money paise={line.amount_paise} />
              </td>
            </tr>
          ))}
          {view.returns.map((line, index) => (
            <tr key={`r${index}`} className="cdisplay-return" data-testid="display-return">
              <td>
                <span className="cdisplay-desc">Returned: {line.description}</span>
              </td>
              <td className="num">{line.qty}</td>
              <td className="num" />
              <td className="num">
                −<Money paise={line.amount_paise} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <aside className="cdisplay-totals">
        {view.offers.length > 0 && (
          <div className="cdisplay-offers" data-testid="display-offers">
            <span>Offers applied</span>
            <ul>
              {view.offers.map((offer) => (
                <li key={offer}>{offer}</li>
              ))}
            </ul>
          </div>
        )}
        {view.savings_paise > 0 && (
          <p className="cdisplay-savings" data-testid="display-savings">
            You save <Money paise={view.savings_paise} />
          </p>
        )}
        <p className="cdisplay-total" data-testid="display-total">
          <span>Total</span>
          <Money paise={view.total_paise} />
        </p>
        {view.upi && (
          <div
            className="cdisplay-upi"
            data-testid="display-upi"
            data-qr={view.upi.qr || undefined}
          >
            <p>
              Pay <Money paise={view.upi.amount_paise} /> by UPI
            </p>
            {/* The scannable code is drawn once a payment machine supplies one
                (ST-POS-1, P3); `data-qr` carries its payload for that step.
                Until then the display shows the amount only, and never a
                picture that looks scannable and pays nobody. */}
          </div>
        )}
      </aside>
    </section>
  );
}
