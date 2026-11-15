# QIO032-46 第七轮（R7）浏览器验收 · 修复前基线（HEAD 0dbc387f）

> 本目录是**修复前基线**证据，不是交付结论。被测代码是 `fix/qio032-46-board-state` 的 HEAD
> `0dbc387f1ecdc2dfa21f658ce16dbe07a6fa9b3d`（与 origin 同 SHA），**不包含** Lead 工作树里
> 未提交的第七轮实现。为拿到真正的 HEAD 代码、又不覆盖 Lead 的工作树，基线跑在独立的
> detached worktree `D:\qio-dev\_r7-baseline-head`（HEAD=0dbc387f，node_modules 走 junction，
> 产品代码零改动），vite 起在 **5200**；后端 / fake provider / 数据目录与正式环境完全一致
> （uvicorn 8734、fake provider 8807、`_lead-logs/qio032-46/e2e-data`，绝不使用 `D:\QIO-data`）。

## 怎么跑

```powershell
# 1) 隔离环境（uvicorn 8734 / vite 5199 / fake provider 8807；数据目录 e2e-data）
powershell -ExecutionPolicy Bypass -File _lead-logs/qio032-46/env-up.ps1

# 2) 基线：另起一个 HEAD 代码的 vite（5200），跑 run-47
cd D:\qio-dev\pm-qio032-integration
$env:QIO_UI_OUT='D:\qio-dev\pm-qio032-integration\_lead-logs\qio032-46\r7-baseline'
$env:QIO_E2E_BASE='http://127.0.0.1:5200'
node _lead-logs/qio032-46/verify/run-47.mjs

# 3) 收尾
powershell -ExecutionPolicy Bypass -File _lead-logs/qio032-46/env-down.ps1
```

（`Q47_ONLY=s1,s3` 可单选场景；`Q47_S4_CASE=0` 可只跑 s4 的某个到达次序。）

## 结果：PASS 5 / FAIL 5 / UNVERIFIED 1（共 11 条记录）

