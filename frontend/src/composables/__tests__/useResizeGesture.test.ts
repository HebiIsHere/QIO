/**
 * 尺寸拖动手势用例（互动模式契约 2026-10-13 · IRL §2）。
 *
 * 重点不是「能不能改大」，而是**边界**：
 * - 拖动中只预览、松手才提交一次；
 * - 阈值内算点击，不改尺寸；
 * - 指针取消 / Esc / 失焦 / 组件卸载 = 恢复拖动前状态 + 清理监听；
 * - 画布缩放下的位移换算。
 */
import { describe, expect, it, vi } from "vitest";
import { effectScope } from "vue";
import { useResizeGesture, type ResizeGestureOptions } from "../useResizeGesture";
import { sizeLimits, type ResizeBox } from "../../interactive/resize";

const LIMITS = sizeLimits(100, 60, 800, 600);
const BASE: ResizeBox = { x: 100, y: 100, w: 400, h: 300 };

function makeEvent(type: string, clientX: number, clientY: number, extra: MouseEventInit = {}): MouseEvent {
  return new MouseEvent(type, { clientX, clientY, bubbles: true, cancelable: true, ...extra });
}

function setup(overrides: Partial<ResizeGestureOptions> = {}) {
  const scope = effectScope();
  const previews: ResizeBox[] = [];
  const commits: { box: ResizeBox; reason: "pointer" | "keyboard" }[] = [];
  const cancels: ResizeBox[] = [];
  let api!: ReturnType<typeof useResizeGesture>;
  scope.run(() => {
    api = useResizeGesture({
      box: () => BASE,
      limits: () => LIMITS,
      onPreview: (box) => previews.push({ ...box }),
      onCommit: (box, reason) => commits.push({ box: { ...box }, reason }),
      onCancel: (box) => cancels.push({ ...box }),
      ...overrides,
    });
  });
  return { scope, api, previews, commits, cancels };
}

describe("useResizeGesture：提交时机", () => {
  it("拖动中只预览，松手才提交一次", () => {
    const { api, previews, commits } = setup();
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 30, 20));
    document.dispatchEvent(makeEvent("pointermove", 50, 40));
    expect(previews).toHaveLength(2);
    expect(commits).toHaveLength(0);
    document.dispatchEvent(makeEvent("pointerup", 50, 40));
    expect(commits).toHaveLength(1);
    expect(commits[0].reason).toBe("pointer");
    expect(commits[0].box.w).toBe(450);
    expect(commits[0].box.h).toBe(340);
  });

  it("松手后监听已清干净：后续 move 不再产生预览", () => {
    const { api, previews } = setup();
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 30, 20));
    document.dispatchEvent(makeEvent("pointerup", 30, 20));
    const before = previews.length;
    document.dispatchEvent(makeEvent("pointermove", 90, 90));
    expect(previews).toHaveLength(before);
  });

  it("阈值之内算点击：不改尺寸、不提交", () => {
    const { api, commits, cancels } = setup();
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 1, 1));
    document.dispatchEvent(makeEvent("pointerup", 1, 1));
    expect(commits).toHaveLength(0);
    expect(cancels).toHaveLength(0);
  });

  it("非主键（右键）不进入拖动", () => {
    const { api, previews, commits } = setup();
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0, { button: 2 }) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 60, 60));
    document.dispatchEvent(makeEvent("pointerup", 60, 60));
    expect(previews).toHaveLength(0);
    expect(commits).toHaveLength(0);
  });
});

describe("useResizeGesture：取消即恢复", () => {
  it("Esc 恢复拖动前状态，且不再提交", () => {
    const { api, commits, cancels, previews } = setup();
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 80, 60));
    expect(previews).toHaveLength(1);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(cancels).toEqual([BASE]);
    expect(commits).toHaveLength(0);
    document.dispatchEvent(makeEvent("pointerup", 80, 60));
    expect(commits).toHaveLength(0);
  });

  it("pointercancel 同样恢复", () => {
    const { api, commits, cancels } = setup();
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 80, 60));
    document.dispatchEvent(makeEvent("pointercancel", 80, 60));
    expect(cancels).toEqual([BASE]);
    expect(commits).toHaveLength(0);
  });

  it("窗口失焦恢复", () => {
    const { api, commits, cancels } = setup();
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 80, 60));
    window.dispatchEvent(new Event("blur"));
    expect(cancels).toEqual([BASE]);
    expect(commits).toHaveLength(0);
  });

  it("组件卸载（scope 销毁）时还在拖动：恢复并清理监听", () => {
    const { scope, api, cancels, previews } = setup();
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 80, 60));
    const before = previews.length;
    scope.stop();
    expect(cancels).toEqual([BASE]);
    document.dispatchEvent(makeEvent("pointermove", 200, 200));
    expect(previews).toHaveLength(before);
  });
});

