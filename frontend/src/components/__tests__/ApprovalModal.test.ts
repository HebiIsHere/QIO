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

/**
 * 第三阶段 spec 第 66~70 条：审批必须回答五个问题，其中第 5 个是
 * 「这是一次性还是长期授权」。用户看不到这一条时，无法判断自己同意了什么。
 */
describe("ApprovalModal 授权范围与访问清单", () => {
  it("工具执行审批显示「仅这一次」，并展示具体的访问清单", async () => {
    const { w, s } = mountModal();
    s.enqueue("a-scope-1", "tool_execution", {
      tool: "fs_write",
      arguments: { path: "a.txt" },
      description: "想修改当前项目中的一个文件",
      access: ["写入：a.txt"],
      scope: "once",
      capabilities: ["联网：否", "写入文件：是", "副作用：write"],
    });
    await flushPromises();
    const text = w.find(".modal").text();
    expect(text).toContain("授权范围");
    expect(text).toContain("仅这一次");
    // 具体路径优先于能力枚举（不是只显示 fs_write path=...）
    expect(text).toContain("写入：a.txt");
    w.unmount();
  });

  it("工具注册审批显示「长期生效」", async () => {
    const { w, s } = mountModal();
    s.enqueue("a-scope-2", "tool_create", {
      name: "excel2csv",
      explanation: "把表格转成 CSV",
      capabilities: ["联网：否", "写入文件：是", "副作用：write"],
    });
    await flushPromises();
    const text = w.find(".modal").text();
    expect(text).toContain("授权范围");
    expect(text).toContain("长期生效");
    w.unmount();
  });

  it("审批内容里不出现内部术语（指纹 / 策略哈希）", async () => {
    const { w, s } = mountModal();
    s.enqueue("a-scope-3", "tool_execution", {
      tool: "run_shell",
      description: "想运行一条 shell 命令",
      access: ["启动一个系统命令进程"],
      scope: "once",
      risk: "careful",
      capabilities: ["启动进程：是", "副作用：destructive"],
      policy_fingerprint: "deadbeef",
    });
    await flushPromises();
    // 指纹只允许出现在默认折叠的高级详情里，首屏正文不得出现
    const adv = w.find("details.adv");
    expect(adv.exists()).toBe(true);
    expect(adv.attributes("open")).toBeUndefined(); // 默认折叠 = 用户第一眼看不到
    const firstScreen = w.find(".body").text().replace(adv.text(), "");
    expect(firstScreen).not.toContain("deadbeef");
    expect(firstScreen).toContain("想运行一条 shell 命令");
    w.unmount();
  });

  /**
   * 文件 / 命令 / 进程类审批走 `kind="computer"`（第一阶段的安全边界就是这条链）。
   * 第三阶段要求它也说人话：首屏给行为句与具体访问清单，`action` / 沙箱判定
   * 这些机器可读字段只进高级详情。
   */
  it("电脑操作审批：首屏是行为句与访问清单，动作名只进高级详情", async () => {
    const { w, s } = mountModal();
    s.enqueue("a-comp-1", "computer", {
      action: "run_shell",
      cmd: "echo hello",
      risk: "danger",
      description: "想运行一条 shell 命令",
      access: ["启动一个系统命令进程", "命令内容见下方详情"],
      scope: "once",
      capabilities: ["启动进程：是", "副作用：destructive"],
      detail: "实际命令：echo hello",
    });
    await flushPromises();
    const text = w.find(".modal").text();
    expect(text).toContain("想运行一条 shell 命令");
    expect(text).toContain("启动一个系统命令进程");
    expect(text).toContain("仅这一次");
    // 机器可读的 action 不当作首屏「建议动作」
    expect(text).not.toContain("建议动作");
    const firstScreen = w.find(".body").text().replace(w.find("details.adv").text(), "");
    expect(firstScreen).not.toContain("run_shell");
    w.unmount();
  });
});

/**
 * 失效的确认（后端已经没有这条审批）必须有一个出口。
 *
 * 实测：此时点批准或拒绝都会 404，弹窗只能反复重试失败，用户被卡在一条
 * 永远处理不掉的待办上。现在的行为：明确说「已经有结局」，并且只留一个「知道了」。
 */
describe("ApprovalModal 失效确认的出口", () => {
  it("审批已有结局：不再给批准/拒绝，只给「知道了」，点掉即清出队列", async () => {
    const { w, s } = mountModal();
    s.enqueue("a1", "tool_create", { name: "t", explanation: "做一个工具" });
    await flushPromises();
    respond.mockRejectedValueOnce(
      Object.assign(new Error("/api/approvals/a1/respond -> 404: approval not found or already answered"), {
        status: 404,
      }),
    );

    await w.find(".reject").trigger("click");
    await flushPromises();

    expect(w.find(".approve").exists()).toBe(false);
    expect(w.find(".reject").exists()).toBe(false);
    const ack = w.find(".approval-stale-ack");
    expect(ack.exists()).toBe(true);
    expect(w.find(".modal").text()).toContain("已经有结局");

    await ack.trigger("click");
    await flushPromises();
    await new Promise((r) => setTimeout(r, 220));
    await nextTick();
    expect(w.find(".modal-mask").exists()).toBe(false);
    expect(s.pendingCount).toBe(0);
    w.unmount();
  });
});
