// Governed configuration, as the two ticket-04 screens read it (design §5.3
// ConfigPayload, E085-E089, change PRD §14.4/§14.7).
//
// A configuration *line* is a draft; approving it publishes an immutable
// *version*. The draft's own state (draft/submitted/approved) is not the state
// of what is in force, so every read here carries `versions[]` — each approved
// version with its real state (effective, scheduled, ended, withdrawn) and its
// dates. A document that pinned a version names it by UUID; `pinnedLabel`
// turns that into "version 3" without ever moving the pin.
import { useEffect, useRef, useState } from "react";

import { api, apiErrorMessage } from "./api";
import type { paths } from "./api-schema";
import { useResourceList, type Page, type ResourceDTO } from "./goodsScreen";

/** The registered configuration kinds (backend `masters.goods_services.CONFIG_KINDS`). */
export const CONFIG_KINDS = [
  "vocabulary",
  "identity_profile",
  "profile",
  "rates",
  "tax_rates",
  "approval",
  "reasons",
  "series",
  "barcode_range",
  "label",
  "notifications",
  "workflow",
  "non_trading",
  "business_profile",
  // The approved calendar governs working-day deadlines and independent reviews.
  "working_calendar",
  "sell_policy",
] as const;

export type ConfigKind = (typeof CONFIG_KINDS)[number];

export const CONFIG_KIND_LABEL: Record<string, string> = {
  vocabulary: "Vocabulary",
  identity_profile: "SKU identity profile",
  profile: "PT profile",
  rates: "Rates",
  tax_rates: "Tax rules",
  approval: "Approval policy",
  reasons: "Reason lists",
  series: "Document series",
  barcode_range: "Barcode range",
  label: "Label settings",
  notifications: "Notifications",
  workflow: "Workflow switches",
  non_trading: "Non-trading declaration",
  business_profile: "Business profile",
  working_calendar: "Working calendar",
  sell_policy: "Selling policy",
};

export interface ConfigScope {
  scope_kind: "tenant" | "entity" | "sites" | "sbus" | "brands";
  entity_id?: string | null;
  site_ids: string[];
  sbu_ids: string[];
  brand_ids: string[];
  purposes?: string[];
}

export const TENANT_SCOPE: ConfigScope = {
  scope_kind: "tenant",
  entity_id: null,
  site_ids: [],
  sbu_ids: [],
  brand_ids: [],
  purposes: [],
};

/** One approved version and the state it is really in right now. */
export interface VersionState {
  id: string;
  version: number;
  state: "effective" | "scheduled" | "ended" | "withdrawn";
  effective_from: string;
  effective_to: string | null;
  withdrawn_at: string | null;
  withdrawn_reason: string | null;
}

export interface ImpactItem {
  resource_key: string;
  effect: string;
  official_unchanged: boolean;
}

export interface Backdate {
  is_backdated: boolean;
  impact: ImpactItem[];
  blocked: { code: string; message: string; issues: { code: string; message: string }[] } | null;
}

/** May a backdated change be sent for approval?
 *
 *  Three separate reasons it may not, kept apart on purpose: the server has
 *  already refused this start (a backdate cannot re-time an approved version);
 *  or the start is in the past and some affected document has not been read
 *  yet (change PRD §14.5 M5). A start that is not in the past, and a backdate
 *  whose impact list is genuinely empty, block nothing — there is nothing to
 *  review, and pretending otherwise would only train people to tick. */
export function backdateBlocksSubmit(
  backdate: Backdate | undefined,
  reviewed: Set<string>,
): boolean {
  if (!backdate) return false;
  if (backdate.blocked) return true;
  if (!backdate.is_backdated) return false;
  return backdate.impact.some((item) => !reviewed.has(item.resource_key));
}

export interface ConfigData {
  kind: string;
  scope: ConfigScope;
  payload: Record<string, unknown>;
  effective_from: string;
  effective_to: string | null;
  versions: VersionState[];
  /** Detail read only — E088 step 10's impact list, previewed before submission. */
  backdate?: Backdate;
}

