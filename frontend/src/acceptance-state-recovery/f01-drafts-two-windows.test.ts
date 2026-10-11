/**
 * 【审计 8fa8452 · F01】另一窗口保存会删除已经保存的草稿。
 *
 * 这条用例走**真实服务层**（services/interactive.ts → fetch），只把传输层换成
 * 一个按契约 C1 实现的假服务端；两个「窗口」是两个独立的 Pinia store 实例。
 *
 * 覆盖任务书 F01 的验收点：
 * - 两页改不同卡片：B 页（旧快照）只保存自己的键，A 页已保存的草稿必须还在；
 * - 同卡片冲突：服务端保留自己那份，并把双方文字交回客户端（不许最后到达静默覆盖）；
 * - 明确删除：只有把键放进 removed 才删除，且带身份与版本；
 * - 迟到回执：在飞期间的新输入不被旧回执清掉。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../stores/interactive";
import { cardLocalDraftStorageKey } from "../interactive/drafts";
import { installFakeBackend, type FakeBackend, type RecordedRequest } from "./support/fakeBackend";

interface KeyMeta {
  version: number;
  deleted: boolean;
  text: string;
}

/** 按契约 C1 实现的假服务端：逐键更新 / 明确删除 / 未修改三态 + baseRev 校验。 */
function installDraftServer() {
  const drafts: Record<string, string> = {};
  const keys: Record<string, KeyMeta> = {};
  let rev = 0;

  const keyVersions = () => {
    const out: Record<string, number> = {};
    for (const [key, meta] of Object.entries(keys)) if (!meta.deleted) out[key] = meta.version;
    return out;
  };

  function applyPatch(body: Record<string, unknown>) {
    const updates = (body.drafts ?? {}) as Record<string, string>;
    const removed = (body.removed ?? []) as string[];
    const baseRev = typeof body.baseRev === "number" ? body.baseRev : null;
    const versions = (body.keyVersions ?? {}) as Record<string, number>;
    const safeBase = baseRev !== null && baseRev === rev;
    const identityOk = (key: string) => {
      const meta = keys[key];
      return Boolean(meta) && meta.version > 0 && versions[key] === meta.version;
    };
    const conflicts: { key: string; local: string; server: string }[] = [];
    const blockedRemovals: string[] = [];
    const cleared: string[] = [];
    for (const [key, text] of Object.entries(updates)) {
      const meta = keys[key];
      if (!meta) {
        keys[key] = { version: 1, deleted: false, text };
        drafts[key] = text;
        rev += 1;
        continue;
      }
      if (meta.deleted) {
        if (safeBase || identityOk(key)) {
          keys[key] = { version: meta.version + 1, deleted: false, text };
          drafts[key] = text;
          rev += 1;
        } else {
          conflicts.push({ key, local: text, server: meta.text });
        }
        continue;
      }
      if (drafts[key] === text) continue; // 未修改：不动版本
      if (safeBase || identityOk(key)) {
        keys[key] = { version: meta.version + 1, deleted: false, text };
        drafts[key] = text;
        rev += 1;
      } else {
        conflicts.push({ key, local: text, server: drafts[key] });
      }
    }
    for (const key of removed) {
      const meta = keys[key];
      if (!meta || meta.deleted || !(key in drafts)) continue;
      if (safeBase || identityOk(key)) {
        keys[key] = { version: meta.version + 1, deleted: true, text: drafts[key] };
        delete drafts[key];
        cleared.push(key);
        rev += 1;
      } else {
        blockedRemovals.push(key);
        conflicts.push({ key, local: "", server: drafts[key] });
      }
    }
    return {
      drafts: { ...drafts },
      updatedAt: "2026-10-11T00:00:00Z",
      rev,
      keyVersions: keyVersions(),
      cleared: cleared.sort(),
      conflicts,
      blockedRemovals: blockedRemovals.sort(),
      mode: "patch",
      legacyReplace: false,
    };
  }

  const backend: FakeBackend = installFakeBackend((req: RecordedRequest) => {
    const url = req.url;
    if (req.method === "PUT" && url.includes("/api/interactive/drafts/")) {
      return { body: applyPatch(req.body ?? {}) };
    }
    if (req.method === "GET" && url.includes("/api/interactive/drafts/")) {
      return { body: { drafts: { ...drafts }, updatedAt: "t", rev, keyVersions: keyVersions() } };
    }
    if (req.method === "GET" && url.endsWith("/state")) {
      return {
        body: {
          board: { id: "board_default", title: "板面" },
          state: {
            boardId: "board_default", seq: 1, updatedAt: "t",
            cards: [
              { id: "a", kind: "text", content: "A 卡正文", meta: {}, x: 0, y: 0, w: 1, h: 1,
                checked: false, hidden: false, folded: false, bookmarked: false, deleted: false,
                createdAt: "t", updatedAt: "t" },
              { id: "b", kind: "text", content: "B 卡正文", meta: {}, x: 0, y: 0, w: 1, h: 1,
                checked: false, hidden: false, folded: false, bookmarked: false, deleted: false,
                createdAt: "t", updatedAt: "t" },
            ],
            groups: [], links: [], selection: [],
          },
          seq: 1,
          baseline: null,
          submissions: [],
          drafts: { drafts: { ...drafts }, updatedAt: "t", rev, keyVersions: keyVersions() },
        },
      };
    }
    if (req.method === "GET" && url.includes("/visible-range")) {
      return { body: { visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true } } };
    }
    if (req.method === "GET" && url.includes("/intents")) {
      return { body: { intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } };
    }
    if (req.method === "PUT" && url.includes("/state")) {
      return { body: { ok: true, seq: 1, savedAt: "t", state: (req.body as { state?: unknown })?.state ?? {} } };
    }
    return undefined;
  });

  return {
    backend,
    state: () => ({ drafts: { ...drafts }, rev, keyVersions: keyVersions() }),
    lastPut: (): Record<string, unknown> | null => {
      const puts = backend.of("PUT").filter((item) => item.url.includes("/drafts/"));
      return (puts[puts.length - 1]?.body ?? null) as Record<string, unknown> | null;
    },
    puts: () => backend.of("PUT").filter((item) => item.url.includes("/drafts/")),
  };
}

