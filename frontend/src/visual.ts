import type {
  VisualAnalysisProgress,
  VisualBudgetCapability,
  VisualProposal,
} from "./types.ts";

/** A rejected model response says nothing about the quality of the recording. */
export function visualValidationFailures(
  analysis?: VisualAnalysisProgress,
): number {
  return (analysis?.windows || []).filter(
    (window) =>
      window.state === "failed" &&
      ["VISUAL_OUTPUT_INVALID", "MODEL_OUTPUT_INVALID"].includes(
        window.error_code || "",
      ),
  ).length;
}

export function visualResultSummary(proposal: VisualProposal): {
  kind:
    | "applied"
    | "validation_failed"
    | "insufficient_evidence"
    | "no_actions"
    | "partial"
    | "ready";
  title: string;
  message: string;
} {
  if (proposal.applied_at)
    return {
      kind: "applied",
      title: "Результат анализа сохранён",
      message: "",
    };
  if (visualValidationFailures(proposal.analysis))
    return {
      kind: "validation_failed",
      title: proposal.steps.length
        ? "Часть ответа ИИ не прошла проверку"
        : "Ответ ИИ не прошёл проверку",
      message: proposal.steps.length
        ? "Для части фрагментов ИИ вернул ответ, который не удалось проверить. Сохранённые шаги доступны для сверки с видео."
        : "ИИ вернул ответ, который не удалось проверить. Шаги для этих фрагментов не созданы. Текущая инструкция сохранена.",
    };
  if (!proposal.steps.length) {
    const uncertain =
      !!proposal.gaps?.length ||
      proposal.warnings.some((warning) =>
        ["insufficient_evidence", "uncertain_action", "occluded_view"].includes(
          warning.code,
        ),
      );
    return uncertain
      ? {
          kind: "insufficient_evidence",
          title: "Шаги требуют уточнения по видео",
          message: proposal.gaps?.length
            ? "ИИ не смог достаточно подтвердить действия. Сохранены неясные моменты и кадры для проверки; шаги можно добавить вручную."
            : "ИИ не смог достаточно подтвердить действия для шагов. Сверьте процесс с записью и при необходимости добавьте шаги вручную.",
        }
      : {
          kind: "no_actions",
          title: "По выбранным кадрам действия не выделены",
          message:
            "Шаги не созданы. Сверьте процесс с записью и при необходимости добавьте шаги вручную.",
        };
  }
  if (proposal.analysis && !proposal.analysis.coverage_complete)
    return {
      kind: "partial",
      title: "Частичный результат сохранён",
      message: "",
    };
  return {
    kind: "ready",
    title:
      proposal.schema_version === "3" &&
      proposal.step_format === "teaching_v1" &&
      !proposal.preliminary
        ? "Черновик инструкции готов"
        : "Предложение по кадрам готово",
    message: "",
  };
}

export function baselineCalls(
  durationMs: number,
  scope: "full" | "window",
): number {
  if (!Number.isFinite(durationMs) || durationMs <= 0) return 0;
  return scope === "full" ? Math.ceil(durationMs / 30_000) : 1;
}

/** Explain the configured plan without changing its call or spending limits. */
export function visualAnalysisCopy(
  automaticRefinements: boolean | undefined,
  minimumCalls: number,
  maxCalls: number,
): { description: string; extraCalls: string } {
  if (automaticRefinements === false) {
    return {
      description:
        "ИИ разбирает видео по кадрам. Приложение проверяет согласованность ответа без дополнительных обращений к ИИ. Неясные места нужно сверить с видео. Звук не обязателен.",
      extraCalls:
        "Дополнительные обращения не запланированы. Неясные моменты проверяет человек.",
    };
  }
  return {
    description:
      "ИИ разбирает видео по кадрам и уточняет неясные моменты в пределах вашего лимита. Звук не обязателен.",
    extraCalls:
      Number.isInteger(maxCalls) && maxCalls >= minimumCalls
        ? `Ещё до ${maxCalls - minimumCalls} — на дополнительные просмотры.`
        : "",
  };
}

/** Accept decimal dollars without floating-point rounding or exponential input. */
export function parseUsdMicros(value: string): number | null {
  const match = /^(\d+)(?:[.,](\d{1,6}))?$/.exec(value.trim());
  if (!match) return null;
  const micros =
    Number(match[1]) * 1_000_000 + Number((match[2] || "").padEnd(6, "0"));
  return Number.isSafeInteger(micros) ? micros : null;
}

export function formatUsd(micros: number): string {
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(micros / 1_000_000);
}

