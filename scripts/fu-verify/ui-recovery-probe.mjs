/**
 * Lead 复验：A01/A03 恢复收件箱的真实浏览器检查。
 *
 * 场景（全部在真实后端 + 真实 Vite 上跑，数据用隔离数据目录）：
 *   1. 升级前留下的「无归属 queued 用户消息」在对话页的未完成事项槽里可见（原文 + 原因 + 动作）；
 *   2. 点「继续」→ 后端真的受理（台账出现后继、原记录离开收件箱）；
 *   3. 刷新后状态一致（不会因为旧快照复活已处理记录）；
 *   4. 读取失败时就地显示失败 + 重试（把 /api/recovery/records 请求打掉再恢复）；
 *   5. 窄窗口（420×720）下不溢出、按钮可点。
 *
 * 用法（服务需在跑）：
 *   node scripts/fu-verify/ui-recovery-probe.mjs
 * 产物：frontend/e2e-shots/fu-verify/**（截图 + manifest）
 */
import { createSession, launchBrowser, runGroup, saveManifest, sleep } from "../ui-catalog/lib.mjs";
import { execFileSync } from "node:child_process";

const GROUP = "fu-verify";
const entries = [];
const checks = [];

function record(name, ok, detail) {
  checks.push({ name, ok, detail });
  console.log(`  [${ok ? "ok" : "FAIL"}] ${name}${detail ? ` — ${detail}` : ""}`);
}

/** 用后端 venv 的 python 往隔离库里塞一条「升级前留下的无归属消息」。 */
function seedLegacyTurn(dataDir) {
  const script = `
import os, sqlite3, sys
from pathlib import Path
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

data = Path(sys.argv[1])
conn = connect(data / "app.db")
apply_migrations(conn)
now = "2026-10-10T00:00:00+00:00"
# 恢复「升级前」的形状：清掉上一次跑留下的归属镜像与后继行，否则这一条会被判成
# 「有归属但现在的主人是谁不知道」，不再是 legacy_unowned（探针可重复运行）。
conn.execute("DELETE FROM record_owners WHERE record_type = 'turn' AND record_id = ?", ("turn_ui_legacy",))
conn.execute("DELETE FROM turn_journal WHERE recovered_by = ?", ("turn_ui_legacy",))
conn.execute(
    "INSERT OR REPLACE INTO turn_journal (turn_id, message, topic_id, notify, status, "
    "created_at, updated_at, owner_instance_id) VALUES ('turn_ui_legacy', "
    "'升级前保存、还没执行的消息（Lead 界面复验）', NULL, 0, 'queued', ?, ?, NULL)",
    (now, now),
)
# 跳过首次引导：否则引导弹窗会盖住页面、拦截点击（本探针要验的是恢复收件箱）
for key, value in (
    ("onboarding.done", "1"),
    ("onboarding.wizard_seen", "1"),
    ("onboarding.welcome_version", "0.1.14"),
    ("onboarding.hint_dismissed", "1"),
):
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now),
    )
conn.commit()
row = conn.execute(
    "SELECT turn_id, status, owner_instance_id FROM turn_journal WHERE turn_id='turn_ui_legacy'"
).fetchone()
print("seeded:", dict(row))
conn.close()
`;
  const py = "D:\\qio-dev\\qio-fu-main\\backend\\.venv\\Scripts\\python.exe";
  return execFileSync(py, ["-c", script, dataDir], { encoding: "utf-8" }).trim();
}

/** 读台账：确认「继续」真的写出了后继。 */
function journalRows(dataDir) {
  const script = `
import sqlite3, sys
from pathlib import Path
conn = sqlite3.connect(str(Path(sys.argv[1]) / "app.db"))
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute(
    "SELECT turn_id, status, reason, recovered_by FROM turn_journal ORDER BY created_at, rowid"
)]
conn.close()
print(rows)
`;
  const py = "D:\\qio-dev\\qio-fu-main\\backend\\.venv\\Scripts\\python.exe";
  return execFileSync(py, ["-c", script, dataDir], { encoding: "utf-8" }).trim();
}

const dataDir = process.env.FU_UI_DATA_DIR;
if (!dataDir) {
  console.error("需要设置 FU_UI_DATA_DIR（隔离数据目录）");
  process.exit(2);
}

console.log("seed:", seedLegacyTurn(dataDir));

/** 首次引导弹窗会盖住页面并拦截点击：探针只验恢复收件箱，所以先把它关掉。 */
async function dismissOnboarding(page) {
  const dialog = page.locator('[role="dialog"][aria-label="首次引导"]');
  if ((await dialog.count()) === 0) return;
  const close = dialog.getByRole("button", { name: /关闭|跳过|知道了|完成|×/ });
  if ((await close.count()) > 0) {
    await close.first().click({ timeout: 3000 }).catch(() => {});
  }
  await page.keyboard.press("Escape").catch(() => {});
  await sleep(300);
}

