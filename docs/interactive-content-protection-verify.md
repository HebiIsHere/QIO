# 互动模式内容保护与界面精修 · 独立验收 D/D2 记录（第六轮）

独立验收子智能体 D 记录，**不修改任何产品代码**。基线：`7ef7537`（分支 `wt/prot-d-verify`）。
本轮只做【第一步：在基线上建立七项反例并记录行为性失败】；正式验收（分层证据、真实关闭重开、
全量门禁）等主智能体集成提交号后在新的后续消息里补。

## 0. 环境与命令

- 后端：`D:\qio-dev\qio-wt-fixes\backend\.venv\Scripts\python.exe -m uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8941`
  （`PYTHONPATH=D:\qio-dev\qio-protect-d\backend\src`、`QIO_PORT=8941`、`QIO_DATA_DIR=%TEMP%\qio-prot-d`、`QIO_DEV_INSECURE=1`）；
  健康检查 `GET /api/health → 200`。
- 前端：`node frontend/node_modules/vite/bin/vite.js --config frontend/vite.e2e.config.ts --port 5441 --host 127.0.0.1`
  （`VITE_QIO_BACKEND_URL=http://127.0.0.1:8941`）。
- 探针：`node scripts/visual_probe.mjs '@d6-verify-prot-d/steps-....json'`
  （`QIO_PROBE_PORT=9641`、`QIO_PROBE_PROFILE=%TEMP%\qio-chrome-prot-d`、`QIO_PROBE_OUT=D:\qio-dev\qio-protect-d\d6-verify-prot-d`）。
- 测试：`cd frontend; npx vitest run <路径>`。

## 1. 反例文件与提交

| 文件 | 覆盖 | 提交 |
| --- | --- | --- |
| `frontend/src/stores/__tests__/d6FailedSendProtection.verify.test.ts` | §12.3 反例 4 / §12.4 反例 5 / §12.5 反例 6 | `274a6a7` |
| `frontend/src/stores/__tests__/d6CardDraftConflict.verify.test.ts` | §12.1 反例 1 剩余口径与反例 2 / §12.2 反例 3 | `6c2d39a` |
| `frontend/src/components/interactive/__tests__/d6SwitchBarOnlyWhenSecondPane.verify.test.ts` | §12.6 反例 7（480×600 + 800×600 对照） | `ec0bea4` |
| `d6-verify-prot-d/steps-d6-700-switchbar.json`、`steps-d6-800-transparency.json` | 探针步骤（480/800 实机几何 + 截图） | 本轮 |
| `docs/interactive-content-protection-verify.md` | 本报告 | 本轮 |

`npx vue-tsc --noEmit`：通过（无输出）。

## 2. 基线「先失败」记录（真实命令与原样输出）

```
$ npx vitest run src/stores/__tests__/d6FailedSendProtection.verify.test.ts src/stores/__tests__/d6CardDraftConflict.verify.test.ts src/components/interactive/__tests__/d6SwitchBarOnlyWhenSecondPane.verify.test.ts

 Test Files  3 failed (3)
      Tests  7 failed | 5 passed (12)
```

七条行为性失败（断言原话 → 实测值）：

1. **§12.1 反例 2**「仅打开编辑器就把冲突提示弄没了（§12.1：打开编辑器不等于选择本机版本）」
   `expected false to be true`；
   同用例的诊断取证（临时把前两个断言改为非阻塞输出再还原，未提交）：
   `[诊断] 冲突提示存在 = false store冲突 = false`、
   `[诊断] 本机版本写上了服务器: 本机上的版本`、
   `AssertionError: 服务器版本被打开动作改掉了: expected '本机上的版本' to be '服务器上的版本'`
   —— 即 `views/InteractiveView.vue`（BoardCard.startEdit）在打开编辑器的同一动作里调
   `store.setDraft(...)`：既清掉了 `draftConflicts`（提示消失），又把未决冲突键的本机候选
   经防抖写上了服务器（整份替换覆盖服务器那份）。两段都水性失败。
2. **§12.2 反例 3**「重读之后新输入变空了：旧的本机清除记录（写失败导致它还是唯一存档）重新取得了决定权（§12.2）」
   `expected '' to be '清除后的新输入'` —— `stores/__tests__/d6CardDraftConflict.verify.test.ts:157` 附近。
   复现路径：清除（依据落盘成功）→ 新输入（本机写入失败）→ `refreshBoardFromServer()`；
   `stores/interactive.ts` 的 `restoreLocalCardDrafts` 在 `kind === "cleared"` 分支先于
   `memoryIsNewer` 检查就 `delete merged[key]`，并把该键重新登记为待同步清除。
