/* Breakpoints (design system v2, PagePanes) for code that has to know them:
   pane docking and table column priority. CSS uses the same numbers. */
export const BREAKPOINTS = {
  /** Below this the record opens as a drawer; from here it docks as a pane. */
  laptop: 1200,
  /** The pane widens to 420px, the sidebar stays open, P2 columns appear. */
  wide: 1600,
  /** An optional filter rail, P3 columns. */
  xwide: 1920,
  /** Two records side by side. */
  ultra: 2400,
} as const;

export const PANE = { width: 360, min: 320, max: 440, wide: 420 } as const;
