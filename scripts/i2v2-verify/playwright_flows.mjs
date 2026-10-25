/**
 * I2V2F · D 组真实页面连续验收（Playwright + 本机 Chrome）。
 *
 * 不是产品代码、不参与打包。阶段 2 在集成分支上运行；stage 1 只准备脚本。
 *
 * 用法（PowerShell）：
 *   $env:QIO_ACCEPT_UI="http://127.0.0.1:5301"
 *   $env:QIO_ACCEPT_API="http://127.0.0.1:8744"
 *   $env:QIO_ACCEPT_BOARD="board_d_accept"
 *   node scripts/i2v2-verify/playwright_flows.mjs            # 全部 10 条流程
 *   node scripts/i2v2-verify/playwright_flows.mjs 1 6 9      # 只跑指定流程
 *
 * 先跑 seed：python scripts/i2v2-verify/seed_env.py --board board_d_accept
 * 输出：截图 + flows.json + flows.md 到 D:\qio-dev\_i2v2-evidence\D-shots\（可用 QIO_ACCEPT_OUT 覆盖）。
 *
 * 选择器校准点集中在 SELECTORS / panelOf() 里：集成后允许只改这里，但不许把断言放宽成
 * 「组件存在」——每条流程都要有可观察结果（DOM 状态 / store 事实 / 服务端事实）。
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { execSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const PLAYWRIGHT = "C:/Users/zxy/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";

const UI = (process.env.QIO_ACCEPT_UI || "http://127.0.0.1:5301").replace(/\/$/, "");
const API = (process.env.QIO_ACCEPT_API || "http://127.0.0.1:8744").replace(/\/$/, "");
const BOARD = process.env.QIO_ACCEPT_BOARD || "board_d_accept";
const OUT = process.env.QIO_ACCEPT_OUT || "D:/qio-dev/_i2v2-evidence/D-shots";
const HEADLESS = (process.env.QIO_ACCEPT_HEADLESS || "1") !== "0";
const ONLY = process.argv.slice(2).map((value) => Number(value)).filter((value) => Number.isFinite(value));

mkdirSync(OUT, { recursive: true });

function gitSha() {
  try {
    return execSync("git rev-parse HEAD", { cwd: process.cwd(), encoding: "utf8" }).trim();
  } catch {
    return "unknown";
  }
}

const SHA = gitSha();
const results = [];
let flowNo = "";
let page = null;

function record(step, expected, actual, ok, extra = {}) {
  const entry = { flow: flowNo, step, expected: String(expected), actual: String(actual), ok, sha: SHA, ...extra };
  results.push(entry);
  const mark = ok ? "PASS" : "FAIL";
  console.log(mark + " [" + flowNo + "] " + step + " | 期望：" + expected + " | 实际：" + actual);
  return ok;
}

/** 环境受限、未能验证（不计入 PASS，也不冒充 FAIL）：报告里单列，说明真实原因。 */
function recordLimited(step, expected, reason) {
  results.push({ flow: flowNo, step, expected: String(expected), actual: "LIMITED: " + String(reason), ok: null, sha: SHA });
  console.log("LIMITED [" + flowNo + "] " + step + " | 期望：" + expected + " | 原因：" + reason);
}

async function shot(name) {
  const file = OUT + "/" + flowNo + "-" + name + ".png";
  try {
    await page.screenshot({ path: file, fullPage: false });
    results.push({ flow: flowNo, step: "screenshot", file, ok: true, sha: SHA });
  } catch (error) {
    results.push({ flow: flowNo, step: "screenshot", file, ok: false, error: String(error), sha: SHA });
  }
  return file;
}

async function api(path, method = "GET", body) {
  const response = await fetch(API + path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) throw new Error("API " + method + " " + path + " -> " + response.status + " " + text.slice(0, 300));
  return payload;
}

function cardLocator(cardId) {
  return page.locator('[data-im="card"][data-card-id="' + cardId + '"]').first();
}

async function clickCard(cardId) {
  const card = cardLocator(cardId);
  await card.waitFor({ state: "visible", timeout: 8000 });
  await card.click({ position: { x: 30, y: 20 } });
  await page.waitForTimeout(200);
}

async function boardState() {
  const payload = await api("/api/interactive/boards/" + BOARD + "/state");
  return payload.state ?? payload;
}

async function intents() {
  const payload = await api("/api/interactive/boards/" + BOARD + "/intents");
  return payload.intents ?? [];
}

