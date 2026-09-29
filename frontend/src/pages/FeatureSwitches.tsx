import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "../auth/AuthContext";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage, goodsMeta, typedApi } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { formatDateTime } from "../lib/format";

type Payload = ApiRead<ApiSchemas["FeatureSwitches"]>;
type SwitchState = ApiSchemas["SwitchState"];

/** Setup > Feature Switches (store operations PRD ST-OPS-6).
 *
 *  Admin turns each store-operations feature on or off per store, and picks
 *  manual or connected where the feature has both. A feature waiting on an open
 *  gate says so by name and cannot be switched on at a real store. Everyone
 *  else holding Setup reads the same page without the controls; the server
 *  refuses their change regardless. */
export function FeatureSwitchesPage() {
  const { refreshSession } = useAuth();
  const [data, setData] = useState<Payload | null>(null);
  const [storeId, setStoreId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await typedApi.get("/goods-v1/masters/store-features");
      const body = response.data as Payload;
      setData(body);
      setStoreId((current) =>
        current !== null && body.stores.some((s) => s.id === current)
          ? current
          : (body.stores[0]?.id ?? null),
      );
    } catch (reason) {
      setError(apiErrorMessage(reason));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const store = data?.stores.find((s) => s.id === storeId) ?? null;
  const states = useMemo(() => {
    const byKey = new Map<string, SwitchState>();
    for (const s of data?.switches ?? []) if (s.store_id === storeId) byKey.set(s.feature_key, s);
    return byKey;
  }, [data, storeId]);
  const featureName = useMemo(
    () => new Map((data?.features ?? []).map((f) => [f.key, f.name])),
    [data],
  );
  const storeCode = useMemo(
    () => new Map((data?.stores ?? []).map((s) => [s.id, s.code])),
    [data],
  );

  async function change(key: string, enabled: boolean, mode?: string) {
    const current = states.get(key);
    if (!store || !current) return;
    setError("");
    setSaved("");
    setBusy(key);
    try {
      await api.post("/goods-v1/masters/store-features/switch", {
        ...goodsMeta(current.revision > 0 ? current.revision : undefined),
        store_id: store.id,
        feature_key: key,
        enabled,
        ...(mode ? { mode } : {}),
      });
      setSaved(`${featureName.get(key) ?? key} is now ${enabled ? "on" : "off"} at ${store.name}.`);
      await load();
      // The menus read the session's switches: re-read it now, not on the next move.
      refreshSession(true);
    } catch (reason) {
      setError(apiErrorMessage(reason));
      await load();
    } finally {
      setBusy(null);
    }
  }

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Feature Switches" />
        {error ? <p className="warn-note">{error}</p> : <p>Loading feature switches…</p>}
      </div>
    );
  }

  return (
    <div className="page-pad">
      <PageHeader
        title="Feature Switches"
        lead="Turn each store feature on or off for one store at a time."
      />
      {!data.can_change && (
        <p className="muted-cell" data-testid="feature-switches-read-only">
          Only Admin can change a feature switch. You can see how each store is set.
        </p>
      )}
      <section className="card section-card">
        <div className="toolbar">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
              data-testid="feature-switches-store"
              value={storeId ?? ""}
              onChange={(event) => setStoreId(Number(event.target.value))}
            >
              {data.stores.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.code}){s.real ? "" : " - demo"}
                </option>
              ))}
            </select>
          </label>
        </div>
        {data.features.length === 0 ? (
          <p className="muted-cell" data-testid="feature-switches-empty">
            No store feature can be switched yet.
          </p>
        ) : !store ? (
          <p className="muted-cell">You have no store to set.</p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="feature-switches-table">
              <thead>
                <tr>
                  <th>Feature</th>
                  <th>Waiting for</th>
                  <th>On</th>
                  <th>Mode</th>
                </tr>
              </thead>
              <tbody>
                {data.features.map((f) => {
                  const state = states.get(f.key);
                  if (!state) return null;
                  const locked = !!state.locked_reason;
                  const cannot = !data.can_change || busy !== null;
                  return (
                    <tr key={f.key} data-testid={`feature-row-${f.key}`}>
                      <td>
                        <strong>{f.name}</strong>
                        {f.description && <div className="muted-cell">{f.description}</div>}
                        {state.locked_reason && (
                          <div className="warn-note" data-testid={`feature-locked-${f.key}`}>
                            {state.locked_reason}
                          </div>
                        )}
                      </td>
                      <td>
                        {f.gate ? (
                          <span className="chip chip-amber" data-testid={`feature-gate-${f.key}`}>
                            {f.gate}
                          </span>
                        ) : (
                          <span className="muted-cell">Nothing</span>
                        )}
                      </td>
                      <td>
                        <label className="check-row">
                          <input
                            type="checkbox"
                            data-testid={`feature-toggle-${f.key}`}
                            checked={state.chosen}
                            disabled={cannot || (locked && !state.chosen)}
                            onChange={(event) => void change(f.key, event.target.checked)}
                          />
                          {state.enabled ? "On" : state.chosen ? "Chosen on, held off" : "Off"}
                        </label>
                      </td>
                      <td>
                        {f.has_modes ? (
                          <select
                            className="input"
                            data-testid={`feature-mode-${f.key}`}
                            value={state.mode}
                            disabled={cannot}
                            onChange={(event) => void change(f.key, state.chosen, event.target.value)}
                          >
                            <option value="manual">Manual</option>
                            <option value="connected">Connected provider</option>
                          </select>
                        ) : (
                          <span className="muted-cell">Manual only</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {error && (
          <p className="warn-note" data-testid="feature-switches-error">
            {error}
          </p>
        )}
        {saved && (
          <p className="ok-note" data-testid="feature-switches-saved">
            {saved}
          </p>
        )}
      </section>
      <section className="card section-card">
        <h2 className="h3">Recent changes</h2>
        {data.changes.length === 0 ? (
          <p className="muted-cell">No switch has been changed yet.</p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="feature-switches-changes">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Who</th>
                  <th>Store</th>
                  <th>Feature</th>
                  <th>Before</th>
                  <th>After</th>
                </tr>
              </thead>
              <tbody>
                {data.changes.map((c) => (
                  <tr key={c.id}>
                    <td>{formatDateTime(c.at)}</td>
                    <td>{c.by}</td>
                    <td>{storeCode.get(c.store_id) ?? c.store_id}</td>
                    <td>{featureName.get(c.feature_key) ?? c.feature_key}</td>
                    <td>{describe(c.before)}</td>
                    <td>{describe(c.after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function describe(value: unknown): string {
  const v = (value ?? {}) as { enabled?: boolean; mode?: string };
  const onOff = v.enabled ? "On" : "Off";
  return v.mode === "connected" ? `${onOff}, connected` : onOff;
}

/** The demo probe's own screen (`KDPS_STORE_FEATURE_DEMO_PROBES`): reached only
 *  from a menu line drawn while the probe is on where the person works, and it
 *  asks the server, which refuses while the probe is off. Test data only. */
export function FeatureCheckPage() {
  const { activeStore, session } = useAuth();
  const [message, setMessage] = useState("Checking…");
  const storeId = activeStore?.id ?? Number(session?.store_features?.["demo-probe"]?.[0] ?? NaN);

  useEffect(() => {
    if (!Number.isFinite(storeId)) {
      setMessage("Pick a store first.");
      return;
    }
    let live = true;
    api
      .get<{ feature_key: string; mode: string }>("/goods-v1/masters/store-features/probe", {
        params: { store_id: storeId },
      })
      .then((r) => live && setMessage(`The demo probe answered (${r.data.mode} mode).`))
      .catch((reason) => live && setMessage(apiErrorMessage(reason)));
    return () => {
      live = false;
    };
  }, [storeId]);

  return (
    <div className="page-pad">
      <PageHeader title="Feature check (demo)" />
      <p data-testid="feature-check-result">{message}</p>
    </div>
  );
}
