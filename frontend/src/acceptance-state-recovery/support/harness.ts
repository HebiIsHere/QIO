/**
 * 【独立验收 D · 第八轮状态恢复收尾】共享测试装置。
 *
 * 只被本目录下的验收测试使用；不改产品代码、不依赖产品实现细节。
 * 这里提供的都是「真实路径上会出现的形状」：板面 payload、演示意图、可控存储、
 * 以及可手动 resolve 的 Promise（用来制造真实的在飞窗口）。
 */
import type { BoardState } from "../../interactive/types";

export function card(
  id: string,
  content: string,
  extra: Partial<BoardState["cards"][number]> = {},
): BoardState["cards"][number] {
  return {
    id,
    kind: "text",
    content,
    meta: {},
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "",
    updatedAt: "",
    ...extra,
  };
}

export function state(
  seq: number,
  cards: BoardState["cards"],
  extra: Partial<BoardState> = {},
): BoardState {
  return {
    boardId: "board_default",
    seq,
    updatedAt: "2026-10-10T00:00:00Z",
    cards,
    groups: [],
    links: [],
    selection: [],
    ...extra,
  };
}

export function payload(
  seq: number,
  cards: BoardState["cards"],
  drafts: Record<string, string> = {},
  extra: Record<string, unknown> = {},
): never {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: state(seq, cards),
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts, updatedAt: "t", rev: 1 },
    ...extra,
  } as never;
}

export function intent(id: string, status = "running"): never {
  return {
    id,
    boardId: "board_default",
    submissionId: null,
    title: "任务-" + id,
    summary: "",
    status,
    preview: { kind: "task" },
    impact: { materials: ["c1"], consequence: "暂停并保留进度" },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: ["c1"],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: true,
    createdAt: "",
    updatedAt: "",
  } as never;
}

/** 可手动 resolve 的 Promise：用来精确制造「请求还在飞」的窗口。 */
export function deferred<T = never>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (err: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** 409 错误体：与 services/interactive.ts 解出的 payload 同形状（FastAPI 的 { detail: {...} }）。 */
export function apiError(status: number, payloadBody: Record<string, unknown>): Error {
  return Object.assign(new Error(String(status)), { status, payload: payloadBody });
}

export interface ControllableStorage {
  /** 下一次 removeItem 抛错（模拟存储故障），只生效一次 */
  failNextRemove(): void;
  /** 下一次 setItem 抛错（模拟本机写入失败），只生效一次 */
  failNextWrite(): void;
  restore(): void;
}

/**
 * 记忆型 localStorage：内容跨「重开」保留，可让某一次 removeItem / setItem 抛错。
 * 产品代码每次访问 localStorage 都重新解析，所以替换 globalThis.localStorage 就能覆盖所有读写路径。
 */
export function installControllableStorage(): ControllableStorage {
  const map = new Map<string, string>();
  let failRemoveOnce = false;
  let failWriteOnce = false;
  const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      get length() {
        return map.size;
      },
      key: (index: number) => Array.from(map.keys())[index] ?? null,
      getItem: (key: string) => (map.has(key) ? (map.get(key) as string) : null),
      setItem: (key: string, value: string) => {
        if (failWriteOnce) {
          failWriteOnce = false;
          const err = new Error("quota");
          err.name = "QuotaExceededError";
          throw err;
        }
        map.set(key, value);
      },
      removeItem: (key: string) => {
        if (failRemoveOnce) {
          failRemoveOnce = false;
          const err = new Error("quota");
          err.name = "QuotaExceededError";
          throw err;
        }
        map.delete(key);
      },
      clear: () => map.clear(),
    },
  });
  return {
    failNextRemove: () => {
      failRemoveOnce = true;
    },
    failNextWrite: () => {
      failWriteOnce = true;
    },
    restore: () => {
      if (original) Object.defineProperty(globalThis, "localStorage", original);
      else delete (globalThis as { localStorage?: unknown }).localStorage;
    },
  };
}