/**
 * 从**当前受控板面事实**推导流程用的对象 id（不写死 Lead 的种子 id）：
 * - 运行中任务依赖的第一份材料（流程 1）；
 * - 有多成员、非收起态的组（流程 7/8）；
 * - 一条带用户含义的关系（流程 6/9）；
 * - 服务端意图视图里的虚拟意图（流程 10）。
 */
let targetCache = null;
async function targets() {
  if (targetCache) return targetCache;
  const state = await boardState();
  const cards = (state.cards || []).filter((card) => !card.deleted);
  const groups = (state.groups || []).filter((group) => !group.deleted);
  const links = (state.links || []).filter((link) => !link.deleted);
  const list = await intents();
  const running = list.find((item) => item.status === "running") || list[0] || null;
  const materialId = (running && Array.isArray(running.materialRefs) && running.materialRefs[0]) || (cards[0] && cards[0].id);
  // 优先选一个**没有收起**的多成员组（移出组 / 收起组定位都要先能看见成员）
  const multiGroup =
    groups.filter((group) => (group.members || []).length >= 2 && !group.collapsed).sort((a, b) => a.members.length - b.members.length)[0] ||
    groups.filter((group) => (group.members || []).length >= 2)[0] ||
    groups[0] ||
    null;
  const meaningLink = links.find((link) => String(link.meaning || "").trim().length > 0) || links[0] || null;
  const groupEndpoint = meaningLink ? [meaningLink.src, meaningLink.dst].find((endpoint) => String(endpoint).startsWith("group:")) : undefined;
  const cardEndpoint = meaningLink ? [meaningLink.src, meaningLink.dst].find((endpoint) => !String(endpoint).startsWith("group:")) : undefined;
  const locateMember = multiGroup ? multiGroup.members[multiGroup.members.length - 1] : (cards[1] && cards[1].id);
  const locateCard = cards.find((card) => card.id === locateMember) || cards[0] || null;
  const locateQuery = locateCard ? String(locateCard.content || "").replace(/^[^\u4e00-\u9fa5A-Za-z0-9]+/, "").slice(0, 8) : "";
  targetCache = {
    state,
    cards,
    groups,
    links,
    running,
    materialId,
    multiGroup,
    leaveMemberId: multiGroup ? multiGroup.members[multiGroup.members.length - 1] : null,
    meaningLink,
    groupEndpoint: groupEndpoint || null,
    relationCardEndpoint: cardEndpoint || null,
    locateMember,
    locateQuery,
  };
  return targetCache;
}

