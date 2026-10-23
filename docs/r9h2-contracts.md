# R9-H2 契约：第三轮后续补修（历史限制链/解析失败状态/内部数据身份）

> 基线：r9h/main = 6ee961d293ec9d06390ca36c00fa491b895b4647（代码提交 bb260107f61f47a2874296de84f4a29f140f7959；两者之间仅 docs/evidence）
> 完整 CI：39930482370 Success（代码 SHA bb260107）；开工已 fetch 核对远端无后续提交。
> 修复分支：r9h2/main（worktree D:/qio-dev/qio-r9h2-main）。
> 本轮沿用 R9-H 累计预算：完整 CI 已用 1/2；本地全量 0/2；最多再用 1 次完整 CI、最多 2 次全量。不新增产品功能、不改变既定权限政策、不合并 main、不发布。

## 1. 三个缺口

1. 历史 legacy 要求补充后，_ensure_legacy_structured_limits 只把子要求当前补充句交给 extract_limits；子要求被标 structured/空限制，unrestricted 下原限制丢失。
2. 解析失败后 fallback record_user_message 写入 structured_limits_state=structured / structured_limits=[]，把“未解析成功”落成“已确认无限制”。
3. internal_data 只按规范化路径判断；硬链接/移动/改名后的同一真实对象失去保护；当前测试把“移出注册范围按普通文件”当预期，与对象身份保护不一致。

## 2. 必须保持

- 真实要求绑定/原话保存；内容/补丁/脚本/前置状态参与授权绑定；批量逐项；执行前重核对；固定查询既定范围；永久删除三模式规则；内部数据三次确认；审批 10 分钟/重启不续期/重复回调不重复执行；unrestricted 跳过普通独立授权审查但遵守有效限制。
- 不新增运行期沙箱/网络隔离/子进程隔离/受控文件代理/新审批产品；不重新引入名称/后缀/目录关键词判定；不新增覆盖所有普通操作的模型审查。

## 3. 修复一：历史要求链限制恢复（A 负责）

### 3.1 台账接口（A 在 security/requirements.py 实现，B 在 app.py 消费）

新增不可变数据类 RequirementRecoveryContext（放 requirements.py，可被 resolution.py 导入）：
- requirement_id: str
- version: int
- status: str
- parent_id: str | None
- chain: tuple[RequirementChainNode, ...]  # 根 -> 当前，按真实父子顺序
- complete: bool  # 父链完整、无缺失/循环/非 active 中断；当前要求仍 active
- reason: str  # complete=False 时原因
- chain_digest: str  # 对 (requirement_id, version, parent_id, text, status) 有序序列的 sha256

RequirementChainNode：
- requirement_id, version, parent_id, text, status, provenance: dict,
- structured_limits: tuple[dict,...] | None, structured_limits_state: str,
- structured_limits_source: str | None

RequirementLedger.recovery_context(requirement_id) -> RequirementRecoveryContext：
- 只用真实台账，按 parent_id 逐级回溯到根，再反转为根->当前；不得用 candidates/最近 active。
- 链中任一父行缺失、id 循环、当前非 active、版本不连续（子 version != parent.version+1）→ complete=False 并给 reason；不抛异常。
- text 原样来自 user_requirements.text（用户原话）；不拼模型摘要/corrections。
- 不把全部历史消息/其他要求拼入；只取该要求祖先链。
- structured_limits/source 原样读出（可能 None=legacy）。

RequirementLedger.set_structured_limits 增加条件写入参数（A 实现）：
- expected_version: int | None = None
- expected_status: str | None = None（恢复时传 "active"）
- expected_chain_digest: str | None = None
- 在同一事务 UPDATE 时用 WHERE id/version/status 与 chain_digest 重新计算核对；不匹配抛 RequirementVersionMismatch（ValueError 子类），绝不覆盖新版本/已撤回要求。
- 失败恢复时保留已有可靠限制：setter 收到 limits 原样写入；调用方不得用 () 覆盖已有非空 known limits。
- 返回 reloaded requirement；source 最大 300 字符（现有约束）。

RequirementLedger.record_user_message 增加（A 实现）：
- structured_state: str = STATE_STRUCTURED（兼容直接调用）
- structured_limits: Sequence[dict] = ()
- structured_source: object = "user_message"
- 按参数写入 state/limits/source；非法 state 抛 ValueError。
- 生产 fallback 必须显式传 STATE_UNRESOLVED，不得依赖默认值；只有成功语义理解后才写 structured。