export interface IdentityProfilePayload {
  family: string;
  distinguishing_dimensions: string[];
  size_dimension: string;
  colour_dimension: string | null;
  grade_dimension: string | null;
  allowed_size_values: string[];
  allowed_colour_values: string[];
  allowed_grade_values: string[];
}

export function useConfigurations(kind?: string) {
  const query = kind ? `/goods-v1/masters/configurations?kind=${encodeURIComponent(kind)}` : null;
  return useResourceList<ConfigData>(query);
}

/** Every approved version of these lines, newest first, with its line's id. */
export function allVersions(
  items: ResourceDTO<ConfigData>[],
): { draftId: string; kind: string; version: VersionState }[] {
  return items
    .flatMap((row) =>
      (row.data.versions ?? []).map((version) => ({
        draftId: row.id,
        kind: row.data.kind,
        version,
      })),
    )
    .sort((a, b) => b.version.effective_from.localeCompare(a.version.effective_from));
}

/** "version 3", for a document showing which version it pinned. `null` when the
 *  pinned id is not among the versions we can see — the screen then shows the id
 *  itself rather than inventing a number. */
export function pinnedLabel(
  items: ResourceDTO<ConfigData>[],
  versionId: string | null | undefined,
): string | null {
  if (!versionId) return null;
  for (const row of items) {
    for (const version of row.data.versions ?? []) {
      if (version.id === versionId) {
        return `version ${version.version}`;
      }
    }
  }
  return null;
}

export interface EffectiveProfile {
  draftId: string;
  versionId: string;
  version: number;
  payload: IdentityProfilePayload;
}

/** The approved SKU identity profiles in force, with their payloads.
 *
 *  Two round trips by necessity: the list says which versions exist and which
 *  are effective, and only `GET .../{id}?version=N` answers with that exact
 *  immutable version's payload. Reading the *draft's* payload instead would
 *  show whatever is being edited, not what SKUs are actually governed by. */
