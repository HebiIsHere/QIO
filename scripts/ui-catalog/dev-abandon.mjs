/**
 * 「放弃开发」界面验证台（验证方维护）。
 *
 * 目标：在**真实隔离实例**上把契约 §8 的界面要求逐条实测出来，而不是读源码推断：
 *   1) 30+ 项开发任务（含 ≥300 字长需求、含连续长串）在普通窗口（1440×900）与
 *      窄窗口（820×900）下的滚动：滚轮、键盘（PageDown / End / 方向键）、
 *      拖动滚动条、触控板等价的小步滚轮；
 *   2) 滚动列表**不带动背景对话**（比对对话滚动容器 `.stream` 的 scrollTop，
 *      含「滚到列表底部后继续往下滚」的边界情况）；
 *   3) 无横向溢出（列表 / 长需求行 / 整页）；
 *   4) 最后一项的按钮完整可点（几何 + elementFromPoint 命中）；
 *   5) 滚动到中间/底部时错误提示仍然可见（错误区在滚动容器之外）；
 *   6) 真实后端的放弃成功、重复点击只发一次请求、刷新后列表状态、最后一项移除。
 *
 * 用法（分阶段跑，好把「重启后端」插在中间；见 .verify-tmp/run-ui-verification.mjs 的一键运行器）：
 *
 *   # 0) 起实例并**先清一遍**（--clean 会清空数据目录）
 *   python scripts/ui-catalog/instance.py up --name dev-abandon --backend-port 8841 --frontend-port 6206 --clean
 *   python scripts/ui-catalog/instance.py down --name dev-abandon
 *
 *   # 1) 播种（必须在后端启动前：DevWorkspace 只在构造时扫盘）
 *   node scripts/ui-catalog/dev-abandon.mjs --seed
 *
 *   # 2) 起实例（这次不要 --clean，否则把刚播的种清掉）
 *   python scripts/ui-catalog/instance.py up --name dev-abandon --backend-port 8841 --frontend-port 6206
 *
 *   # 3) 分段采集
 *   node scripts/ui-catalog/dev-abandon.mjs --stage=scroll    # 两视口滚动/溢出/最后一项/错误可见
 *   node scripts/ui-catalog/dev-abandon.mjs --stage=abandon   # 真实后端放弃成功/重复点击/刷新 + 写快照
 *   python scripts/ui-catalog/instance.py down --name dev-abandon
 *   python scripts/ui-catalog/instance.py up --name dev-abandon --backend-port 8841 --frontend-port 6206
 *   node scripts/ui-catalog/dev-abandon.mjs --stage=restart   # 重启之后：不复活、页面里不出现
 *   node scripts/ui-catalog/dev-abandon.mjs --stage=last      # 最后一项移除（会清空未完成列表）
 *
 *   # 4) 收工
 *   python scripts/ui-catalog/instance.py down --name dev-abandon
 *
 * 产物（已被 .gitignore 忽略）：
 *   frontend/e2e-shots/ui-catalog/dev-abandon/*.png        逐状态截图
 *   frontend/e2e-shots/ui-catalog/manifest-dev-abandon.json
 *   frontend/e2e-shots/ui-catalog/dev-abandon-checks.json  逐条 PASS/FAIL/SKIP（分阶段合并）
 *   frontend/e2e-shots/ui-catalog/dev-abandon/dev-abandon-state.json  已放弃/未完成/已提交 id 快照
 *
 * 退出码：任何一条 FAIL → 1（SKIP 不算失败，但会写清为什么跳过）。
 */
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  createSession,
  launchBrowser,
  runGroup,
  sleep,
  DEFAULT_VIEWPORT,
  NARROW_VIEWPORT,
  QIO_ROOT,
  SHOT_ROOT,
} from "./lib.mjs";

const GROUP = "dev-abandon";
const INSTANCE = process.env.QIO_INSTANCE_NAME || "dev-abandon";
const INSTANCE_STATE = path.join(QIO_ROOT, "scripts", "ui-catalog", `.instance-${INSTANCE}.json`);
const SEED_COUNT = Number(process.env.QIO_DEV_ABANDON_COUNT || 32);
/** 隔离实例的数据目录（与 instance.py 的 default_data_dir 同源） */
const DEFAULT_DATA_DIR = path.join(
  process.env.TEMP || os.tmpdir(),
  "qio-ui-catalog",
  INSTANCE,
);

const args = process.argv.slice(2);
const SEED_ONLY = args.includes("--seed");
const dataDir = (() => {
  const flag = args.find((a) => a.startsWith("--data-dir="));
  if (flag) return flag.slice("--data-dir=".length);
  if (existsSync(INSTANCE_STATE)) {
    try {
      const state = JSON.parse(readFileSync(INSTANCE_STATE, "utf-8"));
      if (state.data_dir) return state.data_dir;
    } catch {
      /* 状态文件坏了就退回默认位置 */
    }
  }
  return DEFAULT_DATA_DIR;
})();

// ---------------------------------------------------------------------------
// 检查框架
// ---------------------------------------------------------------------------

const results = [];
let failures = 0;

function record(id, title, outcome, detail = "") {
  const row = { id, title, outcome, detail };
  results.push(row);
  const icon = outcome === "PASS" ? "PASS" : outcome === "SKIP" ? "SKIP" : "FAIL";
  if (outcome === "FAIL") failures += 1;
  console.log(`  [${icon}] ${id} — ${title}${detail ? ` :: ${detail}` : ""}`);
  return outcome === "PASS";
}

async function check(id, title, fn) {
  try {
    const detail = await fn();
    if (typeof detail === "string" && detail.startsWith("SKIP:")) {
      return record(id, title, "SKIP", detail.slice(5).trim());
    }
    return record(id, title, "PASS", detail ?? "");
  } catch (err) {
    return record(id, title, "FAIL", String(err?.message ?? err).slice(0, 400));
  }
}

function skip(id, title, why) {
  record(id, title, "SKIP", why);
}

function assert(cond, message) {
  if (!cond) throw new Error(message);
}

// ---------------------------------------------------------------------------
// 播种：直接写 dev-workspaces/ws_<12hex>/（request.md + tool.json + state.json）
// ---------------------------------------------------------------------------

const LONG_TEXT_BASE =
  "把这份季度报表拆成可查询的结构化数据，并且要能按地区、渠道和时间段交叉汇总，还要保留每一行的来源出处与更新时刻，方便后面回溯。";
const LONG_TEXT_ONE = LONG_TEXT_BASE.repeat(Math.ceil(320 / LONG_TEXT_BASE.length));
/** 连续长串：故意不换行，用来逼横向溢出（URL 型/密钥型输入）。放在需求最前面 ——
 *  `GET /api/dev/tasks` 会把 request 截到 200 字，太靠后就看不到它了。 */
const LONG_UNBROKEN = `https://example.invalid/${"a1b2c3d4e5".repeat(24)}?token=${"f".repeat(120)}`;
const LONG_TEXT_TWO = `${LONG_UNBROKEN}\n需求：${LONG_TEXT_ONE}`;

const PHASES = [
  "created",
  "proposal",
  "building",
  "testing",
  "testing_passed",
  "testing_failed",
  "no_tests_required",
  "waiting_approval",
  "registering",
  "failed",
];

const TOOL_JSON_TEMPLATE =
  "{\n" +
  '  "name": "",\n' +
  '  "description": "",\n' +
  '  "tool_type": "function",\n' +
  '  "sync": true,\n' +
  '  "parameters": {},\n' +
  '  "code": "",\n' +
  '  "entry": "",\n' +
  '  "requirements": [],\n' +
  '  "tests": []\n' +
  "}\n";

/** 与 backend/src/agent/tools/dev_workspace.py::content_digest 同口径的摘要 */
function contentDigest(taskDir) {
  const files = [];
  const walk = (dir, rel) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const nextRel = rel ? `${rel}/${entry.name}` : entry.name;
      const abs = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(abs, nextRel);
      else if (entry.name !== "state.json") files.push([nextRel, abs]);
    }
  };
  walk(taskDir, "");
  files.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const hash = createHash("sha256");
  for (const [rel, abs] of files) {
    hash.update(Buffer.from(rel, "utf-8"));
    hash.update(Buffer.from([0]));
    hash.update(readFileSync(abs));
    hash.update(Buffer.from([0]));
  }
  return hash.digest("hex");
}

