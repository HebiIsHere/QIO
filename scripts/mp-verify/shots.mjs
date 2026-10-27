/**
 * 静止截图 + 页面度量：给「基线 vs 改版」的同尺寸、同朝向对比取证。
 *
 * 用法（默认连 Lead 分配的端口）：
 *   node scripts/mp-verify/shots.mjs --url http://127.0.0.1:5103 --label base \
 *        --out docs/mp-verify/c-planet/shots --viewports 1440x900,1024x768,900x700 \
 *        --scenarios planet,expanded,collapsed
 *
 * 每个（视口 × 场景）产出一组文件到 <out>/<label>/：
 *   <label>-<viewport>-<scenario>.png           整页（人看）
 *   <label>-<viewport>-<scenario>-stage.png     舞台区域裁剪（逐像素比对用）
 *   <label>-<viewport>-<scenario>-sphere.png    球心附近裁剪（量直径/环宽用）
 * report.json 里是同一时刻的页面度量：视口、内容区、画布、面板、--mp、横向滚动、场景诊断。
 *
 * 注意：所有截图都在 waitSceneStatic（朝向 + 渲染计数连续两次不变）之后拍，
 * 否则空闲自转会让两次截图本来就不可能一样。
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Browser } from "./lib/cdp.mjs";
import { decodePng, measureContour, measureSilhouette, scanSphereRow, strokeRunHistogram } from "./lib/png.mjs";
import {
  collectMetrics,
  DEFAULT_MANAGE_SELECTORS,
  openPlanetFromDock,
  toggleManagement,
  waitPlanetReady,
  waitSceneStatic,
} from "./lib/page.mjs";

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
const outRoot = resolve(String(flag("out", join(here, "..", "..", "docs", "mp-verify", "c-planet", "shots"))));
const outDir = join(outRoot, label);
/**
 * 归一化视口标签：基线窗口 1440x900（画布 900 高）↔ 改版窗口 1440x970（内容区 900 高）——
 * 球体半径与**画布高度**成正比，所以对照的两边必须换成同一画布高度，
 * 但报告里的场景键要写成同一个标签，compare.mjs 才配得上一对。
 */
const viewportTagHint = flag("viewport-tag", null);
const viewports = String(flag("viewports", "1440x900,1024x768"))
  .split(",")
  .map((item) => item.trim())
  .filter(Boolean)
  .map((item) => {
    const [w, h] = item.split("x").map(Number);
    return { width: w, height: h, tag: viewportTagHint ? String(viewportTagHint) : w + "x" + h, raw: w + "x" + h };
  });
const scenarios = String(flag("scenarios", "planet,expanded,collapsed"))
  .split(",")
  .map((item) => item.trim())
  .filter(Boolean);
const demoSpeed = flag("planetdemo", null);
const selectors = {
  open: String(flag("manage-open", DEFAULT_MANAGE_SELECTORS.open.join("|"))).split("|"),
  close: String(flag("manage-close", DEFAULT_MANAGE_SELECTORS.close.join("|"))).split("|"),
};
const settleMs = Number(flag("settle-ms", 500));

mkdirSync(outDir, { recursive: true });

const browser = new Browser({
  port: Number(flag("cdp-port", process.env.QIO_MP_CDP_PORT ?? 9801)),
  width: viewports[0]?.width ?? 1440,
  height: viewports[0]?.height ?? 900,
});

/** --mp 是否已经到 1（管理展开完毕）。没有 --mp 的基线页面视为「该场景不适用」。 */
async function waitMpAtLeast(browser, value, timeout = 6000) {
  return browser
    .waitFor(
      `(() => { const v = document.querySelector('.planet-view'); if (!v) return false; const raw = getComputedStyle(v).getPropertyValue('--mp'); if (!raw) return 'NO_MP'; return Number.parseFloat(raw) >= ` + value + `; })()`,
      { timeout: 150, interval: 100 },
    )
    .then(() => true)
    .catch(() => false);
}

async function stageClip(browser) {
  const metrics = await collectMetrics(browser);
  const stage = metrics.stage;
  if (!stage) return null;
  return { x: stage.left, y: stage.top, width: stage.width, height: stage.height };
}

/**
 * 球心附近的裁剪框，**夹取进视口**后再截图。
 *
 * 为什么要夹：球直径 ≈ 画布高 × 1.005，本来就比画布高略大一点（1440×900 时 905 > 900），
 * 直接把裁剪框往视口外一放，CDP 会把负坐标夹到 0 —— 截到的图与「请求的框」不是同一个区域，
 * 量出来的圆心/半径就偏了。所以先夹取，再用**实际裁剪框的原点**把像素坐标换算回视口坐标。
 */
