import { createContext, useContext, useEffect } from "react";

/**
 * Keeps the mobile drawer and a top-bar popup (switcher, search, bell, user
 * menu) mutually exclusive, in whichever order they are opened.
 *
 * The drawer and its backdrop sit at z-index 55/60 so they can slide in over
 * the page without the fixed shell rewiring every popup's stacking, but that
 * leaves the popups (40/50) rendering underneath the drawer if both are open
 * at once. Closing one when the other opens - instead of raising the popups -
 * keeps that z-order exactly as designed (`AppShell.css`).
 *
 * AppShell provides the real state; the default lets a popup rendered outside
 * the shell (tests, Storybook) behave as if the drawer never opens.
 */
export const MobileNavContext = createContext<{ open: boolean; close: () => void }>({
  open: false,
  close: () => {},
});

/** Call from a popup's own `open` state: closes the drawer the moment the
 *  popup opens, and closes the popup back the moment the drawer opens - so
 *  whichever one is opened second wins, and the shell's z-order never has to
 *  show both at once. */
export function useMobileNavExclusion(popupOpen: boolean, setPopupOpen: (open: boolean) => void): void {
  const nav = useContext(MobileNavContext);
  const { close } = nav;
  // Keyed on `close` (stable), not the whole `nav` object: the object is
  // rebuilt whenever `nav.open` flips, and keying this effect on it too would
  // re-fire `close()` on every drawer-open re-render while a popup already
  // happened to be open - reopening the drawer only to immediately close it.
  useEffect(() => {
    if (popupOpen) close();
  }, [popupOpen, close]);
  useEffect(() => {
    if (nav.open) setPopupOpen(false);
  }, [nav.open, setPopupOpen]);
}
