import { useId, useRef, useState } from "react";
import { Icon } from "./components";
import { formatMsExact } from "./format";
import type { Source } from "./types";
import "./teaching-clip.css";

type TeachingClipProps = {
  source: Source | null;
  startMs: number;
  endMs: number;
  title: string;
};

/** The source is mounted only after an explicit request to watch this step. */
function AvailableClip({
  source,
  startMs,
  endMs,
  title,
}: TeachingClipProps & { source: Source & { playback_url: string } }) {
  const [expanded, setExpanded] = useState(false);
  const [finished, setFinished] = useState(false);
  const [error, setError] = useState("");
  const video = useRef<HTMLVideoElement>(null);
  const playOnLoad = useRef(true);
  const playerId = useId();
  const start = startMs / 1000;
  const end = endMs / 1000;

  function bounds(element: HTMLVideoElement) {
    return {
      start,
      end: Number.isFinite(element.duration)
        ? Math.min(end, element.duration)
        : end,
    };
  }

  function seekStart(element: HTMLVideoElement) {
    const range = bounds(element);
    if (range.end <= range.start) {
      element.pause();
      setError("Этот фрагмент находится за пределами доступного видео.");
      return false;
    }
    element.currentTime = range.start;
    setFinished(false);
    return true;
  }

  async function play(element: HTMLVideoElement) {
    try {
      await element.play();
    } catch (reason) {
      // The browser may require a second click on its native play control.
      // Aborting during collapse/unmount is expected and needs no warning.
      if (
        reason instanceof DOMException &&
        ["NotAllowedError", "AbortError"].includes(reason.name)
      )
        return;
      setError(
        "Не удалось запустить фрагмент. Попробуйте воспроизвести исходную запись.",
      );
    }
  }

  function replay() {
    const element = video.current;
    if (element && seekStart(element)) void play(element);
  }

  return (
    <section className="teaching-clip" aria-label={`Видео шага: ${title}`}>
      <div className="teaching-clip-heading">
        <button
          type="button"
          className="button secondary"
          aria-expanded={expanded}
          aria-controls={playerId}
          onClick={() => {
            if (expanded) video.current?.pause();
            else {
              playOnLoad.current = true;
              setFinished(false);
              setError("");
            }
            setExpanded(!expanded);
          }}
        >
          <Icon name={expanded ? "close" : "play"} size={16} />
          {expanded ? "Скрыть видео" : "Посмотреть движение"}
        </button>
        <span className="teaching-clip-time">
          {formatMsExact(startMs)}–{formatMsExact(endMs)} · приблизительно
        </span>
      </div>
      <div id={playerId} hidden={!expanded}>
        {expanded && (
          <>
            <video
              ref={video}
              controls
              playsInline
              preload="metadata"
              src={source.playback_url}
              aria-label={`Фрагмент видео: ${title}`}
              onLoadedMetadata={(event) => {
                if (seekStart(event.currentTarget) && playOnLoad.current) {
                  playOnLoad.current = false;
                  void play(event.currentTarget);
                }
              }}
              onPlay={(event) => {
                const element = event.currentTarget;
                const range = bounds(element);
                if (
                  element.currentTime < range.start ||
                  element.currentTime >= range.end
                )
                  seekStart(element);
                setFinished(false);
              }}
              onSeeking={(event) => {
                const element = event.currentTarget;
                const range = bounds(element);
                if (element.currentTime < range.start)
                  element.currentTime = range.start;
                else if (element.currentTime > range.end) {
                  element.pause();
                  element.currentTime = range.end;
                  setFinished(true);
                }
              }}
              onTimeUpdate={(event) => {
                const element = event.currentTarget;
                const range = bounds(element);
                if (element.currentTime >= range.end) {
                  element.pause();
                  if (element.currentTime > range.end)
                    element.currentTime = range.end;
                  setFinished(true);
                }
              }}
              onEnded={() => setFinished(true)}
              onError={() =>
                setError(
                  "Видео не удалось загрузить. Проверьте доступность исходной записи.",
                )
              }
            />
            <div className="teaching-clip-footer">
              <button type="button" className="text-button" onClick={replay}>
                Повторить фрагмент
              </button>
              <span className="field-hint" role="status">
                {finished
                  ? "Фрагмент завершён."
                  : "Видео остановится в конце этого шага."}
              </span>
            </div>
            {error && (
              <p className="teaching-clip-error" role="alert">
                {error}
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}

export function TeachingClip(props: TeachingClipProps) {
  const { source, startMs, endMs } = props;
  if (!source?.playback_url || source.status !== "ready")
    return <p className="field-hint">Видео этого шага пока недоступно.</p>;
  if (
    !Number.isFinite(startMs) ||
    !Number.isFinite(endMs) ||
    startMs < 0 ||
    endMs <= startMs
  )
    return (
      <p className="field-hint">
        Уточните границы шага, чтобы посмотреть движение.
      </p>
    );
  return (
    <AvailableClip
      key={`${source.id}:${source.playback_url}:${startMs}:${endMs}`}
      {...props}
      source={source as Source & { playback_url: string }}
    />
  );
}
