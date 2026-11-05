import { FrameEvidence } from "./CameraCapture";
import { formatMsExact } from "./format";
import {
  framesForEvidence,
  gapReasonLabel,
  observationsForStep,
  reviewContext,
} from "./actionEvidence";
import type {
  VisualFrame,
  VisualObservation,
  VisualProposal,
  VisualProposalStep,
} from "./types";

type EvidenceProps = { proposal: VisualProposal; onJump: (ms: number) => void };

function EvidenceFrames({
  frames,
  ids,
}: {
  frames: VisualFrame[];
  ids: string[];
}) {
  const selected = framesForEvidence(frames, ids);
  return (
    <>
      {selected.length > 0 && <FrameEvidence frames={selected} />}
      {selected.length < new Set(ids).size && (
        <p className="field-hint">
          Часть связанных кадров недоступна. Проверьте момент по записи.
        </p>
      )}
    </>
  );
}

function JumpToEvidence({
  start,
  end,
  onJump,
}: {
  start: number;
  end: number;
  onJump: (ms: number) => void;
}) {
  return (
    <button type="button" className="time-link" onClick={() => onJump(start)}>
      Открыть в записи · {formatMsExact(start)}–{formatMsExact(end)} ·
      приблизительно
    </button>
  );
}

function ObservationPhases({
  observation,
  frames,
}: {
  observation: VisualObservation;
  frames: VisualFrame[];
}) {
  const phases = [
    {
      label: "До",
      text: observation.before,
      ids: observation.before_frame_id ? [observation.before_frame_id] : [],
    },
    {
      label: "Движение",
      text: observation.movement,
      ids: observation.action_frame_ids,
    },
    {
      label: "После",
      text: observation.after,
      ids: observation.after_frame_id ? [observation.after_frame_id] : [],
    },
  ];
  return (
    <ol className="stack" aria-label="До, движение, после — наблюдения ИИ">
      {phases.map((phase) => (
        <li key={phase.label}>
          <strong>{phase.label}</strong>
          <p>{phase.text || "Нет данных на выбранных кадрах."}</p>
          <EvidenceFrames frames={frames} ids={phase.ids} />
        </li>
      ))}
    </ol>
  );
}

export function VisualActionEvidence({
  proposal,
  step,
  onJump,
}: EvidenceProps & {
  step: VisualProposalStep;
}) {
  if (proposal.schema_version !== "3") return null;
  const observations = observationsForStep(proposal, step);
  return (
    <details>
      <summary>Что видно на кадрах</summary>
      <div className="stack">
        <p className="field-hint">
          Так ИИ описал выбранные кадры. Сверьте описание с полной записью.
        </p>
        {observations.length ? (
          observations.map((observation) => (
            <section key={observation.id} aria-label={observation.title}>
              <h4>{observation.title}</h4>
              <p>Предмет: {observation.subject}</p>
              <JumpToEvidence
                start={observation.start_ms}
                end={observation.end_ms}
                onJump={onJump}
              />
              <ObservationPhases
                observation={observation}
                frames={proposal.frames}
              />
            </section>
          ))
        ) : (
          <>
            <p>
              Для этого шага нет связанных наблюдений. Проверьте его по записи.
            </p>
            <EvidenceFrames
              frames={proposal.frames}
              ids={step.evidence_frame_ids}
            />
          </>
        )}
      </div>
    </details>
  );
}

export function VisualReviewContext({ proposal, onJump }: EvidenceProps) {
  if (proposal.schema_version !== "3") return null;
  const { states, gaps } = reviewContext(proposal);
  return (
    <>
      {states.length > 0 && (
        <section className="stack" aria-label="Состояния предметов">
          <h3>Состояния предметов · {states.length}</h3>
          <p className="field-hint">
            Это описания ИИ для контекста. Положение предмета само по себе не
            подтверждает выполненное действие и не добавляется в шаги.
          </p>
          {states.map((observation) => (
            <article className="proposal-step" key={observation.id}>
              <h4>{observation.title}</h4>
              <p>Предмет: {observation.subject}</p>
              <JumpToEvidence
                start={observation.start_ms}
                end={observation.end_ms}
                onJump={onJump}
              />
              <p>
                <strong>Раннее состояние:</strong>{" "}
                {observation.before || "Нет данных на выбранных кадрах."}
              </p>
              <p>
                <strong>Позднее состояние:</strong>{" "}
                {observation.after || "Нет данных на выбранных кадрах."}
              </p>
              <details>
                <summary>Связанные кадры</summary>
                <EvidenceFrames
                  frames={proposal.frames}
                  ids={observation.evidence_frame_ids}
                />
              </details>
            </article>
          ))}
        </section>
      )}
      <section className="stack" aria-label="Неясные моменты">
        <h3>Неясные моменты · {gaps.length}</h3>
        {gaps.length ? (
          <>
            <p className="field-hint">
              ИИ отметил места, где выбранных кадров недостаточно. Посмотрите
              эти фрагменты в записи.
            </p>
            {gaps.map((gap) => (
              <article className="proposal-step" key={gap.id}>
                <h4>{gapReasonLabel[gap.reason] || "Неясный момент"}</h4>
                <p>{gap.description}</p>
                <JumpToEvidence
                  start={gap.start_ms}
                  end={gap.end_ms}
                  onJump={onJump}
                />
                <details>
                  <summary>Связанные кадры</summary>
                  <EvidenceFrames
                    frames={proposal.frames}
                    ids={gap.evidence_frame_ids}
                  />
                </details>
              </article>
            ))}
          </>
        ) : (
          <p className="field-hint">
            ИИ не отметил неясных моментов. Это не гарантирует, что все действия
            попали в разбор.
          </p>
        )}
      </section>
    </>
  );
}
