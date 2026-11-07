import { strict as assert } from "node:assert";
import { test } from "node:test";
import { editedPhotoWarning, needsIntentReview, photoAlternative } from "../src/photoPresentation.ts";

test("photo alternative includes the specific visible caption", () => {
  assert.equal(photoAlternative("Замес · фото 1", " Рука складывает тесто "), "Замес · фото 1: Рука складывает тесто");
  assert.equal(photoAlternative("Фото 1", "   "), "Фото 1");
  assert.equal(photoAlternative("Фото 1"), "Фото 1");
});

test("goals and quality claims trigger a review hint without altering text or photos", () => {
  assert.equal(needsIntentReview("Прижимаем тесто, чтобы усилить структуру."), true);
  assert.equal(needsIntentReview("Поверхность стала идеально гладкой."), true);
  assert.equal(needsIntentReview("Поднимаем предмет", undefined, "Деталь удерживают, чтобы выровнять края."), true);
  assert.equal(needsIntentReview("Turn it so that the edge aligns."), true);
  assert.equal(needsIntentReview("Шетін түзету үшін басамыз."), true);
});

test("ordinary action words and partial matches do not trigger a goal hint", () => {
  assert.equal(needsIntentReview("Складываем край и прижимаем ладонью."), false);
  assert.equal(needsIntentReview("Плавно разглаживаем поверхность.", "Рука лежит на тесте."), false);
  assert.equal(needsIntentReview("Нечто вроде чтобыточно", undefined), false);
  assert.equal(needsIntentReview("Показываем структуру материала."), false);
});

test("edited AI photos require comparison without warning on untouched steps", () => {
  assert.equal(editedPhotoWarning("ai_edited"), true);
  for (const origin of ["ai", "manual", undefined]) {
    assert.equal(editedPhotoWarning(origin), false);
  }
});
