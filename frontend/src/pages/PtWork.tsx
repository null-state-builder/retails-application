// PT Work (OPS-17, store and warehouse operations PRD §5.1 and §5.4): the
// warehouse's PT work in one place, as three tabs.
//
//   · To prepare - the receipts whose good pieces no PT covers yet, and every
//     PT still a draft. Opening one opens the OPS-16 grid (`PtEditor`), the
//     same editor the delivery workflow's PT step uses.
//   · To approve - today's approvals queue (`PtApprovePanel`), the same panel
//     the delivery workflow's PT-approval step uses.
//   · Mapping rules - the one review queue and the one proposals list of the
//     rulebook (E227/E228). A preparer proposes a KDPS value for a brand's word;
//     the product-master owner confirms or rejects it (E230/E231). A proposed
//     rule changes nothing until it is confirmed. Mapping rules also lists the
//     new items PTs proposed (OPS-17A); the product-master owner confirms or
//     rejects each one there, through the approvals decision command (E234).
//
// Nothing here is a new record or a new rule: each tab is a way in to the
// records and commands that already exist, and the server decides everything.
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Check, ClipboardList, Tags, X } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta } from "../lib/api";
import { formatDateTime } from "../lib/format";
import {
  Denied,
  Feedback,
  Field,
  hold,
  listState,
  useAllPages,
  useGoodsFetch,
  useStepUp,
  type Page,
  type ResourceDTO,
} from "../lib/goodsScreen";
import { siteName, type InboxItem } from "../lib/goodsReceiving";
import {
  ITEM_REJECT_REASONS,
  PT_WORK_TABS,
  RULE_DECIDE_ACTION,
  RULE_PROPOSE_ACTIONS,
  contentHash,
  describingText,
  issuerLabel,
  itemDecisions,
  ptWorkPath,
  ptWorkTabs,
  resolvePtWorkTab,
  toPrepareRows,
  type ItemProposal,
  type MappingChoice,
  type PtSummary,
  type PtWorkTab,
} from "../lib/ptWork";
import { GrnStart, PtEditor } from "./PtPrepare";
import { PtApprovePanel } from "./PtApprovals";
import "./PtScreens.css";

// ---------------------------------------------------------------------------
// To prepare
// ---------------------------------------------------------------------------

