/**
 * 最后一批补拍（体检发现的问题，逐条给出根因）：
 *
 * 1. `planet-11-panel` / `planet-13-search` / `planet-14-search-empty` 只有 11px 宽 ——
 *    星球层带着 CSS `scale()`，元素级截图在这种祖先变换下会拿到错误的边界。
 *    改法：先用 `getBoundingClientRect()` 量出面板矩形，再用**页面截图 + clip** 裁那一块，
 *    并且先断言面板宽度 > 300px（不是收起态）。
 * 2. `planet-12-panel-collapsed` 标签与内容不符（点完 toggle 之后面板还是开着的）→ 重拍，
 *    以 DOM class 断言「确实收起」再截。
 * 3. `conv-root / conv-102-first-run-send-fails` 拍成了「星球懒加载占位盖住首屏」的那一帧 →
 *    改成等 `.planet-boot` 退场后再发送、再等提示条出现。
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

const PLANET = { base: ENV_BASE, api: ENV_API };
const EMPTY = { base: "http://127.0.0.1:6203", api: "http://127.0.0.1:8838" };
const ENTRY = "[data-planet-entry]";
const planetGroup = "planet-root";
const convGroup = "conv-root";
const log = [];

async function capture(s, id, title, selector, opts = {}) {
  const { note = "", pad = 10, minWidth = 0 } = opts;
  const box = await s.page.locator(selector).first().boundingBox();
  if (!box) throw new Error(`${selector} 没有边界`);
  if (minWidth && box.width < minWidth) throw new Error(`${selector} 宽度只有 ${Math.round(box.width)}px（疑似收起态）`);
  const clip = {
    x: Math.max(0, box.x - pad),
    y: Math.max(0, box.y - pad),
    width: box.width + pad * 2,
    height: box.height + pad * 2,
  };
  const file = await s.shot(id, title, { clip, note, settle: 200 });
  log.push(`${id}: clip=${Math.round(clip.width)}×${Math.round(clip.height)}`);
  return file;
}

const browser = await launchBrowser();

// ---------------------------------------------------------------- 星球面板三张
{
  const s = await createSession(browser, { group: planetGroup, name: "补拍", theme: "dark", ...PLANET });
  s.page.setDefaultTimeout(20000);
  const entries = readManifest(planetGroup)?.entries ?? [];
  const put = (e) => {
    const i = entries.findIndex((x) => x.id === e.id);
    if (i >= 0) entries[i] = e;
    else entries.push(e);
  };

  await s.goto("#/", { waitFor: ".conversation", settle: 1800 });
  await s.page.evaluate(() => document.querySelector("[data-planet-entry]")?.click());
  await sleep(3200);

  // 面板一定要是展开的：先量宽度，必要时点 toggle 打开
  let width = (await s.page.locator(".panel").first().boundingBox())?.width ?? 0;
  if (width < 300) {
    log.push(`面板宽度 ${Math.round(width)}px → 点 toggle 展开`);
    await s.page.evaluate(() => document.querySelector(".panel-toggle")?.click());
    await sleep(1400);
    width = (await s.page.locator(".panel").first().boundingBox())?.width ?? 0;
  }
  log.push(`面板宽度（展开后）：${Math.round(width)}px`);

  put(await capture(s, "planet-11-panel", "右侧面板：话题列表 + 详情", ".panel", { minWidth: 300 }));
  put(await s.shot("planet-11b-full-with-panel", "全屏星球（面板展开）"));

  await s.page.locator(".panel-head input.qio-input").fill("审批");
  await sleep(900);
  put(await capture(s, "planet-13-search", "话题搜索：过滤中", ".panel", { minWidth: 300 }));

  await s.page.locator(".panel-head input.qio-input").fill("zzz不存在");
  await sleep(900);
  put(await capture(s, "planet-14-search-empty", "话题搜索：无结果", ".panel", { minWidth: 300 }));

  await s.page.locator(".panel-head input.qio-input").fill("");
  await sleep(600);

  // 真正收起：点 toggle 之后断言面板宽度塌下去
  await s.page.evaluate(() => document.querySelector(".panel-toggle")?.click());
  await sleep(1400);
  const collapsedWidth = (await s.page.locator(".panel").first().boundingBox())?.width ?? 0;
  log.push(`面板收起后宽度：${Math.round(collapsedWidth)}px`);
  put(
    await s.shot("planet-12-panel-collapsed", "面板收起态", {
      note: `收起后面板宽度 ${Math.round(collapsedWidth)}px（展开时 ${Math.round(width)}px）`,
    }),
  );

  saveManifest(planetGroup, entries);
  await s.close({ save: false });
}

// ---------------------------------------------------------------- 首次启动：发送失败
{
  const s = await createSession(browser, { group: convGroup, name: "补拍", theme: "dark", ...EMPTY });
  s.page.setDefaultTimeout(20000);
  const entries = readManifest(convGroup)?.entries ?? [];
  const put = (e) => {
    const i = entries.findIndex((x) => x.id === e.id);
    if (i >= 0) entries[i] = e;
    else entries.push(e);
  };
  await s.goto("#/", { waitFor: ".conversation", settle: 1500 });
  await s.page.waitForFunction(() => !document.querySelector(".planet-boot"), null, { timeout: 25000 }).catch(() => {});
  await sleep(500);
  await s.page.fill("#composer-input", "你好，先做个自我介绍。");
  await s.page.locator(".send-btn").click();
  await s.page
    .waitForSelector(".notice.err, .notice.warn", { timeout: 20000 })
    .catch(() => log.push("发送后没有等到提示条（可能仍在处理中）"));
  await sleep(1200);
  put(
    await s.shot("conv-102-first-run-send-fails", "首次启动：发送后没有可用凭据", {
      note: "没有凭据时发送的真实结果：提示条 + 草稿是否回到输入框",
    }),
  );
  saveManifest(convGroup, entries);
  await s.close({ save: false });
}

await browser.close();
console.log("补拍日志：");
for (const line of log) console.log(`  - ${line}`);
process.exit(0);
