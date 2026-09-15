// 第二阶段星球可视验收（在真实运行的应用上操作，不用单元测试替代）。
//
// 用法（服务需在跑：后端 8734、前端 5199）：
//   node scripts/baseline/planet-phase2-probe.mjs
// 前置：先写入大批话题
//   backend\.venv\Scripts\python.exe scripts\baseline\seed_phase2_topics.py --topics 100
// 输出：%TEMP%\qio-baseline\phase2\report.json 与 shots\*.png
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199";
const OUT = `${process.env.TEMP}\\qio-baseline\\phase2`;
const SHOTS = `${OUT}\\shots`;
mkdirSync(SHOTS, { recursive: true });

const report = { startedAt: new Date().toISOString(), cases: [], notes: [] };
const rec = (id, title, passed, actual) => {
  report.cases.push({ id, title, passed: !!passed, actual: String(actual) });
  console.log(`[${passed ? "PASS" : "FAIL"}] ${id} ${title} :: ${actual}`);
};
const note = (t) => {
  report.notes.push(t);
  console.log(`[NOTE] ${t}`);
};

let seq = Math.floor(Date.now() / 1000) % 100000;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const shot = async (page, name) => {
  const p = `${SHOTS}\\${name}.png`;
  await page.screenshot({ path: p });
  return p;
};

const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
const consoleErrors = [];
page.on("console", (m) => {
  if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200));
});
page.on("pageerror", (e) => consoleErrors.push("pageerror: " + String(e.message).slice(0, 200)));

const windowInfo = () => page.evaluate(() => window.__qioPlanetWindow?.() ?? null);

/** 当前「正面可见」的 topic_id：用窗口方向与相机方向的内积判断 */
const frontIds = (info) => {
  const cam = info?.debug?.cameraDir ?? [0, 0, 1];
  const out = [];
  (info?.debug?.dirs ?? []).forEach((dir, i) => {
    if (!dir) return;
    if (dir[0] * cam[0] + dir[1] * cam[1] + dir[2] * cam[2] > 0) out.push(info.ids[i]);
  });
  return out;
};

const drag = async (dx, dy, steps = 12) => {
  const box = await page.locator("canvas").boundingBox();
  if (!box) return;
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  for (let i = 1; i <= steps; i++) {
    await page.mouse.move(cx + (dx * i) / steps, cy + (dy * i) / steps);
    await wait(16);
  }
  await page.mouse.up();
  await wait(260); // 让惯性走一段，展示变化由它继续推动
};

