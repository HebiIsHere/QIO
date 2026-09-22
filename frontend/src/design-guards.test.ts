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

  it("输入区两侧贴边取正文列令牌，不再手算 calc(50% ...)", () => {
    expect(composer).toBeTruthy();
    expect(composer!.text).toMatch(/left:\s*var\(--column-inset-left\)/);
    expect(composer!.text).toMatch(/right:\s*var\(--column-inset-right\)/);
    // 手算偏移会与列宽脱钩：它正是本次错位的来源
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
