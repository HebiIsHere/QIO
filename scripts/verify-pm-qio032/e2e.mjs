// PM-QIO032 互动模式修复组 —— E2E 验收入口（正常界面入口 + fake provider 台账）。
//
// 浏览器层：见 ./cdp.mjs（本机没有 Playwright 包，用系统 Edge + CDP 顶替；原因写在 cdp.mjs 头注释）。
//
// 跑法：
//   node scripts/verify-pm-qio032/e2e.mjs                 # 全部场景
//   node scripts/verify-pm-qio032/e2e.mjs --only=s1       # 单场景（独立可跑）
//
// 环境变量：
//   QIO_E2E_BASE     默认 http://127.0.0.1:5199
//   QIO_E2E_API      默认 http://127.0.0.1:8734
//   QIO_E2E_PROVIDER 默认 http://127.0.0.1:8807
//   QIO_E2E_OUT      默认 _lead-logs/pm-qio032/e2e
//   QIO_E2E_FILES    测试材料目录（放 TXT / PNG；默认 <out>/materials）
//
// 纪律：不改产品代码；所有断言读真实 UI 状态 / 真实 provider 台账；
//      唯一的「模拟故障」是 page.failRequests（标注 [模拟]），等价 Playwright 的 route.abort。
import { CdpBrowser, Page, sleep } from "./cdp.mjs";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");
const BASE = process.env.QIO_E2E_BASE || "http://127.0.0.1:5199";
const API = process.env.QIO_E2E_API || "http://127.0.0.1:8734";
const PROVIDER = process.env.QIO_E2E_PROVIDER || "http://127.0.0.1:8807";
const OUT = resolve(process.env.QIO_E2E_OUT || join(ROOT, "_lead-logs", "pm-qio032", "e2e"));
const SHOTS = join(OUT, "shots");
const MATERIALS = process.env.QIO_E2E_FILES || join(OUT, "materials");
mkdirSync(SHOTS, { recursive: true });
mkdirSync(MATERIALS, { recursive: true });

const RUN_ID = process.env.QIO_E2E_RUN_ID || new Date().toISOString().replace(/[-:T.]/g, "").slice(0, 14);
const results = [];
const consoleErrors = [];
let page = null;
let stepNo = 0;

function record(name, ok, detail) {
  results.push({ name: name, ok: Boolean(ok), detail: detail === undefined ? null : detail });
  const line = (ok ? "[PASS] " : "[FAIL] ") + name + (detail === undefined ? "" : " :: " + JSON.stringify(detail).slice(0, 900));
  console.log(line);
  return Boolean(ok);
}
function note(text) { console.log("      · " + text); }
async function shot(name) {
  stepNo += 1;
  const file = join(SHOTS, String(stepNo).padStart(2, "0") + "-" + name + ".png");
  await page.screenshot(file);
  return file;
}

// ---------- provider 控制面 ----------
async function providerReset() {
  await fetch(PROVIDER + "/__reset", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
}
async function providerScript(steps) {
  const r = await fetch(PROVIDER + "/__script", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ steps: steps }),
  });
  return r.status;
}
async function providerLog() {
  const r = await fetch(PROVIDER + "/__log");
  const j = await r.json();
  return (j && j.requests) || [];
}
/** 等台账里出现「和本次提交相关」的模型请求（理解是异步的：受理 ≠ 已经调用模型）。 */
async function waitProviderRequests(predicate, { timeout = 30000, step = 500 } = {}) {
  const deadline = Date.now() + timeout;
  let log = [];
  while (Date.now() < deadline) {
    log = await providerLog();
    if (chatRequests(log).some(predicate)) return { log: log, waited: true };
    await sleep(step);
  }
  return { log: log, waited: false };
}
/**
 * 每个请求的 body 拍平成文本。
 * 注意：fake_provider 把原始请求正文放在台账的 _body 上；旧实现优先读 req.body，
 * 而基类台账没有 body/json/payload 字段，于是退化成 JSON.stringify(整个台账条目)——
 * 嵌套字符串里的每个双引号又被 JSON 再转义一层，/"image_url"/ 这类带引号的模式永远匹配不到。
 * 上一轮 ①-9 的 image_unsupported 有一半来自这个取数 bug。这里优先读 _body。
 */
function bodyText(req) {
  const b = req && (req._body ?? req.body ?? req.json ?? req.payload);
  if (typeof b === "string") return b;
  if (b && typeof b === "object") { try { return JSON.stringify(b); } catch { return ""; } }
  try { return JSON.stringify(req ?? ""); } catch { return ""; }
}
/** 等后端主循环把这次提交的轮次跑完（提交受理与理解是异步的，台账断言前必须等它真的发请求）。 */
async function waitTurnsIdle({ timeout = 120000, step = 500 } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      const snap = await (await fetch(API + "/api/turns/queue")).json();
      if (!snap.running && !(snap.queued || []).length) return true;
    } catch { /* 后端重启中：继续等 */ }
    await sleep(step);
  }
  return false;
}
function chatRequests(log) {
  return log.filter((r) => String(r.path || r.url || "").includes("chat/completions"));
}
/** 取出 role=tool 的消息正文：工具结果回灌的那一轮请求才带它。 */
function toolMessages(req) {
  let parsed = null;
  try { parsed = JSON.parse(bodyText(req)); } catch { return []; }
  const messages = (parsed && parsed.messages) || [];
  return messages
    .filter((m) => m && (m.role === "tool" || m.role === "function"))
    .map((m) => {
      if (typeof m.content === "string") return m.content;
      try { return JSON.stringify(m.content); } catch { return ""; }
    });
}

// ---------- 应用层操作（全部走正常界面入口） ----------
async function waitAppReady(timeout = 45000) {
  await page.waitFor("document.querySelector('.stream') && document.querySelector('#composer-mode-switch .mode-btn')", { timeout: timeout, label: "对话页就绪" });
  await sleep(600);
}
async function enterInteractive() {
  // 正常入口：对话页输入区的模式切换按钮（不直接改 hash）
  const before = await page.attr("#composer-mode-switch .mode-btn", "data-current-mode");
  await page.click("#composer-mode-switch .mode-btn");
  await page.waitFor("document.querySelector('[data-im=\"board\"]')", { timeout: 30000, label: "互动板面" });
  await sleep(1200);
  const after = await page.attr("#composer-mode-switch .mode-btn", "data-current-mode");
  const hash = await page.eval("location.hash");
  return { before: before, after: after, hash: hash };
}
async function ensureInteractive() {
  if (await page.exists('[data-im=\"board\"]')) return { already: true };
  return enterInteractive();
}
async function backToConversation() {
  await page.click("#composer-mode-switch .mode-btn");
  await page.waitFor("!document.querySelector('[data-im=\"board\"]')", { timeout: 20000, label: "回到对话页" });
  await sleep(800);
}
async function cardCount() { return page.count('[data-im="card"]'); }
async function cardIndexByHook(hookName) {
  // 返回含该 data-im 的卡片序号（从 0 开始），找不到 -1
  return page.eval("(function(){ const cards=[...document.querySelectorAll('[data-im=\"card\"]')];" +
    "for (let i=0;i<cards.length;i++) if (cards[i].querySelector('[data-im=" + JSON.stringify(hookName) + "]')) return i;" +
    "return -1; })()");
}
async function openAddMenu(kind) {
  const before = await cardCount();
  await page.click('[data-im="add-menu"]');
  await page.waitFor("document.querySelector('[data-im=\"add-menu-list\"]')", { timeout: 8000, label: "添加菜单" });
  if (kind === "file") {
    const chooser = page.setFilesOnChooser(pendingFiles.shift(), { timeout: 20000 });
    await page.click('[data-im="add-file"]');
    const params = await chooser;
    note("文件选择器已注入：" + JSON.stringify(params));
  } else {
    await page.click('[data-im="add-text"]');
  }
  await page.waitFor("document.querySelectorAll('[data-im=\"card\"]').length > " + before, { timeout: 15000, label: "新卡片出现" });
  await sleep(600);
  return { before: before, after: await cardCount() };
}
let pendingFiles = [];
async function addFileCard(paths) {
  pendingFiles.push(paths);
  return openAddMenu("file");
}
/** 编辑最后一张文字卡：点卡片 → 进编辑 → 输入 → 完成（Esc 收起编辑器）。 */
async function setTextCardContent(index, text) {
  // 文字卡：先选中（工具栏是 Teleport 单例）→ 工具栏「编辑」→ 卡片编辑器
  await selectCard(index);
  if (!(await page.exists('[data-im="card-editor"]'))) {
    await clickToolbar("card-icon-edit").catch(() => null);
    await sleep(600);
  }
  const editorSel = '[data-im="card-editor"]';
  if (!(await page.exists(editorSel))) return { ok: false, reason: "没有找到卡片编辑器" };
  const target = await page.eval("(function(){ const host=document.querySelector(" + JSON.stringify(editorSel) + "); const t=host && (host.matches('textarea')?host:host.querySelector('textarea,[contenteditable=true]')); return t ? (t.tagName.toLowerCase()==='textarea'?'ta':'ce') : null; })()");
  if (target === "ta") {
    await page.eval("(function(){ const host=document.querySelector(" + JSON.stringify(editorSel) + "); const t=host.matches('textarea')?host:host.querySelector('textarea'); t.focus(); t.select(); return true; })()");
    await page.browser.send("Input.insertText", { text: text }, page.sessionId);
    await page.eval("(function(){ const host=document.querySelector(" + JSON.stringify(editorSel) + "); const t=host.matches('textarea')?host:host.querySelector('textarea'); t.dispatchEvent(new Event('input',{bubbles:true})); return true; })()");
  } else {
    await page.eval("(function(){ const host=document.querySelector(" + JSON.stringify(editorSel) + "); const t=host.querySelector('[contenteditable=true]')||host; t.focus(); document.execCommand('selectAll'); return true; })()");
    await page.browser.send("Input.insertText", { text: text }, page.sessionId);
  }
  await sleep(400);
  // 收起编辑器 = 完成编辑（Esc 在编辑态就是 finishEdit）
  await page.pressKey("Escape");
  await sleep(700);
  if (await page.exists('[data-im="card-editor"]')) {
    await clickToolbar("card-icon-edit").catch(() => null);
    await sleep(500);
  }
  const body = await page.text('[data-im="card"] [data-im="card-body"]', index);
  const editing = await page.exists('[data-im="card-editor"]');
  return { ok: body.includes(text.slice(0, 12)) && !editing, body: body.slice(0, 140), stillEditing: editing };
}
/**
 * #08/#54 三态唯一入口：点卡片上的「显示并参与 / 显示但不参与 / 隐藏且不参与」按钮，
 * 再点菜单里的目标档（真实指针事件，不直接改 store）。
 */
