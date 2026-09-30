# QIO 项目状态

**本文只回答一个问题：现在真的做到哪了。**

- 设计意图与分层 → `docs/architecture.md`
- 安装与运行 → `docs/SETUP.md`
- 协作约定 → `AGENTS.md`

最后核对：2026-09-12（`main` 分支）。核对方法见文末。

---

## 状态口径

| 状态 | 含义 |
| --- | --- |
| `completed` | 有实现、有自动化测试覆盖主路径，后续可以依赖 |
| `partial` | 主路径可用，但存在本节列出的已知限制 |
| `active` | 正在改，行为或接口可能变化 |
| `planned` | 只有设计，没有实现 |

**给后续编码 Agent 的硬性规则**

1. 不要在本文件写测试数量、事件数量、表数量这类会迅速过期的硬编码数字。要真实数字就去跑命令。
2. 一个能力只有在「代码 + 测试」都在仓库里时才允许标 `completed`。
3. 改完实现要同步改本文件。状态与代码冲突时，以代码为准，并立刻修正本文件。
4. 本文件与 `architecture.md`、`README.md` 的里程碑状态必须一致；`scripts/check_docs.py` 会检查这一点。

---

## 产品里程碑

### M0 — 骨架与事件协议

- **Status：** completed
- **Implementation：** `backend/src/agent/main.py`（`create_app` 工厂）、`api/server.py`（路由）、`api/events.py`（事件信封与类型）、`api/bus.py`（订阅扇出 + 重放缓冲）
- **Tests：** `backend/tests/test_events.py`、`test_events_bus.py`、`test_api_routes.py`
- **Known limitations：** `POST /api/events/test` 只在开发模式（`QIO_DEV_INSECURE=1` 或 `QIO_ENABLE_TEST_EVENTS=1`）注册，生产构建里这条路由根本不存在，且同样要求会话认证；事件类型集合会随功能增长，数量不写死在文档里。
- **后续依赖：** 无（其余里程碑都建立在这一层上）。

### M1 — 存储层

- **Status：** completed
- **Implementation：** `storage/schema.py`（顺序迁移，当前 schema 版本见 `SCHEMA_VERSION`）、`storage/migrate.py`、`storage/db.py`（WAL）、`storage/archive.py`（冷归档）、`storage/settings.py`
- **Tests：** `backend/tests/test_storage.py`、`test_settings.py`
- **Known limitations：** 归档与维护由后台任务触发，默认不是常驻高频；SQLite 单写者，写入串行化由后端负责。
- **后续依赖：** M2/M6/M7/M8 的表都通过迁移追加；改动 schema 必须新增一条迁移，不能改历史迁移。

### M2 — 凭据层（BYOK）

- **Status：** completed
- **Implementation：** `credentials/store.py`（密钥进 keyring，元数据进 SQLite）、`credentials/policy.py`（按标签解析 + 快照）、`credentials/providers.py`（厂商预设的唯一来源：名称 / 协议 / 地址 / 建议模型 / 类别）、`services/verify.py`（只对**用户选定的地址**做一次真实调用验证）
- **Implementation（2026-09-15 身份边界）：** `endpoint` 视为凭据的**安全身份**而不是普通元数据：变化必须重新输入 secret 并显式确认（`confirm_reconfigure=true`），否则 HTTP 层与 store 双层拒绝；默认只允许 HTTPS，明文 HTTP 仅限 loopback 本地 provider。主 Agent Loop 的凭据解析改为 **`main-loop` 标签优先**（以前排序把专项凭据排在前面，一个 `vision` Key 会被主循环静默拿去用），专项标签只在没有 main-loop 可用时回落。
- **Implementation（2026-09-21 后端可用性）：** 凭据后端的解析改为**惰性**：`CredentialStore` 构造期不再探测系统 keyring，
  第一次真正读写密钥时才解析并缓存。语义上读写不对称是有意的 ——
  **读路径**（`get_secret` / `get_default_secret`）在这台机器没有可用后端时如实返回 `None`
  （应用本来就有「当前没有可用凭据」的降级路径），**写路径**（存 / 轮换 / 删除）仍然大声抛错，
  绝不静默降级到 no-op 后端。动机：headless 环境（CI 的 ubuntu runner、容器、无 SecretService 的机器）
  没有任何可用后端，而旧的构造期抛错会让应用工厂与大量测试在启动阶段直接失败。
- **Implementation（2026-09-28 保存与验证分离）：** 删除「把同一把 Key 依次探测十几家候选厂商」的自动识别模块：
  厂商由用户明确选择，Key 前缀只做**本地**提示，验证请求只发往选定的那一个地址，
  失败也不会转投别家。`credentials` 表新增 `kind` / `verify_state` / `verified_at` / `verify_error` / `is_default`
  （迁移 21，只追加）：历史凭据回填 `legacy`（按老行为视为可用，不误标未验证、不停止使用），并把第一条可用的
  主对话凭据回填为默认项。新建凭据缺省用途 = 主对话；显式提交空用途返回可理解的中文错误（不静默覆盖）。
  保存 = 必要校验 + 安全写入 + 一次自动可用性验证，两者分别反馈（「已保存，模型可用」/「已保存，尚未通过验证」）；
  写入失败不留半条记录。未通过验证的凭据不进自动选择（`CredentialPolicy.resolve` 只认 `verified` / `legacy`），
  因此首次引导也不会误判为已配置完成。显式默认项只影响**合法候选之间**的排序，不绕过停用、撤销、预算与用途限制；
  第一条验证可用的主对话凭据自动成为默认，后续新增不替换（重试与换钥都作用在同一条记录上，带 `client_request_id`
  的重复提交按确定性标识去重）。验证与正式对话共用同一套协议判断（OpenAI 兼容走 `probe_adapter`，Anthropic 走
  `probe_anthropic`），「拿到模型列表」不再被当作「模型可用」。
- **Tests：** `backend/tests/test_credentials.py`、`test_identify.py`、`test_credential_verify.py`、`test_tool_credentials.py`
- **Tests（2026-09-15 追加）：** `test_credential_identity.py`（只改 endpoint 必须被拒、https 默认、loopback 例外）、`test_credential_routing.py`（main-loop 优先、专项凭据只作回落、无匹配用途不得拿别的标签顶上）、`test_credential_endpoint_api.py`（HTTP 层同一套规则）
- **Known limitations：** 真实凭据读写只在 Windows 凭据库上验证过（headless 环境没有可用后端时，
  读路径返回「无凭据」、写路径报错）；预算以 token 计数为主（界面也按 token 显示，不再出现人民币符号）；
  预设里的「建议模型」只是推荐值，是否真的可用由保存后的一次实际调用决定，因此没有可靠默认模型的服务
  （聚合/自定义）要求用户自己选一个模型；`用量上限` 按「进 + 出」合计与真实计量对齐，运行期每次模型调用
  都会累计（主循环、子 agent、后台维护共用 `credentials/usage.py` 的同一份归因），界面把进 / 出分开显示。
- **后续依赖：** M3 适配层、M10 子 agent、embedding 选档都从这里取 Key。

### M3 — 模型适配层

- **Status：** completed
- **Implementation：** `adapters/base.py`（内部 `Completion` / `ChatMessage` / `ToolCall` 契约）、`adapters/native.py`、`adapters/text.py`、`adapters/anthropic.py`、`adapters/probe.py`、`adapters/model_context.py`、`adapters/errors.py`（内部错误分类）
- **Tests：** `backend/tests/test_adapters.py`、`test_anthropic_adapter.py`、`test_adapter_contract.py`、`test_adapter_errors.py`、`test_model_context.py`
- **Known limitations：** `unsupported` 档默认拒绝启动，需要用户显式打开强制继续；`text` 档的能力弱于 `native`（不切换话题）。
- **后续依赖：** M4 主循环、M5 选择器、M9 上下文组装都消费这一层的内部模型，不直接接触任何厂商 SDK 对象。

### M4 — 主循环

- **Status：** completed
- **Implementation：** `core/loop.py`（PLANNING → TOOL_EXEC → OBSERVING → DONE）、`core/budget.py`（迭代 + token 双预算，native 128 / text 64）、`core/guard.py`（重复失败护栏）
- **Tests：** `backend/tests/test_loop.py`、`test_loop_continue.py`、`test_runaway_guard.py`、`test_budget_defaults.py`
- **Known limitations：** 迭代上限可在设置中覆盖，但不会无限；护栏按「同一调用重复失败」计数，命中后 WARN/BLOCK/HALT。
- **后续依赖：** M9 上下文组装与 M10 工具执行都挂在主循环的规划步上。

### M5 — 选择器与工具路由

- **Status：** completed
- **Implementation：** `selector/`（规则层、BM25、ONNX 本地嵌入、远程嵌入）、`services/tool_router.py`（查询只嵌入一次 + 工具描述批量缓存 + 条件暴露）
- **Tests：** `backend/tests/test_selector.py`、`test_tool_router.py`、`test_tool_router_cache.py`、`test_embedding_identity.py`、`test_onnx_backend.py`、`test_remote_embedding.py`
- **Known limitations：** 远程嵌入与本地 ONNX 都需要额外配置或模型文件；缺失时降到 BM25 / 规则层，功能可用但召回质量下降。精排层默认关闭。
- **后续依赖：** M9 检索排序、M10 工具候选展示都复用这一层。

### M6 — 记忆域

- **Status：** completed
- **Implementation：** `memory/ingest.py`、`memory/fragment.py`、`memory/summary.py`、`memory/index.py`、`services/memory_lifecycle.py`（封块、滚动摘要、预算压力整理）
- **Implementation（2026-09-15 第三阶段 · 按轮封块）：** 封块阈值以前数的是**消息条数**，而设置页一直写「标准（10 轮）」—— 一轮 = 用户 + 助手两条消息，所以「10 轮」实际只有 5 轮。现在 `FragmentManager(max_turns=...)` 只数 `role='user'` 的消息（工具消息不计入），并且**最后一条是 user 消息时不封块**（否则第 N 轮的助手回答会被写进下一个片段）。设置键正名为 `fragment.max_turns`（读写与运行时共用 `memory/fragment.py::resolve_max_turns`，旧键 `fragment.max_messages` 只作为一次性迁移回退并写回新键）。
- **Tests：** `backend/tests/test_memory.py`、`test_injection_short_term.py`、`test_turn_short_term.py`
- **Tests（2026-09-15 追加）：** `backend/tests/test_fragment_turn_semantics.py`（10 轮 = 20 条消息才封块、工具消息不计轮、半轮不封块）
- **Known limitations：** 摘要与知识提炼依赖主模型调用，失败时降级为原文直引；索引是机械生成，模型不可写。
- **后续依赖：** M9 注入以此为素材；M12 维护任务读取片段做整理。

### M7 — 知识域

- **Status：** completed
- **Implementation：** `knowledge/lifecycle.py`（状态机 + supersedes 版本链）、`knowledge/verify.py`（分层验证）、`knowledge/inject.py`、`tools/knowledge_correction.py`（对话式纠错）
- **Implementation（2026-09-15 第三阶段 · 高影响候选进对话）：** 高影响候选（`user_profile` / `agent_self` / `goal`）不再只留在 `pending_review` 等用户主动去 Knowledge Panel 发现 —— `services/memory_lifecycle.py` 把本轮新建的高影响候选登记下来，turn 收尾（回答完成之后）由 `AppContext.emit_knowledge_candidates()` 发 `KNOWLEDGE_CANDIDATE`，前端在**回答完成后**以低干扰卡片给出「保存 / 修改 / 忽略」。新增 `POST /api/knowledge/{id}/ignore`：状态转 `revoked` 并在 `provenance` 记 `ignored_at`，同一内容不再重复提示；低影响候选仍自动生效、不进对话。Knowledge Panel 保留浏览 / 修正 / 归档 / 审核历史，但不再是高影响候选唯一的确认入口。
- **Tests：** `backend/tests/test_knowledge.py`、`test_knowledge_correction.py`、`test_knowledge_mgmt_api.py`
- **Tests（2026-09-15 追加）：** `backend/tests/test_knowledge_candidate_event.py`；前端 `stores/__tests__/phase3Cards.test.ts`（候选只在 `TURN_END` 之后出现、保存 / 修改 / 忽略与失败可重试）
- **Known limitations：** 高影响类别必须用户确认；隐式反馈与自动整理属后续项，见本文末「尚未完成」。
- **后续依赖：** M9 只把 `active` 条目纳入注入面。

### M8 — 图导航层

- **Status：** completed
- **Implementation：** `graph/nodes.py`、`graph/edges.py`、`graph/anchors.py`（Anchor 生命周期：位置校验/恢复/推进）、`graph/topics.py`、`graph/layout.py`、`entities/`（识别、抽取、卡片）、`tools/topic_tools.py`、`tools/continue_tool.py`（Agent 显式 `continue_from_fragment`）、`tools/entity_tools.py`
- **Implementation（2026-09-15 第二阶段 · 话题导航与 Planet 浏览景观）：** 新增 `services/navigation.py::TopicNavigationService` 作为 Anchor 的**唯一写入者**（进入话题 / 创建话题 / 确认切换 / 从历史继续），`/api/anchor`、`switch_topic` / `create_topic` / `continue_from_fragment` 与 turn 编排全部改走它；`tests/test_topic_navigation.py` 里有一条源码扫描守卫测试，任何绕过 Navigator 直接写 anchor 的模块都会让它失败。
  「从历史继续」语义修正为**新建接续片段**（迁移 11 增加 `fragments.source_fragment_id`），旧片段零改动，Focus 读来源片段；新增「待确认切换」：用户明确说「切到 X」直接执行，预测器推测只发 `TOPIC_SWITCH_SUGGESTED` 事件并等用户表态。Planet 侧新增 `services/planet.py`（轻量概览 + 确定性浏览序列 / 可前进可后退的游标）与三层数据接口 `GET /api/planet/overview`、`POST /api/planet/browse`、`GET /api/fragments/{id}/messages`；`GET /api/graph/topics/{id}` 不再内联 Message 原文，`message_count` 改为真实计数。星球不再是「固定球面坐标 + 前 16 个话题」，而是「数据层无上限、视觉层固定 16 个槽位、旋转推动话题流」的浏览景观（前端 `planet/browseSession.ts`、`planet/layoutSlots.ts`、`planet/dotPool.ts`、`planet/browseFlow.ts`）。
- **Implementation（2026-09-22 · 工具导航归属）：** `services/navigation.py` 增加工具导航登记（`note_tool_navigation` / `take_tool_navigation`，turn 标记走 ContextVar、回传走共享表，因为子 task 的写入不会传回父上下文）；`tools/topic_tools.py` 的 `create_topic` / `switch_topic` 成功后登记，`services/turn_orchestrator.py::persist` 在写回答前落实；`continue_from_fragment` 刻意不登记（只影响后续提交）。用户导航（另一个请求）读不到本轮 turn 标记，登记不上，因此行为不变。
- **Tests：** `backend/tests/test_graph.py`、`test_anchor_event.py`、`test_anchor_lifecycle.py`、`test_continue_fragment.py`、`test_entities_recognizer.py`、`test_entity_cards.py`、`test_entity_correct.py`、`test_entity_extract.py`、`test_entity_inject.py`、`test_entity_retrieval.py`、`test_topic_tools.py`、`test_topic_dedup.py`、`test_tool_nav_rebind.py`
- **Tests（2026-09-15 追加）：** `test_planet_browse.py`、`test_planet_api_layers.py`、`test_topic_navigation.py`、`test_topic_switch_policy.py`、`test_anchor_no_advance.py`；前端 `planet/__tests__/browseSession.test.ts`、`layoutSlots.test.ts`、`dotPool.test.ts`、`browseFlow.test.ts`、`stores/__tests__/topicSwitch.test.ts`
- **Known limitations：** 实体懒创建依赖提及计数阈值；星球视图的**当前展示布局**在前端按稳定种子临时生成（后端不再为渲染提供永久坐标，`nodes.meta.layout` 仅保留兼容）；浏览排序（哈希 + 近期活跃加权 + 曝光抑制）没有做过体验评估与调参；触控板手势与真机 GPU 帧率未验证。详见 `docs/release-planet-phase2.md`。
- **后续依赖：** M9 的亲和度与 M10 的 `switch_topic` / `create_topic` 都依赖锚点。

### M9 — 注入与检索

- **Status：** completed
- **Implementation：** `services/context.py`（ContextAssembler：Focus 块 `标题 + 摘要 + 开头 2 条 + 省略标记 + 结尾 3 条`，受 `FOCUS.max_tokens` 硬上限）、`services/injection.py`（三面聚合 + **按稳定身份去重**：Focus / 短期记忆已给的片段不再从检索重复注入）、`services/retrieval.py`（命中携带 `fragment_id`）、`services/affinity.py`、`services/token_budget.py`（TokenBudgetPlanner + completion reserve + 预算分解）、`services/decay.py`（分类型时间衰减）、`services/params.py`（集中阈值，含 `FOCUS`）
- **Tests：** `backend/tests/test_service_injection.py`、`test_injection_short_term.py`、`test_token_budget.py`、`test_decay.py`、`test_affinity.py`、`test_focus.py`（含 Focus 尾部结论、Token 上限、开放片段、去重）、`test_turn_no_duplicate_query.py`
- **Known limitations：** 注入上限是硬约束，强制项超预算时走确定性截断；阈值集中在 `services/params.py`，改动需要 eval 支撑（见 P3）。
- **后续依赖：** 无下游；被 P1/P2 的 turn 流水线调用。

### M10 — 工具创建生命周期

