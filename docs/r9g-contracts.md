# R9-G 契约：审批与授权机制修复（冻结）

> 基线：`r9f/main` = `9211652650e6141d1dd32dc731fad450f8df71b5`（开工 fetch 核对：远端一致、未合并 main）。
> 修复分支：`r9g/main`（worktree `D:\qio-dev\qio-r9g-main`）。
> 本轮不合并 main、不发布、不打包；不新增运行期沙箱/隔离机制（用户已暂缓）。

## 1. 五项必须修复

1. **全面移除关键词/句式授权判断**：不得由文字中词、正则、否定词、条件词、问号、引号、句式是否出现，
   决定允许/拒绝/是否模型审查/是否审批/生成或复用授权/适用哪条要求/是否批量确认/条件是否成立/是否越界。
   同时移除关键词授予、关键词拒绝、关键词触发审批。**语义由模型理解，明确事实由固定规则核对。**
2. **批量操作逐项核对**：展开为明确对象与动作，逐项核对模式/要求/授权/特殊规则；不得用一个对象的允许
   覆盖整批；一次明确批量授权可覆盖列明对象（不重复弹窗）；逐项保留状态；恢复只继续未执行且仍获准项。
3. **操作绑定正确用户要求**：替换"取数据库最近一条 active 要求"；操作可追溯到真实用户消息与具体要求；
   补充/修正/撤回由模型理解目标要求、后端核对身份/版本/状态；后台、互动模式、子智能体、审批恢复、
   重启恢复关联一致；不串用、不被新消息覆盖。
4. **审查/复用/批准/执行一致**：围绕真实拟执行操作绑定动作、对象、范围、来源/目的、删除方式、程序/参数/
   cwd/执行上下文、脚本内容或可靠摘要、写入内容/补丁摘要、前置状态、条件证据、授权来源；脚本内容、内联代码、
   参数、cwd、目的地或动作变化后不得沿用旧允许；执行器使用已绑定内容，不得重新生成后直接执行。
5. **每项实际操作开始前重新核对**：模式（含无限制切回受限）、对应要求是否仍有效/修正/撤回、授权或审批
   是否仍有效/过期/拒绝/失效、确认是否完成、对象与内容是否仍一致、前置状态、条件证据与委派范围；
   等待/排队期间变化按最新状态处理；失效时保留进度（禁止则拒绝、仍覆盖则继续、需用户决策则具体审批）。

## 2. 关键词逻辑清除范围（必须删，不可改名保留）

- `backend/src/agent/security/intent.py`：关键词 grant 能力整体删除（模块可删除；导入/测试同步清理）。
- `backend/src/agent/security/review.py`：删除 `deterministic_allow_guard` 及其全部
  `_ACTION_VERBS/_REFUSAL_PREFIXES/_CONDITIONAL_RE/_QUESTION_TAIL_RE/_QUOTED_RE/_GENERIC_REFUSAL_RE`
  与 `_has_explicit_refusal/_has_conditional_or_question/_has_quote_marks/_text_mentions_operation` 等。
  模型 ALLOW 不再因疑问/否定/条件/引用文字被推翻；后端只校验结构、来源、对象/操作一致性、范围可落实。
- `backend/src/agent/security/engine.py`：删除 `_MUTATION_PROHIBITIONS/_DELETE_PROHIBITIONS/_EGRESS_PROHIBITIONS`
  与 `_user_prohibition` 的文本包含判断。
- `backend/src/agent/tools/cmd_tools.py`：删除 `_command_tokens` 全文搜索式永久删除判断；
  删除"程序名里含 shell 元字符"式字符串判断中与授权结论相关的部分（安全校验可保留但不是许可依据）。
- `backend/src/agent/services/computer.py`：保留结构化命令分类（程序身份/argv 语义），但不得用于
  "全文搜索用户文字"；修复期统一由 §4 固定查询分类器作为唯一许可依据。
- 提醒：正常文本搜索（如知识检索、日志查找）与真正的命令语法解析保留；它们不得成为授权结论。

## 3. 要求版本绑定与解析（R9-G 新接口，W1 实现 + Lead 接线）

迁移 57/58（已由 Lead 写入 r9g/main）：
`user_requirements.version`、`pending_approvals.requirement_id/requirement_version`、
`authorization_records.requirement_version`、`authorization_review_audit.requirement_version`、
表 `requirement_resolutions`（模型要求解析审计）。

