# 互动板状态恢复收尾轮报告（fix/interactive-state-recovery-completion，2026-10-10 第八轮）

本报告只写**真实发生的事**：每条结论都对应可复跑的命令或证据文件；没跑的场景写「未验证」。
共同规则与接口清单见 `docs/interactive-state-recovery-contract.md`；里程碑状态见 `docs/status.md` 的 P24。

## 0. 摘要

两轮核查仍未关闭的 11 项（F1–F3、N1–N6、V1）与 2 项证据缺口（E1/E2）在本轮按**具体触发路径与用户可见结果**逐条处理。
根因不是「状态管理没优化」，而是三类具体遗漏：

1. **候选与已保存版本没有分开接收**（F3/N1/N5）：有未保存候选时连服务器事实都拒绝接收，候选只能带着旧 `seq` 反复被拒；
   或者保存的请求生命周期没有覆盖「影响检查」这一段，被取代的检查仍把旧候选写出去。
2. **「撤回」没有同步生效**（F1/N3）：取消只收回版本记账、板面仍留着被取消的正文；
   待决定撤回的「继续/取消」接到只清提示的状态上。
3. **本机记录的删除只按对象身份**（F2/N6）：旧格式记录没有版本可校验，重试会误删另一页面写下的新稿；
   未完成输入只保存了正文。

修复落点与文件归属严格分开（同一文件同一时间只有一个写者，见契约 §6）。

---

## 1. 基线与交付

| 项 | 值 |
| --- | --- |
| 真实基线 SHA | `b3245e5`（`git fetch` 后核对远端 `fix/interactive-closure-followup` 与之完全一致、未推进） |
| 集成分支 | `fix/interactive-state-recovery-completion` |
| 工作分支 | A `wt/src-a-drafts`+`wt/src-a-integration`；B `wt/src-b-backend`；C `wt/src-c-ui`；D `wt/src-d-verify`+`wt/src-d-verify-after`（各独立 worktree，全部从 `b3245e5` 建立） |
| 交付 tip | `origin/fix/interactive-state-recovery-completion` 的 HEAD（本报告主体提交 `72c6d8e`，其后一次提交只回填本节的 CI run）；**代码/测试冻结候选 = `ccc651a`**（其后 `390e0b1` 只增加 D 的 after2 证据与截图） |
| 推送状态 | 已推送 `origin/fix/interactive-state-recovery-completion`（见 §5.4 的 CI run） |
| 工作树状态 | 干净（`git status --porcelain` 为空；门禁运行产物 `scripts/recovery-lead-verify/out/` 已 gitignore、不入库） |
| 是否合主线 / 发布 | **否**。不合 `main`，不合并 `fix/unified-process-audit` 与 `feat/unified-process-attachments-streaming`，不发布；无数据库迁移、未改历史迁移 |

---

## 2. 统一机制（本轮唯一新增口径）

- **版本事实**：服务器 `seq` 是唯一版本号。前端唯一合并入口是 `frontend/src/interactive/serverFacts.ts` 的
  `mergeServerInto(local, server, base)`（`base` = 上一次与服务器一致的快照 `cleanState`）：本地没改取服务器、
  服务器没改取本地、两边都改时布局/选择类字段以本地为准、**同一张卡片正文两边都改登记内容冲突交用户决定**。
  store 的 `adoptServerFacts` 吸收版本事实并保留候选。
- **请求生命周期（N1）**：一次保存 = 影响检查 → 写入 → 回执，全程带代次；任何 `await` 之后若候选已不是当前候选，
  这一次**不再发出旧候选**；服务端再按真实 `seq` 兜底（旧版本 409 `stale_state`，不落库/不推进快照/不暂停任务）。
- **可恢复流程（F3）**：收到 409 `stale_state` / `stale_check` / `impact_confirmation_required` 时，
  读取最新事实 → 合并进候选 → 用最新 `seq` 重新预判 → 重新展示完整范围交用户确认；读取失败也**不堵住用户**
  （照旧给出按当前候选的确认说明并如实标注）。同一次候选最多自动重来 3 次，然后停在明确失败上。
- **取消（F1）**：取消**同步**把板面退回到 `cleanState`（被取消的正文/删除立刻消失），回读只用于把撤回落到
  服务器事实上确认；回读失败给出真实原因与 `retryCancelRecovery()`，不假装撤回成功。
