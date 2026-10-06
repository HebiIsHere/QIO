/**
 * 问题 5：历史（刷新 / 重连后）的附件必须带回可用的引用形状 ——
 * 否则界面只有标签，「打开副本 / 重新定位」无从谈起。
 *
 * 修复前红：_historyMessage 完全忽略后端消息行里的 attachments。
 *
 * 运行：cd frontend; npx vitest run src/stores/__tests__/historyAttachments.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";

const { getSessionContext } = vi.hoisted(() => ({
  getSessionContext: vi.fn(async () => ({
  topic_id: "topic_1",
  topic_name: null,
  anchor_fragment: null,
  messages: [
    {
      id: "msg_1",
      role: "user",
      content: "看看这个文件",
      content_type: "text",
      created_at: "2026-10-06T08:00:00+00:00",
      turn_id: "turn_1",
      attachments: [
        { id: "att_1", name: "report.pdf", size_bytes: 2048, kind: "copy", state: "ready", error: null },
        { id: "att_2", name: "huge.iso", size_bytes: 200000000, kind: "reference", state: "missing", error: "文件不在了" },
        { id: "", name: "坏行", size_bytes: 1, kind: "copy", state: "ready" },
      ],
    },
  ],
})),
}));

vi.mock("../../services/api", () => ({
  api: { getSessionContext },
  ApiError: class ApiError extends Error {},
}));

beforeEach(() => {
  getSessionContext.mockClear();
});

describe("历史附件引用", () => {
  it("刷新后附件仍带 id / 名称 / 保存方式 / 状态（坏行跳过、不编造）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    await session.loadHistory();

    const user = session.messages.find((m) => m.id === "msg_1");
    expect(user).toBeTruthy();
    const refs = user?.attachments ?? [];
    expect(refs).toHaveLength(2);
    expect(refs[0]).toMatchObject({
      id: "att_1",
      name: "report.pdf",
      kind: "copy",
      display: "已保存副本",
      state: "ready",
    });
    expect(refs[1]).toMatchObject({
      id: "att_2",
      name: "huge.iso",
      kind: "reference",
      display: "引用本地文件",
      state: "missing",
      error: "文件不在了",
    });
  });
});
