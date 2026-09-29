import { useState } from "react";
import { Link } from "react-router-dom";
import { Denied, useGoodsFetch } from "../lib/goodsScreen";

interface ReviewPerson {
  user_id: number;
  display_name: string;
  staff_id: string | null;
  before: { role: string; scope_type: string };
  after: {
    role: string;
    scope: { all_sites: boolean; site_ids: number[]; all_brands: boolean; brand_ids: number[] };
    fields: string[];
  }[];
  exclusions: { code: string; detail: string }[];
}
interface ReviewPage {
  fingerprint: string;
  items: ReviewPerson[];
  next_cursor: number | null;
  totals: {
    people: number;
    candidates: number;
    blocked_people: number;
    orphaned_identities: number;
  };
  activation_blockers: string[];
}

export function AssignmentReconciliationPanel() {
  const [cursor, setCursor] = useState(0);
  const [fingerprint, setFingerprint] = useState("");
  const report = useGoodsFetch<ReviewPage, ReviewPage | null>(
    `/auth/admin/reconciliation?cursor=${cursor}&fingerprint=${fingerprint}`,
    (data) => data,
    null,
  );
  if (report.denied) return <Denied what="access reconciliation" />;
  return (
    <section className="card section-card" data-testid="assignment-reconciliation">
      <h2>Review access migration</h2>
      <p>
        Review proposed roles, scope and exclusions before cutover. Resolve identity and assignment
        issues in each person’s record. Unsupported responsibilities remain inactive.
      </p>
      {report.failure && (
        <p className="warn-note" role="alert">
          {report.failure}
        </p>
      )}
      {report.loading ? (
        <p>Loading review…</p>
      ) : (
        report.value && (
          <>
            <p>
              {report.value.totals.people} people; {report.value.totals.candidates} proposed
              assignments; {report.value.totals.blocked_people} people blocked;{" "}
              {report.value.totals.orphaned_identities} identities without a login.
            </p>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Person</th>
                  <th>Legacy role</th>
                  <th>Proposed assignment</th>
                  <th>Resolution needed</th>
                </tr>
              </thead>
              <tbody>
                {report.value.items.map((person) => (
                  <tr key={person.user_id}>
                    <td>
                      {person.staff_id ? (
                        <Link
                          to={`/setup/people-access?panel=people&person=${person.staff_id}&tab=grants`}
                        >
                          {person.display_name || `Login ${person.user_id}`}
                        </Link>
                      ) : (
                        person.display_name || `Login ${person.user_id}`
                      )}
                    </td>
                    <td>{person.before.role || "Unmapped"}</td>
                    <td>
                      {person.after.map((assignment, index) => (
                        <p key={index}>
                          {assignment.role}: sites{" "}
                          {assignment.scope.all_sites
                            ? "all, including future sites"
                            : assignment.scope.site_ids.join(", ") || "none"}
                          ; brands{" "}
                          {assignment.scope.all_brands
                            ? "all, including future brands"
                            : assignment.scope.brand_ids.join(", ") || "none"}
                          . Protected fields: {assignment.fields.join(", ") || "none"}.
                        </p>
                      ))}
                    </td>
                    <td>
                      {person.exclusions.map((issue) => (
                        <p key={`${issue.code}:${issue.detail}`}>{issue.detail}</p>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )
      )}
      <div className="form-actions">
        <button
          className="btn"
          onClick={() => {
            setCursor(0);
            setFingerprint("");
            report.reload();
          }}
        >
          Restart review
        </button>
        <button
          className="btn"
          disabled={report.loading || report.failure !== "" || report.value?.next_cursor == null}
          onClick={() => {
            setFingerprint(report.value?.fingerprint ?? "");
            setCursor(report.value?.next_cursor ?? 0);
          }}
        >
          Next page
        </button>
      </div>
    </section>
  );
}
