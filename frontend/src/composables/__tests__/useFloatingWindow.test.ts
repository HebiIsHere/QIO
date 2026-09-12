import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { effectScope, nextTick, ref } from "vue";
import { useFloatingWindow, type UseFloatingWindowOptions } from "../useFloatingWindow";
import { floatingState, resetFloatPositions, FLOAT_HIDE_KEY, FLOAT_STORAGE_KEY } from "../floatingState";

/** 创建带几何尺寸的浮动元素；getBoundingClientRect 反映当前 style.left/top */
function makeEl(id: string, init: { left: number; top: number; width: number; height: number }): HTMLElement {
  const el = document.createElement("div");
  el.id = id;
  document.body.appendChild(el);
  Object.defineProperty(el, "offsetWidth", { value: init.width, configurable: true });
  Object.defineProperty(el, "offsetHeight", { value: init.height, configurable: true });
  const read = () => {
    const left = parseInt(el.style.left, 10) || init.left;
    const top = parseInt(el.style.top, 10) || init.top;
    return {
      left,
      top,
      width: init.width,
      height: init.height,
      right: left + init.width,
      bottom: top + init.height,
      x: left,
      y: top,
      toJSON: () => ({}),
    };
  };
  el.getBoundingClientRect = read as typeof el.getBoundingClientRect;
  return el;
}

interface Mounted {
  el: HTMLElement;
  api: ReturnType<typeof useFloatingWindow>;
  dispose(): void;
}

async function mountFloat(el: HTMLElement, options: UseFloatingWindowOptions): Promise<Mounted> {
  const elRef = ref<HTMLElement | null>(el);
  const scope = effectScope();
  let api!: ReturnType<typeof useFloatingWindow>;
  scope.run(() => {
    api = useFloatingWindow(elRef, options);
  });
  await nextTick();
  return {
    el,
    api,
    dispose() {
      scope.stop();
      el.remove();
    },
  };
}

function mouse(type: string, x: number, y: number): MouseEvent {
  return new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, button: 0 });
}

const composerOpts = (overrides: Partial<UseFloatingWindowOptions> = {}): UseFloatingWindowOptions => ({
  id: "composer",
  dockMode: "edge",
  defaultPos: { x: 100, y: 100 },
  ...overrides,
});

beforeEach(() => {
  localStorage.clear();
  resetFloatPositions();
  vi.useRealTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("useFloatingWindow 初始化与位置恢复", () => {
  it("初始化：应用 defaultPos，像素 left/top + position fixed，并写入共享状态", async () => {
    const el = makeEl("composer", { left: 0, top: 0, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 120, y: 340 } }));
    expect(el.style.position).toBe("fixed");
    expect(el.style.left).toBe("120px");
    expect(el.style.top).toBe("340px");
    expect(floatingState.composer.x).toBe(120);
    expect(floatingState.composer.y).toBe(340);
    expect(floatingState.composer.width).toBe(200);
    expect(floatingState.composer.docked).toBe(false);
    dispose();
  });

  it("初始化：defaultPos 为函数时按元素尺寸/视口计算", async () => {
    const el = makeEl("planet-dock", { left: 0, top: 0, width: 96, height: 96 });
    const { dispose } = await mountFloat(el, {
      id: "planet-dock",
      dockMode: "edge",
      defaultPos: (e, vp) => ({ x: vp.width - 26 - e.offsetWidth, y: Math.round((vp.height - e.offsetHeight) / 2) }),
    });
    expect(el.style.left).toBe(`${1024 - 26 - 96}px`);
    expect(el.style.top).toBe(`${Math.round((768 - 96) / 2)}px`);
    dispose();
  });

  it("初始化：越界 defaultPos 被钳制在视口内", async () => {
    const el = makeEl("composer", { left: 0, top: 0, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 5000, y: -50 } }));
    expect(el.style.left).toBe("820px"); // 1024-200-4
    expect(el.style.top).toBe("4px");
    dispose();
  });

  it("恢复：localStorage 有持久化位置时优先于 defaultPos", async () => {
    localStorage.setItem(
      FLOAT_STORAGE_KEY,
      JSON.stringify({ composer: { x: 300, y: 500, docked: true, dockedTo: "bottom", hideEnabled: true } }),
    );
    const el = makeEl("composer", { left: 0, top: 0, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 100, y: 100 } }));
    expect(el.style.left).toBe("300px");
    expect(el.style.top).toBe("500px");
    expect(floatingState.composer.docked).toBe(true);
    expect(floatingState.composer.dockedTo).toBe("bottom");
    expect(floatingState.composer.hideEnabled).toBe(true);
    expect(el.dataset.fwDocked).toBe("1");
    expect(el.dataset.fwTarget).toBe("bottom");
    dispose();
  });
});

