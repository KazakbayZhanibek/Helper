import { useEffect, useRef, useState } from "react";
import { api, ApiError, orgApi, requestKey, uploadFrame } from "./api";
import {
  CameraCaptureController,
  CAMERA_ANALYZE_INTERVAL_MS,
  CAMERA_FRAME_INTERVAL_MS,
  CAMERA_MAX_FRAMES,
  cameraError,
  frameDimensions,
  type CaptureResult,
} from "./camera";
import { ErrorBanner, Icon, time } from "./components";
import { formatBytes, activeJob } from "./media";
import { UploadProgress, useVideoUpload } from "./Upload";
import { LiveFrameQueue, prepareLiveSession } from "./liveFrames";
import {
  loadRecovery,
  saveRecovery,
  removeRecovery,
  recoveryKey,
  RecoveryConflictError,
  type RecoveryRecording,
} from "./cameraRecovery";
import type {
  Language,
  ProcessingJob,
  VisualCapabilities,
  VisualFrame,
  VisualSession,
  WorkingCopy,
} from "./types";

export function FrameEvidence({
  frames,
  timeOrigin = "recording",
}: {
  frames: VisualFrame[];
  timeOrigin?: "recording" | "session";
}) {
  return (
    <div className="frame-evidence">
      {frames.map((frame) => (
        <figure key={frame.id}>
          <img
            src={frame.url}
            alt={`Кадр ${timeOrigin === "session" ? "наблюдения" : "записи"} ${time(frame.timestamp_ms)}`}
            loading="lazy"
          />
          <figcaption>
            {timeOrigin === "session" ? "Наблюдение · " : ""}
            {time(frame.timestamp_ms)}
          </figcaption>
        </figure>
      ))}
    </div>
  );
}
export function CameraCapture({
  userId,
  orgId,
  base,
  language,
  prepare,
  onActive,
  onComplete,
  onClose,
}: {
  userId: string;
  orgId: string;
  base: string;
  language: Language;
  prepare: () => Promise<WorkingCopy | null>;
  onActive: (active: boolean) => void;
  onComplete: () => void;
  onClose: () => void;
}) {
  const [capabilities, setCapabilities] = useState<VisualCapabilities | null>(
    null,
  );
  const [capabilityTick, setCapabilityTick] = useState(0);
  const [opening, setOpening] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [recording, setRecording] = useState(false);
  const [audio, setAudio] = useState(true);
  const [rights, setRights] = useState(false);
  const [consent, setConsent] = useState(false);
  useEffect(() => setConsent(false), [capabilities?.provider]);
  const [elapsed, setElapsed] = useState(0);
  const [bytes, setBytes] = useState(0);
  const [recorded, setRecorded] = useState<CaptureResult | null>(null);
  const [recordedUrl, setRecordedUrl] = useState("");
  const [session, setSession] = useState<VisualSession | null>(null);
  const [liveStatus, setLiveStatus] = useState("");
  const [error, setError] = useState("");
  const [liveError, setLiveError] = useState("");
  const [saved, setSaved] = useState(false);
  const [recovery, setRecovery] = useState<RecoveryRecording | null>(null);
  const [recoveryBusy, setRecoveryBusy] = useState(true);
  const [recoveryNotice, setRecoveryNotice] = useState("");
  const [keepLocal, setKeepLocal] = useState(true);
  const localKey = recoveryKey(userId, orgId, base);
  const localCopy = useRef<RecoveryRecording | null>(null);
  const upload = useVideoUpload(orgId);
  const video = useRef<HTMLVideoElement>(null);
  const capture = useRef<CameraCaptureController | null>(null);
  const recordingRef = useRef(false);
  const mounted = useRef(true);
  const sessionRef = useRef<VisualSession | null>(null);
  const frameAbort = useRef<AbortController | null>(null);
  const sessionOpening = useRef<AbortController | null>(null);
  const prepareRef = useRef(prepare);
  prepareRef.current = prepare;
  const org = orgApi(orgId);
  useEffect(() => {
    let active = true;
    setRecoveryBusy(true);
    loadRecovery(localKey)
      .then((copy) => {
        if (active) {
          localCopy.current = copy;
          setRecovery(copy);
        }
      })
      .catch(() => {
        if (active)
          setRecoveryNotice(
            "Не удалось проверить локальную копию. После записи скачайте видео, прежде чем закрывать страницу.",
          );
      })
      .finally(() => {
        if (active) setRecoveryBusy(false);
      });
    return () => {
      active = false;
    };
  }, [localKey]);
  async function preserveRecording(result: CaptureResult) {
    if (!keepLocal) return;
    setRecoveryBusy(true);
    setRecoveryNotice("Сохраняем локальную копию… Не закрывайте страницу.");
    try {
      localCopy.current = await saveRecovery(localKey, result);
      if (mounted.current)
        setRecoveryNotice(
          "Копия сохранена в этом браузере. Её можно восстановить в течение 24 часов, открыв камеру этой инструкции под тем же аккаунтом.",
        );
    } catch (error) {
      if (mounted.current)
        setRecoveryNotice(
          error instanceof RecoveryConflictError
            ? error.message
            : "Не удалось сохранить локальную копию: хранилище недоступно или заполнено. Скачайте запись перед закрытием страницы.",
        );
    } finally {
      if (mounted.current) setRecoveryBusy(false);
    }
  }
  async function discardCopy(): Promise<boolean> {
    const copy = localCopy.current;
    if (!copy) return true;
    setRecoveryBusy(true);
    try {
      await removeRecovery(localKey, copy.token);
      localCopy.current = null;
      setRecovery(null);
      setRecoveryNotice("");
      return true;
    } catch {
      setRecoveryNotice(
        "Не удалось удалить локальную копию. Повторите удаление или очистите данные этого сайта в браузере.",
      );
      return false;
    } finally {
      if (mounted.current) setRecoveryBusy(false);
    }
  }
  const cancelLive = async () => {
    sessionOpening.current?.abort();
    sessionOpening.current = null;
    frameAbort.current?.abort();
    frameAbort.current = null;
    const current = sessionRef.current;
    sessionRef.current = null;
    if (mounted.current)
      setLiveStatus("Предварительное наблюдение остановлено");
    if (current) {
      try {
        const closed = await api<VisualSession>(
          `${org}/visual-sessions/${current.id}/cancel`,
          { method: "POST", body: {} },
        );
        if (mounted.current)
          setSession((previous) =>
            previous?.id === current.id ? closed : previous,
          );
      } catch (e) {
        if (mounted.current && !recordingRef.current)
          setLiveError(
            `Камера остановлена. Не удалось подтвердить остановку анализа: ${(e as Error).message}`,
          );
      }
    }
  };
  const cancelRef = useRef(cancelLive);
  cancelRef.current = cancelLive;
  useEffect(() => {
    const controller = new AbortController();
    api<VisualCapabilities>(org + "/visual-capabilities", {
      signal: controller.signal,
    })
      .then(setCapabilities)
      .catch((e) => {
        if (e.name !== "AbortError") setLiveError(e.message);
      });
    return () => controller.abort();
  }, [org, capabilityTick]);
  useEffect(() => {
    onActive(
      opening ||
        previewing ||
        recording ||
        upload.sending ||
        recoveryBusy ||
        (!!recorded && !saved),
    );
    return () => onActive(false);
  }, [
    opening,
    previewing,
    recording,
    upload.sending,
    recorded,
    saved,
    recoveryBusy,
    onActive,
  ]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      recordingRef.current = false;
      capture.current?.dispose();
      void cancelRef.current();
    };
  }, []);
  useEffect(() => {
    if (!recorded) {
      setRecordedUrl("");
      return;
    }
    const url = URL.createObjectURL(recorded.file);
    setRecordedUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [recorded]);
  async function enableCamera() {
    if (opening || capture.current || recoveryBusy || recovery) return;
    setError("");
    setOpening(true);
    if (
      !navigator.mediaDevices?.getUserMedia ||
      typeof MediaRecorder === "undefined"
    ) {
      setError(
        "Запись камеры недоступна в этом браузере. Откройте приложение через localhost или HTTPS либо загрузите готовое видео.",
      );
      setOpening(false);
      return;
    }
    const controller = new CameraCaptureController({
      devices: navigator.mediaDevices,
      recorder: MediaRecorder,
      onProgress: (ms, size) => {
        if (mounted.current) {
          setElapsed(ms);
          setBytes(size);
        }
      },
      onError: (message) => {
        if (mounted.current) setError(message);
      },
      onPreviewStopped: () => {
        capture.current = null;
        if (video.current) video.current.srcObject = null;
        if (mounted.current) setPreviewing(false);
      },
      onStop: (result) => {
        recordingRef.current = false;
        void cancelRef.current();
        if (video.current) video.current.srcObject = null;
        capture.current = null;
        if (mounted.current) {
          setRecorded(result);
          setRecording(false);
          setPreviewing(false);
          void preserveRecording(result);
        }
      },
    });
    capture.current = controller;
    try {
      const stream = await controller.open(audio);
      if (!mounted.current) return;
      if (video.current) {
        video.current.srcObject = stream;
        await video.current.play().catch(() => {});
      }
      if (
        mounted.current &&
        capture.current === controller &&
        controller.hasStream
      )
        setPreviewing(true);
    } catch (e) {
      controller.dispose();
      capture.current = null;
      if (mounted.current && (e as Error).name !== "AbortError")
        setError(cameraError(e));
    } finally {
      if (mounted.current) setOpening(false);
    }
  }
  function stop() {
    recordingRef.current = false;
    capture.current?.stop();
    void cancelRef.current();
    if (!recording) {
      capture.current?.dispose();
      capture.current = null;
      setPreviewing(false);
      if (video.current) video.current.srcObject = null;
    }
  }
  async function start() {
    if (!capture.current || !rights || recordingRef.current) return;
    setError("");
    setLiveError("");
    setRecorded(null);
    setSession(null);
    setSaved(false);
    try {
      capture.current.start();
      recordingRef.current = true;
      setRecording(true);
    } catch (e) {
      setError(cameraError(e));
      stop();
      return;
    }
    if (!consent || !capabilities?.available) {
      setLiveStatus(
        "Запись идёт только на устройстве. Облачный анализ выключен.",
      );
      return;
    }
    setLiveStatus("Подключаем визуальное наблюдение…");
    const attempt = new AbortController();
    sessionOpening.current = attempt;
    try {
      const current = await prepareLiveSession({
        signal: attempt.signal,
        prepare: () => prepareRef.current(),
        create: (working) =>
          api<VisualSession>(base + "/visual-sessions", {
            method: "POST",
            body: { consent_images: true },
            etag: working.etag,
            key: requestKey(),
          }),
        cancel: async (stale) => {
          await api(`${org}/visual-sessions/${stale.id}/cancel`, {
            method: "POST",
            body: {},
          });
        },
      });
      if (attempt.signal.aborted || !mounted.current) return;
      if (!current) {
        setLiveError(
          "Визуальное наблюдение не запущено. Сначала сохраните рабочую версию.",
        );
        setLiveStatus("Запись продолжается без анализа ИИ");
        return;
      }
      sessionRef.current = current;
      setSession(current);
      startLive(current);
    } catch (e) {
      if (mounted.current && !attempt.signal.aborted) {
        setLiveError((e as Error).message);
        setLiveStatus("Запись продолжается без анализа ИИ");
      }
    } finally {
      if (sessionOpening.current === attempt) sessionOpening.current = null;
    }
  }
  function startLive(current: VisualSession) {
    const controller = new AbortController();
    frameAbort.current = controller;
    let frameBusy = false,
      analysisBusy = false;
    let nextAnalysis = performance.now() + CAMERA_ANALYZE_INTERVAL_MS;
    let lastJob: ProcessingJob | null = null;
    const maxFrames = Math.min(
      CAMERA_MAX_FRAMES,
      capabilities?.limits.max_frames || CAMERA_MAX_FRAMES,
    );
    const queue = new LiveFrameQueue({
      signal: controller.signal,
      maxFrames,
      maxBytes: capabilities?.limits.max_frame_bytes || 524288,
      createKey: requestKey,
      send: (frame, signal) =>
        uploadFrame(
          `${org}/visual-sessions/${current.id}/frames`,
          frame.blob,
          frame.sequence,
          frame.timestampMs,
          frame.key,
          signal,
        ),
    });
    const canvas = document.createElement("canvas");
    const stopped = () =>
      controller.signal.aborted || !recordingRef.current || !mounted.current;
    const tick = async () => {
      if (stopped() || frameBusy) return;
      frameBusy = true;
      try {
        const acknowledged = await queue.sendNext(async () => {
          const element = video.current;
          if (!element || element.readyState < 2 || !element.videoWidth)
            return null;
          const size = frameDimensions(
            element.videoWidth,
            element.videoHeight,
            Math.min(960, capabilities?.limits.max_dimension || 960),
          );
          canvas.width = size.width;
          canvas.height = size.height;
          canvas
            .getContext("2d")
            ?.drawImage(element, 0, 0, size.width, size.height);
          return new Promise<Blob | null>((resolve) =>
            canvas.toBlob(resolve, "image/jpeg", 0.7),
          );
        });
        if (acknowledged && !stopped())
          setLiveStatus(
            `ИИ получает реальные кадры · отправлено ${queue.sent}`,
          );
        if (stopped()) return;
        if (lastJob) {
          lastJob = await api<ProcessingJob>(`${org}/jobs/${lastJob.id}`, {
            signal: controller.signal,
          });
          if (!activeJob(lastJob.state)) {
            analysisBusy = false;
            if (lastJob.state === "failed")
              setLiveError(
                lastJob.error_summary ||
                  "Не удалось проанализировать кадры. Запись продолжается.",
              );
            lastJob = null;
            const updated = await api<VisualSession>(
              `${org}/visual-sessions/${current.id}`,
              { signal: controller.signal },
            );
            if (!stopped()) {
              setSession(updated);
              sessionRef.current = updated;
            }
          }
        }
        if (
          !analysisBusy &&
          queue.sent >= 2 &&
          performance.now() >= nextAnalysis &&
          !stopped()
        ) {
          analysisBusy = true;
          nextAnalysis =
            performance.now() +
            Math.max(
              CAMERA_ANALYZE_INTERVAL_MS,
              capabilities?.limits.analysis_interval_ms || 0,
            );
          try {
            lastJob = await api<ProcessingJob>(
              `${org}/visual-sessions/${current.id}/analyze`,
              {
                method: "POST",
                body: {},
                key: requestKey(),
                signal: controller.signal,
              },
            );
            setLiveStatus("ИИ рассматривает последние кадры…");
          } catch (e) {
            analysisBusy = false;
            throw e;
          }
        }
      } catch (e) {
        if ((e as Error).name !== "AbortError" && mounted.current) {
          setLiveError((e as Error).message);
          if (e instanceof ApiError && [401, 403, 404].includes(e.status)) {
            controller.abort();
            setLiveStatus(
              "Облачный анализ остановлен. Локальная запись продолжается.",
            );
          }
        }
      } finally {
        frameBusy = false;
      }
    };
    const timer = setInterval(
      () => void tick(),
      Math.max(
        CAMERA_FRAME_INTERVAL_MS,
        capabilities?.limits.min_frame_interval_ms || 0,
      ),
    );
    controller.signal.addEventListener("abort", () => clearInterval(timer), {
      once: true,
    });
  }
  const latest = session?.observations[0];
  return (
    <section
      className="camera-panel pipeline-panel stack"
      aria-label="Камера и наблюдение за действиями"
    >
      <div>
        <span className="eyebrow">Живая камера</span>
        <h3>Покажите рабочий процесс</h3>
      </div>
      <p>
        Запишите рабочий процесс до 10 минут, затем сохраните видео и создайте
        по нему инструкцию. Покажите руки, инструмент и результат каждого
        действия. Камера включится только после нажатия кнопки и разрешения
        браузера.
      </p>
      {recovery && !recorded && (
        <div className="pipeline-notice stack">
          <strong>Найдена несохранённая запись</strong>
          <p>
            {time(recovery.durationMs)} · {formatBytes(recovery.file.size)}.
            Камера и ИИ при восстановлении не включаются.
          </p>
          <button
            type="button"
            className="button primary"
            disabled={recoveryBusy}
            onClick={() => {
              setRecorded(recovery);
              setElapsed(recovery.durationMs);
              setBytes(recovery.file.size);
              setRecovery(null);
              setRecoveryNotice(
                "Запись восстановлена из этого браузера. Скачайте её или сохраните в инструкции.",
              );
            }}
          >
            Восстановить запись
          </button>
          <button
            type="button"
            className="text-button"
            disabled={recoveryBusy}
            onClick={() => {
              if (
                window.confirm(
                  "Удалить локальную копию этой записи? Восстановить её после удаления нельзя.",
                )
              )
                void discardCopy();
            }}
          >
            Удалить локальную копию
          </button>
        </div>
      )}
      {recoveryNotice && (
        <p className="pipeline-notice" role="status">
          {recoveryNotice}
        </p>
      )}
      <div className="video-frame camera-preview">
        <video
          ref={video}
          hidden={!previewing && !recorded}
          autoPlay={!recorded}
          muted={!recorded}
          controls={!!recorded}
          playsInline
          src={recordedUrl || undefined}
          aria-label={
            recorded
              ? "Просмотр записанного видео"
              : "Предпросмотр своей камеры"
          }
        />
        {!previewing && !recorded && (
          <div className="camera-placeholder">
            <Icon name="eye" size={30} />
            <span>Камера выключена</span>
          </div>
        )}
      </div>
      {!previewing && !recorded && !recovery && (
        <>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={audio}
              onChange={(e) => setAudio(e.target.checked)}
              disabled={opening}
            />
            <span>Записывать звук с микрофона</span>
          </label>
          <button
            type="button"
            className="button secondary"
            disabled={opening || recoveryBusy}
            onClick={() => void enableCamera()}
          >
            {opening
              ? "Ожидаем разрешение браузера…"
              : audio
                ? "Включить камеру и микрофон"
                : "Включить камеру"}
          </button>
        </>
      )}
      {!recorded && !recovery && (
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={keepLocal}
            disabled={opening || previewing || recording || recoveryBusy}
            onChange={(e) => setKeepLocal(e.target.checked)}
          />
          <span>
            Сохранить остановленную запись в этом браузере для восстановления в
            течение 24 часов. На общем компьютере лучше отключить.
          </span>
        </label>
      )}
      {previewing && !recording && (
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={rights}
            onChange={(e) => setRights(e.target.checked)}
          />
          <span>
            У меня есть право использовать запись и согласие людей в кадре.
          </span>
        </label>
      )}
      {!recorded && (
        <>
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
              подписки ChatGPT. Ответ может занять до 3 минут.
            </p>
          )}
          {capabilities?.budget?.mode === "free_calls" && (
            <p className="field-hint">
              Бесплатные модели доступны с ограничениями. При исчерпании лимита
              анализ остановится; переход на платную модель отключён.
            </p>
          )}
          {capabilities?.provider === "openrouter" &&
            capabilities.budget?.mode === "usd" && (
              <p className="field-hint">
                Анализ оплачивается с баланса OpenRouter. Действуют лимиты
                расходов сеанса и проекта; автоматическая смена модели
                отключена.
              </p>
            )}
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={consent}
              onChange={(e) => setConsent(e.target.checked)}
              disabled={recording || !capabilities?.available}
            />
            <span>
              Разрешаю отправлять кадры камеры{" "}
              {capabilities?.provider === "nvidia"
                ? "в NVIDIA"
                : capabilities?.provider === "openrouter"
                  ? "в OpenRouter и выбранному им провайдеру модели"
                  : capabilities?.provider === "codex"
                    ? "в OpenAI через подписку ChatGPT"
                    : "настроенному облачному ИИ"}{" "}
              для наблюдения за действиями во время записи.
            </span>
          </label>
          <p className="field-hint">
            Изображения — отдельное согласие. При включении отправляем
            уменьшенный кадр не чаще одного раза в 2 секунды. ИИ анализирует
            выборку, поэтому может пропустить движение.
          </p>
        </>
      )}
      {!capabilities?.available && (
        <div className="pipeline-notice">
          <p>
            Визуальный ИИ не подключён.{" "}
            {capabilities?.reason || "Проверяем настройки сервера…"} Запись,
            скачивание и ручная разметка доступны.
          </p>
          <button
            type="button"
            className="text-button"
            disabled={recording}
            onClick={() => setCapabilityTick((v) => v + 1)}
          >
            Проверить подключение
          </button>
        </div>
      )}
      {previewing && (
        <div className="button-row">
          {!recording ? (
            <button
              type="button"
              className="button primary"
              disabled={!rights}
              onClick={() => void start()}
            >
              Начать запись
              {consent && capabilities?.available ? " и наблюдение ИИ" : ""}
            </button>
          ) : (
            <button type="button" className="button primary" onClick={stop}>
              Остановить запись
            </button>
          )}{" "}
          {!recording && (
            <button type="button" className="text-button" onClick={stop}>
              Выключить камеру
            </button>
          )}
        </div>
      )}
      {recording && (
        <p className="recording-state" role="status">
          <span aria-hidden="true" />
          Запись · {time(elapsed)} / 10:00 · {formatBytes(bytes)}
        </p>
      )}
      {error && <ErrorBanner error={error} />}
      {liveStatus && (
        <p className="field-hint" role="status">
          {liveStatus}
        </p>
      )}
      {liveError && <ErrorBanner error={liveError} />}
      {latest && (
        <div className="live-observations">
          <strong>Предварительные наблюдения</strong>
          <p>
            Сейчас ИИ собирает наблюдения во время съёмки. Для учебных шагов
            остановите запись, сохраните её и выберите «Создать инструкцию по
            видео». Здесь ничего не добавляется в рабочие шаги. Время на кадрах
            отсчитывается от подключения наблюдения ИИ; сохранённое видео
            анализируется отдельно.
          </p>
          {latest.steps.map((step, i) => (
            <article key={i}>
              <h4>{step.title}</h4>
              <p>{step.description}</p>
              <FrameEvidence
                timeOrigin="session"
                frames={latest.frames.filter((frame) =>
                  step.evidence_frame_ids.includes(frame.id),
                )}
              />
            </article>
          ))}
          {latest.warnings.map((warning, i) => (
            <p className="pipeline-notice" key={i}>
              {warning.message}
            </p>
          ))}
        </div>
      )}
      {recorded && (
        <div className="stack">
          <strong>
            Запись остановлена · {time(recorded.durationMs)} ·{" "}
            {formatBytes(recorded.file.size)}
          </strong>
          <p>
            {localCopy.current
              ? "Восстановление доступно 24 часа. Копия удаляется после загрузки или вручную; просроченные копии удаляются при следующем открытии камеры."
              : "Видео пока хранится в памяти этой страницы. Скачайте его перед закрытием."}{" "}
            Сохраните видео, дождитесь его подготовки и выберите «Создать
            инструкцию по видео»: получите шаги со способом выполнения и
            ориентирами для проверки по записи. Для анализа потребуется ваше
            согласие и выбранный лимит.
          </p>
          <a
            className="button secondary"
            href={recordedUrl}
            download={recorded.file.name}
          >
            Скачать запись
          </a>
          {recorded.durationMs < 10_000 && (
            <p className="pipeline-notice">
              Для загрузки нужна запись не короче 10 секунд. Скачайте короткий
              фрагмент или запишите заново.
            </p>
          )}
          {upload.error && <ErrorBanner error={upload.error} />}
          {upload.notice && <p role="status">{upload.notice}</p>}
          {upload.sending ? (
            <UploadProgress
              progress={upload.progress}
              cancel={() => void upload.cancel()}
            />
          ) : (
            <button
              type="button"
              className="button primary"
              disabled={
                recorded.durationMs < 10_000 ||
                recorded.truncated ||
                recoveryBusy ||
                saved ||
                !recorded.file.size
              }
              onClick={async () => {
                if (await upload.start(base, recorded.file, language)) {
                  setSaved(true);
                  await discardCopy();
                  onActive(false);
                  onComplete();
                }
              }}
            >
              Сохранить видео в инструкции
            </button>
          )}
          {!upload.sending && (
            <button
              type="button"
              className="text-button"
              disabled={recoveryBusy}
              onClick={async () => {
                if (
                  window.confirm(
                    "Удалить текущую запись и её локальную копию? Сначала скачайте видео, если хотите сохранить.",
                  )
                ) {
                  if (!(await discardCopy())) return;
                  setRecorded(null);
                  setElapsed(0);
                  setBytes(0);
                  setError("");
                  setLiveStatus("");
                }
              }}
            >
              Записать заново
            </button>
          )}
        </div>
      )}
      <button
        type="button"
        className="text-button"
        disabled={opening || recording || upload.sending || recoveryBusy}
        onClick={() => {
          if (
            !recorded ||
            window.confirm(
              localCopy.current
                ? "Закрыть просмотр? Локальную запись можно восстановить через кнопку камеры этой инструкции в течение 24 часов."
                : "Закрыть запись? Несохранённое видео будет удалено из памяти страницы.",
            )
          ) {
            stop();
            onClose();
          }
        }}
      >
        Вернуться к загрузке файла
      </button>
    </section>
  );
}
