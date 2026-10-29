/**
 * R5b 定向回归（先失败后通过）：直接进入 / 刷新互动页时先完成首次历史读取。
 *
 * 背景：普通对话页在 history=idle 时执行 loadHistory；互动页过去只调用
 * reloadHistoryIfStale，而 refreshIntentExecutionRecords 遇到 history=idle 会直接返回。
 * 于是「直接进入互动页 → 确认 → 执行」时执行记录不会自动出现，必须先切到普通对话页。
 *
 * 本文件只验证**互动页首次历史读取与后续刷新的衔接**：
 * - history=idle：挂载互动对话区必须发起一次既有 loadHistory（首次读取），
 *   而不是等待页面激活（reloadHistoryIfStale 在 idle 下是空操作）；
 * - history 已就绪：不重复整段读取，只走既有 stale 刷新；
 * - history 已就绪且有过期提示：继续走既有 reloadHistoryIfStale（后续刷新衔接不变）。
 *
 * 复用真实 session store；loadHistory / reloadHistoryIfStale 用 spy 观察调用次数，
 * 不伪造聊天记录、不预先手动加载历史（读取入口仍是 store 的既有 loadHistory）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h, nextTick } from "vue";
import InteractiveConversation from "../InteractiveConversation.vue";
import { useSessionStore } from "../../../../stores/session";

const StreamStub = defineComponent({
  name: "MessageStream",
  setup() {
    return () => h("div", { "data-stub": "stream" });
  },
});
const EntriesStub = defineComponent({
  name: "ComposerEntries",
  setup() {
    return () => h("div", { "data-stub": "entries" });
  },
});
const ComposerStub = defineComponent({
  name: "Composer",
  setup() {
    return () => h("div", { "data-stub": "composer" });
  },
});

let wrapper: VueWrapper | null = null;

function mountConversation() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = "A";
  wrapper = mount(InteractiveConversation, {
    props: {},
    global: {
      plugins: [pinia],
      stubs: { MessageStream: StreamStub, ComposerEntries: EntriesStub, Composer: ComposerStub },
    },
    attachTo: document.body,
  });
  return { session };
}

function idleSpies(session: ReturnType<typeof useSessionStore>) {
  session.history = { status: "idle", error: null };
  const load = vi.spyOn(session, "loadHistory").mockImplementation(async () => {
    session.history = { status: "ready", error: null };
  });
  const stale = vi.spyOn(session, "reloadHistoryIfStale").mockResolvedValue(false);
  return { load, stale };
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("互动页首次历史读取与后续刷新衔接", () => {
  it("直接进入互动页（history=idle）会发起一次既有首次历史读取，而不是空等页面激活", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.currentTopicId = "A";
    const { load, stale } = idleSpies(session);

    wrapper = mount(InteractiveConversation, {
      props: {},
      global: {
        plugins: [pinia],
        stubs: { MessageStream: StreamStub, ComposerEntries: EntriesStub, Composer: ComposerStub },
      },
      attachTo: document.body,
    });
    await nextTick();
    await Promise.resolve();

    expect(load).toHaveBeenCalledTimes(1);
    expect(stale).not.toHaveBeenCalled();
    // 首次读取完成后 history 进入既有状态机（ready），后续 refreshIntentExecutionRecords 才有前提
    expect(session.history.status).toBe("ready");
  });

  it("history 已就绪时进入互动页：不重复整段读取，继续走既有 stale 刷新", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.history = { status: "ready", error: null };
    const load = vi.spyOn(session, "loadHistory").mockResolvedValue(undefined);
    const stale = vi.spyOn(session, "reloadHistoryIfStale").mockResolvedValue(false);

    wrapper = mount(InteractiveConversation, {
      props: {},
      global: {
        plugins: [pinia],
        stubs: { MessageStream: StreamStub, ComposerEntries: EntriesStub, Composer: ComposerStub },
      },
      attachTo: document.body,
    });
    await nextTick();

    expect(load).not.toHaveBeenCalled();
    expect(stale).toHaveBeenCalledTimes(1);
  });

  it("history 已就绪且记录可能过期：互动页仍由既有 reloadHistoryIfStale 兑现刷新", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.history = { status: "ready", error: null };
    session.intentRecordsStale = true;
    const load = vi.spyOn(session, "loadHistory").mockResolvedValue(undefined);
    const stale = vi.spyOn(session, "reloadHistoryIfStale").mockResolvedValue(true);

    wrapper = mount(InteractiveConversation, {
      props: {},
      global: {
        plugins: [pinia],
        stubs: { MessageStream: StreamStub, ComposerEntries: EntriesStub, Composer: ComposerStub },
      },
      attachTo: document.body,
    });
    await nextTick();

    expect(load).not.toHaveBeenCalled();
    expect(stale).toHaveBeenCalledTimes(1);
  });
});
