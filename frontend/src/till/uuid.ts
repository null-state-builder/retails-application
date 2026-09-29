// One v4 UUID generator for the whole till layer.
//
// It was private to `numbering.ts`, where it makes the key that keeps the server
// idempotent. The hold list needs one too - a parked cart has to be identifiable
// across a reload and up to the server - and two copies of a random-number
// fallback is the sort of thing that drifts into one of them not being v4.

/** A v4 UUID, from the platform where there is one. */
export function newUuid(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi?.randomUUID) return cryptoApi.randomUUID();
  // Chrome is the standardised till (grill Q5) and has had `randomUUID` on
  // secure origins for years, so this is for a plain-http dev box, not for a
  // counter. It is still a v4 shape, so nothing downstream can tell.
  return "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) => {
    const n = Number(c);
    return (n ^ (Math.floor(Math.random() * 256) & (15 >> (n / 4)))).toString(16);
  });
}