### 3.2 模型恢复接口（A 在 security/resolution.py + prompts.py 实现）

ModelRequirementResolver.extract_limits_from_context(context, *, requirement_id, version) -> tuple[dict,...]：
- 输入完整 RequirementRecoveryContext：目标要求版本 + 根到当前全部用户原话、顺序、父子/版本关系、已可靠 known limits/state/source；明确告知模型“输出该目标版本当前完整有效限制”。
- 提示词要求：考虑后续补充、明确解除/替换、顺序；输出仅结构化限制 JSON；不输出解释/关键词；不把摘要当原话。
- 失败/非法输出抛 RequirementResolutionUnavailable（不返回空数组冒充成功）。
- 保留 extract_limits(text, ...) 兼容包装（旧调用/测试），但 app 的历史恢复必须走新方法；两者共用结构校验。
- 提示词版本新常量（如 r9h2-limit-extraction-chain-v1），写入 source；不新增关键词判断。
- 返回结构经 requirement_limits.normalize_limits 校验；空数组合法但仅代表“完整理解后确认没有限制”。

### 3.3 app 接线（B 负责，A 提供接口）

_ensure_legacy_structured_limits(ctx,binding) 改为：
1. requirement = by_id(binding.requirement_id)；state 仅 legacy/unresolved 处理；否则返回。
2. context = requirements.recovery_context(id)；若 complete=False：不调用模型，保持原 state（若已有 known limits 保留），记录 notice/日志，返回。
3. 调 resolver.extract_limits_from_context(context, requirement_id=..., version=...)（同步/异步兼容）。
4. 返回后**重新读** requirement 与 recovery_context：若 id/version/status/parent_id/chain_digest 任一变化，丢弃结果、不写、notice；不得覆盖新状态。
5. setter(..., state=structured, source=prompt_version + ";chain=" + chain_digest + ";version=" + version, expected_version=version, expected_status="active", expected_chain_digest=chain_digest)。
6. 异常/版本不匹配：保留已有 known limits（requirement.structured_limits 原值，不能写空数组覆盖），set state=unresolved、source=失败码 + chain/version；notice；不阻塞本轮。
7. 成功：persist structured；本轮 resolution_limits 使用新限制（app 返回前重新读 binding/limits）。
8. 不把模型 corrections/摘要拼入原话。

### 3.4 A 测试（真实台账 + 真实主流程）

tests/test_r9h2_chain_recovery.py：
- legacy 原要求“修复项目，但不要修改 B”，用户只补充“把标题改成 X” → amend 子要求 legacy；恢复输入包含两条原话/顺序/父子/版本；恢复后 B 仍被禁止；不得只喂补充句。
- 多次补充后原限制仍有效；明确解除 B 后解除；只解除一个时其他保留；不相关要求限制不串入。
- 缺少父要求/来源不完整 → complete=False，不标 structured。
- 模型等待期间 amend/withdraw：旧结果不能写新版本（真实 ledger + 可控 async 闸门）。
- 失败保留已有 known limits 与 source。
- 测试允许 fake resolver，但必须调用真实 _prepare_requirement_binding/_execute_turn 和真实 ledger/apply_resolution；不得测试预先提供完整限制绕过 context 组装；不得只测 merge_limits。

## 4. 修复二：解析失败状态一致性（B 负责）

### 4.1 状态语义

- structured = 已成功完成语义理解并通过结构校验；[] = 明确确认没有限制。
- legacy = 历史数据未知；unresolved = 解析失败/超时/无解析器/非法输出/恢复失败/上下文不完整；两者都不得当无限制。
- 临时 resolution_status 与持久化 state 必须一致：解析失败 → requirement state=unresolved；成功 new/amend → structured（或既有 legacy 父链未恢复时保持 legacy/unresolved）；withdraw → 不绑定。
- record_user_message 默认兼容 structured，但 app fallback 必须显式 unresolved；只有 apply_resolution 成功路径写 structured。

### 4.2 app 修改