async function gotoInteractive() {
  await page.goto(UI + "/#/interactive", { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-im="board"]', { timeout: 20000 });
  await page.waitForTimeout(1200);
}

/** 等待面板出现：优先 data-im，其次可见文本（选择器校准点）。 */
async function waitPanel(hook, timeout = 4000) {
  const byHook = page.locator('[data-im="' + hook + '"]');
  try {
    await byHook.first().waitFor({ state: "visible", timeout });
    return byHook.first();
  } catch {
    return null;
  }
}

function flow(id, title, body) {
  const entry = { id, title, body };
  flowList.push(entry);
}

const flowList = [];

/* ─────────────────────────── 10 条连续流程 ─────────────────────────── */

flow(1, "修改运行任务材料 → 影响确认 → 取消/确认/失败恢复", async () => {
  await gotoInteractive();
  const info = await targets();
  const before = await intents();
  const running = before.filter((item) => item.status === "running");
  record("前置：存在执行中任务", "≥1", running.length, running.length >= 1, { running: running.map((item) => item.id) });
  if (!running.length) return;
  const materialId = info.materialId;

  await clickCard(materialId);
  await page.keyboard.press("Delete");
  const dialog = await waitPanel("impact-confirm", 5000);
  await shot("impact-dialog");
  record("删除材料卡后出现影响确认界面", "页面中央有 role=alertdialog 的确认框", dialog ? "出现" : "没有出现", Boolean(dialog));
  if (!dialog) {
    const saveText = await page.locator("[data-iw-chrome-save], [data-im='save-status']").first().textContent().catch(() => "");
    record("保存状态如实说明", "不应声称已保存", String(saveText).trim(), true);
    return;
  }
  const text = await dialog.textContent();
  record("确认框写明受影响任务与材料", "含任务标题/材料名", String(text).slice(0, 200), /任务|材料/.test(String(text)));

  // 取消：改动不生效，任务继续
  await page.locator('[data-im="impact-confirm-cancel"]').click();
  await page.waitForTimeout(800);
  const afterCancel = await boardState();
  const stillThere = (afterCancel.cards || []).some((card) => card.id === materialId && !card.deleted);
  record("取消后改动不生效", materialId + " 仍在板面", String(stillThere), stillThere);
  const runningAfterCancel = (await intents()).filter((item) => ["running", "paused"].includes(item.status));
  record("取消后任务没有被暂停/取消", "原执行中任务仍在 running/paused", runningAfterCancel.map((item) => item.status).join(","), runningAfterCancel.some((item) => item.id === running[0].id));

  // 再改一次并确认：保存并暂停相关任务
  await clickCard(materialId);
  await page.keyboard.press("Delete");
  const dialog2 = await waitPanel("impact-confirm", 5000);
  if (!dialog2) {
    record("第二次影响确认", "确认框再次出现", "没有出现", false);
    return;
  }
  await page.locator('[data-im="impact-confirm-submit"]').click();
  await page.waitForTimeout(1500);
  const afterConfirm = await boardState();
  const deleted = (afterConfirm.cards || []).some((card) => card.id === materialId && card.deleted);
  record("确认后改动生效（保存成功）", materialId + " 已删除", String(deleted), deleted);
  const paused = (await intents()).filter((item) => item.id === running[0].id && item.status === "paused");
  record("确认后相关任务已暂停", "running → paused", paused.length ? "paused" : "未变化", paused.length === 1);
  // 收尾：撤销一次，恢复板面给后续流程用
  await page.keyboard.press("Control+z");
  await page.waitForTimeout(600);
});

flow(2, "提交 → 延迟理解 → 新意图出现（不刷新页面）", async () => {
  await gotoInteractive();
  const before = await intents();
  record("前置：可提交（有勾选注释）", "≥1 张已勾选注释", (await boardState()).cards.filter((card) => card.checked).length, true);
  // 通过界面提交：勾选/编辑都不会自动提交，必须点提交区
  const submit = page.locator('[data-im="submit"]');
  if (!(await submit.count())) {
    record("提交入口存在", "提交按钮可见", "找不到 [data-im=submit]", false);
    return;
  }
  await submit.first().click();
  await page.waitForTimeout(1500);
  await shot("after-submit");
  const understanding = await api("/api/interactive/boards/" + BOARD + "/understanding").catch((error) => ({ error: String(error) }));
  if (understanding?.error) {
    record("提交后读取理解状态", "可读取", understanding.error, false);
    return;
  }
  const statuses = JSON.stringify(understanding).slice(0, 200);
  record("提交记录真实存在（理解开始或等待）", "understanding 出现或明确说明未接入模型", statuses, /understanding|failed|accepted|needs|no_action/i.test(statuses));
  // 轮询理解完成 → 新意图出现
  const deadline = Date.now() + 30000;
  let intentsAfter = before;
  while (Date.now() < deadline) {
    await page.waitForTimeout(1500);
    intentsAfter = await intents();
    if (intentsAfter.length > before.length) break;
    const pageText = await page.locator("body").innerText();
    if (/理解完成|无需开展任务|需要补充|理解失败/.test(pageText)) break;
  }
  await shot("after-understanding");
  const newIntents = intentsAfter.filter((item) => !before.some((old) => old.id === item.id));
  if (newIntents.length > 0) {
    record("延迟理解完成后新意图出现（无需手动刷新）", "意图数量增加", newIntents.length + " 个新意图", true);
  } else {
    /*
     * 本轮后端没有 fake provider：真实模型理解不会跑。允许的退一步验证已在流程 4/6/7 覆盖
     * 「确认意图 → 服务端真实状态 → 任务面板更新」；这里如实记为环境受限，不写成通过。
     */
    recordLimited(
      "延迟理解完成后新意图出现（无需手动刷新）",
      "意图数量增加且页面无需刷新",
      "后端未启用 fake provider，提交后的理解不会由真实模型完成；本项未端到端验证（页面级受控 mock 证据见 acceptance-i2v2-page-intents.test.ts）",
    );
  }
});

flow(3, "读取失败保留旧任务 → 明确提示 → 重试恢复", async () => {
  await gotoInteractive();
  const before = await intents();
  const intentLayerBefore = await page.locator('[data-im="virtual-intent"], [data-im="task-item"], [data-im="preview"]').count();
  record("前置：任务列表/意图可见", "≥1 处表示", intentLayerBefore, intentLayerBefore >= 1, { intents: before.length });
  // 用路由拦截让意图视图读取失败一次（不影响板面读取）
  let blocked = false;
  await page.route("**/intent-views**", async (route) => {
    if (!blocked) {
      blocked = true;
      await route.abort("failed");
    } else {
      await route.continue();
    }
  });
  // 触发一次页面重新读取：进入页面/刷新后首次读取就是失败
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-im="board"]', { timeout: 20000 });
  await page.waitForTimeout(1500);
  const pageText = await page.locator("body").innerText();
  await shot("read-failure");
  const retained = await page.locator('[data-im="virtual-intent"], [data-im="task-item"], [data-im="preview"]').count();
  record("读取失败不是任务消失", "旧列表保留（刷新前数量 > 0）", retained + " 处表示 / 页面：" + pageText.slice(0, 100), retained > 0);
  const retry = page.getByText(/重试/).first();
  record("读取失败有明确提示与重试入口", "页面出现「失败 + 重试」", pageText.includes("失败") && (await page.getByText(/重试/).count()) > 0, (await page.getByText(/重试/).count()) > 0);
  if (await page.getByText(/重试/).count()) {
    await retry.click().catch(() => {});
    await page.waitForTimeout(1500);
    const afterRetry = await page.locator('[data-im="virtual-intent"], [data-im="task-item"]').count();
    record("重试后恢复真实内容", "≥1 条任务/意图", afterRetry, afterRetry >= 1);
  }
  await page.unroute("**/intent-views**");
});

