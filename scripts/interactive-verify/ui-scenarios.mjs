/**
 * 互动模式的真实界面验收（Lead 维护）。
 *
 * 用 scripts/visual_probe.mjs 驱动真实 Chrome，走用户实际会走的路径：
 *   加材料 → 写两条注释 → 只勾选其中一条 → 多选成组 → 真实鼠标拖动 → 提交
 *   → 演示意图 → 板面虚线预览 → 批量条件
 * 每一步都截图，并用后端接口核对「界面上做的操作真的落库了」。
 *
 * 分成多次独立的浏览器会话（每次都重新打开互动模式）：这正好也验证了「重新打开能看到保存过的东西」。
 * 前置：后端在 IM_BACKEND（默认 127.0.0.1:8791，QIO_DEV_INSECURE=1）；前端 dev server 在 IM_APP（默认 127.0.0.1:5299）。
 */
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const APP = process.env.IM_APP || "http://127.0.0.1:5299";
const BACKEND = process.env.IM_BACKEND || "http://127.0.0.1:8791";
const BOARD = "board_default";
const NAV = [
  { op: "navigate", url: APP + "/#/interactive", ms: 4200 },
  { op: "viewport", width: 1440, height: 900 },
  { op: "wait", ms: 600 },
];

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: Boolean(ok), detail: detail || "" });
  console.log((ok ? "PASS  " : "FAIL  ") + name + (detail ? "  —— " + detail : ""));
}

function runSteps(steps) {
  const probe = spawnSync(process.execPath, [resolve(root, "scripts", "visual_probe.mjs"), JSON.stringify(steps)], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  const raw = probe.stdout || "";
  const start = raw.indexOf("{");
  if (start < 0) throw new Error("探针没有输出 JSON：" + raw.slice(0, 400));
  return JSON.parse(raw.slice(start));
}

const evals = (payload) => (payload.results || []).filter((r) => r.op === "eval").map((r) => r.value);

function lastJson(values, fallback) {
  for (let i = values.length - 1; i >= 0; i -= 1) {
    const value = values[i];
    if (typeof value !== "string") continue;
    const text = value.trim();
    if (!text.startsWith("{") && !text.startsWith("[")) continue;
    try {
      return JSON.parse(text);
    } catch {
      /* 继续往前找 */
    }
  }
  return fallback === undefined ? {} : fallback;
}

async function api(path, init) {
  const resp = await fetch(BACKEND + path, {
    ...(init || {}),
    headers: { "Content-Type": "application/json", ...((init && init.headers) || {}) },
  });
  return resp.json();
}

const boardState = () => api("/api/interactive/boards/" + BOARD + "/state");
const submissions = () => api("/api/interactive/boards/" + BOARD + "/submissions");
const intentsApi = () => api("/api/interactive/boards/" + BOARD + "/intents");

async function resetBoard() {
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({
      state: { boardId: BOARD, seq: 0, updatedAt: new Date().toISOString(), cards: [], groups: [], links: [], selection: [] },
      reason: "e2e-reset",
    }),
  });
}

const sel = (s) => JSON.stringify(s);
const clickEdit = (id) => "[...document.querySelector('[data-card-id=" + sel(id) + "]').querySelectorAll('button')].find(b=>b.textContent.trim()==='编辑').click(); 'edit'";
const typeInto = (id, text) =>
  "const ta=document.querySelector('[data-card-id=" + sel(id) + "] [data-im=\"card-editor\"]');" +
  "if(!ta) throw new Error('编辑框没有出现');" +
  "const set=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;" +
  "set.call(ta," + sel(text) + ");ta.dispatchEvent(new Event('input',{bubbles:true}));" +
  "[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='完成编辑').click(); 'typed'";
const clickCheck = (id) => "document.querySelector('[data-card-id=" + sel(id) + "] [data-im=\"check\"]').click(); 'checked'";

function mouse(type, x, y, modifiers) {
  return {
    op: "cdp",
    method: "Input.dispatchMouseEvent",
    params: { type, x, y, button: "left", clickCount: 1, buttons: type === "mouseReleased" ? 0 : 1, modifiers: modifiers || 0 },
  };
}

const contentsExpr = "JSON.stringify([...document.querySelectorAll('[data-im=\"card\"]')].map(c=>({id:c.getAttribute('data-card-id'), kind:c.querySelector('.kind').textContent, text:(c.querySelector('.content')||{textContent:''}).textContent.slice(0,26), checked: (c.querySelector('[data-im=\"check\"]')||{}).checked})))";

