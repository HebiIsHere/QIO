# 发布前第五轮：合并前集成验证（2026-10-29）

- 候选代码 SHA（本轮验证的明确代码提交）：`dc97b25ea3817c1ad5445959a7b758d2adf9ad61`（第四轮交付尖端）
- 工作分支：`fix/release-step5-integration-validation-20261029`
- 交付 HEAD：本文件所在的文档提交（完整 SHA 以推送回执为准）
- 版本号：0.2.10（未改动）；未合并 main、未打标签、未打包、未发布、未改更新源、未改 CI 工作流。

## 一、候选提交与 main 关系（实际核对结果）

| 项 | 实际值 |
| --- | --- |
| origin/main | `1b03541eedd2dcaa9b82d6102108d717c71c6fb4`（2026-10-25 22:23:36 +0800） |
| 候选交付起点 | `dc97b25ea3817c1ad5445959a7b758d2adf9ad61` = 远端 `origin/fix/release-step4-dead-code-cleanup-20261029` 尖端 |
| 祖先关系 | `git merge-base HEAD origin/main` = `1b03541e…`，候选是 main 的**严格后代**；`origin/main..候选` 313 个提交，`候选..origin/main` 0 个 |
| main 独立新增提交 | 无（0 个）；因此**不需要重放提交、不需要制造合并提交、不需要把 main 新提交纳入候选**，完整修复链原样保留 |
| 候选相对 main 的差异规模 | 986 个文件、+106939 / −32675 行（含前三轮修复、第四轮清理与既有分支链内容） |

所有验证针对 `dc97b25e`；第 4 项发现两个集成阻塞后做了最小必要修复（见第 4 项与「提交与差异」），修复后只重跑第 4 项与直接受影响项。

## 二、运行环境与真实启动

- 每项验证各自启动一套**隔离实例**：真实后端（uvicorn `agent.main:create_app`，每实例独立端口）+ 真实前端 dev server（vite，每实例独立端口与 `VITE_QIO_BACKEND_URL`）+ 本机扮演的 OpenAI 兼容假厂商；数据目录独立（`%TEMP%\qio-r5\<name>\data`，`--clean` 起步），**未使用用户日常数据**。
- 环境：Windows；Node v24.11.1；后端 venv Python 3.11.15（PATH 另有 3.10.7）；vite 6.4.3；真实界面用 Playwright + 系统 Edge 无头（`scripts/ui-catalog/lib.mjs`，真实 Vite 页面 + 真实后端 HTTP/SSE/SQLite 往返）。
- 假厂商：`scripts/verify_stream_provider.py`（本机扮演、支持真 SSE 分片/工具调用碎片/故障注入）及验证员在 `%TEMP%` 下的**临时扩展副本**（只扩展 `/v1/models` 列表、能力声明或按 prompt 生成工具调用，用于模型组合与看图能力用例）。**不是真实厂商**，不能据此证明任何真实厂商的可用性/兼容性/网络行为。
- 六项验证的界面结论均同时给出真实后端响应/数据库事实/假厂商请求台账作为交叉证据；只标注「界面点击」或只标注「API」的地方都已如实区分。


### 提交与差异（受测代码 / 修复 / 交付 HEAD）

- **受测代码 SHA**：`dc97b25ea3817c1ad5445959a7b758d2adf9ad61`。第 1、2、3、5、6 项以及第 4 项主链路/重试恢复的验证结论都针对这个提交。
- **第 4 项最小必要修复提交**：`d4de01a40552425a5ead9d422af5fd6a467d60d0`（8 files changed, +520 / −10：产品代码 5 文件 +108 / −10，新增测试 3 文件 +412）。产品代码差异仅限第 4 项两个阻塞所需：`backend/src/agent/interactive/records.py`（结果入口读取侧回退到 `board_operations` 账本）、`backend/src/agent/interactive/submission.py` 与 `backend/src/agent/services/turn_orchestrator.py` / `backend/src/agent/api/interactive_store.py`（提交轮 user 消息 `board_submission` 类型与程序事实，模型可见正文不变）、`frontend/src/components/MessageItem.vue`（不把该类型渲染为普通用户气泡），加必要的后端/前端用例；实际 diff 以该提交为准。未改版本号、未加迁移、未改权限规则、未改视觉、未改工作流。
- **修复后重跑范围**：只重跑第 4 项（失败项）与直接受影响测试，并对修复后的前端代码重跑一次必要的生产构建（见第五节）；第 1/2/3/5/6 项未重跑，其结论仍对应 `dc97b25e`（这些项不经过被修改的代码路径）。
- **交付 HEAD**：本文件与 `docs/status.md`、`docs/verification-shots-step5/` 的文档提交（完整 SHA 以推送回执为准）。交付 HEAD 相对受测代码的差异 = 上述最小修复 + 文档/截图；除修复提交外无其它产品代码改动。