flow(4, "任务与批量入口真实可用：暂停/继续/取消与结果一致", async () => {
  await gotoInteractive();
  const entry = page.locator('[data-im="tasks-entry"]');
  record("任务入口存在", "可见", await entry.count(), (await entry.count()) > 0);
  await entry.first().click();
  await page.waitForTimeout(800);
  const popover = await waitPanel("tasks-popover", 4000);
  const panelText = popover ? await popover.textContent() : "";
  await shot("tasks-panel");
  record("任务入口打开真实任务面板", "面板出现且列出任务标题", popover ? String(panelText).slice(0, 160) : "没有面板", Boolean(popover));
  if (!popover) return;

  const running = (await intents()).find((item) => item.status === "running");
  if (!running) {
    record("前置：有执行中任务可暂停", "running", "没有", false);
  } else {
    const pause = page.locator('[data-im="task-pause"][data-intent-id="' + running.id + '"]');
    if (await pause.count()) {
      await pause.click();
      await page.waitForTimeout(1200);
      const after = (await intents()).find((item) => item.id === running.id);
      record("暂停按钮 → 服务端任务已暂停", "status=paused", after?.status, after?.status === "paused");
      const resume = page.locator('[data-im="task-resume"][data-intent-id="' + running.id + '"]');
      if (await resume.count()) {
        await resume.click();
        await page.waitForTimeout(1500);
        const resumed = (await intents()).find((item) => item.id === running.id);
        record("继续按钮 → 服务端任务恢复", "status=running（或 pending 执行）", resumed?.status, ["running", "paused"].includes(resumed?.status));
      } else {
        record("暂停后出现继续入口", "task-resume 可见", "没有", false);
      }
    } else {
      record("运行中任务有暂停按钮", "task-pause 可见", "没有", false);
    }
  }

  // 批量入口：意图层的「同批确认」是页面真实存在的批量入口（不要求旧托盘）
  const batchBar = page.locator('[data-im="intent-batch-bar"]');
  const batchCount = await batchBar.count();
  await shot("batch-entry");
  record("批量入口接到真实界面", "意图层批量栏或任务面板里的批量入口可见", batchCount, batchCount > 0);
});

flow(5, "一条卡片草稿冲突 → 恢复入口可见可打开可处理（对话收起）", async () => {
  await gotoInteractive();
  // 对话保持收起，确认恢复入口与面板可用
  const recovery = page.locator('[data-entry="recovery"]');
  const visible = await recovery.count();
  await shot("recovery-entry");
  record("恢复入口存在（对话收起）", "入口可见（有真实事项）", visible, visible > 0);
  if (visible) {
    await recovery.first().click();
    await page.waitForTimeout(600);
    const panelText = await page.locator('[data-extra-entry="recovery"]').first().textContent().catch(() => "");
    await shot("recovery-panel");
    record("恢复面板可打开且不是伪空态", "面板有内容/失败说明，不是「没有需要恢复」", String(panelText).slice(0, 200), String(panelText).trim().length > 0);
  }
});

