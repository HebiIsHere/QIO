/**
 * conversationMotion 纯函数单测（motion-core）。
 *
 * 契约（2026-11-17 用户确认的演示修订）：
 * - 线框「出现 / 消失」共用同一条 650ms、easeInOutCubic（慢-快-慢）时间轴；
 * - 消息内容随下沉虚化：进度进入窗口后 opacity 1→0、blur 0→6px；
 * - 线框几何（镜像路径 / 弧长揭示 / dasharray / 玻璃裁剪）口径不变。
 */
import { describe, expect, it } from "vitest";
import {
  CONVERSATION_MOTION,
  conversationContentMist,
  conversationFrameGeometry,
  easeInOutCubic,
  easedProgress,
  type ConversationFrameGeometry,
  type ConversationFrameInput,
} from "../conversationMotion";

/** 参考预览 drawFrame() 里的 1000×600 实测几何（PLAN §3.1 校准输入）。 */
const CALIBRATION_INPUT: ConversationFrameInput = {
  panelWidth: 976,
  leverWidth: 112,
  leverTop: 0,
  leverHeight: 32,
  inputTop: 230,
  inputHeight: 56,
  reveal: 1,
};

/** 参考几何在 reveal=1 时应有的完整投影（长度 3 位小数、路径 2 位小数）。 */
const CALIBRATION_GEOMETRY: ConversationFrameGeometry = {
  width: 976,
  height: 258,
  center: 488,
  leverY: 16,
  halfHandle: 52,
  endY: 258,
  corner: 14,
  edge: 0.75,
  leftPath: "M 436 16 H 14.75 Q 0.75 16 0.75 30 V 258",
  rightPath: "M 540 16 H 961.25 Q 975.25 16 975.25 30 V 258",
  horizontalLength: 421.25,
  arcLength: 21.991,
  verticalLength: 228,
  pathLength: 671.241,
  visibleLength: 671.241,
  strokeDasharray: "none",
  glassInset: 0,
  glassOpacity: 1,
  fadeY1: 220,
  fadeY2: 258,
  cornerReveal: 0.6276,
  visible: true,
};

/** panelWidth 非法时的安全零几何（不抛错、不出 NaN）。 */
const SAFE_ZERO_GEOMETRY: ConversationFrameGeometry = {
  width: 0,
  height: 0,
  center: 0,
  leverY: 0,
  halfHandle: 0,
  endY: 0,
  corner: 0,
  edge: 0,
  leftPath: "",
  rightPath: "",
  horizontalLength: 0,
  arcLength: 0,
  verticalLength: 0,
  pathLength: 0,
  visibleLength: 0,
  strokeDasharray: "none",
  glassInset: 0,
  glassOpacity: 0,
  fadeY1: 0,
  fadeY2: 0,
  cornerReveal: 0,
  visible: false,
};

/** 从 SVG path 里按顺序取出数字（用于镜像断言）。 */
function pathNumbers(path: string): number[] {
  return (path.match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number);
}


describe("CONVERSATION_MOTION：冻结的时间轴与虚化参数", () => {
  it("与用户确认的演示口径完全一致", () => {
    expect(CONVERSATION_MOTION).toEqual({
      durationMs: 650,
      progressEpsilon: 0.0015,
      mistBlurPx: 6,
      mistStartProgress: 0.5,
      mistEndProgress: 0.15,
    });
  });
});

