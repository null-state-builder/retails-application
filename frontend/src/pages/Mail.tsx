// The full mail screen: folders, a list, a reader, and a composer.
//
// Two panes, not three. The classic mail layout puts folders in a rail of their
// own, but this app already spends horizontal space on a sidebar, and KDPS has
// exactly two folders to offer (Inbox and Sent — the rest of a person's Gmail
// labels stay in Gmail). Two folders do not earn a column; they are tabs above
// the list.
//
// The reader shows `body_html` inside a sandboxed iframe rather than injecting
// it into the page. That is the single most important line in this file: mail
// bodies are attacker-controlled HTML from anyone who knows a KDPS address, and
// rendering them in our own origin would hand every sender a script that runs
// with the reader's session. See `MailBody`.
//
// Nothing here fetches. The mailbox — its status, its rows, what "open this
// one" does to the unread flag on both sides — is `useMailbox`; this file is
// what that looks like.
import { useState } from "react";
import { Inbox, Paperclip, RefreshCw, Reply, Send, Trash2, X } from "lucide-react";

import { PageHeader } from "../components/PageHeader";
import { SearchBox } from "../components/SearchBox";
import { apiErrorMessage } from "../lib/api";
import {
  MAIL_FOLDERS,
  announceMailChanged,
  counterparty,
  fmtMailWhen,
  fmtSize,
  mailApi,
  type MailDetail,
} from "../lib/mail";
import { useMailbox } from "../lib/useMail";
import "./Mail.css";

/**
 * The message body, rendered in a sandboxed iframe.
 *
 * `sandbox` with no `allow-scripts` and no `allow-same-origin` means the frame
 * can render markup and nothing else: no script, no access to our cookies or
 * localStorage (which is where this app's JWT lives), no navigating the parent.
 * `srcDoc` rather than a URL so nothing is fetched and no referrer leaks.
 *
 * The alternative — `dangerouslySetInnerHTML` plus a sanitiser — was rejected.
 * A sanitiser is a denylist that has to stay ahead of every parser quirk
 * forever; the iframe is a boundary the browser enforces. When the two
 * disagree, take the one that does not depend on us being right.
 *
 * Remote images are left alone rather than proxied. They leak "this was read,
 * at this time, from this IP" to the sender, which is what a tracking pixel is
 * for — noted here as a known, accepted trade for now rather than a thing
 * nobody thought about.
 *
 * The frame stays white in dark mode, deliberately. Business mail is written
 * against a white background, and recolouring somebody else's HTML is how a
 * sender's own dark-on-light table becomes invisible. A readable white card
 * beats an unreadable dark one.
 */

/** A base stylesheet, prepended so the *sender's* CSS still wins anything it
 *  sets. It only fixes what an HTML fragment with no styling of its own would
 *  otherwise inherit from the browser: Times at 16px, jammed against the edge,
 *  and long unbroken URLs pushing out a horizontal scrollbar.
 *
 *  The two colours are literals and cannot be anything else: a custom property
 *  does not cross an iframe boundary, and this document has no stylesheet of
 *  ours. They mirror `--mail-paper` / `--mail-paper-ink`, which the frame's own
 *  border and background use so the two edges match — change one, change both. */
const BODY_RESET = `<style>
  html,body{margin:0;padding:14px 16px;background:#ffffff;color:#222222;
    font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    overflow-wrap:anywhere;}
  img,table{max-width:100%;}
</style>`;

/** How tall to draw the frame.
 *
 *  Measuring the real content would be the right answer and the sandbox
 *  forbids it: reading the frame's `scrollHeight` needs `allow-same-origin`,
 *  and letting the frame measure and postMessage itself needs `allow-scripts`
 *  — which is the sender's script running, the exact thing the boundary above
 *  exists to prevent. So the height is estimated from the markup instead.
 *
 *  **The estimate deliberately errs tall.** Both errors are ugly and they are
 *  not equally bad: guess long and a short mail sits above some white space;
 *  guess short and a sentence is sliced off mid-word inside a frame with no
 *  visible scrollbar, which reads as a broken page rather than a scrollable
 *  one. Nobody scrolls a box they cannot tell is scrollable. So the constants
 *  below are generous on purpose, and the cap is high enough that a long
 *  newsletter scrolls rather than a normal mail being truncated. */
function frameHeight(html: string): number {
  const breaks = (html.match(/<(br|\/p|\/div|\/tr|\/li|\/h[1-6]|\/table)\b/gi) || []).length;
  const estimated = 120 + breaks * 34 + html.length / 8;
  return Math.round(Math.min(900, Math.max(220, estimated)));
}

