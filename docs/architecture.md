# QIO 架构文档

版本：v0.8.1（Anthropic 适配器 + Key 自动识别，2026-08-05）
状态：M0–M12 已实现并验证

---

## 1. 项目定位

QIO 是一个本地优先、长对话场景的 agent 应用。核心目标：

- 用户能在同一个地方长时间对话，对话以网状话题结构组织，而非线性会话列表
- agent 拥有记忆域与知识域双基础架构，记忆由主模型写入、小模型只做判断
- agent 可通过对话自主创建工具，工具与原生工具并列，创建需人工审批
- 模型全由用户自带 Key（BYOK），无系统自有 Key

## 2. 技术选型

| 层 | 选型 | 说明 |
| --- | --- | --- |
| 后端 | Python 3.11+ / FastAPI / openai SDK | sidecar 子进程，本地 HTTP + SSE 服务 |
| 存储 | SQLite（WAL 模式） | 嵌入式，9 张表 + 迁移机制 |
| 凭据 | Windows 系统凭据库（keyring） | 密钥永不落盘 |
| 前端壳 | Tauri 2（Rust） | 窗口、托盘、进程管理 |
| 前端框架 | Vue 3 + Vite + TypeScript + Pinia | 与旧项目技术栈一致 |
| 图可视化 | d3-force（后续） | 网状话题导航 |

运行形态：Tauri 壳启动 Python sidecar，前端通过 localhost HTTP + SSE 与后端通信。前端壳可整体替换，后端协议不变。

## 3. 总体架构分层

```
┌─────────────────────────────────────────────┐
│ 前端壳层：Tauri 2 + Vue 3                    │
│   窗口 / 网状导航 / 记忆知识面板 / 凭据管理     │
└──────────────┬──────────────────────────────┘
               │ localhost HTTP + SSE（14 类事件）
┌──────────────▼──────────────────────────────┐
│ API 层：FastAPI 路由 + EventBus              │
│   /api/health /api/events /api/credentials   │
│   /api/turns（后续） /api/approvals（后续）   │
└──────────────┬──────────────────────────────┘
┌──────────────▼──────────────────────────────┐
│ 核心循环：agentloop 状态机                    │
│   PLANNING → TOOL_EXEC → OBSERVING → DONE    │
│   token + 迭代次数双预算                      │
└───────┬──────────────┬────────────┬─────────┘
        │              │            │
┌───────▼───────┐ ┌────▼─────┐ ┌───▼──────────┐
│ 模型适配层     │ │ 工具层    │ │ 凭据层        │
│ native/text/  │ │ 注册表/   │ │ BYOK + 授权   │
│ unsupported   │ │ 沙箱/生命周期│ │ 审计 + 快照   │
└───────────────┘ └──────────┘ └──────────────┘
        │
┌───────▼──────────────────────────────────────┐
│ 记忆域 / 知识域 / 图导航层 / 选择器（M5+）      │
│ 原文→摘要→目录 / 状态机 / 节点边 / 规则+召回    │
└──────────────┬──────────────────────────────┘
┌──────────────▼──────────────────────────────┐
│ 存储层：SQLite（nodes/edges/fragments/...）   │
└─────────────────────────────────────────────┘
```

## 4. 已实现模块（M0–M10）

### 4.1 项目结构

```
qio/
├── backend/
│   ├── pyproject.toml
│   ├── src/agent/
│   │   ├── main.py            # 入口：create_app 工厂（uvicorn --factory）
│   │   ├── config.py          # Settings（数据目录/端口/环境变量覆盖）
│   │   ├── api/               # server.py（EventBus + 路由）、events.py（协议）
│   │   ├── core/              # loop.py（状态机）、budget.py（双预算）
│   │   ├── adapters/          # base/native/text/probe（三态）
│   │   ├── credentials/       # store.py（keyring）、policy.py（授权）
│   │   ├── storage/           # db/schema/migrate/archive
│   │   ├── tools/             # base/builtin/registry
│   │   └── （memory/ knowledge/ graph/ selector/ services 为后续里程碑预留）
│   ├── tests/                 # 36 个测试
│   └── scripts/verify_sse.py  # 真实链路冒烟验证
├── frontend/
│   ├── src/                   # Vue 3（services/events.ts、stores、App.vue）
│   └── src-tauri/             # Rust 壳（main.rs 启动 sidecar）
├── scripts/setup_rust_mirror.ps1
└── docs/
```

