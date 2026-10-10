/**
 * fb-B 验收（R3/R4 · 契约 K1）：**真实** Composer + 真实 store + 真实 localStorage
 * + **真实**恢复服务与守卫（不 mock attachments/attachmentOps），只控制网络时序（fetch 闸门）。
 *
 * 覆盖（与 Lead 的验收清单一一对应）：
 *   1) 移除成功后旧轮询的 onUpdate 与最终返回分别到达；
 *   2) restore 在途时移除，晚到后 UI 与持久化仍无该项；
 *   3) 发送已受理时旧 restore 返回；
 *   4) 暂时无法确认的核对结果晚于用户移除；
 *   5) A→B→A / 卸载重挂载 / 两次 restore 逆序；
 *   6) 删除失败与新操作交错、暂时失败与真正移除的区别；
 *   7) R4：文件对话框返回后切话题，结果仍属发起话题。
 *
 * 断言口径：最终 UI（chip 名单）、localStorage 待发列表、以及「服务器事实」被采纳的位置。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(async () => ({ ok: true, topic_id: null })),
    getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
    cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
    cancelContinuation: vi.fn(async () => ({ ok: true })),
  },
}));

import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";
import {
  addPendingAttachment,
  loadPendingAttachments,
  savePendingAttachments,
  type AttachmentRef,
} from "../../services/attachments";
import { resetAttachmentOpState } from "../../composables/attachmentOps";

// ---------------------------------------------------------------------------
// 受控网络：只按 (method, url) 路由；闸门控制响应时序（不 sleep 碰运气）
// ---------------------------------------------------------------------------

interface Handler {
  method: string;
  match: RegExp;
  respond: (url: string, init: RequestInit) => Response | Promise<Response>;
}

const handlers: Handler[] = [];
function route(method: string, match: RegExp, respond: Handler["respond"]) {
  handlers.push({ method, match, respond });
}

const fetchMock = vi.fn(async (input: unknown, init: RequestInit = {}) => {
  const url = String(input);
  const method = String(init.method ?? "GET").toUpperCase();
  const hit = handlers.find((h) => h.method === method && h.match.test(url));
  if (!hit) throw new TypeError("unexpected fetch " + method + " " + url);
  return hit.respond(url, init);
});

interface Gate {
  wait: Promise<void>;
  release: () => void;
}
function gate(): Gate {
  let release!: () => void;
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { wait, release };
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

function attachmentJson(over: Record<string, unknown> = {}) {
  return {
    id: "att_1",
    name: "报告.pdf",
    size_bytes: 2048,
    kind: "copy",
    state: "ready",
    error: null,
    ...over,
  };
}

function ref(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_1",
    name: "报告.pdf",
    sizeBytes: 2048,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

// ---------------------------------------------------------------------------
// 真实组件挂载 + 真实持久化
// ---------------------------------------------------------------------------

/** 用例失败时也要卸载：留在飞的组件不许在下一个用例里偷偷写持久化。 */
const mountedWrappers: VueWrapper[] = [];

async function mountComposer(topicId: string | null, options: { send?: boolean } = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = topicId;
  let sendMock: ReturnType<typeof vi.fn> | undefined;
  if (options.send !== undefined) {
    sendMock = vi.fn(async () => options.send);
    session.send = sendMock as unknown as typeof session.send;
  }
  const w = mount(Composer, { global: { plugins: [pinia] } });
  mountedWrappers.push(w);
  await flushPromises();
  return { w, session, sendMock };
}

/** 待发 chip 的名字列表（AttachmentChip 没有 data-id，用可见名字作身份）。 */
function chipNames(w: VueWrapper): string[] {
  return w.findAll(".composer .chip .name").map((n) => n.text());
}

function chipText(w: VueWrapper): string {
  return w.find(".composer .attach-row").exists() ? w.find(".composer .attach-row").text() : "";
}

async function removeChip(w: VueWrapper, name: string): Promise<void> {
  const chip = w.findAll(".composer .chip").find((c) => c.find(".name").text() === name);
  expect(chip, "待移除的 chip 必须在列表里：" + name).toBeTruthy();
  await chip!.find(".act.remove").trigger("click");
  await flushPromises();
}

