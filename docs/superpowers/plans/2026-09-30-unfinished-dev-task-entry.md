# 未完成开发任务入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「没做完的工具开发任务」在界面上一眼可见、可点开、可交给模型接着做，而不是只存在于某一次工具调用里。

**Architecture:** 后端已经给出权威列表（`GET /api/dev/tasks`）。前端只做三件事：把这份列表读进 session store（连接建立 / RESYNC / 每轮结束后刷新），在对话页顶部与审批入口同一处显示一行浅色文字，点开后显示**只读**的任务清单，每条带一个「继续开发」动作把任务 id 与当前状态交给模型。界面不自己推断任务状态、不写本地存储，也不把「拉不到列表」装成「一条都没有」。

**Tech Stack:** Vue 3 + Pinia + TypeScript + Vitest（`@vue/test-utils`）；后端 FastAPI（已就绪，本计划不改后端）。

**Spec:** `docs/superpowers/specs/2026-09-29-tool-dev-spec-phase1.md`（第一阶段的「任务恢复界面」要求）

## Global Constraints

- 界面文案全部中文；不出现内部路径、内部事件名。
- 注释写清「为什么」，不写「是什么」。
- 不新增长期依赖。
- 任务状态只有一个来源：后端 `/api/dev/tasks`。界面不做状态推断、不写本地缓存。
- 未验证的事情必须说清未验证（例如「测试通过，但文件改过了，结论不算数」）。

---

## File Structure

- `frontend/src/services/api.ts` — 新增 `DevTaskRow` 类型与 `api.getDevTasks()`（唯一的取数入口）。
- `frontend/src/stores/session.ts` — 新增 `devTasks` 状态、`unfinishedDevTasks` getter、`refreshDevTasks()`。
- `frontend/src/stores/events.ts` — 每轮结束后刷新一次（一轮里可能刚好创建或提交了任务）。
- `frontend/src/components/DevTaskEntry.vue` — 新组件：顶部那一行 + 只读清单 + 「继续开发」。
- `frontend/src/components/ApprovalEntry.vue` — 去掉元素自身的 `position: fixed`，改由 App.vue 的顶部容器统一堆叠，避免与新增的那一行重叠。
- `frontend/src/App.vue` — 顶部容器 `.top-notes`，按顺序放 `<ApprovalEntry />` 与 `<DevTaskEntry />`。

## Task 1: 取数 + store

**Files:**

- Modify: `frontend/src/services/api.ts`
- Modify: `frontend/src/stores/session.ts`
- Test: `frontend/src/stores/__tests__/devTasks.test.ts`

**Interfaces:**

- Produces: `api.getDevTasks(): Promise<{ tasks: DevTaskRow[] }>`；
  `DevTaskRow = { id: string; request: string; phase: string | null; submitted: boolean; test_passed: boolean | null; test_evidence_current: boolean; updated_at: string | null }`；
  `useSessionStore().devTasks: DevTaskRow[]`、`useSessionStore().unfinishedDevTasks: DevTaskRow[]`（只含 `submitted === false`）、`useSessionStore().refreshDevTasks(): Promise<void>`。

- [ ] **Step 1: 写失败测试**

```ts
it("拉取到的任务进 store，已提交的不算「没做完」", async () => {
  const { session } = setup();
  await session.refreshDevTasks();
  expect(session.devTasks.map((t) => t.id)).toEqual(["ws_a", "ws_b"]);
  expect(session.unfinishedDevTasks.map((t) => t.id)).toEqual(["ws_a"]);
});

it("拉不到列表时保留上一次的结果，不把「拉不到」装成「一条都没有」", async () => {
  const { session } = setup();
  await session.refreshDevTasks();
  getDevTasks.mockRejectedValueOnce(new Error("boom"));
  await session.refreshDevTasks();
  expect(session.unfinishedDevTasks.map((t) => t.id)).toEqual(["ws_a"]);
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/stores/__tests__/devTasks.test.ts`
Expected: FAIL（`session.refreshDevTasks is not a function`）

- [ ] **Step 3: 实现**（`api.ts` 加 `GET /api/dev/tasks`；`session.ts` 加状态 / getter / action）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend; npx vitest run src/stores/__tests__/devTasks.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/services/api.ts frontend/src/stores/session.ts frontend/src/stores/__tests__/devTasks.test.ts
git commit -m "feat(ui): 开发任务列表进 store（取数 + 未完成筛选）"
```

## Task 2: 顶部那一行 + 只读清单

**Files:**

- Create: `frontend/src/components/DevTaskEntry.vue`
- Modify: `frontend/src/components/ApprovalEntry.vue`（去掉元素自身定位）
- Modify: `frontend/src/App.vue`（`.top-notes` 容器）
- Test: `frontend/src/components/__tests__/DevTaskEntry.test.ts`

**Interfaces:**

- Consumes: `useSessionStore().unfinishedDevTasks`、`useSessionStore().send(text)`（已有，返回 `Promise<boolean>`）、`useSessionStore().lastError`。
- Produces: `.dev-task-entry`（那一行按钮）、`.dev-task-panel`（只读清单）、`.dev-task-resume`（每条的动作）。

- [ ] **Step 1: 写失败测试**

```ts
it("没有未完成任务时不显示这一行", () => { /* 空列表 → .dev-task-entry 不存在 */ });
it("有 1 个未完成任务时只显示一行，不自动展开", () => { /* 文案 + .dev-task-panel 不存在 */ });
it("点这一行才展开只读清单，并说清证据对不上当前内容", () => { /* 需求 / 阶段 / 「测试通过，但文件改过了，结论不算数」 */ });
it("「继续开发」把任务 id 与当前状态交给模型，然后收起清单", () => { /* session.send 收到的文本含任务 id */ });
it("交给模型失败时如实说明，不假装已经交给它", () => { /* send=false → 出现失败提示，清单不收起 */ });
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/components/__tests__/DevTaskEntry.test.ts`
Expected: FAIL（组件不存在）

- [ ] **Step 3: 实现**

一行浅色文字按钮；展开后是只读清单（需求 / 阶段 / 测试结论 / 更新时间）+ 每条一个「继续开发」。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend; npx vitest run src/components/__tests__/DevTaskEntry.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/DevTaskEntry.vue frontend/src/components/ApprovalEntry.vue frontend/src/App.vue frontend/src/components/__tests__/DevTaskEntry.test.ts
git commit -m "feat(ui): 对话页顶部显示「有 N 个工具开发任务没做完」"
```

## Task 3: 刷新时机 + 文档

**Files:**

- Modify: `frontend/src/stores/events.ts`（连接 / RESYNC / TURN_END 之后刷新）
- Modify: `docs/status.md`
- Test: `frontend/src/stores/__tests__/devTasks.test.ts`

**Interfaces:**

- Consumes: Task 1 的 `refreshDevTasks()`。
- Produces: 一轮结束后列表自动跟上（刚创建 / 刚提交的任务不用刷新页面就能看到）。

- [ ] **Step 1: 写失败测试**（`TURN_END` 之后 `api.getDevTasks` 被调用一次）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（`resyncTurnState` / `startResync` / `TURN_END` 三处刷新）
- [ ] **Step 4: 跑测试确认通过**：`npx vitest run src/stores/__tests__/devTasks.test.ts src/stores/__tests__/finalAnswer.test.ts`
- [ ] **Step 5: 文档 + 全量验证**：`npx vue-tsc --noEmit`、`npx vitest run`、`python scripts/check_docs.py`
- [ ] **Step 6: 提交**
