# R9-H11 共同对象判定契约（冻结）

> 基线 r9h10/main 80b0630874b6749df5ddf90bafc12e2d31f5eced（代码 0a0df3a1f84eec1f0182885efa2af563382a07f4）。
> 本轮分支 r9h11/main + 独立 worktree：qio-r9h11-main / -a / -b / -c / -d。
> 共享文件 engine.py 只由 Lead 修改；各成员只写自己 scope 内文件。

## 0. 环境（所有成员）

* 后端测试：`cd <你的 worktree>\backend; & .\.venv\Scripts\python.exe -m pytest tests/<file>.py -q -p no:cacheprovider`
  （worktree 的 backend\.venv 是指向共享环境的 junction，已可用；不要改它）
* 不联网、不新增依赖、不跑全量 pytest、不跑 npm/cargo、不触发 CI。模型一律 fake/mock（参照 tests/_r9_auth_support.py）。
* 禁止用名称字符串/basename/后缀猜测对象相关性；禁止把"无法确认"当成 false 放行。
* 不允许删除既有检查、降低阈值、放宽产品断言；只允许按新语义修正旧断言并在报告里说明。

## 1. 目标（三类真实流程问题）

1. 历史无绑定记录（state=structured 但 program_name 无 program_binding）仍可通过绝对路径执行或文件读取绕过。
2. 原路径被替换后，旧对象硬链接失去读取保护（文件操作把 binding.path 交给路径比较，重新取该路径当前对象身份）。
3. 编号复用/生命周期变化时新程序被放行（匹配看生命周期返回 false，绑定失效检查只比编号）。

共同根因：生产代码用布尔返回值衔接对象判定，"绑定缺失/绑定失效/事实不足"都退化为 false（=无关），再被模式规则放行。

## 2. 冻结接口：`agent/security/object_state.py`（A 实现，Lead 消费）

