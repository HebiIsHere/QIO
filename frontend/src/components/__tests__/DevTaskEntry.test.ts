/**
 * 「有 N 个工具开发任务没做完」这一行。
 *
 * 回归的是这个真实缺口：任务只在模型那一轮的工具调用里出现过，模型不提，
 * 刷新或重启之后界面上就再也找不到它 —— 用户不知道还有一件事没做完。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DevTaskEntry from "../DevTaskEntry.vue";
import { useSessionStore } from "../../stores/session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(async () => ({
      ok: true,
      accepted: true,
      turn_id: "t1",
      status: "accepted",
      topic_id: null,
    })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
    revokeDevAuthorization: vi.fn(async () => ({ revoked: true })),
  },
}));

const sendTurn = api.sendTurn as unknown as ReturnType<typeof vi.fn>;
const listDevAuthorizations = api.listDevAuthorizations as unknown as ReturnType<typeof vi.fn>;
const revokeDevAuthorization = api.revokeDevAuthorization as unknown as ReturnType<typeof vi.fn>;

function task(overrides: Record<string, unknown> = {}) {
  return {
    id: "ws_aaaaaaaaaaaa",
    request: "做一个求和工具",
    phase: "testing_failed",
    submitted: false,
    test_passed: false,
    test_evidence_current: true,
    updated_at: "2026-09-30T10:00:00+00:00",
    authorized: false,
    ...overrides,
  };
}

function authorization(overrides: Record<string, unknown> = {}) {
  return {
    task_id: "ws_aaaaaaaaaaaa",
    request: "做一个求和工具",
    submitted: false,
    executor: "subprocess",
    isolated: false,
    policy_fingerprint: "p1",
    capabilities: ["受限子进程"],
    filesystem: [],
    network: false,
    network_allow: [],
    credentials: ["weather_key"],
    granted_at: "2026-09-30T10:05:00+00:00",
    ...overrides,
  };
}

function setup(tasks: Record<string, unknown>[] = []) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.devTasks = tasks as never;
  const wrapper = mount(DevTaskEntry, { global: { plugins: [pinia] } });
  return { session, wrapper };
}

beforeEach(() => {
  vi.clearAllMocks();
  sendTurn.mockResolvedValue({
    ok: true,
    accepted: true,
    turn_id: "t1",
    status: "accepted",
    topic_id: null,
  });
  listDevAuthorizations.mockResolvedValue({ authorizations: [] });
  revokeDevAuthorization.mockResolvedValue({ revoked: true });
});

describe("未完成开发任务的入口", () => {
  it("没有未完成任务时不显示这一行", () => {
    const { wrapper } = setup([]);
    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
  });

  it("有 1 个未完成任务时只亮出一行，不自动展开清单", () => {
    const { wrapper } = setup([task()]);

    expect(wrapper.find(".dev-task-entry").text()).toContain("有 1 个工具开发任务没做完");
    expect(wrapper.find(".dev-task-panel").exists()).toBe(false);
  });

  it("已提交的任务不算「没做完」，不出现也不计数", () => {
    const { wrapper } = setup([
      task({ id: "ws_done", request: "已经注册好的工具", phase: "ready", submitted: true, test_passed: true }),
    ]);

    expect(wrapper.find(".dev-task-entry").exists()).toBe(false);
  });

  it("点这一行才展开只读清单，并说清证据对不上当前内容", async () => {
    const { wrapper } = setup([
      task({ id: "ws_stale", request: "把文件转成 CSV", test_passed: true, test_evidence_current: false }),
      task({ id: "ws_none", request: "抓一个网页", test_passed: null, phase: "building" }),
    ]);

    await wrapper.find(".dev-task-entry").trigger("click");

    const panel = wrapper.find(".dev-task-panel");
    expect(panel.exists()).toBe(true);
    expect(panel.text()).toContain("把文件转成 CSV");
    expect(panel.text()).toContain("测试通过，但文件后来改过，结论不算数");
    expect(panel.text()).toContain("还没跑过测试");
    expect(panel.text()).toContain("正在写代码");
    expect(panel.text()).toContain("2026-09-30");
  });

  it("「继续开发」把任务 id 与当前状态交给模型，然后收起清单", async () => {
    const { wrapper } = setup([task({ id: "ws_resume0001", request: "做一个求和工具" })]);
    await wrapper.find(".dev-task-entry").trigger("click");

    await wrapper.find(".dev-task-resume").trigger("click");
    await flushPromises();

    expect(sendTurn).toHaveBeenCalledTimes(1);
    const message = String(sendTurn.mock.calls[0]?.[0] ?? "");
    expect(message).toContain("ws_resume0001");
    expect(message).toContain("做一个求和工具");
    expect(wrapper.find(".dev-task-panel").exists()).toBe(false);
  });

  it("交给模型失败时如实说明，不假装已经交给它", async () => {
    sendTurn.mockRejectedValueOnce(new Error("后端没接上"));
    const { wrapper } = setup([task()]);
    await wrapper.find(".dev-task-entry").trigger("click");

    await wrapper.find(".dev-task-resume").trigger("click");
    await flushPromises();

    expect(wrapper.find(".dev-task-panel").exists()).toBe(true);
    expect(wrapper.find(".dev-task-error").text()).toContain("没能把这件事交给模型");
  });

  it("展开清单时查一次授权范围，并说清「同意了什么」", async () => {
    listDevAuthorizations.mockResolvedValue({ authorizations: [authorization()] });
    const { wrapper } = setup([task({ authorized: true })]);

    await wrapper.find(".dev-task-entry").trigger("click");
    await flushPromises();

    expect(listDevAuthorizations).toHaveBeenCalledTimes(1);
    const scope = wrapper.find(".dev-task-scope").text();
    expect(scope).toContain("受限子进程");
    expect(scope).toContain("weather_key");
  });

  it("没有授权时不显示范围那一行", async () => {
    const { wrapper } = setup([task()]);

    await wrapper.find(".dev-task-entry").trigger("click");
    await flushPromises();

    expect(wrapper.find(".dev-task-scope").exists()).toBe(false);
  });

  it("撤销授权：调后端、这一行的授权提示消失", async () => {
    listDevAuthorizations.mockResolvedValue({ authorizations: [authorization()] });
    const { session, wrapper } = setup([task({ authorized: true })]);
    await wrapper.find(".dev-task-entry").trigger("click");
    await flushPromises();

    await wrapper.find(".dev-task-revoke").trigger("click");
    await flushPromises();

    expect(revokeDevAuthorization).toHaveBeenCalledWith("ws_aaaaaaaaaaaa");
    expect(wrapper.find(".dev-task-scope").exists()).toBe(false);
    expect(session.devAuthorizations).toEqual([]);
  });

  it("撤销失败时如实说明，不假装已经收回", async () => {
    listDevAuthorizations.mockResolvedValue({ authorizations: [authorization()] });
    revokeDevAuthorization.mockRejectedValueOnce(new Error("后端没接上"));
    const { wrapper } = setup([task({ authorized: true })]);
    await wrapper.find(".dev-task-entry").trigger("click");
    await flushPromises();

    await wrapper.find(".dev-task-revoke").trigger("click");
    await flushPromises();

    expect(wrapper.find(".dev-task-scope").exists()).toBe(true);
    expect(wrapper.find(".dev-task-error").text()).toContain("撤销");
  });
});