### 4.2 SSE 事件协议

统一信封：

```json
{ "type": "CAPABILITY", "id": "evt_xxx", "ts": "2026-08-02T10:00:00Z", "data": { } }
```

14 个事件类型：

| 类型 | 负载要点 | 用途 |
| --- | --- | --- |
| CAPABILITY | adapter 三态、model、endpoint | 能力通告 |
| FALLBACK | from/to、reason | 降级通告 |
| MEMORY_INJECT | items、来源、token 预算 | 记忆注入 |
| APPROVAL_REQUIRED | approval_id、kind、payload | 审批请求 |
| APPROVAL_RESULT | approval_id、decision | 审批结果 |
| CREDENTIAL_STATUS | key_id、status、version | 凭据状态 |
| SUBAGENT_STATUS | subagent_id、status | 子 agent 状态 |
| USAGE | tokens、cost、budget_left | 用量 |
| TURN_START / TURN_END | turn 序号、意图/结果 | 循环轮次 |
| TOOL_START / TOOL_END | 工具名、参数、耗时、结果 | 工具执行 |
| WARNING | code、message | 可恢复异常 |
| ERROR | code、recoverable | 不可恢复错误 |

EventBus：订阅者扇出 + 50 条重放缓冲，断线重连后补齐最近状态。

### 4.3 存储 schema（9 张表）

- `nodes`：图节点，type 限定 user/entity/topic；用户根节点全局单例
- `edges`：边，type 限定 mention/related/owns，唯一约束 (src, dst, type)
- `fragments`：片段（封块边界），summary 由主模型封块时写入，closed_at 为空表示开放片段
- `messages`：消息原文，storage_tier 分 hot/cold，raw 保存完整原始 payload 可重放
- `memory_index`：机械索引（关键词/实体/token 估算），模型不可写
- `knowledge`：知识条目，category/state/supersedes_id（版本链）/provenance/confidence/export
- `cursor`：锚点，active 全局单行 + pending 防抖待定 + 各话题历史位置
- `credentials`：凭据元数据（tags/scope/budget/version/status）
- `credential_audit`：凭据操作审计

迁移机制：schema_version 表 + 顺序迁移脚本，自研轻量方案。

归档：messages 超 6 个月 gzip 冷归档到 archive/，SQLite 保留元数据与归档引用；摘要、索引、知识永久保留。

### 4.4 凭据体系（BYOK）

- 密钥只进 keyring（service=qio, username=key_id），元数据进 SQLite，密钥永不出现在日志/事件/API 响应
- 能力标签：main-loop / subagent / vision / video / audio / research / embedding + 自定义
- 授权（方案 C）：请求方能力标签 ∩ Key.tags → 候选；Key.scope 为空（类别默认）或包含请求方 → 通过；取最严格交集
- 多 Key 命中：剩余预算优先，用户可固定单 Key
- 快照：同步主循环每 turn 生成新快照；异步任务启动时生成快照，运行中不变
- 版本审计：改密钥 version+1，审计记录变更；revoke 后引用该 Key 的工具执行报错
- 预算：token 计数为主，可选 USD 折算

### 4.5 模型适配层（三态）

