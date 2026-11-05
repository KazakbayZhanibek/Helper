import assert from "node:assert/strict";
import test from "node:test";
import { recoveryKey, validRecovery, RECOVERY_TTL_MS } from "../src/cameraRecovery.ts";

const key = recoveryKey("author", "workshop", "/instructions/one");
function fixture() {
  return { key, token: "recording-one", savedAt: 1000, file: new File(["video-bytes"], "camera.webm", { type: "video/webm" }), durationMs: 20_960, truncated: false };
}
test("recovery is scoped to account, organization and instruction", () => {
  const r = fixture();
  assert.ok(validRecovery(r, key, 1001));
  for (const other of [recoveryKey("another", "workshop", "/instructions/one"), recoveryKey("author", "another", "/instructions/one"), recoveryKey("author", "workshop", "/instructions/two")])
    assert.equal(validRecovery(r, other, 1001), false);
  assert.notEqual(recoveryKey("a:b", "c", "d"), recoveryKey("a", "b:c", "d"));
});
test("recovery expires at 24h and rejects future/corrupt timestamps", () => {
  const r = fixture();
  assert.ok(validRecovery(r, key, r.savedAt + RECOVERY_TTL_MS - 1));
  assert.equal(validRecovery(r, key, r.savedAt + RECOVERY_TTL_MS), false);
  assert.equal(validRecovery(r, key, r.savedAt - 1), false);
  assert.equal(validRecovery({ ...r, savedAt: NaN }, key, 1001), false);
});
test("recovery preserves exact duration and truncated flag; rejects corrupt payloads", () => {
  assert.ok(validRecovery({ ...fixture(), truncated: true }, key, 1001));
  for (const patch of [{ durationMs: NaN }, { durationMs: -1 }, { durationMs: 600001 }, { truncated: null }, { token: "" }, { file: null }, { file: new File([], "empty.webm", { type: "video/webm" }) }, { file: new File(["html"], "x.html", { type: "text/html" }) }])
    assert.equal(validRecovery({ ...fixture(), ...patch }, key, 1001), false);
});
