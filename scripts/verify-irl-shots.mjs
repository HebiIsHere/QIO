// QIO 互动模式「自由尺寸调整」真实应用验收（Lead 专用，放在仓库之外）
//
// 跑法（后端 + 前端 dev server 已由 scripts/e2e_up.py 起好）：
//     node D:\qio-dev\_irl-shots.mjs
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const PW =
  process.env.QIO_PLAYWRIGHT ||
  "C:\\\\Users\\\\zxy\\\\.cache\\\\codex-runtimes\\\\codex-primary-runtime\\\\dependencies\\\\node\\\\node_modules\\\\playwright";
const { chromium } = require(PW);

const BASE = process.env.QIO_IRL_BASE || "http://127.0.0.1:5199";
const API = process.env.QIO_IRL_API || "http://127.0.0.1:8734";
const OUT = process.env.QIO_IRL_SHOTS || "D:\\qio-dev\\_irl-shots";
mkdirSync(OUT, { recursive: true });

const results = [];
function record(name, ok, detail) {
  results.push({ name, ok, detail: detail ?? null });
  console.log((ok ? "[PASS] " : "[FAIL] ") + name + (detail !== undefined ? " :: " + JSON.stringify(detail) : ""));
}

async function shot(page, name) {
  await page.screenshot({ path: join(OUT, name + ".png") });
}

async function openInteractive(browser, { width = 1440, height = 900, theme = "dark", reduced = false } = {}) {
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 1,
    reducedMotion: reduced ? "reduce" : "no-preference",
  });
  const page = await context.newPage();
  page.on("pageerror", (err) => console.log("[pageerror] " + err.message));
  await page.addInitScript((t) => { try { localStorage.setItem("qio-theme", t); } catch {} }, theme);
  await page.goto(BASE + "/#/interactive", { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-im="board"]', { timeout: 30000 });
  await page.waitForTimeout(1200);
  return { context, page };
}

async function cardBoxes(page) {
  return page.evaluate(() =>
    [...document.querySelectorAll('[data-im="card"]')].map((n) => {
      const r = n.getBoundingClientRect();
      return { id: n.closest("[data-resize-card-id]")?.getAttribute("data-resize-card-id") ?? null, w: Math.round(r.width), h: Math.round(r.height) };
    }),
  );
}

async function addTextCards(page, count) {
  let added = 0;
  for (let i = 0; i < count; i += 1) {
    const before = await page.locator('[data-im="card"]').count();
    await page.click('[data-im="add-menu"]');
    await page.waitForSelector('[data-im="add-menu-list"]', { timeout: 5000 });
    await page.click('[data-im="add-text"]');
    await page.waitForTimeout(400);
    await page.keyboard.press("Escape");
    const after = await page.locator('[data-im="card"]').count();
    if (after > before) added += 1;
  }
  return added;
}

/** 等板面保存落地（状态行显示「已保存」），再刷新 —— 否则测的是「保存还没发出去」 */
async function waitSaved(page, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const text = (await page.locator('[data-im="save-status"]').first().textContent().catch(() => "")) ?? "";
    if (text.includes("已保存")) return text.trim();
    await page.waitForTimeout(200);
  }
  return (await page.locator('[data-im="save-status"]').first().textContent().catch(() => "")) ?? "";
}

async function dragBy(page, locator, dx, dy) {
  const box = await locator.boundingBox();
  if (!box) return false;
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  await page.mouse.move(cx + dx * 0.4, cy + dy * 0.4, { steps: 6 });
  await page.mouse.move(cx + dx, cy + dy, { steps: 12 });
  await page.mouse.up();
  await page.waitForTimeout(350);
  return true;
}

