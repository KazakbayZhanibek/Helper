import { useEffect, useState } from "react";
import { CookingGuide } from "./CookingGuide";
import { Brand, ErrorBanner, Icon, Loading, time } from "./components";
import { countNoun } from "./format";
import type { Language, Recipe, Step, Version } from "./types";

const examplePath = "/examples/vegetables";
type ExampleData = {
  title: string;
  purpose: string;
  language: Language;
  recipe: Recipe;
  duration_ms: number;
  steps: Pick<Step, "title" | "description" | "start_ms" | "end_ms">[];
};

export function ExampleRecipe() {
  const [snapshot, setSnapshot] = useState<Version["snapshot"] | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch(examplePath + "/recipe.json", {
          signal: controller.signal,
          credentials: "omit",
        });
        if (!response.ok) throw new Error("unavailable");
        const data: ExampleData = await response.json();
        setSnapshot({
          title: data.title,
          purpose: data.purpose,
          language: data.language,
          recipe: data.recipe,
          source: {
            id: "vegetables",
            duration_ms: data.duration_ms,
            status: "ready",
            playback_url: examplePath + "/vegetables.mp4",
            thumbnail_url: examplePath + "/vegetables.jpg",
          },
          steps: data.steps.map((step, index) => ({
            ...step,
            id: String(index + 1),
            ordinal: index + 1,
            origin: "manual",
            checked: true,
            checked_by: null,
            evidence_segment_ids: [],
          })),
        });
      } catch {
        if (!controller.signal.aborted) setError(true);
      }
    }
    void load();
    return () => controller.abort();
  }, [attempt]);
  return (
    <div className="public-site recipe-site recipe-sample-page">
      <a className="skip-link" href="#example">
        Перейти к рецепту
      </a>
      <header className="public-header">
        <a href="/" aria-label="Рецепты мастера — главная">
          <Brand />
        </a>
        <nav aria-label="Главное меню">
          <a href="/login" className="button secondary">
            Войти <Icon name="arrow" size={16} />
          </a>
        </nav>
      </header>
      <main id="example">
        {snapshot ? (
          <>
            <div className="reader-heading">
              <h1>{snapshot.title}</h1>
              <p>{snapshot.purpose}</p>
              <div className="reader-meta">
                <span>
                  <Icon name="book" size={16} />
                  {snapshot.steps.length}{" "}
                  {countNoun(snapshot.steps.length, ["шаг", "шага", "шагов"])}
                </span>
                <span>
                  <Icon name="clock" size={16} />
                  Видео {time(snapshot.source.duration_ms)}
                </span>
              </div>
            </div>
            <CookingGuide snapshot={snapshot} />
            <footer className="public-footer">
              <a
                href="https://www.pexels.com/video/person-chopping-vegetables-7009957/"
                target="_blank"
                rel="noreferrer"
              >
                Автор видео — Taryn Elliott / Pexels
              </a>
              <a href="/">На главную</a>
            </footer>
          </>
        ) : error ? (
          <ErrorBanner error="Не удалось открыть пример рецепта.">
            <button
              className="text-button"
              onClick={() => {
                setError(false);
                setAttempt((n) => n + 1);
              }}
            >
              Попробовать ещё раз
            </button>
          </ErrorBanner>
        ) : (
          <Loading label="Открываем пример рецепта…" />
        )}
      </main>
    </div>
  );
}
