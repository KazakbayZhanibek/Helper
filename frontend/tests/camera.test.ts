import assert from "node:assert/strict";
import test from "node:test";
import {
  CameraCaptureController,
  CAMERA_MAX_BYTES,
  CAMERA_MAX_MS,
  cameraError,
  frameDimensions,
  recordingBoundary,
  recordingMime,
  type CaptureResult,
} from "../src/camera.ts";

class FakeRecorder {
  static last: FakeRecorder;
  static isTypeSupported(type: string) {
    return type === "video/webm;codecs=vp8,opus";
  }
  state = "inactive";
  mimeType: string;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(_stream: unknown, options: { mimeType: string }) {
    this.mimeType = options.mimeType;
    FakeRecorder.last = this;
  }
  start(timeslice: number) {
    assert.equal(timeslice, 1000);
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    this.onstop?.();
  }
  chunk(blob: Blob) {
    this.ondataavailable?.({ data: blob });
  }
}
function fixture() {
  let clock = 0,
    stopped = 0,
    requested = 0;
  let constraints: MediaStreamConstraints | undefined;
  let ended: (() => void) | undefined;
  let previewStopped = 0;
  const stream = {
    getTracks: () => [
      {
        addEventListener: (_name: string, listener: () => void) => {
          ended = listener;
        },
        stop: () => {
          stopped++;
        },
      },
    ],
  } as unknown as MediaStream;
  let result: CaptureResult | undefined;
  const errors: string[] = [];
  const controller = new CameraCaptureController({
    devices: {
      getUserMedia: async (options) => {
        requested++;
        constraints = options;
        return stream;
      },
    },
    recorder: FakeRecorder as unknown as typeof MediaRecorder,
    now: () => clock,
    onProgress: () => {},
    onStop: (value) => {
      result = value;
    },
    onError: (message) => errors.push(message),
    onPreviewStopped: () => {
      previewStopped++;
    },
  });
  return {
    controller,
    errors,
    setTime: (ms: number) => {
      clock = ms;
    },
    requested: () => requested,
    stopped: () => stopped,
    result: () => result,
    constraints: () => constraints,
    endTrack: () => ended?.(),
    previewStopped: () => previewStopped,
  };
}
test("camera remains off until explicit open, requests selected audio and stops tracks with recording", async () => {
  const f = fixture();
  assert.equal(f.requested(), 0);
  assert.throws(() => f.controller.start(), /Сначала включите/);
  await f.controller.open(false);
  assert.equal(f.requested(), 1);
  assert.equal(f.constraints()?.audio, false);
  f.controller.start();
  FakeRecorder.last.chunk(new Blob(["real recorded bytes"]));
  f.setTime(12000);
  f.controller.stop();
  assert.ok(f.stopped() >= 1);
  assert.equal(f.result()?.durationMs, 12000);
  assert.equal(await f.result()?.file.text(), "real recorded bytes");
  assert.match(f.result()!.file.name, /\.webm$/);
  assert.equal(f.result()?.truncated, false);
});
test("recording stops at ten minutes and leaves a downloadable recording", async () => {
  const f = fixture();
  await f.controller.open(true);
  f.controller.start();
  FakeRecorder.last.chunk(new Blob(["recording"]));
  f.setTime(CAMERA_MAX_MS + 100);
  f.controller.checkLimits();
  assert.equal(FakeRecorder.last.state, "inactive");
  assert.equal(f.result()?.durationMs, CAMERA_MAX_MS);
  assert.equal(f.result()?.file.size, 9);
  assert.match(f.errors[0], /10 минут/);
});
test("hard size cap rejects an overflowing chunk without allocating or retaining it", async () => {
  const f = fixture();
  await f.controller.open(false);
  f.controller.start();
  FakeRecorder.last.chunk(new Blob(["kept"]));
  FakeRecorder.last.chunk({ size: CAMERA_MAX_BYTES + 1 } as Blob);
  assert.equal(f.result()?.truncated, true);
  assert.equal(await f.result()?.file.text(), "kept");
  assert.match(f.errors[0], /500 MiB/);
});
test("late camera permission after unmount immediately closes tracks", async () => {
  let resolve!: (stream: MediaStream) => void,
    stopped = false;
  const controller = new CameraCaptureController({
    devices: {
      getUserMedia: () =>
        new Promise<MediaStream>((done) => {
          resolve = done;
        }),
    },
    recorder: FakeRecorder as unknown as typeof MediaRecorder,
    onProgress: () => {},
    onStop: () => assert.fail("disposed recording must not update UI"),
    onError: () => {},
  });
  const pending = controller.open(false);
  controller.dispose();
  resolve({
    getTracks: () => [
      {
        stop: () => {
          stopped = true;
        },
      },
    ],
  } as unknown as MediaStream);
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(stopped, true);
});
test("unmount during recording stops capture and discards memory without emitting a result", async () => {
  const f = fixture();
  await f.controller.open(false);
  f.controller.start();
  FakeRecorder.last.chunk(new Blob(["unfinished"]));
  f.controller.dispose();
  assert.ok(f.stopped() >= 1);
  assert.equal(FakeRecorder.last.state, "inactive");
  assert.equal(f.result(), undefined);
});
test("camera denial is explained, and images scale without cropping or enlargement", () => {
  assert.match(cameraError({ name: "NotAllowedError" }), /не разрешён/);
  assert.match(cameraError({ name: "NotFoundError" }), /не найдена/);
  assert.deepEqual(frameDimensions(1920, 1080), { width: 960, height: 540 });
  assert.deepEqual(frameDimensions(1080, 1920), { width: 540, height: 960 });
  assert.deepEqual(frameDimensions(320, 240), { width: 320, height: 240 });
});
test("format selection is browser-supported and reserves room below the hard byte cap", () => {
  assert.equal(
    recordingMime((type) => type === "video/mp4"),
    "video/mp4",
  );
  assert.equal(
    recordingMime(() => false),
    null,
  );
  assert.equal(recordingBoundary(CAMERA_MAX_MS - 1, 0), null);
  assert.equal(recordingBoundary(CAMERA_MAX_MS, 0), "duration");
  assert.equal(
    recordingBoundary(1, CAMERA_MAX_BYTES - 5 * 1024 * 1024),
    "size",
  );
});

