# 发布前第二轮 UI 一致性修复（2026-10-29）

- 基线：`fix/release-step1-model-reliability-20261028` @ `f5d0e7447d4a68760693ec21920ce9919b67c549`
  （开工核对：远端分支尖端就是该提交，且包含第一轮已验收提交，未退回更早基线）。
- 工作分支：`fix/release-step2-ui-consistency-20261029`
- 范围：只处理发布前第二轮指定的四项问题（A 关闭行为 / B 用途分配生效性 / C 候选按用途能力 /
  D 互动意图执行记录）及其直接影响；不合并 main、不打标签、不发布、不开始下一轮。
- 第一轮已通过的行为全部保留：连接复用与按模型缓存、后台配置隔离、连接可用性判断、
  日常对话合并行原子保存与真实目标显示。
- 验证口径：后端全部使用 fake provider / 直接落库夹具，不联网、不用真实 API Key；
  前端只跑定向 vitest 与类型检查；未跑全量测试、未跑 build（见「已知限制」）。

## A. 关闭行为按最新有效选择生效（原报告 F05）

- 原触发条件：`setCloseBehavior` 先乐观写状态、等 HTTP 返回后再写一次并同步桌面壳；连续切换
  （直接退出 → 最小化到托盘）时较早的请求晚返回，会把新选择盖回去，壳也缓存成旧行为。
  序号保护只在设置页的保存助手（页面显示层）里，挡不住 store 真正写状态与同步壳这两处。
- 修复：
  - `frontend/src/stores/ui.ts`：`setCloseBehavior` / `load` 在**真正写状态与同步壳的位置**领单调序号；
    只有最新那次响应允许写 `closeBehavior`、更新 `closeBehaviorEffective`（最近一次确认生效值）并同步壳，
    旧响应（成功或失败）一律忽略；最新选择保存失败时界面回到 `closeBehaviorEffective` 并把错误抛给调用方。
  - `frontend/src/services/desktop.ts`：`syncCloseBehaviorToShell` 串行化——每次同步排队执行，最后发起的那次
    最后到达，单次失败只记日志不阻塞后续同步；壳不会因为并发 IPC 的完成顺序缓存旧行为。
  - `frontend/src/views/settings/sections/RegularSection.vue`：失败回滚用 `closeBehaviorEffective`（真实生效值），
    不用页面上一帧的乐观值；失败提示沿用分区内的「保存关闭行为失败：<原因>」。
- 验证：
  - `frontend/src/stores/__tests__/closeBehaviorOrdering.test.ts`（受控 deferred 响应：连续切换、旧成功晚到、
    旧失败晚到、最新保存失败、加载晚到不覆盖新选择）；
  - `frontend/src/services/__tests__/desktopCloseSync.test.ts`（invoke 手动结算：不并发、最后值最后落地、
    单次失败不阻塞）；
  - `frontend/src/views/settings/__tests__/closeBehaviorSettings.test.ts`（设置页：连续切换后控件仍是最新值；
    失败提示明确且控件回到真实生效值）。
  - 上述用例在基线实现上是红的（store 5 条、desktop 2 条、设置页 1 条），修复后全绿；相邻既有用例
    （WindowChrome / uf-window-chrome-close / settings-content / settings-shell / ui store）全部通过。

## B. 没有实际作用的独立模型选择改为只读说明（F06）

- 逐项核对真实读取路径后的结论（依据是调用点，不是 `call_path` 文案）：
  - 真正参与调用（继续可选）：main-chat / general-chat（`resolve_main_target` → `resolve_shared_target`）、
    subagent（`tools/subagent_tool.py` 的 usage_assignment）、vision（`build_vision_adapter` →
    `resolve_alongside_target`）、memory-organize、knowledge-distill、entity-extract（`DERIVED_USAGE_BY_KIND`
    → `build_derived_adapters` / maintenance）。
  - 没有独立调用路径（改只读）：code（`resolve_usage("code")` 在 `backend/src` 没有调用点，写代码由日常对话
    的模型完成）、research（仅历史标签与 policy 回退）、`custom:<tag>`（`resolve_shared_target` 直接判 no_call_path）。
  - 沿用现有处理方式（改只读）：video / audio（没有模型调用路径）、embedding（`services/retrieval.py`
    走既有本地/远程嵌入方案，不读 `model_usages`）。
