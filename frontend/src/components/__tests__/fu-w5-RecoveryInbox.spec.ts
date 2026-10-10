/**
 * 收件箱组件（A01 + A03）：用户**真的能操作**的那个界面。
 *
 * 守的是任务书里点名的几条：
 * 1. 安静列表 + 每条显示原文（截断）、原状态、「为什么现在动不了」；
 * 2. 可用动作按钮能点，点了真的打到接口；禁用动作给出原因，绝不给点了没效果的按钮；
 * 3. 详情默认收起；
 * 4. 失败就地显示原因 + 重试，不弹全局提示、不静默；
 * 5. 数量上限时显示「还有 N 条未显示」；
 * 6. 颜色一律 `var(--*)`，不硬编码色值。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const fetchRecoveryRecords = vi.fn();
const continueRecovery = vi.fn();
const repairOrphan = vi.fn();
const ignoreRecovery = vi.fn();
const requeueDerived = vi.fn();

vi.mock("../../services/recoveryApi", () => ({
  fetchRecoveryRecords: (...a: unknown[]) => fetchRecoveryRecords(...a),
  continueRecovery: (...a: unknown[]) => continueRecovery(...a),
  repairOrphan: (...a: unknown[]) => repairOrphan(...a),
  ignoreRecovery: (...a: unknown[]) => ignoreRecovery(...a),
  requeueDerived: (...a: unknown[]) => requeueDerived(...a),
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
import source from "../RecoveryInbox.vue?raw";

function readyRecord(overrides: Partial<RecoveryRecordView> = {}): RecoveryRecordView {
  return {
    record_id: "turn_a",
    kind: "user_turn",
    state_class: "ready",
    status: "interrupted",
    message: "帮我把发布闸门跑一遍，再核对一下 sidecar 是不是最新的",
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
    actions: [
      { id: "continue", label: "继续发送这条", enabled: true, reason: "" },
      { id: "ignore", label: "忽略", enabled: true, reason: "" },
    ],
    ...overrides,
  };
}

function blockedRecord(): RecoveryRecordView {
  return readyRecord({
    record_id: "turn_unknown",
    state_class: "owner_unknown",
    status: "running",
    owner_state: "unknown",
    owner_note: "无法确认上次的写入者是否已停止",
    actions: [
      {
        id: "continue",
        label: "继续发送这条",
        enabled: false,
        reason: "无法确认上次的写入者是否已停止",
      },
    ],
  });
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

beforeEach(() => {
  vi.clearAllMocks();
  document.body.innerHTML = "";
  fetchRecoveryRecords.mockResolvedValue({ records: [], total: 0, shown: 0, truncated: false });
  continueRecovery.mockResolvedValue({ ok: true, record_id: "turn_a", turn_id: "turn_new", status: "queued" });
  repairOrphan.mockResolvedValue({ ok: true, repaired: true, record_id: "turn_x" });
  ignoreRecovery.mockResolvedValue({ ok: true, ignored: true });
  requeueDerived.mockResolvedValue({ ok: true, state: "pending" });
});

describe("清单本身", () => {
  it("没有记录、没有错误、也不在读取时：什么都不渲染", () => {
    const { w } = mountInbox([]);
    expect(w.find(".recovery-inbox").exists()).toBe(false);
  });

  it("每条显示原文、原状态与「为什么动不了」，并可展开详情（默认收起）", async () => {
    const { w } = mountInbox([readyRecord()]);

    expect(w.find(".title").text()).toContain("有 1 条未完成事项");
    const item = w.find(".item");
    // 原文在 DOM 里是完整的（视觉两行截断，读屏读到全文）
    expect(item.find(".message").text()).toBe(readyRecord().message);
    expect(item.find(".status").text()).toBe("interrupted");
    expect(item.find(".why").text()).toContain("没有开始执行");
    // 详情默认收起
    expect(item.find(".details").exists()).toBe(false);
    await item.find("button.btn-details").trigger("click");
    expect(item.find(".details").exists()).toBe(true);
    expect(w.find("button.btn-details").attributes("aria-expanded")).toBe("true");
  });

  it("禁用动作给出原因，并且真的点不动", async () => {
    const { w } = mountInbox([blockedRecord()]);

    const button = w.find("button.btn-continue");
    expect(button.attributes("disabled")).toBeDefined();
    expect(button.attributes("title")).toContain("无法确认上次的写入者是否已停止");
    expect(w.find(".blocked").text()).toContain("无法确认上次的写入者是否已停止");

    await button.trigger("click");
    expect(continueRecovery).not.toHaveBeenCalled();
  });

  it("数量上限时明说「还有 N 条未显示」", () => {
    const { w } = mountInbox([readyRecord()], 7);
    expect(w.find(".truncated").text()).toContain("还有 6 条未显示");
  });

  it("读完没有截断时不出现这句（不制造假的数量）", () => {
    const { w } = mountInbox([readyRecord()], 1);
    expect(w.find(".truncated").exists()).toBe(false);
  });
});

describe("动作真的落到接口上", () => {
  it("点「继续发送这条」→ 调接口，成功后那一条就地消失", async () => {
    const { w, session } = mountInbox([readyRecord()]);

    await w.find("button.btn-continue").trigger("click");
    await flushPromises();

    expect(continueRecovery).toHaveBeenCalledWith("turn_a", {
      expected_class: "ready",
      expected_status: "interrupted",
    });
    expect(session.recoveryRecords).toHaveLength(0);
    expect(w.find(".item").exists()).toBe(false);
  });

  it("点「修好」→ 调 repair，成功后同一条就地变成可继续", async () => {
    const { w, session } = mountInbox([
      readyRecord({
        record_id: "turn_x",
        state_class: "orphaned_claim",
        status: "interrupted",
        owner_note: "这条记录的重发关系没有写成",
        actions: [{ id: "repair", label: "修好这条记录", enabled: true, reason: "" }],
      }),
    ]);

    await w.find("button.btn-repair").trigger("click");
    await flushPromises();

    expect(repairOrphan).toHaveBeenCalledWith("turn_x", "orphaned_claim");
    expect(session.recoveryRecords[0]?.state_class).toBe("ready");
    expect(w.find("button.btn-continue").exists()).toBe(true);
    expect(w.find(".inline-notice").text()).toContain("已经修好");
  });

  it("点「忽略」→ 调 ignore，成功后那一条消失", async () => {
    const { w, session } = mountInbox([readyRecord()]);

    await w.find("button.btn-ignore").trigger("click");
    await flushPromises();

    expect(ignoreRecovery).toHaveBeenCalledWith("turn_a", "ready");
    expect(session.recoveryRecords).toHaveLength(0);
  });

  it("提交中禁用按钮：重复点击不发第二次", async () => {
    let release: (v: unknown) => void = () => {};
    continueRecovery.mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve; }) as never,
    );
    const { w } = mountInbox([readyRecord()]);

    await w.find("button.btn-continue").trigger("click");
    await flushPromises();
    expect(w.find("button.btn-continue").text()).toContain("提交中…");
    expect(w.find("button.btn-continue").attributes("disabled")).toBeDefined();

    await w.find("button.btn-continue").trigger("click");
    expect(continueRecovery).toHaveBeenCalledTimes(1);

    release({ ok: true, record_id: "turn_a", turn_id: "turn_new", status: "queued" });
    await flushPromises();
  });
});

describe("失败就地说话 + 可重试", () => {
  it("失败 → 那一条下面显示原因与重试按钮，记录不被清掉", async () => {
    continueRecovery.mockRejectedValueOnce(new Error("fetch failed"));
    const { w, session } = mountInbox([readyRecord()]);

    await w.find("button.btn-continue").trigger("click");
    await flushPromises();

    expect(session.recoveryRecords).toHaveLength(1);
    const err = w.find(".inline-error");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("fetch failed");
    expect(err.find("button.btn-retry").exists()).toBe(true);

    // 重试就是同一个动作再点一次：这次成功
    await err.find("button.btn-retry").trigger("click");
    await flushPromises();
    expect(continueRecovery).toHaveBeenCalledTimes(2);
    expect(session.recoveryRecords).toHaveLength(0);
  });

  it("列表整体失败 → 保留已显示记录，提示不完整 + 一个整体重试", async () => {
    const { w, session } = mountInbox([readyRecord()]);
    session.recoveryError = "没能读取「未完成事项」清单：network down（已显示的记录仍然可以处理，可以重试）";
    await flushPromises();

    const listError = w.find(".list-error");
    expect(listError.exists()).toBe(true);
    expect(listError.text()).toContain("network down");
    expect(w.find(".item").exists()).toBe(true);

    await w.find("button.btn-retry-list").trigger("click");
    await flushPromises();
    expect(fetchRecoveryRecords).toHaveBeenCalledTimes(1);
  });

  it("清单为空但读取失败时，仍然把原因留在屏幕上（不整块消失）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.recoveryError = "没能读取「未完成事项」清单：network down（可以重试）";
    const w = mount(RecoveryInbox, { global: { plugins: [pinia] } });

    expect(w.find(".recovery-inbox").exists()).toBe(true);
    expect(w.find(".list-error").text()).toContain("network down");
  });
});

describe("不抢焦点、样式守规矩", () => {
  it("出现与展开都不抢走用户当前的焦点，也不 autofocus", async () => {
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    const { w } = mountInbox([readyRecord()]);
    await flushPromises();
    expect(document.activeElement).toBe(input);

    await w.find("button.btn-details").trigger("click");
    await flushPromises();
    expect(document.activeElement).toBe(input);
    expect(w.html()).not.toContain("autofocus");
  });

  it("Esc 只收起本块展开的详情，不动任何记录", async () => {
    const { w, session } = mountInbox([readyRecord()]);
    await w.find("button.btn-details").trigger("click");
    expect(w.find(".details").exists()).toBe(true);

    await w.find(".recovery-inbox").trigger("keydown", { key: "Escape" });
    expect(w.find(".details").exists()).toBe(false);
    expect(session.recoveryRecords).toHaveLength(1);
  });

  it("颜色一律 var(--*)，没有硬编码色值；键盘焦点有可见样式", () => {
    const style = source.slice(source.indexOf("<style"));
    const hex = style.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [];
    const rgb = style.match(/\brgba?\(/g) ?? [];
    expect(hex).toEqual([]);
    expect(rgb).toEqual([]);
    expect(style).toContain("var(--");
    expect(style).toContain(":focus-visible");
  });
});
