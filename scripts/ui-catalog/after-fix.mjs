/**
 * 采集「本轮修复之后」的界面状态，补进对应的分组，让 UI 图册反映当前界面。
 *
 * 为什么单独一个脚本：修复改的是**已有状态的外观与行为**（底部让位、凭据提示、
 * 接续提示、同步提示、3D 降级卡片、设置页分段文案），旧图是修复前的现场，
 * 既是证据也不该被覆盖 —— 这里按新 id 追加「改后」的图。
 *
 * 分组归属沿用图册的分区：对话页相关进 `conv-root`，设置页进 `settings-root`，
 * 星球降级进 `planet-root`（这样 human-page 的分区里能直接看到）。
 *
 * 用法：
 *   python scripts/ui-catalog/instance.py up --name afterfix --backend-port 8847 --frontend-port 6212 --seed --clean
 *   $env:QIO_BASE="http://127.0.0.1:6212"; $env:QIO_API="http://127.0.0.1:8847"
 *   node scripts/ui-catalog/after-fix.mjs
 */
import {
  apiRequest,
  createSession,
  launchBrowser,
  readManifest,
  saveManifest,
  sleep,
} from "./lib.mjs";

const INSTANCE = { base: process.env.QIO_BASE, api: process.env.QIO_API };
const results = [];

async function flush(group, entries) {
  saveManifest(group, entries);
}

async function appendTo(group, newEntries) {
  const existing = readManifest(group)?.entries ?? [];
  const byId = new Map(existing.map((e) => [e.id, e]));
  for (const entry of newEntries) byId.set(entry.id, entry);
  const merged = [...byId.values()];
  await flush(group, merged);
  return merged.length;
}

