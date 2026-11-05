export type PendingLiveFrame = Readonly<{
  blob: Blob;
  sequence: number;
  timestampMs: number;
  key: string;
}>;

/** A stopped recording must never adopt a session created by its late request. */
export async function prepareLiveSession<TWorking, TSession>(options: {
  signal: AbortSignal;
  prepare: () => Promise<TWorking | null>;
  create: (working: TWorking) => Promise<TSession>;
  cancel: (session: TSession) => Promise<void>;
}): Promise<TSession | null> {
  if (options.signal.aborted) return null;
  const working = await options.prepare();
  if (!working || options.signal.aborted) return null;
  const session = await options.create(working);
  if (options.signal.aborted) {
    await options.cancel(session);
    return null;
  }
  return session;
}

type LiveFrameOptions = {
  signal: AbortSignal;
  maxFrames: number;
  maxBytes: number;
  createKey: () => string;
  send: (frame: PendingLiveFrame, signal: AbortSignal) => Promise<unknown>;
  now?: () => number;
};

/** One pending frame survives network failure until the server acknowledges it. */
export class LiveFrameQueue {
  private options: LiveFrameOptions;
  private sessionStartedAt: number;
  private pending: PendingLiveFrame | null = null;
  private busy = false;
  private cancelled = false;
  private acknowledged = 0;
  private onAbort = () => this.cancel();

  constructor(options: LiveFrameOptions) {
    this.options = options;
    // Construct only after creating the live session, separately from recording.
    this.sessionStartedAt = this.now();
    if (options.signal.aborted) this.cancel();
    else options.signal.addEventListener("abort", this.onAbort, { once: true });
  }
  private now() {
    return this.options.now ? this.options.now() : performance.now();
  }
  get sent() {
    return this.acknowledged;
  }
  get hasPending() {
    return this.pending !== null;
  }

  async sendNext(capture: () => Promise<Blob | null>): Promise<boolean> {
    if (
      this.cancelled ||
      this.busy ||
      this.acknowledged >= this.options.maxFrames
    )
      return false;
    this.busy = true;
    try {
      if (!this.pending) {
        const timestampMs = Math.max(
          0,
          Math.round(this.now() - this.sessionStartedAt),
        );
        const blob = await capture();
        if (this.cancelled || !blob) return false;
        if (blob.size > this.options.maxBytes)
          throw new Error(
            "Кадр слишком большой для анализа. Запись продолжается локально.",
          );
        this.pending = {
          blob,
          timestampMs,
          sequence: this.acknowledged + 1,
          key: this.options.createKey(),
        };
      }
      await this.options.send(this.pending, this.options.signal);
      if (this.cancelled) return false;
      this.acknowledged += 1;
      this.pending = null;
      return true;
    } finally {
      this.busy = false;
    }
  }

  cancel() {
    this.cancelled = true;
    this.pending = null;
    this.options.signal.removeEventListener("abort", this.onAbort);
  }
}
