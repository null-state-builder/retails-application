import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

// Every write belongs to the separately verified fictional damage sibling.
export default defineConfig({
  testDir: "./browser",
  testMatch: "first-store-damage.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"]],
  outputDir: `../.local/first-store-damage-browser/run-${Date.now()}`,
  use: {
    ...devices["Desktop Chrome"],
    channel: process.env.PLAYWRIGHT_USE_CHROME ? "chrome" : undefined,
    baseURL: "http://127.0.0.1:5186",
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  webServer: [
    {
      command:
        "backend/.venv/bin/python scripts/first-store-damage-proof.py run -- backend/.venv/bin/python -m uvicorn config.asgi:application --host 127.0.0.1 --port 8016",
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      url: "http://127.0.0.1:8016/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command:
        'node --input-type=module -e \'import { createServer } from "vite"; const server = await createServer({ server: { host: "127.0.0.1", port: 5186, strictPort: true, proxy: { "/api": "http://127.0.0.1:8016" } } }); await server.listen();\'',
      url: "http://127.0.0.1:5186/login",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