const record = (name, ok, detail) => {
  results.push({ name, ok, detail });
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name} :: ${detail}`);
};

const browser = await launchBrowser();

// ── 对话页（conv-root）：底部让位 / 凭据提示 / 接续提示 / 同步提示 ────────
const conv = await createSession(browser, { group: "conv-root", name: "修复后", theme: "dark", ...INSTANCE });
const convEntries = [];

// 1) 底部三块让位：知识候选卡完整显示且按钮可点
await conv.goto("#/", { waitFor: ".conversation", settle: 1800 });
await conv.inject("TURN_START", { turn_id: "fx_kc", revision: 201 });
await conv.inject("KNOWLEDGE_CANDIDATE", {
  knowledge_id: "fx_kc_1",
  category: "user_profile",
  content: "用户偏好简洁、不啰嗦的解释，不要长篇大论。",
  reason: "这句话在本次对话里反复出现过",
});
await conv.inject("TURN_END", { turn_id: "fx_kc", status: "completed", final_content: "记住了。", revision: 202 });
await sleep(1500);
{
  const probe = await conv.page.evaluate(() => {
    const btn = document.querySelector(".candidate .keep");
    const composer = document.querySelector(".composer");
    if (!btn || !composer) return { ok: false };
    const r = btn.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {
      ok: !!hit && (hit === btn || btn.contains(hit)),
      hit: hit ? hit.tagName : null,
      btnBottom: Math.round(r.bottom),
      composerTop: Math.round(composer.getBoundingClientRect().top),
    };
  });
  record(
    "候选卡在输入区上方（按钮可点）",
    probe.ok,
    `按钮中点命中=${probe.hit}；按钮下沿 ${probe.btnBottom}px < 输入框上沿 ${probe.composerTop}px`,
  );
  convEntries.push(
    await conv.shot("conv-130-candidate-above-composer", "已修复：知识候选卡在输入区上方，按钮可点", {
      note: `实测：按钮中点命中元素是 ${probe.hit}（不是输入框）`,
    }),
  );
}

// 2) 凭据暂停：对话页出现人话提示与恢复入口
await conv.goto("#/", { waitFor: ".conversation", settle: 2200 });
await conv.inject("CREDENTIAL_STATUS", { status: "paused" });
await sleep(900);
{
  const text = await conv.page.evaluate(() => {
    const el = document.querySelector(".notice.warn");
    return el ? el.innerText.replace(/\s+/g, " ").trim() : null;
  });
  record("凭据暂停有可见提示", !!text && text.includes("暂停"), text ?? "（没有提示）");
  convEntries.push(await conv.shotEl(".notice.warn", "conv-131-credential-paused-notice", "已修复：凭据暂停在对话页有说明", { pad: 8 }));
}

// 3) 话题切换提示条同样在输入区上方
await conv.goto("#/", { waitFor: ".conversation", settle: 2200 });
await conv.inject("TOPIC_SWITCH_SUGGESTED", {
  topic_id: "topic_other",
  topic_name: "对话深度与迭代预算",
  reason: "内容看起来属于另一个话题",
});
await conv.page.waitForSelector(".topic-switch", { timeout: 8000 });
await sleep(700);
{
  const probe = await conv.page.evaluate(() => {
    const el = document.querySelector(".topic-switch");
    const composer = document.querySelector(".composer");
    if (!el || !composer) return null;
    const r = el.getBoundingClientRect();
    return { bottom: Math.round(r.bottom), composerTop: Math.round(composer.getBoundingClientRect().top) };
  });
  record(
    "话题切换提示条在输入区上方",
    !!probe && probe.bottom <= probe.composerTop,
    probe ? `提示条下沿 ${probe.bottom}px ≤ 输入框上沿 ${probe.composerTop}px` : "元素缺失",
  );
  convEntries.push(await conv.shot("conv-132-topic-switch-above-composer", "已修复：话题切换提示条在输入区上方"));
}

// 4) 接续提示：说明生效时机 + 可取消
{
  const overview = await apiRequest("GET", "/api/planet/overview", undefined, INSTANCE.api);
  let target = null;
  for (const topic of overview.topics ?? []) {
    const detail = await apiRequest(
      "GET",
      `/api/graph/topics/${encodeURIComponent(topic.topic_id)}`,
      undefined,
      INSTANCE.api,
    ).catch(() => null);
    const fragments = detail?.fragments ?? [];
    const closed = fragments.find((f) => f.closed_at);
    if (closed) {
      target = { topicId: topic.topic_id, fragmentId: closed.fragment_id ?? closed.id };
      break;
    }
  }
  if (target) {
    await conv.goto("#/", { waitFor: ".conversation", settle: 2000 });
    await apiRequest(
      "POST",
      "/api/anchor",
      { topic_id: target.topicId, fragment_id: target.fragmentId, continue_from_history: true },
      INSTANCE.api,
    );
    await conv.page.waitForSelector(".anchor.pending", { timeout: 10000 }).catch(() => {});
    await sleep(700);
    const hint = await conv.page.evaluate(() => {
      const el = document.querySelector(".anchor.pending");
      return el ? el.textContent.trim() : null;
    });
    record("接续提示说明生效时机", !!hint && hint.includes("下一条消息"), hint ?? "（没有提示）");
    convEntries.push(await conv.shotEl(".composer", "conv-133-continuation-hint", "已修复：接续提示「下一条消息将从…继续」+ 取消", { pad: 10 }));
    // 取消之后提示消失
    await conv.page.locator(".anchor-cancel").click({ timeout: 8000 }).catch(() => {});
    await sleep(1200);
    const stillThere = await conv.page.evaluate(() => !!document.querySelector(".anchor.pending"));
    convEntries.push(await conv.shotEl(".composer", "conv-134-continuation-cancelled", "已修复：取消接续登记后提示消失", { pad: 10 }));
    record("取消接续后提示消失", stillThere === false, `仍在=${stillThere}`);
  } else {
    record("接续提示采集", false, "没有找到可用的历史片段");
  }
}

// 5) 同步提示：快速同步不再闪一下又不留痕
await conv.goto("#/", { waitFor: ".conversation", settle: 2200 });
await conv.inject("RESYNC", { reason: "subscriber_backlog_overflow" });
await sleep(1200);
{
  const notice = await conv.page.evaluate(() => {
    const el = document.querySelector(".notice.warn");
    return el ? el.innerText.trim() : null;
  });
  record("快速同步不留下提示", notice === null, notice ?? "无提示");
  convEntries.push(await conv.shot("conv-135-resync-quiet", "已修复：连接抖动快速同步后不留提示"));
}

// 6) 开发者模式下的底部三块（更容易看出让位效果）
await conv.close({ save: false });
const convDev = await createSession(browser, {
  group: "conv-root",
  name: "修复后-开发者模式",
  theme: "dark",
  developerMode: true,
  ...INSTANCE,
});
await convDev.goto("#/", { waitFor: ".conversation", settle: 1800 });
await convDev.inject("TURN_START", { turn_id: "fx_dev", revision: 211 });
await convDev.inject("FALLBACK", { message: "当前模型不支持原生工具调用，已使用兼容模式（功能可能受限）" });
await sleep(900);
convEntries.push(await convDev.shot("conv-136-fallback-above-composer", "已修复：兼容模式提示条在输入区上方"));
await convDev.close({ save: false });

console.log(`\nconv-root 新图 ${convEntries.length} 张`);
console.log(`conv-root 累计 ${await appendTo("conv-root", convEntries)} 条`);

// ── 设置页（settings-root）：分段语义与单段长度 ─────────────────────────
const setEntries = [];
for (const theme of ["dark", "light"]) {
  const s = await createSession(browser, { group: "settings-root", name: `分段文案-${theme}`, theme, ...INSTANCE });
  await s.goto("#/settings", { waitFor: ".settings", settle: 1200 });
  await s.page.locator(".nav .tab", { hasText: "对话与记忆" }).first().click();
  await sleep(800);
  const text = await s.page.locator(".panel:visible").first().innerText();
  const ok =
    text.includes("根据讨论的进展分段") &&
    text.includes("单段长度目标") &&
    text.includes("不等于上一段的任务已经完成");
  record(`设置页分段文案（${theme}）`, ok, ok ? "含分段语义与单段长度设置" : "文案缺失");
  setEntries.push(
    await s.shotEl(
      ".panel:visible",
      `settings-${theme}-segment-copy`,
      `已修复：设置页「记忆」说明分段语义并提供单段长度目标（${theme}）`,
      { pad: 10 },
    ),
  );
  await s.close({ save: false });
}
console.log(`settings-root 累计 ${await appendTo("settings-root", setEntries)} 条`);

// ── 星球（planet-root）：3D 不可用是独立卡片 ────────────────────────────
const planetEntries = [];
{
  const s = await createSession(browser, { group: "planet-root", name: "3D降级-修复后", theme: "dark", ...INSTANCE });
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
  await sleep(1500);
  const style = await s.page.evaluate(() => {
    const card = document.querySelector(".webgl-fallback .wf-card");
    if (!card) return null;
    const cs = getComputedStyle(card);
    return { bg: cs.backgroundColor, padding: cs.paddingTop };
  });
  record(
    "3D 降级是独立卡片",
    !!style && style.bg !== "rgba(0, 0, 0, 0)" && parseFloat(style.padding) >= 12,
    style ? `底色=${style.bg} 内边距=${style.padding}` : "卡片不存在",
  );
  planetEntries.push(
    await s.shot("planet-80-webgl-fallback-card", "已修复：3D 不可用的说明是独立卡片（不叠在聊天内容上）"),
  );
  await s.close({ save: false });
}
console.log(`planet-root 累计 ${await appendTo("planet-root", planetEntries)} 条`);

await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n断言：${results.length - failed.length}/${results.length} 通过`);
process.exit(failed.length ? 1 : 0);
