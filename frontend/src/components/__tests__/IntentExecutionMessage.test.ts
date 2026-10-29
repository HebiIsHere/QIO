import { describe, expect, it, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import IntentExecutionMessage from "../IntentExecutionMessage.vue";
import MessageItem from "../MessageItem.vue";
import {
  isHiddenIntentExecution,
  useSessionStore,
  type StreamMessage,
} from "../../stores/session";
import type { IntentExecutionRecord } from "../../services/api";

/**
 * 意图执行记录（R2 契约 §四）：
 * 服务端把互动 Turn 的内部指令消息标成 content_type="intent_execution"，并附带展示就绪的
 * intent_execution 对象。前端只认这个字段，不按文本前缀/关键词猜。
 */
const RECORD: IntentExecutionRecord = {
  record_key: "ie:sub_123:int_a,int_b",
  record_head: true,
  title: "整理这组资料",
  status: "completed",
  status_label: "已完成",
  scope: {
    board_name: "研究板面",
    intent_titles: ["整理这组资料", "补充摘要"],
    source: "confirm",
  },
  intents: [
    {
      id: "int_a",
      title: "整理这组资料",
      status: "completed",
      status_label: "已完成",
      attempts: 2,
      failure_reason: "",
      result_ref: { kind: "attachment", label: "结果文件", target: "https://example.com/result" },
    },
    {
      id: "int_b",
      title: "补充摘要",
      status: "completed",
      status_label: "已完成",
      attempts: 1,
      failure_reason: "",
    },
  ],
  attempts: [
    {
      attempt: 1,
      source: "confirm",
      turn_id: "turn_1",
      status: "failed",
      reason: "模型未给出核验结果",
      started_at: "2026-10-29T10:00:00+08:00",
    },
    {
      attempt: 2,
      source: "retry",
      turn_id: "turn_2",
      status: "completed",
      reason: "",
      started_at: "2026-10-29T10:05:00+08:00",
    },
  ],
  result_entry: { kind: "attachment", label: "结果文件", target: "https://example.com/result" },
  authorization_versions: { int_a: 2, int_b: 1 },
  diagnostics: {
    board_id: "board_1",
    submission_id: "sub_123",
    intent_ids: ["int_a", "int_b"],
    turn_ids: ["turn_1", "turn_2"],
    record_key: "ie:sub_123:int_a,int_b",
  },
  verified: true,
  verified_note: "",
};

/** 内部工作文字（旧行为会把它当用户气泡显示；新行为只能出现在折叠详情/诊断里）。 */
const INTERNAL_TEXT =
  "【互动意图执行】请在 board_1 / sub_123 上推进以下意图；结束后调用 update_intent_status。";

function makeMessage(
  over: Partial<StreamMessage> & { role: StreamMessage["role"] },
): StreamMessage {
  return {
    id: "m1",
    content: "hi",
    contentType: "text",
    createdAt: "2026-10-29T10:00:00+08:00",
    ...over,
  };
}

function mountItem(msg: StreamMessage, pinia: Pinia) {
  return mount(MessageItem, {
    props: { message: msg },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

function mountRecord(record: IntentExecutionRecord) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(IntentExecutionMessage, {
    props: { record },
    global: { plugins: [pinia] },
  });
}

beforeEach(() => {
  localStorage.clear();
});

describe("IntentExecutionMessage 默认收起", () => {
  it("只显示意图名称、服务端状态与查看详情入口", () => {
    const w = mountRecord(RECORD);
    const head = w.find('[data-test="intent-execution-head"]');
    expect(head.exists()).toBe(true);
    expect(head.text()).toContain("整理这组资料");
    // 状态只用服务端给的 status_label，前端不自己再推
    expect(head.text()).toContain("已完成");
    expect(head.text()).toContain("查看详情");
    // 默认行不得出现内部编号（契约：内部编号只允许出现在详情折叠区）
    expect(head.text()).not.toContain("board_1");
    expect(head.text()).not.toContain("sub_123");
    expect(head.text()).not.toContain("int_a");
    // 详情默认不可见、不可聚焦
    const detail = w.find('[data-test="intent-execution-detail"]');
    expect(detail.attributes("aria-hidden")).toBe("true");
    expect(detail.attributes("inert")).toBeDefined();
    w.unmount();
  });

  it("展开后给出范围 / 执行经过 / 逐项状态 / 结果入口 / 诊断信息", async () => {
    const w = mountRecord(RECORD);
    await w.find('[data-test="intent-execution-head"]').trigger("click");

    const detail = w.find('[data-test="intent-execution-detail"]');
    expect(detail.attributes("aria-hidden")).toBe("false");
    const text = detail.text();
    // 处理范围
    expect(text).toContain("研究板面");
    expect(text).toContain("确认执行");
    // 执行经过（attempts 时间线）
    expect(text).toContain("第 1 次");
    expect(text).toContain("重试");
    expect(text).toContain("模型未给出核验结果");
    // 逐项状态
    expect(text).toContain("补充摘要");
    // 结果入口
    expect(text).toContain("结果文件");
    const link = w.find('[data-test="intent-result-link"]');
    expect(link.attributes("href")).toBe("https://example.com/result");
    // 诊断信息（内部编号只在详情里）
    expect(text).toContain("board_1");
    expect(text).toContain("sub_123");
    expect(text).toContain("ie:sub_123:int_a,int_b");
    w.unmount();
  });

  it("再次点击收起详情", async () => {
    const w = mountRecord(RECORD);
    const head = w.find('[data-test="intent-execution-head"]');
    await head.trigger("click");
    expect(head.text()).toContain("收起详情");
    await head.trigger("click");
    expect(head.text()).toContain("查看详情");
    expect(w.find('[data-test="intent-execution-detail"]').attributes("aria-hidden")).toBe("true");
    w.unmount();
  });
});

describe("IntentExecutionMessage 不渲染非头部记录", () => {
  it("record_head !== true 时整条记录不渲染", () => {
    const w = mountRecord({ ...RECORD, record_head: false });
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(false);
    expect(w.text()).toBe("");
    w.unmount();
  });
});

describe("IntentExecutionMessage 状态真实性", () => {
  it("verified=false 时显示服务端状态与 verified_note，不冒充已完成", () => {
    const w = mountRecord({
      ...RECORD,
      status: "unknown",
      status_label: "状态无法核对",
      verified: false,
      verified_note: "这组执行记录已无法核对",
      attempts: [],
      intents: [],
      result_entry: null,
    });
    expect(w.find(".qio-state").text()).toBe("状态无法核对");
    expect(w.find('[data-test="intent-execution-verified-note"]').text()).toContain(
      "这组执行记录已无法核对",
    );
    expect(w.text()).not.toContain("已完成");
    w.unmount();
  });
});

describe("MessageItem：意图执行记录与用户消息明确区分", () => {
  it("intent_execution 消息渲染成记录卡，不再把内部文字当用户气泡", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "user",
        content: INTERNAL_TEXT,
        contentType: "intent_execution",
        intentExecution: RECORD,
      }),
      pinia,
    );
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(true);
    expect(w.find(".user-bubble").exists()).toBe(false);
    expect(w.text()).not.toContain(INTERNAL_TEXT);
    w.unmount();
  });

  it("record_head=false 的记录不渲染，也不显示内部文字", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "user",
        content: INTERNAL_TEXT,
        contentType: "intent_execution",
        intentExecution: { ...RECORD, record_head: false },
      }),
      pinia,
    );
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(false);
    expect(w.find(".user-bubble").exists()).toBe(false);
    expect(w.text()).not.toContain(INTERNAL_TEXT);
    w.unmount();
  });

  it("用户自己输入的代码消息保持既有用户气泡展示", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const code = "def add(a, b):\n    return a + b";
    const w = mountItem(makeMessage({ role: "user", content: code }), pinia);
    expect(w.find(".user-bubble").text()).toContain("def add(a, b):");
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(false);
    w.unmount();
  });

  it("服务端对象缺失时按普通消息降级（不报错、不凭空造卡片）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({ role: "user", content: INTERNAL_TEXT, contentType: "intent_execution" }),
      pinia,
    );
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(false);
    expect(w.find(".user-bubble").text()).toContain(INTERNAL_TEXT);
    w.unmount();
  });
});

