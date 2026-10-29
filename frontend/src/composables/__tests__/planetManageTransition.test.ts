/**
 * usePlanetManageTransition（契约 _MP-COMMON §4.1）的定向测试。
 *
 * 只测这个 composable **对外承诺的四件事**：
 * 1. 每帧把 `--mp` / `data-mp` / `.managing` 写到根元素；
 * 2. 非线性缓动来自 CSS 令牌（这里用真实样式注入令牌，断言曲线确实被用上）；
 * 3. 中途反向从当前进度与速度连续衔接（不瞬移、不突然反转、终点精确）；
 * 4. 减少动画直接到位；卸载后 rAF 不残留。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { effectScope, ref } from "vue";
import { nextTick } from "vue";
import { usePlanetManageTransition } from "../usePlanetManageTransition";
import { MANAGE_YAW } from "../../planet/manageMotion";

/** 可手动推进的时钟 + rAF 队列：动画在测试里必须确定性地走帧 */
let clock = 0;
let queue: FrameRequestCallback[] = [];

function installFrames(): void {
  clock = 1000;
  queue = [];
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback): number => {
    queue.push(callback);
    return queue.length;
  });
  vi.stubGlobal("cancelAnimationFrame", (handle: number): void => {
    queue = queue.filter((_, index) => index !== handle - 1);
  });
  vi.spyOn(performance, "now").mockImplementation(() => clock);
}

/** 推进 n 帧，每帧 dtMs */
function runFrames(count: number, dtMs = 16): void {
  for (let i = 0; i < count; i += 1) {
    clock += dtMs;
    const pending = queue;
    queue = [];
    for (const callback of pending) callback(clock);
  }
}

function makeRoot(): HTMLElement {
  const el = document.createElement("div");
  el.className = "planet-view";
  document.body.appendChild(el);
  return el;
}

/** 注入与 tokens.css 一致的令牌（jsdom 不会加载真实样式表） */
function styleTokens(el: HTMLElement): void {
  el.style.setProperty("--ease-3-settle", "cubic-bezier(.24,1.04,.32,1)");
  el.style.setProperty("--ease-3-out", "cubic-bezier(.5,0,.24,1)");
  el.style.setProperty("--mo-3-expand", "520ms");
  el.style.setProperty("--mo-3-collapse", "420ms");
}

function setup() {
  const el = makeRoot();
  styleTokens(el);
  const rootRef = ref<HTMLElement | null>(el);
  const yaws: number[] = [];
  const planet = { setManageYaw: (value: number) => yaws.push(value) };
  const scope = effectScope();
  const api = scope.run(() => usePlanetManageTransition({ rootRef, planet }))!;
  return { el, rootRef, yaws, scope, api, planet };
}

const mp = (el: HTMLElement): string => el.style.getPropertyValue("--mp");