async function setVisibility(index, target) {
  // 用卡片 DOM 内的元素点击（封面式图片卡的浮层会挡住坐标点击，但控件本身仍可点）。
  const before = await page.eval("(function(){ const c=document.querySelectorAll('[data-im=\"card\"]')[" + index + "]; " +
    "const el=c && c.querySelector('[data-im=\"card-visibility\"]'); return el ? el.getAttribute('data-visibility') : null; })()");
  await page.eval("(function(){ const c=document.querySelectorAll('[data-im=\"card\"]')[" + index + "]; " +
    "const el=c && c.querySelector('[data-im=\"card-visibility\"]'); if (el) el.click(); return !!el; })()");
  await sleep(400);
  const opened = await page.eval("(function(){ return document.querySelectorAll('[data-im=\"card-visibility-menu\"]').length; })()");
  await page.eval("(function(){ const el=document.querySelector('[data-im=\"card-visibility-" + target + "\"]'); if (el) el.click(); return !!el; })()");
  await sleep(700);
  const after = await page.eval("(function(){ const c=document.querySelectorAll('[data-im=\"card\"]')[" + index + "]; " +
    "const el=c && c.querySelector('[data-im=\"card-visibility\"]'); return el ? el.getAttribute('data-visibility') : null; })()");
  return { before: before, after: after, menuOpened: opened };
}
async function toggleParticipation(index) {
  return setVisibility(index, "displayed_excluded");
}
/** 读卡片当前三态（included / displayed_excluded / hidden_excluded）。 */
async function cardVisibility(index) {
  return page.eval("(function(){ const c=document.querySelectorAll('[data-im=\"card\"]')[" + index + "]; " +
    "const el=c && c.querySelector('[data-im=\"card-visibility\"]'); return el ? el.getAttribute('data-visibility') : null; })()");
}
/** 把指针移到卡片上（局部工具栏只在悬停/选中时出现）。 */
async function hoverCard(index) {
  const info = await page.box('[data-im="card"]', index);
  if (!info) throw new Error("卡片不存在 #" + index);
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: info.cx, y: info.cy, button: "none", buttons: 0 }, page.sessionId);
  await sleep(350);
  return info;
}
/** 选中卡片（工具栏是 Teleport 到 .card-toolbar-host 的单例，不在卡片内部）。 */
async function selectCard(index) {
  await page.click('[data-im="card"] [data-im="card-body"]', index).catch(async () => { await page.click('[data-im="card"]', index); });
  await sleep(450);
  const cls = await page.attr('[data-im="card"]', "class", index);
  return { selected: /selected/.test(cls || ""), cls: cls };
}
/** 点选中卡片工具栏上的钩子（document 级唯一实例，先确认它挂在工具栏宿主里）。 */
async function clickToolbar(hook) {
  await page.waitFor("document.querySelector('[data-im=\"card-toolbar-host\"] [data-im=" + JSON.stringify(hook) + "]')", { timeout: 8000, label: "工具栏 " + hook });
  const info = await page.box('[data-im="card-toolbar-host"] [data-im="' + hook + '"]', 0);
  if (!info || !info.visible) throw new Error("工具栏控件不可见：" + hook);
  await page.clickAt(info.cx, info.cy, info);
  return info;
}
/** 在指定卡片内部点一个钩子（先悬停，再按真实坐标点）。 */
async function clickInCard(cardIndex, hook) {
  await hoverCard(cardIndex);
  const info = await page.eval("(function(){" +
    "const card = document.querySelectorAll('[data-im=\"card\"]')[" + cardIndex + "];" +
    "if (!card) return null;" +
    "const el = card.querySelector('[data-im=" + JSON.stringify(hook) + "]');" +
    "if (!el) return null;" +
    "const r = el.getBoundingClientRect();" +
    "const st = getComputedStyle(el);" +
    "const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);" +
    "return { x: r.x, y: r.y, width: r.width, height: r.height, cx: r.x + r.width / 2, cy: r.y + r.height / 2," +
    " visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && Number(st.opacity) > 0.05," +
    " hitIsSelf: !!(hit && (hit === el || el.contains(hit))), text: (el.innerText || '').trim().slice(0, 30) };" +
    "})()");
  if (!info || !info.visible) throw new Error("卡片内控件不可见：" + hook + " @" + cardIndex);
  await page.clickAt(info.cx, info.cy, info);
  return info;
}
async function hideCard(index) {
  const before = await page.attr('[data-im="card"]', "class", index);
  const textBefore = await page.eval("(function(){ const c=document.querySelectorAll('[data-im=\"card\"]')[" + index + "]; return c ? c.innerText.replace(/\\s+/g,' ').slice(0,60) : null; })()");
  const vis = await setVisibility(index, "hidden_excluded").catch((e) => ({ before: null, after: null, error: String(e.message) }));
  const after = await page.attr('[data-im="card"]', "class", index);
  const textAfter = await page.eval("(function(){ const c=document.querySelectorAll('[data-im=\"card\"]')[" + index + "]; return c ? c.innerText.replace(/\\s+/g,' ').slice(0,60) : null; })()");
  return { before: before, after: after, visibilityBefore: vis.before, visibilityAfter: vis.after, error: vis.error,
    textBefore: textBefore, textAfter: textAfter, hiddenNow: /hidden/.test(after || "") || vis.after === "hidden_excluded" };
}
async function waitSaved(timeout = 15000) {
  const deadline = Date.now() + timeout;
  let text = "";
  while (Date.now() < deadline) {
    text = await page.text('[data-im="save-status"]').catch(() => "");
    if (/已保存/.test(text)) return text;
    await sleep(250);
  }
  return text;
}
async function clickSubmit() {
  const label = await page.text('[data-im="submit"]');
  await page.click('[data-im="submit"]');
  await sleep(500);
  // 影响预判确认层：出现就按「确认」
  if (await page.exists('[data-im="impact-confirm-submit"]')) {
    await page.click('[data-im="impact-confirm-submit"]').catch(() => null);
    await sleep(400);
  }
  return label;
}
async function waitSubmitSettled(timeout = 90000) {
  const deadline = Date.now() + timeout;
  let last = "";
  while (Date.now() < deadline) {
    last = (await page.text('[data-im="submit-cluster"]').catch(() => "")) || "";
    const label = await page.text('[data-im="submit"]').catch(() => "");
    if (!/正在提交/.test(label) && !/正在保存/.test(last) && (await page.exists('[data-im="understanding"]') || /已提交|提交失败|没有可提交|未重复提交/.test(last))) {
      await sleep(600);
      return { text: last.slice(0, 400), label: label };
    }
    await sleep(500);
  }
  return { text: last.slice(0, 400), label: await page.text('[data-im="submit"]').catch(() => "") };
}

// ---------- 场景 ----------

