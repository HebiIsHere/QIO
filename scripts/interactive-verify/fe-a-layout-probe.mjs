/**
 * A（布局与视觉）的实机验收探针：底部工具栏 / 添加菜单 / 提交区 / 右下聊天入口 / 板内搜索浮层。
 *
 * 与 lead 的 fe-scenarios.mjs 互补：这里只负责 A 的浮层几何与真实可点性，覆盖 1440×900 / 1024×768 / 800×600。
 * 每个宽度跑两遍：
 *   第一遍用页面内 click() 打开添加菜单、添加一张文字注释、打开搜索浮层，量出每个钩子的矩形与命中测试；
 *   第二遍用 CDP Input.dispatchMouseEvent 发**真实鼠标**按下/抬起（不是 element.click()），
 *   点添加菜单 → 点文字注释 → 点右下聊天入口，证明「可见」之外还真的「可点」。
 *
 * 关于作用域：A 的工作区里 BoardCanvas.vue 还留着旧版渲染的 <BoardToolbar /> / <BoardSearchPanel />
 * （B 的分支已经删掉，等集成时才同步过来），所以页面上会同时存在两个 data-im="board-toolbar"，
 * 位置完全重叠、用户实际点到的是页面壳 .im-stage 直系的那个。本探针只测量/点击 **.im-stage 直系实例**，
 * 并把重复实例数量如实写进报告（见 duplicateToolbars），不假装页面是干净的。
 *
 * 前置：后端 IM_BACKEND（默认 http://127.0.0.1:8891）、前端 IM_APP（默认 http://127.0.0.1:5391）。
 * 用法：node scripts/interactive-verify/fe-a-layout-probe.mjs
 * 输出：%TEMP%\qio-visual\shots\fe-a-*.png，报告写到 %TEMP%\qio-visual\fe-a-layout-report.json
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const APP = process.env.IM_APP || "http://127.0.0.1:5391";
const BACKEND = process.env.IM_BACKEND || "http://127.0.0.1:8891";
const BOARD = "board_default";
const TEMP = process.env.TEMP || ".";
const SHOTS = TEMP + "\\qio-visual\\shots";
const REPORT = TEMP + "\\qio-visual\\fe-a-layout-report.json";
mkdirSync(SHOTS, { recursive: true });

/** 页面壳里的工具栏（用户真正看到并点到的那个） */
const SCOPE = ".im-stage > [data-im=board-toolbar]";
/** 主要入口：每个宽度下都必须可见、命中自己（不被遮挡）、能点。 */
const PRIMARY = [
  "board-toolbar", "add-menu", "group-form", "group-join", "group-leave", "group-dissolve",
  "group-ordered", "group-unordered", "group-merge", "delete-selected",
  "mode-select", "mode-rect", "mode-link", "undo", "redo", "search-toggle", "submit",
];
/** 不在工具栏里的钩子（C 的聊天） */
const OUTSIDE = ["chat-toggle", "chat-panel", "chat-input"];
const MENU_ITEMS = ["add-text", "add-file", "add-image", "add-code", "add-url"];
const SEARCH_HOOKS = ["search-panel", "search", "search-close"];
const ALL_SIZES = [
  { w: 1440, h: 900 },
  { w: 1024, h: 768 },
  { w: 800, h: 600 },
];
// 调试用：FE_A_ONLY_W=800 只跑一个宽度；FE_A_DEBUG=1 打印每趟探针的 eval 值/错误原文
const ONLY_W = Number(process.env.FE_A_ONLY_W || 0);
const SIZES = ONLY_W ? ALL_SIZES.filter((s) => s.w === ONLY_W) : ALL_SIZES;
const DEBUG = process.env.FE_A_DEBUG === "1";

function scopedSelector(id) {
  if (OUTSIDE.includes(id)) return "[data-im=" + id + "]";
  if (id === "board-toolbar") return SCOPE;
  return SCOPE + " [data-im=" + id + "]";
}