test("a disconnected preview stops its tracks and lets the UI reopen the camera", async () => {
  const f = fixture();
  await f.controller.open(false);
  assert.equal(f.controller.hasStream, true);
  f.endTrack();
  assert.equal(f.controller.hasStream, false);
  assert.equal(f.previewStopped(), 1);
  assert.ok(f.stopped() >= 1);
  assert.equal(f.result(), undefined);
  assert.throws(() => f.controller.start(), /Сначала включите/);
  f.endTrack();
  assert.equal(f.previewStopped(), 1);
});

test("a delayed recorder stop event and repeated stop keep the actual stop time", async () => {
  const f = fixture();
  await f.controller.open(false);
  f.controller.start();
  FakeRecorder.last.chunk(new Blob(["recorded"]));
  const finish = FakeRecorder.last.onstop!;
  FakeRecorder.last.onstop = null;
  f.setTime(12_345);
  f.controller.stop();
  f.setTime(20_000);
  f.controller.stop();
  finish();
  assert.equal(f.result()?.durationMs, 12_345);
  assert.equal(await f.result()?.file.text(), "recorded");
});

test("a spontaneous recorder stop records the final duration between progress ticks", async () => {
  const f = fixture();
  await f.controller.open(false);
  f.controller.start();
  FakeRecorder.last.chunk(new Blob(["recorded"]));
  f.setTime(12_345);
  FakeRecorder.last.stop();
  assert.equal(f.result()?.durationMs, 12_345);
  assert.equal(f.controller.hasStream, false);
});