describe("easeInOutCubic：慢-快-慢非线性缓动", () => {
  it("端点与中点精确：f(0)=0、f(0.5)=0.5、f(1)=1", () => {
    expect(easeInOutCubic(0)).toBe(0);
    expect(easeInOutCubic(0.5)).toBe(0.5);
    expect(easeInOutCubic(1)).toBe(1);
  });

  it("已知取值：f(0.25)=0.0625、f(0.75)=0.9375（慢起慢收）", () => {
    expect(easeInOutCubic(0.25)).toBeCloseTo(0.0625, 12);
    expect(easeInOutCubic(0.75)).toBeCloseTo(0.9375, 12);
  });

  it("时间镜像对称：f(x)+f(1-x)=1（出现 / 消失共用同一条曲线）", () => {
    for (const x of [0.05, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 0.95]) {
      expect(easeInOutCubic(x) + easeInOutCubic(1 - x)).toBeCloseTo(1, 12);
    }
  });

  it("单调不减，且中间最快：半程斜率明显大于两端", () => {
    let previous = 0;
    for (let step = 0; step <= 200; step += 1) {
      const value = easeInOutCubic(step / 200);
      expect(value).toBeGreaterThanOrEqual(previous);
      previous = value;
    }
    const midSlope = easeInOutCubic(0.55) - easeInOutCubic(0.45);
    const headSlope = easeInOutCubic(0.1) - easeInOutCubic(0);
    const tailSlope = easeInOutCubic(1) - easeInOutCubic(0.9);
    expect(midSlope).toBeGreaterThan(headSlope * 5);
    expect(midSlope).toBeGreaterThan(tailSlope * 5);
  });

  it("输入夹到 [0,1]，NaN 视为 0", () => {
    expect(easeInOutCubic(-3)).toBe(0);
    expect(easeInOutCubic(3)).toBe(1);
    expect(easeInOutCubic(Number.NaN)).toBe(0);
  });
});

describe("easedProgress：线性时间 → 缓动进度", () => {
  it("0 / 半程 / 全程：0、0.5、1", () => {
    expect(easedProgress(0, 650)).toBe(0);
    expect(easedProgress(325, 650)).toBe(0.5);
    expect(easedProgress(650, 650)).toBe(1);
  });

  it("duration<=0 视为瞬间完成；非法输入安全", () => {
    expect(easedProgress(0, 0)).toBe(1);
    expect(easedProgress(10, -1)).toBe(1);
    expect(easedProgress(Number.NaN, 650)).toBe(0);
    expect(easedProgress(100, Number.NaN)).toBe(1);
  });

  it("非线性：前 10% 时间只走约 0.4%，接近结束时收尾变慢", () => {
    expect(easedProgress(65, 650)).toBeCloseTo(0.004, 6);
    expect(easedProgress(585, 650)).toBeGreaterThan(0.99);
  });
});

describe("出现 / 消失同速：同一时间轴的两个方向", () => {
  it("展开进度 p(t) 与收起剩余 1-p(t) 时间镜像：全程时长相同", () => {
    const D = CONVERSATION_MOTION.durationMs;
    for (const t of [0, 60, 130, 250, 325, 400, 520, 650]) {
      const opening = easedProgress(t, D);
      const closing = 1 - opening;
      // 收起进度 = 1 - 展开进度，时长同为 D；进度曲线互相镜像
      expect(opening + closing).toBeCloseTo(1, 12);
      // 时间镜像：ease(t)+ease(D-t)=1 —— 收起进度恰等于 1-展开进度
      expect(easedProgress(D - t, D)).toBeCloseTo(closing, 12);
    }
  });

  it("完整出现与完整消失都恰好在 durationMs 结束（不存在某一方向更快）", () => {
    const D = CONVERSATION_MOTION.durationMs;
    expect(easedProgress(D - 1, D)).toBeLessThan(1);
    expect(easedProgress(D, D)).toBe(1);
    const closeAtHalf = 1 - easedProgress(D / 2, D);
    expect(closeAtHalf).toBe(0.5);
  });
});

