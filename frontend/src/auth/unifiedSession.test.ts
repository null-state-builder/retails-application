import { describe, expect, it } from "vitest";

import { contextSelection, readSession, shellUser } from "./AuthContext";
import { canAccess } from "./routeAccess";
import { resolveLegacyPath } from "../shell/navConfig";
import { switcherModel } from "../shell/unitSwitcher";

const session = {
  contract_version: "access-v2",
  policy_version: "policy-1",
  user: {
    id: "4",
    human_id: "human-4",
    display_name: "Store colleague",
    email: "store@example.test",
    must_change_password: false,
  },
  assignments: [
    {
      id: "assignment-1",
      role_code: "store_person",
      all_sites: false,
      site_ids: [1],
      all_brands: false,
      brand_ids: [1],
      effective_from: "2026-09-29T00:00:00Z",
      effective_to: null,
    },
  ],
  sections: [{ code: "home", label: "Today", order: 1, capability: "view" }],
  capabilities: { home: "view" },
  navigation: ["home"],
  display_actions: [],
  sites: [],
  context_choices: { mode: "units", all_units: false, sites: [], brands: [] },
  store_features: {},
  expires_at: "2026-09-29T12:00:00Z",
  step_up_valid_until: null,
} as const;

describe("unified session", () => {
  it("rejects the old nested-profile contract", () => {
    expect(() => readSession({ profile: {}, actions: ["access.manage"] })).toThrow(
      "Unsupported session response",
    );
  });

  it("derives shell identity and context from the one server response", () => {
    const user = shellUser(readSession(session));
    expect(user.full_name).toBe("Store colleague");
    expect(user.role?.code).toBe("store_person");
    expect(user.stores).toEqual([]);
  });

  it("keeps a direct URL closed when the server omitted its section", () => {
    const user = shellUser(readSession(session));
    expect(canAccess("/", user)).toBe(true);
    expect(canAccess("/setup/people-access", user)).toBe(false);
    expect(canAccess("/staff/payroll", user)).toBe(false);
  });

  it("sends old access bookmarks into the one People & Access workspace", () => {
    expect(resolveLegacyPath("/setup/users")).toBe("/setup/people-access?panel=people");
    expect(resolveLegacyPath("/setup/access")).toBe("/setup/people-access?panel=roles");
  });

  it("does not choose a single persona for mixed assignments", () => {
    const user = shellUser(
      readSession({
        ...session,
        assignments: [
          ...session.assignments,
          { ...session.assignments[0], id: "assignment-2", role_code: "accounts" },
        ],
      }),
    );
    expect(user.role).toBeNull();
  });

  it("keeps mixed-role context on one selector axis", () => {
    const store = {
      id: 1,
      code: "S1",
      name: "Site 1",
      store_type: "store",
      state_name: "Bihar",
      state_code: "10",
      gstin_number: "",
    };
    const brand = { id: 2, code: "B2", name: "Brand 2" };
    const user = shellUser(
      readSession({
        ...session,
        assignments: [
          ...session.assignments,
          {
            ...session.assignments[0],
            id: "assignment-2",
            role_code: "brand_manager",
            site_ids: [2],
            brand_ids: [2],
          },
        ],
        context_choices: { mode: "units", all_units: false, sites: [store], brands: [brand] },
      }),
    );
    expect(
      switcherModel(user, null, null).options.every(
        (option) => option.kind === "unit" || option.kind === "all-units",
      ),
    ).toBe(true);
    expect(contextSelection(store, null)).toEqual({ unit: "1" });
    expect(contextSelection(null, brand)).toEqual({ brand: "2" });
    expect(() => contextSelection(store, brand)).toThrow("cannot be selected together");
  });
});