function seedTasks(targetDir, count) {
  const root = path.join(targetDir, "dev-workspaces");
  mkdirSync(root, { recursive: true });
  // 只清掉上次自己播的种，不动别人的目录（archive/ 与长期授权文件保留）
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (entry.isDirectory() && /^ws_[0-9a-f]{12}$/.test(entry.name)) {
      rmSync(path.join(root, entry.name), { recursive: true, force: true });
    }
  }
  const seeded = [];
  for (let i = 0; i < count; i += 1) {
    const id = `ws_${(i + 1).toString(16).padStart(4, "0")}${"0123456789ab".slice(0, 8)}`;
    if (!/^ws_[0-9a-f]{12}$/.test(id)) throw new Error(`bad seeded id: ${id}`);
    // 需求文字：前 5 条是超长需求（其中 2 条含连续长串）
    let request;
    if (i === 0) request = LONG_TEXT_TWO;
    else if (i < 5) request = LONG_TEXT_ONE;
    else request = `开发任务 #${i + 1}：把一个本地 CSV 变成可按列筛选的小工具（用于滚动与长文字的界面实测）。`;
    const longText = i < 5;

    const taskDir = path.join(root, id);
    mkdirSync(taskDir, { recursive: true });
    writeFileSync(path.join(taskDir, "request.md"), `# 开发需求\n\n${request}\n`, "utf-8");
    writeFileSync(path.join(taskDir, "tool.json"), TOOL_JSON_TEMPLATE, "utf-8");
    const code = `def run(**kwargs):\n    return {"ok": True, "task": ${i}}\n`;
    writeFileSync(path.join(taskDir, "tool.py"), code, "utf-8");

    const phase = PHASES[i % PHASES.length];
    // 测试结论三态混着造：无证据 / 证据对应当前内容 / 证据已过期
    let evidenceState = "none";
    let lastTestPassed = null;
    let lastTestSummary = null;
    let lastTestAt = null;
    let lastTestDigest = null;
    if (i % 3 === 1) {
      evidenceState = "current";
      lastTestPassed = true;
      lastTestSummary = "1/1 tests passed";
      lastTestAt = new Date(Date.UTC(2026, 8, 28, 10, i % 60)).toISOString();
      lastTestDigest = contentDigest(taskDir);
    } else if (i % 3 === 2) {
      evidenceState = "stale";
      lastTestPassed = false;
      lastTestSummary = "0/1 tests passed";
      lastTestAt = new Date(Date.UTC(2026, 8, 27, 9, i % 60)).toISOString();
      lastTestDigest = "0".repeat(64);
    }
    const submitted = i === count - 1 || i === count - 2; // 2 条已提交：界面不该列成未完成
    const authorized = i % 4 === 0;
    const longTerm = i === 4; // 一条长期授权：用来渲染授权范围行 + 撤销授权按钮
    const authorization = authorized || longTerm
      ? {
          policy_fingerprint: `fp_${i}`,
          executor: "subprocess",
          at: new Date(Date.UTC(2026, 8, 29, 8, i % 60)).toISOString(),
          lifetime: longTerm ? "long_term" : "task",
          content_digest: contentDigest(taskDir),
          filesystem: [],
          network: false,
          network_allow: [],
          credentials: [],
          owner_task_id: id,
          scope: { capabilities: ["受限子进程"], isolated: false },
        }
      : null;

    const createdAt = new Date(Date.UTC(2026, 7, 1 + (i % 27), 6, i % 60)).toISOString();
    const state = {
      // 字段与 dev_workspace.py::_write_state 逐一对齐（schema=2 + source 标记缺一不可，
      // 否则后端按「未知」处理，测试证据会被当成没有）
      schema: 2,
      source: "qio.dev_workspace",
      id,
      request,
      created_at: createdAt,
      phase: submitted ? "submitted" : phase,
      submitted,
      test_runs: lastTestAt ? 1 : 0,
      last_test_passed: lastTestPassed,
      last_test_summary: lastTestSummary,
      last_test_at: lastTestAt,
      last_test_digest: lastTestDigest,
      evidence_state: evidenceState,
      test_authorization: authorization,
      submitted_digest: submitted ? contentDigest(taskDir) : null,
      submitted_at: submitted ? createdAt : null,
      // 本次改动新增的两个键（旧格式任务没有它们，也必须能读回）
      abandoned: false,
      abandoned_at: null,
    };
    writeFileSync(
      path.join(taskDir, "state.json"),
      JSON.stringify(state, null, 2),
      "utf-8",
    );
    seeded.push({ id, longText, submitted, authorized: authorized || longTerm });
  }
  return seeded;
}

function seed() {
  mkdirSync(dataDir, { recursive: true });
  const seeded = seedTasks(dataDir, SEED_COUNT);
  console.log(
    `播种完成：${seeded.length} 个开发任务 → ${path.join(dataDir, "dev-workspaces")}\n` +
      `  超长需求 ${seeded.filter((t) => t.longText).length} 条，已提交 ${seeded.filter((t) => t.submitted).length} 条，` +
      `带授权 ${seeded.filter((t) => t.authorized).length} 条`,
  );
  return seeded;
}

// ---------------------------------------------------------------------------
// 浏览器侧小工具
// ---------------------------------------------------------------------------

async function metrics(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      scrollTop: el.scrollTop,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
      rect: { x: r.x, y: r.y, width: r.width, height: r.height, bottom: r.bottom },
      style: {
        overflowY: getComputedStyle(el).overflowY,
        overscrollBehaviorY: getComputedStyle(el).overscrollBehaviorY,
        maxHeight: getComputedStyle(el).maxHeight,
      },
    };
  }, selector);
}

async function rectOf(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height, bottom: r.bottom, right: r.right };
  }, selector);
}

/** 当前页面里真正的对话滚动容器（MessageStream 的 .stream），拿不到就找最近的滚动祖先 */
async function conversationScroller(page) {
  return page.evaluate(() => {
    const stream = document.querySelector(".stream");
    if (stream && stream.scrollHeight > stream.clientHeight + 2) {
      return { selector: ".stream", scrollTop: stream.scrollTop };
    }
    // 兜底：从 .conversation 往上/往下找一个真的能滚的容器
    const candidates = [...document.querySelectorAll("div")].filter(
      (el) => el.scrollHeight > el.clientHeight + 2 && el.clientHeight > 120,
    );
    if (!candidates.length) return null;
    const el = candidates[0];
    return { selector: "auto", scrollTop: el.scrollTop, className: el.className };
  });
}

async function countRows(page) {
  return page.evaluate(
    () => document.querySelectorAll(".dev-task-panel .dev-task-row").length,
  );
}

async function listScrollTop(page) {
  return page.evaluate(() => document.querySelector(".dev-task-list")?.scrollTop ?? -1);
}

async function scrollListTo(page, where) {
  await page.evaluate((pos) => {
    const list = document.querySelector(".dev-task-list");
    if (!list) return;
    if (pos === "top") list.scrollTop = 0;
    else if (pos === "bottom") list.scrollTop = list.scrollHeight;
    else list.scrollTop = Math.round((list.scrollHeight - list.clientHeight) / 2);
  }, where);
  await sleep(180);
}

async function openEntryPanel(page) {
  await page.waitForSelector(".dev-task-entry", { timeout: 30000 });
  await page.click(".dev-task-entry");
  await page.waitForSelector(".dev-task-panel", { timeout: 10000 });
  await sleep(250);
}

// ---------------------------------------------------------------------------
// 单个视口的检查
// ---------------------------------------------------------------------------