## 三、六项集成清单逐项证据

### 1. 启动与入口 — PASS

实际步骤：空数据目录（`--clean`）启动前后端与假厂商 → Playwright 真实界面从 0 走完现有 7 步引导（欢迎 → 连接模型：选「其他/自定义服务」、填假厂商地址与假 Key、保存后由后端真实调用验证通过 → 认识你 → 偏好 → 目标 → 追问 → 核对并完成）→ 进入主页；对话 ↔ 互动 ↔ 设置真实切换；刷新重开不再弹引导。

实际观察（交叉证据）：`GET /api/onboarding/status` 返回 `done/has_credential/has_name/has_content/wizard_seen=true、show_wizard=false`；SQLite `credentials` 1 条（`verify_state=verified`、`is_default=1`）、`settings.onboarding.name` 已写；界面实际发出 `POST /api/credentials → /api/onboarding/followups → /api/onboarding/submit → /api/onboarding/complete` 全 200；路由切换截图与页面文本均正常，`consoleErrors=[]`。

迁移保留与二次打开：用 origin/main 的**真实旧代码**（detached 临时 worktree，`1b03541e`）在独立数据目录写入 1 条已验证凭据、默认话题、4 条消息 / 2 个已完成 turn（旧库 `schema_version max=70`）；停旧后端后用候选后端打开同一目录：升级到 74（新增 `credential_models` / `model_usages` 等），话题 id、4 条消息 id+content、2 个 turn 终态、凭据 id/verify_state/tags **逐字保留**；停掉再二次重开仍为 74 且消息与终态一致（幂等）。未使用用户日常数据；临时 worktree 验证后已移除。

### 2. 普通对话 — PASS（附两条既有语义说明）

实际步骤（同一会话连续完成）：真实界面发送 → 流式显示 → 界面「停止」按钮 → 注入一次厂商 500 失败 → 点该失败轮自己的「重试」→ 刷新重开历史。假厂商脚本对「用户要求解析」先行空分片，正式回答分片以 `[[QIO:ANSWER]]` 声明开头（这是后端把正文按正式回答增量发布的条件）。

实际观察（交叉证据）：

- 流式：假厂商 4 个分片发送时刻 …119.479/120.680/121.932/123.081，`stream_end` …124.282；首个正文 `ASSISTANT`（`streaming=true`）到达 …120.740，**比厂商流结束早 3.542 秒**；流式中截图时厂商只发出 2/4 分片且 `stream_end` 次数为 0、界面只显示第一段；事件序列 `ASSISTANT streaming=true ×3（累计快照）→ streaming=false 收尾校准 → TURN_END`。
- 停止：界面停止按钮 `button.send-btn[data-mode=stop]`（aria-label=停止）→ `TURN_END status=cancelled、reason_code=user_stopped、stopped_by=user、actions=["retry"]`，DB `turn_facts` 同值，界面「已停止」+ 原因并保留已收到部分正文。
- 失败：厂商 500 → `TURN_END failed / provider_error`（reason 含 `500 - {'error': {'message': 'boom'}}`），界面顶部错误条 + 该轮「已失败 · 总耗时 189 毫秒」+ 真实原因 + 重试；点重试后新 turn `completed`，界面显示成功回答。
- 历史重开：`page.reload()` 后 `/api/session/context` 前后 message id/content/turn_facts 完全一致（completed / cancelled+user_stopped / failed+provider_error / completed），界面仍显示停止原因与 500 原因。
- 异常：仅注入的 500 traceback 与用户主动中断导致的 provider `ConnectionAbortedError`（预期）；`consoleErrors=[]`。