try {
  await page.goto(`${BASE}/?fresh=${++seq}#/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".dock", { timeout: 20000 });
  await page.waitForTimeout(900);

  const t0 = Date.now();
  await page.locator(".dock").click();
  await page.waitForSelector(".planet-view", { timeout: 20000 });
  await page.waitForFunction(() => Boolean(window.__qioPlanetWindow?.()?.visible), null, { timeout: 20000 });
  const openMs = Date.now() - t0;
  await page.waitForTimeout(1200);
  const initial = await windowInfo();
  const initialShot = await shot(page, "planet-initial");
  rec(
    "P2-INITIAL",
    "打开星球：同屏话题数有上限，画面保持留白",
    initial && initial.visible <= initial.capacity && initial.visible >= 6,
    `可见 ${initial?.visible}/${initial?.capacity}，打开耗时 ${openMs}ms，截图 ${initialShot}`,
  );

  // 打开时是「球面随机铺开」，不是赤道环带
  {
    const ys = (initial?.debug?.localDirs ?? []).filter(Boolean).map((d) => Math.abs(d[1]));
    rec(
      "P2-SPREAD",
      "打开星球时的话题在整个球面上铺开（不是一条环带）",
      ys.length > 0 && Math.max(...ys) > 0.6 && Math.min(...ys) < 0.3,
      `|y| 范围 ${Math.min(...ys).toFixed(2)} ~ ${Math.max(...ys).toFixed(2)}（环带布局不会超过 0.6）`,
    );
  }

  // 相机纵向限位：用力往纵向拖，极角也不会贴到极点
  {
    await drag(0, 700);
    const polars = await Promise.all(
      [0, 1, 2].map(async () => {
        const info = await windowInfo();
        const cam = info.debug.cameraDir;
        return Math.acos(Math.min(1, Math.max(-1, cam[1])));
      }),
    );
    const polar = polars[0];
    const limit = (35 * Math.PI) / 180;
    rec(
      "P2-POLAR",
      "纵向拖动有极角限位（不会拖到极点再看不清方向）",
      polar >= limit - 0.02 && polar <= Math.PI - limit + 0.02,
      `用力纵向拖动后相机极角 ${((polar * 180) / Math.PI).toFixed(1)}°（限位 35°~145°）`,
    );
  }

  // 转动一小段：正面看到的话题必须发生变化（不是一直同一批）
  const frontBefore = frontIds(await windowInfo());
  await drag(700, 0);
  const frontAfterInfo = await windowInfo();
  const frontAfter = frontIds(frontAfterInfo);
  const enteredFront = frontAfter.filter((id) => !frontBefore.includes(id)).length;
  rec(
    "P2-FRONT",
    "转动一段之后，正面看到的话题确实换了（不是一直同一批）",
    enteredFront > 0,
    `正面 ${frontBefore.length} → ${frontAfter.length}，其中新出现 ${enteredFront} 个`,
  );

  // 持续旋转：每转一小段看一次窗口内容，累计采样（只用水平拖动，
  // 否则相机会被推到极点，横向旋转在那里退化，测不出真实行为）
  const seen = new Set(initial?.ids?.filter(Boolean) ?? []);
  let maxVisible = initial?.visible ?? 0;
  const samples = [];
  for (let round = 0; round < 14; round++) {
    await drag(260, 0);
    const info = await windowInfo();
    if (!info) break;
    info.ids.filter(Boolean).forEach((id) => seen.add(id));
    maxVisible = Math.max(maxVisible, info.visible);
    samples.push(info.ids.filter(Boolean).length);
    if (round === 6) await shot(page, "planet-mid");
  }
  const afterShot = await shot(page, "planet-after");

  rec(
    "P2-FLOW",
    "持续旋转会不断遇到新话题，而不是一直转同一批点",
    seen.size > (initial?.capacity ?? 12),
    `累计遇到 ${seen.size} 个不同话题（初始窗口 ${initial?.visible}），截图 ${afterShot}`,
  );
  rec(
    "P2-CAP",
    "无论转多久，同屏可见数量不超过容量",
    maxVisible <= (initial?.capacity ?? 12),
    `采样期间最大可见 ${maxVisible}（容量 ${initial?.capacity}），每轮可见 ${samples.join("/")}`,
  );

  // 拖动停止后位置必须完全不动（稳定性只要求「不拖动时不动」）
  // 注意：松手后的惯性尾巴仍然算「用户驱动的转动」，先等它收敛再采样
  let stable = "";
  for (let i = 0; i < 24; i++) {
    await wait(250);
    const cur = JSON.stringify((await windowInfo())?.debug?.localDirs);
    if (cur === stable) break;
    stable = cur;
  }
  const idleBefore = await windowInfo();
  await wait(2000);
  const idleAfter = await windowInfo();
  rec(
    "P2-IDLE",
    "停止拖动后，窗口里的话题位置不再变化（不漂移）",
    JSON.stringify(idleBefore?.debug?.localDirs) === JSON.stringify(idleAfter?.debug?.localDirs),
    `静置 2s 前后话题位置是否一致 = ${JSON.stringify(idleBefore?.debug?.localDirs) === JSON.stringify(idleAfter?.debug?.localDirs)}（星球自身的慢速自转不计入）`,
  );

  // 新话题落在背面（用户在它转出来之前看不到这次替换）
  const afterDrag = await windowInfo();
  const cam = afterDrag?.debug?.cameraDir ?? [0, 0, 1];
  const behind = (afterDrag?.debug?.dirs ?? []).filter(
    (dir) => dir && dir[0] * cam[0] + dir[1] * cam[1] + dir[2] * cam[2] < 0,
  ).length;
  rec(
    "P2-BACK",
    "窗口里始终有一部分话题在球体背面（新话题从这里转到正面）",
    behind > 0 && behind < (afterDrag?.debug?.dirs ?? []).length,
    `窗口 ${afterDrag?.debug?.dirs?.filter(Boolean).length} 个点，其中背面 ${behind} 个`,
  );

  // 反向旋转：应该能拿回刚看到过的话题（连续感），而不是全新一批
  const beforeBack = await windowInfo();
  await drag(-260, -40);
  const afterBack = await windowInfo();
  const beforeIds = beforeBack?.ids?.filter(Boolean) ?? [];
  const afterIds = afterBack?.ids?.filter(Boolean) ?? [];
  const overlap = afterIds.filter((id) => beforeIds.includes(id)).length;
  rec(
    "P2-REVERSE",
    "短距离反向浏览仍有连续性（不是重新随机一批）",
    overlap > 0,
    `反向前后共同话题 ${overlap} 个`,
  );

  // 打开侧栏：列表仍然展示全部话题（准确导航的职责没有丢）
  await page.locator(".panel-toggle").click();
  await page.waitForTimeout(600);
  const listCount = await page.locator(".topic-list li").count();
  const listShot = await shot(page, "planet-panel");
  rec(
    "P2-LIST",
    "列表仍能看到全部话题（Planet 浏览 / List 精确导航并存）",
    listCount > 12,
    `侧栏话题 ${listCount} 个，截图 ${listShot}`,
  );

  const errors = consoleErrors.filter((t) => !t.includes("favicon"));
  rec("P2-CONSOLE", "整个过程没有控制台错误", errors.length === 0, errors.slice(0, 3).join(" | ") || "无");
  note(`中间截图：${SHOTS}\\planet-mid.png`);
} catch (e) {
  rec("P2-RUN", "可视验收脚本执行", false, String(e?.message ?? e).slice(0, 300));
}

report.endedAt = new Date().toISOString();
writeFileSync(`${OUT}\\report.json`, JSON.stringify(report, null, 2), "utf8");
console.log(`\n报告：${OUT}\\report.json`);
await browser.close();
