import { CameraCapture } from "./CameraCapture";
import { DeleteRecipeDraft } from "./DeleteRecipeDraft";
import { VisualAnalysis } from "./VisualAnalysis";
import { RecipeStepPhotos } from "./RecipeStepPhotos";
import { TeachingClip } from "./TeachingClip";
import { ProcessingPanel } from "./Processing";
import { RecipeEditor, RecipeIngredients } from "./RecipeDetails";
import { UploadCard } from "./Upload";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type RefObject,
} from "react";
import {
  api,
  ApiError,
  instructionApi,
  instructionPath,
  libraryPath,
  requestKey,
} from "./api";
import {
  Dialog,
  ErrorBanner,
  Icon,
  Loading,
  Status,
  Video,
  time,
} from "./components";
import type { Navigate, Organization, Version, WorkingCopy } from "./types";
import { formatMsExact, parseMs } from "./format";
import {
  payload,
  recipeFromInputs,
  toDraft,
  type Draft,
  type DraftStep,
} from "./editorDraft";
import { stepWarningMessages } from "./visual";
import "./recipe-author.css";

type ClipRange = { start: number; end: number };

function StepMarker({
  video,
  duration,
  disabled,
  onAdd,
}: {
  video: RefObject<HTMLVideoElement | null>;
  duration: number | null;
  disabled: boolean;
  onAdd: (range: ClipRange) => void;
}) {
  const [start, setStart] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  function mark() {
    const player = video.current;
    if (
      !player ||
      player.readyState < HTMLMediaElement.HAVE_METADATA ||
      !Number.isFinite(player.duration)
    ) {
      setMessage("Дождитесь загрузки видео и повторите.");
      return;
    }
    const last = Math.min(
      Math.round(player.duration * 1000),
      duration ?? Infinity,
    );
    const moment = Math.min(Math.round(player.currentTime * 1000), last);
    if (start === null) {
      if (moment >= last) {
        setMessage("Перемотайте запись к началу действия.");
        return;
      }
      setStart(moment);
      setMessage("");
      return;
    }
    if (moment <= start) {
      setMessage("Воспроизведите видео или перемотайте его до конца действия.");
      return;
    }
    player.pause();
    onAdd({ start, end: moment });
    setStart(null);
    setMessage("");
  }
  return (
    <fieldset className="recipe-step-marker" disabled={disabled}>
      <legend>Шаг из видео</legend>
      <p aria-live="polite">
        {message ||
          (start === null
            ? "Найдите начало действия в записи."
            : `Начало — ${formatMsExact(start)}. Найдите конец действия.`)}
      </p>
      <div className="button-row">
        <button type="button" className="button secondary" onClick={mark}>
          <Icon name={start === null ? "clock" : "plus"} size={16} />
          {start === null ? "Отметить начало" : "Добавить шаг до этого момента"}
        </button>
        {start !== null && (
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setStart(null);
              setMessage("");
            }}
          >
            Сбросить отметку
          </button>
        )}
      </div>
    </fieldset>
  );
}
type AuthorTask = { label: string; target: string };
type Issue = AuthorTask & { message: string };

