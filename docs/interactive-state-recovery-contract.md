# 互动板状态恢复收尾轮：共同规则契约（fix/interactive-state-recovery-completion）

本文件是本轮（第八轮）**唯一的新约定来源**，补充并更新历史契约里被本轮反例推翻的部分。
冲突时以本文件为准；历史契约中未被本文件推翻的部分继续有效。

- 真实基线：`b3245e5`（`fix/interactive-closure-followup` 远端与其一致、未推进）。
- 集成分支：`fix/interactive-state-recovery-completion`（Lead worktree `D:\qio-dev\qio-src-lead`）。
- 工作分支/worktree：A=`wt/src-a-drafts`(`qio-src-a`)、B=`wt/src-b-backend`(`qio-src-b`)、
  C=`wt/src-c-ui`(`qio-src-c`)、D=`wt/src-d-verify`(`qio-src-d`)。全部从 `b3245e5` 切出。
- 不合入 main，不合并 `fix/unified-process-audit` / `feat/unified-process-attachments-streaming`，不发布。
- 无数据库迁移；不改历史迁移；最小的本机草稿格式调整必须兼容已有数据。

## 1. 版本事实（唯一口径，F3/N1/N5 共用，禁止各写一套）

1. 服务端 `seq` 是**唯一**的「已保存版本」事实。候选（用户正在编辑的板面）与已保存版本
   必须**分开接收**：PUT 成功只吸收版本事实（`seq`/`updatedAt`/`boardCleanRev` 前进），
   绝不整块替换候选正文。
2. 前端唯一合并入口（Lead 在 `stores/interactive.ts` 实施，其他路径复用）：
   `adoptServerFacts(serverState)` —— 把服务器事实安全合并进当前候选：
   - 用户本页**改过的对象**以本页候选为准；
   - 服务器上**独立变化**（另一页面的移动/勾选、提交清理）合入候选；
   - 同一对象**同一内容字段**两边都改了 → 记为内容冲突，交用户决定，不静默覆盖。
3. 请求生命周期：一次保存 = 影响检查 → 写入 → 回执，**整段**都受「候选是否仍是当前候选」保护。
   任何 await 之后若 `boardLocalRev !== putRev`，这次保存**不得再发出旧候选**（N1）。
4. 服务端按真实版本事实保护普通整板写入（B）：
   `PUT /api/interactive/boards/{id}/state` 收到 `state.seq` 与当前已保存 `seq` 不一致
   （旧版或未知版本）→ `409`，body `{ "detail": { "error": "stale_state", "reason": <可读中文原因>,
   "currentSeq": <当前 seq> } }`，**不落库、不推进快照、不暂停任务**。带 `confirm` 的路径同样先校验版本。
   兼容路径（`confirm` 等）不得变成绕过版本保护的入口。
5. 前端收到 409 `stale_state`（以及确认过期）后的可恢复流程（F3）：
   读取最新服务器事实 → `adoptServerFacts` → 用**最新版本**重新做影响预判 → 重新展示范围交用户确认。
   不得仅把 seq 改大、不得整板覆盖另一页面的成果、不得无限旧版预判或无解释等待。

## 2. 决定结果形状（N2/N3/N4 共用）

`advanceIntent` / `submit` / `saveNow` 的返回必须能区分：
**成功 / 仍待确认 / 用户取消 / 检查失败 / 保存失败**。UI 只依据真实结果声明
「已保存」「任务已暂停」，不得把 Promise 正常返回当成操作成功。
`POST /api/interactive/intents/{id}/demo/advance` 增加可选字段
`{ "decisionIds": string[] }`：只处理这次明确展示给用户、且属于该意图 `pendingDecision` 的项；
未展示项不得被顺带处理。响应继续返回最新的 `intent`（含 `revert`），前端据此刷新。

## 3. 本机记录身份（F2/A + Lead 接线）

- 「删本机冗余副本」(`remove-local-copy`) 与「整份草稿清除」(`clear-draft`) 是不同目的，
  重试不改变目的（R4 保持）。
- **无版本（缺 `version`）或 `version === 0` 的旧格式记录**：登记决定时必须同时保存
  可验证的**原始身份依据**（内容指纹：`text` + 记录种类 + 记录时戳等稳定字段）。
  重试/落地删除前必须用同一依据复核；证明不了「当前仍是原记录」就**保留新稿**，不得按对象 id 删。
- 生产重试入口（`stores/interactive.ts` 的 `retryDraftSave` / `retryLocalRemoval`）必须使用该保护；
  底层 helper 正确不算完成。
- 接口（A 在 `frontend/src/interactive/drafts.ts` 提供，Lead 在 store 接线）：
  `localRecordFingerprint(record: DraftRecord | null): string | null`
  `removeCardLocalDraftIfUnchanged(cardId, expectVersion: number | null, expectFingerprint?: string | null): DraftRemoveResult`
  语义：`expectVersion` 为正数 → 只删该版本；`expectVersion` 为 0/null → 必须提供 `expectFingerprint`
  且与当前记录一致才删；指纹不匹配 → `reason: "version-guard"`（保留、不算失败、不重试）。

## 4. 未完成输入（N6/A）

同一张卡片的**完整未完成输入**（正文 + 适用附加字段 + 空值）必须作为一个整体保存与恢复，
沿用同一套对象/版本/冲突/清除/失败规则，不新增第二写者；兼容旧的「仅正文」记录。

## 5. N3/N4 界面与后端（C + B）

- N3：待决定撤回项的「继续」必须真的调用带 `decisionIds` 的撤回执行并取得真实结果；
  「取消」/Escape 只结束本次提示、保留板面、不执行撤回；被关闭的提示可按意图身份再次查看，
  不因刷新或任务列表更新无理由反复打断。多任务按 `intentId` 处理。
- N4：执行前重核待撤回对象**当前内容及影响**；说明之后新增的编辑、关系、组成员、工作依赖
  不得被旧决定顺带删除 —— 保留后续内容或重新说明并取得新的有效决定。

## 6. 文件归属（同一文件同一时间只有一个写者）

| 归属 | 文件 |
| --- | --- |
| Lead | `frontend/src/stores/interactive.ts`、`frontend/src/services/interactive.ts`、`frontend/src/interactive/types.ts`、最终文档 |
| A | `frontend/src/interactive/drafts.ts`、`frontend/src/components/interactive/BoardCard.vue`、`CardDraftHint.vue`、A 的辅助模块与开发测试 |
| B | `backend/src/agent/interactive/**`、`backend/src/agent/api/interactive*.py`、B 的后端测试 |
| C | `frontend/src/components/interactive/ImpactConfirmDialog.vue`、`BoardGroupFrame.vue`、必要 `BoardCanvas.vue` 接线、独立样式/采集脚本 |
| D | 独立命名的验收测试、脚本与证据；**不改产品代码** |

A/C/B 需要 store 或共享类型调整时，先在本文件/消息里约定接口，由 Lead 实施。

## 7. 证据分层

①状态/单元 ②真实组件 DOM ③ASGI/API + 真实临时数据库 ④真实前后端 HTTP ⑤真浏览器 ⑥实际进程关闭重开。
mock 故障/延迟/存储故障必须标注为模拟；所有服务与脚本必须使用独立临时 `QIO_DATA_DIR`。
