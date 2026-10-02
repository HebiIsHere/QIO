import { describe, expect, it } from "vitest";
import {
  attributeSpans,
  buildTimingSentence,
  buildTurnTiming,
  formatMs,
  type PhaseLedger,
} from "../trace";

/**
 * 真实样本：本机后端跑出来的一轮（假 provider，不联网），原样贴进测试。
 * 它是「后端真的会给出什么形状」的证据，而不是我手编的理想数据。
 */
const REAL_PHASES: PhaseLedger = {
  version: 1,
  total_ms: 225,
  sum_ms: 224,
  residual_ms: 0,
  notes: { queue_wait_ms: 2 },
  spans: [
    { name: "other", ms: 4, depth: 1, detail: null, start_ms: 0 },
    { name: "adapter_setup", ms: 0, depth: 1, detail: null, start_ms: 4 },
    { name: "turn_setup", ms: 0, depth: 1, detail: null, start_ms: 4 },
    { name: "turn_binding", ms: 15, depth: 1, detail: null, start_ms: 4 },
    { name: "context_assembly", ms: 120, depth: 1, detail: null, start_ms: 20 },
    { name: "topic_prediction", ms: 0, depth: 2, detail: null, start_ms: 25 },
    { name: "persistence", ms: 91, depth: 2, detail: "user_message", start_ms: 25 },
    { name: "retrieval", ms: 11, depth: 2, detail: null, start_ms: 126 },
    { name: "agent_loop", ms: 64, depth: 1, detail: null, start_ms: 140 },
    { name: "tool_routing", ms: 1, depth: 2, detail: null, start_ms: 143 },
    { name: "model_wait", ms: 0, depth: 2, detail: "call#1", start_ms: 144 },
    { name: "narrative", ms: 0, depth: 2, detail: null, start_ms: 185 },
    { name: "tool_wait", ms: 13, depth: 2, detail: "calls=1", start_ms: 185 },
    { name: "tool_routing", ms: 2, depth: 2, detail: null, start_ms: 199 },
    { name: "model_wait", ms: 1, depth: 2, detail: "call#2", start_ms: 201 },
    { name: "persistence", ms: 12, depth: 1, detail: null, start_ms: 204 },
    { name: "memory_post", ms: 0, depth: 1, detail: null, start_ms: 216 },
    { name: "finalize", ms: 9, depth: 1, detail: null, start_ms: 216 },
  ],
  after_turn: [],
};

function rowOf(timing: ReturnType<typeof buildTurnTiming>, label: string) {
  return timing?.rows.find((r) => r.label === label);
}

describe("阶段 → 用户分类", () => {
  it("真实账本能算出用户分类，且不显示内部阶段名", () => {
    const timing = buildTurnTiming({ turn_id: "turn_real", duration_ms: 224, phases: REAL_PHASES });
    expect(timing).not.toBeNull();
    const labels = timing!.rows.map((r) => r.label);
    expect(labels).toContain("排队");
    expect(labels).toContain("上下文准备");
    expect(labels).toContain("记忆检索");
    expect(labels).toContain("工具");
    expect(labels).toContain("保存");
    // 内部名不该出现在给用户看的标签里
    expect(labels.join(" ")).not.toContain("adapter_setup");
    expect(labels.join(" ")).not.toContain("persistence");
  });

  it("嵌套阶段不重复计入：agent_loop 的时间归到它真正花掉的子阶段", () => {
    const timing = buildTurnTiming({
      turn_id: "t",
      duration_ms: 1000,
      phases: {
        residual_ms: 0,
        notes: {},
        spans: [
          { name: "agent_loop", ms: 944, depth: 1, start_ms: 0 },
          { name: "model_wait", ms: 20, depth: 2, start_ms: 10 },
          { name: "tool_wait", ms: 14, depth: 2, start_ms: 40 },
          { name: "approval_wait", ms: 910, depth: 2, start_ms: 60 },
        ],
      },
    });
    expect(rowOf(timing, "等待确认")?.ms).toBe(910);
    expect(rowOf(timing, "模型")?.ms).toBe(20);
    expect(rowOf(timing, "工具")?.ms).toBe(14);
    // 剩下的 0 是循环自身开销 → 不造一个假分类，落进「其它」（4ms 中 0 被省略）
    const sum = timing!.rows.reduce((s, r) => s + r.ms, 0);
    expect(sum).toBe(944);
  });

  it("占比按「排队 + 执行」算，一眼能看出谁占大头", () => {
    const timing = buildTurnTiming({
      turn_id: "t",
      duration_ms: 1000,
      phases: {
        residual_ms: 0,
        notes: { queue_wait_ms: 1000 },
        spans: [{ name: "model_wait", ms: 1000, depth: 1, start_ms: 0 }],
      },
    });
    expect(timing!.totalMs).toBe(2000);
    expect(rowOf(timing, "排队")?.percent).toBe(50);
    expect(rowOf(timing, "模型")?.percent).toBe(50);
  });

  it("未知阶段不会消失：落进「其它」，原名记下来给开发者模式", () => {
    const timing = buildTurnTiming({
      turn_id: "t",
      duration_ms: 30,
      phases: {
        residual_ms: 5,
        notes: {},
        spans: [
          { name: "brand_new_stage", ms: 20, depth: 1, start_ms: 0 },
          { name: "other", ms: 5, depth: 1, start_ms: 20 },
        ],
      },
    });
    expect(timing!.unknownStages).toEqual(["brand_new_stage"]);
    // 20（未知）+ 5（other）+ 5（residual）全部进「其它」
    expect(rowOf(timing, "其它")?.ms).toBe(30);
    const raw = timing!.rawStages.find((s) => s.name === "brand_new_stage");
    expect(raw?.ms).toBe(20);
    expect(raw?.count).toBe(1);
  });

  it("residual 如实进「其它」，不假装能解释所有耗时", () => {
    const timing = buildTurnTiming({
      turn_id: "t",
      duration_ms: 100,
      phases: {
        residual_ms: 37,
        notes: {},
        spans: [{ name: "model_wait", ms: 63, depth: 1, start_ms: 0 }],
      },
    });
    expect(timing!.residualMs).toBe(37);
    expect(rowOf(timing, "其它")?.ms).toBe(37);
    expect(timing!.rows.reduce((s, r) => s + r.ms, 0)).toBe(100);
  });

  it("turn 之后的后台整理单独给，不混进总耗时", () => {
    const timing = buildTurnTiming({
      turn_id: "t",
      duration_ms: 50,
      phases: {
        residual_ms: 0,
        notes: {},
        spans: [{ name: "model_wait", ms: 50, depth: 1, start_ms: 0 }],
        after_turn: [{ name: "derived_work", ms: 420, detail: "summary" }],
      },
    });
    expect(timing!.totalMs).toBe(50);
    expect(timing!.afterTurnMs).toBe(420);
  });
});

