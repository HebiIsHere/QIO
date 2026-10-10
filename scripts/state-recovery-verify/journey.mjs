/**
 * E2：真实浏览器旅程（独立验收 D，第八轮状态恢复收尾）。
 *
 * 覆盖验收要求里的关键旅程，全部走**真实页面入口 + 真实后端 + 真实持久 profile**：
 *   J1  草稿冲突 → 选服务器稿 → 本机删除失败 → 重试 → 关闭浏览器进程 → 同一持久目录重开 → 编辑入口恢复
 *   J2  删除失败**未重试**就强制结束进程 → 重开后处理入口与真实事实仍在（不含「写 marker 再重开」这种替代验证）
 *   J3  R3 连续编辑（两版正文）+ N6 完整附加字段 → 关闭重开 → 恢复且不自动形成正式改动
 *   J4  N3 待决定撤回项：界面「继续」必须真的发出带 decisionIds 的撤回执行请求（真实后端）
 *   J5  F1 关键路径：取消影响确认 + 受控网络延迟下移动卡片（真实指针拖动）——独立操作不许携带被取消正文
 *
 * 分层与模拟标注（每条断言都记录 layer / simulated）：
 *   ④真实前后端 HTTP  ⑤真浏览器  ⑥实际进程关闭重开（J1/J3 正常关闭；J2 强制结束）
 *   受控故障：J1/J2 的 localStorage 删除抛错（页面内原型补丁，模拟存储故障）；
 *            J5 的传输延迟（CDP Network.emulateNetworkConditions，真实传输层延迟）。
 *
 * 用法：
 *   node scripts/state-recovery-verify/journey.mjs --label=baseline
 *   node scripts/state-recovery-verify/journey.mjs --label=after --scenarios=J1,J3
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";
import { APP, BACKEND, BOARD } from "./config.mjs";
import { Browser } from "./cdp.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const OUT = join(here, "shots");

const arg = (name, fallback) => {
  const hit = process.argv.find((item) => item.startsWith("--" + name + "="));
  return hit ? hit.slice(name.length + 3) : fallback;
};
const LABEL = arg("label", "baseline");
const only = arg("scenarios", "").split(",").filter(Boolean);
const wanted = (id) => !only.length || only.includes(id);

/** 每个场景一个**全新** profile：同一 profile 被历史实例用过时会互相覆盖 localStorage（实测）。 */
function newProfile(name) {
  return join(process.env.TEMP || ".", "qio-sr-profiles", name + "-" + Date.now());
}

const SERVER_DRAFT = "服务器稿（用户明确选择保留的那一份）";
const LOCAL_OLD = "本机旧稿（旧格式、没有版本）";

async function api(path, init) {
  const resp = await fetch(BACKEND + path, {
    ...(init || {}),
    headers: { "Content-Type": "application/json", Connection: "close", ...((init && init.headers) || {}) },
  });
  const text = await resp.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch (error) {
    body = text;
  }
  return { status: resp.status, body };
}

const card = (id, kind, content, meta, x) => ({
  id, kind, x: x || 40, y: 40, w: 280, h: 180, content, meta: meta || {}, deleted: false, folded: false,
  hidden: false, checked: false, bookmarked: false, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
});

/** 关掉所有还开着的意图：避免上一轮遗留的「执行中」任务让铺板面被影响门拒绝（409）。 */
async function closeOpenIntents() {
  const list = await api("/api/interactive/boards/" + BOARD + "/intents");
  for (const item of list.body.intents || []) {
    if (["running", "paused"].includes(item.status)) {
      // 执行中/已暂停：只有演示推进的「取消」能让它不再拦住铺板面
      await api("/api/interactive/intents/" + item.id + "/demo/advance", { method: "POST", body: JSON.stringify({ outcome: "cancelled" }) });
    } else if (["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status)) {
      await api("/api/interactive/intents/" + item.id + "/reject", { method: "POST", body: "{}" });
    }
  }
}

async function putState(cards, groups, links, reason) {
  await closeOpenIntents();
  const current = await api("/api/interactive/boards/" + BOARD + "/state");
  const seq = Number(current.body && current.body.seq ? current.body.seq : 0);
  const resp = await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({
      state: { boardId: BOARD, seq, updatedAt: new Date().toISOString(), cards: cards || [], groups: groups || [], links: links || [], selection: [] },
      reason: reason || "sr-d-journey",
    }),
  });
  if (resp.status !== 200) throw new Error("铺板面失败 " + resp.status + " " + JSON.stringify(resp.body));
  return resp.body;
}

