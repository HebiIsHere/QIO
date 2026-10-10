# 互动模式收尾轮：统一机制契约（fix/interactive-final-closure）

> **历史文件（只适用于提交 `1da2172c6876c98f9b1076b4477dc75e7fe37638` 及其之前）。**
> 第八轮（`fix/interactive-closure-followup`，2026-10-10）的六条反例推翻并更新了本文的以下条文：
> M1「只在 `dirty=true` 时保护板面」→ 见 `docs/interactive-closure-followup-contract.md` §0 第 1 条与
> `docs/interactive-mode-contract.md` §13.1（已保存成功的新版本与未保存的新输入受同样保护；
> 旧回执成功时必须吸收版本事实而不替换候选）；取消影响确认的撤回语义见 §13.2；
> 影响确认的范围复核见 §13.4。**M2–M9 仍然有效**。

本轮把上一轮遗留五项（01–05）与本轮十五类（06–20）问题一次修复收尾。
本文件由主智能体维护；与 `docs/interactive-mode-contract.md` 冲突时，以互动契约为准，本文件只补充**可靠性机制**。

## 0. 分工与文件所有权（同一文件同时只有一个写者）

| 角色 | worktree（分支） | 负责项 | 主要文件所有权 |
| --- | --- | --- | --- |
| Lead 主智能体 | D:\qio-dev\qio-final（fix/interactive-final-closure） | 01（store 侧）、06、08（store/协议接线）、集成、最终验收、文档 | stores/interactive.ts、services/interactive.ts、interactive/types.ts、docs/** |
| A 卡片内容保护 | D:\qio-dev\qio-final-a（wt/final-a-card） | 01（BoardCard watcher 侧）、05、07（前端删除/完成流约定见 D）、09（前端）、12、13（drafts.ts 底层） | interactive/drafts.ts、BoardCard.vue、CardDraftHint.vue、A 自建辅助模块 |
| B 会话恢复 | D:\qio-dev\qio-final-b（wt/final-b-send） | 02、03、04、10、11、14、15、13（session 侧调用点） | stores/session.ts、Composer.vue、FailedSendNotice.vue、ChatDock.vue |
| C 后端准确性 | D:\qio-dev\qio-final-c（wt/final-c-backend） | 08（后端）、09（后端）、12（服务端草稿清除结果）、16、17、18 | backend/src/agent/interactive/**、backend/src/agent/api/interactive*.py |
| D 板面操作与审美 | D:\qio-dev\qio-final-d（wt/final-d-ui） | 19、20、第六章审美要求、A 的 BoardCanvas 删除流约定实施 | BoardCanvas.vue、BoardGroupFrame.vue、IntentStatusPopover.vue、IntentPreviewCard.vue、ImpactConfirmDialog.vue、overlayLayout.ts、interactive-shell.css |
| E 独立验收 | D:\qio-dev\qio-final-e（wt/final-e-accept） | 全部 20 项反例矩阵与分层验收，不改产品代码 | tests/evidence/**、E 自命名验收脚本 |

- 子智能体分支从 b67d1fe（= 远端 fix/interactive-content-protection-polish 最新）切出；Lead 在 fix/interactive-final-closure 上集成合并。
- 全部沟通、提交说明、文档用中文；提交前缀建议 `[final-A]` 等。
- 禁止合并 main、fix/unified-process-audit、feat/unified-process-attachments-streaming。

## 1. 统一机制（必须按此实现，不要为单个反例加特例）

### M1 板面/草稿版本保护（Lead 实现 store 侧；A/B/C 配合）

- 服务端 `seq` 是板面状态权威版本：PUT 成功返回新 `seq`；GET 返回当前 `seq`。
- 本地维护单调递增 `localRev`（每次产生新候选状态 +1）。PUT 请求携带本地候选的 `localRev`；服务端响应回带 `acceptedLocalRev` 与 `seq`（services 层透传）。
- **应用规则**：任何 GET/PUT 响应落地前，必须与当前内存候选比较——
  - 响应携带的 `localRev`（GET 开始时刻记录的）**小于**本地当前候选 `localRev`，且本地候选包含 dirty 内容 → 不得整体覆盖：合并策略 = 服务器新结果与本地后续编辑共存（服务器结果进入"已保存事实"，本地 dirty 候选保留），且不得清 dirty。
  - 本地已保存成功的候选（`acceptedLocalRev == 响应 localRev` 的新版本）受同样保护。
  - 已被更旧响应替换过的路径必须改为：响应按发起时刻的版本快照记录，落地时只更新"它读到的部分"，新输入版本永不被旧响应回退。
- 草稿同理：草稿本地 `draftRev` 单调递增，PUT 成功记录 `savedDraftRev`；GET 响应带其开始时刻的 `draftRev`。应用条件：`响应.draftRev >= 当前本地 draftRev` 且 `本地已保存成功的版本不被更旧响应替换`。**识别"GET 所读版本与同期写入"：GET 发起时记录读取基准 draftRev，响应落地时与当前比较，而不是比较时间戳或"是否在飞"。**
- 恢复/重开时（M5 关闭重开）：以"服务器已保存版本 + 本机恢复记录"两者中更新者为候选，无法判定新旧时**保留两份候选并提示**，不自动覆盖或删除。

### M2 卡片草稿本机副本与清除事实（A）

- 本机记录结构：`{ key, version, text, savedAt, cleared? }`；`version` 是实际**成功写入本机存储**的版本号（写失败时不推进）。
- 清除流程分两步事实：`planCleared`（意图）与 `committedCleared`（本机存储真实写入结果）。服务器清除成功且本机 cleared 写入失败时，重试必须**先补写本机 cleared**（重建清除保护）再重发网络清除；两者都失败时重试入口同时重建两者。
- `removeItem` 底层必须返回真实结果（成功/失败 + 错误对象），调用方全部更新；失败不得静默报成功/完成。
- 已确认清除的事实优先于旧副本：旧版本候选不得再次取得"恢复并上传"权限；但不得为清除旧稿误删后来输入的更新版本。

### M3 发送归属与重发关联（B）

- 发送尝试对象：`{ attemptId, topicIdAtSend, contentVersion, source, resendOf? }`。
  - 发送时话题为 null：记录 `topicIdAtSend=null`；话题绑定后把该 attempt **迁移**到实际绑定话题（发送归属与输入框内容解耦——输入框后来装了什么文字不影响这次发送在绑定后稳定归属绑定话题）。不把所有历史未绑定失败无条件搬到任意新话题。
- 失败原文记录：`{ recordId, topicId, text, contentVersion, attemptId, restored? }`。
- **显式重发关联**：找回原文到输入框时设置 `pendingResend = { recordId, topicId, contentVersion }`；
  - 输入框文字被用户编辑 → 升级为新 contentVersion 的"编辑后发送"，仍保留与 recordId 的关联但版本更新（成功只清该记录，不误伤同话题其他记录）；
  - 切话题 / 互换（swap）后输入框内容改变 → 关联随内容版本一起更新，旧成功只能处理它实际接受的 (recordId, contentVersion, topicId)；
  - 手动输入相同文字 ≠ 重发：无 pendingResend 关联的发送成功不清任何失败记录；
  - 恢复/重开后关联持久化，输入框仍有原文时保持可用入口（找回/互换或等价操作），重发成功后清除对应记录。
- 成功清理仅作用于：实际被接受的发送尝试、其 contentVersion、其话题、其失败记录。

### M4 影响确认协议（C 定义后端，A/D 前端触发，Lead 接线）—— 2026-10-09 后端定稿

- `POST /api/interactive/boards/{id}/impact-check` 请求 `{ stateVersion, changeSet: { state } }` → 响应
  `{ ok, checkId, stateVersion, affected: [{intentId,title,status,materials[],consequence}], affectedTasks（同义别名）, summary, impactConfirmationRequired }`；
  无法预判或版本过期 → HTTP 200 `{ ok: false, reason, currentSeq? }`（不改任何状态）。
- 保存携带确认：`PUT /state` body 增加 `confirm: { checkId, stateVersion? }`；服务端校验 checkId 归属、绑定版本 == 当前已保存 seq、
  候选语义签名 == check 绑定签名；不符 → HTTP 409 `{ error: "stale_check", reason }`，不落库。
- 服务端门：`PUT /state` 不带 confirm 但这次保存会改变 running 任务依赖材料语义 → HTTP 409
  `{ error: "impact_confirmation_required", affectedTasks: [...] }`，不落库。
- 提交：`POST /submissions` 可选 body `baseStateVersion` 与 `confirmedCheckId`；不一致 → HTTP 409
  `{ error: "stale_state", reason }`，不落库、不更新基准、不产生提交记录。
- 前端已接线（Lead）：`services/interactive.checkMaterialImpact`、`saveBoardState(..., confirm)`、
  `submitBoard(..., baseStateVersion, confirmedCheckId)`；store 对 409 分类显示真实原因并保留候选/勾选。
- check 记录在服务端进程内存（重启后确认如实失效并要求重新预判），上限 64 条 FIFO。
  - 预判失败：返回真实错误（`{ok:false, reason}`），前端不得继续写入受影响材料，保留改动与重试入口。
  - `checkId` 绑定 (stateVersion, 变更范围, 受影响任务)。此后任何板面保存使 `seq` > stateVersion → `checkId` 失效，确认时服务端校验并拒绝（`stale`），前端重新预判。
- `confirm` 时服务端再次校验：`当前已保存 seq == stateVersion` 且 `checkId` 有效。校验失败不落库（不"前端出错后服务端无条件生效"）。
- 提交约束：存在未保存 dirty 或等待影响确认时不得发出提交（前端拦截 + 服务端校验）。提交请求携带 `{ baseStateVersion, confirmedCheckId? }`；服务端校验 after 候选版本与确认版本一致，不一致返回 `stale_state`，不落库。
- 用户取消影响确认：该次板面改动不生效、任务继续；草稿/候选全部保留（见 07）。
- 普通不影响任务的保存不增加确认步骤。

### M5 提交去重与预览落地（C）

- 去重键以**身份结构**为准：卡片 id、链接端点 (src,dst) 的对象身份、组成员 id 集合与顺序；正文不替换端点身份。同正文不同身份 ≠ duplicate；同身份同结构同正文 = duplicate。真正撤回再加回等规则维持现状。
- 预览落地：批准的成组/成员调整必须通过用户可做操作（移入/合并/成员调整）实现；`normalize_state` 不得静默裁掉批准成员后仍报告完成；无法实现时生效前明确拒绝并保留可处理状态。
- 审批时服务端校验材料：意图创建时记录相关材料指纹（cardId + content hash + 检查时的 seq）；`approve` 时重新计算，材料变化 → 拒绝批准（needs_update，附原因）。仅位置/大小变化不算失效。

### M6 长草稿（C 后端 + A 前端）

- 后端：草稿正文超过上限（沿用现有常量，若 20000 正常、仅超限截短则改为）**明确拒绝**：HTTP 422/400 + `{error:"draft_too_long", limit}`，不截短、不返回成功。落库正文完整。
- 前端：保存失败显示准确原因，保留完整本机副本；重开后正文完整。20000/20001/20008 字为验收点。

### M7 错误可见性（B + A）

- Composer（普通对话页）显示共享聊天草稿保存失败状态与重试入口（与 ChatDock 同一状态源，不重复发送/订阅）。
- FailedSendNotice：区分「无失败记录」与「读取失败」；读取失败必须显示真实恢复错误与重试入口，不虚报"已丢失/已恢复"。零条恢复出来也显示错误。
- 保存失败不显示已保存；删除失败不显示完成；未接入能力不显示已执行。

### M8 板面几何（D）

- overlayLayout：预览四方向（右/下/左/上）进入实际可用区域；完全离屏的预览不作为可见锚点；任务操作浮条始终留在视口内（优先视口约束，其次浮层互避让）。
- "在板面上定位"发送真实板面平移通知（非仅局部聚焦）。平移/缩放后仍成立。窄窗口 + 底部工具栏避让。
- 空格框选：失焦 / 页面隐藏 / 取消手势 / 卸载时重置临时按键与拖动状态；输入框、中文输入法、控件激活键、Escape 不受影响。

### M9 数据结构与兼容

- 保持现有数据结构；只允许**新增可选协议字段**（`localRev`、`draftRev`、`checkId`、`contentVersion`、`attemptId`、`pendingResend`、材料指纹等），旧格式恢复兼容：旧记录缺新字段时按"无法判定来源或新旧 → 保留候选并说明"处理。
- 本轮不做数据库迁移；如确需迁移，先向 Lead 说明理由与兼容办法，禁止修改历史迁移。

## 2. 验收矩阵（E 建立，编号 01–20）

每项：触发 → 应有结果 → 基线实际结果 → 证据类型 → 责任人。基线报告中的"断言异常确实发生"探针必须改写为正确行为期望。修复后逐项回归。

## 3. 证据分层

1. 单元/状态（vitest / pytest，注入失败与乱序响应）
2. 真实组件/DOM（@vue/test-utils + jsdom）
3. API/数据库（真实临时数据库 + 实际路由）
4. 真浏览器（scripts/visual_probe.mjs CDP + e2e_up/e2e_fake_provider；故障注入必须标注模拟）
5. 关闭重开（结束 Chrome 进程，同一用户目录重开新进程）

## 4. 收尾条件

二十项逐项有证据；关键跨流程真浏览器与关闭重开验收完成；前端四尺寸×两主题截图；全量检查（vue-tsc、vitest、pytest、check_docs.py）实际运行结果；CI 链接。不把"最后一轮"当降低验证的理由。
