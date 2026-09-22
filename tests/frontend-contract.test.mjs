import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";

const root = path.resolve(import.meta.dirname, "..");
const staticRoot = path.join(root, "src", "codex_quota_dashboard", "static");
const [html, script, styles] = await Promise.all([
  readFile(path.join(staticRoot, "index.html"), "utf8"),
  readFile(path.join(staticRoot, "app.js"), "utf8"),
  readFile(path.join(staticRoot, "styles.css"), "utf8"),
]);

test("homepage carries the complete quota-trend interaction surface", () => {
  for (const id of [
    "remainingQuota",
    "explainedRemainingQuota",
    "explainedRemainingNote",
    "resetCountdown",
    "quotaChart",
    "rangePreset",
    "rangeStartDay",
    "rangeStartMinute",
    "rangeEndDay",
    "rangeEndMinute",
    "quotaBoundaryRows",
    "runtimeDetailRows",
    "scenarioRemaining",
    "scenarioHitting",
    "resetAccountingChart",
    "calibrationRows",
    "budgetModel",
    "calculateBudget",
    "dailyChart",
    "modelChart",
  ]) {
    assert.match(html, new RegExp(`id=["']${id}["']`), `missing #${id}`);
  }
  assert.match(script, /api\/quota\/history/);
  assert.match(script, /api\/quota\/runtime/);
  assert.match(script, /ArrowLeft/);
  assert.match(script, /ArrowRight/);
});

test("standalone frontend exposes only the overview and usage views", () => {
  const views = [...html.matchAll(/data-view=["']([^"']+)["']/g)].map((match) => match[1]);
  assert.deepEqual(views, ["overview", "usage"]);
  assert.doesNotMatch(script, /function renderHostCharts/);
  assert.match(html, /M2 此次重置后的实测对照/);
  assert.match(script, /function renderUsage/);
  assert.match(script, /local_m2_explanation/);
  assert.match(script, /本地 M2 解释 · 自动同构/);
  assert.match(script, /启动参考 · 目标容量/);
  assert.match(styles, /\.reset-accounting \.small-chart \{ height: 705px; \}/);
});

test("overview formula is bound to the current M2 explanation", () => {
  assert.match(script, /current_cycle\?\.current/);
  assert.match(script, /explained_cycle_used_pp\?\?m2Current\.explained_used_pp/);
  assert.match(script, /explained_cycle_lower_pp/);
  assert.match(script, /explained_cycle_upper_pp/);
  assert.match(script, /100-explainedUsed/);
  assert.match(html, /解释预测剩余额度/);
});

test("ids are unique and accessibility fallbacks remain present", () => {
  const ids = [...html.matchAll(/\bid=["']([^"']+)["']/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length, "duplicate HTML ids");
  assert.match(html, /tabindex="0"/);
  assert.match(html, /role="status"/);
  assert.match(styles, /prefers-reduced-motion/);
  assert.match(styles, /:focus-visible/);
});