- **Status：** completed
- **Implementation：** `tools/creator.py`、`tools/lifecycle.py`、`tools/dev_tools.py`、`tools/dev_workspace.py`、`tools/tester.py`、`tools/sandbox.py`、`tools/policy.py`、`tools/approval.py`、`tools/subagent_tool.py`、`tools/task_manager.py`、`storage/tool_store.py`
- **Implementation（2026-09-15 第三阶段 · 创建进度与审批表达）：** 工具创建以前在界面上是一串彼此无关的工具卡（`create_tool` → `dev_write_file` → `dev_run_tests` → `dev_submit_tool`），用户看不出走到哪一步。现在 `tools/dev_tools.py` 的 `ToolCreateStatus` 出口按 **`group_id`（开发工作区）** 发 `TOOL_CREATE_STATUS`，phase 为 `proposal / building / testing / testing_passed / testing_failed / waiting_approval / registering / ready / failed`，前端同一张卡原地推进；失败（测试没过 / 用户拒绝 / 超时 / 凭据不可用 / 注册冲突）都带可理解的中文原因。同时 `tools/approval_present.py::describe_tool_call` 把每次工具调用翻译成「想做什么 / 会访问什么 / 影响 / 一次性还是长期」（`description` / `access` / `capabilities` / `scope`），工具执行的审批不再是 `fs_write path=...` 这种只有工程师能读的形式。
- **Tests（2026-09-15 追加）：** `backend/tests/test_tool_create_events.py`、`test_approval_present.py`；前端 `components/__tests__/ApprovalModal.test.ts`（授权范围与访问清单）、`stores/__tests__/phase3Cards.test.ts`（同一 group_id 一张卡）
- **Tests：** `backend/tests/test_tool_lifecycle.py`、`test_dev_tools.py`、`test_dev_workflow_integration.py`、`test_tool_policy.py`、`test_computer_sandbox.py`、`test_subagent.py`、`test_subagent_integration.py`、`test_tool_parallel_cancel.py`、`test_tool_registry_reversible.py`、`test_tool_restore.py`、`test_tool_store.py`、`test_tool_schema_present.py`、`test_tool_pipeline.py`、`test_tool_event_isolation.py`
- **Known limitations：** 受限子进程不是强安全隔离，而且**不强制**文件/网络隔离——实测声明为 PURE 的工具仍可读取用户目录。当前强制力只来自「按声明拒绝高风险」+「剥离环境变量」，谎报能力的工具拦不住。高风险能力在没有可用 Docker 时直接拒绝执行，不做静默降级。子 agent 异步并行上限见 `tools/task_manager.py`。
- **Implementation（2026-09-14 任务04 确认调度）：** 后台任务请求确认不再无条件抢焦点——用户正在输入（输入框/文本域/可编辑区）时到达的审批
  只入队并亮出常驻入口「⚠ 有 N 项操作等待确认」（`components/ApprovalEntry.vue`，顶部居中，避开右下角浮动组件），用户主动点开才显示窗口；
  没在输入时仍立即弹出（直接相关的确认不延迟）。窗口新增「稍后处理」＝只收起窗口、保留待审批任务（不批准也不拒绝）；**Esc 改为按最上层处理**：
  收起窗口、不做决定、入口重新亮出（此前 Esc 完全无效，用户只能被迫做决定）。收起/关闭后焦点归还被打断的输入框（实测回到凭据表单 `INPUT.qio-input`，
  未提交内容不丢）。设置页把两个浮动开关改名为「星球入口贴边后自动隐藏」「设置入口贴边后自动隐藏」，并把取值文案写成「贴边后自动隐藏：开/关」，
  明确**关闭＝入口常显，不是禁用**。
- **后续依赖：** 无下游。
- **性能测量（任务06，dev 与正式构建各一遍，2026-09-15 补）：** 脚本 `scripts/baseline/t06-perf.mjs`（只测量，不改产品源码）。条件：headless Edge/Chromium + ANGLE/SwiftShader 软件渲染、1440×900、DPR 1、估算 123Hz、隔离数据 23 个话题、当前话题 20+ 条消息；dev = Vite 5199，prod = `npm run build` 后 vite preview 5299。七场景实测（dev / prod）：设置往返 **110.4 / 110 fps**（p95 8.5 / 8.4ms，>50ms 0 / 1；点击到设置出现 80 / 67ms、返回 38 / 33ms）；长回答真流式期间切页 **89.6 fps**（p95 16.7ms，>50ms 5）；**生成中打开星球+选话题+展开侧栏 12.9 / 12.9 fps**（p95 250 / 233ms，>50ms 28 / 27；点击到覆盖层出现 212 / 181ms）；**多话题+管理列表旋转与连续选择 11 / 12.1 fps**（p95 166.7 / 99.9ms，>50ms 52 / 49）；大代码与表格滚动 **120.8 / 120.8 fps**（p95 8.4ms，>50ms 0）；连续开关页面与菜单 **114.4 fps**（菜单反馈 1ms）；后台确认到来时输入 **120.2 fps**（>50ms 0）；连续缩放窗口 117 fps；减少动画运行时切换 121.1 fps。**结论：正式构建与 dev 几乎一致 → 瓶颈不在打包方式，而在星球 3D 场景（软件渲染下）；文字与布局场景全部稳在 p95 ≤16.7ms。**
- **性能测量暴露的未完成项（有数字支撑）：** ① 页面标记为隐藏后星球仍在渲染（实测隐藏前后各 1s 的 rAF 帧数 11 → 12），没有「不可见时暂停装饰动画」；② 反复开关后监听器/观察器计数仍在增长（net listeners 88 → 星球×10 后 809 → 设置×10 后 2119；ResizeObserver 4 → 14 → 54），**是否挂在长生命周期目标上并真正泄漏尚未定论**（当前只统计创建/移除次数，未逐目标核对）；③ 关闭后中心点 `elementFromPoint` 命中的是悬浮球内部图形（`ellipse`），无覆盖层残留；④ 星球每次打开都重建整个 three.js 实例（冷启动实测 1.5–3.4s）、侧栏过渡期间逐帧 resize+render，仍未优化。
- **性能优化第一批（2026-09-15，按上面证据做）：** ① **不可见时暂停渲染**：`usePlanetScene` 监听 `visibilitychange`，隐藏时 `cancelAnimationFrame` 停掉渲染循环、恢复时重置时间基准（避免暂停时长被当成一帧 dt 造成跳变），并暴露 `setPaused()` 供调用方使用；实测把页面标记为隐藏后，同一页面 1s 采样从星球在绘时的 **12 帧** 变为 **102 帧**（重负载消失 → 渲染循环确实停了）。② **resize 去重**：画布尺寸没变化（或 `display:none` 导致 0×0）时不再 `setSize` + 补帧，省掉侧栏过渡期间的重复清缓冲/重绘。③ 复测：生成中打开星球/选话题/展开侧栏 **12.9 → 15.1 fps**、管理列表旋转与连续选择 **11 → 14.6 fps**（软件渲染下星球仍是最重场景，属于填充率/着色器成本，真机 GPU 结论待测）。④ **监听器净增长已定性为「不是泄漏」**：按目标类型细分后，窗口与文档的净监听器反而下降（window net 14 → −25、document net −7 → −218）；元素监听器净增 2451 个，但 1743 个曾绑定监听器的元素里 **只有 40 个仍在文档中**，其余 1703 个已随 DOM 移除（可回收）。ResizeObserver 计数增长只反映构造次数，各处在 `onUnmounted`/`onScopeDispose` 里都有 disconnect（按代码检查，未在运行时验证 disconnect 调用次数）。⑤ 仍未做：星球**实例复用**（现在每次打开仍重建 WebGL 场景，冷启动 1.5–3.4s；ConversationView 目前用 `:key` 保证「旧回调不关新页面」，改成常驻复用需要单独一轮验证）。
- **Implementation（2026-09-14 任务04 收尾 · 设置页移动与数字控件）：** 进入设置改成整页只做短淡入、不再整体位移（去掉 `translateY(2px)`）；
  切分类只让右侧内容短淡入（`panel-in`，左栏与页头保持不动，面板仍用 `v-show` 保留，切分类不重建表单、不丢未提交草稿）。
  `QNumber` 新增 `unit` 属性，维护间隔直接显示「24 小时」；加减按钮宽度 22px→30px 并加 `touch-action: manipulation`。
  实测：入场期间 8 次采样 transform 全为 `none`，左栏位移 0.0/0.0、页头位移 0.0，右栏动画名 `panel-in`，加减宽 30px。

### M11 — 前端

- **Status：** completed
- **Implementation：** `frontend/src/views/`（对话页、星球页、设置页、调试页）、`frontend/src/components/`、`frontend/src/stores/`、`frontend/src/planet/`、`frontend/src-tauri/`（桌面壳）。2026-09-12 稳定化：审批失败保留待审批项并可重试（失败 ≠ 已授权）、锚点切换以后端成功为准、token 用量按 `turn_id` 归属、流式 Markdown 增量渲染、流式自动跟随（上翻即停）、QNumber 统一 commit 语义、设置页分区反馈与凭据留空不清除、星球详情竞态防护、浮动组件单击不贴靠且 resize 保持贴靠关系。
- **Implementation（2026-09-15 第三阶段 · 状态表达与事件收口）：** 事件集合与后端 `EventType` 完全一致（守卫测试 `backend/tests/test_event_protocol.py`：集合相等、每个事件都有生产者、都必须被前端消费）——删掉 `MEMORY_INJECT`（前端写了 case、后端从来不发；普通用户不需要知道「注入了 4 条记忆」，开发者改看 `/debug` 的单轮 `injection`），补上 `TOOL_START` / `TOOL_CREATE_STATUS` / `KNOWLEDGE_CANDIDATE` / `CREDENTIAL_STATUS` / `FALLBACK` / `APPROVAL_RESULT` 的消费分支。工具卡变成「开始时立刻出现运行中、结束时按 `call_id` 原地更新」（不再等结束才可见、不再出现重复卡，并显示耗时与失败结论）；`SUBAGENT_STATUS` 独立成「独立任务」卡（不再混进普通工具卡，按 `task_id` 原地更新）；工具创建是一张卡（`ToolCreationCard.vue`）；高影响知识候选在**回答完成之后**以低干扰卡片出现（`KnowledgeCandidateCard.vue`，保存 / 修改 / 忽略，修改是很轻的内联编辑）；全局只保留一句整体状态（正在处理 / 正在使用工具 / 等待你确认 / 正在处理独立任务 / 正在整理独立任务的结果），不再暴露内部事件名；审批弹窗新增「授权范围：仅这一次 / 长期生效」并优先显示具体访问清单。
- **Tests（2026-09-15 追加）：** `stores/__tests__/phase3Cards.test.ts`（TOOL_START→TOOL_END 同一张卡、独立任务按 task_id 更新、工具创建按 group_id 推进、候选只在 TURN_END 后出现、凭据/降级提示不含内部标识、notify 轮不清排队标记）、`components/__tests__/MessageStream.test.ts`（整体状态文案与「不出现内部事件名」）
- **Implementation（2026-09-28 凭据表单统一）：** 首次引导的「连接模型」与设置页的凭据弹窗改为**同一个组件**
  （`frontend/src/components/credentials/CredentialForm.vue` + `services/credentials.ts` 的 `useCredentialForm`），
  默认值、校验与提示语不再有第二份实现。基础区只保留厂商（可搜索的 `QCombo`）、API Key（可显隐）、用途
  （默认「主对话」，可点「修改」多选）与「取消 / 保存」；地址、协议、模型、显示名称、用量上限、自定义标签与
  只读的内部标识都收进默认收起的「高级设置」。主按钮固定「保存」（执行中「保存中…」+ 独立的进度/结果区，
  提交期间禁用按钮并中断旧请求：关闭表单、切换厂商或改 Key 之后，旧结果不会覆盖新状态）。选厂商即补齐地址、
  协议与建议模型；「其他 / 自定义服务」直接展开必填项；拿不到模型列表时可手填。凭据卡默认只给结论
  （名称/厂商、模型、中文用途、当前默认标记、启用状态与验证状态），地址/内部标识/版本/审计收进详情，
  常用入口是「编辑」与「更多操作」，「换钥」改名「更换 API Key」（先验证新 Key 再原子替换，失败保留原凭据）。
  文案明确：在 QIO 里撤销或删除凭据**不会**吊销厂商账户里的 API Key。
- **Implementation（2026-09-28 续 · 引导页合并按钮）：** 首次引导「连接模型」这一步只有一个主按钮：
  该保存时它是「保存」，保存并验证通过后自动进入下一步；已经有可用凭据又没在填新的时它是「下一步」。
  「跳过」只在它比主按钮多做一件事时才出现（确实有一份没保存的填写）：已有可用凭据且没在填新的时，
  「跳过」与主按钮是同一件事，因此只保留主按钮；没有任何可用凭据时它仍是「先不配，往下走」的出口。
  表单自己那一行「取消 / 保存」在这一步隐藏（`showActions=false`，由引导页调用表单的 `submit()`）。
  验证没通过就停在原地，原因与「重试验证」显示在表单里。同一个表单里如果**已经写过库**，
  后续的保存一律作用在那条记录上：钥匙没变 = 只重试验证（不写库、不涨版本），钥匙变了 = 原子替换
  （失败保留原凭据），改地址/协议仍要求显式确认 —— 避免「改掉打错的 Key 再点保存」意外新建第二条凭据。
- **Implementation（续 2026-09-14 体验轮）：** 复制的诚实反馈（剪贴板不存在/写入失败一律显示「复制失败」，不再假装成功）、草稿连续（输入草稿存 `session.draft`，切页不丢；发送失败回填草稿并撤掉未获受理的乐观消息）、排队请求失败不再清除仍在运行的任务状态、只有本机发送才把消息流拉回底部（后台任务开始不打断向上阅读）、星球详情加载失败独立可见且可重试（与「从这里继续」的错误分开）、QNumber 手输在真实父组件绑定下也会在失焦/回车时提交一次、知识页筛选框有可见标签与「全部」回退项。动效：设置与星球出现/消失 170–260ms、星球相机 420–460ms、边栏重新居中和宽度过渡同时发生、脚本相机补间遵守 `prefers-reduced-motion`。设计规则同步在 `docs/superpowers/specs/2026-08-09-qio-frontend-design.md`。
- **Implementation（2026-09-14 状态一致性补齐）：** 阅读位置随会话保留（上翻阅读 → 设置/星球 → 返回恢复到原位置，恢复期间的程序性 scroll 不参与跟随判定；容器未完成布局时重试有上限）；设置页记住上次分类与分类列表滚动位置（存 ui store，仅同次运行期）；星球首次数据加载失败有可见失败条与重试；四个设置保存路径（记忆/对话深度/搜索/维护）加请求归属序号，旧响应不回填、不用陈旧成功盖住新的失败；起点请求在页面已关闭时失败会回到对话页可见；代码复制按钮每个独立计时（连续复制不同代码块各自复位）、失败时选中代码作为手动复制退路、`@media (hover: none)` 下默认可见（触屏可发现）；审批失败后焦点回到对话框。
- **Implementation（2026-09-14 动画与反馈统一）：** 动画参数按用途分层落到令牌（按下 80ms / 开关选中 150ms / 菜单弹窗 170ms / 设置打开 220ms / 返回 170ms / 侧栏 210ms / 星球打开 300ms、关闭 200ms / 聚焦 320ms 且短距离 0.6×，曲线 `--ease-out` 打开、`--ease-in` 关闭），并去掉唯一的 `transition: all`；菜单与弹窗补齐出现与退出（退出结束从 DOM 移除，菜单退出期间不拦截点击、模态退出期间保留遮罩拦截），折叠的队列列表也补了短过渡；新增「跟随系统 / 标准 / 减少动画」偏好（`localStorage(qio-motion)` + `html[data-motion]`），CSS 过渡与星球相机补间读同一份结果、运行中切换立即生效，减少动画或时长为零时跳过退出阶段；按钮按下反馈立即开始（`.qio-btn/.qio-select/.opt/.tag-chip/.qio-btn` 独立 80ms 位移），处理中用 `min-width` 固定尺寸避免周围跳动，开关「关闭」与「禁用」视觉上区分；运行中任务与排队项分别用「停止」「取消排队」，取消请求期间显示等待确认、失败留下可见错误；队列项的旧 `TURN_END` 不再结束当前运行状态；流式正文标记 `aria-busy` 且不设 live 区域（不逐字播报）。
- **Implementation（2026-09-14 聊天布局与阅读连续性）：** 对话内容改为**一条居中内容列**（860px）——回答与用户消息同列左右对齐，不再分别贴窗口两端，长文仍受 68ch 行宽限制；输入区固定在内容列正下方（中等窗口为星球入口留通道，≤899px 整宽贴底），默认中性边框 + 较轻阴影，聚焦只加强外框一层（内层 textarea 不再叠边框与光晕）；底部留白仍按输入区高度动态预留，实测滚到底时最后一条内容在输入区上方。滚动：上翻阅读时出现「回到最新消息（N）」，点击才滚动并恢复跟随；自动滚动改用可被滚轮/触屏中断的 rAF 短滚动；贴底前**再次确认用户意图**（布局等待期间用户上翻就不滚）；等待布局与恢复位置都不再依赖固定延时。生成状态只表达可观察阶段：已提交且无助手内容时「已提交，等待模型响应…」，有增量后由流式气泡表达「正在生成」，等待动画只在回答附近播一次（任务列表改静态「运行中」标记）；新消息最多一次 170ms 入场（历史消息不带），长回答积压超过 200 字时该段显示压到 600ms 内追上已到达内容；已取消条目提升到 opacity .75 保持可读。
- **Implementation（2026-09-14 阅读位置收尾）：** 两处「阅读位置」缺口修掉：（1）**本机发送被后端拒绝时把阅读位置放回发送前**
  （新增 `session.sendRejectedSeq` 信号 + 发送前快照；被拒时不进入跟随、「回到最新消息」的虚假未读数作废；用户在请求途中自己滚过则不强行放回。
  实现上必须从判定那一刻起就忽略 `scroll` 事件，否则贴底补送的迟到 `scroll` 会把状态重新判成跟随、把位置冲回底部）；
  （2）**「回到最新消息」在长对话里不再停在半路**（虚拟列表滚动中逐条测量使总高度持续变大，平滑滚动改为每帧重算目标，实测从「距底部 5346px」变为 0）。
  验收脚本同步校准：`A5` 断言恢复为「阅读位置不变」，测试事件的 `turn_id` 每次运行唯一（后端会重放最近事件，固定 id 会让上一轮的 `TURN_END` 混进来），
  `A3/A5` 发送前 `Esc` 收起重放出来的「等待确认」窗口。综合验收 19/19 通过。
