// The monitoring screen's wire shapes and the small judgements it makes about
// them (ticket 18, design §8.2 "Monitoring/export/recovery", E189-E192).
//
// Kept out of the screen so the meanings are testable on their own: what each
// health code is *called* in plain words, and what an export job's state means
// to the person waiting for a file.

import type { operations } from "./api-schema";

/** E192's body, as the generated client describes it: the results the worker's
 *  scheduled pass recorded. Loading it never runs a check (ticket 19). */
export type HealthBody =
  operations["goods_v1_operations_health_retrieve"]["responses"][200]["content"]["application/json"];

/** One E192 check: what was observed, the threshold, when it was actually
 *  checked, what it covered, its last recorded pass and the issue to open. */
export type HealthItem = HealthBody["items"][number];

/** E189/E190's ExportJobDTO. */
export interface ExportJob {
  id: string;
  state: "pending" | "running" | "confirmed" | "failed" | "unknown";
  progress: number;
  as_of: string | null;
  sha256: string | null;
  download_url: string | null;
  error_code: string | null;
}

/** E191's CommandStatusDTO. */
export interface CommandStatus {
  status: "not_recorded" | "succeeded" | "refused";
  http_status: number | null;
  result: Record<string, unknown> | null;
}

// The health codes the server sends today. A code this list does not know is
// still shown — with its own code as the label — because hiding an unknown
// check is exactly the wrong failure for a monitoring screen.
const HEALTH_LABELS: Record<string, string> = {
  outbox_backlog: "Jobs waiting",
  outbox_failed: "Jobs that failed",
  anchor_lag: "Evidence anchored off-box",
  series_capacity: "Numbering headroom",
  unconfirmed_uploads: "Uploads never confirmed",
  exceptions_overdue: "Exceptions past their date",
  evidence_store: "Write-once store",
  evidence_verifier: "Evidence verifier",
  email_provider: "Email provider",
  notification_provider: "Notification provider",
};

export function healthLabel(code: string): string {
  return HEALTH_LABELS[code] ?? code;
}

/** Worst first: attention, then not measured, then passing. */
const STATE_RANK: Record<HealthItem["state"], number> = { attention: 0, unavailable: 1, ok: 2 };

/** Sort: anything needing attention first, then what is not measured, then
 *  alphabetically by label. */
export function sortHealth(items: HealthItem[]): HealthItem[] {
  return [...items].sort((a, b) => {
    if (a.state !== b.state) return STATE_RANK[a.state] - STATE_RANK[b.state];
    return healthLabel(a.code).localeCompare(healthLabel(b.code));
  });
}

export function attentionCount(items: HealthItem[]): number {
  return items.filter((item) => item.state === "attention").length;
}

export function healthStateLabel(state: HealthItem["state"]): string {
  switch (state) {
    case "attention":
      return "Needs attention";
    case "unavailable":
      // Never "OK": nothing has measured it.
      return "Not measured";
    case "ok":
      return "OK";
  }
}

/** Where a check's owned exception opens: the shared centre, on that row. The
 *  link grants nothing — the centre still shows only what the reader may see. */
export function issuePath(issueId: string): string {
  return `/action-needed?show=exceptions&open=${encodeURIComponent(issueId)}`;
}

/** Is this job still going to change on its own? */
export function isRunning(job: ExportJob): boolean {
  return job.state === "pending" || job.state === "running";
}

export function exportStateLabel(job: ExportJob): string {
  switch (job.state) {
    case "pending":
      return "Waiting to start";
    case "running":
      return "Building the file";
    case "confirmed":
      if (job.download_url) return "Ready";
      // E190 step 5 reauthorises on every poll: the file is finished, but this
      // reader no longer covers every cell or field in it, so there is no link.
      return job.error_code === "ACTION_DENIED"
        ? "Finished — you may no longer download it"
        : "Finished, file not linked";
    case "failed":
      return `Failed${job.error_code ? ` — ${job.error_code}` : ""}`;
    case "unknown":
      return "Outcome unknown";
  }
}

export function commandStatusLabel(status: CommandStatus): string {
  switch (status.status) {
    case "succeeded":
      return "Committed";
    case "refused":
      return "Refused";
    case "not_recorded":
      // Deliberately not "it did not happen": E191 step 5 is explicit that an
      // unknown key is no guarantee a concurrent request cannot still commit.
      return "No record of it — send the same command id again to be sure";
  }
}
