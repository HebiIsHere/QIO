# 最终收口验收矩阵（E 独立验收，[final-E]）

- 基线：b67d1fe（worktree D:\qio-dev\qio-final-e，分支 wt/final-e-accept）
- 依据：D:\qio-dev\_final-closure-task.md（20 项反例原文）、docs/interactive-final-closure-contract.md（机制契约 M1–M9）、docs/interactive-mode-contract.md
- 口径：矩阵每项写**正确行为期望**；「基线实际结果」用代码定位（文件:行号）与最小探针实证，探针允许断言"异常确实发生"，但不能替代正确行为期望。
- 证据分层：①单元/状态（vitest/pytest，注入失败与乱序）②真实组件/DOM（@vue/test-utils+jsdom）③API/数据库（真实临时库+实际路由）④真浏览器（scripts/visual_probe.mjs CDP + e2e_up + e2e_fake_provider，故障注入标注模拟）⑤关闭重开（结束 Chrome 进程后同用户目录重开新进程，不用同一 store 重读代替）。
- E 只新增独立命名文件（前缀 final-e- 或目录 scripts/final-e-verify/、tests/evidence/final-e-*/），不修改产品代码。

## 0. 探针索引

| 探针 | 覆盖项 | 分层 | 状态 |
| --- | --- | --- | --- |
| scripts/final-e-verify/final-e-baseline-backend-probe.py | 09/16/17/18 | ③API/数据库 | 已运行（2026-10-09，真实临时库，无 mock；结果见附录 A） |
| scripts/final-e-verify/final-e-frontend-race-probe.test.ts（阶段 3） | 01/02/03/04/05/06/07/10/11/12/13/14 | ①单元/状态 | 待建 |
| scripts/final-e-verify/final-e-component-probe（阶段 3） | 05/07/14/15 | ②组件/DOM | 待建 |
| scripts/final-e-verify/final-e-journeys.steps-*.json + visual_probe（阶段 3） | 关键旅程全部 | ④真浏览器 | 待建（QIO_PROBE_PORT / QIO_PROBE_PROFILE 独立） |
| scripts/final-e-verify/final-e-reopen-probe（阶段 3） | 01/09/12/13/14 | ⑤关闭重开 | 待建（结束 Chrome 进程后同用户目录重开） |

---

## 1. 验收矩阵（01–20）

### 01 迟到读取替换刚保存的新卡片草稿

- **触发（真实入口）**：卡片编辑器（BoardCard.vue 编辑框 → onDraftInput → stores/interactive.ts setDraft）输入新稿后，refreshBoardFromServer 的 GET（stores/interactive.ts:395）与草稿 PUT（flushDrafts，:760）同时在飞；PUT 先成功（draftSavedKeySeq=keySeq，:838-846 并清理本机副本 removeCardLocalDraftIfUnchanged），旧 GET 后返回。
- **应有结果**：GET 响应按其**开始时刻读取的版本**（draftRev/localRev 协议，M1）判断：响应版本落后于本地当前候选（含已保存成功的新版本）时不得整体覆盖；服务器新结果进"已保存事实"、本地候选保留、不清 dirty/未保存状态；真实编辑框（BoardCard watcher，BoardCard.vue:205-210）不退回旧文字。
- **基线实际结果**：存在缺陷。GET 落地条件是 memoryIsNewer（interactive.ts:307-311）＝`seq > before[key] || seq > saved`。时序＝输入(seq=N)→GET 开始(before 记 N)→PUT 成功(saved=N)→GET 返回旧正文：两项都为 false → mergedDrafts 用服务器旧值整体替换 drafts（:411），经 watcher 替换实际 textarea。缺 M1 的 draftRev/读取基准协议。
- **证据类型**：①单元/状态（乱序注入）＋②组件/DOM（textarea 不回退）＋⑤关闭重开。
- **责任人**：Lead（store 侧 M1）；A（BoardCard watcher 配合）。