- **Tests：** `frontend/src/**/__tests__/*.test.ts`、`frontend/src/smoke.test.ts`、`frontend/src/styles/tokens.test.ts`
- **Implementation（2026-09-15 状态真实性）：** 最终回答的唯一权威来源是 `TURN_END.final_content`：最后一条是「工具前说的中间话」时不再被当成最终答案，`TURN_END` 没有内容时也不会把中间话升级成答案（`stores/events.ts` + `stores/session.ts::applyFinalAnswer` / `markLastAssistantInterim`，中间话始终带 `interim` 并以「◈ 过程」呈现）。`ERROR` 只显示错误、不再结束界面上的 turn（结束只认 `TURN_END`），同一 turn 的重复 `TURN_END`（重连重放）只生效一次。历史读取有 `idle/loading/ready/error` 状态：失败时保留已加载的消息并在对话顶部给「历史记录暂时无法读取 + 重试」（安静的一行，不是错误横幅）。提交后立即用 POST 返回的 `turn_id` 打开停止能力。取消的结局在顶部安静地写「已停止」，不当错误。
- **Implementation（2026-09-15 桌面边界）：** Markdown 链接按协议白名单渲染（只允许 `https/http/mailto`；`javascript:`、`data:`、`file:`、`vbscript:`、协议相对地址、含控制字符的伪装一律退化成纯文本，见 `frontend/src/utils/externalLink.ts`），点击外链不再让 WebView 自己导航而是走 Tauri 官方 open（`plugin:shell|open`），打开失败有可见反馈；`tauri.conf.json` 的 `csp` 从 `null` 换成生产最小权限策略 + 独立的 `devCsp`（字体/图标是 `self` + `data:`，本机后端与 SSE 走 `connect-src http://127.0.0.1:*`）。
- **Known limitations：** 桌面壳只在 Windows 上验证过；应用内浏览器有模块缓存，改前端后需带 `?fresh=N` 强刷。斜杠命令体系未实现。代码块复制仍依赖 `navigator.clipboard`：受限 WebView 下写不进去时现在会显示「复制失败」并选中代码留出手动复制退路，但不保证写入。草稿、阅读位置、设置分类与**非敏感表单草稿**只活在**当前运行期内存**里（刷新页面不保留，也不跨设备）；凭据密钥一类敏感输入不进这份草稿。星球帧率未做过真实设备采样（只测量了状态变化与过渡时长）；聊天区的流畅度同样只有状态与时长测量，没有帧率曲线。后台审批的调度已按任务 04 重做：用户正在输入时不自动弹出（只亮出「有 N 项操作等待确认」入口，由用户主动打开），打开后可按 Esc／「稍后处理」收起并保留待办，焦点回到被打断的表单（实测凭据表单 `INPUT.qio-input`，未提交内容不丢）。仍未验证的是**后端确认成功出队**的端到端路径：假 `approval_id` 不被后端受理，成功出队后的焦点归还只有前端单测覆盖。「停止当前回答 / 取消运行条目」已统一为同一个对象（当前运行的任务），「取消排队」是不同对象、文案保持区分；三种取消的**事件顺序**仍未逐条实测。迟到 `TURN_END` 已加防护并有单测，但真实的取消与完成同时到达仍未在运行中的应用里复现。输入区改为与内容列对齐属于**默认布局变化**：输入区本身不可拖动、没有用户保存位置（星球/设置入口的浮动位置与贴靠未被覆盖，「还原默认布局」照旧）。
- **性能测量口径（2026-09-14 补测）：** 用页面内 rAF 采样记录帧间隔，环境是 **headless Edge + SwiftShader 软件渲染**，只用于发现卡顿、不代表真机 GPU。三段实测：滚动长对话 120.7fps（p95 8.4ms、>50ms 的帧 0）、长回复增量到达 105fps（p95 16.7ms、>50ms 的帧 0）、**星球打开/拖动/关闭 40.5fps（p95 91.8ms、>50ms 的帧 28）**。结论：目前唯一出现长帧的是星球 3D 场景，且处于软件渲染下；真机结论待任务 06 复测（可先评估把环球网格 128×96 降到 96×64，环宽按像素算，不受影响）。
- **触屏与键盘（2026-09-14 补测）：** 触屏用 CDP 合成触摸事件验证（非真机）：直接点复制入口得到「已复制」；向下滑动消息区后停止跟随并出现「回到最新消息」。消息区加了 `tabindex` + `aria-label`，键盘 PageUp 能滚动并停止跟随、End 回到最新；真机手势未验证。
- **星球展开/收起（2026-09-14 演示版）：** 用户反馈原时长「过快、过渡不连贯」，改为两段式：展开＝140ms 铺底 + 420ms 内容淡入/相机收敛（相机一开始就在接近最终构图，不再从远景小球放大）；收起＝180ms 收势（沿视线轻微后撤 + 星球降到 25% + 面板退场）+ 280ms 整体淡出后卸载。实测关闭 531–541ms（原 225ms）、减少动画下 9ms 直接卸载。演示开关 `?planetdemo=slow`（时间线 ×3）用于逐帧观察；**这组时长比任务 02 记录的动画参数更长，是按用户直接反馈调整的（规则同步在设计规范里）**。
- **Implementation（2026-09-14 任务05 星球：加载、选择与管理）：** 打开时按「起点未变就回到上次浏览的话题、起点变了以起点为准」定位（浏览记忆只在运行期内存，`composables/planetSession.ts`）；数据未回来显示「正在加载话题…」、失败显示原因 + 重试，懒加载 chunk 期间 `PlanetBoot` 立即铺底显示「正在打开星球…」，入口按下即 `scale(0.96)`。详情区改为三段式：顶部固定（页签/标题/搜索）、中部可滚动（话题列表自身滚动且高度封顶 42%，详情正文与它各自滚动）、底部固定操作区，浏览器实测操作区不随滚动移动、不覆盖正文（y 793.8 → 793.8，中部底边 794）。面板固定显示「浏览的话题 / 选中的片段 / 已生效的起点」三行，起点取自服务端返回的会话状态。话题点：悬停显示简短名称（移开即消失）、选中话题有持续可见的选中环（修掉了「用世界坐标写局部位置，环跑到球面别处」的坐标错误）、命中范围放大到屏幕 16px/14px、列表项可聚焦并用 Enter/Space 选择、Esc 先收边栏再收星球；辅助网格减弱（经线 45°→60°，透明度 0.35/0.18→0.22/0.1、0.13→0.07），球体结构/融合环/聚焦距离 2.25 未动。收起阶段整层 `pointer-events: none`，父级用「打开序号 + key + seq」保证关闭动画中的迟到回调不会关掉新打开的一层；知识/实体面板把「读取失败」「暂无记录」「没有匹配」分开，并提供重试与清除筛选。
- **Implementation（2026-09-15 第二阶段 · Planet 浏览景观）：** 星球改成窗口驱动：`VISIBLE_CAPACITY = 16`（正面可见约 8 个），数据层不再有「只显示前 16 个话题」的上限；`loadTopics()`（把全部话题聚簇后切前 16 个）被 `attachBrowse(session)` 取代，话题点来自固定容量对象池 `planet/dotPool.ts`，进出只改数据、不 new / dispose。旋转按方位角累计（0.4 rad 一步）推动话题流；**位置规则 = 打开时在整个球面随机铺开（`spreadPositions`）+ 拖动摇动时新话题在球体背面随机落点（`randomBackPosition`）+ 静止时窗口内位置完全不动**。真的掉头才把刚离开的话题连同原位置放回，继续同向旋转一律引入新话题。选中话题在查看期间锁定、不会被回收；搜索或列表命中的话题会注入展示窗口。相机后撤（planet 2.6→2.9、focus 2.25→2.6）并加**纵向限位 35°~145°**（`POLAR_LIMIT`，程序性相机移动同样受限），球体完整落在画面内、四边留白。开发构建里提供只读调试钩子 `window.__qioPlanetWindow()`（生产构建不注册）。
- **后续依赖：** 无下游。

- **任务 04/05 追加（2026-09-14）：** 审批弹窗改为按「它想做什么 → 会访问什么 → 会改变什么 → 为什么需要 → 验证了吗（已验证/未验证）→
  折叠的高级详情（策略指纹、逐条测试结果、raw params）」组织；高风险操作的批准按钮降调为中性实心（不再像品牌色在推荐你点），
  低风险才用主操作色；失败文案先给结论「未做出任何授权」。设置页补上保存模型标注（自动保存分区「修改后自动保存」、
  搜索分区「开关即时保存、数字参数点保存生效」）与「保存中…」等待态；输出速度保存失败不再只写控制台；
  关闭免密钥搜索且无博查/SearXNG 时说明「当前没有可用的联网搜索通道」。净白主题的 `--success/--danger/--warning`
  与 `--text-faint` 按实测调深（旧值在小字上只有 3.16–4.14:1，达不到 4.5:1）；`scripts/baseline/qa/contrast.mjs`
  客观审计现为 **0 项不达标**，另有 2 项「非文本边界」建议项未改（描边对页面底色约 1.6–1.7:1；输入框另有高对比聚焦态，
  加深全部描边会改动整体视觉语言，属待定取舍）。修掉两个状态真实性缺陷：①「从这里继续」切换话题后对话页不再停留在旧话题
  （新增 `session.setAnchorAndSync`：锚点真的变化才 `loadHistory()`；SSE `ANCHOR` 在 `turnRunning` 时不重载以免擦掉流式内容）；
  ②停止按钮在「已提交、服务端运行标识未到」期间不再显示为灰的「停止」（改为「准备中…」，拿到 `turn_id` 后才叫「停止」）。
  聊天页新增 `.skip-link`「跳到输入框」作为第一个键盘焦点位——实测每条消息的复制按钮会占满 Tab 顺序（前 8 个停靠点全是它）。

### M12 — 离线维护任务

- **Status：** completed
- **Implementation：** `services/maintenance.py`（矛盾扫描、Dreaming 整理、轨迹→工具候选）、`prompts.py`
- **Tests：** `backend/tests/test_m12_maintenance.py`、`test_m12_trace.py`
- **Known limitations：** 属离线任务，不在运行时关键路径上；失败被隔离，不影响主循环；需要模型调用时走主循环凭据。
- **后续依赖：** 无下游。

---

## 工程强化

以下不是产品里程碑，而是对已实现能力的重构与加固，按引入顺序编号。

### P1 — Turn Runtime 边界与并发契约

- **Status：** completed
- **Implementation：** `core/turn.py`（`TurnContext` + `TurnManager`：主 turn single-flight、FIFO 排队、可取消，**并且是 turn 生命周期的唯一事实源**）、`services/turn_orchestrator.py`（单轮流水线）、`POST /api/turns`（受理即返回 `turn_id`）、`POST /api/turns/cancel`
- **Implementation（2026-09-15 生命周期协议）：** 一个被受理的 turn **恰好**产生一次 `TURN_START` 与一次 `TURN_END`（终态 `completed / failed / cancelled / unavailable`，由 `TurnManager` 在 `finally` 里收口），任何异常路径都不会再让界面停在 running；`POST /api/turns` 不再只回 `accepted`，而是同步返回 `{turn_id, status}`，前端不必从 SSE 里猜请求身份。`AgentLoop` 不再发 `TURN_START` / `TURN_END` —— 它同时被 subagent、维护任务、工具开发流水线复用，以前一个子 agent 的 `TURN_END` 会把用户的主 turn 提前结束。取消（`TurnContext.cancelled`）在「模型调用前后 / 工具调用前后 / 下一次迭代前 / 持久化最终回答前 / 收尾记忆处理前」逐点检查：取消后不再发起新的模型或工具调用、不保存后续内容为正常最终回答、不推进锚点、不做记忆整理，`TURN_END.status = cancelled`；底层 HTTP 请求无法物理中断，但返回值会被丢弃。
- **Tests：** `backend/tests/test_turn_manager.py`、`test_turn_concurrency.py`、`test_turn_identity_events.py`、`test_turn_cancel_api.py`、`test_turn_no_duplicate_query.py`、`test_tool_event_isolation.py`、`test_turn_lifecycle_protocol.py`（四种终态各恰好一个 `TURN_END`、无凭据 → `unavailable`、收尾阶段抛错仍收口、取消不落库）
- **Known limitations：** 这是「可靠的单飞主 Agent + 明确排队」，不是多用户并发 Agent Server。契约细节见 `architecture.md` 的并发契约一节。
- **后续依赖：** P2 的 Trace 以 `turn_id` 为主键。

### P2 — Trace 与可观测性

- **Status：** completed
- **Implementation：** `trace/model.py`、`trace/store.py`（`turn_traces` 表）、`trace/recorder.py`、`trace/redact.py`（统一脱敏）、只读 API `/api/traces`、前端 `/debug` 视图
- **Tests：** `backend/tests/test_trace_store.py`、`test_trace_api.py`、`test_trace_redact.py`、`test_trace_turn.py`
- **Known limitations：** Trace 存的是标识、摘要预览与计数，不是完整 prompt；默认脱敏后才是 safe-to-inspect，不要往里塞原文。
- **后续依赖：** 无下游；是调试与未来 eval 的事实来源。

### P3 — Context 预算、衰减策略与 Eval 框架

- **Status：** completed
- **Implementation：** `services/token_budget.py`、`services/decay.py`、`services/params.py`、`agent/eval/`（`topic_eval.py`、`retrieval_eval.py`、`run.py`）、数据 `backend/evals/`、基线 `backend/evals/baseline.json`
- **Tests：** `backend/tests/test_token_budget.py`、`test_decay.py`、`test_eval_regression.py`
- **Known limitations：** 评测集是小型、确定性、离线数据集，指标用于**防退化**，不代表生产质量；它不在运行时关键路径上，跑评测失败不应阻塞对话功能。
- **后续依赖：** 调整 topic/retrieval 阈值前应重跑并对比基线。

### P4 — 工具能力安全、沙箱与执行策略

- **Status：** completed
- **Implementation：** `tools/policy.py`（能力分级 + 指纹）、`tools/sandbox.py`（按策略收紧）、`tools/approval.py`（能力展示；审批绑定 turn/session + 过期 + 摘要 + 单次使用）、`services/tool_router.py`（缓存与条件暴露）
- **Implementation（2026-09-15 命令与文件边界）：** 命令模型拆成两个工具：`run_program`（`program + argv`、`shell=False`、只读程序白名单 + 参数校验，白名单内可自动执行）与 `run_shell`（交给系统 shell 的自由命令，**每次都要审批**，`plan` 模式直接拒绝）。`ComputerSandbox` 新增 `classify_argv` / `shell_verdict` / `command_verdict_for_program`：含 `| > && ; $(` 等元字符的命令至少 DANGER，修掉「按字符串前缀判安全、却把整条字符串交给 shell」的错配。文件工具统一 `resolve → containment(ComputerSandbox.root()) → 权限判定 → 执行`：相对路径与省略参数以工作区根为基准（不再用 `process.cwd()`），`..`、根外绝对路径、指向根外的 symlink/junction（含嵌套）都只能走审批；`fs_find` 不再沿符号链接目录走出根外。
- **Tests：** `backend/tests/test_tool_policy.py`、`test_computer_sandbox.py`、`test_tool_router_cache.py`、`test_tool_router.py`
- **Tests（2026-09-15 追加）：** `test_shell_boundary.py`（链式命令不得判低危自动执行、`run_shell` 永远要审批、`run_program` 参数校验）、`test_fs_containment.py`（默认根不是 cwd、`..`/根外绝对路径/symlink-junction 逃逸）、`test_approval_binding.py`（单次使用、过期、错 turn/session、摘要不符）
- **Known limitations：** 见 `architecture.md` 的沙箱安全契约；扩大能力必须重新审批，不能沿用旧授权。
- **后续依赖：** M10 的工具创建流程依赖这一层的策略判定。

### P5 — Provider 边界、依赖锁定与 CI

- **Status：** completed
- **Implementation：** `adapters/errors.py`、`adapters/base.py`（`finish_reason`）、`backend/uv.lock`、`.github/workflows/ci.yml`、`scripts/setup_env.ps1`
- **Tests：** `backend/tests/test_adapter_contract.py`、`test_adapter_errors.py`、`test_embedding_identity.py`；CI 本身在 push / PR 上执行
- **Known limitations：** 后端本地只在 Python 3.12 验证过，3.11 由 CI 矩阵验证；CI 只对 Rust 做 `cargo check`，不产出完整 Tauri 安装包。
- **后续依赖：** 无下游。

### P7 — 集成验收（Dogfooding / E2E / 视觉）

- **Status：** completed
- **Implementation：** 无新增产品功能；本阶段以验证为主，产出 4 个修复：`agent/tools/sandbox.py`（超时终止子进程 + 清理不再抛异常）、`agent/tools/runtime_tools.py`（输出按策略截断）、`frontend/src/components/MessageStream.vue`（悬浮星球不再遮挡消息）、`scripts/e2e-checklist/run_memory_tests.py`（陈旧 API / 缺判空 / 断言污染 / 陈旧 fixture / 外键清理）。
- **Tests：** `backend/tests/test_p7_*.py`（8 个文件），其中两个沙箱回归做了红绿验证；端到端见 `scripts/e2e-checklist/run_memory_tests.py`。
- **Known limitations：** 完整报告与未修项见 `docs/release-qualification.md`。
- **后续依赖：** 无下游；结论供后续迭代参考。

### P8 — 本机 API 边界与 turn 生命周期协议