- 解析器缺失/异常/超时/输出非法：record_user_message(..., structured_state=STATE_UNRESOLVED, structured_source="resolution_unavailable:<class>")；不再调用 _ensure_legacy_structured_limits 把同一失败消息立即重解释为 structured；返回 status=unavailable。
- 解析成功 new/amend：apply_resolution 已是 structured/unresolved 状态；仅当状态为 legacy/unresolved 才走 §3 链恢复。
- 失败轮相关操作按引擎 unresolved 规则：unrestricted 相关操作需确认，不静默允许；无关工作不受影响。
- 审计/notice 保留失败事实。

### 4.3 engine/recheck 修改（B 负责）

- _apply_review 在 requirement._limits_unresolved 时禁止复用旧授权记录（即使 fingerprint 命中），强制走当前审查/审批；_permanent_delete_decision 同样不得复用旧 allow 记录绕过。
- unresolved/legacy 在 restricted 走正常审查，unrestricted 相关操作不自动放行；read_only 固定规则不变。
- recheck_approved/recheck 从台账加载 state，重建上下文不丢 unresolved；未知状态不因旧授权缓存放行。
- 不新增全局模型审查、不关键词兜底。

### 4.4 兼容迁移（Lead 负责，B 提供识别规则）

- 迁移 61 中 UPDATE 纠正有明确失败证据的旧记录：user_requirements.structured_limits_state='structured' AND structured_limits='[]' AND 存在同 turn_id 的 requirement_resolutions outcome='unavailable'（relation='unknown'）→ state='unresolved', source='migration_61:resolution_unavailable'；保留 text/limits/审计，不删除。
- 不假定所有 [] 都错误；无失败证据的 [] 保持 structured。
- 迁移只追加；补偿 62 = 补偿 60 + 61 去重；版本 62；REQUIRED_OBJECTS 增加新表/列。

### 4.5 B 测试

tests/test_r9h2_parse_state.py（真实 _execute_turn/真实台账，fake resolver）：
- 解析失败/超时/解析器缺失/非法输出 → requirement state=unresolved、[] 不等于 structured、unrestricted 相关操作不自动允许；临时 status 与持久化一致。
- 成功解析且明确无限制 → structured/[] 正常保存并允许。
- 解析失败但用户明确禁止 B（文本）→ 不因错误空限制自动允许；恢复成功后可正常继续。
- 重启/审批恢复/授权复用不改变结论；旧 allow 记录不能绕过 unresolved。
- 无关要求/无依赖工作不受影响。
tests/test_r9h2_state_migration.py（Lead 可共同）验证迁移纠正/不误伤。

## 5. 修复三：内部数据真实对象身份（C 负责，B/Lead 接线）

### 5.1 模块与接口

新增 security/internal_data_identity.py：
- InternalDataIdentityStore(conn)：可信应用维护的身份登记；表由 Lead 迁移 61 建立：
  internal_data_identities(identity_key TEXT PRIMARY KEY, path TEXT, kind TEXT, active INTEGER NOT NULL DEFAULT 1, source TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, retired_reason TEXT)
  index on path/active。
- sync(protected_paths, *, source="app_startup", max_files=5000, max_depth=3) -> dict：
  * 对每个真实注册路径：文件取 file_identity.identity_of；目录取目录自身 + 有界枚举文件（iter_files_limited，禁止全盘扫描）取身份；
  * 同一 path 身份变化（原子替换）→ 旧 identity active=0 retired_reason='replaced'，新 identity active=1；
  * 注册路径暂时不存在/身份读失败 → 保留已有 active 登记（不降级、不删除）；
  * 路径消失但身份仍可能被移动访问 → 保留登记（不永久删除）；
  * 同内容复制件身份不同 → 不登记为原对象；
  * 返回 counts（active/added/retired/errors）。
- active_keys() -> frozenset[str]；known_paths() -> frozenset[str]（active+历史登记路径，用于身份读失败时不静默降级）。
- refresh_path(path, *, source) -> str|None；lookup(identity_key) -> bool。
- 只信任应用路径写入；模型/普通工具不能自报普通来解除；不新增名称/后缀判断。

internal_data.py：
- is_internal_data_path 保持路径规范化（含符号链接 resolve）。
- 新增 is_internal_data_object(path, protected, *, identity_key=None, identity_keys=(), known_paths=(), identity_of=None)：
  1) 路径命中真实注册范围 → True；
  2) identity_key（或 identity_of(path)）在 identity_keys → True；
  3) 路径在 known_paths（历史登记）且身份不可读 → True（不静默降级）；
  4) 否则 False；同内容不同身份 False。
