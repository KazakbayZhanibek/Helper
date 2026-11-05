import assert from "node:assert/strict";
import test from "node:test";
import {
  framesForEvidence,
  gapsForTeachingStep,
  isTeachingProposal,
  observationsForStep,
  reviewContext,
  teachingObservationsForStep,
} from "../src/actionEvidence.ts";
import type { VisualObservation, VisualProposal } from "../src/types.ts";

function observation(
  id: string,
  kind: "action" | "state" = "action",
): VisualObservation {
  return {
    id,
    kind,
    title: "Положение бумаги",
    subject: "Лист бумаги",
    before: "Лист лежит на столе",
    before_frame_id: "frame-1",
    movement: kind === "action" ? "Руки приподнимают край листа" : null,
    action_frame_ids: kind === "action" ? ["frame-1", "frame-2"] : [],
    after: null,
    after_frame_id: null,
    evidence_frame_ids: ["frame-1", "frame-2"],
    start_ms: 0,
    end_ms: 2000,
  };
}

function proposal(): VisualProposal {
  return {
    id: "proposal",
    instruction_id: "instruction",
    session_id: "session",
    base_working_revision: 1,
    provider: "test",
    model: "test",
    created_at: "2026-11-02T00:00:00Z",
    applied_at: null,
    preliminary: false,
    can_apply: true,
    stale: false,
    schema_version: "3",
    language: "ru",
    steps: [
      {
        title: "Поднимает край листа",
        description: "Руки приподнимают край листа",
        start_ms: 0,
        end_ms: 2000,
        evidence_frame_ids: ["frame-1", "frame-2"],
        observation_ids: ["bsecond:o1"],
      },
    ],
    observations: [
      observation("bfirst:o1"),
      observation("bsecond:o1"),
      observation("bsecond:o2", "state"),
    ],
    gaps: [
      {
        id: "bsecond:g1",
        reason: "occluded",
        description: "Край листа закрыт ладонью",
        start_ms: 1000,
        end_ms: 2000,
        evidence_frame_ids: ["frame-1", "frame-2"],
      },
    ],
    warnings: [],
    frames: [1, 2, 3].map((number) => ({
      id: `frame-${number}`,
      sequence: number,
      timestamp_ms: (number - 1) * 1000,
      width: 80,
      height: 60,
      url: `/frames/${number}`,
    })),
  };
}

test("a step shows only its explicit action link, never a neighboring window or a state", () => {
  const value = proposal();
  const step = {
    ...value.steps[0],
    observation_ids: ["bsecond:o1", "bsecond:o1", "bsecond:o2", "missing:o1"],
  };
  assert.deepEqual(
    observationsForStep(value, step).map((item) => item.id),
    ["bsecond:o1"],
  );
  assert.deepEqual(
    observationsForStep(value, { ...step, observation_ids: [] }),
    [],
  );
  assert.deepEqual(
    observationsForStep(value, { ...step, observation_ids: undefined }),
    [],
  );
});

test("state-only results retain context and gaps even with zero projected steps", () => {
  const value = proposal();
  value.steps = [];
  value.observations = [observation("bsecond:o2", "state")];
  const context = reviewContext(value);
  assert.equal(context.states.length, 1);
  assert.equal(context.states[0].movement, null);
  assert.equal(context.states[0].after, null);
  assert.equal(context.gaps[0].description, "Край листа закрыт ладонью");
  assert.deepEqual(
    framesForEvidence(value.frames, context.gaps[0].evidence_frame_ids).map(
      (item) => item.id,
    ),
    ["frame-1", "frame-2"],
  );
  assert.deepEqual(value.steps, []);
});

test("frame lookup preserves linked order without adding unrelated or repeated frames", () => {
  const value = proposal();
  const snapshot = structuredClone(value.frames);
  assert.deepEqual(
    framesForEvidence([...value.frames].reverse(), [
      "frame-1",
      "missing",
      "frame-2",
      "frame-1",
    ]).map((item) => item.id),
    ["frame-1", "frame-2"],
  );
  assert.deepEqual(framesForEvidence(value.frames, []), []);
  assert.deepEqual(value.frames, snapshot);
});

