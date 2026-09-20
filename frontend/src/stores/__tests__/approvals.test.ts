import { describe, expect, it, vi, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useApprovalsStore } from "../approvals";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: { respondApproval: vi.fn(async () => ({ ok: true })) },
}));

const respond = api.respondApproval as unknown as ReturnType<typeof vi.fn>;

function store() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return useApprovalsStore();
}

beforeEach(() => {
  vi.clearAllMocks();
  respond.mockResolvedValue({ ok: true });
});

describe("approvals store 状态可信性", () => {
  it("成功：approval 出队，下一项成为 current，错误清空", async () => {
    const s = store();
    s.enqueue("a1", "tool_create", { name: "t" });
    s.enqueue("a2", "tool_create", { name: "u" });

    await s.respond("approved");

    expect(respond).toHaveBeenCalledWith(
      "a1",
      "approved",
      undefined,
      { turnId: null, sessionId: null, requestDigest: null },
    );
    expect(s.current?.approval_id).toBe("a2");
    expect(s.error).toBeNull();
  });

  it("失败：approval 保留在队列，错误可见，可再次点击重试", async () => {
    const s = store();
    s.enqueue("a1", "tool_create", { name: "t" });
    respond.mockRejectedValueOnce(new Error("POST /api/approvals -> 500: boom"));

    await s.respond("approved");

    // 失败 ≠ 已授权：item 仍在，且能重试
    expect(s.current?.approval_id).toBe("a1");
    expect(s.error).toContain("未做出任何授权");
    expect(s.error).toContain("500");
    expect(s.responding).toBeNull();

    await s.respond("approved");
    expect(s.current).toBeNull();
    expect(s.error).toBeNull();
  });

  it("失败后拒绝也可重试成功（reject 路径）", async () => {
    const s = store();
    s.enqueue("a1", "credential_grant", {});
    respond.mockRejectedValueOnce(new Error("network down"));

    await s.respond("rejected");
    expect(s.current?.approval_id).toBe("a1");

    await s.respond("rejected");
    expect(respond).toHaveBeenLastCalledWith(
      "a1",
      "rejected",
      undefined,
      { turnId: null, sessionId: null, requestDigest: null },
    );
    expect(s.current).toBeNull();
  });

  it("提交中：responding 指向当前项，重复点击不重复发请求", async () => {
    const s = store();
    s.enqueue("a1", "tool_create", {});
    let release!: (v: { ok: boolean }) => void;
    respond.mockImplementationOnce(() => new Promise((r) => { release = r; }));

    const first = s.respond("approved");
    expect(s.responding).toBe("a1");
    const second = s.respond("approved"); // 双提交防护：直接忽略

    expect(respond).toHaveBeenCalledTimes(1);
    release({ ok: true });
    await first;
    await second;
    expect(s.responding).toBeNull();
    expect(s.current).toBeNull();
  });

  it("空队列 respond 不发请求", async () => {
    const s = store();
    await s.respond("approved");
    expect(respond).not.toHaveBeenCalled();
  });

  it("重复 approval_id 不重复入队，overrides 透传", async () => {
    const s = store();
    s.enqueue("a1", "tool_create", {});
    s.enqueue("a1", "tool_create", {});
    expect(s.queue.length).toBe(1);

    await s.respond("approved", { subagent_budget: { max_iterations: 3 } });
    expect(respond).toHaveBeenCalledWith(
      "a1",
      "approved",
      { subagent_budget: { max_iterations: 3 } },
      { turnId: null, sessionId: null, requestDigest: null },
    );
  });

  /**
   * 实测遇到的死结：审批在后端已经不存在（别处应答 / 已过期）时，respond 会一直 404，
   * 队列里那一项既批准不了也拒绝不了 —— 弹窗只能反复重试失败。
   * 处理原则：不静默消失（用户得知道发生了什么），但要有一个出口。
   */
  it("后端说这项审批已经有结局（404）：标记为失效而不是变成点不掉的死结", async () => {
    const s = store();
    s.enqueue("a1", "tool_create", { name: "t" });
    respond.mockRejectedValueOnce(
      Object.assign(
        new Error("/api/approvals/a1/respond -> 404: approval not found or already answered"),
        { status: 404 },
      ),
    );

    await s.respond("rejected");

    // 不静默消失：它还在，但被明确标记为失效
    expect(s.current?.approval_id).toBe("a1");
    expect(s.current?.stale).toBe(true);
    expect(s.error).toContain("已经有结局");
    expect(s.error).toContain("未做出任何授权");

    // 失效之后不再重复发请求
    await s.respond("approved");
    expect(respond).toHaveBeenCalledTimes(1);

    // 用户可以把它清掉（UI 上是一个「知道了」）
    s.resolve("a1");
    expect(s.current).toBeNull();
  });

  it("普通失败（非 404）仍然保留待办、可以重试（不能因为这条改动把失败也吞掉）", async () => {
    const s = store();
    s.enqueue("a1", "tool_create", { name: "t" });
    respond.mockRejectedValueOnce(Object.assign(new Error("boom"), { status: 500 }));

    await s.respond("approved");

    expect(s.current?.approval_id).toBe("a1");
    expect(s.current?.stale).toBeUndefined();
    expect(s.error).toContain("可重试");
  });

  it("应答时原样回传审批的身份绑定（turn / session / request digest）", async () => {
    const s = store();
    s.enqueue("a1", "computer", { action: "read" }, {
      turnId: "turn_a",
      sessionId: "sess_a",
      requestDigest: "digest_a",
    });

    await s.respond("approved");

    expect(respond).toHaveBeenCalledWith("a1", "approved", undefined, {
      turnId: "turn_a",
      sessionId: "sess_a",
      requestDigest: "digest_a",
    });
  });
});