| # | 场景 | 判定 | 关键事实（请求台账 / store 快照 / DOM） |
|---|------|------|------------------------------------------|
| 1 | 草稿双窗口并发 | **FAIL** | A 读 rev1 → 切 B → 回 A 后，窗口1 保存 A 的草稿 PUT 体里 `baseRev=6`（应为 A 的 rev=1）、`keyVersions[card:ca1]=1` 正确；服务端因版本对不上返回同键冲突，前端两份都保留（`draftConflicts[card:ca1]={local:窗口1新版, server:另一窗口文字}`），B 草稿不受影响。**失败点：baseRev 取了别板/全局存簿版本**（第七轮修复点 1）。 |
| 2 | 正式保存后台成功清旧草稿 | **PASS**（口径偏弱） | B 完成编辑登记清除并保存（PUT 延迟 4s 挂起）→ 切 A → 放行成功：B 正式内容落库（`seq=3`），B 服务端草稿里已看不到「旧草稿」正文（此时是编辑器自动保存写进去的正式正文，不是 cleared 回执），A 草稿保持；B 回执前再输入的新草稿保留。**基线没有发出 `removed` 清除请求**（draftLedger 只有 1 条 put，无 removed）；本场景判 PASS 只因断言取「旧草稿正文不再出现」，不能证明「正式保存成功会清旧稿」。 |
| 3 | 后台冲突也计入上限 | **FAIL** | B 保存后立即切 A、PUT 全部回 409：timeline 显示前 4 次 PUT 在 0.8–2.2s 内完成（已达上限），随后 B→A→B 又发 **3 次**（6.4–7.4s），共 **7 次 PUT**，违反「≤4 次、切板往返不新增」；回 B 后 `saveStatus=error`、`dirty=true`、页面出现可点「重试」按钮（`data-iw-chrome-save-retry`，可见/命中），点它后只发 1 次 PUT 且 200 成功（`saveStatus=saved`）。**失败点：后台冲突不计入整轮上限 / 达上限后切板往返重启**（第七轮修复点 3）。 |
| 4 | 预判乱序 | **UNVERIFIED** | 驱动不稳：case0（旧成功先到）两次编辑都落地、3 次预判、确认对应最新候选且确认后 PUT 只有 1 次 200；case1/case2 的**第二次编辑在真实浏览器里没有落地**（board 仍是旧候选），不足以判定三种到达次序。尝试方式：每组独立板面 + 运行中任务（seed.py）+ 按命中次序的 CDP Fetch 延迟/失败规则 + 整页刷新隔离；已试 `typeCard`/`editCardText`/`finishEditing` 三条点击序列与 3 次重试，仍未稳定。按纪律记 UNVERIFIED，不作为产品结论。 |
| 5 | 确认提示不串板 | **PASS** | B 点确认后切已保存 A、B 的保存失败在 A 停留期间到达：A 的 `saveStatus=saved`、`saveError=null`、无 `data-iw-chrome-save-retry`；回 B 看到 `saveStatus=error` 与可点重试按钮，点重试后 PUT 只指向 B（`putsA=0`）。 |
| 6 | 撤回重试 | **FAIL** | B 影响确认里点「取消本次改动」，取消后的回读被 CDP 打断：`cancelRecovery={active:true, reason:"…Failed to fetch"}`，页面出现可点「重新核对服务器事实」（`impact-confirm-recovery-retry`，可见/命中）；解除故障后从该按钮点击 → 页面结果变成「已取消」、按钮消失，**但 store 里 `cancelRecovery` 仍是 `active=true`**。失败点：`cancelRecoveryByBoard` 是普通 Map + computed 不响应（第七轮修复点 6）。 |
| 6b | B 的撤回失败晚于 A 的撤回开始 | **PASS** | A 的取消回读延迟 4s、期间切到 B 让 B 的撤回失败回执到达：回到 A 后 `cancelRecovery.boardId=A、active=false`（A 自己的结果），B 的也各自归属。 |
| 7 | 任务隔离 | **PASS** | B 有运行中任务、A 首次 `GET /intents` 失败：A 的 `intents=[]、intentViews=[]、intentsLoadState=error`，A 的普通改动保存 `saved`（PUT 只指向 A 2 次、0 次指向 B），A 上不发影响预判（与 B 的任务隔离）。 |
| 8 | 附件状态与重试 | **FAIL** | B 上传→切 A→上传完成→回 B：job `phase=failed`、错误文案是「回执到达时当前板面不是发起上传时的原板面…可以重试关联」，页面提示是**上一阶段的成功文案**（「已关联附件「retry-…」」）而非本次真实失败；第二阶段的失败态里**找不到可点的重试入口**（`[data-im=add-material-retry]` / `[data-im=card-attach-retry]` 均不存在），只能靠后续保存自动恢复。失败点：跨板回执状态与成功事件口径（第七轮修复点 8/9）。 |
| 9 | 批量导入 | **FAIL** | 拖入 2 份文件、PUT /state 全部回 500：**两个批次项都标 `succeeded`/「已接收」**，页面上看不到失败与重试入口（`batch-failed` 之后无 `card-attach-retry`）；后端回读没有文件卡/附件。失败点：写入失败仍标成功（第七轮修复点 9）。 |
| 10 | 原始路径回归 | **PASS** | 上传 → 打开设置 → 返回互动页：`POST /api/attachments/upload` 1 次 200，后端回读文件卡 `meta.file.attachmentId=att_…` 存在，无重复上传。 |

## 证据

- `summary.json`：逐条结果 + 完整 detail（请求台账、store 快照、DOM 文本、时间线）。
- `console.log` / `run.log`：运行日志（含 s4 驱动过程的每一步观察）。
- `shots/`：每个场景一张真实浏览器截图（`01-s1-draft-two-windows.png` … `10-s10-settings-roundtrip.png`）。
- `materials/`：上传用例使用的隔离夹具文本。

## 边界（诚实说明）

- 本基线只验证「修复前代码在这些场景上的真实行为」，**不声称任何产品结论**；FAIL 项要由最终实现复跑对照。
- 浏览器层是系统 Edge headless + 自研 CDP 驱动（不是 Playwright），点击是真实鼠标事件与命中测试；「可点」指元素存在、可见、未被禁用、在视口内，并记录命中元素。
- 失败注入与模拟回执全部经 CDP Fetch（延迟 / 失败 / 自定义 409），与真实后端请求分开记录在 detail 里。
- 未跑全量测试、未触发 CI、未 push；产品代码只读，所有写入都在 `_lead-logs/qio032-46/` 下。
