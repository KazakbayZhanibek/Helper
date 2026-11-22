import { useState } from "react";
import type { RecipeInputs } from "./editorDraft";
import { countNoun } from "./format";
import type { Recipe } from "./types";
import "./recipe-cooking.css";

export function RecipeEditor({
  ingredientsInput,
  servingsInput,
  disabled,
  onChange,
}: RecipeInputs & {
  disabled: boolean;
  onChange: (patch: Partial<RecipeInputs>) => void;
}) {
  return (
    <section
      className="recipe-editor"
      aria-labelledby="recipe-ingredients-heading"
    >
      <h2 id="recipe-ingredients-heading">Ингредиенты и порции</h2>
      <fieldset disabled={disabled}>
        <label>
          Список ингредиентов
          <textarea
            rows={5}
            value={ingredientsInput}
            onChange={(event) =>
              onChange({ ingredientsInput: event.target.value })
            }
            placeholder={"Мука — 300 г\nВода — 180 мл"}
            aria-describedby="recipe-ingredients-hint"
          />
        </label>
        <p className="field-hint" id="recipe-ingredients-hint">
          Каждый ингредиент с новой строки. Добавьте количество, если оно вам
          известно.
        </p>
        <label className="recipe-servings">
          Сколько порций получится
          <input
            inputMode="numeric"
            maxLength={4}
            placeholder="Необязательно"
            value={servingsInput}
            onChange={(event) => {
              if (/^\d*$/.test(event.target.value)) {
                onChange({ servingsInput: event.target.value });
              }
            }}
          />
        </label>
      </fieldset>
    </section>
  );
}

export function RecipeIngredients({
  recipe,
  checklist = false,
}: {
  recipe: Recipe;
  checklist?: boolean;
}) {
  const [prepared, setPrepared] = useState<number[]>([]);
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
      {checklist && (
        <p className="ingredient-count" aria-live="polite">
          Подготовлено {prepared.length} из {recipe.ingredients.length}
        </p>
      )}
      <ul className={checklist ? "ingredient-checklist" : undefined}>
        {recipe.ingredients.map((ingredient, index) => (
          <li key={index}>
            {checklist ? (
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={prepared.includes(index)}
                  onChange={(event) => {
                    const checked = event.target.checked;
                    setPrepared((items) =>
                      checked
                        ? [...items, index]
                        : items.filter((item) => item !== index),
                    );
                  }}
                />
                <span>{ingredient}</span>
              </label>
            ) : (
              ingredient
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
