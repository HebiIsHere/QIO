/**
 * #78 / #79 + 文案 5/6/7（RecoveryInbox）。
 *
 * #78：focusRecordId 变化时滚动并聚焦同一条；目标已不存在时给真实反馈。
 * #79：暴露 handleEscape()：先收内层详情/确认，返回 true；没有内层时返回 false（对话页再关弹窗）。
 * 文案 5：整块只有一个标题「未完成事项 · N」。
 * 文案 6：具体后果写在对应动作旁。
 * 文案 7：多个动作共用的禁用原因只显示一次，不同原因分别保留。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  fetchRecoveryRecords: vi.fn(),
  continueRecovery: vi.fn(),
  repairOrphan: vi.fn(),
  ignoreRecovery: vi.fn(),
  requeueDerived: vi.fn(),
}));

vi.mock("../../services/recoveryApi", () => ({
  fetchRecoveryRecords: (...a: unknown[]) => mocks.fetchRecoveryRecords(...a),
  continueRecovery: (...a: unknown[]) => mocks.continueRecovery(...a),
  repairOrphan: (...a: unknown[]) => mocks.repairOrphan(...a),
  ignoreRecovery: (...a: unknown[]) => mocks.ignoreRecovery(...a),
  requeueDerived: (...a: unknown[]) => mocks.requeueDerived(...a),
  isRecoveryConflict: (e: unknown) => (e as { status?: number })?.status === 409,
}));

vi.mock("../../services/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(detail);
    }
  },
  api: {
    getSessionContext: vi.fn(async () => ({ topic_id: "", messages: [] })),
    getRuntimeState: vi.fn(async () => ({
      instance_id: "i",
      revision: 1,
      turn_queue: { instance_id: "i", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
    })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
  },
}));

import RecoveryInbox from "../RecoveryInbox.vue";
import { useSessionStore } from "../../stores/session";
import type { RecoveryActionView, RecoveryRecordView } from "../../services/recoveryApi";

function record(
  actions: RecoveryActionView[],
  overrides: Partial<RecoveryRecordView> = {},
): RecoveryRecordView {
  return {
    record_id: "turn_a",
    kind: "user_turn",
    state_class: "ready",
    status: "interrupted",
    message: "帮我把发布闸门跑一遍",
    topic_id: "topic_1",
    reason: "queued_at_restart",
    created_at: "2026-10-01T10:00:00+00:00",
    updated_at: "2026-10-01T10:05:00+00:00",
    owner_instance_id: null,
    owner_state: "none",
    owner_note: "这条消息在上次退出时没有开始执行",
    claim_generation: null,
    attempts: null,
    last_error: null,
    actions,
    ...overrides,
  };
}

const CONTINUE: RecoveryActionView = { id: "continue", label: "继续发送这条", enabled: true, reason: "" };
const IGNORE: RecoveryActionView = { id: "ignore", label: "忽略", enabled: true, reason: "" };

function mountInbox(records: RecoveryRecordView[], props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.recoveryRecords = records;
  session.recoveryTotal = records.length;
  const w = mount(RecoveryInbox, {
    attachTo: document.body,
    props,
    global: { plugins: [pinia] },
  });
  return { w, session };
}

const scrolled: Element[] = [];
const originalScrollIntoView = Element.prototype.scrollIntoView;

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = "";
  scrolled.length = 0;
  Element.prototype.scrollIntoView = function scrollIntoView(this: Element) {
    scrolled.push(this);
  } as typeof Element.prototype.scrollIntoView;
  mocks.fetchRecoveryRecords.mockResolvedValue({ records: [], total: 0, shown: 0, truncated: false });
  mocks.continueRecovery.mockResolvedValue({
    ok: true,
    record_id: "turn_a",
    turn_id: "turn_new",
    status: "queued",
  });
  mocks.ignoreRecovery.mockResolvedValue({ ok: true, ignored: true });
});

afterEach(() => {
  if (originalScrollIntoView) Element.prototype.scrollIntoView = originalScrollIntoView;
  document.body.innerHTML = "";
});

describe("文案 5：未完成事项只有一个标题", () => {
  it("标题是「未完成事项 · N」，不再写「有 N 条未完成事项」", () => {
    const { w } = mountInbox([record([CONTINUE, IGNORE]), record([CONTINUE], { record_id: "turn_b" })]);
    const title = w.find(".title").text();
    expect(title).toBe("未完成事项 · 2");
    expect(title).not.toContain("有 2 条");
    expect(w.findAll(".title")).toHaveLength(1);
  });
});

describe("文案 6：具体后果写在对应动作旁", () => {
  it("继续与忽略各自带自己的后果说明", () => {
    const { w } = mountInbox([record([CONTINUE, IGNORE])]);
    const text = w.find(".item").text();
    expect(text).toContain("按原话题重新排队，不会新建一条");
    expect(text).toContain("不再提示；原文与记录仍然保留");
  });
});

describe("文案 7：共用的禁用原因只显示一次", () => {
  it("两个动作共用同一原因时只出现一次；不同原因分别保留", () => {
    const shared = "无法确认上次的写入者是否已停止";
    const { w } = mountInbox([
      record([
        { id: "continue", label: "继续发送这条", enabled: false, reason: shared },
        { id: "ignore", label: "忽略", enabled: false, reason: shared },
        { id: "repair", label: "修好这条记录", enabled: false, reason: "这条记录的重发关系没有写成" },
      ]),
    ]);
    const blocked = w.findAll(".blocked");
    expect(blocked).toHaveLength(2);
    const texts = blocked.map((node) => node.text());
    expect(texts.filter((text) => text.includes(shared))).toHaveLength(1);
    expect(texts.find((text) => text.includes(shared))).toContain("继续发送这条、忽略");
    expect(texts.some((text) => text.includes("这条记录的重发关系没有写成"))).toBe(true);
  });
});

describe("#78 定位到指定记录", () => {
  it("focusRecordId 变化：滚动到同一条并聚焦它的第一个可用动作", async () => {
    const { w } = mountInbox([record([CONTINUE, IGNORE])]);
    await w.setProps({ focusRecordId: "turn_a" });
    await flushPromises();
    expect(scrolled.length).toBe(1);
    expect((scrolled[0] as HTMLElement).dataset.recordId).toBe("turn_a");
    expect(document.activeElement?.textContent).toContain("继续发送这条");
    expect(w.find(".item").classes()).toContain("focused");
  });

  it("要定位的记录已不存在：给出真实反馈，不静默", async () => {
    const { w } = mountInbox([record([CONTINUE, IGNORE])]);
    await w.setProps({ focusRecordId: "turn_gone" });
    await flushPromises();
    const error = w.find(".focus-error");
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("已经不存在");
    expect(w.find(".item").classes()).not.toContain("focused");
  });
});

describe("#79 Esc 分层", () => {
  it("handleEscape 先收内层详情（返回 true），没有内层时返回 false", async () => {
    const { w } = mountInbox([record([CONTINUE, IGNORE])]);
    await w.find("button.btn-details").trigger("click");
    expect(w.find(".details").exists()).toBe(true);

    const vm = w.vm as unknown as { handleEscape(): boolean };
    expect(vm.handleEscape()).toBe(true);
    await flushPromises();
    expect(w.find(".details").exists()).toBe(false);
    expect(vm.handleEscape()).toBe(false);
    // 记录与动作不受影响
    expect(w.find("button.btn-continue").exists()).toBe(true);
  });
});
