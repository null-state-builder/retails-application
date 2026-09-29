// Setup -> Access (#173). Who may do what, as a grid an administrator edits.
//
// Sections down, roles across - the shape of the ratified RBAC sheet, so the
// answer to "what changes if I move this?" is one column, readable at a glance.
// Everything on screen arrives from `GET /auth/admin/access-matrix`: the
// sections, the rungs of the ladder, the roles, the locked cells and the reason
// each one is locked. Nothing about access is written into this file, which is
// what lets a section be added or a floor ratified with no front-end release
// (Rule 12).
//
// Editing is one role at a time because the endpoint replaces one role's row:
// pick a column, move its rungs, propose. The save is a *proposal* - changing a
// role is never one person's act (floor rule 4) - so the screen says so rather
// than pretending the change is live.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Lock, Save, ShieldCheck, X } from "lucide-react";

import { apiErrorMessage, typedApi } from "../lib/api";
import { PageHeader } from "../components/PageHeader";
import "./AccessMatrix.css";

interface SectionMeta {
  code: string;
  label: string;
}

interface Cell {
  capability: string;
  label: string;
}

interface LockedCell {
  max_capability: string;
  rule: string;
  reason: string;
}

interface RoleRow {
  code: string;
  name: string;
  is_system: boolean;
  is_active: boolean;
  user_count: number;
  section_access: Record<string, Cell>;
  locked: Record<string, LockedCell>;
}

interface Matrix {
  sections: SectionMeta[];
  /** The ladder, in order, each rung with the server's own word for it. */
  capabilities: SectionMeta[];
  rules: { rule: number; text: string }[];
  roles: RoleRow[];
}

const EMPTY: Matrix = { sections: [], capabilities: [], rules: [], roles: [] };

/** The server's word for a rung. No fallback table lives here on purpose: the
 *  ladder and its wording are one payload, so adding a rung is a backend
 *  change and nothing else (Rule 12). */
function rungLabel(matrix: Matrix, capability: string): string {
  return matrix.capabilities.find((c) => c.code === capability)?.label ?? capability;
}

/** Where a rung sits on the ladder - the ladder is the payload's own order. */
function rungIndex(matrix: Matrix, capability: string): number {
  return matrix.capabilities.findIndex((c) => c.code === capability);
}

