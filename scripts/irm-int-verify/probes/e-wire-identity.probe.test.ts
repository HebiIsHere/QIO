/**
 * 【E 接缝探针 · 不进产品代码】真实后端 JSON × 真实前端解析。
 *
 * 两档证据，跑的时候看控制台里的 [e-wire] 行：
 *   live    —— VITE_QIO_BACKEND_URL 指向真后端（E 起的 8848）：用**真实** services 发请求，
 *              服务端返回的 JSON 原样喂给真实的判定函数；
 *   fixture —— 没有真后端时，用 probe_wire.py 抓下来的**原样响应体**做夹具（夹具级证据）。
 *
 * 断言的都是字段名 / 类型这类**跨语言接缝**：
 *   clientRequestId 原样、submissionId 非空、baseStateVersion 是 number、note 是字符串、
 *   acceptanceMatchesIdentity 判为「本次提交」；换一个身份 → 404 → 判为「不是本次」。
 */
import { describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import * as fs from "node:fs";
import * as path from "node:path";

import * as api from "../services/interactive";
import { useInteractiveStore } from "../stores/interactive";
import {
  acceptanceMatchesIdentity,
  canLookupAcceptance,
  isRequestIdentityConflict,
  submissionIdentityFor,
} from "../interactive/submission";
import type { AcceptanceLookup } from "../interactive/types";

const LIVE_BASE = String((import.meta as { env?: Record<string, string> }).env?.VITE_QIO_BACKEND_URL ?? "");
const LIVE = LIVE_BASE.length > 0;
const BOARD = "board_e_wire_" + Date.now().toString(36);
const RUN_TAG = "e-wire-" + Date.now().toString(36);
const WIRE_JSON =
  (import.meta as { env?: Record<string, string> }).env?.QIO_WIRE_JSON ??
  path.resolve(process.cwd(), "..", "scripts", "irm-int-verify", "out", "wire-1105c3f.json");

if (!LIVE) {
  console.log("[e-wire] mode=fixture  夹具=" + WIRE_JSON);
} else {
  console.log("[e-wire] mode=live  base=" + LIVE_BASE);
}

function wire(): Record<string, any> {
  return JSON.parse(fs.readFileSync(WIRE_JSON, "utf8"));
}

function step(name: string): any {
  const found = (wire().steps as any[]).find((s) => s.name === name);
  if (!found) throw new Error("夹具里没有这一步：" + name);
  return found;
}

async function liveJson(method: string, url: string, body?: unknown): Promise<{ status: number; body: any }> {
  const resp = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  return { status: resp.status, body: text ? JSON.parse(text) : null };
}

/** 保存板面；遇到「需要确认影响」按既有流程确认一次（与 probe_wire.py 同一口径）。 */
async function liveSaveState(boardId: string, state: Record<string, any>): Promise<void> {
  const put = () =>
    liveJson("PUT", LIVE_BASE + "/api/interactive/boards/" + boardId + "/state", { state, reason: "e-wire" });
  let resp = await put();
  if (resp.status === 409) {
    const meta = await liveJson("GET", LIVE_BASE + "/api/interactive/boards/" + boardId + "/state");
    state.seq = Number(meta.body.seq);
    const check = await liveJson("POST", LIVE_BASE + "/api/interactive/boards/" + boardId + "/impact-check", {
      stateVersion: state.seq,
      changeSet: { state },
    });
    resp = await liveJson("PUT", LIVE_BASE + "/api/interactive/boards/" + boardId + "/state", {
      state,
      reason: "e-wire",
      confirm: { checkId: check.body?.checkId, stateVersion: check.body?.stateVersion },
    });
  }
  expect([200, 304], "保存板面必须成功：" + JSON.stringify(resp.body).slice(0, 200)).toContain(resp.status);
}

describe("E 接缝：真实后端 JSON 能被真实前端判定为「本次提交」", () => {
  it.skipIf(!LIVE)("活体：真实 services + 真后端（提交 → 查证 → 换身份 404 → 同身份不同内容 409）", async () => {
    await liveJson("POST", LIVE_BASE + "/api/interactive/boards", { boardId: BOARD, title: "E 接缝" });
    const state = {
      boardId: BOARD,
      seq: 0,
      updatedAt: "2026-10-11T00:00:00+00:00",
      cards: [
        { id: "w1", kind: "file", content: "甲材料", meta: { name: "甲.txt" }, x: 0, y: 0, w: 240, h: 120,
          checked: false, hidden: false, folded: false, bookmarked: false, deleted: false,
          createdAt: "t", updatedAt: "t" },
        { id: "w2", kind: "text", content: "请比较", meta: {}, x: 0, y: 0, w: 240, h: 120,
          checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
          createdAt: "t", updatedAt: "t" },
      ],
      groups: [],
      links: [],
      selection: [],
    };
    await liveSaveState(BOARD, state as Record<string, any>);
    const meta = await liveJson("GET", LIVE_BASE + "/api/interactive/boards/" + BOARD + "/state");
    const seq = Number(meta.body.seq);
    expect(typeof seq).toBe("number");

    const identity = submissionIdentityFor({ boardId: BOARD, baseStateVersion: seq, note: "" });
    expect(canLookupAcceptance(identity)).toBe(true);

    const result = await api.submitBoard(BOARD, ["w1", "w2"], "", seq, undefined, identity.clientRequestId);
    expect(result.submission.clientRequestId, "提交响应里必须回带同一个 clientRequestId").toBe(
      identity.clientRequestId,
    );
    expect(result.delivery?.status, "受理回执").toBe("accepted");
    expect(result.delivery?.understandingId, "受理回执必须给理解请求身份").toBeTruthy();

    const lookup = await api.fetchAcceptance(BOARD, identity.clientRequestId);
    const acceptance = lookup.acceptance as AcceptanceLookup | null;
    expect(acceptance, "刚提交过的身份必须查得到").not.toBeNull();
    expect(typeof acceptance!.baseStateVersion, "baseStateVersion 必须是 number").toBe("number");
    expect(acceptance!.baseStateVersion).toBe(seq);
    expect(typeof acceptance!.note, "note 必须是字符串（空串也算）").toBe("string");
    expect(acceptance!.note).toBe("");
    expect(typeof acceptance!.submissionId === "string" && acceptance!.submissionId.length > 0).toBe(true);
    expect(acceptance!.submissionId).toBe(result.submission.id);
    expect(acceptance!.clientRequestId, "查证响应必须原样回带身份").toBe(identity.clientRequestId);
    expect(acceptanceMatchesIdentity(identity, acceptance), "前端必须判为「本次提交」").toBe(true);

    // 换一个身份查同一个板面 → 404 → 前端保持「不是本次」
    const other = submissionIdentityFor({ boardId: BOARD, baseStateVersion: seq, note: "" });
    expect(other.clientRequestId).not.toBe(identity.clientRequestId);
    const miss = await api.fetchAcceptance(BOARD, other.clientRequestId);
    expect(miss.acceptance, "没有记录的身份必须是 null（不是错误推导的成功）").toBeNull();
    expect(typeof miss.reason).toBe("string");
    expect(acceptanceMatchesIdentity(other, miss.acceptance)).toBe(false);

    // 同身份但内容不同 → 409 request_identity_conflict，且前端能识别
    let conflict = false;
    try {
      await api.submitBoard(BOARD, ["w1", "w2"], "换一个要求", seq, undefined, identity.clientRequestId);
    } catch (err) {
      conflict = isRequestIdentityConflict(err);
    }
    expect(conflict, "同身份不同内容必须被前端识别为 request_identity_conflict").toBe(true);
  }, 30000);

  it.skipIf(!LIVE)("活体：store 级查证 —— 命中恢复受理事实，换身份保持 unknown", async () => {
    const boardId = "board_default";
    await liveJson("POST", LIVE_BASE + "/api/interactive/boards", { boardId, title: "E 接缝 store" });
    const state = {
      boardId,
      seq: 0,
      updatedAt: "2026-10-11T00:00:00+00:00",
      cards: [
        { id: "s1", kind: "file", content: "甲材料", meta: { name: "甲.txt" }, x: 0, y: 0, w: 240, h: 120,
          checked: false, hidden: false, folded: false, bookmarked: false, deleted: false,
          createdAt: "t", updatedAt: "t" },
        { id: "s2", kind: "text", content: "请比较（" + RUN_TAG + "）", meta: {}, x: 0, y: 0, w: 240, h: 120,
          checked: true, hidden: false, folded: false, bookmarked: false, deleted: false,
          createdAt: "t", updatedAt: "t" },
      ],
      groups: [],
      links: [],
      selection: [],
    };
    await liveSaveState(boardId, state as Record<string, any>);
    const seq = Number((await liveJson("GET", LIVE_BASE + "/api/interactive/boards/" + boardId + "/state")).body.seq);

    const identity = submissionIdentityFor({ boardId, baseStateVersion: seq, note: "" });
    const submitted = await api.submitBoard(boardId, ["s1", "s2"], "", seq, undefined, identity.clientRequestId);

    setActivePinia(createPinia());
    const store = useInteractiveStore();
    // 模拟「回执丢了」：本机只知道这次请求身份与内容依据，服务端可能已经受理
    store.pendingSubmission = identity;
    store.submitStatus = "unknown";
    store.submitResultUnknown = true;

    const hit = await store.confirmAcceptanceAfterUnclear();
    expect(hit?.submissionId, "命中必须恢复本次提交的事实").toBe(submitted.submission.id);
    expect(store.lastAcceptance?.submissionId).toBe(submitted.submission.id);
    expect(store.submitStatus, "精确命中才认受理").toBe("succeeded");
    expect(store.submitResultUnknown).toBe(false);
    expect(String(store.submitNote ?? "")).not.toBe("");

    // 换一个身份查同一个板面：查不到 → 保持「结果未知」，保留内容与勾选
    const other = submissionIdentityFor({ boardId, baseStateVersion: seq, note: "" });
    store.pendingSubmission = other;
    store.submitStatus = "unknown";
    store.submitResultUnknown = true;
    const miss = await store.confirmAcceptanceAfterUnclear();
    expect(miss, "没有记录的身份不能返回任何受理事实").toBeNull();
    expect(store.submitStatus, "查不到时必须保持 unknown（不假定成功也不改判失败）").toBe("unknown");
    expect(store.submitResultUnknown).toBe(true);
    expect(String(store.acceptanceLookupNote ?? ""), "必须如实说明「还没确认」").not.toBe("");
    store.stopUnderstandingPolling();
  }, 30000);

  it("夹具：抓下来的服务端原样 JSON 同样能通过同一组判定", () => {
    const submit = step("submit");
    const acceptanceStep = step("acceptance-hit");
    const missStep = step("acceptance-miss");
    const conflictStep = step("submit-conflict");
    expect(submit.status).toBe(200);
    expect(acceptanceStep.status).toBe(200);
    expect(missStep.status).toBe(404);
    expect(conflictStep.status).toBe(409);

    const result = submit.body as Record<string, any>;
    const acceptance = acceptanceStep.body.acceptance as AcceptanceLookup;
    const requestId = String(result.submission.clientRequestId);
    expect(requestId.startsWith("creq_")).toBe(true);

    const identity = submissionIdentityFor({
      boardId: BOARD,
      baseStateVersion: Number(acceptance.baseStateVersion),
      note: String(acceptance.note),
    });
    // 夹具里的身份是服务端回带的那个：这里直接按它构造，验证字段名与类型能被前端消费
    const withEcho: typeof identity = { ...identity, clientRequestId: requestId };
    expect(typeof acceptance.baseStateVersion).toBe("number");
    expect(typeof acceptance.note).toBe("string");
    expect(typeof acceptance.submissionId).toBe("string");
    expect(acceptance.submissionId.length).toBeGreaterThan(0);
    expect(typeof acceptance.understandingId).toBe("string");
    expect(acceptance.delivery?.status).toBe("accepted");
    expect(acceptanceMatchesIdentity(withEcho, acceptance)).toBe(true);

    expect(missStep.body.acceptance).toBeNull();
    expect(typeof missStep.body.reason).toBe("string");
    expect(acceptanceMatchesIdentity(withEcho, missStep.body.acceptance)).toBe(false);

    const conflictPayload = conflictStep.body.detail ?? conflictStep.body;
    expect(conflictPayload.error).toBe("request_identity_conflict");
    expect(isRequestIdentityConflict({ payload: conflictPayload, status: 409 })).toBe(true);
  });
});
