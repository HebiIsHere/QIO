// 反馈场景展示页截图 + 几何复核（视觉验收证据）
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);
const OUT = "D:\\qio-dev\\_uf-shots\\showcase\\";
mkdirSync(OUT, { recursive: true });
const BASE = process.env.QIO_SHOWCASE || "http://127.0.0.1:5199/visual/feedback-showcase.html";
const results = { base: BASE, checks: [], shots: [], logs: [] };
function check(name, ok, detail = "") { results.checks.push({ name, ok: Boolean(ok), detail }); console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  [" + detail + "]" : "")); }
const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 820 } });
  page.on("console", (m) => { if (m.type() === "error") results.logs.push(m.text()); });
  page.on("pageerror", (e) => results.logs.push(String(e.message)));
  const open = async (scene, theme) => {
    await page.goto(BASE + "?scene=" + scene + "&theme=" + theme, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(900);
  };
  const shot = async (name) => { const path = OUT + name + ".png"; await page.screenshot({ path }); results.shots.push(name); return path; };

  await open("batch-restore", "dark");
  const dlg = await page.evaluate(() => {
    const el = document.querySelector('[role="dialog"][aria-modal="true"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const title = el.querySelector("h2, .mrd-title, .title");
    const tr = title?.getBoundingClientRect();
    const actions = [...el.querySelectorAll(".mrd-actions button, footer button, .actions button")].map(b => { const br = b.getBoundingClientRect(); return { w: Math.round(br.width), h: Math.round(br.height) }; });
    return {
      radius: getComputedStyle(el).borderTopLeftRadius,
      width: Math.round(r.width), height: Math.round(r.height),
      centerDx: tr ? Math.round((tr.left + tr.width / 2 - (r.left + r.width / 2)) * 100) / 100 : null,
      actions,
      text: el.innerText.slice(0, 400),
    };
  });
  check("整批撤回中央弹窗存在", Boolean(dlg), JSON.stringify(dlg && { w: dlg.width, h: dlg.height }));
  check("整批撤回弹窗 14px 圆角", dlg?.radius === "14px", String(dlg?.radius));
  check("整批撤回标题居中（偏差≤1px）", dlg?.centerDx !== null && Math.abs(dlg.centerDx) <= 1, String(dlg?.centerDx));
  check("展示可恢复/不可恢复语义", /可恢复/.test(dlg?.text || "") && /不可安全恢复/.test(dlg?.text || ""), (dlg?.text || "").slice(0, 80));
  await shot("showcase-01-batch-restore-dark-1280");
  // 展开修改前后内容
  const expand = await page.$(".mrd-item button, .item button, summary");
  if (expand) { await expand.click(); await page.waitForTimeout(350); }
  await shot("showcase-02-batch-restore-expanded-dark-1280");
  for (const w of [1024, 480, 320]) {
    await page.setViewportSize({ width: w, height: 820 });
    await page.waitForTimeout(500);
    const overflow = await page.evaluate(() => {
      const el = document.querySelector('[role="dialog"][aria-modal="true"]');
      if (!el) return null;
      return { scrollW: el.scrollWidth, clientW: el.clientWidth, right: Math.round(el.getBoundingClientRect().right), vw: innerWidth };
    });
    check("整批撤回 " + w + " 宽不横向溢出", overflow ? overflow.scrollW <= overflow.clientW + 1 && overflow.right <= overflow.vw : false, JSON.stringify(overflow));
    await shot("showcase-03-batch-restore-" + w);
  }
  await page.setViewportSize({ width: 1280, height: 820 });
  await open("batch-restore", "light");
  await shot("showcase-04-batch-restore-light-1280");

  await open("boot-slow", "dark");
  const slowText = await page.evaluate(() => document.body.innerText);
  check("启动 >10 秒显示已等待秒数", /已等待 12 秒/.test(slowText), slowText.slice(0, 80));
  check("技术细节默认折叠", !/QIO\.log/.test(slowText), slowText.slice(0, 80));
  await shot("showcase-05-boot-slow-dark");
  await open("boot-failed", "dark");
  const failText = await page.evaluate(() => document.body.innerText);
  check("启动失败显示真实原因与重试", /没有启动完成/.test(failText) && /失败/.test(failText) && /重试/.test(failText), failText.slice(0, 100));
  await shot("showcase-06-boot-failed-dark");
} catch (e) { results.error = String(e && e.stack || e); console.error(results.error); }
finally {
  results.checks.forEach(() => undefined);
  writeFileSync(OUT + "showcase-results.json", JSON.stringify(results, null, 2), "utf8");
  console.log("FAILED:", results.checks.filter(c=>!c.ok).map(c=>c.name).join(" | ") || "none");
  await browser.close();
}
