# QIO032-46 第七轮（R7）浏览器验收 · 最终结果

> **结论：11/11 PASS（其中 1 条是 6b 补充观察），0 FAIL，0 UNVERIFIED。**
>
> 被测代码：`fix/qio032-46-board-state` 的最终定稿 `1b75e54a`（第七轮实现 `51a35400` +
> 收口修复「选择变化/同内容重复提交不再重置自动恢复轮次；关联重试补排保存」），运行时以
> **5199 的 vite dev 实际提供的代码**为准（本次运行前确认 git HEAD=1b75e54a、工作树只有
> `docs/status.md` 与 `_lead-logs/` 证据目录变化，产品代码零未提交改动）。
>
> 修复前基线：`_lead-logs/qio032-46/r7-baseline/`（HEAD 0dbc387f 的独立 detached worktree，
> 产品代码零改动；基线 5 PASS / 5 FAIL / 1 UNVERIFIED）。
>
> 环境：`env-up.ps1`（uvicorn 8734 / vite 5199 / fake provider 8807 / Edge CDP 9333），
> 数据目录隔离 `_lead-logs/qio032-46/e2e-data`，**绝不使用 `D:\QIO-data`**。
> 驱动：`verify/run-47.mjs` + `verify/lib47.mjs`（追加层；未改 `run-46.mjs` / `lib.mjs`
> 既有语义）。产品代码只读；所有写入都在 `_lead-logs/qio032-46/` 下。

## 结果表

| # | 场景 | 判定 | 关键事实（请求台账 / store 快照 / DOM 证据） |
|---|------|------|----------------------------------------------|
| 1 | 草稿双窗口并发 | **PASS** | A 读 rev1 → 切 B → 回 A；另一窗口把 A 同键推到 rev2 后，窗口1 的草稿 PUT 仍带 `baseRev=1`、`keyVersions[card:ca1]=1`（基线误带 6）；服务端同键冲突返回两份，store `draftConflicts[card:ca1]={local:窗口1新版, server:另一窗口文字}`、`drafts[card:ca1]` 保留本地新版；后端 A 保留另一窗口文字、B 草稿不受影响（`card:cb1=B 草稿第一版`）。截图 `shots/01-s1-draft-two-windows.png`。 |
| 2 | 正式保存后台成功清旧草稿 | **PASS** | B 完成编辑（登记清除）→ PUT 挂 4s → 切 A → 放行成功：草稿台账 2 条 —— 先 `baseRev=2` 写正式正文，随后 `removed:["card:cb2"], baseRev=3` 真清旧稿；后端 B `rev=4, drafts={}`、B 正式内容落库（`seq=3`），A 草稿保持；B 回执前再输入的新草稿保留。截图 `shots/02-s2-draft-clear-after-save.png`。 |
| 3 | 后台冲突也计入上限 | **PASS** | B 保存后立即切 A、PUT 全部回自定义 409：**恰好 4 次 PUT**（2199/6497/6965/7429ms），B→A→B 往返后仍 4 次、无第 5 次；回 B `saveStatus=error`、`dirty=true`、原因「已自动重来 3 次」；页面「重试」按钮真实出现且可点（`data-iw-chrome-save-retry`，可见/在视口/命中 button/rect 762,51,40×22），点它后只发 **1 次 PUT 且 200 成功**（`saveStatus=saved, seq=2`）；A 0 次 PUT。截图 `shots/03-s3-background-conflict-limit.png`。 |
| 4 | 预判乱序（三种到达次序） | **PASS** | 三组各自独立板面 + 运行中任务（seed.py），用「按命中次序决定延迟/失败」的 CDP Fetch 规则控制到达顺序。三种次序都成立：两次编辑均落地、每次 2 次预判、确认入口 `pendingImpact` 始终对应**最新候选**（checkId/stateVersion/候选正文一致）、只出现一份确认；点真实「确认改动」按钮后 **1 次 PUT 200**，请求目标=B、候选正文=最新、`confirm.checkId/stateVersion` 与 pendingImpact 一致、`saveStatus=saved`。截图 `shots/04-s4-impact-ordering.png`。 |
| 5 | 确认提示不串板 | **PASS** | B 点确认后切已保存 A，B 的保存失败在 A 停留期间到达：A `saveStatus=saved、saveError=null`、`data-iw-chrome-save-retry` 不存在、页面无失败文案；回 B 可见 `saveStatus=error`（Failed to fetch）+ 可点「重试」，点它后 PUT 只指向 B（`putsA=0`）。截图 `shots/05-s5-confirm-not-cross-board.png`。 |
| 6 | 撤回重试 | **PASS** | B 影响确认点「取消本次改动」，回读被 CDP 打断：`cancelRecovery={boardId:B, active:true, reason:"…Failed to fetch"}`，页面出现可点「重新核对服务器事实」（`impact-confirm-recovery-retry`，可见/未禁用/命中）；解除故障后从该按钮点重试 → `cancelRecovery={boardId:B, active:false, reason:null}`、结果层变「已取消」、重试入口消失。截图 `shots/06-s6-cancel-recovery-retry.png`。 |
| 6b | B 的撤回失败晚于 A 的撤回开始 | **PASS**（补充观察） | A 取消回读延迟 4s、期间切 B 让 B 的撤回失败回执到达：回到 A `cancelRecovery={boardId:A, active:false, reason:null}`，B 的是 B 自己的结果，互不覆盖。 |
| 7 | 任务隔离 | **PASS** | B 有运行中任务、A 首次 `GET /intents` 失败：A `intents=[]、intentViews=[]、intentsLoadState=error（Failed to fetch）`、页面不显示 B 的任务；A 的普通改动保存 `saved`（PUT 1 次指向 A、0 次指向 B），A 0 次影响预判、B 0 次。截图 `shots/07-s7-task-isolation.png`。 |
| 8 | 附件状态与重试 | **PASS** | 阶段一：B 上传→切 A→上传完成→回 B，页面显示**真实失败态**（「附件保存失败「attach-…」…可以重试关联」），不显示成功。阶段二（关联保存 500）：提示「附件保存失败「retry-…」：…500…」，页面出现可点「重试关联」（`add-material-retry`）；从该按钮点击后：上传仍只有 1 次（复用附件不重传）、后端回读文件卡 `attachmentId=att_23bb0cf133c2`、界面变「已关联附件「retry-…」」、`saveStatus=saved`。截图 `shots/08-s8-attachment-state-retry.png`。 |
| 9 | 批量导入 | **PASS** | 拖入 2 份文件、PUT /state 全部 500：两个批次项均 `failed`（DOM `data-status="failed"` + 失败原因，**没有**「已接收」）；故障解除后点批次项真实「重试」按钮 → 该项 `succeeded`、后端回读文件卡 `attachmentId=att_cf60216af824`；上传仍只有 2 次（每份 1 次，重试复用附件不重传）、卡片数不变（不重复建卡）、**同批另一项仍 failed（状态不变）**。截图 `shots/09-s9-batch-import.png`。 |
| 10 | 原始路径回归 | **PASS** | 上传 → 打开设置 → 返回互动页：`POST /api/attachments/upload` 1 次 200，后端回读文件卡 `meta.file.attachmentId=att_81b4696efe52`，无重复上传。截图 `shots/10-s10-settings-roundtrip.png`。 |