/**
 * ① 首轮交付：参与项（文字卡 / TXT 文件卡 / PNG 图片卡）+ 对照卡（「显示但不参与」/「隐藏且不参与」）
 * + 三态参与 + 提交，用 provider 台账断言「参与项真实交付、排除/隐藏/未提交内容不出现」。
 *
 * 设计修正（上一轮 ①-6b 的缺陷）：旧断言把**被设为「显示但不参与」**的 TXT 卡当成交付对象，
 * 与 ①-7「排除项不泄漏」自相矛盾（同一条正文既要求出现又要求不出现）。现在：
 *   - 交付对象 = 参与项 TXT 卡（正文必须进 provider body）+ 参与项 PNG 卡（按本机能力如实 image_unsupported）；
 *   - 排除/隐藏卡只用来做「不泄漏」断言，不再承担交付断言；
 *   - 排除项不泄漏的强度依赖「参与项文件正文确实会交付」：①-6b 失败时在 detail 里如实标注。
 */
async function s1Delivery() {
  const reset = await resetBoard();
  note("板面已重置：" + JSON.stringify(reset));
  const mark = "M1-" + RUN_ID;
  const txtPartBody = "TXT-PART-" + RUN_ID + "-参与项正文";
  const exTxtBody = "TXT-EXCL-" + RUN_ID + "-排除项正文";
  const hiddenBody = "TXT-HIDDEN-" + RUN_ID + "-隐藏项正文";
  const unsubmitted = "TXT-UNSUB-" + RUN_ID + "-未提交";
  const pngPartName = "png-part-" + RUN_ID + ".png";
  const pngHideName = "png-hide-" + RUN_ID + ".png";
  const pngPartPath = join(MATERIALS, pngPartName);
  const pngHidePath = join(MATERIALS, pngHideName);
  // 1x1 纯色 PNG（唯一文件名标记；像素本身用文件名与哈希断言）
  const pngBytes = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
  writeFileSync(pngPartPath, pngBytes);
  writeFileSync(pngHidePath, pngBytes);
  const txtPartName = "txt-part-" + RUN_ID + ".txt";
  const txtExclName = "txt-excl-" + RUN_ID + ".txt";
  const txtPartPath = join(MATERIALS, txtPartName);
  const txtExclPath = join(MATERIALS, txtExclName);
  writeFileSync(txtPartPath, txtPartBody + "\n", "utf8");
  writeFileSync(txtExclPath, exTxtBody + "\n", "utf8");
  /** 按卡片可见正文里的文件名/标记找卡片序号（-1 = 找不到）。 */
  const findIndexByName = (needle) => page.eval("(function(){ const texts=[...document.querySelectorAll('[data-im=\"card\"]')].map(function(c){return c.innerText||'';}); return texts.findIndex(function(t){return t.indexOf(" + JSON.stringify(needle) + ")>=0;}); })()");

  const entry = await enterInteractive();
  record("①-0 正常入口：对话页模式切换进入互动板面", entry.after === "interactive" && entry.hash.includes("/interactive"), entry);

  // 文字卡（参与项）
  const addedText = await openAddMenu("text");
  const textIndex = addedText.after - 1;
  const edit = await setTextCardContent(textIndex, mark + " 参与项文字");
  record("①-1 文字卡写入唯一标记", edit.ok, edit);

  // 参与项 TXT 文件卡（默认「显示并参与」）—— ①-6b 的交付对象
  const fTxtPart = await addFileCard([txtPartPath]);
  await sleep(1500);
  // 对照：排除项 TXT 文件卡（稍后设为「显示但不参与」）
  const fTxtExcl = await addFileCard([txtExclPath]);
  await sleep(1500);
  const attachState = await page.text('[data-im="card-attach-status"]').catch(() => "");
  record("①-2 上传两张 TXT 文件卡（参与项 + 排除项，正常「添加 → 文件」入口）", fTxtPart.after === fTxtPart.before + 1 && fTxtExcl.after === fTxtExcl.before + 1, { participating: fTxtPart, excluded: fTxtExcl, status: attachState.slice(0, 120) });

  // 参与项 PNG（交付对象）+ 对照：隐藏项 PNG
  const fPngPart = await addFileCard([pngPartPath]);
  await sleep(1800);
  const fPngHide = await addFileCard([pngHidePath]);
  await sleep(1800);
  record("①-3 上传两张 PNG 文件卡（参与项 + 隐藏项）", fPngPart.after === fPngPart.before + 1 && fPngHide.after === fPngHide.before + 1, { participating: fPngPart, hidden: fPngHide });

  const idxTxtPart = await findIndexByName(txtPartName);
  const idxTxtExcl = await findIndexByName(txtExclName);
  const idxPngPart = await findIndexByName(pngPartName);
  const idxPngHide = await findIndexByName(pngHideName);
  note("卡片序号 txt(参与)=" + idxTxtPart + " txt(排除)=" + idxTxtExcl + " png(参与)=" + idxPngPart + " png(隐藏)=" + idxPngHide + " 总卡数=" + (await cardCount()));

  // 排除项文字卡：先隐藏、再恢复显示（恢复默认只回「显示但不参与」，内容绝不能被读到）
  const addedHidden = await openAddMenu("text");
  await setTextCardContent(addedHidden.after - 1, hiddenBody);
  await waitSaved();
  const idxHidden = await findIndexByName(hiddenBody.slice(0, 18));
  const hiddenOn = idxHidden >= 0 ? await hideCard(idxHidden) : { hiddenNow: false };
  const restored = idxHidden >= 0 ? await setVisibility(idxHidden, "displayed_excluded") : { after: null };
  record("①-4a 隐藏后恢复显示默认只回「显示但不参与」", restored.after === "displayed_excluded", { hiddenOn: hiddenOn, restored: restored });

  // 对照卡三态：排除项 TXT「显示但不参与」；隐藏项 PNG「隐藏且不参与」
  const idxTxtExcl2 = await findIndexByName(txtExclName);
  const idxPngHide2 = await findIndexByName(pngHideName);
  const part = idxTxtExcl2 >= 0 ? await toggleParticipation(idxTxtExcl2) : { before: null, after: null };
  record("①-4 对照：排除项 TXT 卡「显示但不参与」", part.after === "displayed_excluded", part);
  const hidden = idxPngHide2 >= 0 ? await hideCard(idxPngHide2) : { hiddenNow: false };
  record("①-5 对照：隐藏项 PNG 卡「隐藏且不参与」", hidden.hiddenNow === true, hidden);
  // 交付对象必须保持参与（否则 ①-6b 的失败原因会被误读成「产品不交付」）
  const partVis = {
    txt: idxTxtPart >= 0 ? await cardVisibility(idxTxtPart) : null,
    png: idxPngPart >= 0 ? await cardVisibility(idxPngPart) : null,
  };
  record("①-5b 交付对象（参与项 TXT / PNG）保持「显示并参与」", idxTxtPart >= 0 && idxPngPart >= 0 && partVis.txt === "included" && partVis.png === "included", { idxTxtPart: idxTxtPart, idxPngPart: idxPngPart, txtVisibility: partVis.txt, pngVisibility: partVis.png });
  await shot("s1-board-three-states");

  // 未提交材料：提交之后再建的卡（其标记绝不该出现在本次请求里）
  const saved = await waitSaved();
  note("保存状态：" + saved);

  await providerReset();
  await providerScript([{ chunks: ["已读到本次互动材料。"], repeat: 8 }]);
  await shot("s1-before-submit");
  const submitLabel = await clickSubmit();
  const settled = await waitSubmitSettled();
  await shot("s1-after-submit");
  // 提交之后（同一轮次受理期间）新建的未提交材料：不在本次请求里
  const addedLate = await openAddMenu("text");
  await setTextCardContent(addedLate.after - 1, unsubmitted);
  const savedAfter = await waitSaved();

  // 受理是同步的、理解是异步的：等主循环真的把这次轮次跑完，再读 provider 台账
  const idle = await waitTurnsIdle({ timeout: 120000 });
  const waited = await waitProviderRequests((req) => bodyText(req).includes(mark), { timeout: 30000 });
  note("轮次空闲=" + idle);
  const log = waited.log;
  const chats = chatRequests(log);
  const hits = chats.filter((r) => bodyText(r).includes(mark));
  const first = hits[0] || chats[0] || null;
  const firstText = first ? bodyText(first) : "";
  const delivery = {
    chatRequests: chats.length,
    requestsWithTextMarker: hits.length,
    provider_log_requests: log.length,
  };
  writeFileSync(join(OUT, "provider-log.json"), JSON.stringify({ runId: RUN_ID, requests: log, s1: { hits: hits.length, firstBodyHead: firstText.slice(0, 4000) } }, null, 2), "utf8");
  record("①-6 提交后首个互动轮次模型请求携带参与项正文", hits.length > 0, delivery);

  // 交付断言只认**参与项**卡：TXT 正文必须进 provider body（文件名出现不算交付）。
  const txtPartDelivered = hits.some((r) => bodyText(r).includes(txtPartBody));
  const txtPartNameSeen = hits.some((r) => bodyText(r).includes(txtPartName));
  const imageBlock = hits.some((r) => /"image_url"/.test(bodyText(r)));
  const pngPartNameSeen = hits.some((r) => bodyText(r).includes(pngPartName));
  // 字节级核对：真实 PNG 的 base64 必须出现在 image_url 的 data URL 里（只出现图片块不算交付）。
  const pngBase64 = pngBytes.toString("base64");
  const imageBytesMatch = hits.some((r) => bodyText(r).includes(pngBase64));
  record("①-6b 参与项 TXT 文件的可读正文进入首轮模型请求（同一正式提交入口）", txtPartDelivered, {
    kind: txtPartDelivered ? "txt_file_body_delivered" : "txt_file_body_not_delivered",
    txtMarkerSeen: txtPartDelivered,
    fileNameSeen: txtPartNameSeen,
    note: txtPartDelivered
      ? "参与项 TXT 正文出现在模型请求里（参与项交付成立）"
      : "参与项 TXT 的正文没有出现在首轮请求里（文件名" + (txtPartNameSeen ? "出现" : "也未出现") + "）；按失败记录，等待 i-material/i-submit 接线后再复跑",
  });
  const imageKind = imageBlock ? (imageBytesMatch ? "image_delivered" : "image_url_mismatch") : "image_unsupported";
  record("①-6c 参与项 PNG 的交付形态：image_url 带真实字节 = 真交付；无视觉链路则如实 image_unsupported", imageBlock ? imageBytesMatch : true, {
    kind: imageKind,
    imageUrlSeen: imageBlock,
    bytesMatch: imageBytesMatch,
    pngNameSeen: pngPartNameSeen,
    note: imageBlock
      ? (imageBytesMatch ? "请求里出现 image_url，且 data URL 的 base64 与本地 PNG 字节一致（字节交付成立）" : "请求里出现 image_url，但字节与本地 PNG 不一致（不得声称交付）")
      : "本机链路没有 image_url：如实记为 image_unsupported，不伪造字节交付",
    submitLabel: submitLabel, settled: settled,
  });
  // 不泄漏断言：排除项 TXT、隐藏项 PNG、排除项文字卡、提交后新建的未提交材料都不得出现。
  const excludedTxtSeen = hits.some((r) => bodyText(r).includes(exTxtBody));
  const excludedTextSeen = hits.some((r) => bodyText(r).includes(hiddenBody));
  const hiddenPngSeen = hits.some((r) => bodyText(r).includes(pngHideName));
  record("①-7 排除项（显示但不参与）正文不出现在模型请求里", hits.length > 0 && !excludedTxtSeen && !excludedTextSeen, {
    excludedTxtMarkerPresent: excludedTxtSeen,
    excludedTextMarkerPresent: excludedTextSeen,
    note: txtPartDelivered ? "参与项 TXT 已交付 → 本项对文件正文有区分力" : "参与项 TXT 正文本轮也未交付 → 本项只能证明文字卡排除，文件正文区分力不足（如实标注）",
  });
  record("①-8 隐藏项（隐藏且不参与）正文/文件名不出现在模型请求里", hits.length > 0 && !excludedTextSeen && !hiddenPngSeen, {
    hiddenTextMarkerPresent: excludedTextSeen,
    hiddenPngNamePresent: hiddenPngSeen,
  });
  record("①-10 提交后新建的未提交材料不进入本次请求", hits.length > 0 && !hits.some((r) => bodyText(r).includes(unsubmitted)), {
    unsubmittedPresent: hits.some((r) => bodyText(r).includes(unsubmitted)),
    saveStatusAfter: (savedAfter || "").slice(0, 40),
  });
  return { mark: mark, txtPartBody: txtPartBody, exTxtBody: exTxtBody, hiddenBody: hiddenBody, unsubmitted: unsubmitted, pngPartName: pngPartName, pngHideName: pngHideName };
}

