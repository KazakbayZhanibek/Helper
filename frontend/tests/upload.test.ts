import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, uploadVideo } from "../src/api.ts";

class FakeXhr {
  static last: FakeXhr;
  upload: {
    onprogress?: (event: {
      loaded: number;
      total: number;
      lengthComputable: boolean;
    }) => void;
  } = {};
  status = 202;
  responseText = '{"job_id":"job-1"}';
  headers: Record<string, string> = {};
  withCredentials = false;
  onload?: () => void;
  onerror?: () => void;
  onabort?: () => void;
  path = "";
  body: unknown;
  aborted = false;
  constructor() {
    FakeXhr.last = this;
  }
  open(method: string, path: string) {
    assert.equal(method, "POST");
    this.path = path;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: unknown) {
    this.body = body;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
}
Object.defineProperty(globalThis, "document", {
  configurable: true,
  value: { cookie: "csrftoken=protected-token" },
});
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: new EventTarget(),
});
Object.defineProperty(globalThis, "XMLHttpRequest", {
  configurable: true,
  value: FakeXhr,
});
const file = new File([new Uint8Array(12)], "process.mp4", {
  type: "video/mp4",
});

test("binary upload includes CSRF and credentials, preserves bytes, reports actual transfer", async () => {
  const controller = new AbortController();
  const progress: number[][] = [];
  const pending = uploadVideo<{ job_id: string }>(
    "/orgs/o/uploads/u/content",
    file,
    (loaded, total) => progress.push([loaded, total]),
    controller.signal,
  );
  const xhr = FakeXhr.last;
  assert.equal(xhr.body, file);
  assert.equal(xhr.headers["Content-Type"], "application/octet-stream");
  assert.equal(xhr.headers["X-CSRFToken"], "protected-token");
  assert.equal(xhr.withCredentials, true);
  assert.equal(xhr.path, "/api/v1/orgs/o/uploads/u/content");
  xhr.upload.onprogress?.({ loaded: 4, total: 12, lengthComputable: true });
  xhr.upload.onprogress?.({ loaded: 8, total: 0, lengthComputable: false });
  assert.deepEqual(progress, [
    [4, 12],
    [8, 12],
  ]);
  xhr.onload?.();
  assert.deepEqual(await pending, { job_id: "job-1" });
  controller.abort();
  assert.equal(
    xhr.aborted,
    false,
    "completed request must detach its abort listener",
  );
});
test("cancel aborts an active XHR without claiming successful upload", async () => {
  const controller = new AbortController();
  const pending = uploadVideo("/upload", file, () => {}, controller.signal);
  const rejection = assert.rejects(pending, { name: "AbortError" });
  controller.abort();
  await rejection;
  assert.equal(FakeXhr.last.aborted, true);
});
test("expired upload sessions trigger relogin and retain the API error code", async () => {
  let expired = false;
  window.addEventListener(
    "session-expired",
    () => {
      expired = true;
    },
    { once: true },
  );
  const pending = uploadVideo(
    "/upload",
    file,
    () => {},
    new AbortController().signal,
  );
  FakeXhr.last.status = 401;
  FakeXhr.last.responseText =
    '{"error":{"code":"UNAUTHENTICATED","message":"Сессия истекла"}}';
  FakeXhr.last.onload?.();
  await assert.rejects(
    pending,
    (error: unknown) =>
      error instanceof ApiError &&
      error.status === 401 &&
      error.code === "UNAUTHENTICATED",
  );
  assert.equal(expired, true);
});
test("network interruption is a retryable user-visible failure, never a completed upload", async () => {
  const pending = uploadVideo(
    "/upload",
    file,
    () => {},
    new AbortController().signal,
  );
  FakeXhr.last.onerror?.();
  await assert.rejects(pending, /Файл не загружен полностью/);
});

test("visual frame upload sends real JPEG with scoped sequence, time and CSRF metadata", async () => {
  const { uploadFrame } = await import("../src/api.ts");
  const originalFetch = globalThis.fetch;
  const picture = new Blob([new Uint8Array([255, 216, 255, 217])], {
    type: "image/jpeg",
  });
  let called = false;
  globalThis.fetch = (async (url, options) => {
    called = true;
    assert.equal(url, "/api/v1/orgs/o/visual-sessions/live/frames");
    assert.equal(options?.body, picture);
    assert.equal(options?.credentials, "same-origin");
    const headers = options?.headers as Record<string, string>;
    assert.equal(headers["Content-Type"], "image/jpeg");
    assert.equal(headers["X-CSRFToken"], "protected-token");
    assert.equal(headers["X-Frame-Sequence"], "3");
    assert.equal(headers["X-Timestamp-Ms"], "6024");
    assert.equal(headers["Idempotency-Key"], "frame-key");
    return new Response('{"id":"frame-3"}', { status: 201 });
  }) as typeof fetch;
  try {
    assert.deepEqual(
      await uploadFrame(
        "/orgs/o/visual-sessions/live/frames",
        picture,
        3,
        6024,
        "frame-key",
        new AbortController().signal,
      ),
      { id: "frame-3" },
    );
    assert.equal(called, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