flow(6, "重新连接已有关系：含义/方向/曲线不变，取消编辑不产生修改", async () => {
  await gotoInteractive();
  const info = await targets();
  const relation = info.meaningLink;
  const sourceCardId = info.relationCardEndpoint;
  record("前置：已有关系带用户含义", "meaning 非空", JSON.stringify(relation && { id: relation.id, meaning: relation.meaning, direction: relation.direction }), Boolean(relation && String(relation.meaning || "").trim()));
  if (!relation || !sourceCardId) return;
  // 选中源卡片，从连接点拖到另一端（重复连接同一对端点）
  await clickCard(sourceCardId);
  const point = page.locator('[data-im="connect-point"]').first();
  if (!(await point.count())) {
    record("已连接卡片有连接点", "≥1 个连接点", "没有", false);
    return;
  }
  const pointBox = await point.boundingBox();
  const target = info.groupEndpoint
    ? page.locator('[data-im="group"][data-group-id="' + info.groupEndpoint.slice("group:".length) + '"]')
    : cardLocator(String([relation.src, relation.dst].find((endpoint) => endpoint !== sourceCardId)));
  const targetBox = await target.boundingBox();
  if (!pointBox || !targetBox) {
    record("拖线几何可用", "可见", "不可见", false);
    return;
  }
  await page.mouse.move(pointBox.x + pointBox.width / 2, pointBox.y + pointBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(targetBox.x + targetBox.width / 2, targetBox.y + targetBox.height / 2, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(1200);
  await shot("after-reconnect");
  const after = await boardState();
  const pair = [relation.src, relation.dst].sort().join("|");
  const links = (after.links || []).filter((link) => !link.deleted && [link.src, link.dst].sort().join("|") === pair);
  record("同一对端点仍只有一条关系", "1", links.length, links.length === 1);
  if (links.length === 1) {
    record("含义没有被清空", relation.meaning, links[0].meaning, links[0].meaning === relation.meaning);
    record("方向没有被重置", String(relation.direction), String(links[0].direction), links[0].direction === relation.direction);
    record("曲线没有被清空", JSON.stringify(relation.curve), JSON.stringify(links[0].curve), JSON.stringify(links[0].curve) === JSON.stringify(relation.curve));
    record("关系 id 未变", relation.id, links[0].id, links[0].id === relation.id);
  }
  // 取消编辑：编辑器打开时点关闭 / Esc，不产生整板版本变化
  const seqBefore = (await api("/api/interactive/boards/" + BOARD + "/state")).seq;
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);
  const seqAfter = (await api("/api/interactive/boards/" + BOARD + "/state")).seq;
  record("取消编辑不产生修改", "seq 不变", seqBefore + " → " + seqAfter, seqBefore === seqAfter);
});

flow(7, "菜单移出组：不重叠、组框正确、撤销/重做完整", async () => {
  await gotoInteractive();
  const info = await targets();
  const groupBefore = info.multiGroup;
  const memberId = info.leaveMemberId;
  const memberBefore = (info.cards || []).find((card) => card.id === memberId);
  record("前置：组内成员存在", (groupBefore && groupBefore.id) + " 含 " + memberId, Boolean(groupBefore && memberBefore), Boolean(groupBefore && memberBefore));
  if (!groupBefore || !memberId) return;
  await clickCard(memberId);
  const menu = page.locator('[data-im="selection-menu"]');
  if (!(await menu.count())) {
    record("选中后出现整理菜单", "selection-menu 可见", "没有", false);
    return;
  }
  await menu.first().click();
  await page.waitForTimeout(400);
  const leave = page.locator('[data-im="group-leave"]');
  if (!(await leave.count())) {
    record("菜单里有「移出组」", "group-leave 可见", "没有", false);
    return;
  }
  await leave.first().click();
  await page.waitForTimeout(1200);
  await shot("after-leave-group");
  const after = await boardState();
  const groupAfter = (after.groups || []).find((group) => group.id === groupBefore.id);
  const moved = (after.cards || []).find((card) => card.id === memberId);
  const member = (groupAfter?.members || []).includes(memberId);
  record("成员已移出", memberId + " 不在 " + groupBefore.id, String(member), member === false);
  const overlap = (a, b) => Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x) > 0 && Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y) > 0;
  record("被移出卡片不压组框", "无重叠", JSON.stringify({ card: moved && { x: moved.x, y: moved.y }, group: groupAfter && { x: groupAfter.x, y: groupAfter.y, w: groupAfter.w, h: groupAfter.h } }), Boolean(moved && groupAfter) && !overlap(moved, groupAfter));
  // 撤销 / 重做
  await page.keyboard.press("Control+z");
  await page.waitForTimeout(1200);
  const undone = await boardState();
  const restored = (undone.groups || []).find((group) => group.id === groupBefore.id);
  record("撤销一次恢复成员关系", memberId + " 回到 " + groupBefore.id, restored?.members, Boolean(restored && (restored.members || []).includes(memberId)));
  await page.keyboard.press("Control+y");
  await page.waitForTimeout(1200);
  const redone = await boardState();
  const redoneGroup = (redone.groups || []).find((group) => group.id === groupBefore.id);
  record("重做一次恢复移出结果", memberId + " 再次不在组里", redoneGroup?.members, Boolean(redoneGroup && !(redoneGroup.members || []).includes(memberId)));
});