- **提交清理（N5）**：`checkedCleared` 按对象与用户选择版本应用；用户在等待期间自己重新勾选（含撤销/重做）的值
  不会被旧回执取消；不会自动再提交。
- **本机记录（F2/N6）**：删除必须带「用途 + 版本 + 内容身份指纹」；无版本记录只能用指纹证明身份，证明不了就保留新稿；
  **成功回执清理本机副本前还要证明「这次真上传给服务器的正文 === 该记录的正文」**；
  未完成输入（正文 + 适用附加字段 + 空值）作为整体保存与恢复。

---

## 3. F1–F3、N1–N6、V1 逐条结果

> 证据层级：①状态/单元 ②真实组件 DOM ③ASGI/API + 真实临时数据库 ④真实前后端 HTTP ⑤真浏览器 ⑥实际进程关闭重开。

### F1 — 取消恢复等待期间，独立操作仍携带被取消正文
- **原触发**：编辑运行任务依赖的材料 → 出现确认 → 取消 → 恢复 GET 还未返回时移动卡片 → GET 返回。
- **最终行为**：取消同步撤回被取消的正文/删除；等待期间的移动/勾选/新增基于已恢复的板面保留；随后真的保存时，
  落库正文是恢复后的正式正文；回读失败时 `cancelRecovery.active=true` + 真实 `reason` + `retryCancelRecovery()`，
  界面不许说「已撤回」。
- **证据**：① `frontend/src/stores/__tests__/recovery-lead-state-facts.test.ts`（慢 GET 期间移动、回读失败+重试）；
  D 的 ⑤⑥ 反例 `frontend/src/acceptance-state-recovery/f1-cancel-recovery-independent-changes.test.ts`（基线红）。
- **口径变更**：R1 的「回读失败」分支按 F1 收敛（见 §4 与契约 §8.5）。

### F2 — 旧格式副本删除重试可误删另一页面的新稿
- **原触发**：本机旧格式记录没有版本（或 version 0）→ 选择服务器稿 → 本机删除失败 → 另一页面写新稿 → 原页重试。
- **最终行为**：本机记录删除改用「版本 + 内容身份指纹」双守卫，且**生产重试入口**（`retryDraftSave`）与三处清理点
  （`resolveDraftConflict`、`restoreLocalCardDrafts`、`flushDrafts`）全部走该守卫；指纹不匹配 = `version-guard`
  （保留、不算失败、不重试）。
- **复核中发现并已修的第二个真实缺口（写入方向）**：重试末尾的草稿保存会把内存候选（用户选定的服务器正文）
  当该卡的载荷上传，成功回执再按「请求时读到的记录版本」清理本机副本 → 删掉另一页面刚写下的新稿（三种记录形态都会）。
  修法：请求时快照 `{version, 指纹, 正文}`，**只有这次真上传的正文 === 记录正文**才允许清理；另外重试补写本机副本时，
  能证明磁盘上是另一页面的更新版本就保留它。红→绿证据：`.evidence/lead-f2-prod-entry-redgreen.txt`。
- **证据**：① `recovery-a-f2-legacy-fingerprint.test.ts`（13 例）、`recovery-a-integration-f2-retry-entry.test.ts`（6 例：
  3 形态 × 2 场景，走 `retryDraftSave`，另一页面写新稿后重试仍保留）、`recovery-lead-state-facts.test.ts`（正版本 + 旧格式各一条）；
  ⑥ D 的 J1/J2 真实关闭重开旅程（含「删除失败→重试只删本机冗余副本、不写 cleared」）。

### F3 — 跨页面保存使确认过期，本页无法重新确认保存
- **原触发**：本页版本 3 有正文候选与确认 → 另一页面独立移动并保存到 4 → 本页确认被拒 → 本页读取最新事实并重试。
- **最终行为**：拒绝后先接收最新事实（本页候选正文保留、另一页面的独立移动被协调进来、`seq` 前进到 4），
  再用最新版本重新预判并重新展示完整范围；用户按新说明确认后才落库（PUT 带 `seq=4`）。正文两边都改时登记内容冲突
  并**阻断保存**，用户选择「用本页 / 用服务器版」后才继续；不把 `seq` 改大蒙过去。
