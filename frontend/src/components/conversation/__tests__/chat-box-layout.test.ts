/**
 * 消息框改版（2026-10-27）的结构守卫。
 *
 * 用户要求：对话页里「装消息的框」要把 输入框 + 记忆星球入口 + 输入框上方的各类按钮
 * 一起包进去 —— 这样审批等按钮出现时只在框内向上生长，输入胶囊不会被推动，
 * 模式切换与星球入口也始终和输入胶囊同一条中心线。
 *
 * jsdom 没有布局引擎，量不到像素，所以这里守**结构与唯一的尺寸来源**；
 * 真实几何（有/无入口行时输入胶囊中心不动、三者中心一致）由浏览器探针核对。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SRC = resolve(process.cwd(), "src");
const read = (rel: string) => readFileSync(resolve(SRC, rel), "utf8");

/** 取某个选择器的规则块（简单 CSS，够用；不依赖正则转义） */
function block(css: string, selector: string): string {
  const at = css.indexOf(selector + " {");
  if (at < 0) return "";
  const close = css.indexOf("\n}", at);
  return close < 0 ? "" : css.slice(at, close + 2);
}

describe("消息框：消息流 + 底部输入块同一个框", () => {
  const chat = read("components/conversation/ChatColumn.vue");
  const dock = read("components/conversation/ComposerDock.vue");
  const composer = read("components/Composer.vue");
  const planetView = read("views/PlanetView.vue");
  const entryLayout = read("planet/entryLayout.ts");

  it("输入区与消息流被同一个 .chat-box 包住", () => {
    const boxAt = chat.indexOf('class="chat-box"');
    const streamAt = chat.indexOf("<MessageStream");
    const dockAt = chat.indexOf("<ComposerDock");
    expect(boxAt, "chat-box 容器").toBeGreaterThan(-1);
    expect(streamAt).toBeGreaterThan(boxAt);
    expect(dockAt).toBeGreaterThan(streamAt);
    // 不再有「底部让位占位块」那套补偿
    expect(chat.includes("bottom-cluster")).toBe(false);
  });

  it("边框/圆角/底色只在 .chat-box 上，消息流自己不再画表面", () => {
    const box = block(chat, ".chat-box");
    expect(box).toMatch(/border:\s*1px solid var\(--border-subtle\)/);
    expect(box).toMatch(/border-radius:\s*var\(--qio-ui-radius\)/);
    expect(box).toMatch(/background:\s*var\(--bg-base\)/);
    expect(box).toMatch(/overflow:\s*clip/);
    const stage = block(chat, ".message-stage");
    expect(stage).toMatch(/border:\s*none/);
    expect(stage).toMatch(/border-radius:\s*0/);
    expect(stage).toMatch(/background:\s*transparent/);
  });

  it("入口按钮行在输入行之外（出现时向上长，不推动输入胶囊）", () => {
    const entriesAt = dock.indexOf('class="composer-entries"');
    const lineAt = dock.indexOf('class="composer-line"');
    expect(entriesAt, "composer-entries").toBeGreaterThan(-1);
    expect(lineAt, "composer-line").toBeGreaterThan(-1);
    // 入口行必须排在 .composer-line 之前，且不在 .composer-stack 里面
    expect(entriesAt).toBeLessThan(lineAt);
    const stackAt = dock.indexOf('class="composer-stack"');
    expect(stackAt).toBeGreaterThan(lineAt);
    expect(dock.indexOf("</template>", stackAt)).toBeGreaterThan(0);
    // 模式/胶囊/星球是同一个对齐单元
    expect(block(dock, ".composer-line")).toMatch(/align-items:\s*center/);
    expect(block(dock, ".composer-planet-cell")).toMatch(/align-items:\s*center/);
  });

  it("星球入口直径 = 输入胶囊单行高度（同一个令牌）", () => {
    expect(composer).toMatch(/--composer-pill-h:\s*54px/);
    expect(block(composer, ".input-pill")).toMatch(/min-height:\s*var\(--composer-pill-h/);
    expect(dock).toMatch(/--planet-size:\s*var\(--composer-pill-h/);
    expect(block(dock, ".composer-line")).toMatch(/var\(--planet-size/);
  });

  it("入口小球的画布尺寸跟着球的直径走（54 / 0.887 ≈ 61，scale ≈ 1）", () => {
    expect(planetView).toMatch(/--ball-size:\s*61px/);
    expect(entryLayout).toMatch(/BALL_SIZE_FALLBACK\s*=\s*61\b/);
    const ball = 61 * 0.887; // 画布里球体占 88.7%
    expect(Math.abs(ball - 54)).toBeLessThanOrEqual(1);
  });

  it("消息流与输入区共用同一个左右留白令牌", () => {
    expect(chat).toMatch(/--chat-pad-inline:\s*12px/);
    const stream = read("components/MessageStream.vue");
    expect(stream).toMatch(/var\(--chat-pad-inline/);
    expect(dock).toMatch(/var\(--chat-pad-inline/);
  });
});
