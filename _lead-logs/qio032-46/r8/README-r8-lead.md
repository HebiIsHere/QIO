# #46 第八轮修复交付报告（Lead）

- 分支：`fix/qio032-46-board-state`
- 基线 SHA（开工 fetch 核对 origin 同 SHA、无后续提交）：`c9fedc60744911c088ebc082faceef266ca3048b`
- 产品工作树：`D:\qio-dev\q46-r8`（detached 起于基线，依赖走 junction）
- 红基线 worktree：`D:\qio-dev\_q46-r8-baseline`（同 SHA，产品代码零改动，node_modules/.venv junction）
- 本轮产品提交：`cf13ca81`（实现 + q46-r8 定向用例 + r8 红/绿证据 + 两个被新语义取代的旧组件用例更新）
- 只做本地定向测试、类型检查与隔离后端浏览器验收；未跑全量、未触发 CI（提交信息带 `[skip ci]`）、未合并 main、未打标签、未发布。

## 一、10 个最新发现 + 2 个遗留问题 → 12 项实现

主要入口 `frontend/src/stores/interactive.ts`，另动 `frontend/src/components/IntentExecutionMessage.vue`、`frontend/src/components/interactive/AddMenu.vue`。

1. **提交归属于发起板面**：`submit()` 入口冻结发起板、提交身份、视口与保存依据；保存回执后只读原板面的保存事实决定下一步；提交回执 / 受理查证 / 重试身份 / 提交后草稿恢复 / 任务刷新按板面会话记账（新增 `BoardSubmissionSession`），切走后写回原板、切回可见；受理查证用 `identity.boardId + clientRequestId`，未知回执不重复 POST。
2. **旧正式保存不得删除另一标签页的新恢复副本**：待清除登记冻结原板 / 卡片 / 草稿版本 / 本机记录版本 + 内容指纹；清理前重读实际存储，身份不一致则保留新记录；跨标签页同步标记（共享 localStorage）阻止旧版本发 `removed`；后端删除继续用逐键版本守卫。
3. **撤销正式编辑保留草稿**：登记绑定正式编辑期望形态（正文 + 适用 meta / 删除）；保存回执必须证明这一形态真的落库；撤销 / 替换让登记失效（`stale`），重做后形态再次对上才允许清。
4. **迟到读取不得显示旧稿**：`refreshBoardFromServer` 增加读取代次（更新的读取已发出则整次响应丢弃）、回执板面身份校验、草稿存簿版本与逐键版本成对应用；正式板面版本与草稿版本分别判断。
5. **一块板失败不堵其他板**：flush 失败只停该板该版本并记录原因，继续处理其他板；失败版本保留「重试保存」入口，新编辑 / 明确重试才重发。
6. **冲突板不卡队列**：先判定「可发送」再选板；只剩等用户选择的冲突板跳过；runner 唯一在途引用、所有退出路径 finally 清理，重复进入只登记再来一轮。
7. **切板代次**：`selectBoard()` 入口登记代次，清单 / 整板读取 / 路由跳转 / 失败提示 / 加载结束都核对；`IntentExecutionMessage.vue` 只为仍有效的打开操作跳转。
8. **撤回代次**：撤回状态带 `opSeq` 与 `expectSeq`；整板回读 / 保存成功结算绑定发起时操作与服务器版本；旧成功 / 旧失败都不能关闭更新的撤回。
9. **替换文件失败后的重试**：可复用附件缓存带 `attemptId`，只允许「当前等待恢复的这次尝试」复用；上传失败的尝试沿用内存文件源重新上传（同名不同内容按尝试区分）；迟到旧上传不写缓存。
10. **旧保存只结算它包含的关联**：附件结算同时用「回执板面状态」与「本次请求候选快照」核对；失败请求不含该关联时保持「关联待保存」，旧保存失败不再误标新附件。
11. **批量导入反馈与重试归原板**：当前批次 / 汇总 / 在途按板面取值（A 不显示 B 的批次与重试）；`AddMenu` 切板时恢复目标板自己的批次入口；重试核对原板 / 批次 / 项 / 目标卡。
12. **撤销关联后结束失效等待**：板面候选变化时核对「卡片 + 附件」关系是否还在；撤销 / 替换 / 删除立刻结束旧尝试等待，旧保存回执不复活；重做 / 重跑恢复关联后重新等包含它的保存回执。

## 二、定向测试（先红后绿）

新增 6 个 store 用例文件（30+ 例）与 3 份逐用例红证据 README；红基线在 `_q46-r8-baseline@c9fedc60` 采集（产品代码零改动）：
- 第 2-6 项：`q46-r8-drafts-protect.test.ts`（6）、`q46-r8-drafts-queue.test.ts`（7）→ 基线 8 failed / 5 passed，红日志 `t1-drafts-red.log`（含第 6 项浏览器卡死探针 `t1-drafts-queue-freeze-probe.log`）
- 第 1/7/8 项：`q46-r8-submit-ownership.test.ts`（5）、`q46-r8-select-cancel.test.ts`（4）→ 基线 9 failed，红日志 `red-r8-task2-combined.log`
- 第 9-12 项：`q46-r8-attachment-replace.test.ts`（5）、`q46-r8-import-board.test.ts`（4）→ 基线 9 failed，红日志 `t3-attach-red.log`

