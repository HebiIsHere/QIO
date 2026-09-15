// 任务 04 PART B 验收：信息架构、保存模型、可用性说明、主题跟随。
// 用法（服务需在跑：后端 8734、前端 5199）：
//   node scripts/baseline/qa/settings.mjs
// 输出：%TEMP%\qio-baseline\qa\report-settings.json 与 shots\*.png
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199";
const OUT = `${process.env.TEMP}\\qio-baseline\\qa`;
const SHOTS = `${OUT}\\shots`;
mkdirSync(SHOTS, { recursive: true });

const report = { startedAt: new Date().toISOString(), cases: [] };
const rec = (id, title, passed, actual, detail = null) => {
  report.cases.push({ id, title, passed: !!passed, actual: String(actual), detail });
  console.log(`[${passed ? "PASS" : "FAIL"}] ${id} ${title} :: ${actual}`);
};

let seq = Math.floor(Date.now() / 1000) % 100000;
const url = (hash = "#/settings") => `${BASE}/?fresh=${++seq}${hash}`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
await context.addInitScript(() => {
  try {
    if (!localStorage.getItem("qio-theme")) localStorage.setItem("qio-theme", "dark");
  } catch {
    /* 忽略 */
  }
});
const page = await context.newPage();
const tab = (label) => page.locator(".nav .tab", { hasText: label }).first();
const visibleMsg = () => page.locator(".panels .panel:visible .msg").first();

