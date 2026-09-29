// Mail in the top bar, beside the bell.
//
// The shape is the bell's, deliberately: same anchored popup, same outside-click
// and Esc, same "a row is a link, the popup does not act" rule. Two surfaces
// that behave differently while sitting 40px apart would be worse than either
// of them being individually nicer.
//
// What the popup will *not* do:
//
// * **It does not open the mail.** A row goes to `/mail`, where the body has
//   room and the attachments are reachable. A reader crammed into a 380px
//   dropdown is a worse version of the screen that already exists.
// * **It does not mark anything read by being opened.** Alerts clear by being
//   glanced at; mail does not. Nobody has read a mail because they saw its
//   subject line, and a badge that zeroed on open would be lying.
//
// It fetches nothing itself either: the state and the calls are in
// `useMailBadge`, including the last step of connecting, which runs here
// because this is the one mail surface mounted on every page.
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Mail, RefreshCw } from "lucide-react";

import { counterparty, fmtMailWhen, mailApi } from "../lib/mail";
import { useMailBadge } from "../lib/useMail";
import { useMobileNavExclusion } from "./MobileNavContext";

export function MailButton() {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { status, unread, rows, loading, connectError, loadPreview } = useMailBadge(pathname);
  useMobileNavExclusion(open, setOpen);

  function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (status?.connected) loadPreview();
  }

  useEffect(() => {
    if (!open) return;
    const off = new AbortController();
    const { signal } = off;
    document.addEventListener(
      "pointerdown",
      (e) => {
        if (!wrap.current?.contains(e.target as Node)) setOpen(false);
      },
      { signal },
    );
    document.addEventListener(
      "keydown",
      (e) => {
        if (e.key === "Escape") setOpen(false);
      },
      { signal },
    );
    return () => off.abort();
  }, [open]);

  // Nothing at all when this server has no Google credentials: a Mail button
  // whose only outcome is "not set up on this server" is noise in a top bar
  // every person in KDPS looks at all day.
  if (status && !status.configured) return null;

  // Three states, not two. "Could not check" is its own answer and must never
  // be rendered as "nothing new" — a badge that says all-clear when it failed
  // to ask is the one way this control can actively mislead.
  const label = !unread.known
    ? "Mail - could not check just now"
    : unread.count
      ? `Mail - ${unread.count} unread`
      : status?.connected
        ? "Mail - nothing new"
        : "Mail - not connected";
  const showCount = unread.known && unread.count > 0;

  return (
    <div className="bell-wrap" ref={wrap}>
      <button
        type="button"
        className="icon-btn"
        onClick={toggle}
        aria-label={label}
        aria-expanded={open}
        title={label}
        data-testid="mail-button"
      >
        <Mail size={18} />
        {showCount && (
          <span className="bell-count" data-testid="mail-unread-count">
            {unread.count > 9 ? "9+" : unread.count}
          </span>
        )}
        {/* A dot, not a number, when the count could not be refreshed. It says
            "ask me again" without inventing a figure. */}
        {!unread.known && <span className="mail-unknown" data-testid="mail-unread-unknown" />}
      </button>

      {open && (
        <div className="bell-popup" data-testid="mail-popup">
          <div className="mail-pop-head">
            <span className="mail-pop-title">Mail</span>
            {status?.connected && (
              <span className="mail-pop-address" title={status.email_address}>
                {status.email_address}
              </span>
            )}
          </div>

          <div className="bell-body">
            {connectError && (
              <p className="bell-quiet mail-sync-warn" data-testid="mail-connect-error">
                {connectError}
              </p>
            )}

            {!status?.connected ? (
              // The whole of the not-connected state: one sentence saying what
              // happens, and the button that does it.
              <div className="mail-connect" data-testid="mail-connect-prompt">
                <p className="bell-quiet">
                  Connect your KDPS Google account to read your mail here, without leaving the
                  system.
                </p>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => mailApi.connect()}
                  data-testid="mail-connect"
                >
                  Connect my mail
                </button>
              </div>
            ) : (
              <>
                {status.status === "needs_reconnect" && (
                  <p className="bell-quiet" data-testid="mail-reconnect">
                    Google needs you to sign in again.{" "}
                    <button type="button" className="link-btn" onClick={() => mailApi.connect()}>
                      Reconnect
                    </button>
                  </p>
                )}
                {loading ? (
                  <p className="bell-quiet">Loading…</p>
                ) : rows === null ? (
                  <p className="bell-quiet" data-testid="mail-error">
                    Could not load your mail just now.
                  </p>
                ) : rows.length === 0 ? (
                  <p className="bell-quiet" data-testid="mail-empty">
                    No unread mail.
                  </p>
                ) : (
                  <ul className="bell-list" data-testid="mail-list">
                    {rows.map((row) => (
                      <li key={row.id} className="bell-row" data-testid={`mail-row-${row.id}`}>
                        <p className="bell-row-head">
                          <span className="mail-from">{counterparty(row)}</span>
                          <span className="mail-when">{fmtMailWhen(row.sent_at)}</span>
                        </p>
                        {/* Into the screen with the message selected, never
                            into a reader inside the popup. */}
                        <p className="bell-row-title">
                          <Link
                            to={`/mail?open=${row.id}`}
                            className="link-cell"
                            onClick={() => setOpen(false)}
                          >
                            {row.subject || "(no subject)"}
                          </Link>
                        </p>
                        <p className="bell-row-meta">{row.snippet}</p>
                      </li>
                    ))}
                  </ul>
                )}

                {status.last_sync_error && (
                  <p className="bell-quiet mail-sync-warn" data-testid="mail-sync-error">
                    <RefreshCw size={12} /> {status.last_sync_error}
                  </p>
                )}
              </>
            )}

            {/* Outside the connected/not-connected split on purpose. The way
                into the full screen is the popup's whole second job, and it has
                to be there in *both* states — somebody who has not connected
                yet is precisely the person who should be able to open the
                screen that explains what connecting gets them. */}
            <button
              type="button"
              className="bell-all mail-open-all"
              onClick={() => {
                setOpen(false);
                navigate("/mail");
              }}
              data-testid="mail-open-full"
            >
              Open the full mail screen
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
