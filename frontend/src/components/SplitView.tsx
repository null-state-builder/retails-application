// SplitView (design system v2): a list on the left and the open record in a
// pane on the right, so a person can work down a list without leaving it. The
// pane has a hairline on its left only - no radius, no shadow - and docks from
// 1200px; below that it becomes a drawer. It is 360px by default and 420px
// from 1600px, and people can drag its grip (or use the arrow keys on it) to
// set it anywhere from 320 to 440px; the width is kept per person. The URL
// carries which record is open (`?open=<id>`), so the consumer provides that.

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, PointerEvent, ReactNode } from "react";

import { useAuth } from "../auth/AuthContext";
import { PANE } from "../theme/layout";

export const PANE_WIDTH_KEY = "kdps-pane-w";

export function clampPaneWidth(px: number): number {
  return Math.round(Math.min(PANE.max, Math.max(PANE.min, px)));
}

function storageKey(userId: number | string | undefined) {
  return userId == null ? PANE_WIDTH_KEY : `${PANE_WIDTH_KEY}:${userId}`;
}

/** The person's own pane width, or null for the default (360, 420 from 1600). */
export function storedPaneWidth(userId?: number | string): number | null {
  try {
    const raw = Number(window.localStorage.getItem(storageKey(userId)));
    return Number.isFinite(raw) && raw > 0 ? clampPaneWidth(raw) : null;
  } catch {
    return null;
  }
}

function storePaneWidth(userId: number | string | undefined, px: number) {
  try {
    window.localStorage.setItem(storageKey(userId), String(px));
  } catch {
    /* storage may be unavailable (privacy mode); the width lasts this visit */
  }
}

const PaneWidthContext = createContext<{
  width: number | null;
  resize: (px: number) => void;
} | null>(null);

export function SplitView({
  children,
  pane,
  className = "",
  testId,
}: {
  /** The list, usually a table. */
  children: ReactNode;
  /** A `<Pane>`, or nothing when no record is open. */
  pane?: ReactNode | undefined;
  className?: string | undefined;
  testId?: string | undefined;
}) {
  const { user } = useAuth() ?? {};
  const [width, setWidth] = useState<number | null>(() => storedPaneWidth(user?.id));
  useEffect(() => setWidth(storedPaneWidth(user?.id)), [user?.id]);
  const resize = useCallback(
    (px: number) => {
      const next = clampPaneWidth(px);
      setWidth(next);
      storePaneWidth(user?.id, next);
    },
    [user?.id],
  );
  if (!pane) {
    return (
      <div className={className} data-testid={testId}>
        {children}
      </div>
    );
  }
  const style = width ? ({ "--pane-current": `${width}px` } as CSSProperties) : undefined;
  return (
    <div className={`split ${className}`.trim()} style={style} data-testid={testId}>
      <div>{children}</div>
      <PaneWidthContext.Provider value={{ width, resize }}>{pane}</PaneWidthContext.Provider>
    </div>
  );
}

export function Pane({
  label,
  children,
  testId,
}: {
  /** The open record's name, for the landmark. */
  label: string;
  children: ReactNode;
  testId?: string | undefined;
}) {
  const ctx = useContext(PaneWidthContext);
  const ref = useRef<HTMLElement>(null);
  const current = () => ctx?.width ?? ref.current?.getBoundingClientRect().width ?? PANE.width;

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (!ctx) return;
    e.preventDefault();
    const startX = e.clientX;
    const startW = current();
    const grip = e.currentTarget;
    grip.setPointerCapture(e.pointerId);
    const move = (ev: globalThis.PointerEvent) => ctx.resize(startW + (startX - ev.clientX));
    const up = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!ctx) return;
    const step = e.shiftKey ? 40 : 10;
    if (e.key === "ArrowLeft") ctx.resize(current() + step);
    else if (e.key === "ArrowRight") ctx.resize(current() - step);
    else return;
    e.preventDefault();
  };

  return (
    <aside className="pane" aria-label={label} ref={ref} data-testid={testId}>
      {ctx && (
        <div
          className="pane-grip"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize the record pane"
          aria-valuemin={PANE.min}
          aria-valuemax={PANE.max}
          aria-valuenow={Math.round(current())}
          tabIndex={0}
          onPointerDown={onPointerDown}
          onKeyDown={onKeyDown}
        />
      )}
      {children}
    </aside>
  );
}
