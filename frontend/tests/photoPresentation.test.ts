import { strict as assert } from "node:assert";
import { test } from "node:test";
import { editedPhotoWarning, photoAlternative } from "../src/photoPresentation.ts";

test("photo alternative includes the specific visible caption", () => {
  assert.equal(photoAlternative("Замес · фото 1", " Рука складывает тесто "), "Замес · фото 1: Рука складывает тесто");
  assert.equal(photoAlternative("Фото 1", "   "), "Фото 1");
  assert.equal(photoAlternative("Фото 1"), "Фото 1");
});

test("edited AI photos require comparison without warning on untouched steps", () => {
  assert.equal(editedPhotoWarning("ai_edited"), true);
  for (const origin of ["ai", "manual", undefined]) {
    assert.equal(editedPhotoWarning(origin), false);
  }
});