export function useEffectiveIdentityProfiles() {
  const list = useConfigurations("identity_profile");
  const [profiles, setProfiles] = useState<EffectiveProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [failure, setFailure] = useState("");
  const wanted = allVersions(list.items)
    .filter((entry) => entry.version.state === "effective")
    .map((entry) => `${entry.draftId}:${entry.version.version}:${entry.version.id}`)
    .join(",");

  useEffect(() => {
    if (list.loading) return;
    const entries = wanted ? wanted.split(",") : [];
    if (entries.length === 0) {
      setProfiles([]);
      setLoading(false);
      return;
    }
    let live = true;
    setLoading(true);
    setFailure("");
    Promise.all(
      entries.map(async (entry) => {
        const [draftId, version, versionId] = entry.split(":");
        if (!draftId || !versionId || !version || !Number.isSafeInteger(Number(version))) {
          throw new Error("Invalid effective identity profile reference");
        }
        const { data } = await api.get<ResourceDTO<ConfigData>>(
          `/goods-v1/masters/configurations/${draftId}?version=${version}`,
        );
        return {
          draftId,
          versionId,
          version: Number(version),
          payload: data.data.payload as unknown as IdentityProfilePayload,
        };
      }),
    )
      .then((found) => live && setProfiles(found))
      .catch((e) => live && setFailure(apiErrorMessage(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [wanted, list.loading]);

  return {
    profiles,
    loading: list.loading || loading,
    denied: list.denied,
    failure: list.failure || failure,
  };
}

type ControlledQuery = NonNullable<
  paths["/api/goods-v1/ptmapper/controlled"]["get"]["parameters"]["query"]
>;

/** VocabularyChoiceDTO, taken from E226's own declared 200 body. */
export type ControlledValue = NonNullable<
  paths["/api/goods-v1/ptmapper/controlled"]["get"]["responses"][200]["content"]["application/json"]["items"]
>[number];

const VALUE_PAGE = 50;

/** Approved vocabulary values for one dimension (E226), searched and paged.
 *
 *  The value's stable `id` only comes from here: it is derived from the
 *  dimension and key, and the configuration payload carries the key alone.
 *
 *  A null `profileVersionId` reads the governed vocabulary itself, which is how
 *  the *first* profile is chosen: it cannot name a profile that does not exist
 *  yet. The server refuses a dimension nothing governs, so an empty `values`
 *  here always means "this dimension is in force and nothing matched" — never
 *  "the read failed", which is why `failure` must be shown rather than treated
 *  as an empty list.
 *
 *  A page arriving for a search or dimension the caller has already moved on
 *  from is dropped: only the newest request may write the list. */
export function useControlledValues(
  profileVersionId: string | null,
  dimension: string | null,
  search = "",
) {
  const key = dimension ? `${profileVersionId ?? ""}${dimension}${search}` : null;
  const [values, setValues] = useState<ControlledValue[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [more, setMore] = useState(0);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState("");
  const wanted = useRef<string | null>(null);

  // A new dimension or search term is a new list, not more of the old one.
  useEffect(() => {
    setValues([]);
    setCursor(null);
    setMore(0);
  }, [key]);

  useEffect(() => {
    if (!key || !dimension) {
      wanted.current = null;
      setFailure("");
      setLoading(false);
      return;
    }
    const query: ControlledQuery = { dimension, limit: VALUE_PAGE };
    if (profileVersionId) query.profile_version_id = profileVersionId;
    if (search) query.q = search;
    if (more > 0 && cursor) query.cursor = cursor;
    const url = `/goods-v1/ptmapper/controlled?${new URLSearchParams(
      Object.entries(query).map(([k, v]) => [k, String(v)]),
    )}`;
    const mine = `${key}${more}`;
    wanted.current = mine;
    setLoading(true);
    setFailure("");
    api
      .get<Page<ControlledValue>>(url)
      .then((r) => {
        if (wanted.current !== mine) return;
        const page = r.data.items ?? [];
        setValues((held) => (more > 0 ? [...held, ...page] : page));
        setCursor(r.data.next_cursor ?? null);
      })
      .catch((e) => {
        if (wanted.current !== mine) return;
        // A failed read leaves no stale list behind for a caller to act on.
        setValues([]);
        setCursor(null);
        setFailure(apiErrorMessage(e));
      })
      .finally(() => {
        if (wanted.current === mine) setLoading(false);
      });
    // `cursor` is read, never depended on: it changes with every page, and
    // depending on it would fetch the next page the moment one arrives.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, more]);

  return {
    values,
    loading,
    failure,
    hasMore: cursor !== null,
    showMore: () => setMore((n) => n + 1),
  };
}

export type VocabularyRead = ReturnType<typeof useControlledValues>;

/** What a person chose for one dimension's allowed-value list. `restricted`
 *  false is the explicit "every approved value" choice, not a default. */
export interface AllowedChoice {
  restricted: boolean;
  ids: string[];
}

/** The `allowed_*_values` list a profile draft should carry, or why it cannot
 *  be decided yet (GSA-04A).
 *
 *  The server reads an empty list as "this profile restricts nothing". That is
 *  a real, approved choice — and precisely because it is, a screen may not
 *  arrive at it by accident. So a read that failed or has not finished blocks
 *  instead of answering `[]`, and narrowing needs at least one value left.
 *
 *  `read` is one page of a searched, paged list, so it is evidence about the
 *  ids it actually carries and about no others. A retired value it shows is
 *  dropped — retired values stay readable on the SKUs that used them, but a new
 *  profile cannot select one. An id it does not mention was ticked on another
 *  page or under another search term, and is kept: forgetting it because the
 *  list has since been narrowed would quietly widen the profile. */
export function allowedValues(
  choice: AllowedChoice,
  read: VocabularyRead,
): { ids: string[] } | { blocked: string } {
  if (read.loading) return { blocked: "The approved vocabulary is still loading." };
  if (read.failure)
    return { blocked: `The approved vocabulary could not be read: ${read.failure}` };
  if (!choice.restricted) return { ids: [] };
  const retired = new Set(read.values.filter((v) => v.state !== "effective").map((v) => v.id));
  const ids = [...new Set(choice.ids)].filter((id) => !retired.has(id));
  if (ids.length === 0) {
    return { blocked: "Choose at least one value, or allow every approved value." };
  }
  return { ids };
}
