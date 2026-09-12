import { describe, expect, it, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";
import MessageStream from "../MessageStream.vue";
import { useSessionStore } from "../../stores/session";

/** jsdom 不做布局：手动给滚动容器一个可测的几何 */
function shape(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
}

/** 等 watcher 内部 await nextTick + 贴底写入全部落定 */
async function settle() {
  await nextTick();
  await nextTick();
  await nextTick();
}

async function mountStream(): Promise<{
  w: ReturnType<typeof mount>;
  session: ReturnType<typeof useSessionStore>;
  stream: HTMLElement;
  pinia: Pinia;
}> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const w = mount(MessageStream, { global: { plugins: [pinia] } });
  await nextTick();
  const stream = w.find(".stream").element as HTMLElement;
  shape(stream, 1000, 400); // 底部 = scrollTop 600
  return { w, session, stream, pinia };
}

beforeEach(() => {
  localStorage.clear();
});

describe("MessageStream 流式跟随（问题12）", () => {
  it("接近底部时新消息继续跟随到底", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 550; // 距底部 50px < 120 → 仍在跟随
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("新消息");
    await settle();

    expect(stream.scrollTop).toBe(600);
    w.unmount();
  });

  it("用户主动向上滚动后立即停止跟随（不被强行拉回）", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120; // 距底部 480px
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("新消息");
    await settle();

    expect(stream.scrollTop).toBe(120);
    w.unmount();
  });

  it("用户重新滚到底部后恢复跟随", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();
    stream.scrollTop = 600;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("之后的消息");
    await settle();

    expect(stream.scrollTop).toBe(600);
    w.unmount();
  });

  it("助手消息内容持续增长（同一条流式消息）也跟随底部", async () => {
    const { w, session, stream } = await mountStream();
    session.pushAssistant("第一段", undefined, true, true);
    await settle();
    stream.scrollTop = 550;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    // 同一条消息增长：messages.length 不变，只有 content 变长
    session.pushAssistant("第一段 + 第二段", undefined, true, true);
    await settle();

    expect(stream.scrollTop).toBe(600);
    w.unmount();
  });

  it("向上滚动后内容增长也不被拉回", async () => {
    const { w, session, stream } = await mountStream();
    session.pushAssistant("第一段", undefined, true, true);
    await settle();
    stream.scrollTop = 100;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushAssistant("第一段 + 第二段", undefined, true, true);
    await settle();

    expect(stream.scrollTop).toBe(100);
    w.unmount();
  });

  it("轮次标签是中文（逻辑见 utils/turnLabel，这里防止组件回退成英文）", async () => {
    const { w } = await mountStream();
    // 虚拟列表在 jsdom 里不渲染条目，这里只断言组件没有把英文标签写进模板
    const src = w.html();
    expect(src).not.toContain("TURN ");
    expect(src).not.toContain(">NOW<");
    w.unmount();
  });
});