describe("会话 store：服务端 intent_execution 解析与头部过滤", () => {
  it("历史消息带 intent_execution 时保留记录对象", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const msg = session._historyMessage({
      id: "m1",
      role: "user",
      content: INTERNAL_TEXT,
      content_type: "intent_execution",
      created_at: "2026-10-29T10:00:00+08:00",
      turn_id: "turn_1",
      intent_execution: RECORD,
    });
    expect(msg.contentType).toBe("intent_execution");
    expect(msg.intentExecution?.title).toBe("整理这组资料");
    expect(msg.intentExecution?.status_label).toBe("已完成");
    expect(isHiddenIntentExecution(msg)).toBe(false);
  });

  it("只有 record_head === true 才渲染，其余同 record 的尝试隐藏", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const row = {
      id: "m2",
      role: "user",
      content: INTERNAL_TEXT,
      content_type: "intent_execution",
      created_at: "2026-10-29T10:06:00+08:00",
      turn_id: "turn_2",
      intent_execution: { ...RECORD, record_head: false },
    };
    const msg = session._historyMessage(row);
    expect(isHiddenIntentExecution(msg)).toBe(true);
  });

  it("普通 text 消息即使混入 intent_execution 字段也按普通消息处理", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const msg = session._historyMessage({
      id: "m3",
      role: "user",
      content: "普通问题",
      content_type: "text",
      created_at: "2026-10-29T10:00:00+08:00",
      intent_execution: RECORD,
    });
    expect(msg.intentExecution).toBeUndefined();
    expect(isHiddenIntentExecution(msg)).toBe(false);
  });
});
