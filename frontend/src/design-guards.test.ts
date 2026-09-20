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
