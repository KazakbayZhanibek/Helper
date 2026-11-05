import assert from "node:assert/strict";
import test from "node:test";
import {
  analysisBudgetProblem,
  baselineCalls,
  parseUsdMicros,
  visualAnalysisCopy,
  visualApplyProblem,
} from "../src/visual.ts";
import type { VisualBudgetCapability, VisualProposal } from "../src/types.ts";

const budget: VisualBudgetCapability = {
  mode: "usd",
  currency: "USD",
  default_max_calls: 30,
  max_calls: 60,
  default_max_cost_microusd: 2_000_000,
  max_cost_microusd: 5_000_000,
  call_upper_bound_microusd: 72_696,
  pricing_version: "test",
};

test("full analysis includes the final short window and window scope is one overview", () => {
  assert.equal(baselineCalls(60_001, "full"), 3);
  assert.equal(baselineCalls(600_000, "full"), 20);
  assert.equal(baselineCalls(60_000, "window"), 1);
  assert.equal(baselineCalls(Number.NaN, "full"), 0);
});

test("same-response verification promises no extra passes even when the call limit has spare capacity", () => {
  const minimum = baselineCalls(70_000, "full");
  assert.equal(minimum, 3);
  const current = visualAnalysisCopy(false, minimum, 30);
  assert.match(current.description, /без дополнительных обращений к ИИ/);
  assert.match(current.extraCalls, /Дополнительные обращения не запланированы/);
  assert.match(current.extraCalls, /проверяет человек/);
  assert.doesNotMatch(current.description + current.extraCalls, /уточняет неясные|Ещё до 27/);
  assert.match(visualAnalysisCopy(true, minimum, 30).extraCalls, /Ещё до 27/);
  assert.equal(visualAnalysisCopy(undefined, minimum, 30).extraCalls, visualAnalysisCopy(true, minimum, 30).extraCalls);
  // Only copy changes: the minimum overview reserve is still required.
  assert.equal(analysisBudgetProblem(minimum, 30, 2_000_000, budget), "");
  assert.match(analysisBudgetProblem(minimum, 2, 2_000_000, budget), /нужно 3 обращений/);
});

test("money input is exact microdollars and rejects unsupported representations", () => {
  assert.equal(parseUsdMicros("1.000001"), 1_000_001);
  assert.equal(parseUsdMicros(" 2,34 "), 2_340_000);
  assert.equal(parseUsdMicros("0"), 0);
  for (const value of [
    "",
    "1e3",
    "-1",
    "0.0000001",
    "Infinity",
    "9007199254740992",
    "1.2.3",
  ])
    assert.equal(parseUsdMicros(value), null, value);
});

test("not enough overview calls, fractional counts and server cap violations block start", () => {
  assert.match(
    analysisBudgetProblem(20, 19, 2_000_000, budget),
    /20 обращений/,
  );
  assert.match(analysisBudgetProblem(1, 2.5, 2_000_000, budget), /целое/);
  assert.match(analysisBudgetProblem(1, 61, 2_000_000, budget), /до 60/);
  assert.equal(analysisBudgetProblem(20, 30, 2_000_000, budget), "");
});

test("API requires the conservative baseline reserve and bounded positive money limit", () => {
  assert.match(
    analysisBudgetProblem(20, 30, 1_000_000, budget),
    /нужен резерв/,
  );
  assert.match(analysisBudgetProblem(1, 30, 0, budget), /больше нуля/);
  assert.match(analysisBudgetProblem(1, 30, 5_000_001, budget), /не выше/);
  assert.equal(analysisBudgetProblem(20, 30, 20 * 72_696, budget), "");
});

test("subscription quota does not invent or require an API dollar balance", () => {
  const subscription = {
    ...budget,
    mode: "subscription_calls" as const,
    currency: null,
    call_upper_bound_microusd: null,
    max_cost_microusd: null,
    default_max_cost_microusd: null,
  };
  assert.equal(analysisBudgetProblem(20, 30, null, subscription), "");
  assert.match(
    analysisBudgetProblem(20, 10, null, subscription),
    /20 обращений/,
  );
  assert.match(
    analysisBudgetProblem(1, 30, null, {
      ...subscription,
      mode: "unavailable",
    }),
    /не настроен/,
  );
});

test("more than thirty observed actions are readable but not silently truncated for applying", () => {
  const proposal = {
    applied_at: null,
    preliminary: false,
    steps: Array.from({ length: 31 }, () => ({})),
  } as VisualProposal;
  assert.match(visualApplyProblem(proposal, 1, false), /сохранён целиком/);
});
