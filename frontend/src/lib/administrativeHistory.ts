// E247 administrative history (ticket 02C): the plain words an owner reads
// for an event, and the only way a history request path is built. Pure
// helpers, so the wording and the path are tested without a screen.

export type MasterHistoryFamily = "entity" | "registration";

/** A `?history=` value names one entity or registration by its numeric id, or nothing. */
export function parseMasterHistory(
  value: string | null,
): { family: MasterHistoryFamily; id: string } | null {
  const match = /^(entity|registration):(\d+)$/.exec(value ?? "");
  const family = match?.[1];
  const id = match?.[2];
  return (family === "entity" || family === "registration") && id ? { family, id } : null;
}

/** What E247 reads: 02C's sites and masters, 03D's people, logins and roles. */
export type HistorySubjectKind = "site" | "master" | "staff" | "user" | "role";

/** The subject id is one path segment, never a path of its own. */
export function historyPath(subjectKind: HistorySubjectKind, subjectId: string): string {
  return `/goods-v1/history/${subjectKind}/${encodeURIComponent(subjectId)}`;
}

/**
 * After a retirement, the record's history opens only for a reader holding
 * `audit.view`; anyone else would get a refused history card (02C-S5).
 */
export function openHistoryAfterRetire(
  canReadHistory: boolean,
  id: string,
  onHistory: (id: string) => void,
): void {
  if (canReadHistory) onHistory(id);
}

const MASTER_KIND: Record<string, string> = {
  entity: "Legal entity",
  registration: "Registration",
  site: "Site",
  location: "Location",
  sbu: "SBU",
  tenant: "Tenant profile",
};

const MASTER_CHANGE: Record<string, string> = {
  created: "created",
  changed: "changed",
  retired: "retired",
};

const SITE_OPERATION: Record<string, string> = {
  opening_setup: "Opening setup",
  goods: "Goods readiness",
  sell: "Selling readiness",
  non_trading: "Non-trading status",
  closing: "Closing",
  closed: "Closure",
};

const COMMAND: Record<string, string> = {
  "org.site.manage": "Site setup change",
  "org.site.lifecycle.run": "Site readiness check",
  "org.site.lifecycle.approve": "Site readiness decision",
  "master.retire": "Retirement",
  "org.entity.manage": "Organisation record change",
  "org.tenant.manage": "Tenant profile change",
  // Ticket 03D: people, logins and roles.
  "staff.create": "Person added",
  "staff.update": "Person details change",
  "staff.assign": "Site assignment change",
  "staff.retire": "Person retirement",
  "access.user.create": "Login creation",
  "access.user.update": "Login change",
  "access.grant.change": "Roles and grants change",
  "access.role.create": "Role creation",
  "access.role.update": "Role change",
  "access.role.access": "Role maximum change",
  "access.privileged_change.review": "Privileged change acknowledgement",
};

/** The privileged kinds a person, login or role history can show (`accounts.actions`). */
const PRIVILEGED_ACCESS_KINDS = new Set([
  "access.user.create",
  "access.user.update",
  "access.grant.change",
  "access.role.create",
  "access.role.update",
  "access.role.access",
]);

/** The one privileged change on the review screen; the owned follow-up links here too. */
export function privilegedChangePath(changeId: string): string {
  return `/setup/people-access?${new URLSearchParams({ panel: "privileged", change: changeId })}`;
}

/**
 * A succeeded privileged change in history opens its review for a reader who may
 * review, and an acknowledgement opens the change it acknowledged (its recorded
 * `audit_event_id`). Reading history is never the review itself (ticket 03D).
 */
export function privilegedReviewLink(
  event: {
    id: string;
    event_kind: string;
    outcome: string;
    after?: { field: string; redacted: boolean; value?: unknown }[] | null;
  },
  canReview: boolean,
): { to: string; label: string } | null {
  if (!canReview || event.outcome !== "succeeded") return null;
  if (PRIVILEGED_ACCESS_KINDS.has(event.event_kind)) {
    return { to: privilegedChangePath(event.id), label: "Open its review" };
  }
  if (event.event_kind === "access.privileged_change.review") {
    const reviewed = event.after?.find((value) => value.field === "audit_event_id");
    if (reviewed && !reviewed.redacted && typeof reviewed.value === "string") {
      return { to: privilegedChangePath(reviewed.value), label: "Open the acknowledged change" };
    }
  }
  return null;
}

const COMMAND_OUTCOME: Record<string, string> = {
  succeeded: "recorded",
  refused: "refused",
  failed: "failed",
};

/** What happened, in plain words; never the internal event code. */
export function historyActionLabel(eventKind: string, outcome: string): string {
  const [family, kind, change] = eventKind.split(".");
  if (family === "master" && kind && change && MASTER_KIND[kind] && MASTER_CHANGE[change]) {
    return `${MASTER_KIND[kind]} ${MASTER_CHANGE[change]}`;
  }
  if (
    family === "site" &&
    kind &&
    SITE_OPERATION[kind] &&
    (change === "approved" || change === "revoked")
  ) {
    return `${SITE_OPERATION[kind]} ${change}`;
  }
  const command = COMMAND[eventKind] ?? "Recorded change";
  return `${command} ${COMMAND_OUTCOME[outcome] ?? outcome}`;
}

/** The server names the actor only where the reader's grants already reveal that person. */
export function historyActorLabel(actorName: string | null | undefined): string {
  return actorName || "Restricted person";
}
