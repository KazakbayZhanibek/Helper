import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import type { Source } from "./types";
export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
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
          <path
            d="M9 28V12h5l6 9 6-9h5v16h-5v-8l-6 9-6-9v8"
            fill="currentColor"
          />
        </svg>
      </span>
      <span>
        Опыт
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
          aria-label="Исходное видео инструкции"
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
export function PackageArt({ variant = 0 }: { variant?: number }) {
  return (
    <div className={`package-art art-${variant % 3}`} aria-hidden="true">
      <svg viewBox="0 0 420 240" fill="none">
        <ellipse
          cx="216"
          cy="195"
          rx="107"
          ry="15"
          fill="currentColor"
          opacity=".08"
        />
        <path d="m134 94 79-40 85 41-80 43Z" fill="var(--box-top)" />
        <path d="m134 94 84 44v68l-84-43Z" fill="var(--box-left)" />
        <path d="m218 138 80-43v69l-80 42Z" fill="var(--box-right)" />
        <path
          d="m164 79 84 44v68l19-11v-68l-84-43Z"
          fill="var(--box-tape)"
          opacity=".7"
        />
        <rect
          x="150"
          y="127"
          width="43"
          height="29"
          rx="2"
          transform="skewY(27) translate(0 -75)"
          fill="#fff"
          opacity=".8"
        />
        <path
          d="m163 152 19 10m-19-5 12 7"
          stroke="var(--box-right)"
          strokeWidth="3"
        />
        <circle cx="320" cy="61" r="23" fill="var(--badge)" />
        <path
          d="m310 61 7 7 13-14"
          stroke="var(--box-right)"
          strokeWidth="3"
          strokeLinecap="round"
        />
        <path
          d="M98 115h-21m28 28H67m36-7H88"
          stroke="currentColor"
          strokeWidth="2"
          opacity=".2"
        />
      </svg>
    </div>
  );
}
