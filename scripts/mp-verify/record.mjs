/**
 * 录屏：CDP Page.startScreencast 的**真实录屏**（不是逐帧截图拼接），帧序列经 ffmpeg 编码成 mp4。
 *
 * 说明（写进报告，别让人误会）：
 * - 帧由浏览器在每次合成后推送，时间戳**不等间隔**（软件光栅下尤其如此）；
 *   mp4 按「总帧数 / 总时长」得到的平均帧率编码，所以画面里的时间流逝是真实的，
 *   但不要拿视频里的帧数去反推某个动画跑了多少帧 —— 逐帧数据看 frames.mjs。
 * - 机器上没有 ffmpeg 时只落 JPEG 帧序列 + 说明，不假装有视频。
 *
 * 用法：
 *   node scripts/mp-verify/record.mjs --url http://127.0.0.1:5103 --label rev \
 *        --out docs/mp-verify/c-planet/record --action expand [--viewport 1440x900] [--fps 30]
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Browser } from "./lib/cdp.mjs";
import { DEFAULT_MANAGE_SELECTORS, openPlanetFromDock, toggleManagement, waitPlanetReady, waitSceneStatic } from "./lib/page.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};

const url = String(flag("url", "http://127.0.0.1:5103")).replace(/\/$/, "");
const label = String(flag("label", "rev"));
const action = String(flag("action", "expand"));
const outRoot = resolve(String(flag("out", join(here, "..", "..", "docs", "mp-verify", "c-planet", "record"))));
const viewportArg = String(flag("viewport", "1440x900"));
const [vpW, vpH] = viewportArg.split("x").map(Number);
const fps = Number(flag("fps", 30));
const openSelectors = String(flag("manage-open", DEFAULT_MANAGE_SELECTORS.open.join("|"))).split("|");
const closeSelectors = String(flag("manage-close", DEFAULT_MANAGE_SELECTORS.close.join("|"))).split("|");
const outDir = join(outRoot, label + "-" + viewportArg + "-" + action + (flag("max-width", null) ? "-s" + flag("max-width", "") : ""));
let outDirNote = "";
mkdirSync(outDir, { recursive: true });

function ffmpegPath() {
  const probe = spawnSync("ffmpeg", ["-version"], { encoding: "utf8" });
  return probe.status === 0 ? "ffmpeg" : null;
}

const report = { url, label, action, viewport: viewportArg, startedAt: new Date().toISOString(), notes: [] };
const browser = new Browser({ port: Number(flag("cdp-port", process.env.QIO_MP_CDP_PORT ?? 9801)), width: vpW, height: vpH });
try {
  await browser.launch();
  await browser.setViewport(vpW, vpH);
  await browser.navigate(url + "/?verify=" + Date.now() + "#/", 2600);
  await browser.waitFor("!!document.querySelector('[data-planet-entry], .dock')", { timeout: 25000 });
  await openPlanetFromDock(browser);
  await waitPlanetReady(browser);
  await waitSceneStatic(browser);
  if (action !== "expand") {
    await toggleManagement(browser, "open", { open: openSelectors, close: closeSelectors }).catch(() => {});
    await new Promise((r) => setTimeout(r, 1400));
    await waitSceneStatic(browser).catch(() => {});
  } else {
    await toggleManagement(browser, "close", { open: openSelectors, close: closeSelectors }).catch(() => {});
    await new Promise((r) => setTimeout(r, 1400));
  }

  /**
   * 录屏帧率受「每帧编码开销」限制：软件光栅 + 全尺寸画面时 CDP 只有 7–13fps。
   * --max-width 把画面缩到较小尺寸可以显著提高取样帧率（动作连续性看得更清），
   * 报告里会写明缩放后的尺寸，避免把「视频分辨率」误当「真实渲染分辨率」。
   */
  const maxWidth = Number(flag("max-width", vpW));
  const scale = maxWidth / vpW;
  await browser.startScreencast({
    format: "jpeg",
    quality: 82,
    maxWidth,
    maxHeight: Math.round(vpH * scale),
  });
  outDirNote = "screencast 缩放为 " + maxWidth + "×" + Math.round(vpH * scale) + "（真实渲染 " + vpW + "×" + vpH + "）";
  report.notes.push(outDirNote);
  const t0 = Date.now();
  if (action === "reverse") {
    await toggleManagement(browser, "open", { open: openSelectors, close: closeSelectors });
    // 中等进度时反向：按 --mp 判定（Node 侧轮询 20ms，足够在这个时长里抓住 0.4–0.6 区间）
    const deadline = Date.now() + 4000;
    while (Date.now() < deadline) {
      const mp = await browser.evalJs("(() => { const v = document.querySelector('.planet-view'); const raw = v ? getComputedStyle(v).getPropertyValue('--mp') : ''; return raw ? Number.parseFloat(raw) : null; })()");
      if (mp !== null && mp >= 0.45) break;
      await new Promise((r) => setTimeout(r, 20));
    }
    await toggleManagement(browser, "close", { open: openSelectors, close: closeSelectors });
  } else if (action === "expand") {
    await toggleManagement(browser, "open", { open: openSelectors, close: closeSelectors });
  } else {
    await toggleManagement(browser, "close", { open: openSelectors, close: closeSelectors });
  }
  await new Promise((r) => setTimeout(r, 1800));
  const screencast = await browser.stopScreencast();
  const elapsed = Date.now() - t0;
  const frames = screencast?.frames ?? [];
  report.screencast = {
    frames: frames.length,
    elapsedMs: elapsed,
    realFrameRate: frames.length ? Number(((frames.length - 1) / ((frames[frames.length - 1].timestamp - frames[0].timestamp))).toFixed(2)) : null,
    kind: "CDP Page.startScreencast（真实录屏）",
  };
  frames.forEach((frame, index) => {
    writeFileSync(join(outDir, "frame-" + String(index).padStart(4, "0") + ".jpg"), Buffer.from(frame.data, "base64"));
  });
  const ffmpeg = ffmpegPath();
  if (ffmpeg && frames.length > 2) {
    const video = join(outDir, "capture.mp4");
    const result = spawnSync(
      ffmpeg,
      ["-y", "-framerate", String(fps), "-i", join(outDir, "frame-%04d.jpg"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", video],
      { encoding: "utf8" },
    );
    if (result.status === 0) {
      report.video = video;
      report.notes.push("mp4 按 " + fps + "fps 编码；CDP 帧时间戳不等间隔，视频用于目视动作连续性，帧级数据以 frames.mjs 为准。");
    } else {
      report.notes.push("ffmpeg 编码失败：" + String(result.stderr || "").split("\n").slice(-4).join(" "));
    }
  } else {
    report.notes.push("没有可用的 ffmpeg：只落了 JPEG 帧序列（" + frames.length + " 帧）");
  }
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  report.error = String(error && error.stack ? error.stack : error);
  console.error("录屏失败：" + report.error);
} finally {
  report.finishedAt = new Date().toISOString();
  writeFileSync(join(outDir, "record.json"), JSON.stringify(report, null, 2), "utf8");
  await browser.close();
  console.log("输出目录：" + outDir);
}