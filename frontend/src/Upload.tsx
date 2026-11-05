import { useEffect, useRef, useState } from "react";
import { api, ApiError, orgApi, requestKey, uploadVideo } from "./api";
import { ErrorBanner } from "./components";
import { formatBytes, uploadPercent, videoFileError } from "./media";
import type { Language } from "./types";

type UploadSession = { id: string; upload_id: string; state: string };
export type UploadResult = {
  upload_id: string;
  asset_id: string;
  job_id: string;
  state: string;
};
export function useVideoUpload(orgId: string) {
  const [sending, setSending] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const controller = useRef<AbortController | null>(null);
  const session = useRef<{ id: string; file: File; base: string } | null>(null);
  const initialization = useRef<{
    key: string;
    file: File;
    base: string;
  } | null>(null);
  const org = orgApi(orgId);
  useEffect(
    () => () => {
      controller.current?.abort();
      const previous = session.current;
      if (previous)
        void api(`${orgApi(orgId)}/uploads/${previous.id}`, {
          method: "DELETE",
        }).catch(() => {});
    },
    [orgId],
  );
  async function start(
    base: string,
    file: File,
    language: Language,
  ): Promise<UploadResult | null> {
    if (controller.current) return null;
    const invalid = videoFileError(file);
    if (invalid) {
      setError(invalid);
      return null;
    }
    const abort = new AbortController();
    controller.current = abort;
    setSending(true);
    setProgress(0);
    setError("");
    setNotice("");
    try {
      let result: UploadResult | null = null;
      for (let attempt = 0; attempt < 2; attempt++) {
        if (
          !session.current ||
          session.current.file !== file ||
          session.current.base !== base
        ) {
          if (session.current)
            await api(`${org}/uploads/${session.current.id}`, {
              method: "DELETE",
            });
          if (
            !initialization.current ||
            initialization.current.file !== file ||
            initialization.current.base !== base
          )
            initialization.current = { key: requestKey(), file, base };
          const init = await api<UploadSession>(base + "/uploads", {
            method: "POST",
            body: {
              filename: file.name,
              size_bytes: file.size,
              language,
              rights_confirmed: true,
            },
            key: initialization.current.key,
          });
          session.current = { id: init.upload_id || init.id, file, base };
        }
        if (abort.signal.aborted) {
          await api(`${org}/uploads/${session.current.id}`, {
            method: "DELETE",
          });
          session.current = null;
          initialization.current = null;
          throw new DOMException("Отменено", "AbortError");
        }
        try {
          result = await uploadVideo<UploadResult>(
            `${org}/uploads/${session.current.id}/content`,
            file,
            (loaded, total) => setProgress(uploadPercent(loaded, total)),
            abort.signal,
          );
          break;
        } catch (e) {
          if (
            e instanceof ApiError &&
            e.code === "UPLOAD_CLOSED" &&
            attempt === 0
          ) {
            session.current = null;
            initialization.current = null;
            continue;
          }
          throw e;
        }
      }
      initialization.current = null;
      session.current = null;
      setNotice("Файл загружен. Подготовка продолжится на сервере.");
      return result;
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError")
        setNotice("Передача отменена. Для повтора загрузите файл с начала.");
      else setError((e as Error).message);
      return null;
    } finally {
      controller.current = null;
      setSending(false);
    }
  }
  async function cancel() {
    controller.current?.abort();
    const previous = session.current;
    session.current = null;
    initialization.current = null;
    if (previous) {
      try {
        await api(`${org}/uploads/${previous.id}`, { method: "DELETE" });
      } catch (e) {
        setError(
          `Передача остановлена. Не удалось подтвердить отмену на сервере: ${(e as Error).message}`,
        );
      }
    }
  }
  return { start, cancel, sending, progress, error, notice };
}
export function VideoFileFields({
  file,
  onFile,
  rights,
  onRights,
  disabled = false,
}: {
  file: File | null;
  onFile: (file: File | null) => void;
  rights: boolean;
  onRights: (rights: boolean) => void;
  disabled?: boolean;
}) {
  const [error, setError] = useState("");
  return (
    <div className="upload-fields stack">
      <label>
        Видеозапись
        <span className="field-hint">
          MP4, MOV или WebM · до 500 MiB · от 10 секунд до 10 минут.
          Длительность и формат проверим на сервере.
        </span>
        <input
          type="file"
          accept=".mp4,.mov,.webm,video/mp4,video/quicktime,video/webm"
          disabled={disabled}
          onChange={(event) => {
            const selected = event.target.files?.[0] || null;
            const invalid = selected ? videoFileError(selected) : "";
            setError(invalid);
            onFile(invalid ? null : selected);
          }}
        />
      </label>
      {file && (
        <span className="file-details">
          {file.name} · {formatBytes(file.size)}
        </span>
      )}
      {error && <ErrorBanner error={error} />}
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={rights}
          disabled={disabled}
          onChange={(event) => onRights(event.target.checked)}
        />
        <span>
          У меня есть право использовать запись и согласие людей, попавших в
          кадр.
        </span>
      </label>
      <p className="field-hint">
        Произносите вслух названия действий и предметов — так расшифровка будет
        полезнее.
      </p>
    </div>
  );
}
export function UploadProgress({
  progress,
  cancel,
}: {
  progress: number;
  cancel: () => void;
}) {
  return (
    <div className="upload-progress" aria-live="polite">
      <label htmlFor="video-upload-progress">
        {progress < 100
          ? `Передаём файл · ${progress}%`
          : "Файл передан. Ждём подтверждения сервера…"}
      </label>
      <progress id="video-upload-progress" max={100} value={progress} />
      <p className="field-hint">
        Оставьте страницу открытой до завершения передачи. Обработка видео
        начнётся после загрузки.
      </p>
      <button className="text-button" type="button" onClick={cancel}>
        Отменить передачу
      </button>
    </div>
  );
}
export function UploadCard({
  orgId,
  base,
  language,
  onComplete,
  onSending,
}: {
  orgId: string;
  base: string;
  language: Language;
  onComplete: () => void;
  onSending: (sending: boolean) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [rights, setRights] = useState(false);
  const upload = useVideoUpload(orgId);
  useEffect(() => {
    onSending(upload.sending);
    return () => onSending(false);
  }, [upload.sending, onSending]);
  return (
    <section
      className="pipeline-panel stack"
      aria-label="Загрузка исходной записи"
    >
      <h3>Добавьте свою запись</h3>
      <VideoFileFields
        file={file}
        onFile={setFile}
        rights={rights}
        onRights={setRights}
        disabled={upload.sending}
      />
      {upload.error && <ErrorBanner error={upload.error} />}
      {upload.notice && <p role="status">{upload.notice}</p>}
      {upload.sending ? (
        <UploadProgress
          progress={upload.progress}
          cancel={() => void upload.cancel()}
        />
      ) : (
        <button
          className="button primary"
          disabled={!file || !rights}
          onClick={async () => {
            if (file && (await upload.start(base, file, language)))
              onComplete();
          }}
        >
          Загрузить видео
        </button>
      )}
    </section>
  );
}