- **证据**：① `recovery-lead-state-facts.test.ts`（跨页面重新确认 + 兜底 409 + 内容冲突两条选择）；
  D 的 `f3-cross-page-stale-reconfirm.test.ts`。

### N1 — 保存前检查乱序，会发出旧写入覆盖已保存新版
- **原触发**：第一版检查未结束时完成第二版；第二次检查先结束并保存第二版，第一次检查迟到后仍保存第一版。
- **最终行为**：前端保存生命周期带代次，被取代的检查不再发出旧候选（实测只发出一次 PUT，内容是第二版）；
  服务端对普通整板写入按真实 `seq` 保护（旧版本 409 `stale_state`；带 `confirm` 也必须先过版本门；
  已保存 `seq=0` 的新板允许第一次保存）；前端收到 409 后读最新事实、合并、用最新版本重试（有界 3 次）。
- **证据**：① `recovery-lead-state-facts.test.ts`（乱序只发一次 + 409 后重试带最新 seq）；
  ③ `backend/tests/test_src_b_n1_stale_state.py` + **真实 HTTP 探针 `backend/tests/src_b_n1n4_http_probe.py` 27/27 PASS**
  （迟到旧写入 409、库里仍是最新正文、快照不推进、confirm 不绕过）；D 的 `n1-superseded-check.test.ts`。
- **未验证**：多进程/并发写同一板面的严格串行化（当前是「读 `seq` + 比较」的兼容最小方案，非数据库级加锁）。

### N2 — 重新确认仍显示「已保存、任务已暂停」
- **原触发**：确认任务 A 的说明 → 保存因范围增加任务 B 被拒 → 补取新说明等待再次确认。
- **最终行为**：`confirmImpact()` 返回真实结果 `{outcome, reason?, paused?}`：
  `saved`（PUT 真的落到这次候选且 `dirty=false`，`paused` 取服务端 `materialImpact.paused`）/ `needs_confirm` /
  `check_failed` / `save_failed` / `superseded`；给出新的有效说明时清掉 `impactCheckError`，
  「等待期间又改别处」的重新核实信息挂到新说明的 `note`；读取最新事实失败时仍给出按当前候选的说明（不堵住用户）。
- **证据**：② C 的 `frontend/src/components/interactive/__tests__/closure-c-n2n3-recovery.test.ts`（37/37，含 N2/N3）+ 真实截图；
  D 的 `n2-reconfirm-result.test.ts`（真实组件 DOM）；① `final-lead-m1.test.ts`。

### N3 — 剩余撤回弹窗的继续/取消均无效
- **原触发**：完成演示任务得到两张结果卡片 → 把其中一张与自己的注释连接 → 让任务失败 → 安全部分撤回，其余进入 `pendingDecision`。
- **最终行为**：`continueRevertDecision(intentId, decisionIds)` 真的调用 `demo/advance` 的 `revert_rest`
  （只处理这次明确展示的项，经 N4 服务端重核），失败返回真实原因与重试入口；`dismissRevertDecision(intentId)`/Escape
  只结束本次提示、保留板面、不执行撤回；`reopenRevertDecision` 可再次查看；多任务按 `intentId` 处理。
- **证据**：② C 的 37/37 与真实点击/键盘；D 的 `n3-pending-revert-decision.test.ts`（3 例，真实 DOM）；
  ③ N4 的服务端 decision 摘要。

### N4 — 旧撤回决定删除等待期间的新编辑
- **原触发**：已有真实待决定撤回项 → 等待期间另一页面修改卡片并成功保存 → 原决定继续调用 `revert_rest`。
- **最终行为**：执行前重核待撤回对象的当前内容与影响；说明之后新增的正文/关系/组成员/其他工作依赖**不被顺带删除**，
  改为保留并重新说明（`reconfirmed`）；未在 `decisionIds` 里的项原样保留；对象已不存在只保留说明；
  重复请求如实返回 `nothing_pending`。
- **证据**：③ `backend/tests/test_src_b_n4_revert_decision.py`（10 例）+ 真实 HTTP 探针 N4.6–N4.11
  （库里保留新正文、说明刷新、再决定才真撤回）。