function clampClip(clip, viewport) {
  const width = Math.min(clip.width, viewport.width);
  const height = Math.min(clip.height, viewport.height);
  return {
    x: Math.min(Math.max(0, clip.x), Math.max(0, viewport.width - width)),
    y: Math.min(Math.max(0, clip.y), Math.max(0, viewport.height - height)),
    width,
    height,
  };
}

async function sphereClip(browser, metrics) {
  const canvas = metrics.canvas;
  if (!canvas) return null;
  const size = Math.max(80, Math.min(canvas.width, canvas.height) * 1.02);
  return clampClip(
    {
      x: canvas.left + canvas.width / 2 - size / 2,
      y: canvas.top + canvas.height / 2 - size / 2,
      width: size,
      height: size,
    },
    metrics.viewport,
  );
}

const report = {
  url,
  label,
  startedAt: new Date().toISOString(),
  demoSpeed: demoSpeed === true ? "slow" : demoSpeed,
  viewports: [],
  runs: [],
  consoleErrors: [],
  httpFails: [],
};

try {
  await browser.launch();
  for (const viewport of viewports) {
    await browser.setViewport(viewport.width, viewport.height);
    const query = ["verify=" + Date.now()];
    if (demoSpeed) query.push("planetdemo=" + (demoSpeed === true ? "slow" : demoSpeed));
    await browser.navigate(url + "/?" + query.join("&") + "#/", 2600);
    await browser.waitFor("!!document.querySelector('[data-planet-entry], .dock')", { timeout: 25000 });
    await browser.settleFrames(3);
    const entry = await openPlanetFromDock(browser);
    await waitPlanetReady(browser);
    await new Promise((r) => setTimeout(r, settleMs));
    const staticState = await waitSceneStatic(browser);
    let state = "planet";

    for (const scenario of scenarios) {
      if (scenario === "planet") {
        // 已经在默认浏览态
      } else if (scenario === "expanded") {
        if (state !== "collapsed") {
          const clicked = await toggleManagement(browser, "open", selectors).catch(() => null);
          if (!clicked) {
            report.runs.push({ viewport: viewport.tag, scenario, skipped: true, reason: "页面还没有管理区入口（A 尚未交付）" });
            continue;
          }
          await waitMpAtLeast(browser, 0.999, 8000);
          await new Promise((r) => setTimeout(r, settleMs));
        }
        state = "expanded";
      } else if (scenario === "collapsed") {
        if (state === "expanded") {
          await toggleManagement(browser, "close", selectors).catch(() => null);
          await browser.waitFor(
            `(() => { const v = document.querySelector('.planet-view'); const raw = v ? getComputedStyle(v).getPropertyValue('--mp') : ''; return !raw || Number.parseFloat(raw) <= 0.001; })()`,
            { timeout: 8000, interval: 100 },
          ).catch(() => {});
          await new Promise((r) => setTimeout(r, settleMs));
        }
        state = "collapsed";
      } else {
        report.runs.push({ viewport: viewport.tag, scenario, skipped: true, reason: "未知场景" });
        continue;
      }

      const stillStatic = await waitSceneStatic(browser);
      const metrics = await collectMetrics(browser);
      const base = label + "-" + viewport.tag + "-" + scenario;
      await browser.shotFile(join(outDir, base + ".png"), null, true);
      const stage = await stageClip(browser);
      if (stage) await browser.shotFile(join(outDir, base + "-stage.png"), stage, false);
      const sphere = await sphereClip(browser, metrics);
      let measurement = null;
      if (sphere) {
        const file = await browser.shotFile(join(outDir, base + "-sphere.png"), sphere, false);
        const image = decodePng(readFileSync(file));
        const contour = measureContour(image);
        /**
         * 球心在裁剪图里的位置：球心 = 画布中心（planetGroup 在原点、相机看向原点）。
         * 用 DOM 量、不用拟合 —— 拟合只当旁证。行扫描用球心所在的那一行，
         * 这样「最左/最右列」就是球体最宽处。
         */
        const centerInImage = metrics.canvas
          ? {
              x: metrics.canvas.left + metrics.canvas.width / 2 - sphere.x,
              y: metrics.canvas.top + metrics.canvas.height / 2 - sphere.y,
            }
          : null;
        const rowScan = centerInImage
          ? scanSphereRow(image, {
              centerX: centerInImage.x,
              rows: [-6, -3, 0, 3, 6].map((offset) => centerInImage.y + offset),
            })
          : null;
        measurement = {
          crop: sphere,
          centerInImage,
          rowScan,
          contour,
          // 换算回视口坐标：像素证据要能直接对 layout 结论（球心 x 相对内容区左边）
          contourViewport: contour.ok
            ? {
                cx: Number((sphere.x + contour.cx).toFixed(2)),
                cy: Number((sphere.y + contour.cy).toFixed(2)),
                radius: contour.radius,
                diameter: contour.diameter,
              }
            : null,
          silhouette: measureSilhouette(image),
          strokeRuns: strokeRunHistogram(image),
        };
      }
      // 舞台裁剪上的玫红包围盒：「展开后只剩右半球」的判据（左边界贴内容区左边、右边界 ≈ 球心 + 半径）
      let stageMeasurement = null;
      if (stage) {
        const file = join(outDir, base + "-stage.png");
        if (existsSync(file)) {
          const image = decodePng(readFileSync(file));
          const silhouette = measureSilhouette(image);
          const canvasCenterX = metrics.canvas ? metrics.canvas.left + metrics.canvas.width / 2 - stage.x : null;
          stageMeasurement = {
            silhouette,
            canvasCenterX: canvasCenterX === null ? null : Number(canvasCenterX.toFixed(2)),
            canvasLeft: metrics.canvas ? Number((metrics.canvas.left - stage.x).toFixed(2)) : null,
            stageWidth: stage.width,
            visibleRightEdge: silhouette.ok ? silhouette.right : null,
            visibleLeftEdge: silhouette.ok ? silhouette.left : null,
            rightReachRatio:
              silhouette.ok && canvasCenterX !== null && metrics.stageHook?.sphere?.radius
                ? Number((silhouette.right / metrics.stageHook.sphere.radius).toFixed(3))
                : null,
          };
        }
      }
      const hook = metrics.stageHook ?? null;
      const radiusHook = hook && hook.sphere ? Number(hook.sphere.radius.toFixed(2)) : null;
      const labelX = hook && hook.selectedLabel ? Number((hook.selectedLabel.x - (metrics.content ? metrics.content.left : 0)).toFixed(1)) : null;
      report.runs.push({
        viewport: viewport.tag,
        viewportRaw: viewport.raw,
        scenario,
        entry,
        staticState: { stable: stillStatic.stable, samples: stillStatic.samples.length },
        radiusHook,
        labelX,
        metrics,
        measurement,
        stageMeasurement,
        files: {
          page: join(outDir, base + ".png"),
          stage: stage ? join(outDir, base + "-stage.png") : null,
          sphere: sphere ? join(outDir, base + "-sphere.png") : null,
        },
      });
      console.log(
        "[" + viewport.tag + "/" + scenario + "] mp=" + metrics.mp + " dataMp=" + metrics.dataMp +
          " managing=" + metrics.managing +
          " radiusHook=" + radiusHook +
          " contourR=" + (measurement && measurement.contour.ok ? measurement.contour.radius : "n/a") +
          " contourC=" + (measurement && measurement.contourViewport ? measurement.contourViewport.cx + "," + measurement.contourViewport.cy : "n/a") +
          " bbox=" + (measurement && measurement.silhouette.ok ? measurement.silhouette.width + "x" + measurement.silhouette.height : "n/a") +
          " stageLeft=" + (stageMeasurement ? stageMeasurement.visibleLeftEdge : "n/a") +
          " stageRight=" + (stageMeasurement ? stageMeasurement.visibleRightEdge : "n/a") +
          " labelX=" + labelX +
          " hOverflow=" + metrics.docScroll.horizontalOverflow,
      );
    }
    // 收起星球，回到对话页，准备下一个视口
    await browser.evalJs("(() => { const b = document.querySelector('.close-btn'); if (b) { b.click(); return true; } return false; })()");
    await new Promise((r) => setTimeout(r, 900));
    report.viewports.push(viewport.tag);
  }
} catch (error) {
  report.error = String(error && error.stack ? error.stack : error);
  console.error("截图失败：" + report.error);
} finally {
  report.consoleErrors = browser.consoleErrors;
  report.httpFails = browser.httpFails;
  report.finishedAt = new Date().toISOString();
  writeFileSync(join(outDir, "report.json"), JSON.stringify(report, null, 2), "utf8");
  await browser.close();
  console.log("报告：" + join(outDir, "report.json"));
}