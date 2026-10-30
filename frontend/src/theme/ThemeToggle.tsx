import { Monitor, Moon, Sun } from "lucide-react";

import { SegmentedControl } from "../components/SegmentedControl";
import type { ThemePreference } from "./theme";
import { useTheme } from "./theme";

const OPTIONS: { value: ThemePreference; label: string; Icon: typeof Sun }[] = [
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "system", label: "System", Icon: Monitor },
];

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const { preference, setPreference } = useTheme();
  return (
    <SegmentedControl
      label="Theme"
      className={compact ? "theme-seg-compact" : ""}
      testId="theme-toggle"
      value={preference}
      options={OPTIONS.map(({ value, label, Icon }) => ({
        value,
        label: (
          <>
            <Icon size={14} aria-hidden="true" />
            {!compact && <span>{label}</span>}
          </>
        ),
        ariaLabel: label,
        title: label,
        testId: `theme-option-${value}`,
      }))}
      onChange={setPreference}
    />
  );
}