export function analysisBudgetProblem(
  minimumCalls: number,
  maxCalls: number,
  costMicros: number | null,
  budget?: VisualBudgetCapability,
): string {
  if (budget?.mode === "unavailable")
    return "Для этой модели не настроен учёт расходов. Выберите поддерживаемую модель в настройках сервера.";
  if (
    !Number.isInteger(maxCalls) ||
    maxCalls < 1 ||
    maxCalls > (budget?.max_calls || 60)
  )
    return `Укажите целое число обращений от 1 до ${budget?.max_calls || 60}.`;
  if (minimumCalls > maxCalls)
    return `Для первого просмотра нужно ${minimumCalls} обращений. Увеличьте лимит или выберите короткий фрагмент.`;
  if (budget?.mode === "usd") {
    if (
      costMicros === null ||
      costMicros <= 0 ||
      (budget.max_cost_microusd !== null &&
        costMicros > budget.max_cost_microusd)
    )
      return `Укажите лимит расходов больше нуля${budget.max_cost_microusd !== null ? ` и не выше ${formatUsd(budget.max_cost_microusd)}` : ""}.`;
    if (
      budget.call_upper_bound_microusd !== null &&
      minimumCalls * budget.call_upper_bound_microusd > costMicros
    )
      return `Для первого просмотра нужен резерв до ${formatUsd(minimumCalls * budget.call_upper_bound_microusd)}. Увеличьте лимит или выберите короткий фрагмент.`;
  }
  return "";
}

/** A stored result remains readable even when applying it is no longer allowed. */
export function visualApplyProblem(
  proposal: VisualProposal,
  workingRevision: number,
  dirty: boolean,
): string {
  if (proposal.applied_at)
    return "Это предложение уже применено к рабочей версии.";
  if (proposal.preliminary)
    return "Это предварительное наблюдение. Сохраните видео и запросите анализ записи.";
  if (!proposal.steps.length) return visualResultSummary(proposal).message;
  if (proposal.steps.length > 30)
    return "ИИ выделил больше 30 действий. Результат сохранён целиком; перенесите нужные действия вручную или разберите более короткий фрагмент.";
  if (dirty)
    return "В рабочей версии есть несохранённые изменения. Сохраните их и запросите новый анализ.";
  if (proposal.stale || proposal.base_working_revision !== workingRevision)
    return "Рабочая версия или исходные кадры изменились после анализа. Текущие шаги сохранены; для применения нужен новый анализ.";
  if (!proposal.can_apply)
    return "Предложение доступно для просмотра. Применение возможно только к открытой рабочей версии в состоянии черновика.";
  return "";
}

const warningText: Record<string, string> = {
  source_changed: "Расшифровка обновилась. Перепроверьте шаг по видео.",
  insufficient_evidence:
    "В исходной записи недостаточно сведений. Проверьте полноту шага.",
  unclear_speech: "Часть речи распознана неуверенно. Сверьте слова с записью.",
  missing_parameters:
    "В исходной записи могут отсутствовать нужные параметры. Проверьте шаг.",
  approximate_timing:
    "Время определено по отдельным кадрам. Уточните начало и конец действия по видео.",
  sampled_frames:
    "ИИ видел отдельные кадры и мог пропустить движения между ними.",
  uncertain_action:
    "Действие распознано неуверенно. Сверьте его описание с полной записью.",
  occluded_view:
    "Часть действия закрыта от камеры. Проверьте, достаточно ли видимого для этого шага.",
  partial_video:
    "Проанализирован только выбранный фрагмент записи. Проверьте полноту инструкции.",
  overlapping_ranges: "Фрагменты шагов пересекаются. Проверьте их порядок.",
  non_procedural_speech:
    "Речь может не описывать действие. Сверьте шаг с видео.",
  preliminary_live:
    "Предварительное наблюдение камеры требует проверки по сохранённой записи.",
  budget_exhausted:
    "Лимит анализа достигнут. Проверьте оставшиеся фрагменты вручную.",
  refinement_budget_exhausted:
    "Общий просмотр завершён, но лимита не хватило на все уточнения. Сверьте сложные моменты по видео.",
  instruction_step_limit:
    "Получилось больше 30 действий. Выберите нужные действия для инструкции вручную.",
  incomplete_coverage:
    "Часть записи ещё не проанализирована. Проверьте оставшиеся интервалы.",
  budget_limit_reached:
    "Достигнут лимит анализа. Готовые наблюдения сохранены; оставшиеся моменты проверьте вручную.",
  analysis_interrupted:
    "Анализ прерван. Готовые наблюдения сохранены; проверьте оставшиеся моменты.",
  refinement_incomplete:
    "Подробный просмотр неясных моментов завершён не полностью. Сверьте эти действия с записью.",
  refinement_unresolved:
    "Уточнение не дало достаточно надёжных наблюдений. Сохранён первый вариант; проверьте действие по записи.",
};

export function stepWarningMessages(codes: readonly string[] = []): string[] {
  return [
    ...new Set(
      codes.map(
        (code) =>
          warningText[code] ||
          "Проверьте основание этого шага по исходному видео.",
      ),
    ),
  ];
}
