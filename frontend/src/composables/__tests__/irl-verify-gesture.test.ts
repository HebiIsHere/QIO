/**
 * IRL-E 独立复核（第一阶段）· useResizeGesture 的对抗式用例。
 *
 * 角度差异：不看「能不能拉大」，而是
 * 1) 指针身份（多指 / pointerId 过滤）与取消的对称性；
 * 2) 拖动阈值的边界值，以及换算系数（画布缩放）对阈值的影响；
 * 3) 松手瞬间的时序（最后一次 move 之后限制/缩放变了会怎样）；
 * 4) 键盘 nudge 的累加语义依赖调用方；接口在空事件下的健壮性。
 *
 * it.fails = 契约应当成立而当前实现不成立的最小复现，缺陷记在 _IRL-REVIEW-E-1.md。
 */
import { describe, expect, it, vi } from "vitest";
import { effectScope, type EffectScope } from "vue";
import { useResizeGesture, type ResizeGestureOptions } from "../useResizeGesture";
import { sizeLimits, type ResizeBox } from "../../interactive/resize";

const LIMITS = sizeLimits(120, 80, 640, 480);
const BASE: ResizeBox = { x: 200, y: 150, w: 320, h: 240 };

interface Extra {
  pointerId?: number;
  button?: number;
  isPrimary?: boolean;
}

/** jsdom 没有 PointerEvent 时也能构造带 pointerId / isPrimary 的事件。 */
function pe(type: string, x: number, y: number, extra: Extra = {}): PointerEvent {
  const event = new MouseEvent(type, {
    clientX: x,
    clientY: y,
    bubbles: true,
    cancelable: true,
    button: extra.button ?? 0,
  });
  if (extra.pointerId !== undefined) {
    Object.defineProperty(event, "pointerId", { value: extra.pointerId, configurable: true });
  }
  if (extra.isPrimary !== undefined) {
    Object.defineProperty(event, "isPrimary", { value: extra.isPrimary, configurable: true });
  }
  return event as unknown as PointerEvent;
}

interface Harness {
  api: ReturnType<typeof useResizeGesture>;
  scope: EffectScope;
  previews: ResizeBox[];
  commits: { box: ResizeBox; reason: string }[];
  cancels: ResizeBox[];
}

function harness(overrides: Partial<ResizeGestureOptions> = {}): Harness {
  const scope = effectScope();
  const previews: ResizeBox[] = [];
  const commits: { box: ResizeBox; reason: string }[] = [];
  const cancels: ResizeBox[] = [];
  let api!: ReturnType<typeof useResizeGesture>;
  scope.run(() => {
    api = useResizeGesture({
      box: () => ({ ...BASE }),
      limits: () => LIMITS,
      onPreview: (box) => previews.push({ ...box }),
      onCommit: (box, reason) => commits.push({ box: { ...box }, reason }),
      onCancel: (box) => cancels.push({ ...box }),
      ...overrides,
    });
  });
  return { api, scope, previews, commits, cancels };
}

const press = (api: ReturnType<typeof useResizeGesture>, handle: "se" | "nw" | "e", extra: Extra = {}) =>
  api.onHandleDown(handle, pe("pointerdown", 0, 0, extra));

describe("指针身份：同一个拖动只认一根指针", () => {
  it("另一根手指的 pointerup 不会结束当前拖动", () => {
    const h = harness();
    press(h.api, "se", { pointerId: 1 });
    document.dispatchEvent(pe("pointermove", 40, 40, { pointerId: 1 }));
    document.dispatchEvent(pe("pointerup", 999, 999, { pointerId: 2 }));
    expect(h.commits).toHaveLength(0);
    document.dispatchEvent(pe("pointerup", 40, 40, { pointerId: 1 }));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.w).toBe(BASE.w + 40);
  });

  it("现状：pointercancel 不校验 pointerId，别的手指取消会把当前拖动回滚", () => {
    const h = harness();
    press(h.api, "se", { pointerId: 1 });
    document.dispatchEvent(pe("pointermove", 40, 40, { pointerId: 1 }));
    document.dispatchEvent(pe("pointercancel", 40, 40, { pointerId: 2 }));
    expect(h.cancels).toEqual([BASE]);
    expect(h.commits).toHaveLength(0);
  });

  it.fails("pointercancel 只应当取消同一个 pointerId 的拖动", () => {
    const h = harness();
    press(h.api, "se", { pointerId: 1 });
    document.dispatchEvent(pe("pointermove", 40, 40, { pointerId: 1 }));
    document.dispatchEvent(pe("pointercancel", 40, 40, { pointerId: 2 }));
    expect(h.cancels).toHaveLength(0);
  });

  it("isPrimary=false 的按下被忽略：既不打断已有拖动，也不开新的", () => {
    const h = harness();
    press(h.api, "se", { pointerId: 1 });
    document.dispatchEvent(pe("pointermove", 40, 40, { pointerId: 1 }));
    press(h.api, "nw", { pointerId: 2, isPrimary: false });
    expect(h.cancels).toHaveLength(0);
    document.dispatchEvent(pe("pointermove", 60, 60, { pointerId: 1 }));
    document.dispatchEvent(pe("pointerup", 60, 60, { pointerId: 1 }));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.w).toBe(BASE.w + 60);
  });

  it("中键按下不进入拖动（只接主键）", () => {
    const h = harness();
    press(h.api, "se", { button: 1 });
    document.dispatchEvent(pe("pointermove", 60, 60));
    document.dispatchEvent(pe("pointerup", 60, 60));
    expect(h.previews).toHaveLength(0);
    expect(h.commits).toHaveLength(0);
  });
});

