/**
 * 第三阶段「状态表达」的核心契约（spec 第 20~65、95~103 条）：
 *
 * - `TOOL_START` 立刻出现「运行中」的工具卡，`TOOL_END` 原地更新同一张卡；
 * - 独立任务（subagent）是**独立**的一张卡，同一 task_id 只有一张；
 * - 工具创建是**一条流程一张卡**，同一 group_id 原地推进阶段；
 * - 高影响知识候选只在回答完成（TURN_END）之后才出现；
 * - 凭据 / 降级提示是人话、不带内部标识。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
    })),
    sendTurn: vi.fn(async () => ({})),
    verifyKnowledge: vi.fn(async () => ({ ok: true, knowledge: { id: "kn_1", state: "active" } })),
    reviseKnowledge: vi.fn(async () => ({ ok: true, knowledge_id: "kn_1" })),
    ignoreKnowledge: vi.fn(async () => ({ ok: true, knowledge_id: "kn_1" })),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function emit(events: ReturnType<typeof useEventStore>, type: string, data: Record<string, unknown>) {
  events.route({ type, id: `e_${Math.random()}`, ts: "2026-09-15T00:00:00Z", data });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("工具运行卡（TOOL_START → TOOL_END 原地更新）", () => {
  it("开始时立即出现运行中的卡，结束时更新同一张卡（不产生第二张）", () => {
    const { events, session } = setup();
    emit(events, "TOOL_START", {
      tool: "web_search",
      call_id: "call_1",
      presentation: { title: "网络搜索", tool: "web_search" },
    });
    expect(session.messages.length).toBe(1);
    const card = session.messages[0];
    expect(card.role).toBe("tool");
    expect(card.toolRunning).toBe(true);
    expect(card.presentation?.title).toBe("网络搜索");

    emit(events, "TOOL_END", {
      tool: "web_search",
      call_id: "call_1",
      ok: true,
      error: null,
      content_preview: "找到 3 条结果",
      duration_ms: 1200,
      presentation: { title: "网络搜索", tool: "web_search", summary: "找到 3 条结果" },
    });
    expect(session.messages.length).toBe(1); // 同一张卡
    const done = session.messages[0];
    expect(done.toolRunning).toBe(false);
    expect(done.toolOk).toBe(true);
    expect(done.toolDurationMs).toBe(1200);
    expect(done.presentation?.summary).toBe("找到 3 条结果");
  });

  it("没有收到 TOOL_START 时，TOOL_END 仍然补一张卡（重连 / 丢帧不丢结果）", () => {
    const { events, session } = setup();
    emit(events, "TOOL_END", {
      tool: "fs_read",
      call_id: "call_9",
      ok: false,
      error: "文件不在允许的目录里，需要你确认",
      content_preview: "",
    });
    expect(session.messages.length).toBe(1);
    expect(session.messages[0].toolOk).toBe(false);
    expect(session.messages[0].toolError).toContain("需要你确认");
  });

  it("工具失败：卡片上留下失败状态与可理解原因", () => {
    const { events, session } = setup();
    emit(events, "TOOL_START", { tool: "run_shell", call_id: "c2" });
    emit(events, "TOOL_END", {
      tool: "run_shell",
      call_id: "c2",
      ok: false,
      error: "用户已拒绝",
      content_preview: "",
    });
    const card = session.messages[0];
    expect(card.toolOk).toBe(false);
    expect(card.toolError).toBe("用户已拒绝");
    expect(card.toolRunning).toBe(false);
  });
});

describe("独立任务卡（SUBAGENT_STATUS）", () => {
  it("同一 task_id 只有一张卡，状态按 queued→running→done 推进", () => {
    const { events, session } = setup();
    emit(events, "SUBAGENT_STATUS", {
      task_id: "task_1",
      tool: "research",
      display_name: "调研任务",
      goal: "对比两套方案的成本",
      status: "queued",
    });
    emit(events, "SUBAGENT_STATUS", {
      task_id: "task_1",
      tool: "research",
      display_name: "调研任务",
      status: "running",
    });
    emit(events, "SUBAGENT_STATUS", {
      task_id: "task_1",
      tool: "research",
      display_name: "调研任务",
      status: "done",
      ok: true,
      content_preview: "结论：方案 B 更省",
    });
    const cards = session.messages.filter((m) => m.role === "subagent");
    expect(cards.length).toBe(1);
    expect(cards[0].taskStatus).toBe("done");
    expect(cards[0].taskGoal).toBe("对比两套方案的成本");
    expect(cards[0].content).toContain("方案 B");
    // 只显示目标 / 状态 / 结果，不出现内部推理字段
    expect(JSON.stringify(cards[0])).not.toMatch(/reasoning|system_prompt|chain_of_thought/i);
  });

  it("独立任务失败时给出可理解原因", () => {
    const { events, session } = setup();
    emit(events, "SUBAGENT_STATUS", {
      task_id: "task_2",
      status: "failed",
      ok: false,
      error: "子任务没有可用凭据",
    });
    const card = session.messages.find((m) => m.role === "subagent");
    expect(card?.taskStatus).toBe("failed");
    expect(card?.toolError).toContain("凭据");
  });
});

describe("工具创建单卡（TOOL_CREATE_STATUS）", () => {
  it("创建流程里的开发工具调用不再各出一张普通工具卡（折叠进创建卡）", () => {
    const { events, session } = setup();
    emit(events, "TOOL_CREATE_STATUS", {
      group_id: "dev_9",
      phase: "proposal",
      label: "已收到创建需求",
    });
    // dev_write_file 会同时发 TOOL_START / TOOL_END
    emit(events, "TOOL_START", {
      tool: "dev_write_file",
      call_id: "c_dev_1",
      arguments: { workspace: "dev_9", name: "impl.py" },
    });
    emit(events, "TOOL_END", {
      tool: "dev_write_file",
      call_id: "c_dev_1",
      ok: true,
      content_preview: "已写入 impl.py（120 字符）",
    });
    expect(session.messages.filter((m) => m.role === "tool").length).toBe(0);
    expect(session.messages.filter((m) => m.role === "tool_creation").length).toBe(1);
  });

  it("折叠掉的开发工具失败时，把原因写回创建卡（不静默）", () => {
    const { events, session } = setup();
    emit(events, "TOOL_CREATE_STATUS", { group_id: "dev_10", phase: "building" });
    emit(events, "TOOL_START", {
      tool: "dev_write_file",
      call_id: "c_dev_2",
      arguments: { workspace: "dev_10", name: "bad" },
    });
    emit(events, "TOOL_END", {
      tool: "dev_write_file",
      call_id: "c_dev_2",
      ok: false,
      error: "文件名不允许包含路径",
    });
    const card = session.messages.find((m) => m.role === "tool_creation");
    expect(card?.createPhase).toBe("failed");
    expect(card?.toolError).toContain("文件名不允许包含路径");
  });

  it("同一 group_id 一张卡，阶段从提案推进到已创建", () => {
    const { events, session } = setup();
    emit(events, "TOOL_CREATE_STATUS", {
      group_id: "dev_1",
      phase: "proposal",
      label: "已收到创建需求",
      detail: "正在准备开发工作区",
    });
    emit(events, "TOOL_CREATE_STATUS", {
      group_id: "dev_1",
      phase: "building",
      label: "正在构建",
      detail: "已写入 2 个文件",
    });
    emit(events, "TOOL_CREATE_STATUS", {
      group_id: "dev_1",
      phase: "waiting_approval",
      label: "等待你确认",
    });
    emit(events, "TOOL_CREATE_STATUS", {
      group_id: "dev_1",
      phase: "ready",
      label: "已创建",
      ok: true,
      tool_name: "excel2csv",
    });
    const cards = session.messages.filter((m) => m.role === "tool_creation");
    expect(cards.length).toBe(1);
    expect(cards[0].createPhase).toBe("ready");
    expect(cards[0].toolOk).toBe(true);
    expect(cards[0].createdToolName).toBe("excel2csv");
  });

  it("创建失败：卡片保留失败状态与原因", () => {
    const { events, session } = setup();
    emit(events, "TOOL_CREATE_STATUS", { group_id: "dev_2", phase: "testing" });
    emit(events, "TOOL_CREATE_STATUS", {
      group_id: "dev_2",
      phase: "failed",
      label: "创建失败",
      detail: "测试没有通过：先让工具把测试跑绿再提交",
      ok: false,
    });
    const card = session.messages[0];
    expect(card.createPhase).toBe("failed");
    expect(card.toolOk).toBe(false);
    expect(card.toolError).toContain("测试没有通过");
  });
});

describe("高影响知识候选（KNOWLEDGE_CANDIDATE）", () => {
  const candidate = {
    knowledge_id: "kn_1",
    category: "user_profile",
    content: "用户偏好简洁的信息展示",
    impact: "high",
    reason: "这句话透露了长期偏好",
  };

  it("回答完成之前不显示，TURN_END 之后才出现", () => {
    const { events, session } = setup();
    session.turnStarted();
    emit(events, "KNOWLEDGE_CANDIDATE", candidate);
    expect(session.knowledgeCandidates.length).toBe(0); // 绝不打断回答
    emit(events, "TURN_END", { turn_id: "t1", status: "completed", final_content: "好的" });
    expect(session.knowledgeCandidates.length).toBe(1);
    expect(session.knowledgeCandidates[0].knowledgeId).toBe("kn_1");
  });

  it("保存 / 忽略调用对应接口，并让候选消失", async () => {
    const { events, session } = setup();
    session.turnStarted();
    emit(events, "KNOWLEDGE_CANDIDATE", candidate);
    emit(events, "TURN_END", { turn_id: "t1", status: "completed", final_content: "" });

    await session.saveCandidate("kn_1");
    expect(api.verifyKnowledge).toHaveBeenCalledWith("kn_1");
    expect(session.knowledgeCandidates.length).toBe(0);

    emit(events, "KNOWLEDGE_CANDIDATE", {
      ...candidate,
      knowledge_id: "kn_2",
      content: "另一条候选",
    });
    emit(events, "TURN_END", { turn_id: "t2", status: "completed", final_content: "" });
    await session.ignoreCandidate("kn_2");
    expect(api.ignoreKnowledge).toHaveBeenCalledWith("kn_2");
    expect(session.knowledgeCandidates.length).toBe(0);
  });

  it("修改用很轻的内联编辑，不走 Knowledge Panel", async () => {
    const { events, session } = setup();
    session.turnStarted();
    emit(events, "KNOWLEDGE_CANDIDATE", candidate);
    emit(events, "TURN_END", { turn_id: "t1", status: "completed", final_content: "" });
    await session.editCandidate("kn_1", "用户偏好极简的信息展示");
    expect(api.reviseKnowledge).toHaveBeenCalledWith("kn_1", "用户偏好极简的信息展示");
  });

  it("保存失败：卡上保留失败原因，候选不消失（可重试）", async () => {
    const { events, session } = setup();
    (api.verifyKnowledge as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("offline"),
    );
    session.turnStarted();
    emit(events, "KNOWLEDGE_CANDIDATE", candidate);
    emit(events, "TURN_END", { turn_id: "t1", status: "completed", final_content: "" });
    await session.saveCandidate("kn_1");
    expect(session.candidateState["kn_1"]).toBe("failed");
    expect(session.candidateError["kn_1"]).toContain("可以重试");
    expect(session.knowledgeCandidates.length).toBe(1);
  });

  it("同一条候选不会重复入列", () => {
    const { events, session } = setup();
    session.turnStarted();
    emit(events, "KNOWLEDGE_CANDIDATE", candidate);
    emit(events, "KNOWLEDGE_CANDIDATE", candidate);
    emit(events, "TURN_END", { turn_id: "t1", status: "completed", final_content: "" });
    expect(session.knowledgeCandidates.length).toBe(1);
  });
});

describe("能力降级与凭据状态：人话、不重复、不含内部标识", () => {
  it("FALLBACK → 一次低干扰提示；重复事件不叠加", () => {
    const { events } = setup();
    emit(events, "FALLBACK", { from: "native", to: "text", message: "当前模型不支持原生工具调用，已使用兼容模式" });
    expect(events.fallbackNotice).toContain("兼容模式");
    emit(events, "FALLBACK", { from: "native", to: "text", message: "当前模型不支持原生工具调用，已使用兼容模式" });
    expect(events.fallbackNotice).toContain("兼容模式");
  });

  it("正常模式（CAPABILITY=native）不产生任何提示", () => {
    const { events } = setup();
    emit(events, "CAPABILITY", { adapter: "native", model: "m" });
    expect(events.fallbackNotice).toBeNull();
    expect(events.credentialNotice).toBeNull();
  });

  it("凭据不可用 → 人话提示且不含 key_ / keychain", () => {
    const { events } = setup();
    emit(events, "CREDENTIAL_STATUS", {
      status: "unavailable",
      scope: "main_loop",
      message: "当前没有可用的模型凭据：请在「设置 → 凭据」里添加一个 API Key",
      key_id: "key_secret_internal",
    });
    expect(events.credentialNotice).not.toContain("key_");
    expect(events.credentialNotice).toContain("凭据");
  });

  it("凭据恢复 → 清掉旧提示", () => {
    const { events } = setup();
    emit(events, "CREDENTIAL_STATUS", { status: "paused", key_id: "k1" });
    expect(events.credentialNotice).toContain("暂停");
    emit(events, "CREDENTIAL_STATUS", { status: "active", key_id: "k1" });
    expect(events.credentialNotice).toBeNull();
  });
});

describe("系统驱动的轮（TURN_START.notify）", () => {
  it("不会清掉排队消息的「等待中」标记", () => {
    const { events, session } = setup();
    session.turnRunning = true;
    session.messages.push({
      id: "m1",
      role: "user",
      content: "排队中的问题",
      contentType: "text",
      createdAt: "2026-09-15T00:00:00Z",
      queued: true,
    });
    session.queuedMessageIds = ["m1"];

    emit(events, "TURN_START", { turn_id: "turn_notify", notify: true });
    expect(session.messages[0].queued).toBe(true);
    expect(session.activity).toBe("notify");

    emit(events, "TURN_START", { turn_id: "turn_user", notify: false });
    expect(session.messages[0].queued).toBe(false);
  });
});