### 02 重发期间互换，旧成功误删后来保留的文字

- **触发**：失败原文「找回原文」（session.ts retryFailedSend:807-838，armResendOf 置显式关联）→ 重发在飞 → 输入新文字 → 「与当前文字互换」（swapFailedSendText:846-877，记录正文换成新文字）→ 旧重发回执成功。
- **应有结果**：身份≠内容版本；互换后记录、正文版本、发送关系一并更新；旧成功只处理它实际接受的 (recordId, contentVersion, topicId)；不按正文相同补救匹配。
- **基线实际结果**：存在缺陷。_clearFailedSendsAccepted（session.ts:978-1006）只按 draftId 身份匹配、明确不校验内容版本（:984-995 注释自述"版本号不参与"）→ 互换后的记录（正文已是新文字）被旧重发成功一并清除，新文字的恢复入口丢失。新文字草稿本身暂有版本守卫（settleSend:2983 stored.seq>targetSeq 跳过删除），阶段 3 需探针确认完整损失通道。
- **证据类型**：①单元/状态＋②组件/DOM（FailedSendNotice 条目仍在）＋⑤关闭重开。
- **责任人**：B。

### 03 重发关联跨话题，成功误清别的话题失败记录

- **触发**：A 话题找回失败原文（armResendOf 置位，session.ts:2859-2862）→ 切到 B（bind，:2784-2849，不解除 armedResend）→ 在 B 手动输入与原文相同的文字并发送成功。
- **应有结果**：显式重发关联必须约束话题；话题切换不得让旧关联套到 B 的发送；B 的发送（无显式关联）成功不清 A 的记录。
- **基线实际结果**：存在缺陷。captureAttribution（:2865-2881）复用 armedResendId 只比较文字（normalizeSendText 相同即命中），bind 切话题不清 armedResendId/armedResendText → B 的手动发送沿用 A 记录的 draftId → 成功后 _clearFailedSendsAccepted 清掉 A 的失败记录。
- **证据类型**：①单元/状态＋②组件/DOM（切回 A 后记录仍在）＋④真浏览器（关键旅程）。
- **责任人**：B。

### 04 绑定话题前输入新稿，原发送失败仍留在未绑定位置

- **触发**：话题为 null 时点击发送（Composer/ChatDock → session.send:2315-2405，attribution.topicId=null）→ 话题未定前输入新稿并完成本机保存 → 绑定 A（keeper.bind→adoptUnboundFailedSends:3092-3096）→ 切 B → 原发送失败。
- **应有结果**：发送归属与输入框保护独立；绑定后该发送稳定归 A（迁移到 A，失败也在 A 找得到）；不把历史未绑定失败无条件搬到任意新话题。
- **基线实际结果**：存在缺陷。bind 只迁移「文字与 carry 相同」的在飞发送（movedSends 按 normalizeSendText 匹配，session.ts:2802-2829）；用户输入了新稿（文字不同）时原发送的登记项 key 停留占位键、topicId 停留 null；adoptUnboundFailedSends 在绑定 A 时已消费过（此后不再触发）→ 失败记录 topicId=null，在 A 找不到。
- **证据类型**：①单元/状态＋②组件/DOM（A 话题下 FailedSendNotice 无该条）＋④真浏览器。
- **责任人**：B。

### 05 改一个字自动解除未决草稿冲突

- **触发**：无法判定新旧的草稿冲突（restoreLocalCardDrafts 登记，interactive.ts:373-384）→ 冲突入口（CardDraftHint「用本机的/用服务器上的」）未点 → 用户在冲突 A 的编辑器改一个字（BoardCard onDraftInput → setDraft）。
- **应有结果**：改字不清另一份候选、不按本机版本覆盖服务器；保留两份来源与可操作入口（明确选择按钮或新的待编辑候选）；分别验证选本机、选服务器、继续编辑、取消。
- **基线实际结果**：存在缺陷。setDraft 仅在「文字与冲突本机候选完全相同」时保持冲突（interactive.ts:658-664）；任何改动走 :673 setDraftConflict(key,null) 无条件清冲突，并把新文字当正常候选排保存（:684-687 只更新守卫版本，冲突已不在）→ "改一个字自动接管"成立。
- **证据类型**：①单元/状态＋②组件/DOM（CardDraftHint 仍在、服务器候选仍可选）。
- **责任人**：A（drafts.ts/BoardCard）；Lead 配合 store 状态。

