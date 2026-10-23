# R9-H 契约：审批与授权机制第三轮修复（冻结）

> 基线：`r9g/main` = `60208e325679303a545f1cac47bef2f0edb56b21`（开工 fetch 核对：远端一致、工作区干净）
> 修复分支：`r9h/main`（worktree `D:/qio-dev/qio-r9h-main`）。本轮不合并 main、不发布安装包、不新增运行期沙箱/文件范围隔离/网络隔离/子进程隔离。
> 前两轮契约 `docs/r9g-contracts.md`、`docs/r9f-contracts.md` 继续有效；本轮只修真实接线、一致性、限制恢复与验证缺口。

## 1. 四个已确认问题与目标

1. **要求解析成功后主流程拿不到绑定**：`applied.requirement_id` / `applied.id` 不存在，`AppliedResolution.requirement` 才是真实要求；成功路径又不走回退登记，导致 `requirement_binding` 为 None。
2. **内容变化仍可能复用旧授权**：`ReviewOperation`/`operation_fingerprint()` 有 content_digest/precondition_digest/spec_digest/delete_mode/conditions，但引擎 `_review_operation()` 没有从真实 effect 传入，工具产生的摘要没有进入复用键。
3. **名称仍直接决定权限**：`security/internal_data.py` 的 `is_sensitive_credential_path()` 用 `.git/.ssh/credentials/.config` 目录名、固定文件名、`.key/.pem/.crt` 后缀直接判敏感；引擎据此拒绝读取/要求确认。
4. **结构化限制不能持久化/恢复**：`user_requirements.limits` 仍是展平文字，引擎忽略纯文字；执行前重核对重建 AuthContext 时丢失限制；重启/后台/审批恢复可能放行本应被用户限制的操作。

## 2. 必须保持的既定规则

- 只读：普通业务修改拒绝；已验证固定查询允许；内部运行数据仍按既定规则（读取拒绝/写入三次确认）。
- 受限：固定规则、有效授权、模型语义审查；永久删除业务文件必须明确人工确认（模型 ALLOW 不可替代）。
- 完全无限制：跳过普通授权审查与常规审批；永久删除允许；**用户有效限制仍生效**。
- 内部运行数据直接维护：三次确认（全模式），不叠加第四次；普通同名文件/目录不因名字自动成为内部数据。
- 固定查询仅限程序版本、当前目录、系统基础信息、目录列表、文件属性等具体用法；解析不确定走普通流程。
- 默认删除优先系统回收站；审批十分钟原始期限、重启不续期、不阻塞无依赖工作。
- **禁止**重新引入自然语言关键词/问句/否定词/条件词/引号/句式匹配决定权限；语义由模型理解，明确事实由固定规则核对（程序/参数/对象身份/路径关系/版本/有效期/确认次数）。

## 3. 修复一：要求绑定与真实用户原话（A 负责 app.py/resolution.py/prompts.py 解析段；C 负责 requirements.py）

### 必须行为
- `apply_resolution()` 返回 `AppliedResolution`，真实要求是 `.requirement`；`new`/`amend` 后必须用 `requirements.binding_for(requirement.requirement_id)` 取得**有效版本绑定**；`withdraw` 后不得绑定已撤回要求（`.requirement` 可能是历史行，但 status=withdrawn，`binding_for` 返回 None）。
- 主流程必须把 `message=instruction.text`（真实用户原话）、`message_id`、`turn_id`、`topic_id=ctx.initial_topic` 传给 `apply_resolution()`；模型 `corrections` 不得替代原话。
- 解析成功后若应有绑定却无法建立（new/amend 且 binding_for None/版本不一致/异常）：记录明确错误（日志/轮上下文/台账审计），`resolution_status` 标为不可用；**不得**再回退登记另一条要求，不得使用“最近一条 active 要求”，相关操作因缺少绑定进入需确认路径，不能标记为完整成功。
- 解析失败（模型不可用/非法输出）继续按新要求登记并标 unavailable（既有行为），不静默扩大旧授权。
- withdraw 后同一轮/后续操作不得继续使用旧要求/旧授权；后台、子智能体、审批恢复继续携带同一有效绑定，不得因本轮修复破坏。

