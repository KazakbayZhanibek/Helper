import { TeachingPhotos } from "./TeachingPhotos";
import type { Step } from "./types";
import { editedPhotoWarning, needsIntentReview } from "./photoPresentation";

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
  const demonstration = step.photo_demonstration?.filter((photo) => ids.includes(photo.frame_id)) ?? [];
  const reviewIntent = (step.origin === "ai" || step.origin === "ai_edited") && needsIntentReview(
    step.title,
    step.description,
    ...demonstration.map((photo) => photo.caption),
  );
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
      {editedPhotoWarning(step.origin) && (
        <p className="field-hint">Шаг изменён после создания ИИ. Сверьте текст и фотографии с видео.</p>
      )}
      {step.warning_codes?.includes('presentation_review') && (
        <p className="field-hint">Некоторые слова или подпись требуют проверки. Сверьте действие и фотографии с видео.</p>
      )}
      {reviewIntent && (
        <p className="field-hint">В тексте или подписи указана цель или результат. Сверьте по видео, действительно ли это показано.</p>
      )}
      <TeachingPhotos
        frames={frames}
        ids={demonstration.length ? demonstration.map((photo) => photo.frame_id) : ids}
        label="Движение на видео"
        onJump={onJump}
        captions={Object.fromEntries(demonstration.map((photo) => [photo.frame_id, photo.caption]))}
      />
    </section>
  );
}
