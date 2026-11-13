/**
 * #12 文件读取失败的结构化事实链（前端侧）：
 *
 * 后端 TOOL_END 为真实读取失败补 file_read 事实（attachment_id / name / reason /
 * next_step / diagnostic）与稳定 code=file_read_failed；历史预览的 tool_records
 * 也带 code / category / recoverable / result_status。界面只消费这些程序字段，
 * 不从 toolError 的中文文本猜。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/toolFileReadFacts.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useEventStore } from "../events";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(async () => ({})),
    getSessionMessagesBefore: vi.fn(async () => ({})),
    getToolRecord: vi.fn(async () => ({})),
    sendTurn: vi.fn(async () => ({})),
  },
}));

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { events: useEventStore(), session: useSessionStore() };
}

function emit(
  events: ReturnType<typeof useEventStore>,
  type: string,
  data: Record<string, unknown>,
) {
  events.route({ type, id: "e_" + Math.random(), ts: "2026-11-12T00:00:00Z", data });
}

function record(over: Record<string, unknown> = {}) {
  return {
    id: "tr1",
    turn_id: "turn_t1",
    call_id: "c1",
    seq: 1,
    tool_name: "read_attachment",
    title: "读取附件",
    status: "failed",
    error: "可以让用户重新上传附件；可以先不传这个文件继续回答。",
    duration_ms: 9,
    truncated: false,
    output_missing: false,
    missing_reason: "",
    output_chars: 0,
    preview: "",
    created_at: "2026-11-12T10:00:03+00:00",
    ...over,
  };
}

function message(id: string, role: string, content: string, at: string) {
  return { id, role, content, content_type: "text", created_at: at, turn_id: "turn_t1" };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("TOOL_END 的 file_read 事实", () => {
  it("真实读取失败：file_read 与 code/category 落进同一张工具卡", () => {
    const { events, session } = setup();
    emit(events, "TOOL_START", { tool: "read_attachment", call_id: "c1" });
    emit(events, "TOOL_END", {
      tool: "read_attachment",
      call_id: "c1",
      ok: false,
      status: "failed",
      code: "file_read_failed",
      category: "io_error",
      category_label: "读写错误",
      recoverable: true,
      result_status: "failed",
      error: "可以让用户重新上传附件；可以先不传这个文件继续回答。",
      content_preview: "",
      file_read: {
        attachment_id: "att_1",
        name: "季度报告.docx",
        reason: "文件内容读不出来（压缩结构损坏）",
        next_step: "请重新上传这个文件，或先把内容贴成文本。",
        diagnostic: "zip: bad central directory",
      },
    });

    const card = session.messages[0];
    expect(card.toolFileRead).toEqual({
      attachmentId: "att_1",
      name: "季度报告.docx",
      reason: "文件内容读不出来（压缩结构损坏）",
      nextStep: "请重新上传这个文件，或先把内容贴成文本。",
      diagnostic: "zip: bad central directory",
    });
    expect(card.toolCode).toBe("file_read_failed");
    expect(card.toolCategory).toBe("io_error");
    expect(card.toolRecoverable).toBe(true);
    // 原始错误仍保留在数据里（诊断/排查用），界面负责不把它当默认文案。
    expect(card.toolError).toContain("可以让用户");
  });

  it("缺字段的旧事件：不伪造 file_read（旧数据仍可回退）", () => {
    const { events, session } = setup();
    emit(events, "TOOL_END", {
      tool: "read_attachment",
      call_id: "c2",
      ok: false,
      status: "failed",
      error: "读取失败",
    });
    const card = session.messages[0];
    expect(card.toolFileRead ?? null).toBeNull();
    expect(card.toolCode ?? null).toBeNull();
  });

  it("重复 TOOL_END：file_read 按最新事实覆盖，不残留旧文件", () => {
    const { events, session } = setup();
    emit(events, "TOOL_END", {
      tool: "read_attachment",
      call_id: "c3",
      ok: false,
      status: "failed",
      code: "file_read_failed",
      file_read: { attachment_id: "att_old", name: "旧文件.pdf", reason: "旧原因", next_step: "旧下一步" },
    });
    emit(events, "TOOL_END", {
      tool: "read_attachment",
      call_id: "c3",
      ok: false,
      status: "failed",
      code: "file_read_failed",
      file_read: { attachment_id: "att_new", name: "新文件.pdf", reason: "新原因", next_step: "新下一步" },
    });
    const card = session.messages[0];
    expect(card.toolFileRead?.attachmentId).toBe("att_new");
    expect(card.toolFileRead?.name).toBe("新文件.pdf");
    expect(card.toolFileRead?.reason).toBe("新原因");
  });
});

describe("历史 tool_records 的结构化字段（刷新后仍能识别文件读取失败）", () => {
  async function loadWith(over: Record<string, unknown> = {}) {
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [
        message("m1", "user", "看看这个文件", "2026-11-12T10:00:00+00:00"),
        message("m2", "assistant", "读完了", "2026-11-12T10:00:05+00:00"),
      ],
      tool_records: [record(over)],
      has_more: false,
      next_before: null,
    } as never);
    const session = setup().session;
    await session.loadHistory();
    return session;
  }

  it("code / category / recoverable / result_status 映射到历史工具卡", async () => {
    const session = await loadWith({
      code: "file_read_failed",
      category: "io_error",
      recoverable: true,
      result_status: "failed",
    });
    const card = session.messages.find((m) => m.role === "tool");
    expect(card).toBeTruthy();
    expect(card?.toolName).toBe("read_attachment");
    expect(card?.toolStatus).toBe("failed");
    expect(card?.toolCode).toBe("file_read_failed");
    expect(card?.toolCategory).toBe("io_error");
    expect(card?.toolRecoverable).toBe(true);
    expect(card?.toolResultStatus).toBe("failed");
  });

  it("历史没有 file_read 对象：保持 null，由 code/category 做可读回退", async () => {
    const session = await loadWith({
      code: "file_read_failed",
      category: "invalid_data",
      recoverable: false,
      result_status: "failed",
    });
    const card = session.messages.find((m) => m.role === "tool");
    expect(card?.toolFileRead ?? null).toBeNull();
    expect(card?.toolCode).toBe("file_read_failed");
    expect(card?.toolCategory).toBe("invalid_data");
    expect(card?.toolRecoverable).toBe(false);
  });

  it("旧历史记录（没有结构化字段）：不伪造", async () => {
    const session = await loadWith();
    const card = session.messages.find((m) => m.role === "tool");
    expect(card?.toolCode ?? null).toBeNull();
    expect(card?.toolCategory ?? null).toBeNull();
    expect(card?.toolRecoverable ?? null).toBeNull();
    expect(card?.toolResultStatus ?? null).toBeNull();
  });
});
