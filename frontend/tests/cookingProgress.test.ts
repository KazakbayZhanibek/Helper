import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  cookingProgressKey,
  emptyCookingProgress,
  openCookingProgress,
} from "../src/cookingProgress.ts";

function device() {
  const entries = new Map<string, string>();
  return {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => {
      entries.set(key, value);
    },
    removeItem: (key: string) => {
      entries.delete(key);
    },
  };
}

test("a published recipe resumes on this device, with independent progress for each account, kitchen, recipe and version", () => {
  const disk = device();
  const key = cookingProgressKey("a", "b", "c", "d");
  const open = (name: string) => openCookingProgress(name, 3, 5, () => disk);
  const saved = { active: 2, finished: true, prepared: [0, 4] };
  open(key).update(() => saved);
  assert.deepEqual(open(key).read(), saved);
  for (const scope of [
    cookingProgressKey("other", "b", "c", "d"),
    cookingProgressKey("a", "other", "c", "d"),
    cookingProgressKey("a", "b", "other", "d"),
    cookingProgressKey("a", "b", "c", "other"),
  ]) {
    assert.deepEqual(open(scope).read(), emptyCookingProgress);
  }
  open(key).update(() => emptyCookingProgress);
  assert.equal(disk.getItem(key), null);
  assert.deepEqual(open(key).read(), emptyCookingProgress);
});

test("a second open tab preserves another tab's ingredient selection while advancing a step", () => {
  const disk = device();
  const open = () => openCookingProgress("k", 3, 5, () => disk);
  const first = open(),
    second = open();
  second.read();
  first.update((previous) => ({ ...previous, prepared: [1] }));
  second.update((previous) => ({ ...previous, active: 1 }));
  assert.deepEqual(first.read(), { active: 1, finished: false, prepared: [1] });
});

test("obsolete or corrupt values never select an unavailable ingredient or step", () => {
  const disk = device();
  const store = openCookingProgress("k", 3, 4, () => disk);
  for (const invalid of [
    "truncated",
    "null",
    '{"active":99,"prepared":[],"finished":false}',
  ]) {
    disk.setItem("k", invalid);
    assert.deepEqual(store.read(), emptyCookingProgress);
  }
  disk.setItem(
    "k",
    JSON.stringify({
      active: 1,
      prepared: [3, 3, -1, 4, "1", 1.8],
      finished: true,
    }),
  );
  assert.deepEqual(store.read(), { active: 1, prepared: [3], finished: false });
});

test("public samples never access storage; disabled or exhausted storage still permits a full cooking session", () => {
  const blocked = () => {
    throw new Error("Storage blocked");
  };
  let lookups = 0;
  const sample = openCookingProgress(undefined, 4, 2, () => {
    lookups++;
    return device();
  });
  const unavailable = openCookingProgress("k", 4, 2, blocked);
  const exhausted = openCookingProgress("k", 4, 2, () => ({
    ...device(),
    setItem: blocked,
  }));
  for (const store of [sample, unavailable, exhausted]) {
    store.update((previous) => ({ ...previous, prepared: [1] }));
    store.update((previous) => ({ ...previous, active: 3, finished: true }));
    assert.deepEqual(store.read(), {
      active: 3,
      finished: true,
      prepared: [1],
    });
  }
  assert.equal(lookups, 0);
});

test("reset clears an earlier saved session even when an intervening write exceeded the quota", () => {
  const disk = device();
  disk.setItem(
    "k",
    JSON.stringify({ active: 1, finished: false, prepared: [] }),
  );
  const store = openCookingProgress("k", 3, 3, () => ({
    ...disk,
    setItem: () => {
      throw new Error("Quota exceeded");
    },
  }));
  store.update((previous) => ({ ...previous, prepared: [0] }));
  assert.equal(store.canPersist(), false);
  store.update(() => emptyCookingProgress);
  assert.equal(disk.getItem("k"), null);
  assert.equal(store.canPersist(), true);
});