/** 新开一个「窗口」：独立的 Pinia 与 store，共用同一个假服务端。 */
async function openWindow() {
  setActivePinia(createPinia());
  const store = useInteractiveStore();
  await store.load();
  await flushPromises();
  return store;
}

let server: ReturnType<typeof installDraftServer>;

beforeEach(() => {
  localStorage.clear();
  server = installDraftServer();
});

afterEach(() => {
  server.backend.restore();
  localStorage.clear();
});

describe("F01 两窗口编辑同一板面的草稿", () => {
  it("B 页用旧快照只保存自己的卡片：A 页已保存的草稿必须还在", async () => {
    const winA = await openWindow();
    const winB = await openWindow(); // B 在读完之后（rev 仍是 A 保存之前的值）

    winA.setDraft("card:a", "A 页写了一半");
    await winA.flushDrafts();
    await flushPromises();
    expect(server.state().drafts, "A 页保存失败").toEqual({ "card:a": "A 页写了一半" });

    // B 只改自己的卡片：请求里不许出现 card:a，也不许带任何 removed
    winB.setDraft("card:b", "B 页的问题");
    await winB.flushDrafts();
    await flushPromises();

    const put = server.lastPut();
    expect(put?.mode).toBe("patch");
    expect(put?.drafts, "B 页只应上传自己改过的键").toEqual({ "card:b": "B 页的问题" });
    expect(put?.removed ?? [], "没有明确删除时不许带 removed（旧快照不得隐式删除）").toEqual([]);
    expect(server.state().drafts, "A 页已保存的草稿被 B 页的保存删掉了（F01）").toEqual({
      "card:a": "A 页写了一半",
      "card:b": "B 页的问题",
    });

    // 新开一个窗口读服务器事实：两份都还在
    const reopened = await openWindow();
    expect(reopened.cardDraftText("a")).toBe("A 页写了一半");
    expect(reopened.cardDraftText("b")).toBe("B 页的问题");
  });

  it("同卡片冲突：服务端保留自己那份，双方文字都交给客户端（不静默覆盖）", async () => {
    const winA = await openWindow();
    winA.setDraft("card:a", "甲的第一版");
    await winA.flushDrafts();
    await flushPromises();

    const winB = await openWindow(); // 读到 card:a=甲的第一版（version 1）
    winA.setDraft("card:a", "甲的第二版");
    await winA.flushDrafts();
    await flushPromises();
    expect(server.state().drafts["card:a"]).toBe("甲的第二版");

    // B 手里还是第一版，却要改成别的内容
    winB.setDraft("card:a", "乙的版本");
    await winB.flushDrafts();
    await flushPromises();

    expect(server.state().drafts["card:a"], "服务端那份被静默覆盖了").toBe("甲的第二版");
    expect(winB.draftConflictFor("a"), "同键冲突必须登记并保留双方文字").toEqual({
      local: "乙的版本",
      server: "甲的第二版",
    });
    expect(winB.cardDraftText("a"), "本机文字不许丢").toBe("乙的版本");
    expect(winB.draftStateFor("card:a").status, "冲突不是「已保存」").toBe("error");
  });

  it("明确删除：只有 removed 里的键才被清掉，且带身份与版本", async () => {
    const win = await openWindow();
    win.setDraft("card:a", "要删掉的草稿");
    await win.flushDrafts();
    await flushPromises();
    expect(server.state().drafts["card:a"]).toBe("要删掉的草稿");

    win.clearDraft("card:a");
    await win.flushDrafts();
    await flushPromises();

    const put = server.lastPut();
    expect(put?.removed, "删除必须以 removed 明确声明").toEqual(["card:a"]);
    expect(server.state().drafts, "服务器没有真的清掉").toEqual({});
    expect(win.draftRemovalStateFor("card:a").status, "服务器已确认的删除不该停在 pending").toBe("idle");
    expect(win.cardDraftText("a")).toBe("");
  });

  it("迟到回执：请求在飞期间的新输入不被旧回执清掉", async () => {
    const win = await openWindow();
    // 手工只让第一次 PUT 挂起，模拟「回执迟到」
    const pending: { resolve: (() => void) | null } = { resolve: null };
    const real = globalThis.fetch;
    let holdFirst = true;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String((input as Request).url ?? input);
      if (holdFirst && init?.method === "PUT" && url.includes("/drafts/")) {
        holdFirst = false;
        await new Promise<void>((resolve) => {
          pending.resolve = resolve;
        });
      }
      return real(input as never, init);
    }) as typeof fetch;

    win.setDraft("card:a", "第一版");
    const flushing = win.flushDrafts();
    await flushPromises();
    win.setDraft("card:a", "在飞期间的新输入");
    pending.resolve?.();
    await flushing;
    await flushPromises();

    expect(win.cardDraftText("a"), "迟到回执把更新的本机文字清掉了").toBe("在飞期间的新输入");

    // 收尾再发一轮：最新文字最终必须落到服务器
    await win.flushDrafts();
    await flushPromises();
    expect(server.state().drafts["card:a"]).toBe("在飞期间的新输入");
    globalThis.fetch = real;
    // 本机恢复副本仍然是新版文字（成功回执只清理确实上传并确认的那一版）
    const local = globalThis.localStorage.getItem(cardLocalDraftStorageKey("a"));
    expect(local === null || local.includes("在飞期间的新输入")).toBe(true);
  });
});
