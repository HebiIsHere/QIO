# R9-F 修复契约（冻结）

> 状态：**冻结**（2026-10-22）。任何跨分支接口变更必须先由 Lead 更新本文件并通知各分支。
> 基线：分支 `r9/main`，SHA `37a2d26b94df113b45fbc8d0702801c2d1a06cf3`
> （代码/验收父提交 `b2d9229fad014739b7dc1826c89a9195c4539d32`）。
> 修复分支：`r9f/main`（worktree `D:\qio-dev\qio-r9f-main`）。
> 目标：修复审查发现的 A/B/C/D 四个问题；受限模式自然语言授权改为**大语言模型审查**；
> 保留 R9 已完成的源文件引用、审批持久化、文件卡片、系统回收站、托盘退出。

## 1. 不可破坏的既有约定

- 三模式：`read_only` / `restricted` / `unrestricted`；旧值映射不变；只有用户可提高模式。
- 内部运行数据直接维护：所有模式都要求**后端 CAS 校验的连续三次确认**；
  应用受控接口（设置/记忆/审批落库）不需要三次确认。
- 审批 v2：600 秒原始期限、持久化、原子领取、执行前重核对、崩溃不重放；
  已批准只表示"不重复询问"，**不表示后续效果/范围/对象一律允许**。
- 用户文件删除一律进系统回收站；不得新增永久删除能力。
- 附件 = 源文件引用；截图/上传/浏览器落盘首次登记为 `user_saved_material`（用户提供材料）。
- 命令/脚本执行不是安全沙箱；Low IL 只阻止写 Medium 完整性对象，不阻止读/网络；
  任何结论必须如实标注能力边界。
- 日志/事件/错误/测试输出不得出现密钥原文；新增输出走 `agent/trace/redact.py`。
- 只追加迁移，禁止改历史迁移。

## 2. 模式决策流水线（冻结语义）

```
authorize(effect, ctx):
  1) 固定规则（所有模式）：
     - 只读模式固定规则（读允许、QIO 受控数据允许、写/删/执行拒绝、网络按既有规则审批）
     - 内部数据三次确认（APPROVE，后端校验）
     - 凭据读取红线、子智能体子集
  2) read_only：不调用任何模型审查；按固定规则结束（绝不由一次普通审批绕过只读）
  3) unrestricted：跳过普通授权模型审查与常规审批；仍执行用户明确禁止/目标限制（记录在 requirement.limits）
     内部数据三次确认、删除进回收站等固定规则不变
  4) restricted：
     a. 固定可直接判定的：读取、应用受控数据、QIO 生成文件、专用环境装依赖、配置云端模型、
        已批准范围（AuthorizationRecord 命中）→ 直接 ALLOW/DENY
     b. 需要自然语言理解的操作（写用户文件/删用户文件/执行程序/外发/系统变更）→ 触发
        **模型授权审查**（见 §4）；审查结果经后端验证后：
          ALLOW → 形成有界 AuthorizationRecord（缓存复用），返回 ALLOW
          CONFIRM → 返回 APPROVE（普通用户审批；审批单列明对象/操作/依据）
          DENY → 返回 DENY（说明与用户要求冲突；不转成审批）
     c. 模型不可用/超时/输出非法/对象无法核实 → 不默认允许；返回 APPROVE（提示"暂时无法确认授权范围，
        可对明确的具体操作手动审批"）；不自动切模式
```

- 关键词动作词**不得**再作为自然语言许可依据。路径/对象标识的机械解析可以保留，用于
  构造审查请求与对象解析，但不得直接产生 grant。
- 已有 `intent.extract_grants` 的授权产生能力删除/降级为"仅提示候选对象"，不得被任何
  authorize 路径当作许可。

## 3. 冻结接口与文件归属