await runGroup(async () => {
  const browser = await launchBrowser();
  const s = await createSession(browser, { group: GROUP, name: "恢复收件箱", theme: "dark" });

  // 1) 首次连接：历史无归属消息必须可见
  await s.goto("#/", { waitFor: ".conversation" });
  await dismissOnboarding(s.page);
  await sleep(900);
  const inbox = s.page.locator(".interrupted-slot");
  await inbox.waitFor({ state: "visible", timeout: 10000 }).catch(() => {});
  const text = (await inbox.innerText().catch(() => "")) || "";
  record(
    "历史无归属消息在收件箱可见",
    text.includes("升级前保存") && text.includes("继续"),
    text.replace(/\s+/g, " ").slice(0, 160),
  );
  await s.shot("recovery-inbox-visible", "恢复收件箱：历史无归属消息可见");

  // 2) 点「继续」→ 后端真的受理
  const before = journalRows(dataDir);
  const cont = s.page.getByRole("button", { name: /继续/ }).first();
  if (await cont.count()) {
    await dismissOnboarding(s.page);
    await cont.click({ timeout: 8000 }).catch(() => {});
    await sleep(1500);
  }
  const after = journalRows(dataDir);
  record("点「继续」后台账出现后继关联", after !== before && /recovered_by': 'turn_/.test(after), after.slice(0, 220));
  await s.shot("recovery-inbox-after-continue", "点了「继续」之后");

  // 3) 刷新后一致（旧快照不得复活已处理记录）
  await s.page.reload({ waitUntil: "domcontentloaded" });
  await s.goto("#/", { waitFor: ".conversation" });
  await dismissOnboarding(s.page);
  await sleep(900);
  const textAfter = (await s.page.locator(".interrupted-slot").innerText().catch(() => "")) || "";
  record(
    "刷新后不复活已处理记录",
    !textAfter.includes("升级前保存"),
    textAfter.replace(/\s+/g, " ").slice(0, 160),
  );
  await s.shot("recovery-inbox-after-refresh", "刷新之后（已处理的记录不再出现）");

  // 4) 读取失败 → 就地失败 + 重试
  await s.page.route("**/api/recovery/records*", (route) => route.abort());
  await s.page.reload({ waitUntil: "domcontentloaded" });
  await s.goto("#/", { waitFor: ".conversation" });
  await dismissOnboarding(s.page);
  await sleep(900);
  const failText = (await s.page.locator(".interrupted-slot").innerText().catch(() => "")) || "";
  record(
    "读取失败时就地显示失败与重试",
    /读取|失败|重试/.test(failText),
    failText.replace(/\s+/g, " ").slice(0, 160),
  );
  await s.shot("recovery-inbox-load-failed", "清单读取失败：就地提示 + 重试");
  await s.page.unroute("**/api/recovery/records*");

  // 5) 窄窗口
  await s.page.setViewportSize({ width: 420, height: 720 });
  await s.page.reload({ waitUntil: "domcontentloaded" });
  await s.goto("#/", { waitFor: ".conversation" });
  await dismissOnboarding(s.page);
  await sleep(900);
  const overflow = await s.page.evaluate(() => {
    const el = document.querySelector(".interrupted-slot");
    if (!el) return null;
    return { scrollW: el.scrollWidth, clientW: el.clientWidth };
  });
  record(
    "窄窗口下收件箱不横向溢出",
    overflow === null || overflow.scrollW <= overflow.clientW + 2,
    JSON.stringify(overflow),
  );
  await s.shot("recovery-inbox-narrow", "窄窗口 420×720");

  // 第 4 步是**故意**打掉清单请求来验失败反馈，所以那两条 net::ERR_FAILED 不算产品错误；
  // 其余控制台错误一律算数。
  const realErrors = s.consoleErrors.filter((e) => !/net::ERR_FAILED|Failed to load resource/.test(e));
  record("无控制台错误（故意 abort 的噪声除外）", realErrors.length === 0, `${realErrors.length} 条`);
  for (const e of realErrors.slice(0, 6)) console.log("     -", e);

  entries.push({
    name: "fu-recovery-ui",
    checks: checks.map((c) => ({ name: c.name, ok: c.ok, detail: c.detail })),
    journalAfterContinue: after,
    consoleErrors: s.consoleErrors.length,
  });

  await s.close();
  await browser.close();
});

await saveManifest(GROUP, entries);

const failed = checks.filter((c) => !c.ok);
console.log(`\n恢复收件箱界面复验：${checks.length - failed.length}/${checks.length} 通过`);
if (failed.length) {
  console.log("失败项：");
  for (const f of failed) console.log(`  - ${f.name}：${f.detail ?? ""}`);
  process.exitCode = 1;
}