```python
from enum import Enum
from dataclasses import dataclass
from typing import Any, Callable, Mapping

class ObjectState(str, Enum):
    MATCH = "match"          # 有效匹配
    UNRELATED = "unrelated"  # 有可靠依据证明无关（唯一允许按模式继续的状态）
    UNBOUND = "unbound"      # 绑定缺失：明确禁止但保存时未形成对象绑定，需专用恢复/澄清
    INVALID = "invalid"      # 绑定失效：原路径对象被替换，或编号相同但生命周期不同
    UNKNOWN = "unknown"      # 事实不足，无法可靠判断

@dataclass(frozen=True)
class ObjectFact:
    field: str                      # "path" | "identity_key" | "birth_key" | "entry_name" | "operation" | "resolution" | ...
    expected: str | None = None
    observed: str | None = None
    source: str = ""                # "program_binding" | "file_identity" | "effect" | "path_lookup" | "path_compare"
    equal: bool | None = None       # True/False/None(=不可比较)
    def as_dict(self) -> dict: ...

@dataclass(frozen=True)
class IdentityComparison:
    state: str                      # same / different_object / different_lifecycle / lifecycle_unverifiable / identity_unavailable
    facts: tuple[ObjectFact, ...] = ()

class ProgramResolution:            # 只读澄清结果，不持久化、不补建绑定
    name: str
    path: str | None
    identity_key: str | None
    birth_key: str | None
    source: str                    # 固定 "path_lookup"
    error: str = ""

@dataclass(frozen=True)
class ObjectDecision:
    state: ObjectState
    basis: str                      # 结构化依据码（英文稳定标识，如 "binding_identity+birth_same"）
    operation: str | None = None    # read/write/delete/execute/...
    object_kind: str | None = None  # path/program/program_name/service
    object_value: str | None = None
    facts: tuple[ObjectFact, ...] = ()
    detail: str = ""                # 仅说明文字，不参与判定
    @property
    def matched(self) -> bool: ...        # state is MATCH
    @property
    def unrelated(self) -> bool: ...      # state is UNRELATED
    @property
    def needs_recovery(self) -> bool: ... # state in {UNBOUND, INVALID}
    @property
    def fail_closed(self) -> bool: ...    # state is UNKNOWN
    def as_dict(self) -> dict: ...        # 审计序列化（facts/limits 全部为有界字符串）

@dataclass(frozen=True)
class LimitAssessment:
    decisions: tuple[ObjectDecision, ...] = ()
    reason: str = ""                # 中文展示原因（只用于说明）
    decision_code: str | None = None
    @property
    def conflict(self) -> bool: ... # 是否有任何命中/未确定/失效

# --- 纯函数 ---
def resolve_path(value: object, base: Any) -> str | None:
    """规范化绝对路径（expanduser+resolve）；不可解析返回 None。base=None 时只接受绝对路径。"""

def same_path_text(left: object, right: object) -> bool:
    """分隔符/大小写/尾斜杠规范化后的文本相等（不可解析时的唯一可靠依据）。"""

def resolve_program_name(name: str) -> ProgramResolution:
    """当前 PATH 只读查找（澄清用）：不写库、不补建绑定、不改变历史 JSON。"""

def compare_bound_identity(
    bound_identity: str | None,
    bound_birth: str | None,
    *,
    candidate_path: str | None = None,
    observed_identity: str | None = None,
    observed_birth: str | None = None,
    cache: dict | None = None,
) -> IdentityComparison:
    """共同身份契约（匹配与失效检查共用，禁止各分支自己比字段）：
    identity 不可读 -> identity_unavailable；
    identity 不同 -> different_object；
    identity 相同且两侧 birth 都在且不同 -> different_lifecycle；
    identity 相同、bound birth 存在但 observed birth 取不到 -> lifecycle_unverifiable；
    否则 same（bound birth 缺失时按“保存时无生命周期依据”的弱依据匹配，fact 标明）。
    禁止用内容摘要/mtime/名称相似/调用方自报代替 lifecycle。"""

def decide_object(
    item: Mapping[str, Any],              # 限制对象条目 {kind, value, program_binding?}
    candidate: Mapping[str, Any],         # 本次真实影响对象 {kind, value}（kind: path|program|service|program_name）
    *,
    operation: str | None = None,         # 本次操作 read/write/create/delete/execute/install/system_change/egress|None
    effect: Any = None,                   # 真实 Effect（取 requested_program / detail 事实）
    workspace: Any = None,
    effect_base: Any = None,
    resolver: Callable[[str], ProgramResolution] = resolve_program_name,
    cache: dict | None = None,
) -> ObjectDecision:
    """唯一对象判定入口。语义表见 §3。绝不返回布尔。"""

def decision_code_for(state: ObjectState, object_kind: str | None) -> str | None:
    """UNBOUND + program_name -> DECISION_PROGRAM_NAME_UNRESOLVED；
    INVALID -> "program_binding_invalidated"；UNKNOWN -> "object_state_unconfirmed"；其余 None。"""
```

## 3. 判定语义表（实现必须逐条覆盖，A 的单元测试逐行对应）

记号：limit=限制对象条目；cand=本次影响对象；binding=limit.get("program_binding")。

### 3.1 操作范围（先过滤）
* limit.operations 非空且 operation 已知且不在集合 -> **UNRELATED** basis="operation_out_of_scope"。
* operation 为 None（无法确定动作）-> 不据此判无关，继续做对象核对（保守）。

### 3.2 path / path_prefix / directory
* 解析失败且文本规范相同 -> MATCH basis="path_text_same"；解析失败其他 -> UNKNOWN basis="path_unresolvable"。
* path_prefix/directory：cand 在 limit 内（相等或 relative_to）-> MATCH basis="path_prefix_contains"；否则 UNRELATED basis="path_prefix_outside"（可靠）。
* path（精确文件）：cand 与 limit 路径相等 -> MATCH basis="path_same"；
  路径不同：比较两者当前文件身份（identity+birth，比较规则同 §3.4）：
  same -> MATCH basis="path_hardlink_same_object"；
  different_object -> UNRELATED basis="path_different_object"；
  different_lifecycle -> MATCH？-> **UNRELATED**（不同生命周期=另一个对象）但 fact 标 lifecycle；
  identity_unavailable/lifecycle_unverifiable -> UNKNOWN basis="path_identity_unverifiable"。

### 3.3 program（显式程序路径）
* cand.kind == "program" 或 path 型：按 §3.2 path 规则（程序文件就是文件对象）。
* cand.kind == "program_name"：用 resolver 解析入口名：
  解析对象与 limit 路径身份 same -> MATCH basis="entry_resolves_to_limit_path"；
  解析到不同对象 -> UNRELATED；解析不到 -> UNKNOWN（不能按名称猜）。

