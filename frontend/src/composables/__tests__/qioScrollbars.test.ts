import { describe, expect, it, afterEach } from "vitest";
import {
  createQioScrollbars,
  QIO_SCROLL_RANGE_ATTR,
  QIO_SCROLL_RAIL_CLASS,
  QIO_SCROLL_THUMB_CLASS,
  type QioScrollbars,
} from "../useQioScrollbars";
import { setScrollbarPreference, applyScrollbarPreference } from "../../utils/scrollIndicator";

/**
 * 共享滚动指示条引擎的行为测试（jsdom 没有布局引擎，这里显式注入尺寸，
 * 验的是「溢出判定 / 策略 / 显隐 / 拖动 / 清理」这些真实逻辑，
 * 真实像素级布局由浏览器验收阶段覆盖）。
 */
const mounted: { el: HTMLElement; ctrl: QioScrollbars }[] = [];

function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 40));
}

function makeHost(metrics: {
  clientHeight: number;
  scrollHeight: number;
  clientWidth?: number;
  scrollWidth?: number;
}): HTMLElement {
  const el = document.createElement("div");
  document.body.appendChild(el);
  setMetrics(el, metrics);
  el.innerHTML = "<div>content</div>";
  return el;
}

function setMetrics(
  el: HTMLElement,
  metrics: { clientHeight: number; scrollHeight: number; clientWidth?: number; scrollWidth?: number },
): void {
  const values: Record<string, number> = {
    clientHeight: metrics.clientHeight,
    scrollHeight: metrics.scrollHeight,
    clientWidth: metrics.clientWidth ?? 400,
    scrollWidth: metrics.scrollWidth ?? 400,
  };
  for (const [key, value] of Object.entries(values)) {
    Object.defineProperty(el, key, { configurable: true, get: () => value });
  }
  el.scrollTop = 0;
  el.scrollLeft = 0;
}

function rail(el: HTMLElement, axis: "x" | "y"): HTMLElement | null {
  return el.querySelector(`.qio-scroll-rail[data-axis="${axis}"]`);
}

afterEach(() => {
  for (const item of mounted.splice(0)) {
    item.ctrl.destroy();
    item.el.remove();
  }
  applyScrollbarPreference("auto");
});

