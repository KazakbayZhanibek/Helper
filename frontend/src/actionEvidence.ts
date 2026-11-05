import type {
  VisualFrame,
  VisualGap,
  VisualObservation,
  VisualProposal,
  VisualProposalStep,
} from "./types.ts";

/** Resolve explicit links only; overlapping timestamps do not establish an action. */
export function observationsForStep(
  proposal: VisualProposal,
  step: VisualProposalStep,
): VisualObservation[] {
  if (proposal.schema_version !== "3") return [];
  const byId = new Map(
    (proposal.observations || []).map((item) => [item.id, item]),
  );
  return [...new Set(step.observation_ids || [])].flatMap((id) => {
    const observation = byId.get(id);
    return observation?.kind === "action" ? [observation] : [];
  });
}

export function isTeachingProposal(proposal: VisualProposal): boolean {
  return (
    proposal.schema_version === "3" &&
    proposal.step_format === "teaching_v1" &&
    !proposal.preliminary
  );
}

/** Resolve a complete learning card without pretending missing links are valid. */
export function teachingObservationsForStep(
  proposal: VisualProposal,
  step: VisualProposalStep,
): VisualObservation[] {
  if (!isTeachingProposal(proposal)) return [];
  const links = [...new Set(step.observation_ids || [])];
  const observations = observationsForStep(proposal, step);
  if (
    !links.length ||
    observations.length !== links.length ||
    observations.some((item) => !item.movement?.trim())
  )
    return [];
  // Exact duplicates may retain several evidence identities after aggregation.
  // Display their shared teaching text once, without merging distinct actions.
  const seen = new Set<string>();
  return observations.filter((item) => {
    const key = JSON.stringify([
      item.subject,
      item.before,
      item.movement,
      item.after,
    ]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** A time overlap alone cannot link an uncertainty to a teaching step. */
export function gapsForTeachingStep(
  proposal: VisualProposal,
  step: VisualProposalStep,
): VisualGap[] {
  if (!isTeachingProposal(proposal)) return [];
  const evidence = new Set(step.evidence_frame_ids);
  const seen = new Set<string>();
  return (proposal.gaps || []).filter((gap) => {
    if (
      seen.has(gap.id) ||
      !gap.evidence_frame_ids.some((id) => evidence.has(id))
    )
      return false;
    seen.add(gap.id);
    return true;
  });
}

/** A gap or a state remains reviewable even when no action produced a step. */
export function reviewContext(proposal: VisualProposal): {
  states: VisualObservation[];
  gaps: VisualGap[];
} {
  if (proposal.schema_version !== "3") return { states: [], gaps: [] };
  return {
    states: (proposal.observations || []).filter(
      (item) => item.kind === "state",
    ),
    gaps: proposal.gaps || [],
  };
}

export function framesForEvidence(
  frames: readonly VisualFrame[],
  ids: readonly string[],
): VisualFrame[] {
  const byId = new Map(frames.map((frame) => [frame.id, frame]));
  return [...new Set(ids)].flatMap((id) => {
    const frame = byId.get(id);
    return frame ? [frame] : [];
  });
}

export const gapReasonLabel: Record<VisualGap["reason"], string> = {
  occluded: "Обзор закрыт",
  sampling_gap: "Между выбранными кадрами",
  cut: "Возможная склейка записи",
  outside_recording: "За пределами записи или фрагмента",
  unclear_action: "Действие неясно",
};
