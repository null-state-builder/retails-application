// Everything mail *does*, so the two mail components can be things mail looks
// like.
//
// The popup and the full screen are one mailbox at two sizes. Left to
// themselves each grows its own fetch, its own error handling and its own idea
// of what "mark as read" means, and the two drift while sitting forty pixels
// apart in the same top bar. So the state and the calls live here and the
// components render what they are handed.
//
// Three hooks, one per surface: the badge, the screen, and the last step of
// connecting — which has to run somewhere always mounted, because Google's
// redirect can land on any page.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { apiErrorMessage } from "./api";
import {
  MAIL_CHANGED,
  MAIL_CONNECT_MESSAGES,
  announceMailChanged,
  mailApi,
  type MailConnectOutcome,
  type MailDetail,
  type MailFolder,
  type MailRow,
  type MailStatus,
} from "./mail";

/** How many unread the popup lists before it stops and points at the screen. */
export const PREVIEW_ROWS = 8;

/** What the badge knows.
 *
 *  `known` is the whole reason this is an object rather than a number. A count
 *  that could not be fetched is not zero: rendering it as zero makes the top
 *  bar say "nothing new" at the exact moment it has no idea, which is the one
 *  wrong answer a notification badge can give. So the last good count stays on
 *  screen and `known` goes false. */
export type UnreadCount = { count: number; known: boolean };

export type MailBadge = {
  status: MailStatus | null;
  unread: UnreadCount;
  /** `null` means the preview fetch failed — an empty list and a failed read
   *  must not render as the same thing. */
  rows: MailRow[] | null;
  loading: boolean;
  /** Whatever went wrong on the way back from Google, if anything. */
  connectError: string;
  refresh: () => void;
  loadPreview: () => void;
};