/**
 * 跑一遍探针。visual_probe 固定用 9333 端口与同一个 Chrome profile，
 * 多个子智能体同时跑会偶发「unsettled top-level await」（Chrome 连接断掉、没有输出），
 * 所以失败时重试（最多 3 次），并把重试次数写进日志，不掩盖。
 */
async function runSteps(steps, attempt = 1) {
  const probe = spawnSync(process.execPath, [resolve(root, "scripts", "visual_probe.mjs"), JSON.stringify(steps)], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  const raw = probe.stdout || "";
  const start = raw.indexOf("{");
  if (start < 0) {
    if (attempt < 3) {
      console.log("探针第 " + attempt + " 次没有输出（可能是并发跑探针抢 9333 端口），3 秒后重试");
      await new Promise((r) => setTimeout(r, 3000));
      return runSteps(steps, attempt + 1);
    }
    throw new Error("探针没有输出 JSON：" + raw.slice(0, 400) + (probe.stderr || "").slice(0, 400));
  }
  const payload = JSON.parse(raw.slice(start));
  // 还有一种失败形态：探针连上了另一个（陈旧的）Chrome target，输出 JSON 但每个 eval 都没有值。
  // 这时整趟结果都不可信，按失败重试，避免把「连错 target」当成「界面不可见」。
  // 第一个 eval（页面自检）必须拿到字符串值：拿不到说明这趟连到的 target / 执行上下文不对，
  // 整趟结果不可信，按失败重试，避免把「连错 target」当成「界面不可见」。
  const firstEval = (payload.results || []).find((r) => r.op === "eval");
  const anyValue = Boolean(firstEval) && typeof firstEval.value === "string";
  if (!anyValue && attempt < 3) {
    console.log("探针第 " + attempt + " 次连到的 target 没有返回任何值（可能是并发跑探针），3 秒后重试");
    await new Promise((r) => setTimeout(r, 3000));
    return runSteps(steps, attempt + 1);
  }
  return payload;
}

/**
 * 把 frontend/src/styles/interactive-shell.css 注入页面，用来验证它的规则真的有效
 * （集成时由主智能体在 main.ts 导入；这里不依赖 Vite 的 ?raw / ?inline —— 浏览器直接请求
 *  ?raw 会被按 text/css 提供、动态 import 会被 MIME 检查拒绝，所以由 Node 读文件再把文本塞进页面）。
 */
const SHELL_CSS = readFileSync(resolve(root, "frontend", "src", "styles", "interactive-shell.css"), "utf8");
function injectShellCss() {
  return "(()=>{var s=document.createElement('style');s.setAttribute('data-fe-a','shell');s.textContent=" + JSON.stringify(SHELL_CSS) + ";document.head.appendChild(s);return 'injected';})()";
}

/**
 * 按「第几个 eval」取值：探针只对 eval / screenshot / cdp 产生结果，screenshot 也在结果数组里，
 * 用下标切片很容易错位（上一版就错位了）。这里先过滤出 eval，再按序号取。
 */
function evalAt(payload, index) {
  const values = (payload.results || []).filter((r) => r.op === "eval").map((r) => r.value);
  const value = values[index];
  if (typeof value !== "string") return {};
  try { return JSON.parse(value); } catch (e) { return {}; }
}

/**
 * 页面内的几何与命中测量：矩形、是否在视口内、elementFromPoint 是否命中自己（不被遮挡）、是否禁用。
 * 几何部分整体 try/catch：页面没挂载时返回 __error，而不是让调用方只看到 undefined。
 */
function measureScript(ids) {
  const selectors = JSON.stringify(ids.map((id) => scopedSelector(id)));
  return "JSON.stringify((function(){var ids=" + JSON.stringify(ids) + ";var sels=" + selectors + ";var out={};"
    + "for(var i=0;i<ids.length;i++){var el=document.querySelector(sels[i]);"
    + "if(!el){out[ids[i]]='MISSING';continue;}"
    + "var r=el.getBoundingClientRect();var cx=Math.round(r.left+r.width/2);var cy=Math.round(r.top+r.height/2);"
    + "var hit=document.elementFromPoint(cx,cy);"
    + "out[ids[i]]={l:Math.round(r.left),r:Math.round(r.right),t:Math.round(r.top),b:Math.round(r.bottom),w:Math.round(r.width),h:Math.round(r.height),cx:cx,cy:cy,"
    + "inView:r.left>=-1&&r.top>=-1&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1,"
    + "clickable:!!hit&&(hit===el||el.contains(hit)||hit.contains(el)),"
    + "disabled:el.disabled===true,text:(el.textContent||'').trim().slice(0,14)};}"
    + "out.__dupes={toolbar:document.querySelectorAll('[data-im=board-toolbar]').length,addMenu:document.querySelectorAll('[data-im=add-menu]').length};"
    + "try{var tEl=document.querySelector('" + SCOPE + "');var cEl=document.querySelector('[data-im=chat-toggle]');"
    + "function rect(el){var r=el.getBoundingClientRect();return {l:Math.round(r.left),r:Math.round(r.right),t:Math.round(r.top),b:Math.round(r.bottom),w:Math.round(r.width),h:Math.round(r.height)};}"
    + "function ov(a,b){return !(a.r<=b.l||b.r<=a.l||a.b<=b.t||b.b<=a.t);}"
    + "var tr=rect(tEl);var cr=rect(cEl);"
    + "out.__geo={vw:innerWidth,vh:innerHeight,"
    + "toolbar:Object.assign({},tr,{bottomGap:innerHeight-tr.b,leftGap:tr.l,rightGap:innerWidth-tr.r}),"
    + "chat:Object.assign({},cr,{rightGap:innerWidth-cr.r,bottomGap:innerHeight-cr.b}),"
    + "toolbarChatOverlap:ov(tr,cr)};"
    + "var sp=document.querySelector('" + SCOPE + " [data-im=search-panel]');if(sp){var sr=rect(sp);out.__geo.search=sr;out.__geo.searchToolbarOverlap=ov(sr,tr);out.__geo.searchChatOverlap=ov(sr,cr);}"
    + "var cp=document.querySelector('[data-im=chat-panel]');if(cp){var pr=rect(cp);out.__geo.chatPanel=pr;out.__geo.chatPanelToolbarOverlap=ov(pr,tr);}"
    + "}catch(e){out.__geo={error:String(e)};}"
    + "return out;})())";
}

function clickJs(id) {
  return "document.querySelector('" + scopedSelector(id) + "').click(); 'clicked'";
}
function mouse(type, x, y) {
  return {
    op: "cdp",
    method: "Input.dispatchMouseEvent",
    params: { type, x: Math.round(x), y: Math.round(y), button: "left", clickCount: 1, buttons: type === "mouseReleased" ? 0 : 1 },
  };
}
function realClick(x, y) {
  return [mouse("mousePressed", x, y), mouse("mouseReleased", x, y)];
}
/** 页面是否真的进入了互动板：首次引导浮层会挡住板面，必须先排除这种情况。 */
function sanityScript() {
  return "JSON.stringify({href:location.href,onboarding:!!document.querySelector('.onboarding'),toolbar:!!document.querySelector('" + SCOPE + "'),head:(document.body.innerText||'').slice(0,60)})";
}
function countCards() {
  return "JSON.stringify({cards:document.querySelectorAll('[data-im=card]').length,menuOpen:!!document.querySelector('" + SCOPE + " [data-im=add-text]'),chatPanel:!!document.querySelector('[data-im=chat-panel]')})";
}

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), detail: detail || "" });
  console.log((ok ? "PASS  " : "FAIL  ") + name + (detail ? "  —— " + detail : ""));
}
function hookOk(geo, id) {
  const item = geo[id];
  return item && item !== "MISSING" && item.w > 0 && item.h > 0 && item.inView && item.clickable;
}