async function resetBoard() {
  const b = await currentBoard();
  if (!b) return { ok: false, reason: "no-board" };
  const snap = await apiJson("/api/interactive/boards/" + encodeURIComponent(b.id) + "/state");
  const cur = (snap.json && snap.json.state) || {};
  const state = {
    boardId: b.id,
    seq: (snap.json && snap.json.seq) || cur.seq || 0,
    updatedAt: new Date().toISOString(),
    cards: [], groups: [], links: [], selection: [],
    participationVersion: cur.participationVersion || 1,
  };
  const put = await apiJson("/api/interactive/boards/" + encodeURIComponent(b.id) + "/state", { method: "PUT", body: JSON.stringify({ state: state, reason: "e2e-reset" }) });
  // fixture 改了服务端状态，页面内的 store 必须重新加载，否则后续编辑会拿旧版本覆盖它。
  await page.goto(BASE + "/");
  await waitAppReady();
  await page.eval("(function(){ try { Object.keys(localStorage).filter(function(k){return k.indexOf('qio.draft.')===0;}).forEach(function(k){ localStorage.removeItem(k); }); } catch(e) {} return true; })()");
  return { ok: put.status === 200, status: put.status, seq: put.json && put.json.seq, detail: put.status === 200 ? null : put.text.slice(0, 200) };
}
async function apiJson(path, options) {
  const opts = Object.assign({ headers: { "content-type": "application/json" } }, options || {});
  const res = await fetch(API + path, opts);
  const text = await res.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { /* 非 JSON */ }
  return { status: res.status, json: json, text: text };
}
async function currentBoard() {
  const r = await apiJson("/api/interactive/boards");
  const j = r.json || {};
  return (j.boards || j.items || [])[0] || null;
}
async function submissionCount(boardId) {
  if (!boardId) return null;
  const r = await apiJson("/api/interactive/boards/" + encodeURIComponent(boardId) + "/submissions");
  const j = r.json || {};
  return (j.submissions || j.items || []).length;
}
async function waitProviderMark(mark, timeout) {
  const waited = await waitProviderRequests((req) => bodyText(req).includes(mark), { timeout: timeout || 60000 });
  const chats = chatRequests(waited.log);
  return { log: waited.log, chats: chats, hits: chats.filter((req) => bodyText(req).includes(mark)), waited: waited.waited };
}
/** ③ 重复提交 / 延迟回执 / 迟到事件 / 保存成功但草稿清理失败。 */
async function s3Duplicate() {
  const mark = "DUP-" + RUN_ID;
  const reset = await resetBoard();
  note("板面已重置：" + JSON.stringify(reset));
  await ensureInteractive();
  const board = await currentBoard();
  const beforeCount = board ? await submissionCount(board.id) : null;
  const added = await openAddMenu("text");
  const edit = await setTextCardContent(added.after - 1, mark + " 重复提交测试");
  await waitSaved();

  await providerReset();
  await providerScript([{ chunks: ["收到。"], repeat: 6 }]);
  const label1 = await page.text('[data-im="submit"]');
  await page.doubleClick('[data-im="submit"]');
  await sleep(600);
  if (await page.exists('[data-im="impact-confirm-submit"]')) {
    await page.click('[data-im="impact-confirm-submit"]').catch(() => null);
  }
  const settled1 = await waitSubmitSettled(60000);
  const hit1 = await waitProviderMark(mark, 60000);
  await shot("s3-after-double-click");
  const afterCount = board ? await submissionCount(board.id) : null;
  record("③-1 双击提交只执行一次（provider 台账 + 提交记录数）", hit1.hits.length === 1 && (beforeCount === null || afterCount - beforeCount === 1), {
    providerHits: hit1.hits.length, chats: hit1.chats.length, submissionsBefore: beforeCount, submissionsAfter: afterCount,
    label1: label1, settled: settled1.text.replace(/\s+/g, " ").slice(0, 200),
  });

  const cluster = await page.text('[data-im="submit-cluster"]');
  const stateText = cluster.replace(/\s+/g, " ");
  record("③-2 主状态停在真实受理态（不重复执行、不丢原受理记录）", /已提交过|已受理|已提交|已回复|待确认|处理中|已结束/.test(stateText) && !/提交失败/.test(stateText), {
    state: stateText.slice(0, 260), editOk: edit.ok,
  });

  // 再次点提交：同一份内容没有新改动时不得再执行；按钮可点但被门禁/幂等挡住。
  // #80：成功后不改内容再提交 —— submissions 不增，且界面必须说「已提交过 / 没有新的改动」。
  const beforeSecond = board ? await submissionCount(board.id) : null;
  const label2 = await page.text('[data-im="submit"]');
  const disabled2 = await page.eval("document.querySelector('[data-im=\"submit\"]').disabled");
  await page.click('[data-im="submit"]').catch(() => null);
  await sleep(2200);
  const afterSecond = board ? await submissionCount(board.id) : null;
  const clusterAfterSecond = ((await page.text('[data-im="submit-cluster"]').catch(() => "")) || "").replace(/\s+/g, " ");
  const duplicateNote = ((await page.text('[data-im="submit-duplicate-text"]').catch(() => "")) || "").replace(/\s+/g, " ");
  const noNewWorkText = /已提交过|没有新的改动|未重复提交|与上次成功提交内容一致/.test(clusterAfterSecond + " " + duplicateNote);
  await shot("s3-second-submit");
  record("③-3 成功后不改内容再提交：submissions 不增且界面说「已提交过 / 没有新的改动」（#80）", afterSecond === beforeSecond && noNewWorkText, {
    label2: label2,
    disabled: disabled2,
    submissionsBefore: beforeSecond,
    submissionsAfter: afterSecond,
    uiText: (clusterAfterSecond + " " + duplicateNote).slice(0, 240),
    noNewWorkText: noNewWorkText,
  });

  // 延迟回执：把提交 POST 的响应推迟 1.5s（[模拟]），界面必须先说「正在提交」再落到受理态。
  const mark2 = "DUP2-" + RUN_ID;
  const added2 = await openAddMenu("text");
  await setTextCardContent(added2.after - 1, mark2 + " 延迟回执");
  await waitSaved();
  await page.delayRequests((url, method) => method === "POST" && /\/api\/interactive\/boards\/[^/]+\/submissions$/.test(url), 1500);
  const label3 = await page.text('[data-im="submit"]');
  await page.click('[data-im="submit"]');
  await sleep(450);
  const during = await page.eval("(function(){ const b=document.querySelector('[data-im=\"submit\"]'); return {label:(b.innerText||'').trim(), disabled:b.disabled}; })()");
  const settled3 = await waitSubmitSettled(60000);
  const hit3 = await waitProviderMark(mark2, 60000);
  await page.clearFailRequests();
  record("③-4 延迟回执期间先显示「正在提交」，回执到达后落到受理态", during.disabled === true || /正在提交|提交中|正在保存/.test(during.label), {
    simulated: true, beforeLabel: label3, during: during, after: settled3.text.replace(/\s+/g, " ").slice(0, 180), providerHits: hit3.hits.length,
  });

  // 迟到事件：重复投递同一条清单变更事件（[模拟]），不得把已结束轮次倒退成「正在理解」，也不得生成重复卡。
  const beforeStatus = await page.attr('[data-im="understanding-status"]', "data-understanding-status");
  const beforeCards = await cardCount();
  const payload = { board_id: board ? board.id : "", submission_id: "stale-submission", intent_ids: [], statuses: {}, reason: "proposed" };
  for (let i = 0; i < 2; i += 1) {
    await apiJson("/api/events/test?event_type=INTERACTIVE_INTENTS_CHANGED", { method: "POST", body: JSON.stringify(payload) });
  }
  await sleep(1800);
  const afterStatus = await page.attr('[data-im="understanding-status"]', "data-understanding-status");
  const afterCards = await cardCount();
  record("③-5 重复/迟到事件不倒退已结束轮次、不生成重复卡（[模拟]事件）", afterStatus === beforeStatus && afterCards === beforeCards, {
    simulated: true, statusBefore: beforeStatus, statusAfter: afterStatus, cardsBefore: beforeCards, cardsAfter: afterCards,
  });

  // 保存成功但本机草稿清理失败（[模拟] Storage.removeItem 抛错）：保留原文，给一处可操作入口。
  const patch = await page.eval("(function(){ if (window.__draftFailPatched) return true; const orig = Storage.prototype.removeItem; Storage.prototype.removeItem = function(key){ if (String(key).indexOf('qio.draft.') === 0) { window.__draftRemoveFailed = (window.__draftRemoveFailed || 0) + 1; throw new DOMException('模拟：草稿清理失败', 'QuotaExceededError'); } return orig.call(this, key); }; window.__draftFailPatched = true; return true; })()");
  const idxText = await page.eval("(function(){ const cards=[...document.querySelectorAll('[data-im=\"card\"]')]; for (let i=cards.length-1;i>=0;i--) if ((cards[i].innerText||'').includes(" + JSON.stringify(mark) + ")) return i; return -1; })()");
  const editFail = idxText >= 0 ? await setTextCardContent(idxText, mark + " 清理失败后的新正文") : { ok: false, reason: "no-card" };
  await sleep(1200);
  const failUi = await page.eval("(function(){ const sels=['[data-im=\"card-draft-local-removal-error\"]','[data-im=\"card-draft-local-removal-retry\"]','[data-im=\"card-draft-removal-error\"]','[data-im=\"card-draft-error\"]']; const found={}; for(const s of sels) found[s]=document.querySelectorAll(s).length; const card=document.querySelector('[data-im=\"card\"]'); return {found:found, removeFailed:window.__draftRemoveFailed||0, cardText:card?(card.innerText||'').replace(/\\s+/g,' ').slice(0,160):null};})()");
  const cleanupOk = Object.values(failUi.found).some((n) => n > 0) || failUi.removeFailed > 0;
  record("③-6 保存成功但草稿清理失败时保留原文并给可操作入口（[模拟]存储失败）", cleanupOk && editFail.ok, {
    simulated: true, patch: patch, editOk: editFail.ok, observed: failUi,
  });
  await page.eval("(function(){ window.__draftFailPatched = false; return true; })()");
  return { mark: mark, hits: hit1.hits.length };
}