如实说明（非缺陷）：① 停止轮的部分正文服务端本不落库，重开后只保留「已停止 + 原因 + 重试」终态；② 重试是以同一用户消息**新建一轮**（失败轮用户消息保留），历史里该用户消息出现两次，这是既有重试语义。

### 3. 模型组合 — PASS

实际步骤：建一条连接（假厂商，`key_07a6f296e286`）→ 发现同一连接下 4 个模型 → 用途分配：`main-chat`/`general-chat`=fake-model，`memory-organize`=fake-model-pro、`knowledge-distill`=fake-model-ultra、`entity-extract`=fake-model-bg（`POST /api/models/usages` 原子写入）；把记忆封块阈值调为 1 个 turn 使真实一轮结束即触发后台派生工作；发真实 turn；再发一轮慢速 turn 并在分片发送期间修改全部用途分配，随后再发新 turn。

实际观察（交叉证据）：

- 首轮真实 turn：假厂商 `/__log` 主对话调用 model=fake-model（非流式解析 + stream=true 主调用 + 回答），后台三条派生调用分别命中 fake-model-pro / fake-model-ultra / fake-model-bg；DB `derived_tasks` 三条 completed、`fragments.summary_model=fake-model-pro`；`GET /api/models/catalog` 五个用途的 resolved/effective 与 DB `model_usages` 一致（主对话合并行 effective 归到 general-chat 属已知合并语义，实际模型相同）。
- 操作期间改配置：慢速 turn 进行中把 4 条用途整体改到新模型（接口 ok=true）；进行中的调用仍全部是旧 main 配置 fake-model；该轮结束后开始的派生批命中**新** memory 目标 fake-model、新 entities 目标 fake-model-ultra，旧目标 pro/bg 未再出现（该片段 summary_model=fake-model）；改后新 turn 主对话三连调用全部 fake-model-pro；catalog 与 DB 均为新分配。
- 限制：假厂商默认回复并非总是合法 JSON，个别后台摘要/实体任务因此 pending/failed（测试替身输出问题，与「本批用哪套配置」的取证无关）。

### 4. 互动意图 — 修复前 FAIL（两个子项），最小修复后复验见下

实际步骤（真实界面 + 真实后端，逐步标注）：API 建板面与材料卡 → 真实界面点「提交给 QIO」→ 真实互动主循环 turn → 假厂商工具碎片调用 `board_propose_intents` → 意图 `awaiting_confirmation` → 界面点「确认这一条」（`POST /api/interactive/intents/{id}/confirm`）→ 真实续作 turn 调 `board_create_card` + `board_finish_intent` → 意图 completed、结果卡真实写入板面、`board_operations` applied；切回普通消息区读取记录；再用补充材料走第二个意图的「部分完成 → 恢复预览 → 确认恢复」路径。

主链路实际观察：意图状态/结果卡/板面 `GET /state`/`/results` 均真实；`/api/session/messages` 的 `intent_execution` 记录为 `{title:整理本周见闻, status:completed, status_label:已完成, record_head:true}`；普通消息区显示「意图执行 / 整理本周见闻 / 已完成 / 查看详情」，默认收起、可展开（处理范围/执行经过/诊断信息，内部编号只在诊断信息），整页无「【互动意图续作】」内部文字；恢复路径后意图 completed(version=2)、attempts 时间线含 confirm/自动 retry/ resume，`/results` 有第二张真实结果卡。

修复前发现的**两个集成阻塞**（复现与定位已记录，修复前如实 FAIL）：