function validation(form: Draft): Issue[] {
  const errors: Issue[] = [];
  function issue(target: string, label: string, message: string) {
    errors.push({ target, label, message });
  }
  if (form.title.trim().length < 3)
    issue(
      "#recipe-title",
      "Уточнить название",
      "Название должно содержать не менее 3 символов.",
    );
  const recipe = recipeFromInputs(form);
  if (!recipe && form.purpose.trim().length < 10)
    issue(
      "#recipe-purpose",
      "Описать результат",
      "Опишите результат хотя бы в 10 символах.",
    );
  if (recipe) {
    if (recipe.ingredients.length > 30)
      issue(
        ".recipe-editor textarea",
        "Уточнить ингредиенты",
        "Добавьте не больше 30 ингредиентов.",
      );
    if (recipe.ingredients.some((line) => [...line].length > 160))
      issue(
        ".recipe-editor textarea",
        "Уточнить ингредиенты",
        "Сократите строку ингредиента до 160 символов.",
      );
    if (
      recipe.servings !== null &&
      (!Number.isInteger(recipe.servings) ||
        recipe.servings < 1 ||
        recipe.servings > 1000)
    )
      issue(
        ".recipe-servings input",
        "Уточнить порции",
        "Укажите от 1 до 1000 порций или оставьте поле пустым.",
      );
  }
  form.steps.forEach((s, i) => {
    if (s.title.trim().length < 3)
      issue(
        `#step-title-${i}`,
        `Назвать шаг ${i + 1}`,
        `Шаг ${i + 1}: добавьте название от 3 символов.`,
      );
    if (!s.description.trim())
      issue(
        `#step-description-${i}`,
        `Описать шаг ${i + 1}`,
        `Шаг ${i + 1}: опишите действие.`,
      );
    const start = parseMs(s.startInput),
      end = parseMs(s.endInput);
    if (
      !Number.isFinite(start) ||
      !Number.isFinite(end) ||
      end <= start ||
      start < 0
    )
      issue(
        !Number.isFinite(start) ? `#step-start-${i}` : `#step-end-${i}`,
        `Уточнить время шага ${i + 1}`,
        `Шаг ${i + 1}: укажите начало и конец в формате 00:00.0, конец позже начала.`,
      );
    else if (
      form.source?.status === "ready" &&
      form.source.duration_ms !== null &&
      end > form.source.duration_ms
    )
      issue(
        `#step-end-${i}`,
        `Уточнить время шага ${i + 1}`,
        `Шаг ${i + 1}: конец выходит за длительность видео ${time(form.source.duration_ms)}.`,
      );
  });
  return errors;
}

