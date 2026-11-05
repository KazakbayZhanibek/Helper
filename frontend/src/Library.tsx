import { useVideoUpload, UploadProgress, VideoFileFields } from "./Upload";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, libraryPath, instructionPath, orgApi, requestKey } from "./api";
import { countNoun } from "./format";
import {
  ErrorBanner,
  Icon,
  Loading,
  PackageArt,
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
  Session,
  WorkingCopy,
} from "./types";
export function Library({
  org,
  session,
  navigate,
}: {
  org: Organization;
  session: Session;
  navigate: Navigate;
}) {
  const initial = new URLSearchParams(location.search);
  const [query, setQuery] = useState(initial.get("q") || "");
  const [status, setStatus] = useState(initial.get("status") || "");
  const [language, setLanguage] = useState(initial.get("language") || "");
  const [items, setItems] = useState<InstructionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [moreBusy, setMoreBusy] = useState(false);
  const reader = org.role === "reader";
  const searchGeneration = useRef(0);
  const moreAbort = useRef<AbortController | null>(null);
  useEffect(() => {
    searchGeneration.current += 1;
    moreAbort.current?.abort();
    setMoreBusy(false);
    const abort = new AbortController();
    const timer = setTimeout(() => {
      setLoading(true);
      setError("");
      const params = new URLSearchParams();
      if (query.trim()) params.set("q", query.trim());
      if (status) params.set("status", status);
      if (language) params.set("language", language);
      history.replaceState(
        {},
        "",
        libraryPath(org.id) + (params.size ? "?" + params : ""),
      );
      api<{ items: InstructionSummary[]; next_cursor: string | null }>(
        `${orgApi(org.id)}/instructions?${params}`,
        { signal: abort.signal },
      )
        .then((r) => {
          setItems(r.items);
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
  }, [query, status, language, org.id, retry]);
  async function more() {
    if (!cursor || moreBusy) return;
    const generation = searchGeneration.current;
    const abort = new AbortController();
    moreAbort.current = abort;
    setMoreBusy(true);
    try {
      const params = new URLSearchParams({ cursor });
      if (query) params.set("q", query);
      if (status) params.set("status", status);
      if (language) params.set("language", language);
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
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Библиотека команды</div>
          <h1>Рабочие инструкции</h1>
          <p className="muted">Нужный опыт — в нужный момент.</p>
        </div>
        {!reader && (
          <button
            className="button primary"
            onClick={() => navigate(libraryPath(org.id) + "/new")}
          >
            <Icon name="plus" />
            Новая инструкция
          </button>
        )}
      </div>
      <section className="library-intro">
        <div>
          <span className="eyebrow">От мастера к команде</span>
          <h2>
            Опыт, который
            <br />
            остаётся в команде.
          </h2>
          <p>
            Понятные шаги. Наглядное видео.
            <br />
            Проверено человеком, который знает работу.
          </p>
          <span className="intro-foot">
            <span className="tiny-check">
              <Icon name="check" size={12} />
            </span>{" "}
            Сначала проверка — потом публикация
          </span>
        </div>
        <div className="intro-visual">
          <PackageArt />
          <span className="visual-label">
            <Icon name="book" size={16} /> Один процесс. Всё по шагам.
          </span>
        </div>
      </section>
      {session.demo_mode && (
        <div className="demo-note">
          <span className="small-dot" />
          <span>Локальное пространство · опыт вашей команды</span>
        </div>
      )}
      <div className="library-toolbar">
        <div className="search-field">
          <Icon name="search" />
          <input
            type="search"
            aria-label="Найти инструкцию"
            placeholder="Найти инструкцию или рабочий шаг"
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
        <label className="select-filter">
          <span className="sr-only">Язык инструкции</span>
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
      <div className="tabs" aria-label="Статус инструкций">
        {(reader
          ? [["", "Все инструкции"]]
          : [
              ["", "Все инструкции"],
              ["published", "Опубликованные"],
              ["draft", "Черновики"],
              ["in_review", "На проверке"],
            ]
        ).map(([value, label]) => (
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
        <Loading label="Загружаем инструкции…" />
      ) : items.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">
            <Icon name={query ? "search" : "book"} size={30} />
          </div>
          <h2>{query ? "Ничего не нашлось" : "Здесь появятся инструкции"}</h2>
          <p>
            {query
              ? `По запросу «${query}» нет подходящих инструкций. Попробуйте другое слово.`
              : reader
                ? "После проверки мастером инструкции будут доступны вашей команде."
                : "Создайте первую инструкцию и разметьте рабочий процесс по шагам."}
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
          ) : (
            !reader && (
              <button
                className="button primary"
                onClick={() => navigate(libraryPath(org.id) + "/new")}
              >
                <Icon name="plus" />
                Создать инструкцию
              </button>
            )
          )}
        </div>
      ) : (
        <>
          <div className="results-line">
            <span>
              {items.length}{" "}
              {countNoun(items.length, [
                "инструкция",
                "инструкции",
                "инструкций",
              ])}
            </span>
            <span>Последние изменения</span>
          </div>
          <div className="instruction-grid">
            {items.map((item, i) => (
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
                      <PackageArt variant={i} />
                    )}
                    <span className="card-duration">
                      <Icon name="play" size={12} />
                      {item.duration_ms ? time(item.duration_ms) : "Без видео"}
                    </span>
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
      <footer className="workspace-footer">
        <span>Хорошая инструкция начинается с опыта.</span>
        <span>Опыт мастера · 2026</span>
      </footer>
    </>
  );
}
export function NewInstruction({
  org,
  session,
  navigate,
  setDirtyGuard,
}: {
  org: Organization;
  session: Session;
  navigate: Navigate;
  setDirtyGuard: (dirty: boolean) => void;
}) {
  const [title, setTitle] = useState("");
  const [purpose, setPurpose] = useState("");
  const [language, setLanguage] = useState<Language>("ru");
  const [sourceMode, setSourceMode] = useState<"real" | "demo" | "camera">(
    "real",
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
  useEffect(() => {
    setDirtyGuard(locked);
    const prevent = (event: BeforeUnloadEvent) => {
      if (locked) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", prevent);
    return () => {
      setDirtyGuard(false);
      window.removeEventListener("beforeunload", prevent);
    };
  }, [locked, setDirtyGuard]);
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
            ...(sourceMode === "demo" ? { demo_source: true } : {}),
          },
          key: key.current,
        }));
      setCreated(wc);
      if (
        sourceMode === "demo" ||
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
        Библиотека
      </button>
      <div className="page-heading">
        <div>
          <span className="eyebrow">Новая инструкция</span>
          <h1>С чего начнём?</h1>
          <p className="muted">
            Один рабочий процесс — одна понятная инструкция.
          </p>
        </div>
      </div>
      <div className="create-layout">
        <form className="form-panel stack" onSubmit={create}>
          <label>
            Название процесса
            <span className="field-hint">
              Например, «Комплектация учебного набора»
            </span>
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
          <label>
            Что должно получиться
            <span className="field-hint">
              Опишите результат работы. От 10 до 600 символов.
            </span>
            <textarea
              value={purpose}
              onChange={(e) => {
                setPurpose(e.target.value);
                key.current = requestKey();
              }}
              required
              minLength={10}
              maxLength={600}
              rows={4}
              disabled={locked || !!created}
            />
          </label>
          <label>
            Язык инструкции
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
          <fieldset className="source-mode" disabled={locked || !!created}>
            <legend>Источник видео</legend>
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
              <span>Камера · записать процесс</span>
            </label>
            {session.demo_mode && (
              <label className="checkbox-label">
                <input
                  type="radio"
                  name="source-mode"
                  checked={sourceMode === "demo"}
                  onChange={() => {
                    setSourceMode("demo");
                    key.current = requestKey();
                  }}
                />
                <span>Учебный пример без речи</span>
              </label>
            )}
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
                <strong>Снимите процесс прямо здесь</strong>
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
                <strong>Учебное видео комплектации</strong>
                <p>
                  Синтетическая запись для ручной разметки и проверки. В ней нет
                  речи для распознавания.
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
                  : sourceMode === "demo"
                    ? "Перейти к шагам"
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
        <aside className="create-aside">
          <span className="eyebrow">Как это работает</span>
          <ol>
            <li>
              <span>01</span>
              <div>
                <h3>Загрузите запись</h3>
                <p>Снимите рабочий процесс и поясняйте действия вслух.</p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <h3>Составьте шаги</h3>
                <p>
                  Распознайте действия по кадрам, используйте расшифровку речи
                  или разметьте запись вручную.
                </p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <h3>Передайте на проверку</h3>
                <p>Мастер сверит шаги с видео перед публикацией.</p>
              </div>
            </li>
          </ol>
          <p className="muted small">
            ИИ предлагает текст отдельно. Вы решаете, применять ли его к
            инструкции.
          </p>
        </aside>
      </div>
    </>
  );
}