const PANEL_STATE_JS = "(function(){" +
  "const rect=function(el){if(!el)return null;const r=el.getBoundingClientRect();return {x:r.x,y:r.y,left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height,cx:r.x+r.width/2,cy:r.y+r.height/2};};" +
  "const hitSelf=function(el){if(!el)return null;const r=el.getBoundingClientRect();const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return !!(hit&&(hit===el||el.contains(hit)));};" +
  "const root=document.querySelector('.interactive');const rr=root?root.getBoundingClientRect():null;" +
  "const d=document.querySelector('[data-im=\\\"submit-details-box\\\"]');" +
  "const task=document.querySelector('[data-im=\\\"task-center\\\"]');" +
  "const submit=document.querySelector('[data-im=\\\"submit\\\"]');" +
  "const toggle=document.querySelector('[data-im=\\\"submit-details\\\"]');" +
  "return {innerWidth:innerWidth,rootWidth:rr?rr.width:null,space:root?root.getAttribute('data-im-panel-space'):null," +
  "details:rect(d),task:rect(task),submit:rect(submit),detailsHit:hitSelf(d),taskHit:hitSelf(task),submitHit:hitSelf(submit)," +
  "detailsExpanded:toggle?toggle.getAttribute('aria-expanded'):null};" +
  "})()";
async function panelSnapshot() { return page.eval(PANEL_STATE_JS); }
function rectOverlap(a, b) {
  if (!a || !b) return false;
  return !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom);
}
async function openSubmitDetails() {
  if (await page.exists('[data-im="submit-details-box"]')) return;
  await page.click('[data-im="submit-details"]');
  await page.waitFor("document.querySelector('[data-im=\"submit-details-box\"]')", { timeout: 8000, label: "提交详情面板" });
  await sleep(450);
}
async function closePanels() {
  for (let i = 0; i < 3; i += 1) {
    if (!(await page.exists('[data-im=\"submit-details-box\"]')) && !(await page.exists('[data-im=\"task-center\"]'))) return;
    await page.pressKey("Escape");
    await sleep(250);
  }
}
async function openTaskCenter() {
  if (await page.exists('[data-im="task-center"]')) return;
  await page.click('[data-im="tasks-entry"]');
  await page.waitFor("document.querySelector('[data-im=\"task-center\"]')", { timeout: 8000, label: "任务面板" });
  await sleep(450);
}

