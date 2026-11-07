import { TeachingPhotos } from "./TeachingPhotos";
import { VisualActionEvidence } from "./VisualActionEvidence";
import {
  gapsForTeachingStep,
  teachingObservationsForStep,
} from "./actionEvidence";
import { formatMsExact } from "./format";
import { needsIntentReview } from "./photoPresentation";
import type { VisualProposal, VisualProposalStep } from "./types";

function StepGaps({
  proposal,
  step,
  onJump,
}: {
  proposal: VisualProposal;
  step: VisualProposalStep;
  onJump: (ms: number) => void;
}) {
  const gaps = gapsForTeachingStep(proposal, step);
  if (!gaps.length) return null;
  return (
    <details className="teaching-gap-note">
      <summary>Уточнить по записи · {gaps.length}</summary>
      <ul>
        {gaps.map((gap) => (
          <li key={gap.id}>
            <p>{gap.description}</p>
            <button
              type="button"
              className="time-link"
              onClick={() => onJump(gap.start_ms)}
            >
              Открыть фрагмент · {formatMsExact(gap.start_ms)}–
              {formatMsExact(gap.end_ms)} · приблизительно
            </button>
          </li>
        ))}
      </ul>
    </details>
  );
}

/** Explain the process first; preserve per-frame details in the review drawer. */
export function VisualTeachingStep({
  proposal,
  step,
  onJump,
}: {
  proposal: VisualProposal;
  step: VisualProposalStep;
  onJump: (ms: number) => void;
}) {
  const observations = teachingObservationsForStep(proposal, step);
  const reviewIntent = step.evidence_frame_ids.length > 0 && needsIntentReview(
    step.title,
    step.description,
    ...(step.photo_demonstration ?? []).map((photo) => photo.caption),
  );
  if (!observations.length)
    return (
      <>
        <p>{step.description}</p>
        <p className="pipeline-notice">
          Не удалось сопоставить этот шаг с наблюдениями. Сверьте текст с
          записью перед применением.
        </p>
        <StepGaps proposal={proposal} step={step} onJump={onJump} />
        <TeachingPhotos
          frames={proposal.frames}
          ids={step.evidence_frame_ids}
          label="Кадры шага"
          onJump={onJump}
        />
      </>
    );
  return (
    <div className="teaching-step-content stack">
      <div className="teaching-story" aria-label="Объяснение этапа">
        <p>{step.description}</p>
      </div>
      {step.presentation_review && (
        <p className="field-hint">Некоторые слова или подпись требуют проверки. Сверьте действие и фотографии с видео.</p>
      )}
      {reviewIntent && (
        <p className="field-hint">В тексте или подписи указана цель или результат. Сверьте по видео, действительно ли это показано.</p>
      )}
      <TeachingPhotos
        frames={proposal.frames}
        ids={step.photo_demonstration?.map((photo) => photo.frame_id) ?? step.evidence_frame_ids}
        label={step.title}
        onJump={onJump}
        captions={Object.fromEntries((step.photo_demonstration ?? []).map((photo) => [photo.frame_id, photo.caption]))}
        compact={!step.photo_demonstration?.length}
      />
      <VisualActionEvidence proposal={proposal} step={step} onJump={onJump} />
      <StepGaps proposal={proposal} step={step} onJump={onJump} />
    </div>
  );
}
