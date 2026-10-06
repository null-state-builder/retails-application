import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

// Receipts and inventory deltas change only the separately verified ALPHA sibling.
export default defineConfig({
  testDir: "./browser",
  testMatch: "first-store-receiving.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"]],
  outputDir: `../.local/first-store-receiving-browser/run-${Date.now()}`,
  use: {
    ...devices["Desktop Chrome"],
    channel: process.env.PLAYWRIGHT_USE_CHROME ? "chrome" : undefined,
    baseURL: "http://127.0.0.1:5182",
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  webServer: [
    {
      command:
        "backend/.venv/bin/python scripts/first-store-receiving-proof.py run -- backend/.venv/bin/python -m uvicorn config.asgi:application --host 127.0.0.1 --port 8012",
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      url: "http://127.0.0.1:8012/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command:
        'node --input-type=module -e \'import { createServer } from "vite"; const server = await createServer({ server: { host: "127.0.0.1", port: 5182, strictPort: true, proxy: { "/api": "http://127.0.0.1:8012" } } }); await server.listen();\'',
      url: "http://127.0.0.1:5182/login",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
