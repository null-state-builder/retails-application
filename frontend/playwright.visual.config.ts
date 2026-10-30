import { defineConfig, devices } from "@playwright/test";

// Design System v2 visual baseline (docs/planning/design-system-v2-rollout.md, Phase 0).
// Screenshots the cashier screens of an app that is ALREADY running: the Vite dev
// server (demo login chips exist only in dev) against the Wardrobe rehearsal API.
// Nothing is started or stopped here, so the open app and its data are left as they are.
//
//   KDPS_VISUAL_LABEL=before yarn test:visual     → ../.local/visual-baseline/before/*.png
//   KDPS_VISUAL_URL=http://127.0.0.1:5173         (default)
export default defineConfig({
  testDir: "./browser",
  testMatch: ["visual-baseline.spec.ts"],
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  timeout: 600_000,
  reporter: [["list"]],
  outputDir: "../.local/visual-baseline/test-results",
  use: {
    ...devices["Desktop Chrome"],
    // The installed Chrome, so a local run needs no Playwright browser download.
    channel: process.env.KDPS_VISUAL_CHANNEL ?? "chrome",
    baseURL: process.env.KDPS_VISUAL_URL ?? "http://127.0.0.1:5173",
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
    trace: "off",
    video: "off",
    screenshot: "off",
  },
});
