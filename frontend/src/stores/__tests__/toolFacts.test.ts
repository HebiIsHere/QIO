/**
 * TOOL_END 结构化事实落进工具消息（用户要求 §四.7 / §四.6 的数据链）：
 *
 * 界面要区分「知识提交失败是依据没通过核对」和「随便一次失败」，只能消费后端
 * 给的稳定字段（code / category / category_label / recoverable / result_status），
 * 不能从 toolError 的中文文本猜。旧事件没有这些字段时必须保持原有行为。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/toolFacts.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({
      topic_id: "",
      topic_name: null,
      anchor_fragment: null,
      messages: [],
    })),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function emit(events: ReturnType<typeof useEventStore>, type: string, data: Record<string, unknown>) {
  events.route({ type, id: "e_" + Math.random(), ts: "2026-11-09T00:00:00Z", data });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("TOOL_END 结构化字段", () => {
  it("code / category / category_label / recoverable / result_status 落到同一张工具卡上", () => {
    const { events, session } = setup();
    emit(events, "TOOL_START", { tool: "propose_knowledge", call_id: "c1" });
    emit(events, "TOOL_END", {
      tool: "propose_knowledge",
      call_id: "c1",
      ok: false,
      error: "evidence_ids 必须来自本轮实际证据目录，未找到：ghost",
      status: "failed",
      code: "evidence_not_found",
      category: "unknown",
      category_label: "未知错误",
      recoverable: true,
      result_status: "failed",
    });

    const card = session.messages[0];
    expect(card.toolCode).toBe("evidence_not_found");
    expect(card.toolCategory).toBe("unknown");
    expect(card.toolCategoryLabel).toBe("未知错误");
    expect(card.toolRecoverable).toBe(true);
    expect(card.toolResultStatus).toBe("failed");
    expect(card.toolStatus).toBe("failed");
    // 原始错误仍然完整保留（展开详情用），没有被结构化字段替代
    expect(card.toolError).toContain("evidence_ids");
  });

  it("等待审批：真实 ToolResult.status 到达时不会被 ok=true 吞掉", () => {
    const { events, session } = setup();
    emit(events, "TOOL_END", {
      tool: "propose_knowledge",
      call_id: "c2",
      ok: true,
      status: "success",
      code: "awaiting_user_approval",
      result_status: "awaiting_approval",
      content_preview: "已提交审批：「…」",
    });
    const card = session.messages[0];
    expect(card.toolStatus).toBe("success");
    expect(card.toolResultStatus).toBe("awaiting_approval");
    expect(card.toolCode).toBe("awaiting_user_approval");
  });

  it("旧事件（没有结构化字段）：不写入伪造值，行为与以前一致", () => {
    const { events, session } = setup();
    emit(events, "TOOL_END", {
      tool: "fs_read",
      call_id: "c3",
      ok: false,
      error: "文件不存在",
      status: "failed",
    });
    const card = session.messages[0];
    expect(card.toolStatus).toBe("failed");
    expect(card.toolError).toBe("文件不存在");
    expect(card.toolCode ?? null).toBeNull();
    expect(card.toolCategoryLabel ?? null).toBeNull();
    expect(card.toolResultStatus ?? null).toBeNull();
  });

  it("TOOL_END 重复到达（重连重放）：结构化字段按最新事实覆盖，旧值不残留", () => {
    const { events, session } = setup();
    emit(events, "TOOL_END", {
      tool: "propose_knowledge",
      call_id: "c4",
      ok: false,
      status: "failed",
      code: "evidence_not_found",
      result_status: "failed",
    });
    emit(events, "TOOL_END", {
      tool: "propose_knowledge",
      call_id: "c4",
      ok: true,
      status: "success",
      code: "awaiting_user_approval",
      result_status: "awaiting_approval",
    });
    const card = session.messages[0];
    expect(card.toolCode).toBe("awaiting_user_approval");
    expect(card.toolResultStatus).toBe("awaiting_approval");
    expect(card.toolStatus).toBe("success");
  });
});