### 接口冻结
- `AppliedResolution` 保持字段：`resolution_id/relation/requirement/target_requirement_id/superseded_requirement_id/limits/corrections/reason`；本轮可增加 `limits_state`/`structured_limits` 等只追加字段（C 实现）。
- `RequirementLedger.binding_for(requirement_id) -> RequirementBinding | None`（仅 active 返回，含 version）。
- `ResolutionOutcome` 可追加 `removed_limits: tuple[dict,...] = ()`（仅 amend 解除限制用，见 §6）；A 在 resolution.py 解析/校验中支持，C 在 apply_resolution 使用。

### 必须测试（真实 `_execute_turn`）
- 不能用测试预先登记正确要求再手动 set `requirement_context` 作为修复证据。允许 fake 模型返回、可观察编排器；要求解析落实、台账、主流程绑定必须真实。
- new 后编排器内 `current_requirement` 是真实 active 绑定；amend 后是新版本且旧版本 binding 不可用；withdraw 后无有效绑定且旧授权不可用。
- 原话逐字等于真实用户输入（含模型 corrections 被改写/追加时）；message_id/turn_id/topic_id 来源正确、不串用。
- 解析成功但 binding 建立失败时不产生错误绑定、不静默成功；解析失败不扩大旧授权。
- 取消/停止不触发后续实际操作；取消轮仍留正确终态（既有行为）。

## 4. 修复二：内容绑定贯通真实组装路径（B 负责 review.py/工具；Lead 负责 engine.py）

### 字段映射（effect.detail → ReviewOperation）冻结
| effect.detail 键（工具生产） | ReviewOperation 字段 | 语义 |
| --- | --- | --- |
| `content_digest` 或 `content_sha256` | `content_digest` | 拟写入内容/补丁/附件写回内容摘要 |
| `precondition_digest` 或 `base_content_sha256` | `precondition_digest` | 覆盖前/删除前对象身份+内容摘要（新文件用 absent 标记） |
| `spec_digest`（或 `execution.spec_digest`） | `spec_digest` | 程序/argv/cwd/脚本内容规范化摘要 |
| `script_sha256` | `script_sha256` | 脚本文件内容摘要（同路径不同内容必须不同） |
| `delete_mode`（`recycle_bin`/`permanent`；否则按 permanent 标记/默认回收站推导） | `delete_mode` | 删除方式 |
| `conditions` | `conditions` | 结构化条件与证据 |
| `network_hosts` / `destination` | `network_hosts` / 对象 | 接收方/目的地 |
- 引擎从真实 effect 读取，不信任工具自报“已审查”字段；对象身份、来源仍走既有 ReviewObject。
- 工具必须为真实操作填写上述事实：fs_write/fs_patch/fs_delete、附件写回/回收、命令 run_program/run_shell（脚本/内联代码/argv/cwd/spec_digest）、审批恢复执行器。相同文件身份不等于相同内容；相同脚本路径不等于相同脚本内容。
- 审查请求、授权记录、审批单、执行前重核对、模型审查/人工批准复用必须使用同一份 ReviewOperation 指纹；内容/对象/影响变化后旧记录不得命中；授权范围确实覆盖变化后操作时可重新判断，不机械重复人工确认。
- 审查后执行使用绑定内容，不重新生成未经核对的脚本/补丁/命令；日志/测试输出/审计不得出现密钥原文，用摘要绑定。

### 必须测试（真实引擎组装）
- 同一文件同一身份：第一次 content A 审查允许；第二次 content B 不得复用仅覆盖 A 的记录（测试必须通过真实工具/引擎 `_review_operation` 产生，不得手填 ReviewOperation.content_digest 作为接线证据）。
- 写入、补丁、附件写回；同路径脚本内容变化；内联代码/参数/cwd/目的地变化；前置状态变化；模型授权与人工批准两条路径；批量子项独立绑定；相同内容在授权有效时正常复用。

## 5. 修复三：清除名称直接决定权限（C 负责 internal_data.py；Lead 负责 engine.py 接线）

- 删除 `SENSITIVE_DIR_NAMES/SENSITIVE_FILE_NAMES/SENSITIVE_SUFFIXES/is_sensitive_credential_path` 及引擎中据此的敏感分支；名称只能作为理解材料信息，不单独产生允许/拒绝/确认。
- 内部运行数据只按 AppContext 真实注册路径 + 路径规范化（含符号链接/相对路径）识别；`is_internal_data_path`/`effect_targets_internal_data` 保留。
- 已明确保护的注册对象继续受保护；普通 `.key/.pem/.crt`、普通 `credentials`/`.config` 目录按普通读取/写入规则处理，不自动敏感。
- 改名/别名/规范化访问不得绕过真实注册范围；移出注册范围的对象按现有模式与语义处理，不新增全盘扫描/敏感识别产品，不新增普通读取模型审查。
- 保留真实命令语法解析、结构化参数核对、路径关系检查；它们不属于自然语言关键词授权。