- 探测：最小 tool-calling 请求成功 → native；API 明确拒绝 tools → text；认证/网络错误向上抛
- 缓存：按 (endpoint, model) 缓存 1 小时
- native 档：openai SDK tool calling，解析失败链——纠错重试 ≤2 次（错误回喂模型），仍失败则抛 ToolCallParseError，由循环层如实展示原文 + WARNING
- text 档（兜底）：工具描述进 system prompt，模型输出 ```json 块解析；成功率 <60% 标记 unsupported
- unsupported：默认拒绝启动，force_continue 开关可强制继续

### 4.6 主循环（agentloop）

状态：PLANNING → TOOL_EXEC → OBSERVING →（PLANNING | DONE | STOPPED）

- 预算：迭代次数（native 5 / text 3）+ token 总量，双约束任一耗尽即 STOPPED；force_continue 可越过
- 单工具失败隔离：registry.execute 不抛异常，失败结果回喂模型 + WARNING
- 事件：每轮 TURN_START/TURN_END、每次工具 TOOL_START/TOOL_END、汇总 USAGE

### 4.7 前端与 Tauri 壳

- Vue 空壳：SSE 连接（14 类型监听）、事件流展示、测试发布按钮
- Tauri 壳（Rust）：setup 时启动 Python sidecar，退出时 kill；debug 直接跑 python 命令，release 跑打包 sidecar（externalBin 已启用，binaries/qio-backend-x86_64-pc-windows-msvc.exe，56.7 MB）
- 数据目录：`%APPDATA%/qio/`（app.db、archive/、config.toml、logs/），后端独占管理

### 4.8 选择器（M5）

- 工具路由（主循环 PLANNING）：核心工具（memory_search/switch_topic/create_topic/await_task/read_task_result）恒可见；其余按当前查询相似度排序截断（默认上限 20，开发意图提示词加权）；执行仍按注册表全局查找；路由异常降级全量
- 规则层（永远启用）：锚点话题亲和、实体提及、关键词重叠、时间衰减（半衰期 30 天）
- 召回层可插拔：向量召回（远程 OpenAI 兼容 embeddings（BYOK embedding 标签，settings embedding.model 可选）→ 本地 ONNX bge-small-zh-v1.5 int8）→ BM25（兜底）→ 规则层（rules-only）
- 向量后端：远程优先（embedding 标签 key + 端点，模型可配，连续失败自动降级）；本地模型文件 data_dir/models/bge-small-zh-v1.5/（QIO_MODELS_DIR 可覆盖）；启动自检自动选档
- 向量持久化：embeddings 表（doc_type memory_index/topic，model/dims/vector），重索引跳过已嵌入文档
- 精排层：可选，默认关闭
- 话题预判（TopicPredictor）：消息嵌入 vs 话题代表向量余弦相似度 → 主话题候选 / 辅助话题候选（top2）/ 疑似新话题标记；切换阈值 0.1、疑似新阈值 0.3；无嵌入时兜底指纹关键词重叠率；话题向量封块后刷新、冷启动用话题名
- 中文分词：轻量 tokenizer（拉丁词 + CJK 1-gram/2-gram），无外部 NLP 依赖

### 4.9 记忆域（M6）

- 消息写入：append-only 绑定开放片段（fragments），原文 + raw 完整 payload
- 封块策略：消息数阈值触发（前端可配置分档：短 5 / 标准 10 / 长 15 / 自定义 1-30，默认标准 10，settings 表持久化）；两步行：append 返回关闭信号 → turn 结束时注入摘要生成器执行关闭
- 短期记忆注入：开放片段原文全量 + 最近 2 个已封块片段摘要，带话题标记确定性注入（不参与相关性排序、优先占用预算），解决封块前对话连续性断裂
- 摘要契约：FragmentSummary（title/summary/entities/keywords），pydantic 本地校验，容忍 ```json 块；主模型调用失败或校验失败 → 降级（摘要为空，索引回退原文直引）
- 机械索引：高频关键词（停用词过滤）、实体关联（仅关联已存在实体节点，懒创建属 M8）、tiktoken 估算 token
- 写入纪律：索引为代码生成，模型不可写
- 预算压力记忆整理：注入候选超预算时，回合结束后对开放片段做滚动摘要（旧摘要 + 新增消息 → 新摘要，覆盖更新、版本递增、consolidated 标注）；同片段 10 分钟冷却；注入侧取摘要内容（不再仅注入标题）
### 4.10 知识域（M7）

