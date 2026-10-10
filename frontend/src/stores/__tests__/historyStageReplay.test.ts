/**
 * 历史重放（契约 §1.4）：阶段沿用 messages，raw.stage 记录顺序与状态。
 *
 * 历史加载时按 created_at 顺序重放 raw.stage → 阶段顺序、阶段内历次说明、
 * 关联工具记录全部可回看；旧数据（没有 raw.stage）走 legacy 平铺，不伪造阶段。
 */
import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(),
    getSessionMessagesBefore: vi.fn(),
    sendTurn: vi.fn(),
  },
}));

function historyRow(over: Record<string, unknown>) {
  return {
    id: "row",
    role: "assistant",
    content: "",
    content_type: "narrative",
    created_at: "2026-10-06T08:00:00+00:00",
    turn_id: "turn_old",
    ...over,
  };
}

function toolRecord(over: Record<string, unknown>) {
  return {
    id: "tr",
    turn_id: "turn_old",
    call_id: "c",
    seq: 1,
    tool_name: "read_file",
    title: "读取文件",
    status: "success",
    error: "",
    duration_ms: 120,
    truncated: false,
    output_missing: false,
    missing_reason: "",
    output_chars: 10,
    preview: "ok",
    created_at: "2026-10-06T08:00:01+00:00",
    ...over,
  };
}

async function loadWith(messages: unknown[], tool_records: unknown[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  vi.mocked(api.getSessionContext).mockResolvedValue({
    topic_id: "t1",
    topic_name: "话题",
    anchor_fragment: null,
    messages,
    tool_records,
    has_more: false,
    next_before: null,
  } as never);
  await session.loadHistory();
  return session;
}

describe("历史阶段重放", () => {
  it("raw.stage 重放出阶段顺序 / 说明 / 状态，工具按 call_id 归属到阶段", async () => {
    const session = await loadWith(
      [
        historyRow({
          id: "n1",
          content: "正在读取仓库结构",
          created_at: "2026-10-06T08:00:01+00:00",
          raw: JSON.stringify({
            narrative: { kind: "progress" },
            stage: { stage_id: "st_old_1", index: 1, name: "读取仓库结构", op: "start", status: "done" },
            calls: [{ call_id: "c1", tool: "read_file", status: "success" }],
          }),
        }),
        historyRow({
          id: "n2",
          content: "正在改写 narrative.py",
          created_at: "2026-10-06T08:00:02+00:00",
          raw: JSON.stringify({
            narrative: { kind: "progress" },
            stage: { stage_id: "st_old_2", index: 2, name: "改写文件", op: "next", status: "done" },
            calls: [{ call_id: "c2", tool: "fs_write", status: "failed" }],
          }),
        }),
      ],
      [
        toolRecord({ id: "tr1", call_id: "c1" }),
        toolRecord({ id: "tr2", call_id: "c2", tool_name: "fs_write", status: "failed", error: "未找到 old" }),
      ],
    );

    const stages = session.stagesFor("turn_old");
    expect(stages.map((s) => s.stageId)).toEqual(["st_old_1", "st_old_2"]);
    expect(stages[0]?.name).toBe("读取仓库结构");
    expect(stages[0]?.notes[0]?.text).toBe("正在读取仓库结构");
    expect(stages[0]?.status).toBe("done");
    expect(stages[1]?.notes[0]?.text).toBe("正在改写 narrative.py");

    const first = session.messages.find((m) => m.role === "tool" && m.callId === "c1");
    const second = session.messages.find((m) => m.role === "tool" && m.callId === "c2");
    expect(first?.stageId).toBe("st_old_1");
    expect(first?.turnId).toBe("turn_old");
    expect(second?.stageId).toBe("st_old_2");
  });

  it("旧数据没有 raw.stage：不伪造阶段（叙事行仍然是平铺记录）", async () => {
    const session = await loadWith(
      [
        historyRow({
          id: "n_old",
          content: "旧版过程说明",
          raw: JSON.stringify({ narrative: { kind: "progress" }, calls: [] }),
        }),
      ],
      [],
    );
    expect(session.stagesFor("turn_old")).toHaveLength(0);
    expect(session.messages.some((m) => m.role === "narrative" && m.content === "旧版过程说明")).toBe(true);
  });

  it("重复加载同一页历史：阶段说明不会重复累加", async () => {
    const rows = [
      historyRow({
        id: "n1",
        content: "正在读取仓库结构",
        raw: JSON.stringify({
          narrative: { kind: "progress" },
          stage: { stage_id: "st_old_1", index: 1, name: "读取仓库结构", op: "start", status: "done" },
        }),
      }),
    ];
    const session = await loadWith(rows, []);
    await session.loadHistory();
    expect(session.stagesFor("turn_old")[0]?.notes).toHaveLength(1);
  });
});
