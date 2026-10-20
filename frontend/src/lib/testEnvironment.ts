// Test-mode marking (baseline B1).
//
// A synthetic tenant runs at a real store beside the legacy software, which
// does the store's real billing. Paper from the test tenant must never pass for
// a real GST invoice, so every document this app prints says so at its top and
// bottom, and the screens carry a badge. A real tenant sees none of it.
//
// The flag comes from the server session (`test_environment`). It is also held
// here, outside React, because the print adapter is a plain object that every
// receipt path shares: marking there catches every bill and reprint at once,
// rather than relying on each caller to remember.

/** What the paper says. One string, so the screen and the paper agree. */
export const TEST_MARK = "TEST – NOT A TAX INVOICE";

let testEnvironment = false;

/** Called by the shell whenever the session changes. */
export function setTestEnvironment(on: boolean): void {
  testEnvironment = on;
}

export function isTestEnvironment(): boolean {
  return testEnvironment;
}
