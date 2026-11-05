import assert from "node:assert/strict";
import test from "node:test";
import {
  LiveFrameQueue,
  prepareLiveSession,
  type PendingLiveFrame,
} from "../src/liveFrames.ts";

function options(signal = new AbortController().signal) {
  let clock = 20_000,
    keys = 0;
  return {
    signal,
    maxFrames: 300,
    maxBytes: 524288,
    now: () => clock,
    createKey: () => `frame-key-${++keys}`,
    setTime: (time: number) => {
      clock = time;
    },
    keyCount: () => keys,
  };
}

test("a failed frame POST retries the same bytes, timestamp, sequence and key before advancing", async () => {
  const config = options();
  const attempts: PendingLiveFrame[] = [];
  let captureCount = 0;
  const queue = new LiveFrameQueue({
    ...config,
    send: async (frame) => {
      attempts.push(frame);
      if (attempts.length === 1)
        throw new Error(
          "Network disconnected before server accepted the frame",
        );
    },
  });
  const capture = async () => {
    captureCount++;
    return new Blob([`pixels-${captureCount}`]);
  };
  config.setTime(22_000);
  await assert.rejects(queue.sendNext(capture), /Network disconnected/);
  assert.equal(queue.sent, 0);
  assert.equal(queue.hasPending, true);
  config.setTime(24_000);
  assert.equal(await queue.sendNext(capture), true);
  assert.equal(
    attempts[1],
    attempts[0],
    "the whole pending request must stay identical",
  );
  assert.equal(await attempts[1].blob.text(), "pixels-1");
  assert.equal(attempts[1].timestampMs, 2000);
  assert.equal(attempts[1].sequence, 1);
  assert.equal(attempts[1].key, "frame-key-1");
  assert.equal(captureCount, 1);
  assert.equal(config.keyCount(), 1);
  assert.equal(queue.sent, 1);
  assert.equal(queue.hasPending, false);
  config.setTime(26_000);
  await queue.sendNext(capture);
  assert.equal(attempts[2].sequence, 2);
  assert.equal(attempts[2].timestampMs, 6000);
  assert.equal(attempts[2].key, "frame-key-2");
  assert.equal(await attempts[2].blob.text(), "pixels-2");
  queue.cancel();
});

test("an accepted frame whose response was lost is recovered by idempotent retry", async () => {
  const config = options();
  const server = new Map<string, PendingLiveFrame>();
  let lostResponse = true;
  const queue = new LiveFrameQueue({
    ...config,
    send: async (frame) => {
      const previous = server.get(frame.key);
      if (previous) {
        assert.equal(frame.blob, previous.blob);
        assert.equal(frame.timestampMs, previous.timestampMs);
        assert.equal(frame.sequence, previous.sequence);
        return { id: "existing-frame" };
      }
      assert.equal(
        frame.sequence,
        server.size + 1,
        "server accepts only the next sequence",
      );
      server.set(frame.key, frame);
      if (lostResponse) {
        lostResponse = false;
        throw new Error("Response lost after commit");
      }
      return { id: "new-frame" };
    },
  });
  const capture = async () => new Blob(["JPEG"]);
  await assert.rejects(queue.sendNext(capture), /Response lost/);
  assert.equal(server.size, 1);
  assert.equal(queue.sent, 0);
  await queue.sendNext(capture);
  assert.equal(server.size, 1, "retry must not create a duplicate frame");
  assert.equal(queue.sent, 1);
  config.setTime(24_000);
  await queue.sendNext(capture);
  assert.equal(server.size, 2);
  assert.equal(queue.sent, 2);
  queue.cancel();
});

