import { describe, expect, it } from "vitest";
import { API_PREFIX, MANIFEST, PWA_OPTIONS } from "./config";

describe("offline app shell boundaries", () => {
  it("preloads only built assets and excludes API navigations", () => {
    const workbox = PWA_OPTIONS.workbox;
    expect(workbox?.runtimeCaching).toEqual([]);
    expect(workbox?.globPatterns).toEqual(["**/*.{js,css,html,svg,png,ico,woff,woff2}"]);
    expect(workbox?.navigateFallback).toBe("/index.html");
    expect(
      workbox?.navigateFallbackDenylist?.some((pattern) =>
        pattern.test(`${API_PREFIX}/sell/sales`),
      ),
    ).toBe(true);
    expect(workbox?.navigateFallbackDenylist?.some((pattern) => pattern.test("/sell"))).toBe(false);
  });

  it("names the installed app and declares a maskable icon", () => {
    expect(MANIFEST.start_url).toBe("/");
    expect(
      MANIFEST.icons?.some((icon) => icon.purpose === "maskable" && icon.sizes === "512x512"),
    ).toBe(true);
  });
});
