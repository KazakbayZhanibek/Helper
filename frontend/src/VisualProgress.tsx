import type { VisualAnalysisProgress } from "./types";
import { formatMsExact } from "./format";
import { formatUsd } from "./visual";

export function VisualProgress({
  analysis,
  onJump,
}: {
  analysis: VisualAnalysisProgress;
  onJump?: (ms: number) => void;
}) {
  const budget = analysis.budget;
  return (
    <div className="analysis-progress">
      <div className="analysis-progress-heading">
        <strong>
          {analysis.coverage_complete
            ? "Первый просмотр завершён"
            : "Последовательный просмотр"}
        </strong>
        <span>
          {analysis.windows_completed} / {analysis.windows_total} фрагментов
        </span>
      </div>
      <progress
        value={analysis.windows_completed}
        max={Math.max(1, analysis.windows_total)}
        aria-label="Просмотренные фрагменты"
      />
      <p className="field-hint">
        {analysis.scope === "full" ? "Вся запись" : "Выбранный фрагмент"} ·
        подробный пересмотр: {analysis.refinements_completed} /{" "}
        {analysis.refinement_windows} фрагментов.
        {analysis.coverage_complete &&
          " Просмотр по кадрам завершён; полноту действий проверяет мастер."}
      </p>
      {budget && (
        <p className="field-hint">
          Обращения к ИИ: {budget.calls_used} / {budget.max_calls}.
          {budget.mode === "subscription_calls" &&
            " Расходуются лимиты тестовой подписки; стоимость в долларах не рассчитывается."}
          {budget.mode === "free_calls" &&
            " Бесплатный режим OpenRouter. Переход на платную модель отключён."}
          {budget.mode === "usd" && budget.max_cost_microusd !== null && (
            <>
              {" "}
              Расход: {formatUsd(budget.spent_microusd || 0)}, удержано:{" "}
              {formatUsd(budget.reserved_microusd || 0)}. Лимит:{" "}
              {formatUsd(budget.max_cost_microusd)}. Удержание включает ещё не
              подтверждённые расходы.
            </>
          )}
          {budget.unknown_calls > 0 && (
            <>
              {" "}
              Не подтверждён расход {budget.unknown_calls} обращений; их резерв
              сохраняется.
            </>
          )}
        </p>
      )}
      {!analysis.coverage_complete &&
        analysis.unprocessed_ranges.length > 0 && (
          <details className="analysis-unprocessed">
            <summary>
              Ещё не просмотрено: {analysis.unprocessed_ranges.length}{" "}
              фрагментов
            </summary>
            <ul>
              {analysis.unprocessed_ranges.map((range) => (
                <li key={`${range.start_ms}:${range.end_ms}`}>
                  {onJump ? (
                    <button
                      className="time-link"
                      onClick={() => onJump(range.start_ms)}
                    >
                      {formatMsExact(range.start_ms)}–
                      {formatMsExact(range.end_ms)}
                    </button>
                  ) : (
                    <span>
                      {formatMsExact(range.start_ms)}–
                      {formatMsExact(range.end_ms)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </details>
        )}
    </div>
  );
}
