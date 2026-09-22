import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";

import { chromium } from "playwright";

const repository = path.resolve(import.meta.dirname, "..");
const sourcePath = path.join(repository, "src");
const pythonPath = process.env.E2E_INSTALLED === "1"
  ? (process.env.PYTHONPATH ?? "")
  : [sourcePath, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
const temporary = await mkdtemp(path.join(os.tmpdir(), "codex-quota-system-e2e-"));
const stateDir = path.join(temporary, "state");
await mkdir(stateDir);

const now = new Date();
const reset = new Date(now.getTime() + 7 * 86400000);
const start = new Date(reset.getTime() - 7 * 86400000);
const iso = value => new Date(value).toISOString();
const actual = Array.from({ length: 29 }, (_, index) => ({
  time: iso(start.getTime() + index * 6 * 3600000),
  used_percent: Number((10 + index * (23.55 / 28)).toFixed(2)),
}));
actual[actual.length - 1] = { time: now.toISOString(), used_percent: 33.55 };
const future = Array.from({ length: 15 }, (_, index) => {
  const expected = 33.61 + index * (36.39 / 14);
  return {
    time: iso(now.getTime() + index * ((reset.getTime() - now.getTime()) / 14)),
    expected_used_pp: expected,
    compatibility_used_pp: {
      lower_used_pp: expected - 1.2,
      upper_used_pp: expected + 1.4,
      range_kind: "alignment_sensitivity_box",
    },
  };
});
const m2 = {
  version: "quota-forecast-v2-m2-live-v2",
  status: "approximate",
  strict_status: "infeasible",
  fit_error: { max_constraint_slack_pp: 0.35 },
  current_cycle: {
    current: {
      time: now.toISOString(),
      actual_used_pp: 33.55,
      explained_used_pp: 33.61,
      explained_lower_pp: 33.55,
      explained_upper_pp: 33.61,
      fit_residual_pp: -0.06,
    },
  },
  model_parameters: [{
    model: "gpt-example",
    channels: {
      uncached_input: { reference: 0.55, lower: 0.3, upper: 0.8 },
      cached_input: { reference: 0.075, lower: 0.05, upper: 0.1 },
      output: { reference: 4, lower: 2, upper: 6 },
    },
  }],
};
const adaptive = {
  status: "observed",
  latest: {
    observed_at: now.toISOString(),
    used_percent: 33.55,
    remaining_percent: 66.45,
    resets_at: reset.toISOString(),
    window_minutes: 10080,
    plan_type: "example",
    limit_id: "codex:primary",
  },
  actual,
  boundaries: [],
};
const taskForecast = {
  status: "conditional",
  issued_at: now.toISOString(),
  as_of: now.toISOString(),
  reset_at: reset.toISOString(),
  points: future.map(point => ({
    time: point.time,
    median: point.expected_used_pp,
    lower: point.compatibility_used_pp.lower_used_pp,
    upper: point.compatibility_used_pp.upper_used_pp,
  })),
  tasks: [],
  minute_history: [],
  thread_distribution: { status: "unavailable", coverage: "unavailable", points: [] },
  scheduled: { jobs: [] },
  warning: "合成验收夹具；不是预测精度证据。",
};
const snapshot = {
  schema: "codex-quota-system-snapshot-v1",
  schema_version: 1,
  snapshot_id: "synthetic-e2e-snapshot",
  generated_at: now.toISOString(),
  timezone: "UTC",
  system_state: "locally_validated",
  runtime: { monitoring_enabled: true, local_fitting_enabled: true, bootstrap_mode: "bundled", retention_days: 35 },
  forecast_v2: { m1: adaptive, m2, m3: { status: "conditional", reference_source: "local_m2_explanation", points: future } },
  adaptive,
  task_forecast: taskForecast,
};
await writeFile(path.join(stateDir, "snapshot.json"), JSON.stringify(snapshot), "utf8");
await writeFile(
  path.join(temporary, "config.toml"),
  `timezone = "UTC"
[state]
directory = "${stateDir.replaceAll("\\", "/")}"
[web]
host = "127.0.0.1"
port = 0
`,
  "utf8",
);

const server = spawn(
  process.env.PYTHON || "python",
  ["-m", "codex_quota_dashboard", "--config", path.join(temporary, "config.toml"), "serve"],
  {
    cwd: repository,
    env: { ...process.env, PYTHONPATH: pythonPath, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
    stdio: ["ignore", "pipe", "pipe"],
  },
);

let stderr = "";
server.stderr.setEncoding("utf8");
server.stderr.on("data", chunk => { stderr += chunk; });
server.stdout.setEncoding("utf8");

const url = await new Promise((resolve, reject) => {
  const timeout = setTimeout(() => reject(new Error(`Server did not start. ${stderr}`)), 15000);
  server.stdout.on("data", chunk => {
    const match = chunk.match(/(http:\/\/[^/]+\/)/);
    if (match) {
      clearTimeout(timeout);
      resolve(match[1]);
    }
  });
  server.once("exit", code => {
    clearTimeout(timeout);
    reject(new Error(`Server exited with ${code}. ${stderr}`));
  });
});

const browser = await chromium.launch({
  channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
  headless: true,
});

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error") pageErrors.push(message.text());
  });
  await page.goto(`${url}#overview`, { waitUntil: "networkidle" });
  await page.getByText("M1–M3 本地系统", { exact: true }).waitFor();
  assert.equal(await page.locator("h1").innerText(), "Codex 额度走势");
  assert.equal(await page.locator("#remainingQuota").innerText(), "66%");
  assert.equal(await page.locator("#explainedRemainingQuota").innerText(), "66.39%");
  assert.equal(await page.locator("#rangePreset").inputValue(), "7d");
  assert.equal(await page.locator("#quotaChart canvas").count(), 1);
  assert.equal(pageErrors.length, 0, pageErrors.join("\n"));

  await page.locator("#rangePreset").selectOption("24h");
  await page.locator("#quotaRangeSummary").filter({ hasText: /—/ }).waitFor();
  await page.getByRole("button", { name: "说明与边界" }).click();
  await page.locator("#view-details:not(.hidden)").waitFor();
  assert.equal(await page.locator("#detailMode").innerText(), "本地解释已采用");
  assert.match(await page.locator("#m2ParameterRows").innerText(), /gpt-example/);

  await page.setViewportSize({ width: 320, height: 900 });
  await page.getByRole("button", { name: "额度走势" }).click();
  await page.waitForTimeout(300);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 1, `320 px viewport overflows by ${overflow}px`);
  await page.keyboard.press("Tab");
  assert.ok(await page.locator(":focus-visible").count(), "Keyboard focus must be visible");

  if (process.env.SCREENSHOT) {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole("button", { name: "额度走势" }).click();
    await page.locator("#runtimeDetails").evaluate(element => { element.open = false; });
    await page.waitForTimeout(200);
    await page.screenshot({ path: process.env.SCREENSHOT, fullPage: true });
  }
} finally {
  await browser.close();
  server.kill();
  await Promise.race([once(server, "exit"), new Promise(resolve => setTimeout(resolve, 3000))]);
  await rm(temporary, { recursive: true, force: true });
}

console.log(`E2E passed at ${url}`);
