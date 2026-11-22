import { useRef } from "react";
import type { RecipeInputs } from "./editorDraft";
import { countNoun } from "./format";
import type { Recipe } from "./types";

export function RecipeEditor({
  ingredientsInput,
  servingsInput,
  disabled,
  onChange,
}: RecipeInputs & {
  disabled: boolean;
  onChange: (patch: Partial<RecipeInputs>) => void;
}) {
  const initiallyOpen = useRef(Boolean(ingredientsInput || servingsInput));
  return (
    <details className="recipe-editor" open={initiallyOpen.current}>
      <summary>Ингредиенты и порции</summary>
      <fieldset disabled={disabled}>
        <label>
          Ингредиенты
          <textarea
            rows={4}
            value={ingredientsInput}
            onChange={(event) =>
              onChange({ ingredientsInput: event.target.value })
            }
            aria-describedby="recipe-ingredients-hint"
          />
        </label>
        <p className="field-hint" id="recipe-ingredients-hint">
          Один ингредиент на строку. Количество можно записать рядом, если оно
          известно.
        </p>
        <label className="recipe-servings">
          Количество порций
          <input
            inputMode="numeric"
            maxLength={4}
            value={servingsInput}
            onChange={(event) => {
              if (/^\d*$/.test(event.target.value)) {
                onChange({ servingsInput: event.target.value });
              }
            }}
          />
        </label>
      </fieldset>
    </details>
  );
}

export function RecipeIngredients({ recipe }: { recipe: Recipe }) {
  return (
    <section className="recipe-ingredients" aria-label="Ингредиенты рецепта">
      <div className="section-heading">
        <h2>Ингредиенты</h2>
        {recipe.servings !== null && (
          <span className="small muted">
            {recipe.servings}{" "}
            {countNoun(recipe.servings, ["порция", "порции", "порций"])}
          </span>
        )}
      </div>
      <ul>
        {recipe.ingredients.map((ingredient, index) => (
          <li key={index}>{ingredient}</li>
        ))}
      </ul>
    </section>
  );
}
