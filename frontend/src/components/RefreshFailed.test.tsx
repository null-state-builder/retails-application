import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { RefreshFailed } from "./RefreshFailed";

const refused = { response: { status: 503, data: { detail: "The server is busy." } } };

describe("RefreshFailed", () => {
  it("warns, without blocking, when a refresh fails with rows on screen", () => {
    const html = renderToStaticMarkup(
      <RefreshFailed
        error={refused}
        shown
        updatedAt={Date.now() - 3 * 60_000}
        onRetry={() => {}}
      />,
    );
    expect(html).toContain('data-testid="refresh-failed"');
    expect(html).toContain("note-warn");
    expect(html).toContain('role="status"');
    expect(html).toContain("may be out of date (last loaded 3 min ago)");
    expect(html).toContain("The server is busy.");
    expect(html).toContain("Try again");
  });

  it("draws nothing without an error, or with nothing on screen to be stale", () => {
    expect(renderToStaticMarkup(<RefreshFailed error={null} shown />)).toBe("");
    expect(renderToStaticMarkup(<RefreshFailed error={refused} shown={false} />)).toBe("");
  });
});