/** ④ 800×600 单开/组合开 + 1120/1121 实际可用宽度边界、中心与命中。 */
async function s4SmallViewport() {
  const reset = await resetBoard();
  note("板面已重置：" + JSON.stringify(reset));
  await ensureInteractive();
  await closePanels();
  await page.setViewport(800, 600);
  await sleep(900);
  await shot("s4-800x600-board");
  let s = await panelSnapshot();
  record("④-0 800×600 进入窄窗 switch 规则（按实际可用宽度）", s.space === "switch", {
    innerWidth: s.innerWidth, rootWidth: s.rootWidth, space: s.space,
  });

  // 单开提交详情
  await openSubmitDetails();
  s = await panelSnapshot();
  await shot("s4-800-details");
  record("④-1 800×600 单开提交详情：面板完整、中心命中自己、不遮提交按钮", Boolean(s.details) && s.detailsHit === true && s.submitHit === true && !rectOverlap(s.details, s.submit), {
    details: s.details && { x: Math.round(s.details.x), y: Math.round(s.details.y), w: Math.round(s.details.width), h: Math.round(s.details.height) },
    detailsHit: s.detailsHit, submitHit: s.submitHit, overlapSubmit: rectOverlap(s.details, s.submit),
  });

  // 组合开：窄窗必须明确切换（任务打开 → 详情收起），不是互相叠着
  await openTaskCenter();
  s = await panelSnapshot();
  await shot("s4-800-task");
  record("④-2 800×600 组合开：任务打开时提交详情收起（switch，不重叠）", Boolean(s.task) && s.details === null && s.taskHit === true && s.submitHit === true, {
    space: s.space, task: s.task && { x: Math.round(s.task.x), y: Math.round(s.task.y), w: Math.round(s.task.width), h: Math.round(s.task.height) },
    taskHit: s.taskHit, detailsOpen: Boolean(s.details), submitHit: s.submitHit,
  });
  record("④-3 800×600 任务面板不遮提交按钮、中心命中自己", !rectOverlap(s.task, s.submit) && s.submitHit === true, {
    overlapSubmit: rectOverlap(s.task, s.submit), submitHit: s.submitHit,
  });

  // 反向组合：详情打开 → 任务收起
  await openSubmitDetails();
  s = await panelSnapshot();
  record("④-4 800×600 反向组合：详情打开时任务收起（同一时间只留最近打开的一块）", Boolean(s.details) && s.task === null && s.detailsHit === true, {
    detailsOpen: Boolean(s.details), taskOpen: Boolean(s.task), detailsHit: s.detailsHit,
  });
  await page.pressKey("Escape");
  await sleep(500);
  s = await panelSnapshot();
  record("④-5 800×600 Esc 收起最上层非模态面板（提交详情）", s.details === null && s.task === null, { detailsOpen: Boolean(s.details), taskOpen: Boolean(s.task) });

  // 实际边界：contentViewport 宽度 = innerWidth - 2*inset；用 width 差找边界窗口宽度。
  const metrics = await page.eval("(function(){const el=document.querySelector('.interactive'); const r=el.getBoundingClientRect(); return {inner:innerWidth,root:r.width};})()");
  const inset = Math.round(metrics.inner - metrics.root);
  const boundaryWindow = 1121 + inset;
  const at = {};
  for (const w of [boundaryWindow - 1, boundaryWindow]) {
    await page.setViewport(w, 700);
    await sleep(600);
    await closePanels();
    const before = await panelSnapshot();
    await openSubmitDetails();
    await openTaskCenter();
    const after = await panelSnapshot();
    at[w] = {
      inner: after.innerWidth, rootWidth: after.rootWidth, space: after.space,
      details: after.details && Math.round(after.details.width), task: after.task && Math.round(after.task.width),
      detailsHit: after.detailsHit, taskHit: after.taskHit, submitHit: after.submitHit,
      overlapSubmit: rectOverlap(after.task, after.submit) || rectOverlap(after.details, after.submit),
      detailsOpen: Boolean(after.details), taskOpen: Boolean(after.task),
    };
    await shot("s4-boundary-" + w);
  }
  record("④-6 1120/1121 实际边界：边界-1 为 switch，边界为 side-by-side", at[boundaryWindow - 1].space === "switch" && at[boundaryWindow].space === "side-by-side", {
    measuredInset: inset, boundaryWindow: boundaryWindow, at: at,
  });
  record("④-7 边界两侧中心与命中一致，且两块面板/提交按钮互不遮挡", at[boundaryWindow].detailsOpen && at[boundaryWindow].taskOpen && at[boundaryWindow].detailsHit === true && at[boundaryWindow].taskHit === true && at[boundaryWindow].submitHit === true && !at[boundaryWindow].overlapSubmit && at[boundaryWindow - 1].detailsOpen === false && at[boundaryWindow - 1].taskOpen === true && at[boundaryWindow - 1].taskHit === true, {
    sideBySide: at[boundaryWindow], switch: at[boundaryWindow - 1],
  });
  // #05 压力场景：有 5 项未提交改动时，800×600 的详情面板必须钳制（限高/内部滚动），
  // 不能盖住提交按钮，也不能让任务入口点不到。
  await page.setViewport(800, 600);
  await sleep(700);
  await closePanels();
  for (let i = 1; i <= 5; i += 1) {
    const addedChange = await openAddMenu("text");
    await setTextCardContent(addedChange.after - 1, "CHG-" + RUN_ID + "-" + i + " 窄窗改动");
  }
  await waitSaved();
  await openSubmitDetails();
  const s5 = await panelSnapshot();
  const viewportHeight = await page.eval("innerHeight");
  const changeItems = await page.count('[data-im="change-item"]');
  await shot("s4-800-details-5changes");
  let taskOpened = false;
  try {
    await page.click('[data-im="tasks-entry"]');
    taskOpened = await page
      .waitFor("document.querySelector('[data-im=\"task-center\"]')", { timeout: 5000, label: "任务面板" })
      .then(() => true)
      .catch(() => false);
  } catch {
    taskOpened = false;
  }
  await shot("s4-800-task-from-5changes");
  record("④-8 800×600、5 项改动时详情面板钳制、不遮提交按钮、任务入口可点（#05）", changeItems >= 5 && Boolean(s5.details) && s5.details.height <= viewportHeight && s5.detailsHit === true && s5.submitHit === true && !rectOverlap(s5.details, s5.submit) && taskOpened, {
    changeItems: changeItems,
    innerHeight: viewportHeight,
    details: s5.details && { x: Math.round(s5.details.x), y: Math.round(s5.details.y), w: Math.round(s5.details.width), h: Math.round(s5.details.height) },
    detailsHit: s5.detailsHit,
    submitHit: s5.submitHit,
    overlapSubmit: rectOverlap(s5.details, s5.submit),
    taskOpened: taskOpened,
  });

  await page.setViewport(1440, 900);
  await sleep(500);
}

/**
 * ② 提交 A → 改 B → 绑定追问：板面显示 B，板面读工具的 source=submission 返回 A、latest 返回 B。
 * 工具调用由 fake provider 的脚本发出（真实工具执行、真实结果回灌），不是 mock 业务接口。
 */
