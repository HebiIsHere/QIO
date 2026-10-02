/**
 * 耗时面板的视觉检查入口（仅用于截图核对，不是产品页面）。
 *
 * 覆盖四种真实状态：正常一轮、长一轮（多分类）、开发者模式、老数据（没有 phases）。
 * 用假数据渲染 —— 视觉检查不依赖后端，也不联网。
 */
import { createApp, h, ref } from "vue";
import { createPinia } from "pinia";
import "../src/styles/tokens.css";
import TurnTimingPanel from "../src/components/TurnTimingPanel.vue";
import { clearTurnTimingCache } from "../src/composables/useTurnTiming";
import type { TurnTiming } from "../src/services/trace";

type FixtureName = "normal" | "long" | "dev" | "legacy";

interface DemoSpan {
  name: string;
  ms: number;
  depth: number;
  start_ms: number;
  detail?: string | null;
}

/** 真实阶段名的 span 定义（视觉检查要喂给面板和后端一样的形状） */
const FIXTURE_SPANS: Record<FixtureName, DemoSpan[]> = {
  // 顶层阶段铺满 1670ms，residual 29ms → duration 1699ms（与真实账本同构）
  normal: [
    { name: "other", ms: 20, depth: 1, start_ms: 0 },
    { name: "adapter_setup", ms: 182, depth: 1, start_ms: 20 },
    { name: "turn_setup", ms: 25, depth: 1, start_ms: 202 },
    { name: "turn_binding", ms: 40, depth: 1, start_ms: 202 },
    { name: "context_assembly", ms: 145, depth: 1, start_ms: 250 },
    { name: "topic_prediction", ms: 15, depth: 2, start_ms: 255 },
    { name: "persistence", ms: 20, depth: 2, start_ms: 275 },
    { name: "retrieval", ms: 110, depth: 2, start_ms: 300 },
    { name: "agent_loop", ms: 1221, depth: 1, start_ms: 400 },
    { name: "tool_routing", ms: 20, depth: 2, start_ms: 410 },
    { name: "narrative", ms: 10, depth: 2, start_ms: 430 },
    { name: "model_wait", ms: 60, depth: 2, start_ms: 440, detail: "call#1" },
    { name: "approval_wait", ms: 881, depth: 2, start_ms: 520, detail: "budget" },
    { name: "tool_wait", ms: 70, depth: 2, start_ms: 1410, detail: "calls=1" },
    { name: "model_wait", ms: 180, depth: 2, start_ms: 1490, detail: "call#2" },
    { name: "persistence", ms: 17, depth: 1, start_ms: 1630 },
    { name: "memory_post", ms: 12, depth: 1, start_ms: 1650 },
    { name: "finalize", ms: 8, depth: 1, start_ms: 1662 },
  ],
  // 顶层铺满 64_769ms + residual 31ms → duration 64_800ms
  long: [
    { name: "other", ms: 200, depth: 1, start_ms: 0 },
    { name: "adapter_setup", ms: 1200, depth: 1, start_ms: 200 },
    { name: "turn_setup", ms: 300, depth: 1, start_ms: 1400 },
    { name: "turn_binding", ms: 900, depth: 1, start_ms: 1400 },
    { name: "context_assembly", ms: 3800, depth: 1, start_ms: 2300 },
    { name: "topic_prediction", ms: 600, depth: 2, start_ms: 2310 },
    { name: "retrieval", ms: 2100, depth: 2, start_ms: 3000 },
    { name: "agent_loop", ms: 54_669, depth: 1, start_ms: 6100 },
    { name: "tool_routing", ms: 400, depth: 2, start_ms: 6110 },
    { name: "model_wait", ms: 41_069, depth: 2, start_ms: 6600, detail: "call#1" },
    { name: "tool_wait", ms: 12_800, depth: 2, start_ms: 47_700, detail: "calls=3" },
    { name: "memory_post", ms: 900, depth: 1, start_ms: 60_800 },
    { name: "persistence", ms: 2200, depth: 1, start_ms: 61_800 },
    { name: "finalize", ms: 400, depth: 1, start_ms: 64_100 },
  ],
  dev: [
    { name: "brand_new_stage", ms: 40, depth: 1, start_ms: 0 },
    { name: "context_assembly", ms: 60, depth: 1, start_ms: 40 },
    { name: "agent_loop", ms: 200, depth: 1, start_ms: 100 },
    { name: "model_wait", ms: 200, depth: 2, start_ms: 100, detail: "call#1" },
  ],
  legacy: [],
};