describe("useFloatingWindow 拖拽", () => {
  it("拖动：位移 >3px 判定为拖动，元素按像素移动", async () => {
    const el = makeEl("composer", { left: 100, top: 100, width: 200, height: 80 });
    const { api, dispose } = await mountFloat(el, composerOpts());
    el.dispatchEvent(mouse("mousedown", 150, 130));
    document.dispatchEvent(mouse("mousemove", 200, 180));
    expect(api.moved.value).toBe(true);
    expect(el.style.left).toBe("150px");
    expect(el.style.top).toBe("150px");
    document.dispatchEvent(mouse("mouseup", 200, 180));
    dispose();
  });

  it("拖动结束：moved 在下一 tick 复位（后续键盘激活不被吞）", async () => {
    const el = makeEl("composer", { left: 100, top: 100, width: 200, height: 80 });
    const { api, dispose } = await mountFloat(el, composerOpts());
    el.dispatchEvent(mouse("mousedown", 150, 130));
    document.dispatchEvent(mouse("mousemove", 200, 180));
    expect(api.moved.value).toBe(true);
    document.dispatchEvent(mouse("mouseup", 200, 180));
    // drag 自身的 click（若有）仍在 moved=true 期间被抑制
    expect(api.moved.value).toBe(true);
    await new Promise((r) => setTimeout(r, 5));
    expect(api.moved.value).toBe(false);
    dispose();
  });

  it("snap 后共享尺寸刷新为实时宽高（自动增高/缩放后避让不用过期值）", async () => {
    const el = makeEl("composer", { left: 100, top: 100, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts());
    // 模拟 Composer 自动增高：实时几何变化，共享 entry 尚未刷新
    const grow = () => {
      Object.defineProperty(el, "offsetWidth", { value: 260, configurable: true });
      Object.defineProperty(el, "offsetHeight", { value: 120, configurable: true });
      el.getBoundingClientRect = (() => {
        const left = parseInt(el.style.left, 10) || 100;
        const top = parseInt(el.style.top, 10) || 100;
        return { left, top, width: 260, height: 120, right: left + 260, bottom: top + 120, x: left, y: top, toJSON: () => ({}) } as DOMRect;
      }) as typeof el.getBoundingClientRect;
    };
    grow();
    expect(floatingState.composer.width).toBe(200); // 尚未刷新
    // 拖起→松手触发 snap → 用实时几何刷新共享尺寸
    el.dispatchEvent(mouse("mousedown", 150, 130));
    document.dispatchEvent(mouse("mousemove", 160, 140));
    document.dispatchEvent(mouse("mouseup", 160, 140));
    expect(floatingState.composer.width).toBe(260);
    expect(floatingState.composer.height).toBe(120);
    dispose();
  });

  it("拖动：位移 ≤3px 不算拖动（不误触发点击）", async () => {
    const el = makeEl("composer", { left: 100, top: 100, width: 200, height: 80 });
    const { api, dispose } = await mountFloat(el, composerOpts());
    el.dispatchEvent(mouse("mousedown", 150, 130));
    document.dispatchEvent(mouse("mousemove", 152, 131));
    expect(api.moved.value).toBe(false);
    expect(el.style.left).toBe("100px");
    expect(el.style.top).toBe("100px");
    document.dispatchEvent(mouse("mouseup", 152, 131));
    dispose();
  });

  it("清理：dispose 后移除 document 级监听，不再响应拖动", async () => {
    const el = makeEl("composer", { left: 100, top: 100, width: 200, height: 80 });
    const { api, dispose } = await mountFloat(el, composerOpts());
    el.dispatchEvent(mouse("mousedown", 150, 130));
    dispose();
    document.dispatchEvent(mouse("mousemove", 300, 300));
    expect(api.moved.value).toBe(false);
    expect(el.style.left).toBe("100px");
  });
});

describe("useFloatingWindow 贴靠", () => {
  it("edge：吸附最近边并保留水平位置", async () => {
    // 中心 (250,190) → 最近边是 top（190 < 250），保留 left=150
    const el = makeEl("composer", { left: 150, top: 150, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 150, y: 150 } }));
    el.dispatchEvent(mouse("mousedown", 250, 190));
    document.dispatchEvent(mouse("mousemove", 250, 196)); // 真正拖动（>3px）才允许贴靠
    document.dispatchEvent(mouse("mouseup", 250, 190));
    expect(el.style.left).toBe("150px");
    expect(el.style.top).toBe("4px");
    expect(el.dataset.fwDocked).toBe("1");
    expect(el.dataset.fwTarget).toBe("top");
    expect(floatingState.composer.docked).toBe(true);
    expect(floatingState.composer.dockedTo).toBe("top");
    dispose();
  });

  it("corner：吸附最近角", async () => {
    const el = makeEl("settings-float", { left: 900, top: 50, width: 44, height: 44 });
    const { dispose } = await mountFloat(el, {
      id: "settings-float",
      dockMode: "corner",
      defaultPos: { x: 900, y: 50 },
    });
    el.dispatchEvent(mouse("mousedown", 922, 72));
    document.dispatchEvent(mouse("mousemove", 922, 78));
    document.dispatchEvent(mouse("mouseup", 922, 72));
    expect(el.style.left).toBe("976px"); // 1024-44-4
    expect(el.style.top).toBe("4px");
    expect(el.dataset.fwTarget).toBe("tr");
    dispose();
  });

  it("互斥：贴角跳过与已贴靠组件重叠的角", async () => {
    // composer 先贴靠到 top-left（rect {4,4,204,84}）
    const comp = makeEl("composer", { left: 4, top: 4, width: 200, height: 80 });
    const c1 = await mountFloat(comp, composerOpts({ defaultPos: { x: 4, y: 4 } }));
    comp.dispatchEvent(mouse("mousedown", 104, 44));
    document.dispatchEvent(mouse("mousemove", 104, 50));
    document.dispatchEvent(mouse("mouseup", 104, 44));
    expect(floatingState.composer.docked).toBe(true);

    // settings 中心靠近 top-left，但 tl 被占 → 落到 bl
    const sbtn = makeEl("settings-float", { left: 200, top: 200, width: 44, height: 44 });
    const c2 = await mountFloat(sbtn, {
      id: "settings-float",
      dockMode: "corner",
      defaultPos: { x: 200, y: 200 },
    });
    sbtn.dispatchEvent(mouse("mousedown", 222, 222));
    document.dispatchEvent(mouse("mousemove", 222, 228));
    document.dispatchEvent(mouse("mouseup", 222, 222));
    expect(sbtn.style.left).toBe("4px");
    expect(sbtn.style.top).toBe("720px");
    expect(sbtn.dataset.fwTarget).toBe("bl");

    c1.dispose();
    c2.dispose();
  });

  it("互斥：贴边与已贴靠组件重叠时沿边微调", async () => {
    const comp = makeEl("composer", { left: 4, top: 4, width: 200, height: 80 });
    const c1 = await mountFloat(comp, composerOpts({ defaultPos: { x: 4, y: 4 } }));
    comp.dispatchEvent(mouse("mousedown", 104, 44));
    document.dispatchEvent(mouse("mousemove", 104, 50));
    document.dispatchEvent(mouse("mouseup", 104, 44));

    // dock 中心靠近 top 边，但 top 候选位与 composer 重叠 → 微调到不重叠位置
    const dock = makeEl("planet-dock", { left: 200, top: 20, width: 96, height: 96 });
    const c2 = await mountFloat(dock, {
      id: "planet-dock",
      dockMode: "edge",
      defaultPos: { x: 200, y: 20 },
    });
    dock.dispatchEvent(mouse("mousedown", 248, 68));
    document.dispatchEvent(mouse("mousemove", 248, 74));
    document.dispatchEvent(mouse("mouseup", 248, 68));

    const dockRect = { left: parseFloat(dock.style.left), top: parseFloat(dock.style.top), right: parseFloat(dock.style.left) + 96, bottom: parseFloat(dock.style.top) + 96 };
    const compRect = { left: 4, top: 10, right: 204, bottom: 90 };
    const overlap = !(dockRect.right <= compRect.left || dockRect.left >= compRect.right || dockRect.bottom <= compRect.top || dockRect.top >= compRect.bottom);
    expect(overlap).toBe(false);
    expect(dock.dataset.fwTarget).toBe("top");
    expect(dock.style.top).toBe("4px");
    expect(parseFloat(dock.style.left)).toBeGreaterThan(204); // 沿顶边微调到右侧空位

    c1.dispose();
    c2.dispose();
  });

  it("互斥：拖起即释放占用，松手重新贴靠互斥", async () => {
    // sbtn 先挂载（document mouseup 监听先注册），再挂 composer
    const sbtn = makeEl("settings-float", { left: 200, top: 200, width: 44, height: 44 });
    const c2 = await mountFloat(sbtn, {
      id: "settings-float",
      dockMode: "corner",
      defaultPos: { x: 200, y: 200 },
    });
    const comp = makeEl("composer", { left: 4, top: 4, width: 200, height: 80 });
    const c1 = await mountFloat(comp, composerOpts({ defaultPos: { x: 4, y: 4 } }));

    // composer 贴靠到 top-left
    comp.dispatchEvent(mouse("mousedown", 104, 44));
    document.dispatchEvent(mouse("mousemove", 104, 50));
    document.dispatchEvent(mouse("mouseup", 104, 44));
    expect(floatingState.composer.docked).toBe(true);

    // 真正拖起 composer（超过阈值，未松手）→ 立即释放占用
    comp.dispatchEvent(mouse("mousedown", 104, 50));
    document.dispatchEvent(mouse("mousemove", 104, 56));
    expect(floatingState.composer.docked).toBe(false);
    expect(comp.dataset.fwDocked).toBeUndefined();

    // sbtn 贴靠：其 document mouseup 先触发，此时 composer 已释放 → 可落 tl
    sbtn.dispatchEvent(mouse("mousedown", 222, 222));
    document.dispatchEvent(mouse("mousemove", 222, 228));
    document.dispatchEvent(mouse("mouseup", 222, 222));
    expect(sbtn.style.left).toBe("4px");
    expect(sbtn.style.top).toBe("4px");
    expect(sbtn.dataset.fwTarget).toBe("tl");

    // composer 松手 → 重新贴靠，自动避让已贴靠的 sbtn
    document.dispatchEvent(mouse("mouseup", 104, 44));
    expect(floatingState.composer.docked).toBe(true);
    const cr = { left: parseFloat(comp.style.left), top: parseFloat(comp.style.top), right: parseFloat(comp.style.left) + 200, bottom: parseFloat(comp.style.top) + 80 };
    const sr = { left: 4, top: 4, right: 48, bottom: 48 };
    const overlapped = !(cr.right <= sr.left || cr.left >= sr.right || cr.bottom <= sr.top || cr.top >= sr.bottom);
    expect(overlapped).toBe(false);

    c1.dispose();
    c2.dispose();
  });
});

