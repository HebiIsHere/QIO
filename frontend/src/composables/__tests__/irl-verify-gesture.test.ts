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

const press = (
  api: ReturnType<typeof useResizeGesture>,
  handle: "se" | "nw" | "e" | "w",
  extra: Extra = {},
) => api.onHandleDown(handle, pe("pointerdown", 0, 0, extra));

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

  it("修复后（F3）：三个不同 pointerId 的 pointercancel 都不回滚当前拖动；后续仍能正常提交", () => {
    const h = harness();
    press(h.api, "se", { pointerId: 1 });
    document.dispatchEvent(pe("pointermove", 40, 40, { pointerId: 1 }));
    for (const foreign of [2, 7, 99]) {
      document.dispatchEvent(pe("pointercancel", 40, 40, { pointerId: foreign }));
    }
    expect(h.cancels).toHaveLength(0);
    expect(h.api.active.value).toBe(true);
    document.dispatchEvent(pe("pointermove", 60, 60, { pointerId: 1 }));
    document.dispatchEvent(pe("pointerup", 60, 60, { pointerId: 1 }));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.w).toBe(BASE.w + 60);
  });

  it("没有 pointerId 的指针（老浏览器 / 合成事件的 pointercancel）仍然回滚", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 40, 40));
    document.dispatchEvent(pe("pointercancel", 40, 40));
    expect(h.cancels).toEqual([BASE]);
  });

  it("修复后（F3）：同一个 pointerId 的 pointercancel 仍然照常回滚（没有矫枉过正）", () => {
    const h = harness();
    press(h.api, "se", { pointerId: 5 });
    document.dispatchEvent(pe("pointermove", 40, 40, { pointerId: 5 }));
    document.dispatchEvent(pe("pointercancel", 40, 40, { pointerId: 5 }));
    expect(h.cancels).toEqual([BASE]);
    expect(h.commits).toHaveLength(0);
    expect(h.api.active.value).toBe(false);
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
  it("修复后（F2）：1px 屏幕抖动在 5 种换算系数下都不进入拖动、不提交", () => {
    for (const scale of [0.25, 0.5, 1, 2, 4]) {
      const h = harness({ scale: () => scale });
      h.api.onHandleDown("se", pe("pointerdown", 0, 0));
      document.dispatchEvent(pe("pointermove", 1, 0));
      document.dispatchEvent(pe("pointerup", 1, 0));
      expect(h.previews).toHaveLength(0);
      expect(h.commits).toHaveLength(0);
    }
  });

  it("修复后（F2）：2px 屏幕位移在每种系数下都开始拖动，尺寸 = 2 × 系数（阈值按屏幕像素）", () => {
    const cases: { scale: number; expected: number }[] = [
      { scale: 0.25, expected: 0.5 },
      { scale: 0.5, expected: 1 },
      { scale: 1, expected: 2 },
      { scale: 2, expected: 4 },
      { scale: 4, expected: 8 },
    ];
    for (const item of cases) {
      const h = harness({ scale: () => item.scale });
      h.api.onHandleDown("se", pe("pointerdown", 0, 0));
      document.dispatchEvent(pe("pointermove", 2, 0));
      document.dispatchEvent(pe("pointerup", 2, 0));
      expect(h.commits).toHaveLength(1);
      expect(h.commits[0].box.w).toBe(BASE.w + item.expected);
    }
  });

  it("修复后（F2）：阈值与缩放无关——0.25 与 4 两种系数下 1px / 2px 的分界一致", () => {
    for (const scale of [0.25, 4]) {
      const short = harness({ scale: () => scale });
      short.api.onHandleDown("se", pe("pointerdown", 0, 0));
      document.dispatchEvent(pe("pointermove", 1, 0));
      document.dispatchEvent(pe("pointerup", 1, 0));
      expect(short.commits).toHaveLength(0);

      const enough = harness({ scale: () => scale });
      enough.api.onHandleDown("se", pe("pointerdown", 0, 0));
      document.dispatchEvent(pe("pointermove", 2, 0));
      document.dispatchEvent(pe("pointerup", 2, 0));
      expect(enough.commits).toHaveLength(1);
    }
  });

  it("位移相对按下点计算：按下点不在原点时阈值同样成立", () => {
    const short = harness();
    short.api.onHandleDown("se", pe("pointerdown", 100, 80));
    document.dispatchEvent(pe("pointermove", 101, 80));
    document.dispatchEvent(pe("pointerup", 101, 80));
    expect(short.commits).toHaveLength(0);

    const enough = harness();
    enough.api.onHandleDown("se", pe("pointerdown", 100, 80));
    document.dispatchEvent(pe("pointermove", 102, 80));
    document.dispatchEvent(pe("pointerup", 102, 80));
    expect(enough.commits).toHaveLength(1);
    expect(enough.commits[0].box.w).toBe(BASE.w + 2);
  });

  it("反方向 2px 同样触发（阈值看绝对值），尺寸变小", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 100, 100));
    document.dispatchEvent(pe("pointermove", 98, 100));
    document.dispatchEvent(pe("pointerup", 98, 100));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.w).toBe(BASE.w - 2);
  });

  it("阈值语义（沿用旧实现）：任一轴达到 2px 就算拖动，对角 2px/1px 也算", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 2, 1));
    expect(h.previews).toHaveLength(1);
    document.dispatchEvent(pe("pointerup", 2, 1));
    expect(h.commits).toHaveLength(1);
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

  it("修复后（W5）：最后一次 move 之后可用区域缩小，提交按当下限制复算（不超界）", () => {
    let limits = LIMITS;
    const h = harness({ limits: () => limits });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 500, 500));
    expect(h.previews[h.previews.length - 1].w).toBe(LIMITS.maxW);
    expect(h.previews[h.previews.length - 1].h).toBe(LIMITS.maxH);
    limits = sizeLimits(120, 80, 420, 310);
    document.dispatchEvent(pe("pointerup", 500, 500));
    expect(h.commits[0].box.w).toBe(420);
    expect(h.commits[0].box.h).toBe(310);
  });

  it("修复后（W5）：拉西边时复算仍然保持对边固定（右边界不动、左下角不漂）", () => {
    let limits = LIMITS;
    const h = harness({ limits: () => limits });
    h.api.onHandleDown("w", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 100, 0));
    expect(h.previews[h.previews.length - 1].w).toBe(BASE.w - 100);
    limits = sizeLimits(300, 80, 640, 480);
    document.dispatchEvent(pe("pointerup", 100, 0));
    expect(h.commits[0].box.w).toBe(300);
    expect(h.commits[0].box.x + h.commits[0].box.w).toBe(BASE.x + BASE.w);
  });

  it("修复后（R2）：松手前限制变大时，先推最终预览再提交——界面不会在松手后跳", () => {
    let limits = sizeLimits(120, 80, 420, 310);
    const h = harness({ limits: () => limits });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 500, 500));
    expect(h.previews[h.previews.length - 1].w).toBe(420);
    const previewsBefore = h.previews.length;
    limits = LIMITS;
    document.dispatchEvent(pe("pointerup", 500, 500));
    expect(h.commits[0].box.w).toBe(LIMITS.maxW);
    expect(h.previews).toHaveLength(previewsBefore + 1);
    expect(h.previews[h.previews.length - 1]).toEqual(h.commits[0].box);
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

  it("调用方契约：没有同步应用提交时，连续 nudge 得到同一个目标（丢更新）", () => {
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

describe("R2：复算与预览一致（松手不跳）", () => {
  it("情形一（限制变大）：提交在当下限制内，且最后一次 onPreview === onCommit", () => {
    let limits = sizeLimits(120, 80, 420, 310);
    const h = harness({ limits: () => limits });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 500, 500));
    expect(h.previews[h.previews.length - 1].w).toBe(420);
    limits = LIMITS;
    document.dispatchEvent(pe("pointerup", 500, 500));
    const commit = h.commits[0].box;
    expect(commit.w).toBe(LIMITS.maxW);
    expect(commit.h).toBe(LIMITS.maxH);
    expect(commit.w).toBeLessThanOrEqual(limits.maxW);
    expect(commit.h).toBeLessThanOrEqual(limits.maxH);
    expect(h.previews[h.previews.length - 1]).toEqual(commit);
  });

  it("情形二（限制变小）：提交在当下限制内，且最后一次 onPreview === onCommit", () => {
    let limits = LIMITS;
    const h = harness({ limits: () => limits });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 500, 500));
    expect(h.previews[h.previews.length - 1].w).toBe(LIMITS.maxW);
    limits = sizeLimits(120, 80, 420, 310);
    document.dispatchEvent(pe("pointerup", 500, 500));
    const commit = h.commits[0].box;
    expect(commit.w).toBe(420);
    expect(commit.h).toBe(310);
    expect(commit.w).toBeLessThanOrEqual(limits.maxW);
    expect(commit.h).toBeLessThanOrEqual(limits.maxH);
    expect(h.previews[h.previews.length - 1]).toEqual(commit);
  });

  it("情形三（拖动中 scale 改变）：提交按新系数复算，且最后一次 onPreview === onCommit", () => {
    let scale = 1;
    const h = harness({ scale: () => scale });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 50, 50));
    expect(h.previews[h.previews.length - 1].w).toBe(BASE.w + 50);
    scale = 2;
    document.dispatchEvent(pe("pointerup", 50, 50));
    const commit = h.commits[0].box;
    expect(commit.w).toBe(BASE.w + 100);
    expect(commit.h).toBe(BASE.h + 100);
    expect(h.previews[h.previews.length - 1]).toEqual(commit);
  });

  it("情形四（五个把手 × 收紧的限制）：被拉的那条轴一律落在当下限制内，且不跳", () => {
    const cases: { handle: "se" | "nw" | "e" | "w" | "s"; axes: ("w" | "h")[] }[] = [
      { handle: "se", axes: ["w", "h"] },
      { handle: "nw", axes: ["w", "h"] },
      { handle: "e", axes: ["w"] },
      { handle: "w", axes: ["w"] },
      { handle: "s", axes: ["h"] },
    ];
    const snug = sizeLimits(120, 80, 260, 200);
    for (const item of cases) {
      const h = harness({ limits: () => snug });
      h.api.onHandleDown(item.handle, pe("pointerdown", 0, 0));
      document.dispatchEvent(pe("pointermove", 400, 400));
      document.dispatchEvent(pe("pointerup", 400, 400));
      expect(h.commits).toHaveLength(1);
      const commit = h.commits[0].box;
      if (item.axes.indexOf("w") >= 0) {
        expect(commit.w).toBeGreaterThanOrEqual(snug.minW);
        expect(commit.w).toBeLessThanOrEqual(snug.maxW);
      }
      if (item.axes.indexOf("h") >= 0) {
        expect(commit.h).toBeGreaterThanOrEqual(snug.minH);
        expect(commit.h).toBeLessThanOrEqual(snug.maxH);
      }
      expect(h.previews[h.previews.length - 1]).toEqual(commit);
    }
  });

  it("现状（R4）：限制/缩放没变时，一次指针提交仍会多推一次预览（差异守卫失效）", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 40, 40));
    const before = h.previews.length;
    document.dispatchEvent(pe("pointerup", 40, 40));
    expect(h.previews).toHaveLength(before + 1);
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box).toEqual(h.previews[h.previews.length - 1]);
  });

  it.fails("R4（守卫应当生效）：复算结果与最后一次预览相同时不该再多推一次 onPreview", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 40, 40));
    const before = h.previews.length;
    document.dispatchEvent(pe("pointerup", 40, 40));
    expect(h.previews).toHaveLength(before);
  });

  it("拖动中限制收紧到比原框还小、又拖回原点：提交被修正后的框（不是空操作）", () => {
    const h = harness({ limits: () => sizeLimits(400, 300, 640, 480) });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 300, 200));
    document.dispatchEvent(pe("pointermove", 0, 0));
    document.dispatchEvent(pe("pointerup", 0, 0));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.w).toBe(400);
    expect(h.commits[0].box.h).toBe(300);
    expect(h.previews[h.previews.length - 1]).toEqual(h.commits[0].box);
  });
});