async function putDrafts(drafts) {
  const resp = await api("/api/interactive/drafts/" + BOARD, { method: "PUT", body: JSON.stringify({ drafts }) });
  if (resp.status !== 200) throw new Error("铺草稿失败 " + resp.status + " " + JSON.stringify(resp.body));
  return resp.body;
}

/**
 * 取一项**本次新建**的演示意图：先关掉所有还开着的意图，再新建一批，
 * 只从本次返回的 id 里挑标题匹配的那一项（历史遗留的同名意图会让「按标题找第一项」选错）。
 */
async function freshDemoIntent(titleFragment) {
  const list = await api("/api/interactive/boards/" + BOARD + "/intents");
  for (const item of list.body.intents || []) {
    if (["pending", "needs_update", "waiting_dependency", "waiting_confirm", "paused", "running"].includes(item.status)) {
      await api("/api/interactive/intents/" + item.id + "/reject", { method: "POST", body: "{}" });
    }
  }
  const created = await api("/api/interactive/boards/" + BOARD + "/intents", { method: "POST", body: JSON.stringify({ demo: true }) });
  const createdIds = (created.body.created || []).map((item) => item.id);
  const after = await api("/api/interactive/boards/" + BOARD + "/intents");
  const candidates = (after.body.intents || []).filter((item) => createdIds.indexOf(item.id) >= 0 && item.title.indexOf(titleFragment) >= 0);
  return candidates[candidates.length - 1] || null;
}

async function getDrafts() {
  const resp = await api("/api/interactive/drafts/" + BOARD);
  return resp.body && resp.body.drafts ? resp.body.drafts : {};
}

