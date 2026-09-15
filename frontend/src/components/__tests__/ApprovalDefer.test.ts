import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import ApprovalModal from "../ApprovalModal.vue";
import ApprovalEntry from "../ApprovalEntry.vue";
import { useApprovalsStore } from "../../stores/approvals";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: { respondApproval: vi.fn(async () => ({ ok: true })) },
}));

const respond = api.respondApproval as unknown as ReturnType<typeof vi.fn>;

function mountBoth() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const s = useApprovalsStore();
  const entry = mount(ApprovalEntry, { attachTo: document.body, global: { plugins: [pinia] } });
  const modal = mount(ApprovalModal, { attachTo: document.body, global: { plugins: [pinia] } });
  return { s, entry, modal };
}

beforeEach(() => {
  vi.clearAllMocks();
  respond.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
});

describe("后台确认的调度：不抢焦点、有入口、不丢待办", () => {
  it("用户正在输入时到达的确认：不自动弹出，改为亮出「有 N 项操作等待确认」入口", async () => {
    const { s, entry, modal } = mountBoth();
    s.enqueue("a1", "tool_create", { name: "t" }, { autoOpen: false });
    await flushPromises();

    expect(modal.find(".modal").exists()).toBe(false);
    expect(s.pendingCount).toBe(1);
    expect(entry.find(".approval-entry").exists()).toBe(true);
    expect(entry.text()).toContain("有 1 项操作等待确认");
  });

  it("点击入口才打开窗口；打开后待办仍在队列里", async () => {
    const { s, entry, modal } = mountBoth();
    s.enqueue("a1", "tool_create", { name: "t" }, { autoOpen: false });
    await flushPromises();

    await entry.find(".approval-entry").trigger("click");
    await flushPromises();

    expect(modal.find(".modal").exists()).toBe(true);
    expect(s.queue.map((q) => q.approval_id)).toEqual(["a1"]);
    expect(entry.find(".approval-entry").exists()).toBe(false);
  });

  it("按下 Esc 不做决定：收起窗口、保留待审批项、入口重新出现", async () => {
    const { s, entry, modal } = mountBoth();
    s.enqueue("a1", "tool_create", { name: "t" });
    await flushPromises();
    expect(modal.find(".modal").exists()).toBe(true);

    await modal.find(".modal").trigger("keydown", { key: "Escape" });
    await flushPromises();
    await new Promise((r) => setTimeout(r, 220));
    await nextTick();

    expect(respond).not.toHaveBeenCalled();
    expect(s.queue.map((q) => q.approval_id)).toEqual(["a1"]);
    expect(modal.find(".modal-mask").exists()).toBe(false);
    expect(entry.find(".approval-entry").exists()).toBe(true);
  });

  it("「稍后处理」只收窗口：既不批准也不拒绝，队列不丢", async () => {
    const { s, modal } = mountBoth();
    s.enqueue("a1", "tool_create", { name: "t" });
    await flushPromises();

    await modal.find(".later").trigger("click");
    await flushPromises();

    expect(respond).not.toHaveBeenCalled();
    expect(s.pendingCount).toBe(1);
    expect(s.visible).toBe(false);
  });

  it("稍后处理后关闭窗口：焦点还给被打断的输入框（不落到 body）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const s = useApprovalsStore();
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    const modal = mount(ApprovalModal, { attachTo: document.body, global: { plugins: [pinia] } });

    s.enqueue("a1", "tool_create", { name: "t" });
    await flushPromises();
    expect(document.activeElement).toBe(modal.find(".modal").element);

    await modal.find(".later").trigger("click");
    await flushPromises();
    await new Promise((r) => setTimeout(r, 220));
    await nextTick();

    expect(document.activeElement).toBe(input);
  });

  it("用户没在输入时到达的确认仍然立即弹出（直接相关的确认不延迟）", async () => {
    const { s, modal } = mountBoth();
    s.enqueue("a1", "tool_create", { name: "t" }, { autoOpen: true });
    await flushPromises();
    expect(modal.find(".modal").exists()).toBe(true);
  });

  it("重复事件不产生重复窗口，也不重复计数", async () => {
    const { s, entry } = mountBoth();
    s.enqueue("a1", "tool_create", { name: "t" }, { autoOpen: false });
    s.enqueue("a1", "tool_create", { name: "t" }, { autoOpen: false });
    await flushPromises();
    expect(s.pendingCount).toBe(1);
    expect(entry.text()).toContain("有 1 项操作等待确认");
  });
});