async function runViewportChecks(s, label, expectedCount) {
  const page = s.page;
  console.log(`\n== ${label} 视口 ${s.viewport.width}×${s.viewport.height} ==`);

  await s.goto("#/", { waitFor: ".conversation", settle: 900 });
  await page.waitForSelector(".dev-task-entry", { timeout: 30000 });
  await sleep(300);

  const entryText = await page.textContent(".dev-task-entry");
  await check(`${label}-01-入口计数`, "入口行显示未完成数量", async () => {
    const match = /(\d+)/.exec(entryText ?? "");
    assert(match, `入口文案里没有数字：${entryText}`);
    const n = Number(match[1]);
    assert(n >= 30, `未完成计数 ${n} < 30，播种可能没被后端读到`);
    assert(
      n === expectedCount,
      `界面计数 ${n} 与后端权威列表 ${expectedCount} 不一致`,
    );
    return `计数=${n}（与 /api/dev/tasks 的未完成行数一致）`;
  });
  await s.shot(`${label}-01-entry`, `入口行（${label}）`);

  await openEntryPanel(page);

  await check(`${label}-02-展开面板`, "点入口展开面板，清单渲染出全部未完成行", async () => {
    const rows = await countRows(page);
    assert(rows === expectedCount, `渲染 ${rows} 行，后端有 ${expectedCount} 行`);
    return `行数=${rows}`;
  });
  await check(`${label}-02b-入口常驻`, "面板展开时入口行仍然可见（不抢焦点、不消失）", async () => {
    const visible = await page.isVisible(".dev-task-entry");
    assert(visible, "入口行在展开后消失了");
    return await page.textContent(".dev-task-entry");
  });
  await s.shotEl(".dev-task-panel", `${label}-02-panel`, `展开的任务面板（${label}）`, { pad: 6 });

  const listMetrics = await metrics(page, ".dev-task-list");
  await check(
    `${label}-03-滚动容器`,
    "列表是可滚动容器（overflow-y:auto + overscroll-behavior:contain + 键盘可聚焦）",
    async () => {
      assert(listMetrics, "找不到 .dev-task-list");
      assert(listMetrics.style.overflowY === "auto", `overflow-y=${listMetrics.style.overflowY}`);
      assert(
        listMetrics.style.overscrollBehaviorY === "contain",
        `overscroll-behavior-y=${listMetrics.style.overscrollBehaviorY}`,
      );
      const attrs = await page.evaluate(() => {
        const list = document.querySelector(".dev-task-list");
        return { tabindex: list?.getAttribute("tabindex"), aria: list?.getAttribute("aria-label") };
      });
      assert(attrs.tabindex === "0", `tabindex=${attrs.tabindex}`);
      assert(attrs.aria, "缺少 aria-label");
      assert(
        listMetrics.scrollHeight > listMetrics.clientHeight + 10,
        `列表没溢出（scrollHeight=${listMetrics.scrollHeight} clientHeight=${listMetrics.clientHeight}）`,
      );
      return `scrollHeight=${listMetrics.scrollHeight} clientHeight=${listMetrics.clientHeight}`;
    },
  );

  // ---- 背景对话：必须真的能滚，否则「不带动背景」是空断言
  // 空实例的对话流可能一屏放得下（scrollHeight == clientHeight），这时「不带动」
  // 恒真。先注入若干条消息把背景撑到可滚动，并把它作为一条独立检查（可滚动）。
  await check(
    `${label}-00-背景对话可滚动`,
    "背景对话本身可滚动（否则「滚动不带动背景」是空断言）",
    async () => {
      const before = await page.evaluate(() => {
        const el = document.querySelector(".stream");
        return el ? { sh: el.scrollHeight, ch: el.clientHeight } : null;
      });
      if (!before || before.sh <= before.ch + 40) {
        const filler = "这是一段用来把背景对话撑到可滚动的正文，它需要在窄窗口和普通窗口下都足够长。";
        for (let i = 0; i < 8; i += 1) {
          const turnId = `dev_abandon_bg_${i}`;
          const content = `背景消息 ${i + 1}：${filler.repeat(6)}`;
          await s.inject("TURN_START", { turn_id: turnId, revision: 7000 + i });
          await s.inject("ASSISTANT", { content });
          await s.inject("TURN_END", {
            turn_id: turnId,
            status: "completed",
            revision: 7100 + i,
            final_content: content,
          });
          await sleep(150);
        }
        await sleep(700);
      }
      const after = await page.evaluate(() => {
        const el = document.querySelector(".stream");
        if (!el) return null;
        el.scrollTop = Math.min(160, el.scrollHeight - el.clientHeight);
        return { sh: el.scrollHeight, ch: el.clientHeight, top: el.scrollTop };
      });
      assert(after, "找不到对话滚动容器 .stream");
      assert(
        after.sh > after.ch + 40,
        `注入后背景对话仍不可滚动（scrollHeight=${after.sh} clientHeight=${after.ch}）`,
      );
      return `背景 scrollHeight=${after.sh} clientHeight=${after.ch}`;
    },
  );

  const bgBefore = await page.evaluate(() => {
    const stream = document.querySelector(".stream");
    if (!stream) return null;
    stream.scrollTop = Math.min(160, Math.max(0, stream.scrollHeight - stream.clientHeight));
    return {
      scrollTop: stream.scrollTop,
      scrollHeight: stream.scrollHeight,
      clientHeight: stream.clientHeight,
    };
  });
  const bgSelector = bgBefore ? ".stream" : (await conversationScroller(page))?.selector ?? null;

  async function bgScrollTop() {
    if (!bgSelector) return -1;
    if (bgSelector === ".stream") return page.evaluate(() => document.querySelector(".stream").scrollTop);
    return page.evaluate(() => {
      const el = [...document.querySelectorAll("div")].find(
        (d) => d.scrollHeight > d.clientHeight + 2 && d.clientHeight > 120,
      );
      return el ? el.scrollTop : -1;
    });
  }

  // ---- 滚轮（含触控板等价的小步）
  await check(`${label}-04-滚轮滚动`, "鼠标滚轮（含小步触控板等价）能滚动列表", async () => {
    await scrollListTo(page, "top");
    const before = await listScrollTop(page);
    const box = await rectOf(page, ".dev-task-list");
    await page.mouse.move(box.x + box.width / 2, box.y + Math.min(120, box.height / 2));
    for (let i = 0; i < 3; i += 1) await page.mouse.wheel(0, 120); // 触控板：小步
    await sleep(200);
    await page.mouse.wheel(0, 400); // 鼠标滚轮：一大格
    await sleep(250);
    const after = await listScrollTop(page);
    assert(after > before + 20, `滚动前后 scrollTop 没变：${before} → ${after}`);
    return `scrollTop ${before} → ${after}`;
  });

  await check(`${label}-05-滚轮不带动背景`, "列表里滚动时背景对话的 scrollTop 不变", async () => {
    if (!bgSelector) return "找不到对话滚动容器：跳过（不算通过）";
    const before = await bgScrollTop();
    const box = await rectOf(page, ".dev-task-list");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, 300);
    await sleep(250);
    const after = await bgScrollTop();
    assert(before === after, `背景对话被带着滚了：${before} → ${after}`);
    return `背景 scrollTop 保持 ${before}`;
  });

  await check(
    `${label}-06-滚到底后继续滚不穿透`,
    "列表滚到底部后继续往下滚，背景对话仍然不动（overscroll-behavior: contain 的作用点）",
    async () => {
      if (!bgSelector) return "找不到对话滚动容器：跳过";
      await scrollListTo(page, "bottom");
      const bgBeforeBottom = await bgScrollTop();
      const listBefore = await listScrollTop(page);
      const box = await rectOf(page, ".dev-task-list");
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      for (let i = 0; i < 4; i += 1) await page.mouse.wheel(0, 400);
      await sleep(300);
      const bgAfter = await bgScrollTop();
      const listAfter = await listScrollTop(page);
      assert(bgBeforeBottom === bgAfter, `背景对话被穿透滚动了：${bgBeforeBottom} → ${bgAfter}`);
      assert(listAfter >= listBefore, `列表回弹了：${listBefore} → ${listAfter}`);
      return `列表停在 ${listAfter}，背景保持 ${bgAfter}`;
    },
  );

  // ---- 键盘
  await check(`${label}-07-键盘滚动`, "键盘 PageDown / End / 方向键能滚动列表", async () => {
    await scrollListTo(page, "top");
    await page.focus(".dev-task-list");
    const start = await listScrollTop(page);
    await page.keyboard.press("PageDown");
    await sleep(250);
    const afterPageDown = await listScrollTop(page);
    assert(afterPageDown > start, `PageDown 没有滚动：${start} → ${afterPageDown}`);
    await page.keyboard.press("End");
    await sleep(300);
    const afterEnd = await listScrollTop(page);
    const max = await page.evaluate(() => {
      const list = document.querySelector(".dev-task-list");
      return list.scrollHeight - list.clientHeight;
    });
    assert(afterEnd >= max - 4, `End 没有到到底部：${afterEnd} / ${max}`);
    await page.keyboard.press("ArrowUp");
    await sleep(200);
    const afterUp = await listScrollTop(page);
    assert(afterUp < afterEnd, `方向键没有滚动：${afterEnd} → ${afterUp}`);
    return `0 → PageDown ${afterPageDown} → End ${afterEnd} → ArrowUp ${afterUp}`;
  });

  await check(`${label}-08-键盘滚动不带动背景`, "键盘滚动列表时背景对话不动", async () => {
    if (!bgSelector) return "找不到对话滚动容器：跳过";
    const before = await bgScrollTop();
    await page.focus(".dev-task-list");
    await page.keyboard.press("Home");
    await page.keyboard.press("PageDown");
    await sleep(250);
    const after = await bgScrollTop();
    assert(before === after, `背景对话被带动：${before} → ${after}`);
    return `背景 scrollTop 保持 ${before}`;
  });

  // ---- 拖动滚动条
  await check(`${label}-09-拖动滚动条`, "用鼠标真的拖动滚动条能滚动列表", async () => {
    await scrollListTo(page, "top");
    const box = await rectOf(page, ".dev-task-list");
    const barWidth = await page.evaluate(() => {
      const list = document.querySelector(".dev-task-list");
      return list.offsetWidth - list.clientWidth;
    });
    if (barWidth <= 0) return "SKIP: 无经典滚动条（offsetWidth==clientWidth），改由滚轮/键盘/触控板覆盖";
    const before = await listScrollTop(page);
    const x = box.x + box.width - Math.max(2, barWidth / 2);
    await page.mouse.move(x, box.y + 30);
    await page.mouse.down();
    await page.mouse.move(x, box.y + box.height - 30, { steps: 12 });
    await page.mouse.up();
    await sleep(300);
    const after = await listScrollTop(page);
    assert(after > before + 20, `拖动滚动条后 scrollTop 没变：${before} → ${after}`);
    return `scrollTop ${before} → ${after}（滚动条宽 ${barWidth}px）`;
  });

  // ---- 横向溢出
  await check(`${label}-10-无横向溢出`, "列表 / 长需求行 / 整页都没有横向溢出", async () => {
    const report = await page.evaluate(() => {
      const list = document.querySelector(".dev-task-list");
      const panel = document.querySelector(".dev-task-panel");
      const rows = [...document.querySelectorAll(".dev-task-row")];
      const overflowRows = rows
        .map((row, index) => ({
          index,
          over: row.scrollWidth - row.clientWidth,
          text: (row.querySelector(".dev-task-request")?.textContent ?? "").slice(0, 40),
        }))
        .filter((row) => row.over > 1);
      const doc = document.documentElement;
      const panelRect = panel.getBoundingClientRect();
      return {
        listOver: list.scrollWidth - list.clientWidth,
        docOver: doc.scrollWidth - doc.clientWidth,
        bodyOver: document.body.scrollWidth - document.body.clientWidth,
        panelRight: panelRect.right,
        panelLeft: panelRect.left,
        viewportWidth: window.innerWidth,
        overflowRows,
        requestWrap: getComputedStyle(
          document.querySelector(".dev-task-request") ?? document.body,
        ).overflowWrap,
      };
    });
    assert(report.listOver <= 1, `列表横向溢出 ${report.listOver}px`);
    assert(report.docOver <= 1, `整页横向溢出 ${report.docOver}px`);
    assert(report.bodyOver <= 1, `body 横向溢出 ${report.bodyOver}px`);
    assert(
      report.panelRight <= report.viewportWidth + 1 && report.panelLeft >= -1,
      `面板越出视口：left=${report.panelLeft} right=${report.panelRight} vw=${report.viewportWidth}`,
    );
    assert(report.overflowRows.length === 0, `有行横向溢出：${JSON.stringify(report.overflowRows)}`);
    return `列表/整页/body 溢出 0px；面板 right=${Math.round(report.panelRight)} ≤ ${report.viewportWidth}；overflow-wrap=${report.requestWrap}`;
  });

  await check(
    `${label}-11-长需求文字`,
    "长需求（含连续长串）完整渲染，不撑破面板",
    async () => {
      // 先把列表滚到长需求那一行，截图才拍得到它（证据要能看出被验证的东西）
      await page.evaluate(() => {
        const rows = [...document.querySelectorAll(".dev-task-row")];
        const longRow = rows.find(
          (row) => (row.querySelector(".dev-task-request")?.textContent ?? "").length >= 150,
        );
        if (longRow) longRow.scrollIntoView({ block: "center" });
      });
      await sleep(250);
      const report = await page.evaluate(() => {
        const rows = [...document.querySelectorAll(".dev-task-row")];
        const requests = rows
          .map((row) => row.querySelector(".dev-task-request")?.textContent ?? "")
          .filter(Boolean);
        // 后端把 request 截到 200 字（见 server.py 的 /api/dev/tasks），
        // 所以「长」的判据是「拿满了 200 字的窗口」，而不是 ≥300。
        const longest = requests.reduce((a, b) => (b.length > a.length ? b : a), "");
        const unbrokenRow = rows.find((row) =>
          (row.querySelector(".dev-task-request")?.textContent ?? "").includes(
            "a1b2c3d4e5a1b2c3d4e5",
          ),
        );
        const rect = unbrokenRow?.getBoundingClientRect() ?? null;
        const panelRect = document.querySelector(".dev-task-panel").getBoundingClientRect();
        return {
          longestLength: longest.length,
          hasUnbrokenRow: !!unbrokenRow,
          unbrokenInsidePanel: rect
            ? rect.left >= panelRect.left - 1 && rect.right <= panelRect.right + 1
            : false,
          seedLongRequests: requests.filter((text) => text.length >= 150).length,
        };
      });
      assert(
        report.seedLongRequests >= 5,
        `渲染出来的长需求只有 ${report.seedLongRequests} 条（播种要求 ≥5 条）`,
      );
      assert(report.longestLength >= 150, `最长的需求文字只有 ${report.longestLength} 字`);
      assert(report.hasUnbrokenRow, "没有找到含连续长串的需求行（播种没生效）");
      assert(report.unbrokenInsidePanel, "连续长串那一行超出了面板宽度");
      return `长需求 ${report.seedLongRequests} 条（最长 ${report.longestLength} 字，后端截到 200 字），连续长串在面板内`;
    },
  );
  await s.shotEl(".dev-task-panel", `${label}-11-longtext`, `长需求文字（${label}）`, { pad: 6 });

  // ---- 最后一项按钮可达
  await check(`${label}-12-最后一项按钮可点`, "滚到底部时最后一行的按钮完整可点", async () => {
    await scrollListTo(page, "bottom");
    await sleep(250);
    const hit = await page.evaluate(() => {
      const rows = [...document.querySelectorAll(".dev-task-row")];
      const last = rows[rows.length - 1];
      const buttons = [...last.querySelectorAll("button")];
      const btn = buttons[buttons.length - 1];
      btn.scrollIntoView({ block: "nearest" });
      const r = btn.getBoundingClientRect();
      const px = r.left + r.width / 2;
      const py = r.top + r.height / 2;
      const top = document.elementFromPoint(px, py);
      const panelRect = document.querySelector(".dev-task-panel").getBoundingClientRect();
      return {
        label: btn.textContent.trim(),
        insideViewport:
          r.top >= 0 && r.bottom <= window.innerHeight && r.left >= 0 && r.right <= window.innerWidth,
        insidePanel: r.bottom <= panelRect.bottom + 1,
        hitSelf: !!top && (top === btn || btn.contains(top)),
        hitClass: top ? `${top.tagName}.${top.className}` : null,
        bottom: r.bottom,
        viewport: window.innerHeight,
      };
    });
    assert(hit.insideViewport, `按钮不在视口内：bottom=${hit.bottom} / ${hit.viewport}`);
    assert(hit.insidePanel, "按钮超出了面板底边（底部内边距不够）");
    assert(hit.hitSelf, `按钮被别的元素盖住：elementFromPoint=${hit.hitClass}`);
    return `最后一行按钮「${hit.label}」完整可见且命中自身`;
  });
  await s.shotEl(".dev-task-panel", `${label}-12-lastitem`, `滚到底部的最后一项（${label}）`, { pad: 6 });

  await check(
    `${label}-17-滚到底后入口仍可见`,
    "列表滚到最底部后，顶部入口行仍然可见、没被面板挤走",
    async () => {
      const state = await page.evaluate(() => {
        const entry = document.querySelector(".dev-task-entry");
        if (!entry) return null;
        const r = entry.getBoundingClientRect();
        return {
          visible: r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= window.innerHeight,
          top: r.top,
          text: entry.textContent.trim(),
        };
      });
      assert(state, "找不到入口行 .dev-task-entry");
      assert(state.visible, `入口行不在视口内：top=${state.top}`);
      return `滚到底后入口仍可见（top=${Math.round(state.top)}）：「${state.text}」`;
    },
  );

  // ---- 错误提示：真实浏览器路径（让这次 POST 失败）→ 就地显示原因
  await check(
    `${label}-13-错误提示在滚动容器之外且滚动后仍可见`,
    "放弃失败时就地显示原因，滚到中间/底部仍然看得见",
    async () => {
      await s.page.route("**/api/dev/tasks/*/abandon", (route) => route.abort("failed"));
      try {
        await scrollListTo(page, "top");
        await page.click(".dev-task-row .dev-task-abandon");
        await page.waitForSelector(".qio-confirm", { timeout: 8000 });
        await page.click(".qio-confirm button.danger-solid");
        await page.waitForSelector(".dev-task-error", { timeout: 10000 });
        await sleep(300);

        const structural = await page.evaluate(() => {
          const error = document.querySelector(".dev-task-error");
          const list = document.querySelector(".dev-task-list");
          const panel = document.querySelector(".dev-task-panel");
          return {
            role: error.getAttribute("role"),
            className: error.className,
            insideList: list.contains(error),
            insidePanel: panel.contains(error),
            text: error.textContent.trim().slice(0, 120),
          };
        });
        assert(structural.role === "alert", `role=${structural.role}`);
        assert(structural.insideList === false, "错误区在滚动容器里面（滚下去就看不到了）");
        assert(structural.insidePanel === true, "错误区不在面板里");

        const visibility = [];
        for (const where of ["middle", "bottom"]) {
          await scrollListTo(page, where);
          await sleep(200);
          const box = await rectOf(page, ".dev-task-error");
          const visible = await page.isVisible(".dev-task-error");
          visibility.push({ where, box, visible });
          assert(visible, `滚到 ${where} 后错误提示不可见`);
          assert(
            box.bottom <= s.viewport.height + 1 && box.y >= -1,
            `滚到 ${where} 后错误提示在视口外：y=${box.y} bottom=${box.bottom}`,
          );
        }
        await s.shotEl(".dev-task-panel", `${label}-13-error-visible`, `滚动后错误提示仍可见（${label}）`, { pad: 6 });
        return `置顶错误区：role=alert，在 .dev-task-list 之外；滚到 middle/bottom 都可见；文案「${structural.text}」`;
      } finally {
        await s.page.unroute("**/api/dev/tasks/*/abandon");
      }
    },
  );

  // ---- 面板最大高度：必须是「随窗口可用高度变化」的视口相对值，不能是固定像素
  await check(
    `${label}-14-面板高度随视口`,
    "面板 max-height 声明为视口相对值（vh/dvh/calc），且不超过窗口高度",
    async () => {
      const report = await page.evaluate(() => {
        const declared = [];
        for (const sheet of document.styleSheets) {
          let rules;
          try {
            rules = sheet.cssRules;
          } catch {
            continue; // 跨源表读不到就跳过
          }
          for (const rule of rules) {
            if (rule.selectorText && rule.selectorText.includes(".dev-task-panel")) {
              const mh = rule.style?.getPropertyValue("max-height");
              if (mh) declared.push({ selector: rule.selectorText, maxHeight: mh.trim() });
            }
          }
        }
        const el = document.querySelector(".dev-task-panel");
        return {
          declared,
          computed: getComputedStyle(el).maxHeight,
          panelHeight: el.getBoundingClientRect().height,
          viewport: window.innerHeight,
        };
      });
      assert(report.declared.length > 0, "没有找到 .dev-task-panel 的 max-height 声明");
      const relative = report.declared.filter((row) => /vh|dvh|svh|lvh|calc\(/.test(row.maxHeight));
      assert(
        relative.length > 0,
        `面板最大高度是固定像素：${JSON.stringify(report.declared)}`,
      );
      const computedPx = Number.parseFloat(report.computed);
      if (Number.isFinite(computedPx)) {
        assert(
          computedPx <= report.viewport + 1,
          `面板 max-height=${report.computed} 超过窗口高度 ${report.viewport}px`,
        );
      }
      return `声明 ${relative.map((r) => `${r.selector}: ${r.maxHeight}`).join(" | ")}；计算值 ${report.computed}；面板高 ${Math.round(report.panelHeight)} / 视口 ${report.viewport}`;
    },
  );

  return { bgSelector, listMetrics };
}

