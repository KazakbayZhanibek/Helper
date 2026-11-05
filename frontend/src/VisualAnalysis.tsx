import { useEffect, useRef, useState } from "react";
import { api, ApiError, orgApi, requestKey } from "./api";
import { FrameEvidence } from "./CameraCapture";
import { Dialog, ErrorBanner } from "./components";
import { formatMsExact, parseMs } from "./format";
import { activeJob, pollDelay, stageName } from "./media";
import {
  analysisBudgetProblem,
  baselineCalls,
  formatUsd,
  parseUsdMicros,
  visualAnalysisCopy,
  visualApplyProblem,
  visualResultSummary,
  visualValidationFailures,
} from "./visual";
import { VisualProgress } from "./VisualProgress";
import { isTeachingProposal } from "./actionEvidence";
import { VisualTeachingStep } from "./VisualTeachingStep";
import { TeachingClip } from "./TeachingClip";
import {
  VisualActionEvidence,
  VisualReviewContext,
} from "./VisualActionEvidence";
import type {
  ProcessingJob,
  VisualCapabilities,
  VisualProposal,
  WorkingCopy,
} from "./types";

export function VisualAnalysis({
  orgId,
  base,
  working,
  dirty,
  disabled,
  prepare,
  onBusy,
  onApplied,
  onConflict,
  onJump,
}: {
  orgId: string;
  base: string;
  working: WorkingCopy;
  dirty: boolean;
  disabled: boolean;
  prepare: () => Promise<WorkingCopy | null>;
  onBusy: (busy: boolean) => void;
  onApplied: (working: WorkingCopy) => void;
  onConflict: (error: ApiError) => void;
  onJump: (ms: number) => void;
}) {
  const [capabilities, setCapabilities] = useState<VisualCapabilities | null>(
    null,
  );
  const [consent, setConsent] = useState(false);
  const [scope, setScope] = useState<"full" | "window">("full");
  const [maxCalls, setMaxCalls] = useState("30");
  const [maxCost, setMaxCost] = useState("1.00");
  useEffect(() => setConsent(false), [capabilities?.provider, scope]);
  useEffect(() => {
    if (capabilities?.budget) {
      setMaxCalls(String(capabilities.budget.default_max_calls));
      setMaxCost(
        String(
          (capabilities.budget.default_max_cost_microusd || 1_000_000) /
            1_000_000,
        ),
      );
    }
  }, [
    capabilities?.provider,
    capabilities?.budget?.default_max_calls,
    capabilities?.budget?.default_max_cost_microusd,
  ]);
  const [start, setStart] = useState("00:00.0");
  const [end, setEnd] = useState(
    formatMsExact(Math.min(working.source?.duration_ms || 30_000, 30_000)),
  );
  const [proposals, setProposals] = useState<VisualProposal[]>([]);
  const [preview, setPreview] = useState<VisualProposal | null>(null);
  const [replace, setReplace] = useState(false);
  const [applyMode, setApplyMode] = useState<"append" | "replace">("append");
  const [job, setJob] = useState<ProcessingJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const [capabilityError, setCapabilityError] = useState("");
  const [tick, setTick] = useState(0);
  const runningRef = useRef(false);
  const requestKeys = useRef(new Map<string, string>());
  const org = orgApi(orgId);
  const sourceId = working.source?.id;
  useEffect(() => {
    setProposals([]);
    setPreview(null);
    setJob(null);
    setError("");
    setPollError("");
    setConsent(false);
    setScope("full");
    requestKeys.current.clear();
  }, [base, sourceId]);
  useEffect(() => {
    setStart("00:00.0");
    setEnd(
      formatMsExact(Math.min(working.source?.duration_ms || 30_000, 30_000)),
    );
  }, [sourceId, working.source?.duration_ms]);
  useEffect(() => {
    const controller = new AbortController();
    api<VisualCapabilities>(org + "/visual-capabilities", {
      signal: controller.signal,
    })
      .then((result) => {
        setCapabilities(result);
        setCapabilityError("");
      })
      .catch((e) => {
        if (e.name !== "AbortError") setCapabilityError(e.message);
      });
    return () => controller.abort();
  }, [org, tick]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>,
      cancelled = false,
      failures = 0;
    const began = Date.now();
    async function poll() {
      try {
        const results = await api<{ items: ProcessingJob[] }>(base + "/jobs", {
          signal: controller.signal,
        });
        const current = results.items.find(
          (item) => item.kind === "analyze_video",
        );
        if (cancelled) return;
        setJob(current || null);
        const result = await api<{ items: VisualProposal[] }>(
          base + "/visual-proposals",
          { signal: controller.signal },
        );
        if (cancelled) return;
        setProposals(result.items);
        setPollError("");
        failures = 0;
        if (!current || !activeJob(current.state)) return;
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setPollError(
          `Не удалось обновить результат визуального анализа. ${(e as Error).message}`,
        );
        failures += 1;
      }
      if (!cancelled)
        timer = setTimeout(
          poll,
          pollDelay(began, Date.now(), document.hidden, failures),
        );
    }
    void poll();
    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [base, sourceId, tick]);
  function key(action: string) {
    if (!requestKeys.current.has(action))
      requestKeys.current.set(action, requestKey());
    return requestKeys.current.get(action)!;
  }
  async function perform(action: string, operation: () => Promise<void>) {
    if (runningRef.current) return;
    runningRef.current = true;
    setBusy(true);
    onBusy(true);
    setError("");
    try {
      await operation();
      requestKeys.current.delete(action);
      setTick((v) => v + 1);
    } catch (e) {
      if (e instanceof ApiError && e.status === 412) onConflict(e);
      setError((e as Error).message);
    } finally {
      runningRef.current = false;
      setBusy(false);
      onBusy(false);
    }
  }
  async function generate() {
    const duration = working.source?.duration_ms || 0,
      startMs = scope === "full" ? 0 : parseMs(start),
      endMs = scope === "full" ? duration : parseMs(end);
    if (
      !Number.isFinite(startMs) ||
      !Number.isFinite(endMs) ||
      startMs < 0 ||
      endMs <= startMs ||
      endMs > duration ||
      (scope === "window" && endMs - startMs > 60_000)
    ) {
      setError(
        "Укажите фрагмент до 60 секунд внутри видеозаписи. Формат времени: 00:00.0.",
      );
      return;
    }
    if (budgetProblem) {
      setError(budgetProblem);
      return;
    }
    if (!consent) return;
    await perform("generate", async () => {
      const current = await prepare();
      if (!current) return;
      await api(base + "/visual-proposals", {
        method: "POST",
        body: {
          consent_images: true,
          scope,
          ...(scope === "window" ? { start_ms: startMs, end_ms: endMs } : {}),
          max_ai_calls: Number(maxCalls),
          ...(capabilities?.budget?.mode === "usd"
            ? { max_cost_microusd: parseUsdMicros(maxCost) }
            : {}),
        },
        etag: current.etag,
        key: key("generate"),
      });
    });
  }
  const latest = proposals[0];
  const jobRunning = !!job && activeJob(job.state);
  const locked = disabled || busy;
  const minimumCalls = baselineCalls(
    scope === "full"
      ? working.source?.duration_ms || 0
      : parseMs(end) - parseMs(start),
    scope,
  );
  const budgetProblem = analysisBudgetProblem(
    minimumCalls,
    Number(maxCalls),
    parseUsdMicros(maxCost),
    capabilities?.budget,
  );
  const previewProblem = preview
    ? visualApplyProblem(preview, working.revision, dirty)
    : "";
  const invalidPreview = !preview || !!previewProblem;
  const latestSummary = latest ? visualResultSummary(latest) : null;
  const previewSummary = preview ? visualResultSummary(preview) : null;
  function openProposal(proposal: VisualProposal) {
    setPreview(proposal);
    setReplace(false);
    setError("");
    setApplyMode(working.steps.length ? "append" : "replace");
  }
  function jumpToEvidence(ms: number) {
    if (busy) return;
    setPreview(null);
    // The recording sits behind the modal; close it before focusing the video.
    requestAnimationFrame(() => onJump(ms));
  }
  const planCopy = visualAnalysisCopy(
    capabilities?.automatic_refinements,
    minimumCalls,
    Number(maxCalls),
  );
  return (
    <section
      className="pipeline-panel visual-analysis stack"
      aria-label="Создание инструкции по видео"
    >
      <div>
        <span className="eyebrow">Видео → инструкция</span>
        <h3>Создать инструкцию по видео</h3>
      </div>
      <p>
        Получите черновик для повторения работы: что сделать, как выполнить
        движение и что проверить по записи. {planCopy.description}
      </p>
      <p className="field-hint">
        Это анализ отдельных кадров, а не непрерывного движения. Быстрые
        действия между кадрами могут быть пропущены. Таймкоды потребуется
        уточнить по видео.
      </p>
      {working.editable && (
        <>
          <fieldset className="source-mode" disabled={locked || jobRunning}>
            <legend>Что разобрать</legend>
            <label className="checkbox-label">
              <input
                type="radio"
                name="visual-scope"
                checked={scope === "full"}
                onChange={() => {
                  setScope("full");
                  requestKeys.current.delete("generate");
                }}
              />
              <span>
                Всю запись · {formatMsExact(working.source?.duration_ms || 0)}
              </span>
            </label>
            <label className="checkbox-label">
              <input
                type="radio"
                name="visual-scope"
                checked={scope === "window"}
                onChange={() => {
                  setScope("window");
                  requestKeys.current.delete("generate");
                }}
              />
              <span>Выбранный фрагмент до 60 секунд</span>
            </label>
          </fieldset>
          {scope === "window" && (
            <fieldset className="time-fields" disabled={locked || jobRunning}>
              <label>
                Начало фрагмента
                <input
                  value={start}
                  onChange={(e) => {
                    setStart(e.target.value);
                    requestKeys.current.delete("generate");
                  }}
                  inputMode="decimal"
                  aria-label="Начало визуального анализа"
                />
              </label>
              <label>
                Конец фрагмента
                <input
                  value={end}
                  onChange={(e) => {
                    setEnd(e.target.value);
                    requestKeys.current.delete("generate");
                  }}
                  inputMode="decimal"
                  aria-label="Конец визуального анализа"
                />
              </label>
            </fieldset>
          )}
          <div className="analysis-budget-plan">
            <strong>Лимит этого анализа</strong>
            <fieldset className="time-fields" disabled={locked || jobRunning}>
              <label>
                Не больше обращений к ИИ
                <input
                  type="number"
                  min="1"
                  max={capabilities?.budget?.max_calls || 60}
                  step="1"
                  value={maxCalls}
                  onChange={(e) => {
                    setMaxCalls(e.target.value);
                    requestKeys.current.delete("generate");
                  }}
                />
              </label>
              {capabilities?.budget?.mode === "usd" && (
                <label>
                  Не больше расходов, USD
                  <input
                    type="text"
                    inputMode="decimal"
                    value={maxCost}
                    onChange={(e) => {
                      setMaxCost(e.target.value);
                      requestKeys.current.delete("generate");
                    }}
                  />
                </label>
              )}
            </fieldset>
            <p className="field-hint">
              Первый просмотр: {minimumCalls} обращений
              {scope === "full" ? " · по одному на каждые 30 секунд" : ""}.
              {planCopy.extraCalls && <> {planCopy.extraCalls}</>}
            </p>
            {capabilities?.budget?.mode === "usd" &&
              capabilities.budget.call_upper_bound_microusd !== null && (
                <p className="field-hint">
                  Резерв на первый просмотр: до{" "}
                  {formatUsd(
                    minimumCalls *
                      capabilities.budget.call_upper_bound_microusd,
                  )}
                  . Это верхняя оценка, фактический расход может быть меньше.
                  Дополнительно действуют общие лимиты сервера.
                </p>
              )}
            {capabilities?.budget?.mode === "subscription_calls" && (
              <p className="field-hint">
                Тестовая подписка: ограничиваем число обращений. Денежная
                стоимость API здесь не рассчитывается.
              </p>
            )}
            {capabilities?.budget?.mode === "free_calls" && (
              <p className="field-hint">
                Бесплатный режим OpenRouter: число обращений ограничено. При
                недоступности подходящей модели анализ остановится; переход на
                платную модель отключён.
              </p>
            )}
            {budgetProblem && (
              <p className="pipeline-notice" role="status">
                {budgetProblem}
              </p>
            )}
          </div>
          {capabilities?.configured && (
            <p className="field-hint">
              Провайдер анализа:{" "}
              {capabilities.provider === "nvidia"
                ? "NVIDIA"
                : capabilities.provider === "openrouter"
                  ? `OpenRouter · ${capabilities.model}`
                  : capabilities.provider === "codex"
                    ? "Codex · подписка ChatGPT"
                    : capabilities.provider === "openai"
                      ? "Сервер с поддержкой API OpenAI"
                      : "Настроенный облачный ИИ"}
            </p>
          )}
          {capabilities?.provider === "codex" && (
            <p className="field-hint">
              Тестовый режим: кадры отправляются в OpenAI и расходуют лимиты
              подписки ChatGPT. Каждый фрагмент может занимать до 3 минут.
            </p>
          )}
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={consent}
              disabled={locked || jobRunning || !capabilities?.available}
              onChange={(e) => setConsent(e.target.checked)}
            />
            <span>
              Разрешаю отправить кадры{" "}
              {scope === "full" ? "этой записи" : "этого фрагмента"}{" "}
              {capabilities?.provider === "nvidia"
                ? "в NVIDIA"
                : capabilities?.provider === "openrouter"
                  ? "в OpenRouter и выбранному им провайдеру модели"
                  : capabilities?.provider === "codex"
                    ? "в OpenAI через подписку ChatGPT"
                    : "настроенному облачному ИИ"}{" "}
              для составления черновика инструкции.
            </span>
          </label>
          {capabilities && !capabilities.available && (
            <p className="pipeline-notice">
              Визуальный ИИ пока не подключён. {capabilities.reason} Видео можно
              смотреть и размечать вручную.
            </p>
          )}
          <button
            className="button primary"
            disabled={
              locked ||
              jobRunning ||
              !consent ||
              !capabilities?.available ||
              !!budgetProblem ||
              working.source?.status !== "ready"
            }
            onClick={() => void generate()}
          >
            {scope === "full"
              ? "Создать инструкцию по всей записи"
              : "Создать шаги по фрагменту"}
          </button>
          {!capabilities?.available && (
            <button
              className="text-button"
              disabled={busy}
              onClick={() => setTick((v) => v + 1)}
            >
              Проверить подключение визуального ИИ
            </button>
          )}
        </>
      )}
      {job && (
        <div className="job-status">
          <div role="status">
            <strong>Визуальный анализ</strong>
            <span>
              {job.cancel_requested && activeJob(job.state)
                ? "Останавливаем…"
                : job.state === "done" &&
                    visualValidationFailures(job.result.analysis)
                  ? "Ответ ИИ не прошёл проверку для части фрагментов"
                  : job.state === "done" &&
                      job.result.analysis &&
                      !job.result.analysis.coverage_complete
                    ? "Сохранён частичный результат"
                    : stageName(activeJob(job.state) ? job.stage : job.state)}
            </span>
          </div>
          {job.result.analysis && (
            <VisualProgress analysis={job.result.analysis} onJump={onJump} />
          )}
          {job.error_summary && <p>{job.error_summary}</p>}
          {job.error_code && <small>Код: {job.error_code}</small>}
          {working.editable && jobRunning && !job.cancel_requested && (
            <button
              className="text-button"
              disabled={locked}
              onClick={() =>
                void perform("cancel", async () => {
                  await api(`${org}/jobs/${job.id}/cancel`, {
                    method: "POST",
                    body: {},
                  });
                })
              }
            >
              Остановить анализ
            </button>
          )}
          {working.editable && job.retry_allowed && (
            <button
              className="text-button"
              disabled={locked}
              onClick={() =>
                void perform("retry", async () => {
                  await api(`${org}/jobs/${job.id}/retry`, {
                    method: "POST",
                    body: {},
                    key: key("retry"),
                  });
                })
              }
            >
              Повторить анализ
            </button>
          )}
        </div>
      )}
      {error && !preview && <ErrorBanner error={error} />}
      {(pollError || capabilityError) && (
        <ErrorBanner
          error={[capabilityError, pollError].filter(Boolean).join(" ")}
        >
          <button className="text-button" onClick={() => setTick((v) => v + 1)}>
            Обновить
          </button>
        </ErrorBanner>
      )}
      {latest && (
        <div className="proposal-ready">
          <strong>{latestSummary?.title}</strong>
          {latestSummary?.message && <p>{latestSummary.message}</p>}
          <p>
            {latest.steps.length}{" "}
            {isTeachingProposal(latest) ? "шагов" : "действий"} ·{" "}
            {latest.applied_at
              ? "применено к рабочей версии"
              : "требуется проверка человеком"}
          </p>
          {latest.analysis && (
            <VisualProgress analysis={latest.analysis} onJump={onJump} />
          )}
          <button
            className="button secondary"
            onClick={() => openProposal(latest)}
          >
            {isTeachingProposal(latest) && latest.steps.length > 0
              ? "Проверить шаги инструкции"
              : "Посмотреть действия и кадры"}
          </button>
        </div>
      )}
      {proposals.length > 1 && (
        <details>
          <summary>
            Предыдущие результаты анализа · {proposals.length - 1}
          </summary>
          <div className="stack">
            {proposals.slice(1).map((proposal) => (
              <div className="proposal-ready" key={proposal.id}>
                <p>
                  {new Date(proposal.created_at).toLocaleString("ru-RU")} ·{" "}
                  {proposal.steps.length} действий
                  {proposal.applied_at ? " · применено" : " · не применено"}
                </p>
                <button
                  className="text-button"
                  onClick={() => openProposal(proposal)}
                >
                  Посмотреть результат
                </button>
              </div>
            ))}
          </div>
        </details>
      )}
      {preview && (
        <Dialog
          className="teaching-dialog"
          title={
            !preview.steps.length
              ? previewSummary?.title || "Результат анализа видео"
              : isTeachingProposal(preview)
                ? "Черновик инструкции по видео"
                : "Действия, замеченные на видео"
          }
          onClose={() => {
            if (!busy) setPreview(null);
          }}
        >
          <div className="proposal-content stack">
            {previewSummary?.message && (
              <p className="pipeline-notice">{previewSummary.message}</p>
            )}
            {isTeachingProposal(preview) ? (
              <>
                {preview.steps.length > 0 && (
                  <p>
                    Процесс по этапам: прочитайте пояснение и посмотрите
                    фрагмент рядом. Моментов для уточнения:{" "}
                    {preview.gaps?.length || 0}.
                  </p>
                )}
                <details className="analysis-unprocessed">
                  <summary>Ограничения анализа и расход</summary>
                  <div className="stack">
                    <p>
                      {preview.analysis?.scope === "full"
                        ? "Предложение составлено по последовательному просмотру записи."
                        : "Предложение относится к выбранному фрагменту."}{" "}
                      Сверьте каждое действие с записью и уточните время.
                      Недостающие сведения уточните у мастера.
                    </p>
                    {preview.analysis && (
                      <VisualProgress
                        analysis={preview.analysis}
                        onJump={jumpToEvidence}
                      />
                    )}
                    {preview.warnings.map((warning, i) => (
                      <p className="pipeline-notice" key={i}>
                        {warning.message}
                      </p>
                    ))}
                  </div>
                </details>
              </>
            ) : (
              <>
                {preview.schema_version === "3" && (
                  <p className="pipeline-notice">
                    Экспериментальный разбор. Проверьте наблюдения по записи.
                  </p>
                )}
                <p>
                  {preview.analysis?.scope === "full"
                    ? "Предложение составлено по последовательному просмотру записи."
                    : "Предложение относится к выбранному фрагменту."}{" "}
                  Сверьте каждое действие с записью и уточните время.
                </p>
                {preview.analysis && (
                  <VisualProgress
                    analysis={preview.analysis}
                    onJump={
                      preview.schema_version === "3" ? jumpToEvidence : onJump
                    }
                  />
                )}
              </>
            )}
            {preview.applied_at ? (
              <p className="pipeline-notice">
                Это предложение уже применено. Здесь сохранён исходный ответ ИИ;
                текущие шаги можно редактировать в рабочей версии.
              </p>
            ) : !isTeachingProposal(preview) ? (
              <p>При применении отметки проверки сбрасываются.</p>
            ) : null}
            {error && <ErrorBanner error={error} />}
            {!isTeachingProposal(preview) &&
              preview.warnings.map((warning, i) => (
                <p className="pipeline-notice" key={i}>
                  {warning.message}
                </p>
              ))}
            {preview.steps.map((step, i) => (
              <article
                className={`proposal-step${isTeachingProposal(preview) ? " teaching-step" : ""}`}
                key={i}
              >
                {isTeachingProposal(preview) && (
                  <p className="eyebrow">Этап {i + 1}</p>
                )}
                <h3>
                  {!isTeachingProposal(preview) && `${i + 1}. `}
                  {step.title}
                </h3>
                <button
                  className="time-link"
                  onClick={() =>
                    preview.schema_version === "3"
                      ? jumpToEvidence(step.start_ms)
                      : onJump(step.start_ms)
                  }
                >
                  {formatMsExact(step.start_ms)}–{formatMsExact(step.end_ms)} ·
                  приблизительно
                </button>
                {isTeachingProposal(preview) ? (
                  <>
                    <VisualTeachingStep
                      proposal={preview}
                      step={step}
                      onJump={jumpToEvidence}
                    />
                    <TeachingClip
                      source={preview.source || null}
                      startMs={step.start_ms}
                      endMs={step.end_ms}
                      title={step.title}
                    />
                  </>
                ) : (
                  <p>{step.description}</p>
                )}
                {!isTeachingProposal(preview) &&
                preview.schema_version === "3" ? (
                  <VisualActionEvidence
                    proposal={preview}
                    step={step}
                    onJump={jumpToEvidence}
                  />
                ) : !isTeachingProposal(preview) ? (
                  <FrameEvidence
                    frames={preview.frames.filter((frame) =>
                      step.evidence_frame_ids.includes(frame.id),
                    )}
                  />
                ) : null}
              </article>
            ))}
            {preview.schema_version === "3" && (
              <details>
                <summary>
                  Проверка неясных моментов · {preview.gaps?.length || 0}
                </summary>
                <VisualReviewContext
                  proposal={preview}
                  onJump={jumpToEvidence}
                />
              </details>
            )}
            {previewProblem && !preview.applied_at && (
              <ErrorBanner error={previewProblem} />
            )}
            {working.editable && !preview.applied_at && (
              <>
                {isTeachingProposal(preview) && (
                  <p>При применении отметки проверки сбрасываются.</p>
                )}
                <fieldset
                  className="source-mode"
                  disabled={locked || invalidPreview}
                >
                  <legend>Как применить предложение</legend>
                  <label className="checkbox-label">
                    <input
                      type="radio"
                      name="visual-apply-mode"
                      checked={applyMode === "append"}
                      onChange={() => {
                        setApplyMode("append");
                        setReplace(false);
                      }}
                    />
                    <span>
                      Добавить после текущих шагов ({working.steps.length} +{" "}
                      {preview.steps.length})
                    </span>
                  </label>
                  <label className="checkbox-label">
                    <input
                      type="radio"
                      name="visual-apply-mode"
                      checked={applyMode === "replace"}
                      onChange={() => {
                        setApplyMode("replace");
                        setReplace(false);
                      }}
                    />
                    <span>
                      Заменить все текущие {working.steps.length} шагов
                    </span>
                  </label>
                </fieldset>
                {applyMode === "append" &&
                  working.steps.length + preview.steps.length > 30 && (
                    <ErrorBanner error="Получится больше 30 шагов. Сократите текущие шаги или выберите замену." />
                  )}
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={replace}
                    onChange={(e) => setReplace(e.target.checked)}
                    disabled={locked || invalidPreview}
                  />
                  <span>
                    Я сверил предложение с кадрами и подтверждаю{" "}
                    {applyMode === "append"
                      ? "добавление новых шагов"
                      : "замену текущих шагов"}
                    .
                  </span>
                </label>
                <button
                  className="button primary"
                  disabled={
                    locked ||
                    invalidPreview ||
                    !replace ||
                    (applyMode === "append" &&
                      working.steps.length + preview.steps.length > 30)
                  }
                  onClick={() =>
                    void perform("apply:" + preview.id, async () => {
                      const updated = await api<WorkingCopy>(
                        `${base}/visual-proposals/${preview.id}/apply`,
                        {
                          method: "POST",
                          body: { confirm_replace: true, mode: applyMode },
                          etag: working.etag,
                          key: key("apply:" + preview.id + ":" + applyMode),
                        },
                      );
                      onApplied(updated);
                      setPreview(null);
                    })
                  }
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
              {preview.applied_at
                ? "Закрыть результат"
                : "Оставить текущие шаги"}
            </button>
          </div>
        </Dialog>
      )}
    </section>
  );
}