- 状态机：draft → pending_review → verified → active → expired | revoked；非法转换拒绝
- 分层验证：高影响（user_profile/agent_self/goal）必须用户确认，system 验证被拒；低影响（general_fact/tool_experience）自动验证，可挂交叉验证器（默认无则基线置信度 0.9）
- 版本链：supersedes_id 指向被替代条目；新版本激活时旧版本自动 revoked；history() 沿链回溯
- 注入面：只有 active 进入；按类别过滤/分组、exclude 支持（M9 预算接入）
- 显式反馈纠错（v1）：对话式 correct_knowledge 工具（快照锚定 → 全库检索 → 候选索引确认，不暴露内部 id）+ 前端「修正/删除」按钮（/api/knowledge/{id}/revise|revoke）；修正走 supersedes 版本链，用户纠错即确认直接生效；Dreaming/隐式反馈（v1.5）
- 节点多对多挂载：node_ids 可挂话题/实体/用户任意组合（迁移 v3）；注入按节点归属过滤
- 封块提炼链：封块摘要后主模型提炼知识候选（内容/类别/建议挂载节点）→ draft → 分层验证（低影响自动激活、高影响待用户确认）→ 激活挂载；提炼失败不影响封块
### 4.11 图导航层（M8）

- 三类节点：user（根单例）/ entity / topic；边类型：mention / related / owns（weight 累加）
- 实体懒创建：提及计数表 entity_mentions（迁移 v2），阈值 2 或用户标注（force）才建节点；手动合并（边重定向 + 权重合并 + meta.merged_into 标记）
- 锚点：active 全局单行；切换先入 pending（防抖），confirm 后生效，discard 保持原锚点；原位置转 history 不丢弃；get_position 取话题最新位置
- 模型驱动话题管理：判断归嵌入预判，执行归主模型——内置工具 switch_topic / create_topic 确认切换与创建（text 档兜底不切换）；切换/创建更新锚点并自动建立 related 边（无向规范化，weight 累加）；切换后本轮用户消息迁移到新话题开放片段
- 话题指纹：聚合最近片段摘要与机械索引关键词（跨会话第一跳检索用，M9 接入）
### 4.12 注入与检索（M9）

- 注入预算：模型上下文窗口 × 默认 25%（动态解析：内置模型长度表 → 运行时降级 256K/128K/64K → 兜底）；记忆强度滑块已移除，注入量由系统决定
- 每轮预判：消息 → 话题向量相似度 → 主话题 / 辅助话题 / 疑似新话题；注入按预判结果组装
- 注入内容：短期记忆（当前话题开放片段原文 + 最近 2 个片段摘要，优先占预算）→ 主话题面知识 → 辅助话题面（知识 ≤3 + 最近片段摘要 ≤2，标记【相关话题】）→ 记忆检索（向量召回 top_k=6）→ 实体面 / 用户面知识；疑似新话题时附【话题建议】块供主模型 create_topic
- 注入附【话题】上下文（当前话题 + 相关话题 id/名称），主模型据此调用 switch_topic / create_topic
- 统一预算截断（不分域固定带宽），短期记忆优先占用
- 检索管线：第一跳话题指纹（query 与指纹关键词重叠率）→ 全局召回（M5 selector）→ 三权重排序（相关性 0.4 / 新鲜度 0.25 / 话题亲和 0.35）
- 新鲜度：指数衰减（半衰期 30 天）；亲和：锚点话题 + 指纹命中话题加权
- 注入组装：知识条目（带类别标记）+ 记忆片段（带标题/来源）拼接为【长期记忆注入】块
- memory_search 工具：主循环可主动检索历史记忆（query/top_k/topic_id）
- BM25 查询词过滤单字 CJK（噪声控制）

### 4.13 工具创建生命周期（M10）

