/**
 * I2V2-C · F09 关系胶囊拖动（缩放换算 / 一次提交 / 取消 / 指针身份 / 既有交互回归）。
 *
 * 用户在板面上拖动关系胶囊（含义标签）调整曲线：
 * 1. 位移必须按真实坐标变换换算：板面位移 = 屏幕位移 / scale（50% / 100% / 200% 都跟手）；
 * 2. 拖动中只更新本层临时预览，正常松手恰好提交一次（父层一次 commit = 撤销一次即可恢复）；
 * 3. Esc / pointercancel / 窗口失焦 / 组件卸载必须取消本次拖动、恢复起始曲线、清理全局监听，
 *    取消后迟到的 pointerup 不能再提交；
 * 4. 手势用当前有效 pointerId 约束：其他指针的移动 / 松手不能改变或结束本次拖动；
 * 5. 单击选中、双击编辑、圆形删除按钮各自行为保留，拖动结束不误触编辑 / 删除；
 * 6. 父层回写（提交 / 撤销 / 重做 / 保存恢复）后显示跟随传入数据，端点身份始终不变。
 *
 * 这些用例在基线代码上会失败（未除 scale、无取消通道、不校验 pointerId、双击未被防误触拦截）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import BoardLinkLayer from "../BoardLinkLayer.vue";
import type { BoardCard, BoardGroup, BoardLink, LinkCurve } from "../../../interactive/types";

const STAMP = "2026-10-16T00:00:00.000Z";

function mkCard(id: string, x: number, y: number, patch: Partial<BoardCard> = {}): BoardCard {
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
    ...patch,
  };
}

function mkLink(id: string, src: string, dst: string, patch: Partial<BoardLink> = {}): BoardLink {
  return {
    id,
    src,
    dst,
    direction: false,
    meaning: "用户写的关系",
    deleted: false,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...patch,
  };
}

interface LayerProps {
  cards: BoardCard[];
  links: BoardLink[];
  activeLinkId: string | null;
  width: number;
  height: number;
  draftFromId: string | null;
  draftPoint: { x: number; y: number } | null;
  draftTargetId: string | null;
  groups?: BoardGroup[];
  scale?: number;
}

function layerProps(patch: Partial<LayerProps> = {}): LayerProps {
  return {
    cards: [mkCard("a", 100, 100), mkCard("b", 400, 100), mkCard("c", 100, 400)],
    links: [mkLink("l1", "a", "b")],
    activeLinkId: null,
    width: 2000,
    height: 2000,
    draftFromId: null,
    draftPoint: null,
    draftTargetId: null,
    ...patch,
  };
}

/** a 右边缘 + 外距 = 226；b 左边缘 - 外距 = 394；标签正好在 (310, 140)。 */
const LABEL_X = 310;
const LABEL_Y = 140;

interface PointerInit {
  pointerId?: number;
  clientX?: number;
  clientY?: number;
  button?: number;
  pointerType?: string;
}

function pointerEvent(type: string, init: PointerInit = {}): PointerEvent {
  return new PointerEvent(type, {
    bubbles: true,
    cancelable: true,
    pointerId: init.pointerId ?? 1,
    clientX: init.clientX ?? 0,
    clientY: init.clientY ?? 0,
    button: init.button ?? 0,
    pointerType: init.pointerType ?? "mouse",
  });
}

function winPointer(type: string, init: PointerInit = {}): void {
  window.dispatchEvent(pointerEvent(type, init));
}

const wrappers: VueWrapper[] = [];

function mountLayer(patch: Partial<LayerProps> = {}): VueWrapper {
  const wrapper = mount(BoardLinkLayer, { props: layerProps(patch) });
  wrappers.push(wrapper);
  return wrapper;
}

/** 读当前传入的 links：VTU 默认 VueWrapper 泛型会把 props 键收窄成 never，这里收一处。 */
function linksProp(wrapper: VueWrapper): BoardLink[] {
  return (wrapper.vm.$props as unknown as { links: BoardLink[] }).links;
}

function labelBackground(wrapper: VueWrapper): Element {
  return wrapper.get('[data-link-label="l1"] .label-bg').element;
}

