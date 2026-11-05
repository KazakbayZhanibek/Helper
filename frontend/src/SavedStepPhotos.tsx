import { TeachingPhotos } from "./TeachingPhotos";
import type { Step } from "./types";

/** Published steps preserve action frames, without inferred before/after labels. */
export function SavedStepPhotos({
  step,
  orgId,
  onJump,
}: {
  step: Step;
  orgId: string;
  onJump: (ms: number) => void;
}) {
  const ids = step.evidence_frame_ids || [];
  if (!ids.length) return null;
  // Older immutable publications contain IDs only. Never invent their timestamps.
  const frames =
    step.evidence_frames ??
    ids.map((id) => ({
      id,
      url: `/api/v1/orgs/${orgId}/visual-frames/${id}`,
    }));
  return (
    <section className="saved-step-photos" aria-label="Фото к шагу">
      <p className="eyebrow">Фото к шагу</p>
      <TeachingPhotos
        frames={frames}
        ids={ids}
        label="Движение на видео"
        onJump={onJump}
      />
    </section>
  );
}