async function s2SnapshotReads() {
  const reset = await resetBoard();
  note("板面已重置：" + JSON.stringify(reset));
  await ensureInteractive();
  const markA = "AB-A-" + RUN_ID + "-提交版";
  const markB = "AB-B-" + RUN_ID + "-提交后改动";
  const added = await openAddMenu("text");
  const cardIndex = added.after - 1;
  const editA = await setTextCardContent(cardIndex, markA);
  await waitSaved();
  const cardId = await page.eval("(function(){ const c=document.querySelectorAll('[data-im=\"card\"]')[" + cardIndex + "]; return c ? c.getAttribute('data-card-id') : null; })()");
  // 正常提交入口提交 A
  await providerReset();
  await providerScript([{ chunks: ["已收到提交 A。"], repeat: 8 }]);
  const submitLabel = await clickSubmit();
  const settled = await waitSubmitSettled();
  await waitTurnsIdle({ timeout: 120000 });
  // 提交后把同一张卡改成 B（真实编辑 + 保存）
  const editB = await setTextCardContent(cardIndex, markB);
  await waitSaved();
  const boardShowsB = await page.eval("(function(){ return [...document.querySelectorAll('[data-im=\"card\"]')].some(function(c){ return (c.innerText||'').includes(" + JSON.stringify(markB) + "); }); })()");
  const boardShowsA = await page.eval("(function(){ return [...document.querySelectorAll('[data-im=\"card\"]')].some(function(c){ return (c.innerText||'').includes(" + JSON.stringify(markA) + "); }); })()");
  record("②-1 提交 A 后改成 B：板面显示 B（提交快照已不再等于板面）", editA.ok === true && editB.ok === true && boardShowsB === true && boardShowsA === false, {
    cardId: cardId, editA: editA.ok, editB: editB.ok, boardShowsA: boardShowsA, boardShowsB: boardShowsB,
    submitLabel: submitLabel, settled: settled.text.replace(/\s+/g, " ").slice(0, 160),
  });

  // 先发一条普通绑定追问（也用于消化可能的话题标题请求），再发脚本化工具读取。
  await providerReset();
  await providerScript([{ chunks: ["好的，我按你的要求读取。"], repeat: 4 }]);
  await page.focus("#composer-input");
  await page.type("#composer-input", "接下来请按提交快照读一下刚才那张卡");
  await page.pressKey("Enter");
  await waitTurnsIdle({ timeout: 60000 });
  const warmLog = await providerLog();
  const warmBound = chatRequests(warmLog).some((r) => bodyText(r).includes("interactive"));

  const readViaTool = async (source) => {
    const args = JSON.stringify({ card_ids: cardId ? [cardId] : [], source: source, offset: 0, limit: 20 });
    await providerReset();
    await providerScript([
      { tool_chunks: [{ id: "call_read_" + source + "_1", name: "board_read_cards", args_fragments: [args] }] },
      { chunks: ["已按 " + source + " 读取。"], repeat: 4 },
    ]);
    await page.focus("#composer-input");
    await page.type("#composer-input", "请用 board_read_cards（source=" + source + "）读同一张卡");
    await page.pressKey("Enter");
    const waited = await waitProviderRequests((req) => toolMessages(req).some((t) => t.includes(markA) || t.includes(markB)), { timeout: 90000 });
    const withTool = chatRequests(waited.log).filter((r) => toolMessages(r).length > 0);
    const toolText = withTool.map((r) => toolMessages(r).join("\n")).join("\n");
    return {
      waited: waited.waited,
      requests: chatRequests(waited.log).length,
      toolRequests: withTool.length,
      hasA: toolText.includes(markA),
      hasB: toolText.includes(markB),
      toolHead: toolText.replace(/\s+/g, " ").slice(0, 200),
      basis: (() => {
        const m = toolText.match(/\"basis\"\s*:\s*\{[^}]{0,200}\}/);
        return m ? m[0].slice(0, 200) : "";
      })(),
    };
  };

  const subRead = await readViaTool("submission");
  record("②-2 绑定追问：board_read_cards(source=submission) 的工具结果含提交版 A（不含 B）", subRead.waited === true && subRead.toolRequests >= 1 && subRead.hasA === true && subRead.hasB === false, {
    warmBound: warmBound, ...subRead,
  });

  const latestRead = await readViaTool("latest");
  record("②-3 同一工具 source=latest 返回当前板面 B（快照 / 最新两路分开，不静默互换）", latestRead.waited === true && latestRead.toolRequests >= 1 && latestRead.hasB === true && latestRead.hasA === false, {
    ...latestRead,
  });
  return { markA: markA, markB: markB, cardId: cardId };
}

/** ⑤ 路由往返 / 六件拖放（第 6 件不得静默丢失）/ 首件失败重试（[模拟]）。 */
async function s5FilesAndRouting() {
  const reset = await resetBoard();
  note("板面已重置：" + JSON.stringify(reset));
  await ensureInteractive();
  const roundMark = "ROUTE-" + RUN_ID;
  const added = await openAddMenu("text");
  await setTextCardContent(added.after - 1, roundMark + " 路由往返");
  await waitSaved();
  const beforeRoute = await cardCount();
  await backToConversation();
  await enterInteractive();
  await sleep(800);
  const afterRoute = await cardCount();
  const routeText = await page.eval("(function(){ return [...document.querySelectorAll('[data-im=\"card\"]')].some(function(c){ return (c.innerText||'').includes(" + JSON.stringify(roundMark) + "); }); })()");
  record("⑤-1 互动 → 对话 → 互动路由往返后卡片与内容仍在", afterRoute === beforeRoute && routeText === true, { cardsBefore: beforeRoute, cardsAfter: afterRoute, markPresent: routeText });

  // 六件拖放：同一批 5 份上限；批次面板必须逐项列出「已接收 / 未接收」，第 6 份可重新导入（#44/#45）。
  const specs = [];
  for (let i = 1; i <= 6; i += 1) {
    const body = "BATCH-" + RUN_ID + "-" + i + "\n";
    specs.push({ name: "batch-" + RUN_ID + "-" + i + ".txt", type: "text/plain", dataUrl: "data:text/plain;base64," + Buffer.from(body).toString("base64") });
  }
  const batchSnapshot = () => page.eval("(function(){" +
    "const panel=document.querySelector('[data-im=\"add-material-status\"][data-batch-id]');" +
    "const summaryEl=document.querySelector('[data-im=\"add-material-summary\"]');" +
    "const items=[...document.querySelectorAll('[data-im=\"add-batch-item\"]')].map(function(li){ " +
    "const retry=li.querySelector('[data-im=\"card-attach-retry\"]'); const err=li.querySelector('[data-im=\"file-card-error\"]');" +
    "return { name:li.getAttribute('data-name'), status:li.getAttribute('data-status')," +
    " retry: retry ? (retry.innerText||'').trim() : '', error: err ? (err.innerText||'').replace(/\\s+/g,' ').trim() : ''," +
    " text:(li.innerText||'').replace(/\\s+/g,' ').trim().slice(0,120) }; });" +
    "return { panel:!!panel, batchId: panel?panel.getAttribute('data-batch-id'):null," +
    " summary: summaryEl ? (summaryEl.innerText||'').replace(/\\s+/g,' ').trim() : '', items: items };})()");
  const beforeDrop = await cardCount();
  const drop = await page.dragFiles('[data-im="add-menu"]', specs);
  await sleep(6000);
  const afterDrop = await cardCount();
  const batch = await batchSnapshot();
  await shot("s5-six-file-drop");
  const sixthItem = batch.items[5] || null;
  const sixthListed = Boolean(sixthItem) && /-6\.txt$/.test(sixthItem.name || "") && sixthItem.status === "unreceived";
  const summaryOk = /已接收\s*5\s*份/.test(batch.summary) && /未接收\s*1\s*份/.test(batch.summary);
  // 重新导入第 6 件：拖放当时的 File 句柄已不在内存，产品应重新打开文件选择器（正常入口）。
  let reimport = { ok: false };
  if (sixthListed) {
    const chooser = page.setFilesOnChooser([specs[5]], { timeout: 20000 });
    await page.eval("(function(){ const items=[...document.querySelectorAll('[data-im=\"add-batch-item\"]')]; const li=items[5]; " +
      "const b=li&&li.querySelector('[data-im=\"card-attach-retry\"]'); if(b) b.click(); return !!b; })()");
    const chooserInfo = await chooser.then((p) => ({ ok: true, mode: p.mode })).catch((e) => ({ ok: false, error: String(e.message) }));
    await sleep(4500);
    const afterReimport = await cardCount();
    const statuses = batch.items.map((_, i) => i);
    const sixthStatus = await page.eval("(function(){ const li=[...document.querySelectorAll('[data-im=\"add-batch-item\"]')][5]; return li?li.getAttribute('data-status'):null; })()");
    reimport = { ok: afterReimport === afterDrop + 1 && sixthStatus === "succeeded", cardsAfter: afterReimport, sixthStatus: sixthStatus, chooser: chooserInfo };
  }
  record("⑤-2 六件拖放：只接收 5 件、批次清单列出第 6 件「未接收」且可重新导入（#44/#45）", drop.ok === true && drop.count === 6 && afterDrop - beforeDrop === 5 && batch.panel === true && summaryOk && sixthListed && reimport.ok === true, {
    dropped: drop.count, cardsBefore: beforeDrop, cardsAfter: afterDrop,
    batchSummary: batch.summary, itemStatuses: batch.items.map((it) => it.status),
    sixth: sixthItem, sixthListed: sixthListed, reimport: reimport,
  });

  // 首件失败：只让本批第一次上传失败（[模拟]网络故障），失败项要有可见原因与重试入口，后续文件不陪葬。
  let failedOnce = false;
  await page.failRequests((url, method) => {
    if (method === "POST" && url.indexOf("/api/attachments/upload") >= 0 && !failedOnce) { failedOnce = true; return true; }
    return false;
  }, { errorReason: "Failed" });
  const two = [
    { name: "batch-" + RUN_ID + "-7.txt", type: "text/plain", dataUrl: "data:text/plain;base64," + Buffer.from("BATCH7\n").toString("base64") },
    { name: "batch-" + RUN_ID + "-8.txt", type: "text/plain", dataUrl: "data:text/plain;base64," + Buffer.from("BATCH8\n").toString("base64") },
  ];
  const beforeFail = await cardCount();
  const drop2 = await page.dragFiles('[data-im="add-menu"]', two);
  await sleep(6000);
  await page.clearFailRequests();
  const afterFail = await cardCount();
  const failBatch = await batchSnapshot();
  const firstItem = failBatch.items[0] || null;
  const secondItem = failBatch.items[1] || null;
  const firstErrorVisible = Boolean(firstItem) && firstItem.status === "failed" && Boolean(firstItem.retry) && (Boolean(firstItem.error) || /失败|不可用|错误|网络/.test(firstItem.text));
  const secondSucceeded = Boolean(secondItem) && secondItem.status === "succeeded";
  // 重试首件：同批 File 句柄还在内存，应直接重发成功并补建卡片（只影响这一项）。
  let retryFirst = { ok: false };
  if (firstErrorVisible) {
    await page.eval("(function(){ const items=[...document.querySelectorAll('[data-im=\"add-batch-item\"]')]; const li=items[0]; " +
      "const b=li&&li.querySelector('[data-im=\"card-attach-retry\"]'); if(b) b.click(); return !!b; })()");
    await sleep(5500);
    const afterRetry = await cardCount();
    const statuses = await page.eval("(function(){ return [...document.querySelectorAll('[data-im=\"add-batch-item\"]')].map(function(li){return li.getAttribute('data-status');}); })()");
    retryFirst = { ok: afterRetry === afterFail + 1 && statuses[0] === "succeeded", cardsAfter: afterRetry, statuses: statuses };
  }
  await shot("s5-first-file-failure");
  record("⑤-3 首件上传失败：失败项有可见原因与重试入口，重试成功且后续文件不陪葬（[模拟]首请求失败）", drop2.ok === true && failedOnce === true && afterFail - beforeFail === 1 && firstErrorVisible && secondSucceeded && retryFirst.ok === true, {
    simulated: true, failedOnce: failedOnce, cardsBefore: beforeFail, cardsAfter: afterFail,
    batchSummary: failBatch.summary, items: failBatch.items, firstErrorVisible: firstErrorVisible,
    secondSucceeded: secondSucceeded, retryFirst: retryFirst,
  });
  return { routeMark: roundMark };
}

/** ⑥ 净白/暗紫晶、Tab 焦点、Esc（含聊天焦点与中文输入法组合态）。 */
async function s6ThemesAndKeys() {
  await ensureInteractive();
  const themeShots = {};
  for (const theme of ["light", "dark"]) {
    await page.eval("(function(){ localStorage.setItem('qio-theme', " + JSON.stringify(theme) + "); return true; })()");
    await page.reload();
    await page.waitFor("document.querySelector('[data-im=\"board\"]')", { timeout: 30000, label: "主题刷新后板面" }).catch(() => null);
    await sleep(1000);
    const applied = await page.eval("document.documentElement.getAttribute('data-theme')");
    const bg = await page.eval("getComputedStyle(document.querySelector('.interactive')).backgroundColor");
    themeShots[theme] = { applied: applied, bodyBg: bg, shot: await shot("s6-theme-" + theme) };
  }
  record("⑥-1 净白（light）/ 暗紫晶（dark）两主题真实生效且截图", themeShots.light.applied === "light" && themeShots.dark.applied === "dark" && themeShots.light.bodyBg !== themeShots.dark.bodyBg, themeShots);

  // Tab：连续 Tab 必须真实移动焦点，且焦点元素可见（不能落在 body / 不可见节点）。
  await page.focus('[data-im="add-menu"]');
  const focusSeq = [];
  for (let i = 0; i < 4; i += 1) {
    await page.pressKey("Tab");
    await sleep(200);
    focusSeq.push(await page.eval("(function(){ const a=document.activeElement; if(!a) return null; const r=a.getBoundingClientRect(); return { tag:a.tagName.toLowerCase(), im:a.getAttribute('data-im'), text:(a.innerText||a.getAttribute('aria-label')||'').trim().slice(0,24), visible:r.width>0&&r.height>0 }; })()"));
  }
  const visibleFocus = focusSeq.filter((f) => f && f.visible).length;
  const distinct = new Set(focusSeq.filter(Boolean).map((f) => (f.im || f.tag) + "|" + f.text)).size;
  record("⑥-2 板面工具栏连续 Tab 焦点真实移动且可见", visibleFocus >= 3 && distinct >= 3, { sequence: focusSeq });

  // Esc：即使焦点在聊天输入框，也收起提交详情；不停止回复、不清草稿。
  await openSubmitDetails();
  await page.focus("#composer-input");
  const draft = "ESC-DRAFT-" + RUN_ID;
  await page.type("#composer-input", draft);
  await page.pressKey("Escape");
  await sleep(500);
  const escState = await page.eval("(function(){ return { details:!!document.querySelector('[data-im=\"submit-details-box\"]'), value:document.querySelector('#composer-input')?document.querySelector('#composer-input').value:null }; })()");
  record("⑥-3 焦点在聊天输入框时 Esc 收起提交详情且不清聊天草稿", escState.details === false && escState.value === draft, escState);

  // 中文输入法：组合态 Esc 优先给输入法，不收起面板、不清草稿；组合结束可正常提交中文。
  await openSubmitDetails();
  await page.focus("#composer-input");
  await page.eval("(function(){ const el=document.querySelector('#composer-input'); el.value='ni'; el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:''})); el.dispatchEvent(new CompositionEvent('compositionupdate',{bubbles:true,data:'ni'})); el.dispatchEvent(new InputEvent('input',{bubbles:true,data:'ni',inputType:'insertCompositionText',isComposing:true})); return true; })()");
  await page.eval("(function(){ const el=document.querySelector('#composer-input'); el.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',bubbles:true,isComposing:true})); return true; })()");
  await sleep(400);
  const imeDuring = await page.eval("(function(){ const el=document.querySelector('#composer-input'); return { details:!!document.querySelector('[data-im=\"submit-details-box\"]'), value:el?el.value:null }; })()");
  const ime = await page.imeCompose("#composer-input", "你好世界");
  await sleep(300);
  const imeAfter = await page.inputValue("#composer-input");
  record("⑥-4 中文输入法组合态：Esc 不抢输入法（面板/草稿不动），组合结束中文正常落字", imeDuring.details === true && imeDuring.value === "ni" && ime.ok === true && imeAfter.indexOf("你好世界") >= 0, {
    simulatedComposition: true, during: imeDuring, committed: imeAfter.slice(0, 40),
  });
  await page.eval("(function(){ const el=document.querySelector('#composer-input'); if(el){ el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); } return true; })()");
  await page.pressKey("Escape");
  await sleep(400);
  const imeClosed = !(await page.exists('[data-im="submit-details-box"]'));
  record("⑥-5 组合结束后 Esc 正常收起提交详情", imeClosed, { closed: imeClosed });
}

const SCENARIOS = {
  s1: { name: "① 首轮交付 / 三态参与 / 不泄漏", fn: s1Delivery },
  s2: { name: "② A/B 快照一致（板面 B / 快照读 A / latest 读 B）", fn: s2SnapshotReads },
  s3: { name: "③ 重复提交与幂等", fn: s3Duplicate },
  s4: { name: "④ 800×600 布局与命中", fn: s4SmallViewport },
  s5: { name: "⑤ 文件批 / 首件失败 / 路由往返", fn: s5FilesAndRouting },
  s6: { name: "⑥ 主题 / 焦点 / Esc / 中文输入法", fn: s6ThemesAndKeys },
};

async function main() {
  const only = (process.argv.find((a) => a.startsWith("--only=")) || "").split("=")[1] || process.env.QIO_E2E_ONLY || "";
  const names = only ? only.split(",").map((s) => s.trim()).filter(Boolean) : Object.keys(SCENARIOS);
  const browser = await CdpBrowser.launch({ port: Number(process.env.QIO_CDP_PORT || 9333) });
  page = await Page.create(browser);
  await page.setViewport(1440, 900);
  console.log("== PM-QIO032 E2E ==  base=" + BASE + " api=" + API + " provider=" + PROVIDER + " runId=" + RUN_ID);
  try {
    await fetch(API + "/api/onboarding/seen", { method: "POST" }).catch(() => null);
    // 干净数据域：只 mark seen 不会让「初始设置尚未完成」消失，必须 complete（否则模式切换被拦）。
    await fetch(API + "/api/onboarding/complete", { method: "POST" }).catch(() => null);
    await fetch(API + "/api/onboarding/hint", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ dismissed: true }),
    }).catch(() => null);
    await page.goto(BASE + "/");
    await waitAppReady();
  } catch (error) {
    record("环境：应用起来（对话页就绪）", false, String(error).slice(0, 400));
  }
  for (const name of names) {
    const def = SCENARIOS[name];
    if (!def) { record("未知场景 " + name, false, {}); continue; }
    console.log("\n---- " + name + " " + def.name + " ----");
    try {
      await def.fn();
    } catch (error) {
      record(name + " 场景未跑完", false, String(error && error.stack || error).slice(0, 800));
      await shot(name + "-failed").catch(() => null);
    }
  }
  const passed = results.filter((r) => r.ok).length;
  const summary = {
    runId: RUN_ID, base: BASE, api: API, provider: PROVIDER,
    pass: passed, total: results.length,
    results: results, consoleErrors: consoleErrors.concat(page ? page.consoleErrors : []),
    shotsDir: SHOTS, note: "浏览器层=系统 Edge + CDP（本机没有 Playwright 包，见 README）",
    simulated: ["③-4 延迟回执（CDP Fetch 延迟 1.5s）", "③-5 重复/迟到事件（/api/events/test 注入）", "③-6 草稿清理失败（Storage.removeItem 抛错）", "⑤-3 首件上传失败（CDP Fetch abort 首个 /api/attachments/upload）", "⑥-4 中文输入法组合态（CompositionEvent 模拟）"],
    unverified: ["⑤ 旧卡迁移（需要旧 schema 板面 fixture）", "⑤ 隐藏成员/撤销连线（画布连线手势与组成员未纳入本轮）", "② 读文件一路：A/B 用例目前用文字卡，读文件（read_attachment）未取证"],
  };
  writeFileSync(join(OUT, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  writeFileSync(join(OUT, "console.log"), results.map((r) => (r.ok ? "[PASS] " : "[FAIL] ") + r.name + (r.detail ? " :: " + JSON.stringify(r.detail) : "")).join("\n") + "\n", "utf8");
  console.log("\n== 结果：PASS " + passed + " / " + results.length + " ==");
  for (const r of results) console.log((r.ok ? "PASS  " : "FAIL  ") + r.name);
  await browser.close();
  process.exit(passed === results.length ? 0 : 1);
}

main().catch(async (error) => {
  console.error("E2E 主流程失败：" + (error && error.stack || error));
  process.exit(1);
});
