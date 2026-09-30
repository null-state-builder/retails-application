import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

export default defineConfig({
  testDir: "./browser",
  testIgnore: ["manager-workspace.spec.ts", "first-store-pos.spec.ts"],
  fullyParallel: false,
  forbidOnly: true,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command:
        "python3 scripts/proof.py run -- backend/.venv/bin/python -m uvicorn config.asgi:application --host 127.0.0.1 --port 8000",
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      url: "http://127.0.0.1:8000/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: "yarn preview",
      url: "http://127.0.0.1:5173/login",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
