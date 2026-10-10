/**
 * V 组独立验证 · 前端 A01/A03：未完成事项收件箱（恢复清单）。
 *
 * 原缺陷（基线 `da0436b`，见 _contracts §2.3）
 *
 *   前端只有 `session.interruptedTurns` 这一条入口，它由 `/api/runtime/state` 的
 *   `interrupted_turns` 喂。历史无归属行 / 孤立 claim 都不在那个数组里，于是界面上
 *   **完全没有出口**：
 *     * 没有 `stores/session.ts` 的 `recoveryRecords` / `recoveryLoading` /
 *       `recoveryError` / `loadRecoveryInbox()` / `continueRecovery(id)` /
 *       `repairOrphan(id)` / `ignoreRecovery(id)` / `requeueDerived(id)`；
 *     * 没有 `services/recoveryApi.ts`；
 *     * 没有 `components/RecoveryInbox.vue`；
 *     * `stores/restore.ts`（唯一恢复入口）也不把 `orphaned_turns` 并进同一清单。
 *
 *   用户看得见的状态里，那些消息就是「不存在」。
 *
 * 验证手段（不 import 尚不存在的新模块作为**静态**依赖，因此基线仍能收集）
 *
 *   * 打桩全局 `fetch`，按 URL 路由；浏览器模式下 `resolveBackend()` 解析到
 *     本机默认地址，不需要 Tauri；
 *   * 需要探测新模块是否交付时用**动态 import**（失败是断言失败，不是收集错误）；
 *   * 断言只看 store 字段、以及实际发出的 HTTP 请求路径与方法。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { restoreRuntimeState } from "../restore";

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

type Route = { status?: number; body?: unknown };
type Handler = (url: string, init: RequestInit) => Route;

const calls: FetchCall[] = [];

function installFetch(handler: Handler) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown, init: RequestInit = {}) => {
      const url = String(input);
      const method = String(init.method ?? "GET").toUpperCase();
      let body: unknown = null;
      try {
        body = init.body ? JSON.parse(String(init.body)) : null;
      } catch {
        body = init.body ?? null;
      }
      calls.push({ url, method, body });
      const route = handler(url, init) ?? {};
      const status = route.status ?? 200;
      const payload = route.body ?? {};
      return {
        ok: status >= 200 && status < 300,
        status,
        async text() {
          return JSON.stringify(payload);
        },
        async json() {
          return payload;
        },
      } as unknown as Response;
    }),
  );
}

function callsTo(fragment: string): FetchCall[] {
  return calls.filter((c) => c.url.includes(fragment));
}

function recoveryRecord(overrides: Record<string, unknown> = {}) {
  return {
    record_id: "turn_legacy_1",
    kind: "user_turn",
    state_class: "legacy_unowned",
    status: "queued",
    message: "这条消息当时还在排队，进程退出后没有开始执行",
    topic_id: null,
    reason: null,
    created_at: "2026-10-10T00:00:00+00:00",
    updated_at: "2026-10-10T00:00:00+00:00",
    owner_instance_id: null,
    owner_state: "none",
    owner_note: "历史上没有记下这条消息是谁写的",
    actions: [
      { id: "continue", label: "继续这条", enabled: true, reason: "" },
      { id: "ignore", label: "知道了", enabled: true, reason: "" },
    ],
    ...overrides,
  };
}

function listing(records: unknown[]) {
  return {
    records,
    total: records.length,
    shown: records.length,
    truncated: false,
  };
}

function runtimeState(overrides: Record<string, unknown> = {}) {
  return {
    instance_id: "inst_verify",
    revision: 1,
    turn_queue: { instance_id: "inst_verify", revision: 1, running: null, queued: [], cancelled: [] },
    approvals: [],
    tasks: [],
    tools: [],
    narratives: [],
    interrupted_approvals: [],
    interrupted_turns: [],
    orphaned_turns: [],
    ...overrides,
  };
}

function defaultHandler(options: {
  records?: unknown[];
  recoveryStatus?: number;
  orphaned?: unknown[];
} = {}): Handler {
  const records = options.records ?? [recoveryRecord()];
  return (url) => {
    if (url.includes("/api/recovery/records")) {
      if (options.recoveryStatus && options.recoveryStatus >= 400) {
        return { status: options.recoveryStatus, body: { detail: "boom" } };
      }
      return { body: listing(records) };
    }
    if (url.includes("/api/runtime/state")) {
      return { body: runtimeState({ orphaned_turns: options.orphaned ?? [] }) };
    }
    if (url.includes("/api/dev/tasks")) return { body: { tasks: [] } };
    if (url.includes("/api/dev/authorizations")) return { body: { authorizations: [] } };
    return { body: {} };
  };
}

/**
 * 收件箱的契约形状（见 _contracts §2.3）。
 *
 * 基线里这些成员还不存在；这里用一个**断言式**类型（而不是静态 import）来引用它们，
 * 目的是让 `vue-tsc --noEmit` 在基线上仍然能通过 —— 用例的失败必须是**运行期断言**
 * 失败，而不是类型/收集错误。
 */