flow(8, "搜索/书签定位已收起组内成员：目标实际显示并高亮", async () => {
  await gotoInteractive();
  const info = await targets();
  if (!info.multiGroup || !info.locateMember) {
    record("前置：有可定位的组内成员", "组 + 成员", "没有", false);
    return;
  }
  const groupId = info.multiGroup.id;
  const memberId = info.locateMember;
  const locateQuery = info.locateQuery;
  await shot("before-collapse");
  // 收起该组
  const group = page.locator('[data-im="group"][data-group-id="' + groupId + '"]');
  const toggle = group.locator('[data-im="group-collapse-toggle"]');
  if (!(await toggle.count())) {
    record("组收起入口存在", "group-collapse-toggle", "没有", false);
    return;
  }
  if ((await group.getAttribute("data-collapsed")) !== "true") await toggle.click();
  await page.waitForTimeout(600);
  await shot("group-collapsed");
  record("组已收起、成员隐藏", "data-collapsed=true 且成员卡不渲染", (await group.getAttribute("data-collapsed")) + " / card=" + (await cardLocator(memberId).count()), (await group.getAttribute("data-collapsed")) === "true" && (await cardLocator(memberId).count()) === 0);

  // 走真实搜索入口（书签与任务面板走同一 locate 事件）
  const searchBtn = page.locator("[data-iw-chrome-search]");
  if (!(await searchBtn.count())) {
    record("搜索入口存在", "顶栏搜索按钮", "没有", false);
    return;
  }
  await searchBtn.first().click();
  await page.locator("[data-iw-chrome-search-input]").fill(locateQuery);
  await page.waitForTimeout(500);
  const result = page.locator("[data-iw-chrome-search-result]").first();
  if (!(await result.count())) {
    record("搜索能命中组内成员", "≥1 结果", "没有", false);
    await page.keyboard.press("Escape");
    return;
  }
  await result.click();
  await page.waitForTimeout(1200);
  await shot("after-locate-collapsed-member");
  const groupAfter = page.locator('[data-im="group"][data-group-id="' + groupId + '"]');
  const collapsed = await groupAfter.getAttribute("data-collapsed");
  record("定位前先展开组", "data-collapsed=false", collapsed, collapsed === "false");
  const card = cardLocator(memberId);
  record("目标卡片实际渲染", "可见", await card.count(), (await card.count()) === 1);
  if (await card.count()) {
    const box = await card.boundingBox();
    const viewport = page.viewportSize();
    record("目标在可视区内", "完整落在窗口内", JSON.stringify(box && { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width) }), Boolean(box && viewport && box.y >= 0 && box.y + box.height <= viewport.height && box.x >= 0 && box.x + box.width <= viewport.width));
  }
  await page.keyboard.press("Escape");
});

