/**
 * FU5 布局守卫 1：空对话引导必须在消息可视区水平 / 垂直居中。
 *
 * 现场：无消息时「今天想聊点什么？」贴在右侧（1440 下 empty.x≈1181，stream.x=68）。
 * 根因是空状态仍作为滚动内容流里的普通子节点，被前面的 flex/宽度链推到右边。
 *
 * 约束（新结构）：
 * - .stream 作为定位祖先（position: relative），空状态绝对铺满整个可视区并居中；
 * - 空状态脱离内容流：不再参与 spacer / 虚拟列表 / typing / ContinueBar 的横向分配，
 *   因此有消息时的虚拟列表与滚动行为完全不受影响；
 * - 三种真实状态文案（加载中 / 读取失败 / 真空）保持不变。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import MessageStream from "../MessageStream.vue";
import { useSessionStore } from "../../stores/session";

const SRC = resolve(process.cwd(), "src");
const source = readFileSync(resolve(SRC, "components/MessageStream.vue"), "utf8");

/** 取某个 scoped 样式块（selector { ... } 到第一个顶层换行 }）。 */
function block(selector: string): string {
  const start = source.indexOf(selector + " {");
  if (start < 0) return "";
  const end = source.indexOf("\n}", start);
  return end < 0 ? "" : source.slice(start, end + 2);
}

describe("空状态布局：绝对铺满并居中（不再被内容流推到右边）", () => {
  it(".stream 是定位祖先；.empty 绝对铺满 + 水平/垂直居中", () => {
    const stream = block(".stream");
    expect(stream, "找不到 .stream 样式块").toContain("position: relative");
    // 滚动视口是块容器：不参与 flex 行分配（否则 spacer width:100% 仍会把空状态推右）
    expect(stream).toContain("display: block");

    const empty = block(".empty");
    expect(empty, "找不到 .empty 样式块").toContain("position: absolute");
    expect(empty).toMatch(/inset:\s*0/);
    expect(empty).toContain("justify-content: center");
    expect(empty).toContain("align-items: center");
    // 空状态只是文字引导：不该拦截滚动/其它控件
    expect(empty).toContain("pointer-events: none");
  });

  it("空状态脱离内容流：不是虚拟列表容器的子节点", async () => {
    setActivePinia(createPinia());
    useSessionStore().history = { status: "ready", error: null };
    const w = mount(MessageStream, { global: { plugins: [createPinia()] } });
    await nextTick();

    const empty = w.find('[data-test="stream-empty"]');
    expect(empty.exists()).toBe(true);
    expect(w.find('.spacer [data-test="stream-empty"]').exists()).toBe(false);
    expect(empty.element.parentElement?.className).toContain("stream");
    expect(empty.text()).toContain("今天想聊点什么");
    w.unmount();
  });

  it("三种真实状态文案保持不变（加载 / 失败 / 真空）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore(pinia);

    session.history = { status: "loading", error: null };
    let w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    expect(w.get('[data-test="stream-empty"]').text()).toContain("正在读取历史");
    w.unmount();

    session.history = { status: "error", error: "timeout" };
    w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    expect(w.get('[data-test="stream-empty"]').text()).toContain("历史没有读取出来");
    w.unmount();
  });
});