describe("NOOP_EPSILON：空操作判定边界", () => {
  it("0.005 / 0.01 不提交，0.011 / 0.5 / 8 提交", () => {
    const cases: { scale: number; committed: boolean }[] = [
      { scale: 0.0025, committed: false },
      { scale: 0.005, committed: false },
      { scale: 0.0055, committed: true },
      { scale: 0.25, committed: true },
      { scale: 4, committed: true },
    ];
    for (const item of cases) {
      const h = harness({ scale: () => item.scale });
      h.api.onHandleDown("se", pe("pointerdown", 0, 0));
      document.dispatchEvent(pe("pointermove", 2, 0));
      document.dispatchEvent(pe("pointerup", 2, 0));
      expect(h.commits.length).toBe(item.committed ? 1 : 0);
      if (item.committed) expect(h.commits[0].box.w).toBeCloseTo(BASE.w + 2 * item.scale, 6);
    }
  });

  it("F2 分界矩阵的真实场景：zoom 4（系数 0.25）下 2px = 0.5 板面单位仍然提交（旧的 0.5 容差会吞掉它）", () => {
    const h = harness({ scale: () => 0.25 });
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 2, 0));
    document.dispatchEvent(pe("pointerup", 2, 0));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.w).toBe(BASE.w + 0.5);
  });

  it("高度方向同样按 0.01 判定（不是只看宽度）", () => {
    const noop = harness({ scale: () => 0.005 });
    noop.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 0, 2));
    document.dispatchEvent(pe("pointerup", 0, 2));
    expect(noop.commits).toHaveLength(0);

    const moved = harness({ scale: () => 0.0055 });
    moved.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 0, 2));
    document.dispatchEvent(pe("pointerup", 0, 2));
    expect(moved.commits).toHaveLength(1);
    expect(moved.commits[0].box.h).toBeCloseTo(BASE.h + 0.011, 6);
  });

  it("拖出去再拖回来：一次都不提交，也不多推预览（显示停在拖动前那一版）", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 300, 200));
    document.dispatchEvent(pe("pointermove", 0, 0));
    const before = h.previews.length;
    document.dispatchEvent(pe("pointerup", 0, 0));
    expect(h.commits).toHaveLength(0);
    expect(h.previews).toHaveLength(before);
    expect(h.previews[h.previews.length - 1]).toEqual(BASE);
  });

  it("行为说明：提交用最后一次 move 的位移，不使用 pointerup 自带的坐标", () => {
    const h = harness();
    h.api.onHandleDown("se", pe("pointerdown", 0, 0));
    document.dispatchEvent(pe("pointermove", 100, 0));
    document.dispatchEvent(pe("pointerup", 0, 0));
    expect(h.commits).toHaveLength(1);
    expect(h.commits[0].box.w).toBe(BASE.w + 100);
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