- effect_targets_internal_data(effect, protected, *, identity_keys=(), known_paths=(), identity_of=None)：对 path target 用 target.identity 或 identity_of(target.value) 检查；EffectKind.INTERNAL_DATA 保持 True。
- 旧调用签名保持兼容（新增参数默认空）。

### 5.2 接线

- engine：新增 internal_data_identity_keys / internal_data_known_paths callable 参数（缺省空）；_verdict 与 recheck_approved 调 effect_targets_internal_data 时传入；不改权限政策/顺序。
- app：创建 InternalDataIdentityStore(conn)；启动时 sync(_internal_data_paths())；engine 注入 active_keys/known_paths；提供 refresh 方法；失败时仅告警，不静默降级（路径判定仍兜底）。
- security_gate/文件工具：确保 effect path target 已带真实 identity（现有 file_identity/identity_key 已提供）；不新增名称判断；审批恢复沿用同一 effect/身份。
- 正常应用受控路径保存设置/记忆/日志/审批记录不因身份登记而触发额外内部数据维护确认。

### 5.3 C 测试

tests/test_r9h2_internal_identity.py（临时目录模拟内部数据；不操作真实用户数据）：
- 原路径、相对路径、路径规范化；符号链接；硬链接（支持平台真实执行；不支持明确记录）；同一对象改名/移动；普通同名文件；同内容不同身份复制件；原子替换后新身份登记且旧身份 retired；身份读取失败不静默降级已登记路径；重启（关闭连接重开 store）恢复；只读/受限/unrestricted 既定行为。
- 分类器测试 + 真实已接入执行路径（engine authorize/recheck，B 接线后）；修正“移出注册范围自动普通”旧预期。
- Windows/POSIX 能力差异如实记录。

## 6. 文件归属

| 分支/worktree | 负责 | 独占文件 |
| --- | --- | --- |
| r9h2/main qio-r9h2-main | Lead：迁移/schema/migrate、契约、集成、最终验收、docs | storage/migration_r9h2_*.py、storage/schema.py、storage/migrate.py、tests/test_r9h2_state_migration.py、docs/status、docs/r9h2-* |
| r9h2/a-chain qio-r9h2-a | A：历史链恢复 | security/requirements.py、security/resolution.py、prompts.py（解析段）、tests/test_r9h2_chain_recovery.py、docs/r9h2-reports/A.md |
| r9h2/b-state qio-r9h2-b | B：解析失败状态/引擎/recheck/app | services/app.py、security/engine.py、security/recheck.py、tests/test_r9h2_parse_state.py、docs/r9h2-reports/B.md |
| r9h2/c-identity qio-r9h2-c | C：内部数据身份 | security/internal_data.py、security/internal_data_identity.py（新）、services/file_identity.py、tools/security_gate.py、tests/test_r9h2_internal_identity.py、tests/test_internal_data_guard.py、docs/r9h2-reports/C.md |

- 只提交自己分支；不 push、不 merge main；Lead 统一集成/推送/CI。
- 共享接口按本契约；变更先报告 Lead。
- 定向测试优先；全量/完整 CI 只能 Lead 统一（累计预算：完整 CI 已用 1，剩余最多 1；本地全量最多 2）。
- 不删除/skip/xfail/放宽既有测试；模型调用 fake/mock；不改历史迁移。

## 7. 验收真实流程

真实用户消息 → 真实台账链/版本 → 真实主流程绑定 → 真实模型恢复输入（fake 模型返回）→ 条件写回 → 引擎/审批/重核对读取同一状态 → 实际操作或明确阻止。
不能用替身跳过：要求链读取、原话顺序、状态持久化、身份登记、执行前重核对。竞态用可控同步点，不用随机 sleep。

## 8. 交付

- 更新 docs/status.md（P77）、docs/r9h2-merge-notes.md，区分已修复/已验证/未验证/边界。
- 最终中文报告：基线/分支/最终 SHA/HEAD；三缺口根因/位置/真实证据；历史链与失败状态兼容；内部身份 Windows/POSIX 范围；定向/完整检查/累计次数/CI 运行号；未验证平台与原因；合并准备（不合并/不发布）。