### 06 正式板面被旧状态覆盖

- **触发 A**：第一版自动保存（commit→scheduleSave→saveNow，interactive.ts:209-284）未返回时用户完成第二版编辑（commit 更新 board.value、dirty=true）→ 旧保存响应返回。
- **触发 B**：已有未保存正式编辑时执行审批（approve/reject/decideBatch→settleAfterIntentChange:985-990）→ 审批收尾保存失败 → refreshBoardFromServer 回读服务器。
- **应有结果**：板面状态有版本保护（M1：localRev/seq 协议）；保存等待结束≠当前版本已保存；在飞期间的新编辑不被旧响应覆盖、不清 dirty；保存失败后不得用服务器旧板面覆盖未保存编辑；审批成功不丢未保存编辑；有可靠的后续保存与明确失败处理。
- **基线实际结果**：存在缺陷。A：saveNow 落地无条件 `board.value = result.state; dirty.value = false`（interactive.ts:261-266），且 saveInFlight 存在时新调用只等待不重排（:228-231）→ 第二版被旧响应整块替换并清 dirty。B：settleAfterIntentChange 在 saveNow 失败（saveStatus=error）后仍执行 refreshBoardFromServer（:986-987），它无条件 `board.value = payload.state; dirty=false`（:399-422）→ 未保存编辑被服务器旧板面覆盖。
- **证据类型**：①单元/状态（乱序/失败注入）＋③API/数据库＋④真浏览器（关键旅程）。
- **责任人**：Lead（store/协议）；C（后端 seq 回带配合）。

### 07 取消影响确认后，草稿已经丢失

- **触发**：运行任务依赖的卡片 → 真实删除按钮或「完成编辑」（BoardCard confirmEdit:213-229；删除流经 SelectionMenu/BoardCanvas）→ saveNow 影响确认出现（pendingImpact，interactive.ts:239-253）→ 期间草稿删除（clearDraft:633-654，写 cleared+登记 pendingRemovals）已独立成功 → 用户点取消（cancelImpact:1052-1057）。
- **应有结果**：未确认、保存失败和取消期间保留候选（含被删草稿对应的恢复候选）；正式变更成功后才清对应版本；不得通过禁用正常编辑/删除绕开。
- **基线实际结果**：存在缺陷。confirmEdit 无条件 clearDraft（BoardCard.vue:227）——正文从 drafts 删除、写 cleared 记录、登记待同步清除；cancelImpact 只回读服务器板面、不恢复任何候选 → 取消后旧板面回来，完成编辑/删除前的新输入失去恢复来源（cleared 依据还会把服务器那份旧稿清掉）。
- **证据类型**：①单元/状态＋②组件/DOM＋④真浏览器。
- **责任人**：A（confirmEdit/clearDraft 时序）；D（删除流约定实施）；Lead（确认协议接线）。

### 08 影响确认没有约束完整保存/提交链

- **触发**（三路径分别验收）：
  - (a) saveNow 的 previewMaterialImpact 请求失败（api interactive_intents.py:81-92 material-impact）；
  - (b) 预判 A 在飞期间又改 B → 确认；
  - (c) 等待影响确认（pendingImpact 非空）或版本未保存时点击提交（SubmitCluster → store.submit:955-983）。
