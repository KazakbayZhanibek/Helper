import { formatMsExact, parseMs } from "./format.ts";
import type { Step, WorkingCopy } from "./types.ts";

export type DraftStep = Step & { startInput: string; endInput: string };
export type Draft = Omit<WorkingCopy, "steps"> & { steps: DraftStep[] };

export const toDraft = (wc: WorkingCopy): Draft => ({
  ...wc,
  steps: wc.steps.map((step) => ({
    ...step,
    startInput: formatMsExact(step.start_ms),
    endInput: formatMsExact(step.end_ms),
  })),
});

export const payload = (form: Draft) => ({
  title: form.title.trim(),
  purpose: form.purpose.trim(),
  language: form.language,
  steps: form.steps.map((step) => ({
    id: step.id,
    title: step.title.trim(),
    description: step.description.trim(),
    start_ms: parseMs(step.startInput),
    end_ms: parseMs(step.endInput),
    evidence_segment_ids: step.evidence_segment_ids || [],
    evidence_frame_ids: step.evidence_frame_ids || [],
  })),
});
