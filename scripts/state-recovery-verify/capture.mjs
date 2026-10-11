/**
 * E1：状态恢复收尾轮的截图断言矩阵（独立验收 D）。
 *
 * 修正了上一轮采集流程的三个问题：
 * 1. **截图前实际断言**，而不是靠文件名：主题（data-theme + 计算背景亮度 + 持久化偏好）、
 *    测试卡片 / 关系 / 组数量、预定面板状态、目标失败的原因与重试入口；条件不满足就判该格失败
 *    （不会用空板面或名字对得上的文件冒充有效场景）。
 * 2. 主题走真实持久化路径（localStorage qio-theme → 重新加载），**加载之后再次验证主题**。
 * 3. 同一次页面状态内完成测量与截图（测量之后不重新加载、不改状态）。
 *
 * 矩阵：1440x900 / 1024x768 / 800x600 / 480x600 × 亮色 / 暗色 ×
 *       正常 / 双面板共存 / 保存失败 / 提交失败。
 * 800x600 是桌面最小窗口；480x600 是浏览器窄窗口。
 * 保存失败 / 提交失败由**页面内 fetch 桩**制造受控故障（证据里标注为模拟）。
 *
 * 用法：
 *   node scripts/state-recovery-verify/capture.mjs --label=baseline
 *   node scripts/state-recovery-verify/capture.mjs --label=after --sizes=1440x900 --scenes=base
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";
import { APP, BACKEND, BOARD } from "./config.mjs";
import { Browser } from "./cdp.mjs";
import { METRICS_JS } from "./metrics.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const OUT = join(here, "shots");

const arg = (name, fallback) => {
  const hit = process.argv.find((item) => item.startsWith("--" + name + "="));
  return hit ? hit.slice(name.length + 3) : fallback;
};
const LABEL = arg("label", "baseline");
const sizeFilter = arg("sizes", "").split(",").filter(Boolean);
const sceneFilter = arg("scenes", "").split(",").filter(Boolean);
const themeFilter = arg("themes", "light,dark").split(",").filter(Boolean);

const ALL_SCENES = {
  "1440x900": ["base", "coexist", "savefail", "submitfail"],
  "1024x768": ["base", "coexist", "savefail", "submitfail"],
  "800x600": ["base", "coexist", "savefail", "submitfail"],
  "480x600": ["base", "coexist", "savefail", "submitfail"],
};
const SIZES = Object.keys(ALL_SCENES)
  .filter((key) => !sizeFilter.length || sizeFilter.includes(key))
  .map((key) => key.split("x").map(Number));
const THEMES = themeFilter;
const scenesFor = (key) => ALL_SCENES[key].filter((s) => !sceneFilter.length || sceneFilter.includes(s));

const EXPECTED_CARDS = 6;
const EXPECTED_LINKS = 6;
const DEV_TERMS = ["stale_check", "stale_state", "checkId", "impact_confirmation_required", "/api/", "draft_too_long", "undefined", "NaN"];

async function api(path, init) {
  const resp = await fetch(BACKEND + path, {
    ...(init || {}),
    headers: { "Content-Type": "application/json", Connection: "close", ...((init && init.headers) || {}) },
  });
  return resp.json();
}

/** 固定数据：6 张卡片 / 2 个组 / 6 条关系。写入前先读当前 seq（服务端按版本事实保护写入）。 */
async function seed() {
  const now = new Date().toISOString();
  const card = (id, kind, x, y, content, meta, checked) => ({
    id, kind, x, y, w: 280, h: 180, content, meta: meta || {}, deleted: false, folded: false,
    hidden: false, checked: Boolean(checked), bookmarked: false, createdAt: now, updatedAt: now,
  });
  const cards = [
    card("c_note_1", "text", 40, 40, "把这一季度的材料按「谁在做、依赖什么、什么时候要」三个问题重新排一遍；这段文字故意写得很长，用来验证密集中文排版。", {}, true),
    card("c_note_2", "text", 360, 40, "这一条还没有勾选：QIO 看不到它的文字，提交时也不会被查看。", {}, false),
    card("c_code", "code", 40, 260, "def summarize(items):\n    return [{'title': it.title, 'why': it.reason} for it in items if it.enabled]\n", { language: "python" }, false),
    card("c_file", "file", 360, 260, "2026 年第三季度跨团队协作材料汇总与后续行动项（含附录与修订记录）", { name: "2026Q3-跨团队协作材料汇总-最终修订版-v12.pdf" }, false),
    card("c_url", "url", 680, 40, "https://example.invalid/qio/2026-q3-collaboration-review-with-a-very-long-slug", { title: "季度评审材料（外部链接）" }, false),
    card("c_note_3", "text", 680, 260, "结论：先交付可验证的一小步，再决定要不要扩大范围。", {}, true),
  ];
  const mk = (id, members) => ({ id, name: "默认组名", defaultName: true, ordered: false, deleted: false, members, createdAt: now, updatedAt: now });
  const groups = [mk("g_1", ["c_note_1", "c_code"]), mk("g_2", ["c_url", "c_note_3"])];
  const link = (id, src, dst, meaning) => ({ id, src, dst, direction: false, meaning, deleted: false, createdAt: now, updatedAt: now });
  const links = [
    link("l_1", "c_note_1", "c_note_2", "放在一起看"),
    link("l_2", "c_note_2", "c_code", "结论依赖这段代码"),
    link("l_3", "c_code", "c_file", "实现依据这份材料"),
    link("l_4", "c_file", "c_note_3", "材料支持这个结论"),
    link("l_5", "c_url", "c_note_1", "外部来源"),
    link("l_6", "c_note_3", "c_note_1", "回到开头重新看"),
  ];
  const current = await api("/api/interactive/boards/" + BOARD + "/state");
  const seq = Number(current.seq || 0);
  const resp = await fetch(BACKEND + "/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    headers: { "Content-Type": "application/json", Connection: "close" },
    body: JSON.stringify({
      state: { boardId: BOARD, seq, updatedAt: now, cards, groups, links, selection: [] },
      reason: "sr-d-seed",
    }),
  });
  if (!resp.ok) throw new Error("铺板面失败 " + resp.status + " " + (await resp.text()));
  return { cards: cards.length, groups: groups.length, links: links.length, seqIn: seq };
}