3. **§12.3 反例 4**「恢复记录落在了 B（当前话题），而未绑定发送的归属已稳定为 A（§12.3）」
   `expected false to be true` —— `stores/session.ts`：失败时 `settleSend(at, false)` 先把
   在飞记录从 `inFlightSends` 摘除，`_failedSendTopicFor` 的 `locationOf` 找不到就
   `return this.currentTopicId`（此时用户已切到 B）→ 失败原文/原因落到与之无关的话题 B。
4. **§12.4 反例 5**「后一次的失败原文被「前一次成功」按文字相同一起清掉了（§12.4：成功只处理该次发送的记录，不许按文字相同匹配）」
   `expected false to be true` —— `_clearFailedSendsAccepted` 的 `sameText` 分支（同话题+同文字）
   把另一条失败记录（不同 draftId）一并删了。
5. **§12.5 反例 6（内存）**「『第1份失败原文』被静默淘汰了（§12.5：不许自动淘汰用户尚未处理的失败原文）」
   —— 九次发送九次失败后只剩 8 条，最早的一份消失。
6. **§12.5 反例 6（本机）**「写本机的时候就已经把最早的一份悄悄裁掉了（用户还没有处理过）」
   `expected 8 to be greater than or equal to 9` —— `FAILED_SEND_TOPIC_LIMIT = 8` 的
   `trimFailedSendsPerTopic` 在 `_recordFailedSend` 与重装载两条路径上都在写盘前裁剪。
7. **§12.6 反例 7（组件）**「没有审批项、只开聊天时出现了虚假切换条（§12.6：不存在可展示的第二个面板就不许进入切换布局）」
   `expected true to be false`；诊断取证：
   `[诊断] 切换条存在 = true lift = 70px mode = switched chatMaxH = 292px`
   —— `IntentBatchTray.vue` 用 `overlaysAreCramped`（把批量按开着算）决定 `switched`，
   没有先看「第二个面板是否真的存在」（`batches.length === 0`）。

与预期一致地**通过**的 5 条（如实记录，非本轮新修复）：

- §12.1 反例 1 剩余口径 ×4（基线已含主智能体 §12.1 首步修复）：
  「未选择 → 仅编辑 B、保存 → 刷新后 A 冲突仍在、两份都可处理」「分别选择两种版本后保存与恢复符合选择」
  「旧格式无法判定新旧时登记冲突、不直接覆盖」「请求在飞期间的新输入不被旧的清理动作误删」。
- §12.6 对照：800×600 只开聊天（宽窗口）无切换条、无抬高。`--im-geo-chat-lift = 0px`、无
  `[data-im="overlay-switch"]`。

## 3. 探针实机证据（480×600 / 800×600）

### 3.1 §12.6 反例 7 实机（480×600，无审批项，只开聊天）

实测（`steps-d6-800-transparency.json`，第二次运行数据与首次一致）：

```json
{
  "switchBar": { "rect": { "t": 360, "b": 394, "h": 34, "w": 294 },
                 "text": "空间不足，只展开一个面板（内容都还在） 看对话 " },
  "vars": { "lift": "65px", "chatMaxH": "283px", "batchMaxH": "280px" },
  "chatPanel": { "t": 65, "b": 348, "h": 283 },
  "batchEntryCount": 0, "view": { "w": 480, "h": 600 }
}
```

- 界面上真实画出了切换条（`[data-im="overlay-switch"]`，34px 高），但此刻没有任何审批项
  （`batchEntryCount: 0`）、也只有聊天一个面板 —— §12.6说的「虚假切换提示」实机成立；
- 聊天面板被抬高了 65px、可用高度被压到 283px —— 不存在的切换空间被真实预留。

800×600 对照：`switchBar = null`、`lift = "0px"`、`chatMaxH = "370px"` —— 正常。

截图：
- [`d6-shot-480-chat-only.png`](<../d6-verify-prot-d/d6-shot-480-chat-only.png>)（首次运行，含引导浮层，几何数据有效）
- [`d6-shot-480-transparent-chat.png`](<../d6-verify-prot-d/d6-shot-480-transparent-chat.png>)（关引导后：切换条 + 抬高 + 透明区叠字同框）
- [`d6-shot-800-transparent-chat.png`](<../d6-verify-prot-d/d6-shot-800-transparent-chat.png>)（800×600 对照：无切换条、布局正常）