async function main() {
  try { await fetch(API + "/api/onboarding/seen", { method: "POST" }); } catch {}
  const browser = await chromium.launch({ channel: "msedge", headless: true });

  // ── 1. 常规桌面 + 加卡 ────────────────────────────────────────────────────
  const { context, page } = await openInteractive(browser, {});
  // 演示预览层是产品里的「演示内容」，会把真实卡片盖住。它是弹层入口，点开只是展开菜单，
  // 这里只把它收回去，不假装改变了产品状态。
  await page.click('[data-im="demo-entry"]').catch(() => {});
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForTimeout(500);
  const cards0 = await page.locator('[data-im="card"]').count();
  const added = await addTextCards(page, 3);
  const cards1 = await page.locator('[data-im="card"]').count();
  record("桌面 1440x900 进入互动模式并可添加卡片", cards1 > cards0, { cards0, cards1, added });
  await shot(page, "01-desktop-dark-1440x900");

  // ── 2. 选中卡片 → 出现尺寸把手 → 八向拖动改尺寸 ─────────────────────────
  await page.locator('[data-im="card"]').first().click();
  await page.waitForTimeout(400);
  const hosts = await page.locator("[data-resize-card-id]").count();
  record("选中卡片后出现尺寸把手", hosts > 0, { hosts });
  const before = (await cardBoxes(page))[0];
  const handle = page.locator("[data-resize-card-id]").first().locator('[data-resize-handle="se"]');
  const dragged = await dragBy(page, handle, 180, 120);
  const after = (await cardBoxes(page))[0];
  record(
    "拖动右下角改变卡片容器宽高",
    dragged && after.w > before.w + 100 && after.h > before.h + 60,
    { before, after },
  );
  // F-E2E1：getBoundingClientRect 含 transform，单靠它分不出「容器尺寸」与「整体缩放」。
  // 这里改看 offsetWidth / computed width 与 computed transform，并顺带证明
  // **别的卡片文字大小没被带着变**、聊天文字大小也没变（任务书 §3.4）。
  const sizeEvidence = await page.evaluate(() => {
    const cards = [...document.querySelectorAll('[data-im="card"]')];
    const target = cards[0];
    const other = cards[cards.length - 1];
    const chat = document.querySelector('[data-im="chat-panel"]');
    const fs = (el) => (el ? getComputedStyle(el).fontSize : null);
    return {
      offsetWidth: target?.offsetWidth ?? null,
      computedWidth: target ? getComputedStyle(target).width : null,
      transform: target ? getComputedStyle(target).transform : null,
      otherCardFontSize: fs(other?.querySelector('[data-im="card-body"]')),
      chatFontSize: fs(chat?.querySelector('[data-im="chat-stream"]')),
    };
  });
  record(
    "改的是容器尺寸而不是 transform 缩放，且不影响其它卡片与聊天的文字大小",
    sizeEvidence.transform === "none" &&
      sizeEvidence.offsetWidth > 300 &&
      sizeEvidence.computedWidth === sizeEvidence.offsetWidth + "px" &&
      sizeEvidence.otherCardFontSize === "14.5px",
    sizeEvidence,
  );
  await shot(page, "02-card-resized");

  // 键盘 −/＋
  const kbBefore = (await cardBoxes(page))[0];
  await page.locator("[data-resize-card-id]").first().locator('[data-resize-key="w+"]').click({ force: true });
  await page.waitForTimeout(300);
  const kbAfter = (await cardBoxes(page))[0];
  record("键盘 ＋ 按钮增大宽度", kbAfter.w > kbBefore.w, { kbBefore, kbAfter });
  await shot(page, "03-card-keyboard-nudge");

  // ── 3. 刷新后恢复 ────────────────────────────────────────────────────────
  const savedText = await waitSaved(page);
  record("尺寸调整走既有保存路径并如实报告保存状态", savedText.includes("已保存"), { savedText });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-im="board"]', { timeout: 30000 });
  await page.waitForTimeout(1500);
  const reloaded = (await cardBoxes(page))[0];
  record("刷新后卡片尺寸恢复（板面身份绑定）", Math.abs(reloaded.w - kbAfter.w) <= 4 && Math.abs(reloaded.h - kbAfter.h) <= 4, { kbAfter, reloaded });

  // ── 4. 对话窗口：打开 / 位置 / 尺寸 / 持久化 ─────────────────────────────
  await page.click('[data-im="chat-toggle"]');
  await page.waitForSelector('[data-im="chat-panel"]', { timeout: 10000 });
  await page.waitForTimeout(600);
  await shot(page, "04-chat-open");
  const panelBefore = await page.locator('[data-im="chat-panel"]').boundingBox();
  const header = page.locator('[data-im="chat-head"]');
  await dragBy(page, header, -260, -200);
  const panelMoved = await page.locator('[data-im="chat-panel"]').boundingBox();
  record("拖动标题栏移动对话窗口", Math.abs(panelMoved.x - panelBefore.x) > 80 && Math.abs(panelMoved.y - panelBefore.y) > 60, { panelBefore: { x: Math.round(panelBefore.x), y: Math.round(panelBefore.y) }, panelMoved: { x: Math.round(panelMoved.x), y: Math.round(panelMoved.y) } });
  await shot(page, "05-chat-moved");

  const wKey = page.locator('[data-im="chat-panel"] [data-resize-key="w+"]');
  await wKey.click({ force: true });
  await wKey.click({ force: true });
  await page.waitForTimeout(400);
  const panelWider = await page.locator('[data-im="chat-panel"]').boundingBox();
  record("键盘 ＋ 增大对话窗口宽度", panelWider.width > panelMoved.width + 20, { from: Math.round(panelMoved.width), to: Math.round(panelWider.width) });

  const frameKey = await page.evaluate(() => Object.keys(localStorage).find((k) => k.startsWith("qio-chat-frame")) ?? null);
  record("对话窗口位置/尺寸按板面身份写入本机偏好", Boolean(frameKey), { frameKey });

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-im="board"]', { timeout: 30000 });
  await page.waitForTimeout(1500);
  await page.click('[data-im="chat-toggle"]');
  await page.waitForSelector('[data-im="chat-panel"]', { timeout: 10000 });
  await page.waitForTimeout(600);
  const panelRestored = await page.locator('[data-im="chat-panel"]').boundingBox();
  const restoredOk =
    Math.abs(panelRestored.x - panelWider.x) <= 8 &&
    Math.abs(panelRestored.y - panelWider.y) <= 8 &&
    Math.abs(panelRestored.width - panelWider.width) <= 8;
  record("刷新后对话窗口位置与尺寸恢复", restoredOk, { expected: { x: Math.round(panelWider.x), y: Math.round(panelWider.y), w: Math.round(panelWider.width) }, actual: { x: Math.round(panelRestored.x), y: Math.round(panelRestored.y), w: Math.round(panelRestored.width) } });
  await shot(page, "06-chat-restored");

  // ── 5. 多卡同时展开阅读 ──────────────────────────────────────────────────
  const toggles = page.locator('[data-im="card-preview-toggle"]');
  const n = await toggles.count();
  let expanded = 0;
  for (let i = 0; i < Math.min(2, n); i += 1) {
    try {
      await toggles.nth(i).click({ force: true });
      await page.waitForTimeout(600);
      expanded += 1;
    } catch (err) {
      console.log("[warn] 展开第 " + i + " 张失败：" + err.message);
    }
  }
  record("可以同时展开多张卡片阅读", expanded >= 2, { expanded, toggles: n });
  await shot(page, "07-two-cards-expanded");

  // ── 5b. 最小尺寸下内容区仍然可用 ─────────────────────────────────────────
  await page.locator('[data-im="card"]').first().click();
  await page.waitForTimeout(300);
  const minHandle = page.locator("[data-resize-card-id]").first().locator('[data-resize-handle="se"]');
  await dragBy(page, minHandle, -900, -700);
  await page.waitForTimeout(400);
  const minBox = await page.locator('[data-im="card"]').first().boundingBox();
  const minInfo = await page.evaluate(() => {
    const card = document.querySelector('[data-im="card"]');
    if (!card) return null;
    const visible = (sel) => {
      const el = card.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return r.height > 0 && r.width > 0;
    };
    const body = card.querySelector('[data-im="card-body"]');
    return {
      // 标题是可选的（无标题的卡片本来就没有标题节点），所以看「标题或类型标签」至少有一个可读
      headerVisible: Boolean(visible('[data-im="card-title"]') || visible('[data-im="card-license"]')),
      bodyHeight: Math.round(body?.getBoundingClientRect().height ?? 0),
      scrollW: card.scrollWidth - card.clientWidth,
      // 必要状态：许可徽章或「更多」入口至少有一个可见且可点
      statusOrAction:
        Boolean(visible('[data-im="card-license"]')) ||
        Boolean(visible('[data-im="card-preview-toggle"]')) ||
        Boolean(visible('[data-im="card-unfold"]')),
    };
  });
  record(
    "拉到最小尺寸时卡头、必要状态与内容区仍可用，卡片不横向撑破",
    minInfo && minInfo.headerVisible && minInfo.bodyHeight > 0 && minInfo.scrollW <= 1 && minInfo.statusOrAction,
    { minBox: { w: Math.round(minBox.width), h: Math.round(minBox.height) }, ...minInfo },
  );
  await shot(page, "07b-card-min-size");

  // ── 5c. 对话窗口缩到最小：消息阅读区不被压成一行 ─────────────────────────
  const hMinus = page.locator('[data-im="chat-panel"] [data-resize-key="h-"]');
  for (let i = 0; i < 14; i += 1) await hMinus.click({ force: true }).catch(() => {});
  await page.waitForTimeout(500);
  const streamH = await page.evaluate(() => {
    const stream = document.querySelector('[data-im="chat-stream"]');
    const composer = document.querySelector('[data-im="chat-composer"]');
    return { stream: Math.round(stream?.getBoundingClientRect().height ?? 0), composer: Math.round(composer?.getBoundingClientRect().height ?? 0) };
  });
  record("对话窗口缩到最小时消息阅读区仍保留（不是只剩一行）", streamH.stream >= 60, streamH);
  await shot(page, "07c-chat-min-size");

  await context.close();

  // ── 6. 窄窗口 / 明色 / 减少动画 ──────────────────────────────────────────
  for (const [name, width, height, theme, reduced] of [
    ["08-narrow-1024x768", 1024, 768, "dark", false],
    ["09-narrow-800x600", 800, 600, "dark", false],
    ["10-desktop-light", 1440, 900, "light", false],
    ["11-reduced-motion", 1440, 900, "dark", true],
  ]) {
    // headless Edge 偶尔会在连续开上下文后崩掉：这一档单独重试一次，崩了如实记，不伪装成通过。
    let opened = null;
    for (let attempt = 0; attempt < 2 && !opened; attempt += 1) {
      try {
        opened = await openInteractive(browser, { width, height, theme, reduced });
      } catch (err) {
        console.log("[warn] " + name + " 第 " + (attempt + 1) + " 次打开失败：" + err.message);
        await new Promise((r) => setTimeout(r, 800));
      }
    }
    if (!opened) {
      record(name + " 可以打开", false, { error: "browser context crashed" });
      continue;
    }
    const { context: ctx, page: p } = opened;
    await p.click('[data-im="chat-toggle"]').catch(() => {});
    await p.waitForTimeout(600);
    const geom = await p.evaluate(() => {
      const root = document.documentElement;
      const bar = getComputedStyle(root).getPropertyValue("--titlebar-height").trim();
      const barPx = parseFloat(bar) || 0;
      const board = document.querySelector('[data-im="board"]')?.getBoundingClientRect();
      const toolbar = document.querySelector('[data-im="board-toolbar"]')?.getBoundingClientRect();
      const panel = document.querySelector('[data-im="chat-panel"]')?.getBoundingClientRect();
      const stream = document.querySelector('[data-im="chat-stream"]')?.getBoundingClientRect();
      return {
        titlebar: barPx,
        boardTop: Math.round(board?.top ?? -1),
        overlapTitlebar: panel ? Math.round(barPx - panel.top) : 0,
        panelBottom: panel ? Math.round(panel.bottom) : -1,
        toolbarTop: toolbar ? Math.round(toolbar.top) : -1,
        streamH: Math.round(stream?.height ?? 0),
        streamVisible: board ? Math.round(Math.min(stream?.bottom ?? 0, board.bottom) - Math.max(stream?.top ?? 0, board.top)) : 0,
        overflow: root.scrollWidth - root.clientWidth,
      };
    });
    record(name + " 无页面级横向溢出", geom.overflow <= 1, { overflow: geom.overflow });
    // 0.2.3 自绘标题栏：板面内容与对话窗口都要在标题栏之下，不被压住
    record(
      name + " 自绘标题栏避让（板面与对话窗口都在标题栏之下）",
      geom.boardTop >= geom.titlebar - 1 && geom.overlapTitlebar <= 0,
      { titlebar: geom.titlebar, boardTop: geom.boardTop, titlebarOverlap: geom.overlapTitlebar },
    );
    // 窄窗口是重排/收敛，不是把内容裁掉：消息阅读区仍有可见高度、面板底边不越过工具栏
    record(
      name + " 窄窗口下对话窗口重排而非被裁切",
      geom.streamH >= 60 && geom.streamVisible >= 40 && (geom.toolbarTop < 0 || geom.panelBottom <= geom.toolbarTop + 1),
      { streamH: geom.streamH, streamVisible: geom.streamVisible, panelBottom: geom.panelBottom, toolbarTop: geom.toolbarTop },
    );
    await shot(p, name);
    await ctx.close();
  }

  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log("\n== 汇总 ==");
  console.log("shots: " + OUT);
  console.log("PASS=" + (results.length - failed.length) + " FAIL=" + failed.length);
  if (failed.length) process.exitCode = 1;
}

await main();
