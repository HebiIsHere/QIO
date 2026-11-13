import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import PlanetDock from "../PlanetDock.vue";
import { hideTip, tipState } from "../../composables/useQioTip";
import { planetContinuum } from "../../composables/planetContinuum";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
    sendTurn: vi.fn(async () => ({})),
  },
}));

async function mountDock(anchored = false) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(PlanetDock, {
    // 用途说明浮层只在锚点真实连在文档里时才显示（showTip 的 isConnected 守卫）
    attachTo: document.body,
    props: anchored ? { anchored: true } : {},
    global: { plugins: [pinia] },
  });
  await nextTick(); // 等浮动系统初始化（flush: post）
  return w;
}

beforeEach(() => {
  localStorage.clear();
  document.body.innerHTML = "";
  hideTip();
  planetContinuum.phase = "idle";
});

describe("PlanetDock 话题星球入口", () => {
  it("渲染悬浮球按钮（可访问标签）：浮动模式保留贴边能力（#39 边界）", async () => {
    const w = await mountDock(false);
    const dock = w.find(".dock");
    expect(dock.exists()).toBe(true);
    expect(dock.attributes("aria-label")).toBe("打开话题星球（星球入口；可拖动贴边）");
    w.unmount();
  });

  it("对话页锚定入口不再声称可拖动贴边（#39）", async () => {
    const w = await mountDock(true);
    const dock = w.find(".dock");
    expect(dock.classes()).toContain("anchored");
    // 旧行为被本项否定：锚定入口曾复用浮动模式的「可拖动贴边」表述（实际不可拖动）
    expect(dock.attributes("aria-label")).toBe("打开话题星球（星球入口）");
    expect(dock.attributes("aria-label")).not.toContain("贴边");
    w.unmount();
  });

  it("点击展开话题星球（open 事件）", async () => {
    const w = await mountDock();
    await w.find(".dock").trigger("click");
    expect(w.emitted("open")).toBeTruthy();
    w.unmount();
  });

  it("拖动后点击不触发 open（不误触发）", async () => {
    const w = await mountDock();
    await w.find(".dock").trigger("mousedown", { clientX: 40, clientY: 40 });
    document.dispatchEvent(new MouseEvent("mousemove", { clientX: 160, clientY: 120, bubbles: true }));
    document.dispatchEvent(new MouseEvent("mouseup", { clientX: 160, clientY: 120, bubbles: true }));
    await w.find(".dock").trigger("click");
    expect(w.emitted("open")).toBeFalsy();
    w.unmount();
  });

  it("贴边：初始位置为右侧居中（像素 left/top，position fixed）", async () => {
    const w = await mountDock();
    const dock = w.find(".dock").element as HTMLElement;
    expect(dock.style.position).toBe("fixed");
    expect(dock.style.left).toMatch(/^\d+px$/);
    expect(dock.style.top).toMatch(/^\d+px$/);
    w.unmount();
  });
});

describe("进入/退出星球时清理入口用途说明（#37）", () => {
  it("进入星球：收起已显示的说明 + 清掉排队中的迟到定时器，星球期间悬停/聚焦不再弹", async () => {
    vi.useFakeTimers();
    try {
      const w = await mountDock(true);
      const dock = w.find(".dock");

      // 悬停排队 → 说明出现
      await dock.trigger("pointerenter");
      vi.advanceTimersByTime(520);
      expect(tipState.open).toBe(true);

      // 进入星球：说明立即收起
      planetContinuum.phase = "activating";
      await nextTick();
      expect(tipState.open).toBe(false);

      // 星球期间：悬停 / 聚焦都不再弹（旧行为：迟到定时器仍会弹、聚焦立即弹）
      await dock.trigger("pointerenter");
      vi.advanceTimersByTime(600);
      expect(tipState.open).toBe(false);
      await dock.trigger("focusin");
      vi.advanceTimersByTime(600);
      expect(tipState.open).toBe(false);

      w.unmount();
    } finally {
      vi.useRealTimers();
      planetContinuum.phase = "idle";
    }
  });

  it("退出星球后不自动弹说明；等下一次真实悬停/聚焦再出现", async () => {
    vi.useFakeTimers();
    try {
      const w = await mountDock(true);
      const dock = w.find(".dock");

      planetContinuum.phase = "expanding";
      await nextTick();
      planetContinuum.phase = "idle";
      await nextTick();
      // 宽限期结束：不会自己弹出来
      vi.advanceTimersByTime(600);
      expect(tipState.open).toBe(false);

      // 重新悬停（新事件）才出现
      await dock.trigger("pointerenter");
      vi.advanceTimersByTime(520);
      expect(tipState.open).toBe(true);
      hideTip();

      // 聚焦也能重新出现（键盘路径）
      await dock.trigger("focusin");
      expect(tipState.open).toBe(true);

      w.unmount();
    } finally {
      vi.useRealTimers();
      planetContinuum.phase = "idle";
    }
  });
});