const fixtures: Record<FixtureName, TurnTiming> = {
  normal: {
    turnId: "turn_normal",
    totalMs: 1799,
    executionMs: 1699,
    queueMs: 100,
    rows: [
      { key: "queue", label: "排队", ms: 100, percent: 5.6 },
      { key: "prepare", label: "准备环境", ms: 182, percent: 10.1 },
      { key: "context", label: "上下文准备", ms: 210, percent: 11.7 },
      { key: "memory", label: "记忆检索", ms: 120, percent: 6.7 },
      { key: "model", label: "模型", ms: 260, percent: 14.5 },
      { key: "tool", label: "工具", ms: 90, percent: 5.0 },
      { key: "approval", label: "等待确认", ms: 900, percent: 50.0 },
      { key: "save", label: "保存", ms: 37, percent: 2.1 },
    ],
    residualMs: 29,
    afterTurnMs: 420,
    unknownStages: [],
    rawStages: [{ name: "approval_wait", ms: 900, count: 1 }],
    legacy: false,
  },
  long: {
    turnId: "turn_long",
    totalMs: 65_400,
    executionMs: 64_800,
    queueMs: 600,
    rows: [
      { key: "queue", label: "排队", ms: 600, percent: 0.9 },
      { key: "prepare", label: "准备环境", ms: 1200, percent: 1.8 },
      { key: "context", label: "上下文准备", ms: 3400, percent: 5.2 },
      { key: "memory", label: "记忆检索", ms: 2100, percent: 3.2 },
      { key: "model", label: "模型", ms: 41_500, percent: 63.5 },
      { key: "tool", label: "工具", ms: 12_800, percent: 19.6 },
      { key: "approval", label: "等待确认", ms: 0, percent: 0 },
      { key: "save", label: "保存", ms: 2200, percent: 3.4 },
      { key: "organize", label: "记忆整理", ms: 900, percent: 1.4 },
      { key: "other", label: "其它", ms: 700, percent: 1.1 },
    ].filter((r) => r.ms > 0),
    residualMs: 700,
    afterTurnMs: 0,
    unknownStages: [],
    rawStages: [],
    legacy: false,
  },
  dev: {
    turnId: "turn_dev",
    totalMs: 320,
    executionMs: 320,
    queueMs: null,
    rows: [
      { key: "context", label: "上下文准备", ms: 60, percent: 18.8 },
      { key: "model", label: "模型", ms: 200, percent: 62.5 },
      { key: "other", label: "其它", ms: 60, percent: 18.8 },
    ],
    residualMs: 0,
    afterTurnMs: 0,
    unknownStages: ["brand_new_stage"],
    rawStages: [
      { name: "brand_new_stage", ms: 40, count: 2 },
      { name: "model_wait", ms: 200, count: 1 },
      { name: "context_assembly", ms: 60, count: 1 },
    ],
    legacy: false,
  },
  legacy: {
    turnId: "turn_legacy",
    totalMs: 8436,
    executionMs: 8436,
    queueMs: null,
    rows: [],
    residualMs: 0,
    afterTurnMs: 0,
    unknownStages: [],
    rawStages: [],
    legacy: true,
  },
};

/** 用假数据替掉真实请求：视觉检查不碰后端 */
async function installFakeApi(): Promise<void> {
  const mod = await import("../src/services/api");
  (mod.api as unknown as { getTrace: (id: string) => Promise<unknown> }).getTrace = async (id: string) => {
    const name = (Object.keys(fixtures) as FixtureName[]).find((k) => id === fixtures[k].turnId);
    const timing = fixtures[name ?? "normal"];
    return {
      turn_id: id,
      status: "done",
      duration_ms: timing.executionMs,
      phases: {
        total_ms: timing.totalMs,
        sum_ms: timing.executionMs,
        residual_ms: timing.residualMs,
        notes: { queue_wait_ms: timing.queueMs ?? 0 },
        spans: FIXTURE_SPANS[name ?? "normal"],
        after_turn: timing.afterTurnMs ? [{ name: "derived_work", ms: timing.afterTurnMs }] : [],
      },
    };
  };
}

const cases: { name: FixtureName; title: string; hint: string; dev: boolean }[] = [
  { name: "normal", title: "正常一轮（含等待确认与结束后整理）", hint: "展开态：用户分类 + 占比条", dev: false },
  { name: "long", title: "长一轮（65 秒，多分类）", hint: "数字与占比在长耗时下仍然可读", dev: false },
  { name: "dev", title: "开发者模式（含未归类阶段）", hint: "内部阶段名只在开发者模式出现", dev: true },
  { name: "legacy", title: "老数据（没有 phases）", hint: "只给总耗时 + 一句说明，不显示 0 或空白", dev: false },
];

const App = {
  setup() {
    const opened = ref(true);
    return () =>
      h("div", { class: "col" }, [
        ...cases.map((c) =>
          h("section", { class: "case" }, [
            h("h2", c.title),
            h("p", { class: "hint" }, c.hint),
            h("div", { class: "bubble" }, "这是一条助手回答，用来给耗时面板一个真实的落位上下文。"),
            h(TurnTimingPanel, {
              turnId: fixtures[c.name].turnId,
              dev: c.dev,
              "data-case": c.name,
              key: opened.value ? c.name : c.name + "-x",
            }),
          ]),
        ),
      ]);
  },
};

async function main(): Promise<void> {
  await installFakeApi();
  clearTurnTimingCache();
  createApp(App).use(createPinia()).mount("#app");
}

void main();