### 3.1 `agent/security/requirements.py`（W1）
```python
@dataclass(frozen=True)
class UserRequirement:
    requirement_id: str
    text: str                      # 用户原话（不得由模型改写）
    provenance: dict               # {"kind":"user_message","message_id":...,"turn_id":...,"topic_id":...}
    parent_id: str | None
    status: str                    # active | superseded | withdrawn
    limits: tuple[str, ...]        # 用户明确写出的限制/后续修正（只来自用户原话或用户确认）
    created_at: str
    updated_at: str

class RequirementLedger:           # 由 AppContext 注入 db 连接
    def record_user_message(self, *, text, message_id, turn_id, topic_id) -> UserRequirement
    def current(self) -> UserRequirement | None
    def by_id(self, requirement_id) -> UserRequirement | None
    def add_correction(self, requirement_id, text, *, message_id) -> UserRequirement
    def withdraw(self, requirement_id) -> None
    def supersede(self, requirement_id) -> None

@dataclass(frozen=True)
class AuthorizationRecord:
    record_id: str; requirement_id: str; operation_kind: str; fingerprint: str
    decision: str                  # allow
    reason: str; allowed_operations: tuple[str, ...]; objects: tuple[dict, ...]
    limits: tuple[str, ...]; source: str; mode: str; created_at: str
    expires_at: str | None; revoked_at: str | None; review_id: str | None

class AuthorizationLedger:         # 持久化到用户库；进程内缓存可加，但重启后必须能重建
    def reuse(self, *, requirement_id, operation_kind, fingerprint) -> AuthorizationRecord | None
    def record(self, record: AuthorizationRecord) -> None
    def revoke_for_requirement(self, requirement_id) -> int
    def revoke(self, record_id) -> None
```

### 3.2 `agent/security/review.py`（W1）
```python
class ReviewDecision(str, Enum): ALLOW="allow"; CONFIRM="confirm"; DENY="deny"

@dataclass(frozen=True) class ReviewObject:
    kind: str            # path | directory | program | service | material
    value: str
    identity_key: str | None = None
    file_origin: str | None = None

@dataclass(frozen=True) class ReviewOperation:
    kind: str            # write | create | delete | execute | shell | install | egress | system_change
    objects: tuple[ReviewObject, ...]
    program: str | None = None
    argv: tuple[str, ...] = ()
    cwd: str | None = None
    script_sha256: str | None = None
    network_hosts: tuple[str, ...] = ()
    summary: str = ""

@dataclass(frozen=True) class ReviewRequest:
    requirement: UserRequirement
    corrections: tuple[str, ...]
    limits: tuple[str, ...]
    mode: str
    operation: ReviewOperation
    related_grants: tuple[dict, ...]
    prior_decisions: tuple[dict, ...]     # 本 requirement 相关的审查/审批记录（含拒绝）
    approvals: tuple[dict, ...]
    user_constraints: tuple[str, ...]

@dataclass(frozen=True) class ReviewOutcome:
    decision: ReviewDecision
    reason: str                         # 简短依据（≤500 字），不持久化推理过程
    requirement_id: str                 # 必须引用真实存在的用户要求
    allowed_operations: tuple[str, ...]
    objects: tuple[ReviewObject, ...]
    limits: tuple[str, ...]
    purpose: str

class ReviewUnavailable(Exception): ...

class AuthorizationReviewer(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewOutcome: ...

class ModelAuthorizationReviewer:
    def __init__(self, *, adapter_factory, model_call=None, timeout_seconds=20)
    async def review(self, request) -> ReviewOutcome  # 单独模型调用；不允许工具、文件、设置能力

def validate_outcome(outcome, request) -> ReviewOutcome
# 校验：结构合法；requirement_id 存在且为当前/父要求；objects/ops ⊆ 请求集合；
# 未越过模式/limits/固定规则；ALLOW 必须覆盖全部目标且带 limits/purpose；
# 不得把一次操作扩展成长期/广度许可；reason 非空且引用真实用户要求。
```

