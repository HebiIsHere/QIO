/**
 * conversationMotion 纯函数单测（motion-core）。
 *
 * 契约：`_lead-logs/interactive-action/PLAN.md` §3.1（冻结接口）与 §5 验收。
 * 参考实现：预览 `QIO_Animated_Chat_Preview.html` 的 approach()/drawFrame()。
 * 覆盖：临界阻尼轴单步、解析进度与反解、镜像 SVG 几何/揭示/dasharray/玻璃裁剪、
 *       976/112/0/32/230/56 参考几何的「到角」校准，以及非法输入的稳健回退。
 */
import { describe, expect, it } from "vitest";
import {
  CONVERSATION_MOTION,
  conversationFrameGeometry,
  createMotionAxis,
  criticallyDampedDuration,
  criticallyDampedProgress,
  motionRateFor,
  stepMotionAxis,
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

describe("CONVERSATION_MOTION：冻结常量", () => {
  it("与 PLAN §3.1 完全一致", () => {
    expect(CONVERSATION_MOTION).toEqual({
      openRate: 11,
      closeRate: 21,
      dragRate: 45,
      dtMax: 0.04,
      heightEpsilon: 0.25,
      revealEpsilon: 0.0015,
    });
  });
});

describe("createMotionAxis：轴工厂", () => {
  it("默认 value=target=0、velocity=0", () => {
    expect(createMotionAxis()).toEqual({ value: 0, velocity: 0, target: 0 });
  });

  it("只给 value 时 target 跟随 value（初始静止）", () => {
    expect(createMotionAxis(294)).toEqual({ value: 294, velocity: 0, target: 294 });
  });

  it("value 与 target 可分别给定（收起态初始化）", () => {
    expect(createMotionAxis(0, 258)).toEqual({ value: 0, velocity: 0, target: 258 });
  });

  it("每次返回新对象，不共享状态", () => {
    expect(createMotionAxis()).not.toBe(createMotionAxis());
  });
});

describe("motionRateFor：拖动 > 展开 > 收起", () => {
  it("拖动优先 45，其次展开 11，其余收起 21", () => {
    expect(motionRateFor({ opening: true, dragging: true })).toBe(CONVERSATION_MOTION.dragRate);
    expect(motionRateFor({ opening: false, dragging: true })).toBe(45);
    expect(motionRateFor({ opening: true, dragging: false })).toBe(CONVERSATION_MOTION.openRate);
    expect(motionRateFor({ opening: false, dragging: false })).toBe(CONVERSATION_MOTION.closeRate);
  });
});

describe("stepMotionAxis：单步临界阻尼逼近", () => {
  it("rate=11、dt=1/60 时单调、无过冲，约 1s 内 settled 且吸附到 target", () => {
    const axis = createMotionAxis(0, 1);
    const dt = 1 / 60;
    let settledAt = -1;
    let previous = axis.value;
    for (let step = 1; step <= 120; step += 1) {
      const settled = stepMotionAxis(axis, dt, CONVERSATION_MOTION.openRate, CONVERSATION_MOTION.revealEpsilon);
      // 单调不减
      expect(axis.value).toBeGreaterThanOrEqual(previous);
      // 无过冲
      expect(axis.value).toBeLessThanOrEqual(axis.target + 1e-9);
      previous = axis.value;
      if (settled) {
        settledAt = step;
        break;
      }
    }
    expect(settledAt).toBeGreaterThan(0);
    expect(settledAt).toBeLessThanOrEqual(60); // ≤1s（参考用例实测 ~0.80s）
    expect(axis.value).toBe(axis.target);
    expect(axis.velocity).toBe(0);
  });

  it("离散单步与解析临界阻尼进度一致（同一初始条件下逐帧对齐）", () => {
    const axis = createMotionAxis(0, 1);
    const dt = 1 / 60;
    for (let step = 1; step <= 120; step += 1) {
      const settled = stepMotionAxis(axis, dt, 11, CONVERSATION_MOTION.revealEpsilon);
      if (settled) break;
      expect(axis.value).toBeCloseTo(criticallyDampedProgress(step / 60, 11), 12);
    }
  });

  it("高度轴（epsilon=0.25）同样在约 1s 内 settled", () => {
    const axis = createMotionAxis(0, 196);
    let settledAt = -1;
    for (let step = 1; step <= 120; step += 1) {
      if (stepMotionAxis(axis, 1 / 60, 11, CONVERSATION_MOTION.heightEpsilon)) {
        settledAt = step;
        break;
      }
    }
    expect(settledAt).toBeGreaterThan(0);
    expect(settledAt).toBeLessThanOrEqual(60);
    expect(axis.value).toBe(196);
    expect(axis.velocity).toBe(0);
  });

  it("已经在 target 上时立即 settled（velocity 清零）", () => {
    const axis = createMotionAxis(294, 294);
    expect(stepMotionAxis(axis, 1 / 60, 11, 0.25)).toBe(true);
    expect(axis.value).toBe(294);
    expect(axis.velocity).toBe(0);

    const resting = { value: 10, velocity: 8, target: 10 };
    expect(stepMotionAxis(resting, 1 / 60, 11, 0.25)).toBe(true);
    expect(resting.value).toBe(10);
    expect(resting.velocity).toBe(0);
  });

  it("dt 先夹到 [0, dtMax]：dt=1 与 dt=dtMax 等价", () => {
    const clamped = createMotionAxis(0, 1);
    stepMotionAxis(clamped, 1, 11, 0.0015);
    const explicit = createMotionAxis(0, 1);
    stepMotionAxis(explicit, CONVERSATION_MOTION.dtMax, 11, 0.0015);
    expect(clamped.value).toBe(explicit.value);
    expect(clamped.velocity).toBe(explicit.velocity);
    // 冻结公式在首帧的确定值（dt=0.04、rate=11 的一次积分）
    expect(clamped.value).toBeCloseTo(0.07258755364027636, 12);
    expect(clamped.velocity).toBeCloseTo(3.117136278042404, 12);
  });

  it("负值 / NaN dt 视为 0：不移动、不 settled", () => {
    for (const dt of [-1, Number.NaN, -Number.NaN]) {
      const axis = createMotionAxis(0, 1);
      expect(stepMotionAxis(axis, dt, 11, 0.0015)).toBe(false);
      expect(axis.value).toBe(0);
      expect(axis.velocity).toBe(0);
    }
  });

  it("快速反向：target 中途翻转时 value 连续、不跳变，最终吸附新 target", () => {
    const axis = createMotionAxis(0, 1);
    for (let step = 0; step < 10; step += 1) {
      stepMotionAxis(axis, 1 / 60, CONVERSATION_MOTION.openRate, CONVERSATION_MOTION.revealEpsilon);
    }
    const before = axis.value;
    const beforeVelocity = axis.velocity;
    expect(before).toBeGreaterThan(0);
    expect(beforeVelocity).toBeGreaterThan(0);

    axis.target = 0;
    const settledImmediately = stepMotionAxis(axis, 1 / 60, CONVERSATION_MOTION.closeRate, CONVERSATION_MOTION.revealEpsilon);
    expect(settledImmediately).toBe(false);
    // 连续：单帧位移远小于剩余距离（实测约 0.011，任一点跳变都会远大于此）
    expect(Math.abs(axis.value - before)).toBeLessThan(0.05);

    let settled = false;
    for (let step = 0; step < 240 && !settled; step += 1) {
      settled = stepMotionAxis(axis, 1 / 60, CONVERSATION_MOTION.closeRate, CONVERSATION_MOTION.revealEpsilon);
    }
    expect(settled).toBe(true);
    expect(axis.value).toBe(0);
    expect(axis.velocity).toBe(0);
  });

  it("揭示轴与高度轴同 rate 同步：同一时间步归一化进度一致", () => {
    const heightAxis = createMotionAxis(0, 196);
    const revealAxis = createMotionAxis(0, 1);
    const dt = 1 / 60;
    for (let step = 1; step <= 40; step += 1) {
      const heightSettled = stepMotionAxis(heightAxis, dt, 11, CONVERSATION_MOTION.heightEpsilon);
      const revealSettled = stepMotionAxis(revealAxis, dt, 11, CONVERSATION_MOTION.revealEpsilon);
      expect(heightSettled).toBe(false);
      expect(revealSettled).toBe(false);
      const heightProgress = heightAxis.value / 196;
      expect(heightProgress).toBeCloseTo(revealAxis.value, 12);
      expect(heightProgress).toBeCloseTo(criticallyDampedProgress(step / 60, 11), 12);
    }
  });
});

describe("criticallyDampedProgress：解析进度", () => {
  it("t<=0 返回 0；rate<=0 视为瞬间完成", () => {
    expect(criticallyDampedProgress(0, 11)).toBe(0);
    expect(criticallyDampedProgress(-0.3, 11)).toBe(0);
    expect(criticallyDampedProgress(Number.NaN, 11)).toBe(0);
    expect(criticallyDampedProgress(1, 0)).toBe(1);
    expect(criticallyDampedProgress(1, -11)).toBe(1);
  });

  it("1-(1+rt)e^{-rt} 的已知取值", () => {
    expect(criticallyDampedProgress(1 / 11, 11)).toBeCloseTo(1 - 2 / Math.E, 12);
    expect(criticallyDampedProgress(1, 11)).toBeCloseTo(0.9997995795905169, 12);
    expect(criticallyDampedProgress(1, 21)).toBeCloseTo(1 - 22 * Math.exp(-21), 12);
  });

  it("随时间单调不减且夹在 [0,1]", () => {
    let previous = 0;
    for (let step = 0; step <= 240; step += 1) {
      const value = criticallyDampedProgress(step / 60, 11);
      expect(value).toBeGreaterThanOrEqual(previous);
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThanOrEqual(1);
      previous = value;
    }
    expect(criticallyDampedProgress(10, 11)).toBe(1);
  });
});

describe("criticallyDampedDuration：反解到角时间", () => {
  it("progress<=0 → 0；progress>=1 或 rate<=0 → Infinity", () => {
    expect(criticallyDampedDuration(0, 11)).toBe(0);
    expect(criticallyDampedDuration(-0.5, 11)).toBe(0);
    expect(criticallyDampedDuration(Number.NaN, 11)).toBe(0);
    expect(criticallyDampedDuration(1, 11)).toBe(Number.POSITIVE_INFINITY);
    expect(criticallyDampedDuration(1.5, 11)).toBe(Number.POSITIVE_INFINITY);
    expect(criticallyDampedDuration(0.5, 0)).toBe(Number.POSITIVE_INFINITY);
    expect(criticallyDampedDuration(0.5, -11)).toBe(Number.POSITIVE_INFINITY);
  });

  it("与 criticallyDampedProgress 互为反函数（多进度 × 多 rate）", () => {
    for (const rate of [11, 21, 45]) {
      for (const progress of [0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99]) {
        const duration = criticallyDampedDuration(progress, rate);
        expect(duration).toBeGreaterThan(0);
        expect(criticallyDampedProgress(duration, rate)).toBeCloseTo(progress, 9);
      }
    }
  });

  it("Lead 指定目标：进度 0.690 在 rate=11 下 ≈ 216–218ms", () => {
    const duration = criticallyDampedDuration(0.69, 11);
    expect(duration).toBeGreaterThan(0.21);
    expect(duration).toBeLessThan(0.225);
    expect(duration).toBeCloseTo(0.2175, 3);
  });

  it("参考几何的到角校准：cornerReveal 反解落在 0.19–0.245s", () => {
    const geometry = conversationFrameGeometry(CALIBRATION_INPUT);
    // 冻结公式（horizontal/pathLength）在 976/112/0/32/230/56 下为 421.25/671.241 = 0.6276；
    // PLAN §3.1 备注的 ≈0.690 与「216ms」是同一目标进度的两种口径（0.690 ↔ 217.5ms），
    // 这里同时把两者写进测试：公式真值 + 时长区间，保证冻结几何与 Lead 的时长验收口径都被覆盖。
    expect(geometry.cornerReveal).toBe(0.6276);
    const duration = criticallyDampedDuration(geometry.cornerReveal, 11);
    expect(duration).toBeGreaterThanOrEqual(0.19);
    expect(duration).toBeLessThanOrEqual(0.245);
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
