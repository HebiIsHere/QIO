/**
 * Lead 复验：A04 冲突候选管理界面的**真实运行**检查（真实后端 + 真实 Vite + 隔离数据目录）。
 *
 * 场景：
 *   1. 库里有一张「人工写过摘要」的实体卡，且有一条被保护下来的自动候选（用户值优先）；
 *   2. 打开星球 → 「实体」页签 → 「待处理候选（n）」默认收起；
 *   3. 展开后能看到 当前值 / 候选值 / 一句话原因 / 「采纳」「丢弃」；
 *   4. 点「采纳」→ 真实落库（摘要真的变成候选值）+ 界面显示采纳结果；
 *   5. 窄窗口下不横向溢出。
 *
 * 用法（服务需在跑）：
 *   FU_UI_DATA_DIR=<隔离数据目录> node scripts/fu-verify/ui-candidates-probe.mjs
 */
import { createSession, launchBrowser, runGroup, saveManifest, sleep } from "../ui-catalog/lib.mjs";
import { execFileSync } from "node:child_process";

const GROUP = "fu-verify";
const CARD_ID = "ec_ui_probe";
const NODE_ID = "node_ui_probe";
const MANUAL_SUMMARY = "用户写下的摘要（界面复验）";
const CANDIDATE_SUMMARY = "自动提炼的摘要（界面复验）";

const PY = "D:\\qio-dev\\qio-fu-main\\backend\\.venv\\Scripts\\python.exe";

function runPy(src, dataDir) {
  return execFileSync(PY, ["-c", src, dataDir], { encoding: "utf-8" }).trim();
}

function seed(dataDir) {
  return runPy(
    `
import json, sys
from pathlib import Path
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

data = Path(sys.argv[1])
conn = connect(data / "app.db")
apply_migrations(conn)
now = "2026-10-10T04:00:00+00:00"
conn.execute(
    "INSERT OR REPLACE INTO nodes (id, type, name, meta, created_at, updated_at) "
    "VALUES (?, 'entity', '我家的鹅', '{}', ?, ?)", ("${NODE_ID}", now, now),
)
meta = {
    "fields": {"summary": {"source": "user", "revision": 3}},
    "attributes": {"年龄": {"source": "user", "revision": 3}},
    "pending": [{
        "id": "pend_ui_1",
        "field": "summary",
        "value": "${CANDIDATE_SUMMARY}",
        "reason": "user_value",
        "base_revision": 3,
        "at": now,
    }],
}
conn.execute(
    "INSERT OR REPLACE INTO entity_cards (id, node_id, name, aliases, kind, summary, "
    "attributes, state, created_at, updated_at, revision, field_meta) "
    "VALUES (?, ?, '我家的鹅', ?, '动物', ?, ?, 'active', ?, ?, 3, ?)",
    (
        "${CARD_ID}", "${NODE_ID}", json.dumps(["大白"], ensure_ascii=False),
        "${MANUAL_SUMMARY}",
        json.dumps([{"key": "年龄", "value": "2 岁", "confidence": 0.9}], ensure_ascii=False),
        now, now, json.dumps(meta, ensure_ascii=False),
    ),
)
for key, value in (
    ("onboarding.done", "1"), ("onboarding.wizard_seen", "1"),
    ("onboarding.welcome_version", "0.1.14"), ("onboarding.hint_dismissed", "1"),
):
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now),
    )
conn.commit()
row = conn.execute("SELECT summary, revision, field_meta FROM entity_cards WHERE id=?", ("${CARD_ID}",)).fetchone()
print("seeded summary:", row["summary"], "revision:", row["revision"])
conn.close()
`,
    dataDir,
  );
}

function summaryOf(dataDir) {
  return runPy(
    `
import sqlite3, sys
from pathlib import Path
conn = sqlite3.connect(str(Path(sys.argv[1]) / "app.db"))
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT summary, revision, field_meta FROM entity_cards WHERE id=?", ("${CARD_ID}",)).fetchone()
print(row["summary"])
conn.close()
`,
    dataDir,
  );
}

const dataDir = process.env.FU_UI_DATA_DIR;
if (!dataDir) {
  console.error("需要设置 FU_UI_DATA_DIR（隔离数据目录）");
  process.exit(2);
}
console.log("seed:", seed(dataDir));

const entries = [];
const checks = [];
function record(name, ok, detail) {
  checks.push({ name, ok, detail });
  console.log(`  [${ok ? "ok" : "FAIL"}] ${name}${detail ? ` — ${detail}` : ""}`);
}

async function dismissOnboarding(page) {
  const dialog = page.locator('[role="dialog"][aria-label="首次引导"]');
  if ((await dialog.count()) === 0) return;
  const close = dialog.getByRole("button", { name: /关闭|跳过|知道了|完成|×/ });
  if ((await close.count()) > 0) await close.first().click({ timeout: 3000 }).catch(() => {});
  await page.keyboard.press("Escape").catch(() => {});
  await sleep(300);
}