describe("conversationContentMist：消息随下沉虚化", () => {
  it("展开态 / 进度 >= 窗口起点：完全不虚化", () => {
    for (const progress of [1, 0.8, 0.6, 0.5]) {
      expect(conversationContentMist(progress)).toEqual({ mist: 0, opacity: 1, blurPx: 0 });
    }
  });

  it("进度 <= 窗口终点：完全消失（透明度 0 + 6px 模糊）", () => {
    for (const progress of [0.15, 0.1, 0, Number.NaN, -1]) {
      expect(conversationContentMist(progress)).toEqual({ mist: 1, opacity: 0, blurPx: 6 });
    }
  });

  it("窗口中点：mist 0.5、opacity 0.5、blur 3px（smoothstep 中点）", () => {
    expect(conversationContentMist(0.325)).toEqual({ mist: 0.5, opacity: 0.5, blurPx: 3 });
  });

  it("进度从 1 降到 0：透明度单调减、模糊单调增、始终有界", () => {
    let previous = conversationContentMist(1);
    for (let step = 1; step <= 100; step += 1) {
      const current = conversationContentMist(1 - step / 100);
      expect(current.mist).toBeGreaterThanOrEqual(previous.mist);
      expect(current.opacity).toBeLessThanOrEqual(previous.opacity);
      expect(current.blurPx).toBeGreaterThanOrEqual(previous.blurPx);
      expect(current.mist).toBeGreaterThanOrEqual(0);
      expect(current.mist).toBeLessThanOrEqual(1);
      expect(current.blurPx).toBeGreaterThanOrEqual(0);
      expect(current.blurPx).toBeLessThanOrEqual(CONVERSATION_MOTION.mistBlurPx);
      previous = current;
    }
  });

  it("展开与收起共用同一映射：同一进度得到同一虚化值", () => {
    for (const progress of [0.45, 0.35, 0.25, 0.18]) {
      expect(conversationContentMist(progress)).toEqual(conversationContentMist(progress));
    }
  });
});

