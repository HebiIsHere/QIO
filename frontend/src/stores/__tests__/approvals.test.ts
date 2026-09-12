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

    expect(respond).toHaveBeenCalledWith("a1", "approved", undefined);
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
    expect(respond).toHaveBeenLastCalledWith("a1", "rejected", undefined);
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
    expect(respond).toHaveBeenCalledWith("a1", "approved", { subagent_budget: { max_iterations: 3 } });
  });
});