describe("阈值与换算系数", () => {
  it("阈值边界恰好 2 个单位：换算后 1.5 不动，2.0 开始", () => {
    const below = harness({ scale: () => 0.5 });
    below.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 3, 0));
    document.dispatchEvent(pe("pointerup", 3, 0));
    expect(below.previews).toHaveLength(0);
    expect(below.commits).toHaveLength(0);

    const exact = harness({ scale: () => 0.5 });
    exact.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 4, 0));
    document.dispatchEvent(pe("pointerup", 4, 0));
    expect(exact.commits).toHaveLength(1);
    expect(exact.commits[0].box.w).toBe(BASE.w + 2);
  });

  it("现状：低缩放（画布 0.25 → 换算系数 4）下 1px 屏幕抖动就提交，尺寸 +4", () => {
    const h = harness({ scale: () => 4 });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 1, 0));
    document.dispatchEvent(pe("pointerup", 1, 0));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.w).toBe(BASE.w + 4);
  });

  it.fails("低缩放（换算系数 4）下 1px 屏幕抖动不该越过 2 单位的拖动阈值", () => {
    const h = harness({ scale: () => 4 });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 1, 0));
    document.dispatchEvent(pe("pointerup", 1, 0));
    expect(h.commits).toHaveLength(0);
  });

  it("换算系数 0 / 负数 / NaN 都退回 1", () => {
    for (const scale of [0, -3, Number.NaN]) {
      const h = harness({ scale: () => scale });
      h.api.onHandleDown("se", pe("pointerdown", 0, 0));
      document.dispatchEvent(pe("pointermove", 50, 30));
      document.dispatchEvent(pe("pointerup", 50, 30));
      expect(h.commits).toHaveLength(1);
      expect(h.commits[0].box.w).toBe(BASE.w + 50);
      expect(h.commits[0].box.h).toBe(BASE.h + 30);
    }
  });

  it("换算系数每次 move 重新求值：拖动中改缩放会按新系数整体重算（画面会跳一下）", () => {
    let scale = 1;
    const h = harness({ scale: () => scale });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 10, 0));
    expect(h.previews[h.previews.length - 1].w).toBe(BASE.w + 10);
    scale = 2;
    document.dispatchEvent(pe("pointermove", 20, 0));
    expect(h.previews[h.previews.length - 1].w).toBe(BASE.w + 40);
  });
});

describe("取消即恢复：四类取消源", () => {
  it("拖动中连按两次 Esc 只回调一次（第一次已经清干净）", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 80, 60));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(h.cancels).toHaveLength(1);
    expect(h.commits).toHaveLength(0);
  });

  it("阈值之内的 Esc 不回调（没有需要恢复的东西）", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 1, 1));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(h.cancels).toHaveLength(0);
  });

  it("没按下把手时 Esc / window.blur 都不回调", () => {
    const h = harness();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    window.dispatchEvent(new Event("blur"));
    expect(h.cancels).toHaveLength(0);
  });

  it("成功松手不回调 onCancel", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 30, 30));
    document.dispatchEvent(pe("pointerup", 30, 30));
    expect(h.cancels).toHaveLength(0);
    expect(h.commits).toHaveLength(1);
  });

  it("Esc 之后监听已摘除：继续 move 不产生预览", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 80, 60));
    const before = h.previews.length;
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.dispatchEvent(pe("pointermove", 200, 200));
    expect(h.previews).toHaveLength(before);
  });

  it("window.blur 之后监听已摘除：继续 move 不产生预览", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 80, 60));
    const before = h.previews.length;
    window.dispatchEvent(new Event("blur"));
    document.dispatchEvent(pe("pointermove", 200, 200));
    expect(h.previews).toHaveLength(before);
  });

  it("取消后 active 复位，可以重新开始一次拖动", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 80, 60));
    expect(h.api.active.value).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(h.api.active.value).toBe(false);
    h.api.onHandleDown("e", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 20, 0));
    expect(h.api.active.value).toBe(true);
    document.dispatchEvent(pe("pointerup", 20, 0));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.w).toBe(BASE.w + 20);
  });
});

