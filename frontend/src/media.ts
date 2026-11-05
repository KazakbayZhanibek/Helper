export const MAX_UPLOAD_BYTES = 500 * 1024 * 1024;
export const MAX_UPLOAD_SECONDS = 10 * 60;
export function videoFileError(file: { name: string; size: number }): string {
  if (!/\.(mp4|mov|webm)$/i.test(file.name))
    return "Выберите видео в формате MP4, MOV или WebM.";
  if (file.size <= 0) return "Файл пуст. Выберите другую запись.";
  if (file.size > MAX_UPLOAD_BYTES)
    return "Видео больше 500 MiB. Сократите запись или уменьшите размер файла.";
  return "";
}
export const formatBytes = (bytes: number) =>
  `${(bytes / 1024 / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} MiB`;
export function uploadPercent(loaded: number, total: number): number {
  return total > 0
    ? Math.min(100, Math.max(0, Math.floor((loaded / total) * 100)))
    : 0;
}
export function pollDelay(
  startedAt: number,
  now: number,
  hidden: boolean,
  failures = 0,
): number {
  if (hidden) return 30_000;
  return Math.min(
    30_000,
    Math.max(
      now - startedAt > 60_000 ? 10_000 : 3_000,
      failures ? 3_000 * 2 ** failures : 0,
    ),
  );
}
export const activeJob = (state: string) =>
  ["queued", "running", "pending", "processing", "cancelling"].includes(state);
export const stageName = (stage: string) =>
  (
    ({
      queued: "Ожидает обработки",
      validating: "Проверяем файл",
      probing: "Проверяем запись",
      validate: "Проверяем файл",
      transcode: "Готовим видео",
      transcoding: "Готовим видео",
      thumbnail: "Готовим обложку",
      extracting_audio: "Подготавливаем звук",
      audio: "Подготавливаем звук",
      transcribing: "Распознаём речь",
      transcription: "Распознаём речь",
      asr: "Распознаём речь",
      generating: "Составляем шаги",
      generation: "Составляем шаги",
      drafting: "Составляем шаги",
      llm: "Составляем шаги",
      extracting_frames: "Выбираем кадры видео",
      extracting_visual_frames: "Выбираем кадры видео",
      analyzing_frames: "Распознаём действия по кадрам",
      analyzing_visual_frames: "Распознаём действия по кадрам",
      analyzing_video_window: "Последовательно просматриваем фрагменты",
      refining_visual_window: "Подробнее смотрим неясный момент",
      checking_visual_actions: "Сопоставляем учебные шаги с кадрами",
      validating_visual_proposal: "Проверяем предложенные действия",
      visual_analysis: "Распознаём действия по кадрам",
      done: "Готово",
      ready: "Готово",
      completed: "Готово",
      succeeded: "Готово",
      failed: "Обработка остановлена",
      cancelled: "Отменено",
    }) as Record<string, string>
  )[stage] || "Обрабатываем запись";
