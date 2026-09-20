// 抓「星球缩小的途中闪现」：用 CDP screencast 抓**每一帧合成画面**，逐帧量球的粉色墨迹
// （数量/球心/包围盒），把异常帧连前后邻居一起存成 PNG，供人眼复核。
//
// 为什么需要它（2026-09-20）：用户实测「缩小途中闪现一下」。这条链路上真正的证据只有
// **合成后的逐帧画面** —— 页面内读状态、读 computed style、读 uniform 都看不见它：
// 问题是「某一帧渲染时用的几何与那一帧被合成时用的几何不一致」，两者的差只在像素上。
// 实测定位到：尾段切布局那一帧，环宽补偿还是旧约定下的值（整层缩放 0.13 时的 ≈2.0），
// 而画布已经变成球自己的 108px（缓冲像素 = 屏幕像素）—— 于是环被画成 12px 宽，
// 看起来是一块实心亮斑；下一帧读到新变换才恢复正常，用户看到的就是「闪一下」。
//
// 用法（服务需在跑）：
//   node scripts/baseline/qa/planet-flash-probe.mjs <tag> [--slow]
// 输出：%TEMP%\qio-probe\out\<tag>-frame-*.png（异常帧与邻居）与 <tag>-report.json
//
// 判读：看「跳变」列里**向上**的跳变（墨迹比邻居多得多 = 环被画粗了/画成实心斑）。
// 单调衰减（球在变小）会让墨迹一路下降，那是正常的，不是异常。
import { createRequire } from "node:module";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const { chromium } = require(`${process.env.TEMP}\\qio-pw\\node_modules\\playwright-core`);
const CHROME = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => existsSync(p));

const TAG = process.argv[2] ?? "flash";
const SLOW = process.argv.includes("--slow");
const OUT = `${process.env.TEMP}\\qio-probe\\out`;
mkdirSync(OUT, { recursive: true });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
let seq = Math.floor(Date.now() / 1000) % 100000;

const browser = await chromium.launch({
  ...(CHROME ? { executablePath: CHROME } : { channel: "msedge" }),
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => {
  try {
    localStorage.setItem("qio-theme", "dark");
  } catch {
    /* 忽略 */
  }
});
const page = await context.newPage();
page.on("pageerror", (e) => console.log("[pageerror]", String(e.message).slice(0, 200)));

await page.goto(`http://127.0.0.1:5199/?${SLOW ? "planetdemo=slow&" : ""}fresh=${++seq}#/`);
await page.waitForSelector("#composer-input", { timeout: 15000 });
await page
  .waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).ball === true, { timeout: 15000 })
  .catch(() => console.log("[warn] 等球态超时"));
await wait(1000);
await page.$eval(".dock", (el) => el.click());
await page.waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).phase === "ready", { timeout: 20000 });
await wait(600);

