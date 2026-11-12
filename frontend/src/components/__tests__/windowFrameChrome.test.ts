/**
 * WindowChrome × 0.3.2 壳通道（运行时）：
 * 1) mount 后 rAF 合并上报 qio_set_hit_regions（物理 px 结构完整）；
 * 2) qio://caption-hover 驱动 max 按钮 .is-hover（overlay 覆盖后 DOM :hover 失效）；
 * 3) native 模式不再上报自绘标题条/三键命中（系统窗框接管）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => {
  const unlisten = vi.fn();
  const hitRegions: unknown[] = [];
  const hoverCallbacks: Array<(payload: unknown) => void> = [];
  const stateCallbacks: Array<(payload: unknown) => void> = [];
  const win = {
    minimize: vi.fn(async () => {}),
    toggleMaximize: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
    isMaximized: vi.fn(async () => false),
    onResized: vi.fn(async () => unlisten),
  };
  return { unlisten, hitRegions, hoverCallbacks, stateCallbacks, win, push: vi.fn(async () => undefined) };
});

vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => mocks.win }));
vi.mock("../../services/desktop", () => ({
  inTauriShell: () => true,
  listenShellEvent: vi.fn(async () => mocks.unlisten),
  requestClose: vi.fn(async () => ({ action: "tray", hidden: true, quitting: false, detail: "" })),
  ackCloseRequest: vi.fn(async () => {}),
  syncCloseBehaviorToShell: vi.fn(async () => {}),
  noteShellQuit: vi.fn(),
  setHitRegions: vi.fn(async (regions: unknown) => {
    mocks.hitRegions.push(regions);
    return true;
  }),
  listenWindowState: vi.fn(async (cb: (payload: unknown) => void) => {
    mocks.stateCallbacks.push(cb);
    return mocks.unlisten;
  }),
  listenCaptionHover: vi.fn(async (cb: (payload: unknown) => void) => {
    mocks.hoverCallbacks.push(cb);
    return mocks.unlisten;
  }),
  listenFrameReady: vi.fn(async () => mocks.unlisten),
  getWindowFrame: vi.fn(async () => null),
}));
vi.mock("../../services/api", () => ({
  api: {
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50, close_behavior: "tray" })),
    updateUISettings: vi.fn(async () => ({ ok: true, typewriter_cps: 50 })),
    updateCloseBehavior: vi.fn(async (behavior: string) => ({
      ok: true,
      typewriter_cps: 50,
      close_behavior: behavior,
    })),
  },
}));
vi.mock("../../router", async () => {
  const { ref } = await import("vue");
  const currentRoute = ref({ name: "conversation", fullPath: "/conversation" });
  return { default: { currentRoute, push: mocks.push } };
});

import WindowChrome from "../WindowChrome.vue";
import { applyWindowFrameState, normalizeWindowState } from "../../composables/useWindowFrame";

function mountChrome() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(WindowChrome, { global: { plugins: [pinia] }, attachTo: document.body });
}

async function settle(): Promise<void> {
  await flushPromises();
  await new Promise((resolve) => setTimeout(resolve, 30));
  await nextTick();
  await flushPromises();
}

beforeEach(() => {
  (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  mocks.hitRegions.length = 0;
  mocks.hoverCallbacks.length = 0;
  mocks.stateCallbacks.length = 0;
  applyWindowFrameState(normalizeWindowState({ state: "normal", frame: "css" }));
});

afterEach(() => {
  delete (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  document.body.innerHTML = "";
});

describe("WindowChrome × 命中区上报", () => {
  it("mount 后上报 qio_set_hit_regions（结构完整，物理 px 字段）", async () => {
    const wrapper = mountChrome();
    await settle();
    expect(mocks.hitRegions.length).toBeGreaterThan(0);
    const regions = mocks.hitRegions[mocks.hitRegions.length - 1] as Record<string, unknown>;
    expect(regions.dpr).toBeGreaterThan(0);
    expect(regions.state).toBe("normal");
    expect(typeof regions.framePadding).toBe("number");
    expect(typeof regions.radius).toBe("number");
    expect(typeof regions.cornerRadius).toBe("number");
    expect(typeof regions.resizeBorder).toBe("number");
    expect(Array.isArray(regions.otherButtons)).toBe(true);
    expect(Array.isArray(regions.interactive)).toBe(true);
    wrapper.unmount();
  });

  it("qio://caption-hover 驱动 max 按钮 .is-hover（不依赖 DOM :hover）", async () => {
    const wrapper = mountChrome();
    await settle();
    expect(mocks.hoverCallbacks.length).toBeGreaterThan(0);
    const hover = mocks.hoverCallbacks[0];
    hover({ target: "max", hover: true, ts: 1 });
    await nextTick();
    expect(wrapper.find(".wc-btn.is-hover").exists()).toBe(true);
    hover({ target: "max", hover: false, ts: 2 });
    await nextTick();
    expect(wrapper.find(".wc-btn.is-hover").exists()).toBe(false);
    wrapper.unmount();
  });

  it("native 模式：不报自绘标题条/三键（系统窗框接管），framePadding=0", async () => {
    applyWindowFrameState(normalizeWindowState({ state: "normal", frame: "native" }));
    const wrapper = mountChrome();
    await settle();
    window.dispatchEvent(new Event("resize"));
    await settle();
    const regions = mocks.hitRegions[mocks.hitRegions.length - 1] as Record<string, unknown>;
    expect(regions.titlebar).toBeNull();
    expect(regions.maxButton).toBeNull();
    expect(regions.otherButtons).toEqual([]);
    expect(regions.framePadding).toBe(0);
    wrapper.unmount();
  });
});