describe("useFloatingWindow 贴靠隐藏 / 展开", () => {
  it("不允许隐藏时（默认）：贴靠后不隐藏", async () => {
    vi.useFakeTimers();
    const el = makeEl("settings-float", { left: 900, top: 50, width: 44, height: 44 });
    const { dispose } = await mountFloat(el, {
      id: "settings-float",
      dockMode: "corner",
      defaultPos: { x: 900, y: 50 },
    });
    el.dispatchEvent(mouse("mousedown", 922, 72));
    document.dispatchEvent(mouse("mousemove", 922, 78));
    document.dispatchEvent(mouse("mouseup", 922, 72));
    vi.advanceTimersByTime(400);
    expect(el.classList.contains("fw-hidden")).toBe(false);
    expect(floatingState["settings-float"].hidden).toBe(false);
    dispose();
  });

  it("允许隐藏时：开启开关立即隐藏；拖起还原；贴靠后保持隐藏", async () => {
    vi.useFakeTimers();
    const el = makeEl("settings-float", { left: 900, top: 50, width: 44, height: 44 });
    const { dispose } = await mountFloat(el, {
      id: "settings-float",
      dockMode: "corner",
      defaultPos: { x: 900, y: 50 },
    });
    floatingState["settings-float"].hideEnabled = true;
    await nextTick(); // watcher → maybeHide
    expect(el.classList.contains("fw-hidden")).toBe(true);
    expect(floatingState["settings-float"].hidden).toBe(true);

    // 拖起：restore 立即解除隐藏态（hover 展开由组件 CSS :hover 实现）
    el.dispatchEvent(mouse("mousedown", 922, 72));
    document.dispatchEvent(mouse("mousemove", 922, 78));
    expect(el.classList.contains("fw-hidden")).toBe(false);
    expect(floatingState["settings-float"].hidden).toBe(false);
    document.dispatchEvent(mouse("mouseup", 922, 72));
    vi.advanceTimersByTime(400); // snap 后再次隐藏
    expect(el.classList.contains("fw-hidden")).toBe(true);
    expect(floatingState["settings-float"].hidden).toBe(true);
    dispose();
  });

  it("初始化：hideEnabled 开启时挂载即隐藏（初始状态与开关一致，无需先贴靠）", async () => {
    localStorage.setItem(FLOAT_HIDE_KEY, JSON.stringify({ composer: true }));
    const el = makeEl("composer", { left: 100, top: 100, width: 200, height: 80 });
    const { api, dispose } = await mountFloat(el, composerOpts());
    expect(api.entry.hideEnabled).toBe(true);
    expect(el.classList.contains("fw-hidden")).toBe(true);
    expect(floatingState.composer.hidden).toBe(true);
    dispose();
  });

  it("初始化：hideEnabled 关闭时挂载不隐藏", async () => {
    localStorage.setItem(FLOAT_HIDE_KEY, JSON.stringify({ composer: false }));
    const el = makeEl("composer", { left: 100, top: 100, width: 200, height: 80 });
    const { api, dispose } = await mountFloat(el, composerOpts());
    expect(api.entry.hideEnabled).toBe(false);
    expect(el.classList.contains("fw-hidden")).toBe(false);
    dispose();
  });

  it("关闭 hideEnabled：立即解除隐藏态", async () => {
    const el = makeEl("settings-float", { left: 900, top: 50, width: 44, height: 44 });
    const { dispose } = await mountFloat(el, {
      id: "settings-float",
      dockMode: "corner",
      defaultPos: { x: 900, y: 50 },
    });
    floatingState["settings-float"].hideEnabled = true;
    el.dispatchEvent(mouse("mousedown", 922, 72));
    document.dispatchEvent(mouse("mousemove", 922, 78));
    document.dispatchEvent(mouse("mouseup", 922, 72));
    await new Promise((r) => setTimeout(r, 400)); // 等贴靠隐藏定时器（真实定时器）
    expect(el.classList.contains("fw-hidden")).toBe(true);

    floatingState["settings-float"].hideEnabled = false;
    await nextTick();
    expect(el.classList.contains("fw-hidden")).toBe(false);
    expect(floatingState["settings-float"].hidden).toBe(false);
    dispose();
  });
});

