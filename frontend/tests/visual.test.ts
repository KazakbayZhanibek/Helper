import assert from "node:assert/strict";
import test from "node:test";
import {
  stepWarningMessages,
  visualApplyProblem,
  visualResultSummary,
  visualValidationFailures,
} from "../src/visual.ts";
import type { VisualAnalysisProgress, VisualProposal } from "../src/types.ts";

const proposal: VisualProposal = {
  id: "proposal",
  instruction_id: "instruction",
  session_id: "session",
  base_working_revision: 2,
  provider: "codex",
  model: "gpt-6-luna",
  created_at: "2026-10-30T18:51:34Z",
  applied_at: null,
  preliminary: false,
  can_apply: true,
  stale: false,
  schema_version: "2",
  language: "ru",
  steps: [
    {
      title: "Прижимает сгиб",
      description: "Прижимает бумагу ладонями.",
      start_ms: 960,
      end_ms: 4_760,
      evidence_frame_ids: ["frame-2", "frame-3"],
    },
  ],
  warnings: [],
  frames: [],
};

test("an already applied result is readable but cannot be applied again after reload", () => {
  const reloaded = {
    ...proposal,
    applied_at: "2026-10-30T18:53:13Z",
    can_apply: false,
    stale: true,
  };
  assert.match(visualApplyProblem(reloaded, 3, false), /уже применено/);
  assert.doesNotMatch(visualApplyProblem(reloaded, 3, false), /новый анализ/);
});

test("empty, preliminary and outdated results explain distinct blocked actions", () => {
  assert.equal(visualApplyProblem(proposal, 2, false), "");
  assert.match(
    visualApplyProblem({ ...proposal, steps: [], can_apply: false }, 2, false),
    /Шаги не созданы/,
  );
  assert.match(
    visualApplyProblem({ ...proposal, preliminary: true }, 2, false),
    /предварительное/,
  );
  assert.match(
    visualApplyProblem(proposal, 3, false),
    /изменились после анализа/,
  );
  assert.match(visualApplyProblem(proposal, 2, true), /несохранённые/);
  assert.match(
    visualApplyProblem({ ...proposal, can_apply: false }, 2, false),
    /открытой рабочей версии/,
  );
});

function analysis(errorCode: string | null): VisualAnalysisProgress {
  return {
    scope: "full",
    windows_total: 1,
    windows_completed: errorCode ? 0 : 1,
    refinement_windows: 0,
    refinements_completed: 0,
    coverage_complete: !errorCode,
    sampling: "discrete_frames",
    unprocessed_ranges: errorCode ? [{ start_ms: 0, end_ms: 5_000 }] : [],
    windows: [
      {
        id: "window",
        start_ms: 0,
        end_ms: 5_000,
        kind: "overview",
        state: errorCode ? "failed" : "completed",
        error_code: errorCode,
      },
    ],
  };
}

test("a completed job with invalid model output is a failed response, never an empty ready draft", () => {
  for (const errorCode of ["VISUAL_OUTPUT_INVALID", "MODEL_OUTPUT_INVALID"]) {
    const result: VisualProposal = {
      ...proposal,
      schema_version: "3",
      step_format: "teaching_v1",
      steps: [],
      can_apply: false,
      analysis: analysis(errorCode),
      warnings: [{ code: "insufficient_evidence", message: "Empty aggregate" }],
    };
    const summary = visualResultSummary(result);
    assert.equal(summary.kind, "validation_failed");
    assert.match(summary.title, /Ответ ИИ не прошёл проверку/);
    assert.doesNotMatch(
      summary.title + summary.message,
      /готов|плох|качество видео|недостаточно видимых/,
    );
    assert.equal(visualValidationFailures(result.analysis), 1);
    assert.equal(visualApplyProblem(result, 2, false), summary.message);
  }
});

test("empty verified output retains uncertainty separately from selected actions", () => {
  const empty = {
    ...proposal,
    steps: [],
    can_apply: false,
    analysis: analysis(null),
  };
  const noActions = visualResultSummary(empty);
  assert.equal(noActions.kind, "no_actions");
  assert.match(noActions.title, /не выделены/);
  assert.doesNotMatch(noActions.title, /готов|действий нет/);
  const withGaps: VisualProposal = {
    ...empty,
    schema_version: "3",
    step_format: "teaching_v1",
    gaps: [
      {
        id: "gap",
        reason: "unclear_action",
        description: "Сверьте с записью",
        evidence_frame_ids: ["f1", "f2"],
        start_ms: 0,
        end_ms: 5_000,
      },
    ],
  };
  const original = structuredClone(withGaps);
  assert.equal(visualResultSummary(withGaps).kind, "insufficient_evidence");
  assert.match(
    visualResultSummary(withGaps).message,
    /Сохранены неясные моменты и кадры/,
  );
  assert.deepEqual(withGaps, original);
  assert.equal(
    visualResultSummary({
      ...empty,
      warnings: [{ code: "insufficient_evidence", message: "Unknown" }],
    }).kind,
    "insufficient_evidence",
  );
});

test("partial validated steps remain usable and errors from budget or workflow are not validation failures", () => {
  const partial = { ...proposal, analysis: analysis("VISUAL_OUTPUT_INVALID") };
  assert.equal(visualResultSummary(partial).kind, "validation_failed");
  assert.match(
    visualResultSummary(partial).message,
    /Сохранённые шаги доступны/,
  );
  assert.equal(visualApplyProblem(partial, 2, false), "");
  assert.equal(
    visualValidationFailures(analysis("AI_CALL_OUTCOME_UNKNOWN")),
    0,
  );
  assert.equal(
    visualResultSummary({ ...proposal, analysis: analysis("AI_CALL_LIMIT") })
      .kind,
    "partial",
  );
  assert.equal(
    visualResultSummary({ ...proposal, can_apply: false, stale: true }).kind,
    "ready",
  );
  assert.equal(visualValidationFailures(undefined), 0);
  assert.equal(
    visualResultSummary({ ...partial, applied_at: "2026-11-03T00:00:00Z" })
      .kind,
    "applied",
  );
});

test("visual warnings remain specific and duplicate unknown fallbacks appear only once", () => {
  const messages = stepWarningMessages([
    "approximate_timing",
    "sampled_frames",
    "uncertain_action",
    "sampled_frames",
    "future_code",
    "other_future_code",
  ]);
  assert.equal(messages.length, 4);
  assert.ok(
    messages.some((message) => message.includes("пропустить движения")),
  );
  assert.ok(
    messages.some((message) => message.includes("распознано неуверенно")),
  );
  assert.equal(
    messages.filter(
      (message) =>
        message === "Проверьте основание этого шага по исходному видео.",
    ).length,
    1,
  );
  assert.deepEqual(stepWarningMessages(), []);
});
