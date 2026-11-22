import { useEffect, useRef, useState } from "react";
import { RecipeIngredients } from "./RecipeDetails";
import { RecipeStepPhotos } from "./RecipeStepPhotos";
import { ErrorBanner, Icon, Video, time } from "./components";
import { countNoun } from "./format";
import type { Version } from "./types";

export function CookingGuide({
  snapshot,
  orgId,
}: {
  snapshot: Version["snapshot"];
  orgId?: string;
}) {
  const [error, setError] = useState("");
  const [active, setActive] = useState(0);
  const [segment, setSegment] = useState(true);
  const [finished, setFinished] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const stepHeading = useRef<HTMLDivElement>(null);
  const current = snapshot.steps[active];
  useEffect(() => {
    const player = video.current;
    if (!player || !current || !segment || finished) return;
    const start = current.start_ms / 1000;
    const end = current.end_ms / 1000;
    const seekInsideStep = () => {
      if (player.currentTime < start || player.currentTime >= end)
        player.currentTime = start;
    };
    if (player.readyState >= HTMLMediaElement.HAVE_METADATA) seekInsideStep();
    player.addEventListener("loadedmetadata", seekInsideStep);
    player.addEventListener("play", seekInsideStep);
    return () => {
      player.removeEventListener("loadedmetadata", seekInsideStep);
      player.removeEventListener("play", seekInsideStep);
    };
  }, [current, segment, finished]);
  function select(index: number, focus = false) {
    setFinished(false);
    setActive(index);
    if (video.current) {
      video.current.pause();
      if (video.current.readyState >= HTMLMediaElement.HAVE_METADATA)
        video.current.currentTime = snapshot.steps[index].start_ms / 1000;
    }
    if (focus) requestAnimationFrame(() => stepHeading.current?.focus());
  }
  async function play() {
    if (!video.current || !current) return;
    const step = current;
    setSegment(true);
    if (
      video.current.currentTime < step.start_ms / 1000 ||
      video.current.currentTime >= step.end_ms / 1000
    )
      video.current.currentTime = step.start_ms / 1000;
    try {
      await video.current.play();
    } catch {
      setError(
        "Не удалось запустить видео. Попробуйте кнопку воспроизведения в проигрывателе.",
      );
    }
  }
  return (
    <>
      {error && <ErrorBanner error={error} />}
      {snapshot.recipe && (
        <RecipeIngredients recipe={snapshot.recipe} checklist />
      )}
      <div className="reader-grid">
        <section className="reader-video">
          {finished && snapshot.recipe && (
            <div className="cook-complete" role="status">
              <h2>Приготовление завершено</h2>
              <button
                className="button secondary"
                onClick={() => select(0, true)}
              >
                Вернуться к первому шагу
              </button>
            </div>
          )}
          {current && (
            <div className="cook-step-heading" ref={stepHeading} tabIndex={-1}>
              <span className="eyebrow">
                Шаг {active + 1} из {snapshot.steps.length}
              </span>
              <h2>{current.title}</h2>
              <p>{current.description}</p>
            </div>
          )}
          <Video
            source={snapshot.source}
            videoRef={video}
            onTimeUpdate={() => {
              const player = video.current;
              if (!player || !current || player.paused) return;
              const ms = player.currentTime * 1000;
              if (segment && ms >= current.end_ms) player.pause();
              if (!segment && (ms < current.start_ms || ms >= current.end_ms)) {
                const index = snapshot.steps.findIndex(
                  (step) => ms >= step.start_ms && ms < step.end_ms,
                );
                if (index !== -1) setActive(index);
              }
            }}
          />
          <div className="playback-options">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={segment}
                onChange={(e) => setSegment(e.target.checked)}
              />
              <span>Воспроизводить только этот шаг</span>
            </label>
            <span>
              {current && `${time(current.start_ms)}–${time(current.end_ms)}`}
            </span>
          </div>
          {current && (
            <div className="current-step">
              <RecipeStepPhotos
                step={current}
                recipe={!!snapshot.recipe}
                source={snapshot.source}
                orgId={orgId}
                onJump={(ms) => {
                  if (video.current) {
                    video.current.pause();
                    video.current.currentTime = ms / 1000;
                    video.current.scrollIntoView({ block: "nearest" });
                  }
                }}
              />
              <button className="button primary" onClick={() => void play()}>
                <Icon name="play" size={17} />
                Посмотреть этот шаг
                <span className="button-time">
                  {time(current.end_ms - current.start_ms)}
                </span>
              </button>
              <div className="reader-step-nav">
                <button
                  className="text-button"
                  disabled={active === 0}
                  onClick={() => select(active - 1, true)}
                >
                  <Icon name="back" size={16} />
                  Предыдущий шаг
                </button>
                {active === snapshot.steps.length - 1 && snapshot.recipe ? (
                  <button
                    className="button primary"
                    disabled={finished}
                    onClick={() => {
                      video.current?.pause();
                      setFinished(true);
                      requestAnimationFrame(() =>
                        document
                          .querySelector(".cook-complete")
                          ?.scrollIntoView({ block: "center" }),
                      );
                    }}
                  >
                    {finished ? "Завершено" : "Завершить приготовление"}
                  </button>
                ) : (
                  <button
                    className="text-button"
                    disabled={active === snapshot.steps.length - 1}
                    onClick={() => select(active + 1, true)}
                  >
                    Следующий шаг
                    <Icon name="arrow" size={16} />
                  </button>
                )}
              </div>
            </div>
          )}
        </section>
        <aside className="reader-outline">
          <div className="section-heading">
            <h2>{snapshot.recipe ? "Приготовление" : "По шагам"}</h2>
            <span className="small muted">
              {snapshot.steps.length}{" "}
              {countNoun(snapshot.steps.length, [
                "действие",
                "действия",
                "действий",
              ])}
            </span>
          </div>
          <ol>
            {snapshot.steps.map((step, index) => (
              <li key={step.id}>
                <button
                  className={active === index ? "active" : ""}
                  aria-current={active === index ? "step" : undefined}
                  onClick={() => select(index, true)}
                >
                  <span className="outline-number">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <span>
                    <strong>{step.title}</strong>
                    <small>
                      {time(step.start_ms)}–{time(step.end_ms)}
                    </small>
                  </span>
                  {active === index ? <Icon name="play" size={16} /> : null}
                </button>
              </li>
            ))}
          </ol>
          {orgId && (
            <div className="reader-proof">
              <Icon name="shield" />
              <p>
                Опубликованную версию проверил человек. Изменения проходят новую
                проверку.
              </p>
            </div>
          )}
        </aside>
      </div>
    </>
  );
}