### `security/requirements.py`（W1 负责）
```python
@dataclass(frozen=True) class RequirementBinding:
    requirement_id: str
    version: int

class RequirementLedger:
    # 既有：record_user_message / add_correction / withdraw / supersede / by_id / chain/corrections_for
    def binding_for(self, requirement_id: str) -> RequirementBinding | None   # 仅 active 返回
    def validate_binding(self, binding: RequirementBinding) -> bool
    def candidates(self, *, limit: int = 12) -> list[dict]   # 供模型解析的有限候选（id/原话摘录/版本/时间）
    def apply_resolution(self, resolution: "ResolutionOutcome", *, message_id, turn_id) -> AppliedResolution
    # 行为：new→新建 requirement(version=1)；amend→以 target 为父 add_correction(version=parent+1) 并 supersede 父；
    #       withdraw→target withdrawn（不改写历史）；全部写 requirement_resolutions 审计；目标不存在/非 active/版本不符 → 抛错，不静默新建。
```
- **删除当前 `current()` 作为授权依据的用法**（可保留只读列表用途但 engine/tools 不得调用）。
- `UserRequirement` 增加 `version: int`；active 判定 + version 是操作绑定依据。

### `security/resolution.py`（W1 新增）：模型理解"这条消息是新要求/补充/修正/撤回"
```python
class RequirementRelation(str, Enum):
    NEW = "new"; AMEND = "amend"; WITHDRAW = "withdraw"

@dataclass(frozen=True) class ResolutionOutcome:
    relation: RequirementRelation
    target_requirement_id: str | None
    corrections: tuple[str, ...]        # 对目标要求的补充/修正（用户语义）
    limits: tuple[dict, ...]            # 结构化限制：{"operations": [...], "objects": [...], "reason": str}
    reason: str
    model: str; prompt_version: str

class RequirementResolutionUnavailable(Exception): ...

class RequirementResolver(Protocol):
    async def resolve(self, *, message: str, message_id: str, turn_id: str,
                      candidates: list[dict]) -> ResolutionOutcome: ...

class ModelRequirementResolver:  # 复用主模型 adapter_factory；单次调用、无工具/文件/设置
    ...
def validate_resolution(outcome, *, candidates) -> ResolutionOutcome
# 校验：relation 合法；AMEND/WITHDRAW 必须给出存在的候选 target；limits 对象必须来自候选/后端可解析对象；
# 不落推理全文；失败 → RequirementResolutionUnavailable（后端按"新要求"记录并标 unresolved，不用关键词兜底）。
```
- 解析调用发生在**每个用户 turn 开始**（不是授权审查；不因模式不同而跳过；完全不产生执行许可，
  只产生要求关系与结构化限制）。解析失败：登记失败审计 + 按新要求继续；授权审查请求中带
  `requirement_resolution_status="unavailable"`，模型审查可结合上下文判断，但后端不补关键词。
- 完全无限制模式的用户限制来自该解析结果的结构化 limits（不是关键词，也不新增授权审查）。

### 上下文绑定
`security/context.py` 增加 `RequirementBinding` contextvar（Lead）：turn 内工具/子智能体/后台派生共享；
`AuthContext` 增加 `requirement: RequirementBinding | None`；engine 只用该绑定，缺失 → 需人工确认，不回退"最近一条"。
子智能体 scope 继承父 requirement 绑定，不得改绑。

## 4. 固定查询（W2 新增 `security/fixed_queries.py`；engine 调用，唯一许可依据）

仅允许已验证无修改行为的具体用法：
- 程序版本（python --version / -V、git --version、node --version、npm --version、pip --version、uv --version 等，
  按真实解析后的程序身份 + 精确 argv 语义判定）；
- 当前目录（pwd / cd 无参 等，按平台实际程序）；
- 系统基础信息（已定义白名单：whoami / uname 等价只读，不得整程序放行）；
- 指定目录文件列表（ls/dir 及已验证参数，目标按读取规则校验）；
- 文件大小/类型/修改时间属性（stat/ls -l 等价已验证用法）。

接口：
```python
@dataclass(frozen=True) class FixedQueryFacts:      # 由启动方从真实 LaunchSpec 生成，非模型文本
    kind: str; program_path: str | None; program_name: str
    argv: tuple[str, ...]; cwd: str | None; shell: bool

@dataclass(frozen=True) class FixedQuery:
    kind: str  # version | cwd | system_info | list_dir | stat
    facts: dict
    reason: str

def classify_fixed_query(facts: FixedQueryFacts) -> FixedQuery | None
```
- 只看程序真实身份/argv 结构；**禁止**搜索 "--version" 等字符串、禁止按程序名整程序放行、
  禁止对同名程序/任意参数/解释器脚本豁免；查询 + 重定向/串联/嵌套/后续修改一律不属固定查询；
  解析不确定 → None（退出固定查询，走普通流程）。