1. **结果定位不可达**：确认→board 工具路径只写 `board_operations`（applied），未把结果汇总写回 `board_intents.applied`；而记录读取侧只从 `board_intents.applied` 派生结果入口，导致 `intent_execution.result_entry=null`、详情无「结果入口」按钮，无法从执行记录定位回真实板面/结果卡（同期 `/results` 有真实结果卡）。
2. **提交轮内部绑定文字作为普通用户消息出现**：提交轮 user 消息落库 `content_type=text`，正文原样含「【互动板面提交】board_id=… submission_id=…」等内部编号与程序文字，出现在普通消息区用户气泡里。

最小必要修复（本轮唯一产品代码改动，详见「提交与差异」）：① 读取侧在 `records.py` 中当 `board_intents.applied` 为空时回退到权威账本 `board_operations` 的 applied 创建操作派生计数（只读展示，不写库、不改 schema/权限/前端；真实没有结果仍返回 None，不编造入口）；② 提交轮 user 消息新增单一定义的 `board_submission` content_type 并携带程序事实（模型可见正文一字不改），前端**不把该类型渲染为普通用户气泡**（不新增视觉组件、不改布局/样式；历史旧消息原样兼容）。

修复后复验（只重跑失败项与直接受影响项）：
修复后在同一隔离实例、重新 `--clean` 数据上重跑第 4 项真实界面全流程（只重跑失败项与直接受影响项）：

- `result_entry` 非空：`{kind: board, label: 板面结果：1 张卡片, target: board_default}`，`intents[0].result_ref` 同样非空；记录详情出现「结果入口 · 板面结果：1 张卡片」，点击后 URL 切到 `#/interactive` 且真实板面渲染（`[data-im=interactive-stage]`=1，`/results` 返回真实结果卡 `c_12b925df1c59` / 「本周见闻整理」）。
- 普通消息区 DOM 中「【互动板面提交】」与「【互动意图续作】」均**不出现**（`bodyHasSubmissionBindingText=false`、`bodyHasExecutionBindingText=false`）；记录头部仍为可读的「意图执行 / 整理本周见闻 / 已完成 / 查看详情」，详情可展开（处理范围 / 执行经过 / 结果入口 / 诊断信息）。
- `/api/session/messages` 中提交轮 user 消息 `content_type=board_submission`，content 仍保留 `【互动板面提交】`、`board_id`、`submission_id`（模型上下文与提交号一字不改）。
- 恢复路径回归：部分完成 → 恢复预览 → 确认恢复 → 意图 completed(version=2)，记录 attempts 含 confirm / 自动 retry / resume，三条已完成记录都出现结果入口。
- 控制台无 error。

### 5. 设置与凭据 — PASS

实际步骤（真实设置页 + 真实后端）：设置页「常规 → 输出速度」逐次操作并用 `GET /api/settings/ui` 作实际保存值对照；用 Playwright route 注入一次 500 与一次延迟制造失败/连续修改；设置页「模型与服务 → Key 接入与管理 → 添加 Key」真实表单保存（第一次 route.fetch 真实落库后 abort 制造「服务端已保存、客户端丢应答」，未改内容再点保存），并用拦截制造自动获取模型失败后再单独「重新获取」；改设置后刷新页面重开核对。

实际观察（交叉证据）：

- 设置保存：初始 50（界面「中（50 字/秒）」）；选 75 → PUT 200 `{"typewriter_cps":75}`、GET=75、界面 75；刷新后 GET=75、界面=75（持久化一致）；拦截 PUT 500 后选 25 → 界面回滚 75、错误可见（含注入的 500 文本）、GET 仍 75；连续 25→75（首请求延迟 500ms）等待期间界面保持 75、两次请求依次发出（同一设置串行）、最终界面=GET=75；连续 25 成功 + 75 失败 → 回滚 25、GET=25、错误可见。
- 输出速度固定清单：定向 vitest `outputSpeedSettings.test.ts` + `ReleaseStep3Settings.test.ts` exit 0，29 项全过（六项固定清单由测试覆盖）；真实界面另验证「保存→刷新重开一致」。
- F13 凭据未改内容重试：第一次 POST 真实落库后 abort → 界面 Failed to fetch、`GET /api/credentials` 已有 1 条；未改内容再保存 → 两次 `client_request_id` 相同（`cred-mvtgy7gsx6lib6`）、第二次响应 `idempotent=true`、同一 `key_id=key_cb4137c337cf`、凭据仍 1 条。
- F12 获取模型失败结束等待并可单独重试：自动 discover 被 abort → 「连接已保存，但获取模型时网络异常…可稍后重试获取」，不再停在「正在同步」；恢复后点「重新获取」→ 200、已获取模型列表，凭据数/key_id 未变（没有重新保存连接）；业务失败（200 + error）路径同样结束等待并可重试。
- 持久化：改设置（50→75）后刷新页面重新打开设置页，GET 与界面均为 75。