模型调用规范（冻结）：
- 复用用户已配置主模型：`ctx.build_adapter()`（或注入的 `adapter_factory`），一次普通
  `adapter.complete(messages, tools=[])`；不新建 Agent、不暴露工具、不写库/文件/设置。
- 提示词只放在 `agent/prompts.py` 新增常量（`AUTHORIZATION_REVIEW_SYSTEM` 等），
  单一来源；输出 JSON 契约见 §4.3；不得在别处散落第二套提示词。
- 输入必须由后端组装：用户原话+来源 id、上下文修正/限制、具体操作、后端核实的对象
  （路径/身份/来源/接收服务）、相关既有授权/批准/撤回；文件/网页/工具结果内容作为
  **不可信材料**单独标注（`untrusted_material`），不得混入用户要求。
- 输出结构非法/超时/网络错误 → `ReviewUnavailable`（不默认允许）。

### 3.3 `agent/security/launch.py`（W2，统一受控启动入口）
```python
@dataclass(frozen=True)
class LaunchSpec:
    kind: str                     # program | shell | script
    program: str | None           # program 绝对路径/名称（解析后）
    argv: tuple[str, ...]
    cwd: str | None
    shell_cmd: str | None          # kind=shell 时
    script_source: str | None      # kind=script（自建工具/临时脚本）时
    script_name: str | None
    timeout: float

@dataclass(frozen=True)
class LaunchPermit:
    permit_id: str
    spec_digest: str               # 规范化 spec 的 sha256（程序身份/argv/cwd/脚本内容）
    authorized: bool
    mode: str
    allowed_roots: tuple[str, ...]          # 允许写的文件/目录（不是 cwd 等价物）
    allowed_operations: tuple[str, ...]     # write/create/delete/execute/...
    network_hosts: tuple[str, ...] | None
    internal_data_confirmed: bool
    requirement_id: str | None
    approval_id: str | None
    source: str                    # instruction | approval | unrestricted
    reason: str

@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    returncode: int | None
    stdout: str; stderr: str
    category: str | None           # permission|boundary_violation|timeout|startup|code_error|...
    error: str | None
    guard: dict | None             # 事后核验（补充审计）
    isolation: dict | None
    spec_digest: str

class ControlledLauncher:
    async def launch(self, spec: LaunchSpec, permit: LaunchPermit) -> LaunchResult
    # 强制：spec_digest 必须与 permit 一致（执行前重算）；程序路径/脚本内容执行前后
    # 不得变化；经 isolation.harden（Low IL 按模式/许可）+ Job Object；执行前后
    # ExecutionGuard 快照（保护范围包含用户业务文件与内部数据；排除 QIO 自身日志/
    # SQLite 正常写路径要**有证据**，不能整类排除）；后代进程同边界；输出上限。
```

接入范围（必须全部走 `ControlledLauncher`，或使用其等价受控启动）：
- `tools/cmd_tools.py::RunProgramTool/RunCmdTool`（含批准后重放 `approved_operations.py`）；
- 自建工具执行（`SandboxExecutor` 的 worker 启动路径复用同一隔离/guard 组件）；
- 依赖安装/测试命令（`tools/dev_tools.py` / `tool_env` 安装器）——至少程序身份+argv+
  cwd 校验与隔离/guard 复用；
- `_ApprovedReplayGateway.authorize()` 不再无条件 ALLOW：只返回"不重复询问"的
  有界 ALLOW，且 permit 必须来自已批准操作的 payload 重核对（对象/程序/脚本内容一致）。

**不得**以命令名黑名单（rm/del 等）代表删除边界；不得以 cwd 代表文件访问范围；
不得以 Low IL 代表完整文件/网络边界；不得以事后快照代替事前约束。