describe("useResizeGesture：坐标系与键盘", () => {
  it("scale 把屏幕位移换算成板面单位（画布缩放 0.5 → 位移翻倍）", () => {
    const { api, commits } = setup({ scale: () => 1 / 0.5 });
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 50, 30));
    document.dispatchEvent(makeEvent("pointerup", 50, 30));
    expect(commits[0].box.w).toBe(500);
    expect(commits[0].box.h).toBe(360);
  });

  it("scale 返回 0 / NaN 时按 1 处理，不产生 NaN 尺寸", () => {
    const { api, commits } = setup({ scale: () => Number.NaN });
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 50, 30));
    document.dispatchEvent(makeEvent("pointerup", 50, 30));
    expect(Number.isFinite(commits[0].box.w)).toBe(true);
    expect(commits[0].box.w).toBe(450);
  });

  it("nudge 立即提交，标记来源是键盘", () => {
    const { api, commits, previews } = setup();
    api.nudge("w", 1);
    api.nudge("h", -1);
    expect(previews).toHaveLength(0);
    expect(commits.map((item) => item.reason)).toEqual(["keyboard", "keyboard"]);
    expect(commits[0].box.w).toBe(424);
    expect(commits[1].box.h).toBe(276);
  });

  it("拖动期间 active 为真，松手后复位", () => {
    const { api } = setup();
    expect(api.active.value).toBe(false);
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 40, 40));
    expect(api.active.value).toBe(true);
    expect(api.previewing()).toBe(true);
    document.dispatchEvent(makeEvent("pointerup", 40, 40));
    expect(api.active.value).toBe(false);
  });

  it("限制在拖动过程中实时生效（窗口被缩小后立刻夹住）", () => {
    let limits = LIMITS;
    const { api, commits } = setup({ limits: () => limits });
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    limits = sizeLimits(100, 60, 420, 310);
    document.dispatchEvent(makeEvent("pointermove", 300, 300));
    document.dispatchEvent(makeEvent("pointerup", 300, 300));
    expect(commits[0].box.w).toBe(420);
    expect(commits[0].box.h).toBe(310);
  });
});

describe("useResizeGesture：重复按下", () => {
  it("前一次拖动还没结束时再次按下：先恢复上一次，再开新的一次", () => {
    const { api, commits, cancels } = setup();
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 60, 60));
    api.onHandleDown("nw", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    expect(cancels).toEqual([BASE]);
    document.dispatchEvent(makeEvent("pointermove", 10, 10));
    document.dispatchEvent(makeEvent("pointerup", 10, 10));
    expect(commits).toHaveLength(1);
    // nw：左上角跟着走，右下角固定。
    expect(commits[0].box.x).toBe(110);
    expect(commits[0].box.w).toBe(390);
  });
});

describe("useResizeGesture：不启动无意义监听", () => {
  it("没有按下把手时，document 上的 move 不产生任何回调", () => {
    const { previews, commits } = setup();
    document.dispatchEvent(makeEvent("pointermove", 100, 100));
    expect(previews).toHaveLength(0);
    expect(commits).toHaveLength(0);
  });

  it("cancel() 在没有拖动时是安全的空操作", () => {
    const { api, cancels } = setup();
    expect(() => api.cancel()).not.toThrow();
    expect(cancels).toHaveLength(0);
  });
});

describe("useResizeGesture：可注入的步长", () => {
  it("step 选项生效", () => {
    const { api, commits } = setup({ step: 8 });
    api.nudge("h", 1);
    expect(commits[0].box.h).toBe(308);
  });

  it("预览与提交使用同一份 limits（不出现「预览能拉、松手弹回」）", () => {
    const spy = vi.fn(() => LIMITS);
    const { api, previews, commits } = setup({ limits: spy });
    api.onHandleDown("se", makeEvent("pointerdown", 0, 0) as unknown as PointerEvent);
    document.dispatchEvent(makeEvent("pointermove", 50, 50));
    document.dispatchEvent(makeEvent("pointerup", 50, 50));
    expect(commits[0].box).toEqual(previews[previews.length - 1]);
  });
});
