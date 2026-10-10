/**
 * R3（W2）迟到的恢复补丁不得删除较新的同 ID 附件（round2）。
 *
 * 反例（基线 6ca65f9 红）：恢复补丁只把 `revision` 原样回显发起时的值，
 * Composer 的 `dropMissing: patch.revision === revision` 于是**恒为真** ——
 * 恢复在途期间本地已经写过（同一个 ID 条目被更新）时，旧补丁里的 missingIds
 * 仍会把当前记录删掉：用户看到刚改好的那条莫名其妙没了。
 *
 * 冻结规则：应用补丁前，比较「**当前本地修订号**」与「请求发起时捕获的修订号」；
 * 两者不同（期间有新写入）→ 不应用旧 missingIds 删除，保留当前记录。
 *
 * 时序全部用受控 Promise 闸门，不 sleep。
 * 运行：cd frontend; npx vitest run src/services/__tests__/r2-w2-r3-restore-late-patch.test.ts
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const backendMock = vi.hoisted(() => ({
  resolveBackend: vi.fn(async () => ({ base: "http://127.0.0.1:1", token: "t0ken" })),
  authHeaders: vi.fn((token: string) => ({ Authorization: "Bearer " + token })),
  resetBackend: vi.fn(),
}));
vi.mock("../backend", () => backendMock);

import { mergeRestorePatch } from "../../composables/attachmentOps";
import {
  loadPendingAttachments,
  pendingRevision,
  restorePendingAttachments,
  savePendingAttachments,
  type AttachmentRef,
} from "../attachments";

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "一号.txt",
    sizeBytes: 10,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "ERR",
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

/** 受控闸门：核对请求挂在 gate 上，由测试决定什么时候放行（不 sleep）。 */
function gate<T>() {
  let release!: (value: T) => void;
  const promise = new Promise<T>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

beforeEach(() => {
  localStorage.clear();
  backendMock.resetBackend.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("R3 迟到的恢复补丁 vs 期间的新写入", () => {
  it("恢复在途期间本地更新了同 ID 条目 → 旧补丁不得把它当 missing 删掉（界面与持久化都不许）", async () => {
    savePendingAttachments("A", [
      ref({ id: "att_1", name: "一号.txt", state: "ready" }),
      ref({ id: "att_2", name: "二号.txt", state: "ready" }),
    ]);
    const captured = pendingRevision("A");

    const hung = gate<Response>();
    const fetchMock = vi.fn((url: string) => {
      if (String(url).includes("att_1")) return hung.promise; // 第一条核对卡住
      return Promise.resolve(
        jsonResponse({
          attachment: { id: "att_2", name: "二号.txt", size_bytes: 10, kind: "copy", state: "ready", topic_id: "A" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const inflight = restorePendingAttachments("A", {
      candidateIds: ["att_1", "att_2"],
      revision: captured,
    });

    // 恢复在途：本地又写了一次（同一个 ID 条目被更新 → 修订号前进）
    savePendingAttachments("A", [
      ref({ id: "att_1", name: "一号.txt", state: "prepared" }),
      ref({ id: "att_2", name: "二号.txt", state: "ready" }),
    ]);
    expect(pendingRevision("A"), "本地写入必须真的推进了修订号").toBeGreaterThan(captured);

    // 后端此刻才回话：att_1 按它发起核对时的旧事实是「永久无效」（404）
    hung.release(jsonResponse({ detail: "没有这个附件" }, 404));
    const patch = await inflight;

    expect(patch.missingIds, "迟到的恢复补丁把期间更新过的条目当成了 missing").not.toContain("att_1");
    expect(patch.missing, "旧补丁仍在界面报「它不在了」").not.toContain("一号.txt");

    // 界面与持久化一致：按 Composer 的口径合并 + 落盘
    const merged = mergeRestorePatch(patch, loadPendingAttachments("A"), { dropMissing: true });
    savePendingAttachments("A", merged.list);
    expect(merged.list.map((i) => i.id), "界面把更新过的条目弄丢了").toEqual(["att_1", "att_2"]);
    expect(loadPendingAttachments("A").map((i) => i.id), "持久化把更新过的条目弄丢了").toEqual(["att_1", "att_2"]);
    expect(loadPendingAttachments("A").find((i) => i.id === "att_1")?.state).toBe("prepared");
  });

  it("对照：恢复期间没人写过（修订号一致）→ 确认永久无效的条目照旧剔除", async () => {
    savePendingAttachments("A", [ref({ id: "att_1", name: "一号.txt" })]);
    const captured = pendingRevision("A");
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ detail: "没有这个附件" }, 404)));
    vi.stubGlobal("fetch", fetchMock);

    const patch = await restorePendingAttachments("A", { candidateIds: ["att_1"], revision: captured });

    expect(patch.missingIds).toEqual(["att_1"]);
    expect(patch.missing).toEqual(["一号.txt"]);
    const merged = mergeRestorePatch(patch, loadPendingAttachments("A"), { dropMissing: true });
    expect(merged.list).toEqual([]);
  });

  it("合并边界：补丁生成之后、合并之前又有本地写入 → 同样不剔除旧 missingIds", () => {
    savePendingAttachments("A", [ref({ id: "att_1", name: "一号.txt" })]);
    const captured = pendingRevision("A");
    const patch = {
      topicId: "A",
      revision: captured,
      restored: [],
      missing: ["一号.txt"],
      missingIds: ["att_1"],
    };
    // 补丁已经生成，但用户在合并之前又改了一次这条（修订号前进）
    savePendingAttachments("A", [ref({ id: "att_1", name: "一号.txt", state: "prepared" })]);
    expect(pendingRevision("A")).toBeGreaterThan(captured);

    const merged = mergeRestorePatch(patch, loadPendingAttachments("A"), { dropMissing: true });
    expect(merged.list.map((i) => i.id), "合并边界上没有比较当前本地修订号").toEqual(["att_1"]);
  });

  it("对照：补丁修订号与当前本地修订号一致时，合并照旧剔除 missing", () => {
    savePendingAttachments("A", [ref({ id: "att_1", name: "一号.txt" })]);
    const captured = pendingRevision("A");
    const merged = mergeRestorePatch(
      { topicId: "A", revision: captured, restored: [], missing: ["一号.txt"], missingIds: ["att_1"] },
      loadPendingAttachments("A"),
      { dropMissing: true },
    );
    expect(merged.list).toEqual([]);
    expect(merged.skipped).toEqual([{ id: "att_1", reason: "missing" }]);
  });

  it("兼容调用形状（不带 options）：期间更新过的同 ID 记录也不得被旧 droppedIds 删掉", async () => {
    savePendingAttachments("A", [
      ref({ id: "att_1", name: "一号.txt", state: "ready" }),
      ref({ id: "att_2", name: "二号.txt", state: "ready" }),
    ]);

    const hung = gate<Response>();
    const fetchMock = vi.fn((url: string) => {
      if (String(url).includes("att_1")) return hung.promise;
      return Promise.resolve(
        jsonResponse({
          attachment: { id: "att_2", name: "二号.txt", size_bytes: 10, kind: "copy", state: "ready", topic_id: "A" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    // 旧调用形状：不带 options —— 服务**自己**合并并落盘（N3 的兼容边界）
    const inflight = restorePendingAttachments("A");

    // 核对在途：本地更新了同一条（修订号前进）
    savePendingAttachments("A", [
      ref({ id: "att_1", name: "一号.txt", state: "prepared" }),
      ref({ id: "att_2", name: "二号.txt", state: "ready" }),
    ]);

    // 后端按它发起核对时的旧事实说 att_1 永久无效（404）
    hung.release(jsonResponse({ detail: "没有这个附件" }, 404));
    const out = await inflight;

    expect(out.dropped, "兼容路径仍把期间更新过的条目报成「永久无效」").not.toContain("一号.txt");
    expect(loadPendingAttachments("A").map((i) => i.id), "兼容路径按进入时的旧快照删掉了更新的记录").toEqual([
      "att_1",
      "att_2",
    ]);
    expect(loadPendingAttachments("A").find((i) => i.id === "att_1")?.state).toBe("prepared");
  });

  it("对照：兼容调用形状在没人写过（修订号一致）时照旧剔除确认永久无效的条目", async () => {
    savePendingAttachments("A", [
      ref({ id: "att_1", name: "一号.txt" }),
      ref({ id: "att_2", name: "二号.txt" }),
    ]);
    const fetchMock = vi.fn((url: string) =>
      String(url).includes("att_1")
        ? Promise.resolve(jsonResponse({ detail: "没有这个附件" }, 404))
        : Promise.resolve(
            jsonResponse({
              attachment: { id: "att_2", name: "二号.txt", size_bytes: 10, kind: "copy", state: "ready", topic_id: "A" },
            }),
          ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const out = await restorePendingAttachments("A");

    expect(out.dropped).toEqual(["一号.txt"]);
    expect(out.items.map((i) => i.id)).toEqual(["att_2"]);
    expect(loadPendingAttachments("A").map((i) => i.id)).toEqual(["att_2"]);
  });
});
