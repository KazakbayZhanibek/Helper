import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { RecipeIngredients } from "./RecipeDetails";
import { canShowStepPhoto, RecipeStepPhotos } from "./RecipeStepPhotos";
import { ErrorBanner, Icon, Video, time } from "./components";
import { countNoun } from "./format";
import {
  emptyCookingProgress,
  hasCookingProgress,
  openCookingProgress,
  type CookingProgress,
} from "./cookingProgress";
import type { Version } from "./types";

type GuideProps = {
  snapshot: Version["snapshot"];
  orgId?: string;
  progressKey?: string;
};

export function CookingGuide(props: GuideProps) {
  const source = props.snapshot.source;
  return (
    <Guide
      key={JSON.stringify([
        props.progressKey,
        source?.id,
        source?.playback_url,
      ])}
      {...props}
    />
  );
}

function Guide({ snapshot, orgId, progressKey }: GuideProps) {
  const [store] = useState(() =>
    openCookingProgress(
      progressKey,
      snapshot.steps.length,
      snapshot.recipe?.ingredients.length || 0,
      () => window.localStorage,
    ),
  );
  const [progress, setProgress] = useState(store.read);
  const [resumed, setResumed] = useState(
    () => !!progressKey && hasCookingProgress(progress),
  );
  const { active, finished } = progress;
  const [error, setError] = useState("");
  const [segment, setSegment] = useState(true);
  const [motion, setMotion] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const pendingSeek = useRef<number | null>(null);
  const playAttempt = useRef(0);
  const stepHeading = useRef<HTMLDivElement>(null);
  function updateProgress(
    change: (previous: CookingProgress) => CookingProgress,
  ) {
    setResumed(false);
    setProgress(store.update(change));
  }
  useEffect(() => {
    if (!progressKey) return;
    const sync = (event: StorageEvent) => {
      if (event.key !== null && event.key !== progressKey) return;
      const next = store.read();
      if (active !== next.active || finished !== next.finished) {
        video.current?.pause();
        playAttempt.current++;
        pendingSeek.current = null;
        setSegment(true);
        setMotion(false);
        setError("");
      }
      setResumed(false);
      setProgress(next);
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, [active, finished, progressKey, store]);
  useEffect(() => {
    const player = video.current;
    if (!player) return;
    const applySeek = () => {
      if (pendingSeek.current !== null)
        player.currentTime = pendingSeek.current;
      pendingSeek.current = null;
    };
    player.addEventListener("loadedmetadata", applySeek);
    return () => {
      playAttempt.current++;
      player.removeEventListener("loadedmetadata", applySeek);
      player.pause();
    };
  }, []);
  const current = snapshot.steps[active];
  const recipe = !!snapshot.recipe;
  const playable =
    snapshot.source?.status === "ready" && !!snapshot.source.playback_url;
  const hasPhoto =
    !!current && canShowStepPhoto(current, recipe, orgId, snapshot.source);
  const showPlayer = !recipe || !hasPhoto || motion;
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
  function seek(ms: number) {
    const player = video.current;
    if (!player) return;
    if (player.readyState >= HTMLMediaElement.HAVE_METADATA) {
      player.currentTime = ms / 1000;
      pendingSeek.current = null;
    } else {
      pendingSeek.current = ms / 1000;
    }
  }
  function pause() {
    playAttempt.current++;
    video.current?.pause();
    setError("");
  }
  function select(index: number, focus = false) {
    pause();
    setMotion(false);
    updateProgress((previous) => ({
      ...previous,
      active: index,
      finished: false,
    }));
    seek(snapshot.steps[index].start_ms);
    if (focus) requestAnimationFrame(() => stepHeading.current?.focus());
  }
  function restart() {
    pause();
    setSegment(true);
    setMotion(false);
    updateProgress(() => emptyCookingProgress);
    if (snapshot.steps.length) seek(snapshot.steps[0].start_ms);
    requestAnimationFrame(() => stepHeading.current?.focus());
  }
  async function play() {
    if (!video.current || !current) return;
    const attempt = ++playAttempt.current;
    const step = current;
    // Reveal the native player within this tap so mobile browsers can start it.
    if (recipe && !motion) flushSync(() => setMotion(true));
    setError("");
    setSegment(true);
    if (
      (recipe && !motion) ||
      video.current.currentTime < step.start_ms / 1000 ||
      video.current.currentTime >= step.end_ms / 1000
    )
      seek(step.start_ms);
    try {
      await video.current.play();
    } catch {
      if (attempt !== playAttempt.current) return;
      setError(
        "Не удалось запустить видео. Попробуйте кнопку воспроизведения в проигрывателе.",
      );
    }
  }
  function jumpToFrame(ms: number) {
    if (!current || !playable) return;
    pause();
    setMotion(true);
    setSegment(ms >= current.start_ms && ms < current.end_ms);
    seek(ms);
    requestAnimationFrame(() => {
      video.current?.focus({ preventScroll: true });
      if (!recipe) video.current?.scrollIntoView({ block: "nearest" });
    });
  }
  return (
    <>
      {error && <ErrorBanner error={error} />}
      {progressKey && hasCookingProgress(progress) && !store.canPersist() && (
        <p role="status" className="field-hint">
          Браузер не смог сохранить прогресс. Отметки доступны, пока эта вкладка
          открыта.
        </p>
      )}
      <div className={recipe ? "reader-grid cook-guide" : "reader-grid"}>
        <section className="reader-video">
          {finished && snapshot.recipe && (
            <div className="cook-complete" role="status">
              <h2>Приготовление завершено</h2>
              <button className="button secondary" onClick={restart}>
                Приготовить ещё раз
              </button>
            </div>
          )}
          {current && (
            <div className="cook-step-heading" ref={stepHeading} tabIndex={-1}>
              <div className="cook-progress-heading">
                <span className="eyebrow">
                  {resumed && !finished ? "Продолжаем с шага" : "Шаг"}{" "}
                  {active + 1} из {snapshot.steps.length}
                </span>
                {recipe && !finished && hasCookingProgress(progress) && (
                  <button
                    type="button"
                    className="text-button"
                    onClick={restart}
                  >
                    Начать сначала
                  </button>
                )}
              </div>
              <h2>{current.title}</h2>
              <p>{current.description}</p>
            </div>
          )}
          <div className="cook-media-video" hidden={!showPlayer}>
            <Video
              source={snapshot.source}
              videoRef={video}
              onTimeUpdate={() => {
                const player = video.current;
                if (!player || !current || player.paused) return;
                const ms = player.currentTime * 1000;
                if (segment && ms >= current.end_ms) player.pause();
                if (
                  !segment &&
                  (ms < current.start_ms || ms >= current.end_ms)
                ) {
                  const index = snapshot.steps.findIndex(
                    (step) => ms >= step.start_ms && ms < step.end_ms,
                  );
                  if (index !== -1 && index !== active) {
                    updateProgress((previous) => ({
                      ...previous,
                      active: index,
                      finished: false,
                    }));
                  }
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
          </div>
          {current && (
            <div className="current-step">
              <div className="cook-step-picture" hidden={recipe && showPlayer}>
                <RecipeStepPhotos
                  key={current.id || active}
                  step={current}
                  recipe={recipe}
                  oneAtATime={recipe}
                  source={snapshot.source}
                  orgId={orgId}
                  onJump={jumpToFrame}
                />
              </div>
              {playable &&
                (recipe && motion && hasPhoto ? (
                  <button
                    className="button secondary cook-motion-button"
                    onClick={() => {
                      pause();
                      setMotion(false);
                    }}
                  >
                    <Icon name="back" size={16} /> Вернуться к фото
                  </button>
                ) : (
                  <button
                    className={
                      recipe
                        ? "button secondary cook-motion-button"
                        : "button primary"
                    }
                    onClick={() => void play()}
                  >
                    <Icon name="play" size={17} />
                    {recipe ? "Показать движение" : "Посмотреть этот шаг"}
                    <span className="button-time">
                      {time(current.end_ms - current.start_ms)}
                    </span>
                  </button>
                ))}
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
                      pause();
                      updateProgress((previous) => ({
                        ...previous,
                        active,
                        finished: true,
                      }));
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
          {snapshot.recipe && (
            <RecipeIngredients
              recipe={snapshot.recipe}
              checklist={{
                prepared: progress.prepared,
                onCheck: (index, checked) =>
                  updateProgress((previous) => {
                    const prepared = previous.prepared.filter(
                      (value) => value !== index,
                    );
                    if (checked) prepared.push(index);
                    return { ...previous, prepared };
                  }),
              }}
            />
          )}
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
