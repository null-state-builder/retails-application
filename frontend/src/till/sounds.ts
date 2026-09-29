// What the counter hears when a tag is scanned (#247, grill Q8).
//
// A cashier at a busy counter is looking at the customer, not at the screen. The
// scanner fires, a line lands, and the only honest confirmation the screen can
// give without being read is a sound - so a landed scan ticks, and anything that
// still needs a person buzzes: a barcode nothing could place, or a tag that
// belongs to two live seasons and is waiting to be told which.
//
// Two tones, and they must not be near neighbours. The whole value of the buzz
// is that a person who has already turned away from the screen notices it, so it
// is lower, longer and a harsher waveform than the tick rather than a second
// beep at another pitch.
//
// **Synthesised rather than bundled.** design.md said "two bundled samples"; two
// oscillator recipes are what shipped, deliberately. The till is a PWA that has
// to work with the line down - an audio file is a fetch that can miss the
// service worker's precache and go silent exactly when the shop is offline - and
// a committed binary is a sample no reviewer can inspect. Sixty bytes of numbers
// below are reviewable and cannot 404.
//
// Nothing here is allowed to matter. Sound is a courtesy on a screen whose real
// job is money: a browser that refuses to make one (autoplay policy, no audio
// device, a locked-down kiosk) must not cost a cashier the scan, so every path
// out of here is swallowed.

/** The two tones, by what they mean rather than by how they sound. */
export type Tone = "tick" | "buzz";

interface ToneSpec {
  /** `sine` reads as an instrument, `square` as a machine complaining. */
  wave: "sine" | "square";
  hz: number;
  ms: number;
  /** Peak gain. Loud enough over a shop, quiet enough to hear all day. */
  gain: number;
}

export const TONES: Record<Tone, ToneSpec> = {
  tick: { wave: "sine", hz: 1320, ms: 45, gain: 0.09 },
  buzz: { wave: "square", hz: 190, ms: 260, gain: 0.06 },
};

/** What a scan did, in the two terms the sound depends on. */
export interface ScanOutcome {
  /** The barcode matched a piece the counter holds. */
  resolved: boolean;
  /** That piece is live in more than one season, so the line is asking which
   *  (`BillGrid`'s season cell). The line *is* on the bill - this is a question,
   *  not a refusal - but it is not finished, and Q8 rules it a buzz. */
  ambiguous: boolean;
}

/**
 * Which tone a scan earns.
 *
 * Separated from the playing because this is the rule and that is a side effect:
 * "an ambiguous season buzzes even though the line landed" is the sort of thing
 * that gets quietly inverted in a refactor, and it is worth a test that does not
 * need an audio device to run.
 */
export function toneForScan(outcome: ScanOutcome): Tone {
  return outcome.resolved && !outcome.ambiguous ? "tick" : "buzz";
}

/** The slice of `AudioContext` this module actually uses. Structural, so a test
 *  can hand in a recorder and the real thing still satisfies it. */
export interface ToneAudio {
  currentTime: number;
  state: string;
  /** `unknown` because the real one answers a promise, and this module has no
   *  use for it - see `playTone`, which still has to catch its rejection. */
  resume(): unknown;
  destination: unknown;
  createOscillator(): {
    type: string;
    frequency: { value: number };
    connect(to: unknown): unknown;
    start(at: number): void;
    stop(at: number): void;
  };
  createGain(): {
    gain: {
      value: number;
      setValueAtTime(value: number, at: number): void;
      exponentialRampToValueAtTime(value: number, at: number): void;
    };
    connect(to: unknown): unknown;
  };
}

export type ToneAudioOpener = () => ToneAudio | null;

// One context for the life of the tab. A browser allows a small number of them
// and never reclaims one on its own, so opening a fresh one per scan would take
// a counter that bills all day to silence by the afternoon.
let shared: ToneAudio | null | undefined;

const webAudio: ToneAudioOpener = () => {
  if (shared !== undefined) return shared;
  const Ctor = (globalThis as { AudioContext?: new () => ToneAudio }).AudioContext;
  // `null` and not `undefined`: undefined means "not asked yet", and a browser
  // with no Web Audio at all must be asked exactly once.
  shared = Ctor ? new Ctor() : null;
  return shared;
};

/**
 * Make the sound, unless the counter is muted.
 *
 * The mute check is a parameter rather than something each caller remembers to
 * write, so every scan path goes through the one gate: the toggle on Till & Sync
 * is the only thing standing between a cashier and a noise they cannot stop.
 * The toggle itself passes `false` on purpose - pressing "turn the sounds on"
 * plays the tick it just turned on, which is the only honest answer to "is the
 * sound working?" on a machine whose volume might be down.
 *
 * A context that the browser suspended (nothing has been clicked in this tab
 * yet) is resumed rather than skipped: the first scan of a shift is exactly the
 * one worth confirming, and typing into the scan box is the gesture that lets it
 * through.
 */
export function playTone(tone: Tone, muted: boolean, open: ToneAudioOpener = webAudio): void {
  if (muted) return;
  try {
    const audio = open();
    if (!audio) return;
    // The real `resume` answers a promise, and a rejected one thrown from
    // inside a `try` block is not caught by it - it escapes as an unhandled
    // rejection in a shop's browser. Swallowed like everything else here.
    if (audio.state === "suspended") {
      void Promise.resolve(audio.resume()).catch(() => undefined);
    }

    const spec = TONES[tone];
    const now = audio.currentTime;
    const until = now + spec.ms / 1000;
    const oscillator = audio.createOscillator();
    const level = audio.createGain();
    oscillator.type = spec.wave;
    oscillator.frequency.value = spec.hz;
    level.gain.setValueAtTime(spec.gain, now);
    // Ramped to a hair above nought rather than to nought - an exponential ramp
    // to zero is undefined and some browsers drop the whole envelope, leaving a
    // tone that ends with a click instead of fading.
    level.gain.exponentialRampToValueAtTime(0.0001, until);
    oscillator.connect(level);
    level.connect(audio.destination);
    oscillator.start(now);
    oscillator.stop(until);
  } catch {
    // See the module note: a scan is never lost over a sound.
  }
}