### 3.4 `agent/services/file_origins.py`（W3）
```python
ORIGIN_USER_ORIGINAL = "user_original"
ORIGIN_USER_SAVED_MATERIAL = "user_saved_material"
ORIGIN_QIO_GENERATED = "qio_generated"

@dataclass(frozen=True)
class FileOriginRecord:
    path: str; volume: str; file_id: str; origin: str; source: str
    attachment_id: str | None; created_at: str; updated_at: str; missing_at: str | None

class FileOriginStore:
    def __init__(self, conn, workspace_root: Path)
    def record_created(self, path: Path, *, origin: str, source: str,
                       attachment_id: str | None = None) -> FileOriginRecord | None
        # 仅在"本次受控创建"后调用；创建前已存在的路径不得冒充 generated
    def lookup(self, path: Path) -> FileOriginRecord | None   # 身份优先，其次同路径
    def is_generated(self, path: Path) -> bool
    def update_after_replace(self, old: FileOriginRecord, new_path: Path,
                             *, require_identity_match: bool = True) -> FileOriginRecord | None
    def mark_missing(self, path: Path) -> None
```

归属必须由**实际创建/接收文件的受控服务**记录，并关联文件身份（复用
`services/file_identity.py`）。`fs_write/fs_patch/fs_delete`、附件写回、原子替换
统一查询来源并把**后端核实过的** `file_origin` 传给授权引擎（引擎/Gateway 从
`FileOriginStore` 校验，不接受模型传入的 origin 字段）。

### 3.5 引擎/Gateway 扩展点（Lead 实现，W1/W3 只调用）
- 新增 `ApprovalKind.REVIEW`（或等价内部信号）由 `_restricted` 返回，Gateway 调
  `AuthorizationReviewer` 后再定最终 verdict；**engine.py 不直接持有 async 模型调用**。
- 新增 Gateway 可注入 `origin_provider: Callable[[Path], str | None]` 与
  `reviewer: AuthorizationReviewer | None`；restricted 才启用 reviewer；read_only/
  unrestricted 不调用。
- `_grant_scope_id` 的 execute 匹配改为"有界授权记录/模型审查记录"驱动：
  目录授权本身**不**放行任意程序；记录必须包含具体 program 身份/argv/cwd；
  同一 requirement+operation fingerprint 可复用，新程序/新参数/新对象重新审查。
- 授权生命周期：requirement active 期间有效；requirement 被修正/撤回/替换、
  对象身份变化、模式变化、审批过期/拒绝后失效；执行前重新核对。

## 4. 数据迁移（Lead 实现，迁移 55 + 补偿 56）

```sql
CREATE TABLE user_requirements(
  id TEXT PRIMARY KEY, text TEXT NOT NULL, provenance TEXT NOT NULL,
  parent_id TEXT, status TEXT NOT NULL, limits TEXT NOT NULL DEFAULT '[]',
  turn_id TEXT, topic_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE authorization_records(
  id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL, operation_kind TEXT NOT NULL,
  fingerprint TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
  allowed_operations TEXT NOT NULL, objects TEXT NOT NULL, limits TEXT NOT NULL,
  source TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL,
  expires_at TEXT, revoked_at TEXT, review_id TEXT
);
CREATE TABLE authorization_review_audit(
  id TEXT PRIMARY KEY, requirement_id TEXT, request_digest TEXT NOT NULL,
  model TEXT, prompt_version TEXT, outcome TEXT NOT NULL, decision TEXT,
  reason TEXT, latency_ms INTEGER, error TEXT, created_at TEXT NOT NULL
);
CREATE TABLE file_origins(
  volume TEXT NOT NULL, file_id TEXT NOT NULL, path TEXT NOT NULL,
  origin TEXT NOT NULL, source TEXT NOT NULL, attachment_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, missing_at TEXT,
  PRIMARY KEY(volume, file_id)
);
```
（索引：requirements(status), records(requirement_id,fingerprint), audit(requirement_id),
file_origins(path)。）

## 5. 分支、类与验证纪律