### 3.2 §12.7 反例 8 实机（透明区域）

480×600 实测 computed style：

```json
{ "panelBg": "rgba(0, 0, 0, 0)", "headBg": "rgba(0, 0, 0, 0)",
  "scopeBg": "rgba(0, 0, 0, 0)", "scopeLineBg": "rgba(0, 0, 0, 0)" }
```

- 聊天**标题行（panel-head）与常驻说明（panel-scope / scope-line）四层全部透明**；
- `document.elementsFromPoint` 在标题与说明行中点取到的下层依次是 `board-surface → board-scroll-content`
  及我们的长文字卡片（文字直接透到标题/说明底下）；
- 截图 `d6-shot-480-transparent-chat.png` 里可见：顶部横幅文字（「拖动空白处平移查看位置…」）
  与「对话 · 收起」标题行重叠；板面卡片长文字（「…应当被聊天的标题与说明压在下面读不清…」）
  从「应当被…」起一路透到聊天面板中部，标题/说明没有任何局部底色把它们隔开（§12.7 实机成立；
  输入框与发送按钮区仍保有底色，属实测观察，不改变结论）。

## 4. 本轮红线遵守

- 只新增了自己的文件：三个测试文件、`d6-verify-prot-d/`（步骤 JSON + 截图）、本报告；
  未改 `views/InteractiveView.vue`、`interactive/types.ts`、`services/interactive.ts`、
  `scripts/interactive-verify/fe-scenarios.mjs`、他人文件与其它 `docs/**`。
- 未删除、未缩窄任何反例；诊断时临时放宽的两处断言在取证后已原样还原（见 §2.1 的「未提交」标注）。
- 全程中文提交：`274a6a7`、`6c2d39a`、`ec0bea4`；无 `git add -A`。

## 5. 没做到 / 留给第二步

- §12.1 反例 1 的**首个主口径**（「未选择 → 编辑 B → 保存 → 刷新」的完整真实浏览器用户旅程，含
  「A 服务器版本被覆盖 / 本机副本被删」两端）没有在真 UI 里走完：基线已含主智能体 §12.1 首步修复，
  验收重点按任务口径转到了选择两种版本、在飞新输入、旧格式无法判定（已按 store 层真实动作覆盖，
  结果见 §2「通过」段）。第二步若有集成提交，将补实机旅程。
- 反例 4/5 的高保真成分（组件事件链真实点击发送）以 store 状态层 + 受控替身驱动（`d5` 系列同款），
  真实鼠标旅程留到第二步。
- `vue-tsc` 已跑（通过）；全量 `vitest`（`npm test`）、后端 `pytest`、`scripts/check_docs.py`
  按任务书属第二步（等主智能体集成提交号后与分层证据、两个入口的真实关闭重开、三档尺寸 × 明暗
  480×600 前后截图一起补）。


## 6. 第二步正式验收 · 关闭重开（主智能体补齐，2026-10-09）

### 关闭重开（新浏览器进程、同一持久化 profile）

- 实例：前端 `127.0.0.1:5475`、后端 `8975`（独立数据目录 `%TEMP%\qio-r6-closereopen`）；
- 探针配置：同一 `QIO_PROBE_PROFILE=%TEMP%\qio-chrome-closereopen3`（第二次调用 = 新浏览器进程、同一目录）；
- 第一次进程：互动聊天发送一条失败原文（`/api/turns` 拦截，模拟），恢复入口 1 条、原因真实；
- **第二次进程**（关闭后重开同一 profile）：
  - 互动聊天：恢复入口 `recoverCount=1`、原文 `关闭重开要找回的原话` —— 原文保留；
  - 平移到对话页：`composer-reopen` `recoverCount=1`、原文一致 —— **同一失败原文在两个入口都找回**；
- 证据截图：`docs/interactive-ui-screenshots/r6-lead-chat-reopen-recovery.png`、
  `r6-lead-composer-reopen-recovery.png`。

### 其余视觉与功能证据

主题与三档尺寸（明暗 / 1440×900 / 1024×768 / 800×600 / 480×600）来自子智能体与主智能体的既有截图；
反例覆盖失败原文无静默淘汰（查看其余 N 条）、无批次无切换条、标题与说明底色等。
