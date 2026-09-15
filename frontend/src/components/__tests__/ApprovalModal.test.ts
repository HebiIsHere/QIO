import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import ApprovalModal from "../ApprovalModal.vue";
import { useApprovalsStore } from "../../stores/approvals";
import { api } from "../../services/api";
import { nextTick } from "vue";

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
    // 关闭先播短退出动画：此期间仍在 DOM 里但已不可交互（标记 leaving）
    expect(w.find(".modal-mask").classes()).toContain("leaving");
    await new Promise((r) => setTimeout(r, 220));
    await nextTick();
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
    await new Promise((r) => setTimeout(r, 220)); // 等退出动画结束再卸载
    await nextTick();
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

  it("未知 kind：标题用中文兜底，不把后端枚举名直接给用户", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const approvals = useApprovalsStore();
    approvals.enqueue("a9", "brand_new_kind", {});
    const w = mount(ApprovalModal, { global: { plugins: [pinia] } });
    await nextTick();
    expect(w.find("#approval-title").text()).toBe("需要你确认的操作");
    expect(w.find("#approval-title").attributes("title")).toBe("brand_new_kind");
    w.unmount();
  });

  it("审批关闭后把焦点还给被打断的元素（后台审批不留下无焦点状态）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const approvals = useApprovalsStore();
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    expect(document.activeElement).toBe(input);

    approvals.enqueue("a10", "tool_create", { name: "x" });
    const w = mount(ApprovalModal, { attachTo: document.body, global: { plugins: [pinia] } });
    await nextTick();
    expect(document.activeElement).not.toBe(input); // 焦点已进入对话框

    approvals.queue = []; // 审批处理完成 → 对话框关闭
    await nextTick();
    await nextTick();
    expect(document.activeElement).toBe(input);
    w.unmount();
    input.remove();
  });

  it("提交失败后焦点回到对话框（失败保留待审批项，用户可直接重试）", async () => {
    respond.mockRejectedValueOnce(new Error("404 not found"));
    const pinia = createPinia();
    setActivePinia(pinia);
    const approvals = useApprovalsStore();
    approvals.enqueue("a11", "tool_create", { name: "x" });
    const w = mount(ApprovalModal, { attachTo: document.body, global: { plugins: [pinia] } });
    await nextTick();
    await w.find("button.reject").trigger("click");
    await flushPromises();
    await nextTick();
    expect(approvals.queue.length).toBe(1); // 失败不丢待审批项
    expect(document.activeElement?.classList.contains("modal")).toBe(true);
    w.unmount();
  });
});

describe("审批信息顺序：做什么 / 会改变什么 / 为什么需要 / 验证了吗 / 高级详情", () => {
  const CAPS = [
    "联网：是（限 api.example.com）",
    "读取文件：否",
    "写入文件：是",
    "启动进程：否",
    "使用凭据：无",
    "副作用：write",
  ];

  it("写入型工具：显示「会改变什么」的具体行为、为什么需要、已验证 + 折叠的高级详情", async () => {
    const { w, s } = mountModal();
    s.enqueue("t1", "tool_create", {
      name: "fetch_doc",
      description: "抓取指定文档并写入工作区",
      explanation: "用户要求自动归档资料，需要在联网抓取后落盘",
      capabilities: CAPS,
      policy_fingerprint: "fp_123",
      test_summary: "3/3 检查通过",
      test_details: [{ name: "dry_run", passed: true, detail: "无异常" }],
    });
    await flushPromises();

    const text = w.find(".modal").text();
    // 默认可见区域（不含默认折叠的高级详情）
    const main = w.find(".intent").text() + w.find(".risk-row").text() + w.find(".facts").text();
    expect(main).toContain("抓取指定文档并写入工作区"); // 它想做什么
    expect(main).toContain("会联网"); // 它会访问什么
    expect(main).toContain("会写入或修改数据"); // 会改变什么（翻成人话，不显示 write）
    expect(main).not.toContain("副作用：write");
    expect(main).toContain("用户要求自动归档资料"); // 为什么需要
    expect(main).toContain("已验证");
    expect(main).toContain("3/3 检查通过");
    // 说明性字段不再以「创建解释 / 描述 / 测试」重复一遍
    expect(main).not.toContain("创建解释");
    expect(text).not.toContain("创建解释");

    // 高级详情默认折叠：内容在 DOM 里但 details 未 open
    const adv = w.find("details.adv");
    expect(adv.exists()).toBe(true);
    expect(adv.attributes("open")).toBeUndefined();
    expect(adv.text()).toContain("策略指纹：fp_123");
    expect(adv.find("pre.adv-raw").text()).toContain("\"policy_fingerprint\": \"fp_123\"");
    w.unmount();
  });

  it("子 agent 型工具（无自动测试）：显示「未验证」，不冒充已验证", async () => {
    const { w, s } = mountModal();
    s.enqueue("t2", "tool_create", {
      name: "sub",
      tool_type: "subagent",
      explanation: "需要独立子 agent 做长任务",
      capabilities: ["联网：否", "读取文件：否", "写入文件：否", "启动进程：否", "使用凭据：无", "副作用：pure"],
      test_summary: "n/a (subagent)",
    });
    await flushPromises();
    const text = w.find(".modal").text();
    expect(text).toContain("未验证");
    expect(text).toContain("子 agent 型工具，没有自动测试报告");
    expect(text).toContain("不修改任何数据"); // 副作用 pure → 人话
    expect(w.find(".modal").classes()).not.toContain("risk-high"); // 无网络/写入/命令 → 非高风险
    w.unmount();
  });

  it("高风险操作：批准按钮降调（risk-high），理由与行为清单仍然可见", async () => {
    const { w, s } = mountModal();
    s.enqueue("t3", "credential_grant", {
      key_id: "openai",
      tool_name: "call_model",
      capabilities: ["联网：是", "读取文件：否", "写入文件：否", "启动进程：否", "使用凭据：openai", "副作用：read"],
    });
    await flushPromises();
    expect(w.find(".modal").classes()).toContain("risk-high");
    expect(w.find(".modal").text()).toContain("会使用凭据");
    expect(w.find(".modal").text()).toContain("只读取，不修改数据");
    expect(w.find(".approve").exists()).toBe(true);
    w.unmount();
  });

  it("没有任何测试信息时不显示验证行：宁可不显示，也不假装已验证", async () => {
    const { w, s } = mountModal();
    s.enqueue("t4", "tool_create", { name: "plain", capabilities: ["联网：否", "副作用：pure"] });
    await flushPromises();
    expect(w.find(".modal").text()).not.toContain("验证情况");
    w.unmount();
  });
});
