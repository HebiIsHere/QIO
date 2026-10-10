/**
 * V 组独立验证：R3（迟到的恢复补丁不得删除「请求期间被更新过」的同 ID 记录）。
 *
 * 装置：真实的 attachments 服务模块（只把 ./backend 与 fetch 换成受控桩），
 * 用**受控 Promise**把恢复请求卡在途中，期间本地推进修订号，再放行响应。
 * 不使用 r2-w2 / fb_* / acc_* 的用例。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../backend", () => ({
  resolveBackend: async () => ({ base: "http://unit.test", token: "test-token" }),
  authHeaders: () => ({}),
  resetBackend: () => undefined,
}));

import {
  COPY_LABEL,
  loadPendingAttachments,
  pendingRevision,
  restorePendingAttachments,
  savePendingAttachments,
  type AttachmentRef,
} from "../attachments";
import { mergeRestorePatch } from "../../composables/attachmentOps";

function ref(overrides: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_r3_1",
    name: "原始.bin",
    sizeBytes: 64,
    kind: "copy",
    display: COPY_LABEL,
    state: "ready",
    error: null,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function response(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const TOPIC = "topic-v-r3";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  localStorage.clear();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

describe("R3 迟到的恢复补丁（应用边界按当前本地修订号判定）", () => {
  it("请求期间同 ID 记录被更新 → missingIds 不应用，记录保留（不被删除）", async () => {
    savePendingAttachments(TOPIC, [ref()]);
    // 发起核对时捕获的修订号
    const capturedRevision = pendingRevision(TOPIC);
    expect(capturedRevision).toBeGreaterThan(0);

    const gate = deferred<Response>();
    let requestSent = false;
    fetchMock.mockImplementation(() => {
      requestSent = true;
      return gate.promise;
    });

    const inflight = restorePendingAttachments(TOPIC, {
      revision: capturedRevision,
      candidateIds: ["att_r3_1"],
    });
    // 受控：等请求真的发出去（不是 sleep）
    for (let i = 0; i < 50 && !requestSent; i += 1) await Promise.resolve();
    expect(requestSent).toBe(true);

    // 期间本地把同 ID 记录更新了（修订号前进）
    const updated = ref({ name: "期间更新过.bin", state: "changed", error: "内容有变化" });
    savePendingAttachments(TOPIC, [updated]);
    const movedRevision = pendingRevision(TOPIC);
    expect(movedRevision).toBeGreaterThan(capturedRevision);

    // 后端在核对那一刻说它永久无效（404）—— 这是**旧事实**
    gate.resolve(response(404, { detail: "没有这个附件" }));
    const patch = await inflight;

    expect(patch.revision, "回显仍是发起时捕获的修订号").toBe(capturedRevision);
    expect(patch.missingIds, "期间有新写入 → 不得应用剔除").toEqual([]);
    expect(patch.missing).toEqual([]);

    const merged = mergeRestorePatch(patch, loadPendingAttachments(TOPIC));
    expect(merged.list.map((item) => item.id)).toEqual(["att_r3_1"]);
    expect(merged.list[0]?.name).toBe("期间更新过.bin");
    expect(merged.skipped).toEqual([]);

    // 持久化与界面一致（调用方把合并结果写回）
    savePendingAttachments(TOPIC, merged.list);
    expect(loadPendingAttachments(TOPIC)).toEqual(merged.list);
  });

  it("对照：修订号一致（期间无人写过）→ 补丁照旧应用，该条被剔除", async () => {
    savePendingAttachments(TOPIC, [ref()]);
    const capturedRevision = pendingRevision(TOPIC);

    fetchMock.mockResolvedValue(response(404, { detail: "没有这个附件" }));
    const patch = await restorePendingAttachments(TOPIC, {
      revision: capturedRevision,
      candidateIds: ["att_r3_1"],
    });

    expect(patch.missingIds).toEqual(["att_r3_1"]);
    const merged = mergeRestorePatch(patch, loadPendingAttachments(TOPIC));
    expect(merged.list).toEqual([]);
    expect(merged.skipped).toEqual([{ id: "att_r3_1", reason: "missing" }]);
  });

  it("合并这一层自己也必须挡：修订号已前进的补丁即使带着 missingIds 也不得剔除", async () => {
    savePendingAttachments(TOPIC, [ref()]);
    const capturedRevision = pendingRevision(TOPIC);
    // 期间有人写过
    savePendingAttachments(TOPIC, [ref({ name: "更新的名字.bin" })]);

    // 手工构造一份「带旧 missingIds」的补丁（模拟生成侧没挡住的窗口）
    const stalePatch = {
      topicId: TOPIC,
      revision: capturedRevision,
      restored: [],
      missing: ["原始.bin"],
      missingIds: ["att_r3_1"],
    };
    const merged = mergeRestorePatch(stalePatch, loadPendingAttachments(TOPIC));
    expect(merged.list.map((item) => item.id)).toEqual(["att_r3_1"]);
    expect(merged.list[0]?.name).toBe("更新的名字.bin");
  });
});

describe("R3 / N3 兼容调用形状（不带 options）", () => {
  it("请求期间同一 ID 被更新过 → 保留当前记录，且不再报成永久无效（N3 闭合）", async () => {
    savePendingAttachments(TOPIC, [ref()]);
    const gate = deferred<Response>();
    let sent = false;
    fetchMock.mockImplementation(() => {
      sent = true;
      return gate.promise;
    });

    const inflight = restorePendingAttachments(TOPIC); // 不带 options（旧调用形状）
    for (let i = 0; i < 50 && !sent; i += 1) await Promise.resolve();
    expect(sent).toBe(true);

    // 期间本地把同 ID 记录更新了（修订号前进）
    savePendingAttachments(TOPIC, [
      ref({ name: "期间更新过.bin", state: "changed", error: "内容有变化" }),
    ]);

    gate.resolve(response(404, { detail: "没有这个附件" }));
    const outcome = await inflight;

    expect(outcome.dropped, "没有按旧事实删，就不能同时说它永久无效").toEqual([]);
    const list = loadPendingAttachments(TOPIC);
    expect(list.map((item) => item.id)).toEqual([ref().id]);
    expect(list[0]?.name).toBe("期间更新过.bin");
  });

  it("对照：期间无人写过 → 照旧清理并报出名字", async () => {
    savePendingAttachments(TOPIC, [ref()]);
    fetchMock.mockResolvedValue(response(404, { detail: "没有这个附件" }));
    const outcome = await restorePendingAttachments(TOPIC);
    expect(outcome.dropped).toEqual([ref().name]);
    expect(loadPendingAttachments(TOPIC)).toEqual([]);
  });
});
