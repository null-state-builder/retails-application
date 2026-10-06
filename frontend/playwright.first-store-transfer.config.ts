import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

export default defineConfig({
  testDir: "./browser",
  testMatch: "first-store-transfer.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"]],
  outputDir: "../.local/first-store-transfer-browser",
  use: {
    ...devices["Desktop Chrome"],
    ...(process.env.PLAYWRIGHT_USE_CHROME ? { channel: "chrome" } : {}),
    baseURL: "http://127.0.0.1:5184",
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
    trace: "off",
    video: "off",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command:
        "backend/.venv/bin/python scripts/first-store-transfer-proof.py run -- backend/.venv/bin/python -m uvicorn config.asgi:application --host 127.0.0.1 --port 8014",
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      url: "http://127.0.0.1:8014/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command:
        'node --input-type=module -e \'import { createServer } from "vite"; const server = await createServer({ server: { host: "127.0.0.1", port: 5184, strictPort: true, proxy: { "/api": "http://127.0.0.1:8014" } } }); await server.listen();\'',
      url: "http://127.0.0.1:5184/login",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