- **应有结果**：(a) 预判失败不继续写入受影响材料，显示真实原因，保留改动与重试入口；(b) checkId 绑定 (stateVersion, 范围, 任务)，确认时服务端校验 seq==stateVersion，版本改变→stale→重新预判，未说明的 B 不能放行；(c) 存在未保存 dirty 或等待确认时不得发出提交（前端拦截+服务端校验，请求带 baseStateVersion/confirmedCheckId，不一致返回 stale_state 不落库）；等待期间新修改留在板面不混入本次授权；取消后不自动继续提交。普通不影响任务的保存不加确认步骤。
- **基线实际结果**：存在缺陷。(a) interactive.ts:254-256 catch 后**继续保存**，原因不可见；(b) 无 checkId/stateVersion，confirmImpact 仅置布尔（:1046-1050），确认不重新核实；前端有 impact 请求但后端 confirm 校验不存在；(c) submit() 的 saveNow 在 pendingImpact 分支早退（saveStatus=idle，:250-252），submit 只拦 saveStatus==="error"（:960-965）→ 继续调用 api.submitBoard 提交**服务器旧板面**；后端提交路由无版本校验。
- **证据类型**：①单元/状态＋③API/数据库＋④真浏览器（等待确认时点提交旅程）。
- **责任人**：C（后端协议）；Lead（store/提交接线）；A/D（前端触发）。

### 09 长草稿静默截短

- **触发**：卡片草稿输入 20,001 / 20,008 字（结尾唯一标识）→ 防抖 flushDrafts → PUT /api/interactive/drafts/{board_id}（board_store.save_draft:203-224）。
- **应有结果**：优先完整保存；必须设上限时前后端明确拒绝（如 422 {error:"draft_too_long", limit}），不截短、不返回成功；失败显示准确原因、保留完整本机副本；重开后正文完整。验收点：20,000（应成功且完整）、20,001、20,008。
- **基线实际结果**：已实证（探针附录 A）——save_draft 用 _clip 把超过 20,000 字的值静默截短到 20,000 并正常返回成功（board_store.py:213-215 DRAFT_MAX_CHARS），结尾唯一标识丢失、无任何 error；前端 flushDrafts 成功路径随后无条件清理本机完整副本（interactive.ts:840-846）→ 重开只剩截短稿。
- **证据类型**：③API/数据库（已实证）＋②组件/DOM（界面状态）＋⑤关闭重开。
- **责任人**：C（后端拒绝）；A（前端保护与提示）。

### 10 聊天保存失败，切话题后新稿丢失

- **触发**：话题 A 有旧稿 → 新输入的本机写入失败（commit→writeDraft error，session.ts:2673-2723）→ 切 B（bind）→ 切回 A。
- **应有结果**：按话题保留尚未持久化成功的内存候选与失败状态；话题导航不主动丢弃当前输入；恢复写入能力后可只重试保存（不发送）；切回 A 时新稿不丢失、不与旧稿混淆。
- **基线实际结果**：存在缺陷。失败的文字只存在 host.draft 与失败状态里；bind（session.ts:2784-2849）只读本机存储（readDraft(nextKey)）装载新话题草稿 → host.draft 被覆盖，未持久化成功的内存候选没有任何按话题的暂存；切回 A 后 bind 再读 A 的旧存储稿 → 新稿被旧稿替换。
- **证据类型**：①单元/状态（注入存储写失败）＋②组件/DOM。
- **责任人**：B。

### 11 首次话题迁移覆盖或删除已有稿

- **触发**（三子项分别验收）：话题 null 时输入新稿 → 绑定 A（keeper.bind:2784-2849）：
  - (a) A 既有草稿同时存在；
  - (b) 迁移写入 A 失败；
  - (c) A 原有与本次发送无关的恢复记录（失败原文）。
