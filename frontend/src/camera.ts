export const CAMERA_MAX_BYTES = 500 * 1024 * 1024;
export const CAMERA_MAX_MS = 10 * 60 * 1000;
export const CAMERA_MAX_FRAMES = 300;
export const CAMERA_FRAME_INTERVAL_MS = 2000;
export const CAMERA_ANALYZE_INTERVAL_MS = 8000;
const MIME_TYPES = [
  "video/webm;codecs=vp8,opus",
  "video/webm;codecs=vp8",
  "video/mp4;codecs=avc1.42E01E,mp4a.40.2",
  "video/mp4",
  "video/webm",
];
export function recordingMime(
  supported: (type: string) => boolean,
): string | null {
  return MIME_TYPES.find(supported) || null;
}
export function cameraError(error: unknown): string {
  const name = (error as Error)?.name;
  if (["NotAllowedError", "PermissionDeniedError"].includes(name))
    return "Доступ к камере или микрофону не разрешён. Разрешите доступ в настройках браузера или запишите видео без микрофона.";
  if (name === "NotFoundError")
    return "Камера не найдена. Подключите её или загрузите готовое видео.";
  if (name === "NotReadableError")
    return "Камера занята другой программой или недоступна. Освободите её и повторите.";
  if (name === "OverconstrainedError")
    return "Камера не поддерживает выбранные параметры. Попробуйте другое устройство.";
  return (
    (error as Error)?.message ||
    "Не удалось включить камеру. Загрузите видео с устройства."
  );
}
export function frameDimensions(width: number, height: number, max = 960) {
  const ratio = Math.min(1, max / Math.max(width, height));
  return {
    width: Math.max(1, Math.round(width * ratio)),
    height: Math.max(1, Math.round(height * ratio)),
  };
}
export function recordingBoundary(
  elapsedMs: number,
  bytes: number,
): "duration" | "size" | null {
  if (elapsedMs >= CAMERA_MAX_MS) return "duration";
  // Stop before the hard cap, leaving room for the recorder's final chunk.
  if (bytes >= CAMERA_MAX_BYTES - 5 * 1024 * 1024) return "size";
  return null;
}
export function stopCameraTracks(
  stream: Pick<MediaStream, "getTracks"> | null,
) {
  stream?.getTracks().forEach((track) => track.stop());
}
export type CaptureResult = {
  file: File;
  durationMs: number;
  truncated: boolean;
};
type CaptureOptions = {
  devices: Pick<MediaDevices, "getUserMedia">;
  recorder: typeof MediaRecorder;
  now?: () => number;
  onProgress: (elapsedMs: number, bytes: number) => void;
  onStop: (result: CaptureResult) => void;
  onError: (message: string) => void;
  onPreviewStopped?: () => void;
};
/** Owns tracks and recorder lifecycle. Opening a camera is only called by a user action. */
export class CameraCaptureController {
  private options: CaptureOptions;
  private stream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private bytes = 0;
  private startedAt = 0;
  private elapsedMs = 0;
  private stopAt: number | null = null;
  private truncated = false;
  private disposed = false;
  private opening = false;
  private timer: ReturnType<typeof setInterval> | null = null;
  constructor(options: CaptureOptions) {
    this.options = options;
  }
  get hasStream() {
    return !!this.stream && !this.disposed;
  }
  private now() {
    return (this.options.now || performance.now.bind(performance))();
  }
  async open(audio: boolean): Promise<MediaStream> {
    if (this.disposed || this.opening || this.stream)
      throw new Error("Камера уже открывается или включена.");
    this.opening = true;
    try {
      const stream = await this.options.devices.getUserMedia({
        video: {
          width: { ideal: 1280 },
          height: { ideal: 720 },
          frameRate: { ideal: 24, max: 30 },
          facingMode: "environment",
        },
        audio,
      });
      if (this.disposed) {
        stopCameraTracks(stream);
        throw new DOMException("Отменено", "AbortError");
      }
      this.stream = stream;
      stream.getTracks().forEach((track) =>
        track.addEventListener?.(
          "ended",
          () => {
            if (!this.disposed && this.stream) {
              this.options.onError(
                this.recorder
                  ? "Камера или микрофон отключились. Запись остановлена."
                  : "Камера или микрофон отключились. Включите камеру снова.",
              );
              const wasPreviewing = !this.recorder;
              this.stop();
              if (wasPreviewing) this.options.onPreviewStopped?.();
            }
          },
          { once: true },
        ),
      );
      return stream;
    } finally {
      this.opening = false;
    }
  }
  start() {
    if (!this.stream || this.disposed || this.recorder)
      throw new Error("Сначала включите камеру.");
    const mime = recordingMime(
      this.options.recorder.isTypeSupported.bind(this.options.recorder),
    );
    if (!mime)
      throw new Error(
        "Браузер не поддерживает запись MP4 или WebM. Загрузите готовое видео.",
      );
    const recorder = new this.options.recorder(this.stream, {
      mimeType: mime,
      videoBitsPerSecond: 2_000_000,
      audioBitsPerSecond: 96_000,
    });
    this.recorder = recorder;
    this.chunks = [];
    this.bytes = 0;
    this.truncated = false;
    this.startedAt = this.now();
    this.elapsedMs = 0;
    this.stopAt = null;
    recorder.ondataavailable = (event) => {
      if (this.disposed || !event.data.size) return;
      if (this.bytes + event.data.size > CAMERA_MAX_BYTES) {
        this.truncated = true;
        this.options.onError(
          "Запись превысила 500 MiB и остановлена. Сохраните файл и сократите его перед загрузкой.",
        );
        this.stop();
        return;
      }
      this.chunks.push(event.data);
      this.bytes += event.data.size;
      this.checkLimits();
    };
    recorder.onerror = () => {
      this.options.onError(
        "Браузер прервал запись. Сохраните доступный фрагмент и попробуйте ещё раз.",
      );
      this.stop();
    };
    recorder.onstop = () => {
      this.elapsedMs = Math.min(
        CAMERA_MAX_MS,
        Math.max(0, (this.stopAt ?? this.now()) - this.startedAt),
      );
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
      stopCameraTracks(this.stream);
      this.stream = null;
      this.recorder = null;
      if (this.disposed) {
        this.chunks = [];
        return;
      }
      const type = recorder.mimeType || mime;
      const file = new File(
        this.chunks,
        `camera-${new Date().toISOString().replace(/[:.]/g, "-")}.${type.includes("mp4") ? "mp4" : "webm"}`,
        { type },
      );
      this.chunks = [];
      this.options.onStop({
        file,
        durationMs: this.elapsedMs,
        truncated: this.truncated,
      });
    };
    recorder.start(1000);
    this.timer = setInterval(() => this.checkLimits(), 500);
  }
  checkLimits() {
    if (!this.recorder || this.recorder.state !== "recording") return;
    this.elapsedMs = Math.min(
      CAMERA_MAX_MS,
      Math.max(0, this.now() - this.startedAt),
    );
    this.options.onProgress(this.elapsedMs, this.bytes);
    const boundary = recordingBoundary(this.elapsedMs, this.bytes);
    if (boundary) {
      this.options.onError(
        boundary === "duration"
          ? "Достигнут лимит 10 минут. Запись остановлена и готова к сохранению."
          : "Достигнут лимит размера. Запись остановлена и готова к сохранению.",
      );
      this.stop();
    }
  }
  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    if (this.recorder && this.stopAt === null) {
      this.stopAt = this.now();
      this.elapsedMs = Math.min(
        CAMERA_MAX_MS,
        Math.max(0, this.stopAt - this.startedAt),
      );
    }
    if (this.recorder && this.recorder.state !== "inactive")
      this.recorder.stop();
    stopCameraTracks(this.stream);
    if (!this.recorder) this.stream = null;
  }
  dispose() {
    this.disposed = true;
    this.stop();
    this.chunks = [];
  }
}