describe("conversationFrameGeometry：参考几何投影", () => {
  it("976/112/0/32/230/56、reveal=1 的完整投影", () => {
    expect(conversationFrameGeometry(CALIBRATION_INPUT)).toEqual(CALIBRATION_GEOMETRY);
  });

  it("左右路径关于 center 镜像", () => {
    const geometry = CALIBRATION_GEOMETRY;
    const left = pathNumbers(geometry.leftPath);
    const right = pathNumbers(geometry.rightPath);
    // 每条路径 8 个数字：M(x,y) H(x) Q(cx,cy,ex,ey) V(y)
    expect(left).toHaveLength(8);
    expect(right).toHaveLength(8);
    // x 坐标镜像：M x / H x / Q 控制点 x / Q 终点 x（下标 0、2、3、5）
    for (const index of [0, 2, 3, 5]) {
      expect(right[index]).toBe(geometry.width - left[index]);
    }
    // y 坐标一致：M y / Q 控制点 y / Q 终点 y / V y（下标 1、4、6、7）
    for (const index of [1, 4, 6, 7]) {
      expect(right[index]).toBe(left[index]);
    }
  });

  it("reveal=0：线头不可见但保留 dash 结构，玻璃不透明度 0", () => {
    const geometry = conversationFrameGeometry({ ...CALIBRATION_INPUT, reveal: 0 });
    expect(geometry.visibleLength).toBe(0);
    expect(geometry.strokeDasharray).toBe("0 672.241");
    expect(geometry.glassInset).toBe(436);
    expect(geometry.glassOpacity).toBe(0);
    expect(geometry.visible).toBe(false);
    // cornerReveal 只取决于几何，不随 reveal 变化
    expect(geometry.cornerReveal).toBe(CALIBRATION_GEOMETRY.cornerReveal);
  });

  it("reveal=0.5：可见长度一半、玻璃从把手处向两侧打开到一半", () => {
    const geometry = conversationFrameGeometry({ ...CALIBRATION_INPUT, reveal: 0.5 });
    expect(geometry.visibleLength).toBe(335.621);
    expect(geometry.strokeDasharray).toBe("335.621 672.241");
    expect(geometry.glassInset).toBe(100.379);
    expect(geometry.glassOpacity).toBe(0.5);
    expect(geometry.visible).toBe(true);
  });

  it("reveal 先夹到 [0,1]：超界与 NaN 都安全", () => {
    const tooBig = conversationFrameGeometry({ ...CALIBRATION_INPUT, reveal: 3 });
    expect(tooBig.strokeDasharray).toBe("none");
    expect(tooBig.glassOpacity).toBe(1);
    expect(tooBig.glassInset).toBe(0);

    const tooSmall = conversationFrameGeometry({ ...CALIBRATION_INPUT, reveal: -0.8 });
    expect(tooSmall.strokeDasharray).toBe("0 672.241");
    expect(tooSmall.glassOpacity).toBe(0);
    expect(tooSmall.visible).toBe(false);

    const nanReveal = conversationFrameGeometry({ ...CALIBRATION_INPUT, reveal: Number.NaN });
    expect(nanReveal.glassOpacity).toBe(0);
    expect(nanReveal.visible).toBe(false);
  });

  it("渐变高度：fadeY1=胶囊顶-10，fadeY2=胶囊中线（即 height）", () => {
    expect(CALIBRATION_GEOMETRY.fadeY1).toBe(220);
    expect(CALIBRATION_GEOMETRY.fadeY2).toBe(258);
    expect(CALIBRATION_GEOMETRY.height).toBe(CALIBRATION_GEOMETRY.endY);
    expect(CALIBRATION_GEOMETRY.fadeY2).toBe(CALIBRATION_GEOMETRY.endY);
  });

  it("reveal 增大时可见长度单调增、玻璃内缩单调减、不透明度单调增", () => {
    const samples = [0, 0.1, 0.25, 0.5, 0.75, 0.9, 1].map((reveal) =>
      conversationFrameGeometry({ ...CALIBRATION_INPUT, reveal }),
    );
    for (let index = 1; index < samples.length; index += 1) {
      expect(samples[index].visibleLength).toBeGreaterThanOrEqual(samples[index - 1].visibleLength);
      expect(samples[index].glassInset).toBeLessThanOrEqual(samples[index - 1].glassInset);
      expect(samples[index].glassOpacity).toBeGreaterThanOrEqual(samples[index - 1].glassOpacity);
      expect(samples[index].glassInset).toBeGreaterThanOrEqual(0);
      expect(samples[index].visibleLength).toBeLessThanOrEqual(samples[index].pathLength + 1e-9);
    }
  });

  it("不修改传入的输入对象（纯函数）", () => {
    const input: ConversationFrameInput = { ...CALIBRATION_INPUT, reveal: 0.4 };
    const snapshot = { ...input };
    conversationFrameGeometry(input);
    expect(input).toEqual(snapshot);
  });
});