- **Status：** completed
- **Implementation：** `api/auth.py`（`SessionAuth`：会话令牌校验 + origin/Host 策略 + 一次性 SSE ticket）、`api/server.py`（认证中间件、CORS 白名单、`/api/instance`、`/api/events/ticket`；`/api/events/test` 只在开发模式注册）、`config.py`（`QIO_SESSION_TOKEN` / `QIO_SESSION_TOKEN_FILE` / `QIO_DEV_INSECURE` / `QIO_ENABLE_TEST_EVENTS` 与 origin 白名单）、`frontend/src-tauri/src/main.rs`（随机空闲端口 + 后端生成会话令牌写入用户私有临时文件 + `qio_backend_info` 命令把 `{port, token}` 交给自己的 WebView）、`frontend/src/services/backend.ts`（地址与令牌解析，不再硬编码端口）、`api/bus.py` + `api/events.py`（线上 `id:` 行与 cursor 重放）
- **Tests：** `backend/tests/test_api_auth.py`、`test_sse_replay.py`、`test_credential_endpoint_api.py`
- **Known limitations：** 桌面壳的随机端口与令牌交接只在 Windows 上验证过；未在真实打包产物里跑过完整验收。开发脚本 `scripts/e2e_up.py` 默认仍以显式开发豁免（`QIO_DEV_INSECURE=1`）启动，并用 `--secure` 提供带令牌的口径；直接 `uvicorn agent.main:create_app --factory` 且不设任何环境变量时后端会自建令牌并拒绝所有未带令牌的请求（fail-closed，日志里只打印提示、不打印令牌）。
- **后续依赖：** 无下游。

### P9 — 第四阶段：视觉统一、交互收口与动效语言

- **Status：** partial
- **Implementation：** `docs/superpowers/specs/2026-09-15-visual-language-phase4-design.md`（规范 v3）；
  `frontend/src/styles/tokens.css`（三层动效 `--mo-1/2/3-*`、曲线集 `--ease-1/2/3-*`、位移 `--shift-*`、晶体玻璃 `--glass-*`）、
  `frontend/src/styles/base.css`（组件原语 + 过渡工具类 + 重写的 reduced-motion 语义）、
  `frontend/src/components/planet/PlanetOrb.vue`（入口小球 = 全屏 Planet 的压缩态）、
  `frontend/src/components/ui/QConfirm.vue`（取代原生确认框的确认层）
- **Implementation（本阶段改了什么）：** 把前三阶段已建立的能力收敛到同一套视觉/交互/动效语言：
  ① 动效分三层（高频简洁、中频柔顺、低频优雅），旧 `--dur-*` 全部降级为别名，页面组件不再散落硬编码值；
  ② Planet 入口小球改成 Planet 本体的压缩态，进入/退出是同一个对象的连续长大与收缩（不再是两个组件淡入淡出）；
  ③ 晶体玻璃材质只用于高层级空间浮层；
  ④ 卡片家族（Approval / Tool Creation / Subagent / 知识候选 / 工具调用）统一骨架与状态表达，状态原位推进；
  ⑤ 原生 `confirm` / `alert` 全部替换为按危险程度分档的确认层；
  ⑥ Knowledge / Entity / Topic Detail 改为「默认阅读、按需编辑」；
  ⑦ reduced-motion 从「全部瞬切」改为「保留淡入淡出、去掉位移与弹性」。
  2026-09-20 追加（用户实测反馈 + 一个死结）：
  ⑧ 星球「变大」的起点改用布局值计算（此前第二次打开会因为残留缩放把起始尺度算成 ≈1，
  表现为「星球直接出现、没有变大」）；⑨ 铺底与长大/收拢改成同一刻起止，对话页随星球渐隐渐显；
  ⑩ 审批在后端已不存在（404）时标记为失效并给一个「知道了」，不再留成永远点不掉的待办
  （其它失败仍保留待办并可重试）；⑪ `.planet-view` 整层背景改为透明（此前它自带不透明底色，
  一挂载就把对话页盖死，导致「对话页随星球渐隐」根本不可见 —— 只有铺底这一层负责遮盖）；
  ⑫ 收起不再做相机后撤（那是旧编排「收势 + 整层淡出」的遗留物，会让收起出现两段缩小：
  相机后撤一段、星球层缩回入口一段；现在收势只降密度、退浮层，体量收缩只有一段）；
  ⑬ **入口小球改为真实星球渲染**（用户要求）：关闭星球时把同一个场景缩到入口尺度常驻渲染
  （慢速自转、低帧率、展开后聚焦当前话题），不再是另画的 2D 压缩态；代价是 three.js 会在对话页
  懒加载并常驻（空闲挂载、正在对话时不抢主线程），WebGL 不可用时仍退回 2D 压缩态；
  ⑭ 打开延迟回到设计值：已预热时不再等数据刷新（点击 → 开始长大 1240ms → 391ms，
  点击 → 场景接管 1891ms → 915ms，设计值约 920ms），展开结束之后才聚焦当前话题；
  ⑮ 收起星球时右侧话题栏**一起退场**（收势阶段宽度收到 0、透明度 0，球态下不出现）——
  此前它只由 open/entered 驱动，会留在对话页上（用户实测反馈）；
  ⑯ 进入星球只剩**一段**动作：聚焦与长大同刻开始、同刻收束，并去掉人为的「激活等待」
  （此前先放大进入、再回正，用户实测反馈为两段动画）；⑰ 入口小球拖动松手后**本体跟着贴边**
  （此前只跟随内联 style 变化，而贴边是 CSS 过渡，本体停在原地再闪现），并按用户要求去掉
  「话题星球」文字标注（保留 title / aria-label）；⑱ 入口小球改为**按自身分辨率渲染**
  （球态画布 = 108px、scale≈1），不再把全屏渲染缩到 ~10% —— 那会让 1 像素宽的轮廓
  被打成断续的点。配套 `renderer.setSize(w, h, false)`：不让 three 写内联尺寸样式；
  ⑲ 球态的**环宽按球体屏幕直径成比例**（0.75%，保底 1.3px）—— 此前为了「看得见」用了固定 1.7px，
  相对粗度是全屏的 2.7 倍，小球看起来是一圈圈加粗的同心环而不是缩小的星球；
 ⑳ 收起与球态共用同一个密度常量 `BALL_REVEAL`（此前收起设 0.12、球态 0.55，
 交接那一帧点阵与网格会突然冒出来，用户实测「最后一帧和缩小状态完全不同」）。
  ㉑ **收缩尾段改在球自己的分辨率下渲染**：屏幕上球直径降到入口球的 1.35 倍时，星球层切到
  入口小球那套布局（画布 108px），并用当前 computed transform 反算球此刻的屏幕几何写回新坐标，
  之后每帧用**同一条令牌曲线**（新增 `frontend/src/utils/easing.ts` 求值）推进到终点 ——
  只有「画布 CSS 尺寸 = 屏幕尺寸」才是真 1:1（把绘图缓冲改小只会更糊）。换布局那一帧实测
  球心偏离应有轨迹 0.01px、半径单帧零回升（新增验收 S2.19）。
  ㉒ **环宽补偿改为「渲染前按当时几何现算」**：视图只交「环在屏幕上的目标宽度」
  （`setRingScreenWidth`），补偿倍数由 `usePlanetScene.applyRingWidthCompensation` 用渲染那一刻的
  `画布 rect 宽 / 布局宽` 现推。起因是用户实测「缩小途中闪一下」——尾段换布局时补偿倍数还是旧约定下的
  ≈2.0（对应整层缩放 0.13），而画布已变成 1:1，那一帧的环被画成 10px 宽的实心斑；
  改成现算后这类「几何与补偿错配」结构上不可能再出现（新增验收 S2.20，逐帧画面复核见
  `scripts/baseline/qa/planet-flash-probe.mjs`）。
  ㉓ **打开时以「当前在聊的话题」为中心**：定位规则由「起点没变就回到上次浏览的话题」改成
  「当前话题优先、浏览记忆只兜底」—— 用户要求「打开前它不在最中心，就在打开的过程中转到中心」。
  旋转本来就在长大过程中发生（实测锚点从偏离 162px 收到 1px，收束点在长大进度 96–99%），
  缺口只是被浏览记忆顶掉了。新增验收 S2.21（对着修复前规则确认会红）。
  ㉔ **收起时也转：转回「打开前的朝向」**：`markReturnOrientation()` 在展开聚焦**之前**记下球体四元数，
  `rotateBack(maxMs)` 在体量收缩段同刻发起，时长按夹角缩放并封顶在 `--mo-3-collapse`
  （超出会和球态自转抢同一个四元数）；曲线用对称的 `easeInOutCubic`，所以收起正好是展开的时间倒放。
  新增验收 S2.22（实测展开 159°、收起首帧仍 159°、末帧 4°），单测 +1。
  代价：大角度会在窗口内转得较快（159°/420ms ≈ 6.6 rad/s）。
- **Tests：** `frontend/src/styles/tokens.test.ts`、`frontend/src/styles/primitives.test.ts`（令牌与降级语义）、
  各组件既有 `__tests__` 的家族契约断言；真实界面验收脚本 `scripts/baseline/qa/phase4.mjs`
- **Known limitations：** 见本文末「尚未完成」里的第四阶段条目。摘要：工具创建卡只做了家族语言统一
  而没有真实端到端跑通；Planet 关键转场只在无头软件渲染（swiftshader）下逐帧验证过，
  真机 GPU 帧率与触控板手势未验证；动效手感未经真人评审；首次打开的 WebGL 冷启动耗时未优化。
  本阶段没有新增后端能力，也没有改变任何状态机。
- **后续依赖：** 后续新增界面必须复用本阶段的令牌与组件原语，不允许再引入新的视觉语法。

---

### P10 — 全工程稳定性、逻辑一致性与性能修复

- **Status：** completed
- **Implementation（turn 状态模型）：** `core/turn.py` 的 `TurnContext.status` 明确区分
  `accepted` / `queued` / `running` 与终态；**只有真正开始执行的 worker 才发 `TURN_START`**。
  终态单向：进入 `TERMINAL_STATUSES` 之后不再变化。被取消的 queued turn 变成 tombstone ——
  仍留在底层 `asyncio.Queue` 里，但 worker 取到它会直接跳过（不占用 active、不发任何 turn 事件），
  并且在取消那一刻就兑现它的等待者。`TurnManager.shutdown()` 按「停止受理 → 处理排队 turn
  → resolve pending futures → 取消 worker → 清理」的顺序收尾，`wait()` 超时也会摘掉 waiter。
- **Implementation（前端 turn 状态）：** `frontend/src/stores/session.ts` 拆出
  `activeTurnId`（**只由 `TURN_START` 或后端 `TURN_QUEUE` 快照里的 running 写入**）与
  `queuedTurnIds`；`send()` 不再把 POST 返回的 `turn_id` 当成 active。`stopActiveTurn()`
  已知 active 时精确取消它，还不知道 turn_id 时改为 `POST /api/turns/cancel`（后端只取消 active），
  绝不误伤排队消息。`TURN_END` 归属规则重新审查为：属于当前 active 的必须生效、属于从未开始的
  queued turn 只清理排队登记、active 未知时按去重表收敛、其余迟到事件忽略。
- **Implementation（用量与预算）：** 新增统一内部模型 `ModelUsage(input_tokens / output_tokens /
  total_tokens)`，供应商差异（OpenAI 的 `prompt_tokens/completion_tokens`、Anthropic 的
  `input_tokens/output_tokens`、文本兼容档）全部在 Adapter 层归一化，`AgentLoop` 不再读供应商字段。
  `USAGE` 事件在保留原有 `tokens`（= 输出 token）的同时给出 input / output / total。
  **默认不再使用整轮累计输出 token 作为强制停止条件**（`DEFAULT_TOKEN_BUDGET = 0` 表示不限），
  用户显式配置的预算仍然严格执行；迭代上限、重复失败护栏、provider 自身窗口与单次输出上限照旧。
- **Implementation（上下文预算）：** Adapter 新增 `system_prompt_text()` /
  `protocol_overhead_tokens()` / `tools_in_prompt`，`services/turn_orchestrator.py` 的
  `adapter_prompt_costs()` 把 system prompt（text 档含全部工具说明）、协议开销与工具定义
  真实计入 `TokenBudgetPlanner`，不再恒为 0，也不会把已经拼进 prompt 的工具说明重复计一遍。
- **Implementation（Adapter 生命周期）：** `AppContext` 缓存 adapter / 底层 HTTP client
  （键为凭据 id + 版本 + 端点 + 模型），同一凭据不再每个 Turn 重建连接池；Anthropic 能力探测
  按凭据缓存（默认 1 小时），凭据轮换立即失效；`create_app` 的 lifespan 在关闭时统一 `aclose()`。
- **Implementation（Subagent / EventBus）：** `tools/task_manager.py` 改为「提交即 queued、
  **拿到执行名额之后**才 running」，`await_result` 所有出口清理 waiter，任务记录有数量与 TTL 上限
  并在回收时释放 `full_content`。`api/bus.py` 的订阅者缓冲改为有界：同一 `(类型, turn_id)` 的
  `ASSISTANT` / `USAGE` 只保留最新（累计语义），溢出时先牺牲这类可合并事件，
  `TURN_START` / `TURN_END` / 审批 / 工具生命周期等关键事件优先保留。
- **Implementation（记忆检索与向量）：** `Selector` 新增 `upsert()` / `remove()`，`BM25Backend`
  实现真正的增量（df / doc_len / avgdl 增量维护，打分公式与排序键一个字未改），
  ONNX / 远程向量后端改为维护可复用矩阵（脏标记驱动重建），不再每次 `search()` 都 `np.stack` 全量重组；
  `services/memory_lifecycle.py` 封块后只 `upsert` 新写入的那一条，
  `turn_orchestrator.post_turn` 不再在增量更新之后又全量重建一次。
- **Implementation（数据与安全边界）：** 迁移 12 追加「同一 topic 最多一个开放 fragment」的部分唯一索引，
  迁移前先做**不删数据**的归一化（只归档关闭较早的开放片段），保证旧库仍能启动；
  `storage/db.py` 新增 `transaction()`，「关闭片段 + 写记忆索引」包成原子操作。
  `POST /api/approvals/{id}/respond` 现在真正接收并校验 `turn_id` / `session_id` / `request_digest`，
  前端应答时原样回传，正常审批体验不变。
- **Implementation（前端体验与性能）：** `MarkdownContent.vue` 把解析与显现动画解耦
  （解析按批次节拍，与动画帧率无关；`unified` processor 单例、hljs 结果按 `(lang, code)` 记忆化、
  落定时做一次完整解析）；会话历史改为渐进加载（首屏最近一页 + 向上滚动按游标加载更早，
  游标是 `created_at|id` 复合键，同一时刻写入的消息也不丢不重，插入旧历史时做滚动锚定保持阅读位置）。
- **Implementation（2026-09-20 收尾轮 · 极端时序）：** 四处「只有在极端时序下才会暴露」的状态缺陷：
  ① `api/bus.py` 的事件分类显式化（可合并 `ASSISTANT`/`USAGE`、关键状态转换、控制事件 `RESYNC`），
  缓冲里**全是关键事件**时不再静默丢最旧的一条：仍然保持有界，但会先发一条 `RESYNC`
  告知客户端「这一路事件流已经不完整」，客户端据此重新拉取权威快照；
  ② `TURN_QUEUE` 成为真正的权威快照：既能**恢复**缺失状态，也能**清除**本地已经过期的 active，
  同时 `core/turn.py` 给快照与 `TURN_START` / `TURN_END` 带上单调递增 `revision`，
  新的 `GET /api/turns/queue` 供 resync 使用 —— 旧的权威快照不能覆盖更新的状态；
  ③ `TurnManager.wait(timeout)` 不再删除 / 取消 turn 的 completion future
  （`asyncio.shield`）：某一次等待超时或调用方被取消，都只结束那一次等待，turn 照常跑完并正常 resolve；
  ④ `TaskManager.await_result` 超时返回**真实状态**（排队中报 `queued`，不报 `running`），
  并且兑现 waiter 时传「结局快照」而不是记录对象 —— 任务刚完成就被 retention 回收时，
  waiter 仍然拿得到结果（否则会拿到「done 但没有内容」的假结论）。
  另外 `Selector.select()` 把规则层时效项用到的时钟暴露成可选参数 `now`（`QueryContext` 本来就支持注入）：
  默认仍是当前时间，行为不变；测试 / 评测钉住它之后，「同一份索引、同一时刻」的两次排序才逐位可比
  —— 否则两次调用相隔几微秒，得分会在第 12 位小数上漂移（曾导致约 1/10 的偶发失败）。
- **Tests：** `backend/tests/test_turn_manager.py`、`test_turn_state_sequences.py`、`test_budget_defaults.py`、
  `test_model_usage.py`、`test_context_budget_accuracy.py`、`test_adapter_lifecycle.py`、
  `test_events_backpressure.py`、`test_selector_incremental.py`、`test_memory_selector_wiring.py`、
  `test_session_pagination.py`、`test_db_invariants.py`、`test_approval_binding.py`、`test_subagent.py`；
  前端 `stores/__tests__/turnSequences.test.ts`、`stores/__tests__/historyPagination.test.ts`、
  `components/__tests__/MarkdownStreaming.test.ts`、`stores/__tests__/approvals.test.ts`
- **Tests（2026-09-20 收尾轮追加）：** `backend/tests/test_turn_queue_snapshot.py`（快照 revision 单调、
  事件携带 revision、`GET /api/turns/queue` 与 SSE 快照同源）；`test_events_backpressure.py` 的反例用例
  （满缓冲全是关键事件时不得静默丢失、可合并事件淘汰不触发 resync、事件分类完备性守卫）；
  `test_turn_manager.py` 的 wait 语义用例（超时/取消不破坏 completion future、第二个 waiter 仍拿得到结果）；
  `test_subagent.py` 的 queued/running 超时语义与 retention 竞态用例；
  前端 `turnSequences.test.ts` 的 `TURN_QUEUE` 权威快照用例（清 stale、恢复 running/queued、旧快照不覆盖新状态、
  RESYNC 后重新同步）
- **Known limitations：** 取消仍不能物理中断已经发出的模型 HTTP 请求（返回值会被丢弃，turn 以
  `cancelled` 结束）；流式 Markdown 的单次解析仍是 O(全文长度)，只是频率不再等于动画帧数；
  增量检索的收益依赖后端支持 `supports_incremental`（不支持时自动回退全量重建，行为正确但没有加速）；
  本轮未启动真实浏览器做人工视觉复核（改动以「渲染仍是 source 前缀、逐状态 DOM 一致」为前提）。
- **后续依赖：** 后续新增 turn 状态、用量字段或索引维护路径都应以本节的契约为准。

---

