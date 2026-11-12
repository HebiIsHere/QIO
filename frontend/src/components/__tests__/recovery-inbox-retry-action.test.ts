/**
 * #77 失败后的「重试」必须重试**原来那个动作**（回归）。
 *
 * 红（修复前）：RecoveryInbox 的重试按钮挑的是「第一个可用动作」——
 * 用户点的是「忽略」，忽略失败后点「重试」却会执行「继续发送」（两件后果完全不同的事）。
 * 另外动作在失败后可能已经失效：开始前必须重新核对这条记录与这个动作是否仍然可用，
 * 不可用就给真实反馈（不是再打一次注定失败的请求，也不是静默）。
 *
 * 绿（修复后）：
 * - 重试保存「原记录 + 原失败动作」，忽略失败只会重试忽略；
 * - 记录已不在清单 / 动作已不可用（删除或禁用）时给出真实原因，且不再调用接口。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
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

const CONTINUE: RecoveryActionView = {
  id: "continue",
  label: "继续发送这条",
  enabled: true,
  reason: "",
};
const IGNORE: RecoveryActionView = { id: "ignore", label: "忽略", enabled: true, reason: "" };

function mountInbox(records: RecoveryRecordView[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.recoveryRecords = records;
  session.recoveryTotal = records.length;
  const w = mount(RecoveryInbox, { attachTo: document.body, global: { plugins: [pinia] } });
  return { w, session };
}

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = "";
  mocks.fetchRecoveryRecords.mockResolvedValue({ records: [], total: 0, shown: 0, truncated: false });
  mocks.continueRecovery.mockResolvedValue({
    ok: true,
    record_id: "turn_a",
    turn_id: "turn_new",
    status: "queued",
  });
  mocks.repairOrphan.mockResolvedValue({ ok: true, repaired: true, record_id: "turn_x" });
  mocks.ignoreRecovery.mockResolvedValue({ ok: true, ignored: true });
  mocks.requeueDerived.mockResolvedValue({ ok: true, state: "pending" });
});

describe("#77 重试保存原记录与原失败动作", () => {
  it("「忽略」失败后点重试：只会再试忽略，绝不改跑「继续发送」", async () => {
    mocks.ignoreRecovery.mockRejectedValueOnce(new Error("后端拒绝了忽略请求"));
    const { w, session } = mountInbox([record([CONTINUE, IGNORE])]);

    await w.find("button.btn-ignore").trigger("click");
    await flushPromises();
    expect(session.recoveryRecords).toHaveLength(1);
    const error = w.find(".inline-error");
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("忽略");

    await error.find("button.btn-retry").trigger("click");
    await flushPromises();

    expect(mocks.ignoreRecovery).toHaveBeenCalledTimes(2);
    expect(mocks.continueRecovery).not.toHaveBeenCalled();
  });

  it("失败动作在新状态里已经不可用：重试给出真实原因，不再盲目调用接口", async () => {
    mocks.ignoreRecovery.mockRejectedValueOnce(new Error("后端拒绝了忽略请求"));
    const { w, session } = mountInbox([record([CONTINUE, IGNORE])]);
    await w.find("button.btn-ignore").trigger("click");
    await flushPromises();
    expect(mocks.ignoreRecovery).toHaveBeenCalledTimes(1);

    // 记录被别处处理/状态变化：这个动作已经禁用，且给出了真实原因
    session.recoveryRecords = [
      record([
        CONTINUE,
        { id: "ignore", label: "忽略", enabled: false, reason: "这条记录已经在别处处理过" },
      ]),
    ];
    await flushPromises();

    await w.find(".inline-error button.btn-retry").trigger("click");
    await flushPromises();

    expect(mocks.ignoreRecovery).toHaveBeenCalledTimes(1);
    expect(w.find(".inline-error").text()).toContain("这条记录已经在别处处理过");
  });

  it("原动作在新状态里已经不存在：重试说清它已不存在，不再调用接口", async () => {
    mocks.ignoreRecovery.mockRejectedValueOnce(new Error("后端拒绝了忽略请求"));
    const { w, session } = mountInbox([record([CONTINUE, IGNORE])]);
    await w.find("button.btn-ignore").trigger("click");
    await flushPromises();
    expect(mocks.ignoreRecovery).toHaveBeenCalledTimes(1);

    // 后端重新给的清单里不再提供「忽略」（只有继续）：原动作已经不存在
    session.recoveryRecords = [record([CONTINUE])];
    await flushPromises();
    await w.find(".inline-error button.btn-retry").trigger("click");
    await flushPromises();

    expect(mocks.ignoreRecovery).toHaveBeenCalledTimes(1);
    expect(mocks.continueRecovery).not.toHaveBeenCalled();
    expect(w.find(".inline-error").text()).toContain("已经不存在");
  });
});