- **应有结果**：(a) 两份都保留、明确当前编辑哪份，不拼接、不自动发送、不静默覆盖；(b) 迁移失败时原未绑定副本继续存在，目标保存成功后才按版本清理源；(c) 迁移不吸收与本次发送无关的未绑定失败。与第 04 项统一迁移规则但分别验收。
- **基线实际结果**：存在缺陷。(a) bind 的 carry（未绑定新输入）直接覆盖目标键：text=carry 优先于 stored（:2814-2816），A 既有草稿被静默顶掉；(b) 写入失败仅 setStatus("error")（:2837-2843），随后 `if (previousUnbound) removeDraft(previousKey)`（:2845）无条件删除未绑定副本 → 双输；(c) adoptUnboundFailedSends（:1012-1019）把**全部** topicId=null 的失败记录无条件搬到新话题。
- **证据类型**：①单元/状态＋②组件/DOM＋⑤关闭重开（a/b 的持久化后果）。
- **责任人**：B。

### 12 卡片清除依据失效，已清除旧稿复活

- **触发**（两条反例分别验收）：
  - (i) 本机 v1 存在 → 新稿与 cleared 的本机写入失败（磁盘仍 v1）→ 服务器清除成功 → 重开；
  - (ii) 本机清除与网络清除都失败 → 恢复本机存储后点重试 → 网络在飞期间重开。
- **应有结果**：区分计划版本与真实存下的版本（M2 planCleared/committedCleared）；检查清理结果，失败不得标完成；重试先重建本机 cleared 保护再重发网络；保留已确认清除的事实，旧副本不得重新取得恢复与上传权限；不为清除旧稿误删后来输入。
- **基线实际结果**：存在缺陷。(i) clearDraft 登记的 version 取自**写失败的** cleared 记录（interactive.ts:645-647，失败也返回 version）；成功回执 removeCardLocalDraft(entry.cardId, entry.version)（:866）与磁盘 v1 版本不符返回 false，**返回值被无视**且状态置 idle（:867）→ v1 存活，重开恢复并重新上传。(ii) retryDraftSave（:903-926）只重排 pendingRemovals 与网络请求，本机 cleared 写失败（draftLocalState 不 ok 且 drafts 里已无该键）不会被补写 → 网络成功后 removeCardLocalDraft(version 失实) 仍删不掉 → 在飞期间重开旧稿复活。
- **证据类型**：①单元/状态（注入本机写失败）＋③API/数据库（服务端清除）＋⑤关闭重开。
- **责任人**：A（drafts.ts/interactive.ts 清除事实）；C（服务端清除结果回传）。

### 13 本机删除失败静默报成完成

- **触发**：真实「用服务器上的」选择（resolveDraftConflict:700-730 removeCardLocalDraft）或聊天清空草稿（keeper.commit:2708-2713 removeDraft）；removeItem/removeDraft 底层失败（存储被禁/配额）。
- **应有结果**：底层删除返回真实结果（成功/失败+错误对象），全部调用方更新；失败准确提示、保留待处理决定与重试入口；明确选择不被旧副本撤销、不误伤其他对象/版本；不只改一个组件。
- **基线实际结果**：已代码实证——removeDraft 静默 catch（drafts.ts:383-391 无返回值）；DraftStorage.removeItem 签名 void（:140）；removeCardLocalDraft 版本不符返回 false，interactive.ts 的全部调用点（:344、:370、:724、:727、:844、:866）都不检查返回值；聊天 keeper commit 删除后直接 setStatus("idle")（session.ts:2708-2713）→ 删除失败显示正常且状态 idle。
- **证据类型**：①单元/状态（替换 localStorage 注入失败）＋②组件/DOM（提示与重试入口）。
- **责任人**：A（drafts.ts 底层）；B（session 侧调用点）。

### 14 重开后重发成功，失败记录仍留下