### 必须测试
- 普通 .key/.pem/.crt 同名材料不因后缀被拒绝；普通 credentials/.config 同名目录不整体限制。
- 真实注册内部数据（db/config/credentials/instances/logs/tmp 等）仍按既定规则（读取拒绝、写入三次确认）。
- 改名、符号链接/相对路径引用、路径规范化不绕过真实保护；固定查询不成为绕过入口。

## 6. 修复四：结构化限制持久化、继承和恢复（C 负责 requirements.py/迁移/schema/migrate/新模块；Lead 负责 engine/recheck；A 负责 resolution 字段）

### 迁移 59 / 补偿 60（只追加，不改历史迁移）
- 迁移 59 给 `user_requirements` 增加：
  - `structured_limits TEXT`（JSON 数组；NULL = 历史未知，`[]` = 明确无限制）
  - `structured_limits_state TEXT NOT NULL DEFAULT 'legacy'`（legacy / structured / unresolved）
  - `structured_limits_source TEXT`（记录理解依据，如模型+提示词版本/错误码）
- 补偿 60 = 补偿 58 全部语句 + 迁移 59，去重；`SCHEMA_VERSION=COMPENSATION_VERSION=60`；`REQUIRED_OBJECTS` 增加新列；迁移套件必须通过。
- 新要求：state=structured、structured_limits='[]' 或实际 JSON；amend 继承/合并父限制；withdraw 失效；new 写本次限制。

### 接口冻结（新模块 `security/requirement_limits.py`）
```python
def normalize_limits(value: object) -> tuple[dict, ...]      # 结构校验/去重，非法抛 ValueError
def serialize_limits(limits) -> str                          # JSON（不含密钥原文）
def deserialize_limits(raw: object) -> tuple[dict, ...] | None  # NULL/非法 -> None（历史未知）
def merge_limits(parent, *, added=(), removed=()) -> tuple[dict, ...]
def limits_from_requirement(requirement) -> tuple[dict, ...] | None
```
- 限制条目结构：`{operations: [write/create/delete/execute/install/system_change/egress], objects: [{kind: path|path_prefix|directory|program|service, value: str}], reason: str}`；`removed` 条目同结构（仅 amend 使用）。
- `UserRequirement.structured_limits: tuple[dict,...] | None`、`structured_limits_state`、`structured_limits_source`；`RequirementLedger` 读取/写入；`apply_resolution` 持久化。
- amend 语义：父 structured 已知 → 子 = merge(parent, added=resolution.limits, removed=resolution.removed_limits)，state=structured；父 legacy/unresolved → 子保留 legacy/unresolved（可存已知新增但不得声称完整，恢复时按未解决处理）。用户只补充一句话不得清空父限制；明确解除的限制从父集合移除。
- withdraw：目标 status=withdrawn；旧限制不再被绑定/复用。
- 引擎 `_limit_conflict` 使用：context 本轮 limits + requirement.structured_limits；state legacy/unresolved 不得当作“明确无限制”：完全无限制模式下对受影响操作要求确认/不自动执行（只处理相关操作），受限模式走正常审查；read-only 固定规则不变。
- 执行前重核对：从台账按 requirement_id/version 重新加载有效限制，重建 AuthContext 不得丢失 limits/resolution 状态；后台、子智能体、人工审批恢复、重启恢复一致。

### 历史数据兼容
- 旧行 structured_limits=NULL/state=legacy 不得当作“已确认无限制”。
- 可从真实原话按既有模型流程重新理解并记录结果/依据（新提示词/方法，不关键词解析展平文字）；失败标 unresolved。
- 无法可靠恢复时只处理受影响具体操作，不静默清空全部授权、不让所有操作一律重复审批；授权生命周期/跨重启范围仍按既定规则。

