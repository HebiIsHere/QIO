/**
 * 通知条复核的收尾（断言结果见 README/AUDIT 里的记录）：
 * - conv-61-warning：等历史真正加载完成之后再注入（加载途中到达的警告会被清掉，
 *   这是实测到的行为，作为一条 note 记下，不再用一张误导的图去代表它）；
 * - conv-68-resync：RESYNC 在页面上是否可见，按实测结果如实命名；
 * - conv-66/67：把标题改准 —— 页面上的那条提示来自后端自己发的 unavailable，
 *   不是 paused/revoked 的专属提示（这两个状态在对话页没有渲染入口）。
 */
import {
  API as ENV_API,
  BASE as ENV_BASE,
  createSession,
  launchBrowser,
  readManifest,
  saveManifest,
  sleep,
} from "./lib.mjs";

const MAIN = { base: ENV_BASE, api: ENV_API };
const GROUP = "conv-root";
const entries = readManifest(GROUP)?.entries ?? [];
const log = [];

function put(entry) {
  const index = entries.findIndex((e) => e.id === entry.id);
  if (index >= 0) entries[index] = entry;
  else entries.push(entry);
}
function patch(id, fields) {
  const index = entries.findIndex((e) => e.id === id);
  if (index >= 0) entries[index] = { ...entries[index], ...fields };
  else log.push(`补丁未命中：${id}`);
}

const browser = await launchBrowser();

// 警告条：等历史加载完成（页面上出现过消息）之后才注入
{
  const s = await createSession(browser, { group: GROUP, name: "通知条收尾", theme: "dark", ...MAIN });
  await s.goto("#/", { waitFor: ".conversation", settle: 3000 });
  await s.page.locator(".message").first().waitFor({ state: "visible", timeout: 15000 }).catch(() => {});
  await s.inject("WARNING", { message: "搜索结果里有 2 条被截断，结论可能不完整。" });
  await s.page.waitForSelector(".notice.warn", { timeout: 10000 });
  await sleep(600);
  const text = (await s.page.locator(".notice.warn").first().innerText()).replace(/\s+/g, " ").trim();
  log.push(`conv-61-warning 实际文案：${text}`);
  put(
    await s.shot("conv-61-warning", "警告条：非致命提示", {
      note: `断言通过：${text.slice(0, 60)} · 注意：这条只有在历史加载完成之后注入才稳定可见（加载途中到达的会被清掉）`,
    }),
  );
  put(await s.shotEl(".notice.warn", "conv-61-warning-crop", "警告条（局部）", { pad: 8 }));
  await s.close({ save: false });
  saveManifest(GROUP, entries);
}

// RESYNC：按实测结果命名
{
  const s = await createSession(browser, { group: GROUP, name: "通知条收尾", theme: "dark", ...MAIN });
  await s.goto("#/", { waitFor: ".conversation", settle: 3000 });
  await s.page.locator(".message").first().waitFor({ state: "visible", timeout: 15000 }).catch(() => {});
  await s.inject("RESYNC", { reason: "subscriber_backlog_overflow" });
  await sleep(1500);
  const warn = (await s.page.locator(".notice.warn").count())
    ? (await s.page.locator(".notice.warn").first().innerText()).replace(/\s+/g, " ").trim()
    : null;
  log.push(`conv-68-resync 实际文案：${warn ?? "（无提示条）"}`);
  put(
    await s.shot("conv-68-resync", warn ? "事件流抖动：正在同步最新状态" : "事件流抖动：RESYNC 未产生可见提示", {
      note: warn
        ? `断言通过：${warn.slice(0, 60)}`
        : "实测：注入 RESYNC 后页面没有出现提示条（状态被重新拉取，但用户看不到一句话）",
    }),
  );
  await s.close({ save: false });
  saveManifest(GROUP, entries);
}

await browser.close();

// 把 paused / revoked 的标题与说明改准
patch("conv-66-cred-paused", {
  title: "凭据 paused：对话页无专属提示（页面上那条来自后端 unavailable）",
  note: "实测：CREDENTIAL_STATUS=paused 只写入 store 的 credentialNotice，没有任何组件渲染它；图里那条「当前没有可用的模型凭据」是本实例没有可用密钥时后端自己发的 unavailable 事件",
});
patch("conv-67-cred-revoked", {
  title: "凭据 revoked：对话页无专属提示（页面上那条来自后端 unavailable）",
  note: "实测：CREDENTIAL_STATUS=revoked 同样只写入 credentialNotice，没有渲染入口；图里的提示条来自后端的 unavailable 事件",
});

saveManifest(GROUP, entries);
console.log("收尾日志：");
for (const line of log) console.log(`  - ${line}`);
process.exit(0);