// ---------------------------------------------------------------------------
// 真实后端上的放弃流程（成功 / 重复点击 / 刷新 / 最后一项）
// ---------------------------------------------------------------------------

async function runAbandonFlow(s, api, base) {
  const page = s.page;
  console.log(`\n== 真实后端放弃流程（${s.viewport.width}×${s.viewport.height}） ==`);

  await s.goto("#/", { waitFor: ".conversation", settle: 900 });
  await page.waitForSelector(".dev-task-entry", { timeout: 30000 });
  await openEntryPanel(page);

  const posts = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && /\/api\/dev\/tasks\/[^/]+\/abandon$/.test(req.url())) {
      posts.push(req.url());
    }
  });

  const rowsBefore = await countRows(page);

  await check("flow-01-放弃成功", "点「放弃开发」→ 确认 → 该行消失、计数减一", async () => {
    await page.click(".dev-task-row .dev-task-abandon");
    await page.waitForSelector(".qio-confirm", { timeout: 8000 });
    const confirmCopy = await page.textContent(".qio-confirm");
    assert(confirmCopy.includes("放弃开发"), "确认层里没有「放弃开发」按钮文案");
    for (const phrase of ["未完成", "保留", "不删除已注册的工具", "授权", "失效"]) {
      assert(confirmCopy.includes(phrase), `确认文案缺「${phrase}」：${confirmCopy}`);
    }

    // 长需求文字的确认层不能把页面撑宽
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      const box = document.querySelector(".qio-confirm").getBoundingClientRect();
      return {
        docOver: doc.scrollWidth - doc.clientWidth,
        left: box.left,
        right: box.right,
        vw: window.innerWidth,
      };
    });
    assert(overflow.docOver <= 1, `打开确认层后整页横向溢出 ${overflow.docOver}px`);
    assert(
      overflow.left >= -1 && overflow.right <= overflow.vw + 1,
      `确认层越出视口：left=${overflow.left} right=${overflow.right} vw=${overflow.vw}`,
    );
    await s.shotEl(".qio-confirm", "flow-00-confirm", "放弃开发的确认层（含该项需求）", { pad: 8 });

    // 连点两次：第二次不该再发请求（按钮已禁用 / 正在放弃）
    await page.click(".qio-confirm button.danger-solid");
    await page
      .click(".qio-confirm button.danger-solid", { timeout: 1200, force: true })
      .catch(() => {});
    await page.waitForFunction(
      (n) => document.querySelectorAll(".dev-task-panel .dev-task-row").length === n - 1,
      rowsBefore,
      { timeout: 15000 },
    );
    await sleep(300);
    const rowsAfter = await countRows(page);
    assert(rowsAfter === rowsBefore - 1, `行数 ${rowsBefore} → ${rowsAfter}`);
    assert(posts.length === 1, `重复点击发出了 ${posts.length} 个放弃请求（应为 1）`);
    return `行数 ${rowsBefore} → ${rowsAfter}，POST 次数=${posts.length}`;
  });
  await s.shot(`${s.group}-flow-01-chat`, `放弃成功后的对话页（${page.viewportSize().width}）`);

  await check("flow-02-后端事实一致", "刷新前后端里的那一行已经是 abandoned=true", async () => {
    const listed = await fetch(`${api}/api/dev/tasks`).then((r) => r.json());
    const abandonedRows = listed.tasks.filter((row) => row.abandoned === true);
    assert(abandonedRows.length >= 1, "后端列表里没有 abandoned 行");
    assert(
      listed.tasks.some((row) => row.abandoned === true && row.abandoned_at),
      "abandoned_at 为空",
    );
    return `列表共 ${listed.tasks.length} 行，其中已放弃 ${abandonedRows.length} 行`;
  });

  await check("flow-03-刷新后不再出现", "刷新页面后：已放弃的条目仍然不在未完成列表里", async () => {
    const beforeReload = await countRows(page);
    await s.goto("#/", { waitFor: ".conversation", settle: 900 });
    await page.waitForSelector(".dev-task-entry", { timeout: 30000 });
    await openEntryPanel(page);
    const afterReload = await countRows(page);
    assert(afterReload === beforeReload, `刷新后行数变了：${beforeReload} → ${afterReload}`);
    const abandonedVisible = await page.evaluate(() => {
      const rows = [...document.querySelectorAll(".dev-task-row")];
      return rows.filter((row) => row.textContent.includes("已放弃")).length;
    });
    assert(abandonedVisible === 0, `刷新后列表里出现了 ${abandonedVisible} 条「已放弃」`);
    return `刷新后行数保持 ${afterReload}，没有已放弃条目混进来`;
  });

  return { rowsBefore };
}

