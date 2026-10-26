/**
 * QIconButton（2026-10-26 统一改造）：图标按钮只有统一才能保证三件事：
 * 可访问名称、悬停 500ms 用途说明、触控 44×44 命中区。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import QIconButton from "../QIconButton.vue";
import QTipLayer from "../QTipLayer.vue";
import { hideTip, TIP_SHOW_DELAY_MS } from "../../../composables/useQioTip";

const componentPath = "../QIconButton.vue";
const source = readFileSync(new URL(componentPath, import.meta.url), "utf8");

let layer: ReturnType<typeof mount> | null = null;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  hideTip();
  layer?.unmount();
  layer = null;
  vi.useRealTimers();
  document.body.innerHTML = "";
});

function mountWithTip(props: { icon: string; label: string; description?: string }) {
  layer = mount(QTipLayer, { attachTo: document.body });
  return mount(QIconButton, { props, attachTo: document.body });
}

async function flush(): Promise<void> {
  await nextTick();
  await nextTick();
}

describe("QIconButton 语义与可访问名称", () => {
  it("渲染语义化按钮：type=button、aria-label=label、无原生 title", () => {
    const w = mount(QIconButton, { props: { icon: "close", label: "关闭" } });
    const btn = w.find("button");
    expect(btn.attributes("type")).toBe("button");
    expect(btn.attributes("aria-label")).toBe("关闭");
    expect(btn.attributes("title")).toBeUndefined();
    // 图标本身是装饰性的，不参与可访问名称
    expect(btn.find("svg").attributes("aria-hidden")).toBe("true");
    w.unmount();
  });

  it("disabled：原生 disabled 且点击不发事件", async () => {
    const w = mount(QIconButton, { props: { icon: "close", label: "关闭", disabled: true } });
    const btn = w.find("button");
    expect(btn.attributes("disabled")).toBeDefined();
    await btn.trigger("click");
    expect(w.emitted("click")).toBeUndefined();
    w.unmount();
  });

  it("busy：aria-busy + 挡住点击（防重复提交）", async () => {
    const w = mount(QIconButton, { props: { icon: "refresh", label: "刷新", busy: true } });
    const btn = w.find("button");
    expect(btn.attributes("aria-busy")).toBe("true");
    expect(btn.attributes("disabled")).toBeDefined();
    expect(btn.classes()).toContain("is-busy");
    await btn.trigger("click");
    expect(w.emitted("click")).toBeUndefined();
    w.unmount();
  });

  it("expanded 只在传入时渲染 aria-expanded", () => {
    const collapsed = mount(QIconButton, { props: { icon: "expand", label: "展开" } });
    expect(collapsed.find("button").attributes("aria-expanded")).toBeUndefined();
    collapsed.unmount();
    const expanded = mount(QIconButton, { props: { icon: "collapse", label: "收起", expanded: true } });
    expect(expanded.find("button").attributes("aria-expanded")).toBe("true");
    expanded.unmount();
  });
});

describe("QIconButton 用途说明（500ms）", () => {
  it("悬停 500ms 出现 role=tooltip 的用途说明（优先 description）", async () => {
    const w = mountWithTip({ icon: "copy", label: "复制", description: "复制这段内容到剪贴板" });
    const btn = w.find("button").element;
    btn.dispatchEvent(new Event("pointerenter"));
    await flush();
    expect(document.querySelector('[role="tooltip"]')).toBeNull();

    vi.advanceTimersByTime(TIP_SHOW_DELAY_MS - 1);
    await flush();
    expect(document.querySelector('[role="tooltip"]')).toBeNull();

    vi.advanceTimersByTime(1);
    await flush();
    const tip = document.querySelector('[role="tooltip"]');
    expect(tip?.textContent?.trim()).toBe("复制这段内容到剪贴板");

    // 移出立即消失
    btn.dispatchEvent(new Event("pointerleave"));
    await flush();
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
    w.unmount();
  });

  it("未传 description 时用 label 作为用途说明", async () => {
    const w = mountWithTip({ icon: "retry", label: "重试" });
    const btn = w.find("button").element;
    btn.dispatchEvent(new Event("pointerenter"));
    vi.advanceTimersByTime(TIP_SHOW_DELAY_MS);
    await flush();
    expect(document.querySelector('[role="tooltip"]')?.textContent?.trim()).toBe("重试");
    w.unmount();
  });

  it("键盘聚焦立即出现说明（不等 500ms）", async () => {
    const w = mountWithTip({ icon: "settings", label: "设置" });
    const btn = w.find("button").element as HTMLButtonElement;
    btn.focus();
    btn.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    await flush();
    expect(document.querySelector('[role="tooltip"]')?.textContent?.trim()).toBe("设置");
    hideTip();
    w.unmount();
  });

  it("Esc 与滚动会让说明立即消失（不留下过期浮层）", async () => {
    const w = mountWithTip({ icon: "locate", label: "定位任务" });
    const btn = w.find("button").element;
    btn.dispatchEvent(new Event("pointerenter"));
    vi.advanceTimersByTime(TIP_SHOW_DELAY_MS);
    await flush();
    expect(document.querySelector('[role="tooltip"]')).not.toBeNull();

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flush();
    expect(document.querySelector('[role="tooltip"]')).toBeNull();

    btn.dispatchEvent(new Event("pointerenter"));
    vi.advanceTimersByTime(TIP_SHOW_DELAY_MS);
    await flush();
    expect(document.querySelector('[role="tooltip"]')).not.toBeNull();
    window.dispatchEvent(new Event("scroll"));
    await flush();
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
    w.unmount();
  });

  it("按钮卸载时清掉自己的说明（不留悬空浮层）", async () => {
    const w = mountWithTip({ icon: "more", label: "更多操作" });
    const btn = w.find("button").element;
    btn.dispatchEvent(new Event("pointerenter"));
    vi.advanceTimersByTime(TIP_SHOW_DELAY_MS);
    await flush();
    expect(document.querySelector('[role="tooltip"]')).not.toBeNull();
    w.unmount();
    await flush();
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
  });
});

describe("QIconButton 尺寸与圆角", () => {
  it("容器 14px 圆角来自唯一 token（不在组件里写近似值）", () => {
    expect(source).toContain("border-radius: var(--qio-ui-radius)");
    expect(source).not.toMatch(/border-radius:\s*\d+px/);
  });

  it("触控 coarse 下有效命中区 44×44，相邻按钮不用负 inset 重叠", () => {
    expect(source).toMatch(/@media \(pointer: coarse\)/);
    expect(source).toMatch(/height: 44px/);
    expect(source).toMatch(/width: 44px/);
    expect(source).not.toContain("inset:");
  });
});
