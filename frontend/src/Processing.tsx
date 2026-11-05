import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, orgApi, requestKey } from "./api";
import { Dialog, ErrorBanner, Icon, time } from "./components";
import { activeJob, pollDelay, stageName } from "./media";
import type {
  AiCapabilities,
  DraftProposal,
  ProcessingJob,
  Transcript,
  WorkingCopy,
} from "./types";

type Props = {
  orgId: string;
  base: string;
  working: WorkingCopy;
  dirty: boolean;
  disabled: boolean;
  refreshSeed: number;
  prepare: () => Promise<WorkingCopy | null>;
  onApplied: (working: WorkingCopy) => void;
  onBusy: (busy: boolean) => void;
  onRefresh: () => Promise<void>;
  onConflict: (error: ApiError) => void;
  onJump: (ms: number) => void;
};
export function ProcessingPanel({
  orgId,
  base,
  working,
  dirty,
  disabled,
  refreshSeed,
  prepare,
  onApplied,
  onBusy,
  onRefresh,
  onConflict,
  onJump,
}: Props) {
  const [jobs, setJobs] = useState<ProcessingJob[]>([]);
  const [capabilities, setCapabilities] = useState<AiCapabilities | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [proposals, setProposals] = useState<DraftProposal[]>([]);
  const [preview, setPreview] = useState<DraftProposal | null>(null);
  const [replace, setReplace] = useState(false);
  const [showTranscript, setShowTranscript] = useState(false);
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState("");
  const acting = useRef(false);
  const refreshRef = useRef(onRefresh);
  refreshRef.current = onRefresh;
  const sourceId = working.source?.id;
  const org = orgApi(orgId);
  const running = jobs.some((job) => activeJob(job.state));
  const latest = proposals.find((proposal) => !proposal.applied_at);
  const keys = useRef(new Map<string, string>());
  function actionKey(action: string) {
    if (!keys.current.has(action)) keys.current.set(action, requestKey());
    return keys.current.get(action)!;
  }
  const fetchArtifacts = useCallback(
    async (signal: AbortSignal) => {
      const result = await api<{ items: DraftProposal[] }>(
        base + "/draft-proposals",
        { signal },
      );
      setProposals(result.items);
      if (sourceId) {
        try {
          setTranscript(
            await api<Transcript>(`${org}/assets/${sourceId}/transcript`, {
              signal,
            }),
          );
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) setTranscript(null);
          else throw e;
        }
      } else setTranscript(null);
    },
    [base, org, sourceId],
  );
  useEffect(() => {
    const controller = new AbortController();
    api<AiCapabilities>(org + "/ai-capabilities", { signal: controller.signal })
      .then(setCapabilities)
      .catch((e) => {
        if (e.name !== "AbortError") setPollError(e.message);
      });
    return () => controller.abort();
  }, [org, tick]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    let previous = "";
    let stopped = false;
    let finished = false;
    let polling = false;
    const startedAt = Date.now();
    async function poll() {
      if (stopped || polling) return;
      polling = true;
      try {
        const result = await api<{ items: ProcessingJob[] }>(base + "/jobs", {
          signal: controller.signal,
        });
        if (stopped) return;
        setJobs(result.items);
        setPollError("");
        failures = 0;
        const signature = JSON.stringify(
          result.items.map((job) => [job.id, job.state, job.stage]),
        );
        if (signature !== previous) {
          await fetchArtifacts(controller.signal);
          await refreshRef.current();
          previous = signature;
        }
        if (!result.items.some((job) => activeJob(job.state))) {
          finished = true;
          return;
        }
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setPollError(
          `Не удалось обновить состояние. Задание на сервере продолжает работать. ${(e as Error).message}`,
        );
        failures += 1;
      } finally {
        polling = false;
      }
      if (!stopped)
        timer = setTimeout(
          poll,
          pollDelay(startedAt, Date.now(), document.hidden, failures),
        );
    }
    void poll();
    const visible = () => {
      if (!document.hidden && !finished) {
        clearTimeout(timer);
        if (!stopped) timer = setTimeout(poll, 0);
      }
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [base, fetchArtifacts, refreshSeed, tick]);
  async function perform(action: string, operation: () => Promise<void>) {
    if (acting.current) return;
    acting.current = true;
    setBusy(true);
    onBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
      keys.current.delete(action);
      setTick((value) => value + 1);
    } catch (e) {
      if (
        e instanceof ApiError &&
        (e.status === 412 || e.code === "STALE_PROPOSAL")
      )
        onConflict(e);
      setError((e as Error).message);
    } finally {
      acting.current = false;
      setBusy(false);
      onBusy(false);
    }
  }
  async function transcribe() {
    await perform("transcribe", async () => {
      const saved = await prepare();
      if (!saved) return;
      await api(base + "/transcribe", {
        method: "POST",
        body: {},
        etag: saved.etag,
        key: actionKey("transcribe"),
      });
    });
  }
  async function generate() {
    if (!transcript) return;
    await perform("generate", async () => {
      const saved = await prepare();
      if (!saved) return;
      await api(base + "/draft-proposals", {
        method: "POST",
        body: {
          transcript_id: transcript.id,
          transcript_revision: transcript.revision,
          base_working_revision: saved.revision,
        },
        etag: saved.etag,
        key: actionKey("generate"),
      });
    });
  }
  async function apply() {
    if (
      !preview ||
      !replace ||
      dirty ||
      preview.stale ||
      preview.base_working_revision !== working.revision
    )
      return;
    await perform("apply:" + preview.id, async () => {
      const updated = await api<WorkingCopy>(
        `${base}/draft-proposals/${preview.id}/apply`,
        {
          method: "POST",
          body: { confirm_replace: true },
          etag: working.etag,
          key: actionKey("apply:" + preview.id),
        },
      );
      onApplied(updated);
      setPreview(null);
      setReplace(false);
      setNotice(
        "Шаги добавлены в рабочую версию. Проверьте их по записи перед отправкой мастеру.",
      );
    });
  }
  const locked = disabled || busy;
  const ready = working.source?.status === "ready";
  const canEdit = working.editable;
  const noAudio =
    ready && (working.source?.has_audio === false || working.source?.is_demo);
  return (
    <section
      className="pipeline-panel stack"
      aria-label="Обработка видео и помощь ИИ"
    >
      <div>
        <span className="eyebrow">Видео и шаги</span>
        <h3>От записи к инструкции</h3>
      </div>
      {jobs
        .filter((job) =>
          ["prepare_media", "transcribe", "draft_steps"].includes(job.kind),
        )
        .slice(0, 4)
        .map((job) => (
          <div
            className={`job-status ${job.state === "failed" ? "job-failed" : ""}`}
            key={job.id}
          >
            <div role="status">
              <strong>
                {job.kind === "transcribe"
                  ? "Расшифровка"
                  : job.kind === "draft_steps"
                    ? "Черновик ИИ"
                    : "Подготовка видео"}
              </strong>
              <span>
                {job.cancel_requested && activeJob(job.state)
                  ? "Останавливаем…"
                  : stageName(
                      activeJob(job.state) ? job.stage || job.state : job.state,
                    )}
              </span>
            </div>
            {activeJob(job.state) && (
              <div className="job-indicator" aria-hidden="true" />
            )}
            {job.error_summary && <p>{job.error_summary}</p>}
            {job.error_code && <small>Код: {job.error_code}</small>}
            <div className="button-row">
              {canEdit && job.retry_allowed && (
                <button
                  className="text-button"
                  disabled={locked || running}
                  onClick={() =>
                    void perform("retry:" + job.id, async () => {
                      await api(`${org}/jobs/${job.id}/retry`, {
                        method: "POST",
                        body: {},
                        key: actionKey("retry:" + job.id),
                      });
                    })
                  }
                >
                  Повторить обработку
                </button>
              )}
              {canEdit && activeJob(job.state) && !job.cancel_requested && (
                <button
                  className="text-button"
                  disabled={locked}
                  onClick={() =>
                    void perform("cancel:" + job.id, async () => {
                      await api(`${org}/jobs/${job.id}/cancel`, {
                        method: "POST",
                        body: {},
                      });
                    })
                  }
                >
                  Остановить
                </button>
              )}
            </div>
          </div>
        ))}
      {canEdit && ready && (
        <button
          className="text-button"
          onClick={() =>
            document.querySelector<HTMLButtonElement>(".add-step")?.focus()
          }
        >
          Добавить шаг вручную
        </button>
      )}
      {pollError && (
        <ErrorBanner error={pollError}>
          <button
            className="text-button"
            onClick={() => setTick((value) => value + 1)}
          >
            Обновить состояние
          </button>
        </ErrorBanner>
      )}
      {error && <ErrorBanner error={error} />}
      {notice && (
        <p className="pipeline-notice" role="status">
          {notice}
        </p>
      )}
      {!ready && (
        <p className="muted">
          {working.source
            ? "Готовим запись для просмотра. После передачи файла обработка продолжится, даже если закрыть страницу."
            : "Добавьте видео, чтобы распознать речь. Текст и шаги можно подготовить вручную."}
        </p>
      )}
      {noAudio && (
        <p className="pipeline-notice">
          В записи нет аудио для распознавания речи. Используйте распознавание
          действий по кадрам или добавьте шаги вручную.
        </p>
      )}
      {ready && !noAudio && (
        <>
          {working.language !== "ru" && (
            <p className="pipeline-notice">
              Распознавание казахского и смешанной речи экспериментальное.
              Особенно внимательно проверьте термины и таймкоды.
            </p>
          )}
          {capabilities && !capabilities.asr.available && (
            <p className="muted">
              Распознавание речи пока не подключено.{" "}
              {capabilities.asr.reason ||
                "Оператору нужно настроить локальную модель распознавания."}{" "}
              Можно разметить видео вручную.
            </p>
          )}
          {canEdit && (
            <button
              className="button secondary"
              disabled={locked || running || !capabilities?.asr.available}
              onClick={() => void transcribe()}
            >
              {transcript ? "Распознать речь заново" : "Распознать речь"}
            </button>
          )}
        </>
      )}
      {transcript && (
        <>
          <button
            className="text-button"
            onClick={() => setShowTranscript(!showTranscript)}
            aria-expanded={showTranscript}
          >
            {showTranscript ? "Скрыть расшифровку" : "Открыть расшифровку"} ·{" "}
            {transcript.segments.length} фрагм.
          </button>
          {showTranscript && (
            <div className="transcript-panel">
              <p className="field-hint">
                Автоматическая расшифровка, ревизия {transcript.revision}.
                Сверьте слова с записью.
              </p>
              {transcript.warnings?.map((warning, i) => (
                <p className="pipeline-notice" key={i}>
                  {typeof warning === "string" ? warning : warning.message}
                </p>
              ))}
              {transcript.segments.map((segment) => (
                <div className="transcript-segment" key={segment.id}>
                  <button
                    className="time-link"
                    onClick={() => onJump(segment.start_ms)}
                  >
                    <Icon name="play" size={12} />
                    {time(segment.start_ms)}–{time(segment.end_ms)}
                  </button>
                  <p>{segment.text}</p>
                </div>
              ))}
            </div>
          )}
          {canEdit && (
            <div className="ai-generation stack">
              <p>
                ИИ составит предложение по расшифровке. Он может пропустить
                действия или ошибиться — проверьте результат по видео.
              </p>
              {capabilities?.step_drafts.external && (
                <p className="pipeline-notice">
                  По нажатию кнопки текст расшифровки будет отправлен
                  настроенному облачному провайдеру. Видеозапись не
                  отправляется.
                </p>
              )}
              {capabilities && !capabilities.step_drafts.available && (
                <p className="muted">
                  Автоматическое составление шагов пока не подключено.{" "}
                  {capabilities.step_drafts.reason ||
                    "Оператору нужно настроить модель и ключ API на сервере."}{" "}
                  Ключ не вводится в браузере.
                </p>
              )}
              <button
                className="button primary"
                disabled={
                  locked || running || !capabilities?.step_drafts.available
                }
                onClick={() => void generate()}
              >
                Создать черновик шагов
              </button>
            </div>
          )}
        </>
      )}
      {capabilities &&
        ((!capabilities.asr.available && !noAudio) ||
          !capabilities.step_drafts.available) && (
          <button
            className="text-button"
            disabled={busy}
            onClick={() => setTick((value) => value + 1)}
          >
            Проверить подключение ИИ
          </button>
        )}
      {latest && (
        <div className="proposal-ready">
          <strong>Предложение ИИ готово</strong>
          <p>{latest.steps.length} шагов · отдельно от текущей инструкции</p>
          <button
            className="button secondary"
            disabled={busy}
            onClick={() => {
              setPreview(latest);
              setReplace(false);
            }}
          >
            Посмотреть предложение
          </button>
        </div>
      )}
      {preview && (
        <Dialog
          title="Предложение шагов от ИИ"
          onClose={() => {
            if (!busy) setPreview(null);
          }}
        >
          <div className="proposal-content stack">
            <p>
              Проверьте действия, порядок, термины и соответствие записи.
              Применение заменит текущие {working.steps.length} шагов и снимет
              отметки проверки.
            </p>
            {preview.warnings.map((warning, i) => (
              <div className="notice warning" key={i}>
                <p>{warning.message}</p>
              </div>
            ))}
            {preview.steps.map((step, i) => (
              <article className="proposal-step" key={i}>
                <h3>
                  {i + 1}. {step.title}
                </h3>
                <button
                  className="time-link"
                  onClick={() => onJump(step.start_ms)}
                >
                  {time(step.start_ms)}–{time(step.end_ms)}
                </button>
                <p>{step.description}</p>
                <details>
                  <summary>
                    Основание в расшифровке · {step.evidence_segment_ids.length}
                  </summary>
                  {step.evidence_segment_ids.map((segmentId) => {
                    const segment = transcript?.segments.find(
                      (s) => s.id === segmentId,
                    );
                    return (
                      <blockquote key={segmentId}>
                        {segment ? (
                          <>
                            <button
                              className="time-link"
                              onClick={() => onJump(segment.start_ms)}
                            >
                              {time(segment.start_ms)}
                            </button>{" "}
                            {segment.text}
                          </>
                        ) : (
                          "Фрагмент относится к предыдущей расшифровке. Создайте новое предложение."
                        )}
                      </blockquote>
                    );
                  })}
                </details>
              </article>
            ))}
            {(dirty ||
              preview.stale ||
              preview.base_working_revision !== working.revision) && (
              <ErrorBanner error="Инструкция или расшифровка изменилась после генерации. Сохраните правки и создайте новое предложение — текущие шаги не будут заменены." />
            )}
            {canEdit && (
              <>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={replace}
                    onChange={(e) => setReplace(e.target.checked)}
                    disabled={locked}
                  />
                  <span>
                    Я просмотрел предложение и хочу заменить им текущие шаги.
                  </span>
                </label>
                <button
                  className="button primary"
                  disabled={
                    locked ||
                    !replace ||
                    dirty ||
                    preview.stale ||
                    preview.base_working_revision !== working.revision
                  }
                  onClick={() => void apply()}
                >
                  Применить к рабочей версии
                </button>
              </>
            )}
            <button
              className="text-button"
              disabled={busy}
              onClick={() => setPreview(null)}
            >
              Оставить текущие шаги
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}
