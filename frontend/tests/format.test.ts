import assert from "node:assert/strict";
import test from "node:test";
import {
  countNoun,
  formatMs,
  formatMsExact,
  MAX_VIDEO_MS,
  parseMs,
} from "../src/format.ts";

test("exact video endpoints preserve milliseconds without crossing the source boundary", () => {
  assert.equal(formatMsExact(25_583), "00:25.583");
  assert.equal(parseMs(formatMsExact(25_583)), 25_583);
  for (const ms of [
    0,
    25_583,
    29_999,
    30_000,
    59_950,
    59_999,
    60_000,
    60_001,
    599_999,
    MAX_VIDEO_MS,
  ]) {
    assert.equal(parseMs(formatMsExact(ms)), ms);
  }
  assert.equal(formatMsExact(59_999), "00:59.999");
  assert.equal(formatMsExact(60_000), "01:00.000");
  assert.equal(formatMsExact(MAX_VIDEO_MS), "10:00.000");
});

test("exact boundaries reject invalid and fractional millisecond values", () => {
  for (const ms of [-1, 1.5, NaN, Infinity, -Infinity, MAX_VIDEO_MS + 1]) {
    assert.equal(formatMsExact(ms), "");
  }
});

test("rounding at a minute boundary never produces invalid 00:60.0", () => {
  assert.equal(formatMs(59_949), "00:59.9");
  assert.equal(formatMs(59_950), "01:00.0");
  assert.equal(formatMs(59_999), "01:00.0");
  assert.equal(parseMs(formatMs(59_950)), 60_000);
});

test("editable time round-trips to the nearest tenth across the video limit", () => {
  for (let ms = 0; ms <= MAX_VIDEO_MS; ms += 37) {
    assert.equal(parseMs(formatMs(ms)), Math.round(ms / 100) * 100);
  }
  assert.equal(formatMs(0), "00:00.0");
  assert.equal(parseMs("00:00.0"), 0);
  assert.equal(formatMs(MAX_VIDEO_MS), "10:00.0");
  assert.equal(parseMs("10:00.000"), MAX_VIDEO_MS);
});

test("time accepts the documented decimal separators and millisecond precision", () => {
  assert.equal(parseMs(" 01:02.345 "), 62_345);
  assert.equal(parseMs("01:02,3"), 62_300);
  assert.equal(parseMs("01:02.03"), 62_030);
  assert.equal(parseMs("1:02"), 62_000);
});

test("invalid seconds, negative values, nonfinite text, and overflow remain invalid", () => {
  for (const value of [
    "00:60.0",
    "00:99",
    "-01:00",
    "NaN",
    "Infinity",
    "",
    "1",
    "1:2",
    "00:00.0001",
    "00:00.",
    "10:00.001",
    "999:59.999",
    "1000:00",
  ]) {
    assert.ok(Number.isNaN(parseMs(value)), value);
  }
  for (const ms of [-1, NaN, Infinity, -Infinity, MAX_VIDEO_MS + 1]) {
    assert.equal(formatMs(ms), "");
    assert.ok(Number.isNaN(parseMs(formatMs(ms))));
  }
});

test("Russian count forms cover zero, teens, tens, and hundreds", () => {
  const forms = ["инструкция", "инструкции", "инструкций"] as const;
  const expected = new Map([
    [0, "инструкций"],
    [1, "инструкция"],
    [2, "инструкции"],
    [4, "инструкции"],
    [5, "инструкций"],
    [11, "инструкций"],
    [12, "инструкций"],
    [14, "инструкций"],
    [20, "инструкций"],
    [21, "инструкция"],
    [22, "инструкции"],
    [25, "инструкций"],
    [101, "инструкция"],
    [111, "инструкций"],
    [114, "инструкций"],
  ]);
  for (const [count, word] of expected)
    assert.equal(countNoun(count, forms), word);
  assert.equal(countNoun(4, ["шаг", "шага", "шагов"]), "шага");
  assert.equal(countNoun(1, ["действие", "действия", "действий"]), "действие");
});
