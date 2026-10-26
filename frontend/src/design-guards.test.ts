/**
 * 设计语言守卫（第四阶段）。
 *
 * 这些扫描测试守的是「会慢慢退回去的东西」：一旦有人再写原生确认框、
 * 或者在组件里写裸时长/裸位移，第四阶段的统一语言就开始漏水。
 * 它们读源码文本，跑得很快，也不需要浏览器。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

// 用 cwd 而不是 import.meta.url：Vite 会把字面量形式的 URL 改写成资源路径
const SRC = resolve(process.cwd(), "src");

/** 递归收集源码文件（跳过测试自身） */
function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      sourceFiles(full, out);
    } else if (/\.(vue|ts)$/.test(name) && !/\.test\.ts$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

const FILES = sourceFiles(SRC).map((f) => ({
  // 统一成 posix 风格路径，断言不依赖运行平台
  path: relative(SRC, f).split("\\").join("/"),
  text: readFileSync(f, "utf8"),
}));

describe("设计语言守卫", () => {
  it("没有任何浏览器原生确认框（confirm / alert / prompt）", () => {
    const hits = FILES.filter((f) => /window\.(confirm|alert|prompt)\s*\(/.test(f.text)).map((f) => f.path);
    expect(hits).toEqual([]);
  });

  it("原生确认框只能由 QConfirm 这一处实现", () => {
    // QConfirm 自己也不允许用原生框（它是替代品）；这里是一个反向确认：
    // 只要有人在别处重新引入原生框，上一条测试就会失败并指出文件。
    const qconfirm = FILES.find((f) => f.path.endsWith("ui/QConfirm.vue"));
    expect(qconfirm).toBeTruthy();
    expect(qconfirm!.text).toContain("qio-confirm");
  });

  it("星球连续体只有一处「入口几何」读取（否则两个组件会各猜一份）", () => {
    const readers = FILES.filter((f) => f.text.includes("data-planet-entry")).map((f) => f.path);
    // 允许：定义处（planetContinuum.ts）、入口元素本身（PlanetDock.vue）；
    // 任何第三处都意味着有人在别处偷偷量入口位置 —— 那正是「两个对象」的开端。
    expect(readers.sort()).toEqual(
      [
        "components/PlanetDock.vue",
        "composables/planetContinuum.ts",
      ].sort(),
    );
  });
});

/**
 * 输入区必须与消息列是同一条列（2026-09-22 回归修复）。
 *
 * 现场（安装包默认窗口 1200×800）：输入区用 `left: calc(50% - 66px)` + 860px 宽手算位置，
 * 结果比消息列右移 418px、右边缘溢出视口 194px（发送按钮跑到屏幕外），并且盖住了
 * 停在右下角的星球入口球 —— 球点不到也拖不动，用户看到的是「UI 错位 + 星球移不动」。
 *
 * 现在两侧留白由同一对令牌给出：任何人只改一边（组件里手算偏移）都会在这里被拦下。
 */
describe("输入区与消息列同一条列", () => {
  const composer = FILES.find((f) => f.path.endsWith("components/Composer.vue"));
  const stream = FILES.find((f) => f.path.endsWith("components/MessageStream.vue"));
  // tokens.css 不是 .vue/.ts，FILES 扫不到它 —— 直接读
  const tokens = readFileSync(resolve(SRC, "styles", "tokens.css"), "utf8");

  it("输入区在对话列内正常布局：不固定悬浮、不手算视口偏移（v2）", () => {
    expect(composer).toBeTruthy();
    // v2：右侧预览栏与中央列并排，输入区必须留在对话列内部（position:relative + 容器宽度），
    // 固定悬浮会让它盖住右侧预览栏；手算 50% 偏移仍被禁止（历史错位的来源）。
    const rootBlock = composer!.text.match(/\.composer\s*\{[\s\S]*?\n\}/)?.[0] ?? "";
    expect(rootBlock).toMatch(/position:\s*relative/);
    // v3：外层是透明列（width:100% + max-width:860px），真正的卡片是内部胶囊 .input-pill
    expect(rootBlock).toMatch(/width:\s*100%/);
    expect(rootBlock).toMatch(/max-width:\s*860px/);
    const pillBlock = composer!.text.match(/\.input-pill\s*\{[\s\S]*?\n\}/)?.[0] ?? "";
    // 2026-10-26 统一改造：输入框属于本次涉及控件，圆角用唯一 token，而不是胶囊 999px
    expect(pillBlock).toMatch(/border-radius:\s*var\(--qio-ui-radius\)/);
    // 用户确认不要阴影：胶囊只用 1px 边框，不再有会被 overflow-x: clip 截断的投影
    expect(pillBlock).toMatch(/box-shadow:\s*none/);
    expect(rootBlock).not.toMatch(/position:\s*fixed/);
    expect(composer!.text).not.toMatch(/left:\s*calc\(50%/);
    expect(composer!.text).not.toMatch(/left:\s*max\(12px/);
  });

  it("消息流的内容盒用同一对令牌留白", () => {
    expect(stream).toBeTruthy();
    expect(stream!.text).toMatch(
      /padding:\s*34px\s+var\(--column-inset-right\)\s+20px\s+var\(--column-inset-left\)/,
    );
  });

  it("列留白只在 tokens.css 定义一次：宽屏 24px、≤1400px 右侧让出 132px 球通道", () => {
    expect(tokens).toMatch(/--column-inset-left:\s*24px/);
    expect(tokens).toMatch(/--column-inset-right:\s*24px/);
    expect(tokens).toMatch(
      /@media\s*\(max-width:\s*1400px\)\s*\{[\s\S]{0,240}?--column-inset-right:\s*132px/,
    );
  });
});

/**
 * 入口球贴靠时不能被固定控件压住（2026-09-22）。
 *
 * 两个现场都是「球停在那儿，但点不到也拖不动」：
 * - 输入气泡（`.composer`）不是浮动组件，z-index 12 + 不透明底，球贴到底边就藏在它下面；
 * - 设置齿轮（`.settings-float`）冷启动停在右上角时**不算已贴靠**，不进互斥表，
 *   窄窗口里底边被输入区占满后球改停右上角，正好压在它底下。
 * 障碍清单只有一份，入口球必须声明它 —— 少一个就会重现「星球没办法进行移动」。
 */
describe("浮动球的障碍清单", () => {
  const state = FILES.find((f) => f.path.endsWith("composables/floatingState.ts"));
  const dock = FILES.find((f) => f.path.endsWith("components/PlanetDock.vue"));

  it("清单含输入区与设置齿轮", () => {
    expect(state).toBeTruthy();
    const list = state!.text.match(/FLOAT_AVOID_SELECTORS[^=]*=\s*\[([^\]]*)\]/);
    expect(list).not.toBeNull();
    expect(list![1]).toContain('".composer"');
    expect(list![1]).toContain('".settings-float"');
  });

  it("入口球贴靠时声明这份清单", () => {
    expect(dock).toBeTruthy();
    expect(dock!.text).toContain("avoidSelectors: FLOAT_AVOID_SELECTORS");
  });
});

/**
 * 无边框窗口契约（2026-10-13）。
 *
 * 系统页眉被 `decorations: false` 去掉之后，可拖拽与三个窗口按钮全靠前端补。
 * 这里的四件事缺任何一件，用户看到的就是「窗口关不掉 / 拖不动 / 顶部控件点不到」——
 * 而且都不会在类型检查或任何单测里报错。所以按源码与配置文本守住：
 * 1. 壳配置确实是无边框；
 * 2. capabilities 里有拖拽 + 最小化 + 最大化 + 关闭的权限（少一个按钮就静默失灵）；
 * 3. 标题条高度只有一个来源（tokens.css），App 让出这一段高度；
 * 4. 拖拽区由 WindowChrome 承担。
 */
describe("无边框窗口契约", () => {
  const readJson = (rel: string) => JSON.parse(readFileSync(resolve(process.cwd(), rel), "utf8"));
  const tokens = readFileSync(resolve(SRC, "styles", "tokens.css"), "utf8");
  const app = FILES.find((f) => f.path === "App.vue");
  const chrome = FILES.find((f) => f.path === "components/WindowChrome.vue");

  it("tauri.conf.json：窗口无边框，且保留阴影（Win11 圆角与窗口边缘靠它）", () => {
    const conf = readJson("src-tauri/tauri.conf.json");
    const win = conf.app.windows[0];
    expect(win.decorations).toBe(false);
    expect(win.shadow).toBe(true);
  });

  it("capabilities：拖拽与三个窗口按钮的权限一个都不能少", () => {
    const cap = readJson("src-tauri/capabilities/default.json");
    const perms: string[] = cap.permissions;
    for (const p of [
      "core:window:allow-start-dragging",
      "core:window:allow-minimize",
      "core:window:allow-toggle-maximize",
      "core:window:allow-close",
      "core:window:allow-is-maximized",
    ]) {
      expect(perms).toContain(p);
    }
  });

  it("标题条高度只在 tokens.css 定义一次，App 让出这一段", () => {
    expect(tokens).toMatch(/--titlebar-height:\s*32px/);
    expect(app).toBeTruthy();
    expect(app!.text).toContain("<WindowChrome />");
    expect(app!.text).toMatch(/padding-top:\s*var\(--titlebar-height\)/);
    // 顶部锚定的组件读同一个令牌下移，不许自己写死像素
    for (const path of ["views/SettingsView.vue", "views/PlanetView.vue"]) {
      const file = FILES.find((f) => f.path === path);
      expect(file, path).toBeTruthy();
      expect(file!.text, path).toContain("var(--titlebar-height)");
    }
    // 顶部提示条（待确认审批 / 没执行的操作 / 开发任务）统一挂在 App 的 .top-notes 里：
    // 默认位置必须让开标题条 —— 有人在这条上写死 12px，它就会重新被压住
    const notes = app!.text.match(/\.top-notes\s*\{[^}]*\}/);
    expect(notes, ".top-notes 样式块").toBeTruthy();
    expect(notes![0]).toContain("var(--titlebar-height)");
  });

  it("拖拽面在 WindowChrome 里，且按钮不在拖拽面内部", () => {
    expect(chrome).toBeTruthy();
    expect(chrome!.text).toContain("data-tauri-drag-region");
    // 按钮与拖拽面是兄弟：按钮长在拖拽面里，点击会被拖拽逻辑吃掉
    expect(chrome!.text).toMatch(/<div class="drag-surface" data-tauri-drag-region[\s\S]*?<\/div>\s*<div v-if="inShell" class="window-controls">/);
  });

  it("按钮容器不许铺满整条：否则整个拖拽面都被它盖住，窗口拖不动", () => {
    expect(chrome).toBeTruthy();
    const controls = chrome!.text.match(/\.window-controls\s*\{[^}]*\}/);
    expect(controls, ".window-controls 样式块").toBeTruthy();
    expect(controls![0]).toMatch(/position:\s*absolute/);
    expect(controls![0]).toMatch(/right:\s*0/);
    expect(controls![0]).toMatch(/pointer-events:\s*none/);
    // 按钮自己重新打开命中
    const btn = chrome!.text.match(/\.wc-btn\s*\{[^}]*\}/);
    expect(btn, ".wc-btn 样式块").toBeTruthy();
    expect(btn![0]).toMatch(/pointer-events:\s*auto/);
  });
});
