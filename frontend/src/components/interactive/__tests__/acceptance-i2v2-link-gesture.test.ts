/**
 * D 组独立验收 · F09：关系胶囊拖动（缩放换算与取消清理）——组件级。
 *
 * 用户可见契约（任务书 F09）：
 * - 50% / 100% / 200% 缩放下拖动关系胶囊都跟手：屏幕位移必须按画布缩放换算成板面位移；
 * - 拖动中只改预览，正常松手才提交一次；
 * - Esc / pointercancel / 窗口失焦 / 组件卸载都要取消并恢复起始曲线、清理全局监听；
 *   取消后迟到的 pointerup 不得提交；
 * - 其它指针的松手不能结束本次拖动（pointerId 约束）。
 *
 * 基线问题：BoardLinkLayer 没有 scale prop，直接用未换算的 clientX 差值；
 * 只监听 pointermove / pointerup，没有取消路径（Esc / pointercancel / blur / 卸载都不清理）。
 */
import { afterEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import BoardLinkLayer from "../BoardLinkLayer.vue";
import type { BoardCard, BoardLink } from "../../../interactive/types";

const STAMP = "2026-10-25T00:00:00.000Z";

function mkCard(id: string, x: number, y: number): BoardCard {
  return {
    id,
    kind: "text",
    content: "卡片 " + id,
    meta: {},
    x,
    y,
    w: 120,
    h: 80,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

const cards = [mkCard("a", 100, 100), mkCard("b", 400, 100)];
const link: BoardLink = {
  id: "l1",
  src: "a",
  dst: "b",
  direction: false,
  meaning: "属于",
  curve: { x: 20, y: 10 },
  deleted: false,
  createdAt: STAMP,
  updatedAt: STAMP,
};

const baseProps = {
  cards,
  activeLinkId: null,
  width: 2000,
  height: 1600,
  draftFromId: null,
  draftPoint: null,
  draftTargetId: null,
};

let wrapper: VueWrapper | null = null;

afterEach(() => {
  try {
    wrapper?.unmount();
  } catch {
    /* 已卸载 */
  }
  wrapper = null;
  document.body.innerHTML = "";
});

function mountLayer(extra: Record<string, unknown> = {}): VueWrapper {
  wrapper = mount(BoardLinkLayer, { props: { ...baseProps, links: [link], ...extra } });
  return wrapper;
}

function pointerEvent(type: string, x: number, y: number, pointerId = 1): MouseEvent {
  const event = new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true, cancelable: true });
  Object.defineProperty(event, "pointerId", { value: pointerId, configurable: true });
  return event;
}

function linePath(frame: VueWrapper): string {
  return String(frame.get('[data-link-id="l1"] path.line').attributes("d") ?? "");
}

function pressLabel(frame: VueWrapper, x = 100, y = 100, pointerId = 1): void {
  frame.get('[data-link-label="l1"] .label-bg').element.dispatchEvent(pointerEvent("pointerdown", x, y, pointerId));
}

function moveTo(x: number, y: number, pointerId = 1): void {
  window.dispatchEvent(pointerEvent("pointermove", x, y, pointerId));
}

function releaseAt(x: number, y: number, pointerId = 1): void {
  window.dispatchEvent(pointerEvent("pointerup", x, y, pointerId));
}

function emittedCurve(frame: VueWrapper): unknown {
  const list = frame.emitted("curve-change") as unknown[][] | undefined;
  return list && list.length ? list[list.length - 1] : undefined;
}

describe("F09 缩放换算：屏幕位移按画布 scale 换成板面位移", () => {
  // 校准（集成后真实实现）：curve 是「起始曲线 + 板面位移」，起始曲线 {x:20,y:10}。
  it.each([
    [1, 80, { x: 100, y: 50 }],
    [2, 80, { x: 60, y: 30 }],
    [0.5, 40, { x: 100, y: 50 }],
  ])("scale=%s 时屏幕位移 %spx 提交的曲线偏移正确", (scale, dx, expected) => {
    const frame = mountLayer({ scale });
    pressLabel(frame);
    moveTo(100 + dx, 100 + (dx * 40) / 80);
    releaseAt(100 + dx, 100 + (dx * 40) / 80);
    expect(emittedCurve(frame), "拖动必须按真实缩放换算，否则缩放后不跟手").toEqual(["l1", expected]);
  });

  it("非法 scale（0 / NaN）按 1 处理，不产生 NaN / Infinity 曲线", () => {
    const frame = mountLayer({ scale: Number.NaN });
    pressLabel(frame);
    moveTo(140, 130);
    releaseAt(140, 130);
    const emitted = emittedCurve(frame) as unknown[] | undefined;
    expect(emitted).toBeTruthy();
    const curve = emitted![1] as { x: number; y: number };
    expect(Number.isFinite(curve.x)).toBe(true);
    expect(Number.isFinite(curve.y)).toBe(true);
    expect(curve).toEqual({ x: 60, y: 40 });
  });
});

describe("F09 取消路径：Esc / pointercancel / 失焦 / 卸载都不提交并恢复起始曲线", () => {
  it("Esc 取消：曲线回到拖动前，之后松手也不提交", async () => {
    const frame = mountLayer();
    const before = linePath(frame);
    pressLabel(frame);
    moveTo(180, 140);
    await frame.vm.$nextTick();
    expect(linePath(frame), "拖动中只有预览").not.toBe(before);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await frame.vm.$nextTick();

    expect(linePath(frame), "Esc 后必须恢复起始曲线").toBe(before);
    releaseAt(180, 140);
    expect(emittedCurve(frame), "取消后迟到的 pointerup 不得提交").toBeUndefined();
  });

  it("pointercancel 取消：恢复起始曲线，之后松手不提交", () => {
    const frame = mountLayer();
    const before = linePath(frame);
    pressLabel(frame);
    moveTo(170, 130);
    window.dispatchEvent(pointerEvent("pointercancel", 170, 130));
    expect(linePath(frame)).toBe(before);
    releaseAt(170, 130);
    expect(emittedCurve(frame)).toBeUndefined();
  });

  it("窗口失焦：取消拖动并恢复起始曲线，不留下半次修改", () => {
    const frame = mountLayer();
    const before = linePath(frame);
    pressLabel(frame);
    moveTo(160, 120);
    window.dispatchEvent(new Event("blur"));
    expect(linePath(frame), "失焦必须取消拖动").toBe(before);
    releaseAt(160, 120);
    expect(emittedCurve(frame)).toBeUndefined();
  });

  it("组件卸载：清掉全局监听，卸载后的 pointerup 不得提交", () => {
    const frame = mountLayer();
    pressLabel(frame);
    moveTo(160, 120);
    frame.unmount();
    wrapper = null;
    expect(() => releaseAt(160, 120)).not.toThrow();
    expect(emittedCurve(frame), "卸载后不得再向已销毁组件提交").toBeUndefined();
  });
});

describe("F09 指针身份：别的指针松手不能结束这次拖动", () => {
  it("pointerId 不匹配的 pointerup 不提交；匹配的松手才提交一次", () => {
    const frame = mountLayer();
    pressLabel(frame, 100, 100, 1);
    moveTo(150, 120, 1);
    releaseAt(150, 120, 2);
    expect(emittedCurve(frame), "另一个指针的松手不能结束本次拖动").toBeUndefined();
    releaseAt(150, 120, 1);
    expect(emittedCurve(frame)).toEqual(["l1", { x: 70, y: 30 }]);
  });
});