### 3.4 program_name + binding 存在（有效/失效）
候选为程序对象或路径（文件读/写/删/执行目标）：
* `compare_bound_identity(binding.identity, binding.birth_key, candidate_path=..., observed_*=...)`：
  * same -> **MATCH** basis="binding_identity+birth_same"（binding.birth 缺失时 basis="binding_identity_only"，fact 标明 weak_lifecycle）
  * different_lifecycle -> **INVALID** basis="lifecycle_changed_same_identity"（问题三：编号复用=另一个生命周期对象）
  * different_object：
      * cand 路径等于 binding.path（realpath/normcase 比较）-> **INVALID** basis="bound_path_object_replaced"（问题二：原路径新对象）
      * 否则 -> **UNRELATED** basis="different_object_identity"（旧对象别名会因身份相同命中 MATCH；新对象其它别名不继承禁止）
  * lifecycle_unverifiable -> **UNKNOWN** basis="lifecycle_unverifiable"（保存有生命周期、当前取不到：不能默认一致）
  * identity_unavailable -> **UNKNOWN** basis="identity_unavailable"

候选为**入口名**（effect.requested_program / detail.execution.program 是不含分隔符的名称）：
* 名称与 limit.value 平台规范化后相等 -> binding 存在时：若 cand 解析对象与 binding same -> MATCH basis="entry_name_same_object_same"；否则 INVALID basis="entry_name_same_object_differs"（同一来源名，对象已变，需恢复/澄清）。
* 名称不同（精确规范化比较，不做 basename/去后缀/相似判断）：
  * cand 解析对象与 binding same -> MATCH basis="entry_resolves_to_bound_object"（别名入口仍保护）
  * 解析到不同对象或解析不到 -> **UNRELATED** basis="different_entry_name"（历史 H7/H8 已接受语义：不同入口名不互相阻塞）

### 3.5 program_name + binding 缺失（UNBOUND，问题一）
候选为程序对象或路径：
* resolver(limit.value)：
  * 解析到对象且与 cand 身份 same -> **UNBOUND** basis="binding_missing_same_object"（相关，需专用恢复/澄清；普通批准不能解除）
  * 解析到不同对象 -> **UNRELATED** basis="binding_missing_resolved_elsewhere"（可靠：名称当前对象不是候选）
  * 解析不到 -> **UNKNOWN** basis="binding_missing_lookup_unresolved"（事实不足，不得静默放行）
  * 解析到对象但 cand 身份不可读 -> **UNKNOWN**
候选为入口名：
* 入口名与 limit.value 相等 -> **UNBOUND** basis="binding_missing_entry_same"
* 入口名不同 -> **UNRELATED** basis="different_entry_name"

### 3.6 service
* 现有 `_limit_service_matches` 语义：相等/首段相等 -> MATCH；否则 UNRELATED。

## 4. 聚合（engine._limit_conflict 消费，Lead 实现）
对每条 limit × 每个 impact（impact.operation 先过滤）：
1. limit.objects 为空 -> 该条限制覆盖全部对象：命中（MATCH，basis="objects_empty_all"）。
2. impact.objects 为空且 limit.objects 非空 -> **UNKNOWN**（影响对象无法枚举，不能当无关）。
3. 否则逐对象 decide_object。聚合优先级：MATCH > INVALID/UNBOUND > UNKNOWN > UNRELATED。
   * 任一 MATCH -> 拒绝，reason 用 limit.reason；decision_code=None。
   * 否则任一 INVALID/UNBOUND -> 拒绝 + 对应 decision_code（普通批准不能解除）。
   * 否则任一 UNKNOWN -> 拒绝 + decision_code="object_state_unconfirmed"。
   * 全部 UNRELATED -> 该条限制不命中。
4. 多 limit 条目只要一条命中即冲突；所有被评估的 ObjectDecision 进入审计（AuthDecision.object_decisions 追加字段）。
5. 不得因"限制对象无法确认"而禁止与限制无关的操作：operation 过滤与 §3 的可靠无关依据必须先执行。