最终绿（产品工作树）：
```
cd frontend
npx vitest run src/stores/__tests__/q46-r8-*.test.ts src/stores/__tests__/q46-r7-*.test.ts
→ Test Files 12 passed (12) / Tests 57 passed (57)
npx vue-tsc --noEmit → exit 0
```
证据：`lead-final-r8-r7-targeted.log`、`lead-final-vue-tsc.log`；三个队友 README（`README-task{1,2,3}-*.md`）含逐项基线失败断言与修复方向。

受影响面定向批次（`src/stores/__tests__ src/acceptance-state-recovery src/components/interactive/__tests__ src/components/interactive/v2/__tests__ src/views/__tests__ src/services/__tests__`）：
```
Test Files 3 failed | 303 passed (306)
     Tests  10 failed | 2452 passed (2462)
```
10 个失败全部是既有问题（`acceptance-i2v2-page-intents` 7 / `page-recovery` 2 / `page-tasks` 1），已在基线 `_q46-r8-baseline` 同命令复现同样 10 个失败（`3 failed / 10 failed | 7 passed`），不归因本轮。证据 `lead-full-targeted3.log`。

**被新语义取代的旧组件用例（2 个）**：`fix025-attach-notice`（上传失败后的重试现在按任务书四.9「重试关联」并重传当前文件源，不再要求重新选择文件；卡片已有附件时重试成功替换原有附件）、`pm032-canvas-import-batch`（批次入口按板面归属后，切到 A 时当前批次为 null；用 `importBatchById` 读原板批次，切回后等一次渲染再找重试按钮）。改动只调整这些新语义下的断言，不放宽原有安全断言。

## 三、浏览器与真实后端验收

环境：`_lead-logs/qio032-46/r8/verify/env-up-r8.ps1`（前端 q46-r8@5201/5203、后端 q46-r8/backend、fake provider、数据目录 `r8/e2e-data/final` 隔离）；驱动 `r8/verify/run-48.mjs` + `r8-s1/s2/s3/s4s5/s6s7.mjs`；真实系统 Edge headless + CDP；失败注入用 CDP Fetch（与真实回执在 detail / simulated 分开标注）。

**聚合结果：`r8/final/summary.json` → PASS 8 / FAIL 0 / OBSERVE 2 / TOTAL 10**（截图与逐组日志同目录）：
| 场景 | 结果 | 关键事实 |
|---|---|---|
| s1 第1项 | **PASS** | 真实「提交给 QIO」按钮；挂起 B 的 save PUT → store.selectBoard(A) → 放行；POST 只打 B、baseStateVersion=2（B 的保存版本），B 提交 0→1、A 0→0、仍在 A、A 后端正文逐字不变 |
| s1b 迟到提交回执 | **PASS** | B 的提交回执在 A 打开时才到：A submitStatus 保持 idle，回 B 可见 succeeded |
| s2 第2项 双标签页 | **PASS** | 两个真实标签页；页1 正式保存回执到达后页2 未同步新稿的本机恢复副本（正文/版本/kind）全保留，重开编辑器可恢复 |
| s2b 已同步新稿 | OBSERVE/UNVERIFIED | 驱动窗口内页2 草稿 PUT 未真正落库（tab2Synced=false），不判产品结果；store 用例 q46-r8-drafts-protect 2B 覆盖 |
| s3a 第5项 | **PASS** | A 草稿 PUT 注入 503 后 B/C 草稿仍自动保存、各归原板，浏览器响应，故障解除后 A 重试补存成功 |
| s3b 第6项 | **PASS** | B 真实同键双窗口冲突保留两份；A/C 草稿继续保存、冲突项不反复重发；响应探针 9ms（基线此处浏览器卡死） |
| s4 第7项 | **PASS** | 先 B 后 A 与交换顺序当前板始终是最后加载完成的板；最新 C 读取失败错误归 C，旧 B 响应不能抢回页面 |
| s5 第8项 | OBSERVE/UNVERIFIED | read1/read2 均扣住、第一次 active=true；第二次撤回被另一次合法整板读取提前确认，浏览器层无法与「第一次旧回执迟到」隔离；store 用例 q46-r8-select-cancel 覆盖精确乱序 |
| s6a 第9项 | **PASS（驱动兜底）** | old.txt 真实关联；替换用公开 startCardAttachment（页面内真实 File + 浏览器上传通道）→ 注入上传 500 → 卡片保留旧附件；重试用公开 retryCardAttachment（真实按钮因 AddMenu 无该 job token 未出现）→ 重传同一文件源（200）→ 后端 attachmentId 变为新附件 |
| s7a 第11项 | **PASS** | B 拖入 2 文件并注入关联保存 500；A 看不到 B 的批次/重试，回 B 恢复；真实点批次重试只 B 建卡、不重复上传/建卡，A 保持 1 张原卡 |

第七轮浏览器 11/11 本轮未整体复跑（本轮复跑第七轮 store 6 文件 26/26 与受影响组件/页面定向批次），r7 浏览器证据保留在 `_lead-logs/qio032-46/r7-final-*`。

## 四、诚实边界

- 未跑全量前后端测试、未触发 CI、未合并 main、未打标签、未发布。
- 浏览器验收失败注入经 CDP Fetch 完成（与真实回执分开标注）；驱动不稳的场景记 UNVERIFIED 并写清尝试方式。
- Windows 原生 Tauri 壳、真实多窗口并发保存、整页刷新后的恢复记录重建仍为未验证。