describe("松手瞬间的时序", () => {
  it("提交的数值与最后一次预览完全一致", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 31, 17));
    document.dispatchEvent(pe("pointermove", 62, 34));
    document.dispatchEvent(pe("pointerup", 62, 34));
    expect(h.commits[0].box).toEqual(h.previews[h.previews.length - 1]);
  });

  it("现状：松手不再夹一次——最后一次 move 之后可用区域缩小，提交的仍是旧预览", () => {
    let limits = LIMITS;
    const h = harness({ limits: () => limits });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 500, 500));
    expect(h.previews[h.previews.length - 1].w).toBe(LIMITS.maxW);
    expect(h.previews[h.previews.length - 1].h).toBe(LIMITS.maxH);
    limits = sizeLimits(120, 80, 420, 310);
    document.dispatchEvent(pe("pointerup", 500, 500));
    expect(h.commits[0].box.w).toBe(LIMITS.maxW);
    expect(h.commits[0].box.h).toBe(LIMITS.maxH);
  });
});

describe("键盘 nudge", () => {
  it("用调用方当前的框：调用方每步应用提交，步进就会累加", () => {
    let box: ResizeBox = { ...BASE };
    const h = harness({
      box: () => ({ ...box }),
      onCommit: (next) => {
        box = { ...next };
      },
    });
    h.api.nudge("w", 1);
    h.api.nudge("w", 1);
    expect(box.w).toBe(BASE.w + 48);
  });

  it("现状：调用方没有同步应用提交时，连续 nudge 得到同一个目标（丢更新）", () => {
    const h = harness();
    h.api.nudge("w", 1);
    h.api.nudge("w", 1);
    expect(h.commits.map((item) => item.box.w)).toEqual([BASE.w + 24, BASE.w + 24]);
  });

  it("立即用当前限制：上限收紧后马上夹住", () => {
    let limits = LIMITS;
    const h = harness({ limits: () => limits });
    limits = sizeLimits(120, 80, BASE.w + 4, 480);
    h.api.nudge("w", 1);
    expect(h.commits[0].box.w).toBe(BASE.w + 4);
  });

  it("步长 0 时提交一份没变的框（调用方要自己防重复提交）", () => {
    const h = harness({ step: 0 });
    h.api.nudge("h", 1);
    expect(h.commits[0].box.h).toBe(BASE.h);
  });
});

describe("接口健壮性", () => {
  it("onHandleDown(null) 安全：不抛、不附着监听、不提交", () => {
    const h = harness();
    expect(() => h.api.onHandleDown("se", null)).not.toThrow();
    document.dispatchEvent(pe("pointermove", 100, 100));
    document.dispatchEvent(pe("pointerup", 100, 100));
    expect(h.previews).toHaveLength(0);
    expect(h.commits).toHaveLength(0);
  });

  it("按下时 preventDefault + stopPropagation（不要同时启动宿主的拖拽 / 选择）", () => {
    const h = harness();
    const event = pe("pointerdown", 0, 0);
    const prevent = vi.fn();
    const stop = vi.fn();
    Object.defineProperty(event, "preventDefault", { value: prevent, configurable: true });
    Object.defineProperty(event, "stopPropagation", { value: stop, configurable: true });
    h.api.onHandleDown("se", event);
    expect(prevent).toHaveBeenCalledTimes(1);
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it("未拖动时 scope.stop() 不回调 onCancel", () => {
    const h = harness();
    h.scope.stop();
    expect(h.cancels).toHaveLength(0);
  });

  it("拖动中 scope.stop()：先恢复再摘监听", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 80, 60));
    const before = h.previews.length;
    h.scope.stop();
    expect(h.cancels).toEqual([BASE]);
    document.dispatchEvent(pe("pointermove", 200, 200));
    expect(h.previews).toHaveLength(before);
  });

  it("重复按下时先恢复上一次：恢复的是按下那一刻的快照", () => {
    const h = harness();
    press(h.api, "se");
    document.dispatchEvent(pe("pointermove", 60, 60));
    press(h.api, "nw");
    expect(h.cancels).toEqual([BASE]);
    document.dispatchEvent(pe("pointermove", 10, 10));
    document.dispatchEvent(pe("pointerup", 10, 10));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.x).toBe(BASE.x + 10);
    expect(h.commits[0].box.w).toBe(BASE.w - 10);
  });
});