function startDrag(wrapper: VueWrapper, init: PointerInit = {}): void {
  labelBackground(wrapper).dispatchEvent(
    pointerEvent("pointerdown", {
      pointerId: init.pointerId ?? 1,
      clientX: init.clientX ?? LABEL_X,
      clientY: init.clientY ?? LABEL_Y,
      button: init.button ?? 0,
      pointerType: init.pointerType ?? "mouse",
    }),
  );
}

function numbersOf(path: string): number[] {
  return (path.match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number);
}

/** 三次贝塞尔路径 "M x1 y1 C c1x c1y c2x c2y x2 y2" 的第一个控制点（曲线偏移直接加在它上面）。 */
function controlPoint(wrapper: VueWrapper, linkId = "l1"): { x: number; y: number } {
  const d = String(wrapper.get('[data-link-id="' + linkId + '"] path.line').attributes("d") ?? "");
  const numbers = numbersOf(d);
  if (numbers.length < 8) throw new Error("曲线路径数字不足：" + d);
  return { x: numbers[2], y: numbers[3] };
}

function dragBy(wrapper: VueWrapper, screenDx: number, screenDy: number, pointerId = 1): void {
  winPointer("pointermove", {
    pointerId,
    clientX: LABEL_X + screenDx,
    clientY: LABEL_Y + screenDy,
  });
}

async function settle(wrapper: VueWrapper): Promise<void> {
  await wrapper.vm.$nextTick();
}

afterEach(() => {
  while (wrappers.length) {
    const wrapper = wrappers.pop();
    try {
      wrapper?.unmount();
    } catch {
      // 已经卸载过（卸载取消用例）
    }
  }
  vi.restoreAllMocks();
});

