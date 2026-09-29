/**
 * The two pickers on the new-transfer screen, which ask two different questions
 * and so must never share one list (#147).
 *
 * The *source* comes from `/masters/stores` — the units this person may operate
 * on. The *destination* comes from `/masters/locations` — the whole network,
 * cross-state included, because sending stock somewhere claims no rights at the
 * place it is going. `masters.views.LocationListView` carries the full argument.
 */

export interface LocationT {
  id: number;
  code: string;
  name: string;
  store_type: string;
  gstin: number | null;
  state_code: string;
  state_name: string;
}

/** Everywhere this transfer may go: the whole network, minus where it starts. */
export function destinationOptions(locations: LocationT[], sourceId: string): LocationT[] {
  return locations.filter((l) => String(l.id) !== sourceId);
}

/**
 * Do the two ends sit under different GST registrations (Bihar ↔ Jharkhand)?
 *
 * A taxable IGST supply needing an e-way bill, so the screen asks for one.
 * Registrations are compared rather than state codes because that is the
 * question the server answers when it stores the flag
 * (`StoreTransfer.save()` → `source.gstin_id != destination.gstin_id`): ask a
 * different one here and the screen could stay silent about a transfer the
 * books have already recorded as cross-state, leaving its e-way bill blank.
 *
 * Unknown either end (lists still loading) is not cross-state — the same answer
 * the screen gave before either dropdown was touched.
 */
export function isCrossState(
  locations: LocationT[],
  sourceId: string,
  destId: string,
): boolean {
  if (!sourceId || !destId) return false;
  const src = locations.find((l) => String(l.id) === sourceId);
  const dst = locations.find((l) => String(l.id) === destId);
  if (!src || !dst) return false;
  return src.gstin !== dst.gstin;
}