/** 最后一项移除：先用 HTTP 把别的任务全放弃，再在界面上点掉最后一条 */
async function runLastItemFlow(s, api) {
  const page = s.page;
  console.log(`\n== 最后一项移除（${s.viewport.width}×${s.viewport.height}） ==`);

  const listed = await fetch(`${api}/api/dev/tasks`).then((r) => r.json());
  const unfinished = listed.tasks.filter((row) => !row.submitted && !row.abandoned);
  for (const row of unfinished.slice(0, -1)) {
    await fetch(`${api}/api/dev/tasks/${row.id}/abandon`, { method: "POST" });
  }
  const remaining = unfinished.length ? unfinished[unfinished.length - 1] : null;
  assert(remaining, "没有剩下任何未完成任务：播种或前置放弃流程出了问题");

  await s.goto("#/", { waitFor: ".conversation", settle: 900 });
  await page.waitForSelector(".dev-task-entry", { timeout: 30000 });
  await check("last-01-只剩一项", "只剩 1 项未完成时入口显示「有 1 个」", async () => {
    const text = await page.textContent(".dev-task-entry");
    assert(text.includes("有 1 个"), `入口文案：${text}`);
    return text.trim();
  });

  await openEntryPanel(page);
  await check("last-02-移除最后一项后面板收起、入口消失", "放弃最后一项 → 面板收起、入口整行消失", async () => {
    await page.click(".dev-task-row .dev-task-abandon");
    await page.waitForSelector(".qio-confirm", { timeout: 8000 });
    await page.click(".qio-confirm button.danger-solid");
    await page.waitForFunction(() => !document.querySelector(".dev-task-entry"), null, {
      timeout: 15000,
    });
    await sleep(300);
    const state = await page.evaluate(() => ({
      entry: !!document.querySelector(".dev-task-entry"),
      panel: !!document.querySelector(".dev-task-panel"),
      wrap: !!document.querySelector(".dev-task-wrap"),
    }));
    assert(state.entry === false, "入口还在");
    assert(state.panel === false, "面板没有收起");
    assert(state.wrap === false, "入口容器还在");
    return "入口 / 面板 / 容器全部消失";
  });
  await s.shot(`${s.group}-last-item-gone`, "移除最后一项后的对话页（入口消失）");
}

// ---------------------------------------------------------------------------
// 主流程：按 --stage 分阶段执行，好让「重启后端」这类跨进程步骤插在中间
//   scroll  —— 两视口滚动/溢出/最后一项/错误可见（不改数据）
//   abandon —— 真实后端放弃成功/重复点击/刷新（会放弃 1 项）
//   restart —— 后端进程重启之后：已放弃的不复活、页面里不出现（读 abandon 阶段的快照）
//   last    —— 最后一项移除（会清空未完成列表，放最后）
//   all     —— 顺序跑 scroll → abandon → last（不插重启，便于快速自测）
// ---------------------------------------------------------------------------

const STAGE = (() => {
  const flag = args.find((a) => a.startsWith("--stage="));
  return flag ? flag.slice("--stage=".length) : "all";
})();

const SNAPSHOT_FILE = path.join(SHOT_ROOT, GROUP, "dev-abandon-state.json");
const CHECKS_FILE = path.join(SHOT_ROOT, `${GROUP}-checks.json`);

function persistChecks(stageName, meta) {
  mkdirSync(path.join(SHOT_ROOT, GROUP), { recursive: true });
  let merged = [...results];
  let stages = [stageName];
  if (existsSync(CHECKS_FILE)) {
    try {
      const prev = JSON.parse(readFileSync(CHECKS_FILE, "utf-8"));
      const byId = new Map((prev.checks ?? []).map((row) => [row.id, row]));
      for (const row of results) byId.set(row.id, row);
      merged = [...byId.values()];
      stages = [...new Set([...(prev.stages ?? []), stageName])];
    } catch {
      /* 之前那份坏了就只写本轮 */
    }
  }
  const failed = merged.filter((row) => row.outcome === "FAIL").length;
  writeFileSync(
    CHECKS_FILE,
    JSON.stringify(
      { ...meta, updatedAt: new Date().toISOString(), stages, total: merged.length, failed, checks: merged },
      null,
      2,
    ),
    "utf-8",
  );
  console.log(`\n[${stageName}] 本轮 ${results.length} 条；累计 ${merged.length} 条，FAIL ${failed} 条 → ${CHECKS_FILE}`);
  return failed;
}

function readSnapshot() {
  if (!existsSync(SNAPSHOT_FILE)) return null;
  try {
    return JSON.parse(readFileSync(SNAPSHOT_FILE, "utf-8"));
  } catch {
    return null;
  }
}

function writeSnapshot(base, api) {
  return fetch(`${api}/api/dev/tasks`)
    .then((r) => r.json())
    .then((listed) => {
      const snapshot = {
        generatedAt: new Date().toISOString(),
        base,
        api,
        dataDir,
        abandoned_ids: listed.tasks.filter((row) => row.abandoned).map((row) => row.id),
        unused_ids: [],
        unfinished_ids: listed.tasks
          .filter((row) => !row.submitted && !row.abandoned)
          .map((row) => row.id),
        submitted_ids: listed.tasks.filter((row) => row.submitted).map((row) => row.id),
        abandoned_rows: listed.tasks.filter((row) => row.abandoned),
      };
      mkdirSync(path.join(SHOT_ROOT, GROUP), { recursive: true });
      writeFileSync(SNAPSHOT_FILE, JSON.stringify(snapshot, null, 2), "utf-8");
      return snapshot;
    });
}

/** 后端进程重启之后：已放弃的不复活；页面里不再出现；已提交的不受影响 */
async function verifyAfterRestart(browser, base, api) {
  const snapshot = readSnapshot();
  const listed = await fetch(`${api}/api/dev/tasks`).then((r) => r.json());
  const byId = new Map(listed.tasks.map((row) => [row.id, row]));

  await check("restart-01-后端事实不变", "重启后：快照里已放弃的行仍 abandoned=true，已提交的行仍未提交/未放弃", async () => {
    assert(snapshot, `没有快照文件 ${SNAPSHOT_FILE}（先跑 --stage=abandon）`);
    for (const id of snapshot.abandoned_ids) {
      const row = byId.get(id);
      assert(row, `已放弃的 ${id} 在重启后从列表里消失了`);
      assert(row.abandoned === true, `${id} 重启后不是 abandoned`);
      assert(row.abandoned_at, `${id} 重启后 abandoned_at 为空`);
    }
    for (const id of snapshot.submitted_ids) {
      const row = byId.get(id);
      assert(row, `已提交的 ${id} 不见了`);
      assert(row.submitted === true, `${id} 重启后不再是已提交`);
      assert(row.abandoned === false, `${id} 被误标成已放弃`);
    }
    return `已放弃 ${snapshot.abandoned_ids.length} 行仍为 abandoned；已提交 ${snapshot.submitted_ids.length} 行未受影响`;
  });

  const s = await createSession(browser, {
    group: GROUP,
    name: "重启之后",
    theme: "dark",
    viewport: DEFAULT_VIEWPORT,
    base,
    api,
  });
  try {
    await s.goto("#/", { waitFor: ".conversation", settle: 900 });
    await check("restart-02-页面里不复活", "重启后刷新页面：已放弃的条目一条都不出现（未完成数量与后端一致）", async () => {
      const expected = listed.tasks.filter((row) => !row.submitted && !row.abandoned).length;
      if (expected === 0) {
        const entry = await page_present(s.page, ".dev-task-entry");
        assert(entry === false, "后端已无未完成任务，但入口行还在");
        return "未完成为 0：入口行不存在（与后端一致）";
      }
      await s.page.waitForSelector(".dev-task-entry", { timeout: 30000 });
      await openEntryPanel(s.page);
      const rows = await countRows(s.page);
      assert(rows === expected, `页面 ${rows} 行 ≠ 后端未完成 ${expected} 行`);
      const leaked = await s.page.evaluate(
        (ids) => ids.filter((id) => document.body.innerHTML.includes(id)).length,
        snapshot.abandoned_ids,
      );
      assert(leaked === 0, `页面里出现了 ${leaked} 个已放弃任务的 id`);
      return `页面 ${rows} 行 = 后端未完成 ${expected} 行；已放弃的 id 一个都没出现`;
    });
    await s.shot(`${s.group}-restart-after`, "后端进程重启之后的对话页（已放弃的不复活）");
  } finally {
    await s.close();
  }
}