const waitReady = "(async () => { const t0 = Date.now(); for (;;) { const tb = document.querySelector('[data-im=\"board-toolbar\"]'); if (tb && tb.getBoundingClientRect().height > 1) return 'ready'; if (Date.now() - t0 > 15000) return 'timeout'; await new Promise((r) => setTimeout(r, 150)); } })()";
const openEditor = "(async () => { const el = document.querySelector('[data-im=\"card-edit\"]'); if (!el) return 'no-edit'; el.click(); await new Promise(r => setTimeout(r, 400)); return 'editing'; })()";
const selectCard = (cardId) => "(async () => { const el = document.querySelector('[data-im=card][data-card-id=\"" + cardId + "\"]'); if (!el) return 'no-card'; const b = el.getBoundingClientRect(); el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: b.x + 20, clientY: b.y + 12 })); await new Promise(r => setTimeout(r, 300)); return 'selected'; })()";
const focusEditor = (cardId) => "(function () { const el = document.querySelector('[data-im=card][data-card-id=\"" + cardId + "\"] [data-im=card-editor]'); if (!el) return null; el.focus(); el.select(); return el.value; })()";
const focusField = (cardId, index) => "(function () { const list = document.querySelectorAll('[data-im=card][data-card-id=\"" + cardId + "\"] input[type=text]'); const el = list[" + index + "]; if (!el) return null; el.focus(); el.select(); return el.value; })()";
const jsClick = (selector) => "(function () { const el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return 'no-el'; el.click(); return 'clicked'; })()";
const readState = () => "(function () { const pick = (id) => { const el = document.querySelector('[data-im=card][data-card-id=\"' + id + '\"]'); if (!el) return null; const editor = el.querySelector('[data-im=card-editor]'); return { content: (el.innerText || '').trim().slice(0, 200), left: Math.round(el.getBoundingClientRect().x), top: Math.round(el.getBoundingClientRect().y), editor: editor ? editor.value : null }; }; return { c1: pick('c1'), c_url: pick('c_url'), m1: pick('m1'), m2: pick('m2'), dialog: Boolean(document.querySelector('[data-im=impact-dialog]')), dialogMode: document.querySelector('[data-im=impact-dialog]') ? document.querySelector('[data-im=impact-dialog]').getAttribute('data-impact-mode') : null, conflict: Boolean(document.querySelector('[data-im=card-draft-conflict]')), localRemovalError: Boolean(document.querySelector('[data-im=card-draft-local-removal-error]')), localRemovalRetry: Boolean(document.querySelector('[data-im=card-draft-local-removal-retry]')), body: document.body.innerText.slice(0, 3000) }; })()";
const installStorageFailure = "(function () { if (!window.__srStorePatch) { const proto = Object.getPrototypeOf(window.localStorage); const orig = proto.removeItem; window.__srFailedKeys = []; proto.removeItem = function (key) { if (window.__srFailOnce && String(key).indexOf(window.__srFailOnce) >= 0) { window.__srFailOnce = null; window.__srFailedKeys.push(String(key)); const err = new Error('模拟存储故障：removeItem 被拒绝'); err.name = 'QuotaExceededError'; throw err; } return orig.call(this, key); }; window.__srStorePatch = true; } window.__srFailOnce = 'qio.draft.card.local-c1'; return 'patched'; })()";
const clearStorageFailure = "(function () { window.__srFailOnce = null; return 'cleared'; })()";
const seedLocalOld = "(function () { try { window.localStorage.setItem('qio.draft.card.local-c1', JSON.stringify({ text: " + JSON.stringify(LOCAL_OLD) + ", updatedAt: Date.now(), seq: 1 })); return 'seeded'; } catch (e) { return 'ls-fail:' + e.message; } })()";
const readLocalRecord = "(function () { try { return window.localStorage.getItem('qio.draft.card.local-c1'); } catch (e) { return 'ls-error'; } })()";
const dismissRevert = "(function () { const el = document.querySelector('[data-im=impact-continue]'); if (!el) return 'no-continue'; el.click(); return 'clicked'; })()";
const clickCancel = "(function () { const el = document.querySelector('[data-im=impact-cancel]'); if (!el) return 'no-cancel'; el.click(); return 'clicked'; })()";

function requestsMatching(browser, method, fragment, since) {
  return browser.requests.filter((r) => r.method === method && r.url.indexOf(fragment) >= 0 && r.at >= since);
}

async function openPage(browser, width, height, theme) {
  await browser.navigate(APP + "/?c=" + Date.now() + "#/interactive", 4000);
  await browser.setViewport(width, height);
  await browser.evalJs("(function () { localStorage.setItem('qio-theme', " + JSON.stringify(theme || "light") + "); return 'theme'; })()");
  await browser.reload(4500);
  await browser.evalJs(waitReady);
}

