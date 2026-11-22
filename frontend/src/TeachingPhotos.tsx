import { useEffect, useRef, useState } from "react";
import { formatMsExact } from "./format";
import type { VisualFrame } from "./types";
import { photoAlternative } from "./photoPresentation";
import "./teaching-photos.css";

export type TeachingPhotoFrame = Pick<VisualFrame, "id" | "url"> &
  Partial<Pick<VisualFrame, "timestamp_ms" | "sequence" | "width" | "height">>;

function frameTime(frame: TeachingPhotoFrame): number | null {
  return typeof frame.timestamp_ms === "number" &&
    Number.isFinite(frame.timestamp_ms) &&
    frame.timestamp_ms >= 0
    ? frame.timestamp_ms
    : null;
}

function PhotoImage({
  frame,
  label,
}: {
  frame: TeachingPhotoFrame;
  label: string;
}) {
  const [failed, setFailed] = useState(false);
  if (failed)
    return (
      <span className="teaching-photo-unavailable">
        Фото недоступно. Проверьте этот момент по записи.
      </span>
    );
  return (
    <img
      src={frame.url}
      alt={label}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  );
}

function PhotoViewer({
  frame,
  label,
  caption,
  onClose,
  onJump,
}: {
  frame: TeachingPhotoFrame;
  label: string;
  caption?: string;
  onClose: () => void;
  onJump?: (ms: number) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const timestamp = frameTime(frame);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="teaching-photo-dialog"
      aria-label={`Фото: ${label}`}
      onCancel={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
    >
      <header className="teaching-photo-dialog-head">
        <h2>{label}</h2>
        <button type="button" onClick={onClose} autoFocus>
          Закрыть фото
        </button>
      </header>
      <PhotoImage
        key={`${frame.id}:${frame.url}`}
        frame={frame}
        label={photoAlternative(label, caption)}
      />
      {caption && <p className="teaching-photo-dialog-description">{caption}</p>}
      {timestamp !== null && (
        <p className="teaching-photo-dialog-caption">
          {onJump ? (
            <button
              type="button"
              className="time-link"
              onClick={() => {
                onClose();
                onJump(timestamp);
              }}
            >
              {formatMsExact(timestamp)} · Открыть в видео
            </button>
          ) : (
            formatMsExact(timestamp)
          )}
        </p>
      )}
    </dialog>
  );
}

/** Uses explicit evidence links only. Missing phases never borrow a nearby image. */
export function TeachingPhotos({
  frames,
  ids,
  label,
  onJump,
  compact = false,
  oneAtATime = false,
  captions = {},
}: {
  frames: readonly TeachingPhotoFrame[];
  ids: readonly string[];
  label: string;
  onJump?: (ms: number) => void;
  compact?: boolean;
  oneAtATime?: boolean;
  captions?: Readonly<Record<string, string>>;
}) {
  const [opened, setOpened] = useState<TeachingPhotoFrame | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [photoIndex, setPhotoIndex] = useState(0);
  const byId = new Map(frames.map((frame) => [frame.id, frame]));
  const references = [...new Set(ids)];
  const selected = references
    .flatMap((id) => {
      const frame = byId.get(id);
      return frame ? [frame] : [];
    })
    .sort((a, b) => {
      const aTime = frameTime(a);
      const bTime = frameTime(b);
      if (aTime === null) return bTime === null ? 0 : 1;
      if (bTime === null) return -1;
      return aTime - bTime;
    });
  const canExpand = !oneAtATime && compact && selected.length > 3;
  const currentIndex = Math.min(photoIndex, Math.max(0, selected.length - 1));
  let visible = selected;
  if (oneAtATime) {
    visible = selected.slice(currentIndex, currentIndex + 1);
  } else if (canExpand && !expanded) {
    visible = [selected[0], selected[Math.floor((selected.length - 1) / 2)], selected[selected.length - 1]];
  }
  return (
    <div className="teaching-photos" aria-label={`Фото: ${label}`}>
      {selected.length > 0 && (
        <div className="teaching-photo-grid">
          {visible.map((frame) => {
            const index = selected.indexOf(frame);
            const timestamp = frameTime(frame);
            const photoLabel = `${label} · фото ${index + 1}${timestamp !== null ? ` · ${formatMsExact(timestamp)}` : ""}`;
            return (
              <figure key={frame.id}>
                <button
                  type="button"
                  className="teaching-photo-zoom"
                  aria-label={`Увеличить: ${photoAlternative(photoLabel, captions[frame.id])}`}
                  onClick={() => setOpened(frame)}
                >
                  <PhotoImage
                    key={`${frame.id}:${frame.url}`}
                    frame={frame}
                    label={photoAlternative(photoLabel, captions[frame.id])}
                  />
                  <span className="teaching-photo-zoom-label">Увеличить</span>
                </button>
                <figcaption>
                  {captions[frame.id] && <p>{captions[frame.id]}</p>}
                  <span>Фото {index + 1}</span>
                  {timestamp !== null &&
                    (onJump ? (
                      <button
                        type="button"
                        className="time-link"
                        onClick={() => onJump(timestamp)}
                        aria-label={`Открыть в видео: ${photoLabel}`}
                      >
                        {formatMsExact(timestamp)} · В видео
                      </button>
                    ) : (
                      <span>{formatMsExact(timestamp)}</span>
                    ))}
                </figcaption>
              </figure>
            );
          })}
        </div>
      )}
      {canExpand && (
        <button
          type="button"
          className="time-link"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Оставить три кадра" : `Показать все ${selected.length} кадров`}
        </button>
      )}
      {oneAtATime && selected.length > 1 && (
        <div className="cook-photo-switch">
          <button
            type="button"
            className="text-button"
            disabled={currentIndex === 0}
            onClick={() => setPhotoIndex(currentIndex - 1)}
          >
            Предыдущее фото
          </button>
          <span aria-live="polite">{currentIndex + 1} из {selected.length}</span>
          <button
            type="button"
            className="text-button"
            disabled={currentIndex === selected.length - 1}
            onClick={() => setPhotoIndex(currentIndex + 1)}
          >
            Следующее фото
          </button>
        </div>
      )}
      {selected.length < references.length && (
        <p className="field-hint">
          {selected.length
            ? "Часть фотографий недоступна. Сверьте пояснение с записью."
            : "Фотографии этого пояснения недоступны. Сверьте его с записью."}
        </p>
      )}
      {!references.length && (
        <p className="field-hint">
          Для этого пояснения нет связанного фото. Проверьте фрагмент записи.
        </p>
      )}
      {opened && (
        <PhotoViewer
          frame={opened}
          label={label}
          caption={captions[opened.id]}
          onClose={() => setOpened(null)}
          onJump={onJump}
        />
      )}
    </div>
  );
}