- **触发**：关闭重开 → 失败原文已恢复进输入框（keeper.restore:2754-2762 / failedSends 从存储恢复:3106-3118）→ FailedSendNotice 因 originalInInput 隐藏「找回/互换」按钮（FailedSendNotice.vue:147-172）→ 用户直接点发送并成功。
- **应有结果**：保持或重新建立可靠显式关联，提供可理解操作入口；区分「恢复原文后发送」与「手动输入相同文字发送」；重发成功清对应记录；覆盖编辑后发送、互换、取消关联、切话题、再次重开。
- **基线实际结果**：存在缺陷。重开后没有任何路径置 armedResend（armResendOf 只在用户点「找回」时调用）→ 直接重发 mint 新 draftId → 成功后 _clearFailedSendsAccepted 按身份匹配不到 → 失败记录残留，界面仍显示未发送。
- **证据类型**：①单元/状态＋②组件/DOM＋⑤关闭重开（必测）。
- **责任人**：B。

### 15 保存/恢复错误已发生，界面却不可见

- **触发**：普通对话页 Composer 聊天草稿保存失败；失败原文读取失败（readFailedSendStateFromStorage error）且零条恢复出来。
- **应有结果**：Composer 显示共享聊天草稿保存失败状态与重试入口（与 ChatDock 同一状态源，不重复发送/订阅）；FailedSendNotice 显示真实恢复错误与重试，区分「从来没有记录」与「读取失败」，不虚报已丢失/已恢复；错误提示不依赖「已经成功恢复出内容」。
- **基线实际结果**：已代码实证——Composer.vue 不渲染 draftSaveStatus/draftSaveError/retryDraftSave（grep 无 chat-draft-status/chat-draft-retry 钩子；ChatDock.vue:378-385 有）→ 对话页保存失败不可见；FailedSendNotice 的 failedSendPersistError 提示在 `v-if="records.length"` 块**内部**（FailedSendNotice.vue:111-112 与 :193-200），零条恢复出来时整个恢复块（含错误）不渲染。
- **证据类型**：②组件/DOM＋①单元/状态（注入存储失败）。
- **责任人**：B。

### 16 材料变了，旧待审批预览仍可批准

- **触发**：真实保存接口（PUT /state）写入材料改动，用户未再提交板面 → 点旧预览的批准（单项 approve_intent；批量 batch_decide；依赖等待后再确认；暂停后继续 confirmDependency）。
- **应有结果**：服务端审批时校验当前材料与预览依据（M5 指纹），失效则禁止批准（needs_update，附更新原因）；单项、批量、依赖等待后再确认、暂停后继续一致；仅位置移动不判失效；不借失效把未提交新内容交给 QIO。
- **基线实际结果**：已实证（探针附录 A）——材料经保存接口改动后 approve_intent 仍 ok→running（intents.py:596-730；needs_update 只由 on_new_submission（提交时）与 update_preview（预览手改）触发，:457-495）。
- **证据类型**：③API/数据库（已实证）＋④真浏览器（旅程）。
- **责任人**：C；Lead（提交联动）。

### 17 真实关系改变被判为重复提交

- **触发**：A、B 内容相同身份不同，C 为另一材料；提交 A→C 成功后改为 B→C 再提交（submission.py diff/_decide_status:593-603）。
- **应有结果**：去重按身份结构（卡片 id、链接端点对象身份、组成员与顺序），正文不替换端点身份；B→C 含新增/移除关系 → 正常 succeeded；同内容多卡片、不同连接、组成员/顺序变化、真正撤回再加回的既有重复规则保持。
- **基线实际结果**：已实证（探针附录 A）——content_fingerprint（submission.py:468-532）以 kind+content+meta 的 card_fp 充当端点与成员身份 → A→C 成功后提交 B→C 返回 duplicate（"与上次成功提交内容一致"）。
- **证据类型**：③API/数据库（已实证）＋④真浏览器。
- **责任人**：C。

### 18 演示成组结果与批准预览不一致