- 生命周期（开发工作流）：对话澄清需求（指南约束必填项）→ create_tool 创建沙箱开发任务 → dev 工具链自主实现（dev_write_file / dev_read_file / dev_run_tests 迭代修复，技术细节折叠展示）→ dev_submit_tool 提交 → 双保险交叉测试 → 审批段 1（创建，用户友好展示：人话用途/类型/测试摘要/预算）→ 审批段 2（凭据授权）→ 并列注册并落库（tools 表，重启自动恢复）；批准后清理工作区，拒绝保留可继续迭代
- 工具定义契约：name（snake_case）/ description / parameters（JSON Schema）/ code（纯函数，沙箱执行）/ tool_type（function | subagent）/ credential_ref / tests（≥1 确定性用例）
- 沙箱：默认受限子进程（临时目录、最小环境变量、超时、输出捕获）；Docker 可选（--network none），auto 自检选档
- 函数型工具：CodeTool 沙箱执行；引用凭据时授权后运行时注入环境变量（QIO_KEY_*）
- 子 agent 型工具：完整运行时——自有 Key（BYOK 必填 credential_ref）与模型跑独立循环；支持同步/异步；异步并行硬上限 4（TaskManager，超出排队）；三策略取回（同轮 await_task / 自主 await_task / notify 完成唤起）；预算 subagent_budget（max_iterations/max_tokens/output_limit_chars）创建时提案、审批可覆盖；结果不截断（提示词软上限 + 超限完整存储 + read_task_result 分段读取）；子 agent 工具集 v1 仅 memory_search（ToolSetPolicy 预留）；SUBAGENT_STATUS 事件上报状态
- 凭据授权：授权后 scope 收窄为 [tool_name]（方案 C 最严格交集）
- 审批服务：APPROVAL_REQUIRED 发布 → 前端 POST /api/approvals/{id}/respond → APPROVAL_RESULT；超时默认 300s
- 失败路径：提案校验失败 / 交叉测试失败 / 审批拒绝 / 重名注册，均在对应步骤返回明确结果，不产生部分注册

## 5. 已冻结的核心设计（后续里程碑）

### 5.1 记忆域（M6）

三层结构，append-only：

```
原文（messages） → 摘要（fragments.summary） → 目录（memory_index）
```

- 原文如实记录对话，hot 6 个月后 gzip 冷归档
- 摘要由主模型在封块时写入，JSON Schema 契约 + 本地校验，失败降级原文直引
- 目录为机械索引（关键词、实体关联、token 估算），主模型不可写
- 写入纪律：小模型只判断不写入，主模型是唯一写入者

### 5.2 知识域（M7）

状态机：draft → pending_review → verified → active → expired | revoked

- 只有 active 进入注入面
- 本体记忆（agent-self）是知识域类别
- supersedes 版本链（v1 纳入，受 MindMemOS 启发）
- 分层验证默认规则：用户画像/本体记忆/目标相关 = 高影响需用户确认；通用事实/工具经验 = 低影响交叉模型验证自动生效
- 显式反馈纠错（v1）：对话式 correct_knowledge 工具（快照锚定 → 全库检索 → 候选索引确认，不暴露内部 id）+ 前端「修正/删除」按钮（/api/knowledge/{id}/revise|revoke）；修正走 supersedes 版本链，用户纠错即确认直接生效；隐式反馈 + Dreaming 离线整理 + 轨迹→工具候选（v1.5）

### 5.3 图导航层（M8）

三类节点：用户（根）、实体（懒创建：提及 ≥2 或用户标注，手动合并）、话题

- 用户节点挂：本体记忆、用户画像、目标（内容层条目，带 topic_ids 引用，不建独立节点）
- 话题挂：片段（封块边界）、知识条目（多对多）、实体引用
- 锚点 = 当前话题 + 当前片段位置；防抖切换、移动非丢弃、优先级非硬限制、单锚点 + 一跳联想
- 跨会话检索：会话级元摘要（话题指纹）第一跳 + 全局检索三权重排序（recency decay / session affinity / relevance）；memory_search 工具

### 5.4 注入预算（M9）

- 注入预算 = 模型真实上下文窗口 × 25%（动态解析：内置模型长度表 → 探测降级 256K/128K/64K → 兜底）
- 短期记忆优先占用，其余候选相关性排序后统一截断
- 记忆强度滑块已移除（注入量由系统决定，不暴露给用户）

### 5.5 工具机制（M10）

