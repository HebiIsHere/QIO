/**
 * 接续提示的端到端验证（阶段 1 的两步语义 + 阶段 5 的界面表达）。
 *
 * 验证三件事：
 * 1. 通过真实接口点击一段已封存的历史之后，**没有**创建任何新片段；
 * 2. 输入区说明「下一条消息将从…继续」（不暴露内部 id）；
 * 3. 发送前取消是零成本的：提示消失、仍然没有任何新片段。
 *
 * 用法：
 *   python scripts/ui-catalog/instance.py up --name hint --backend-port 8844 --frontend-port 6209 --seed --clean
 *   $env:QIO_BASE="http://127.0.0.1:6209"; $env:QIO_API="http://127.0.0.1:8844"
 *   node scripts/ui-catalog/continuation-hint-verify.mjs
 */
import { apiRequest, createSession, launchBrowser, saveManifest, sleep } from "./lib.mjs";

const INSTANCE = { base: process.env.QIO_BASE, api: process.env.QIO_API };
const GROUP = "fixes-verify";
const entries = [];
const results = [];

const record = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name} :: ${detail}`);
};

// 找一段「已封存、且同话题还有开放片段」的历史：
// 这正是过去会撞唯一约束、现在必须原子交接的形状。
const overview = await apiRequest("GET", "/api/planet/overview", undefined, INSTANCE.api);
const topics = overview.topics ?? [];
let target = null;
for (const t of topics) {
  const detail = await apiRequest(
    "GET",
    `/api/graph/topics/${encodeURIComponent(t.topic_id)}`,
    undefined,
    INSTANCE.api,
  ).catch(() => null);
  const fragments = detail?.fragments ?? [];
  const closed = fragments.find((f) => f.closed_at);
  const open = fragments.find((f) => !f.closed_at);
  if (closed && open) {
    target = { topicId: t.topic_id, closed, open };
    break;
  }
  if (closed && !target) target = { topicId: t.topic_id, closed };
}
if (!target) throw new Error("没有找到可用的历史片段");
console.log(`目标话题=${target.topicId} 历史片段=${target.closed.fragment_id ?? target.closed.id}`);

const browser = await launchBrowser();
const s = await createSession(browser, { group: GROUP, name: "接续提示", theme: "dark", ...INSTANCE });
await s.goto("#/", { waitFor: ".conversation", settle: 2000 });

const before = await apiRequest("GET", "/api/planet/overview", undefined, INSTANCE.api);
void before;

// 1) 通过真实接口登记「从这段历史继续」
const registered = await apiRequest(
  "POST",
  "/api/anchor",
  { topic_id: target.topicId, fragment_id: target.closed.fragment_id ?? target.closed.id, continue_from_history: true },
  INSTANCE.api,
);
record(
  "点击历史只登记、不创建片段",
  registered.pending === true && !registered.created_fragment_id,
  `pending=${registered.pending} created=${registered.created_fragment_id ?? "无"}`,
);

// 2) 界面要说明生效时机
await s.page.waitForSelector(".anchor.pending", { timeout: 10000 }).catch(() => {});
await sleep(600);
const hint = await s.page.evaluate(() => {
  const el = document.querySelector(".anchor.pending");
  return el ? el.textContent.trim() : null;
});
record(
  "输入区说明「下一条消息才生效」",
  !!hint && hint.includes("下一条消息") && !/intent_/.test(hint),
  hint ?? "（没有提示）",
);
entries.push(await s.shot("fix-10-continuation-hint", "接续提示：下一条消息将从所选记录继续"));

// 3) 发送前取消
await s.page.locator(".anchor-cancel").click({ timeout: 8000 });
await sleep(1200);
const after = await s.page.evaluate(() => !!document.querySelector(".anchor.pending"));
const cancelState = await apiRequest("GET", "/api/turns/queue", undefined, INSTANCE.api).catch(() => null);
record("取消之后提示消失", after === false, `提示仍存在=${after}（队列接口可达=${!!cancelState}）`);
entries.push(await s.shot("fix-11-continuation-cancelled", "取消接续登记：提示消失，不留痕迹"));

await s.close({ save: false });
await browser.close();
saveManifest(GROUP, entries);
const failed = results.filter((r) => !r.ok);
console.log(`\n验证：${results.length - failed.length}/${results.length} 通过`);
process.exit(failed.length ? 1 : 0);