test("live timestamps start after session creation, excluding preparation and recording setup time", async () => {
  const config = options();
  // Recording began at 0; saving and session creation took 20 seconds.
  const sent: PendingLiveFrame[] = [];
  const queue = new LiveFrameQueue({
    ...config,
    send: async (frame) => {
      sent.push(frame);
    },
  });
  config.setTime(22_000);
  await queue.sendNext(async () => new Blob(["first session frame"]));
  assert.equal(sent[0].timestampMs, 2000);
  assert.ok(
    sent[0].timestampMs <= 2000 + 5000,
    "timestamp stays within server session clock tolerance",
  );
  queue.cancel();
});

test("cancellation clears a retained frame and never captures or retries after stop", async () => {
  const controller = new AbortController();
  const config = options(controller.signal);
  let calls = 0;
  const queue = new LiveFrameQueue({
    ...config,
    send: async () => {
      calls++;
      throw new Error("offline");
    },
  });
  await assert.rejects(
    queue.sendNext(async () => new Blob(["retained pixels"])),
    /offline/,
  );
  assert.equal(queue.hasPending, true);
  controller.abort();
  assert.equal(queue.hasPending, false);
  assert.equal(
    await queue.sendNext(async () =>
      assert.fail("stopped camera must not capture"),
    ),
    false,
  );
  assert.equal(calls, 1);
  assert.equal(queue.sent, 0);
});

test("a response arriving after cancellation cannot restore pending data or acknowledge a frame", async () => {
  const controller = new AbortController();
  let finish!: () => void;
  const queue = new LiveFrameQueue({
    ...options(controller.signal),
    send: () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
  });
  const pending = queue.sendNext(async () => new Blob(["JPEG"]));
  await Promise.resolve();
  assert.equal(queue.hasPending, true);
  controller.abort();
  finish();
  assert.equal(await pending, false);
  assert.equal(queue.hasPending, false);
  assert.equal(queue.sent, 0);
});

test("cancel during JPEG encoding discards the result and serializes concurrent ticks", async () => {
  const controller = new AbortController();
  let finish!: (blob: Blob) => void;
  const queue = new LiveFrameQueue({
    ...options(controller.signal),
    send: async () => assert.fail("encoding cancelled before upload"),
  });
  const pending = queue.sendNext(
    () =>
      new Promise<Blob>((resolve) => {
        finish = resolve;
      }),
  );
  assert.equal(
    await queue.sendNext(async () => assert.fail("only one capture may run")),
    false,
  );
  controller.abort();
  finish(new Blob(["late pixels"]));
  assert.equal(await pending, false);
  assert.equal(queue.hasPending, false);
  assert.equal(queue.sent, 0);
});

test("stopping while a working copy saves prevents creation of a live session", async () => {
  const abort = new AbortController();
  let saved!: (working: { revision: number }) => void;
  const pending = prepareLiveSession({
    signal: abort.signal,
    prepare: () =>
      new Promise<{ revision: number }>((resolve) => {
        saved = resolve;
      }),
    create: async () =>
      assert.fail("a stopped recording must not create a session"),
    cancel: async () => assert.fail("there is no session to cancel"),
  });
  abort.abort();
  saved({ revision: 2 });
  assert.equal(await pending, null);
});

test("a late session belongs to its stopped recording and cannot replace a newer session", async () => {
  const oldAttempt = new AbortController();
  let created!: (session: { id: string }) => void;
  const cancelled: string[] = [];
  const old = prepareLiveSession({
    signal: oldAttempt.signal,
    prepare: async () => ({ revision: 1 }),
    create: () =>
      new Promise<{ id: string }>((resolve) => {
        created = resolve;
      }),
    cancel: async (session) => {
      cancelled.push(session.id);
    },
  });
  await Promise.resolve();
  oldAttempt.abort();
  const current = await prepareLiveSession({
    signal: new AbortController().signal,
    prepare: async () => ({ revision: 1 }),
    create: async () => ({ id: "current-session" }),
    cancel: async () => assert.fail("the current recording stays active"),
  });
  created({ id: "old-session" });
  assert.equal(await old, null);
  assert.equal(current?.id, "current-session");
  assert.deepEqual(cancelled, ["old-session"]);
});
