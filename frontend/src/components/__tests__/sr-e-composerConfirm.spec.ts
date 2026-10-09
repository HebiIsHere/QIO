/**
 * 契约 5（Agent E2）· 组件层接线：Composer 的发送确认条。
 *
 * 要点（Lead 裁决 + 契约 5 第 3/6 条）：
 * * needs-confirm 时 Composer **不**自动恢复草稿（由确认条的【重试 / 放弃】接管）；
 * * 「正在确认是否已发送」与「发送未确认：后端没有该请求记录」两态文案必须真的渲染；
 * * 「明确失败：被拒绝」沿用既有提示通道（lastError），不与确认条混用。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";
import { API_TIMEOUT_MS } from "../../services/api";
import {
  flushAsyncTimers,
  installFakeFetch,
  okResponse,
  statusResponse,
  hang,
  type FakeFetchGate,
} from "../../__tests__/sr-e-helpers";

let gate: FakeFetchGate;

const beforeEachImpl = () => {
  gate = installFakeFetch();
  const pinia = createPinia();
  setActivePinia(pinia);
};

afterEach(() => {
  vi.useRealTimers();
});

async function mountComposer() {
  const w = mount(Composer);
  await nextTick();
  return { w, session: useSessionStore() };
}

describe("契约 5 · Composer 发送确认条", () => {
  it("响应丢失进入 needs-confirm：不自动恢复草稿，确认条显示「正在确认」，消息保留", async () => {
    beforeEachImpl();
    vi.useFakeTimers();
    // 查证响应由测试控制：先挂住，稍后以「查询失败」放行
    const lookupRejecters: ((e: Error) => void)[] = [];
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) return hang(init.signal);
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return new Promise((_res, rej) => { lookupRejecters.push(rej); });
      }
      return undefined;
    });
    const { w, session } = await mountComposer();
    const ta = w.find("textarea");
    await ta.setValue("正在确认的消息");
    await w.find(".send-btn").trigger("click");

    await flushAsyncTimers(API_TIMEOUT_MS.write + 1000);
    await nextTick();

    // ← 要求 1：不自动恢复草稿（重试/放弃入口接管；再按 Enter 不会换 id 重发）
    const taValue = (ta.element as HTMLTextAreaElement).value;
    expect(taValue).toBe("");
    // 确认条真的渲染出来，且是「正在确认」这一态
    const bar = w.find(".send-confirm");
    expect(bar.exists()).toBe(true);
    expect(bar.text()).toContain("正在确认这条消息是否已经发出…");
    expect(bar.text()).not.toContain("发送未确认");
    expect(bar.text()).not.toContain("⚠");
    // 查询在飞（busy）：确认条只表达「正在确认」，不抢按钮
    expect(bar.text()).not.toContain("再查一次");

    // 查询失败（测试放行）：保持「正在确认（可以再查一次）」，操作位出现
    // 读类请求自带一次重试：两次尝试都以网络失败放行，查询才算真的失败
    lookupRejecters[0]?.(new TypeError("Failed to fetch"));
    await flushAsyncTimers(0);
    lookupRejecters[1]?.(new TypeError("Failed to fetch"));
    await flushAsyncTimers(0);
    await nextTick();
    const bar2 = w.find(".send-confirm");
    expect(bar2.text()).toContain("可以再查一次");
    expect(bar2.text()).toContain("再查一次");
    expect(bar2.text()).toContain("重试");
    // 没有「放弃」：还没有任何 404 证据，不能让用户误判「没送出」
    expect(w.findAll(".confirm-btn.abandon").length).toBe(0);
    // 消息没有被动过：还在流里，POST 只发过一次（不自动建第二条任务）
    expect(session.messages.some((m) => m.content === "正在确认的消息")).toBe(true);
    expect(gate.callsMatching("POST", "/api/turns").length).toBe(1);
    w.unmount();
  });

  it("查证 404：文案切到「发送未确认…」，提供【重试 / 放弃】；放弃恢复草稿、撤回消息", async () => {
    beforeEachImpl();
    vi.useFakeTimers();
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return statusResponse(404, { ok: false, unknown: true });
      }
      return undefined;
    });
    const { w, session } = await mountComposer();
    const ta = w.find("textarea");
    await ta.setValue("没送达的消息");
    await w.find(".send-btn").trigger("click");
    expect((ta.element as HTMLTextAreaElement).value).toBe("");

    await flushAsyncTimers(700);
    await nextTick();

    const bar = w.find(".send-confirm");
    expect(bar.exists()).toBe(true);
    // ← 要求 2：两态文案必须是**真的渲染**出来的两段话
    expect(bar.text()).toContain("发送未确认：后端没有该请求记录（可能未送达）");
    expect(bar.text()).not.toContain("正在确认这条消息是否已经发出…");
    expect(w.findAll(".confirm-btn.abandon").length).toBe(1);

    // 放弃：撤回消息 + 恢复草稿（store 负责，输入框显示回原文）
    await w.find(".confirm-btn.abandon").trigger("click");
    await nextTick();

    expect(w.find(".send-confirm").exists()).toBe(false);
    expect(session.messages.some((m) => m.content === "没送达的消息")).toBe(false);
    expect((ta.element as HTMLTextAreaElement).value).toBe("没送达的消息");
    expect(session.sendAttempts[0].state).toBe("confirmed-rejected");
    w.unmount();
  });

  it("明确 4xx：沿用既有提示通道（lastError=发送失败：被拒绝），不出现确认条，草稿恢复", async () => {
    beforeEachImpl();
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        return statusResponse(400, { detail: "bad request" });
      }
      return undefined;
    });
    const { w, session } = await mountComposer();
    const ta = w.find("textarea");
    await ta.setValue("会被拒绝的消息");
    await w.find(".send-btn").trigger("click");
    await flushPromises();
    await nextTick();

    // 「明确失败：被拒绝」走 lastError（ConversationView 的既有错误条渲染它）
    expect(session.lastError).toContain("发送失败：被拒绝");
    expect(session.sendConfirm).toBeNull();
    // ← 与 needs-confirm 相反：明确失败要恢复草稿（既有行为）
    expect((ta.element as HTMLTextAreaElement).value).toBe("会被拒绝的消息");
    expect(w.find(".send-confirm").exists()).toBe(false);
    expect(session.messages.some((m) => m.content === "会被拒绝的消息")).toBe(false);
    w.unmount();
  });

  it("确认条上的【重试】：复用同一个 client_request_id 重新 POST，命中即收下确认条", async () => {
    beforeEachImpl();
    vi.useFakeTimers();
    let postCount = 0;
    gate.respond((url, init) => {
      if (init.method === "POST" && url.endsWith("/api/turns")) {
        postCount += 1;
        if (postCount === 1) return Promise.reject(new TypeError("Failed to fetch"));
        return okResponse({ ok: true, turn_id: "turn_x", status: "accepted", deduplicated: true });
      }
      if (init.method === "GET" && url.includes("/api/turns/by-request/")) {
        return statusResponse(404, { ok: false, unknown: true });
      }
      return undefined;
    });
    const { w, session } = await mountComposer();
    await w.find("textarea").setValue("幂等重试的消息");
    await w.find(".send-btn").trigger("click");
    await flushAsyncTimers(700); // 查证 → 404 → 确认条两态到位
    await nextTick();
    expect(w.find(".send-confirm").text()).toContain("发送未确认");

    await w.find(".send-confirm .confirm-btn").trigger("click"); // 重试
    await flushAsyncTimers(0);

    const posts = gate.callsMatching("POST", "/api/turns");
    expect(posts.length).toBe(2);
    const id1 = (posts[0].body as Record<string, unknown>)?.client_request_id;
    const id2 = (posts[1].body as Record<string, unknown>)?.client_request_id;
    expect(id2).toBe(id1); // 幂等：不换 id 重发
    expect(session.sendAttempts[0].state).toBe("accepted");
    // 已受理：确认条收下，消息保留
    expect(w.find(".send-confirm").exists()).toBe(false);
    expect(session.messages.some((m) => m.content === "幂等重试的消息")).toBe(true);
    w.unmount();
  });
});
