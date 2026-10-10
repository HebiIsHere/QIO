/**
 * Lead 复验：M12「所有连接入口统一恢复完整运行状态」的真实浏览器检查。
 *
 * 只看可观察事实：
 *   1) 首次连接会不会去拉权威快照 `/api/runtime/state`（新页面没有历史游标）；
 *   2) 页面刷新（普通重连的等价形态）会不会**再拉一次**快照（而不是靠重放旧事件）；
 *   3) 两次加载都不产生控制台错误、不缺恢复必需的响应；
 *   4) 刷新后界面仍然渲染出对话区（不是白屏 / 不是卡在恢复中）。
 *
 * 用法（服务需在跑，与 smoke.mjs 相同）：
 *   node scripts/rm-verify/m12-refresh-probe.mjs
 */
import { createSession, launchBrowser, runGroup, saveManifest, sleep } from "../ui-catalog/lib.mjs";

const GROUP = "rm-lead-m12";
const entries = [];
const checks = [];

function record(name, ok, detail) {
  checks.push({ name, ok, detail });
  console.log(`  [${ok ? "ok" : "FAIL"}] ${name}${detail ? ` — ${detail}` : ""}`);
}

await runGroup(async () => {
  const browser = await launchBrowser();
  const s = await createSession(browser, { group: GROUP, name: "刷新恢复", theme: "dark" });

  const snapshots = [];
  const snapshotResponses = [];
  s.page.on("request", (req) => {
    const url = req.url();
    if (url.includes("/api/runtime/state")) snapshots.push(url);
  });
  s.page.on("response", (resp) => {
    const url = resp.url();
    if (url.includes("/api/runtime/state")) {
      snapshotResponses.push({ url, status: resp.status() });
    }
  });

  // 1) 首次连接（等价「新页面打开」，没有历史事件游标）
  await s.goto("#/", { waitFor: ".conversation" });
  await sleep(700);
  record(
    "首次连接拉取权威快照",
    snapshots.length >= 1,
    `runtime/state 请求 ${snapshots.length} 次`,
  );
  await s.shot("m12-first-connect", "首次连接的对话页");

  const firstCount = snapshots.length;
  const firstOk = snapshotResponses.filter((r) => r.status === 200).length;

  // 2) 页面刷新 = 普通重连的等价形态：必须重新走统一恢复入口
  await s.page.reload({ waitUntil: "domcontentloaded" });
  await s.goto("#/", { waitFor: ".conversation" });
  await sleep(700);
  record(
    "刷新后重新拉取权威快照",
    snapshots.length > firstCount,
    `累计 runtime/state 请求 ${snapshots.length} 次（刷新前 ${firstCount}）`,
  );
  await s.shot("m12-after-refresh", "刷新后的对话页");

  // 3) 快照响应必须成功（不能靠「恢复了但请求失败」假装恢复）
  const allOk = snapshotResponses.length > 0 && snapshotResponses.every((r) => r.status === 200);
  record(
    "快照响应全部 2xx",
    allOk,
    `首次 200 数 ${firstOk}；全部 ${snapshotResponses.map((r) => r.status).join(",")}`,
  );

  // 4) 刷新后界面仍然可用（不是白屏、不是卡在恢复中）
  const conversationVisible = await s.page
    .locator(".conversation")
    .first()
    .isVisible()
    .catch(() => false);
  record("刷新后对话区可见", Boolean(conversationVisible));

  // 5) 没有控制台错误
  record("无控制台错误", s.consoleErrors.length === 0, `${s.consoleErrors.length} 条`);
  for (const e of s.consoleErrors.slice(0, 5)) console.log("     -", e);

  entries.push({
    name: "m12-refresh",
    checks: checks.map((c) => ({ name: c.name, ok: c.ok, detail: c.detail })),
    runtimeStateRequests: snapshots.length,
    consoleErrors: s.consoleErrors.length,
  });

  await s.close();
  await browser.close();
});

await saveManifest(GROUP, entries);

const failed = checks.filter((c) => !c.ok);
console.log(`\nM12 刷新恢复：${checks.length - failed.length}/${checks.length} 通过`);
if (failed.length) {
  console.log("失败项：");
  for (const f of failed) console.log(`  - ${f.name}：${f.detail ?? ""}`);
  process.exitCode = 1;
}
