/** A tiny generation gate for async answers tied to rapidly changing counter
 * state. Starting a request invalidates every older answer; explicit state
 * changes can invalidate the current one without starting another. */
export class LatestRequest {
  private generation = 0;

  start(): () => boolean {
    const mine = ++this.generation;
    return () => mine === this.generation;
  }

  invalidate(): void {
    this.generation += 1;
  }
}