### P11 — 稳定性第二轮收敛：恢复协议与生命周期

- **Status：** completed
- **Implementation（Event / RESYNC 恢复协议）：** `api/bus.py` 把 RESYNC 从「顺便发一条提示」
  升级成**硬边界**：关键事件过载时清空该订阅者失真区间的全部缓冲（含触发溢出的那一条），
  只标记需要 resync，此后只有新事件进入；客户端据此拉权威快照，不会被旧事件改回错误状态。
  `Last-Event-ID` 现在有明确三分法：**在 history 里** → 从下一条续传；**正好是最新一条** → 安静等新事件；
  **不存在 / 已被挤出 / 来自旧实例** → `RESYNC`（不得补发最近几条假装连续）。
  RESYNC 自身会写进 history，因此它是一个**合法游标** —— 客户端把它存成 Last-Event-ID 后重连
  不会被当成未知游标（否则每次重连都会再 RESYNC）。
- **Implementation（权威运行状态快照）：** 新增 `GET /api/runtime/state`，返回
  `instance_id` + `revision` + `turn_queue` + `approvals`（仍在等待的审批）+ `tasks`（仍在跑 / 排队的独立任务）。
  RESYNC 之后前端用它一次性恢复：turn 队列、断线期间错过的审批、仍在进行的独立任务，
  并把服务器已不存在的「运行中」工具卡收口为「连接中断，未收到执行结果」。
  审批的**过期**（timeout → `APPROVAL_RESULT(decision=timeout)`）与**取消**
  （等待的那一轮被 Stop → `APPROVAL_RESULT(decision=cancelled)`）都会通知客户端，
  界面不会一直留着已经不可能被批准的 Allow / Reject。
- **Implementation（版本基准与单一状态来源）：** 快照与 `TURN_START` / `TURN_END` 都带
  `instance_id`：同一实例内比较 `revision`，实例变化（后端重启，revision 从头计数）
  则重置基准并接受新实例状态。前端 Turn 队列只有**一个** apply 入口
  （`applyTurnQueue`）：先按 revision / instance 校验，再一次性落地 `turnQueue` +
  `activeTurnId` + `queuedTurnIds`；陈旧事件**整条**不生效（不再出现「active 用新数据、
  QueueChip 用旧数据」的半应用）。
- **Implementation（Event Ownership）：** 前端按 `turn_id` 判定归属：`subagent:task_x`
  的 `ASSISTANT` / `TOOL_START` / `TOOL_END` 不进入主对话，没有主 turn 在跑时带 turn_id 的
  助手输出同样不进；`USAGE` 按**事件自带的 turn_id** 归属，子任务用量不再记到主 turn 上。
- **Implementation（凭据写入原子性）：** `create` 改为「先校验 endpoint（与更新同一套规则：
  远端必须 HTTPS、明文 HTTP 仅限 loopback）→ 写密钥 → 事务内写元数据 + 审计」，
  任一失败都把刚写的密钥删掉；`update_secret` 先记住旧密钥、写新密钥成功后才提交
  version + 审计，失败则把旧密钥写回。不再出现「有元数据没密钥」或「version 涨了密钥没换」。
- **Implementation（应用生命周期）：** 维护调度改在 FastAPI lifespan startup 启动
  （构造阶段没有 running loop，旧代码在那里 `except RuntimeError: pass`，等于从未启动且不出声），
  新增 `stop()`；`_loop` 的异常隔离覆盖**整个循环体**（旧代码引用了早已不存在的
  `ctx._active_loop`，一次 AttributeError 就会永久杀死调度器，真正状态来源改为 `ctx.turns`）。
  `AppContext.aclose()` 现在是统一关机入口：停维护 → `TurnManager.shutdown()` →
  `TaskManager.shutdown()`（取消在跑任务、兑现所有 waiter、把记录收口成终态）→ 关 adapter/HTTP；
  真实入口 `main.create_app()` 再最后关 DB（避免后台任务还在写时连接先断）。
  `TurnManager` / `TaskManager` 关闭后 `submit()` 直接拒绝，不再接一个永远不会执行的任务。
- **Tests：** `backend/tests/test_event_recovery.py`（边界 / 过期游标 / 续传 / RESYNC 游标身份）、
  `test_runtime_state.py`（快照内容、待审批恢复、审批过期与取消通知）、
  `test_credential_atomicity.py`（create / update 失败注入、endpoint 校验）、
  `test_app_lifecycle.py`（真实 startup/shutdown、维护错误隔离、submit 拒绝、任务取消）；
  前端 `stores/__tests__/eventOwnership.test.ts`（归属 + RESYNC 恢复），
  `stores/__tests__/turnSequences.test.ts` 扩充（stale TURN_START / stale 快照不半应用 / 实例切换）。
- **Known limitations：** 事件流仍可能丢可合并的累计型事件（设计如此，状态可由快照恢复）；
  RESYNC 之后「失真区间」内被丢掉的关键事件不再回放，其状态由 `/api/runtime/state` 覆盖。
- **后续依赖：** 新增任何「丢一次事件就会让界面永久停在错误状态」的东西，
  都必须同时进 `/api/runtime/state`。

---

### P12 — 稳定性最终收尾：RESYNC 闭环、快照核对与组合更新

- **Status：** completed
- **Implementation（RESYNC 闭环）：** 两条 RESYNC 来源（缓冲过载 / 游标过期）现在共用同一套恢复身份：
  两者产生的 RESYNC 都写进 history，因此客户端把它存成 `Last-Event-ID` 之后重连能被识别，
  不会「每次重连都再 RESYNC」。前端新增 `resyncing` 状态与**同步缓冲**：
  收到 RESYNC 后进入 resyncing → 期间到达的实时事件先缓存 → 拉权威快照并完整应用 →
  再按到达顺序补放缓冲事件 → 回到 normal；同一时间只允许一个同步在跑
  （同步期间再来 RESYNC 只做标记，完成后补一次），既不丢事件也不会被旧 snapshot 覆盖。
  同步失败进入 `failed` 并如实保留错误，不假装已同步。
- **Implementation（快照核对语义）：** 恢复不再「只追加」，而是按状态性质分别处理：
  Turn 队列 **replace**（含 revision / instance 校验）、审批 **reconcile**
  （服务器没列出的 = 已不再 pending，本地移除）、独立任务 **reconcile**
  （不在活动集合里的 running/queued 卡片收口为「结果未收到」）、工具 **reconcile**
  （`/api/runtime/state.tools` 报告此刻真正在跑的工具，不在其中的「运行中」卡片收口）。
  `AgentLoop.active_tools()` 提供该列表，`TOOL_END` 丢失 + 重连后工具卡不会再永久转圈。
- **Implementation（审批路由统一）：** 实时 `APPROVAL_REQUIRED` 与 RESYNC 恢复出来的 pending approval
  走同一个入口 `handleApprovalRequired()`：`kind=continue` 一律进 ContinueBar，
  其余进审批队列（恢复时不抢焦点）。审批身份仍由服务端掌握：应答只需 `approval_id`。
- **Implementation（事件归属补全）：** `WARNING` / `ERROR` 也按 turn_id 判归属：
  `subagent:*` 的警告与错误不再升级成主会话的全局提示 / 错误（它们由任务卡表达）。
- **Implementation（凭据组合更新）：** 新增 `CredentialStore.reconfigure()`，把
  secret + endpoint + 其他元数据当成**一个**操作：校验全部输入（endpoint 变化必须重新输入 secret
  并显式确认）→ 先写密钥 → 事务内写元数据 + 推进 version + 审计；任一步失败都回滚两边。
  如果连密钥回滚都失败，记 **CRITICAL** 并抛 `CredentialRollbackError`（带两个原因），
  绝不让调用方以为「只是没更新」。API 的 `PATCH /api/credentials/{id}` 改为只调用它，
  不再把 `update_secret` 与 `update_metadata` 串起来。
- **Implementation（Maintenance 避让）：** 是否避让主任务改为判断「有没有 active Turn」
  （`ctx.turns.active is not None`），而不是 active AgentLoop —— Turn 处于上下文准备 /
  结果保存 / 收尾阶段时 loop 可能已经不在，但主任务并没有结束。
- **Tests：** `backend/tests/test_event_recovery.py`（过期游标产生的 RESYNC 也是合法游标）、
  `test_active_tools.py`（只有真正在跑的工具被报告）、`test_credential_atomicity.py`
  （组合更新的成功 / 失败回滚 / 回滚失败必须大声报错）、`test_app_lifecycle.py`
  （维护避让任何 active Turn）、`test_runtime_state.py`（应答只需 approval_id）；
  前端 `stores/__tests__/resyncProtocol.test.ts`（同步缓冲、单飞、快照核对、实例切换、失败状态）、
  `approvalRouting.test.ts`（continue 审批两条路径一致）、`eventOwnership.test.ts`（WARNING/ERROR 归属）。
- **Known limitations：** 同步缓冲只在内存里：如果同步期间进程被杀，缓冲事件随之消失
  （下一次连接仍会走 RESYNC + 快照，状态最终一致）。失真区间内被丢弃的关键事件依然回放不了，
  由快照覆盖。
- **后续依赖：** 无下游；这是这一系列稳定性修复的收尾。

---

### P13 — 工具终态可恢复：过程可丢，结论不可丢

- **Status：** completed
- **Implementation（权威状态）：** 新增 `agent/core/tool_state.py`：进程级、纯内存、有界的
  `ToolExecutionState`（active + recent terminal tool executions）。每条只留
  `tool_call_id` / `tool_name` / `turn_id` / `status`（running / success / failed / cancelled）/
  起止时间 / 一行错误摘要 —— 不保存完整工具输出、不落盘、不建事件日志。
  身份是 `(turn_id, tool_call_id)`：同一次调用原位更新，跨 Turn 不会互相污染，
  同一个工具名在一轮里连续调用多次也能区分。
- **Implementation（写入顺序）：** `AgentLoop` 在 `tool/start` / `tool/end` 管线里**先**写权威状态、
  **再**发 `TOOL_START` / `TOOL_END`。所以「`TOOL_END` 丢在失真区间里」不会让服务器已经知道的
  终态一起消失。`TOOL_END` 载荷新增 `status`（success / failed / cancelled）：
  工具注册表的取消路径显式标记 cancelled，前端不必从 `ok=false` 反推「取消还是失败」。
- **Implementation（恢复入口）：** `GET /api/runtime/state` 的 `tools` 字段由这份状态生成
  （活工具 + 最近结束的工具），只包含主 Turn：`subagent:*` 等内部循环的调用既不返回、
  也不进 UI —— 产品上独立任务的最小显示单位仍然是「独立任务」。
  属于 active Turn 的 running 记录如实报 running；所属 Turn 已经不在的记录报 unknown
  （服务器不能替它保证「还在跑」）。
- **Implementation（前端核对）：** 工具卡数据模型区分 running / success / failed / cancelled / unknown
  （`session.reconcileTools()`）。优先级：服务器给出的终态 > 本地过期的「运行中」；
  快照之后到达的实时事件 > 快照（同步缓冲按到达顺序补放）；服务器说还在跑时不把已有终态
  降级回运行中；别的 Turn 的 `tool_call_id` 不会被当成这次调用的事实。`TURN_END` 时残余的
  「运行中」卡片也会收口 —— 但下一次快照只要有真实终态，就会改写回 success / failed / cancelled。
- **接受的限制 1：** RESYNC 期间到达的新事件只临时存在内存里。恢复过程中应用被强制结束，
  下次启动重新获取完整快照（不尝试恢复上一次未完成的 resync buffer），不持久化临时缓冲。
- **接受的限制 2：** Subagent 只恢复 Task 级状态（queued / running / done / failed），
  不恢复、也不展示 subagent 内部单个 Tool 的执行明细。
- **保证：** 主 Turn 中只要服务器仍然知道工具最终状态，即使实时 `TOOL_END` 丢失，
  RESYNC 后仍能恢复成 success / failed / cancelled；只有服务器自己也无法确认
  （记录已按 retention 回收、或进程重启过）时，界面才显示「结果未收到」。
- **Tests：** `backend/tests/test_tool_state.py`（权威状态语义与 retention：active Turn 的终态不被提前清理）、
  `backend/tests/test_tool_recovery.py`（`TOOL_END` 丢失后 snapshot 仍能恢复终态、取消不等于失败、
  stale running 只能报 unknown、子任务内部工具不进主 snapshot）、
  `frontend/src/stores/__tests__/toolRecovery.test.ts`（快照核对、同名多次调用、跨 Turn 不串、
  快照与缓冲事件的顺序、`TURN_END` 收敛）、
  `frontend/src/components/__tests__/MessageItem.test.ts`（「已取消」与「结果未收到」有自己的文案）。
- **Known limitations：** 这份状态是**进程内**的：后端重启后它为空，此时界面显示「结果未收到」
  是诚实答案（不伪造终态）；terminal 记录按 TTL 与最大条数回收，回收之后同样回落为「结果未收到」。
  本轮不建设工具执行历史数据库，也不扩大 Subagent 的展示范围。
- **后续依赖：** 无下游。

---

### P14 — Execution Narrative（执行叙事层，2026-09-22）

- **Status：** completed
- **Implementation（表达与事实分离）：** 新增 `agent/core/narrative.py`：`Narrative(kind, text, explanation, silent)`
  与白名单解析 `parse_narrative`（只认 `kind` / `text` / `explanation`，其它键丢弃、长度截断、
  过 `trace/redact.redact_text`）。模型在工具调用参数里携带保留字段 `_qio`；
  `NativeAdapter` / `TextAdapter` 解析时用 `split_narrative_arguments` **剥离**它，
  所以工具参数、风险判断、沙箱判定、审批摘要都看不到模型文案。
  `ToolRegistry.specs()` 在每份 parameters 副本里声明可选的 `_qio` 属性（不加入 required）。
- **Implementation（一批一条 + 先说明再执行）：** `AgentLoop` 新增 `narrative_sink` / `narrative_settler`；
  每个工具批次在取消检查之后、真正执行之前取**第一条有效叙事**，注入的 sink 先落库再广播
  `NARRATIVE`，批次结束后把真实终态与耗时补写进同一行的 `raw.calls`（系统生成，模型改不了）。
  子 agent / 维护循环不注入 sink，因此不会往主对话写过程说明。
- **Implementation（审批 explanation）：** `ToolRegistry.execute` 用 ContextVar 挂上"当前调用的叙事"
  （并行调用各 task 隔离）；`ApprovalService.request` 只在载荷自己没有 explanation 时补上模型文案。
  `description` / `access` / `capabilities` / `scope` / `detail` / 事件载荷一个字段都不动，
  审批的单次使用、过期、摘要校验、拒绝/超时/取消语义完全不变。
- **Implementation（落库与恢复）：** 叙事写入 `messages`（`role='assistant'`、`content_type='narrative'`、
  `raw={narrative, calls}`），不新增迁移；`GET /api/runtime/state.narratives` 与历史分页
  （`raw` 一起返回）构成恢复路径，前端按 `narrative_id` 三层去重；
  `FragmentManager.content_tokens` 排除叙事行，展示文本不会让片段提前封存。
- **Implementation（前端）：** 新增 `NarrativeStage.vue`：一行叙事 = 一个**默认收起**的抽屉头，
  收纳它之后、下一行叙事之前的调用卡；折叠头由系统状态显示
  `N 次调用 · 耗时` / `N 运行中` / `N 失败` / `N 已取消`（异常不会因为收纳而消失）；
  历史里抽屉内容来自 `raw.calls` 的系统生成调用摘要。机械提示「正在使用工具」已移除，
  `MessageStream` 不再显示它；`ApprovalModal.vue` 把模型说明单独成段并标注「QIO 的说明」。
- **Tests：** `backend/tests/test_execution_narrative.py`（silent / 执行前顺序 / 批量合并 /
  审批 explanation 与事实不变 / 并行不串味 / `_qio` 不进参数 / 批次结算 / runtime 与历史恢复 /
  容量排除 / 完整一轮端到端）、`frontend/src/stores/__tests__/executionNarrative.test.ts`、
  `frontend/src/components/__tests__/ExecutionNarrativeDrawer.test.ts`、
  `frontend/src/components/__tests__/ApprovalModal.test.ts`。
- **Known limitations：**
  - 工具卡本身**不写入 `messages` 表**（沿用既有边界）：历史里抽屉打开看到的是
    系统生成的调用摘要。2026-09-24 起，工具调用另存于 `tool_records` 表，
    历史与实时都能按记录 id 展开看完整参数与输出（见本文末「工具调用历史」一节）。
  - 叙事是展示记录，不参与记忆整理判断；它会计入片段的摘要输入但不计入容量。
  - 模型可以不写叙事（silent 是默认）；此时不会出现任何过程文案，只保留工具卡。
  - 叙事在取消/失败的轮次里会留在历史中（如实反映"说明过、没做完"）。
- **后续依赖：** 无下游。设计见 `docs/superpowers/specs/2026-09-22-execution-narrative-design.md`。

---

### P15 — 应用内联网更新（Updater，2026-09-22）

- **Status：** completed
- **Implementation（真机闭环，2026-09-22）：** 已发布 v0.1.3 → v0.1.6 六个版本，更新源为
  GitHub Releases（`releases/latest/download/latest.json`）。**应用内一键更新已在真机跑通**：
  0.1.5 内点「检查更新」→ 发现 0.1.6 → 下载 → 签名校验通过 → 安装 → 重启后版本为 0.1.6。
  途中修掉三个真实故障：更新插件只读 `TAURI_SIGNING_PRIVATE_KEY`（导致"有安装包、无签名"）、
  后端孤儿进程锁住 `qio-backend.exe` 让安装中止（改用 Windows Job Object 根治）、
  以及残留的失效代理地址让请求一直等（改为读系统代理 + 采用前做连通测试）。
- **Implementation（运行时）：** Tauri 官方 `tauri-plugin-updater`（检查 / 下载 / **签名校验** / 安装）
  + `tauri-plugin-process`（装完重启）；`main.rs` 注册两个插件，`capabilities/default.json` 增加
  `updater:default` 与 `process:allow-restart`，`tauri.conf.json` 开启 `bundle.createUpdaterArtifacts`
  并配置 `plugins.updater`（endpoints = GitHub Releases 的 latest.json，pubkey = 用户生成的 minisign 公钥）。
  更新请求在 Rust 侧发出，**前端 CSP 未放宽**。