### N5 — 提交成功时独立移动会重新保存旧勾选
- **原触发**：注释已勾选，开始提交 → 等待返回时仅移动卡片 → 服务端成功并取消勾选 → 本页保护未保存移动，
  拒绝整板回读 → 后续保存仍携带旧 `checked=true`。
- **最终行为**：提交成功按 `checkedCleared` + 提交时刻的勾选代次把清理作用于本页候选（即使回读被候选保护跳过），
  后续保存写的是 `checked=false` + 用户的位置移动；用户等待期间自己重新勾选（含取消勾选后撤销回来）的值
  不被旧回执取消；撤销/重做与取消也更新勾选记账；不自动再提交。
- **证据**：① `recovery-lead-state-facts.test.ts`（3 条：等待期间移动、等待期间重新勾选、取消勾选后撤销回来）；
  D 的 `n5-submit-move-checked.test.ts`。

### N6 — 未完成的附加字段不恢复
- **原触发**：编辑卡片正文和附加字段，不完成编辑 → 刷新或正常关闭重开 → 再打开编辑。
- **最终行为**：本机记录升级为 `{text, meta}` 负载（`writeCardDraftInput`），空串也是有效值；
  `restoreCardDraftInput` 按卡片种类重建适用字段（url→网址+标题、file/image→文件名、code→语言）；
  旧「仅正文」记录回退正式值；store 的 `setCardDraftInput` 与 `setDraft` 共用同一套草稿规则（不新增第二写者）。
- **证据**：① `recovery-a-n6-draft-input.test.ts`（18 例，基线红 17 failed）；② `recovery-a-n6-board-card-restore.test.ts`（14 例，基线红 14）；
  ② `recovery-a-integration-n6-store-input.test.ts`（6 例，真实 `setCardDraftInput` 往返 + 新 Pinia 重开还原）；
  D 的 `n6-incomplete-input-fields.test.ts`（4 例）。**真实进程关闭重开**（⑥）由 D 的 J 系列覆盖（见 §6）。

### V1 — 组操作被卡片或固定提示挡住
- **最终行为**：组头部与局部操作层级调整（`BoardGroupFrame.vue`），真实点击/键盘可达；
  不靠整体抬高组框，不引入新的卡片/拖动/连接点遮挡。
- **证据（②⑤，C 的独立采集装置）**：`scripts/closure-c-verify/v1-group-actions.mjs` + `shots/before-v1`/`after-v1` +
  `evidence/closure-c-verify-summary.txt`：`summary={"total":98,"reachable":78,"covered":3,"panel":5,"offscreen":12}`；
  真实点击（1024×768，CDP `Input.dispatchMouseEvent`）：设为有序 false→true ✅、移出成员 `[cA,cB]`→`[cB]` ✅、解除组后组消失 ✅；
  剩余 `covered 3` 全在 480×600，是底部固定工具栏**主动打开**时的遮挡（不是新缺陷），已在证据里区分。

### E1 — 截图名称与实际状态不一致
- **修正方式**：截图前实际断言主题（`data-theme` + 真实背景亮度 + 主题持久化并在重新加载后再次断言）、测试卡片/关系数量、
  预定面板状态、目标失败及原因与重试入口；条件不满足即失败。
- **结果（基线 b3245e5）**：**32/32 格采集断言通过**（4 尺寸 × 亮暗 × 正常/双面板共存/保存失败/提交失败），失败 0；
  工具栏实测 1440=51px、1024/800=80px、480=104px（提交失败态 114/172px）。
  `480×600` 的 coexist 按产品的「切换条（只展开一个面板）」形态断言。保存/提交失败用页面内 `fetch` 桩制造（证据里标注为模拟），
  其余为真实后端。证据：`scripts/state-recovery-verify/evidence/`、`shots/baseline/`。
- **修后矩阵（D 在 `c8bde01` 上跑，改动不进入这四类场景路径）**：受影响场景 **24 格全绿**
  （4 尺寸 × 亮暗 × 双面板共存/保存失败/提交失败）；`base` 场景 after 侧**未重跑**，如实标注。
  证据：`scripts/state-recovery-verify/shots/after/`+`after-report-partial.json`+`evidence/e1-after-run.log`。

