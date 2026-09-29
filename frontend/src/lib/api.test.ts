import { describe, expect, it } from "vitest";
import { apiErrorCode, apiErrorMessage } from "./api";

describe("API refusals", () => {
  it("preserves a deliberate server refusal and its code", () => {
    const refusal = {
      response: { data: { error: "This store is outside your grant.", code: "SCOPE_DENIED" } },
    };
    expect(apiErrorMessage(refusal)).toBe("This store is outside your grant.");
    expect(apiErrorCode(refusal)).toBe("SCOPE_DENIED");
  });

  it("shows the first field error without exposing a debug HTML response", () => {
    expect(apiErrorMessage({ response: { data: { roles: ["Choose one role."] } } })).toBe(
      "Choose one role.",
    );
    expect(apiErrorMessage({ response: { data: "<html>server crash</html>" } })).toBe(
      "Something went wrong. Please try again.",
    );
  });

  it("does not claim a code when the server sent none", () => {
    expect(apiErrorCode({ response: { data: { detail: "Sign in." } } })).toBeUndefined();
  });
});
