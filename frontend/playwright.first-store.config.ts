import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

// Separate ports and an identity-checking wrapper preserve the normal proof
// fixture and any application already open in the in-app browser.
export default defineConfig({
  testDir: "./browser",
  testMatch: ["manager-workspace.spec.ts", "first-store-pos.spec.ts"],
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"]],
  outputDir: "../.local/manager-workspace-browser",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5178",
    trace: "off",
    video: "off",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command:
        "backend/.venv/bin/python scripts/first-store-proof.py run -- backend/.venv/bin/python -m uvicorn config.asgi:application --host 127.0.0.1 --port 8008",
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      url: "http://127.0.0.1:8008/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command:
        'node --input-type=module -e \'import { createServer } from "vite"; const server = await createServer({ server: { host: "127.0.0.1", port: 5178, strictPort: true, proxy: { "/api": "http://127.0.0.1:8008" } } }); await server.listen();\'',
      url: "http://127.0.0.1:5178/login",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