function nextReviewTask(form: Draft): AuthorTask | null {
  const recipe = recipeFromInputs(form);
  if (!recipe) return null;
  if (!form.source) return { label: "Добавить видео", target: "#recipe-video" };
  if (form.source.status !== "ready")
    return { label: "Проверить видео", target: "#recipe-video" };
  if (!recipe.ingredients.length)
    return { label: "Добавить ингредиенты", target: ".recipe-editor textarea" };
  if (!form.steps.length)
    return { label: "Выбрать фрагмент", target: "#recipe-video video" };
  return validation(form)[0] ?? null;
}
const confirmationLabels = {
  order: "Порядок действий правильный",
  terminology: "Названия и термины понятны",
  completeness: "Пояснений достаточно для выполнения",
  relevance: "Инструкция актуальна",
  privacy: "В записи нет лишних чувствительных данных",
};
const recipeConfirmationLabels: typeof confirmationLabels = {
  order: "Этапы приготовления идут по порядку",
  terminology: "По описанию понятно, что и как делать",
  completeness: "Ингредиенты и указанные количества проверены",
  relevance: "Каждое действие сверено с записью или подтверждено автором",
  privacy: "Запись можно показывать участникам кухни",
};
export function Editor({
  org,
  userId,
  id,
  navigate,
  setDirtyGuard,
}: {
  org: Organization;
  userId: string;
  id: string;
  navigate: Navigate;
  setDirtyGuard: (dirty: boolean) => void;
}) {
  const [form, setForm] = useState<Draft | null>(null);
  const [baseline, setBaseline] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pipelineBusy, setPipelineBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [cameraOpen, setCameraOpen] = useState(
    () => new URLSearchParams(location.search).get("camera") === "1",
  );
  const [refreshSeed, setRefreshSeed] = useState(0);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [showLocal, setShowLocal] = useState(false);
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [deleteIndex, setDeleteIndex] = useState<number | null>(null);
  const [returnOpen, setReturnOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [confirmations, setConfirmations] = useState<Record<string, boolean>>(
    {},
  );
  const [savedNotice, setSavedNotice] = useState("");
  const video = useRef<HTMLVideoElement>(null);
  const addedStep = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (addedStep.current === null) return;
    document.getElementById(`step-title-${addedStep.current}`)?.focus();
    addedStep.current = null;
  }, [form?.steps.length]);
  const inFlight = useRef(false);
  const publishKey = useRef(requestKey());
  const base = instructionApi(org.id, id);
  const dirty = !!form && JSON.stringify(payload(form)) !== baseline;
  const formRef = useRef(form);
  const baselineRef = useRef(baseline);
  formRef.current = form;
  baselineRef.current = baseline;
  const reviewer = org.role === "admin" || org.role === "reviewer";
  const accept = useCallback((wc: WorkingCopy) => {
    const next = toDraft(wc);
    setForm(next);
    setBaseline(JSON.stringify(payload(next)));
    setConflict(false);
    setError("");
    setValidationErrors([]);
    setConfirmations({});
    publishKey.current = requestKey();
  }, []);
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      accept(await api<WorkingCopy>(base + "/working-copy"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [base, accept]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    setDirtyGuard(dirty || uploading);
    const prevent = (e: BeforeUnloadEvent) => {
      if (dirty || uploading) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", prevent);
    return () => {
      setDirtyGuard(false);
      window.removeEventListener("beforeunload", prevent);
    };
  }, [dirty, uploading, setDirtyGuard]);
  async function save(explicit = true): Promise<WorkingCopy | null> {
    if (!form || inFlight.current || conflict) return null;
    const errors = validation(form);
    if (errors.length) {
      if (explicit) setValidationErrors(errors.map((e) => e.message));
      return null;
    }
    inFlight.current = true;
    setSaving(true);
    setError("");
    setSavedNotice("");
    try {
      const wc = await api<WorkingCopy>(base + "/working-copy", {
        method: "PUT",
        body: payload(form),
        etag: form.etag,
      });
      accept(wc);
      setSavedNotice("Все изменения сохранены");
      return wc;
    } catch (e) {
      if (e instanceof ApiError && e.status === 412) setConflict(true);
      setError((e as Error).message);
      return null;
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  }
  useEffect(() => {
    if (
      !dirty ||
      !form?.editable ||
      busy ||
      pipelineBusy ||
      uploading ||
      saving ||
      conflict ||
      error
    )
      return;
    const timer = setTimeout(() => {
      void save(false);
    }, 1500);
    return () => clearTimeout(timer);
  });
  async function refreshSource() {
    const wc = await api<WorkingCopy>(base + "/working-copy");
    const current = formRef.current;
    if (!current || inFlight.current) return;
    if (JSON.stringify(payload(current)) === baselineRef.current) accept(wc);
    else setForm((f) => (f ? { ...f, source: wc.source } : f));
  }
  async function prepareAi() {
    if (!form || conflict || inFlight.current) return null;
    if (dirty) return save();
    return form;
  }
  async function action(name: string, body: unknown = {}, etag?: string) {
    if (!form || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      const wc = await api<WorkingCopy>(base + "/" + name, {
        method: "POST",
        body,
        etag: etag || form.etag,
      });
      accept(wc);
      setReturnOpen(false);
      setSavedNotice(
        name === "submit-review"
          ? "Передано на проверку"
          : name === "return-to-draft"
            ? "Возвращено автору"
            : "Изменения сохранены",
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 412) setConflict(true);
      setError((e as Error).message);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  async function submitReview() {
    if (!form) return;
    const errors = validation(form).map((e) => e.message);
    if (!form.steps.length) errors.push("Добавьте хотя бы один шаг.");
    const recipe = recipeFromInputs(form);
    if (recipe && !recipe.ingredients.length)
      errors.push("Перед проверкой добавьте хотя бы один ингредиент.");
    if (errors.length) {
      setValidationErrors(errors);
      return;
    }
    let etag = form.etag;
    if (dirty) {
      const saved = await save();
      if (!saved) return;
      etag = saved.etag;
    }
    await action("submit-review", {}, etag);
  }
  async function publish() {
    if (!form || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      await api<Version>(base + "/publish", {
        method: "POST",
        body: { confirmations },
        etag: form.etag,
        key: publishKey.current,
      });
      setDirtyGuard(false);
      navigate(instructionPath(org.id, id));
    } catch (e) {
      if (e instanceof ApiError && e.status === 412) setConflict(true);
      setError((e as Error).message);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  function updateStep(index: number, patch: Partial<DraftStep>) {
    setForm((f) =>
      f
        ? {
            ...f,
            steps: f.steps.map((s, i) =>
              i === index ? { ...s, ...patch, checked: false } : s,
            ),
          }
        : f,
    );
    setSavedNotice("");
    setValidationErrors([]);
  }
  function addStep(range?: ClipRange) {
    if (!form || form.steps.length >= 30) return;
    const position = Math.floor((video.current?.currentTime || 0) * 1000);
    const end =
      range?.end ??
      Math.min(position + 5000, form.source?.duration_ms ?? position + 5000);
    const start =
      range?.start ?? (position < end ? position : Math.max(0, end - 5000));
    const step: DraftStep = {
      id: null,
      ordinal: form.steps.length + 1,
      title: "",
      description: "",
      start_ms: start,
      end_ms: end,
      startInput: formatMsExact(start),
      endInput: formatMsExact(end),
      origin: "manual",
      checked: false,
      checked_by: null,
      evidence_segment_ids: [],
    };
    addedStep.current = form.steps.length;
    setForm({ ...form, steps: [...form.steps, step] });
    setSavedNotice("");
  }
  function move(index: number, delta: number) {
    if (!form) return;
    const steps = [...form.steps];
    [steps[index], steps[index + delta]] = [steps[index + delta], steps[index]];
    setForm({ ...form, steps });
    setSavedNotice("");
  }
  function jump(ms: number) {
    if (video.current) {
      video.current.pause();
      video.current.currentTime = ms / 1000;
      video.current.focus();
    }
  }
  if (loading) return <Loading label="Открываем рабочую версию…" />;
  if (!form)
    return (
      <>
        <button
          className="back-link"
          onClick={() => navigate(libraryPath(org.id))}
        >
          <Icon name="back" />
          Библиотека
        </button>
        <ErrorBanner error={error || "Черновик недоступен."}>
          <button className="text-button" onClick={() => void load()}>
            Повторить
          </button>
        </ErrorBanner>
      </>
    );
  const editable = form.editable && !conflict;
  const locked = busy || saving || pipelineBusy || uploading;
  const inReview = form.workflow === "in_review" && form.is_open;
  const checked = form.steps.filter((s) => s.checked).length;
  const recipe = recipeFromInputs(form);
  const reviewLabels = recipe ? recipeConfirmationLabels : confirmationLabels;
  const nextTask = editable ? nextReviewTask(form) : null;
  return (
    <>
      <button
        className="back-link"
        onClick={() => navigate(libraryPath(org.id))}
      >
        <Icon name="back" size={17} />
        Книга рецептов
      </button>
      <div className="editor-header">
        <div>
          <div className="editor-kicker">
            <span className="eyebrow">
              {inReview
                ? "Проверка перед публикацией"
                : recipe
                  ? "Редактор рецепта"
                  : "Архивный материал"}
            </span>
            <Status status={!form.is_open ? "published" : form.workflow} />
          </div>
          <h1>{form.title || (recipe ? "Новый рецепт" : "Черновик")}</h1>
          <span className="save-state" role="status">
            {saving
              ? "Сохраняем…"
              : dirty
                ? "Есть несохранённые изменения"
                : savedNotice || "Все изменения сохранены"}
          </span>
        </div>
        <div className="button-row">
          {editable && (
            <>
              <button
                className="button secondary"
                onClick={() => void save()}
                disabled={locked || !dirty}
              >
                Сохранить
              </button>
              <button
                className="button primary"
                onClick={() => {
                  if (nextTask) {
                    const target = document.querySelector<HTMLElement>(
                      nextTask.target,
                    );
                    target?.focus({ preventScroll: true });
                    target?.scrollIntoView({ block: "center" });
                  } else {
                    void submitReview();
                  }
                }}
                disabled={
                  locked ||
                  (!recipe &&
                    (form.source?.status !== "ready" ||
                      form.steps.length === 0))
                }
              >
                {nextTask?.label || "На проверку"}
                <Icon name="arrow" size={16} />
              </button>
            </>
          )}
          {!form.is_open && (
            <button
              className="button primary"
              onClick={() => navigate(instructionPath(org.id, id))}
            >
              Открыть публикацию
              <Icon name="arrow" size={16} />
            </button>
          )}
        </div>
      </div>
      {error && (
        <ErrorBanner error={error}>
          {conflict && (
            <div className="button-row">
              <button
                className="button secondary"
                onClick={() => setShowLocal(true)}
              >
                Скопировать мой текст
              </button>
              <button
                className="text-button"
                onClick={() => {
                  if (
                    !dirty ||
                    window.confirm(
                      "Ваши несохранённые изменения будут заменены. Сначала скопируйте их при необходимости.",
                    )
                  )
                    void load();
                }}
              >
                Загрузить актуальную версию
              </button>
            </div>
          )}
        </ErrorBanner>
      )}
      {validationErrors.length > 0 && (
        <ErrorBanner error="Проверьте поля перед сохранением">
          <ul>
            {validationErrors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </ErrorBanner>
      )}
      {form.return_reason && (
        <div className="notice warning">
          <Icon name="info" />
          <div>
            <strong>Комментарий проверяющего</strong>
            <p>{form.return_reason}</p>
          </div>
        </div>
      )}
      {inReview && (
        <div className="notice review-note">
          <Icon name="shield" />
          <div>
            <strong>
              {reviewer
                ? "Сверьте каждый шаг с видео"
                : "Черновик ожидает проверки"}
            </strong>
            <p>
              На время проверки содержание заблокировано.
              {reviewer
                ? " Просмотрите фотографии и фрагменты видео, затем отметьте проверенные шаги."
                : " Чтобы внести изменения, отзовите запрос."}
            </p>
          </div>
        </div>
      )}
      <nav className="editor-jumps" aria-label="К разделу рецепта">
        <a href="#recipe-details">Карточка блюда</a>
        {recipe && editable && (
          <a href="#recipe-ingredients-heading">Ингредиенты</a>
        )}
        <a href="#recipe-video">Видео</a>
        <a href="#recipe-steps">Шаги ({form.steps.length})</a>
      </nav>
      <div className="editor-grid">
        <aside className="editor-source" id="recipe-video" tabIndex={-1}>
          {form.source && <Video source={form.source} videoRef={video} />}
          <div className="source-meta">
            <span>
              <Icon name="play" size={16} />
              Видео приготовления
            </span>
            <span>{form.source ? time(form.source.duration_ms) : ""}</span>
          </div>
          {form.source?.is_demo && (
            <p className="source-caption">Учебная видеозапись</p>
          )}
          {recipe &&
            editable &&
            form.source?.status === "ready" &&
            form.source.playback_url && (
              <StepMarker
                key={form.source.id}
                video={video}
                duration={form.source.duration_ms}
                disabled={locked || form.steps.length >= 30}
                onAdd={addStep}
              />
            )}
          {form.source?.status === "rejected" && editable && (
            <div className="notice warning">
              <div>
                <p>
                  Не удалось подготовить запись. Можно создать рецепт с другим
                  видео.
                </p>
                <button
                  className="text-button"
                  onClick={() => navigate(libraryPath(org.id) + "/new")}
                >
                  Создать рецепт с другим видео
                </button>
              </div>
            </div>
          )}
          {!form.source && editable && cameraOpen && (
            <CameraCapture
              userId={userId}
              orgId={org.id}
              base={base}
              language={form.language}
              prepare={prepareAi}
              onActive={setUploading}
              onComplete={() => {
                setCameraOpen(false);
                setRefreshSeed((v) => v + 1);
                void refreshSource().catch((e) => setError(e.message));
              }}
              onClose={() => setCameraOpen(false)}
            />
          )}
          {!form.source && editable && !cameraOpen && (
            <button
              className="button secondary"
              disabled={locked}
              onClick={() => setCameraOpen(true)}
            >
              <Icon name="eye" />
              Записать с камеры
            </button>
          )}
          {!form.source && editable && !cameraOpen && (
            <UploadCard
              orgId={org.id}
              base={base}
              language={form.language}
              onSending={setUploading}
              onComplete={() => {
                setRefreshSeed((value) => value + 1);
                void refreshSource().catch((e) => setError(e.message));
              }}
            />
          )}
          {form.source?.status === "ready" && (
            <details className="editor-tool">
              <summary>Составить шаги по видео с ИИ</summary>
              <VisualAnalysis
                orgId={org.id}
                base={base}
                working={form}
                dirty={dirty}
                disabled={locked || conflict}
                prepare={prepareAi}
                onApplied={accept}
                onBusy={setPipelineBusy}
                onConflict={(e) => {
                  if (e.status === 412) setConflict(true);
                  setError(e.message);
                }}
                onJump={jump}
              />
            </details>
          )}
          <details className="editor-tool" hidden={!form.source}>
            <summary>Обработка видео и расшифровка</summary>
            <ProcessingPanel
              orgId={org.id}
              base={base}
              working={form}
              dirty={dirty}
              disabled={locked || conflict}
              refreshSeed={refreshSeed}
              prepare={prepareAi}
              onApplied={accept}
              onBusy={setPipelineBusy}
              onRefresh={refreshSource}
              onConflict={(e) => {
                if (e.status === 412) setConflict(true);
                setError(e.message);
              }}
              onJump={jump}
            />
          </details>
        </aside>
        <section
          id="recipe-details"
          className="editor-content"
          aria-label={recipe ? "Содержание рецепта" : "Содержание материала"}
        >
          <fieldset className="metadata-fields" disabled={!editable || locked}>
            <label>
              {recipe ? "Название блюда" : "Название"}
              <input
                id="recipe-title"
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                minLength={3}
                maxLength={120}
              />
            </label>
            <label>
              {recipe ? "Описание блюда · необязательно" : "Результат работы"}
              <textarea
                id="recipe-purpose"
                rows={2}
                value={form.purpose}
                onChange={(e) => setForm({ ...form, purpose: e.target.value })}
                minLength={recipe ? 0 : 10}
                maxLength={600}
              />
            </label>
            <label className="language-field">
              Язык
              <select
                value={form.language}
                onChange={(e) =>
                  setForm({
                    ...form,
                    language: e.target.value as WorkingCopy["language"],
                  })
                }
              >
                <option value="ru">Русский</option>
                <option value="kk">Қазақша · экспериментально</option>
                <option value="mixed">Смешанный · экспериментально</option>
              </select>
            </label>
          </fieldset>
          {form.editable && recipe ? (
            <RecipeEditor
              key={form.instruction_id}
              ingredientsInput={form.ingredientsInput}
              servingsInput={form.servingsInput}
              disabled={!editable || locked}
              onChange={(patch) => {
                setForm((current) =>
                  current ? { ...current, ...patch } : current,
                );
                setSavedNotice("");
                setValidationErrors([]);
              }}
            />
          ) : recipe ? (
            <RecipeIngredients recipe={recipe} />
          ) : (
            editable && (
              <button
                className="button secondary legacy-to-recipe"
                disabled={locked}
                onClick={() =>
                  setForm({
                    ...form,
                    recipe: { ingredients: [], servings: null },
                  })
                }
              >
                Сделать из материала рецепт
              </button>
            )
          )}
          <div className="section-heading" id="recipe-steps">
            <h2>
              {recipe ? "Приготовление" : "Шаги"}{" "}
              <span>{form.steps.length}</span>
            </h2>
            {inReview && (
              <span className="small muted">
                Проверено {checked} из {form.steps.length}
              </span>
            )}
          </div>
          {form.steps.length === 0 && (
            <div className="empty-steps">
              <Icon name="book" size={28} />
              <p>
                {recipe
                  ? "Добавьте первый этап приготовления."
                  : "Разделите процесс на простые действия."}
              </p>
              <span>
                Можно описать шаг вручную или взять его из разбора видео.
              </span>
            </div>
          )}
          <div className="steps-list">
            {form.steps.map((step, index) => (
              <article
                className={`step-card ${step.checked ? "is-checked" : ""}`}
                key={step.id || `new-${index}`}
              >
                <div className="step-heading">
                  <span className="step-number">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <span className="step-origin">
                    {step.evidence_frame_ids?.length ? "По видео · " : ""}
                    {step.origin === "ai"
                      ? "Предложен ИИ · требует проверки"
                      : step.origin === "ai_edited"
                        ? "Предложен ИИ · отредактирован"
                        : "Добавлен вручную"}
                  </span>
                  <button
                    className="time-link"
                    onClick={() => jump(parseMs(step.startInput) || 0)}
                  >
                    <Icon name="play" size={12} />
                    {step.startInput}–{step.endInput}
                  </button>
                  {editable && (
                    <div className="step-controls">
                      <button
                        className="icon-button"
                        onClick={() => move(index, -1)}
                        disabled={index === 0 || locked}
                        aria-label={`Шаг ${index + 1} выше`}
                      >
                        <Icon name="up" size={17} />
                      </button>
                      <button
                        className="icon-button"
                        onClick={() => move(index, 1)}
                        disabled={index === form.steps.length - 1 || locked}
                        aria-label={`Шаг ${index + 1} ниже`}
                      >
                        <Icon name="down" size={17} />
                      </button>
                      <button
                        className="icon-button"
                        onClick={() => {
                          if (step.title || step.description)
                            setDeleteIndex(index);
                          else
                            setForm({
                              ...form,
                              steps: form.steps.filter((_, i) => i !== index),
                            });
                        }}
                        disabled={locked}
                        aria-label={`Удалить шаг ${index + 1}`}
                      >
                        <Icon name="trash" size={16} />
                      </button>
                    </div>
                  )}
                </div>
                {!!step.evidence_segment_ids?.length && (
                  <p className="step-evidence">
                    Основание: {step.evidence_segment_ids.length} фрагм.
                    расшифровки. Сверьте текст с исходным видео.
                  </p>
                )}
                {stepWarningMessages(step.warning_codes).map((message) => (
                  <p className="pipeline-notice" key={message}>
                    {message}
                  </p>
                ))}
                {editable ? (
                  <fieldset disabled={locked}>
                    <label>
                      <span className="sr-only">Название шага {index + 1}</span>
                      <input
                        id={`step-title-${index}`}
                        className="step-title-input"
                        placeholder={
                          recipe
                            ? "Например, замесите тесто"
                            : "Что нужно сделать?"
                        }
                        value={step.title}
                        onChange={(e) =>
                          updateStep(index, { title: e.target.value })
                        }
                        maxLength={120}
                      />
                    </label>
                    <label>
                      <span className="sr-only">Описание шага {index + 1}</span>
                      <textarea
                        id={`step-description-${index}`}
                        rows={3}
                        placeholder={
                          recipe
                            ? "Что сделать с ингредиентами и как понять, что шаг завершён?"
                            : "Опишите действие и важные условия"
                        }
                        value={step.description}
                        onChange={(e) =>
                          updateStep(index, { description: e.target.value })
                        }
                        maxLength={2000}
                      />
                    </label>
                    <div className="time-fields">
                      <label>
                        Начало
                        <input
                          aria-label={`Начало шага ${index + 1}`}
                          id={`step-start-${index}`}
                          value={step.startInput}
                          onChange={(e) =>
                            updateStep(index, { startInput: e.target.value })
                          }
                          placeholder="00:00.0"
                          inputMode="decimal"
                        />
                      </label>
                      <label>
                        Конец
                        <input
                          aria-label={`Конец шага ${index + 1}`}
                          id={`step-end-${index}`}
                          value={step.endInput}
                          onChange={(e) =>
                            updateStep(index, { endInput: e.target.value })
                          }
                          placeholder="00:05.0"
                          inputMode="decimal"
                        />
                      </label>
                      {form.source?.status === "ready" && (
                        <button
                          className="text-button current-time"
                          type="button"
                          onClick={() =>
                            updateStep(index, {
                              startInput: formatMsExact(
                                Math.floor(
                                  (video.current?.currentTime || 0) * 1000,
                                ),
                              ),
                            })
                          }
                        >
                          <Icon name="clock" size={15} />
                          Начало по плееру
                        </button>
                      )}
                    </div>
                  </fieldset>
                ) : (
                  <div className="step-reading">
                    <h3>{step.title}</h3>
                    <p>{step.description}</p>
                    {inReview && reviewer ? (
                      <label className="checkbox-label step-check">
                        <input
                          type="checkbox"
                          checked={step.checked}
                          disabled={locked || conflict}
                          onChange={(e) =>
                            void action("review/check-step", {
                              step_id: step.id,
                              checked: e.target.checked,
                            })
                          }
                        />
                        <span>
                          {step.checked
                            ? "Шаг проверен"
                            : "Шаг соответствует записи"}
                        </span>
                      </label>
                    ) : (
                      step.checked && (
                        <span className="checked-label">
                          <Icon name="check" size={15} />
                          Проверен мастером
                        </span>
                      )
                    )}
                  </div>
                )}
                <RecipeStepPhotos
                  step={step}
                  recipe={!!recipe}
                  orgId={org.id}
                  source={form.source}
                  start={parseMs(step.startInput)}
                  end={parseMs(step.endInput)}
                  onJump={jump}
                />
                <TeachingClip
                  source={form.source}
                  startMs={parseMs(step.startInput)}
                  endMs={parseMs(step.endInput)}
                  title={step.title || `Шаг ${index + 1}`}
                />
              </article>
            ))}
          </div>
          {editable && (
            <button
              className="add-step"
              onClick={() => addStep()}
              disabled={locked || form.steps.length >= 30}
            >
              <Icon name="plus" />
              Добавить шаг <span>{form.steps.length}/30</span>
            </button>
          )}
          {inReview && (
            <div className="review-panel">
              <h2>{reviewer ? "Перед публикацией" : "Ожидает проверки"}</h2>
              {reviewer ? (
                <>
                  <p>
                    {recipe
                      ? "Сверьте список продуктов, количества и порядок приготовления. После публикации участники кухни смогут открыть рецепт и готовить по нему."
                      : "Проверьте материал целиком. После публикации его смогут открыть участники."}
                  </p>
                  <div className="review-checklist">
                    {Object.entries(reviewLabels).map(([key, label]) => (
                      <label className="checkbox-label" key={key}>
                        <input
                          type="checkbox"
                          checked={!!confirmations[key]}
                          onChange={(e) =>
                            setConfirmations({
                              ...confirmations,
                              [key]: e.target.checked,
                            })
                          }
                          disabled={locked || conflict}
                        />
                        <span>{label}</span>
                      </label>
                    ))}
                  </div>
                  <div className="button-row">
                    <button
                      className="button primary"
                      onClick={() => void publish()}
                      disabled={
                        locked ||
                        conflict ||
                        checked !== form.steps.length ||
                        Object.keys(reviewLabels).some((k) => !confirmations[k])
                      }
                    >
                      {busy ? "Публикуем…" : "Опубликовать"}
                      <Icon name="check" size={17} />
                    </button>
                    <button
                      className="button secondary"
                      onClick={() => setReturnOpen(true)}
                      disabled={locked || conflict}
                    >
                      Вернуть автору
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <p>
                    Автор может отозвать запрос и продолжить редактирование.
                  </p>
                  <button
                    className="button secondary"
                    onClick={() => void action("withdraw-review")}
                    disabled={locked || conflict}
                  >
                    Отозвать с проверки
                  </button>
                </>
              )}
            </div>
          )}
        </section>
      </div>
      {form.can_delete_draft && (
        <DeleteRecipeDraft
          orgId={org.id}
          instructionId={id}
          title={form.title}
          etag={form.etag}
          disabled={locked || dirty || conflict}
          onDeleted={() => navigate(libraryPath(org.id))}
        />
      )}
      {deleteIndex !== null && (
        <Dialog title="Удалить шаг?" onClose={() => setDeleteIndex(null)}>
          <p>
            Шаг «{form.steps[deleteIndex]?.title || "Без названия"}» будет
            удалён из черновика.
          </p>
          <div className="button-row">
            <button
              className="button danger"
              onClick={() => {
                setForm({
                  ...form,
                  steps: form.steps.filter((_, i) => i !== deleteIndex),
                });
                setDeleteIndex(null);
              }}
            >
              Удалить шаг
            </button>
            <button
              className="button secondary"
              onClick={() => setDeleteIndex(null)}
            >
              Отмена
            </button>
          </div>
        </Dialog>
      )}
      {showLocal && (
        <Dialog
          title="Ваши несохранённые изменения"
          onClose={() => setShowLocal(false)}
        >
          <p>Скопируйте текст перед загрузкой актуальной версии.</p>
          <textarea
            className="local-copy"
            aria-label="Локальная копия черновика"
            readOnly
            rows={14}
            value={JSON.stringify(payload(form), null, 2)}
            onFocus={(e) => e.target.select()}
          />
        </Dialog>
      )}
      {returnOpen && (
        <Dialog
          title="Вернуть на доработку"
          onClose={() => setReturnOpen(false)}
        >
          <form
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              void action("return-to-draft", { reason });
            }}
            className="stack"
          >
            <label>
              Что нужно исправить?
              <textarea
                autoFocus
                rows={4}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                minLength={10}
                maxLength={2000}
                required
                disabled={busy}
              />
            </label>
            <span className="field-hint">
              От 10 символов. Автор увидит причину в редакторе.
            </span>
            <button
              className="button primary"
              disabled={busy || reason.trim().length < 10}
            >
              Вернуть на доработку
            </button>
          </form>
        </Dialog>
      )}
    </>
  );
}