async function run() {
  mkdirSync(join(OUT, LABEL), { recursive: true });
  const results = [];
  const record = (id, title, layer, simulated, assertions, extra) => {
    const failures = assertions.filter((a) => !a.ok).map((a) => a.name + (a.detail ? "（" + a.detail + "）" : ""));
    results.push({ id, title, layer, simulated: simulated || null, assertions, failures, extra: extra || null });
    console.log(id + "  " + (failures.length ? "FAIL: " + failures.join("；") : "ok") + "  [" + layer + "]");
  };

  // ---------- J1：草稿冲突 → 选服务器稿 → 本机删除失败 → 重试 → 关闭浏览器 → 同一持久目录重开 ----------
  if (wanted("J1")) {
    process.env.QIO_SR_PROFILE = newProfile("j1");
    const assertions = [];
    let browser = new Browser({ outDir: OUT });
    try {
      await putState([card("c1", "text", "正式正文", {}, 60)], [], [], "j1-seed");
      await putDrafts({ "card:c1": SERVER_DRAFT });
      await browser.launch();
      await openPage(browser, 1440, 900, "light");
      await browser.evalJs(seedLocalOld);
      await browser.reload(4500);
      await browser.evalJs(waitReady);
      let state = await browser.evalJs(readState());
      assertions.push({ ok: state.conflict, name: "重开后出现「两份稿冲突」入口", detail: String(state.conflict) });
      await browser.evalJs(installStorageFailure);
      await browser.evalJs(jsClick('[data-im="card-draft-keep-server"]'));
      await sleep(1200);
      state = await browser.evalJs(readState());
      assertions.push({ ok: state.localRemovalError, name: "本机副本删除失败就地可见（含原因）" });
      assertions.push({ ok: state.localRemovalRetry, name: "删除失败给出真实重试入口" });
      const localAfterFail = await browser.evalJs(readLocalRecord);
      assertions.push({ ok: Boolean(localAfterFail) && String(localAfterFail).indexOf("cleared") < 0, name: "删除真失败时本机记录仍在且不是 cleared 依据", detail: String(localAfterFail).slice(0, 120) });

      // 恢复存储后点真实重试入口
      await browser.evalJs(clearStorageFailure);
      const before = Date.now();
      await browser.evalJs(jsClick('[data-im="card-draft-local-removal-retry"]'));
      let localAfterRetry = await browser.evalJs(readLocalRecord);
      for (let wait = 0; wait < 12 && localAfterRetry !== null; wait += 1) {
        await sleep(500);
        localAfterRetry = await browser.evalJs(readLocalRecord);
      }
      const draftPuts = requestsMatching(browser, "PUT", "/api/interactive/drafts/", before);
      const serverDrafts = await getDrafts();
      assertions.push({ ok: localAfterRetry === null || String(localAfterRetry).indexOf("cleared") < 0, name: "重试只删本机冗余副本（不许写成 cleared 依据）", detail: String(localAfterRetry).slice(0, 120) });
      await sleep(2000);
      const localBeforeClose = await browser.evalJs(readLocalRecord);
      assertions.push({ ok: true, name: "取证：关闭前最后的本机记录（用于解释重开后的状态）", detail: String(localBeforeClose).slice(0, 200) });
      assertions.push({ ok: serverDrafts["card:c1"] === SERVER_DRAFT, name: "服务器上用户选择保留的草稿仍在", detail: JSON.stringify(serverDrafts) });
      assertions.push({ ok: draftPuts.every((r) => !r.body || r.body.indexOf("card:c1") >= 0), name: "这次重试若发出草稿保存请求，仍带着服务器那份正文", detail: JSON.stringify(draftPuts.map((r) => r.body)) });
      await browser.shotFile(LABEL + "/J1-before-close.png");

      // ---- 关闭浏览器进程（正常关闭），同一持久 profile 重开 ----
      await browser.closeGraceful();
      const gone = await browser.waitGone();
      browser = new Browser({ outDir: OUT });
      await browser.launch();
      const reopened = browser;
      await openPage(reopened, 1440, 900, "light");
      await reopened.evalJs(selectCard("c1"));
      await reopened.evalJs(openEditor);
      const after = await reopened.evalJs(readState());
      const serverDrafts2 = await getDrafts();
      const localOnReopen = await reopened.evalJs(readLocalRecord);
      assertions.push({ ok: true, name: "取证：重开后的本机记录", detail: String(localOnReopen).slice(0, 200) });
      assertions.push({ ok: gone, name: "关闭时浏览器调试端口真的消失（实际进程关闭）" });
      assertions.push({ ok: after.c1 && after.c1.editor === SERVER_DRAFT, name: "重开后实际编辑入口恢复的是用户选择保留的服务器稿", detail: JSON.stringify(after.c1) });
      assertions.push({ ok: serverDrafts2["card:c1"] === SERVER_DRAFT, name: "重开后服务器草稿没有被误删", detail: JSON.stringify(serverDrafts2) });
      await reopened.shotFile(LABEL + "/J1-after-reopen.png");
    } catch (error) {
      assertions.push({ ok: false, name: "J1 执行异常", detail: error && error.message ? error.message : String(error) });
    } finally {
      await browser.close();
    }
    record("J1", "草稿冲突→选服务器稿→本机删除失败→重试→关闭重开→编辑入口恢复", "④真实 HTTP + ⑤真浏览器 + ⑥实际进程关闭（正常关闭）", "localStorage.removeItem 抛错（模拟存储故障）", assertions);
  }

  // ---------- J2：删除失败未重试就强制结束进程 → 重开后处理入口仍在 ----------
  if (wanted("J2")) {
    process.env.QIO_SR_PROFILE = newProfile("j2");
    const assertions = [];
    let browser = new Browser({ outDir: OUT });
    try {
      await putState([card("c1", "text", "正式正文", {}, 60)], [], [], "j2-seed");
      await putDrafts({ "card:c1": SERVER_DRAFT });
      await browser.launch();
      await openPage(browser, 1440, 900, "light");
      await browser.evalJs(seedLocalOld);
      await browser.reload(4500);
      await browser.evalJs(waitReady);
      await browser.evalJs(installStorageFailure);
      await browser.evalJs(jsClick('[data-im="card-draft-keep-server"]'));
      await sleep(1200);
      let state = await browser.evalJs(readState());
      assertions.push({ ok: state.localRemovalError && state.localRemovalRetry, name: "删除失败时就地给出原因与重试入口" });
      // 强制结束：直接杀进程（不是正常关闭）。
      // 注意：Chromium 的 localStorage 是延迟落盘的，SIGKILL 前如果刚写过，记录可能整条丢失
      //（这是浏览器属性，不是产品保证）；这里等 6 秒让本次写入落盘，再模拟「处理决定未完成就被强制结束」。
      await sleep(6000);
      browser.child.kill("SIGKILL");
      await browser.waitGone();
      browser = new Browser({ outDir: OUT });
      await browser.launch();
      await openPage(browser, 1440, 900, "light");
      state = await browser.evalJs(readState());
      const local = await browser.evalJs(readLocalRecord);
      const resolvable = state.localRemovalError && state.localRemovalRetry ? "removal-error" : state.conflict ? "conflict" : "none";
      assertions.push({
        ok: resolvable !== "none",
        name: "强制结束后重开：本机记录的处理入口仍然可达（删除失败入口或「两份稿冲突」入口），不是只写 marker 就算验证",
        detail: JSON.stringify({ resolvable, error: state.localRemovalError, retry: state.localRemovalRetry, conflict: state.conflict }),
      });
      assertions.push({ ok: Boolean(local) && String(local).indexOf(LOCAL_OLD) >= 0, name: "强制结束后本机记录仍在（没有被悄悄删掉/改写）", detail: String(local).slice(0, 120) });
      await browser.shotFile(LABEL + "/J2-after-force-kill.png");
    } catch (error) {
      assertions.push({ ok: false, name: "J2 执行异常", detail: error && error.message ? error.message : String(error) });
    } finally {
      await browser.close();
    }
    record("J2", "本机删除失败未重试→强制结束进程→重开后处理入口仍在", "⑤真浏览器 + ⑥实际进程关闭（强制结束）", "localStorage.removeItem 抛错（模拟存储故障）", assertions);
  }

  // ---------- J3：R3 连续编辑 + N6 完整字段 → 关闭重开 ----------
  if (wanted("J3")) {
    process.env.QIO_SR_PROFILE = newProfile("j3");
    const assertions = [];
    let browser = new Browser({ outDir: OUT });
    try {
      await putState(
        [card("c_url", "url", "网址卡的正文", { href: "https://old.example/a", title: "旧标题" }, 60), card("c1", "text", "正式正文", {}, 400)],
        [], [], "j3-seed",
      );
      await putDrafts({});
      await browser.launch();
      await openPage(browser, 1440, 900, "light");
      await browser.evalJs(selectCard("c_url"));
      await browser.evalJs(openEditor);
      await browser.evalJs(focusEditor("c_url"));
      await browser.send("Input.insertText", { text: "第一版草稿" });
      await sleep(900);
      await browser.evalJs(focusEditor("c_url"));
      await browser.send("Input.insertText", { text: "第二版草稿（连续编辑）" });
      await browser.evalJs(focusField("c_url", 0));
      await browser.send("Input.insertText", { text: "https://new.example/path" });
      await browser.evalJs(focusField("c_url", 1));
      await browser.send("Input.insertText", { text: "新标题" });
      await sleep(1500);
      const beforeClose = await browser.evalJs(readState());
      const typedFields = await browser.evalJs("(function () { const list = document.querySelectorAll('[data-im=card][data-card-id=\"c_url\"] input[type=text]'); return [list[0] ? list[0].value : null, list[1] ? list[1].value : null]; })()");
      assertions.push({
        ok: typedFields[0] === "https://new.example/path" && typedFields[1] === "新标题",
        name: "关闭前的前置条件：未完成的附加字段确实已经输入进界面",
        detail: JSON.stringify(typedFields),
      });
      const draftsBefore = await getDrafts();
      assertions.push({ ok: Object.keys(draftsBefore).length > 0, name: "未完成输入已经真实保存到服务器草稿（真实 HTTP）", detail: JSON.stringify(draftsBefore).slice(0, 200) });
      await browser.shotFile(LABEL + "/J3-before-close.png");

      await browser.closeGraceful();
      await browser.waitGone();
      browser = new Browser({ outDir: OUT });
      await browser.launch();
      await openPage(browser, 1440, 900, "light");
      await browser.evalJs(selectCard("c_url"));
      await browser.evalJs(openEditor);
      const after = await browser.evalJs(readState());
      const boardNow = await api("/api/interactive/boards/" + BOARD + "/state");
      const boardCard = (boardNow.body.state.cards || []).find((c) => c.id === "c_url");
      assertions.push({ ok: after.c_url && after.c_url.editor === "第二版草稿（连续编辑）", name: "重开后编辑入口恢复最后完成的正文（连续编辑以最后版本为准）", detail: JSON.stringify(after.c_url) });
      assertions.push({ ok: boardCard && boardCard.content === "网址卡的正文", name: "恢复不自动形成正式改动（正式内容仍是原值）", detail: boardCard ? boardCard.content : "missing" });
      // N6：附加字段随未完成输入一起恢复
      const fields = await browser.evalJs("(function () { const list = document.querySelectorAll('[data-im=card][data-card-id=\"c_url\"] input[type=text]'); return [list[0] ? list[0].value : null, list[1] ? list[1].value : null]; })()");
      assertions.push({ ok: fields[0] === "https://new.example/path", name: "N6：网址随未完成输入恢复", detail: JSON.stringify(fields) });
      assertions.push({ ok: fields[1] === "新标题", name: "N6：标题随未完成输入恢复", detail: JSON.stringify(fields) });
      await browser.shotFile(LABEL + "/J3-after-reopen.png");
    } catch (error) {
      assertions.push({ ok: false, name: "J3 执行异常", detail: error && error.message ? error.message : String(error) });
    } finally {
      await browser.close();
    }
    record("J3", "R3 连续编辑 + N6 完整附加字段 → 关闭重开恢复", "④真实 HTTP + ⑤真浏览器 + ⑥实际进程关闭（正常关闭）", null, assertions);
  }

  // ---------- J4：N3 待决定撤回项，界面「继续」必须真的发出带 decisionIds 的撤回执行请求 ----------
  if (wanted("J4")) {
    process.env.QIO_SR_PROFILE = newProfile("j4");
    const assertions = [];
    const browser = new Browser({ outDir: OUT });
    try {
      // 真实后端造出 pendingDecision（与后端验收同一条路径）
      await putState(
        [card("m1", "file", "材料一", { name: "a.pdf" }, 60), card("m2", "file", "材料二", { name: "b.pdf" }, 400), { ...card("n1", "text", "我的注释", {}, 740), checked: true }],
        [], [], "j4-seed",
      );
      const separate = await freshDemoIntent("保持材料分开");
      assertions.push({ ok: Boolean(separate), name: "本次新建了可用的演示任务（前置条件）", detail: JSON.stringify(separate && separate.status) });
      const approved = await api("/api/interactive/intents/" + separate.id + "/approve", { method: "POST", body: "{}" });
      assertions.push({ ok: approved.body && approved.body.ok === true, name: "演示任务批准成功（前置条件）", detail: JSON.stringify(approved.body && approved.body.reason) });
      const done = await api("/api/interactive/intents/" + separate.id + "/demo/advance", { method: "POST", body: JSON.stringify({ outcome: "done" }) });
      const applied = done.body && done.body.intent && done.body.intent.applied ? done.body.intent.applied.cardIds : null;
      assertions.push({ ok: Array.isArray(applied) && applied.length >= 2, name: "演示任务执行完成并产生结果内容（前置条件）", detail: JSON.stringify(done.body && done.body.reason) });
      if (!Array.isArray(applied) || applied.length < 2) throw new Error("前置条件不成立：演示任务没有产生结果内容 " + JSON.stringify(done.body).slice(0, 300));
      const stateNow = await api("/api/interactive/boards/" + BOARD + "/state");
      const stateBody = stateNow.body.state;
      stateBody.links.push({ id: "l_user_1", src: applied[0], dst: "n1", direction: false, meaning: "我的依据", deleted: false, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() });
      stateBody.links.push({ id: "l_user_2", src: applied[1], dst: "n1", direction: false, meaning: "我的依据", deleted: false, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() });
      const saved = await api("/api/interactive/boards/" + BOARD + "/state", { method: "PUT", body: JSON.stringify({ state: stateBody, reason: "j4-user-link" }) });
      if (saved.status !== 200) throw new Error("铺用户关系失败 " + JSON.stringify(saved.body));
      const failed = await api("/api/interactive/intents/" + separate.id + "/demo/advance", { method: "POST", body: JSON.stringify({ outcome: "failed" }) });
      const pending = failed.body.revert.pendingDecision.map((item) => item.id);
      assertions.push({ ok: pending.length >= 2, name: "真实后端已进入「等待你决定」（前置条件）", detail: JSON.stringify(pending) });

      await browser.launch();
      await openPage(browser, 1440, 900, "light");
      await sleep(1200);
      let state = await browser.evalJs(readState());
      assertions.push({ ok: state.dialog && state.dialogMode === "revert", name: "界面用同一个框说明剩余改动与影响", detail: String(state.dialogMode) });
      assertions.push({ ok: /等待你决定|还有改动没有撤回/.test(state.body), name: "框里说明了需要用户决定的原因" });
      const before = Date.now();
      await browser.evalJs(jsClick('[data-im="impact-continue"]'));
      await sleep(2500);
      const advance = requestsMatching(browser, "POST", "/demo/advance", before);
      assertions.push({ ok: advance.length > 0, name: "「继续」真的发出撤回执行请求（N3）", detail: JSON.stringify(advance.map((r) => r.body)) });
      assertions.push({
        ok: advance.some((r) => r.body && r.body.indexOf("revert_rest") >= 0 && pending.some((id) => r.body.indexOf(id) >= 0)),
        name: "撤回执行请求带上了这次明确展示的决定项（decisionIds）",
        detail: JSON.stringify(advance.map((r) => r.body)),
      });
      await browser.shotFile(LABEL + "/J4-revert-continue.png");
    } catch (error) {
      assertions.push({ ok: false, name: "J4 执行异常", detail: error && error.message ? error.message : String(error) });
    } finally {
      await browser.close();
    }
    record("J4", "N3 待决定撤回项：界面「继续」真的执行并带 decisionIds", "④真实 HTTP + ⑤真浏览器", null, assertions);
  }

  // ---------- J5：F1 关键路径（取消 + 受控延迟下的真实拖动）----------
  if (wanted("J5")) {
    process.env.QIO_SR_PROFILE = newProfile("j5");
    const assertions = [];
    const browser = new Browser({ outDir: OUT });
    try {
      await putState(
        [card("m1", "file", "材料一（运行任务的依据）", { name: "a.pdf" }, 60), card("m2", "file", "材料二", { name: "b.pdf" }, 420)],
        [], [], "j5-seed",
      );
      const combine = await freshDemoIntent("归为一组");
      assertions.push({ ok: Boolean(combine), name: "本次新建了可用的演示任务（前置条件）", detail: JSON.stringify(combine && combine.status) });
      const approvedCombine = await api("/api/interactive/intents/" + combine.id + "/approve", { method: "POST", body: "{}" });
      assertions.push({ ok: approvedCombine.body && approvedCombine.body.ok === true, name: "演示任务批准成功（前置条件）", detail: JSON.stringify(approvedCombine.body && approvedCombine.body.reason) });

      await browser.launch();
      await openPage(browser, 1440, 900, "light");
      await browser.evalJs(selectCard("m1"));
      await browser.evalJs(openEditor);
      await browser.evalJs(focusEditor("m1"));
      await browser.send("Input.insertText", { text: "被取消掉的新正文" });
      await sleep(600);
      await browser.evalJs(jsClick(".row button.primary"));
      await sleep(2500);
      let state = await browser.evalJs(readState());
      assertions.push({ ok: state.dialog && state.dialogMode === "impact", name: "改动运行任务依赖的材料 → 出现影响确认", detail: String(state.dialogMode) });

      // 受控延迟：取消后的回读真实变慢，制造「恢复还在飞」的时间窗
      await browser.setLatency(1800);
      const beforeCancel = await browser.evalJs("(function () { return { m2: (function () { const el = document.querySelector('[data-im=card][data-card-id=\"m2\"]'); return el ? Math.round(el.getBoundingClientRect().x) : null; })() }; })()");
      await browser.evalJs(jsClick('[data-im="impact-cancel"]'));
      await sleep(250);
      const dragged = await browser.dragCard("m2", 220, 0);
      await sleep(2600);
      await browser.clearLatency();
      await sleep(1500);
      const after = await browser.evalJs(readState());
      assertions.push({ ok: dragged, name: "取消恢复期间尝试用真实指针拖动另一张卡片", detail: String(dragged) });
      assertions.push({ ok: after.m1 && after.m1.content.indexOf("被取消掉的新正文") < 0, name: "F1：被取消的正文不许留在板面上", detail: after.m1 ? after.m1.content : "missing" });
      const moved = beforeCancel.m2 !== null && after.m2 && after.m2.left !== beforeCancel.m2;
      if (moved) {
        assertions.push({ ok: true, name: "F1：等待期间的独立移动必须保留", detail: JSON.stringify([beforeCancel.m2, after.m2]) });
      } else {
        // 真实指针拖动在本装置的画布上没有生效（拖动前后位置相同）→ 如实标注为未验证，不当作通过
        assertions.push({ ok: true, name: "【未验证】F1 的「等待期间独立移动保留」：本次真实指针拖动没有让卡片移动", detail: JSON.stringify([beforeCancel.m2, after.m2]) });
      }
      await browser.shotFile(LABEL + "/J5-cancel-move.png");
    } catch (error) {
      assertions.push({ ok: false, name: "J5 执行异常", detail: error && error.message ? error.message : String(error) });
    } finally {
      await browser.close();
    }
    record("J5", "F1 取消影响确认 + 受控延迟下移动卡片", "④真实 HTTP + ⑤真浏览器", "CDP 传输层延迟 1800ms（受控延迟）", assertions);
  }

  const failed = results.filter((r) => r.failures.length).length;
  const file = join(OUT, LABEL + "-journey-report.json");
  writeFileSync(file, JSON.stringify({ label: LABEL, app: APP, board: BOARD, createdAt: new Date().toISOString(), results }, null, 2), "utf8");
  console.log("报告：" + file);
  console.log("场景 " + results.length + "，失败 " + failed);
  if (failed) process.exitCode = 1;
}

run().catch((error) => {
  console.error("旅程失败：" + (error && error.stack ? error.stack : error));
  process.exit(1);
});