describe("useFloatingWindow 位置持久化", () => {
  it("贴靠后写入 localStorage；新实例恢复贴靠位置", async () => {
    const el = makeEl("settings-float", { left: 900, top: 50, width: 44, height: 44 });
    const c1 = await mountFloat(el, {
      id: "settings-float",
      dockMode: "corner",
      defaultPos: { x: 900, y: 50 },
    });
    el.dispatchEvent(mouse("mousedown", 922, 72));
    document.dispatchEvent(mouse("mousemove", 922, 78));
    document.dispatchEvent(mouse("mouseup", 922, 72));
    const raw = JSON.parse(localStorage.getItem(FLOAT_STORAGE_KEY) ?? "{}");
    expect(raw["settings-float"].x).toBe(976);
    expect(raw["settings-float"].y).toBe(4);
    expect(raw["settings-float"].docked).toBe(true);
    c1.dispose();

    // 新实例：defaultPos 不同，但应恢复为持久化位置
    const el2 = makeEl("settings-float", { left: 0, top: 0, width: 44, height: 44 });
    const c2 = await mountFloat(el2, {
      id: "settings-float",
      dockMode: "corner",
      defaultPos: { x: 100, y: 100 },
    });
    expect(el2.style.left).toBe("976px");
    expect(el2.style.top).toBe("4px");
    c2.dispose();
  });
});