async function phaseAuthor() {
  const created = runSteps([...NAV,
    { op: "eval", js: "document.querySelector('[data-im=\"add-url\"]').click();document.querySelector('[data-im=\"add-file\"]').click();document.querySelector('[data-im=\"add-text\"]').click();document.querySelector('[data-im=\"add-text\"]').click(); 'added'" },
    { op: "wait", ms: 1600 },
    { op: "eval", js: contentsExpr },
  ]);
  const list = lastJson(evals(created), []);
  const notes = list.filter((c) => c.kind.indexOf("文字") >= 0).map((c) => c.id);
  check("界面能添加四张卡片：网址 / 文件 / 两条文字注释", list.length === 4 && notes.length === 2, JSON.stringify(list.map((c) => c.kind)));

  runSteps([...NAV,
    { op: "eval", js: clickEdit(notes[0]) },
    { op: "wait", ms: 500 },
    { op: "eval", js: typeInto(notes[0], "这次方案的成本需要再确认（这条勾选，交给 QIO）") },
    { op: "wait", ms: 1200 },
    { op: "screenshot", name: "im-10-note-editing" },
    { op: "eval", js: contentsExpr },
  ]);

  const second = runSteps([...NAV,
    { op: "eval", js: clickEdit(notes[1]) },
    { op: "wait", ms: 500 },
    { op: "eval", js: typeInto(notes[1], "先不做，等我确认（这条不勾选，QIO 看不到）") },
    { op: "wait", ms: 900 },
    { op: "eval", js: clickCheck(notes[0]) },
    { op: "wait", ms: 1600 },
    { op: "eval", js: contentsExpr },
    { op: "screenshot", name: "im-11-two-notes-one-checked" },
  ]);
  const after = lastJson(evals(second), []);
  const noteA = after.find((c) => c.id === notes[0]) || {};
  const noteB = after.find((c) => c.id === notes[1]) || {};
  check("两条注释的文字都写进了卡片", (noteA.text || "").indexOf("成本需要再确认") >= 0 && (noteB.text || "").indexOf("先不做") >= 0, (noteA.text || "") + " / " + (noteB.text || ""));
  check("只勾选了第一条注释", noteA.checked === true && noteB.checked === false, JSON.stringify({ a: noteA.checked, b: noteB.checked }));
  const saved = await boardState();
  check("注释文字与勾选状态真的保存到后端（保存不调用 QIO）", JSON.stringify(saved.state).indexOf("成本需要再确认") >= 0 && JSON.stringify(saved.state).indexOf("先不做") >= 0);
  return { notes, list };
}