- **Implementation（前端）：** `services/updater.ts`（唯一插件入口 + 版本比较 + 错误分类）、
  `stores/updater.ts`（状态机：idle/checking/up-to-date/available/downloading/ready/failed）、
  `components/UpdateCard.vue`（设置 → 数据与维护）；启动后静默检查一次 + 每 24 小时一次，
  设置里可关；**只检查不自动下载**，下载与安装必须用户点击；浏览器开发预览不触发检查。
- **Implementation（发布侧）：** `scripts/build_installer.ps1` 增加第 4 步：校验签名环境变量 →
  产出 `.sig` → 生成 `latest.json` → 复制 exe/.sig/latest.json 到 `dist/` → 汇总 `SHA256SUMS.txt`；
  新增 `scripts/publish_release.ps1`（gh release create + 上传三个资产）。
- **Tests：** `frontend/src/stores/__tests__/updater.test.ts`（版本比较、错误分类、
  状态机正反两条路径、检查失败不得显示成"已是最新"、只有用户点击才 relaunch）、
  `frontend/src/components/__tests__/UpdateCard.test.ts`（各状态按钮文案与可用性）。
- **Known limitations：**
  - 0.1.2 及更早版本没有更新器代码，升级到 0.1.3 需要**手动安装一次**；0.1.5 起已在真机验证；
  - 代理自动适配覆盖"系统代理模式"与 TUN/全局模式的 VPN；**PAC 自动配置脚本、浏览器插件式
    VPN、需要用户名密码的代理不覆盖**（任何非浏览器程序都不行）；
  - 更新包未做 Authenticode 代码签名，安装时 Windows 可能仍提示「已保护你的电脑」；
  - 私钥与口令是信任根：泄露即等于所有已安装实例可被投毒；丢失即无法再发签名更新；
  - 不做差分更新、不做 beta/stable 多通道、不做回滚。
- **后续依赖：** 发布 v0.1.3 时补齐端到端验证。
  设计与实现计划见 `docs/superpowers/specs/2026-09-22-updater-design.md`、
  `docs/superpowers/plans/2026-09-22-updater.md`。

---

## 尚未完成

这些是最容易让后续 Agent 误判的地方，明确列出来：

- **斜杠命令体系**：未实现。工具创建没有独立的「入口页面」——按第三阶段的入口原则，
  它在对话里发生（说明需求 → Agent 调 `create_tool` → 同一张工具创建卡推进到「已创建」），
  设置页只放长期配置（电脑操控权限、联网通道），不承担这个动作。
- **对话页内嵌的记忆/知识面板**：未实现，面板在星球页详情里；对话页只在回答完成后
  显示高影响知识候选卡（保存 / 修改 / 忽略），不做成常驻面板。
- **工具创建的真实端到端未跑**：`TOOL_CREATE_STATUS` 的九个阶段、同一 `group_id` 一张卡、
  失败原因都有自动化测试（进程内 + 真实 subprocess 沙箱），但「真实模型提议 → 真实沙箱测试 →
  两次审批 → 注册 → 立刻可用」这条完整链路没有在真实运行里走通。
- **工具开发第一阶段的三个未收口项**（2026-09-30 收尾后的现状，详见文末「第一阶段收尾」一节）：
  1. **最终结论的事实校正已做完（后端 + 界面标记）**：后端有「每轮事实台账 +
     `declare_completion` 核对 + 收尾事实注记」，界面上的「后端已核对」那一行也已上线
     （见文末「结论标记与已创建文案」一节）。仍未做的只剩「未完成任务入口与审批跨重启语义」。
  2. **测试前授权与真实能力策略没补**：`dev_run_tests` 在拿到任何授权之前就执行生成代码，
     测试用的隔离数据（临时库 / 临时目录 / 模拟服务）也没做。
  3. **已保存对话的原文检索没做**：开放片段与摘要失败的内容搜不到（`memory_search` 只覆盖
     封存后摘要成功的部分）。
- **能力降级的真实触发未跑**：`CAPABILITY` / `FALLBACK` 的语义有测试（native 不提示、text 只在
  模式变化那一次提示、unsupported 不发降级），但没有用真实「不支持原生工具调用」的模型跑过。
- **子 agent 长任务未验证**：独立任务卡的 queued / running / done / failed 与结果回传有测试，
  但耗时很久（分钟级）的真实子任务没有跑过。
- **片段级检索偏置（已评测，决定不实现）**：Planet「从这里开始」/ Agent
  `continue_from_fragment` 只改变 Focus 与当前位置，**不**改变记忆检索的排序权重
  （检索侧只有话题级亲和 `anchor_topic_id`）。离线 Anchor Continuation Eval
  （`agent/eval/anchor_eval.py` + `backend/evals/anchor_continuation/` 下的 case 集）对比了
  baseline（Focus + 语义检索 + 身份去重）、focus_only 与 anchor_distance（按序数距离加权）：
  距离偏置 recall@5 无提升（1.00 → 1.00）、MRR 反而下降（0.667 → 0.633）、
  wrong-memory injection 翻倍（0.20 → 0.40）、anchor distraction 上升（0.333 → 0.667），
  因此**不实现**距离偏置。基线数字与结论存于 `backend/evals/baseline.json`，
  `tests/test_anchor_eval.py` 会守住这个决策（哪天评测翻盘会直接测试失败，强制重新决策）。
- **取消只掐客户端这一头**：取消现在会**真的中断**正在等待的模型请求（不再等它跑完再丢结果），
  单飞队列里的下一条消息也会立刻开始；但服务端是否立刻停止生成由供应商决定 ——
  「不再占用等待时间、不再堵住下一条消息」有保证，「不再产生费用」没有。
- **Trace 时长归因缺口**：极端情况下 turn 总时长与已记录的模型/工具耗时差距很大
  （实测一次 51.5 秒的 turn 只记录了 1.7 秒模型耗时），无法从 Trace 解释时间去向。
- **无嵌入模型时话题预判变弱**：缺本地 ONNX 模型时降级到规则层，关键词重叠分数被 1-gram/2-gram
  分词稀释；阈值调整需要 eval 支撑（见 P3）。
- **内置嵌入模型已有离线基线，但只有两小套用例**：`python -m agent.eval.run --embedding onnx`
  把真实模型接进话题判定（12 条）与检索（8 条）两套评测；结果与明细留在做那次评测的分支上，
  没有随主线提交（要复核就重跑这条命令）。评测数字指向两处需要重新校准：
  话题阈值 0.7 对真实模型偏严（「继续聊 SQLite 迁移」被判成新话题），检索排序权重是在
  「关键词召回候选很少」的前提下调的（向量召回把「近但无关」的条目也带进候选，`person_entity`
  与 `cross_topic_recall` 因此从对变错）。锚点延续评测没有接模型；用例规模只够看方向，
  不是生产准确率。
- **记忆的类别化衰减**：只对已有可靠元数据（知识条目 vs 片段）做差异化；未引入模型生成的记忆分类字段。
- **多用户/多会话并发 Agent Server**：明确不做。当前是单机、单用户的 single-flight 主 turn。
- **非 Windows 平台**：keyring 与桌面壳只在 Windows 验证，Linux/macOS 未验证。
- **联网搜索通道（2026-09-12 重做）**：免密钥搜索改为「Exa / Parallel 免费 MCP +
  DuckDuckGo HTML」，实测可用（前者返回结构化结果，后者中文结果正常但会限流）；
  必应/百度降级为最后兜底，并保留两道闸门（解析抓 `h2 > a`、相关性闸门、反爬通道
  冷却 10 分钟，见 `services/params.py::SEARCH`）。公共 SearXNG 实例实测全部被
  Anubis/Substation 拦截，故只支持自建实例（设置 → 高级 填 URL）；博查 Key 仍然最稳。
  免密钥开关在「设置 → 模型与联网」，默认开启、保存即生效。
- **搜索设置的即时生效**：此前保存 SearXNG/博查只写 settings 表，运行中的
  `SearchService` 不更新（要重启后端才生效）——已修为保存时调用
  `AppContext.apply_search_settings()`。
- **单 turn token 已有输入/输出分解，前端展示仍是总量**：后端 `USAGE` / `TURN_END` 现在给出
  `input_tokens` / `output_tokens` / `total_tokens`（见 P10 的 `ModelUsage`），但界面目前只显示
  总量，不展示分解数字（需要界面设计后再开）。
- **审批成功路径未做端到端注入**：approvals 的失败/404/网络断开（保留待审批项 + 可重试 +
  「未做出任何授权」提示）已用真实后端无头浏览器验证；成功出队路径目前只有前端单测覆盖
  （后端没有可用的「造一个待审批项」测试注入口）。
- **无障碍只做了自动化抽样**：Tab 顺序、focus-visible、对比度（暗/亮）已用无头浏览器脚本检查，
  未做完整 WCAG 审计，也未做屏幕阅读器实测。
- **星球浏览体验只做了结构 + 静帧验证**：同屏上限、旋转推动话题流、反向连续性、对象池复用
  都有自动化测试与真实运行证据（`scripts/baseline/planet-phase2-probe.mjs`），
  但惯性曲线、触控板手势、逐帧「看不到数据替换」与真机 GPU 帧率**未验证**；
  完整清单见 `docs/release-planet-phase2.md` 的「剩余问题」。
- **第四阶段（视觉语言）尚未收口的项**（2026-09-20 收口一轮后更新）：
  - **工具创建卡只统一了语言、没有真实端到端跑通**：家族骨架（`.qio-card` + `data-state` + 状态徽章 +
    真实高度过渡的展开详情）已在代码与单测层面成立，但验收脚本里「界面上没有工具创建卡」——
    需要真实模型触发创建流程才能看到它，这条链路仍未在本阶段跑过（与本文档前面 M10 的已知限制同源）。
  - **Planet 关键转场只在无头软件渲染下逐帧验证**：`scripts/baseline/qa/phase4.mjs` 用
    swiftshader 采到了 0.13 → 1.00 的长大与 1.00 → 0.18 的收拢，但没有在真机 GPU、120Hz 屏、
    触控板上重新测量帧率与手感；`?planetdemo=slow` 只能逐帧看，不能替代手感评估。
  - **首次打开的冷启动耗时没有优化**：实测 WebGL 初始化 + 话题装配要 1.5–3.4s（软件渲染更久）。
    本阶段只保证「这段时间不会把转场动画吞掉」（等数据与渲染就绪再开始展开），没有缩短它。
  - **动效手感未经真人评审**：所有判断来自令牌、截图与帧序列，需要真实用户反馈来定
    「高频弹性是否合适、中频是否偏慢」。
  - **收缩中途的画质已查清（2026-09-20）**：曾怀疑「中间段间歇性断续」，pilot 用两条独立证据
    否掉了 —— 冻结渲染下确实能复现（那是栅格化时机造成的假象：画面冻住后浏览器不再按动画尺度
    重新栅格化），真实动画里「缩放合成层」（现状）与「缩放投影」（候选）的墨迹量逐档相同
    （40–60px 档 4.6 vs 4.5、350–1000px 档 18.5 vs 18.4）。那条「把缩小搬进渲染器」的路数
    几何等价性已验证（目标 300px 量到 302px），但它换来的是「主线程一卡动画就卡」
    （探针轮询下一度 p50 34ms vs 16ms），因此**决定不做**；尾段钉死 1:1 保留作为加固。
   判断这类问题必须以**真实动画**的逐帧数据为准，冻结渲染的静态对照会放大差异。

---

## 核对方法

文档里的状态可以自己复核，不需要信任本文件：

```powershell
# 1. 文档一致性检查（里程碑状态、被引用的路径与命令是否存在、是否出现硬编码数字）
python scripts/check_docs.py

# 2. 后端测试（uv 环境）
cd backend
uv run --frozen pytest

# 3. 评测基线（离线、确定性）
uv run --frozen python -m agent.eval.run

# 4. 前端
cd ..\frontend
npm ci
npx vue-tsc --noEmit
npm test
```

命令与 CI 一致，见 `.github/workflows/ci.yml`。

---

## 本轮变更：Fragment 重设计（2026-09-21 起）

> 任务书：`docs/superpowers/specs/2026-09-21-fragment-redesign-spec.md`
> 实施计划与逐条清单：`docs/superpowers/plans/2026-09-21-fragment-redesign.md`
> 配套笔记：写入入口清单、分段边界评测报告（`docs/superpowers/notes/`）

这一轮把「一轮对话属于哪个 Topic / Fragment」从「写入时临时看 Anchor」改成**持久绑定**，
并把封存、派生数据、历史路径、分段边界逐层拆开。按阶段记状态（口径同上表）：

| 阶段 | Status | 说明 |
| --- | --- | --- |
| 1 固定轮次归属 + 安全接续 | completed | 绑定与接续意图两张表；点击历史只登记、执行时才原子交接；队列顺序与幂等有测试；接续提示与取消已上界面。**工具自己换话题是唯一受控例外**：`create_topic` / `switch_topic` 整轮跟着走，用户导航仍一步不动 |
| 2 封存与派生分开 | completed | 派生任务表（退避、重启恢复、幂等）；封存不等模型；摘要失败不影响对话；索引失败不再回滚封存 |
| 3 历史关系与路径上下文 | completed | 祖先链（深度/环保护）、路径前提与「仅参考」标注、知识与其他话题实体卡的范围标注、旧数据迁移 16 |
| 4 边界策略与容量 | partial | 确定性规则（容量 / 明确开工 / 短确认 / 同阶段修正）可运行、三档模式（off/shadow/enabled，默认 shadow）、离线评测；**Embedding 语义信号只做到「有模型就观察」**，尚未校准 |
| 5 界面适配 | partial | 接续提示与取消、设置页分段文案与单段长度已完成；生成中改选等状态有实现但视觉重检未全部覆盖 |

**阶段 1 的缺口与修复（2026-09-22 · 工具导航归属）**：阶段 1 把「本轮归属在轮前
固定」落到绑定上时，把「模型自己用 `create_topic` / `switch_topic` 换话题」和
「用户在本轮跑着的时候改导航」一起排除了 —— 于是建话题的那一轮（用户提问 +
助手回答）留在**上一个**话题里：新话题的历史从第二轮才开始，追问看不到前提，
`memory_search` 也搜不到（索引在片段封存时才生成）。现在把两者按来源分开：

- 工具在 turn 派生的 task 里执行，能读到本轮的 `turn_id`；另一个请求（用户导航）
  读不到，所以登记不上 —— 判定不靠 trace（trace 可关闭，靠它会静默退化）。
- 工具换话题登记在 `services/navigation.py`（`note_tool_navigation`），由
  `services/turn_orchestrator.py::persist` 在写回答前落实：先把「本轮自己的工具
  换过话题」当成一次受控改向（`TurnBindingService.rebind_topic_from_tool_nav`，
  只允许 `write_state='open'`），再把用户消息搬进新话题的开放片段
  （`MemoryWriter.move_message`，两个片段的 `start/end_message_id` 都按实际消息
  重算）。两个前提缺一不可：Anchor 仍停在那个话题、本轮绑定还没收尾；任何一条
  不成立都退回原绑定（目标片段由 `get_or_create_open` 懒创建）。
- 用户导航、失败/取消、`continue_from_fragment`（只影响后续提交）行为不变。
- Tests：`backend/tests/test_tool_nav_rebind.py`（工具建话题 / 工具切话题 / 本轮内
  用户导航三条），并同步更新了 `test_turn_short_term.py`、
  `test_p7_message_uniqueness.py` 里原先把旧行为写死的断言。

