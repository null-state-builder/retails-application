import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

// A separate owned copy preserves the original three-unit/one-bill evidence.
export default defineConfig({
  testDir: "./browser",
  testMatch: "first-store-operating.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"]],
  outputDir: "../.local/first-store-operating-browser",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5180",
    trace: "off",
    video: "off",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command:
        "backend/.venv/bin/python scripts/first-store-operating-proof.py run -- backend/.venv/bin/python -m uvicorn config.asgi:application --host 127.0.0.1 --port 8010",
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      url: "http://127.0.0.1:8010/api/health",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command:
        'node --input-type=module -e \'import { createServer } from "vite"; const server = await createServer({ server: { host: "127.0.0.1", port: 5180, strictPort: true, proxy: { "/api": "http://127.0.0.1:8010" } } }); await server.listen();\'',
      url: "http://127.0.0.1:5180/login",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
