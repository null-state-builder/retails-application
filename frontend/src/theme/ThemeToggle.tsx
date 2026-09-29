import { Monitor, Moon, Sun } from "lucide-react";

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
    <div className={`theme-seg ${compact ? "theme-seg-compact" : ""}`} data-testid="theme-toggle" role="group">
      {OPTIONS.map(({ value, label, Icon }) => (
        <button
          key={value}
          type="button"
          className={`theme-seg-btn ${preference === value ? "active" : ""}`}
          onClick={() => setPreference(value)}
          aria-pressed={preference === value}
          aria-label={label}
          title={label}
          data-testid={`theme-option-${value}`}
        >
          <Icon size={14} />
          {!compact && <span>{label}</span>}
        </button>
      ))}
    </div>
  );
}
