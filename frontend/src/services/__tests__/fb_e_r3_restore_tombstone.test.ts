/**
 * fb-E / R3（入口二）反例（阶段二按 K1.6 生产契约对齐）：恢复不得复活已移除附件。
 *
 * 冻结契约（K1.4/K1.6，阶段二实现形态）：
 *  * 生产路径是**补丁模式**：restorePendingAttachments(topicId, { candidateIds, revision })
 *    → { topicId, revision, restored, missing, missingIds }，**服务不写任何持久化**；
 *    合并权在发起方（Composer），由它结合当前列表 + tombstone + sent 失效集决定；
 *  * 已移除（tombstone）的候选**连核对请求都不发**；
 *  * 旧调用形状（不带 options）保留兼容路径，不属于生产入口。
 *
 * 基线（9e53736）：服务按「进入时快照」整表回写持久化 → 在途移除的附件被写回来。
 * 阶段二（4454f10 合并后）应转绿：tombstone 被尊重、服务不再整表回写。
 *
 * 运行：cd frontend; npx vitest run src/services/__tests__/fb_e_r3_restore_tombstone.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../backend", () => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:1", token: "t0ken" })),
  authHeaders: vi.fn(() => ({ Authorization: "Bearer t0ken" })),
  resetBackend: vi.fn(),
}));

import {
  loadPendingAttachments,
  markAttachmentRemoved,
  restorePendingAttachments,
  savePendingAttachments,
  type AttachmentRef,
} from "../attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "文件.txt",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function payload(item: AttachmentRef): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => ({
      attachment: {
        id: item.id,
        name: item.name,
        size_bytes: item.sizeBytes,
        kind: item.kind,
        state: item.state,
        topic_id: "A",
      },
    }),
    text: async () => "",
  } as unknown as Response;
}

beforeEach(() => {
  localStorage.clear();
});

describe("R3② 移除后恢复：tombstone 必须被尊重，服务不得整表回写", () => {
  it("已移除的候选连核对请求都不发；补丁返回后持久化不得被复活", async () => {
    savePendingAttachments("A", [ref({ id: "att_1", name: "一号.txt" }), ref({ id: "att_2", name: "二号.txt" })]);
    // 用户移除 att_1：tombstone + 持久化移除（与 removeOne 同口径）
    markAttachmentRemoved("A", "att_1");
    savePendingAttachments("A", [ref({ id: "att_2", name: "二号.txt" })]);

    const fetchMock = vi.fn((_url: string) =>
      Promise.resolve(payload(ref({ id: "att_2", name: "二号.txt" }))),
    );
    vi.stubGlobal("fetch", fetchMock);

    const patch = await restorePendingAttachments("A", {
      candidateIds: ["att_1", "att_2"],
      revision: 7,
    });

    const ids = patch.restored.map((i) => i.id);
    expect(ids, "已移除（tombstone）的候选被恢复结果复活了").not.toContain("att_1");
    const askedUrls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(askedUrls.some((u) => u.includes("att_1")), "对已移除附件仍然发了核对请求").toBe(false);
    expect(patch.revision, "补丁必须原样回显发起时的修订号").toBe(7);
    expect(loadPendingAttachments("A").map((i) => i.id), "服务把已移除的附件写回了持久化").toEqual(["att_2"]);
    vi.unstubAllGlobals();
  });
});
