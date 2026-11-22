import assert from "node:assert/strict";
import test from "node:test";
import { payload, toDraft } from "../src/editorDraft.ts";
import type { WorkingCopy } from "../src/types.ts";

const working: WorkingCopy = {
  id: "working",
  instruction_id: "instruction",
  revision: 3,
  etag: '"3"',
  editable: true,
  is_open: true,
  workflow: "draft",
  title: "Бумажная лодка",
  purpose: "Проверка действий по настоящей записи",
  language: "ru",
  return_reason: "",
  source: {
    id: "video",
    duration_ms: 20_960,
    status: "ready",
    playback_url: "/video",
  },
  steps: [
    {
      id: "step",
      ordinal: 1,
      title: "Раскрывает заготовку",
      description: "Расправляет бумагу руками.",
      start_ms: 960,
      end_ms: 4_760,
      origin: "ai",
      checked: false,
      checked_by: null,
      evidence_segment_ids: [],
      evidence_frame_ids: ["frame-2", "frame-3"],
    },
  ],
};

test("saving a title edit preserves exact AI evidence intervals", () => {
  const draft = toDraft(working);
  draft.title = "Бумажная лодка — изменённое название";
  const saved = payload(draft);
  assert.equal(saved.steps[0].start_ms, 960);
  assert.equal(saved.steps[0].end_ms, 4_760);
  assert.deepEqual(saved.steps[0].evidence_frame_ids, ["frame-2", "frame-3"]);
});

test("reloading and saving a last step does not round beyond the source endpoint", () => {
  const draft = toDraft({
    ...working,
    steps: [{ ...working.steps[0], start_ms: 19_999, end_ms: 20_960 }],
  });
  assert.equal(payload(draft).steps[0].start_ms, 19_999);
  assert.equal(payload(draft).steps[0].end_ms, working.source!.duration_ms);
  draft.steps[0].startInput = "00:20.025";
  assert.equal(payload(draft).steps[0].start_ms, 20_025);
});

test("editing a recipe preserves the author's quantities and the step's photo evidence", () => {
  const draft = toDraft({
    ...working,
    recipe: { ingredients: ["Мука — 300 г", "Соль — по вкусу"], servings: 4 },
  });
  assert.equal(draft.ingredientsInput, "Мука — 300 г\nСоль — по вкусу");
  assert.equal(draft.servingsInput, "4");
  draft.ingredientsInput += "\n\n  Вода — ½ стакана  ";
  const saved = payload(draft);
  assert.deepEqual(saved.recipe, {
    ingredients: ["Мука — 300 г", "Соль — по вкусу", "Вода — ½ стакана"],
    servings: 4,
  });
  assert.deepEqual(saved.steps[0].evidence_frame_ids, ["frame-2", "frame-3"]);
  draft.ingredientsInput = "";
  draft.servingsInput = "";
  assert.deepEqual(payload(draft).recipe, { ingredients: [], servings: null });
});

test("an untouched legacy instruction stays outside the cookbook", () => {
  const draft = toDraft(working);
  assert.equal(payload(draft).recipe, null);
  draft.ingredientsInput = "Яблоко";
  assert.deepEqual(payload(draft).recipe, {
    ingredients: ["Яблоко"],
    servings: null,
  });
});
