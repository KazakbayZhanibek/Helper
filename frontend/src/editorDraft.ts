import { formatMsExact, parseMs } from "./format.ts";
import type { Recipe, Step, WorkingCopy } from "./types.ts";

export type DraftStep = Step & { startInput: string; endInput: string };
export type RecipeInputs = { ingredientsInput: string; servingsInput: string };
export type Draft = Omit<WorkingCopy, "steps"> &
  RecipeInputs & { steps: DraftStep[] };

export const toDraft = (wc: WorkingCopy): Draft => ({
  ...wc,
  ingredientsInput: wc.recipe?.ingredients.join("\n") ?? "",
  servingsInput: wc.recipe?.servings?.toString() ?? "",
  steps: wc.steps.map((step) => ({
    ...step,
    startInput: formatMsExact(step.start_ms),
    endInput: formatMsExact(step.end_ms),
  })),
});

export function recipeFromInputs({
  ingredientsInput,
  servingsInput,
  recipe,
}: RecipeInputs & { recipe?: Recipe | null }): Recipe | null {
  const ingredients = ingredientsInput
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const servings = servingsInput === "" ? null : Number(servingsInput);
  return recipe != null || ingredients.length || servings !== null
    ? { ingredients, servings }
    : null;
}

export const payload = (form: Draft) => ({
  title: form.title.trim(),
  purpose: form.purpose.trim(),
  language: form.language,
  recipe: recipeFromInputs(form),
  steps: form.steps.map((step) => ({
    id: step.id,
    title: step.title.trim(),
    description: step.description.trim(),
    start_ms: parseMs(step.startInput),
    end_ms: parseMs(step.endInput),
    evidence_segment_ids: step.evidence_segment_ids || [],
    evidence_frame_ids: step.evidence_frame_ids || [],
  })),
});