- 函数型 + 子 agent 型并列；同步/异步；子 agent 独立循环/模型/BYOK Key 引用，默认 untrusted，产出不自动进知识域；主 agent 反馈进记忆与知识，子 agent 结果作为可折叠工具卡片展示
- 生命周期：需求澄清（用户只提需求，技术实现自动化）→ 沙箱开发工作区 + dev 工具链 → 双保险交叉测试 → 双段审批（创建 + 凭据授权，用户友好展示）→ 并列注册
- 沙箱：默认受限子进程执行器 + Docker 可选（无显卡低配机不常驻 Docker）
- 工具定义可脱离主循环自主运行（子 agent 化），拥有自己的 apikey 引用；异步任务完成可唤起主 agent（notify 策略），通知 turn 不写 user 消息、不递归

### 5.6 选择器（M5）

可插拔多层：

- 规则层：永远启用
- 召回层：可插拔（BM25 / ONNX 量化嵌入 CPU / 远程 API 嵌入）
- 精排层：可选默认关闭
- 启动自检自动选档，适配无显卡机器

### 5.7 凭据 API 与前端交互

- 创建/更新 Key：密钥只写不读，查询返回掩码元数据
- 测试连接：最小请求验证 Key + 返回三态探测结果
- 撤销：软删除保留审计
- 状态变化实时推送 CREDENTIAL_STATUS

## 6. 数据流（一个对话 turn）

```
用户输入 → POST /api/turns（后续）
  → 快照（凭据解析）→ CAPABILITY 校验
  → TURN_START
  → 循环：
      PLANNING：适配层 complete（native/text）
      ├─ 无 tool_calls → 终答 → DONE
      └─ 有 tool_calls → TOOL_START → 执行（失败隔离）→ TOOL_END
         → 结果回喂 → OBSERVING → 下一轮
  → 预算检查（次数/token）
  → TURN_END + USAGE → 记忆域写入（M6 后）→ 封块摘要（阈值触发）
```

## 7. 安全边界

- 密钥：只进 keyring，不落 SQLite、日志、事件、API 响应；前端永远掩码；不参与 export/import
- 工具：默认 untrusted，沙箱执行，创建需双段审批
- 数据：SQLite 本地存储，后端独占管理，前端只走 API
- 子 agent：独立循环，BYOK Key 快照，产出不自动进知识域

## 8. 里程碑状态

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 骨架、事件协议 | ✅ 完成 |
| M1 | 存储层（schema/迁移/归档） | ✅ 完成 |
| M2 | 凭据层 | ✅ 完成 |
| M3 | 适配层（三态 + 解析纠错） | ✅ 完成 |
| M4 | 主循环骨架 | ✅ 完成 |
| M5 | 选择器（规则 + 可插拔召回） | ✅ 完成 |
| M6 | 记忆域 | ✅ 完成 |
| M7 | 知识域 | ✅ 完成 |
| M8 | 图导航层 | ✅ 完成 |
| M9 | 注入 + 检索 | ✅ 完成 |
| M10 | 工具创建生命周期 | ✅ 完成 |
| M11 | 前端（对话页/星球页/设置页/审批） | ✅ 完成 |
| M12 | 离线任务（Dreaming/隐式反馈/轨迹→工具候选） | ✅ 完成 |

## 9. 环境与工具链备注

- Python：3.12（codex 捆绑运行时，含 fastapi/openai/keyring/tiktoken/pytest）
- Node：24.x；npm 需用 npm.cmd（PowerShell 执行策略）
- Rust：1.97.1（已装）；crates.io 直连不通，已配置 rsproxy.cn 镜像（~/.cargo/config.toml）
- sidecar 打包：powershell scripts/build_sidecar.ps1（PyInstaller onefile，tiktoken/uvicorn/keyring 钩子）
- 验证命令：
  - `python -m pytest`（backend，75 测试）
  - `python backend/scripts/verify_sse.py`（真实 SSE 链路）
  - `npm run build`（frontend）
  - `cargo check`（src-tauri）
- Codex 审批：auto-review 请求经 cc-switch 转发时模型名须映射（codex-auto-review → deepseek-v4-flash），已在 cc-switch modelCatalog 配置

## 10. 后续待办（M7 起）

1. M11 前端：聊天视图、网状导航、记忆/知识面板、凭据管理界面
2. ~~M12 离线任务设计~~ 已完成（v1）