describe("QIO 滚动指示条引擎", () => {
  it("没有实际溢出时不创建指示条，也不留轨道空白", async () => {
    const el = makeHost({ clientHeight: 400, scrollHeight: 400 });
    const ctrl = createQioScrollbars(el, { policy: "auto" });
    mounted.push({ el, ctrl });
    await flush();
    expect(el.getAttribute(QIO_SCROLL_RANGE_ATTR)).toBe("none");
    expect(rail(el, "y")).toBeNull();
  });

  it("实际溢出时创建指示条：带范围语义、不进入 Tab 序列、默认不可见", async () => {
    const el = makeHost({ clientHeight: 200, scrollHeight: 1000 });
    const ctrl = createQioScrollbars(el, { policy: "auto" });
    mounted.push({ el, ctrl });
    await flush();
    expect(el.getAttribute(QIO_SCROLL_RANGE_ATTR)).toBe("y");
    const r = rail(el, "y");
    expect(r).not.toBeNull();
    expect(r!.getAttribute("role")).toBe("scrollbar");
    expect(r!.getAttribute("aria-orientation")).toBe("vertical");
    expect(r!.getAttribute("aria-controls")).toBe(el.id);
    expect(r!.getAttribute("aria-valuenow")).toBe("0");
    expect(r!.tabIndex).toBe(-1);
    // 不滚动时保持隐藏：不可见就没有可交互覆盖层
    expect(r!.classList.contains("is-visible")).toBe(false);
    const thumb = r!.querySelector("." + QIO_SCROLL_THUMB_CLASS) as HTMLElement;
    // jsdom 没有布局：轨道长度读出来是 0，因此走最小可抓取长度（minThumb）下限。
    // 真实比例另由浏览器验收阶段测量（track * view/content）。
    expect(thumb.style.getPropertyValue("--qio-thumb-size")).toBe("24px");
    expect(r!.getAttribute("aria-valuenow")).toBe("0");
  });

  it("滚动时按需显示，停止滚动后淡出", async () => {
    const el = makeHost({ clientHeight: 200, scrollHeight: 1000 });
    const ctrl = createQioScrollbars(el, { policy: "auto", lingerMs: 200 });
    mounted.push({ el, ctrl });
    await flush();
    el.dispatchEvent(new Event("scroll"));
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(rail(el, "y")!.classList.contains("is-visible")).toBe(true);
    // 停止滚动后短暂保留，然后淡出（不是永久驻留）
    await new Promise((resolve) => setTimeout(resolve, 260));
    expect(rail(el, "y")!.classList.contains("is-visible")).toBe(false);
  });

  it("「始终显示」只作用于允许显示且实际溢出的区域", async () => {
    setScrollbarPreference("always");
    const overflowing = makeHost({ clientHeight: 200, scrollHeight: 900 });
    const fits = makeHost({ clientHeight: 300, scrollHeight: 300 });
    const c1 = createQioScrollbars(overflowing, { policy: "auto" });
    const c2 = createQioScrollbars(fits, { policy: "auto" });
    mounted.push({ el: overflowing, ctrl: c1 }, { el: fits, ctrl: c2 });
    await flush();
    const r = rail(overflowing, "y")!;
    expect(r.classList.contains("is-visible")).toBe(true);
    expect(r.classList.contains("is-persistent")).toBe(true);
    // 不溢出的区域仍然什么都没有
    expect(rail(fits, "y")).toBeNull();
  });

  it("hidden 策略：保留滚动与定位能力，但始终不画指示条", async () => {
    const el = makeHost({ clientHeight: 200, scrollHeight: 1000 });
    const ctrl = createQioScrollbars(el, { policy: "hidden" });
    mounted.push({ el, ctrl });
    await flush();
    expect(rail(el, "y")).toBeNull();
    expect(el.getAttribute(QIO_SCROLL_RANGE_ATTR)).toBe("none");
    expect(el.dataset.qioScroll).toBe("hidden");
    // 滚动能力没有被夺走
    el.scrollTop = 120;
    expect(el.scrollTop).toBe(120);
  });

  it("滚动后指示条仍贴在可视边缘（滚动容器内的 absolute 子节点会被内容带走）", async () => {
    const el = makeHost({ clientHeight: 200, scrollHeight: 1000 });
    const ctrl = createQioScrollbars(el, { policy: "auto" });
    mounted.push({ el, ctrl });
    await flush();
    expect(rail(el, "y")!.style.transform).toBe("translate(0px,0px)");
    el.scrollTop = 150;
    el.scrollLeft = 0;
    el.dispatchEvent(new Event("scroll"));
    // 反向平移量与滚动偏移一致：top 从 -150 被拉回 0，指示条不会滚出视口
    expect(rail(el, "y")!.style.transform).toBe("translate(0px,150px)");
    await flush();
    expect(rail(el, "y")!.style.transform).toBe("translate(0px,150px)");
  });

  it("内容缩短/容器变大后指示条及时消失", async () => {
    const el = makeHost({ clientHeight: 200, scrollHeight: 1000 });
    const ctrl = createQioScrollbars(el, { policy: "auto" });
    mounted.push({ el, ctrl });
    await flush();
    expect(rail(el, "y")).not.toBeNull();
    setMetrics(el, { clientHeight: 400, scrollHeight: 400 });
    ctrl.refresh();
    await flush();
    expect(el.getAttribute(QIO_SCROLL_RANGE_ATTR)).toBe("none");
    expect(rail(el, "y")!.classList.contains("is-visible")).toBe(false);
  });

  it("滑块拖动与卡片拖动/连线/板面平移隔离（同一个指针操作不触发第二种）", async () => {
    const el = makeHost({ clientHeight: 200, scrollHeight: 1000 });
    const ctrl = createQioScrollbars(el, { policy: "auto" });
    mounted.push({ el, ctrl });
    await flush();
    el.dispatchEvent(new Event("scroll"));
    await flush();
    const r = rail(el, "y")!;
    expect(r.classList.contains("is-visible")).toBe(true);

    let hostPointerDown = 0;
    el.addEventListener("pointerdown", () => {
      hostPointerDown += 1;
    });

    const thumb = r.querySelector("." + QIO_SCROLL_THUMB_CLASS) as HTMLElement;
    const down = new Event("pointerdown", { bubbles: true, cancelable: true });
    Object.assign(down, { button: 0, pointerType: "mouse", pointerId: 1, clientY: 0 });
    thumb.dispatchEvent(down);
    expect(hostPointerDown).toBe(0);

    const move = new Event("pointermove", { bubbles: true, cancelable: true });
    Object.assign(move, { pointerId: 1, clientY: 160 });
    thumb.dispatchEvent(move);
    expect(el.scrollTop).toBeGreaterThan(0);

    const up = new Event("pointerup", { bubbles: true, cancelable: true });
    Object.assign(up, { pointerId: 1 });
    thumb.dispatchEvent(up);
    expect(el.classList.contains("is-qio-scroll-dragging")).toBe(false);
  });

  it("零尺寸宿主（折叠/隐藏）不算可滚动范围，也不建轨道", async () => {
    // 收起状态的话题栏就是这种形态：clientWidth=0，内容宽度仍算进 scrollWidth
    const el = makeHost({ clientHeight: 100, scrollHeight: 100, clientWidth: 0, scrollWidth: 76 });
    const ctrl = createQioScrollbars(el, { policy: "auto" });
    mounted.push({ el, ctrl });
    await flush();
    expect(el.getAttribute(QIO_SCROLL_RANGE_ATTR)).toBe("none");
    expect(rail(el, "x")).toBeNull();
    expect(rail(el, "y")).toBeNull();
  });

  it("destroy 后不残留指示条、属性与观察器", async () => {
    const el = makeHost({ clientHeight: 200, scrollHeight: 1000 });
    const ctrl = createQioScrollbars(el, { policy: "auto" });
    await flush();
    expect(el.querySelector("." + QIO_SCROLL_RAIL_CLASS)).not.toBeNull();
    ctrl.destroy();
    expect(el.querySelector("." + QIO_SCROLL_RAIL_CLASS)).toBeNull();
    expect(el.hasAttribute(QIO_SCROLL_RANGE_ATTR)).toBe(false);
    expect(el.dataset.qioScroll).toBeUndefined();
    expect(el.classList.contains("qio-scroll-host")).toBe(false);
  });
});