- 修复：
  - `backend/src/agent/credentials/usages.py`：把上述结论固化进 `delivery`（model / existing / unsupported）并加说明；
    `code` 从「与主对话同一路径」改为未接通。
  - `backend/src/agent/credentials/models.py`、`services/model_usage.py`、`api/model_routes.py`：
    单条与批量保存入口对不生效用途的新分配返回结构化 422（`code="usage_not_effective"` + 人话 detail）；
    「恢复跟随默认 / 清除历史覆盖」仍然允许；`list_usages` / catalog 增加 `assignment_effective`、
    `historical`、`historical_target`，历史覆盖行标注「历史配置（未生效）」。
  - `frontend/src/components/models/UsageAssignmentSection.vue`：不生效用途不再出现「指定模型」；显示
    「沿用现有处理方式 / 未接通独立调用路径」一句话说明；有历史覆盖时只读展示其目标并提供「清除历史覆盖」。
    QIO 原本的写代码、媒体处理与检索能力不变——关闭的只是无效的独立分配入口。
- 验证：`backend/tests/test_release_step2_bc.py`；前端 `ReleaseStep2BC.test.ts`、`ModelSections.test.ts`（同步为只读语义）。
  另同步了四处编码旧行为的既有用例（`test_model_catalog.py`、`test_release_step1_b/c/d.py`）：语义不变，
  只是把「先保存分配、后让连接/模型变坏」与「直接写历史行」按新规则构造。

## C. 模型候选按当前用途判断能力（F07）

- 原问题：候选只看目录里的 `usable`（与用途无关的一般可用性），所以看图用途能选中明确只有文字能力的模型。
- 修复：
  - `backend/src/agent/credentials/models.py`：catalog 支持按用途读取，给每个模型服务端算好的
    `usage_usable` / `usage_reason` / `capability_state`（supported / missing / unknown / not_required）；
    判断只来自已有能力声明与 `UsageSpec.requires_capabilities`，不按模型名猜、不联网、不发验证请求。
  - `backend/src/agent/services/model_usage.py`：单条与批量保存用同一个 `model_usable(..., usage_key=...)` 校验；
    明确缺少能力或连接不可用 → 拒绝整批（批量保持第一轮的整体成功/失败原子语义）；能力未知仍然允许保存。
  - `frontend/src/components/models/CandidatePicker.vue` + `UsageAssignmentSection.vue` + `services/modelCatalog.ts`：
    候选同时显示「一般可用」与「该用途能力已声明 / 不适用于该用途：缺少所需能力 / 能力未确认」；
    连接不可用的候选仍按第一轮原因禁用；`usageUsable=false` 的候选禁用并给出原因；能力未知仍可选。
- 验证：`backend/tests/test_release_step2_bc.py`（四类结论、批量原子失败、普通目录不算用途结论）；
  前端 `ReleaseStep2BC.test.ts`。同一模型在普通对话（`usable=true`）与看图（`usage_usable=false`）得到不同且合理的结论。

## D. 互动意图执行显示为清楚的操作记录（F10）

- 原现状：确认 / 批量确认 / 恢复 / 重试生成的内部工作文字（板面编号、提交编号、意图编号、来源、工具调用要求）
  被当作普通用户消息保存并原样显示。
- 修复（服务端事实 + 前端展示两层）：
  - `backend/src/agent/interactive/records.py`（新增）：`record_key = ie:<submission_id>:<排序后的意图集>`；
    写入事实（材料事实只来自真实绑定）与读取派生（7 档状态 + 中文标签、`record_head`、attempts 时间线、
    逐项状态、结果入口、诊断编号、`verified` / `verified_note`）。
  - `interactive/submission.py`：`start_interactive_turn` 把程序事实绑进 TurnContext（来源真实取值
    confirm / batch_confirm / recover / manual_retry / retry，展示层归一为 confirm / resume / retry）。
  - `services/turn_orchestrator.py`：互动 Turn 的 user 消息落 `content_type="intent_execution"` +
    `raw={"intent_execution": 事实}`；**content 原样保留**，模型上下文、授权版本、已完成部分保护、恢复信息不动。
  - `api/server.py`：`/api/session/messages`、`/api/fragments/{id}/messages`、`/api/session/context`
    对这类消息附带展示就绪对象；旧消息（没有元数据的）原样返回，不补写、不猜测、不隐藏。
  - `frontend/src/components/IntentExecutionMessage.vue`（新增）+ `MessageItem.vue`、`MessageStream.vue`、
    `services/api.ts`、`stores/session.ts`：默认一行「意图执行 | 意图名称 | 服务端状态 | 查看详情」，
    详情含处理范围、执行经过（每次尝试的来源/状态/失败原因）、逐项状态（批量）、结果入口、失败原因、
    诊断编号（等宽小字）；`record_head !== true` 不渲染（自动重试不刷屏）；`verified=false` 如实显示说明；
    数据只认服务端字段，不按文本前缀判断。沿用现有字体/间距/颜色 token 与 14px 圆角，不新增弹窗。
  - 用户确认时看到的意图名称直接复用（`board_intents.title`），没有新增模型调用起名。
  - 状态只依据真实记录（`board_intents` / `board_intent_turns` / `board_intent_retries` / `board_executions` /
    `turn_journal` / `board_submissions`）：没有真实完成证据不会显示「已完成」；无法核对时 `unknown` + 说明。