### E2 — 真实关闭重开与关键浏览器旅程缺失
- **结果（基线）**：J1 通过、J2 通过、J3/J4/J5 按预期红（正是 N6/N3/F1 的缺陷）。
- **修后（`390e0b1`，被验收 SHA `ccc651a`）**：**J1–J5 五条全绿**（基线是 2 绿 3 红）。
  J3（N6）实测：关闭前附加字段已输入（`https://new.example/path` / `新标题`）、未完成正文已真实落到服务器草稿；
  关闭浏览器进程 → 同一 profile 重开 → 正文恢复最后版本、**网址与标题一并恢复**、正式内容仍是原值（不自动形成正式改动）。
  J4 真实发出 `POST /demo/advance`，body `{"outcome":"revert_rest","decisionIds":[两个真实待决定项]}`；
  J5 取消后被取消的正文不再留在板面；J1 关闭前后本机记录均为 null、编辑入口恢复服务器稿且服务器稿未误删。
  证据：`scripts/state-recovery-verify/shots/after2-journey-report.json`、`evidence/e2-after2-run.log`、`shots/after2/*.png`。

---

## 4. 原 R1–R6 的保持情况

| # | 结论 | 依据 |
| --- | --- | --- |
| R1 取消后板面回到已保存状态、草稿新输入保留、不被后续无关操作带回 | 保持（**回读失败**那一支按 F1 收敛，见下） | `closure-lead-r1r2r3r5.test.ts`（R1 用例）、D 的 F1 反例 |
| R2 迟到的 GET 不回退已保存成功的新板面 | 保持 | 同文件 R2 三条；读取代次 + `knownServerSeq` 只允许更新的读取改动板面事实 |
| R3 保住第二版正文同时吸收第一版保存的版本事实 | 保持 | 同文件 R3；回执分支仍是「吸收版本事实、不替换候选」 |
| R4 选服务器稿后重试只删本机冗余副本、不写 cleared | 保持（并加强） | `closure-d-r1-r4.test.ts`、`closure-a-local-removal-purpose.test.ts`、D 的 J1 |
| R5 兜底确认取得有效 `checkId` 后真实落库 | 保持（并加强） | 同文件 R5；本轮在该分支前**增加**了「先读最新事实」，读取失败仍给出说明 |
| R6 旧确认不暂停后来新增的运行任务 | 保持 | 同文件 R6；`scopeChanged` 的重新说明在新的确认说明里展示 |

**有意变更的两处旧口径（已写进契约 §8.5，不是回归）**
1. R1/F1「取消 + 回读失败」：旧口径 = 恢复被取消的候选 + `dirty=true` + `saveStatus="error"`；
   新口径 = 本地撤回立即生效 + `cancelRecovery.reason` + 重试入口。理由：旧口径正是 F1 的根因
   （后续独立操作会从尚未恢复的板面复制被取消的正文）。
   → `frontend/src/stores/__tests__/closure-lead-r1r2r3r5.test.ts` 该用例已按新口径改名并改写。
2. 08 路径2「等待确认期间又改别处」：旧口径把「已重新核实」写在 `impactCheckError`；新口径写在新的确认说明
   `pendingImpact.note`，并在给出有效说明时清掉 `impactCheckError`（N2：界面不能同时宣称两种状态）。
   → `frontend/src/stores/__tests__/final-lead-m1.test.ts` 已按新口径断言。

---

## 5. 本轮实际运行的命令与结果

> 全部数字来自真实终端输出；门禁可复跑：`scripts/recovery-lead-verify/final-gate.ps1`（输出落 `out/`，不入库）。

### 5.1 开发阶段（各成员，目标范围）
- A：4 个自测文件 51 例（红→绿）+ 相邻 24 文件 209 例；`vue-tsc` exit 0；合并后接线复核 6 文件 63 例、
  相邻 27 文件 233 例、三个受影响目录 104 文件 958 例全绿；另发现并报告了 F2 写入方向的真实缺口（已由 Lead 修复并复验）。
- B：`uv run --frozen --extra dev python -m pytest` 14 文件 230 passed / 6 skipped / 0 failed；
  真实 HTTP 探针（uvicorn + 独立临时 `QIO_DATA_DIR`）27/27 PASS；红证据 16 failed（ASGI）+ 13 条 FAIL（HTTP）。