### 6. 搜索与候选 — PASS

实际步骤：用真实 API 创建 18 条同类知识（关键词「集成验证样例」）→ 真实界面从对话页打开记忆星球 → 管理区全局搜索输入关键词；另用本机扮演的第二个假厂商（明确声明两个模型均无 vision）建连接并发现模型，真实界面打开「为看图/视觉指定模型」。

实际观察（交叉证据）：

- 搜索：体面「共 18 项结果」、分组「知识（18）」、首批 8 条（`data-search-kind` 全为 knowledge）；「查看更多（还剩 10 条）」→16 条 → 第 9 条（真实 id `kn_ab0c018f4b4c`）出现并可点，点开后知识详情 `.k-id`/`.k-detail-content` 与该条逐字一致；返回搜索保留关键词与展开状态；再展开 →18 条、按钮消失，最后一条（`kn_c2f6dd68dac9`）可见并点开一致；控制台 0 错误。
- 看图候选：`GET /api/models/catalog?usage_key=vision` 两模型 `usage_usable=false`、`capability_state=missing`、原因「缺少 vision」；真实 UI 候选中两模型 disabled + 「不适用于该用途：缺少所需能力」；对照「日常对话」候选两模型 enabled（证明按用途判定，不是连接不可用）；后端 `POST /api/models/usage`(vision) → 422 `usage_target_unusable`。
- 未接通用途只读：界面「不单独生效的用途」5 行（写代码/研究/视频/语音/向量），badge 标「未接通独立调用路径 / 沿用现有处理方式」，组内 0 个「指定模型」按钮；catalog 中 `assignment_effective=false`；后端对 video 分配 → 422 `usage_not_effective`。

## 四、定向测试（按项，全部为实际命令与实际结果）