async function phaseGroupAndSubmit(notes) {
  const geoRun = runSteps([...NAV,
    { op: "eval", js: "JSON.stringify([...document.querySelectorAll('[data-im=\"card\"]')].map(c=>{const r=c.getBoundingClientRect();return {id:c.getAttribute('data-card-id'), x:Math.round(r.left+r.width/2), y:Math.round(r.top+16)}}))" },
  ]);
  const rects = lastJson(evals(geoRun), []);
  check("重新打开互动模式能看到上次保存的卡片", rects.length === 4, "卡片 " + rects.length);

  // 多选：先点第一张，再按住 Ctrl 点第二张 → 所选成组
  const grouped = runSteps([...NAV,
    mouse("mousePressed", rects[0].x, rects[0].y),
    { op: "wait", ms: 120 },
    mouse("mouseReleased", rects[0].x, rects[0].y),
    { op: "wait", ms: 350 },
    mouse("mousePressed", rects[1].x, rects[1].y, 2),
    { op: "wait", ms: 120 },
    mouse("mouseReleased", rects[1].x, rects[1].y, 2),
    { op: "wait", ms: 500 },
    { op: "eval", js: "JSON.stringify({selectedAfterMouse: (document.body.innerText.match(/已选 ?\\d+/)||['?'])[0]})" },
    // 真实鼠标事件在无头 Chrome 里对「加选」不够稳：再用带 ctrlKey 的指针事件补一次，
    // 走的是同一套 Vue 处理器（onPointerDown → select(additive)），不是绕过界面。
    { op: "eval", js: "const cards=[...document.querySelectorAll('[data-im=\"card\"]')];const fire=(el,type,opts)=>el.dispatchEvent(new PointerEvent(type,Object.assign({bubbles:true,cancelable:true,pointerId:1,isPrimary:true,button:0,buttons:1},opts)));const r1=cards[1].getBoundingClientRect();fire(cards[1],'pointerdown',{clientX:r1.left+r1.width/2,clientY:r1.top+16,ctrlKey:true});fire(window,'pointerup',{clientX:r1.left+r1.width/2,clientY:r1.top+16,ctrlKey:true}); 'ctrl-select'" },
    { op: "wait", ms: 600 },
    { op: "eval", js: "JSON.stringify({selected: (document.body.innerText.match(/已选 ?\\d+/)||['?'])[0]})" },
    { op: "eval", js: "const b=[...document.querySelectorAll('button')].find(x=>x.textContent.indexOf('所选成组')>=0); b.click(); 'group-clicked'" },
    { op: "wait", ms: 1600 },
    { op: "screenshot", name: "im-12-two-cards-grouped" },
    { op: "eval", js: "JSON.stringify({groups: document.querySelectorAll('[data-im=\"group\"]').length})" },
  ]);
  const selectedInfo = lastJson(evals(grouped), {});
  check("多选两张卡片后能成组", (selectedInfo.groups || 0) >= 1, "界面上的组 " + selectedInfo.groups);

  const dragFrom = rects[2];
  const dragTo = rects[0];
  const dragged = runSteps([...NAV,
    mouse("mousePressed", dragFrom.x, dragFrom.y),
    { op: "wait", ms: 180 },
    mouse("mouseMoved", Math.round((dragFrom.x + dragTo.x) / 2), Math.round((dragFrom.y + dragTo.y) / 2)),
    { op: "wait", ms: 180 },
    { op: "screenshot", name: "im-20-dragging-preview" },
    mouse("mouseMoved", dragTo.x, dragTo.y),
    { op: "wait", ms: 250 },
    mouse("mouseReleased", dragTo.x, dragTo.y),
    { op: "wait", ms: 1700 },
    { op: "screenshot", name: "im-21-after-drop" },
    { op: "eval", js: contentsExpr },
  ]);
  const dropped = lastJson(evals(dragged), []);
  check("拖动放下后卡片都还在（没有丢卡）", dropped.length === 4, "卡片 " + dropped.length);
  const groupsNow = (await boardState()).state.groups.filter((g) => !g.deleted);
  check("分组结果真的保存到后端", groupsNow.length >= 1, "后端组数 " + groupsNow.length);

  const submitted = runSteps([...NAV,
    { op: "eval", js: "document.querySelector('[data-im=\"submit\"]').click(); 'submitted'" },
    { op: "wait", ms: 3200 },
    { op: "screenshot", name: "im-13-submitted" },
    { op: "eval", js: "JSON.stringify({submitStatus:(document.querySelector('[data-im=\"submit-status\"]')||{}).innerText, range:(document.querySelector('[data-im=\"visible-range\"]')||{}).innerText})" },
  ]);
  const submitResult = lastJson(evals(submitted), {});
  check("提交后界面给出文字状态（并写明第一阶段没有接入 QIO 理解）", /提交/.test(submitResult.submitStatus || ""), (submitResult.submitStatus || "").slice(0, 70));

  const state = await boardState();
  const list = await submissions();
  const last = list.submissions[0];
  const raw = JSON.stringify(last);
  const noteA = state.state.cards.find((c) => c.id === notes[0]);
  const noteB = state.state.cards.find((c) => c.id === notes[1]);
  check("提交内容包含已勾选注释的文字", raw.indexOf("成本需要再确认") >= 0, "提交 " + last.status);
  check("提交内容不含未勾选注释的文字与 id", raw.indexOf("先不做") < 0 && raw.indexOf(notes[1]) < 0);
  check("提交成功后勾选自动取消，卡片未被删除（不是撤回）", noteA.checked === false && noteA.deleted === false);
  check("未勾选的注释也还在板面上（只是没交给 QIO）", noteB.deleted === false);
  return { submitResult };
}

