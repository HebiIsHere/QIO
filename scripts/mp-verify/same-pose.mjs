/**
 * 同姿态逐像素比对（最强的一层美术证据）。
 *
 * 背景：星球的最终朝向在基线里不可复现（入口球态空闲自转的量取决于加载时长，
 * 聚焦 slerp 又被相机补间截断）。于是「基线 vs 改版」直接截图 diff 会得到 10%+ 的假差异。
 *
 * 办法：基线截图时顺手记下它的四元数（DEV 钩子 __qioPlanetStage().debug.quaternion），
 * 然后在改版页面上用 DEV-only 的 __qioPlanetSetPose({ quaternion }) 把球摆成同一个姿态
 * 并冻结自转，再用同一个裁剪框截图，与基线逐像素比 —— 这时差异就等于纯美术差异。
 *
 * 用法：
 *   node scripts/mp-verify/same-pose.mjs --url http://127.0.0.1:5113 --base docs/mp-verify/c-planet/shots/base \
 *        --out docs/mp-verify/c-planet/same-pose --label c
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { isAbsolute } from "node:path";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Browser } from "./lib/cdp.mjs";
import { decodePng, diffImage, diffStats, encodePng } from "./lib/png.mjs";
import { openPlanetFromDock, waitPlanetReady, waitSceneStatic } from "./lib/page.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const url = String(flag("url", "http://127.0.0.1:5113")).replace(/\/$/, "");
const baseDir = resolve(String(flag("base", "")));
const outDir = resolve(String(flag("out", join(here, "..", "..", "docs", "mp-verify", "c-planet", "same-pose"))));
const label = String(flag("label", "rev"));
const tolerance = Number(flag("tolerance", 2));
mkdirSync(outDir, { recursive: true });

const baseReport = JSON.parse(readFileSync(join(baseDir, "report.json"), "utf8"));
const baseRun = baseReport.runs.find((run) => run.scenario === "planet");
if (!baseRun) throw new Error("基线报告里没有 planet 场景");
const quaternion = baseRun.metrics?.stageHook?.debug?.quaternion;
const crop = baseRun.measurement?.crop;
const centerInImage = baseRun.measurement?.centerInImage;
if (!quaternion || !crop) throw new Error("基线报告缺少四元数或裁剪框信息（需要 shots.mjs 抓过 stageHook）");

const report = {
  url,
  label,
  method: "用基线四元数摆成同姿态后逐像素比（crop 与基线完全相同）",
  baseQuaternion: quaternion,
  crop,
  startedAt: new Date().toISOString(),
};
const browser = new Browser({ port: Number(flag("cdp-port", process.env.QIO_MP_CDP_PORT ?? 9801)), width: 1440, height: 900 });
try {
  await browser.launch();
  await browser.setViewport(1440, 900);
  await browser.navigate(url + "/?verify=" + Date.now() + "#/", 6000);
  await browser.waitFor("!!document.querySelector('[data-planet-entry], .dock')", { timeout: 40000 });
  await openPlanetFromDock(browser);
  await waitPlanetReady(browser);
  await waitSceneStatic(browser, { timeout: 12000 }).catch(() => {});
  const applied = await browser.evalJs(
    "(() => { const fn = window.__qioPlanetSetPose; if (typeof fn !== 'function') return 'NO_HOOK'; return fn({ quaternion: " +
      JSON.stringify(quaternion) +
      " }); })()",
  );
  report.poseApplied = applied;
  if (applied !== true) throw new Error("改版页面没有 __qioPlanetSetPose（DEV 钩子不可用：" + applied + "）");
  await browser.settleFrames(6, 6000).catch(() => {});
  await new Promise((r) => setTimeout(r, 600));
  const nowQuaternion = await browser.evalJs(
    "(() => { const s = (typeof window.__qioPlanetScene === 'function') ? window.__qioPlanetScene() : null; return s ? s.quaternion : null; })()",
  );
  report.appliedQuaternion = nowQuaternion;
  const file = await browser.shotFile(join(outDir, label + "-same-pose-sphere.png"), crop, false);
  // 报告里存的是绝对路径：绝对路径直接用，相对路径才拼 baseDir
  const baseSphere = baseRun.files.sphere;
  const a = decodePng(readFileSync(isAbsolute(baseSphere) ? baseSphere : join(baseDir, baseSphere)));
  const b = decodePng(readFileSync(file));
  if (a.width !== b.width || a.height !== b.height) {
    throw new Error("裁剪尺寸不同：" + a.width + "x" + a.height + " vs " + b.width + "x" + b.height);
  }
  const stats = diffStats(a, b, { tolerance });
  report.diff = stats;
  const heat = join(outDir, label + "-same-pose-diff.png");
  writeFileSync(heat, encodePng(diffImage(a, b, { tolerance })));
  report.heatmap = heat;
  report.centers = { base: centerInImage };
  console.log(JSON.stringify({ poseApplied: report.poseApplied, quaternion: nowQuaternion, changed: stats.changedPixels, total: stats.totalPixels, ratio: (stats.changedRatio * 100).toFixed(3) + "%", maxDelta: stats.maxDelta }, null, 1));
} catch (error) {
  report.error = String(error && error.stack ? error.stack : error);
  console.error("同姿态比对失败：" + report.error);
} finally {
  report.finishedAt = new Date().toISOString();
  writeFileSync(join(outDir, label + "-same-pose.json"), JSON.stringify(report, null, 2), "utf8");
  await browser.close();
}
