import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import path from "node:path";
import process from "node:process";

import { chromium } from "playwright";

const repository = path.resolve(import.meta.dirname, "..");
const sourcePath = path.join(repository, "src");
const pythonPath = [sourcePath, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
const server = spawn(
  process.env.PYTHON || "python",
  ["-m", "codex_quota_dashboard", "--mode", "demo", "--port", "0"],
  {
    cwd: repository,
    env: { ...process.env, PYTHONPATH: pythonPath, PYTHONUTF8: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  },
);

let stderr = "";
server.stderr.setEncoding("utf8");
server.stderr.on("data", (chunk) => { stderr += chunk; });
server.stdout.setEncoding("utf8");

const url = await new Promise((resolve, reject) => {
  const timeout = setTimeout(() => reject(new Error(`Server did not start. ${stderr}`)), 10000);
  server.stdout.on("data", (chunk) => {
    const match = chunk.match(/(http:\/\/[^/]+\/)/);
    if (match) {
      clearTimeout(timeout);
      resolve(match[1]);
    }
  });
  server.once("exit", (code) => {
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
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") pageErrors.push(message.text());
  });
  await page.goto(`${url}#overview`, { waitUntil: "networkidle" });
  await page.getByText("合成演示", { exact: true }).waitFor();
  assert.equal(await page.locator("h1").innerText(), "Codex 额度走势");
  assert.match(await page.locator("#remainingQuota").innerText(), /%/);
  assert.equal(await page.locator("#quotaChart canvas").count(), 1);
  assert.ok(await page.locator("#compositionLegend span").count() >= 2);
  assert.equal(pageErrors.length, 0, pageErrors.join("\n"));

  await page.locator("#rangePreset").selectOption("24h");
  await page.locator("#quotaRangeSummary").filter({ hasText: /—/ }).waitFor();
  await page.getByText("查看指针时刻的线程明细", { exact: true }).click();
  await page.locator("#quotaChart").focus();
  await page.keyboard.press("ArrowRight");
  await page.locator("#runtimeDetailTime").filter({ hasText: /已证实/ }).waitFor();

  await page.getByRole("button", { name: "说明与边界" }).click();
  await page.locator("#view-details:not(.hidden)").waitFor();
  assert.equal(await page.locator("#detailMode").innerText(), "合成演示");

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
    await page.locator("#runtimeDetails").evaluate((element) => { element.open = false; });
    await page.waitForTimeout(200);
    await page.screenshot({ path: process.env.SCREENSHOT, fullPage: true });
  }
} finally {
  await browser.close();
  server.kill();
  await Promise.race([once(server, "exit"), new Promise((resolve) => setTimeout(resolve, 3000))]);
}

console.log(`E2E passed at ${url}`);