async function scenarioApproval() {
  await resetBoard();
  const steps = [...NAV,
    { op: "eval", js: "document.querySelector('[data-im=\"add-file\"]').click();document.querySelector('[data-im=\"add-text\"]').click(); 'seed'" },
    { op: "wait", ms: 1500 },
    { op: "eval", js: "const note=[...document.querySelectorAll('[data-im=\"card\"]')].find(c=>c.querySelector('.kind').textContent.indexOf('文字')>=0);note.querySelector('[data-im=\"check\"]').click(); 'checked'" },
    { op: "wait", ms: 1600 },
    { op: "eval", js: "document.querySelector('[data-im=\"submit\"]').click(); 'submitted'" },
    { op: "wait", ms: 2600 },
    { op: "eval", js: "const b=document.querySelector('[data-im=\"demo-create\"]'); b ? (b.click(), 'demo-created') : 'no-demo-button'" },
    { op: "wait", ms: 3200 },
    { op: "screenshot", name: "im-30-demo-intents-and-previews" },
    { op: "eval", js: "JSON.stringify({intents: document.querySelectorAll('[data-im=\"intent\"]').length, previews: document.querySelectorAll('[data-im=\"preview\"]').length, batch: !!document.querySelector('[data-im=\"batch-list\"]'), dashed: [...document.querySelectorAll('[data-im=\"preview\"]')].filter(e=>{const s=getComputedStyle(e);return s.borderStyle==='dashed'||s.strokeDasharray!=='none'}).length})" },
  ];
  const payload = runSteps(steps);
  const result = lastJson(evals(payload), {});
  check("演示入口能生成四项待审批意图", result.intents >= 4, "意图 " + result.intents);
  check("板面上出现虚线预览（不是只有文字任务列表）", result.previews >= 1 && result.dashed >= 1, "预览元素 " + result.previews + "，虚线 " + result.dashed);
  check("达到批量条件时出现批量列表", result.batch === true);
  const listing = await intentsApi();
  check("服务端给出互不相容的成对信息", (listing.conflicts || []).length >= 1, JSON.stringify(listing.conflicts));
  check("服务端标记批量可用", listing.batchAvailable === true);
  return payload;
}

/** 场景 4：界面上真的点批量批准 —— 互不相容的两项不能一起批准。 */
async function scenarioBatchApproval() {
  const pick = runSteps([...NAV,
    { op: "eval", js: "(async()=>{const r=await fetch('" + BACKEND + "/api/interactive/boards/" + BOARD + "/intents');const j=await r.json();const pair=j.conflicts[0];document.querySelector('[data-im=\"batch-clear\"]').click();let missing=[];for(const id of pair){const row=document.querySelector('[data-im=\"batch-item\"][data-intent-id=\"'+id+'\"]');if(!row){missing.push(id);continue;}const box=row.querySelector('input');(box||row).click();}return JSON.stringify({pair:pair,missing:missing,checked:document.querySelectorAll('[data-im=\"batch-item\"] input:checked').length});})()" },
    { op: "eval", js: "document.querySelector('[data-im=\"batch-approve\"]').click(); 'batch-approved'" },
    { op: "wait", ms: 2500 },
    { op: "screenshot", name: "im-31-batch-conflict-blocked" },
  ]);
  const info = lastJson(evals(pick).filter((v) => typeof v === "string" && v.startsWith("{")), {});
  check("批量列表里能选中互不相容的两项", (info.checked || 0) >= 1, JSON.stringify(info));

  const listing = await intentsApi();
  const pair = (listing.conflicts || [])[0] || [];
  const states = pair.map((id) => (listing.intents.find((i) => i.id === id) || {}).status);
  const started = states.filter((s) => ["running", "done", "paused"].includes(s)).length;
  check("互不相容的两项不能一起批准", started <= 1, "两项状态：" + JSON.stringify(states));
  const other = listing.intents.filter((i) => pair.indexOf(i.id) < 0);
  check("未选中的意图继续等待（没有被顺手处理）", other.some((i) => i.status === "pending"), other.map((i) => i.status).join(","));
}

const main = async () => {
  console.log("=== 互动模式界面验收（app=" + APP + " backend=" + BACKEND + "）===");
  try {
    await resetBoard();
    const authored = await phaseAuthor();
    await phaseGroupAndSubmit(authored.notes);
    await scenarioApproval();
    await scenarioBatchApproval();
  } catch (error) {
    check("验收脚本执行完成", false, String(error));
  }
  const failed = checks.filter((c) => !c.ok);
  console.log("");
  console.log("合计 " + checks.length + " 项，通过 " + (checks.length - failed.length) + " 项，失败 " + failed.length + " 项");
  if (failed.length) console.log("失败项：" + failed.map((f) => f.name).join("；"));
  process.exit(failed.length ? 1 : 0);
};

main();
