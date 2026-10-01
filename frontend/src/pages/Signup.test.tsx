import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  previewStaffCodes,
  registrationErrors,
  RegistrationFeedback,
  RegistrationSummaryView,
  type RegistrationSummary,
} from "./Signup";

describe("signup validation feedback", () => {
  it("keeps all field messages and shows one summary with person-specific links", () => {
    const errors = registrationErrors({
      response: {
        data: {
          owner: { temporary_password: ["First issue.", "Second issue."] },
          admin: { temporary_password: ["Admin issue."] },
        },
      },
    });
    const html = renderToStaticMarkup(
      <RegistrationFeedback errors={errors} error="First issue." onSelect={() => {}} />,
    );
    expect(html.match(/role="alert"/g)).toHaveLength(1);
    expect(html).toContain('href="#signup-owner-password"');
    expect(html).toContain("Owner temporary password: First issue. Second issue.");
    expect(html).toContain("Admin temporary password: Admin issue.");
    expect(html).not.toContain('data-testid="signup-error"');
  });
  it("shows a service error when there are no actionable field errors", () => {
    const html = renderToStaticMarkup(
      <RegistrationFeedback
        errors={{}}
        error="Setup unavailable. Try again."
        onSelect={() => {}}
      />,
    );
    expect(html.match(/role="alert"/g)).toHaveLength(1);
    expect(html).toContain('data-testid="signup-error"');
    expect(html).toContain("Setup unavailable. Try again.");
  });
});

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
    expect(html).toContain("Their login and store access are set up after registration.");
    expect(html).toContain("access.review");
  });
});

describe("signup code previews", () => {
  it("uses one sequence across roles and skips custom codes case-insensitively", () => {
    expect(
      previewStaffCodes([{ staff_code: "" }, { staff_code: "emp-0001" }, { staff_code: "" }]),
    ).toEqual(["EMP-0002", "emp-0001", "EMP-0003"]);
  });
  it("keeps explicit codes and supplies automatic defaults", () => {
    expect(
      previewStaffCodes([{ staff_code: "OWNER" }, { staff_code: "" }, { staff_code: "" }]),
    ).toEqual(["OWNER", "EMP-0001", "EMP-0002"]);
  });
});