describe("缩放换算：板面位移 = 屏幕位移 / scale", () => {
  it.each([
    { scale: 0.5, expected: { x: 200, y: -100 } },
    { scale: 1, expected: { x: 100, y: -50 } },
    { scale: 2, expected: { x: 50, y: -25 } },
  ])("scale=$scale 时相同屏幕位移 (100,-50) 的板面位移与提交曲线是 $expected", async ({ scale, expected }) => {
    const wrapper = mountLayer({ scale });
    const origin = controlPoint(wrapper);

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    await settle(wrapper);

    // 拖动中的临时预览也已按缩放换算，不是只在松手时折算
    expect(controlPoint(wrapper).x - origin.x).toBeCloseTo(expected.x);
    expect(controlPoint(wrapper).y - origin.y).toBeCloseTo(expected.y);
    expect(wrapper.emitted("curve-change")).toBeUndefined();

    winPointer("pointerup", { clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);

    expect(wrapper.emitted("curve-change")).toHaveLength(1);
    expect(wrapper.emitted("curve-change")?.[0]).toEqual(["l1", expected]);
  });

  it.each([0, -2, Number.NaN, Number.POSITIVE_INFINITY])(
    "scale=%s 非正 / 非有限时按 1 换算（不放大也不缩小）",
    async (scale) => {
      const wrapper = mountLayer({ scale });
      const origin = controlPoint(wrapper);

      startDrag(wrapper);
      dragBy(wrapper, 100, -50);
      await settle(wrapper);
      expect(controlPoint(wrapper).x - origin.x).toBeCloseTo(100);
      expect(controlPoint(wrapper).y - origin.y).toBeCloseTo(-50);

      winPointer("pointerup", { clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
      await settle(wrapper);
      expect(wrapper.emitted("curve-change")?.[0]).toEqual(["l1", { x: 100, y: -50 }]);
    },
  );
});

describe("一次提交：拖动中只预览、松手提交一次", () => {
  it("拖动过程中不 emit、不改传入的 links；松手恰好 emit 一次 curve-change", async () => {
    const wrapper = mountLayer();
    const linksBefore = JSON.parse(JSON.stringify(linksProp(wrapper)));

    startDrag(wrapper);
    dragBy(wrapper, 120, 60);
    await settle(wrapper);
    dragBy(wrapper, 140, 80);
    await settle(wrapper);

    expect(wrapper.emitted("curve-change")).toBeUndefined();
    // 拖动只改本地预览，绝不原地改调用方数据（store 只有松手后的 commit）
    expect(JSON.parse(JSON.stringify(linksProp(wrapper)))).toEqual(linksBefore);

    winPointer("pointerup", { clientX: LABEL_X + 140, clientY: LABEL_Y + 80 });
    await settle(wrapper);

    expect(wrapper.emitted("curve-change")).toHaveLength(1);
    expect(wrapper.emitted("curve-change")?.[0]).toEqual(["l1", { x: 140, y: 80 }]);
  });

  it("松手后预览清空：父层回写前不残留、手势结束后也不会再提交", async () => {
    const wrapper = mountLayer();
    const origin = controlPoint(wrapper);

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    winPointer("pointerup", { clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);

    // 这个用例不模拟父层回写：预览必须清空，显示回到调用方数据
    expect(controlPoint(wrapper).x).toBeCloseTo(origin.x);
    expect(controlPoint(wrapper).y).toBeCloseTo(origin.y);

    // 手势已结束：后续无关移动 / 松手不能再提交
    winPointer("pointermove", { clientX: LABEL_X + 400, clientY: LABEL_Y - 400 });
    winPointer("pointerup", { clientX: LABEL_X + 400, clientY: LABEL_Y - 400 });
    await settle(wrapper);
    expect(wrapper.emitted("curve-change")).toHaveLength(1);
  });
});

describe("取消：Esc / pointercancel / 窗口失焦 / 卸载", () => {
  it("Esc 取消：恢复起始曲线、不提交，并清理全局监听", async () => {
    const wrapper = mountLayer();
    const origin = controlPoint(wrapper);

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    await settle(wrapper);
    expect(controlPoint(wrapper).x - origin.x).toBeCloseTo(100);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await settle(wrapper);

    expect(controlPoint(wrapper).x).toBeCloseTo(origin.x);
    expect(controlPoint(wrapper).y).toBeCloseTo(origin.y);
    expect(wrapper.emitted("curve-change")).toBeUndefined();
  });

  it("取消后迟到的 pointerup / pointermove 不能再提交或移动曲线", async () => {
    const wrapper = mountLayer();
    const origin = controlPoint(wrapper);

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await settle(wrapper);

    winPointer("pointermove", { clientX: LABEL_X + 300, clientY: LABEL_Y - 300 });
    winPointer("pointerup", { clientX: LABEL_X + 300, clientY: LABEL_Y - 300 });
    await settle(wrapper);

    expect(wrapper.emitted("curve-change")).toBeUndefined();
    expect(controlPoint(wrapper).x).toBeCloseTo(origin.x);
    expect(controlPoint(wrapper).y).toBeCloseTo(origin.y);
  });

  it("pointercancel 取消：恢复起始曲线、不提交", async () => {
    const wrapper = mountLayer();
    const origin = controlPoint(wrapper);

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    await settle(wrapper);

    winPointer("pointercancel", { clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);
    expect(wrapper.emitted("curve-change")).toBeUndefined();
    expect(controlPoint(wrapper).x).toBeCloseTo(origin.x);
    expect(controlPoint(wrapper).y).toBeCloseTo(origin.y);

    winPointer("pointerup", { clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);
    expect(wrapper.emitted("curve-change")).toBeUndefined();
  });

  it("窗口失焦取消：恢复起始曲线、不提交", async () => {
    const wrapper = mountLayer();
    const origin = controlPoint(wrapper);

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    await settle(wrapper);

    window.dispatchEvent(new Event("blur"));
    await settle(wrapper);

    expect(wrapper.emitted("curve-change")).toBeUndefined();
    expect(controlPoint(wrapper).x).toBeCloseTo(origin.x);
    expect(controlPoint(wrapper).y).toBeCloseTo(origin.y);
  });

  it("组件卸载时取消并清理全局监听，卸载后的事件不再触发提交", async () => {
    const wrapper = mountLayer();
    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    await settle(wrapper);

    const removeSpy = vi.spyOn(window, "removeEventListener");
    wrapper.unmount();
    // 全局监听必须成对移除（含 capture 标志）
    expect(removeSpy).toHaveBeenCalledWith("pointermove", expect.any(Function), true);
    expect(removeSpy).toHaveBeenCalledWith("pointerup", expect.any(Function), true);
    expect(removeSpy).toHaveBeenCalledWith("pointercancel", expect.any(Function), true);
    expect(removeSpy).toHaveBeenCalledWith("keydown", expect.any(Function), true);
    expect(removeSpy).toHaveBeenCalledWith("blur", expect.any(Function), true);

    expect(() => {
      winPointer("pointermove", { clientX: LABEL_X + 200, clientY: LABEL_Y - 200 });
      winPointer("pointerup", { clientX: LABEL_X + 200, clientY: LABEL_Y - 200 });
    }).not.toThrow();
    expect(wrapper.emitted("curve-change")).toBeUndefined();
  });
});

describe("指针身份：pointerId 约束手势", () => {
  it("其他指针的移动 / 松手不能改变曲线、也不能结束本次手势", async () => {
    const wrapper = mountLayer();
    const origin = controlPoint(wrapper);

    startDrag(wrapper, { pointerId: 7 });
    winPointer("pointermove", { pointerId: 9, clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);

    // 无关指针移动：预览不动
    expect(controlPoint(wrapper).x).toBeCloseTo(origin.x);
    expect(controlPoint(wrapper).y).toBeCloseTo(origin.y);

    // 无关指针松手：不提交、手势仍在
    winPointer("pointerup", { pointerId: 9, clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);
    expect(wrapper.emitted("curve-change")).toBeUndefined();

    winPointer("pointermove", { pointerId: 7, clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);
    expect(controlPoint(wrapper).x - origin.x).toBeCloseTo(100);
    expect(controlPoint(wrapper).y - origin.y).toBeCloseTo(-50);

    winPointer("pointerup", { pointerId: 7, clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);
    expect(wrapper.emitted("curve-change")).toHaveLength(1);
    expect(wrapper.emitted("curve-change")?.[0]).toEqual(["l1", { x: 100, y: -50 }]);
  });

  it("拖动中第二个指针按下不会重置起点（曲线仍按第一次按下的起点换算）", async () => {
    const wrapper = mountLayer();
    const origin = controlPoint(wrapper);

    startDrag(wrapper, { pointerId: 7 });
    winPointer("pointermove", { pointerId: 7, clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);

    // 第二根手指 / 第二个指针在同一标签上按下
    labelBackground(wrapper).dispatchEvent(
      pointerEvent("pointerdown", { pointerId: 9, clientX: LABEL_X + 200, clientY: LABEL_Y - 200 }),
    );
    winPointer("pointermove", { pointerId: 7, clientX: LABEL_X + 50, clientY: LABEL_Y - 20 });
    await settle(wrapper);

    expect(controlPoint(wrapper).x - origin.x).toBeCloseTo(50);
    expect(controlPoint(wrapper).y - origin.y).toBeCloseTo(-20);

    winPointer("pointerup", { pointerId: 7, clientX: LABEL_X + 50, clientY: LABEL_Y - 20 });
    await settle(wrapper);
    expect(wrapper.emitted("curve-change")?.[0]).toEqual(["l1", { x: 50, y: -20 }]);
  });
});

describe("既有交互保留", () => {
  it("单击标签选中、双击标签编辑", async () => {
    const wrapper = mountLayer();
    const background = labelBackground(wrapper);

    background.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, clientX: 310, clientY: 140 }));
    background.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, cancelable: true, clientX: 310, clientY: 140 }));
    await settle(wrapper);

    expect(wrapper.emitted("select-link")?.[0]).toEqual(["l1", 310, 140]);
    expect(wrapper.emitted("edit-link")?.[0]).toEqual(["l1", 310, 140]);
    expect(wrapper.emitted("curve-change")).toBeUndefined();
  });

  it("没有超过抖动阈值的按下-松开仍算单击：选中照常、不提交曲线", async () => {
    const wrapper = mountLayer();
    const background = labelBackground(wrapper);

    background.dispatchEvent(pointerEvent("pointerdown", { clientX: LABEL_X, clientY: LABEL_Y }));
    winPointer("pointermove", { clientX: LABEL_X + 1, clientY: LABEL_Y + 1 });
    winPointer("pointerup", { clientX: LABEL_X + 1, clientY: LABEL_Y + 1 });
    background.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, clientX: LABEL_X + 1, clientY: LABEL_Y + 1 }),
    );
    await settle(wrapper);

    expect(wrapper.emitted("curve-change")).toBeUndefined();
    expect(wrapper.emitted("select-link")?.[0]).toEqual(["l1", LABEL_X + 1, LABEL_Y + 1]);
    expect(wrapper.emitted("edit-link")).toBeUndefined();
  });

  it("拖动结束不误触选中或编辑（只提交曲线）", async () => {
    const wrapper = mountLayer();

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    winPointer("pointerup", { clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);
    expect(wrapper.emitted("curve-change")).toHaveLength(1);

    // 浏览器在拖动结束后可能仍派发 click / dblclick：不能被当成选中或编辑
    const background = labelBackground(wrapper);
    background.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, clientX: LABEL_X + 100, clientY: LABEL_Y - 50 }));
    background.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, cancelable: true, clientX: LABEL_X + 100, clientY: LABEL_Y - 50 }));
    await settle(wrapper);

    expect(wrapper.emitted("select-link")).toBeUndefined();
    expect(wrapper.emitted("edit-link")).toBeUndefined();
    expect(wrapper.emitted("curve-change")).toHaveLength(1);
  });

  it("圆形删除按钮照常删除，且不触发拖动 / 选中 / 编辑", async () => {
    const wrapper = mountLayer();
    const deleteButton = wrapper.get('[data-link-label="l1"] [data-link-delete]');

    deleteButton.element.dispatchEvent(pointerEvent("pointerdown", { clientX: 358, clientY: 140 }));
    winPointer("pointermove", { clientX: 500, clientY: 300 });
    winPointer("pointerup", { clientX: 500, clientY: 300 });
    deleteButton.element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    await settle(wrapper);

    expect(wrapper.emitted("delete-link")?.[0]).toEqual(["l1"]);
    expect(wrapper.emitted("curve-change")).toBeUndefined();
    expect(wrapper.emitted("select-link")).toBeUndefined();
    expect(wrapper.emitted("edit-link")).toBeUndefined();
  });

  it("拖动刚结束时删除按钮仍然可用（不被防误触窗口挡住）", async () => {
    const wrapper = mountLayer();

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    winPointer("pointerup", { clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);

    wrapper
      .get('[data-link-label="l1"] [data-link-delete]')
      .element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    await settle(wrapper);

    expect(wrapper.emitted("delete-link")?.[0]).toEqual(["l1"]);
    expect(wrapper.emitted("select-link")).toBeUndefined();
  });
});