**向量模型（内置已落地）**：默认内置 **fp32** ONNX（`model.onnx`，约 90MB）。
构建前用 `scripts/models/fetch_model.py` 把模型（含清单、许可证、声明）抓到
`frontend/src-tauri/resources/models/`，由 `bundle.resources` 打进安装包；
桌面壳启动时按内容指纹复制到 `%APPDATA%\qio\models\` 并注入 `QIO_MODELS_DIR`，
失败不阻塞启动（退回 BM25 并在日志写明原因）。向量缓存按「身份 + 维度」过滤，
换档位必须重新编码。清单生成：`scripts/models/write_manifest.py`；
自检与 fp32/int8 对比：`scripts/models/onnx_ab.py`。

**已知限制（不粉饰）**

- 语义阶段切分没有经过校准，默认不实际切分；Embedding 只做观察与降级；
- 进程重启后**排队中的消息**不会自动恢复（队列未持久化）；
- 旧数据里 `relation_type='unknown'` 的片段没有路径隔离能力；
- 打包时需附模型许可证与 NOTICE（上游 BAAI/bge-small-zh-v1.5 为 MIT）。

---

## 本轮变更：工具调用历史（2026-09-24）

> 设计：`docs/superpowers/specs/2026-09-24-tool-record-history-design.md`
> 计划：`docs/superpowers/plans/2026-09-24-tool-record-history.md`

**问题**：工具卡只活在实时事件流里。刷新、重开应用、切话题之后，那一轮"调了哪些工具、
各自成功还是失败、为什么失败"在对话里就没了；而且实时卡片的展开内容来自
`TOOL_END.content_preview`（后端只给 200 字），**实时与历史都看不到完整输出**。

**改法**：新增一张专用表 `tool_records`（迁移 20）+ 一个按 id 取全文的接口，
写入挂在工具的权威终态上，历史与实时两种卡片共用同一条取全文的路径。

| 部分 | 实现 | Tests |
| --- | --- | --- |
| 存储 | 迁移 20 建 `tool_records`（打码后的参数与输出全文、状态、一行错误、耗时、截断与"输出不在库里"的原因）；`storage/tool_records.py` 集中写入 / 查询 / 清理 | `test_tool_records.py` |
| 写入 | `tool/result` 暂存 call 与 result，`tool/end` 拿权威终态后落库（取消与失败要分开）；`AppContext._record_tool_call` 同时写轨迹审计行与历史行，返回记录 id | `test_tool_record_wiring.py`、`test_tool_call_audit.py` |
| 读取 | 历史接口按 `turn_id` 附带 400 字预览；`GET /api/tool-records/{id}` 取参数与输出全文（记录不存在 404，输出被清理时 `output_missing=1`） | `test_tool_record_api.py` |
| 设置 | `GET/PUT /api/settings/tools`：`tools.record_outputs`（默认开）与 `tools.output_retention_days`（默认 90，0 = 永久）；启动、后台维护、保存设置三处清理 | `test_settings_tools_api.py` |
| 前端 | 历史加载把记录按时间插进对应轮次（跨页按 id 去重）；卡片展开时才取全文，显示「参数」与「输出」两段；未保存 / 已清理 / 截断各有文案 | `toolRecordHistory.test.ts`、`MessageItem.test.ts`、`SettingsView.test.ts` |

**保留期口径**：到期只清空**输出全文**（`missing_reason='retention'`），参数、状态、
失败原因、耗时继续保留 —— 三个月后仍能查到"当时哪个工具失败、为什么失败"。

**边界（与用户确认过的产品决定）**：这些记录**只给人看** —— 不进上下文、不进片段摘要、
不进检索索引，模型检索不到；子任务内部的工具调用不入表（与"子任务不进主对话"一致）。

**实拍证据**：隔离实例里写两条真实记录后刷新，历史卡片展开可见参数、
密钥已打码为 `***redacted***`、输出为全文（含第 25 行）；窄窗口（820px）下卡片
横向不溢出。脚本 `scripts/ui-catalog/tool-history.mjs`，图在
`frontend/e2e-shots/ui-catalog/toolhist/`。

**已知限制（不粉饰）**

- 打码是规则匹配（`trace/redact.py`）：规则之外的密钥形态仍可能落库；
  关掉「保存输出全文」是唯一彻底的做法。
- 库会随时间增长。默认 90 天只清输出；参数与错误永远保留（体积很小），
  目前没有"整条记录自动删除"的规则。
- 本功能上线前的历史调用没有记录，不做回填。
- 单条参数与输出上限 4 万字（与工具自身的输出上限对齐），超出会截断并标注。

---

## 本轮变更：启动加固（2026-09-25）

**问题（真机事故）**：0.1.8 首次启动时窗口一片空白，旁边弹出 `reg.exe` 的
「应用程序无法正常启动(0xc0000142)」。核对证据：外壳 15:05:41 启动，日志里第一条记录
出现在 15:07:23（**启动被拖住 1 分 42 秒**），后端 15:07:23 才被拉起；同一错误码
（`ExitStatus(3221225794)`）也出现在外壳更新前调用的 `taskkill.exe` 上。也就是说：
白窗是"启动被拖住"的等待状态，而拖住它的是**系统级**的子进程初始化失败
（`reg.exe` 报错后会留一个必须先点掉的模态框，外壳在等它退出）。

| 改法 | 实现 | Tests |
| --- | --- | --- |
| 跑系统命令带**硬超时**：超时杀子进程（模态框随之关闭）并按"拿不到"处理 | `frontend/src-tauri/src/main.rs` 新增 `run_command_with_timeout`；`reg query`（读系统代理）3 秒、`taskkill`（更新前结束后端）5 秒 | `hanging_command_is_killed_after_the_timeout`、`fast_command_still_returns_its_output` |
| 探测失败即直连，不再等待 | `system_proxy()` 拿不到就返回 None → 更新按直连走（原本就有直连兜底），并写一条 WARN | 同上（超时返回 None 就是这条路径） |
| 启动耗时进日志，下次能直接归因 | setup 里记录并打印「代理探测 / 内置模型 / 拉起后端 / 合计」四段耗时 | 随应用启动实测 |
| 后端没应答时**不再白屏** | 新增 `services/boot.ts::waitForBackend`（解析连接信息 + 轮询 `/api/health`，单次探测带超时）；`App.vue` 增加启动状态：等待时显示「正在启动 QIO 后端…（已等 N 秒）」，超过 60 秒显示原因、日志位置与「重试」，后端应答后才渲染主界面并建立事件流 | `frontend/src/__tests__/appBoot.test.ts`、`frontend/src/services/__tests__/boot.test.ts` |

**实拍证据**（`scripts/ui-catalog/boot-gate.mjs`，图在 `frontend/e2e-shots/ui-catalog/bootgate/`）：

- 后端故意不起来：界面显示「正在启动 QIO 后端… 已等 N 秒」；
- 等满 60 秒：「QIO 后端没有应答（已经等了 60 秒）／后端没有应答（最后一条错误：Failed to fetch）／
  也可能只是启动很慢：点「重试」会接着等。日志：…QIO.log／重试」；
- 后端起来后：同一套代码自己渲染出主界面（不靠刷新）。

**已知限制（不粉饰）**

- `0xC0000142` 本身是系统级故障（同一台机器上 `reg.exe`、`taskkill.exe` 都中过），
  应用只能做到"不因为它卡死"，不能修好本机环境。
- 代理探测仍在启动路径上（它决定后端继承的代理环境变量），现在最坏 3 秒/次；
  启动日志会如实写出这一段花了多久。
- 启动等待的默认上限是 60 秒：超过就显示可重试的失败说明（后端只是很慢时，重试会接着等）。

---

## 本轮变更：工具失败与静默失败加固（2026-09-24）

> 规格：`docs/superpowers/specs/2026-09-24-tool-failure-hardening-spec.md`
> 计划：`docs/superpowers/plans/2026-09-24-tool-failure-hardening.md`

起因是一次真实安装实例的运行数据核对：工具调用失败里，一批是设备自身没准备好
（默认工作区目录不存在、开发工作区重启后不认），一批是等待方式不对（审批窗口被
工具超时截断、审批结局只有一句「未获批准」），还有一批是原因说不清（抓取把脚本
渲染的页面报成登录墙、底层异常没有文本时只剩半句）。另有两轮以**空白回答**结束，
用户在界面上看不到任何解释。逐条修法如下。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 默认工作区根目录不存在 → 相对路径的文件工具全报路径错误 | `ensure_dirs` 一起建工作区目录；沙箱新增 `ensure_root()`，启动与保存设置时各建一次（建不出来只记日志，工具照实报错） | `config.py`、`services/computer.py`、`services/app.py`、`api/server.py` | `test_workspace_root.py` |
| 应用重启后 `dev_*` 报「找不到工作区」 | `DevWorkspace` 构造时扫盘回填 `ws_*` 工作区（需求正文从 `request.md` 读回） | `tools/dev_workspace.py` | `test_dev_tools.py` |
| `run_shell` 的审批窗口被 45 秒工具超时截断 | 工具级超时改为「审批窗口 + 执行窗口」；超时/拒绝/取消用 `refusal_reason` 分别表述 | `tools/cmd_tools.py`、`tools/fs_tools.py`、`tools/approval.py`、`tools/registry.py` | `test_cmd_tools.py`、`test_fs_tools.py` |
| 抓取把脚本渲染的页面报成「需要登录或验证」；异常无文本时错误只有半句 | 登录墙只认明确短语；新增「正文由 JavaScript 生成」这一类；连接失败带地址与原因；异常兜底补 cause 说明 | `tools/web_fetch.py`、`tools/registry.py` | `test_web_fetch_tool.py`、`test_tool_pipeline.py` |
| 护栏终止 / 预算停止后回答是空串 | 循环结束前若没有任何最终文本，写出一句说明（哪个工具、失败几次、最后一次原因）；取消轮不补 | `core/loop.py` | `test_loop_continue.py` |
| 失败原因不在轨迹表里 | 迁移 19 给 `tool_calls` 加 `error` 列；循环把失败原因交给审计回调 | `storage/schema.py`、`services/app.py`、`core/loop.py` | `test_tool_call_audit.py` |
| 工具卡把真正的原因折叠成笼统文案 | 失败结论上限从 60 字放到 120 字，超出才截断并提示展开 | `frontend/src/components/MessageItem.vue` | `MessageItem.test.ts` |

**已知限制（不粉饰）**

- 外部站点仍然会抓不到：站点下架、反爬、必须浏览器渲染的页面依旧失败，改动只让
  原因准确。
- 工具调用历史受打码与保留期约束（见文末「工具调用历史」一节）：本功能上线前的
  历史没有记录，不做回填。
- 用户主动取消的轮次不补收尾文本（界面已有「已停止」状态行）。
- `import agent.tools.*` 必须在 `agent.core` 之后：单独导入会触发既有的循环导入
  错误，因此只有 `agent.tools.*` 作为首个模块的单文件测试无法单独收集（整目录运行
  不受影响）。本轮未改这条链路。

---

## 本轮变更：确认之前不外发已存的 Key 与摘要超限（2026-09-29）

> 计划：`docs/superpowers/plans/2026-09-29-credential-key-leak-and-summary-limit.md`

起因是 2026-09-29 的代码评审。两条高危问题的共同形态是「自动动作跑在用户确认之前」。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 编辑已有凭据时改了服务地址，界面会自动去新地址取模型列表 —— 用户还没点「保存」、也还没勾「确认发送到新地址」，**原来保存的 Key 已经被发到新地址** | 已存的 Key 只回答**它自己那个地址**：地址/协议变了又没有新 Key 时根本不取列表，提示保存后再取 | `frontend/src/services/credentials.ts` | `CredentialForm.test.ts`（改地址后一次都不发 / 地址没变仍然用 `keyId`） |
| 同一件事的另一半：`GET /api/credentials/models` 只要拿到 `key_id` 就解密并向任意 `endpoint` 发请求，前端漏改就直接变成外发 | 请求地址与这条凭据落库地址不一致（忽略大小写与结尾斜杠）时不解密、不外发，返回空列表加一句说明 | `backend/src/agent/api/server.py` | `test_credential_endpoint_api.py` |
| 摘要的实体/关键词超过上限（实测出现过 79 项）被判成 schema 违规 → 整个派生任务失败：这个片段没有摘要、没有检索记录、没有实体卡、没有知识条目 | 本地先「去空白 + 去重 + 保序 + 截断」再交给 pydantic 校验；上限同时写进提示词；校验只负责挡真正的结构错误 | `backend/src/agent/memory/summary.py` | `test_memory.py`、`test_fault_injection.py`（超出上限时摘要与索引照常落库） |

**已知限制（不粉饰）**

- 两条都只做到「源码 + 自动化测试」：**没有用真实 Key 实测**，改地址的完整点击路径也没有真机走查。
- 摘要的同类风险没有一次收完：`title` / `summary` 文本超长、知识提炼的 `candidates` 超上限仍会让那一项失败
  （文本超长会丢掉整条摘要，候选超上限只丢知识条目）。本轮只改了实体与关键词。
- 「已存的 Key 只能问它自己那个地址」是精确匹配（去尾斜杠、忽略大小写）：给同一个地址补一段路径也会被
  当成换了目标，需要保存之后才重新取候选模型列表。

---

## 本轮变更：Windows 检查、用量累计与「停止」真取消（2026-09-29）

接上一节的评审，处理剩下的三条：Windows 路径没有自动检查、用量上限不累计、按停止不掐请求。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 发布平台是 Windows，但 CI 只有 Linux；壳的单元测试（含 `#[cfg(windows)]` 的启动超时用例）**在哪都没跑过** | CI 新增 `backend-windows`（同一套后端测试）与 `rust-windows`（`cargo test`，不再只 `cargo check`）；两条命令同步进 SETUP | `.github/workflows/ci.yml`、`docs/SETUP.md` | 本机等价命令实跑：`cargo test` 八个用例全过（含 `hanging_command_is_killed_after_the_timeout`） |
| 「用量上限」从来不随真实调用累计（`record_usage` 只有测试调用），已用量永远停在 0，上限也就不可能生效 | 建 adapter 时记住自己的 `key_id`；每次模型调用把「进 / 出」报给归因 sink 并写进这条凭据；主循环、子 agent、后台维护共用同一份归因 | 迁移 22（`usage_input` / `usage_output`）、`credentials/store.py`、`credentials/usage.py`、`core/loop.py`、`services/app.py` | `test_usage_accounting.py`、`test_credentials.py`、`CredentialCard.test.ts`（进 / 出 / 合计与上限比较） |
| 按「停止」只是不再用结果，已经发出的模型请求照跑 | 请求放进自己的 task，与取消事件竞速；取消时立刻取消它（连接随之中断），turn 仍按 `cancelled` 语义收尾 | `core/loop.py`（`_await_completion`、`cancel`） | `test_cancel_and_rapid_turns.py`（请求被真正中断、排队消息不再被堵住） |
| 附带修掉的既有缺陷：通知在「最后一次 planning 之后」到达时会被**静默丢掉** | 这一轮的 loop 把没读到的通知交还给上层，由它变成自己的一轮 | `core/loop.py`（`TurnResult.unread_notices`）、`services/turn_orchestrator.py` | `test_p7_subagent_race.py`（迟到的子任务结果仍成为独立一轮） |

**已知限制（不粉饰）**

- 新增的两个 CI 任务**还没在 GitHub 上真跑过**（本机等价命令通过）：Windows runner 是全新环境，
  第一次跑可能要按它的情况再调一轮（占位文件命名、个别依赖本机状态的用例）。
- 计量口径是「进 + 出」合计：注入的 system prompt / 记忆上下文都算在「进」里，所以进度条会比直觉更快接近上限。
  换上限＝三个数字一起归零（重新计数）。
- 取消掐的是客户端等待；服务端是否立刻停止生成、停止计费，由供应商决定。
- 用量只在主循环 / 子 agent / 后台维护三条路径归因；其它直接调用适配器的实验代码不计入。
- 第 6 条（真实界面与 Planet 手感验收）**没有做**：那需要真机人工验收，自动化替代不了。

---

## 本轮变更：工具开发规范与可靠性修复 · 第一阶段（2026-09-29）

计划：`docs/superpowers/plans/2026-09-29-tool-dev-spec-phase1.md`。目标不是改提示词，而是让「失败被如实、结构化地反馈」以及「开发任务有权威状态」进入代码。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 工具失败且 `content` 为空时，模型收到空正文，随后声称成功也被接受 | 公共执行层新增唯一的结果→模型消息构造点：失败至少带状态、错误类别、简短原因、是否可重试、`call_id` 与脱敏限长的诊断；成功且正文非空时保持原样 | `core/tool_feedback.py`、`core/loop.py`、`tools/base.py`（`ToolResult.category/recoverable`） | `test_tool_feedback.py`（含主循环集成：失败且空正文仍回填事实） |
| 沙箱捕获的 `stderr` 没传到上层，模型只看到「exit code 1」 | `SandboxResult` 增加错误类别与 `diagnostic()`（stderr 优先、脱敏限长）；tester 与 CodeTool 把诊断一并交给模型 | `tools/sandbox.py`、`tools/tester.py`、`tools/runtime_tools.py` | `test_tool_feedback.py`（已知异常 / 缺依赖两类 stderr 与类别） |
| 子进程用 `sys.executable -c`，冻结后指向后端 exe（入口只启动服务） | 改为统一执行协议：开发态 `python tool_worker.py`、正式态 `后端 exe --tool-worker`（见下一节）。本轮先做「解析出可执行命令 + 明确报环境问题」，旧接口随后被 worker 取代 | `tools/executor_env.py`、`tools/sandbox.py` | `test_executor_env.py` |
| `dev_submit_tool` 要求模型再传完整 `definition`，容易与工作区文件/测试对象不一致 | 提交以工作区 `tool.json` 为唯一权威：省略 `definition` 即可提交；传了则必须与工作区一致，否则拒绝；提交时绑定工作区内容摘要 | `tools/dev_tools.py`、`prompts.py` | `test_dev_tools.py`（工作区为准的提交路径 + 不一致拒绝） |
| 开发任务只有内存态，缺少可枚举的权威状态 | `DevWorkspace` 增加 `state.json` 落盘（阶段 / 测试通过与否 / 摘要 / 次数 / 提交摘要）、`list_tasks()`、`status()`、`content_digest()`；新增 `dev_list_tasks` 工具 | `tools/dev_workspace.py`、`tools/dev_tools.py`、`tools/display.py`、`services/app.py` | `test_dev_tools.py`（重启回填、删 `state.json` 后标未知、摘要随内容变化、任务枚举） |

**已验证**：后端全量 `uv run --frozen pytest`（通过），新增用例覆盖上表每一行；`scripts/check_docs.py` 通过。

**仍未验证 / 留到后续阶段（NOT RUN，不宣称完成）**

- **冻结产物的真实执行链**：已由下一节「工具 worker 模式」补齐（后端 exe 自带 `--tool-worker`），本节的这项缺口作废。
- **开发规范落地为单一权威、分节注入**：本阶段只更新了 `DEV_GUIDE` / 工具描述文案；尚未建立结构化的规范模块与按步骤注入。仍属 planned。
- **模型谎报成功时的状态校正**：只修了「失败被如实反馈」，尚未实现「最终答复涉及验证/可用结论时按后端状态校验并纠正」。仍属 planned。
- **所有已保存对话的原文检索**：开放片段、摘要失败内容的独立原文检索路径**未实现**。仍属 planned。
- **前端界面**：任务卡、未完成任务提示、恢复入口都没动。未验证。

---

## 本轮变更：工具 worker 模式（2026-09-29）

目标：让冻结产物在没有系统 Python、没有 Docker 的 Windows 上也能跑生成工具，同时不把后端主进程当作执行器、不把独立子进程说成安全沙箱。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 冻结后 `sys.executable` 是后端 exe，入口只启动服务，工具子进程测试与运行必然失败 | 后端 exe 增加 `--tool-worker` 模式；`agent/main.py` 顶层只留标准库，`main()` 第一件事按 argv 分流，worker 分支在加载 uvicorn / FastAPI / 数据库 / 凭据**之前**返回 | `src/agent/main.py`、`src/agent/tool_worker.py` | `test_entrypoint_split.py`（真子进程：import 入口不加载服务栈；worker 模式不建 app.db） |
| 开发与正式运行的执行协议不一致（`-c` 拼脚本 vs 冻结 exe） | 统一的 worker 协议：stdin 传结构化 JSON 请求，stdout 只回一行 JSON 结果；工具自身的打印与异常栈收进结果的 `stdout`/`stderr` 字段 | `src/agent/tool_worker.py`、`src/agent/tools/executor_env.py`、`src/agent/tools/sandbox.py` | `test_tool_worker.py`（真子进程跑协议：成功/异常类型/输出捕获/非法结果/畸形请求）、`test_sandbox_worker.py` |
| 超时只 `kill()` 直接子进程，工具自己起的子进程会残留；按名称清理会误伤别的进程 | 新增 `_kill_process_tree`：Windows 用 `taskkill /PID <pid> /T /F`（按 PID 遍历子树），POSIX 用独立进程组；取消路径同样清理后再传递取消语义 | `src/agent/tools/sandbox.py` | `test_sandbox_worker.py`（超时/取消各清理一次且 pid 正确；Windows 断言命令行是 `/PID` 而非镜像名） |