describe("老数据 / 缺数据的降级", () => {
  it("没有 phases 的旧行：只有总耗时，标记 legacy", () => {
    const timing = buildTurnTiming({ turn_id: "old", duration_ms: 8436, phases: {} });
    expect(timing!.legacy).toBe(true);
    expect(timing!.rows).toEqual([]);
    expect(timing!.totalMs).toBe(8436);
  });

  it("phases 为 null（更老的响应）同样不炸", () => {
    const timing = buildTurnTiming({ turn_id: "old2", duration_ms: 1000, phases: null });
    expect(timing!.legacy).toBe(true);
    expect(timing!.totalMs).toBe(1000);
  });

  it("连 duration 都没有：totalMs 为 null（界面说「没有记录」，不显示 0）", () => {
    const timing = buildTurnTiming({ turn_id: "empty", duration_ms: null, phases: {} });
    expect(timing!.totalMs).toBeNull();
    expect(buildTimingSentence(timing)).toBe("这次没有耗时记录");
  });

  it("没有 trace 时返回 null", () => {
    expect(buildTurnTiming(null)).toBeNull();
    expect(buildTimingSentence(null)).toBe("这次没有耗时记录");
  });
});

describe("时间格式化与读屏句子", () => {
  it("毫秒 / 秒 / 分都有单位与可读小数", () => {
    expect(formatMs(0)).toBe("0 毫秒");
    expect(formatMs(920)).toBe("920 毫秒");
    expect(formatMs(1699)).toBe("1.7 秒");
    expect(formatMs(12_400)).toBe("12 秒");
    expect(formatMs(65_000)).toBe("1 分 5 秒");
    expect(formatMs(null)).toBe("未知");
  });

  it("读屏句子给出总耗时 + 占比最大的几项", () => {
    const timing = buildTurnTiming({
      turn_id: "t",
      duration_ms: 1699,
      phases: {
        residual_ms: 30,
        notes: { queue_wait_ms: 100 },
        spans: [
          { name: "agent_loop", ms: 1000, depth: 1, start_ms: 0 },
          { name: "approval_wait", ms: 900, depth: 2, start_ms: 10 },
          { name: "model_wait", ms: 60, depth: 2, start_ms: 910 },
        ],
      },
    });
    const sentence = buildTimingSentence(timing);
    expect(sentence).toContain("总耗时 1.8 秒");
    expect(sentence).toContain("等待确认 900 毫秒");
  });
});

describe("长 trace 的上限策略", () => {
  it("5000 个 span 的归属计算仍在毫秒级（DOM 行数只与分类数有关）", () => {
    const spans = [{ name: "agent_loop", ms: 100_000, depth: 1, start_ms: 0 }];
    for (let i = 0; i < 2500; i += 1) {
      spans.push({ name: "model_wait", ms: 20, depth: 2, start_ms: i * 40 + 1 } as never);
      spans.push({ name: "tool_wait", ms: 20, depth: 2, start_ms: i * 40 + 21 } as never);
    }
    const started = performance.now();
    const timing = buildTurnTiming({
      turn_id: "turn_huge",
      duration_ms: 100_000,
      phases: { residual_ms: 0, notes: {}, spans },
    });
    const elapsed = performance.now() - started;
    // eslint-disable-next-line no-console
    console.log(`[perf] 5001-span 归属计算 ${elapsed.toFixed(1)}ms`);
    // 行数只与分类数有关，不会随 span 数增长
    expect(timing!.rows.length).toBeLessThanOrEqual(10);
    expect(elapsed).toBeLessThan(100);
  });
});

describe("attributeSpans", () => {
  it("父阶段减去子阶段，父子不重复计", () => {
    const owned = attributeSpans([
      { name: "parent", ms: 100, depth: 1, start_ms: 0 },
      { name: "kid_a", ms: 30, depth: 2, start_ms: 10 },
      { name: "kid_b", ms: 20, depth: 2, start_ms: 50 },
    ]);
    expect(owned).toEqual([
      { name: "kid_a", ms: 30 },
      { name: "kid_b", ms: 20 },
      { name: "parent", ms: 50 },
    ]);
  });

  it("乱序 / 缺 start_ms 也不抛", () => {
    const owned = attributeSpans([
      { name: "b", ms: 10, depth: 1 },
      { name: "a", ms: 10, depth: 1 },
    ]);
    expect(owned.reduce((s, o) => s + o.ms, 0)).toBe(20);
  });
});
