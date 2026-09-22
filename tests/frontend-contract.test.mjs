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
  ]) {
    assert.match(html, new RegExp(`id=["']${id}["']`), `missing #${id}`);
  }
  assert.match(script, /api\/quota\/history/);
  assert.match(script, /api\/quota\/runtime/);
  assert.match(script, /ArrowLeft/);
  assert.match(script, /ArrowRight/);
});

test("standalone frontend does not retain unrelated monitor views", () => {
  assert.doesNotMatch(html, /VPS 状态|本机状态/);
  assert.doesNotMatch(script, /function renderVps|function renderHostCharts|function renderUsage/);
});

test("ids are unique and accessibility fallbacks remain present", () => {
  const ids = [...html.matchAll(/\bid=["']([^"']+)["']/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length, "duplicate HTML ids");
  assert.match(html, /tabindex="0"/);
  assert.match(html, /role="status"/);
  assert.match(styles, /prefers-reduced-motion/);
  assert.match(styles, /:focus-visible/);
});