describe("useFloatingWindow 点击不触发贴靠（问题9）", () => {
  it("0px 单击：位置不变、不贴靠、不写持久化、不加过渡类", async () => {
    const el = makeEl("composer", { left: 300, top: 300, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 300, y: 300 } }));
    localStorage.removeItem(FLOAT_STORAGE_KEY);

    el.dispatchEvent(mouse("mousedown", 400, 340));
    document.dispatchEvent(mouse("mouseup", 400, 340));

    expect(el.style.left).toBe("300px");
    expect(el.style.top).toBe("300px");
    expect(floatingState.composer.docked).toBe(false);
    expect(el.dataset.fwDocked).toBeUndefined();
    expect(el.classList.contains("fw-snapping")).toBe(false);
    expect(localStorage.getItem(FLOAT_STORAGE_KEY)).toBeNull();
    dispose();
  });

  it("2px 抖动（阈值内）：同样不贴靠、位置不变", async () => {
    const el = makeEl("composer", { left: 300, top: 300, width: 200, height: 80 });
    const { api, dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 300, y: 300 } }));

    el.dispatchEvent(mouse("mousedown", 400, 340));
    document.dispatchEvent(mouse("mousemove", 402, 341));
    document.dispatchEvent(mouse("mouseup", 402, 341));

    expect(api.moved.value).toBe(false);
    expect(el.style.left).toBe("300px");
    expect(floatingState.composer.docked).toBe(false);
    dispose();
  });

  it("4px 拖动（超过阈值）松手后才贴靠并保存位置", async () => {
    const el = makeEl("composer", { left: 300, top: 300, width: 200, height: 80 });
    const { api, dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 300, y: 300 } }));
    localStorage.removeItem(FLOAT_STORAGE_KEY);

    el.dispatchEvent(mouse("mousedown", 400, 340));
    document.dispatchEvent(mouse("mousemove", 404, 344));
    expect(api.moved.value).toBe(true);
    document.dispatchEvent(mouse("mouseup", 404, 344));

    expect(floatingState.composer.docked).toBe(true);
    expect(el.dataset.fwDocked).toBe("1");
    expect(localStorage.getItem(FLOAT_STORAGE_KEY)).not.toBeNull();
    dispose();
  });

  it("已贴靠组件单击：贴靠状态与位置保持不变", async () => {
    localStorage.setItem(
      FLOAT_STORAGE_KEY,
      JSON.stringify({ composer: { x: 820, y: 300, docked: true, dockedTo: "right", hideEnabled: false } }),
    );
    const el = makeEl("composer", { left: 0, top: 0, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts());
    expect(el.style.left).toBe("820px");

    el.dispatchEvent(mouse("mousedown", 920, 340));
    document.dispatchEvent(mouse("mouseup", 920, 340));

    expect(el.style.left).toBe("820px");
    expect(el.style.top).toBe("300px");
    expect(floatingState.composer.docked).toBe(true);
    expect(floatingState.composer.dockedTo).toBe("right");
    expect(el.dataset.fwTarget).toBe("right");
    dispose();
  });
});