/** 真实路径入口：粘贴路径后回车（走 prepareAttachment）。 */
async function pastePath(w: VueWrapper, path: string) {
  await w.findAll(".attach-btn")[1].trigger("click");
  await w.find(".path-input").setValue(path);
  await w.find(".path-input").trigger("keydown.enter");
  await flushPromises();
}

/** 浏览器回退入口：把文件塞进 input[type=file] 再触发 change。 */
async function chooseFiles(w: VueWrapper, ...names: string[]) {
  const input = w.find("input.file-input");
  Object.defineProperty(input.element, "files", {
    value: names.map((name) => new File(["x"], name)),
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
}

beforeEach(() => {
  handlers.length = 0;
  localStorage.clear();
  resetAttachmentOpState();
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  for (const w of mountedWrappers.splice(0)) {
    try {
      w.unmount();
    } catch {
      /* 已经卸过就算了 */
    }
  }
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("R3 附件异步结果的操作身份", () => {
  it("① 移除成功后：旧轮询的 onUpdate 与最终返回都不得复活该附件", async () => {
    const firstPoll = gate();
    let pollCount = 0;
    route("POST", /\/api\/attachments$/, () =>
      jsonResponse({ attachment: attachmentJson({ id: "att_poll", name: "轮询.bin", state: "prepared", topic_id: "A" }) }),
    );
    route("GET", /\/api\/attachments\/att_poll$/, async () => {
      pollCount += 1;
      if (pollCount === 1) {
        await firstPoll.wait;
        // 第一轮：仍在准备（onUpdate 路径）
        return jsonResponse({ attachment: attachmentJson({ id: "att_poll", name: "轮询.bin", state: "prepared", topic_id: "A" }) });
      }
      // 第二轮：最终态 ready（最终返回路径）
      return jsonResponse({ attachment: attachmentJson({ id: "att_poll", name: "轮询.bin", state: "ready", topic_id: "A" }) });
    });
    route("DELETE", /\/api\/attachments\/att_poll$/, () => jsonResponse({ ok: true }));

    const { w } = await mountComposer("A");
    await pastePath(w, "D:/tmp/轮询.bin");
    expect(chipText(w)).toContain("准备中");

    await removeChip(w, "轮询.bin");
    expect(chipNames(w)).toEqual([]);
    expect(loadPendingAttachments("A")).toEqual([]);

    firstPoll.release();
    await vi.waitFor(() => expect(pollCount).toBeGreaterThanOrEqual(2), { timeout: 5000 });
    await flushPromises();

    expect(chipNames(w)).toEqual([]);
    expect(loadPendingAttachments("A")).toEqual([]);
    w.unmount();
  });

  it("② restore 在途时移除：晚到的恢复不得把已移除条目写回 UI 或持久化", async () => {
    savePendingAttachments("A", [ref({ id: "att_x", name: "待发.txt", topicId: "A" })]);
    const restoreGate = gate();
    route("GET", /\/api\/attachments\/att_x$/, async () => {
      await restoreGate.wait;
      return jsonResponse({ attachment: attachmentJson({ id: "att_x", name: "待发.txt", state: "ready", topic_id: "A" }) });
    });
    route("DELETE", /\/api\/attachments\/att_x$/, () => jsonResponse({ ok: true }));

    const { w } = await mountComposer("A");
    // 发起前就能看到持久化基线（否则「恢复在途时移除」根本无从发生）
    expect(chipNames(w)).toEqual(["待发.txt"]);

    await removeChip(w, "待发.txt");
    expect(loadPendingAttachments("A")).toEqual([]);

    restoreGate.release();
    await flushPromises();

    expect(chipNames(w)).toEqual([]);
    expect(loadPendingAttachments("A")).toEqual([]);
    w.unmount();
  });

  it("③ 发送已受理时：晚到的 restore 不得把已发送附件放回待发列表", async () => {
    savePendingAttachments("A", [ref({ id: "att_sent", name: "已发送.txt", topicId: "A" })]);
    const restoreGate = gate();
    route("GET", /\/api\/attachments\/att_sent$/, async () => {
      await restoreGate.wait;
      return jsonResponse({ attachment: attachmentJson({ id: "att_sent", name: "已发送.txt", state: "ready", topic_id: "A" }) });
    });

    const { w, sendMock } = await mountComposer("A", { send: true });
    expect(chipNames(w)).toEqual(["已发送.txt"]);

    await w.find("textarea").setValue("带上它");
    await w.find(".send-btn").trigger("click");
    await flushPromises();
    expect(sendMock).toHaveBeenCalledTimes(1);
    expect(chipNames(w)).toEqual([]);

    restoreGate.release();
    await flushPromises();

    expect(chipNames(w)).toEqual([]);
    expect(loadPendingAttachments("A")).toEqual([]);
    w.unmount();
  });

  it("④ 暂时无法确认的核对结果晚于用户移除：不得复活", async () => {
    savePendingAttachments("A", [
      ref({ id: "att_u", name: "核对.txt", state: "ready", unconfirmed: true, topicId: "A" }),
    ]);
    const verifyGate = gate();
    let calls = 0;
    route("GET", /\/api\/attachments\/att_u$/, async () => {
      calls += 1;
      // 第 1 次（挂载恢复）暂时失败 → 条目保留「暂时无法确认」；第 2 次是用户点「重试」的核对
      if (calls === 1) return jsonResponse({ detail: "内部错误" }, 500);
      await verifyGate.wait;
      return jsonResponse({ attachment: attachmentJson({ id: "att_u", name: "核对.txt", state: "ready", topic_id: "A" }) });
    });
    route("DELETE", /\/api\/attachments\/att_u$/, () => jsonResponse({ ok: true }));

    const { w } = await mountComposer("A");
    const retry = w.find(".composer .chip .act.retry");
    expect(retry.exists(), "暂时无法确认的条目必须给「重试」").toBe(true);
    expect(chipText(w)).toContain("暂时无法确认");

    await retry.trigger("click");
    await vi.waitFor(() => expect(calls).toBe(2), { timeout: 3000 });

    await removeChip(w, "核对.txt");
    expect(chipNames(w)).toEqual([]);

    verifyGate.release();
    await flushPromises();

    expect(chipNames(w)).toEqual([]);
    expect(loadPendingAttachments("A")).toEqual([]);
    w.unmount();
  });

  it("⑤ 卸载/重挂载：tombstone 仍有效，迟到的恢复不得写回被移除的条目", async () => {
    savePendingAttachments("A", [ref({ id: "att_x", name: "待发.txt", topicId: "A" })]);
    const lateRestore = gate();
    route("GET", /\/api\/attachments\/att_x$/, async () => {
      await lateRestore.wait;
      return jsonResponse({ attachment: attachmentJson({ id: "att_x", name: "待发.txt", state: "ready", topic_id: "A" }) });
    });
    route("DELETE", /\/api\/attachments\/att_x$/, () => jsonResponse({ ok: true }));

    const first = await mountComposer("A");
    expect(chipNames(first.w)).toEqual(["待发.txt"]);
    await removeChip(first.w, "待发.txt");
    first.w.unmount();

    lateRestore.release(); // 卸载后迟到：只允许落持久化 —— 必须被 tombstone 挡住
    await flushPromises();
    expect(loadPendingAttachments("A")).toEqual([]);

    const second = await mountComposer("A");
    expect(chipNames(second.w)).toEqual([]);
    expect(loadPendingAttachments("A")).toEqual([]);
    second.w.unmount();
  });

  it("⑥ 删除失败与新操作交错：只恢复这一条，且它的在飞轮询仍能更新（身份保留）", async () => {
    let pollCount = 0;
    const polls = [gate(), gate()];
    route("POST", /\/api\/attachments$/, () =>
      jsonResponse({ attachment: attachmentJson({ id: "att_f", name: "失败删除.bin", state: "prepared", topic_id: "A" }) }),
    );
    route("GET", /\/api\/attachments\/att_f$/, async () => {
      const index = pollCount;
      pollCount += 1;
      await polls[index].wait;
      return jsonResponse({
        attachment: attachmentJson({
          id: "att_f",
          name: "失败删除.bin",
          state: index === 0 ? "prepared" : "ready",
          topic_id: "A",
        }),
      });
    });
    const deleteGate = gate();
    route("DELETE", /\/api\/attachments\/att_f$/, async () => {
      await deleteGate.wait;
      return jsonResponse({ detail: "后端没响应" }, 500);
    });

    const { w } = await mountComposer("A");
    await pastePath(w, "D:/tmp/失败删除.bin");
    expect(chipText(w)).toContain("准备中");

    await removeChip(w, "失败删除.bin"); // DELETE 挂住：移除是立即反馈
    expect(chipNames(w)).toEqual([]);

    await vi.waitFor(() => expect(pollCount).toBe(1), { timeout: 3000 });
    polls[0].release(); // 第一轮结果：移除窗口内到达 → 不得复活
    await flushPromises();
    expect(chipNames(w)).toEqual([]);

    deleteGate.release(); // 删除失败 → 只恢复这一条（身份保留）
    await flushPromises();
    expect(chipNames(w)).toEqual(["失败删除.bin"]);
    expect(w.find('[data-test="attach-error"]').text()).toContain("移除附件失败");
    expect(loadPendingAttachments("A").map((i) => i.id)).toEqual(["att_f"]);

    await vi.waitFor(() => expect(pollCount).toBe(2), { timeout: 4000 });
    polls[1].release(); // 第二轮最终态：同一操作身份，仍能写回
    await vi.waitFor(() => expect(chipText(w)).toContain("已保存副本"), { timeout: 4000 });
    expect(loadPendingAttachments("A")[0].state).toBe("ready");
    w.unmount();
  });

  it("⑦ 暂时失败保留身份与原持久化；只有确认永久无效才清理", async () => {
    savePendingAttachments("A", [
      ref({ id: "att_keep", name: "保留.txt", topicId: "A" }),
      ref({ id: "att_gone", name: "没了.txt", topicId: "A" }),
    ]);
    route("GET", /\/api\/attachments\/att_keep$/, () => jsonResponse({ detail: "内部错误" }, 500));
    route("GET", /\/api\/attachments\/att_gone$/, () => jsonResponse({ detail: "没有这个附件" }, 404));

    const { w } = await mountComposer("A");
    await flushPromises();

    expect(chipNames(w)).toEqual(["保留.txt"]);
    expect(chipText(w)).toContain("暂时无法确认");
    expect(loadPendingAttachments("A").map((i) => i.id)).toEqual(["att_keep"]);
    expect(w.find('[data-test="attach-error"]').text()).toContain("没了.txt");
    w.unmount();
  });

  it("⑧ A→B→A：两次 restore 逆序返回，旧的一次不得覆盖新的一次", async () => {
    savePendingAttachments("A", [
      ref({ id: "att_a1", name: "A的.txt", topicId: "A" }),
      ref({ id: "att_a2", name: "A的第二条.txt", topicId: "A" }),
    ]);
    const staleGate = gate();
    const calls: Record<string, number> = {};
    route("GET", /\/api\/attachments\/att_a1$/, () =>
      jsonResponse({ attachment: attachmentJson({ id: "att_a1", name: "A的.txt", state: "ready", topic_id: "A" }) }),
    );
    route("GET", /\/api\/attachments\/att_a2$/, async () => {
      calls.a2 = (calls.a2 ?? 0) + 1;
      // 第一次（旧恢复）认为它已经没了；第二次（新恢复）说它还在
      if (calls.a2 === 1) {
        await staleGate.wait;
        return jsonResponse({ detail: "没有这个附件" }, 404);
      }
      return jsonResponse({ attachment: attachmentJson({ id: "att_a2", name: "A的第二条.txt", state: "ready", topic_id: "A" }) });
    });

    const { w, session } = await mountComposer("A");
    expect(chipNames(w)).toEqual(["A的.txt", "A的第二条.txt"]);

    session.currentTopicId = "B";
    await flushPromises();
    session.currentTopicId = "A";
    await flushPromises();
    expect(chipNames(w)).toEqual(["A的.txt", "A的第二条.txt"]);

    staleGate.release(); // 第一次 A 恢复（旧快照）迟到：必须被更新的恢复取代
    await flushPromises();

    expect(chipNames(w)).toEqual(["A的.txt", "A的第二条.txt"]);
    expect(loadPendingAttachments("A").map((i) => i.id)).toEqual(["att_a1", "att_a2"]);
    w.unmount();
  });

  it("⑨ 订阅（历史重传）结果晚于发送受理：不得把已发送的 id 放回待发列表", async () => {
    savePendingAttachments("A", [ref({ id: "att_s", name: "已发送.txt", topicId: "A" })]);
    route("GET", /\/api\/attachments\/att_s$/, () =>
      jsonResponse({ attachment: attachmentJson({ id: "att_s", name: "已发送.txt", state: "ready", topic_id: "A" }) }),
    );

    const { w, sendMock } = await mountComposer("A", { send: true });
    await flushPromises();
    expect(chipNames(w)).toEqual(["已发送.txt"]);

    await w.find("textarea").setValue("发出它");
    await w.find(".send-btn").trigger("click");
    await flushPromises();
    expect(sendMock).toHaveBeenCalledTimes(1);
    expect(chipNames(w)).toEqual([]);

    // 迟到的「加入待发送附件」广播（历史重传结果）
    const accepted = addPendingAttachment("A", ref({ id: "att_s", name: "已发送.txt", topicId: "A" }));
    await flushPromises();

    expect(accepted).toBe(false);
    expect(chipNames(w)).toEqual([]);
    expect(loadPendingAttachments("A")).toEqual([]);
    w.unmount();
  });
});

describe("R4 发起话题归属（点击入口捕获）", () => {
  it("文件对话框返回后切了话题：结果仍落发起话题 A，B 不被污染", async () => {
    const uploadGate = gate();
    const uploadTopics: string[] = [];
    route("POST", /\/api\/attachments\/upload$/, async (_url, init) => {
      const headers = (init.headers ?? {}) as Record<string, string>;
      uploadTopics.push(String(headers["X-QIO-Topic-Id"] ?? ""));
      await uploadGate.wait;
      return jsonResponse({ attachment: attachmentJson({ id: "att_picked", name: "选好的.txt", state: "ready", topic_id: "A" }) });
    });

    const { w, session } = await mountComposer("A");
    await w.find(".attach-btn").trigger("click"); // 发起附件选择：此刻话题 = A
    await flushPromises();

    session.currentTopicId = "B"; // 对话框还开着，用户切到别的话题
    await flushPromises();

    await chooseFiles(w, "选好的.txt"); // 对话框返回
    uploadGate.release();
    await flushPromises();

    expect(uploadTopics).toEqual(["A"]);
    expect(loadPendingAttachments("A").map((i) => i.id)).toEqual(["att_picked"]);
    expect(loadPendingAttachments("B")).toEqual([]);
    expect(chipNames(w)).toEqual([]); // 当前话题 B 的 UI 不被污染
    expect(session.currentTopicId).toBe("B");
    w.unmount();
  });

  it("订阅结果按发起话题落地：切到 B 之后仍进 A 的持久化，切回 A 才显示", async () => {
    route("GET", /\/api\/attachments\/att_hist$/, () =>
      jsonResponse({ attachment: attachmentJson({ id: "att_hist", name: "历史新附件.txt", state: "ready", topic_id: "A" }) }),
    );

    const { w, session } = await mountComposer("A");
    session.currentTopicId = "B";
    await flushPromises();

    addPendingAttachment("A", ref({ id: "att_hist", name: "历史新附件.txt", topicId: "A" }));
    await flushPromises();

    expect(chipNames(w)).toEqual([]);
    expect(loadPendingAttachments("A").map((i) => i.id)).toEqual(["att_hist"]);

    session.currentTopicId = "A";
    await flushPromises();
    expect(chipNames(w)).toEqual(["历史新附件.txt"]);
    w.unmount();
  });
});
