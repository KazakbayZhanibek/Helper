import { useEffect, useRef, useState } from "react";
import { SavedStepPhotos } from "./SavedStepPhotos";
import { TeachingPhotos } from "./TeachingPhotos";
import type { Source, Step } from "./types";

// Manual steps already have a video interval. Decode one actual frame in the
// browser when the step scrolls into view, without submitting media to an AI.
function LocalStepPhoto({
  url,
  timestamp,
  title,
  onJump,
}: {
  url: string;
  timestamp: number;
  title: string;
  onJump: (ms: number) => void;
}) {
  const container = useRef<HTMLElement>(null);
  const [image, setImage] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let player: HTMLVideoElement | undefined;
    let objectUrl: string | undefined;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    let cancelled = false;

    function releaseVideo() {
      clearTimeout(timeout);
      if (!player) return;
      player.onloadedmetadata = player.onseeked = player.onerror = null;
      player.removeAttribute("src");
      player.load();
      player = undefined;
    }
    function fail() {
      releaseVideo();
      if (!cancelled) setFailed(true);
    }
    function decode() {
      player = document.createElement("video");
      player.muted = true;
      player.playsInline = true;
      player.preload = "metadata";
      player.onloadedmetadata = () => {
        if (
          !player ||
          !Number.isFinite(player.duration) ||
          timestamp >= player.duration * 1000
        ) {
          fail();
          return;
        }
        player.currentTime = timestamp / 1000;
      };
      player.onseeked = () => {
        if (!player || !player.videoWidth || !player.videoHeight) return fail();
        const scale = Math.min(
          1,
          960 / Math.max(player.videoWidth, player.videoHeight),
        );
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(player.videoWidth * scale));
        canvas.height = Math.max(1, Math.round(player.videoHeight * scale));
        const context = canvas.getContext("2d");
        if (!context) return fail();
        try {
          context.drawImage(player, 0, 0, canvas.width, canvas.height);
          releaseVideo();
          canvas.toBlob(
            (blob) => {
              if (cancelled) return;
              if (!blob) return fail();
              objectUrl = URL.createObjectURL(blob);
              setImage(objectUrl);
            },
            "image/jpeg",
            0.86,
          );
        } catch {
          fail();
        }
      };
      player.onerror = fail;
      timeout = setTimeout(fail, 20_000);
      player.src = url;
    }
    let observer: IntersectionObserver | undefined;
    if (container.current && typeof IntersectionObserver !== "undefined") {
      observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          observer?.disconnect();
          decode();
        }
      });
      observer.observe(container.current);
    } else {
      decode();
    }
    return () => {
      cancelled = true;
      observer?.disconnect();
      releaseVideo();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url, timestamp]);

  return (
    <section
      ref={container}
      className="saved-step-photos"
      aria-label="Кадр из видео"
    >
      <p className="eyebrow">Кадр из видео</p>
      {image ? (
        <TeachingPhotos
          frames={[{ id: "still", url: image, timestamp_ms: timestamp }]}
          ids={["still"]}
          label={title || "Шаг приготовления"}
          onJump={onJump}
        />
      ) : failed ? (
        <button
          type="button"
          className="text-button"
          onClick={() => onJump(timestamp)}
        >
          Открыть этот момент в видео
        </button>
      ) : (
        <p className="field-hint">Загружаем кадр…</p>
      )}
    </section>
  );
}

export function RecipeStepPhotos({
  step,
  recipe,
  orgId,
  source,
  start = step.start_ms,
  end = step.end_ms,
  onJump,
}: {
  step: Step;
  recipe: boolean;
  orgId?: string;
  source: Source | null;
  start?: number;
  end?: number;
  onJump: (ms: number) => void;
}) {
  if (orgId && step.evidence_frame_ids?.length)
    return <SavedStepPhotos step={step} orgId={orgId} onJump={onJump} />;
  if (
    !recipe ||
    source?.status !== "ready" ||
    !source.playback_url ||
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    start < 0 ||
    end <= start ||
    (source.duration_ms !== null && end > source.duration_ms)
  )
    return null;
  const timestamp = start + (end - start) / 2;
  return (
    <LocalStepPhoto
      key={source.id + ":" + timestamp}
      url={source.playback_url}
      timestamp={Math.floor(timestamp)}
      title={step.title}
      onJump={onJump}
    />
  );
}