// screencast：半分辨率 PNG，每帧都收
const cdp = await context.newCDPSession(page);
const frames = [];
cdp.on("Page.screencastFrame", async (ev) => {
  frames.push({
    ts: ev.metadata.timestamp,
    data: ev.data,
    // 落点窗口：入口小球在 (1366,450) 附近，尾段与交接都发生在这一带
    dock: { x: 1318 - 140, y: 402 - 140, w: 96 + 280, h: 96 + 280 },
  });
  try {
    await cdp.send("Page.screencastFrameAck", { sessionId: ev.sessionId });
  } catch {
    /* 忽略 */
  }
});
await cdp.send("Page.enable");
await cdp.send("Page.startScreencast", { format: "png", everyNthFrame: 1, maxWidth: 720, maxHeight: 450 });
// 逐帧记状态（与画面互相对照：切换布局那一帧的几何来自这里）
await page.evaluate(() => {
  window.__flash = [];
  const step = () => {
    const v = document.querySelector(".planet-view");
    const c = document.querySelector(".planet-canvas");
    const h = window.__qioPlanetStage?.();
    window.__flash.push({
      ms: Math.round(performance.now()),
      tail: v ? v.classList.contains("tail") : null,
      ball: v ? v.classList.contains("ball") : null,
      canvas: c ? c.clientWidth : null,
      radius: h?.sphere?.radius ? Math.round(h.sphere.radius * 10) / 10 : null,
      k: h?.k ?? null,
    });
    if (window.__flash.length < 2000) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
});
await page.$eval(".close-btn", (el) => el.click()).catch(() => {});
await wait(SLOW ? 3000 : 2000);
await cdp.send("Page.stopScreencast").catch(() => {});
const state = await page.evaluate(() => window.__flash ?? []);
console.log(`[采集] screencast 帧 ${frames.length} 帧；rAF 状态 ${state.length} 条（${SLOW ? "慢放 3×" : "常速"}）`);

// 逐帧量墨迹（分批送进页面解码，避免一次传太多）
const stats = [];
for (let i = 0; i < frames.length; i += 8) {
  const batch = frames.slice(i, i + 8).map((f) => f.data);
  const res = await page.evaluate(async (list) => {
    const out = [];
    for (const b64 of list) {
      const img = new Image();
      img.src = "data:image/png;base64," + b64;
      await img.decode();
      const c = document.createElement("canvas");
      c.width = img.width;
      c.height = img.height;
      const ctx = c.getContext("2d");
      ctx.drawImage(img, 0, 0);
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      // 粉色墨迹：R 明显高于 G（对话页的灰白文字/控件不会通过这一条）
      let n = 0;
      let sx = 0;
      let sy = 0;
      let minX = 1e9;
      let maxX = -1;
      let minY = 1e9;
      let maxY = -1;
      for (let y = 0; y < c.height; y++) {
        for (let x = 0; x < c.width; x++) {
          const i4 = (y * c.width + x) * 4;
          const r = d[i4];
          const g = d[i4 + 1];
          const b = d[i4 + 2];
          // 收紧：球体环/轮廓是 0xe878bd（232,120,189），对话页的粉色控件淡得多
          if (r > 120 && r - g > 55 && r - b > 20) {
            n++;
            sx += x;
            sy += y;
            if (x < minX) minX = x;
            if (x > maxX) maxX = x;
            if (y < minY) minY = y;
            if (y > maxY) maxY = y;
          }
        }
      }
      out.push(
        n
          ? { n, cx: Math.round(sx / n), cy: Math.round(sy / n), w: maxX - minX + 1, h: maxY - minY + 1 }
          : { n: 0, cx: null, cy: null, w: 0, h: 0 },
      );
    }
    return out;
  }, batch);
  stats.push(...res);
}

// 异常检测：墨迹数相对邻居骤降，或球心/尺寸相对邻居跳变
const flags = [];
for (let i = 2; i < stats.length - 2; i++) {
  const s = stats[i];
  const nb = [stats[i - 2], stats[i - 1], stats[i + 1], stats[i + 2]];
  const med = (key) => {
    const v = nb.map((x) => x[key]).filter((x) => typeof x === "number").sort((a, b) => a - b);
    return v.length ? v[Math.floor(v.length / 2)] : null;
  };
  const nMed = med("n");
  const wMed = med("w");
  if (nMed === null || nMed < 40) continue;
  const nDrop = s.n / nMed;
  const jump = s.cx !== null && stats[i - 1].cx !== null ? Math.hypot(s.cx - stats[i - 1].cx, s.cy - stats[i - 1].cy) : 0;
  if (nDrop < 0.55 || jump > Math.max(18, 0.45 * (wMed ?? 100))) {
    flags.push({ i, ...s, nMed, ratio: Math.round(nDrop * 100) / 100, jump: Math.round(jump) });
  }
}

console.log("\n[逐帧] i  ts(s)  墨迹数  球心(x,y)  包围盒(w×h)");
for (let i = 0; i < stats.length; i++) {
  const s = stats[i];
  const mark = flags.some((f) => f.i === i) ? "  <== 异常" : "";
  console.log(
    `${String(i).padStart(3)}  ${frames[i].ts.toFixed(3)}  ${String(s.n).padStart(6)}  (${s.cx},${s.cy})  ${s.w}×${s.h}${mark}`,
  );
}
console.log(`\n[异常帧] ${flags.length} 个`);
for (const f of flags) console.log(`  #${f.i} 墨迹 ${f.n}（邻居中位 ${f.nMed}，比值 ${f.ratio}）跳变 ${f.jump}px 包围盒 ${f.w}×${f.h}`);

// 存下**最严重**的异常帧与前后邻居（按墨迹骤降幅度排），方便肉眼看
const worst = flags.slice().sort((a, b) => a.ratio - b.ratio).slice(0, 4);
for (const f of worst) {
  for (const j of [f.i - 1, f.i, f.i + 1]) {
    if (frames[j]) writeFileSync(`${OUT}\\${TAG}-frame-${String(j).padStart(3, "0")}.png`, Buffer.from(frames[j].data, "base64"));
  }
}
writeFileSync(`${OUT}\\${TAG}-report.json`, JSON.stringify({ slow: SLOW, frames: stats, flags, state }, null, 2));
console.log(`[写出] ${OUT}\\${TAG}-report.json`);
await browser.close();