function ToPrepare({
  onOpenGrn,
  onOpenPt,
}: {
  onOpenGrn: (grnId: string) => void;
  onOpenPt: (ptId: string) => void;
}) {
  const { session } = useAuth();
  const mySites = session?.sites ?? [];
  // Every site this person may read: the inbox answers each delivery's next
  // step, so "waiting on its PT" is the server's word, not a guess made here.
  // Only a receipt preparer has receipts to prepare; an opening preparer's work
  // is the drafts below.
  const inbox = useAllPages<InboxItem>(
    hold(session, "pt.prepare") ? "/goods-v1/inbound/inbox?view=pending&limit=100" : null,
  );
  const pts = useAllPages<PtSummary>("/goods-v1/ptmapper/files?limit=100");
  const rows = useMemo(() => toPrepareRows(inbox.items, pts.items), [inbox.items, pts.items]);

  const state = listState(
    {
      loading: inbox.loading || pts.loading,
      failure: inbox.failure || pts.failure,
      empty: rows.length === 0,
    },
    "No receipt is waiting for its PT, and no PT is still a draft.",
  );

  return (
    <div data-testid="pt-work-prepare">
      <h3 className="h3">
        <ClipboardList size={15} /> To prepare
      </h3>
      <p className="pt-hint">
        Receipts with good pieces no PT covers yet, and every PT still a draft. Open one to prepare
        it on the grid; held pieces are never offered.
      </p>
      {state ?? (
        <div className="table-wrap">
          <table className="data" data-testid="pt-work-prepare-table">
            <caption className="sr-only">PT work waiting for a preparer</caption>
            <thead>
              <tr>
                <th scope="col">What</th>
                <th scope="col">GRN</th>
                <th scope="col">PT</th>
                <th scope="col">Site</th>
                <th scope="col">Since</th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key} data-testid={`pt-work-row-${row.grnId ?? row.ptId}`}>
                  <td>{row.label}</td>
                  <td className="mono">{row.grnNumber ?? "—"}</td>
                  <td className="mono">
                    {row.ptId ? (row.ptNumber ?? "Draft") : <span className="muted">Not started</span>}
                  </td>
                  <td>{siteName(mySites, row.siteId)}</td>
                  <td>{formatDateTime(row.since)}</td>
                  <td>
                    <button
                      className="btn btn-sm"
                      onClick={() => (row.ptId ? onOpenPt(row.ptId) : onOpenGrn(row.grnId ?? ""))}
                      data-testid={`pt-work-open-${row.grnId ?? row.ptId}`}
                    >
                      {row.ptId ? "Open the grid" : "Start the PT"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Mapping rules
// ---------------------------------------------------------------------------

interface ConfigSummary {
  effective_from: string;
  payload?: { family?: string };
  versions: { id: string; state: string }[];
}

interface VocabRow {
  id: string;
  label: string;
  state: string;
}

interface NamedMaster {
  code: string;
  name: string;
}

/** The version a profile pins, as the PT start reads it: its effective version,
 *  else its first. */
function versionOf(config: ConfigSummary): string | null {
  const effective = config.versions.find((v) => v.state === "effective");
  return (effective ?? config.versions[0])?.id ?? null;
}

/** The values a waiting word may be mapped to, for its own dimension. */
function useTargets(dimension: string, profileVersionId: string) {
  const vocabulary = useAllPages<VocabRow>(
    dimension === "brand" || dimension === "vendor"
      ? null
      : `/goods-v1/ptmapper/controlled?${new URLSearchParams({
          dimension,
          profile_version_id: profileVersionId,
          limit: "100",
        }).toString()}`,
  );
  const brands = useAllPages<ResourceDTO<NamedMaster>>(
    dimension === "brand" ? "/goods-v1/masters/brands?limit=100" : null,
  );
  const vendors = useAllPages<ResourceDTO<NamedMaster>>(
    dimension === "vendor" ? "/goods-v1/vendors?limit=100" : null,
  );
  if (dimension === "brand") {
    return brands.items.map((row) => ({ id: String(row.id), label: row.data.name }));
  }
  if (dimension === "vendor") {
    return vendors.items.map((row) => ({ id: String(row.id), label: row.data.name }));
  }
  return vocabulary.items
    .filter((row) => row.state !== "retired")
    .map((row) => ({ id: row.id, label: row.label }));
}

function ProposeTarget({
  row,
  profileVersionId,
  busy,
  onPropose,
}: {
  row: MappingChoice;
  profileVersionId: string;
  busy: boolean;
  onPropose: (targetId: string) => void;
}) {
  const targets = useTargets(row.dimension, profileVersionId);
  const [chosen, setChosen] = useState("");
  return (
    <div className="toolbar" style={{ gap: 6 }}>
      <label className="sr-only" htmlFor={`rule-target-${row.id}`}>
        KDPS value for {row.source_key}
      </label>
      <select
        id={`rule-target-${row.id}`}
        className="select"
        value={chosen}
        onChange={(e) => setChosen(e.target.value)}
        data-testid={`rule-target-${row.id}`}
      >
        <option value="">Choose the KDPS value</option>
        {targets.map((target) => (
          <option key={target.id} value={target.id}>
            {target.label}
          </option>
        ))}
      </select>
      <button
        className="btn btn-sm"
        disabled={busy || !chosen}
        onClick={() => onPropose(chosen)}
        data-testid={`rule-propose-${row.id}`}
      >
        Propose
      </button>
    </div>
  );
}

/** The new items PTs proposed that wait for the product-master owner. The
 *  server says who sees each one and who may decide it; deciding runs each of
 *  the item's approvals through E234, the style's first. */
function NewItems() {
  const { session } = useAuth();
  const mySites = session?.sites ?? [];
  const items = useAllPages<ItemProposal>("/goods-v1/masters/item-proposals?limit=100");
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const stepUp = useStepUp();

  async function decide(item: ItemProposal, decision: "approve" | "reject") {
    const reason = reasons[item.id] ?? "";
    if (decision === "reject" && !reason) {
      setError("Choose why the item is rejected first.");
      return;
    }
    setError("");
    setOk("");
    setBusy(true);
    try {
      for (const approval of itemDecisions(item, decision)) {
        await stepUp.guarded(() =>
          api.post(`/goods-v1/approvals/${approval.id}/decide`, {
            decision,
            reviewed_hash: approval.reviewed_hash,
            ...(decision === "reject" ? { reason_code: reason } : {}),
            ...goodsMeta(approval.revision),
          }),
        );
      }
      setOk(
        decision === "approve"
          ? `Confirmed: ${item.style_code} is a product from now on, and its PT row stops waiting.`
          : `Rejected: ${item.style_code}. Its PT row stays blocked until it is given another item.`,
      );
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
      items.reload();
    }
  }

  return (
    <div className="card section-card" data-testid="rules-new-items">
      <h4 className="gr-h4">New items waiting for confirmation</h4>
      {stepUp.dialog}
      <Feedback error={error} ok={ok} />
      {listState(
        { loading: items.loading, failure: items.failure, empty: items.items.length === 0 },
        "No new item is waiting.",
      ) ?? (
        <div className="table-wrap">
          <table className="data" data-testid="rules-new-items-table">
            <caption className="sr-only">New items waiting for the product-master owner</caption>
            <thead>
              <tr>
                <th scope="col">Style</th>
                <th scope="col">Brand</th>
                <th scope="col">Describing values</th>
                <th scope="col">From PT</th>
                <th scope="col">Proposed by</th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              {items.items.map((item) => (
                <tr key={item.id} data-testid={`new-item-${item.id}`}>
                  <td>
                    <b className="mono">{item.style_code}</b>
                    {item.style_is_new && <span className="muted"> (new style)</span>}
                    {item.kind === "style" && <span className="muted"> (style only)</span>}
                  </td>
                  <td>{item.brand_name}</td>
                  <td>{describingText(item) || <span className="muted">None given</span>}</td>
                  <td>
                    {item.pt ? (
                      <Link to={ptWorkPath({ pt: item.pt.id })} data-testid={`new-item-pt-${item.id}`}>
                        {item.pt.number ?? "Draft PT"}
                        {item.pt.site_id ? `, ${siteName(mySites, item.pt.site_id)}` : ""}
                      </Link>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    {item.proposed_by.name || "—"}
                    {item.proposed_at && (
                      <div className="muted">{formatDateTime(item.proposed_at)}</div>
                    )}
                  </td>
                  <td>
                    {item.can_decide ? (
                      <div className="toolbar" style={{ gap: 6 }}>
                        <button
                          className="btn btn-sm btn-primary"
                          disabled={busy}
                          onClick={() => decide(item, "approve")}
                          data-testid={`new-item-confirm-${item.id}`}
                        >
                          <Check size={14} /> Confirm
                        </button>
                        <label className="sr-only" htmlFor={`new-item-reason-${item.id}`}>
                          Why {item.style_code} is rejected
                        </label>
                        <select
                          id={`new-item-reason-${item.id}`}
                          className="select"
                          value={reasons[item.id] ?? ""}
                          onChange={(e) => setReasons({ ...reasons, [item.id]: e.target.value })}
                          data-testid={`new-item-reason-${item.id}`}
                        >
                          <option value="">Reason to reject</option>
                          {ITEM_REJECT_REASONS.map((reason) => (
                            <option key={reason.code} value={reason.code}>
                              {reason.label}
                            </option>
                          ))}
                        </select>
                        <button
                          className="btn btn-sm"
                          disabled={busy || !reasons[item.id]}
                          onClick={() => decide(item, "reject")}
                          data-testid={`new-item-reject-${item.id}`}
                        >
                          <X size={14} /> Reject
                        </button>
                      </div>
                    ) : (
                      <span className="muted">Waiting for the product-master owner</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function MappingRules() {
  const { session } = useAuth();
  const canPropose = RULE_PROPOSE_ACTIONS.some((action) => hold(session, action));
  const canDecide = hold(session, RULE_DECIDE_ACTION);
  const profiles = useGoodsFetch<
    Page<ResourceDTO<ConfigSummary>>,
    { id: string; label: string }[]
  >(
    "/goods-v1/masters/configurations?kind=profile&limit=100",
    (r) =>
      (r.items ?? [])
        .filter((row) => row.state === "approved" && versionOf(row.data))
        .map((row) => ({
          id: versionOf(row.data) ?? "",
          label: `${row.data.payload?.family ?? "Unnamed family"} (since ${row.data.effective_from.slice(0, 10)})`,
        })),
    [],
  );
  const [profileId, setProfileId] = useState("");
  useEffect(() => {
    if (!profileId && profiles.value.length > 0) setProfileId(profiles.value[0].id);
  }, [profiles.value, profileId]);

  const query = profileId
    ? new URLSearchParams({ profile_version_id: profileId, limit: "100" }).toString()
    : "";
  const review = useAllPages<MappingChoice>(query ? `/goods-v1/ptmapper/review?${query}` : null);
  const proposals = useAllPages<MappingChoice>(
    query ? `/goods-v1/ptmapper/proposals?${query}` : null,
  );
  const brands = useAllPages<ResourceDTO<NamedMaster>>("/goods-v1/masters/brands?limit=100");
  const brandName = (id: string) =>
    brands.items.find((row) => String(row.id) === id)?.data.name ?? `Brand ${id}`;

  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const stepUp = useStepUp();

  async function send(path: string, row: MappingChoice, body: Record<string, unknown>, done: string) {
    setError("");
    setOk("");
    setBusy(true);
    try {
      const reviewed = await contentHash(row);
      await stepUp.guarded(() =>
        api.post(path, { ...body, reviewed_hash: reviewed, ...goodsMeta(row.revision) }),
      );
      setOk(done);
      review.reload();
      proposals.reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const words = (row: MappingChoice) =>
    `${row.source_key ?? "—"} (${row.dimension}, ${issuerLabel(row.issuer_key, brandName)})`;

  return (
    <div data-testid="pt-work-rules">
      <h3 className="h3">
        <Tags size={15} /> Mapping rules
      </h3>
      <p className="pt-hint">
        How brands' words become KDPS values, and the new items PTs propose. A preparer proposes a
        value for a word no rule settles, or a new item for a row no item matches; neither applies
        until the product-master owner confirms it here.
      </p>
      {stepUp.dialog}
      <NewItems />
      <Feedback error={error} ok={ok} />
      {profiles.value.length > 1 && (
        <Field id="rules-profile" label="Product family">
          <select
            id="rules-profile"
            className="select"
            value={profileId}
            onChange={(e) => setProfileId(e.target.value)}
            data-testid="rules-profile"
          >
            {profiles.value.map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.label}
              </option>
            ))}
          </select>
        </Field>
      )}
      {!profiles.loading && profiles.value.length === 0 && (
        <p className="muted" data-testid="rules-no-profile">
          No approved pricing profile is in force, so there is no rulebook to review yet.
        </p>
      )}

      <div className="card section-card" data-testid="rules-proposals">
        <h4 className="gr-h4">Proposed, waiting for the product-master owner</h4>
        {listState(
          {
            loading: proposals.loading,
            failure: proposals.failure,
            empty: proposals.items.length === 0,
          },
          "No proposed rule is waiting.",
        ) ?? (
          <div className="table-wrap">
            <table className="data" data-testid="rules-proposals-table">
              <caption className="sr-only">Proposed mapping rules</caption>
              <thead>
                <tr>
                  <th scope="col">The file says</th>
                  <th scope="col">Proposed KDPS value</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {proposals.items.map((row) => (
                  <tr key={row.id} data-testid={`rule-proposal-${row.id}`}>
                    <td>{words(row)}</td>
                    <td>
                      <b>{row.target_label ?? row.target_id ?? "—"}</b>
                    </td>
                    <td>
                      {canDecide ? (
                        <div className="toolbar" style={{ gap: 6 }}>
                          <button
                            className="btn btn-sm btn-primary"
                            disabled={busy}
                            onClick={() =>
                              send(
                                `/goods-v1/ptmapper/proposals/${row.id}/decide`,
                                row,
                                { action: "confirm", reason_code: "CONFIRMED_IN_PT_WORK" },
                                `Confirmed: ${row.source_key} is read as ${row.target_label} from now on.`,
                              )
                            }
                            data-testid={`rule-confirm-${row.id}`}
                          >
                            <Check size={14} /> Confirm
                          </button>
                          <button
                            className="btn btn-sm"
                            disabled={busy}
                            onClick={() =>
                              send(
                                `/goods-v1/ptmapper/proposals/${row.id}/decide`,
                                row,
                                { action: "reject", reason_code: "REJECTED_IN_PT_WORK" },
                                `Rejected: ${row.source_key} is not read as ${row.target_label}.`,
                              )
                            }
                            data-testid={`rule-reject-${row.id}`}
                          >
                            <X size={14} /> Reject
                          </button>
                        </div>
                      ) : (
                        <span className="muted">Waiting for the product-master owner</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card section-card" data-testid="rules-review">
        <h4 className="gr-h4">Words no rule settles yet</h4>
        {listState(
          { loading: review.loading, failure: review.failure, empty: review.items.length === 0 },
          "Every word seen so far has a rule or a proposal.",
        ) ?? (
          <div className="table-wrap">
            <table className="data" data-testid="rules-review-table">
              <caption className="sr-only">Words waiting for a KDPS value</caption>
              <thead>
                <tr>
                  <th scope="col">The file says</th>
                  <th scope="col">KDPS value</th>
                </tr>
              </thead>
              <tbody>
                {review.items.map((row) => (
                  <tr key={row.id} data-testid={`rule-review-${row.id}`}>
                    <td>{words(row)}</td>
                    <td>
                      {canPropose ? (
                        <ProposeTarget
                          row={row}
                          profileVersionId={profileId}
                          busy={busy}
                          onPropose={(targetId) =>
                            send(
                              `/goods-v1/ptmapper/review/${row.id}/resolve`,
                              row,
                              {
                                action: "propose",
                                chosen_value_id: targetId,
                                reason_code: "PROPOSED_IN_PT_WORK",
                              },
                              `Proposed. ${row.source_key} waits for the product-master owner.`,
                            )
                          }
                        />
                      ) : (
                        <span className="muted">Not chosen yet</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

export function PtWorkPage() {
  const { session } = useAuth();
  const [params, setParams] = useSearchParams();
  const shown = ptWorkTabs(session?.actions ?? []);
  const ptId = params.get("pt");
  const grnId = params.get("grn");
  // A PT or a GRN always opens on To prepare, which is where the grid lives.
  const asked = ptId || grnId ? "prepare" : params.get("tab");
  const tab = resolvePtWorkTab(shown, asked);

  function show(next: PtWorkTab) {
    setParams(next === "prepare" ? {} : { tab: next });
  }

  return (
    <div className="page-pad">
      <PageHeader
        lead="Preparing, approving and the rules that map brands' words to KDPS values."
      />
      {tab === null ? (
        <Denied what="PT work" />
      ) : (
        <>
          <div className="page-tabs" role="tablist" data-testid="pt-work-tabs">
            {PT_WORK_TABS.filter((entry) => shown.includes(entry.tab)).map((entry) => (
              <button
                key={entry.tab}
                role="tab"
                aria-selected={entry.tab === tab}
                className={`page-tab ${entry.tab === tab ? "active" : ""}`}
                onClick={() => show(entry.tab)}
                data-testid={`pt-work-tab-${entry.tab}`}
              >
                {entry.label}
              </button>
            ))}
          </div>
          <div className="pt-layout pt-layout-wide" role="tabpanel">
            {tab === "prepare" &&
              (ptId ? (
                <PtEditor ptId={ptId} onClose={() => setParams({})} />
              ) : grnId ? (
                <GrnStart
                  grnId={grnId}
                  onBack={() => setParams({})}
                  onCreated={(id) => setParams({ pt: id })}
                />
              ) : (
                <ToPrepare
                  onOpenGrn={(id) => setParams({ grn: id })}
                  onOpenPt={(id) => setParams({ pt: id })}
                />
              ))}
            {tab === "approve" && <PtApprovePanel />}
            {tab === "rules" && <MappingRules />}
          </div>
        </>
      )}
    </div>
  );
}

export default PtWorkPage;
