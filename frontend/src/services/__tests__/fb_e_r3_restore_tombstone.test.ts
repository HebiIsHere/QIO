/**
 * fb-E / R3（入口二）反例：恢复在途时移除附件，恢复结果的写回把已删 ID 复活。
 *
 * 用户可见规则（K1.4/K1.6）：restorePendingAttachments 的落盘必须尊重**在途发生的移除**
 * （removed/tombstone）；不得用「进入时的快照」整表回写，把用户刚删掉的附件写回来。
 *
 * 确定性时序：getAttachment(att_1) 用受控 deferred 卡住；期间模拟 removeOne 改写持久化；
 * 再放行响应。全程真实 service + 真实 localStorage，只控制网络响应时序。
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function payload(item: AttachmentRef): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => ({ attachment: { id: item.id, name: item.name, size_bytes: item.sizeBytes, kind: item.kind, state: item.state, topic_id: "A" } }),
    text: async () => "",
  } as unknown as Response;
}

beforeEach(() => {
  localStorage.clear();
});

describe("R3② 恢复在途时移除：写回不得复活已删附件", () => {
  it("恢复写回必须尊重在途移除（tombstone）", async () => {
    savePendingAttachments("A", [ref({ id: "att_1", name: "一号.txt" }), ref({ id: "att_2", name: "二号.txt" })]);

    const slow = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        String(url).includes("att_1") ? slow.promise : Promise.resolve(payload(ref({ id: "att_2", name: "二号.txt" }))),
      ),
    );

    const restoring = restorePendingAttachments("A");
    // 让恢复走到 att_1 的 await 上
    await Promise.resolve();
    await Promise.resolve();

    // 期间用户移除了 att_1（removeOne 会改写持久化）
    savePendingAttachments("A", [ref({ id: "att_2", name: "二号.txt" })]);

    slow.resolve(payload(ref({ id: "att_1", name: "一号.txt" })));
    const outcome = await restoring;

    expect(outcome.items.map((i) => i.id)).not.toContain("att_1");
    expect(loadPendingAttachments("A").map((i) => i.id), "恢复写回把已移除的附件复活了").toEqual(["att_2"]);
    vi.unstubAllGlobals();
  });
});