async function page_present(page, selector) {
  return page.evaluate((sel) => !!document.querySelector(sel), selector);
}

// ---------------------------------------------------------------------------
// 拖动滚动条（有头窗口；含「对照组」以区分产品缺陷与合成鼠标事件的限制）
//
// 无头 Chromium 用 overlay 滚动条（不占布局，offsetWidth==clientWidth），没有可拖的轨道；
// 有头 Edge 才有经典滚动条（与 QIO 外壳的 WebView2 一致）。这里：
//   1) 用几何算出滑块（thumb）位置，从滑块**中心**按下拖动（点在轨道上只会翻页，不是拖动）；
//   2) 对列表与「背景对话自己的滚动条」做**同样的**拖动 —— 如果两者都失败，
//      说明是合成鼠标事件/Chromium 的限制，而不是 QIO 列表的缺陷；
//   3) 顺带测一次「点轨道翻页」，证明滚动条确实可交互。
// ---------------------------------------------------------------------------

async function measureScrollbar(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const gutter = el.offsetWidth - el.clientWidth;
    const maxScroll = el.scrollHeight - el.clientHeight;
    const trackHeight = r.height;
    const thumbH = Math.max(24, (el.clientHeight / el.scrollHeight) * trackHeight);
    const thumbTop = r.y + (maxScroll > 0 ? (el.scrollTop / maxScroll) * (trackHeight - thumbH) : 0);
    return {
      rect: { x: r.x, y: r.y, width: r.width, height: r.height },
      gutter,
      scrollTop: el.scrollTop,
      maxScroll,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      thumb: { top: thumbTop, height: thumbH, centerY: thumbTop + thumbH / 2 },
      x: r.x + r.width - Math.max(3, gutter / 2),
    };
  }, selector);
}

async function dragScrollbar(page, selector, toFraction) {
  const m = await measureScrollbar(page, selector);
  if (!m || m.gutter <= 0) return { ok: false, reason: "没有经典滚动条", m };
  // 目标点必须以**轨道**为基准。曾经的写法用「当前滑块位置」插值，
  // 于是 toFraction=0 时目标点 == 起点（拖动在构造上就是原地不动）——
  // 这会伪装成「反向拖动不生效」的产品缺陷。这里按轨道算，并加一道自检。
  const trackTop = m.rect.y;
  const travel = Math.max(1, m.rect.height - m.thumb.height);
  const targetY = trackTop + travel * toFraction + m.thumb.height / 2;
  if (Math.abs(targetY - m.thumb.centerY) < 8) {
    return { ok: false, reason: `目标点与起点重合（脚本问题：fraction=${toFraction}，滑块中心=${Math.round(m.thumb.centerY)}）`, m };
  }
  await page.mouse.move(m.x, m.thumb.centerY);
  await page.mouse.down();
  const steps = 14;
  for (let i = 1; i <= steps; i += 1) {
    await page.mouse.move(m.x, m.thumb.centerY + ((targetY - m.thumb.centerY) * i) / steps);
    await sleep(18);
  }
  await page.mouse.up();
  await sleep(260);
  const after = await page.evaluate((sel) => document.querySelector(sel).scrollTop, selector);
  return { ok: true, m, before: m.scrollTop, after, targetY, toFraction };
}

async function runScrollbarChecks(browser, base, api, expectedCount) {
  const s = await createSession(browser, {
    group: GROUP,
    name: "滚动条拖动（有头）",
    theme: "dark",
    viewport: DEFAULT_VIEWPORT,
    base,
    api,
  });
  const page = s.page;
  try {
    await s.goto("#/", { waitFor: ".conversation", settle: 900 });
    await page.waitForSelector(".dev-task-entry", { timeout: 30000 });
    await openEntryPanel(page);
    await ensureConversationScrollable(s, page);

    const list = await measureScrollbar(page, ".dev-task-list");
    await check("sb-01-经典滚动条存在", "有头窗口里列表占到一条经典滚动条（宽度 = 轨道）", async () => {
      assert(list, "找不到 .dev-task-list");
      assert(list.gutter > 0, `offsetWidth-clientWidth=${list.gutter}（0 表示 overlay 滚动条）`);
      return `轨道≈${list.gutter}px；滑块高≈${Math.round(list.thumb.height)}px；轨道高≈${Math.round(list.rect.height)}px`;
    });
    await check("sb-02-内容确实溢出", "列表内容高于可视区（滚动条真的需要）", async () => {
      assert(list.scrollHeight > list.clientHeight + 10, "列表没溢出");
      return `scrollHeight=${list.scrollHeight} > clientHeight=${list.clientHeight}`;
    });

    const listMetrics = await metrics(page, ".dev-task-list");
    if (list.gutter <= 0) {
      skip("sb-03-拖滑块", "拖动列表滚动条滑块：列表滚动", `没有经典滚动条（gutter=0），无法拖动`);
    } else {
      await check("sb-03-拖滑块", "从滑块中心按住往下拖 → 列表确实滚动了", async () => {
        await page.evaluate(() => {
          document.querySelector(".dev-task-list").scrollTop = 0;
        });
        await sleep(150);
        const r = await dragScrollbar(page, ".dev-task-list", 0.6);
        assert(r.ok, r.reason ?? "拖动失败");
        assert(r.after > r.before + 20, `拖动后 scrollTop 没变：${r.before} → ${r.after}`);
        return `scrollTop ${r.before} → ${Math.round(r.after)}（滑块 ${Math.round(r.m.thumb.top)} → 目标 ${Math.round(r.targetY)}）`;
      });
      await check("sb-04-拖到底部", "拖到轨道底部 → 列表到 maxScroll", async () => {
        const r = await dragScrollbar(page, ".dev-task-list", 1);
        assert(r.ok, r.reason ?? "拖动失败");
        assert(
          r.after >= r.m.maxScroll - 4,
          `没到底：scrollTop=${r.after} maxScroll=${r.m.maxScroll}`,
        );
        return `scrollTop=${Math.round(r.after)} / max=${r.m.maxScroll}`;
      });
      await check("sb-05-反向拖回", "从底部把滑块往上拖 → 列表回到顶部（不是只进不退）", async () => {
        // 先把状态摆到底部（这一步只是布置；被验证的是下面这次拖动）
        await page.evaluate(() => {
          const el = document.querySelector(".dev-task-list");
          el.scrollTop = el.scrollHeight;
        });
        await sleep(180);
        const r = await dragScrollbar(page, ".dev-task-list", 0);
        assert(r.ok, r.reason ?? "拖动失败");
        assert(r.after < r.before - 20, `反向拖动没生效：${r.before} → ${r.after}`);
        return `scrollTop ${Math.round(r.before)} → ${Math.round(r.after)}（目标点 ${Math.round(r.targetY)}）`;
      });
      await check("sb-06-点轨道翻页", "点轨道空白处会翻一页（滚动条确实可交互）", async () => {
        await page.evaluate(() => {
          document.querySelector(".dev-task-list").scrollTop = 0;
        });
        await sleep(150);
        const m = await measureScrollbar(page, ".dev-task-list");
        await page.mouse.click(m.x, m.rect.y + m.rect.height - 6);
        await sleep(260);
        const after = await page.evaluate(() => document.querySelector(".dev-task-list").scrollTop);
        assert(after > 20, `点轨道后 scrollTop=${after}`);
        return `翻页到 ${Math.round(after)}`;
      });
      await check("sb-07-拖动不带动背景", "拖动列表滚动条时背景对话的 scrollTop 不变", async () => {
        const before = await page.evaluate(() => document.querySelector(".stream").scrollTop);
        await dragScrollbar(page, ".dev-task-list", 0.4);
        const after = await page.evaluate(() => document.querySelector(".stream").scrollTop);
        assert(before === after, `背景被带动：${before} → ${after}`);
        return `背景 scrollTop 保持 ${before}`;
      });
    }

    // 对照组：对「背景对话自己的滚动条」做同样的拖动。
    // 注意：这一格的语义**只是记录**——列表那侧已经由 sb-03/04/05 证明可拖，
    // 所以对照组动不动都不改变列表的结论；不要用它去反推「合成事件拖不动滚动条」。
    await check(
      "sb-08-对照组（对话自己的滚动条）",
      "对背景对话的滚动条做同样的拖动（仅作对照记录，不作为列表结论的依据）",
      async () => {
        const m = await measureScrollbar(page, ".stream");
        assert(m && m.gutter > 0, `背景对话也没有经典滚动条（gutter=${m ? m.gutter : "n/a"}），无法做对照`);
        await page.evaluate(() => {
          document.querySelector(".stream").scrollTop = 0;
        });
        await sleep(150);
        const r = await dragScrollbar(page, ".stream", 0.6);
        assert(r.ok, r.reason ?? "拖动失败");
        return r.after > r.before + 20
          ? `对照组也拖动了：scrollTop ${r.before} → ${Math.round(r.after)}`
          : `对照组没有移动：${r.before} → ${r.after}（原因未查明；不影响列表侧 sb-03/04/05 的结论）`;
      },
    );

    await s.shot("headed-30-scrollbar-drag", "有头窗口：拖动列表滚动条之后", { fullPage: false });
    await s.shotEl(".dev-task-panel", "headed-31-panel-after-drag", "有头窗口：拖动后的面板", { pad: 6 });
  } finally {
    await s.close();
  }
}

// ---------------------------------------------------------------------------
// 审批卡：放弃之后「确认卡必须自己消失」（真实浏览器 + 真实 SSE + 真实后端审批）
//
// 需要场景服务器在跑（它能在真实进程里发起一条**真的在等**的审批）：
//   .verify-tmp/run-approval-card-check.mjs 会一条命令把「播种 + 场景后端 + vite + 本阶段」串起来。
// 本阶段自己不做任何桩：卡片来自真实 POST /scenario/approval/{id}，作废来自真实 POST /abandon。
// ---------------------------------------------------------------------------

const CARD_APPROVAL_API = (api) => `${api}/scenario`;

