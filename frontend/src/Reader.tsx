import { useCallback, useEffect, useState } from "react";
import { countNoun } from "./format";
import { CookingGuide } from "./CookingGuide";
import { FavoriteButton } from "./FavoriteButton";
import { cookingProgressKey } from "./cookingProgress";
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
  userId,
  navigate,
}: {
  org: Organization;
  id: string;
  userId: string;
  navigate: Navigate;
}) {
  const [item, setItem] = useState<Instruction | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [newVersion, setNewVersion] = useState(false);
  const [versions, setVersions] = useState<Version[] | null>(null);
  const [withdrawOpen, setWithdrawOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [copied, setCopied] = useState(false);
  const base = instructionApi(org.id, id);
  const book = libraryPath(org.id);
  const from = window.history.state?.fromBook;
  const returnToBook =
    typeof from === "string" && from.split("?")[0] === book ? from : book;
  const isWriter = org.role !== "reader";
  const isReviewer = org.role === "admin" || org.role === "reviewer";
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await api<Instruction>(base);
      setItem(r);
      setNewVersion(false);
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
          setItem(next);
        } else if (next.publication.id !== item.publication?.id)
          setNewVersion(true);
      } catch (e) {
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) {
          setItem(null);
          setError("Публикация больше недоступна. Обратитесь к автору.");
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
  if (loading) return <Loading label="Открываем публикацию…" />;
  if (!item?.publication)
    return (
      <>
        <button className="back-link" onClick={() => navigate(returnToBook)}>
          <Icon name="back" />
          Библиотека
        </button>
        {error && <ErrorBanner error={error} />}
        <div className="empty-state">
          <Icon name="book" size={32} />
          <h1>Публикация пока недоступна</h1>
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
  return (
    <>
      <button className="back-link" onClick={() => navigate(returnToBook)}>
        <Icon name="back" size={17} />
        Книга рецептов
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
            Видео {time(snapshot.source.duration_ms)}
          </span>
          <span>{languageName(snapshot.language)}</span>
          <span>
            <Icon name="shield" size={16} />
            Проверил: {version.published_by}
          </span>
          {typeof item.favorite === "boolean" && (
            <FavoriteButton
              orgId={org.id}
              id={id}
              title={snapshot.title}
              saved={item.favorite}
              onChange={(favorite) =>
                setItem((previous) => previous && { ...previous, favorite })
              }
              onError={setError}
            />
          )}
        </div>
      </div>
      {error && <ErrorBanner error={error} />}{" "}
      {newVersion && (
        <div className="notice warning">
          <Icon name="info" />
          <div>
            <strong>Доступна новая версия</strong>
            <p>
              Вы просматриваете прежнюю версию. Новую можно открыть, когда
              будете готовы.
            </p>
            <button className="text-button" onClick={() => void load()}>
              Открыть новую версию
            </button>
          </div>
        </div>
      )}
      <CookingGuide
        key={version.id}
        snapshot={snapshot}
        orgId={org.id}
        progressKey={
          snapshot.recipe
            ? cookingProgressKey(userId, org.id, id, version.id)
            : undefined
        }
      />
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
