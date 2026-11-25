import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BoardCanvas from "../BoardCanvas.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { emptyState } from "../../../interactive/board";
import { dotGridMetrics, gridPhase } from "../../../interactive/viewport";

const source = readFileSync(
  resolve(process.cwd(), "src/components/interactive/BoardCanvas.vue"),
  "utf8",
);

function block(selector: string): string {
  const start = source.indexOf(selector);
  if (start < 0) return "";
  const open = source.indexOf("{", start);
  const close = source.indexOf("}", open);
  return open < 0 || close < 0 ? "" : source.slice(open + 1, close);
}

const VIEW_LEFT = 40;
const VIEW_TOP = 100;
const VIEW_W = 1200;
const VIEW_H = 800;

let wrapper: VueWrapper | null = null;
let store: ReturnType<typeof useInteractiveStore>;

function mountCanvas(): VueWrapper {
  wrapper = mount(BoardCanvas, { attachTo: document.body });
  const view = wrapper.get(".board-viewport").element as HTMLElement;
  view.getBoundingClientRect = () =>
    ({ left: VIEW_LEFT, top: VIEW_TOP, right: VIEW_LEFT + VIEW_W, bottom: VIEW_TOP + VIEW_H, width: VIEW_W, height: VIEW_H, x: VIEW_LEFT, y: VIEW_TOP, toJSON: () => ({}) }) as DOMRect;
  Object.defineProperty(view, "clientWidth", { value: VIEW_W, configurable: true });
  Object.defineProperty(view, "clientHeight", { value: VIEW_H, configurable: true });
  return wrapper;
}

function gridOf(frame: VueWrapper): { size: string; position: string } {
  const style = (frame.get(".board-viewport").element as HTMLElement).style;
  return { size: style.backgroundSize, position: style.backgroundPosition };
}

function pointer(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  store.board = emptyState("board_t");
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
});

describe("板面点阵纸", () => {
  it("点阵铺在整个视口上：白色底 + 视口自身背景（不再靠滚动内容撑开）", () => {
    const viewport = block(".board-viewport {");
    expect(viewport).toContain("--bg-surface");
    expect(viewport).toContain("radial-gradient");
    expect(viewport).toContain("background-repeat: repeat");
    // 旧模型靠 background-attachment: local 让点阵跟着滚动内容走；无限画布由 view 决定相位
    expect(viewport).not.toContain("background-attachment");
    expect(viewport).not.toContain("--bg-inset");
    const surface = block(".board-surface {");
    expect(surface).toContain("background: transparent");
  });

  it("使用主题色点阵：100% 缩放时 24px 间距、1px 点、低透明度", () => {
    const viewport = block(".board-viewport {");
    expect(viewport).toMatch(/color-mix\([^)]*var\(--accent\)/);
    expect(viewport).toContain("1px");
    // 间距由 view 写入（gridStyle），基准值仍是 24px
    expect(dotGridMetrics({ scale: 1, x: 0, y: 0 }, 24, 8).spacing).toBe(24);
  });

  it("空板：点阵铺满视口，四个方向平移后相位跟着走（不是固定背景）", async () => {
    const frame = mountCanvas();
    const before = gridOf(frame);
    expect(before.size).toBe("24px 24px");
    expect(before.position).toBe("0px 0px");

    // 向左上拖 300 / 200 屏幕像素：世界原点右移下移，点阵相位跟着走
    const view = frame.get(".board-viewport").element as HTMLElement;
    view.dispatchEvent(pointer("pointerdown", 600, 500));
    window.dispatchEvent(pointer("pointermove", 900, 700));
    await frame.vm.$nextTick();
    const after = gridOf(frame);
    expect(after.size).toBe("24px 24px");
    // 300 % 24 = 12，200 % 24 = 8
    expect(after.position).toBe("12px 8px");
    window.dispatchEvent(pointer("pointerup", 900, 700));
  });

  it("负方向平移：相位用取模收口，跨过零点不跳格", () => {
    // 直接对纯函数断言：负偏移必须落在 [0, spacing)
    expect(gridPhase(-1, 24)).toBe(23);
    expect(gridPhase(-24, 24)).toBe(0);
    expect(gridPhase(-25, 24)).toBe(23);
    expect(gridPhase(0, 24)).toBe(0);
    expect(gridPhase(25, 24)).toBe(1);
    // 往返跨过零点：相位连续（相差一个间距的整数倍）
    const left = gridPhase(-7, 24);
    const right = gridPhase(17, 24);
    expect(left).toBe(right);
  });

  it("缩得很小时按 2 的倍数合并点，避免糊成一片", () => {
    const at100 = dotGridMetrics({ scale: 1, x: 0, y: 0 }, 24, 8);
    expect(at100.spacing).toBe(24);
    // 25%：24 × 0.25 = 6px < 8px 下限 → 世界间距翻倍到 48，屏幕间距 12px
    const at25 = dotGridMetrics({ scale: 0.25, x: 0, y: 0 }, 24, 8);
    expect(at25.spacing).toBe(12);
    // 合并前后用同一个世界原点算相位：不会跳格
    expect(at25.phaseX).toBe(gridPhase(0, 12));
  });
});
