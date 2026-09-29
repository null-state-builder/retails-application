// The mail client seam: the types and the four calls both mail surfaces share.
//
// The popup and the full screen show the same mailbox at two sizes, so they read
// through one module rather than each growing its own fetch. That is what keeps
// "mark as read" meaning the same thing in both places.
import { api } from "./api";

export type MailStatus = {
  /** Does this *server* have Google credentials at all? False means an operator
   *  has not set mail up here — a different thing from "you have not connected",
   *  and the reason the top bar hides the button entirely rather than offering
   *  one that cannot work. */
  configured: boolean;
  connected: boolean;
  email_address?: string;
  status?: "connected" | "needs_reconnect" | "disconnected";
  last_synced_at?: string | null;
  last_sync_error?: string;
};

export type MailRow = {
  id: number;
  thread_id: string;
  subject: string;
  from_name: string;
  from_email: string;
  to_emails: string;
  snippet: string;
  sent_at: string;
  is_unread: boolean;
  is_starred: boolean;
  is_sent: boolean;
};

export type MailAttachment = {
  id: number;
  filename: string;
  mime_type: string;
  size_bytes: number;
};

export type MailDetail = MailRow & {
  cc_emails: string;
  body_html: string;
  body_text: string;
  label_ids: string[];
  attachments: MailAttachment[];
};

/** Which slice of the mailbox a list read is asking for.
 *
 *  `unread` is not a folder in Gmail and is one here: it is what the top-bar
 *  popup shows, and asking for "inbox, then filter" would ship sixty rows to
 *  render eight. */
export type MailFolder = "inbox" | "sent" | "unread";

/** The two the screen offers as tabs, with their labels. `unread` is absent on
 *  purpose - it is the popup's view, not a place to browse to. */
export const MAIL_FOLDERS: [MailFolder, string][] = [
  ["inbox", "Inbox"],
  ["sent", "Sent"],
];

/** What the Google round trip left in the address bar when the browser came
 *  back. Only `ready` carries a handoff worth finishing. */
export type MailConnectOutcome = "ready" | "denied" | "no_code" | "bad_state" | "failed";

/** Said to the person, in the terms they can act on. `bad_state` is the one
 *  that reads oddly and the one it is most important not to dress up: it means
 *  the link they finished was not the one this browser started, which is
 *  exactly the case the server refuses on purpose. */
export const MAIL_CONNECT_MESSAGES: Record<MailConnectOutcome, string> = {
  ready: "",
  denied: "Google sign-in was cancelled, so nothing was connected.",
  no_code: "Google did not send anything back. Please try connecting again.",
  bad_state: "That sign-in link has expired or was not started here. Please connect again.",
  failed: "Google could not complete the sign-in. Please try connecting again.",
};

/** Anyone who reads or sends mail says so here, so the top-bar badge can
 *  recount. The same one-line pattern the bell uses for approvals — the badge
 *  and the screen are the only two things that care, and they are never
 *  mounted apart from the shell. */
export const MAIL_CHANGED = "kdps:mail-changed";

export function announceMailChanged() {
  window.dispatchEvent(new Event(MAIL_CHANGED));
}

export const mailApi = {
  status: () => api.get("/mail/status").then((r) => r.data as MailStatus),

  unread: () =>
    api
      .get("/mail/unread")
      .then((r) => r.data as { connected: boolean; unread: number; last_sync_error?: string }),

  /** `sync: false` for reads that must not wait on Google — paging around the
   *  screen after the first load, where a fresh pull buys nothing. */
  list: (box: MailFolder, opts: { q?: string; limit?: number; sync?: boolean } = {}) =>
    api
      .get("/mail/messages", {
        params: {
          box,
          q: opts.q || undefined,
          limit: opts.limit,
          sync: opts.sync === false ? "0" : undefined,
        },
      })
      .then((r) => r.data as { connected: boolean; messages: MailRow[]; last_sync_error?: string }),

  message: (id: number) => api.get(`/mail/messages/${id}`).then((r) => r.data as MailDetail),

  markRead: (id: number) => api.post(`/mail/messages/${id}/read`).then(() => undefined),

  send: (body: { to: string; cc?: string; subject?: string; body: string; reply_to?: number }) =>
    api.post("/mail/send", body).then(() => undefined),

  /** The connect URL is fetched, then the *browser* is navigated to it. It
   *  cannot be a redirect the XHR follows — Google's consent screen has to
   *  appear in the address bar, not inside an axios response. */
  connect: async () => {
    const { data } = await api.get("/mail/connect");
    window.location.href = data.url as string;
  },

  /** The third step of connecting, and the reason there are three.
   *
   *  Google's redirect lands on the API unauthenticated, so it cannot be
   *  allowed to attach anything — a link like that can be sent to somebody
   *  else, and whoever finished it would have *their* mailbox hung off the
   *  sender's login. It hands back a one-time handoff instead, and this call
   *  carries our own JWT, so the server can insist that the person finishing is
   *  the person who started. */
  complete: (handoff: string) =>
    api.post("/mail/complete", { handoff }).then((r) => r.data as MailStatus),

  disconnect: () => api.post("/mail/disconnect").then(() => undefined),

  /** Attachments are streamed by the API, which needs the bearer token — so the
   *  bytes are fetched as a blob and handed to the browser, rather than pointed
   *  at with a bare <a href>, which would arrive unauthenticated and 401. */
  downloadAttachment: async (id: number, filename: string) => {
    const response = await api.get(`/mail/attachments/${id}`, { responseType: "blob" });
    const url = URL.createObjectURL(response.data as Blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  },
};

/** "14 Mar, 4:20 pm" for anything older than today, "4:20 pm" for today.
 *
 *  Explicitly Asia/Kolkata, not the browser's zone. A device left on UTC — and
 *  they exist on shop counters — would otherwise date this morning's mail as
 *  yesterday's, the same trap `tillToday()` exists for on the POS side. */
export function fmtMailWhen(iso: string): string {
  const when = new Date(iso);
  const zone = "Asia/Kolkata";
  const dayOf = (d: Date) => d.toLocaleDateString("en-IN", { timeZone: zone });
  const time = when.toLocaleTimeString("en-IN", {
    timeZone: zone,
    hour: "numeric",
    minute: "2-digit",
  });
  if (dayOf(when) === dayOf(new Date())) return time;
  return `${when.toLocaleDateString("en-IN", {
    timeZone: zone,
    day: "numeric",
    month: "short",
  })}, ${time}`;
}

/** Who a row is "from" as far as the reader is concerned: the sender for
 *  received mail, the recipient for something they sent. A Sent folder listing
 *  everybody's own name down the left is useless. */
export function counterparty(row: MailRow): string {
  if (row.is_sent) return row.to_emails || "(no recipient)";
  return row.from_name || row.from_email || "(unknown sender)";
}

export function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