## 运行与证据

```powershell
powershell -ExecutionPolicy Bypass -File _lead-logs/qio032-46/env-up.ps1
cd D:\qio-dev\pm-qio032-integration
$env:QIO_UI_OUT='D:\qio-dev\pm-qio032-integration\_lead-logs\qio032-46\r7-final-main'
$env:QIO_E2E_BASE='http://127.0.0.1:5199'
node _lead-logs/qio032-46/verify/run-47.mjs      # 11 条记录：s1..s10 + 6b
powershell -ExecutionPolicy Bypass -File _lead-logs/qio032-46/env-down.ps1
```

- `summary.json`：11 条结果 + 每条完整 detail（请求台账 method/URL/status/次数、请求体、store 快照、DOM 文本、时间线）。`rerun` 字段记录本次是最终定稿后的一次完整运行；中间定向复跑（s3 的 `r7-final-rerun/`、s8/s9 的 `r7-final-rerun2/`）作为过程证据保留。
- `console.log` / `run.log`：本次运行日志（含 s4 每组驱动过程与请求时序）。
- `shots/`：每个场景一张真实浏览器截图。
- `materials/`：上传用例的隔离夹具文本。
- 原始完整运行目录：`_lead-logs/qio032-46/r7-final-main/`（本目录是它的汇总副本）。

## 边界（诚实说明）

- 浏览器层是系统 Edge headless + 自研 CDP 驱动（不是 Playwright）：点击是真实鼠标事件 + 元素命中测试；「可点」= 元素存在、可见、未禁用、在视口内，并记录命中元素与矩形。
- 失败注入与模拟回执全部经 CDP Fetch（延迟 / 失败 / 自定义 409 / 自定义 500），与真实后端请求分开记录在 detail 里；真实请求走隔离 uvicorn + 隔离数据目录。
- 「切板」由真实 store 的 `selectBoard()` 驱动（互动页当前没有面向用户的板面切换器），所有**重试/确认/取消**动作都从页面真实按钮点击完成。
- 未跑全量前后端测试、未触发 CI、未 push、未改产品代码；本轮只做本地定向浏览器验收。
- 与基线对照的差异（1/3/6/8/9 从 FAIL 转 PASS）见 `r7-baseline/README.md`。