| 项 | 实际命令 | 实际结果 |
| --- | --- | --- |
| 1 启动/迁移 | 后端 `pytest tests/test_integ_a_migration_upgrade.py tests/test_migrations_append_only_verify.py tests/test_settings_migration_models.py` | exit 0，26 passed（含 main-51 来源库升级、append-only、对象级复核、二次启动幂等） |
| 2 普通对话 | 前端 `vitest run` 12 个文件（AnswerContainerStreaming / answerStreaming / streamingDeltas.verify / TurnProcess / turnEnd / turnFactsReason / CancelExactTarget / recovery-a-integration-f2-retry-entry / historyPagination / FailedTurnRestore / turnProcessState / turnFactsRestore） | exit 0，12 files / 74 passed |
| 2 普通对话 | 后端 `pytest tests/test_turn_cancel_semantics.py tests/test_turn_identity_events.py tests/test_turn_lifecycle_protocol.py tests/test_turn_journal_facts.py tests/test_session_pagination.py` | exit 0，22 passed |
| 3 模型组合 | 后端 `pytest tests/test_release_step1_a.py tests/test_release_step1_b.py tests/test_release_step1_c.py tests/test_release_step1_d.py tests/test_release_step2_bc.py` | exit 0，75 passed（含 step1_b「暂停第一种连接构建→改用途→放行」确定性用例） |
| 4 互动意图（修复前） | 后端 `pytest tests/test_p3_c_intents_api.py tests/test_p3_d_intent_resume_retry.py tests/test_r2b_intent_records.py tests/test_release_step2_d.py tests/test_p3_b_tools.py` | exit 0，77 passed |
| 4 互动意图（修复前） | 前端 `vitest run` IntentExecutionMessage / MessageStreamIntentExecution / IntentExecutionRefresh / IntentExecutionRefreshQueue / i2v2-a-intent-flow | exit 0，5 files / 43 passed（本机本次 i2v2-a-intent-flow 6 项也通过，与文档中 Windows 既有失败记录不一致，按实际观察记录） |
| 4 互动意图（修复后） | 后端 `pytest` 10 文件（新增 test_release_step5_result_entry / test_release_step5_submission_message + test_release_step2_d / test_r2b_intent_records / test_p3_b_tools / test_p3_c_intents_api / test_p3_d_intent_resume_retry / test_p3_d_finish_ledger / test_p3_a_submission_flow / test_interactive_submission） | exit 0，142 passed（Lead 独立复跑同样 exit 0）；前端 `vitest run` 11 文件（释放新增 ReleaseStep5SubmissionMessage + MessageItem 系列 / MessageStream / IntentExecutionMessage / IntentExecutionRefresh* / i2v2-a-intent-flow）→ exit 0，11 files / 111 passed（Lead 独立复跑同样 exit 0） |
| 5 设置与凭据 | 前端 `vitest run src/views/settings/__tests__/outputSpeedSettings.test.ts src/views/settings/__tests__/ReleaseStep3Settings.test.ts` | exit 0，29 passed（固定清单六项由测试覆盖） |
| 5/6 设置/搜索/候选（后端） | 后端 `pytest tests/test_release_step3_c.py tests/test_release_step2_bc.py tests/test_release_step2b_bc_repair.py tests/test_interactive_vision.py -o addopts="" -q` | exit 0，65 passed |
| 6 搜索与候选 | 前端 `vitest run release-step3-search / mp-a-global-search / KnowledgePanel / KnowledgePanel.visibility` | exit 0，4 files / 37 passed |

说明：只按项跑定向文件，未运行前后端全量测试；未触发 CI、未重跑任何工作流。

## 五、前端生产构建与仓库级检查

本轮的构建在**两个代码状态**上各跑了一次，分别记录、不互相冒充：

1. **候选代码 `dc97b25e` 的构建（基线，先做）**：`cd frontend; npm run build`（= `vue-tsc --noEmit && vite build`）exit 0；类型检查在 vite 之前执行且无输出（通过）；vite 6.4.3，771 modules transformed，`✓ built in 7.86s`；`frontend/dist` 425 个文件 / 19,535,090 字节（约 18.6 MiB）；4 条非致命警告（3 条「模块同时被动态/静态导入、不会单独分块」+ 1 条 chunk >500kB），`error/ERR!/Rollup failed` 命中 0；构建前后 `git status --porcelain` 为空（`dist/` 被 `.gitignore` 忽略）。
2. **修复后代码 `d4de01a4` 的构建（交付代码的必要构建，后做）**：同一命令 exit 0；`vue-tsc --noEmit` 先执行且无输出（类型检查通过）；vite `✓ built in 8.68s`；`dist` 425 个文件 / 19,535,123 字节；同样只有上述 4 类非致命警告，`error/ERR!/Rollup failed` 命中 0；工作树状态不变（`dist/` 仍被忽略）。

为什么跑两次：第 4 项最小修复改变了前端代码，交付物必须对应修复后的代码；第一次构建只用于候选代码基线，不能把它的结果写成修复后代码已验证，反之亦然。两次都只构建前端、未触发 CI、未改工作流。

仓库级检查（实际命令与实际结果）：