## 5. 引擎/工具/恢复接线（Lead+C+B）
* `AuthorizationEngine.assess_limit_conflict(effect, requirement, *, extra_limits=()) -> LimitAssessment`（冻结）：
  `_limit_conflict` 改为其薄包装（返回 reason 字符串），所有真实入口（authorize / recheck_approved / fs 读取边界 / 审批恢复）必须消费同一 assessment。
* 删除/收敛 `_program_name_unresolved`、`_program_binding_path_replaced` 的独立布尔判断（它们的语义并入 decide_object），保留方法名时也只能转调共同入口。
* AuthDecision 追加 `object_decisions: tuple[dict, ...] = ()`（追加字段，默认空）；DENY 时携带。
* 读取（正文/摘要）在读取前用同一 assessment 核对；禁止先读后拒。
* 专用恢复只理解和绑定限制：不生成普通授权、不调用普通独立授权审查；unrestricted 也不因此进入普通审查。
* 恢复写回前核对 requirement id/version/status/chain_digest；失败保留原限制/known/来源，不清空为无限制；历史原始 JSON 在未恢复时保持不变。

## 6. 文件所有权（严格）
* Lead：engine.py、effects.py、docs/*（r9h11 报告/status）、最终集成与 CI。
* A：security/object_state.py（新增）、tests/test_r9h11_object_state.py。
* B：security/requirement_limits.py、security/requirements.py、services/app.py、tests/test_r9h11_binding_recovery.py。
* C：tools/fs_tools.py、tools/cmd_tools.py、tools/security_gate.py、security/operations.py、tools/approved_operations.py、security/recheck.py、tests/test_r9h11_cross_operation.py。
* D：tests/test_r9h11_regressions.py（独立验收，先在基线跑红）、_r9h11-evidence/。
共享文件只有 Lead 能改；需要别人文件时发消息给 Lead。

## 7. 必须保留的既有语义（防局部修复）
整体限制空对象/空操作含义；三模式已知限制优先于固定查询/历史授权/模型允许/人工批准；程序禁止不通过普通审批解除；目录完整性独立前提；批量逐项；内部维护确认前读取边界；补丁同源快照与读取顺序；未变化脚本批准后成功执行、变化脚本不执行；保存/读取/继承/部分解除/恢复/执行完整保留绑定字段。

## 8. 交付与证据
* 每个分支：小步提交（中文 + 范围前缀），报告：分支/commit SHA、文件清单、定向测试命令与结果、未做完/已知限制、契约冲突点。
* 测试必须覆盖"应停止/应继续"相邻案例，并检查拒绝前是否已读取正文/启动程序（读取次数 spy / marker 文件不存在）。
* 真实硬链接/真实临时程序/真实 SQLite/真实审批；平台不支持真实链接时 skip 并如实报告；模拟分支（如注入生命周期事实）必须与真实验证分开标注。

## 3.7 补充判定细则（Lead 冻结补充，2026-10-25）
* **候选对象是否存在**（沙箱/测试常用不存在路径）：
  * 候选路径不存在且候选路径 != 绑定/限制路径 -> **UNRELATED**（不存在的对象不可能是已存在的绑定对象；不能因此阻塞无关操作）。
  * 候选路径 == 绑定路径但对象不存在/身份不同 -> **INVALID**（原路径对象被删除/替换=绑定失效）。
  * 候选路径存在但身份不可读（权限/平台不支持）-> **UNKNOWN**（失败关闭）。
* **候选 kind=="program" 的 value 可能是裸入口名**（cmd effect 的 detail.execution.program 会被拍平成 program 对象）：不含 "/" 与 "\\" 时按入口名处理（走 §3.4/§3.5 的入口名分支）。
* **入口名比较**：只做平台规范化后的完全相等（normcase+strip）；名称不同即不同来源，不做 basename/去后缀/相似判断。
* **path 型限制**（无保存身份）：cand != limit 路径时，若 limit 路径当前不可读身份 -> UNRELATED（如实记录 fact；路径型限制没有保存身份，不做别名扫描）；cand 身份不可读且存在 -> UNKNOWN。
* **program_name 绑定 + 入口名相同**：即使 cand 目标身份不可读，也按同一来源名处理 -> INVALID（不是 UNKNOWN），reason 必须含"绑定"。
* reason 文案要求：UNBOUND/UNKNOWN 且对象为 program_name 时必须含"程序名称"；INVALID 必须含"绑定"。