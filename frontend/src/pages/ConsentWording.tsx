import { useCallback, useEffect, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta, typedApi } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";

type Payload = ApiRead<ApiSchemas["ConsentWording"]>;
type Version = Payload["versions"][number];

/** Setup > Consent Wording (store operations ticket 15, ST-CMP-6).
 *
 *  The three questions the customer answers on the customer display: send my
 *  bill, are you under 18, send me offers. Saving changes the wording by adding
 *  a new version; an old version is never changed, and every answer keeps the
 *  version it was given under. Everyone holding Setup reads the page; only Admin
 *  saves, and the server refuses anybody else regardless. */
export function ConsentWordingPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [form, setForm] = useState({ bill: "", age: "", offers: "", note: "" });
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);

  /** `fill` puts the current wording into the form: on the first load and after
   *  a save, never over what Admin is typing (a second load in flight must not
   *  wipe it). */
  const load = useCallback(async (fill: boolean) => {
    try {
      const response = await typedApi.get("/goods-v1/masters/consent-wording");
      const payload = response.data as Payload;
      setData(payload);
      const current = payload.versions[0];
      if (!current) return;
      setForm((form) =>
        fill || !form.bill
          ? { bill: current.bill, age: current.age, offers: current.offers, note: "" }
          : form,
      );
    } catch (reason) {
      setError(apiErrorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  async function save() {
    if (!data) return;
    setError("");
    setSaved("");
    setBusy(true);
    try {
      const response = await api.post<{ version: number }>(
        "/goods-v1/masters/consent-wording/versions",
        { ...goodsMeta(data.current_version), ...form },
      );
      setSaved(
        `Saved as version ${response.data.version}. Tills ask with it from their next sync.`,
      );
      await load(true);
    } catch (reason) {
      setError(apiErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Consent Wording" />
        {error ? <p className="warn-note">{error}</p> : <p>Loading consent wording…</p>}
      </div>
    );
  }

  const on = data.stores.filter((store) => store.consent_on);
  return (
    <div className="page-pad">
      <PageHeader
        title="Consent Wording"
        lead="The questions the customer answers on the customer display. A change is a new version; every answer keeps the version it was given under."
      />
      {!data.can_change && (
        <p className="muted-cell" data-testid="consent-wording-read-only">
          Only Admin can change the consent wording. You can see everything here.
        </p>
      )}
      {error && (
        <p className="warn-note" data-testid="consent-wording-error">
          {error}
        </p>
      )}
      {saved && (
        <p className="ok-note" data-testid="consent-wording-saved">
          {saved}
        </p>
      )}
      <p className="muted-cell" data-testid="consent-wording-stores">
        {on.length
          ? `Asked at ${on.map((store) => store.code).join(", ")}.`
          : "Customer consent is switched off at every store you can see."}
      </p>

      {data.can_change && (
        <section className="card section-card" data-testid="consent-wording-form">
          <h2 className="h3">Change the wording (version {data.current_version + 1})</h2>
          <label className="field">
            <span>Send my bill</span>
            <input
              className="input"
              data-testid="consent-wording-bill"
              maxLength={200}
              value={form.bill}
              onChange={(event) => setForm({ ...form, bill: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Under 18 (asked before offers)</span>
            <input
              className="input"
              data-testid="consent-wording-age"
              maxLength={200}
              value={form.age}
              onChange={(event) => setForm({ ...form, age: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Send me offers</span>
            <input
              className="input"
              data-testid="consent-wording-offers"
              maxLength={200}
              value={form.offers}
              onChange={(event) => setForm({ ...form, offers: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Why it is changing</span>
            <input
              className="input"
              data-testid="consent-wording-note"
              maxLength={240}
              value={form.note}
              onChange={(event) => setForm({ ...form, note: event.target.value })}
            />
          </label>
          <div className="toolbar">
            <button
              type="button"
              className="btn btn-primary"
              data-testid="consent-wording-save"
              disabled={busy}
              onClick={() => void save()}
            >
              Save new version
            </button>
          </div>
        </section>
      )}

      <section className="card section-card">
        <h2 className="h3">Versions</h2>
        <div className="table-wrap">
          <table className="data" data-testid="consent-wording-versions">
            <thead>
              <tr>
                <th>Version</th>
                <th>Send my bill</th>
                <th>Under 18</th>
                <th>Send me offers</th>
                <th>Saved</th>
                <th>Why</th>
              </tr>
            </thead>
            <tbody>
              {data.versions.map((version: Version) => (
                <tr key={version.version} data-testid={`consent-wording-v${version.version}`}>
                  <td className="mono">
                    {version.version}
                    {version.version === data.current_version ? " (current)" : ""}
                  </td>
                  <td>{version.bill}</td>
                  <td>{version.age}</td>
                  <td>{version.offers}</td>
                  <td>
                    {version.first
                      ? "Built in"
                      : `${version.saved_at?.slice(0, 10) ?? ""}${version.saved_by ? ` · ${version.saved_by}` : ""}`}
                  </td>
                  <td>{version.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
