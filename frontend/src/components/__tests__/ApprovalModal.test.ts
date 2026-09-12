import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import ApprovalModal from "../ApprovalModal.vue";
import { useApprovalsStore } from "../../stores/approvals";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: { respondApproval: vi.fn(async () => ({ ok: true })) },
}));

const respond = api.respondApproval as unknown as ReturnType<typeof vi.fn>;

function mountModal(): { w: ReturnType<typeof mount>; s: ReturnType<typeof useApprovalsStore>; pinia: Pinia } {
  const pinia = createPinia();
  setActivePinia(pinia);
  const s = useApprovalsStore();
  const w = mount(ApprovalModal, { attachTo: document.body, global: { plugins: [pinia] } });
  return { w, s, pinia };
}

beforeEach(() => {
  vi.clearAllMocks();
  respond.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
});

describe("ApprovalModal 失败可见性与可重试", () => {
  it("成功批准：modal 关闭", async () => {
    const { w, s } = mountModal();
    s.enqueue("a1", "tool_create", { name: "t", explanation: "做一个工具" });
    await flushPromises();
    expect(w.find(".modal").exists()).toBe(true);

    await w.find(".approve").trigger("click");
    await flushPromises();
    expect(w.find(".modal-mask").exists()).toBe(false);
    w.unmount();
  });

  it("请求失败：modal 保留并显示「未做出任何授权」，按钮可再次点击", async () => {
    const { w, s } = mountModal();
    respond.mockRejectedValueOnce(new Error("POST /api/approvals -> 500: boom"));
    s.enqueue("a1", "tool_create", { name: "t" });
    await flushPromises();

    await w.find(".approve").trigger("click");
    await flushPromises();

    expect(w.find(".modal").exists()).toBe(true);
    const err = w.find(".approval-error");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("未做出任何授权");
    const approveBtn = w.find(".approve");
    expect(approveBtn.attributes("disabled")).toBeUndefined();

    // 重试成功 → 关闭
    await approveBtn.trigger("click");
    await flushPromises();
    expect(w.find(".modal-mask").exists()).toBe(false);
    w.unmount();
  });

  it("请求进行中：两个按钮 disabled 且显示提交中，禁止双提交", async () => {
    const { w, s } = mountModal();
    let release!: (v: { ok: boolean }) => void;
    respond.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    s.enqueue("a1", "tool_create", { name: "t" });
    await flushPromises();

    await w.find(".approve").trigger("click");
    await flushPromises();

    expect(w.find(".approve").attributes("disabled")).toBeDefined();
    expect(w.find(".reject").attributes("disabled")).toBeDefined();
    expect(w.find(".approve").text()).toContain("提交中");

    await w.find(".reject").trigger("click");
    expect(respond).toHaveBeenCalledTimes(1);

    release({ ok: true });
    await flushPromises();
    w.unmount();
  });

  it("对话框具备 role=dialog / aria-modal / 标题关联，且初始焦点不落在批准按钮", async () => {
    const { w, s } = mountModal();
    s.enqueue("a1", "tool_create", { name: "t" });
    await flushPromises();
    const dialog = w.find(".modal");
    expect(dialog.attributes("role")).toBe("dialog");
    expect(dialog.attributes("aria-modal")).toBe("true");
    const labelId = dialog.attributes("aria-labelledby");
    expect(labelId).toBeTruthy();
    expect(w.find(`#${labelId}`).exists()).toBe(true);
    // 高风险审批不默认把焦点放在「允许」上（Enter 不应直接批准）
    expect(document.activeElement?.classList.contains("approve")).toBe(false);
    w.unmount();
  });
});
