// QIO 全应用提示/通知/弹窗统一改造 · 视觉与 DOM 审计工具（Lead）
// 只依赖本机 Playwright（msedge channel），与产品代码无关。
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
export const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
export const APP = process.env.QIO_APP || "http://127.0.0.1:5199/";
export const API = process.env.QIO_API || "http://127.0.0.1:8734";
export const OUT = process.env.QIO_SHOTS || "D:\\qio-dev\\_uf-shots\\";

export async function launch(viewport = { width: 1280, height: 800 }) {
  const { chromium } = require(PW);
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport, deviceScaleFactor: Number(process.env.QIO_DPR || 1) });
  const logs = [];
  page.on("console", (m) => { if (m.type() === "error") logs.push("console: " + m.text()); });
  page.on("pageerror", (e) => logs.push("pageerror: " + e.message));
  return { browser, page, logs };
}

export function ensureOut(dir = OUT) {
  mkdirSync(dir, { recursive: true });
  return dir;
}

export async function shot(page, name, dir = OUT) {
  ensureOut(dir);
  const path = dir + name + ".png";
  await page.screenshot({ path });
  return path;
}

export async function api(path, method = "GET", body) {
  const resp = await fetch(API + path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let json = null;
  try { json = await resp.json(); } catch { /* 无正文 */ }
  return { status: resp.status, json };
}

/** 测量一组元素的实际圆角（computed）。返回 [{selector, tag, cls, radius}]。 */
export async function measureRadii(page, selectors) {
  return page.evaluate((list) => {
    const out = [];
    for (const selector of list) {
      for (const el of document.querySelectorAll(selector)) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const s = getComputedStyle(el);
        if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) continue;
        out.push({
          selector,
          tag: el.tagName.toLowerCase(),
          cls: String(el.className || "").slice(0, 90),
          radius: [s.borderTopLeftRadius, s.borderTopRightRadius, s.borderBottomRightRadius, s.borderBottomLeftRadius].join(" "),
          size: Math.round(r.width) + "x" + Math.round(r.height),
        });
      }
    }
    return out;
  }, selectors);
}

/** 文字容器中心与所属组件中心的偏差（CSS px）。用于标题/通知中心对称检查。 */
export async function centerOffset(page, parentSelector, childSelector) {
  return page.evaluate(([p, c]) => {
    const parent = document.querySelector(p);
    const child = parent ? parent.querySelector(c) : null;
    if (!parent || !child) return null;
    const pr = parent.getBoundingClientRect();
    const cr = child.getBoundingClientRect();
    return {
      dx: Math.round((cr.left + cr.width / 2 - (pr.left + pr.width / 2)) * 100) / 100,
      dy: Math.round((cr.top + cr.height / 2 - (pr.top + pr.height / 2)) * 100) / 100,
      parent: Math.round(pr.width) + "x" + Math.round(pr.height),
      child: Math.round(cr.width) + "x" + Math.round(cr.height),
      leftPad: Math.round(cr.left - pr.left),
      rightPad: Math.round(pr.right - cr.right),
    };
  }, [parentSelector, childSelector]);
}

/** 原生外观/浏览器默认提示审计：title 用途提示、原生控件、内联 emoji 代理。 */
export async function nativeAudit(page, scope = "body") {
  return page.evaluate((rootSel) => {
    const root = document.querySelector(rootSel) || document.body;
    const out = { titleElements: [], nativeControls: [], emojiish: [] };
    for (const el of root.querySelectorAll("[title]")) {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 && r.height <= 0) continue;
      out.titleElements.push({
        tag: el.tagName.toLowerCase(),
        cls: String(el.className || "").slice(0, 80),
        title: (el.getAttribute("title") || "").slice(0, 120),
        visibleText: (el.textContent || "").trim().slice(0, 80),
      });
    }
    for (const el of root.querySelectorAll("select, input[type=checkbox], input[type=radio], input[type=range], progress, dialog")) {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 && r.height <= 0) continue;
      const s = getComputedStyle(el);
      if (s.display === "none" || s.visibility === "hidden") continue;
      out.nativeControls.push({ tag: el.tagName.toLowerCase(), type: el.getAttribute("type"), cls: String(el.className || "").slice(0, 80) });
    }
    // 粗查交互元素里的 emoji/杂符号（排除正常中文/标点/数学/箭头）
    const emoji = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}]/u;
    for (const el of root.querySelectorAll("button, [role=button], a, summary")) {
      const t = (el.textContent || "").trim();
      if (t && emoji.test(t)) out.emojiish.push({ tag: el.tagName.toLowerCase(), text: t.slice(0, 40) });
    }
    return out;
  }, scope);
}

/** 横向溢出审计：返回所有 scrollWidth 明显大于 clientWidth 的可见元素。 */
export async function overflowAudit(page, scope = "body") {
  return page.evaluate((rootSel) => {
    const root = document.querySelector(rootSel) || document.body;
    const out = [];
    for (const el of [root, ...root.querySelectorAll("*")]) {
      if (!(el instanceof HTMLElement)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const s = getComputedStyle(el);
      if (s.display === "none" || s.visibility === "hidden") continue;
      const overflow = el.scrollWidth - el.clientWidth;
      if (overflow > 2 && s.overflowX !== "hidden" && s.overflowX !== "clip") {
        out.push({
          tag: el.tagName.toLowerCase(),
          cls: String(el.className || "").slice(0, 90),
          overflow,
          clientWidth: el.clientWidth,
          scrollWidth: el.scrollWidth,
        });
      }
    }
    return out.slice(0, 80);
  }, scope);
}

/** 命中性审计：元素中心点是否真的能点到自己（没被上层挡住）。 */
export async function hitTest(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    const target = document.elementFromPoint(x, y);
    return {
      ok: Boolean(target && (target === el || el.contains(target))),
      target: target ? target.tagName.toLowerCase() + "." + String(target.className || "").split(" ")[0] : null,
    };
  }, selector);
}

/** 等进入动效结束：连续两帧 + 指定 ms（截图前用，避免截到动画中间态）。 */
export async function settle(page, ms = 350) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(null)))));
  await page.waitForTimeout(ms);
}

/** 写 JSON 证据文件。 */
export function writeJson(name, data, dir = OUT) {
  ensureOut(dir);
  writeFileSync(dir + name + ".json", JSON.stringify(data, null, 2), "utf8");
  return dir + name + ".json";
}
