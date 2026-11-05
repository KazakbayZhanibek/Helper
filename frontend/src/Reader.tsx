import { useCallback, useEffect, useRef, useState } from "react";
import { countNoun } from "./format";
import { SavedStepPhotos } from "./SavedStepPhotos";
import {
  api,
  ApiError,
  instructionApi,
  instructionPath,
  libraryPath,
} from "./api";
import {
  Dialog,
  ErrorBanner,
  Icon,
  Loading,
  Status,
  Video,
  date,
  languageName,
  time,
} from "./components";
import type {
  Instruction,
  Navigate,
  Organization,
  Version,
  WorkingCopy,
} from "./types";
export function Reader({
  org,
  id,
  navigate,
}: {
  org: Organization;
  id: string;
  navigate: Navigate;
}) {
  const [item, setItem] = useState<Instruction | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [active, setActive] = useState(0);
  const [segment, setSegment] = useState(true);
  const [busy, setBusy] = useState(false);
  const [newVersion, setNewVersion] = useState(false);
  const [versions, setVersions] = useState<Version[] | null>(null);
  const [withdrawOpen, setWithdrawOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [copied, setCopied] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const base = instructionApi(org.id, id);
  const isWriter = org.role !== "reader";
  const isReviewer = org.role === "admin" || org.role === "reviewer";
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await api<Instruction>(base);
      setItem(r);
      setNewVersion(false);
      setActive(0);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [base]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (!item?.publication) return;
    const timer = setInterval(async () => {
      try {
        const next = await api<Instruction>(base);
        if (!next.publication) {
          video.current?.pause();
          setItem(next);
        } else if (next.publication.id !== item.publication?.id)
          setNewVersion(true);
      } catch (e) {
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) {
          video.current?.pause();
          setItem(null);
          setError(
            "Инструкция больше недоступна. Обратитесь к ответственному.",
          );
        }
      }
    }, 30000);
    return () => clearInterval(timer);
  }, [base, item?.publication?.id]);
  async function edit() {
    if (item?.working_copy?.is_open) {
      navigate(
        instructionPath(org.id, id) +
          (item.working_copy.workflow === "in_review" ? "/review" : "/edit"),
      );
      return;
    }
    if (!item?.publication) return;
    setBusy(true);
    setError("");
    try {
      await api<WorkingCopy>(base + "/create-draft", {
        method: "POST",
        body: { source_version_id: item.publication.id },
      });
      navigate(instructionPath(org.id, id) + "/edit");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function history() {
    setBusy(true);
    setError("");
    try {
      const r = await api<{ items: Version[] }>(base + "/versions");
      setVersions(r.items);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function withdraw() {
    if (!item?.publication) return;
    setBusy(true);
    setError("");
    try {
      await api(base + "/withdraw-publication", {
        method: "POST",
        body: { reason, current_version_id: item.publication.id },
      });
      setWithdrawOpen(false);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function select(index: number) {
    setActive(index);
    if (video.current && item?.publication) {
      video.current.pause();
      video.current.currentTime =
        item.publication.snapshot.steps[index].start_ms / 1000;
    }
  }
  async function play() {
    if (!video.current || !item?.publication) return;
    const step = item.publication.snapshot.steps[active];
    if (
      segment &&
      (video.current.currentTime < step.start_ms / 1000 ||
        video.current.currentTime >= step.end_ms / 1000)
    )
      video.current.currentTime = step.start_ms / 1000;
    try {
      await video.current.play();
    } catch {
      setError(
        "Не удалось запустить видео. Попробуйте кнопку воспроизведения в проигрывателе.",
      );
    }
  }
  if (loading) return <Loading label="Открываем инструкцию…" />;
  if (!item?.publication)
    return (
      <>
        <button
          className="back-link"
          onClick={() => navigate(libraryPath(org.id))}
        >
          <Icon name="back" />
          Библиотека
        </button>
        {error && <ErrorBanner error={error} />}
        <div className="empty-state">
          <Icon name="book" size={32} />
          <h1>Инструкция пока недоступна</h1>
          <p>
            Публикация ещё не готова или была отозвана. Обратитесь к
            ответственному.
          </p>
          {item?.working_copy && isWriter && (
            <button
              className="button primary"
              onClick={() => navigate(instructionPath(org.id, id) + "/edit")}
            >
              Открыть рабочую версию
            </button>
          )}
        </div>
      </>
    );
  const version = item.publication;
  const { snapshot } = version;
  const current = snapshot.steps[active];
  return (
    <>
      <button
        className="back-link"
        onClick={() => navigate(libraryPath(org.id))}
      >
        <Icon name="back" size={17} />
        Все инструкции
      </button>
      <div className="reader-heading">
        <div className="editor-kicker">
          <Status status="published" />
          <span className="small muted">
            Версия {version.version_number} · {date(version.published_at)}
          </span>
        </div>
        <h1>{snapshot.title}</h1>
        <p>{snapshot.purpose}</p>
        <div className="reader-meta">
          <span>
            <Icon name="book" size={16} />
            {snapshot.steps.length}{" "}
            {countNoun(snapshot.steps.length, ["шаг", "шага", "шагов"])}
          </span>
          <span>
            <Icon name="clock" size={16} />
            {time(snapshot.source.duration_ms)}
          </span>
          <span>{languageName(snapshot.language)}</span>
          <span>
            <Icon name="shield" size={16} />
            Проверил: {version.published_by}
          </span>
        </div>
      </div>
      {error && <ErrorBanner error={error} />}{" "}
      {newVersion && (
        <div className="notice warning">
          <Icon name="info" />
          <div>
            <strong>Доступна новая версия</strong>
            <p>
              Текущий просмотр сохранён. Обновите инструкцию, когда будете
              готовы.
            </p>
            <button className="text-button" onClick={() => void load()}>
              Открыть новую версию
            </button>
          </div>
        </div>
      )}
      <div className="reader-grid">
        <section className="reader-video">
          <Video
            source={snapshot.source}
            videoRef={video}
            onTimeUpdate={() => {
              if (
                segment &&
                current &&
                video.current &&
                video.current.currentTime >= current.end_ms / 1000
              )
                video.current.pause();
            }}
          />
          <div className="playback-options">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={segment}
                onChange={(e) => setSegment(e.target.checked)}
              />
              <span>Воспроизводить только этот шаг</span>
            </label>
            <span>{time(snapshot.source.duration_ms)}</span>
          </div>
          {current && (
            <div className="current-step">
              <span className="eyebrow">
                Шаг {String(active + 1).padStart(2, "0")} из{" "}
                {snapshot.steps.length}
              </span>
              <h2>{current.title}</h2>
              <p>{current.description}</p>
              <SavedStepPhotos
                step={current}
                orgId={org.id}
                onJump={(ms) => {
                  if (video.current) {
                    video.current.pause();
                    video.current.currentTime = ms / 1000;
                    video.current.scrollIntoView({ block: "nearest" });
                  }
                }}
              />
              <button className="button primary" onClick={() => void play()}>
                <Icon name="play" size={17} />
                Посмотреть этот шаг
                <span className="button-time">
                  {time(current.end_ms - current.start_ms)}
                </span>
              </button>
              <div className="reader-step-nav">
                <button
                  className="text-button"
                  disabled={active === 0}
                  onClick={() => select(active - 1)}
                >
                  <Icon name="back" size={16} />
                  Предыдущий
                </button>
                <button
                  className="text-button"
                  disabled={active === snapshot.steps.length - 1}
                  onClick={() => select(active + 1)}
                >
                  Следующий
                  <Icon name="arrow" size={16} />
                </button>
              </div>
            </div>
          )}
        </section>
        <aside className="reader-outline">
          <div className="section-heading">
            <h2>По шагам</h2>
            <span className="small muted">
              {snapshot.steps.length}{" "}
              {countNoun(snapshot.steps.length, [
                "действие",
                "действия",
                "действий",
              ])}
            </span>
          </div>
          <ol>
            {snapshot.steps.map((step, index) => (
              <li key={step.id}>
                <button
                  className={active === index ? "active" : ""}
                  aria-current={active === index ? "step" : undefined}
                  onClick={() => select(index)}
                >
                  <span className="outline-number">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <span>
                    <strong>{step.title}</strong>
                    <small>
                      {time(step.start_ms)}–{time(step.end_ms)}
                    </small>
                  </span>
                  {active === index ? (
                    <Icon name="play" size={16} />
                  ) : (
                    <Icon name="check" size={15} />
                  )}
                </button>
              </li>
            ))}
          </ol>
          <div className="reader-proof">
            <Icon name="shield" />
            <p>
              Опубликованную версию проверил человек. Изменения проходят новую
              проверку.
            </p>
          </div>
        </aside>
      </div>
      <div className="reader-actions">
        <button
          className="button secondary"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(
                location.origin + instructionPath(org.id, id),
              );
              setCopied(true);
              setTimeout(() => setCopied(false), 2500);
            } catch {
              setError(
                "Не удалось скопировать ссылку. Скопируйте адрес из строки браузера.",
              );
            }
          }}
        >
          {copied ? "Ссылка скопирована" : "Копировать ссылку"}
          <Icon name={copied ? "check" : "arrow"} size={16} />
        </button>
        {isWriter && (
          <>
            <button
              className="button secondary"
              onClick={() => void edit()}
              disabled={busy}
            >
              <Icon name="edit" size={16} />
              {item.working_copy?.is_open
                ? "Открыть рабочую версию"
                : "Создать новую версию"}
            </button>
            <button
              className="text-button"
              onClick={() => void history()}
              disabled={busy}
            >
              История версий
            </button>
          </>
        )}
        {isReviewer && (
          <button
            className="text-button danger-text"
            onClick={() => setWithdrawOpen(true)}
            disabled={busy}
          >
            Отозвать публикацию
          </button>
        )}
      </div>
      <p className="small muted">
        Ссылка доступна только участникам организации после входа.
      </p>
      {versions && (
        <Dialog title="История версий" onClose={() => setVersions(null)}>
          <p className="muted">
            Опубликованные снимки сохраняют содержание на момент проверки.
          </p>
          <ul className="version-list">
            {versions.map((v) => (
              <li key={v.id}>
                <div>
                  <strong>Версия {v.version_number}</strong>
                  <span>{v.snapshot.title}</span>
                </div>
                <span>{date(v.published_at)}</span>
                {v.id === version.id && <Status status="published" />}
              </li>
            ))}
          </ul>
        </Dialog>
      )}
      {withdrawOpen && (
        <Dialog
          title="Отозвать публикацию"
          onClose={() => setWithdrawOpen(false)}
        >
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              void withdraw();
            }}
          >
            <p>
              «{snapshot.title}» исчезнет из библиотеки читателей. Старая версия
              не станет текущей автоматически.
            </p>
            <label>
              Причина отзыва
              <textarea
                autoFocus
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                minLength={10}
                maxLength={2000}
                required
                rows={4}
                disabled={busy}
              />
            </label>
            <button
              className="button danger"
              disabled={busy || reason.trim().length < 10}
            >
              Отозвать публикацию
            </button>
          </form>
        </Dialog>
      )}
    </>
  );
}
