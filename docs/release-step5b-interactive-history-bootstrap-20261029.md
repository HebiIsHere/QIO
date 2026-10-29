# 发布前第五轮补充：互动页直接进入时执行记录自动出现（2026-10-29）

- 基线（本轮开工核对，本地 = 远端）：`681d610369e533ce651d71332fede11ccde87b11`
- 分支：`fix/release-step5-integration-validation-20261029`（未合并 main）
- 修复提交（本轮受测代码 SHA）：`a1531e4980c909697a470ba9942121850fd3c8c9`
- 交付 HEAD：本文件所在的文档提交（完整 SHA 以推送回执为准）
- 范围：只修复「直接进入或刷新互动页后，确认意图并执行结束，当前对话面板不自动出现执行记录」。不改后端、不改版本号（0.2.10）、不改权限规则/视觉/DB schema/CI 工作流，不打包、不发布、未触发 CI。

## 问题与根因

普通对话页在 `history.status === "idle"` 时负责首次历史读取（`session.loadHistory()`）；互动页过去只在 `onMounted` 调 `session.reloadHistoryIfStale()`，而 `refreshIntentExecutionRecords()` 遇到 `history.status` 为 `idle` 会直接返回。于是**直接进入 / 刷新互动页**时，历史从未加载：确认并执行后，服务端已把 `content_type=intent_execution` 的记录写进当前话题，但界面上的「刷新记录」前提不成立，记录不会自动出现——只有先切到普通对话页（触发首次加载）才看得到。

## 修复（最小必要）

`frontend/src/components/interactive/v2/InteractiveConversation.vue` 的 `onMounted`：

- `session.history.status === "idle"` → 调用**同一个既有** `session.loadHistory()`（同一 `/api/session/messages` 读取入口；沿用既有消息去重、`_historyRequestSeq` 请求代号与话题归属保护）；
- 已加载过（ready / error / loading）→ 继续既有 `session.reloadHistoryIfStale()`（只兑现「记录可能过期」提示，不重复整段读取）。

不轮询、不伪造本地记录、不强制跳页；不改 store 的刷新/去重/话题保护逻辑，不改展示层与后端。互动页与普通对话页从此共用同一个「首次加载 + 后续 stale 刷新」入口。

## 先失败后通过的定向回归

新增 `frontend/src/components/interactive/v2/__tests__/release-step5b-history-bootstrap.test.ts`（复用真实 session store；用 spy 观察既有读取入口的调用，不预先手动加载历史）：

- 修复前（同一工作树、仅回退组件改动）：`Test Files 1 failed (1)`、`Tests 1 failed | 2 passed (3)`，失败断言为「直接进入互动页（history=idle）会发起一次既有首次历史读取」——`expected "wrappedAction" to be called 1 times, but got 0 times`；
- 修复后：`Test Files 1 passed (1)`、`Tests 3 passed (3)`；另两条守护「history 已就绪时不重复整段读取、仍走既有 stale 刷新」。

## 定向测试（实际命令与实际结果）

```
cd frontend
npx vitest run src/components/interactive/v2/__tests__/release-step5b-history-bootstrap.test.ts \
  src/components/interactive/v2/__tests__/InteractiveConversation.test.ts \
  src/components/interactive/v2/__tests__/conversationGeometryBridge.test.ts \
  src/components/interactive/v2/__tests__/fu5-composer-layout.test.ts \
  src/components/__tests__/IntentExecutionRefresh.test.ts \
  src/components/__tests__/IntentExecutionRefreshQueue.test.ts \
  src/components/__tests__/IntentExecutionMessage.test.ts \
  src/components/__tests__/MessageStreamIntentExecution.test.ts \
  src/components/__tests__/p3b-composer-binding.test.ts \
  src/__tests__/pm-acceptance/e2-interactive-wiring.test.ts \
  src/views/__tests__/InteractiveViewFloating.test.ts \
  src/views/__tests__/InteractiveOldEntriesGone.test.ts
```

- 结果：exit 0，`Test Files 12 passed (12)`、`Tests 84 passed (84)`。
- `npx vue-tsc --noEmit`：exit 0（无输出）。
- 未改后端，未跑后端用例；未跑全量测试、未跑前端生产构建（本轮不要求）、未重跑六项集成、未触发 CI。

