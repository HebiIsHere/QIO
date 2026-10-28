/**
 * 设置页录屏：CDP Page.startScreencast 的真实合成帧 → ffmpeg 编 mp4（不是逐帧截图拼接）。
 *
 * 关键流程：切换分类 / 宽窄切换 / 搜索定位 / Key 接入 / 分组保存。
 *
 * 真实性边界：录屏里的 Key 是企业内网都不用的假串（fake-...），
 * 写请求在这条录屏里被浏览器侧替身接住（不联网、不落库、不验证），
 * 报告里逐条写明哪些步骤是替身。没装 ffmpeg 时只落帧序列 + 明说没有视频。
 *
 * 用法：
 *   node scripts/settings-verify/record.mjs --url http://127.0.0.1:5106 \\
 *        --out docs/verification-video-settings --flows categories,resize,search,key-flow,group-save
 *   可选：--cdp-port 9806 --max-width 1280 --fps 24 --keep-frames
 */
import { mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { VerifyBrowser } from "./lib/browser.mjs";
import { openSettings, clickCategory, clickNavByText, installHelpers } from "./lib/probe.mjs";
import { installFixtures } from "./lib/fixtures.mjs";
import { EVIDENCE_DIR, SETTINGS_CATEGORIES, VIDEO_DIR, stamp } from "./lib/env.mjs";

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const appUrl = String(flag("url", "http://127.0.0.1:5106")).replace(/\/$/, "");
const cdpPort = Number(flag("cdp-port", 9806));
const apiBase = String(flag("api", process.env.QIO_SETTINGS_API || "http://127.0.0.1:8756")).replace(/\/$/, "");
const outDir = String(flag("out", VIDEO_DIR));
const label = String(flag("label", "run-" + stamp()));
const maxWidth = Number(flag("max-width", 1280));
const fps = Number(flag("fps", 24));
const keepFrames = !!flag("keep-frames", false);
const allowFail = !!flag("allow-fail", false);
const flowList = String(flag("flows", "categories,resize,search,key-flow,group-save")).split(",").map((item) => item.trim()).filter(Boolean);
mkdirSync(outDir, { recursive: true });
mkdirSync(EVIDENCE_DIR, { recursive: true });

function ffmpegPath() {
  const probe = spawnSync("ffmpeg", ["-version"], { encoding: "utf8" });
  return probe.status === 0 ? "ffmpeg" : null;
}

const report = { label, startedAt: new Date().toISOString(), appUrl, flows: [], notes: [] };

async function prepare(browser) {
  await browser.setViewport(1440, 900);
  await browser.setStoredPreference("qio-theme", "dark");
  await browser.setStoredPreference("qio-motion", "standard");
  await openSettings(browser, appUrl);
}

const FLOWS = {
  categories: {
    title: "切换五个分类",
    async run(browser) {
      await openSettings(browser, appUrl);
      for (const category of SETTINGS_CATEGORIES) {
        const hit = await clickCategory(browser, category);
        if (!hit?.ok) throw new Error("分类不可达：" + category.label + " :: " + JSON.stringify(hit));
        await sleep(760);
      }
      await sleep(500);
      return { categories: SETTINGS_CATEGORIES.map((category) => category.label) };
    },
  },
  resize: {
    title: "宽窄窗口切换（侧栏 → 顶部标签页）",
    async run(browser) {
      await openSettings(browser, appUrl);
      await clickCategory(browser, SETTINGS_CATEGORIES[1]);
      await sleep(600);
      for (const [width, height] of [[1024, 768], [800, 600], [480, 600], [1440, 900]]) {
        await browser.setViewport(width, height);
        await sleep(900);
      }
      return { viewports: ["1440x900", "1024x768", "800x600", "480x600", "1440x900"] };
    },
  },
  search: {
    title: "搜索定位（输入 → 结果 → 定位高亮）",
    async run(browser) {
      await openSettings(browser, appUrl);
      const opened = await browser.evalJs("window.__stg.openSearch()");
      if (!opened?.found) throw new Error("搜索入口不可达：" + JSON.stringify(opened));
      await sleep(500);
      await browser.evalJs("window.__stg.search('返回条数')");
      await sleep(900);
      const results = await browser.evalJs("window.__stg.searchResults()");
      if (!results?.length) throw new Error("搜索没有结果");
      await browser.evalJs("window.__stg.clickSearchResult(0)");
      await sleep(1200);
      const after = await browser.evalJs("window.__stg.activeCategory()");
      const highlighted = await browser.evalJs("window.__stg.highlighted()");
      return { query: "返回条数", hits: results.length, after, highlighted: highlighted?.length ?? 0 };
    },
  },
  "key-flow": {
    title: "Key 接入（假 Key + 替身响应，不联网）",
    async run(browser) {
      await openSettings(browser, appUrl);
      await clickCategory(browser, SETTINGS_CATEGORIES[1]);
      await sleep(500);
      const labels = ["新建 Key", "新建凭据", "添加 Key", "接入新的服务商", "新建连接", "新建"];
      let opened = { ok: false };
      for (const text of labels) {
        opened = await browser.evalJs("window.__stg.clickLabel(" + JSON.stringify(text) + ")");
        if (opened?.ok) break;
      }
      if (!opened?.ok) {
        await clickNavByText(browser, "凭据");
        await sleep(400);
        for (const text of labels) {
          opened = await browser.evalJs("window.__stg.clickLabel(" + JSON.stringify(text) + ")");
          if (opened?.ok) break;
        }
      }
      if (!opened?.ok) throw new Error("Key 接入入口不可达");
      await sleep(900);
      // 假 Key：明确不是任何真实 Key（前缀 fake-），只用于展示表单流程
      const fakeKey = "fake-verification-only-not-a-real-key";
      const filled = await browser.evalJs(
        "(() => { const inputs = [...window.__stg.root().querySelectorAll('input[type=password], input[type=text]')].filter((el) => window.__stg.visible(el)); if (!inputs.length) return { ok: false }; const el = inputs[0]; el.focus(); window.__stg.fireInput(el, " + JSON.stringify(fakeKey) + "); return { ok: true, id: el.getAttribute('aria-label') || el.className }; })()",
      );
      await sleep(700);
      for (const text of ["获取模型", "下一步", "连接并获取模型", "保存并验证", "连接", "保存"]) {
        const hit = await browser.evalJs("window.__stg.clickLabel(" + JSON.stringify(text) + ")");
        if (hit?.ok) break;
      }
      await sleep(1400);
      await browser.pressKey("Escape");
      await sleep(400);
      return { fakeKeyUsed: true, filled };
    },
  },
  "group-save": {
    title: "分组保存（改值 → 保存 → 成功反馈）",
    async run(browser) {
      await openSettings(browser, appUrl);
      await clickCategory(browser, SETTINGS_CATEGORIES[1]);
      await sleep(500);
      await browser.evalJs("window.__stg.expandAdvanced()");
      await sleep(300);
      const info = await browser.evalJs("window.__stg.findControl('返回条数')");
      if (!info?.found) throw new Error("找不到「返回条数」字段");
      await browser.evalJs("window.__stg.setControl('返回条数', '7')");
      await sleep(700);
      let clicked = { ok: false };
      for (const text of ["保存搜索配置", "保存修改", "保存设置", "保存本组"]) {
        clicked = await browser.evalJs("window.__stg.clickLabel(" + JSON.stringify(text) + ")");
        if (clicked?.ok) break;
      }
      if (!clicked?.ok) clicked = await browser.evalJs("window.__stg.clickLabel('保存', { exact: true })");
      await sleep(1300);
      const notices = await browser.evalJs("(() => [...window.__stg.root().querySelectorAll('[role=status], [role=alert], .msg, .notice, [data-settings-notice]')].filter((el) => window.__stg.visible(el)).map((el) => window.__stg.norm(el.innerText || el.textContent || '')))()");
      return { saveClicked: !!clicked?.ok, notices };
    },
  },
};

async function recordFlow(browser, name) {
  const flow = FLOWS[name];
  const entry = { name, title: flow.title ?? name, status: "pending", notes: [] };
  const dir = join(outDir, name);
  mkdirSync(dir, { recursive: true });
  try {
    await prepare(browser);
  } catch (error) {
    entry.status = "prepare-failed";
    entry.error = String(error?.message ?? error);
    report.flows.push(entry);
    console.log("[SKIP] " + name + "：准备失败 " + entry.error);
    return entry;
  }
  // 依赖 B 面板的流程在占位期如实跳过，不录一段「点不到入口」的假流程
  if (name === "key-flow") {
    const placeholder = await browser.evalJs("!!document.querySelector('[data-testid=models-panel-placeholder]')");
    if (placeholder) {
      entry.status = "skipped-placeholder";
      entry.notes.push("模型与服务里的 Key 面板由 B 提供，当前是占位；Key 接入流程等集成后再录。");
      writeFileSync(join(dir, "record.json"), JSON.stringify(entry, null, 2), "utf8");
      report.flows.push(entry);
      console.log("[SKIP] key-flow：B 的模型面板还是占位");
      return entry;
    }
  }
  await browser.startScreencast({ format: "jpeg", quality: 78, maxWidth, maxHeight: Math.round((900 * maxWidth) / 1440) });
  const startedAt = Date.now();
  try {
    entry.result = await flow.run(browser);
    entry.status = "recorded";
  } catch (error) {
    entry.status = "flow-failed";
    entry.error = String(error?.message ?? error);
    entry.notes.push("流程没走通：视频保留，用来定位卡在哪一步；不要把这段视频当作「已完成」的证据。");
  }
  await sleep(500);
  const screencast = await browser.stopScreencast();
  const frames = screencast?.frames ?? [];
  entry.elapsedMs = Date.now() - startedAt;
  entry.frames = frames.length;
  entry.realFrameRate = frames.length > 1 ? Number(((frames.length - 1) / ((frames[frames.length - 1].timestamp - frames[0].timestamp))).toFixed(2)) : null;
  frames.forEach((frame, index) => writeFileSync(join(dir, "frame-" + String(index).padStart(4, "0") + ".jpg"), Buffer.from(frame.data, "base64")));
  const ffmpeg = ffmpegPath();
  if (ffmpeg && frames.length > 2) {
    const video = join(dir, "capture.mp4");
    const encoded = spawnSync(
      ffmpeg,
      ["-y", "-framerate", String(fps), "-i", join(dir, "frame-%04d.jpg"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", video],
      { encoding: "utf8" },
    );
    if (encoded.status === 0) {
      entry.video = video;
      entry.notes.push("mp4 按 " + fps + "fps 编码；CDP 帧时间戳不等间隔，视频用于目视流程，帧级数据以截图与 JSON 为准。");
      if (!keepFrames) {
        for (const file of readdirSync(dir)) if (/^frame-.*\.jpg$/.test(file)) rmSync(join(dir, file));
        entry.notes.push("原始帧已删除（--keep-frames 可保留）。");
      }
    } else {
      entry.notes.push("ffmpeg 编码失败：" + String(encoded.stderr || "").split("\n").slice(-3).join(" "));
    }
  } else {
    entry.notes.push(ffmpeg ? "帧数不足，未编码视频" : "没有可用的 ffmpeg：只落了 JPEG 帧序列");
  }
  writeFileSync(join(dir, "record.json"), JSON.stringify({ ...entry, viewport: "1440x900", screencastScale: maxWidth }, null, 2), "utf8");
  report.flows.push(entry);
  console.log((entry.status === "recorded" ? "[OK] " : "[PARTIAL] ") + name + " :: " + JSON.stringify(entry.result ?? entry.error).slice(0, 240));
  return entry;
}

const browser = new VerifyBrowser({ port: cdpPort, width: 1440, height: 900, apiBase });
try {
  await browser.launch();
  await browser.navigate(appUrl + "/", 2200);
  const fixtures = await installFixtures(browser);
  report.fixtures = fixtures.hits;
  // Key 流程：所有写请求由替身接住，确保假 Key 不会被真的提交出去
  const fakeReply = (request) => {
    let body = {};
    try { body = request.postData ? JSON.parse(request.postData) : {}; } catch { body = {}; }
    if (/\/api\/models\/discover/.test(request.url)) {
      return { body: { credential_id: "cred_primary", provider_id: "example-primary", source: "provider", models: [{ model_id: "example-chat-2", display_name: "示例对话 2", source: "discovered", capabilities: ["chat"], verify_state: "unverified", verified_at: null, verify_error: null, usable: true, unavailable_reason: "" }], page: 1, page_size: 20, total: 1, has_more: false, error: null } };
    }
    if (/\/api\/credentials/.test(request.url)) {
      return { body: { ok: true, saved: true, credential: { key_id: "cred_new_fake", version: 1, tags: ["main-loop"], endpoint: null, default_model: "example-chat-2", budget: null, budget_used: 0, status: "active", enabled: true, note: null, kind: "openai", verify_state: "verified", verified_at: null, verify_error: null, is_default: false, provider_id: "example-primary", provider_name: "示例服务商甲" }, verify: { ok: true, state: "verified", reason_code: null, message: "验收替身：未发起真实调用", detail: "", mode: null } } };
    }
    return { body: { ok: true, ...body } };
  };
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    await browser.route({ method, path: /\/api\// }, fakeReply);
  }
  report.notes.push("录屏里的写请求全部由浏览器侧替身接住：假 Key 未提交、未联网、未落库；读取类数据同样是 fixture。");
  for (const name of flowList) {
    if (!FLOWS[name]) {
      report.flows.push({ name, status: "unknown-flow" });
      continue;
    }
    await recordFlow(browser, name);
  }
} catch (error) {
  report.error = String(error && error.stack ? error.stack : error);
  console.error("录屏中断：" + report.error);
} finally {
  report.finishedAt = new Date().toISOString();
  report.summary = {
    flows: report.flows.length,
    recorded: report.flows.filter((flow) => flow.status === "recorded").length,
    skipped: report.flows.filter((flow) => flow.status === "skipped-placeholder").length,
    partial: report.flows.filter((flow) => flow.status !== "recorded" && flow.status !== "skipped-placeholder").length,
    videos: report.flows.filter((flow) => flow.video).length,
  };
  writeFileSync(join(outDir, "record-report.json"), JSON.stringify(report, null, 2), "utf8");
  writeFileSync(join(EVIDENCE_DIR, "record-" + label + ".json"), JSON.stringify(report, null, 2), "utf8");
  await browser.close();
  console.log("\n录屏 " + report.summary.recorded + "/" + report.summary.flows + " 条完整，视频 " + report.summary.videos + " 个 → " + outDir);
  if (((report.summary.partial > 0 || report.error) && !allowFail)) process.exitCode = 1;
}