describe("conversationFrameGeometry：自定义尺寸与夹取", () => {
  it("自定义 edge/corner/leverInset 进入镜像路径与长度", () => {
    const geometry = conversationFrameGeometry({
      panelWidth: 600,
      leverWidth: 80,
      leverTop: 10,
      leverHeight: 24,
      inputTop: 120,
      inputHeight: 40,
      reveal: 0.25,
      edge: 1,
      corner: 10,
      leverInset: 2,
    });
    expect(geometry.center).toBe(300);
    expect(geometry.leverY).toBe(22);
    expect(geometry.halfHandle).toBe(38);
    expect(geometry.endY).toBe(140);
    expect(geometry.edge).toBe(1);
    expect(geometry.corner).toBe(10);
    expect(geometry.leftPath).toBe("M 262 22 H 11 Q 1 22 1 32 V 140");
    expect(geometry.rightPath).toBe("M 338 22 H 589 Q 599 22 599 32 V 140");
    expect(geometry.horizontalLength).toBe(251);
    expect(geometry.arcLength).toBe(15.708);
    expect(geometry.verticalLength).toBe(108);
    expect(geometry.pathLength).toBe(374.708);
    expect(geometry.visibleLength).toBe(93.677);
    expect(geometry.strokeDasharray).toBe("93.677 375.708");
    expect(geometry.glassInset).toBe(168.323);
    expect(geometry.glassOpacity).toBe(0.25);
    expect(geometry.fadeY1).toBe(110);
    expect(geometry.fadeY2).toBe(140);
    expect(geometry.cornerReveal).toBe(0.6699);
  });

  it("胶囊中线过近时 endY 夹到 leverY+corner，竖边长度为 0 但不为负", () => {
    const geometry = conversationFrameGeometry({
      panelWidth: 800,
      leverWidth: 100,
      leverTop: 0,
      leverHeight: 30,
      inputTop: 0,
      inputHeight: 0,
      reveal: 1,
    });
    expect(geometry.leverY).toBe(15);
    expect(geometry.endY).toBe(29);
    expect(geometry.height).toBe(29);
    expect(geometry.verticalLength).toBe(0);
    expect(geometry.leftPath).toBe("M 354 15 H 14.75 Q 0.75 15 0.75 29 V 29");
    expect(geometry.fadeY1).toBe(-10);
  });

  it("负的 leverWidth / leverHeight / inputHeight 被夹到 0", () => {
    const geometry = conversationFrameGeometry({
      panelWidth: 800,
      leverWidth: -120,
      leverTop: 0,
      leverHeight: -40,
      inputTop: 100,
      inputHeight: -50,
      reveal: 1,
    });
    expect(geometry.halfHandle).toBe(0);
    expect(geometry.leverY).toBe(0);
    expect(geometry.endY).toBe(100);
    expect(geometry.verticalLength).toBe(86);
  });
});

describe("conversationFrameGeometry：非法输入稳健性", () => {
  it("panelWidth<=0 / NaN / Infinity 返回安全零几何且不抛错", () => {
    for (const panelWidth of [0, -10, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      expect(() => conversationFrameGeometry({ ...CALIBRATION_INPUT, panelWidth })).not.toThrow();
      expect(conversationFrameGeometry({ ...CALIBRATION_INPUT, panelWidth })).toEqual(SAFE_ZERO_GEOMETRY);
    }
  });

  it("其余字段 NaN 时全部输出仍是有限数，路径不出 NaN", () => {
    const geometry = conversationFrameGeometry({
      panelWidth: 800,
      leverWidth: Number.NaN,
      leverTop: Number.NaN,
      leverHeight: Number.NaN,
      inputTop: Number.NaN,
      inputHeight: Number.NaN,
      reveal: Number.NaN,
      edge: Number.NaN,
      corner: Number.NaN,
      leverInset: Number.NaN,
    });
    for (const value of Object.values(geometry)) {
      if (typeof value === "number") expect(Number.isFinite(value)).toBe(true);
      if (typeof value === "string") expect(value).not.toContain("NaN");
    }
    expect(geometry.width).toBe(800);
    expect(geometry.leftPath.startsWith("M ")).toBe(true);
    expect(geometry.visible).toBe(false);
  });

  it("数值量化：路径坐标 ≤2 位小数、长度 ≤3 位小数、比率 ≤4 位小数", () => {
    const geometry = conversationFrameGeometry({ ...CALIBRATION_INPUT, reveal: 0.333 });
    expect(geometry.leftPath).not.toMatch(/\.\d{3,}/);
    expect(geometry.rightPath).not.toMatch(/\.\d{3,}/);
    for (const key of [
      "horizontalLength",
      "arcLength",
      "verticalLength",
      "pathLength",
      "visibleLength",
      "glassInset",
    ] as const) {
      expect(Math.abs(geometry[key] * 1000 - Math.round(geometry[key] * 1000))).toBeLessThan(1e-9);
    }
    for (const key of ["cornerReveal", "glassOpacity"] as const) {
      expect(Math.abs(geometry[key] * 10000 - Math.round(geometry[key] * 10000))).toBeLessThan(1e-9);
    }
  });
});