- **触发**：材料已在旧组 → 批准「combine」演示意图（预览=材料+摘要进新组）→ advance done（intents.py:_apply_preview:986-1101 → _save_state → normalize_state）。
- **应有结果**：预览落地通过用户可做操作（移入/合并/成员调整）实现；normalize_state 不静默裁掉批准成员后仍报完成；无法实现时生效前明确拒绝、保留可处理状态；验证旧组、目标组、默认名、顺序、关系、撤回保护一致，不破坏"一张卡只属于一个组"。
- **基线实际结果**：已实证（探针附录 A）——新组（默认组名）只剩摘要卡，两份材料留在旧组，任务仍 ok→done 且 applied.groupIds 报告了新组 → 落地与批准预览不一致且报完成。
- **证据类型**：③API/数据库（已实证）＋④真浏览器（旅程）。
- **责任人**：C。

### 19 预览定位与任务操作浮条离屏

- **触发**（三入口分别验收）：(a) 预览位于右/下视口外 → 批量列表/任务浮条点「在板面上定位」（approval.ts:883-891 dispatch qio:interactive:locate-preview → BoardCanvas onLocatePreview:1111-1130）；(b) 任务列表定位是否真实移动板面；(c) 完全离屏预览作为锚点、操作浮条是否出视口（IntentStatusPopover.vue:116-146 候选计算）。
- **应有结果**：(a) 四方向（右/下/左/上）都定位进实际可用区域（不只左/上）；(b) 真实通知并移动板面（非仅局部聚焦）；(c) 完全离屏预览不作为可见锚点，浮条留在视口内（优先视口约束，其次浮层互避）；覆盖四方向、部分可见、极远坐标、平移、缩放、窄窗口、底部工具栏避让；普通卡片定位作对照（locate:1064-1095 已有四向）；几何数值先确认有效（无 NaN 假反例）。
- **基线实际结果**：部分处理——onLocatePreview 只处理左/上（BoardCanvas.vue:1124-1125 的 dx/dy 仅在 screen.x<left+60 / screen.y<top+60 时计算），右/下离屏不移动；完全离屏锚点约束在 BoardCanvas/IntentStatusPopover 中未见（阶段 3 逐条核实其余子项）。
- **证据类型**：④真浏览器（四方向×平移缩放×窄窗口）＋②组件/DOM（几何计算）。
- **责任人**：D。

### 20 空格框选状态在失焦后残留

- **触发**：按住空格 → 切换窗口（window blur）→ 在别处松开 → 返回后按普通拖动空白处。
- **应有结果**：失焦、页面隐藏、取消手势、卸载时重置临时按键与拖动状态 → 返回后普通拖动恢复为平移；输入框/聊天/菜单空格输入、中文输入法选字、控件激活键、Escape 不被劫持。
- **基线实际结果**：存在缺陷。spaceDown 仅由 window keydown/keyup 维护（BoardCanvas.vue:1134-1179），window blur 监听只取消拖动（:629-636 onDragCancel），不重置 spaceDown → 失焦期间松开的空格残留，返回后普通拖动仍被当框选；输入目标/控件/Escape 豁免已实现（:1150-1158、:1136-1147），此部分应保持不回退。
- **证据类型**：④真浏览器（blur 场景）＋②组件/DOM（事件注入）。
- **责任人**：D。

---

## 2. 关键旅程 → 矩阵项映射（逐条列入，阶段 3 逐条走完）