async function scenarioAvailable(api) {
  try {
    const resp = await fetch(`${CARD_APPROVAL_API(api)}/submit-calls`);
    return resp.ok;
  } catch {
    return false;
  }
}

/** 请场景服务器为某个任务发起一条真实等待中的审批 */
async function createRealApproval(api, taskId) {
  const resp = await fetch(`${CARD_APPROVAL_API(api)}/approval/${encodeURIComponent(taskId)}`, {
    method: "POST",
  });
  if (!resp.ok) throw new Error(`POST /scenario/approval 失败：${resp.status} ${await resp.text()}`);
  return resp.json();
}

function unfinishedRows(api) {
  return fetch(`${api}/api/dev/tasks`)
    .then((r) => r.json())
    .then((body) => body.tasks.filter((row) => !row.submitted && !row.abandoned));
}

async function abandonRowFor(page, api, taskId) {
  const rows = await unfinishedRows(api);
  const row = rows.find((item) => item.id === taskId);
  assert(row, `后端列表里没有未完成任务 ${taskId}`);
  const prefix = String(row.request || "").slice(0, 24);
  const index = await page.evaluate((needle) => {
    const all = [...document.querySelectorAll(".dev-task-row")];
    return all.findIndex((el) =>
      (el.querySelector(".dev-task-request")?.textContent ?? "").includes(needle),
    );
  }, prefix);
  assert(index >= 0, `面板里找不到任务行（需求前缀「${prefix}」）`);
  await page.locator(".dev-task-row").nth(index).locator(".dev-task-abandon").click();
  await page.waitForSelector(".qio-confirm", { timeout: 8000 });
  await page.click(".qio-confirm button.danger-solid");
  await page.waitForFunction(
    (n) => document.querySelectorAll(".dev-task-panel .dev-task-row").length === n - 1,
    await page.evaluate(() => document.querySelectorAll(".dev-task-panel .dev-task-row").length),
    { timeout: 15000 },
  );
  await sleep(250);
}

async function runApprovalCardChecks(browser, base, api) {
  const s = await createSession(browser, {
    group: GROUP,
    name: "审批卡消失",
    theme: "dark",
    viewport: DEFAULT_VIEWPORT,
    base,
    api,
  });
  const page = s.page;
  try {
    if (!(await scenarioAvailable(api))) {
      skip("ac-00-场景服务器", "真实审批卡消失的实测", `场景服务器不可用（${api}/scenario/*）：请用 .verify-tmp/run-approval-card-check.mjs 起实例`);
      return;
    }
    const unfinished = await unfinishedRows(api);
    assert(unfinished.length >= 3, `未完成任务不足（${unfinished.length}）`);
    const first = unfinished[0].id;
    const control = unfinished[1].id;

    // 先打开页面、等它连上（开发任务入口出现＝已经同步过一次权威状态），
    // **然后**再发起审批 —— 这样卡片来自实时的 APPROVAL_REQUIRED 事件，会以弹窗出现；
    // 反过来（先发审批再开页面）会走 RESYNC 恢复路径，那是「不抢焦点」的入口条形态。
    await s.goto("#/", { waitFor: ".conversation", settle: 900 });
    await page.waitForSelector(".dev-task-entry", { timeout: 30000 });
    const created = await createRealApproval(api, first);
    assert(created.approval_id, "场景服务器没有返回 approval_id");

    // 1) 真实审批 → 卡片出现
    await check("ac-01-审批卡出现", "为任务发起真实审批后，界面上真的出现确认卡（modal）", async () => {
      await page.waitForSelector('.modal-mask [role="dialog"][aria-modal="true"]', { timeout: 20000 });
      const text = await page.textContent('.modal-mask [role="dialog"]');
      assert(text && text.trim().length > 0, "确认卡是空的");
      return `卡片文案前 60 字：「${text.replace(/\s+/g, " ").trim().slice(0, 60)}」`;
    });
    await s.shot("approval-card-01-modal", "真实审批卡出现（modal）");

    // 2) 稍后处理 → 收成入口条（待审批仍然在）
    await check("ac-02-稍后处理收成入口条", "点「稍后处理」→ 弹窗收起，但待审批入口条还在（审批没被丢弃）", async () => {
      await page.click(".modal-mask button.later");
      await page.waitForSelector(".approval-entry", { timeout: 8000 });
      const entryText = await page.textContent(".approval-entry");
      return `入口条：「${entryText.replace(/\s+/g, " ").trim()}」`;
    });
    await s.shot("approval-card-02-deferred", "稍后处理后的待审批入口条");

    // 3) 从界面放弃该任务 → 入口条必须自己消失（SSE 的 APPROVAL_RESULT(cancelled)）
    await check("ac-03-放弃后卡片自己消失", "在面板里放弃该任务 → 待审批入口条消失（未决审批被作废，界面自己收敛）", async () => {
      await openEntryPanel(page);
      await abandonRowFor(page, api, first);
      await page.waitForFunction(() => !document.querySelector(".approval-entry"), null, {
        timeout: 20000,
      });
      const stillThere = await page.evaluate(() => !!document.querySelector(".approval-entry"));
      assert(stillThere === false, "入口条还在");
      return "入口条已消失（由 APPROVAL_RESULT{cancelled} 驱动，不是刷新页面）";
    });
    await s.shot("approval-card-03-gone", "放弃之后审批卡消失");

    await check("ac-04-后端与库一致", "运行时状态里没有这条审批；库里它的结局是 cancelled", async () => {
      const state = await fetch(`${api}/api/runtime/state`).then((r) => r.json());
      const pending = (state.approvals ?? []).filter((row) => row.approval_id === created.approval_id);
      assert(pending.length === 0, "运行时状态里还挂着这条审批");
      const db = await fetch(`${api}/scenario/db/approval/${created.approval_id}`).then((r) => r.json());
      assert(db.found === true && db.status === "cancelled", `库里状态=${db.status}`);
      return `runtime=pending 0 条；db.status=${db.status}`;
    });

    // 4) 对照组：再发一条审批 → 卡片必须能再出现（证明「消失」不是因为界面不再渲染审批）
    const second = await createRealApproval(api, control);
    await check("ac-05-对照组（新审批仍能出卡）", "再为另一个任务发起审批 → 卡片再次出现（排除「界面根本不渲染审批」的假象）", async () => {
      await page.waitForSelector('.modal-mask [role="dialog"][aria-modal="true"]', { timeout: 20000 });
      return `新审批 ${second.approval_id} 的卡片已出现`;
    });
    await s.shot("approval-card-04-control", "对照：新审批的卡片");

    // 5) 卡片开着的时候从「别处」放弃（直接打后端）→ 卡片必须自己消失
    await check("ac-06-并发路径：卡片开着时放弃", "卡片开着时从别处放弃该任务 → 卡片自己消失（不需要刷新）", async () => {
      const resp = await fetch(`${api}/api/dev/tasks/${control}/abandon`, { method: "POST" });
      assert(resp.ok, `abandon 失败：${resp.status}`);
      await page.waitForFunction(() => !document.querySelector(".modal-mask"), null, { timeout: 20000 });
      return "modal 已从 DOM 移除";
    });

    await check("ac-07-卡片不会回来", "等 3 秒：已作废的审批不会重新出现", async () => {
      await sleep(3000);
      const back = await page.evaluate(
        () => !!document.querySelector(".modal-mask") || !!document.querySelector(".approval-entry"),
      );
      assert(back === false, "卡片又回来了");
      return "3 秒内没有再出现";
    });
    await s.shot("approval-card-05-after", "作废之后（对话页无审批卡）");
  } finally {
    await s.close();
  }
}

/** 请场景服务器为某个任务发起一条真实等待中的**凭据授权**审批（契约 v2 B1） */
async function createCredentialApproval(api, taskId) {
  const resp = await fetch(
    `${CARD_APPROVAL_API(api)}/credential-approval/${encodeURIComponent(taskId)}`,
    { method: "POST" },
  );
  if (!resp.ok) throw new Error(`POST /scenario/credential-approval 失败：${resp.status} ${await resp.text()}`);
  return resp.json();
}

/**
 * 凭据授权确认卡：放弃之后必须**立即消失**（契约 v2 B1/B2/B4、D3）。
 *
 * 与 approval-card 阶段的分工：那一轮验的是「测试执行审批」（tool_execution）；
 * 这一轮验的是**凭据授权审批**（credential_grant）—— 它以前载荷里没有 workspace，
 * 放弃时找不到它，卡片会一直挂着等人点「允许」。
 */