- **单一来源**：cmd_tools 的只读证明、engine 的 read_only/restricted 判定都调用该分类器；
  引擎自行用 effect.detail 的程序/argv/cwd 重新分类，不采信工具自报的"fixed_query"字段。
- read_only：固定查询 ALLOW；其余执行拒绝。restricted：固定查询直接 ALLOW（不调用模型审查、不弹确认）。
- 修复"命令工具认为可只读、统一引擎一律拒绝"的冲突：引擎对 EXECUTE 先调用固定查询分类器；命中 → ALLOW。

## 5. 永久删除（W2 新增 `security/deletion.py`；engine 政策，Lead 接线）

```python
@dataclass(frozen=True) class DeleteObject:
    path: str; identity: str | None; is_directory: bool

@dataclass(frozen=True) class DeleteIntent:
    permanent: bool
    objects: tuple[DeleteObject, ...]
    method: str            # fs.permanent_delete | command.delete
    program: str | None; argv: tuple[str, ...]; shell_cmd: str | None
    verified: bool         # 后端从实际程序/参数/工具声明解析成功且完整
    reason: str

def detect_delete_intent(*, effect_kind, detail, facts) -> DeleteIntent | None
```
- 命令按实际程序身份/参数语法/已知语义解析（rm/del/Remove-Item/rmdir 等，但**不得全文搜索**这些文字；
  讨论/引用/输出这些词不触发）；shell 含管道/重定向/串联/嵌套 → 不视为可解析的永久删除（走普通流程）。
- 文件工具：只有明确声明永久删除的合法工具/参数才产生 permanent intent；默认删除仍走系统回收站。
- 政策：
  | 模式 | 永久删除 |
  | --- | --- |
  | read_only | DENY |
  | restricted | 必须有**明确人工确认**（审批单列具体对象、永久删除、不进回收站、不可恢复）；模型 ALLOW/一般授权记录不能替代 |
  | unrestricted | ALLOW |
  | 内部运行数据 | 三次确认优先，不叠加第四次；普通一次确认不能替代三次 |
- 复用：已有仍有效、明确覆盖相同对象与永久删除行为的人工确认记录可按规则复用（AuthorizationRecord source=user_approval,
  kind=delete_permanent，fingerprint=对象身份集合+动作）；不得扩大为目录/永久许可。
- 审批/确认必须绑定同一对象集合与动作摘要；执行前重核对。

## 6. 批量（W3 + W2 + Lead）
- 批量入口统一展开：`BatchItem(item_id, effect, label, payload, fingerprint)`；
  引擎按项判定（`authorize` 逐项，复用已有记录）→ 逐项状态：allowed/denied/waiting/invalidated/completed。
- 一次明确批量授权可覆盖列明对象：仅**需要确认的项**合并为**一个**审批操作（payload items 列表，
  每项含对象身份/动作/内容摘要），不逐项弹窗；已允许项直接执行；被拒项如实列出。
- 目录/通配符展开后新增未覆盖对象不得偷用授权：逐项核对，未覆盖项单独处理。
- 恢复/重试只继续未执行且仍获准的项；不重复成功项；有依赖/整体一致性要求的批次执行前确定策略
  （无依赖的已授权项可继续）。
- 移动/重命名核对源+目的；复制/上传/外发核对来源与目的地两侧。

## 7. 一致性与执行前重核对（Lead `security/recheck.py` + 各执行器调用）
操作绑定字段（审查请求、授权记录、审批单、队列项都必须携带同一份）：
```python
@dataclass(frozen=True) class OperationBinding:
    requirement: RequirementBinding
    mode: str
    operation_kind: str
    fingerprint: str              # review.operation_fingerprint（含对象身份/程序/argv/cwd/脚本摘要/接收方/模式）
    spec_digest: str | None       # 启动类：程序身份/argv/cwd/脚本内容
    content_digests: tuple[tuple[str, str], ...]  # (对象, 内容/前置状态摘要)
    conditions: tuple[dict, ...]  # 条件与证据
    authorization_id: str | None  # authorization_record / approval_id
    delegation: dict | None       # 子智能体范围
```
```python
@dataclass(frozen=True) class RecheckResult: ok: bool; code: str; reason: str; refreshed: OperationBinding | None
class RecheckService:
    async def recheck(self, binding, *, effect) -> RecheckResult
```
- 重核对 = 用**当前**模式与绑定 requirement 重新走 engine.authorize（含固定规则/模型审查复用/审批规则），
  再核对对象身份/内容摘要/前置状态/审批有效期/确认次数/条件证据/委派范围；全部一致才返回 ok。
  模式变化（含无限制→受限）必须按新状态重新判定；已过期/拒绝/撤回 → 不执行；仍覆盖 → 继续；
  需用户决策 → 具体审批。检查与真正开始之间不得长期排队：执行器取得重核对结果后立即开始该操作。