interface RecoveryRecordView {
  record_id: string;
  message?: string;
  status?: string;
  resolved?: boolean;
}

interface RecoverySessionStore {
  recoveryRecords: RecoveryRecordView[];
  recoveryLoading: boolean;
  recoveryError: string | null;
  loadRecoveryInbox: () => Promise<unknown>;
  continueRecovery: (recordId: string) => Promise<unknown>;
  repairOrphan: (recordId: string) => Promise<unknown>;
  ignoreRecovery: (recordId: string) => Promise<unknown>;
  requeueDerived: (recordId: string, expected?: unknown) => Promise<unknown>;
}

function setup(): RecoverySessionStore {
  const pinia = createPinia();
  setActivePinia(pinia);
  return useSessionStore() as unknown as RecoverySessionStore;
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** 「就地更新」的可判定形态：记录消失，或带上已解决标记 / 不再是原状态。 */
function stillPending(store: RecoverySessionStore, id: string): boolean {
  const record = (store.recoveryRecords ?? []).find((r) => r.record_id === id);
  if (!record) return false;
  if (record.resolved === true) return false;
  return true;
}

describe("A01/A03 前端：恢复收件箱", () => {
  it("store 暴露收件箱状态与加载动作，并从 /api/recovery/records 拉取", async () => {
    const store = setup();
    installFetch(defaultHandler());

    expect(Array.isArray(store.recoveryRecords), "store 必须暴露 recoveryRecords").toBe(true);
    expect(typeof store.recoveryLoading, "store 必须暴露 recoveryLoading").toBe("boolean");
    expect(typeof store.loadRecoveryInbox, "store 必须暴露 loadRecoveryInbox()").toBe("function");

    await store.loadRecoveryInbox();
    await flushPromises();

    expect(callsTo("/api/recovery/records").length).toBeGreaterThan(0);
    expect(store.recoveryRecords.map((r) => r.record_id)).toContain("turn_legacy_1");
    expect(store.recoveryRecords[0].message).toContain("排队");
    expect(store.recoveryError ?? null).toBeNull();
  });

  it("专用请求失败时如实报错，不静默吞、也不假装清空", async () => {
    const store = setup();
    installFetch(defaultHandler({ recoveryStatus: 500 }));

    await store.loadRecoveryInbox().catch(() => undefined);
    await flushPromises();

    expect(String(store.recoveryError ?? "").length).toBeGreaterThan(0);
  });

  it("continueRecovery 成功后**就地**更新本地状态（不靠刷新）", async () => {
    const store = setup();
    installFetch((url) => {
      if (url.includes("/continue")) {
        return { body: { ok: true, record_id: "turn_legacy_1", turn_id: "turn_new_1", status: "queued" } };
      }
      return defaultHandler()(url, {});
    });

    await store.loadRecoveryInbox();
    await flushPromises();
    expect(store.recoveryRecords.length).toBe(1);

    await store.continueRecovery("turn_legacy_1");
    await flushPromises();

    const posted = callsTo("/api/recovery/records/turn_legacy_1/continue");
    expect(posted.length, "必须 POST 到冻结的继续端点").toBeGreaterThan(0);
    expect(posted[0].method).toBe("POST");
    expect(stillPending(store, "turn_legacy_1")).toBe(false);
  });

  it("continueRecovery 收到 409 时给出可读原因，并保留这条记录", async () => {
    const store = setup();
    installFetch((url) => {
      if (url.includes("/continue")) {
        return { status: 409, body: { ok: false, conflict: true, reason: "状态已变化" } };
      }
      return defaultHandler()(url, {});
    });

    await store.loadRecoveryInbox();
    await flushPromises();

    let thrown: unknown = null;
    try {
      await store.continueRecovery("turn_legacy_1");
    } catch (error) {
      thrown = error;
    }
    await flushPromises();

    const message = thrown
      ? String((thrown as Error).message ?? "")
      : String(store.recoveryError ?? "");
    expect(message.length, "失败必须抛出可读错误或写进 recoveryError").toBeGreaterThan(0);
    expect(stillPending(store, "turn_legacy_1"), "失败不得把记录从入口里抹掉").toBe(true);
  });

  it("修复 / 忽略 / 放回队列各自打到冻结的端点", async () => {
    const store = setup();
    installFetch((url) => {
      if (url.includes("/repair")) return { body: { ok: true, repaired: true, record_id: "turn_orphan_1" } };
      if (url.includes("/ignore")) return { body: { ok: true, ignored: true, record_id: "turn_legacy_1" } };
      if (url.includes("/requeue")) return { body: { ok: true, state: "pending", record_id: "task_1" } };
      return defaultHandler({
        records: [
          recoveryRecord(),
          recoveryRecord({
            record_id: "turn_orphan_1",
            state_class: "orphaned_claim",
            status: "interrupted",
          }),
        ],
      })(url, {});
    });

    await store.loadRecoveryInbox();
    await flushPromises();

    await store.repairOrphan("turn_orphan_1").catch(() => undefined);
    await store.ignoreRecovery("turn_legacy_1").catch(() => undefined);
    await store.requeueDerived("task_1", { expected_state: "running", expected_generation: 1 }).catch(
      () => undefined,
    );
    await flushPromises();

    expect(callsTo("/api/recovery/records/turn_orphan_1/repair").length).toBeGreaterThan(0);
    expect(callsTo("/api/recovery/records/turn_legacy_1/ignore").length).toBeGreaterThan(0);
    expect(callsTo("/api/recovery/records/task_1/requeue").length).toBeGreaterThan(0);
  });

  it("唯一恢复入口把 /api/runtime/state 的 orphaned_turns 并入同一清单（专用请求失败也看得见）", async () => {
    const store = setup();
    installFetch(
      defaultHandler({
        recoveryStatus: 500,
        orphaned: [
          {
            turn_id: "turn_orphan_9",
            message: "抢占过但没有后继的那条消息",
            status: "interrupted",
            reason: null,
            reason_text: "",
          },
        ],
      }),
    );

    await restoreRuntimeState("resync");
    await flushPromises();

    expect(
      store.recoveryRecords.map((r) => r.record_id),
      "专用请求失败时也必须能看见 orphaned_turns",
    ).toContain("turn_orphan_9");
  });

  it("旧快照不得复活已经处理过的记录", async () => {
    const store = setup();
    installFetch(
      defaultHandler({
        orphaned: [
          {
            turn_id: "turn_orphan_9",
            message: "抢占过但没有后继的那条消息",
            status: "interrupted",
            reason: null,
            reason_text: "",
          },
        ],
      }),
    );

    await restoreRuntimeState("resync");
    await flushPromises();
    expect(store.recoveryRecords.map((r) => r.record_id)).toContain("turn_orphan_9");

    installFetch((url) => {
      if (url.includes("/continue")) {
        return { body: { ok: true, record_id: "turn_orphan_9", turn_id: "turn_new_9", status: "queued" } };
      }
      return defaultHandler({
        orphaned: [
          {
            turn_id: "turn_orphan_9",
            message: "抢占过但没有后继的那条消息",
            status: "interrupted",
            reason: null,
            reason_text: "",
          },
        ],
      })(url, {});
    });
    await store.continueRecovery("turn_orphan_9").catch(() => undefined);
    await flushPromises();

    // 再拉一次**同一份旧快照**：已经处理过的记录不得复活
    await restoreRuntimeState("resync");
    await flushPromises();

    expect(
      store.recoveryRecords.map((r) => r.record_id),
      "已经处理过的记录不许被稍旧的快照复活",
    ).not.toContain("turn_orphan_9");
  });

  it("交付物存在性：services/recoveryApi.ts 与 components/RecoveryInbox.vue", () => {
    // 用文件系统判断交付物是否存在：**不能**用字面量动态 import —— Vite 会在
    // 导入分析阶段就把「解析不到」变成整份 suite 的失败，那样基线会失去「用例红」
    // 的证据（只剩收集错误）。
    const apiPath = resolve(process.cwd(), "src/services/recoveryApi.ts");
    expect(existsSync(apiPath), `services/recoveryApi.ts 必须存在（${apiPath}）`).toBe(true);

    const source = readFileSync(apiPath, "utf8");
    for (const name of [
      "fetchRecoveryRecords",
      "continueRecovery",
      "repairOrphan",
      "ignoreRecovery",
      "requeueDerived",
    ]) {
      expect(source.includes(name), `services/recoveryApi.ts 必须提供 ${name}`).toBe(true);
    }

    const inboxPath = resolve(process.cwd(), "src/components/RecoveryInbox.vue");
    expect(existsSync(inboxPath), `components/RecoveryInbox.vue 必须存在（${inboxPath}）`).toBe(
      true,
    );
  });
});