- C：N2/N3 组件与共用测试 37/37；`src/interactive` 目录 541/541；前端全量 186 文件 1707 例全绿；`vue-tsc` exit 0；V1 真实点击三项成功。
- D：基线反例前端 8 文件 12 failed / 2 passed、后端 3 failed / 1 passed；E1 基线 32/32 格采集断言通过；
  E2 基线 J1/J2 通过、J3/J4/J5 按预期红。
- Lead：`recovery-lead-state-facts.test.ts` 13 例；`src/stores` + `src/interactive` 83 文件 839 例全绿；
  F2 写入方向缺口红→绿证据（`.evidence/lead-f2-prod-entry-redgreen.txt`）。

### 5.2 集成阶段（Lead，集成分支）
- `scripts/recovery-lead-verify/final-gate.ps1`（SHA `b75239a`，合并 C 后的候选）：
  `vue-tsc --noEmit` exit 0；前端全量 **186 文件 / 1707 用例全绿**；
  后端受影响模块（14 个引用 `interactive`/`board_store` 的测试文件）**222 用例 / 0 失败 / 0 错误 / 0 跳过**；
  `scripts/check_docs.py` 通过。
- **门禁（`scripts/recovery-lead-verify/final-gate.ps1`，在代码/测试冻结候选 `ccc651a` 上实跑）**：
  `vue-tsc --noEmit` exit 0；前端全量 **194 文件 / 1723 用例全绿**；
  后端受影响模块（15 个引用 `interactive`/`board_store` 的测试文件）**226 用例 / 0 失败 / 0 错误 / 0 跳过**；
  `scripts/check_docs.py` 通过。其后只追加证据与文档（`390e0b1` 仅为证据/截图），文档提交上复跑 `check_docs.py` 通过。

### 5.3 未运行 / 说明
- **未跑后端全仓全量**：本轮只改互动板相关的 4 个后端模块（版本门与撤回重核）与前端互动板模块；
  按用户要求做针对性验证，不机械重跑全量。合入 `main` / 发布时再对最终候选做一次全量。
- **未跑 `agent.eval.run`**：本轮未改 runtime / 预算 / 工具策略。
- **未跑安装/升级/恢复等发布检查**：本轮不发版。
- **CI（推送后的真实运行，SHA `ab1b6caab2d85b6f41f67a5405d0ec71c72bbb6c`）**：
  run **38043038509**（https://github.com/HebiIsHere/QIO/actions/runs/38043038509）**9/9 任务全部 success**
  （rust windows / docs consistency / backend windows / backend py3.12 / frozen worker / install e2e / rust ubuntu / frontend / backend py3.11）。
  该 SHA 与本节其余内容只差「这一行 CI 结果的回填」，代码与测试完全一致。

### 5.4 CI 与推送
- 远端分支：`origin/fix/interactive-state-recovery-completion`（新建，未合并 `main`、未发布）。
- 推送后的 CI：run **38043038509** = **9/9 success**（对应提交 `ab1b6ca`）。
- 两个数字口径不同但都存在：CI 跑的是**后端与前端全量**（含 Linux/Windows 两个平台与安装 e2e），
  本地门禁跑的是**受影响模块**（按用户要求不做无收益的重复全量，见 §5.3）。

---

## 6. 截图、组操作与关闭重开旅程

### 6.1 E1：有效截图与测量（D 的独立装置）
- **基线 b3245e5：32/32 格采集断言通过**（4 尺寸 × 亮暗 × 正常/双面板共存/保存失败/提交失败）。
  每格截图前实测：`data-theme` + 真实背景亮度（亮 0.9728 / 暗 0.0061）+ 主题持久化（重新加载后再断言）、6 卡/6 关系/2 组、
  预定面板状态、目标失败的原因与重试入口；条件不满足即失败。
- **修后：受影响场景 24 格全绿**（同上；`base` 未重跑，如实标注）。
- 工具栏实测：1440=51px、1024/800=80px、480=104px（提交失败态 114/172px），before/after 一致。
- `480×600` 的共存按产品「切换条（只展开一个面板）」形态断言，**不是缺陷**。
- 保存/提交失败用页面内 `fetch` 桩制造（证据里标注为模拟），其余为真实后端。