- 覆盖入口：fs_*、cmd_*、自建工具、子智能体、批量子项、模型审查返回、人工批准恢复、后台继续、重启恢复。

## 8. 三种模式与固定命令规则（冻结）

| 场景 | read_only | restricted | unrestricted |
| --- | --- | --- | --- |
| 普通读取 | 既有规则 | 既有规则 | 既有规则 |
| 固定查询 | ALLOW（不审查） | ALLOW（不审查/不弹确认） | ALLOW |
| 普通写入/安装/一般执行 | DENY | 固定规则/有效授权直接执行；需理解时模型审查 | ALLOW（跳过审查/审批） |
| 永久删除业务文件 | DENY | 明确人工确认（模型 ALLOW 不可替代） | ALLOW |
| 内部数据直接维护 | 三次确认（既有关系不变） | 三次确认 | 三次确认 |
| 应用自身受控运行 | 可信内部操作类型/调用路径区分，不一律当内部数据 | 同左 | 同左 |
| 用户明确限制 | 遵守（结构化 limits） | 遵守（优先于模型结论） | 遵守（解析模型理解，不新增授权审查） |
- 模式影响尚未开始的操作；不把未来步骤提前当已开始。
- 应用自身运行（保存记忆/设置/日志/审批落库）通过可信内部操作类型与调用路径区分，
  不允许任意工具自报标记豁免。

## 9. 审批生命周期（保留并修复）
- 10 分钟原始期限；持久化；重启恢复不续期；批准后执行前重核对；重复点击/回调/恢复重试不重复执行；
  三次确认按序且绑定相同对象/动作/内容；普通批量确认/模型允许记录/模式切换不能替代三次确认；
  等待不阻塞无依赖工作；过期/拒绝/撤回不自动启动；界面只说明对象/操作/原因/影响/状态。

## 10. 兼容
- 历史记录缺 requirement 绑定字段：不得默认补成宽泛有效授权；无法可靠恢复的操作重新判断；
  不静默清空用户记录/重置模式/扩大跨重启授权。旧 keyword 相关数据/字段可保留只读，但不得再参与判定。

## 11. 分支、文件归属与验证纪律

| 分支/worktree | 负责 | 独占文件 |
| --- | --- | --- |
| `r9g/main` qio-r9g-main | Lead：migration 57/58、engine/gateway/context/app、fixed query/永久删除/批量/recheck 接线核心、集成、文档 | storage/migration_r9g_*、schema.py、migrate.py、security/engine.py、gateway.py、context.py、effects.py、security/recheck.py、fixed_queries/deletion 的 engine 接线、app.py、status/docs |
| `r9g/w1-review` qio-r9g-w1 | W1：删除关键词守卫；模型审查语义化；要求解析模型；requirements 版本/绑定；prompts；语义测试/真实模型评估 | security/review.py、security/resolution.py、security/requirements.py、prompts.py、test_r9g_semantics*、eval 脚本 |
| `r9g/w2-exec` qio-r9g-w2 | W2：固定查询分类器；永久删除结构化解析；cmd_tools/approved proc 绑定与重核对；相关测试/真机证据 | security/fixed_queries.py、security/deletion.py、tools/cmd_tools.py、approved_operations.py 的 proc/delete 部分、tests |
| `r9g/w3-origin` qio-r9g-w3 | W3：fs/附件批量逐项；file tools 要求绑定与重核对；审批恢复/批次状态；相关测试 | tools/fs_tools.py、tools/attachment_tools.py、services/attachments.py、approved_operations.py 的 fs/附件部分、tests |
| `r9g/w4-verify` qio-r9g-w4 | W4：验收测试与审计矩阵、入口审计、定向/全量测试与 CI 调度协助、前端审批文案检查 | tests/test_r9g_acceptance_e2e.py、scripts/audit_r9g_*.py、docs/r9g-reports/* |

- 只提交自己的分支；不 push、不 merge（Lead 统一推送/集成）。
- 定向验证优先：`cd backend; uv run --frozen pytest -q <file/nodeid>`；**全量测试/全量 CI 由 Lead 统一最多各两次**，
  子智能体不得自行触发全量。push 提交带 `[skip ci]`，定向 CI 用 `targeted-verify.yml` 手动派发。
- 不得删除/跳过/放宽既有测试取得通过；测试必须验证实际接线（真实引擎/注册表/工具；模型调用用 FakeAdapter）。