- `python scripts/check_docs.py` → exit 0：「文档一致性检查通过（106 个里程碑条目）」。文档（本文件 + `docs/status.md`）同步完成后按同一命令复跑，exit 0、条目数仍为 106。
- `python scripts/check_docs.py --selftest` → exit 0：6 个场景全部符合预期（能红能绿）。
- `python scripts/licenses/check_third_party.py` → exit 0：锁文件组件 914 个、声明覆盖 918 条、许可证 42 种；`--selftest` → 7/7 通过。
- `python scripts/release_gate.py --selftest` → exit 0：11 个场景全部符合预期（健康产物全绿；哈希不符 / sidecar 过期 / 卸载钩子缺失 / manifest 串包等能变红）。**该自检只代表检查机制本身能红能绿，不代表已有安装包或真实厂商已通过验证。**
- 额外如实记录（非必做）：真实跑了一次 `python scripts/release_gate.py`，exit 1，15 PASS / 1 FAIL(sidecar：工作树缺 `frontend\src-tauri\binaries`) / 1 SKIP(内置模型资源未生成)。它校验的是仓库**既有旧包** `QIO_0.2.10_x64-setup.exe`（build manifest commit=`764dff14`，2026-10-21），不是本轮候选；`binaries`/`resources` 都是 `.gitignore` 内的打包产物，未执行 tauri 打包的工作树必然缺失 → 属「当前工作树未打包」的预期状态，但**本轮不能给出「可发布」结论**，安装包级验证属后续原生发布验证。该次运行向 `docs/releases/release-history.jsonl` 追加的记录已立即还原，最终工作树干净。
- 仓库状态：`git rev-parse HEAD`（验证/修复/构建各时点）与修复提交 `d4de01a4` 一致；`git status --porcelain` 在提交前仅含本轮授权改动，提交后干净。

## 六、异常、如实说明与已知限制

1. 停止轮的部分正文服务端本不落库：停止后当次界面保留已收到部分，刷新重开只保留「已停止 + 原因 + 重试」终态。这是既有语义，本轮未改。
2. 重试以同一用户消息新建一轮，历史中该用户消息出现两次、重试轮有自己的回答；这是既有重试语义，不是重复发送 bug。
3. 假厂商是**本机扮演的端点**：`scripts/verify_stream_provider.py` 实际 `/v1/models` 只返回 `verify-model`；模型组合与看图能力用例使用了验证员在 `%TEMP%` 下的临时扩展副本（只扩展模型列表/能力声明/按 prompt 生成工具调用）。凡用到假厂商的结论只能读成「QIO 自己的链路对」，不能证明任何真实厂商。
4. 本仓库真实设置接口按分区（`/api/settings/ui`、`/memory`、`/maintenance`、`/tools`、`/loop`、`/search`、`/computer`、`/trace`），裸 `GET /api/settings` 为 404；验证全部按真实分区接口取证，未把 404 当产品缺陷。
5. 设置跨进程重启持久化本轮用「刷新页面再读」验证（未关闭后端进程重启复读），后者保持未验证。
6. 第 4 项「记录在互动页底部会话面板、不切页也自动出现」未能从脚本单独观察到（确认后 chatRecords=0，面板只到提交轮）；切回普通消息区按历史读取后状态为 completed。该项标为未单独验证。
7. 失败/停止路径中的 500 traceback、provider `ConnectionAbortedError`、界面 500 / `net::ERR_FAILED` 均为验证脚本注入或用户主动中断的预期现象；正常路径 `consoleErrors=[]`。
8. `i2v2-a-intent-flow.test.ts` 在 `docs/release-step1-model-reliability-20261029.md` 中记录为「本机 Windows 基线既有失败」；本轮实际运行 6 项全部通过。按实际观察记录，不沿用旧结论。
9. 真实发布闸门额外跑了一次真实操作（非必做）：`python scripts/release_gate.py` exit 1，15 PASS / 1 FAIL(sidecar：「没有 sidecar 目录 frontend\src-tauri\binaries」) / 1 SKIP(models 资源未生成)。它校验的是**仓库既有旧包** `QIO_0.2.10_x64-setup.exe`（build manifest commit=764dff14，2026-10-21），不是本轮候选；`binaries`/`models` 都是 `.gitignore` 内的打包产物，未执行 tauri 打包的工作树必然缺失 → 属「当前工作树未打包」的预期状态，但**本轮不能给出「可发布」结论**，安装包级验证留给后续原生发布验证。该次运行会向 `docs/releases/release-history.jsonl` 追加记录，已立即还原，最终工作树干净。
10. 除第 4 项的最小必要修复与两个测试文件外，本轮未改任何产品代码；版本号、schema 迁移、权限规则、视觉与工作流均未改动（见「提交与差异」）。

