import { useVideoUpload, UploadProgress, VideoFileFields } from "./Upload";
import { FavoriteButton } from "./FavoriteButton";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, libraryPath, instructionPath, orgApi, requestKey } from "./api";
import { countNoun } from "./format";
import {
  ErrorBanner,
  Icon,
  Loading,
  RecipeCover,
  Status,
  date,
  languageName,
  time,
} from "./components";
import type {
  InstructionSummary,
  Language,
  Navigate,
  Organization,
  WorkingCopy,
} from "./types";
export function Library({
  org,
  navigate,
}: {
  org: Organization;
  navigate: Navigate;
}) {
  const initial = new URLSearchParams(location.search);
  const [query, setQuery] = useState(initial.get("q") || "");
  const [archive, setArchive] = useState(initial.get("kind") === "legacy");
  const [favorites, setFavorites] = useState(
    !archive && initial.get("favorites") === "1",
  );
  const [status, setStatus] = useState(
    org.role === "reader" || favorites ? "" : initial.get("status") || "",
  );
  const kind = archive ? "legacy" : "recipe";
  const [language, setLanguage] = useState(initial.get("language") || "");
  const [items, setItems] = useState<InstructionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [favoriteError, setFavoriteError] = useState("");
  const [total, setTotal] = useState(0);
  const [retry, setRetry] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [moreBusy, setMoreBusy] = useState(false);
  const reader = org.role === "reader";
  const searchGeneration = useRef(0);
  const moreAbort = useRef<AbortController | null>(null);
  const filters = new URLSearchParams();
  if (query.trim()) filters.set("q", query.trim());
  if (status) filters.set("status", status);
  if (language) filters.set("language", language);
  if (archive) filters.set("kind", "legacy");
  if (favorites) filters.set("favorites", "1");
  const search = filters.toString();
  useEffect(() => {
    searchGeneration.current += 1;
    moreAbort.current?.abort();
    setMoreBusy(false);
    const abort = new AbortController();
    setLoading(true);
    setError("");
    setFavoriteError("");
    setCursor(null);
    const params = new URLSearchParams(search);
    history.replaceState(
      history.state,
      "",
      libraryPath(org.id) + (params.size ? "?" + params : ""),
    );
    const timer = setTimeout(() => {
      params.set("kind", kind);
      api<{
        items: InstructionSummary[];
        next_cursor: string | null;
        total: number;
      }>(`${orgApi(org.id)}/instructions?${params}`, { signal: abort.signal })
        .then((r) => {
          if (abort.signal.aborted) return;
          setItems(r.items);
          setTotal(r.total);
          setCursor(r.next_cursor);
        })
        .catch((e) => {
          if (e.name !== "AbortError") setError(e.message);
        })
        .finally(() => {
          if (!abort.signal.aborted) setLoading(false);
        });
    }, 300);
    return () => {
      clearTimeout(timer);
      abort.abort();
      moreAbort.current?.abort();
    };
  }, [search, org.id, retry, kind]);
  async function more() {
    if (!cursor || moreBusy) return;
    const generation = searchGeneration.current;
    const abort = new AbortController();
    moreAbort.current = abort;
    setMoreBusy(true);
    try {
      const params = new URLSearchParams(search);
      params.set("cursor", cursor);
      params.set("kind", kind);
      const r = await api<{
        items: InstructionSummary[];
        next_cursor: string | null;
      }>(`${orgApi(org.id)}/instructions?${params}`, { signal: abort.signal });
      if (generation === searchGeneration.current) {
        setItems((a) => [...a, ...r.items]);
        setCursor(r.next_cursor);
      }
    } catch (e) {
      if (!abort.signal.aborted && generation === searchGeneration.current)
        setError((e as Error).message);
    } finally {
      if (generation === searchGeneration.current) setMoreBusy(false);
    }
  }
  const displayedGeneration = searchGeneration.current;
  return (
    <>
      <div className="page-heading recipe-library-heading">
        <div>
          <h1>{archive ? "Архив материалов" : "Книга рецептов"}</h1>
          <p className="muted">
            {archive
              ? "Здесь хранятся прежние инструкции вашей команды."
              : reader
                ? "Выберите блюдо и готовьте по шагам."
                : org.name}
          </p>
        </div>
        {!reader &&
          (loading ||
            items.length > 0 ||
            !!query ||
            !!status ||
            !!language ||
            archive ||
            favorites) && (
            <button
              className="button primary"
              onClick={() => navigate(libraryPath(org.id) + "/new")}
            >
              <Icon name="plus" /> Новый рецепт
            </button>
          )}
      </div>
      <div className="library-toolbar">
        <div className="search-field">
          <Icon name="search" />
          <input
            type="search"
            aria-label={archive ? "Найти инструкцию" : "Найти рецепт"}
            placeholder={
              archive
                ? "Название или текст инструкции"
                : "Название блюда или ингредиент"
            }
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button
              className="icon-button"
              onClick={() => setQuery("")}
              aria-label="Очистить поиск"
            >
              <Icon name="close" size={16} />
            </button>
          )}
        </div>
        {!archive && (
          <button
            className="favorite-filter"
            type="button"
            aria-pressed={favorites}
            onClick={() => {
              setFavorites(!favorites);
              setStatus("");
            }}
          >
            <Icon name="bookmark" size={17} /> Избранное
          </button>
        )}
        <details className="library-options">
          <summary>Параметры поиска</summary>
          <div>
            <label>
              Раздел
              <select
                value={archive ? "legacy" : "recipe"}
                onChange={(e) => {
                  setArchive(e.target.value === "legacy");
                  setFavorites(false);
                  setStatus("");
                }}
              >
                <option value="recipe">Рецепты</option>
                <option value="legacy">Архив инструкций</option>
              </select>
            </label>
            <label className="select-filter">
              <span className="sr-only">Язык</span>
              <select
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              >
                <option value="">Все языки</option>
                <option value="ru">Русский</option>
                <option value="kk">Қазақша</option>
                <option value="mixed">Смешанный</option>
              </select>
            </label>
          </div>
        </details>
      </div>
      {!reader && !favorites && (
        <div className="tabs" role="group" aria-label="Статус публикации">
          {[
            ["", archive ? "Все материалы" : "Все рецепты"],
            ["published", "Опубликованные"],
            ["draft", "Черновики"],
            ["in_review", "На проверке"],
          ].map(([value, label]) => (
            <button
              key={value}
              className={status === value ? "active" : ""}
              aria-pressed={status === value}
              onClick={() => setStatus(value)}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      {favoriteError && <ErrorBanner error={favoriteError} />}
      {error ? (
        <ErrorBanner error={error}>
          <button
            className="text-button"
            onClick={() => setRetry((n) => n + 1)}
          >
            Повторить загрузку
          </button>
        </ErrorBanner>
      ) : loading ? (
        <Loading
          label={archive ? "Загружаем материалы…" : "Загружаем рецепты…"}
        />
      ) : items.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">
            <Icon name={query ? "search" : "book"} size={30} />
          </div>
          <h2>
            {query || status || language
              ? "Ничего не нашлось"
              : favorites
                ? "В избранном пока нет рецептов"
                : archive
                  ? "Архив пуст"
                  : reader
                    ? "Рецепты появятся здесь"
                    : "Добавьте первый рецепт"}
          </h2>
          <p>
            {query || status || language
              ? "Измените запрос или сбросьте фильтры."
              : favorites
                ? "Нажмите на закладку у блюда, которое хотите приготовить ещё раз."
                : archive
                  ? "Прежние инструкции будут доступны в этом разделе."
                  : reader
                    ? "Здесь появятся рецепты, которые опубликует ваша кухня."
                    : "Начните с названия блюда. Видео и ингредиенты можно добавить позже."}
          </p>
          {query || status || language ? (
            <button
              className="button secondary"
              onClick={() => {
                setQuery("");
                setStatus("");
                setLanguage("");
              }}
            >
              Сбросить фильтры
            </button>
          ) : favorites ? (
            <button
              type="button"
              className="button secondary"
              onClick={() => setFavorites(false)}
            >
              Все рецепты
            </button>
          ) : (
            !reader &&
            !archive && (
              <button
                className="button primary"
                onClick={() => navigate(libraryPath(org.id) + "/new")}
              >
                <Icon name="plus" />
                Добавить рецепт
              </button>
            )
          )}
        </div>
      ) : (
        <>
          <div className="results-line">
            <span>
              {total}{" "}
              {countNoun(
                total,
                archive
                  ? ["материал", "материала", "материалов"]
                  : ["рецепт", "рецепта", "рецептов"],
              )}
            </span>
            <span>Последние изменения</span>
          </div>
          <div className="instruction-grid">
            {items.map((item) => (
              <article className="instruction-card" key={item.id}>
                <button
                  className="card-open"
                  onClick={() =>
                    navigate(
                      instructionPath(org.id, item.id) +
                        (item.status === "published"
                          ? ""
                          : item.status === "in_review"
                            ? "/review"
                            : "/edit"),
                    )
                  }
                  aria-label={`Открыть: ${item.title}`}
                >
                  <div className="card-image">
                    {item.thumbnail_url ? (
                      <img
                        className="card-thumbnail"
                        src={item.thumbnail_url}
                        alt=""
                        loading="lazy"
                      />
                    ) : (
                      <RecipeCover legacy={archive} />
                    )}
                    {item.duration_ms != null && (
                      <span
                        className="card-duration"
                        aria-label={`Длительность видео ${time(item.duration_ms)}`}
                      >
                        <Icon name="play" size={12} />
                        {time(item.duration_ms)}
                      </span>
                    )}
                    {item.source_is_demo && (
                      <span className="card-source">Учебный материал</span>
                    )}
                  </div>
                  <div className="card-body">
                    <Status status={item.status} />
                    <h2>{item.title}</h2>
                    <p>{item.purpose}</p>
                    <div className="card-facts">
                      <span>
                        <Icon name="book" size={15} />
                        {item.step_count}{" "}
                        {countNoun(item.step_count, ["шаг", "шага", "шагов"])}
                      </span>
                      {!archive && item.servings != null && (
                        <span>
                          {item.servings}{" "}
                          {countNoun(item.servings, [
                            "порция",
                            "порции",
                            "порций",
                          ])}
                        </span>
                      )}
                      <span>{languageName(item.language)}</span>
                    </div>
                    <div className="card-footer">
                      <span className="avatar mini">
                        {(item.owner_name || "М")[0]}
                      </span>
                      <span>{item.owner_name || "Команда"}</span>
                      <time dateTime={item.updated_at}>
                        {date(item.updated_at)}
                      </time>
                      <Icon name="arrow" size={17} />
                    </div>
                  </div>
                </button>
                {!archive && typeof item.favorite === "boolean" && (
                  <FavoriteButton
                    compact
                    orgId={org.id}
                    id={item.id}
                    title={item.title}
                    saved={item.favorite}
                    onError={setFavoriteError}
                    onChange={(saved) => {
                      if (displayedGeneration !== searchGeneration.current) {
                        setRetry((n) => n + 1);
                        return;
                      }
                      setFavoriteError("");
                      if (favorites && !saved) {
                        // Reload paged results because removing a row shifts the server cursor.
                        if (cursor || moreBusy) setRetry((n) => n + 1);
                        else {
                          setItems((rows) =>
                            rows.filter((row) => row.id !== item.id),
                          );
                          setTotal((n) => Math.max(0, n - 1));
                        }
                      } else {
                        setItems((rows) =>
                          rows.map((row) =>
                            row.id === item.id
                              ? { ...row, favorite: saved }
                              : row,
                          ),
                        );
                      }
                    }}
                  />
                )}
              </article>
            ))}
          </div>
          {cursor && (
            <div className="load-more">
              <button
                className="button secondary"
                onClick={more}
                disabled={moreBusy}
              >
                {moreBusy ? "Загружаем…" : "Показать ещё"}
              </button>
            </div>
          )}
        </>
      )}
    </>
  );
}
export function NewRecipe({
  org,
  navigate,
  setDirtyGuard,
}: {
  org: Organization;
  navigate: Navigate;
  setDirtyGuard: (dirty: boolean) => void;
}) {
  const [title, setTitle] = useState("");
  const [purpose, setPurpose] = useState("");
  const [language, setLanguage] = useState<Language>("ru");
  const [sourceMode, setSourceMode] = useState<"real" | "manual" | "camera">(
    "manual",
  );
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<WorkingCopy | null>(null);
  const key = useRef(requestKey());
  const creating = useRef(false);
  const upload = useVideoUpload(org.id);
  const locked = busy || upload.sending;
  const unsaved =
    locked ||
    (!created &&
      Boolean(
        title ||
        purpose ||
        file ||
        language !== "ru" ||
        sourceMode !== "manual",
      ));
  useEffect(() => {
    setDirtyGuard(unsaved);
    const prevent = (event: BeforeUnloadEvent) => {
      if (unsaved) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", prevent);
    return () => {
      setDirtyGuard(false);
      window.removeEventListener("beforeunload", prevent);
    };
  }, [unsaved, setDirtyGuard]);
  function openEditor(wc: WorkingCopy) {
    setDirtyGuard(false);
    navigate(
      instructionPath(org.id, wc.instruction_id) +
        (sourceMode === "camera" ? "/edit?camera=1" : "/edit"),
    );
  }
  async function create(event: FormEvent) {
    event.preventDefault();
    if (creating.current || (sourceMode === "real" && (!file || !rights)))
      return;
    if (title.trim().length < 3) {
      setError("Введите название блюда из трёх или более символов.");
      return;
    }
    creating.current = true;
    setBusy(true);
    setError("");
    try {
      const wc =
        created ||
        (await api<WorkingCopy>(`${orgApi(org.id)}/instructions`, {
          method: "POST",
          body: {
            title: title.trim(),
            purpose: purpose.trim(),
            language,
            recipe: { ingredients: [], servings: null },
          },
          key: key.current,
        }));
      setCreated(wc);
      if (
        sourceMode === "manual" ||
        sourceMode === "camera" ||
        (file &&
          (await upload.start(
            `${orgApi(org.id)}/instructions/${wc.instruction_id}`,
            file,
            language,
          )))
      )
        openEditor(wc);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      creating.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      <button
        className="back-link"
        onClick={() => navigate(libraryPath(org.id))}
      >
        <Icon name="back" size={17} />
        Книга рецептов
      </button>
      <div className="page-heading">
        <div>
          <h1>Новый рецепт</h1>
          <p className="muted">
            Назовите блюдо и выберите, как добавить запись приготовления.
          </p>
        </div>
      </div>
      <div className="create-layout simple-create">
        <form className="form-panel stack" onSubmit={create}>
          <label>
            Название блюда
            <span className="field-hint">Например, «Домашняя лапша»</span>
            <input
              value={title}
              onChange={(e) => {
                setTitle(e.target.value);
                key.current = requestKey();
              }}
              required
              minLength={3}
              maxLength={120}
              disabled={locked || !!created}
              autoFocus
            />
          </label>
          <details className="form-details">
            <summary>Описание и язык · необязательно</summary>
            <label>
              Описание блюда
              <span className="field-hint">
                Что приготовим и каким должно получиться блюдо? До 600 символов.
              </span>
              <textarea
                value={purpose}
                onChange={(e) => {
                  setPurpose(e.target.value);
                  key.current = requestKey();
                }}
                maxLength={600}
                rows={4}
                disabled={locked || !!created}
              />
            </label>
            <label>
              Язык рецепта
              <select
                value={language}
                onChange={(e) => {
                  setLanguage(e.target.value as Language);
                  key.current = requestKey();
                }}
                disabled={locked || !!created}
              >
                <option value="ru">Русский</option>
                <option value="kk">Қазақша · экспериментально</option>
                <option value="mixed">Смешанный · экспериментально</option>
              </select>
            </label>
          </details>
          <fieldset className="source-mode" disabled={locked || !!created}>
            <legend>Видео приготовления</legend>
            <label className="checkbox-label">
              <input
                type="radio"
                name="source-mode"
                checked={sourceMode === "real"}
                onChange={() => {
                  setSourceMode("real");
                  key.current = requestKey();
                }}
              />
              <span>Своя видеозапись</span>
            </label>
            <label className="checkbox-label">
              <input
                type="radio"
                name="source-mode"
                checked={sourceMode === "camera"}
                onChange={() => {
                  setSourceMode("camera");
                  key.current = requestKey();
                }}
              />
              <span>Записать на камеру</span>
            </label>
            <label className="checkbox-label">
              <input
                type="radio"
                name="source-mode"
                checked={sourceMode === "manual"}
                onChange={() => {
                  setSourceMode("manual");
                  key.current = requestKey();
                }}
              />
              <span>Начать без видео</span>
            </label>
          </fieldset>
          {sourceMode === "real" ? (
            <VideoFileFields
              file={file}
              onFile={setFile}
              rights={rights}
              onRights={setRights}
              disabled={locked}
            />
          ) : sourceMode === "camera" ? (
            <div className="source-choice">
              <Icon name="eye" />
              <div>
                <strong>Запишите приготовление</strong>
                <p>
                  На следующем экране вы сможете включить камеру, записать видео
                  и отдельно разрешить ИИ наблюдать за действиями. Сейчас камера
                  выключена.
                </p>
              </div>
            </div>
          ) : (
            <div className="source-choice">
              <Icon name="play" />
              <div>
                <strong>Начните с ингредиентов</strong>
                <p>
                  Черновик откроется в редакторе. Видеозапись можно загрузить,
                  когда будете готовы показать приготовление.
                </p>
              </div>
            </div>
          )}
          {(error || upload.error) && (
            <ErrorBanner error={error || upload.error} />
          )}
          {upload.notice && <p role="status">{upload.notice}</p>}
          {upload.sending && (
            <UploadProgress
              progress={upload.progress}
              cancel={() => void upload.cancel()}
            />
          )}
          {!upload.sending && (
            <button
              className="button primary"
              disabled={locked || (sourceMode === "real" && (!file || !rights))}
            >
              {busy
                ? "Создаём…"
                : sourceMode === "camera"
                  ? "Создать и перейти к камере"
                  : sourceMode === "manual"
                    ? "Создать черновик"
                    : created
                      ? "Повторить загрузку"
                      : "Создать и загрузить видео"}
              <Icon name="arrow" />
            </button>
          )}
          {created && !locked && (
            <button
              className="text-button"
              type="button"
              onClick={() => openEditor(created)}
            >
              Открыть сохранённый черновик
            </button>
          )}
        </form>
      </div>
    </>
  );
}
