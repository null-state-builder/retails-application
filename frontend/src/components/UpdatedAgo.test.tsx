import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { UpdatedAgo, agoText } from "./UpdatedAgo";

describe("UpdatedAgo", () => {
  it("says how old the shown data is in plain words", () => {
    const now = Date.UTC(2026, 9, 30, 12, 0, 0);
    expect(agoText(now, now)).toBe("just now");
    expect(agoText(now - 4_000, now)).toBe("just now");
    expect(agoText(now - 42_000, now)).toBe("42s ago");
    expect(agoText(now - 3 * 60_000, now)).toBe("3 min ago");
    expect(agoText(now - 2 * 3_600_000, now)).toBe("2 h ago");
    expect(agoText(now - 24 * 3_600_000, now)).toBe("1 day ago");
    expect(agoText(now - 72 * 3_600_000, now)).toBe("3 days ago");
    // A clock that runs a little behind the server never says "in the future".
    expect(agoText(now + 5_000, now)).toBe("just now");
  });

  it("draws nothing before anything has been read", () => {
    expect(renderToStaticMarkup(<UpdatedAgo updatedAt={null} />)).toBe("");
    expect(renderToStaticMarkup(<UpdatedAgo updatedAt={null} fetching />)).toBe("");
  });

  it("shows the age, and a quiet refreshing state while a new copy is read", () => {
    const at = Date.now() - 90_000;
    const idle = renderToStaticMarkup(<UpdatedAgo updatedAt={at} />);
    expect(idle).toContain("Updated");
    expect(idle).toContain("1 min ago");
    expect(idle).toContain(`dateTime="${new Date(at).toISOString()}"`);
    expect(idle).not.toContain("refreshing");
    expect(idle).toContain('class="muted-cell"');

    const busy = renderToStaticMarkup(
      <UpdatedAgo updatedAt={at} fetching testId="stock-updated" />,
    );
    expect(busy).toContain("refreshing…");
    expect(busy).toContain('aria-busy="true"');
    expect(busy).toContain('data-testid="stock-updated"');
  });
});
