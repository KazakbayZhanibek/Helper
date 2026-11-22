import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import type { Source } from "./types";
export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    bookmark: <path d="M6 4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v17l-6-4-6 4Z" />,
    bowl: (
      <>
        <path d="M3 12h18a9.4 9.4 0 0 1-18 0ZM9 21h6M9 9c-1.5-2.6 2.6-3.3 1.2-6M14 9c-1.5-2.6 2.6-3.3 1.2-6" />
      </>
    ),
    people: <><circle cx="9" cy="8" r="3" /><path d="M3 20v-2a6 6 0 0 1 12 0v2m0-14a3 3 0 0 1 0 6m3 2a5 5 0 0 1 3 5" /></>,
    card: <><rect x="2" y="5" width="20" height="14" rx="3" /><path d="M3 10h18M6 15h4" /></>,
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    plus: <path d="M12 5v14M5 12h14" />,
    search: (
      <>
        <circle cx="10.5" cy="10.5" r="6.5" />
        <path d="m16 16 4.5 4.5" />
      </>
    ),
    arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
    back: <path d="M19 12H5m6-6-6 6 6 6" />,
    check: <path d="m5 12 4 4L19 6" />,
    play: <path d="m9 5 10 7-10 7Z" />,
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>
    ),
    book: (
      <>
        <path d="M4 4h7l1 2 1-2h7v15h-7l-1 2-1-2H4Z" />
        <path d="M12 6v15" />
      </>
    ),
    logout: (
      <>
        <path d="M9 4H4v16h5M10 12h11m-4-4 4 4-4 4" />
      </>
    ),
    info: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 11v6m0-10v1" />
      </>
    ),
    chevron: <path d="m8 5 7 7-7 7" />,
    up: <path d="m6 14 6-6 6 6" />,
    down: <path d="m6 10 6 6 6-6" />,
    trash: (
      <>
        <path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" />
      </>
    ),
    edit: (
      <>
        <path d="m16 3 5 5-12 12-6 1 1-6Z" />
        <path d="m13 6 5 5" />
      </>
    ),
    close: <path d="m6 6 12 12M6 18 18 6" />,
    eye: (
      <>
        <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z" />
        <circle cx="12" cy="12" r="3" />
      </>
    ),
    shield: (
      <>
        <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z" />
        <path d="m8 12 3 3 5-6" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.book}
    </svg>
  );
}
export function Brand() {
  return (
    <span className="brand">
      <span className="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 40 40">
          <g
            fill="none"
            stroke="currentColor"
            strokeWidth="2.25"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M7 20h26c-1.6 6.4-6.3 10.5-13 10.5S8.6 26.4 7 20Z" />
            <path d="M15 32h10M16 16c-2-3.5 3.5-4.5 1.5-8M22 16c-2-3.5 3.5-4.5 1.5-8" />
          </g>
        </svg>
      </span>
      <span>
        Рецепты
        <br />
        мастера<span className="brand-dot">.</span>
      </span>
    </span>
  );
}
export function ErrorBanner({
  error,
  children,
}: {
  error: string;
  children?: ReactNode;
}) {
  return (
    <div className="notice error" role="alert">
      <Icon name="info" />
      <div>
        <p>{error}</p>
        {children}
      </div>
    </div>
  );
}
export function Loading({ label = "Загружаем…" }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <div className="loading-bar" />
      <span>{label}</span>
    </div>
  );
}
export function Status({ status }: { status: string }) {
  const labels: Record<string, string> = {
    draft: "Черновик",
    in_review: "На проверке",
    published: "Опубликовано",
  };
  return (
    <span className={`status status-${status}`}>
      <span />
      {labels[status] || status}
    </span>
  );
}
export const languageName = (l: string) =>
  ({ ru: "Русский", kk: "Қазақша", mixed: "Смешанный" })[l] || l;
export const time = (ms: number | null) =>
  ms === null
    ? "Подготовка"
    : `${Math.floor(ms / 60000)
        .toString()
        .padStart(2, "0")}:${Math.floor((ms % 60000) / 1000)
        .toString()
        .padStart(2, "0")}`;
export const date = (s: string) =>
  new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "short",
    timeZone: "Asia/Almaty",
  }).format(new Date(s));
export function Video({
  source,
  videoRef,
  onTimeUpdate,
}: {
  source: Source | null;
  videoRef?: RefObject<HTMLVideoElement | null>;
  onTimeUpdate?: () => void;
}) {
  return (
    <div className="video-frame">
      {source?.status === "ready" ? (
        <video
          ref={videoRef}
          controls
          preload="metadata"
          playsInline
          src={source.playback_url || undefined}
          poster={source.thumbnail_url || undefined}
          onTimeUpdate={onTimeUpdate}
          aria-label="Исходная видеозапись"
        />
      ) : (
        <div className="video-empty">
          <Icon name="play" size={40} />
          <p>
            {source
              ? ["failed", "rejected"].includes(source.status)
                ? "Не удалось подготовить видео"
                : "Подготавливаем исходное видео"
              : "Добавьте исходное видео"}
          </p>
        </div>
      )}
    </div>
  );
}
export function Dialog({
  title,
  children,
  onClose,
  className = "",
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
    return () => ref.current?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={onClose}
      className={`dialog ${className}`}
      aria-label={title}
    >
      <div className="dialog-head">
        <h2>{title}</h2>
        <button className="icon-button" onClick={onClose} aria-label="Закрыть">
          <Icon name="close" />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function RecipeCover({ legacy = false }: { legacy?: boolean }) {
  return (
    <div className="recipe-cover-fallback" aria-hidden="true">
      <Icon name={legacy ? "book" : "bowl"} size={44} />
    </div>
  );
}