| # | 关键旅程 | 覆盖矩阵项 | 证据类型 |
| --- | --- | --- | --- |
| J1 | 保存在飞继续输入（第二版不被旧响应覆盖、不清 dirty，保存随后可靠落库） | 01、06 | ①＋④ |
| J2 | 冲突未选择期间：开编辑器不消冲突；编辑别卡不影响本卡冲突；改一个字不清两份候选；随后明确选择两来源分别生效 | 05（契约 §12.1 全分支） | ①＋② |
| J3 | 本机写入失败 / 本机删除失败分别注入；服务端清除成功与失败两种结局；重试补写本机保护；关闭重开验证不复活 | 12、13 | ①＋③＋⑤ |
| J4 | 发送未绑定→绑定→切话题→失败→找回→互换→重发→关闭重开；全程同时存在多失败原文与新草稿，互不误清 | 02、03、04、10、11、14、15 | ①＋②＋④＋⑤ |
| J5 | 等待影响确认时点提交（不得发出）；预判失败（原因可见+重试）；预判在飞新增其他受影响改动（确认不放行未说明项） | 08 | ①＋③＋④ |
| J6 | 材料已变时单项审批与批量审批都拒绝（needs_update+原因）；位置移动不误判 | 16 | ③＋④ |
| J7 | 真实表达（B→C、组成员/顺序变化）不被 duplicate 吞掉；真重复仍拦截 | 17 | ③ |
| J8 | 演示成组落地=批准预览（旧组/目标组/默认组名/顺序/关系/撤回保护一致） | 18 | ③＋④ |
| J9 | 四方向离屏定位+平移缩放后浮条在视口+失焦后普通拖动=平移+输入法选字不发送不劫持 | 19、20（+§8.3 IME） | ④＋② |
| J10 | 两个聊天入口不重复发送/订阅/建轮次；聊天发送不调板面提交接口（网络层断言） | 15、契约 §8.3 | ④（网络/订阅计数） |
| J11 | 未勾选注释及其关系在 before/after/删除路径不可见（载荷与服务端投影断言） | 契约 §1.4（本轮不改其机制，作为防回归验收） | ③＋④ |

---

## 3. 阶段 3 执行计划（待 Lead 通知候选 SHA 后开始）

1. 前端环境：qio-final-e/frontend 现无 node_modules（仅 qio-final-d 有）；阶段 3 先在 E worktree 内独立安装（pnpm/npm 独立目录），不占用 D 的依赖目录。
2. 后端环境：backend/.venv 已由 uv 建立（uv run --frozen 可复现）。
3. 单元/状态探针：scripts/final-e-verify/final-e-frontend-race-probe.test.ts，按矩阵 01–14 的触发时序注入失败与乱序。
4. 真浏览器：复用 scripts/visual_probe.mjs，E 固定 QIO_PROBE_PORT / QIO_PROBE_PROFILE 独立值；e2e_up 起真实前后端，模型用 e2e_fake_provider（**证据中标注"假厂商"**）。
5. 关闭重开：结束 Chrome 进程后同一用户目录重开新进程；测试数据目录独立，不碰用户真实 app.db；无头实例用完即清理。
6. 所有探针的"断言异常发生"结果**必须**在矩阵中改写为正确行为期望后回归。

---

## 附录 A：基线后端探针输出（2026-10-09 实测，真实临时库，无 mock）

> 运行命令：`cd backend && uv run --frozen python ../scripts/final-e-verify/final-e-baseline-backend-probe.py`
> 数据目录：%TEMP%/qio-final-e-probe-*/（独立目录，不碰用户 app.db）

```text
=== 09 长草稿 ===
  20000 字（含标识共 20014）→ 请求长度 20014；返回带 error 键 False；落库长度 20000；唯一标识还在 False
  20001 字（含标识共 20015）→ 同上：落库 20000、无 error、标识丢失
  20008 字（含标识共 20022）→ 同上：落库 20000、无 error、标识丢失
=== 16 材料已变仍可批准 ===
  意图创建时状态 pending；材料经保存接口改动后：批准 ok=True，reason=approved，状态→running
=== 17 身份不同被判 duplicate ===
  第一次提交 succeeded；换成同内容不同身份的 B→C 后第二次提交：duplicate（error=None）
=== 18 演示成组落地 ===
  批准 ok=True；推进 ok=True；任务状态 done；applied.groupIds=[新组]
  材料最终所在组=旧组；板上：旧组[材料甲,材料丙]、默认组名[仅摘要卡]
```

结论：09（静默截短）、16（材料已变仍可批准）、17（身份不同判 duplicate）、18（落地≠预览仍报完成）四项在基线 b67d1fe 上**以真实数据库实证复现**；其余各项以代码定位为基线判断，阶段 3 分层探针逐项实证后回填。