describe("父层回写与端点身份", () => {
  it("提交后父层回写曲线：显示跟随；撤销 / 重做一次即恢复；端点身份始终不变", async () => {
    const wrapper = mountLayer();
    const origin = controlPoint(wrapper);
    const originalLink = mkLink("l1", "a", "b");

    startDrag(wrapper);
    dragBy(wrapper, 90, -40);
    winPointer("pointerup", { clientX: LABEL_X + 90, clientY: LABEL_Y - 40 });
    await settle(wrapper);
    const committed = wrapper.emitted("curve-change")?.[0][1] as LinkCurve;
    expect(committed).toEqual({ x: 90, y: -40 });

    // 父层按一次 commit 回写（撤销栈里只有这一次）
    await wrapper.setProps({ links: [mkLink("l1", "a", "b", { curve: committed })] });
    await settle(wrapper);
    expect(controlPoint(wrapper).x - origin.x).toBeCloseTo(committed.x);
    expect(controlPoint(wrapper).y - origin.y).toBeCloseTo(committed.y);

    // 撤销一次：回到起始曲线
    await wrapper.setProps({ links: [originalLink] });
    await settle(wrapper);
    expect(controlPoint(wrapper).x).toBeCloseTo(origin.x);
    expect(controlPoint(wrapper).y).toBeCloseTo(origin.y);

    // 重做一次：曲线回来
    await wrapper.setProps({ links: [mkLink("l1", "a", "b", { curve: committed })] });
    await settle(wrapper);
    expect(controlPoint(wrapper).x - origin.x).toBeCloseTo(committed.x);

    // 全程端点身份不变
    const group = wrapper.get('[data-link-id="l1"]');
    expect(group.attributes("data-src")).toBe("a");
    expect(group.attributes("data-dst")).toBe("b");
    expect(group.attributes("data-src-kind")).toBe("card");
    expect(group.attributes("data-dst-kind")).toBe("card");
  });

  it("拖动中外部回写（撤销 / 保存恢复）取消本次拖动，不覆盖新数据", async () => {
    const wrapper = mountLayer();
    const origin = controlPoint(wrapper);

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    await settle(wrapper);

    // 外部（撤销 / 保存恢复）把这条关系的曲线换成另一个值
    await wrapper.setProps({ links: [mkLink("l1", "a", "b", { curve: { x: 10, y: 10 } })] });
    await settle(wrapper);

    // 预览不能盖住外部真相：显示的是外部回写的曲线，不是本次拖动的预览
    const external = controlPoint(wrapper);
    expect(external.x - origin.x).toBeCloseTo(10);
    expect(external.y - origin.y).toBeCloseTo(10);

    // 本次拖动已被取消：迟到的松手 / 移动都不能提交
    winPointer("pointermove", { clientX: LABEL_X + 200, clientY: LABEL_Y - 200 });
    winPointer("pointerup", { clientX: LABEL_X + 200, clientY: LABEL_Y - 200 });
    await settle(wrapper);
    expect(wrapper.emitted("curve-change")).toBeUndefined();
    expect(controlPoint(wrapper).x).toBeCloseTo(external.x);
    expect(controlPoint(wrapper).y).toBeCloseTo(external.y);
  });

  it("拖动中关系被删除（外部回写）取消拖动，不会给已删除的关系提交曲线", async () => {
    const wrapper = mountLayer();

    startDrag(wrapper);
    dragBy(wrapper, 100, -50);
    await settle(wrapper);

    await wrapper.setProps({ links: [mkLink("l1", "a", "b", { deleted: true })] });
    await settle(wrapper);
    expect(wrapper.find('[data-link-id="l1"]').exists()).toBe(false);

    winPointer("pointerup", { clientX: LABEL_X + 100, clientY: LABEL_Y - 50 });
    await settle(wrapper);
    expect(wrapper.emitted("curve-change")).toBeUndefined();
  });

  it("组端点关系拖曲线只改 curve，group:<id> 身份与原端点不变", async () => {
    const wrapper = mountLayer({
      cards: [mkCard("a", 100, 100), mkCard("b", 400, 100), mkCard("c", 100, 400)],
      groups: [
        {
          id: "g1",
          name: "发布计划",
          defaultName: false,
          ordered: false,
          x: 80,
          y: 40,
          w: 360,
          h: 160,
          members: ["a", "b"],
          deleted: false,
          createdAt: STAMP,
          updatedAt: STAMP,
        },
      ],
      links: [mkLink("l2", "group:g1", "c")],
    });
    const before = wrapper.get('[data-link-id="l2"]');
    const dataBefore = {
      src: before.attributes("data-src"),
      dst: before.attributes("data-dst"),
      srcKind: before.attributes("data-src-kind"),
      dstKind: before.attributes("data-dst-kind"),
    };

    const label = wrapper.get('[data-link-label="l2"] .label-bg').element;
    label.dispatchEvent(pointerEvent("pointerdown", { clientX: 200, clientY: 120, pointerId: 3 }));
    winPointer("pointermove", { pointerId: 3, clientX: 260, clientY: 90 });
    winPointer("pointerup", { pointerId: 3, clientX: 260, clientY: 90 });
    await settle(wrapper);

    expect(wrapper.emitted("curve-change")?.[0][0]).toBe("l2");
    const after = wrapper.get('[data-link-id="l2"]');
    expect({
      src: after.attributes("data-src"),
      dst: after.attributes("data-dst"),
      srcKind: after.attributes("data-src-kind"),
      dstKind: after.attributes("data-dst-kind"),
    }).toEqual(dataBefore);
    expect(dataAfterIsGroupEndpoint(dataBefore)).toBe(true);
  });
});

function dataAfterIsGroupEndpoint(data: Record<string, string | undefined>): boolean {
  return data.src === "group:g1" && data.srcKind === "group" && data.dst === "c" && data.dstKind === "card";
}