### 6.2 V1：组操作实际点击/键盘结果（C 的独立装置）
- `summary={"total":98,"reachable":78,"covered":3,"panel":5,"offscreen":12}`；真实点击（1024×768，CDP 真实鼠标）：
  设为有序 `false→true` ✅、移出成员 `[cA,cB]→[cB]` ✅、解除组后组消失 ✅。
- 剩余 `covered 3` 全部在 480×600：组头部落在**主动打开的**底部固定工具栏下方（面板遮挡），已在证据里区分，不是新缺陷；
  独立 480×600 反例（两组分别在视口上/下部）实测上部组可达、下部组被工具栏遮挡。

### 6.3 E2：真实关闭重开与关键旅程（分层如实标注）
| 场景 | 层 | 基线 | 修后 |
| --- | --- | --- | --- |
| J1 草稿冲突→选服务器稿→本机删除失败→重试→正常关闭重开→编辑入口恢复 | ④真实 HTTP + ⑤真浏览器 + ⑥实际进程关闭（正常关闭） | 通过 | 通过 |
| J2 删除失败未重试→**强制结束**进程→重开处理入口仍可达 | ⑤ + ⑥（强制结束） | 通过 | 通过 |
| J3 R3 连续编辑 + N6 完整附加字段→关闭重开恢复 | ④ + ⑤ + ⑥（正常关闭） | **红**（N6：网址/标题回退） | **通过** |
| J4 N3 待决定撤回：界面「继续」真实执行并带 `decisionIds` | ④ + ⑤ | **红**（不发请求） | 通过 |
| J5 F1 取消影响确认（受控延迟 1800ms）后被取消正文不留在板面 | ④ + ⑤（CDP 传输层延迟=模拟） | **红** | 通过 |
- 模拟部分如实标注：J1/J2 的 `localStorage.removeItem` 抛错（存储故障）、J5 的 CDP 传输层延迟；其余为真实后端与真浏览器，
  正常关闭用 CDP `Browser.close`、强制结束用真实进程终止，均验证了「调试端口/进程真的消失」。
- **未验证**：J5 的「等待期间独立移动保留」子断言（真实指针拖动在本装置没有让卡片移动，已标未验证）；
  R2/R5 与 N1/N4/N5 的浏览器级证据（已有 store/ASGI 层反例）；N6 的 file/code 字段在真浏览器（组件级已覆盖）。
- 跨进程证据：J1 关闭前后本机记录均为 `null`（重试只删本机冗余副本、没有写成 cleared 依据），服务器稿未被误删；
  J3 关闭前本机记录里的附加字段快照在重开后仍被读到（这正是 N6 修复的落点）。

---

## 7. 剩余问题、真实限制与建议

1. **并发写同一板面没有数据库级串行化**：服务端版本门是「读当前 `seq` + 比较」，两个并发请求都读到同一 `seq` 时仍可能先后都通过。
   本轮按兼容最小方案处理（契约 §1.4）；要严格串行化需要另立任务（例如行级锁或 CAS 更新）。
2. **采集脚本的固定 `seq`**：`scripts/closure-c-verify/capture.mjs`、`scripts/interactive-verify/*.mjs`、
   `scripts/closure-d-verify/closure-d-browser-probe.mjs` 里把固定 `seq`（0/1）写进 PUT，在非空板面上会 409。
   属于本轮有意的服务端保护；D 的新装置（`scripts/state-recovery-verify`）已按「先读当前版本」改写，旧脚本建议后续统一改造。
3. **真浏览器覆盖范围**：见 §6 的如实标注（未跑的旅程写「未验证」）；R2/R5、N1/N4/N5 的浏览器级证据本轮未做，
   已有 ①③④ 层证据。
4. **F2 指纹是 64 位哈希**（双 32 位 FNV-1a）：正版本记录仍先按版本守卫，指纹只是额外校验；理论碰撞概率非零但极低。
5. **同一浏览器多页面共用一份本机记录**：两个页面同时编辑同一张卡片的草稿时，本机记录只有一个槽位；
   冲突机制在读取时识别「本机 / 服务器」两份候选并由用户选择，但同一时刻两边同时写本机记录的极端情形仍以最后一次写为准
   （既有设计限制，本轮未改变）。
