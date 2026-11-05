import assert from "node:assert/strict";
import test from "node:test";
import {
  activeJob,
  MAX_UPLOAD_BYTES,
  pollDelay,
  stageName,
  uploadPercent,
  videoFileError,
} from "../src/media.ts";

test("video upload accepts documented bounds and rejects empty, disguised and oversize files", () => {
  assert.equal(
    videoFileError({ name: "Сборка.MOV", size: MAX_UPLOAD_BYTES }),
    "",
  );
  assert.equal(videoFileError({ name: "опыт.mp4", size: 1 }), "");
  assert.equal(videoFileError({ name: "камера.webm", size: 20 }), "");
  assert.match(
    videoFileError({ name: "фильм.mp4.exe", size: 10 }),
    /MP4, MOV или WebM/,
  );
  assert.match(videoFileError({ name: "фильм.mp4", size: 0 }), /пуст/);
  assert.match(
    videoFileError({ name: "фильм.mp4", size: MAX_UPLOAD_BYTES + 1 }),
    /500 MiB/,
  );
});
test("upload percentage measures bytes and does not imply processing completion", () => {
  assert.equal(uploadPercent(1, 3), 33);
  assert.equal(uploadPercent(999, 1000), 99);
  assert.equal(uploadPercent(1000, 1000), 100);
  assert.equal(uploadPercent(0, 0), 0);
  assert.equal(uploadPercent(-10, 100), 0);
  assert.equal(uploadPercent(110, 100), 100);
});
test("jobs poll quickly at first, back off after a minute and slow down in hidden tabs", () => {
  assert.equal(pollDelay(0, 59_000, false), 3000);
  assert.equal(pollDelay(0, 61_000, false), 10000);
  assert.equal(pollDelay(0, 100, true), 30000);
  assert.equal(pollDelay(0, 100, false, 1), 6000);
  assert.equal(pollDelay(0, 100, false, 10), 30000);
});
test("terminal job states stop polling and done is presented as completed", () => {
  for (const state of ["queued", "running"])
    assert.equal(activeJob(state), true);
  for (const state of ["done", "failed", "cancelled"])
    assert.equal(activeJob(state), false);
  assert.equal(stageName("done"), "Готово");
  assert.equal(stageName("extracting_audio"), "Подготавливаем звук");
  assert.equal(stageName("extracting_visual_frames"), "Выбираем кадры видео");
  assert.equal(
    stageName("analyzing_visual_frames"),
    "Распознаём действия по кадрам",
  );
  assert.equal(
    stageName("validating_visual_proposal"),
    "Проверяем предложенные действия",
  );
});