async function runCredentialCardChecks(browser, base, api) {
  const s = await createSession(browser, {
    group: GROUP,
    name: "凭据授权卡消失",
    theme: "dark",
    viewport: DEFAULT_VIEWPORT,
    base,
    api,
  });
  const page = s.page;
  try {
    if (!(await scenarioAvailable(api))) {
      skip("cc-00-场景服务器", "凭据确认卡消失的实测", `场景服务器不可用（${api}/scenario/*）`);
      return;
    }
    const unfinished = await unfinishedRows(api);
    assert(unfinished.length >= 3, `未完成任务不足（${unfinished.length}）`);
    const target = unfinished[0].id;
    const control = unfinished[1].id;

    // 先开页面并等它连上，再发起审批（这样卡片来自实时事件，是弹窗形态）
    await s.goto("#/", { waitFor: ".conversation", settle: 900 });
    await page.waitForSelector(".dev-task-entry", { timeout: 30000 });
    const created = await createCredentialApproval(api, target);
    assert(created.approval_id, "场景服务器没有返回 approval_id");

    await check("cc-01-凭据确认卡出现", "需要凭据的开发任务发起审批后，界面上出现确认卡", async () => {
      await page.waitForSelector('.modal-mask [role="dialog"][aria-modal="true"]', { timeout: 20000 });
      const text = await page.textContent('.modal-mask [role="dialog"]');
      assert(text && text.trim().length > 0, "确认卡是空的");
      return `卡片文案前 50 字：「${text.replace(/\s+/g, " ").trim().slice(0, 50)}」`;
    });
    await s.shot("credential-card-01-modal", "凭据授权确认卡出现");

    await check("cc-02-稍后处理收成入口条", "「稍后处理」后弹窗收起、待审批入口条保留", async () => {
      await page.click(".modal-mask button.later");
      await page.waitForSelector(".approval-entry", { timeout: 8000 });
      return `入口条：「${(await page.textContent(".approval-entry")).replace(/\s+/g, " ").trim()}」`;
    });

    await check("cc-03-放弃后立即消失（不刷新）", "在面板里放弃该任务 → 入口条**立即**消失，页面没有刷新", async () => {
      await page.evaluate(() => {
        window.__verifyReload = false;
        window.addEventListener("beforeunload", () => {
          window.__verifyReload = true;
        });
      });
      await openEntryPanel(page);
      await abandonRowFor(page, api, target);
      await page.waitForFunction(() => !document.querySelector(".approval-entry"), null, { timeout: 5000 });
      const reloaded = await page.evaluate(() => window.__verifyReload === true);
      assert(reloaded === false, "页面被刷新了 —— 不能靠刷新才消失");
      return "入口条已消失，且没有发生页面刷新";
    });
    await s.shot("credential-card-02-gone", "放弃后凭据确认卡消失");

    await check("cc-04-后端与库一致", "运行时状态里没有它；库里是 cancelled；等待方 cancelled；旧批准请求无效", async () => {
      const state = await fetch(`${api}/api/runtime/state`).then((r) => r.json());
      const still = (state.approvals ?? []).filter((row) => row.approval_id === created.approval_id);
      assert(still.length === 0, "运行时状态里还挂着这条审批");
      const db = await fetch(`${api}/scenario/db/approval/${created.approval_id}`).then((r) => r.json());
      assert(db.found === true && db.status === "cancelled", `库里状态=${db.status}`);
      const waiter = await fetch(`${api}/scenario/credential-approval/${target}`).then((r) => r.json());
      assert(waiter.done === true && waiter.decision === "cancelled", `等待方结局=${JSON.stringify(waiter)}`);
      const lateResp = await fetch(`${api}/scenario/task/${target}/respond-late`, { method: "POST" });
      const lateBody = await lateResp.json().catch(() => ({}));
      // 2026-10-05 修（Lead 定性的验证台缺陷）：这条断言以前只看 `handled`，而 404 的响应体里
      // 根本没有 `handled`，`undefined === false` 被读成「仍然可以被批准」→ 误报成产品缺陷。
      // 现在先要求 HTTP 成功（否则是**验证台**没读到审批登记），再看 handled 与库里的结局。
      assert(
        lateResp.ok,
        `respond-late 返回 HTTP ${lateResp.status}：${JSON.stringify(lateBody)} —— ` +
          "验证台没读到这条审批的登记，不能据此判断「是否还能被批准」",
      );
      assert(lateBody.handled === false, `作废过的凭据审批仍然可以被批准：${JSON.stringify(lateBody)}`);
      assert(
        lateBody.product_http?.db_status === "cancelled",
        `库里结局不是 cancelled：${JSON.stringify(lateBody)}`,
      );
      return "db.status=cancelled；等待方=cancelled；再批准 handled=false（HTTP 200）";
    });

    await check("cc-05-对照组（别的任务的凭据卡不受影响）", "另一个任务的凭据审批仍然在等（没有被误伤）", async () => {
      const other = await createCredentialApproval(api, control);
      assert(other.approval_id, "对照组审批没有建立");
      await page.waitForSelector('.modal-mask [role="dialog"][aria-modal="true"]', { timeout: 20000 });
      const state = await fetch(`${api}/api/runtime/state`).then((r) => r.json());
      const pending = (state.approvals ?? []).filter((row) => row.approval_id === other.approval_id);
      assert(pending.length === 1, "对照组的审批没在等待表里");
      return `对照组 ${other.approval_id} 的卡片仍在等`;
    });
    await s.shot("credential-card-03-control", "对照：别的任务的凭据卡仍在等");
  } finally {
    await s.close();
  }
}

/** 把背景对话撑到能滚（与 wide/narrow 里那段同一目的，抽出来给滚动条阶段复用） */
async function ensureConversationScrollable(s, page) {  const before = await page.evaluate(() => {
    const el = document.querySelector(".stream");
    return el ? { sh: el.scrollHeight, ch: el.clientHeight } : null;
  });
  if (before && before.sh > before.ch + 40) return;
  const filler = "这是一段用来把背景对话撑到可滚动的正文，它需要在窄窗口和普通窗口下都足够长。";
  for (let i = 0; i < 8; i += 1) {
    const turnId = `dev_abandon_sb_${i}`;
    const content = `背景消息 ${i + 1}：${filler.repeat(6)}`;
    await s.inject("TURN_START", { turn_id: turnId, revision: 7200 + i });
    await s.inject("ASSISTANT", { content });
    await s.inject("TURN_END", {
      turn_id: turnId,
      status: "completed",
      revision: 7300 + i,
      final_content: content,
    });
    await sleep(150);
  }
  await sleep(600);
}

await runGroup(async () => {
  if (SEED_ONLY) {
    seed();
    return;
  }

  const base = process.env.QIO_BASE || "http://127.0.0.1:6206";
  const api = process.env.QIO_API || "http://127.0.0.1:8841";
  const meta = { group: GROUP, instance: INSTANCE, stage: STAGE, base, api, dataDir };

  mkdirSync(path.join(SHOT_ROOT, GROUP), { recursive: true });
  const health = await fetch(`${api}/api/health`).then((r) => r.status).catch(() => 0);
  if (health >= 500 || health === 0) {
    throw new Error(
      `后端 ${api} 不可用（/api/health=${health}）。先起隔离实例：\n` +
        `  node scripts/ui-catalog/dev-abandon.mjs --seed\n` +
        `  python scripts/ui-catalog/instance.py up --name ${INSTANCE} --backend-port 8841 --frontend-port 6206`,
    );
  }

  const listed = await fetch(`${api}/api/dev/tasks`).then((r) => r.json());
  const unfinished = listed.tasks.filter((row) => !row.submitted && !row.abandoned);
  if (!["restart", "approval-card", "credential-card"].includes(STAGE) && unfinished.length < 30) {
    throw new Error(
      `后端只看到 ${unfinished.length} 个未完成开发任务（需要 ≥30）。` +
        `播种必须在后端启动前完成：先 --seed，再 instance.py up（数据目录 ${dataDir}）。\n` +
        `正确顺序：instance.py up --clean → instance.py down → dev-abandon.mjs --seed → instance.py up`,
    );
  }

  const unfinishedCount = async () => {
    const rows = await fetch(`${api}/api/dev/tasks`).then((r) => r.json());
    return rows.tasks.filter((row) => !row.submitted && !row.abandoned).length;
  };

  if (STAGE === "restart") {
    const browser = await launchBrowser();
    try {
      await verifyAfterRestart(browser, base, api);
    } finally {
      await browser.close();
    }
    const failed = persistChecks(STAGE, meta);
    if (failed > 0) process.exitCode = 1;
    return;
  }

  // 拖动滚动条：无头窗口是 overlay 滚动条（没有可拖的轨道），所以这一阶段默认有头。
  if (STAGE === "scrollbar") {
    const headed = process.env.QIO_HEADED !== "0";
    console.log(`  滚动条阶段：${headed ? "有头窗口" : "无头窗口"}（QIO_HEADED=0 可切无头）`);
    const browser = await launchBrowser({ headless: !headed });
    try {
      await runScrollbarChecks(browser, base, api, await unfinishedCount());
    } finally {
      await browser.close();
    }
    const failed = persistChecks(STAGE, meta);
    if (failed > 0) process.exitCode = 1;
    return;
  }

  // 凭据授权确认卡：放弃后立即消失（需要场景服务器）
  if (STAGE === "credential-card") {
    const browser = await launchBrowser();
    try {
      await runCredentialCardChecks(browser, base, api);
    } finally {
      await browser.close();
    }
    const failed = persistChecks(STAGE, meta);
    if (failed > 0) process.exitCode = 1;
    return;
  }

  // 审批卡消失：需要场景服务器（真实等待中的审批）
  if (STAGE === "approval-card") {
    const browser = await launchBrowser();
    try {
      await runApprovalCardChecks(browser, base, api);
    } finally {
      await browser.close();
    }
    const failed = persistChecks(STAGE, meta);
    if (failed > 0) process.exitCode = 1;
    return;
  }

  const browser = await launchBrowser();
  try {
    if (STAGE === "scroll" || STAGE === "all") {
      const wide = await createSession(browser, {
        group: GROUP,
        name: "普通窗口",
        theme: "dark",
        viewport: DEFAULT_VIEWPORT,
        base,
        api,
      });
      try {
        await runViewportChecks(wide, "wide", await unfinishedCount());
      } finally {
        await wide.close();
      }
      const narrow = await createSession(browser, {
        group: GROUP,
        name: "窄窗口",
        theme: "dark",
        viewport: NARROW_VIEWPORT,
        base,
        api,
      });
      try {
        await runViewportChecks(narrow, "narrow", await unfinishedCount());
      } finally {
        await narrow.close();
      }
    }

    if (STAGE === "abandon" || STAGE === "all") {
      const s = await createSession(browser, {
        group: GROUP,
        name: "放弃流程",
        theme: "dark",
        viewport: DEFAULT_VIEWPORT,
        base,
        api,
      });
      try {
        await runAbandonFlow(s, api, base);
      } finally {
        await s.close();
      }
      const snapshot = await writeSnapshot(base, api);
      console.log(
        `快照：已放弃 ${snapshot.abandoned_ids.length} / 未完成 ${snapshot.unfinished_ids.length} → ${SNAPSHOT_FILE}`,
      );
    }

    if (STAGE === "last" || STAGE === "all") {
      const last = await createSession(browser, {
        group: GROUP,
        name: "最后一项",
        theme: "dark",
        viewport: DEFAULT_VIEWPORT,
        base,
        api,
      });
      try {
        await runLastItemFlow(last, api);
      } finally {
        await last.close();
      }
      await writeSnapshot(base, api);
    }
  } finally {
    await browser.close();
  }

  const failed = persistChecks(STAGE, meta);
  if (failed > 0) process.exitCode = 1;
});