### 必须测试（真实台账 + 持久化 DB）
- 当前上下文限制有效；清除临时上下文后从台账重载仍有效；新建服务实例/独立进程恢复仍有效。
- 审批等待、批准恢复、后台继续仍有效；执行前重核对不丢限制；amend 保留/修改/解除；withdraw 后旧依据失效；完全无限制恢复后仍遵守限制；无关要求限制不串入。

## 7. 固定查询兼容性与 CI（D 负责）
- 复现 `tests/test_r9g_fixed_queries.py::test_node_npm_version_forms` 在 `/opt` 安装路径的失败：区分程序身份、符号链接、安装位置与测试假设。
- 修复必须按真实可执行程序及参数身份；只允许系统安装目录下同族包管理器符号链接入口（如 `/usr/local/bin/npm -> npm-cli.js`、`/opt/.../bin/npm`），拒绝工作区/cwd/临时目录/任意可写目录的同名替身或脚本；不放行整个 `/opt` 任意程序。
- 不得 xfail/skip/删除断言；Windows 与 POSIX 路径都覆盖；保留同名替身/内容替换负向测试；不支持的程序来源产品行为与测试预期一致并说明依据。
- CI 排查：开工重新读取实际运行状态与日志；记录工作流/run id/SHA/失败 job/用例/根因/修复/重跑范围。不得删检查、降阈值、放宽断言、continue-on-error 或跳过必要 job。

## 8. 分支、文件归属与接口纪律

| 分支/worktree | 负责 | 独占文件 |
| --- | --- | --- |
| `r9h/main` qio-r9h-main | Lead：engine.py/context.py/recheck.py/gateway.py、集成、最终验收、文档 | engine.py、context.py、recheck.py、gateway.py、storage 只读审查、docs/status、docs/r9h-* |
| `r9h/a-binding` qio-r9h-a | A：要求绑定/原话/主流程接线 | services/app.py、security/resolution.py、prompts.py 解析段、tests/test_r9h_binding_flow.py |
| `r9h/b-content` qio-r9h-b | B：审查对象/内容绑定/复用 | security/review.py、tools/fs_tools.py、tools/attachment_tools.py、services/attachment_access.py、services/attachments.py、tools/cmd_tools.py、tools/approved_operations.py、tests/test_r9h_content_binding.py |
| `r9h/c-limits` qio-r9h-c | C：名称清理 + 结构化限制持久化/恢复 | security/internal_data.py、security/requirements.py、security/requirement_limits.py、storage/migration_r9h_limits.py、storage/schema.py、storage/migrate.py、tests/test_r9h_internal_data.py、test_r9h_limits.py |
| `r9h/d-verify` qio-r9h-d | D：固定查询/真实流程回归/CI 排查 | security/fixed_queries.py、tests/test_r9g_fixed_queries.py、tests/test_r9h_fixed_queries_platform.py、scripts/r9h_*.py、docs/r9h-reports/D.md |

- 只提交自己分支；不 push、不 merge main；Lead 统一推送/集成/完整 CI。
- 共享接口按本契约；需要变更先在报告说明并由 Lead 冻结。
- 定向测试优先；**全量 pytest 与完整 CI 只能 Lead 统一各最多两次**（本轮独立预算），子智能体不得触发全量。
- 不删除/跳过/放宽既有测试；测试必须验证真实接线；模型调用用 FakeAdapter/mock provider，不使用真实 Key。

## 9. 验证关系（必须真实接线）
真实用户消息 → 要求语义解析（fake 模型返回）→ 真实台账/版本 → 主流程绑定 → 真实工具产生的操作事实 → 引擎组装审查对象 → 授权记录/人工审批 → 执行前重核对 → 实际执行或明确阻止。
不能用替身跳过：主流程绑定取得、原话/来源保存、effect→审查对象转换、内容摘要进入复用依据、真实数据库保存/恢复限制、审批恢复/重核对读取信息。
竞态使用可控同步点（审查等待、排队、审批恢复期间模式变化、要求撤回、内容变化），不依赖随机 sleep。

## 10. 交付
- 更新 `docs/status.md`（P76）与 `docs/r9h-merge-notes.md`，区分已实现/已验证/未验证/缺口。
- 最终报告中文，包含：基线/分支/最终提交；四个问题根因/修改位置/真实流程验证；固定查询处理依据与平台结果；结构化限制迁移/兼容/继承/恢复策略；测试命令/结果/全量次数/远端 CI 运行号；最终代码 CI 成功证据；已知边界；合并准备。