## 七、未验证事项（后续原生发布验证）

- 真实厂商端点（OpenAI / DeepSeek / Anthropic / 聚合服务等）的真实调用、兼容性、限流与网络行为——一律未验证；本轮所有模型调用都打到本机扮演的假厂商。
- Windows 安装包/原生发布链路：`tauri build`、签名、安装、卸载、更新源、发布资产——未构建、未验证；真实发布闸门对既有旧包仍有 sidecar FAIL（未打包工作树）。
- 真实服务（发布后的服务端）验证——未做。
- CI 同版本环境（Node 20 / Python 3.12）——未触发 CI，本轮在 Node 24.11.1 + Python 3.10.7(PATH)/3.11.15(venv) 上验证。
- 设置跨进程重启持久化、同页不切页自动出现记录、多实例并发、真实桌面壳交互——未验证/未单独验证。
- 前后端全量测试与全量构建外的验证——未运行（本轮只跑定向测试与一次必要生产构建）。


## 七、证据位置

本轮截图证据已随分支提交在 `docs/verification-shots-step5/`（共 23 张，全部来自隔离实例的真实界面，假厂商用本机扮演的端点、数据为合成样本）：

| 文件 | 内容 |
| --- | --- |
| `01-onboarding-welcome.png` ~ `03-onboarding-review.png` | 空数据实例的现有引导（欢迎/连接模型/核对并完成） |
| `04-home.png` ~ `06-settings.png` | 引导完成后主页、互动页、设置页（对话/互动/设置切换） |
| `07-chat-streaming-mid.png` ~ `11-chat-history-reopen.png` | 流式中（仅第一段已显示）、停止、500 失败、重试成功、历史重开 |
| `12-settings-save-success.png` ~ `17-model-discover-retry.png` | 设置保存成功/失败/连续失败、凭据丢失应答后幂等重试、获取模型失败与单独重试成功 |
| `18-search-first-batch.png` ~ `21-search-last-detail.png` | 18 条同类搜索首批、第 9 条可见、第 9 条详情、最后一条详情 |
| `22-vision-candidates-disabled.png`、`23-readonly-usages.png` | 无看图能力的模型在看图候选中被禁用、未接通用途只读 |
| 第 4 项截图 | `24-intent-awaiting-confirm.png`（意图待确认）、`25-intent-execution-done.png`（确认并执行后）、`26-intent-record-collapsed.png` / `27-intent-record-expanded.png`（修复前记录收起/展开）、`28-intent-result-entry-fixed.png`（修复后详情出现结果入口）、`29-intent-result-located-fixed.png`（点击结果入口定位到真实板面） |

原始 JSON/事件/日志证据（假厂商 `/__log` 与 `/__timeline`、SSE 事件时刻、DB 快照、pytest/vitest 输出）保存在各验证员的临时目录（`%TEMP%\qio-r5\conv|mi|ss`、`%TEMP%\qio-r5\build-checks`），属可再生产物**未入库**；本文件记录了对应命令、关键字段与实际观测值，可按同一步骤重放。

## 八、状态

- 六项清单结论：第 1、2、3、5、6 项 PASS；第 4 项主链路（确认→执行→记录自动更新）与重试/恢复 PASS，但发现「结果定位不可达」与「提交轮内部文字作为普通用户消息」两个集成阻塞；已按最小必要修复处理并只重跑第 4 项与直接受影响项（见第 4 项与「提交与差异」）。
- 本轮分支已推送远端；受测代码 SHA、修复提交 SHA 与交付 HEAD 见文末「提交与差异」；远端完整 SHA 以推送回执为准。
- 未合并 main、未打标签、未改版本号（0.2.10）、未制作/发布正式安装包、未改更新源；未触发 CI。
- 后续原生发布验证：版本确认 → Windows 安装包构建/签名/安装/卸载/更新 → 真实厂商与真实服务验证；本轮结果不能冒充这些通过。