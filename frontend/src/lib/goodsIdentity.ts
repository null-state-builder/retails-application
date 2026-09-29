/** What a scanned or typed code turns out to be (E090/E091, GSA-T04/GSA-T05).
 *
 *  A count screen must never invent a SKU. It asks the server what the code on
 *  the carton means, and the server answers one of three things: this is exactly
 *  one product, this is one of several and somebody has to choose, or nothing
 *  matches at all. Only the first can be recorded straight away; the second needs
 *  an authorised choice bound to that exact scan; the third stays honestly
 *  unidentified, described in words, with no SKU at all.
 *
 *  Everything here is pure. The calls themselves live on the screens. */

/** One product a code could mean, as the server describes it to a human. */
export interface IdentityCandidate {
  sku_id: string;
  brand: string;
  style: string;
  size: string;
  colour?: string;
  grade?: string;
}

export type IdentityResult = "resolved" | "ambiguous" | "unknown";

export interface IdentityResolution {
  result: IdentityResult;
  /** Covers exactly the candidate set that was shown. An E091 pick quotes it back
   *  so the server can refuse a choice made against a stale set. */
  candidate_hash: string;
  candidates: IdentityCandidate[];
  chosen_sku_id: string | null;
  issues: unknown[];
}

/** The AliasContext every lookup and pick carries: where, whose code, when, and
 *  under which governed identity profile it is being read. */
export interface AliasContext {
  site_id: string;
  issuer_key: string;
  alias_type: "barcode" | "vendor_code" | "generated";
  as_of: string;
  profile_version_id: string;
}

export const ALIAS_TYPES: { value: AliasContext["alias_type"]; label: string }[] = [
  { value: "barcode", label: "Vendor's barcode" },
  { value: "vendor_code", label: "Vendor's item code" },
  { value: "generated", label: "A label we printed" },
];

/** One product in the words a receiver reads off the carton. */
export function candidateLabel(candidate: IdentityCandidate): string {
  const parts = [candidate.brand, candidate.style, candidate.size];
  if (candidate.colour) parts.push(candidate.colour);
  if (candidate.grade) parts.push(candidate.grade);
  return parts.filter(Boolean).join(" · ");
}

/** What the screen says about a lookup, in the reader's words. Never a code. */
export function resolutionMessage(resolution: IdentityResolution): string {
  if (resolution.result === "resolved") return "Matched one product.";
  if (resolution.result === "ambiguous") {
    return `This code matches ${resolution.candidates.length} products. Choose which one it is.`;
  }
  return "No product matches this code. Describe the goods below; they are counted as unidentified.";
}

/** The single SKU a lookup settled on, or null when nobody may settle it yet.
 *
 *  A resolved lookup has exactly one candidate and that is the answer. An
 *  ambiguous one has no answer until a person chooses, and an unknown one has
 *  none at all — returning a "best guess" for either is the whole thing GSA-T05
 *  forbids. */
export function resolvedSkuId(resolution: IdentityResolution | null): string | null {
  if (!resolution || resolution.result !== "resolved") return null;
  return resolution.chosen_sku_id ?? resolution.candidates[0]?.sku_id ?? null;
}

/** Whether this row still needs a person to choose between products. */
export function needsChoice(
  resolution: IdentityResolution | null,
  chosen: string | null,
): boolean {
  return Boolean(resolution && resolution.result === "ambiguous" && !chosen);
}

/** The identity to record for a counted row: the resolved product, the product a
 *  person chose, or nothing. A chosen product is recorded as a pick bound to the
 *  scan, not written onto the observation, so the observation stays exactly what
 *  the counter saw. */
export function identityForRow(
  resolution: IdentityResolution | null,
  chosen: string | null,
): { sku_id: string | null; pick: { chosen_sku_id: string; candidate_hash: string } | null } {
  const resolved = resolvedSkuId(resolution);
  if (resolved) return { sku_id: resolved, pick: null };
  if (resolution && resolution.result === "ambiguous" && chosen) {
    return { sku_id: null, pick: { chosen_sku_id: chosen, candidate_hash: resolution.candidate_hash } };
  }
  return { sku_id: null, pick: null };
}

/** Configuration versions, as the configuration read answers them. */
export interface ConfigVersionRow {
  id: string;
  state: string;
  effective_from?: string | null;
  effective_to?: string | null;
}

/** One identity profile a code can be read under: which version, and the product
 *  family it is for. A receiver recognises the family, never the version id. */
export interface IdentityProfileChoice {
  id: string;
  family: string;
}

/** One configuration draft as the configuration read answers it. */
export interface ConfigRow {
  data: { payload?: { family?: string }; versions?: ConfigVersionRow[] };
}

/** Whether this version is the one actually in force at `now`.
 *
 *  "Effective" is the server's own word for it, so this goes by that state and
 *  by the period actually containing `now` — never simply the newest row, which
 *  is how ticket 04's wizard once offered a person a version that was not yet in
 *  force. */
export function inForce(version: ConfigVersionRow, now: Date): boolean {
  if (version.state !== "effective") return false;
  const from = version.effective_from ? new Date(version.effective_from) : null;
  const to = version.effective_to ? new Date(version.effective_to) : null;
  if (from && from > now) return false;
  if (to && to <= now) return false;
  return true;
}

/** Every identity profile in force, one per product family.
 *
 *  A tenant may run several at once — apparel and footwear are different things
 *  and say so — so this returns all of them rather than picking. A screen with
 *  one uses it; a screen with several has to ask, because reading a code under
 *  the wrong family answers "no product matches" and means nothing of the sort. */
export function effectiveProfiles(rows: ConfigRow[], now: Date): IdentityProfileChoice[] {
  const out: IdentityProfileChoice[] = [];
  for (const row of rows) {
    const live = (row.data.versions ?? []).find((version) => inForce(version, now));
    if (live) out.push({ id: live.id, family: row.data.payload?.family ?? "Unnamed family" });
  }
  return out;
}
