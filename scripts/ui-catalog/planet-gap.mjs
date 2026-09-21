/**
 * 补齐 `planet-root` 里缺的状态：片段历史选中 / 查看原文 / 从这里继续 /
 * 知识面板四态 / 实体面板三态 / 管理模式。
 *
 * 这些状态在最初那一次跑批里没写出来（那批在浅色那一步被清理掉，日志也没留）。
 * 这里重采，并且：
 * - 打开星球用 DOM 事件（软件渲染时按钮偶尔不满足「稳定可点」，用真实用户点击会卡住）；
 * - 每一步都先断言目标元素出现或面板宽度正常，再截图。
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

const INSTANCE = { base: ENV_BASE, api: ENV_API };
const GROUP = "planet-root";
const ENTRY = "[data-planet-entry]";
const entries = readManifest(GROUP)?.entries ?? [];
const log = [];

function put(entry) {
  const index = entries.findIndex((e) => e.id === entry.id);
  if (index >= 0) entries[index] = entry;
  else entries.push(entry);
}

const domClick = (s, selector, index = 0) =>
  s.page.evaluate(
    ({ sel, i }) => {
      const list = document.querySelectorAll(sel);
      const el = list[i];
      if (el) el.click();
      return list.length;
    },
    { sel: selector, i: index },
  );

async function openPlanet(s) {
  await s.page.evaluate(() => document.querySelector("[data-planet-entry]")?.click());
  await sleep(3400);
  // 面板要展开：宽度不够就点开
  let width = (await s.page.locator(".panel").first().boundingBox())?.width ?? 0;
  if (width < 300) {
    await domClick(s, ".panel-toggle");
    await sleep(1400);
    width = (await s.page.locator(".panel").first().boundingBox())?.width ?? 0;
  }
  log.push(`面板宽度 ${Math.round(width)}px`);
}

const browser = await launchBrowser();

// ---------------------------------------------------------------- 话题详情：片段 / 原文 / 从这里继续
{
  const s = await createSession(browser, { group: GROUP, name: "补采", theme: "dark", ...INSTANCE });
  s.page.setDefaultTimeout(20000);
  await s.goto("#/", { waitFor: ".conversation", settle: 1600 });
  await openPlanet(s);

  await domClick(s, ".topic-list li");
  await sleep(1500);
  put(await s.shotEl(".panel", "planet-15-topic-selected", "选中话题：详情 + 片段历史", { pad: 10 }));

  await domClick(s, ".fragment-item", 0);
  await sleep(800);
  put(
    await s.shotEl(".detail-scroll", "planet-16-fragment-selected", "片段历史：选中一段（底部操作区跟着变）", {
      pad: 8,
    }),
  );

  const rawCount = await domClick(s, ".raw-toggle", 0);
  log.push(`查看原文按钮数 ${rawCount}`);
  await sleep(1600);
  put(await s.shotEl(".fragment-item >> nth=0", "planet-17-raw", "查看原文（只读历史，可继续读取）", { pad: 8 }));

  await domClick(s, ".start-btn");
  await sleep(2200);
  put(
    await s.shot("planet-18-start-here", "从这里继续：写入起点后的结果", {
      note: "成功则回到对话页并带上起点；失败则面板保留并显示原因",
    }),
  );
  await s.close({ save: false });
  saveManifest(GROUP, entries);
}

// ---------------------------------------------------------------- 知识面板 / 实体面板 / 管理模式
{
  const s = await createSession(browser, { group: GROUP, name: "补采", theme: "dark", ...INSTANCE });
  s.page.setDefaultTimeout(20000);
  await s.goto("#/", { waitFor: ".conversation", settle: 1600 });
  await openPlanet(s);

  await domClick(s, ".tabs .tab-knowledge");
  await sleep(2200);
  put(await s.shotEl(".panel", "planet-20-knowledge", "知识面板：各状态同屏", { pad: 10 }));

  await domClick(s, ".k-edit-open", 0);
  await sleep(900);
  put(await s.shotEl(".panel", "planet-21-knowledge-edit", "知识面板：内联修正", { pad: 10 }));

  await domClick(s, ".k-archive", 1);
  await sleep(1000);
  put(await s.shotEl(".panel", "planet-22-knowledge-archive", "知识面板：归档确认", { pad: 10 }));

  await domClick(s, ".k-create-open");
  await sleep(1000);
  put(await s.shotEl(".panel", "planet-23-knowledge-create", "知识面板：新建表单", { pad: 10 }));

  await domClick(s, ".tabs .tab-entity");
  await sleep(2200);
  put(await s.shotEl(".panel", "planet-30-entities", "实体面板：实体卡列表", { pad: 10 }));

  await domClick(s, ".e-item", 0);
  await sleep(1200);
  put(await s.shotEl(".panel", "planet-31-entity-detail", "实体详情：摘要 / 别名 / 属性 / 关系", { pad: 10 }));

  await domClick(s, ".e-revoke");
  await sleep(1000);
  put(await s.shotEl(".panel", "planet-32-entity-archive", "实体：归档确认", { pad: 10 }));

  await domClick(s, ".tabs .tab-topic");
  await sleep(1200);
  await domClick(s, ".mode-btn");
  await sleep(1200);
  put(await s.shot("planet-33-manage-mode", "管理模式"));

  await s.close({ save: false });
  saveManifest(GROUP, entries);
}

await browser.close();
console.log("补采日志：");
for (const line of log) console.log(`  - ${line}`);
process.exit(0);
