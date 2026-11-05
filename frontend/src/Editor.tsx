import { CameraCapture } from "./CameraCapture";
import { VisualAnalysis } from "./VisualAnalysis";
import { SavedStepPhotos } from "./SavedStepPhotos";
import { TeachingClip } from "./TeachingClip";
import { ProcessingPanel } from "./Processing";
import { UploadCard } from "./Upload";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
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
import { payload, toDraft, type Draft, type DraftStep } from "./editorDraft";
import { stepWarningMessages } from "./visual";
function validation(form: Draft): string[] {
  const errors: string[] = [];
  if (form.title.trim().length < 3)
    errors.push("Название должно содержать не менее 3 символов.");
  if (form.purpose.trim().length < 10)
    errors.push("Опишите цель: не менее 10 символов.");
  form.steps.forEach((s, i) => {
    if (s.title.trim().length < 3)
      errors.push(`Шаг ${i + 1}: добавьте название от 3 символов.`);
    if (!s.description.trim()) errors.push(`Шаг ${i + 1}: опишите действие.`);
    const start = parseMs(s.startInput),
      end = parseMs(s.endInput);
    if (
      !Number.isFinite(start) ||
      !Number.isFinite(end) ||
      end <= start ||
      start < 0
    )
      errors.push(
        `Шаг ${i + 1}: укажите начало и конец в формате 00:00.0, конец позже начала.`,
      );
    else if (
      form.source?.status === "ready" &&
      form.source.duration_ms !== null &&
      end > form.source.duration_ms
    )
      errors.push(
        `Шаг ${i + 1}: конец выходит за длительность видео ${time(form.source.duration_ms)}.`,
      );
  });
  return errors;
}
const confirmationLabels = {
  order: "Порядок действий правильный",
  terminology: "Названия и термины понятны",
  completeness: "Пояснений достаточно для выполнения",
  relevance: "Инструкция актуальна",
  privacy: "В записи нет лишних чувствительных данных",
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
      if (explicit) setValidationErrors(errors);
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
          ? "Инструкция передана на проверку"
          : name === "return-to-draft"
            ? "Инструкция возвращена автору"
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
    const errors = validation(form);
    if (!form.steps.length) errors.push("Добавьте хотя бы один шаг.");
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
  function addStep() {
    if (!form) return;
    const start = Math.floor((video.current?.currentTime || 0) * 1000);
    const end = Math.min(
      start + 5000,
      form.source?.duration_ms || start + 5000,
    );
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
    setForm({ ...form, steps: [...form.steps, step] });
    setSavedNotice("");
    setTimeout(
      () => document.getElementById(`step-title-${form.steps.length}`)?.focus(),
      0,
    );
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
        <ErrorBanner error={error || "Инструкция недоступна."}>
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
  return (
    <>
      <button
        className="back-link"
        onClick={() => navigate(libraryPath(org.id))}
      >
        <Icon name="back" size={17} />
        Все инструкции
      </button>
      <div className="editor-header">
        <div>
          <div className="editor-kicker">
            <span className="eyebrow">
              {inReview ? "Проверка мастером" : "Редактор инструкции"}
            </span>
            <Status status={!form.is_open ? "published" : form.workflow} />
          </div>
          <h1>{form.title || "Новая инструкция"}</h1>
          <span className="save-state" role="status">
            {saving
              ? "Сохраняем…"
              : dirty
                ? "Есть несохранённые изменения"
                : savedNotice || `Сохранено · ревизия ${form.revision}`}
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
                onClick={() => void submitReview()}
                disabled={
                  locked ||
                  form.source?.status !== "ready" ||
                  form.steps.length === 0
                }
              >
                На проверку
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
                : "Инструкция ожидает проверки мастером"}
            </strong>
            <p>
              На время проверки содержание заблокировано.
              {reviewer
                ? " Отметьте шаги, затем подтвердите инструкцию целиком."
                : " Чтобы внести изменения, отзовите запрос."}
            </p>
          </div>
        </div>
      )}
      <div className="editor-grid">
        <aside className="editor-source">
          {!(cameraOpen && !form.source) && (
            <Video source={form.source} videoRef={video} />
          )}
          <div className="source-meta">
            <span>
              <Icon name="play" size={16} />
              Исходная запись
            </span>
            <span>
              {form.source ? time(form.source.duration_ms) : "Нет видео"}
            </span>
          </div>
          <p className="source-caption">
            {form.source?.is_demo
              ? "Учебная запись · синтетическая анимация без речи"
              : form.source
                ? "Ваша исходная запись · доступна участникам пространства"
                : "Видео пока не добавлено"}
          </p>
          {form.source?.status === "rejected" && editable && (
            <div className="notice warning">
              <div>
                <p>
                  Эта запись не подготовлена. Если файл не подходит, создайте
                  инструкцию с другим видео.
                </p>
                <button
                  className="text-button"
                  onClick={() => navigate(libraryPath(org.id) + "/new")}
                >
                  Создать инструкцию с другим видео
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
          <div className="editor-tip">
            <Icon name="info" />
            <p>
              Выбирайте короткие фрагменты: один шаг — одно действие. Время
              можно взять из проигрывателя.
            </p>
          </div>
          {form.source?.status === "ready" && (
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
          )}
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
        </aside>
        <section className="editor-content" aria-label="Содержание инструкции">
          <fieldset className="metadata-fields" disabled={!editable || locked}>
            <label>
              Название
              <input
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                minLength={3}
                maxLength={120}
              />
            </label>
            <label>
              Результат работы
              <textarea
                rows={2}
                value={form.purpose}
                onChange={(e) => setForm({ ...form, purpose: e.target.value })}
                minLength={10}
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
          <div className="section-heading">
            <h2>
              Шаги инструкции <span>{form.steps.length}</span>
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
              <p>Разделите процесс на простые действия.</p>
              <span>
                Начните с первого шага и укажите его фрагмент в видео.
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
                        placeholder="Что нужно сделать?"
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
                        rows={3}
                        placeholder="Опишите действие, детали и важные условия"
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
                          value={step.endInput}
                          onChange={(e) =>
                            updateStep(index, { endInput: e.target.value })
                          }
                          placeholder="00:05.0"
                          inputMode="decimal"
                        />
                      </label>
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
                        Взять текущее время
                      </button>
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
                <SavedStepPhotos step={step} orgId={org.id} onJump={jump} />
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
              onClick={addStep}
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
                    Подтвердите инструкцию целиком. После публикации её смогут
                    открыть коллеги.
                  </p>
                  <div className="review-checklist">
                    {Object.entries(confirmationLabels).map(([key, label]) => (
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
                        Object.keys(confirmationLabels).some(
                          (k) => !confirmations[k],
                        )
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
            aria-label="Локальная копия инструкции"
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
