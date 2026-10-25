/**
 * 统一审批 store 的行为测试。
 *
 * 覆盖验收点：类型分派（工具→审批接口 vs 知识→知识接口）、队列去重与切换、
 * 关闭不批准、暂缓不重复自动弹、提交防重、知识三动作（尤其「修改后保存」提交的是
 * 编辑后的内容）、失败/超时/失效分类、自动弹出规则、来源字段展示。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  respondApproval: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  request: mocks.request,
  api: { respondApproval: mocks.respondApproval },
}));

import {
  classifyKnowledgeSubmitError,
  classifyToolOrigin,
  knowledgeDetailFromView,
  toolSourceFacts,
  useUnifiedApprovalsStore,
  type KnowledgeApprovalItem,
} from "../unifiedApprovals";
import { useApprovalsStore } from "../approvals";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return {
    unified: useUnifiedApprovalsStore(),
    approvals: useApprovalsStore(),
  };
}

function knowledgeItem(id: string, extra: Partial<KnowledgeApprovalItem> = {}): KnowledgeApprovalItem {
  return {
    id,
    key: `knowledge:${id}`,
    version: 1,
    content: "用户偏好安静的界面",
    category: "user_profile",
    scope: ["全局（你）"],
    epistemicStatus: "user_explicit",
    source: "主对话",
    evidenceIds: ["ev_1"],
    updatedAt: "2026-10-22T00:00:00+00:00",
    reason: "你在本次对话里明确说过",
    arrivedAt: 1,
    detailStatus: "ready",
    detailError: "",
    ...extra,
  };
}

const listing = (items: unknown[] = []) => ({ items, total: items.length, truncated: false });

beforeEach(() => {
  mocks.request.mockReset();
  mocks.respondApproval.mockReset();
  try {
    localStorage.clear();
  } catch {
    // 存储不可用时只影响跨重启用例
  }
  mocks.request.mockImplementation(async (path: string) => {
    if (String(path).includes("/api/memory/pending")) return listing();
    return { ok: true, decision: "approve" };
  });
  mocks.respondApproval.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
});

describe("审批类型分派：按结构化身份，不按标题", () => {
  it("工具审批走 /api/approvals/{id}/respond（approval_id 身份）", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_tool_1", "tool_create", { name: "dev_write_file" }, { autoOpen: true });
    unified.open("tool:appr_tool_1");

    const ok = await unified.submit("approve");

    expect(ok).toBe(true);
    expect(mocks.respondApproval).toHaveBeenCalledTimes(1);
    expect(mocks.respondApproval.mock.calls[0][0]).toBe("appr_tool_1");
    expect(mocks.request).not.toHaveBeenCalled();
  });

  it("知识保存申请走 /api/knowledge/{id}/approval（knowledge id 身份），绝不碰审批响应接口", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    unified.open("knowledge:kn_1");

    const ok = await unified.submit("approve");

    expect(ok).toBe(true);
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    const [path, init] = mocks.request.mock.calls[0];
    expect(path).toBe("/api/knowledge/kn_1/approval");
    const body = JSON.parse(String(init.body));
    expect(body.decision).toBe("approve");
    // CAS：把用户看到的那一版一起带上，服务端据此拒绝「确认的不是这一版」
    expect(body.expected_version).toBe(1);
    expect(body.expected_content).toBe("用户偏好安静的界面");
    expect(body.expected_updated_at).toBe("2026-10-22T00:00:00+00:00");
  });

  it("修改后保存：提交的是用户编辑后的内容（不是候选原文）", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    unified.open("knowledge:kn_1");
    unified.startEdit();
    unified.updateDraft("用户偏好安静、留白充足的界面");

    const ok = await unified.submit("approve_modified");

    expect(ok).toBe(true);
    const body = JSON.parse(String(mocks.request.mock.calls[0][1].body));
    expect(body.decision).toBe("approve_modified");
    expect(body.content).toBe("用户偏好安静、留白充足的界面");
    expect(body.expected_content).toBe("用户偏好安静的界面");
    expect(unified.knowledge).toHaveLength(0);
  });

  it("不保存：走 ignore 动作，确认原文不生效", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    unified.open("knowledge:kn_1");

    await unified.submit("ignore");

    const body = JSON.parse(String(mocks.request.mock.calls[0][1].body));
    expect(body.decision).toBe("ignore");
    expect(body.content).toBeUndefined();
  });

  it("修改后内容为空时不会发起请求", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    unified.open("knowledge:kn_1");
    unified.startEdit();
    unified.updateDraft("   ");

    const ok = await unified.submit("approve_modified");

    expect(ok).toBe(false);
    expect(mocks.request).not.toHaveBeenCalled();
    expect(unified.submitError).toContain("不能为空");
  });
});

describe("队列：去重、一次一项、新申请不替换正在看的", () => {
  it("同一知识 id 重复入队只保留一项", async () => {
    const { unified } = setup();
    mocks.request.mockResolvedValue(listing([
      { kind: "knowledge_proposal", id: "p1", title: "x", updated_at: "t", evidence: [], knowledge: [{ id: "kn_1", version: 1, content: "内容", category: "user_profile", scope: ["全局（你）"], source: "主对话" }] },
    ]));
    unified.ingestKnowledgeCandidate({ knowledgeId: "kn_1", category: "user_profile", content: "内容" });
    unified.ingestKnowledgeCandidate({ knowledgeId: "kn_1", category: "user_profile", content: "内容" });
    await flushPromises();
    expect(unified.knowledge).toHaveLength(1);
    expect(unified.count).toBe(1);
  });

  it("新到的工具审批不会替换用户正在阅读的知识申请", async () => {
    const { unified, approvals } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    unified.open("knowledge:kn_1");
    approvals.enqueue("appr_new", "tool_execution", { description: "运行脚本" }, { autoOpen: true });

    expect(unified.activeKey).toBe("knowledge:kn_1");
    expect(unified.count).toBe(2);
    expect(unified.activeItem?.identity).toBe("kn_1");
  });

  it("可以主动切换到队列中的另一项，切换不改变申请状态", () => {
    const { unified, approvals } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" });
    unified.open("knowledge:kn_1");

    expect(unified.switchTo("tool:appr_1")).toBe(true);
    expect(unified.activeItem?.key).toBe("tool:appr_1");
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(unified.switchTo("tool:missing")).toBe(false);
  });

  it("关闭只隐藏、不批准；同一项稍后不再自动弹", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    expect(unified.maybeAutoOpen()).toBe(true);
    unified.close();

    expect(unified.visible).toBe(false);
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(approvals.queue).toHaveLength(1);
    expect(unified.deferredKeys).toContain("tool:appr_1");
    // 重渲染 / 轮询再次尝试：不重复自动弹
    expect(unified.maybeAutoOpen()).toBe(false);
    unified.noteQueueChanged();
    expect(unified.maybeAutoOpen()).toBe(false);
  });

  it("「稍后处理」当前项后展示下一条待办（不丢项）", () => {
    const { unified, approvals } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" });
    unified.open("knowledge:kn_1");

    unified.deferActive();

    expect(unified.count).toBe(2);
    expect(unified.visible).toBe(true);
    expect(unified.activeItem?.key).toBe("tool:appr_1");
    expect(unified.deferredKeys).toContain("knowledge:kn_1");
  });
});

describe("自动弹出规则", () => {
  it("工具/权限申请在无打扰时自动弹出", () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_1", "credential_grant", {}, { autoOpen: true });
    expect(unified.maybeAutoOpen()).toBe(true);
    expect(unified.visible).toBe(true);
    expect(unified.activeItem?.kind).toBe("tool");
  });

  it("主对话的知识保存从不自动弹，只更新数量", () => {
    const { unified } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    expect(unified.maybeAutoOpen()).toBe(false);
    expect(unified.visible).toBe(false);
    expect(unified.count).toBe(1);
  });

  it("用户正在输入时不抢焦点，条件恢复后再弹", () => {
    const { unified, approvals } = setup();
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    approvals.enqueue("appr_1", "tool_execution", {}, { autoOpen: true });

    expect(unified.maybeAutoOpen()).toBe(false);
    expect(unified.autoPending).toBe(true);

    input.blur();
    (document.activeElement as HTMLElement | null)?.blur();
    document.body.focus();
    expect(unified.maybeAutoOpen()).toBe(true);
  });

  it("已经有别的模态打开时不叠加，关掉后再处理", () => {
    const { unified, approvals } = setup();
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    document.body.appendChild(dialog);
    approvals.enqueue("appr_1", "tool_create", {}, { autoOpen: true });

    expect(unified.maybeAutoOpen()).toBe(false);
    dialog.remove();
    expect(unified.maybeAutoOpen()).toBe(true);
  });

  it("恢复路径（全局稍后）不抢焦点；内联审批显示按钮时不叠加", () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_1", "tool_execution", {}, { autoOpen: false });
    expect(unified.maybeAutoOpen()).toBe(false);

    approvals.openNow();
    approvals.claimInline("appr_1");
    expect(unified.maybeAutoOpen()).toBe(false);
    expect(unified.open("tool:appr_1")).toBe(false);
    expect(unified.lastNotice).toContain("对话过程中");
  });
});

describe("提交防重与失败/超时/已处理说明", () => {
  it("提交期间重复点击不会重复发请求", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    unified.open("knowledge:kn_1");
    let release!: (value: unknown) => void;
    mocks.request.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));

    const first = unified.submit("approve");
    const second = unified.submit("approve");
    expect(mocks.request).toHaveBeenCalledTimes(1);
    expect(await second).toBe(false);

    release({ ok: true, decision: "approve" });
    expect(await first).toBe(true);
  });

  it("版本/内容冲突（409）：保留申请、说明「没有保存」、重新拉取", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    unified.open("knowledge:kn_1");
    unified.startEdit();
    unified.updateDraft("用户编辑后的内容");
    // 冲突后重新拉取的权威列表里这条仍在（说明只是内容变了，没被处理）
    mocks.request.mockImplementation(async (path: string) =>
      String(path).includes("/api/memory/pending")
        ? listing([
            {
              kind: "knowledge_proposal",
              id: "p1",
              title: "x",
              updated_at: "t",
              evidence: [],
              knowledge: [
                {
                  id: "kn_1",
                  version: 2,
                  content: "别处修改后的内容",
                  category: "user_profile",
                  scope: ["全局（你）"],
                  source: "主对话",
                },
              ],
            },
          ])
        : { ok: true },
    );
    mocks.request.mockRejectedValueOnce(
      Object.assign(new Error("409"), {
        status: 409,
        body: { code: "content_changed", error: "内容已经变化" },
      }),
    );

    const ok = await unified.submit("approve_modified");

    expect(ok).toBe(false);
    expect(unified.submitError).toContain("已经变化");
    expect(unified.submitError).toContain("没有保存");
    await flushPromises();
    // 申请保留，并已换成服务端权威内容（用户需要重新确认）
    expect(unified.knowledge).toHaveLength(1);
    expect(unified.knowledge[0]?.content).toBe("别处修改后的内容");
    expect(unified.knowledge[0]?.version).toBe(2);
  });

  it("已在别处处理（404）：收口申请，明确说明没有保存", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    unified.open("knowledge:kn_1");
    mocks.request.mockRejectedValueOnce(Object.assign(new Error("404"), { status: 404 }));

    await unified.submit("approve");

    expect(unified.knowledge).toHaveLength(0);
    expect(unified.visible).toBe(false);
    expect(unified.submitError).toContain("不存在");
  });

  it("超时：不假装成功，说明「还不确定」，并核对服务端是否仍待确认", async () => {
    const { unified } = setup();
    unified.knowledge = [knowledgeItem("kn_1")];
    unified.open("knowledge:kn_1");
    mocks.request.mockImplementation(async (path: string) =>
      String(path).includes("/api/memory/pending")
        ? listing([
            {
              kind: "knowledge_proposal",
              id: "p1",
              title: "x",
              updated_at: "t",
              evidence: [],
              knowledge: [
                {
                  id: "kn_1",
                  version: 1,
                  content: "用户偏好安静的界面",
                  category: "user_profile",
                  scope: ["全局（你）"],
                  source: "主对话",
                },
              ],
            },
          ])
        : { ok: true },
    );
    mocks.request.mockRejectedValueOnce(
      Object.assign(new Error("timeout"), { name: "ApiTimeoutError" }),
    );

    await unified.submit("approve");
    await flushPromises();

    expect(unified.submitError).toContain("还不确定");
    // 服务端仍列着这条 → 保留申请（不能因为超时就当作已保存）
    expect(unified.knowledge).toHaveLength(1);
    expect(unified.lastNotice).not.toContain("已不在");
  });

  it("工具审批失败：保留待办并把失败原因显示出来（失败 ≠ 已授权）", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_1", "tool_execution", {}, { autoOpen: true });
    unified.open("tool:appr_1");
    mocks.respondApproval.mockRejectedValueOnce(new Error("POST /api/approvals -> 500: boom"));

    const ok = await unified.submit("approve");

    expect(ok).toBe(false);
    expect(unified.submitError).toContain("未做出任何授权");
    expect(unified.submitError).toContain("500");
    expect(approvals.queue).toHaveLength(1);
  });

  it("后端已无此审批（stale）：只给「知道了」，不再提供批准/拒绝", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_1", "tool_execution", {}, { autoOpen: true });
    unified.open("tool:appr_1");
    mocks.respondApproval.mockRejectedValueOnce(Object.assign(new Error("404"), { status: 404 }));

    await unified.submit("approve");

    expect(unified.activeItem?.stale).toBe(true);
    expect(await unified.submit("approve")).toBe(false);
    unified.dismissStale();
    expect(approvals.queue).toHaveLength(0);
  });
});

describe("来源字段展示：只显示后端真实给的结构化字段", () => {
  it("按 kind/来源字段区分主智能体、后台工作、子智能体", () => {
    expect(classifyToolOrigin("high_impact_knowledge", {})).toBe("background");
    expect(classifyToolOrigin("tool_candidate", {})).toBe("background");
    expect(classifyToolOrigin("tool_execution", { source: "background" })).toBe("background");
    expect(classifyToolOrigin("tool_execution", { subagent_id: "t1" })).toBe("subagent");
    expect(classifyToolOrigin("tool_execution", { executor: "subagent:reviewer" })).toBe("subagent");
    expect(classifyToolOrigin("tool_execution", {})).toBe("main_agent");
  });

  it("来源 / 任务 / 执行者：有就显示，没有就不编造", () => {
    const empty = toolSourceFacts({
      kind: "tool_execution",
      origin: "main_agent",
      approvalId: "appr_1",
      payload: {},
    });
    expect(empty).toEqual([{ label: "来源", value: "主智能体" }]);

    const full = toolSourceFacts({
      kind: "tool_execution",
      origin: "subagent",
      approvalId: "appr_2",
      payload: { executor: "reviewer", workspace: "dev_1", tool_name: "dev_run_tests" },
    });
    expect(full).toContainEqual({ label: "来源", value: "子智能体" });
    expect(full).toContainEqual({ label: "执行者", value: "reviewer" });
    expect(full).toContainEqual({ label: "来源任务", value: "dev_1" });
  });

  it("知识待确认视图映射：范围 / 确定程度 / 依据 / 来源原样保留", () => {
    const detail = knowledgeDetailFromView({
      id: "p1",
      kind: "knowledge_proposal",
      status: "awaiting_approval",
      handler: "knowledge_proposal",
      title: "标题",
      question: null,
      knowledge: [
        {
          id: "kn_1",
          version: 3,
          content: "内容",
          category: "goal",
          scope: ["话题：布局"],
          epistemic_status: "qio_inference",
          source: "主对话",
          state: "pending_review",
        },
      ],
      before: [],
      after: [],
      evidence: [{ id: "ev_1" }, { id: "ev_2" }],
      actions: ["approve"],
      created_at: "t1",
      updated_at: "t2",
    });
    expect(detail).not.toBeNull();
    expect(detail?.version).toBe(3);
    expect(detail?.scope).toEqual(["话题：布局"]);
    expect(detail?.epistemicStatus).toBe("qio_inference");
    expect(detail?.source).toBe("主对话");
    expect(detail?.evidenceIds).toEqual(["ev_1", "ev_2"]);
    expect(detail?.updatedAt).toBe("t2");
  });

  it("错误分类：401/网络/500 不会被说成「已保存」", () => {
    expect(classifyKnowledgeSubmitError({ status: 409, body: { code: "version_changed" } })).toMatchObject({
      refresh: true,
      terminal: false,
    });
    expect(classifyKnowledgeSubmitError({ status: 404 }).terminal).toBe(true);
    expect(classifyKnowledgeSubmitError({ name: "ApiTimeoutError" }).code).toBe("timeout");
    expect(classifyKnowledgeSubmitError(new TypeError("fetch failed")).message).toContain("没有连上");
  });
});