## 真实前后端 + 受控模型服务复验（两个固定入口）

环境：真实后端 uvicorn + 真实前端 vite（隔离实例 8845/6208，独立空数据目录）+ 本机扮演的 OpenAI 兼容假厂商（受控模型服务，支持 SSE 分片与板面工具调用碎片；**不是真实厂商**）；真实界面用 Playwright + 系统 Edge 无头。记录观察阶段不刷新、不切页、不切 visibility。

### 入口 A：新上下文直接进入 `#/interactive`（含页面刷新场景）— PASS

- `POST /submissions` = 200（succeeded）；确认前对话面板 `data-ic-expanded=true`、`[data-ic-stream]` 可见。
- 点击「确认这一条」后 **t+11ms**：面板因既有「板面操作自动收起」变为 `data-ic-expanded=false`（虚拟列表高度 0，记录 DOM 不渲染，这是**既有展示行为**，不是本轮改动）；**t+503ms** 用真实点击「展开对话」重新展开（非切页、非刷新）。
- **t+514ms**：执行记录自动出现，文案「意图执行 / 整理材料A3… / 正在处理 / 查看详情」（非终态）；**t+4773ms**：同一条记录自动更新为「已完成」（DOM 直接更新，未刷新、未切页；URL 全程 `#/interactive`）。
- 展开详情：出现「结果入口 / 板面结果：1 张卡片」（上一轮结果定位修复未回退）；面板与整页不出现「【互动板面提交】」内部绑定文字；`/api/session/messages` 的 `intent_execution` = `status=completed`、`status_label=已完成`、`result_entry={kind:board,label:板面结果：1 张卡片,target:board_default}`；`consoleErrors=[]`。

### 入口 B：先进入普通对话页再进入 `#/interactive` — PASS

- `POST /submissions` = 200；t+8ms 面板同因既有自动收起变收起，t+441ms 真实重新展开。
- t+447ms 记录出现「正在处理」，t+4867ms 更新为「已完成」；详情含结果入口；无内部绑定文字；URL 全程 `#/interactive`；`consoleErrors=[]`。

### 装置说明（如实）

- 本构建里异步提交后的意图清单没有「不重载页面」的刷新入口，因此复验在**确认之前**做过一次真实页面 reload 让意图进入界面（属「直接进入或刷新互动页」的入口语义）；**确认之后**的记录观察严格不刷新、不切页。
- 面板被产品按既有「板面操作自动收起」行为收起后，用真实点击重新展开再观察（展开不是切页、不是刷新）；记录本身在收起期间已由会话层自动刷新，重新展开即见，无需手动刷新。
- 补充变体 A-reload（加卡后刷新页面再提交）在装置修正后未重跑；入口 A/B 两个固定验收已通过。
- 入口 A/B 的意图标题带随机后缀（验证脚本每轮重新造材料），与截图中的标题一致。

## 保留项与边界

- 保留上一轮已通过的结果入口（详情「结果入口 / 板面结果：N 张卡片」）与提交轮内部文字处理（普通消息区不出现「【互动板面提交】/【互动意图续作】」）。
- 未验证：真实厂商调用与网络行为、Windows 安装包/原生发布、CI 同版本环境（未触发 CI）、前后端全量测试。
- 未改后端，故无新增后端测试；未改版本号、未合并 main、未打标签、未打包、未发布、未改更新源。

## 证据

- 截图（随分支提交）：`docs/verification-shots-step5b/01-entryA-direct-interactive-expanded.png`（直接进入互动页并展开）、`02-entryA-intent-awaiting.png`、`03-entryA-record-appeared.png`（自动出现「正在处理」）、`04-entryA-terminal-detail-result-entry.png`（终态 + 结果入口）、`05-entryB-record-appeared.png`、`06-entryB-terminal-detail-result-entry.png`。
- 原始 JSON（可再生产物，未入库）：`%TEMP%\qio-r5\mi2\evidence\history-bootstrap-verify.json`（两入口的 panelTransitions 时间线、记录出现/终态毫秒、API 记录、consoleErrors）。

## 状态

- 修复与回归已完成，两个入口真实复验 PASS；分支已推送，远端完整 SHA 以推送回执为准。
- 未合并 main、未打标签、未改版本号（0.2.10）、未打包、未发布；提交带 `[skip ci]`。