function MailBody({ message }: { message: MailDetail }) {
  if (!message.body_html) {
    return (
      <pre className="mail-body-text" data-testid="mail-body">
        {message.body_text || "(no message body)"}
      </pre>
    );
  }
  return (
    <iframe
      className="mail-body-frame"
      title="Message body"
      sandbox=""
      referrerPolicy="no-referrer"
      srcDoc={BODY_RESET + message.body_html}
      style={{ height: frameHeight(message.body_html) }}
      data-testid="mail-body"
    />
  );
}

function Composer({
  replyTo,
  from,
  onClose,
}: {
  replyTo: MailDetail | null;
  from: string;
  onClose: (sent: boolean) => void;
}) {
  const [to, setTo] = useState(replyTo ? replyTo.from_email : "");
  const [subject, setSubject] = useState(replyTo ? `Re: ${replyTo.subject}` : "");
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  function send() {
    setSending(true);
    setError("");
    mailApi
      .send({ to, subject, body, reply_to: replyTo?.id })
      .then(() => {
        announceMailChanged();
        onClose(true);
      })
      .catch((e) => setError(apiErrorMessage(e)))
      .finally(() => setSending(false));
  }

  return (
    <div className="mail-composer" data-testid="mail-composer">
      <div className="mail-composer-head">
        <span>{replyTo ? "Reply" : "New mail"}</span>
        <span className="mail-composer-from">from {from}</span>
        <button
          type="button"
          className="icon-btn"
          onClick={() => onClose(false)}
          aria-label="Close composer"
          data-testid="mail-composer-close"
        >
          <X size={16} />
        </button>
      </div>
      <input
        className="mail-field"
        placeholder="To"
        value={to}
        onChange={(e) => setTo(e.target.value)}
        data-testid="mail-to"
      />
      <input
        className="mail-field"
        placeholder="Subject"
        value={subject}
        onChange={(e) => setSubject(e.target.value)}
        data-testid="mail-subject"
      />
      <textarea
        className="mail-field mail-body-input"
        placeholder="Write your message"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        rows={8}
        data-testid="mail-body-input"
      />
      {error && (
        <p className="mail-error" data-testid="mail-send-error">
          {error}
        </p>
      )}
      <div className="mail-composer-actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={send}
          disabled={sending || !to.trim()}
          data-testid="mail-send"
        >
          <Send size={15} /> {sending ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}

export function MailPage() {
  const mailbox = useMailbox();
  const {
    status,
    folder,
    setFolder,
    query,
    setQuery,
    rows,
    loading,
    unreadCount,
    openMessage,
    readError,
  } = mailbox;
  const [composing, setComposing] = useState(false);
  const [replyTo, setReplyTo] = useState<MailDetail | null>(null);
  const [confirmingDisconnect, setConfirmingDisconnect] = useState(false);

  if (status && !status.configured) {
    return (
      <>
        <PageHeader title="Mail" />
        <div className="card mail-empty-state">
          <p>Mail has not been set up on this server yet.</p>
        </div>
      </>
    );
  }

  if (status && !status.connected) {
    return (
      <>
        <PageHeader title="Mail" />
        <div className="card mail-empty-state" data-testid="mail-page-connect">
          <Inbox size={32} />
          <h3>Read your KDPS mail here</h3>
          <p>
            Connect your KDPS Google account once, and your inbox appears in this system - beside
            your bookings, your stock and your ledgers.
          </p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => mailApi.connect()}
            data-testid="mail-page-connect-button"
          >
            Connect my mail
          </button>
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Mail"
        lead={status?.email_address}
        actions={
          <>
            <button
              type="button"
              className="btn"
              onClick={() => mailbox.reload({ sync: true })}
              data-testid="mail-refresh"
            >
              <RefreshCw size={15} /> Refresh
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                setReplyTo(null);
                setComposing(true);
              }}
              data-testid="mail-compose"
            >
              <Send size={15} /> New mail
            </button>
          </>
        }
      />

      {status?.status === "needs_reconnect" && (
        <div className="card mail-warn" data-testid="mail-page-reconnect">
          Google needs you to sign in again before more mail can arrive.{" "}
          <button type="button" className="link-btn" onClick={() => mailApi.connect()}>
            Reconnect
          </button>
        </div>
      )}
      {status?.last_sync_error && (
        <div className="card mail-warn" data-testid="mail-page-sync-error">
          {status.last_sync_error}
        </div>
      )}
      {readError && (
        <div className="card mail-warn" data-testid="mail-page-read-error">
          {readError}
        </div>
      )}

      <div className="mail-layout">
        <div className="card mail-list-pane">
          <div className="mail-boxes" role="tablist">
            {MAIL_FOLDERS.map(([key, text]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={folder === key}
                className={`bell-tab ${folder === key ? "active" : ""}`}
                onClick={() => {
                  setFolder(key);
                  mailbox.closeReader();
                }}
                data-testid={`mail-box-${key}`}
              >
                {text}
                {key === "inbox" && !!unreadCount && (
                  <span className="bell-tab-count">{unreadCount}</span>
                )}
              </button>
            ))}
          </div>

          <div className="mail-search">
            <SearchBox
              value={query}
              onChange={setQuery}
              placeholder="Search your mail"
              label="Search your mail"
              testId="mail-search"
            />
          </div>

          {loading ? (
            <p className="bell-quiet">Loading…</p>
          ) : rows === null ? (
            <p className="bell-quiet" data-testid="mail-list-error">
              Could not load your mail just now.
            </p>
          ) : rows.length === 0 ? (
            <p className="bell-quiet" data-testid="mail-list-empty">
              {query ? "Nothing matches that." : "Nothing here."}
            </p>
          ) : (
            <ul className="mail-rows" data-testid="mail-rows">
              {rows.map((row) => (
                <li key={row.id}>
                  <button
                    type="button"
                    className={`mail-row ${row.is_unread ? "unread" : ""} ${
                      openMessage?.id === row.id ? "active" : ""
                    }`}
                    onClick={() => mailbox.openId(row.id)}
                    data-testid={`mail-item-${row.id}`}
                  >
                    <span className="mail-row-top">
                      <span className="mail-row-from">{counterparty(row)}</span>
                      <span className="mail-row-when">{fmtMailWhen(row.sent_at)}</span>
                    </span>
                    <span className="mail-row-subject">{row.subject || "(no subject)"}</span>
                    <span className="mail-row-snippet">{row.snippet}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="card mail-reader-pane">
          {composing ? (
            <Composer
              replyTo={replyTo}
              from={status?.email_address ?? ""}
              onClose={(sent) => {
                setComposing(false);
                setReplyTo(null);
                if (sent) mailbox.reload({ sync: true });
              }}
            />
          ) : !openMessage ? (
            <p className="bell-quiet" data-testid="mail-reader-empty">
              Pick a message to read it.
            </p>
          ) : (
            <div data-testid="mail-reader">
              <div className="mail-reader-head">
                <h2 className="mail-reader-subject">{openMessage.subject || "(no subject)"}</h2>
                <div className="mail-reader-actions">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => {
                      setReplyTo(openMessage);
                      setComposing(true);
                    }}
                    data-testid="mail-reply"
                  >
                    <Reply size={15} /> Reply
                  </button>
                  <button
                    type="button"
                    className="icon-btn"
                    onClick={mailbox.closeReader}
                    aria-label="Close message"
                    data-testid="mail-close"
                  >
                    <X size={16} />
                  </button>
                </div>
              </div>
              <p className="mail-reader-meta">
                <strong>{openMessage.from_name || openMessage.from_email}</strong>
                {openMessage.from_name && <span> &lt;{openMessage.from_email}&gt;</span>}
                <span className="mail-reader-when">{fmtMailWhen(openMessage.sent_at)}</span>
              </p>
              <p className="mail-reader-meta">to {openMessage.to_emails || "(nobody)"}</p>

              {openMessage.attachments.length > 0 && (
                <div className="mail-attachments" data-testid="mail-attachments">
                  {openMessage.attachments.map((a) => (
                    <button
                      key={a.id}
                      type="button"
                      className="mail-attachment"
                      onClick={() => mailApi.downloadAttachment(a.id, a.filename)}
                      data-testid={`mail-attachment-${a.id}`}
                    >
                      <Paperclip size={14} /> {a.filename}
                      <span className="mail-attachment-size">{fmtSize(a.size_bytes)}</span>
                    </button>
                  ))}
                </div>
              )}

              <MailBody message={openMessage} />
            </div>
          )}
        </div>
      </div>

      {/* Two clicks, and the second one says what it does. Disconnecting throws
          away the cached mail, and a single mis-aimed click at the bottom of a
          screen should not do that. An inline confirm rather than window.confirm
          on purpose: a native dialog blocks the whole page (and the browser
          automation we QA with) until somebody dismisses it. */}
      <div className="mail-footer">
        {confirmingDisconnect ? (
          <span className="mail-disconnect-confirm">
            Disconnect {status?.email_address}? Your mail stays in Gmail; only the copy in KDPS
            goes.
            <button
              type="button"
              className="btn btn-cta"
              onClick={() => {
                setConfirmingDisconnect(false);
                mailbox.disconnect();
              }}
              data-testid="mail-disconnect-confirm"
            >
              Yes, disconnect
            </button>
            <button
              type="button"
              className="link-btn"
              onClick={() => setConfirmingDisconnect(false)}
            >
              Keep it
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="link-btn mail-disconnect"
            onClick={() => setConfirmingDisconnect(true)}
            data-testid="mail-disconnect"
          >
            <Trash2 size={13} /> Disconnect this mailbox from KDPS
          </button>
        )}
      </div>
    </>
  );
}

export default MailPage;
