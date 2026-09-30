import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { RegistrationSummaryView, type RegistrationSummary } from "./Signup";

describe("initial joint confirmation", () => {
  it("renders the server's exact capability/label policy and separate initial people", () => {
    const summary: RegistrationSummary = {
      company: {
        code: "PROOF",
        name: "Proof company",
        legal_name: "Proof entity",
        pan: "ABCDE1234F",
        gstin: "29ABCDE1234F1Z5",
        state_name: "Karnataka",
        state_code: "29",
        billing_address: "Proof address",
        country: "IN",
        currency: "INR",
        timezone: "Asia/Kolkata",
        locale: "en-IN",
      },
      store: { code: "FIRST", name: "Proof store", setup_kind: "new", city: "Proof city" },
      owner: { name: "Owner", email: "owner@example.test", staff_code: "OWNER" },
      admin: { name: "Admin", email: "admin@example.test", staff_code: "ADMIN" },
      proposed_team: [
        {
          name: "Manager",
          email: "manager@example.test",
          staff_code: "MANAGER",
          role_code: "store_person",
        },
      ],
      initial_access: {
        policy_baseline: {
          roles: {
            it_admin: {
              section_access: { setup: { capability: "manage", label: "Configure company" } },
              field_access: [],
              step_actions: ["access.review"],
            },
          },
          action_levels: { "access.review": { section: "setup", minimum: "manage" } },
        },
      },
    };
    const html = renderToStaticMarkup(<RegistrationSummaryView summary={summary} />);
    expect(html).toContain("manage · Configure company");
    expect(html).toContain("Protected fields: None.");
    expect(html).toContain("owner@example.test");
    expect(html).toContain("admin@example.test");
    expect(html).toContain("These people receive no login or access from signup.");
    expect(html).toContain("access.review");
  });
});