**已验证（本机实跑）**

- 后端全量 `uv run --frozen pytest`（退出码 0），含上表所有新用例。
- **真实冻结产物**：用 `uv run --with pyinstaller` 按 `scripts/build_sidecar.ps1` 的参数打包出 `qio-backend.exe`，然后：
  - `qio-backend.exe --tool-worker`（stdin 传请求、PATH 里已剔除所有真实 Python，只剩 Microsoft Store 的 `python.exe` 占位符）返回 `{"ok": true, "value": {"sum": 42}}`，退出码 0，**没有**创建数据目录与 `app.db`（证明 worker 模式不连库、不跑迁移）。
  - 同一 exe 正常启动：创建 `app.db`、HTTP 端口应答（`/api/tools` 返回 404 = 服务在听）、按 PID 停止成功（证明正常启动路径没有退化）。

**仍未验证 / 未实现（如实标注）**

- **完整安装包（`tauri build` 产物）的人工安装验收**：NOT RUN。上面跑的是 PyInstaller 冻结 exe，不是装完的安装包。
- **「没有 Docker」的对照实验**：NOT RUN。本机 Docker 状态未变更，worker 路径本来就不经过 Docker。
- **Docker 执行器的协议统一**：Docker 分支仍用容器内 `python -c` 的旧协议，未与新 worker 协议合并。NOT RUN（未实测）。
- **额外依赖的项目级隔离**（QIO 管理的专用 Python 环境 / 项目级依赖安装）：**未实现**，只有接口位置。默认只复用随包依赖；用到第三方库的工具会以 `missing_dependency` 明确失败，不会自动安装。
- 「测试也执行实际权限检查」这条只覆盖了既有 `policy` 能力分级与审批路径，**测试数据隔离**（临时库/临时目录/模拟服务）没有在本轮补。

> 上面两条与 worker 协议可信度、输出保护相关的缺口，已由下一节「第一阶段收尾」补齐；安装包人工验收仍是 NOT RUN。

---

## 本轮变更：第一阶段收尾 · 可信状态与执行协议（2026-09-30）

计划：`docs/superpowers/plans/2026-09-30-tool-dev-spec-phase1-completion.md`。范围是复核报告里指出的五个会**掩盖真实状态**的缺陷：测试证据不绑定版本、`state.json` 可被伪造、提交后项目被删、注册与落库失败不回滚、worker 的假成功被采信。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 测试通过后把代码改成错的，任务列表仍显示「测试通过」 | 测试证据绑定被测内容的 sha256 摘要；内容一变（含越权直接改磁盘）立即标为 `stale`，任务列表照实说「证据失效，需重跑」 | `tools/dev_workspace.py`、`tools/dev_tools.py` | `test_dev_tools.py`（内容变更即失效、重启后仍失效、任务列表不谎报） |
| agent 能用 `dev_write_file` 写 `state.json` 伪造「测试通过 / 99 次测试」 | `state.json` 与 `request.md` 列为后端保留名，文件工具写入即拒绝；`state.json` 带 `schema` 与 `source`，恢复时必须对上才认，否则按「未知」处理 | `tools/dev_workspace.py` | `test_dev_tools.py`（伪造被拒、无 schema 的 state 不被采信） |
| 提交成功后工作区被删除，工具注册了但任务再也枚举不到 | 完成后保留项目：标记已提交 + 复制一份不可变快照到 `dev-workspaces/archive/<id>/` | `tools/dev_workspace.py`、`tools/dev_tools.py` | `test_dev_tools.py`、`test_dev_workflow_integration.py` |
| 提交时的交叉复测失败，持久状态仍是「测试通过」 | 复测结论经 `test_sink` 在**任何对外事件之前**写回同一个任务 | `tools/lifecycle.py`、`tools/dev_tools.py` | `test_dev_tools.py`（复测写回同一任务） |
| `ToolStore.save` 抛磁盘错误时，提交报失败但工具已在内存注册表里可调用 | 改为「先持久化、后注册」；失败回滚：撤销本次注册 + 写回上一可用版本 | `tools/lifecycle.py`、`storage/tool_store.py`（新增 `load`） | `test_tool_lifecycle.py`（落库失败不留注册、注册失败恢复上一版本） |
| worker 直接吐一行形似成功的结果再以退出码 17 退出，沙箱仍报 `ok=True` | 成功必须同时满足：退出码 0、结果通道恰好一行 JSON、`ok` 是布尔、成功时 `value` 是对象 | `tools/sandbox.py` | `test_sandbox_worker.py`（伪造成功 / 多余行 / 非布尔 ok / 非对象 value 全部拒绝） |
| Windows 上工具子进程环境缺 `SystemRoot` 等系统变量 | 环境改为显式白名单（`SystemRoot`、`SystemDrive`、`WINDIR`、`COMSPEC`、`PATHEXT`、`TEMP`、`TMP`、`TMPDIR`、`PATH`、`LANG`），白名单外一律不传 | `tools/sandbox.py` | `test_sandbox_worker.py`（系统变量可见、非白名单变量不可见） |
| 输出限制发生得太晚：worker 先全写进 StringIO，父进程再 `communicate` 全部读回 | 两侧都加界：worker 端捕获缓冲封顶并标注截断，父进程边读边计数（stdout 2 MiB / stderr 512 KiB），超限即终止进程树并如实报错 | `tool_worker.py`、`tools/sandbox.py` | `test_sandbox_worker.py`（工具狂打印仍可用且标注截断；坏 worker 无限输出被截断） |

**已验证（本机 Windows，源码运行）**：受影响的工具 / 沙箱 / 开发工作区相关测试文件全绿（`tests/test_tool_feedback.py`、`test_tool_worker.py`、`test_sandbox_worker.py`、`test_entrypoint_split.py`、`test_executor_env.py`、`test_dev_tools.py`、`test_tool_lifecycle.py`，以及 `test_tool_store.py`、`test_tool_restore.py`、`test_tool_recovery.py`、`test_tool_registry_reversible.py`、`test_tool_policy.py`、`test_tool_pipeline.py`、`test_active_tools.py`）；`python scripts/check_docs.py` 通过。

**仍未实现 / 仍是 NOT RUN（不宣称完成）**

- **模型谎报成功时的最终结论校正**：后端部分已实现（见文末「最终结论的事实校正」一节）；前端「已核对」标记仍未做。
- **测试前授权与真实能力策略**：`dev_run_tests` 仍在审批之前就执行生成代码；测试数据隔离（临时库 / 临时目录 / 模拟服务）没有补。
- **所有已保存对话的原文检索**：开放片段与摘要失败内容的独立原文检索仍未实现。
- **开发规范分节注入**：仍是单一长文 `DEV_GUIDE`，没有按当前步骤注入。
- **多文件项目与项目级依赖隔离**：未实现，文件工具仍只接受单层文件名。
- **前端**：任务卡、未完成任务提示、恢复入口未动。
- **真实冻结产物与安装包验收**：本轮只跑源码测试，未重跑 PyInstaller 冻结 exe，也未做安装包人工验收 —— NOT RUN。

---

## 本轮变更：最终结论的事实校正（2026-09-30）

规格：`docs/superpowers/specs/2026-09-30-final-answer-fact-check-design.md`；计划：`docs/superpowers/plans/2026-09-30-final-answer-fact-check.md`。

要解决的问题：工具失败的结果**已经**如实回填给模型（`core/tool_feedback.py`），模型随后仍然说
「测试全部通过，已提交审批」，而这句话照样成为最终答复 —— 因为「本轮到底做了什么」没有任何
地方记得住，最终答复也不经过任何核对。明确不采用的做法是「扫关键词改话」。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 本轮的失败与开发任务状态没有任何地方记得住 | 新增每轮事实台账（工具终态 + 开发工具上报的任务状态）；`ToolResult` 增加机器可读的 `facts` 通道，开发类工具在**操作之后**上报「任务 / 版本摘要 / 测试证据 / 是否提交」 | `core/turn_facts.py`、`tools/base.py`、`tools/dev_tools.py`、`tools/dev_workspace.py` | `test_turn_facts.py`、`test_dev_tools.py` |
| 「测试通过 / 已注册 / 可以使用」可以被模型自己宣布 | 新增只读工具 `declare_completion`：按工作区状态、注册表与持久层逐条核对 `test_passed` / `registered` / `usable`（版本必须等于当前内容摘要，`subagent` 型不要求测试证据）；对不上就逐条说清缺什么 | `tools/declare_completion.py`、`services/app.py`、`prompts.py`、`tools/display.py` | `test_declare_completion.py`、`test_dev_workflow_integration.py` |
| 最终答复不经过任何核对 | 主循环收尾：本轮有未解决失败（工具最后一次结局是失败、任务测试失败/证据失效/还没有测试证据）且没有被接受的声明时，在答复**末尾追加**一段后端事实说明。不重写模型正文；取消不算失败；先脱敏再限长；文案不含花括号 | `core/loop.py` | `test_final_answer_fact_check.py` |

**已验证**：本机 Windows 源码运行，`tests/test_turn_facts.py`、`test_dev_tools.py`、`test_declare_completion.py`、`test_final_answer_fact_check.py`、
`test_tool_feedback.py`、`test_loop.py`、`test_dev_workflow_integration.py`、`test_app_integration.py`、`test_settings_tools_api.py` 全绿；后端全量测试见本次提交说明。

**仍未做（不宣称完成）**

- **前端「已核对」标记**：已由下一节补齐（复用 `messages.raw`，未加迁移）。
- **声明面只覆盖开发类结论**：网页 / 桌面操作类结论要核对时，沿用同一张台账与同一套 claim 协议扩展，本次不做。

---

## 本轮变更：结论标记与「已创建」文案（2026-09-30）

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 「已核对」这件事在后端没有留痕，用户看不到哪句话是后端核对过的 | 被接受的 `declare_completion` 结论随那条 assistant 消息落库（复用已有的 `messages.raw`，**不加迁移**），并随 `TURN_END` 发给前端；界面在回答里显示一行「后端已核对：版本 … ；测试 … 」 | `core/turn_facts.py`、`core/loop.py`、`core/turn.py`、`services/turn_orchestrator.py`、`services/app.py`、`frontend/src/stores/session.ts`、`frontend/src/stores/events.ts`、`frontend/src/components/MessageItem.vue` | 后端 `test_turn_facts.py`、`test_final_answer_fact_check.py`；前端 `finalAnswer.test.ts`、`MessageItem.test.ts` |
| 「已创建」卡片写死「现在可以使用」，把「注册成功」说成「验证过」 | 文案按实际拿到的证据写：「已注册，可以调用（1/1 tests passed）」；引用凭据的工具再加一句「真实服务未验证（测试不注入凭据）」 | `tools/dev_tools.py`（`ready_detail`）、`tools/lifecycle.py` | `test_dev_tools.py`、`test_tool_create_events.py` |

**已验证**：后端全量 `pytest` 退出码 0；前端 `npx vitest run` 全绿、`npx vue-tsc --noEmit` 通过；`scripts/check_docs.py` 通过。

**仍未做（不宣称完成）**

- **真机界面验收**：只有组件级渲染测试（`.verified-note` 存在/不存在），没有在真实应用里看着这条标记出现。
- **未完成任务入口与审批跨重启语义**：下一批（已定语义：重启后不恢复等待，只留「那次操作没有执行」的记录 + 安静的继续入口）。

---

## 本轮变更：审批跨重启（2026-09-30，B 批前半）

语义（已定）：**重启后不恢复等待** —— 等待中的那次工具调用随进程一起没了，恢复一个「等你回答」的授权是假的。要做的是把它变成一条明确记录：「那一次操作没有执行」。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 等待中的审批只活在进程内存里，重启后既不会执行、也没人告诉用户「那件事没做」 | 新增迁移 23 的 `pending_approvals`：每次 `ApprovalService.request` 落一行；应答 / 超时 / 取消时收口；**构造新实例时**把仍是 `pending` 的行标成 `interrupted`（新进程不可能有自己的等待项，此刻还 pending 的都是上一个进程留下的） | `storage/schema.py`（迁移 23）、`tools/approval.py` | `test_approval_persistence.py` |
| 「那次没执行」只在库里，界面看不到 | `/api/runtime/state` 增加 `interrupted_approvals`（只有 kind / 描述 / 时间 / `outcome: not_executed`，不回传完整载荷） | `api/server.py` | `test_approval_persistence.py`（装配级：真装配后能读出来） |
| 应用装配没把连接交给审批服务，记录永远是空的 | `AppContext` 用 `ApprovalService(bus, conn=conn)`；没有连接时（老装配 / 单测）行为与以前完全一致 | `services/app.py` | `test_approval_persistence.py` |

**已验证**：涉及审批、API、库不变量的测试文件全绿；后端全量测试见提交说明。

**仍未做**：前端还没显示这条记录，也还没有「未完成任务」入口 —— 那是 B 批的后半，跟着一起做。

**B 批后半（界面，2026-09-30 同日）**

- 前端在 RESYNC 时读 `/api/runtime/state` 的 `interrupted_approvals`，在审批入口上方显示一句事实：
  「上次有一项操作没有执行：…」（多项时写「上次有 N 项操作没有执行（例如 …）」）。
  它**不是待办、也点不动** —— 那次调用随进程没了，恢复一个「等你回答」的授权是假的。
- 实现：`frontend/src/stores/session.ts`（`interruptedOperations` + RESYNC 时填充）、
  `frontend/src/components/ApprovalEntry.vue`（只读的一行）、`frontend/src/services/api.ts`（类型）。
- 已验证：前端 `npx vitest run` 全绿、`npx vue-tsc --noEmit` 通过。
- 仍未做：**「未完成任务」入口**（把开发任务列表带进界面）还没做；`ApprovalEntry` 那一行只有 store 级测试，没有组件级渲染测试；真机界面验收仍未做。

**未完成任务入口 · 后端（2026-09-30 同日）**

- 新增 `GET /api/dev/tasks`：返回开发任务的权威状态（id / 需求 / 阶段 / 是否已提交 /
  测试结论 / **证据是否还对应当前内容** / 更新时间）。状态来自工作区本身，刷新、重启、
  断线之后都查得到 —— 以前任务只存在于工具调用里，模型不说，界面就再也找不到它。
- 实现：`api/server.py`；测试 `tests/test_dev_tasks_api.py`。注意：这些用例显式清掉
  `QIO_DATA_DIR`，否则本机开发用的真实数据目录会漏进来（与 `test_workspace_root.py`
  那条既有环境敏感用例同源）。
- 仍未做：前端还没有用这个接口显示「有 N 个未完成任务」那一行。

---

## 本轮变更：测试前授权（2026-09-30）

补的是第一阶段里那条顺序错误：`dev_run_tests` 会把模型刚写出来的代码**真的跑起来**，
而以前这条路径没有任何授权 —— 审批只发生在注册之后，等于「先执行、后确认」。

| 问题 | 修法 | Implementation | Tests |
| --- | --- | --- | --- |
| 执行 AI 生成代码之前没有任何授权 | 新增执行前闸门：第一次在某个任务上执行生成代码之前必须拿到用户确认；确认按（能力策略指纹 + 实际执行环境）记在工作区状态里，同一任务之后不再重复打扰 | `tools/dev_auth.py`、`tools/dev_workspace.py` | `test_dev_test_authorization.py`（首次询问、二次放行、换环境/换策略重新确认、重启后仍有效） |
| 拒绝 / 超时之后仍可能被执行 | 拒绝 / 超时 / 拿不到审批服务 → **不执行**，返回类别为 `permission` 的失败；测试记录也不会留下 | `tools/dev_auth.py`、`tools/dev_tools.py` | 同上（断言沙箱执行次数为 0） |
| 审批说明可能说不准「到底在哪儿执行」 | 说明用运行路径同一份策略（`policy.default_policy_for`），写明执行环境（Docker 容器 / **受限子进程，不是安全沙箱**）、一次性临时目录、超时、不注入凭据 | `tools/dev_auth.py` | `test_dev_test_authorization.py`（子进程与凭据两种边界的文案断言） |
| 提交时的复测同样在执行生成代码，却没被授权约束 | `dev_submit_tool` 在 function 型工具提交前走同一道闸门 | `tools/dev_tools.py`、`services/app.py` | `test_dev_test_authorization.py`（未授权时提交被拦下且不进入审批） |
| 「测试通过」容易被当成「真实链路验过」 | 引用凭据的工具，测试结果正文与审批说明都写明「凭据是模拟的，未注入真实凭据」 | `tools/dev_tools.py`、`tools/dev_auth.py` | `test_dev_test_authorization.py`、`test_dev_tools.py` |

顺带收敛：应用启动时只建一份 `SandboxExecutor`，开发测试、提交复测与注册后的真实执行共用它（超时 / 环境 / 输出上限同一套）。

**仍未做（不宣称完成）**

- **项目级依赖隔离**（QIO 管理的专用 Python 环境）仍未实现；缺依赖仍以 `missing_dependency` 明确失败。
- **真实外部服务模拟**：测试不注入凭据、不联网，但「模拟服务」本身没有做（工具里写死的真实 HTTP 调用在测试里就是失败或模拟不到）。
- **旧的传统创建路径** `ToolLifecycle.create_from_request` 没有接这道闸门 —— 它在生产代码里没有调用点（只有测试用），但确实还留着。