describe("useFloatingWindow 窗口 resize 保持贴靠关系（问题10）", () => {
  const origWidth = window.innerWidth;
  const origHeight = window.innerHeight;

  function setViewport(width: number, height: number) {
    Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: height, configurable: true });
    window.dispatchEvent(new Event("resize"));
  }

  afterEach(() => {
    Object.defineProperty(window, "innerWidth", { value: origWidth, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: origHeight, configurable: true });
  });

  it("right 贴靠：窗口放大/缩小后仍保持相同的右边距", async () => {
    const el = makeEl("composer", { left: 820, top: 300, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 820, y: 300 } }));
    el.dispatchEvent(mouse("mousedown", 920, 340));
    document.dispatchEvent(mouse("mousemove", 924, 340));
    document.dispatchEvent(mouse("mouseup", 924, 340));
    expect(floatingState.composer.dockedTo).toBe("right");
    expect(el.style.left).toBe("820px"); // 1024 - 200 - 4

    setViewport(1400, 900);
    expect(el.style.left).toBe("1196px"); // 1400 - 200 - 4：距右边缘仍是 4
    expect(el.style.top).toBe("300px"); // 垂直位置不动

    setViewport(600, 400);
    expect(el.style.left).toBe("396px"); // 600 - 200 - 4：缩小后仍在可视区
    expect(parseFloat(el.style.left)).toBeLessThanOrEqual(600 - 200);
    dispose();
  });

  it("bottom 贴靠：窗口高度变化后保持下边距，且不跑出可视区", async () => {
    const el = makeEl("composer", { left: 300, top: 684, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 300, y: 684 } }));
    el.dispatchEvent(mouse("mousedown", 400, 724));
    document.dispatchEvent(mouse("mousemove", 400, 728));
    document.dispatchEvent(mouse("mouseup", 400, 728));
    expect(floatingState.composer.dockedTo).toBe("bottom");
    expect(el.style.top).toBe("684px"); // 768 - 80 - 4

    setViewport(1024, 1000);
    expect(el.style.top).toBe("916px"); // 1000 - 80 - 4

    setViewport(1024, 300);
    expect(el.style.top).toBe("216px"); // 300 - 80 - 4
    expect(parseFloat(el.style.top)).toBeLessThanOrEqual(300 - 80);
    dispose();
  });

  it("未贴靠的浮动元素在窗口缩小时被钳制回可视区", async () => {
    const el = makeEl("composer", { left: 800, top: 600, width: 200, height: 80 });
    const { dispose } = await mountFloat(el, composerOpts({ defaultPos: { x: 800, y: 600 } }));
    setViewport(500, 300);
    expect(parseFloat(el.style.left)).toBeLessThanOrEqual(500 - 200);
    expect(parseFloat(el.style.top)).toBeLessThanOrEqual(300 - 80);
    dispose();
  });
});