export function useMailBadge(pathname: string): MailBadge {
  const [status, setStatus] = useState<MailStatus | null>(null);
  const [unread, setUnread] = useState<UnreadCount>({ count: 0, known: true });
  const [rows, setRows] = useState<MailRow[] | null>([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(() => {
    mailApi
      .status()
      .then((s) => {
        setStatus(s);
        if (!s.connected) {
          setUnread({ count: 0, known: true });
          return;
        }
        mailApi
          .unread()
          .then((u) => setUnread({ count: u.unread, known: true }))
          .catch(() => setUnread((last) => ({ count: last.count, known: false })));
      })
      .catch(() => {
        setStatus(null);
        setUnread((last) => ({ count: last.count, known: false }));
      });
  }, []);

  // Recounted on navigation and whenever anything sends or reads a mail — the
  // same rule the bell learned the hard way, where clearing the last item left
  // the badge insisting something was still waiting.
  useEffect(() => {
    refresh();
    window.addEventListener(MAIL_CHANGED, refresh);
    return () => window.removeEventListener(MAIL_CHANGED, refresh);
  }, [refresh, pathname]);

  const loadPreview = useCallback(() => {
    setLoading(true);
    mailApi
      .list("unread", { limit: PREVIEW_ROWS })
      .then((r) => setRows(r.messages))
      .catch(() => setRows(null))
      .finally(() => setLoading(false));
  }, []);

  // Finishing the connect belongs here, in the one component that is mounted on
  // every page: Google's redirect can land anywhere, and the handoff is
  // single-use — two surfaces both trying to spend it would mean one of them
  // reporting a failure that had in fact succeeded.
  const connectError = useMailConnectHandoff(refresh);

  return { status, unread, rows, loading, connectError, refresh, loadPreview };
}

/** The last step of connecting: read `?mail=` off the address bar, finish the
 *  handoff with an authenticated call, and clear both markers so a reload does
 *  not replay them. Returns whatever needs saying to the person, or "". */
function useMailConnectHandoff(onConnected: () => void): string {
  const [params, setParams] = useSearchParams();
  const [message, setMessage] = useState("");
  /** Guards against finishing the same handoff twice — React runs effects
   *  twice in development, and the second attempt would fail because the first
   *  consumed it, replacing a success with an error on screen. */
  const attempted = useRef("");
  /** The two things the effect needs but must not re-run for. Both change
   *  identity on every render; depending on them would re-enter the effect
   *  after its own `setParams` and spend the handoff a second time. */
  const latest = useRef({ params, setParams, onConnected });
  latest.current = { params, setParams, onConnected };

  const outcome = params.get("mail") as MailConnectOutcome | null;
  const handoff = params.get("mail_handoff") ?? "";

  useEffect(() => {
    if (!outcome) return;
    if (attempted.current === `${outcome}:${handoff}`) return;
    attempted.current = `${outcome}:${handoff}`;

    // Cleared first, so a reload of this page is not a second attempt.
    const next = new URLSearchParams(latest.current.params);
    next.delete("mail");
    next.delete("mail_handoff");
    latest.current.setParams(next, { replace: true });

    if (outcome !== "ready" || !handoff) {
      setMessage(MAIL_CONNECT_MESSAGES[outcome] ?? MAIL_CONNECT_MESSAGES.failed);
      return;
    }
    mailApi
      .complete(handoff)
      .then(() => {
        setMessage("");
        announceMailChanged();
        latest.current.onConnected();
      })
      .catch((e) => setMessage(apiErrorMessage(e)));
  }, [outcome, handoff]);

  return message;
}

export type Mailbox = {
  status: MailStatus | null;
  folder: MailFolder;
  setFolder: (folder: MailFolder) => void;
  query: string;
  setQuery: (query: string) => void;
  rows: MailRow[] | null;
  loading: boolean;
  unreadCount: number;
  openMessage: MailDetail | null;
  /** Non-empty when Gmail refused to mark something read. Shown rather than
   *  swallowed: the row went back to bold under the reader's eyes and that
   *  needs an explanation. */
  readError: string;
  openId: (id: number) => void;
  closeReader: () => void;
  reload: (opts?: { sync?: boolean }) => void;
  disconnect: () => void;
};

export function useMailbox(): Mailbox {
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState<MailStatus | null>(null);
  const [folder, setFolder] = useState<MailFolder>("inbox");
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<MailRow[] | null>([]);
  const [openMessage, setOpenMessage] = useState<MailDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState("");

  const loadStatus = useCallback(
    () =>
      mailApi
        .status()
        .then(setStatus)
        .catch(() => setStatus(null)),
    [],
  );

  const loadList = useCallback(
    (opts: { sync?: boolean } = {}) => {
      setLoading(true);
      mailApi
        .list(folder, { q: query, sync: opts.sync })
        .then((r) => setRows(r.messages))
        .catch(() => setRows(null))
        .finally(() => setLoading(false));
    },
    [folder, query],
  );

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    loadList();
  }, [loadList]);

  // The top bar owns the last step of connecting (see `useMailBadge`), so this
  // screen learns a mailbox has just been attached the same way it learns
  // anything else changed — by listening rather than by asking on a timer.
  useEffect(() => {
    const onChanged = () => {
      loadStatus();
      loadList({ sync: true });
    };
    window.addEventListener(MAIL_CHANGED, onChanged);
    return () => window.removeEventListener(MAIL_CHANGED, onChanged);
  }, [loadStatus, loadList]);

  const setUnread = useCallback((id: number, isUnread: boolean) => {
    setRows((current) =>
      (current ?? []).map((r) => (r.id === id ? { ...r, is_unread: isUnread } : r)),
    );
  }, []);

  const openId = useCallback(
    (id: number) => {
      setReadError("");
      mailApi
        .message(id)
        .then((message) => {
          setOpenMessage(message);
          if (!message.is_unread) return;
          // Un-bolded under the click so the list feels immediate, then told to
          // the server — and put *back* if Gmail would not take it. Leaving the
          // row read here while Gmail still says unread is the state the two
          // sides can never reconcile, so the optimism has to be reversible.
          setUnread(id, false);
          mailApi
            .markRead(id)
            .then(announceMailChanged)
            .catch((e) => {
              setUnread(id, true);
              setOpenMessage((m) => (m && m.id === id ? { ...m, is_unread: true } : m));
              setReadError(apiErrorMessage(e));
            });
        })
        .catch(() => setOpenMessage(null));
    },
    [setUnread],
  );

  /** The popup deep-links here with `?open=<id>`, so a person clicking a
   *  subject line in the dropdown lands on that message rather than on a list
   *  they then have to find it in again. */
  const deepLink = params.get("open");

  useEffect(() => {
    if (deepLink) openId(Number(deepLink));
  }, [deepLink, openId]);

  const closeReader = useCallback(() => {
    setOpenMessage(null);
    setReadError("");
    // Drop the deep-link so closing the reader does not immediately reopen it.
    if (params.get("open")) {
      const next = new URLSearchParams(params);
      next.delete("open");
      setParams(next, { replace: true });
    }
  }, [params, setParams]);

  const reload = useCallback(
    (opts: { sync?: boolean } = {}) => {
      loadStatus();
      loadList(opts);
    },
    [loadStatus, loadList],
  );

  const disconnect = useCallback(() => {
    mailApi.disconnect().then(() => {
      announceMailChanged();
      loadStatus();
      setRows([]);
      setOpenMessage(null);
    });
  }, [loadStatus]);

  /** How many unread sit in the Inbox — which is a fact about the Inbox, not
   *  about whatever list is on screen.
   *
   *  Counting the loaded rows alone got this wrong twice over: switch to Sent
   *  and the Inbox tab's count vanished, and type in the search box and it fell
   *  to however many unread happened to match. So the live count is used while
   *  the Inbox is the unfiltered list, and remembered for every other view. It
   *  still has to be derived from the rows rather than fetched, because marking
   *  a message read has to take the tab down by one immediately. */
  const liveUnread = useMemo(() => (rows ?? []).filter((r) => r.is_unread).length, [rows]);
  const showingWholeInbox = folder === "inbox" && !query.trim();
  const [rememberedUnread, setRememberedUnread] = useState(0);

  useEffect(() => {
    if (showingWholeInbox) setRememberedUnread(liveUnread);
  }, [showingWholeInbox, liveUnread]);

  const unreadCount = showingWholeInbox ? liveUnread : rememberedUnread;

  return {
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
    openId,
    closeReader,
    reload,
    disconnect,
  };
}