- 验证：`backend/tests/test_release_step2_d.py`（写侧事实、单条/批量/手动重试/自动重试同 record、缺失记录
  unknown + verified=false、旧消息、跨页 head、内部编号只在 diagnostics）；前端
  `IntentExecutionMessage.test.ts`、`MessageStreamIntentExecution.test.ts`。

### 意图执行记录展示示例（真实组件 + 真实 tokens 渲染）

- 默认收起（单条确认）：`意图执行 整理这组资料 已确认，等待处理 查看详情`
- 展开后（批量 + 部分完成）：处理范围（研究板面 / 整理这组资料、补充摘要 / 确认执行）、
  执行经过（第 1 次 确认执行 处理失败 10-29 10:00 模型未给出核验结果；第 2 次 重试 已完成）、
  逐项状态（整理这组资料 已完成 共 2 次尝试；补充摘要 处理失败 共 1 次尝试）、失败原因、结果入口、
  诊断信息（板面编号 / 提交编号 / 意图编号 / 轮次编号 / 记录标识）。
- 无法核对：状态「状态无法核对」+ 一句「这组执行记录已无法核对（原有记录已不可读）」，不显示已完成。
- 截图：`docs/verification-shots/r2-ui-consistency-20261029/d-intent-execution-wide-760.png`、
  `docs/verification-shots/r2-ui-consistency-20261029/d-intent-execution-narrow-380.png`。

### 视觉核对（改动区域宽窄窗口）

用真实组件 + 真实设计令牌在 headless Chrome 渲染改动区域截图（暗色），窄窗 380px：
`docs/verification-shots/r2-ui-consistency-20261029/` 下 B（用途分配只读组 + 历史覆盖）、
C（候选四类结论）、D（意图执行记录）各有宽/窄两张。核对结论：宽窄窗口均无横向溢出，
标题/范围/诊断值可换行，详情默认收起、展开后纵向排列；候选的禁用原因与「能力未确认」在窄窗可见。

## 定向验证汇总（本地）

- 后端：新增四项用例 + 相邻既有用例（模型目录/路由/用途解析/互动提交/执行/意图/恢复/附件/叙事/分页/会话）
  全部通过；Lead 另写了一组独立核对用例（不入库）：两个保存入口拦不生效用途、历史行可读且标未生效、
  清除覆盖允许、catalog 四类候选结论与保存校验一致、真实确认后历史接口返回的 intent_execution 对象
  （record_head / record_key / 标题 / 状态 / attempts.status_label / 诊断编号）、真实完成证据写入后状态才变已完成、
  普通用户消息仍是 text。
- 前端：`npx vue-tsc --noEmit` 通过；定向 vitest（模型组件、设置页、Message/MessageStream、WindowChrome、
  ui store、本轮新增用例）全部通过。

## 已知限制（如实记录，不在本轮扩展）

1. **直播首帧**：`TURN_START` 事件不携带结构化记录（仍只带 turn_id / message 摘要），前端没有为此新增取数或事件；
   意图执行记录在首次进入对话页、刷新、切换话题 / 「从这里继续」、向上翻历史、历史读取重试时出现。
   同一话题内确认后需要一次这样的重载才能看到记录；本轮明确不扩展实时通道（验收口径以刷新与历史回看为准）。
2. **批量确认后只重试其中一条**：重试的意图集是原子集的子集，按契约「不同意图集不得合并」会产生一条独立记录；
   只有意图集不变（例如单条自动重试）才在原记录里增加 attempt 并更新原记录。
3. **paused / cancelled / rejected** 不在七档内，显示为「处理失败」，真实原因放在逐项 `failure_reason`。
4. **星球「完整讨论记录」原始消息视图**（`MemoryTab.vue`）仍按原始消息展示，不在本轮授权文件范围；
   互动页与普通对话页两个入口共用同一记录渲染。
5. **调试接口** `GET /api/models/usage-resolve` 对不生效用途返回 `resolved=None`，但服务层 `supported` 仍按「已知用途」为真；
   该接口只用于调试展示，本轮不改动该文件。
6. 视觉核对用的是真实组件 + 真实令牌的局部渲染页（未起整应用、未 build），380px 只覆盖本轮改动区域，
   不做整套视觉验收。
7. 旧记录（本轮实现之前写入的、没有元数据的续作消息）原样展示；如需回填需要另开迁移，本轮不做。

## 状态

- 本地定向验证已完成；分支已推送远端（完整 SHA 与 CI 结果在交付记录中回填）。
- 未合并 main、未打包、未发布、未开始下一轮。