async function main() {
  await page.goto(url());
  await page.waitForSelector(".nav .tab");

  // B1 信息架构：七个一级分组的名称与顺序
  const navLabels = (await page.locator(".nav .tab").allInnerTexts()).map((s) => s.trim());
  const expected = ["外观", "对话与记忆", "模型与联网", "工具与权限", "数据与维护", "凭据", "高级"];
  rec(
    "B1-IA",
    "七个一级分组齐备且顺序稳定",
    JSON.stringify(navLabels) === JSON.stringify(expected),
    `实际=${navLabels.join("/")}`,
  );

  // B1 普通用户第一屏（外观）不出现服务端 tuning 参数
  const appearanceText = await page.locator(".panels .panel:visible").innerText();
  rec(
    "B1-NO-TUNING",
    "「外观」第一屏不出现服务端调参项",
    !/SearXNG|迭代上限|正文字符预算|封块/.test(appearanceText),
    `外观文本片段=${appearanceText.replace(/\s+/g, " ").slice(0, 60)}…`,
  );
  const shotAppearance = `${SHOTS}\\settings-appearance-dark.png`;
  await page.screenshot({ path: shotAppearance });

  // B2 保存模型：自动保存有「保存中…」等待态（用路由延迟制造真实等待）
  await page.route("**/api/settings/maintenance", async (route) => {
    if (route.request().method() === "PUT") await wait(1200);
    await route.continue();
  });
  await tab("数据与维护").click();
  await wait(250);
  const toggle = page.locator("button[aria-label='离线维护开关']");
  const before = await toggle.getAttribute("aria-checked");
  await toggle.click();
  await wait(300);
  const duringText = (await visibleMsg().innerText().catch(() => "")).trim();
  rec(
    "B2-SAVING",
    "自动保存先显示「保存中…」等待态",
    /正在保存|保存中/.test(duringText),
    `等待态文案=${duringText}`,
  );
  await wait(1700);
  const afterText = (await visibleMsg().innerText().catch(() => "")).trim();
  rec(
    "B2-SAVED",
    "保存完成后给出结果（成功或失败都说清楚）",
    /已保存/.test(afterText) || /失败/.test(afterText),
    `完成文案=${afterText}`,
  );
  await toggle.click();
  await wait(1600);
  const restored = await toggle.getAttribute("aria-checked");
  rec("B2-RESTORE", "验收后把维护开关还原（不污染共享后端设置）", restored === before, `原值=${before} 还原后=${restored}`);
  await page.unroute("**/api/settings/maintenance");

  // B2 保存模型可见：自动保存 / 显式保存的界面标注
  await tab("对话与记忆").click();
  await wait(250);
  const chatHints = await page.locator(".panels .panel:visible .mode-hint").allInnerTexts();
  await tab("模型与联网").click();
  await wait(250);
  const modelHints = await page.locator(".panels .panel:visible .mode-hint").allInnerTexts();
  const saveBtn = (await page.locator(".panels .panel:visible button.qio-btn.primary").first().innerText()).trim();
  rec(
    "B2-MODEL",
    "界面上看得出「自动保存」与「需点保存」的区别",
    chatHints.some((t) => t.includes("自动保存")) &&
      modelHints.some((t) => t.includes("保存搜索配置")) &&
      saveBtn.includes("保存搜索配置"),
    `自动保存提示=${chatHints.join("|")} 显式保存提示=${modelHints.join("|")} 按钮=${saveBtn}`,
  );

  // B5 可用性说明：关闭免密钥且无其它通道时，说明为什么不可用
  const availBefore = (await page.locator(".panels .panel:visible .avail").innerText()).trim();
  await page.locator("button[aria-label='免密钥联网搜索']").click();
  await wait(1000);
  const availOffText = (await page.locator(".panels .panel:visible .avail").innerText()).trim();
  const offClass = (await page.locator(".panels .panel:visible .avail").getAttribute("class")) || "";
  rec(
    "B5-UNAVAILABLE",
    "没有任何搜索通道时说明原因（不是只把开关摆着）",
    /没有可用的联网搜索通道/.test(availOffText) && /off/.test(offClass),
    `关闭后提示=${availOffText.slice(0, 70)}`,
  );
  await page.locator("button[aria-label='免密钥联网搜索']").click();
  await wait(1000);
  const availOnText = (await page.locator(".panels .panel:visible .avail").innerText()).trim();
  const onClass = (await page.locator(".panels .panel:visible .avail").getAttribute("class")) || "";
  rec(
    "B5-AVAILABLE",
    "有通道时说明当前用哪条通道；并还原设置",
    /免密钥通道/.test(availOnText) && !/off/.test(onClass),
    `开启后提示=${availOnText.slice(0, 60)}（关闭前=${availBefore.slice(0, 40)}）`,
  );
  const shotModel = `${SHOTS}\\settings-model-dark.png`;
  await page.screenshot({ path: shotModel });

  // B3 高级：SearXNG 等参数默认折叠
  await tab("高级").click();
  await wait(300);
  const fold = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".panels .panel:not([style*='display: none']) details.adv-fold")).map((d) => ({
      open: d.hasAttribute("open"),
      summary: d.querySelector("summary")?.textContent?.trim(),
    })),
  );
  rec(
    "B3-ADVANCED-FOLD",
    "高级里的 SearXNG 参数默认折叠",
    fold.length > 0 && fold.every((f) => f.open === false),
    JSON.stringify(fold),
  );

  // 主题：偏好=系统时，系统主题切换无需刷新即生效
  await tab("外观").click();
  await wait(250);
  await page.locator(".theme-opt", { hasText: "系统" }).click();
  await wait(350);
  await page.emulateMedia({ colorScheme: "light" });
  await wait(600);
  const afterLight = await page.evaluate(() => document.documentElement.dataset.theme);
  await page.emulateMedia({ colorScheme: "dark" });
  await wait(600);
  const afterDark = await page.evaluate(() => document.documentElement.dataset.theme);
  rec(
    "B2-THEME-SYSTEM",
    "偏好=系统时，系统主题切换无需刷新即生效",
    afterLight === "light" && afterDark === "dark",
    `系统浅色→data-theme=${afterLight}，系统深色→data-theme=${afterDark}`,
  );
  await page.locator(".theme-opt", { hasText: "暗紫晶" }).click();
  await wait(300);

  // 浅色截图（净白主题）
  await page.evaluate(() => localStorage.setItem("qio-theme", "light"));
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(url());
  await page.waitForSelector(".nav .tab");
  await wait(400);
  const shotLight = `${SHOTS}\\settings-appearance-light.png`;
  await page.screenshot({ path: shotLight });
  const lightTheme = await page.evaluate(() => document.documentElement.dataset.theme);
  rec("B6-LIGHT", "浅色主题下设置页正常渲染", lightTheme === "light", `data-theme=${lightTheme}`);

  report.shots = [shotAppearance, shotModel, shotLight];
  await context.close();
  await browser.close();
  writeFileSync(`${OUT}\\report-settings.json`, JSON.stringify(report, null, 2), "utf-8");
  const failed = report.cases.filter((c) => !c.passed);
  console.log(`\n合计 ${report.cases.length} 项，失败 ${failed.length} 项。报告：${OUT}\\report-settings.json`);
  process.exit(failed.length ? 1 : 0);
}

await main();