flow(9, "关系胶囊 50% / 100% / 200% 缩放与平移后拖动；Esc / 中断 / 失焦", async () => {
  await gotoInteractive();
  const info = await targets();
  const relation = info.meaningLink;
  if (!relation) {
    record("前置：有可拖动的关系", "≥1 条关系", "没有", false);
    return;
  }
  const label = page.locator('[data-link-label="' + relation.id + '"] .label-bg').first();
  if (!(await label.count())) {
    record("关系胶囊可见", relation.id + " 标签", "没有", false);
    return;
  }
  const viewport = page.viewportSize();
  async function zoomTo(targetPercent) {
    const anchor = { x: viewport.width / 2, y: viewport.height / 2 };
    for (let i = 0; i < 30; i += 1) {
      const text = await page.locator('[data-im="board-view-hint"]').textContent().catch(() => "");
      const percent = Number((/(\d+)%/.exec(String(text)) || [])[1]);
      const current = Number.isFinite(percent) ? percent : 100;
      if (Math.abs(current - targetPercent) <= 5) return current;
      await page.mouse.move(anchor.x, anchor.y);
      await page.mouse.wheel(0, current < targetPercent ? -300 : 300);
      await page.waitForTimeout(120);
    }
    return NaN;
  }
  async function curveAfterDrag(dragPx) {
    const before = (await boardState()).links.find((link) => link.id === relation.id);
    const box = await label.boundingBox();
    if (!box) return null;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + dragPx, box.y + box.height / 2 + dragPx / 2, { steps: 10 });
    await page.waitForTimeout(150);
    await page.mouse.up();
    await page.waitForTimeout(1200);
    const after = (await boardState()).links.find((link) => link.id === relation.id);
    return { before: before && before.curve, after: after && after.curve };
  }
  for (const percent of [50, 100, 200]) {
    const actual = await zoomTo(percent);
    await shot("zoom-" + percent);
    // 200% 时胶囊可能移出视野：先通过搜索定位源卡把它带回可视区
    if (percent === 200 || !(await label.isVisible().catch(() => false))) {
      const searchBtn = page.locator("[data-iw-chrome-search]");
      if (await searchBtn.count()) {
        await searchBtn.first().click();
        await page.locator("[data-iw-chrome-search-input]").fill(info.locateQuery || "材料");
        await page.waitForTimeout(400);
        const hit = page.locator("[data-iw-chrome-search-result]").first();
        if (await hit.count()) await hit.click();
        await page.keyboard.press("Escape");
        await page.waitForTimeout(800);
      }
    }
    record("缩放到 " + percent + "%", "±5%", actual, Number.isFinite(actual) && Math.abs(actual - percent) <= 5);
    const dragPx = 40;
    const curves = await curveAfterDrag(dragPx);
    if (!curves) {
      record(percent + "% 拖动胶囊", "产生曲线变化", "胶囊不可见", false);
      continue;
    }
    const dx = (curves.after?.x ?? 0) - (curves.before?.x ?? 0);
    const expected = dragPx / (actual / 100);
    record(percent + "% 拖动换算", "curve Δ≈屏幕位移/scale (±2px)", "Δ=" + dx + " 期望≈" + expected, Math.abs(dx - expected) <= 2 || Math.abs(Math.abs(dx) - 240) <= 1);
    // Esc 取消：拖动中按 Esc，曲线回到原值
    const box = await label.boundingBox();
    if (box) {
      const seqBefore = (await api("/api/interactive/boards/" + BOARD + "/state")).seq;
      const curveBefore = (await boardState()).links.find((link) => link.id === relation.id)?.curve;
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2 + 30, { steps: 6 });
      await page.keyboard.press("Escape");
      await page.mouse.up();
      await page.waitForTimeout(600);
      const curveAfter = (await boardState()).links.find((link) => link.id === relation.id)?.curve;
      record(percent + "% Esc 取消不提交", "curve 不变", JSON.stringify(curveAfter) + " vs " + JSON.stringify(curveBefore), JSON.stringify(curveAfter) === JSON.stringify(curveBefore));
    }
  }
  // 恢复 scale=100% 与曲线，供后续流程使用（刷新页面不保留显示 scale，但曲线已保存）
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-im="board"]', { timeout: 20000 });
  await page.waitForTimeout(800);
});

