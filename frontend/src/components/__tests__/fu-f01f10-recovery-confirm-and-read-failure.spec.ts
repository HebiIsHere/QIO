/**
 * F01 + F05 的前端回归（本轮收口）。
 *
 * 守两件事：
 *
 * 1. **F01**：无归属记录（旧版本写的行）默认只可见 ——「继续 / 知道了」点不动，
 *    唯一的解锁入口是「确认旧执行者已停止」；它先弹一次就地确认（不执行任何东西），
 *    确认之后才调接口、并按服务端重读的结果刷新可用性。
 * 2. **F05**：清单读取失败（后端现在是明确的 503）时**保留上一次可见的内容**，
 *    如实说明「这一份可能不完整」并给出可重试入口 —— 绝不把「拉不到」说成
 *    「没有未完成的事」。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const fetchRecoveryRecords = vi.fn();
const continueRecovery = vi.fn();
const repairOrphan = vi.fn();
const ignoreRecovery = vi.fn();
const requeueDerived = vi.fn();
const confirmStopped = vi.fn();

vi.mock("../../services/recoveryApi", () => ({
  fetchRecoveryRecords: (...a: unknown[]) => fetchRecoveryRecords(...a),
  continueRecovery: (...a: unknown[]) => continueRecovery(...a),
  repairOrphan: (...a: unknown[]) => repairOrphan(...a),
  ignoreRecovery: (...a: unknown[]) => ignoreRecovery(...a),
  requeueDerived: (...a: unknown[]) => requeueDerived(...a),
  confirmStopped: (...a: unknown[]) => confirmStopped(...a),
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
import type { RecoveryRecordView } from "../../services/recoveryApi";

function legacyRecord(overrides: Partial<RecoveryRecordView> = {}): RecoveryRecordView {
  return {
    record_id: "turn_old",
    kind: "user_turn",
    state_class: "legacy_unowned",
    status: "running",
    message: "旧版本还在跑的消息",
    topic_id: null,
    reason: null,
    created_at: "2026-10-01T10:00:00+00:00",
    updated_at: "2026-10-01T10:05:00+00:00",
    owner_instance_id: null,
    owner_state: "none",
    owner_note: "这条记录来自没有实例归属的旧版本，无法确认它的执行者是否已经停止",
    claim_generation: null,
    attempts: null,
    last_error: null,
    confirmed_stopped: false,
    actions: [
      {
        id: "continue",
        label: "继续",
        enabled: false,
        reason: "这条记录来自没有实例归属的旧版本，无法确认它的执行者是否已经停止；确认旧执行者已停止后才能操作",
      },
      {
        id: "ignore",
        label: "知道了",
        enabled: false,
        reason: "这条记录来自没有实例归属的旧版本，无法确认它的执行者是否已经停止；确认旧执行者已停止后才能操作",
      },
      { id: "confirm_stopped", label: "确认旧执行者已停止", enabled: true, reason: "" },
    ],
    ...overrides,
  };
}

function mountInbox(records: RecoveryRecordView[], total = records.length) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.recoveryRecords = records;
  session.recoveryTotal = total;
  const w = mount(RecoveryInbox, { attachTo: document.body, global: { plugins: [pinia] } });
  return { w, session };
}

function confirmButtons(w: ReturnType<typeof mountInbox>["w"]) {
  return w.findAll(".qio-confirm__actions .qio-btn");
}

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = "";
  fetchRecoveryRecords.mockResolvedValue({ records: [], total: 0, shown: 0, truncated: false });
  continueRecovery.mockResolvedValue({
    ok: true,
    record_id: "turn_old",
    turn_id: "turn_new",
    status: "queued",
  });
  repairOrphan.mockResolvedValue({ ok: true, repaired: true, record_id: "turn_old" });
  ignoreRecovery.mockResolvedValue({ ok: true, ignored: true, record_id: "turn_old" });
  requeueDerived.mockResolvedValue({ ok: true, record_id: "turn_old", state: "pending" });
  confirmStopped.mockResolvedValue({
    ok: true,
    confirmed: true,
    already_confirmed: false,
    confirmed_at: "2026-10-10T00:00:00+00:00",
    record_id: "turn_old",
    kind: "user_turn",
  });
});

describe("F01 无归属记录：确认之前一个状态动作都点不动", () => {
  it("禁用动作给出原因，点了也不发请求", async () => {
    const { w } = mountInbox([legacyRecord()]);

    const cont = w.find("button.btn-continue");
    expect(cont.attributes("disabled")).toBeDefined();
    expect(w.find(".blocked").text()).toContain("旧版本");
    await cont.trigger("click");
    expect(continueRecovery).not.toHaveBeenCalled();
    expect(confirmStopped).not.toHaveBeenCalled();

    const ignore = w.find("button.btn-ignore");
    expect(ignore.attributes("disabled")).toBeDefined();
    await ignore.trigger("click");
    expect(ignoreRecovery).not.toHaveBeenCalled();
  });

  it("「确认旧执行者已停止」先弹就地确认，取消不调接口", async () => {
    const { w } = mountInbox([legacyRecord()]);

    await w.find("button.btn-confirm_stopped").trigger("click");
    expect(w.find(".qio-confirm").exists()).toBe(true);
    expect(w.find(".qio-confirm__detail").text()).toContain("被执行两次");
    expect(confirmStopped).not.toHaveBeenCalled();

    const buttons = confirmButtons(w);
    await buttons[0].trigger("click"); // 取消
    await flushPromises();
    expect(confirmStopped).not.toHaveBeenCalled();
    expect(w.find(".qio-confirm").exists()).toBe(false);
  });

  it("确认之后调接口、重读清单，并把服务端重判的动作展示出来", async () => {
    const { w, session } = mountInbox([legacyRecord()]);
    // 服务端在确认之后重新判定：这条记录变成可继续
    fetchRecoveryRecords.mockResolvedValue({
      records: [
        legacyRecord({
          confirmed_stopped: true,
          owner_note: "你已确认这条记录的旧执行者已经停止",
          actions: [
            { id: "continue", label: "继续", enabled: true, reason: "" },
            { id: "ignore", label: "知道了", enabled: true, reason: "" },
          ],
        }),
      ],
      total: 1,
      shown: 1,
      truncated: false,
    });

    await w.find("button.btn-confirm_stopped").trigger("click");
    await confirmButtons(w)[1].trigger("click"); // 确认已停止
    await flushPromises();

    expect(confirmStopped).toHaveBeenCalledWith("turn_old", "legacy_unowned");
    expect(fetchRecoveryRecords).toHaveBeenCalled();
    expect(session.recoveryRecords[0]?.confirmed_stopped).toBe(true);
    const cont = w.find("button.btn-continue");
    expect(cont.attributes("disabled")).toBeUndefined();
    await cont.trigger("click");
    await flushPromises();
    expect(continueRecovery).toHaveBeenCalledWith("turn_old", {
      expected_class: "legacy_unowned",
      expected_status: "running",
    });
    expect(session.recoveryRecords).toHaveLength(0);
  });

  it("确认失败：就地显示原因，不假装已确认", async () => {
    confirmStopped.mockRejectedValueOnce(new Error("409 conflict"));
    const { w, session } = mountInbox([legacyRecord()]);

    await w.find("button.btn-confirm_stopped").trigger("click");
    await confirmButtons(w)[1].trigger("click");
    await flushPromises();

    expect(w.find(".inline-error").text()).toContain("确认旧执行者已停止");
    expect(session.recoveryRecords[0]?.confirmed_stopped).toBe(false);
    expect(w.find("button.btn-continue").attributes("disabled")).toBeDefined();
  });
});

describe("F05 读取失败：保留旧内容 + 可重试", () => {
  it("503 不会把清单擦成「没有记录」", async () => {
    const { w, session } = mountInbox([legacyRecord()]);

    fetchRecoveryRecords.mockRejectedValueOnce(new Error("503 Service Unavailable"));
    await session.loadRecoveryInbox();
    await flushPromises();

    // 「拉不到」不等于「没有」：旧内容必须留在屏幕上
    expect(session.recoveryRecords).toHaveLength(1);
    expect(session.recoveryTotal).toBe(1);
    expect(w.find(".list-error").exists()).toBe(true);
    expect(w.find(".list-error").text()).toContain("没能读取");
    expect(w.find(".item").exists()).toBe(true);
    expect(w.find("button.btn-retry-list").exists()).toBe(true);
  });

  it("重试成功后恢复为真实清单并清掉错误提示", async () => {
    const { w, session } = mountInbox([]);

    fetchRecoveryRecords.mockRejectedValueOnce(new Error("503 Service Unavailable"));
    await session.loadRecoveryInbox();
    await flushPromises();
    expect(w.find(".list-error").exists()).toBe(true);

    fetchRecoveryRecords.mockResolvedValueOnce({
      records: [legacyRecord()],
      total: 1,
      shown: 1,
      truncated: false,
    });
    await w.find("button.btn-retry-list").trigger("click");
    await flushPromises();

    expect(w.find(".list-error").exists()).toBe(false);
    expect(session.recoveryRecords).toHaveLength(1);
    expect(w.find(".item").exists()).toBe(true);
  });
});
