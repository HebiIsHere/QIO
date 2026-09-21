/**
 * 上一轮 6 个界面/事件问题的**改后验证**（截图 + 客观断言）。
 *
 * 用法（隔离实例）：
 *   python scripts/ui-catalog/instance.py up --name fixes --backend-port 8843 --frontend-port 6208 --seed --clean
 *   $env:QIO_BASE="http://127.0.0.1:6208"; $env:QIO_API="http://127.0.0.1:8843"
 *   node scripts/ui-catalog/fixes-verify.mjs
 */
import {
  API as ENV_API,
  BASE as ENV_BASE,
  createSession,
  launchBrowser,
  saveManifest,
  sleep,
} from "./lib.mjs";

const INSTANCE = { base: ENV_BASE, api: ENV_API };
const GROUP = "fixes-verify";
const entries = [];
const results = [];

function record(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name} :: ${detail}`);
}

const browser = await launchBrowser();

// ── 问题 1：知识候选卡的按钮不再被输入框压住 ────────────────────────────
{
  const s = await createSession(browser, { group: GROUP, name: "候选卡位置", theme: "dark", ...INSTANCE });
  await s.goto("#/", { waitFor: ".conversation", settle: 1800 });
  await s.inject("TURN_START", { turn_id: "fix_kc", revision: 101 });
  await s.inject("KNOWLEDGE_CANDIDATE", {
    knowledge_id: "fix_kc_1",
    category: "user_profile",
    content: "用户偏好简洁、不啰嗦的解释，不要长篇大论。",
    reason: "这句话在本次对话里反复出现过",
  });
  await s.inject("TURN_END", {
    turn_id: "fix_kc",
    status: "completed",
    final_content: "记住了。",
    revision: 102,
  });
  await sleep(1600);

  const probe = await s.page.evaluate(() => {
    const card = document.querySelector(".candidate");
    const btn = card?.querySelector(".keep"); // 保存按钮
    const composer = document.querySelector(".composer");
    if (!btn || !composer) return { ok: false, why: "元素缺失" };
    const r = btn.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {
      ok: !!hit && (hit === btn || btn.contains(hit)),
      hit: hit ? `${hit.tagName}.${String(hit.className).slice(0, 24)}` : null,
      buttonBottom: Math.round(r.bottom),
      composerTop: Math.round(composer.getBoundingClientRect().top),
    };
  });
  record(
    "知识候选卡的按钮可点",
    probe.ok,
    `按钮中点命中=${probe.hit}；按钮下沿=${probe.buttonBottom}px，输入框上沿=${probe.composerTop}px`,
  );
  entries.push(await s.shot("fix-01-candidate-clickable", "改后：候选卡在输入框上方，按钮可点"));
  await s.close({ save: false });
}

// ── 问题 4：新打开的页面不再出现上一次的提示与排队 ───────────────────────
{
  const a = await createSession(browser, { group: GROUP, name: "残留制造", theme: "dark", ...INSTANCE });
  await a.goto("#/", { waitFor: ".conversation", settle: 1800 });
  await a.inject("TURN_QUEUE", {
    revision: 901,
    running: { turn_id: "fix_q1", message: "上一条还在跑的任务" },
    queued: [{ turn_id: "fix_q2", message: "排队里的第一条" }],
    cancelled: [{ turn_id: "fix_q3", message: "上一条被取消的任务" }],
  });
  await a.inject("ERROR", { message: "上一条消息执行失败：模型调用被拒绝（401）" });
  await sleep(900);
  await a.close({ save: false });

  const b = await createSession(browser, { group: GROUP, name: "残留检查", theme: "dark", ...INSTANCE });
  await b.goto("#/", { waitFor: ".conversation", settle: 2600 });
  const residue = await b.page.evaluate(() => {
    const text = (sel) => {
      const el = document.querySelector(sel);
      return el ? el.innerText.replace(/\s+/g, " ").trim() : null;
    };
    return { err: text(".notice.err"), queue: text(".queue"), warn: text(".notice.warn") };
  });
  record(
    "新页面没有上一次的残留",
    !residue.err && !residue.queue,
    `错误条=${residue.err ?? "无"} / 排队=${residue.queue ?? "无"}`,
  );
  entries.push(await b.shot("fix-04-no-replay", "改后：新打开的页面是干净的"));
  await b.close({ save: false });
}

// ── 问题 5：凭据暂停有可见说明 ──────────────────────────────────────────
{
  const s = await createSession(browser, { group: GROUP, name: "凭据提示", theme: "dark", ...INSTANCE });
  await s.goto("#/", { waitFor: ".conversation", settle: 2200 });
  await s.inject("CREDENTIAL_STATUS", { status: "paused" });
  await sleep(900);
  const text = await s.page.evaluate(() => {
    const el = document.querySelector(".notice.warn");
    return el ? el.innerText.replace(/\s+/g, " ").trim() : null;
  });
  record("凭据暂停有可见提示", !!text && text.includes("暂停"), text ?? "（没有提示条）");
  entries.push(await s.shot("fix-05-credential-notice", "改后：凭据暂停在对话页有说明"));
  await s.close({ save: false });
}

// ── 问题 6a：同步很快时不再闪一下就消失，也不留下假提示 ─────────────────
{
  const s = await createSession(browser, { group: GROUP, name: "同步提示", theme: "dark", ...INSTANCE });
  await s.goto("#/", { waitFor: ".conversation", settle: 2200 });
  await s.inject("RESYNC", { reason: "subscriber_backlog_overflow" });
  await sleep(1200);
  const quick = await s.page.evaluate(() => {
    const el = document.querySelector(".notice.warn");
    return el ? el.innerText.replace(/\s+/g, " ").trim() : null;
  });
  record("同步很快完成时不留下提示（不制造噪音）", quick === null, quick ?? "无提示");
  entries.push(await s.shot("fix-06-resync-quiet", "改后：快速同步不留下提示"));
  await s.close({ save: false });
}

// ── 问题 7：3D 不可用时说明独立成块 ─────────────────────────────────────
{
  const s = await createSession(browser, { group: GROUP, name: "3D 降级", theme: "dark", ...INSTANCE });
  await s.context.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, ...rest) {
      if (String(type).toLowerCase().startsWith("webgl")) return null;
      return original.call(this, type, ...rest);
    };
  });
  await s.goto("#/", { waitFor: ".conversation", settle: 1600 });
  await s.page.evaluate(() => document.querySelector("[data-planet-entry]")?.click());
  await s.page.waitForSelector(".webgl-fallback .wf-card", { timeout: 20000 }).catch(() => {});
  await sleep(1200);
  const style = await s.page.evaluate(() => {
    const card = document.querySelector(".webgl-fallback .wf-card");
    if (!card) return null;
    const cs = getComputedStyle(card);
    return { bg: cs.backgroundColor, padding: cs.paddingTop, radius: cs.borderTopLeftRadius };
  });
  record(
    "3D 降级说明是独立卡片",
    !!style && style.bg !== "rgba(0, 0, 0, 0)" && parseFloat(style.padding) >= 12,
    style ? `底色=${style.bg} 内边距=${style.padding} 圆角=${style.radius}` : "卡片不存在",
  );
  entries.push(await s.shot("fix-07-webgl-card", "改后：3D 不可用的说明块"));
  await s.close({ save: false });
}

await browser.close();
saveManifest(GROUP, entries);
const failed = results.filter((r) => !r.ok);
console.log(`\n验证：${results.length - failed.length}/${results.length} 通过`);
process.exit(failed.length ? 1 : 0);
