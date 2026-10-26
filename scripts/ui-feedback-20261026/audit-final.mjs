// 最终全页面审计：原生用途提示/原生控件/横向溢出/关键圆角（Lead）
import { launch, api, settle, writeJson, nativeAudit, overflowAudit, measureRadii } from "./lib.mjs";
const OUT = "D:\\qio-dev\\_uf-shots\\audit\\";
const { browser, page, logs } = await launch({ width: 1280, height: 820 });
const results = { pages: {}, checks: {}, logs: [] };
function check(name, ok, detail = "") { results.checks[name] = { ok: Boolean(ok), detail: String(detail) }; console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  [" + detail + "]" : "")); }
async function audit(name) {
  const native = await nativeAudit(page);
  const overflow = await overflowAudit(page);
  const pageOverflow = await page.evaluate(() => ({ docW: document.documentElement.scrollWidth, winW: innerWidth, bodyW: document.body.scrollWidth }));
  results.pages[name] = {
    title: native.titleElements,
    nativeControls: native.nativeControls,
    emojiish: native.emojiish,
    overflow: overflow.slice(0, 10),
    pageOverflow,
  };
  check(name + " 无原生 title 用途提示", native.titleElements.length === 0, JSON.stringify(native.titleElements.slice(0, 4)));
  check(name + " 无默认外观原生控件", native.nativeControls.length === 0, JSON.stringify(native.nativeControls));
  check(name + " 页面不横向溢出", pageOverflow.docW <= pageOverflow.winW + 1, JSON.stringify(pageOverflow));
}
try {
  await page.goto("http://127.0.0.1:5199/#/conversation", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".conversation-workspace"); await settle(page, 2500);
  await audit("conversation");
  await page.goto("http://127.0.0.1:5199/#/settings", { waitUntil: "domcontentloaded" }); await page.waitForTimeout(2000); await audit("settings");
  await page.goto("http://127.0.0.1:5199/#/debug", { waitUntil: "domcontentloaded" }); await page.waitForTimeout(1500); await audit("debug");
  await page.goto("http://127.0.0.1:5199/#/interactive", { waitUntil: "domcontentloaded" }); await page.waitForTimeout(2500); await audit("interactive");
  // 窄窗：对话页 + 设置页不横向溢出
  for (const [name, hash] of [["conversation", "#/conversation"], ["settings", "#/settings"]]) {
    for (const w of [1024, 480]) {
      await page.setViewportSize({ width: w, height: 820 });
      await page.goto("http://127.0.0.1:5199/" + hash, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(1500);
      const o = await page.evaluate(() => ({ docW: document.documentElement.scrollWidth, winW: innerWidth }));
      check(name + " " + w + " 宽不横向溢出", o.docW <= o.winW + 1, JSON.stringify(o));
    }
  }
  await page.setViewportSize({ width: 1280, height: 820 });
  await page.goto("http://127.0.0.1:5199/#/conversation", { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".conversation-workspace"); await settle(page, 2500);
  const radii = await measureRadii(page, [".qio-btn", ".entry-btn", ".qio-badge", ".qio-state", ".qio-input", ".composer .input-pill", ".chrome-btn"]);
  results.radii = radii.slice(0, 40);
  const non14 = radii.filter(r => !/^14px 14px 14px 14px$/.test(r.radius));
  check("对话页关键控件圆角均为 14px（无 999/8/10 等混用）", non14.length === 0, JSON.stringify(non14.slice(0, 6)));
  results.logs = logs.slice(0, 20);
} catch (e) { results.error = String(e && e.stack || e); console.error(results.error); }
finally { writeJson("audit-final", results, OUT); console.log("FAILED:", Object.entries(results.checks).filter(([,v])=>!v.ok).map(([k])=>k).join(" | ") || "none"); await browser.close(); }