test("legacy proposals ignore experimental metadata and missing optional metadata stays empty", () => {
  const value = proposal();
  const legacy = { ...value, schema_version: "2" as const };
  assert.deepEqual(observationsForStep(legacy, legacy.steps[0]), []);
  assert.deepEqual(reviewContext(legacy), { states: [], gaps: [] });
  assert.deepEqual(
    reviewContext({ ...value, observations: undefined, gaps: undefined }),
    { states: [], gaps: [] },
  );
});

test("teaching cards are available only for saved video in the explicit teaching format", () => {
  const value = { ...proposal(), step_format: "teaching_v1" as const };
  assert.equal(isTeachingProposal(value), true);
  const result = teachingObservationsForStep(value, value.steps[0]);
  assert.equal(result.length, 1);
  assert.equal(result[0].subject, "Лист бумаги");
  assert.equal(result[0].after, null); // UI must show missing result, never invent one.
  assert.equal(isTeachingProposal({ ...value, preliminary: true }), false);
  assert.equal(isTeachingProposal({ ...value, schema_version: "2" }), false);
  assert.equal(isTeachingProposal({ ...value, step_format: undefined }), false);
});

test("teaching cards preserve distinct methods and remove only identical teaching text", () => {
  const value = { ...proposal(), step_format: "teaching_v1" as const };
  value.observations!.push({
    ...observation("bthird:o1"),
    movement: "Опустите край листа на поверхность.",
  });
  const step = {
    ...value.steps[0],
    observation_ids: ["bfirst:o1", "bsecond:o1", "bthird:o1"],
  };
  assert.deepEqual(
    teachingObservationsForStep(value, step).map((item) => item.movement),
    ["Руки приподнимают край листа", "Опустите край листа на поверхность."],
  );
});

test("incomplete teaching links fall back to the saved step instead of hiding missing claims", () => {
  const value = { ...proposal(), step_format: "teaching_v1" as const };
  const snapshot = structuredClone(value);
  assert.deepEqual(
    teachingObservationsForStep(value, {
      ...value.steps[0],
      observation_ids: ["bsecond:o1", "missing:o1"],
    }),
    [],
  );
  value.observations = [{ ...observation("bsecond:o1"), movement: "  " }];
  assert.deepEqual(teachingObservationsForStep(value, value.steps[0]), []);
  assert.deepEqual(snapshot.steps[0].description, value.steps[0].description);
});

test("inline uncertainty links require shared step frame IDs, not overlapping times", () => {
  const value = { ...proposal(), step_format: "teaching_v1" as const };
  const linked = value.gaps![0];
  value.gaps = [
    linked,
    linked,
    {
      ...linked,
      id: "unrelated-gap",
      evidence_frame_ids: ["frame-3"],
      description: "The same time range belongs to different evidence.",
    },
  ];
  const snapshot = structuredClone(value);
  assert.deepEqual(gapsForTeachingStep(value, value.steps[0]), [linked]);
  assert.deepEqual(value, snapshot);
  assert.deepEqual(
    gapsForTeachingStep(value, {
      ...value.steps[0],
      evidence_frame_ids: ["other-window-frame"],
    }),
    [],
  );
});

test("inline gaps remain absent for legacy/live and absent metadata without implying verification", () => {
  const value = { ...proposal(), step_format: "teaching_v1" as const };
  assert.deepEqual(
    gapsForTeachingStep({ ...value, schema_version: "2" }, value.steps[0]),
    [],
  );
  assert.deepEqual(
    gapsForTeachingStep({ ...value, preliminary: true }, value.steps[0]),
    [],
  );
  assert.deepEqual(
    gapsForTeachingStep({ ...value, gaps: undefined }, value.steps[0]),
    [],
  );
});
