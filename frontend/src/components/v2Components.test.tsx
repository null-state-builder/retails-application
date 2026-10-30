import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { FilterChip } from "./FilterChip";
import { KpiStrip, LeadFigure } from "./KpiStrip";
import { Note } from "./Note";
import { Section } from "./Section";
import { SegmentedControl } from "./SegmentedControl";
import { SettingRow } from "./SettingRow";
import { EmptyState, ErrorState } from "./States";
import { Status } from "./Status";
import { Switch, switchStateWord } from "./Switch";
import { clampPaneWidth } from "./SplitView";

const noop = () => {};

describe("design system v2 shared components", () => {
  it("writes the switch's state beside it, including held off", () => {
    expect(switchStateWord(true)).toBe("On");
    expect(switchStateWord(false)).toBe("Off");
    expect(switchStateWord(true, true)).toBe("On, held off");
    expect(switchStateWord(false, true)).toBe("Off");
    const html = renderToStaticMarkup(
      <Switch checked held onChange={noop} ariaLabel="Gift cards" />,
    );
    expect(html).toContain('role="switch"');
    expect(html).toContain("On, held off");
    expect(html).toContain("state-word held");
  });

  it("disables the switch while it saves", () => {
    const html = renderToStaticMarkup(<Switch checked={false} busy onChange={noop} />);
    expect(html).toContain("disabled");
  });

  it("presses exactly one segment, as buttons not tabs", () => {
    const html = renderToStaticMarkup(
      <SegmentedControl
        label="Period"
        value="week"
        onChange={noop}
        options={[
          { value: "day", label: "Today" },
          { value: "week", label: "Week" },
          { value: "month", label: "Month", count: 3 },
        ]}
      />,
    );
    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="Period"');
    expect(html).not.toContain("tablist");
    expect(html.match(/aria-pressed="true"/g)).toHaveLength(1);
    expect(html).toContain('<span class="seg-n">3</span>');
  });

  it("keeps a zero count on a filter chip", () => {
    const html = renderToStaticMarkup(
      <FilterChip pressed={false} onClick={noop} count={0}>
        Waiting
      </FilterChip>,
    );
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain(">0<");
  });

  it("draws a status as a dot and a word", () => {
    expect(renderToStaticMarkup(<Status>Draft</Status>)).toBe('<span class="status">Draft</span>');
    expect(renderToStaticMarkup(<Status tone="green">Synced</Status>)).toContain(
      'class="status status-green"',
    );
  });

  it("shows one lead figure and at most five in the strip", () => {
    const items = Array.from({ length: 7 }, (_, i) => ({ label: `K${i}`, value: String(i) }));
    const html = renderToStaticMarkup(<KpiStrip items={items} />);
    expect(html.match(/kpi-cell/g)).toHaveLength(5);
    expect(renderToStaticMarkup(<LeadFigure label="Net sales" value="₹1,20,000" />)).toContain(
      "lead-figure-v",
    );
  });

  it("names a section by its heading, with no box", () => {
    const html = renderToStaticMarkup(<Section heading="Cash drawer">x</Section>);
    expect(html).toMatch(
      /<section class="section" aria-labelledby="([^"]+)"><div class="section-head"><h2 class="heading" id="\1">/,
    );
    expect(html).not.toContain("card");
  });

  it("hides a setting's control while its form is open", () => {
    const closed = renderToStaticMarkup(
      <SettingRow title="Pause billing" control={<button>Pause</button>} />,
    );
    expect(closed).toContain("setting-row-control");
    const open = renderToStaticMarkup(
      <SettingRow title="Pause billing" control={<button>Pause</button>} more={<p>Why?</p>} />,
    );
    expect(open).not.toContain("setting-row-control");
    expect(open).toContain("setting-row-more");
  });

  it("makes an error note an alert", () => {
    expect(renderToStaticMarkup(<Note tone="error">Refused</Note>)).toContain('role="alert"');
    expect(renderToStaticMarkup(<Note>Offer applied</Note>)).not.toContain("role=");
  });

  it("states empty and failed lists left-aligned with their action", () => {
    expect(renderToStaticMarkup(<EmptyState>No bookings are open.</EmptyState>)).toContain(
      'class="state state-empty"',
    );
    const retry = vi.fn();
    const html = renderToStaticMarkup(
      <ErrorState message="The list did not load." onRetry={retry} busy />,
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain("Retrying…");
  });

  it("keeps the record pane between 320 and 440px", () => {
    expect(clampPaneWidth(200)).toBe(320);
    expect(clampPaneWidth(380.4)).toBe(380);
    expect(clampPaneWidth(900)).toBe(440);
  });
});