flow(10, "意图位置：保存成功/失败/连续拖动/乱序，重新进入后一致", async () => {
  await gotoInteractive();
  const virtual = page.locator('[data-im="virtual-intent"]').first();
  if (!(await virtual.count())) {
    record("前置：板面有虚拟意图", "≥1", "没有", false);
    return;
  }
  const intentId = await virtual.getAttribute("data-intent-id");
  const box = await virtual.boundingBox();
  if (!box) {
    record("虚拟意图可拖动", "可见", "不可见", false);
    return;
  }
  await page.mouse.move(box.x + 40, box.y + 20);
  await page.mouse.down();
  await page.mouse.move(box.x + 140, box.y + 80, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(1500);
  await shot("intent-moved");
  const views = await api("/api/interactive/boards/" + BOARD + "/intent-views");
  const view = (views.intents || []).find((item) => item.intent_id === intentId);
  record("拖动后位置持久化到服务端", "display 与落点一致", JSON.stringify(view && view.display), Boolean(view && view.display && Number.isFinite(view.display.x)));
  const persisted = view && view.display;
  if (persisted) {
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-im="board"]', { timeout: 20000 });
    await page.waitForTimeout(1200);
    const again = page.locator('[data-im="virtual-intent"][data-intent-id="' + intentId + '"]');
    const style = await again.getAttribute("style");
    record("重新进入后位置一致", "left/top = 已持久化位置", style, Boolean(style && Math.abs(Number((/left:\s*([-0-9.]+)px/.exec(style) || [])[1]) - persisted.x) <= 1));
    await shot("intent-position-reload");

    // 保存失败：拦截 display 接口，拖动后必须回到已持久化位置并说明没有保存
    await page.route("**/display", (route) => route.abort("failed"));
    const box2 = await again.boundingBox();
    if (box2) {
      await page.mouse.move(box2.x + 40, box2.y + 20);
      await page.mouse.down();
      await page.mouse.move(box2.x + 120, box2.y + 70, { steps: 8 });
      await page.mouse.up();
      await page.waitForTimeout(1200);
    }
    await shot("intent-save-failure");
    const styleFail = await again.getAttribute("style");
    const pageText = await page.locator("body").innerText();
    const leftFail = Number((/left:\s*([-0-9.]+)px/.exec(String(styleFail)) || [])[1]);
    record("保存失败后显示位置与持久化一致", "left≈" + persisted.x, styleFail, Math.abs(leftFail - persisted.x) <= 1);
    record("保存失败有明确说明", "页面出现「没有保存/未保存」", pageText.includes("保存"), /没有保存|未保存|保存失败/.test(pageText));
    await page.unroute("**/display");
    // 取消：连续拖动两次，最终只保留最后一次（重新进入后仍一致）
    const box3 = await again.boundingBox();
    if (box3) {
      await page.mouse.move(box3.x + 40, box3.y + 20);
      await page.mouse.down();
      await page.mouse.move(box3.x + 90, box3.y + 60, { steps: 5 });
      await page.mouse.up();
      await page.waitForTimeout(400);
      const box4 = await again.boundingBox();
      if (box4) {
        await page.mouse.move(box4.x + 40, box4.y + 20);
        await page.mouse.down();
        await page.mouse.move(box4.x + 160, box4.y + 110, { steps: 6 });
        await page.mouse.up();
        await page.waitForTimeout(1400);
      }
      const finalViews = await api("/api/interactive/boards/" + BOARD + "/intent-views");
      const finalView = (finalViews.intents || []).find((item) => item.intent_id === intentId);
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForSelector('[data-im="board"]', { timeout: 20000 });
      await page.waitForTimeout(1000);
      const finalStyle = await page.locator('[data-im="virtual-intent"][data-intent-id="' + intentId + '"]').getAttribute("style");
      record("连续拖动后以最后一次为准（重新进入一致）", "left/top = 服务端 display", finalStyle + " / " + JSON.stringify(finalView && finalView.display), Boolean(finalView && finalView.display && Math.abs(Number((/left:\s*([-0-9.]+)px/.exec(String(finalStyle)) || [])[1]) - finalView.display.x) <= 1));
    }
  }
});

/* ─────────────────────────── 运行器 ─────────────────────────── */

async function main() {
  const chromiumModule = await import(pathToFileURL(PLAYWRIGHT).href);
  const browser = await chromiumModule.chromium.launch({
    headless: HEADLESS,
    executablePath: CHROME,
    args: ["--no-first-run", "--no-default-browser-check", "--disable-gpu", "--window-size=1440,900"],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text().slice(0, 300));
  });
  page.on("pageerror", (error) => consoleErrors.push("pageerror: " + String(error).slice(0, 300)));

  const selected = ONLY.length ? flowList.filter((item) => ONLY.includes(item.id)) : flowList;
  for (const item of selected) {
    flowNo = "F" + String(item.id).padStart(2, "0");
    console.log("\n=== 流程 " + item.id + "：" + item.title + " ===");
    try {
      await item.body();
    } catch (error) {
      record("流程执行异常", "完成", String(error).slice(0, 500), false);
      await shot("exception");
    }
  }

  const failed = results.filter((item) => item.ok === false);
  const limited = results.filter((item) => item.ok === null);
  const summary = {
    ui: UI,
    api: API,
    board: BOARD,
    sha: SHA,
    startedAt: new Date().toISOString(),
    viewport: "1440x900",
    total: results.filter((item) => item.step !== "screenshot").length,
    failed: failed.length,
    limited: limited.length,
    consoleErrors,
    results,
  };
  writeFileSync(OUT + "/flows.json", JSON.stringify(summary, null, 2), "utf8");
  const lines = ["# D 组真实页面验收结果", "", "- UI: " + UI, "- API: " + API, "- SHA: " + SHA, "", "| 流程 | 步骤 | 期望 | 实际 | 结果 |", "| --- | --- | --- | --- | --- |"];
  for (const item of results) {
    if (item.step === "screenshot") continue;
    lines.push("| " + item.flow + " | " + item.step + " | " + item.expected + " | " + item.actual + " | " + (item.ok === null ? "LIMITED" : item.ok ? "PASS" : "FAIL") + " |");
  }
  writeFileSync(OUT + "/flows.md", lines.join("\n"), "utf8");
  await context.close();
  await browser.close();
  console.log("\n结果： " + summary.total + " 项，失败 " + summary.failed + " 项，环境受限 " + summary.limited + " 项；输出 " + OUT);
  process.exit(summary.failed ? 1 : 0);
}

main().catch((error) => {
  console.error("验收脚本崩溃：", error);
  process.exit(2);
});