async function seedIntents() {
  const list = await api("/api/interactive/boards/" + BOARD + "/intents");
  for (const item of list.intents || []) {
    if (["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status)) {
      await api("/api/interactive/intents/" + item.id + "/reject", { method: "POST", body: "{}" });
    }
  }
  const created = await api("/api/interactive/boards/" + BOARD + "/intents", { method: "POST", body: JSON.stringify({ demo: true }) });
  const after = await api("/api/interactive/boards/" + BOARD + "/intents");
  const pending = (after.intents || [])
    .filter((item) => ["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status))
    .map((item) => item.id);
  return { created: (created.created || []).length, pending };
}

const waitReady = "(async () => { const t0 = Date.now(); for (;;) { const tb = document.querySelector('[data-im=\"board-toolbar\"]'); const st = document.querySelector('[data-im=\"submit-status\"]'); if (tb && st && tb.getBoundingClientRect().height > 1) return 'ready'; if (Date.now() - t0 > 12000) return 'timeout'; await new Promise((r) => setTimeout(r, 120)); } })()";
const setTheme = (theme) => "(function () { localStorage.setItem('qio-theme', " + JSON.stringify(theme) + "); return localStorage.getItem('qio-theme'); })()";
const batchSeedJs = (ids) => "(function () { try { localStorage.setItem('qio.interactive.intentBatches', JSON.stringify({ version: 2, records: " + JSON.stringify(ids.map((id) => ({ id, key: "sr-d-batch", at: Date.now() }))) + " })); return 'seeded'; } catch (e) { return 'ls-fail'; } })()";
const openChat = "(async () => { const b = document.querySelector('[data-im=\"chat-toggle\"]'); if (b && !document.querySelector('[data-im=\"chat-panel\"]')) b.click(); await new Promise(r => setTimeout(r, 700)); return 'chat'; })()";
const openBatch = "(async () => { const b = document.querySelector('[data-im=\"batch-entry\"]'); if (b && !document.querySelector('[data-im=\"batch-list\"]')) b.click(); await new Promise(r => setTimeout(r, 900)); return 'batch'; })()";
const waitNoOverlap = "(async () => { const rect = (sel) => { const el = document.querySelector(sel); if (!el) return null; const b = el.getBoundingClientRect(); return { l: b.left, t: b.top, r: b.right, b: b.bottom }; }; const t0 = Date.now(); for (;;) { const a = rect('[data-im=\"chat-panel\"]'); const b = rect('[data-im=\"batch-list\"]'); if (a && b) { const w = Math.max(0, Math.min(a.r, b.r) - Math.max(a.l, b.l)); const h = Math.max(0, Math.min(a.b, b.b) - Math.max(a.t, b.t)); if (w * h === 0) return 'no-overlap'; } if (Date.now() - t0 > 8000) return 'timeout'; await new Promise((r) => setTimeout(r, 150)); } })()";
const breakSave = "(function () { if (!window.__srOrigFetch) window.__srOrigFetch = window.fetch.bind(window); window.fetch = function (input, init) { const url = String((input && input.url) || input); const method = String((init && init.method) || 'GET').toUpperCase(); if (/\\/boards\\/[^/]+\\/state$/.test(url) && method === 'PUT') { return Promise.resolve(new Response(JSON.stringify({ detail: '本机模拟：保存这次板面改动时服务端出错（用于验证保存失败的原因与重试入口）' }), { status: 500, headers: { 'Content-Type': 'application/json' } })); } return window.__srOrigFetch(input, init); }; return 'stubbed-save'; })()";
const breakSubmit = "(function () { if (!window.__srOrigFetch) window.__srOrigFetch = window.fetch.bind(window); window.fetch = function (input, init) { const url = String((input && input.url) || input); const method = String((init && init.method) || 'GET').toUpperCase(); if (/\\/submissions$/.test(url) && method === 'POST') { return Promise.resolve(new Response(JSON.stringify({ detail: { error: 'submission_failed', reason: '本机模拟：服务端处理这次提交时出错了；改动与勾选都还在（这段原文故意写得很长，用来验证长失败原文的显示与重试入口）' } }), { status: 500, headers: { 'Content-Type': 'application/json' } })); } return window.__srOrigFetch(input, init); }; return 'stubbed-submit'; })()";
const openDetails = "(async () => { const b = document.querySelector('[data-im=\"submit-details\"]'); if (b) b.click(); await new Promise(r => setTimeout(r, 400)); return 'details'; })()";
const restoreFetch = "(function () { if (window.__srOrigFetch) { window.fetch = window.__srOrigFetch; return 'restored'; } return 'no-stub'; })()";
const addTextCard = "(async () => { const m = document.querySelector('[data-im=\"add-menu\"]'); if (m) m.click(); await new Promise(r => setTimeout(r, 400)); const t = document.querySelector('[data-im=\"add-text\"]'); if (t) t.click(); await new Promise(r => setTimeout(r, 3000)); return 'added'; })()";
const clickSubmit = "(async () => { const b = document.querySelector('[data-im=\"submit\"]'); if (!b) return 'no-submit'; b.click(); await new Promise(r => setTimeout(r, 3000)); return 'submitted'; })()";

async function run() {
  mkdirSync(join(OUT, LABEL), { recursive: true });
  const seeded = await seed();
  console.log("铺板面：" + JSON.stringify(seeded));
  const intents = await seedIntents();
  console.log("铺意图：" + JSON.stringify(intents));

  const browser = new Browser({ outDir: OUT });
  const runs = [];
  try {
    await browser.launch();
    for (const [w, h] of SIZES) {
      const key = w + "x" + h;
      for (const theme of THEMES) {
        for (const scene of scenesFor(key)) {
          const tag = LABEL + "-" + key + "-" + theme + "-" + scene;
          const failures = [];
          const started = Date.now();
          try {
            await seed();
            await browser.navigate(APP + "/?c=" + Date.now() + "#/interactive", 4000);
            await browser.setViewport(w, h);
            await browser.evalJs(setTheme(theme));
            if (scene === "coexist") {
              const refreshed = await seedIntents();
              await browser.evalJs(batchSeedJs(refreshed.pending));
            }
            await browser.reload(4000);
            await browser.evalJs(waitReady);
            if (scene === "coexist") {
              await browser.evalJs(openChat);
              await browser.evalJs(openBatch);
              await browser.evalJs(waitNoOverlap);
            }
            if (scene === "savefail") {
              await browser.evalJs(breakSave);
              await browser.evalJs(addTextCard);
              await browser.evalJs(openDetails);
            }
            if (scene === "submitfail") {
              await browser.evalJs(breakSubmit);
              await browser.evalJs(clickSubmit);
            }
            await sleep(400);
            const metrics = await browser.evalJs(METRICS_JS);

            if (metrics.themeAttr !== theme) failures.push("主题未应用：data-theme=" + metrics.themeAttr + " 期望 " + theme);
            if (metrics.themeStored !== theme) failures.push("主题偏好没有持久化：qio-theme=" + metrics.themeStored);
            if (typeof metrics.bodyLuminance === "number") {
              const dark = metrics.bodyLuminance < 0.5;
              if (theme === "dark" && !dark) failures.push("暗色主题下背景是亮的：" + metrics.bodyBg + " lum=" + metrics.bodyLuminance);
              if (theme === "light" && dark) failures.push("亮色主题下背景是暗的：" + metrics.bodyBg + " lum=" + metrics.bodyLuminance);
            } else failures.push("拿不到背景色：" + metrics.bodyBg);
            if (metrics.cardCount < EXPECTED_CARDS) failures.push("卡片数量不足：" + metrics.cardCount + " < " + EXPECTED_CARDS);
            if (metrics.linkCount < EXPECTED_LINKS) failures.push("关系数量不足：" + metrics.linkCount + " < " + EXPECTED_LINKS);
            if (metrics.linkLabelCount < EXPECTED_LINKS) failures.push("关系文字标签不足：" + metrics.linkLabelCount);
            if (metrics.groupCount < 2) failures.push("组框数量不足：" + metrics.groupCount);
            if (!metrics.toolbarHeight || metrics.toolbarHeight < 10) failures.push("工具栏没有渲染");

            if (scene === "coexist") {
              if (w < 520) {
                // 窄窗口是刻意设计：不并排，改用切换条「空间不足，只展开一个面板（内容都还在）」
                if (!metrics.switchBarText) failures.push("窄窗口没有出现切换条");
                if (!metrics.switchChat || !metrics.switchBatch) failures.push("切换条缺少两个面板的入口");
                if (!metrics.chatOpen && !metrics.batchOpen) failures.push("两个面板都没有打开");
                if (metrics.chatOpen && metrics.batchOpen) failures.push("窄窗口同时展开了两个面板");
                // 只展开一个面板时 overlap 是 null（另一个面板不存在）；只要不为正数就算成立
                if (typeof metrics.overlap === "number" && metrics.overlap > 0) failures.push("窄窗口面板重叠面积不为 0：" + metrics.overlap);
              } else {
                if (!metrics.chatOpen) failures.push("聊天面板没有打开");
                if (!metrics.batchOpen) failures.push("批量列表没有打开");
                if (metrics.batchItems < 1) failures.push("批量列表没有条目");
                if (metrics.overlap !== 0) failures.push("两个面板重叠面积不为 0：" + metrics.overlap);
              }
            }
            let savefailRecovery = null;
            if (scene === "savefail") {
              if (!/保存失败/.test(metrics.saveStatusText || "")) failures.push("保存失败没有如实显示状态：" + metrics.saveStatusText);
              if (metrics.cardCount < EXPECTED_CARDS + 1) failures.push("保存失败时改动没有保留在页面上：cards=" + metrics.cardCount);
              if (/已保存（/.test(metrics.submitStatusText || "")) failures.push("保存失败却显示成已保存：" + metrics.submitStatusText);
            }
            if (scene === "submitfail") {
              if (!metrics.failureReason) failures.push("提交失败没有显示原因");
              if (!metrics.failureRetention) failures.push("提交失败没有显示保留情况");
              if (!/重新提交|重试/.test(metrics.submitButtonText || "")) failures.push("提交失败后没有可见的重试入口：" + metrics.submitButtonText);
            }
            const dev = DEV_TERMS.filter((term) => (metrics.visibleText || "").includes(term));
            if (dev.length) failures.push("可见文本出现开发术语：" + dev.join("|"));

            const shot = await browser.shotFile(LABEL + "/" + tag);
            if (scene === "savefail") {
              // 保存失败之后必须能真的恢复：恢复传输 → 用界面上的「撤销」再保存一次
              await browser.evalJs(restoreFetch);
              await browser.evalJs("(function () { const b = document.querySelector('[data-im=undo]'); if (b) b.click(); return 'undo'; })()");
              await sleep(2200);
              const recovered = await browser.evalJs(METRICS_JS);
              savefailRecovery = { saveStatusText: recovered.saveStatusText, cardCount: recovered.cardCount };
              if (!/已保存/.test(recovered.saveStatusText || "")) failures.push("保存失败后恢复传输再保存仍然失败：" + recovered.saveStatusText);
            }
            runs.push({
              tag, scene, size: key, theme, shot, metrics, failures,
              costMs: Date.now() - started,
              simulated: scene === "savefail" || scene === "submitfail" ? "页面内 fetch 桩（受控故障注入）" : null,
              savefailRecovery,
            });
            console.log(
              tag + "  " + (failures.length ? "FAIL: " + failures.join("；") : "ok") +
                "  cards=" + metrics.cardCount + " links=" + metrics.linkCount + " toolbar=" + metrics.toolbarHeight,
            );
          } catch (error) {
            runs.push({ tag, scene, size: key, theme, shot: null, metrics: null, failures: ["采集异常：" + (error && error.message ? error.message : String(error))], costMs: Date.now() - started });
            console.log(tag + "  FAIL: 采集异常 " + (error && error.message ? error.message : error));
          }
        }
      }
    }
  } finally {
    await browser.close();
  }
  const report = {
    label: LABEL, app: APP, board: BOARD, seeded, intents,
    createdAt: new Date().toISOString(),
    runs,
    httpFails: [...new Set(browser.httpFails)],
    consoleErrors: browser.consoleErrors.slice(-20),
  };
  const suffix = sizeFilter.length || sceneFilter.length || THEMES.length < 2 ? "-partial" : "";
  const file = join(OUT, LABEL + "-report" + suffix + ".json");
  writeFileSync(file, JSON.stringify(report, null, 2), "utf8");
  const failed = runs.filter((r) => r.failures.length).length;
  console.log("报告：" + file);
  console.log("格子 " + runs.length + "，失败 " + failed + (failed ? "（E1 判定：采集/断言未通过）" : "（全部通过）"));
  if (failed) process.exitCode = 1;
}

run().catch((error) => {
  console.error("采集失败：" + (error && error.stack ? error.stack : error));
  process.exit(1);
});