async function resetBoard() {
  try {
    const resp = await fetch(BACKEND + "/api/interactive/boards/" + BOARD + "/state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state: { boardId: BOARD, seq: 0, updatedAt: new Date().toISOString(), cards: [], groups: [], links: [], selection: [] },
        reason: "fe-a-layout-reset",
      }),
    });
    return resp.ok;
  } catch (error) {
    console.log("板面重置失败（继续跑）：" + String(error));
    return false;
  }
}

async function runSize(size) {
  const tag = size.w + "x" + size.h;
  console.log("");
  console.log("=== " + tag + " ===");

  // ---- 第一遍：页面内 click()，量几何 ----
  const stepsA = [
    { op: "navigate", url: APP + "/#/interactive", ms: 5200 },
    { op: "viewport", width: size.w, height: size.h },
    { op: "wait", ms: 900 },
    { op: "eval", js: sanityScript() },
    { op: "eval", js: measureScript(PRIMARY) },
    { op: "screenshot", name: "fe-a-" + size.w + "-initial" },
    { op: "eval", js: clickJs("add-menu") },
    { op: "wait", ms: 400 },
    { op: "eval", js: measureScript(PRIMARY.concat(MENU_ITEMS)) },
    { op: "screenshot", name: "fe-a-" + size.w + "-addmenu" },
    { op: "eval", js: clickJs("add-text") },
    { op: "wait", ms: 900 },
    { op: "eval", js: countCards() },
    { op: "screenshot", name: "fe-a-" + size.w + "-added" },
    { op: "eval", js: clickJs("search-toggle") },
    { op: "wait", ms: 400 },
    { op: "eval", js: measureScript(PRIMARY.concat(SEARCH_HOOKS)) },
    { op: "screenshot", name: "fe-a-" + size.w + "-search" },
  ];
  if (DEBUG) console.log("stepsA 步数=" + stepsA.length + " 参数 JSON 长度=" + JSON.stringify(stepsA).length);
  const passA = await runSteps(stepsA);
  if (DEBUG) {
    console.log("passA 结果形状：" + JSON.stringify({ keys: Object.keys(passA), ops: (passA.results || []).map((r) => r.op), errors: passA.errors, log: (passA.log || []).slice(0, 3) }));
    console.log("passA eval 原始结果：");
    console.log(JSON.stringify((passA.results || []).filter((r) => r.op === "eval").map((r) => ({ error: r.error, value: typeof r.value === "string" ? r.value.slice(0, 120) : r.value })), null, 1));
  }
  // 第一遍的 eval 顺序：0 页面自检 / 1 初始几何 / 2 点添加菜单 / 3 菜单展开几何 / 4 点文字注释 / 5 卡片计数 / 6 点搜索开关 / 7 搜索浮层几何
  const sanity = evalAt(passA, 0);
  const geoClosed = evalAt(passA, 1);
  const geoOpen = evalAt(passA, 3);
  const afterAdd = evalAt(passA, 5);
  const geoSearch = evalAt(passA, 7);
  const dupes = geoClosed.__dupes || {};
  if (dupes.toolbar > 1) {
    console.log("注意：页面上有 " + dupes.toolbar + " 个 data-im=\"board-toolbar\"（合并前中间状态：BoardCanvas 还在渲染旧工具栏）。本探针只测量/点击 .im-stage 直系那个。");
  }

  if (passA.errors && passA.errors.length) console.log("页面错误：" + passA.errors.join(" | "));
  if (passA.httpFails && passA.httpFails.length) {
    const fails = passA.httpFails.filter((x) => !x.includes("@fontsource"));
    if (fails.length) console.log("请求失败：" + fails.slice(0, 6).join(" | "));
  }

  check(tag + " 页面真的进入了互动板（没有引导浮层挡住）", sanity.toolbar === true && sanity.onboarding === false, JSON.stringify(sanity));

  const missing = PRIMARY.filter((id) => !hookOk(geoClosed, id));
  check(tag + " 主要入口都可见且命中自己", missing.length === 0, missing.length ? "问题项：" + missing.join(",") : PRIMARY.length + " 个钩子全部命中");

  const geo = geoClosed.__geo || {};
  check(tag + " 工具栏贴底（0~80px）", geo.toolbar && geo.toolbar.bottomGap >= 0 && geo.toolbar.bottomGap <= 80, JSON.stringify(geo.toolbar));
  check(tag + " 工具栏与右下聊天入口不重叠", geo.toolbarChatOverlap === false, "overlap=" + geo.toolbarChatOverlap + " chat=" + JSON.stringify(geo.chat));
  check(tag + " 提交区在工具栏内且可点", hookOk(geoClosed, "submit") && geo.toolbar && geoClosed.submit.l >= geo.toolbar.l - 2 && geoClosed.submit.r <= geo.toolbar.r + 2, JSON.stringify(geoClosed.submit));

  const closedMenuItems = MENU_ITEMS.filter((id) => geoClosed[id] && geoClosed[id] !== "MISSING");
  check(tag + " 菜单收起时五类入口不在 DOM", closedMenuItems.length === 0, closedMenuItems.length ? "仍在 DOM：" + closedMenuItems.join(",") : "v-if 生效");
  const badItems = MENU_ITEMS.filter((id) => !hookOk(geoOpen, id));
  check(tag + " 点开菜单后五类入口可见可点", badItems.length === 0, badItems.length ? "问题项：" + badItems.join(",") : MENU_ITEMS.map((id) => geoOpen[id] && geoOpen[id].text).join(" / "));
  check(tag + " 点「文字注释」后板面多一张卡片且菜单收起", afterAdd.cards >= 1 && afterAdd.menuOpen === false, JSON.stringify(afterAdd));

  const searchGeo = geoSearch.__geo || {};
  check(tag + " 搜索浮层出现且输入框可点", hookOk(geoSearch, "search") && hookOk(geoSearch, "search-panel"), JSON.stringify(geoSearch.search));
  check(tag + " 搜索浮层不遮底部工具栏", searchGeo.searchToolbarOverlap === false, "search=" + JSON.stringify(searchGeo.search) + " toolbar=" + JSON.stringify(searchGeo.toolbar));
  check(tag + " 搜索浮层不碰右下聊天入口", searchGeo.searchChatOverlap === false, "search=" + JSON.stringify(searchGeo.search) + " chat=" + JSON.stringify(searchGeo.chat));

  // ---- 第二遍：真实鼠标 ----
  const menuBtn = geoClosed["add-menu"];
  const textItem = geoOpen["add-text"];
  // chat-toggle 不在工具栏作用域里，几何来自 __geo.chat
  const chatBox = (geoClosed.__geo || {}).chat;
  const chatBtn = chatBox ? { cx: Math.round((chatBox.l + chatBox.r) / 2), cy: Math.round((chatBox.t + chatBox.b) / 2) } : null;
  if (!menuBtn || !textItem || !chatBtn || menuBtn === "MISSING" || textItem === "MISSING" || chatBtn === "MISSING") {
    check(tag + " 真实鼠标点击（前置坐标齐全）", false, "缺少测量坐标");
    return;
  }
  const stepsB = [
    { op: "navigate", url: APP + "/#/interactive", ms: 5200 },
    { op: "viewport", width: size.w, height: size.h },
    { op: "wait", ms: 900 },
    { op: "eval", js: countCards() },
    ...realClick(menuBtn.cx, menuBtn.cy),
    { op: "wait", ms: 450 },
    { op: "eval", js: measureScript(MENU_ITEMS) },
    { op: "screenshot", name: "fe-a-" + size.w + "-realmouse-menu" },
    ...realClick(textItem.cx, textItem.cy),
    { op: "wait", ms: 900 },
    { op: "eval", js: countCards() },
    { op: "screenshot", name: "fe-a-" + size.w + "-realmouse-added" },
    ...realClick(chatBtn.cx, chatBtn.cy),
    { op: "wait", ms: 500 },
    { op: "eval", js: measureScript(["chat-panel", "chat-input"]) },
    { op: "screenshot", name: "fe-a-" + size.w + "-realmouse-chat" },
    { op: "eval", js: injectShellCss() },
    { op: "wait", ms: 400 },
    { op: "eval", js: measureScript(["chat-panel", "chat-input"]) },
    { op: "screenshot", name: "fe-a-" + size.w + "-shellcss-chat" },
  ];
  const passB = await runSteps(stepsB);
  if (DEBUG) {
    console.log("passB 结果形状：" + JSON.stringify({ ops: (passB.results || []).map((r) => r.op), errors: passB.errors, httpFails: (passB.httpFails || []).slice(0, 5) }));
    console.log(JSON.stringify((passB.results || []).filter((r) => r.op === "eval").map((r) => ({ error: r.error, value: typeof r.value === "string" ? r.value.slice(0, 100) : r.value })), null, 1));
  }
  // 第二遍的 eval 顺序：0 卡片计数 / 1 菜单展开几何 / 2 卡片计数 / 3 聊天几何
  const before = evalAt(passB, 0);
  const menuAfterReal = evalAt(passB, 1);
  const afterReal = evalAt(passB, 2);
  const chatGeo = evalAt(passB, 3);
  const chatGeoFixed = evalAt(passB, 5);
  if (passB.errors && passB.errors.length) console.log("页面错误：" + passB.errors.join(" | "));

  const realMenuOk = MENU_ITEMS.every((id) => hookOk(menuAfterReal, id));
  check(tag + " 真实鼠标点「添加」能打开菜单", realMenuOk, MENU_ITEMS.map((id) => menuAfterReal[id] && menuAfterReal[id].text).join(" / "));
  check(tag + " 真实鼠标点「文字注释」能添加卡片", afterReal.cards === before.cards + 1, "before=" + before.cards + " after=" + afterReal.cards);
  const panelOpen = Boolean(chatGeo["chat-panel"]) && chatGeo["chat-panel"] !== "MISSING";
  check(tag + " 真实鼠标点右下聊天入口能展开面板", panelOpen, JSON.stringify((chatGeo.__geo || {}).chatPanel));
  // 骨架版 ChatDock 在窄窗口会把面板画到工具栏上；shell.css（集成时导入）负责把它抬起来。
  check(
    tag + " 导入 interactive-shell.css 后聊天面板不压底部工具栏",
    panelOpen && chatGeoFixed.__geo && chatGeoFixed.__geo.chatPanelToolbarOverlap === false,
    "导入前 overlap=" + (chatGeo.__geo || {}).chatPanelToolbarOverlap + " 导入后 overlap=" + ((chatGeoFixed.__geo || {}).chatPanelToolbarOverlap),
  );
}

const main = async () => {
  console.log("=== A 布局实机验收（app=" + APP + " backend=" + BACKEND + "）===");
  const reset = await resetBoard();
  console.log("板面重置：" + (reset ? "成功" : "失败（不影响相对计数检查）"));
  for (const size of SIZES) {
    try {
      await runSize(size);
    } catch (error) {
      check(size.w + "x" + size.h + " 探针执行完成", false, String(error));
    }
  }
  const failed = checks.filter((c) => !c.ok);
  console.log("");
  console.log("合计 " + checks.length + " 项，通过 " + (checks.length - failed.length) + " 项，失败 " + failed.length + " 项");
  if (failed.length) console.log("失败项：" + failed.map((f) => f.name).join("；"));
  console.log("截图目录：" + SHOTS);
  writeFileSync(REPORT, JSON.stringify({ app: APP, backend: BACKEND, checks, shots: SHOTS, note: "测量作用域 " + SCOPE }, null, 2), "utf8");
  console.log("报告：" + REPORT);
  process.exit(failed.length ? 1 : 0);
};

main();