beforeEach(() => {
  document.documentElement.removeAttribute("data-motion");
  installFrames();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("进度 → DOM 契约", () => {
  it("applyProgress 写 --mp / data-mp（3 位小数）并按需加 .managing", () => {
    const { el, api, yaws, scope } = setup();
    api.applyProgress(0.4);
    expect(mp(el)).toBe("0.400");
    expect(el.getAttribute("data-mp")).toBe("0.400");
    expect(el.classList.contains("managing")).toBe(true);
    // 姿态角同步跟上：0.4 × MANAGE_YAW
    expect(yaws[yaws.length - 1]).toBeCloseTo(MANAGE_YAW * 0.4, 6);

    api.applyProgress(0);
    expect(mp(el)).toBe("0.000");
    expect(el.classList.contains("managing")).toBe(false);
    expect(yaws[yaws.length - 1]).toBe(0);
    scope.stop();
  });

  it("applyProgress 夹取越界值，非法值落到 0", () => {
    const { el, api, scope } = setup();
    api.applyProgress(3);
    expect(mp(el)).toBe("1.000");
    api.applyProgress(-2);
    expect(mp(el)).toBe("0.000");
    api.applyProgress(Number.NaN);
    expect(mp(el)).toBe("0.000");
    scope.stop();
  });

  it("根元素晚到：挂上之后立刻补写当前进度", async () => {
    const el = makeRoot();
    styleTokens(el);
    const rootRef = ref<HTMLElement | null>(null);
    const scope = effectScope();
    const api = scope.run(() =>
      usePlanetManageTransition({ rootRef, planet: { setManageYaw: () => {} } }),
    )!;
    api.applyProgress(0.25);
    expect(mp(el)).toBe("");
    rootRef.value = el;
    await nextTick();
    expect(mp(el)).toBe("0.250");
    expect(el.getAttribute("data-mp")).toBe("0.250");
    scope.stop();
  });
});

describe("展开 / 收起动画", () => {
  it("setExpanded(true) 逐帧推进到 1，且每帧都写 DOM", () => {
    const { el, api, scope } = setup();
    api.setExpanded(true);
    expect(api.expanded.value).toBe(true);
    runFrames(1);
    const first = Number.parseFloat(mp(el));
    expect(first).toBeGreaterThan(0);
    expect(first).toBeLessThan(1);
    runFrames(60);
    expect(mp(el)).toBe("1.000");
    expect(api.progress.value).toBe(1);
    expect(queue.length).toBe(0); // 动画结束不再排帧
    scope.stop();
  });

  it("时长来自 --mo-3-expand 令牌：520ms 的 1/2 处进度明显过半（settle 是快速起步）", () => {
    const { el, api, scope } = setup();
    api.setExpanded(true);
    runFrames(17, 16); // ≈272ms
    const value = Number.parseFloat(mp(el));
    expect(value).toBeGreaterThan(0.5);
    scope.stop();
  });

  it("收起用 --mo-3-collapse：从 1 逐帧回到 0", () => {
    const { el, api, scope } = setup();
    api.applyProgress(1);
    api.setExpanded(false);
    expect(api.expanded.value).toBe(false);
    runFrames(40, 16);
    expect(mp(el)).toBe("0.000");
    expect(api.progress.value).toBe(0);
    scope.stop();
  });

  it("重复点同一个状态不会重播动画", () => {
    const { api, scope } = setup();
    api.applyProgress(1);
    api.setExpanded(true);
    expect(queue.length).toBe(0);
    scope.stop();
  });
});

describe("中途反向：连续衔接", () => {
  it("从半途反向不会瞬移，最终精确落到 0", () => {
    const { el, api, scope } = setup();
    api.setExpanded(true);
    runFrames(12, 16); // 约 200ms，进度在半途
    const mid = Number.parseFloat(mp(el));
    expect(mid).toBeGreaterThan(0.2);
    expect(mid).toBeLessThan(0.95);

    api.setExpanded(false);
    // 反向后的第一帧：进度**不能**跳变（相邻帧变化必须很小）
    runFrames(1, 16);
    const afterFirst = Number.parseFloat(mp(el));
    expect(Math.abs(afterFirst - mid)).toBeLessThan(0.12);

    runFrames(60, 16);
    expect(mp(el)).toBe("0.000");
    scope.stop();
  });

  it("反向时先沿用旧方向的惯性（第一帧不会立刻反向），随后才换向", () => {
    const { el, api, scope } = setup();
    api.setExpanded(true);
    runFrames(12, 16);
    const mid = Number.parseFloat(mp(el));
    api.setExpanded(false);
    const samples: number[] = [];
    // 反向段的时长 ≈ --mo-3-collapse × 剩余距离 × 1.1（≤ 462ms）：多跑一倍帧确保收尾
    for (let i = 0; i < 90; i += 1) {
      runFrames(1, 8);
      samples.push(Number.parseFloat(mp(el)) || 0);
    }
    // 头两帧仍在向 1 走（惯性），之后转到下降
    expect(samples[0]).toBeGreaterThan(mid - 1e-6);
    expect(Math.max(...samples)).toBeGreaterThanOrEqual(mid);
    expect(samples[samples.length - 1]).toBe(0);
    // 全程没有「一帧跳掉一大截」
    let prev = mid;
    for (const value of samples) {
      expect(Math.abs(value - prev)).toBeLessThan(0.16);
      prev = value;
    }
    scope.stop();
  });

  it("连续来回反向（展开→收起→展开）仍然收敛到目标", () => {
    const { el, api, scope } = setup();
    api.setExpanded(true);
    runFrames(8, 16);
    api.setExpanded(false);
    runFrames(5, 16);
    api.setExpanded(true);
    runFrames(80, 16);
    expect(mp(el)).toBe("1.000");
    api.setExpanded(false);
    runFrames(80, 16);
    expect(mp(el)).toBe("0.000");
    scope.stop();
  });
});

describe("减少动态效果", () => {
  it('html[data-motion="reduced"] 时一帧直接到位（不排任何帧）', () => {
    document.documentElement.setAttribute("data-motion", "reduced");
    const { el, api, scope } = setup();
    api.setExpanded(true);
    expect(mp(el)).toBe("1.000");
    expect(api.progress.value).toBe(1);
    expect(queue.length).toBe(0);
    api.setExpanded(false);
    expect(mp(el)).toBe("0.000");
    expect(queue.length).toBe(0);
    scope.stop();
  });

  it("运行中被切成 regime=reduced：立刻落到目标，不停在半截", () => {
    const { el, api, scope } = setup();
    api.setExpanded(true);
    runFrames(6, 16);
    expect(Number.parseFloat(mp(el))).toBeLessThan(1);
    document.documentElement.setAttribute("data-motion", "reduced");
    runFrames(1, 16);
    expect(mp(el)).toBe("1.000");
    expect(queue.length).toBe(0);
    scope.stop();
  });
});

describe("清理", () => {
  it("scope 停止后不再排帧（不泄漏 rAF）", () => {
    const { api, scope } = setup();
    api.setExpanded(true);
    runFrames(3, 16);
    expect(queue.length).toBe(1);
    scope.stop();
    expect(queue.length).toBe(0);
    // 已停的状态不会因为迟到的帧回调再写一次
    expect(() => runFrames(2, 16)).not.toThrow();
  });
});