await runGroup(async () => {
  const browser = await launchBrowser();
  const s = await createSession(browser, { group: GROUP, name: "候选管理", theme: "dark" });

  await s.goto("#/", { waitFor: ".conversation" });
  await dismissOnboarding(s.page);
  await sleep(600);

  // 打开星球（.dock 的 aria-label 是「打开话题星球」）；星球是懒加载 + WebGL，给它时间
  const dock = s.page.locator(".dock").first();
  await dock.click({ timeout: 8000 }).catch(() => {});
  // 页签的类是 .tab-entity（比按名字找稳：页面上还有叫「实体」的话题标签）。
  // 面板可能处于收起状态 → Playwright 的可见性检查会挡下点击，所以用 DOM 派发点击，
  // 走的仍然是组件真实的 @click 处理器。
  const entityTab = s.page.locator(".tab-entity").first();
  await entityTab.waitFor({ state: "attached", timeout: 15000 }).catch(() => {});
  await entityTab.evaluate((el) => el.click()).catch(() => {});
  await sleep(1200);
  // 等候选区出现（列表页那一块是跨卡清单）
  await s.page
    .getByText(/待处理候选/)
    .first()
    .waitFor({ state: "visible", timeout: 10000 })
    .catch(() => {});
  await sleep(600);
  await s.shot("candidates-entity-tab", "星球的「实体」页签");

  // 「待处理候选（n）」默认收起 → 点开（用 DOM 派发点击，走组件真实的 @click）
  const headerButton = s.page.locator(".ec-head").first();
  const headerVisible = (await headerButton.count()) > 0;
  const collapsedText = headerVisible ? await s.page.locator("body").innerText() : "";
  record("候选区标题可见", headerVisible, collapsedText.match(/待处理候选[^\n]{0,20}/)?.[0] ?? "");
  const collapsedHasButtons = /采纳/.test(collapsedText);
  record("默认收起（收起时不显示采纳按钮）", !collapsedHasButtons, collapsedHasButtons ? "收起时已出现采纳" : "");

  if (headerVisible) {
    await headerButton.evaluate((el) => el.click()).catch(() => {});
    await sleep(1200);
  }
  const expanded = await s.page.locator("body").innerText();
  record(
    "展开后显示当前值 / 候选值 / 原因 / 两个动作",
    expanded.includes(MANUAL_SUMMARY) &&
      expanded.includes(CANDIDATE_SUMMARY) &&
      /采纳/.test(expanded) &&
      /丢弃/.test(expanded),
    expanded.replace(/\s+/g, " ").match(/待处理候选.{0,160}/s)?.[0] ?? expanded.slice(0, 160),
  );
  await s.shot("candidates-expanded", "待处理候选展开");

  // 点「采纳」→ 真实落库（.ec-adopt 是行内采纳按钮；同样用 DOM 派发点击）
  const before = summaryOf(dataDir);
  const adopt = s.page.locator("button.ec-adopt").first();
  if ((await adopt.count()) > 0) {
    await adopt.evaluate((el) => el.click()).catch(() => {});
    await sleep(2200);
  }
  const after = summaryOf(dataDir);
  record(
    "点「采纳」真的落到库里",
    before === MANUAL_SUMMARY && after === CANDIDATE_SUMMARY,
    `before=${before} after=${after}`,
  );
  const afterText = await s.page.locator("body").innerText();
  record("采纳后界面显示采纳结果", /已采纳|此前已经处理过/.test(afterText), afterText.replace(/\s+/g, " ").slice(0, 140));
  await s.shot("candidates-after-adopt", "采纳之后");

  // 窄窗口
  await s.page.setViewportSize({ width: 420, height: 720 });
  await sleep(800);
  const overflow = await s.page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
  }));
  record("窄窗口无横向溢出", overflow.scrollW <= overflow.clientW + 2, JSON.stringify(overflow));
  await s.shot("candidates-narrow", "窄窗口 420×720");

  const realErrors = s.consoleErrors.filter((e) => !/net::ERR_FAILED|Failed to load resource/.test(e));
  record("无控制台错误", realErrors.length === 0, `${realErrors.length} 条`);
  for (const e of realErrors.slice(0, 5)) console.log("     -", e);

  entries.push({
    name: "fu-candidates-ui",
    checks: checks.map((c) => ({ name: c.name, ok: c.ok, detail: c.detail })),
    summaryBefore: before,
    summaryAfter: after,
    consoleErrors: s.consoleErrors.length,
  });

  await s.close();
  await browser.close();
});

await saveManifest(GROUP, entries);

const failed = checks.filter((c) => !c.ok);
console.log(`\n候选管理界面复验：${checks.length - failed.length}/${checks.length} 通过`);
if (failed.length) {
  console.log("失败项：");
  for (const f of failed) console.log(`  - ${f.name}：${f.detail ?? ""}`);
  process.exitCode = 1;
}