export function AccessMatrixPage() {
  const [matrix, setMatrix] = useState<Matrix>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [editing, setEditing] = useState("");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    typedApi
      .get("/auth/admin/access-matrix")
      .then((r) => setMatrix(r.data as Matrix))
      .catch((e) => setError(apiErrorMessage(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  const role = useMemo(
    () => matrix.roles.find((r) => r.code === editing),
    [matrix.roles, editing],
  );

  function startEditing(next: RoleRow) {
    setError("");
    setOk("");
    if (next.code === editing) {
      setEditing("");
      return;
    }
    setEditing(next.code);
    setDraft(
      Object.fromEntries(
        matrix.sections.map((s) => [s.code, next.section_access[s.code]?.capability ?? "none"]),
      ),
    );
  }

  function stopEditing() {
    setEditing("");
    setDraft({});
  }

  /** The rung a role holds on a section, as stored. */
  function stored(row: RoleRow, section: string): string {
    return row.section_access[section]?.capability ?? "none";
  }

  /** What the column shows: the draft while editing it, the stored row otherwise. */
  function shown(row: RoleRow, section: string): string {
    return row.code === editing ? (draft[section] ?? "none") : stored(row, section);
  }

  function moved(row: RoleRow, section: string): boolean {
    return row.code === editing && shown(row, section) !== stored(row, section);
  }

  /** The cells the open column has moved - what the Propose button counts. */
  const changes = useMemo(() => {
    if (!role) return [];
    return matrix.sections
      .filter((s) => (draft[s.code] ?? "none") !== stored(role, s.code))
      .map((s) => ({ section: s.label, from: stored(role, s.code), to: draft[s.code] }));
  }, [role, draft, matrix.sections]);

  function propose() {
    if (!role) return;
    setSaving(true);
    setError("");
    setOk("");
    typedApi
      .put(`/auth/admin/roles/${role.code}/access` as "/auth/admin/roles/{code}/access", {
        section_access: Object.fromEntries(
          matrix.sections.map((s) => [s.code, { capability: draft[s.code] ?? "none" }]),
        ),
      })
      .then((r) => {
        setOk(
          r.data?.status === "unchanged"
            ? "Nothing changed, so nobody was asked to approve anything."
            : `${role.name}: sent for a different Owner or IT Admin to approve. Access does not move until they do.`,
        );
        stopEditing();
        load();
      })
      .catch((e) => setError(apiErrorMessage(e)))
      .finally(() => setSaving(false));
  }

  // Every locked cell on the grid, said once in full - the greyed square carries
  // the reason in its tooltip, and a tooltip is not an answer for someone who
  // cannot hover.
  const lockNotes = useMemo(() => {
    const seen = new Map<string, { rule: string; reason: string; roles: string[] }>();
    for (const row of matrix.roles) {
      for (const [section, lock] of Object.entries(row.locked)) {
        const key = `${section}:${lock.rule}`;
        const held = seen.get(key) ?? { rule: lock.rule, reason: lock.reason, roles: [] };
        held.roles.push(row.name);
        seen.set(key, held);
      }
    }
    return [...seen.values()];
  }, [matrix.roles]);

  return (
    <div className="page-pad">
      <PageHeader
        title="Access"
        lead="Who may see and do what, section by section. Editing a role needs a second Owner or IT Admin."
        titleTestId="access-matrix-title"
        actions={
          editing ? (
            <>
              <button className="btn" onClick={stopEditing} data-testid="access-cancel">
                <X size={14} /> Cancel
              </button>
              <button
                className="btn btn-cta"
                onClick={propose}
                disabled={saving || changes.length === 0}
                data-testid="access-propose"
              >
                <Save size={15} />{" "}
                {saving
                  ? "Sending…"
                  : `Propose ${changes.length} change${changes.length === 1 ? "" : "s"}`}
              </button>
            </>
          ) : null
        }
      />

      {error && (
        <div className="warn-note" data-testid="access-error">
          {error}
        </div>
      )}
      {ok && (
        <div className="ok-note" data-testid="access-ok">
          {ok}
        </div>
      )}

      {editing ? (
        <div className="card section-card" data-testid="access-editing-note">
          Editing <b>{role?.name}</b>. Move the rungs in that column, then propose. Greyed cells are
          money floor rules and cannot be moved by anyone.
        </div>
      ) : (
        <div className="card section-card" data-testid="access-pick-note">
          Click a role's name to edit its column.
        </div>
      )}

      <div className="table-wrap matrix-wrap">
        <table className="data matrix" data-testid="access-matrix-table">
          <thead>
            <tr>
              <th className="matrix-section">Section</th>
              {matrix.roles.map((r) => (
                <th key={r.code}>
                  <div className="matrix-role-head">
                    <button
                      type="button"
                      className={`matrix-pick ${r.code === editing ? "active" : ""}`}
                      onClick={() => startEditing(r)}
                      data-testid={`access-role-${r.code}`}
                    >
                      <ShieldCheck size={12} /> {r.name}
                    </button>
                    <span className="muted-cell">
                      {r.user_count} user{r.user_count === 1 ? "" : "s"}
                      {r.is_active ? "" : " · inactive"}
                    </span>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={matrix.roles.length + 1}>Loading…</td>
              </tr>
            ) : matrix.sections.length === 0 ? (
              <tr data-testid="access-matrix-empty">
                <td>No sections to show.</td>
              </tr>
            ) : (
              matrix.sections.map((s) => (
                <tr key={s.code} data-testid={`access-row-${s.code}`}>
                  <td className="matrix-section">
                    <b>{s.label}</b>
                    <div className="mono muted-cell" style={{ fontSize: 12 }}>
                      {s.code}
                    </div>
                  </td>
                  {matrix.roles.map((r) => {
                    const lock = r.locked[s.code];
                    const held = shown(r, s.code);
                    const cellId = `access-cell-${r.code}-${s.code}`;
                    // A locked cell keeps every rung *below* the floor's ceiling
                    // - a role capped at "do the work" still chooses freely
                    // between no access, view only and that.
                    const capped = Boolean(lock);
                    const ceiling = capped ? rungIndex(matrix, lock.max_capability) : -1;
                    // The ratified sheet wording ("Expenses only (create)") is the
                    // "why this role sees what it sees"; the floor's reason wins
                    // the tooltip where there is one, because it answers the
                    // question a greyed square provokes.
                    const why = capped
                      ? `${lock.rule} ${lock.reason}`
                      : r.section_access[s.code]?.label || undefined;
                    if (r.code !== editing) {
                      return (
                        <td
                          key={r.code}
                          className={capped ? "matrix-locked" : ""}
                          title={why}
                          data-testid={cellId}
                        >
                          {capped && <Lock size={11} />} {rungLabel(matrix, held)}
                        </td>
                      );
                    }
                    return (
                      <td
                        key={r.code}
                        className={`matrix-editing ${moved(r, s.code) ? "matrix-moved" : ""}`}
                        title={why}
                        data-testid={cellId}
                      >
                        <select
                          className="input"
                          value={held}
                          onChange={(e) => setDraft({ ...draft, [s.code]: e.target.value })}
                          data-testid={`access-select-${r.code}-${s.code}`}
                        >
                          {matrix.capabilities
                            .filter((_cap, index) => !capped || index <= ceiling)
                            .map(({ code: cap, label: word }) => (
                              <option key={cap} value={cap}>
                                {word}
                              </option>
                            ))}
                        </select>
                      </td>
                    );
                  })}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {lockNotes.length > 0 && (
        <div className="card section-card" data-testid="access-locks">
          <h3 className="h3">
            <Lock size={14} /> Locked cells
          </h3>
          <ul className="matrix-rules">
            {lockNotes.map((note) => (
              <li key={note.rule + note.roles.join()}>
                <b>{note.rule}</b> {note.reason}
              </li>
            ))}
          </ul>
        </div>
      )}

      {matrix.rules.length > 0 && (
        <div className="card section-card" data-testid="access-rules">
          <h3 className="h3">The four rules nobody can switch off</h3>
          <ul className="matrix-rules">
            {matrix.rules.map((r) => (
              <li key={r.rule}>{r.text}</li>
            ))}
          </ul>
          <div className="muted-cell">
            Two of them are not squares on this grid: nobody approves their own document, and no
            role change - including one made here - takes effect until a second Owner or IT Admin
            approves it.
          </div>
        </div>
      )}
    </div>
  );
}