| 分支/worktree | 负责 | 独占文件 |
| --- | --- | --- |
| `r9f/main` / qio-r9f-main | Lead 集成、schema、engine/gateway/app、D 修复、文档 | migration 55/56、schema.py、migrate.py、engine.py、gateway.py、effects.py、app.py、prompts.py 的审查常量最终合并、docs |
| `r9f/w1-review` / qio-r9f-w1 | 模型审查、需求台账、授权记录生命周期、真实模型评估 | security/review.py、security/requirements.py、eval 脚本、test_r9f_review_* |
| `r9f/w2-exec` / qio-r9f-w2 | 统一执行边界、审批后执行、cmd/shell/自建工具/依赖安装接入 | security/launch.py、cmd_tools.py、approved_operations.py、sandbox.py 受控部分、verify_r9f_exec_* |
| `r9f/w3-origin` / qio-r9f-w3 | 文件来源持久化与工作区自主操作 | services/file_origins.py、fs_tools.py、services/attachments.py 写回/替换部分、test_r9f_origin_* |
| `r9f/w4-verify` / qio-r9f-w4 | 真实链路 E2E、CI 响应性诊断、定向验证运行 | tests/test_r9f_acceptance_e2e.py、scripts/diagnose_loop_stall.py、docs/r9f-reports/* |

- 各分支只提交自己的分支，不整合、不 push、不改 main。
- 定向验证优先：`cd backend; uv run --frozen pytest -q <文件/nodeid>`；
  CI 用 `gh workflow run targeted-verify.yml -f ref=r9f/<branch> -f sha=<40位> -f targets=...`
  （push 时提交信息带 `[skip ci]`，避免触发完整 ci.yml；禁止删除/削弱既有检查）。
- 合并材料：`docs/r9f-merge-notes.md`、`docs/r9f-acceptance-matrix.md`、`docs/r9f-reports/W*.md`。
- 真实链路结论必须使用真实 AuthorizationEngine + 真实注册表 + 实际工具调用；
  假网关只可用于局部接口测试；系统边界结论需 Windows 真机证据。

## 6. 四问题对应验收（简表）

| 问题 | 修复点 | 必测 |
| --- | --- | --- |
| A 程序/shell 无统一约束 | `ControlledLauncher` + permit + 重放网关收紧 | 批准后执行同一边界；解释器内部写入/删除/外发/后代不能绕过；程序/脚本变化不沿用旧许可；范围内脚本可用 |
| B 关键词否定误授权 | 关键词授权能力删除；模型审查 + 后端验证 + 缓存/生命周期 | 否定/引用/条件/多对象不误授权；对象解析；不可信材料不增权；模型不可用不默认放行；正常请求不反复审批 |
| C 工作区来源不可达 | 持久化 file_origins + 各路径统一查询 | 自然语言"整理报告"创建→修改→回收自主；用户材料不被当生成物；替换/重启/原子写回后归属正确 |
| D 项目执行授权不匹配 | 审查记录绑定 program/argv/cwd；目录不直接放行任意程序 | 真实用户要求→真实测试命令不反复审批；新程序重新判断；不放行目录写/系统变更/外发 |

## 7. 本轮 CI 响应性问题的诊断要求（W4）

- 对 `tests/test_interactive_during_heavy_work.py` 的计时失败：
  用测量证据区分**事件循环真实阻塞 / 线程工作 / 首次初始化 / runner 调度延迟**；
  在 HEAD 与基线 `249a6344`（或 `37a2d26b` 前一可运行基线）同机对照运行，
  记录 gap 分布、采样栈、CPU/负载条件。
- 若实现有问题则修实现；若测量方式有问题则修测量方式并保留对真实阻塞的检测能力；
  不得只归因负载，也不得直接放宽阈值。
- 旧 CI 状态：run `39771507126` 失败重跑仍红（349ms/300ms）；run `39784106563`
  完整重跑（非仅 docs），Windows 后端红：`test_a_slow_retrieval_does_not_stall_the_turn_loop`
  981ms/300ms。开工已刷新：两个 run 均 completed=failure，其余 9 job 全绿。
