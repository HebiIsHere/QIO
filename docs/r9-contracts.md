# R9 统一授权 · 源文件引用 · 桌面退出 —— 冻结契约（2026-10-22）

> 状态：**已冻结**。所有并行分支以本文件为共享接口与验收口径的唯一事实源。
> 基线：`origin/main` = v0.2.10 = `249a63443c72b3dad011b5446ee9920f4dadf0fd`（开工时已重新 fetch 确认，无更新）。
> 主开发分支：`r9/main`（worktree：`D:\qio-dev\qio-r9-main`）。子分支从本契约提交拉出。
> 契约变更必须由 Lead 书面追加"变更记录"并通知全部工作流；不允许各分支自成一套权限体系。

## 0. 不可违背的产品事实（先读）

1. QIO **没有**统摄所有用户工作的"任务/会话生命周期"，turn/topic/session_id/工具开发任务/子智能体任务都不是授权有效期。
2. 授权语义 = **哪条用户指令 + 哪些对象 + 什么操作 + 什么目的 + 什么边界/例外**。内部 id 只用于追踪校验。
3. 添加附件/创建文件卡 = 提供材料，**不授权修改源文件**。
4. 本地附件**不再复制**内容副本；读取和获准修改都作用于源文件。
5. 三种权限模式：`read_only`（只读）/ `restricted`（受限）/ `unrestricted`（完全无限制）。只有用户能提高模式。
6. 所有真实操作入口（文件工具、命令、脚本、自建工具、依赖安装、子智能体、子进程）共享同一个判定与执行边界。
7. 内部运行数据（DB/内部配置/凭据）例外：直接文件/脚本维护必须"明确范围+警告+连续三次确认"（后端校验状态），**所有模式**适用。
8. 用户业务文件删除一律进系统回收站；失败保留文件并如实报告。

---

## 1. 权限模式（模块：`agent/security/modes.py`）

```python
class PermissionMode(str, Enum):
    READ_ONLY = "read_only"
    RESTRICTED = "restricted"          # 首次安装默认
    UNRESTRICTED = "unrestricted"

MODE_LABELS = {"read_only": "只读模式", "restricted": "受限模式", "unrestricted": "完全无限制模式"}
LEGACY_MODE_MAP = {
    "default": "restricted", "plan": "read_only",
    "accept-edits": "restricted", "bypass": "unrestricted",
}
DEFAULT_MODE = PermissionMode.RESTRICTED

def normalize_mode(value: str | None) -> PermissionMode  # 旧值映射 + 未知值→restricted（不静默提高）
def is_valid_mode_value(value: str) -> bool             # 新值或旧值都认可（用于设置接口）
```

- 持久化仍用设置键 `computer.permission_mode`（兼容既有数据）。读取时 normalize；新写入只写新值。
- **只有用户**可提高模式：唯一写入口是 `PUT /api/security/mode`（必需 `user_confirmed=true`，只接受新值）。
  该接口由 HTTP 层调用；模型/工具/子智能体没有任何调用路径。`AuthorizationEngine.set_mode()` 只允许 API 层以
  `user_initiated=True` 调用，否则抛 `PermissionError`。
- 当前模式必须可见：`GET /api/security/mode` → `{mode, label, legacy_value}`；模式变化发事件
  `SECURITY_MODE_CHANGED {mode, label}`，前端顶部/输入区常驻显示。
- 旧 `/api/settings/computer` 保留兼容：GET 返回 normalize 后 `permission_mode`（新值）；PUT 接受新旧值但**映射后落库**。
- 切换语义（引擎实现）：
  - 每个执行操作在**真正开始执行前**快照 `mode_at_start`；已开始执行沿用该快照。
  - 排队中、等待审批中不算开始；批准后执行前按**当前模式**重核对，strict 模式下不再允许则不执行。
  - 严格→宽松：待审批项继续等待用户决定，不自动批准。
  - 过期/拒绝/取消的审批永不复活。

---

## 2. 统一授权模型（模块：`agent/security/`）

### 2.1 模块与公共类型

```python
# agent/security/effects.py
class EffectKind(str, Enum):
    READ = "read"; WRITE = "write"; CREATE = "create"; DELETE = "delete"
    EXECUTE = "execute"; INSTALL_DEPS = "install_deps"; SYSTEM_CHANGE = "system_change"
    NETWORK_EGRESS = "network_egress"; INTERNAL_DATA = "internal_data"
    TOOL_REGISTER = "tool_register"; SUBAGENT = "subagent"

@dataclass(frozen=True)
class Target:
    kind: str            # "path" | "url" | "program" | "record"
    value: str
    identity: str | None = None   # 文件身份键（见 §7），写入/删除类必填（可延迟填）
    exists: bool | None = None

@dataclass(frozen=True)
class Effect:
    kind: EffectKind
    targets: tuple[Target, ...]
    purpose: str                       # 人类可读用途
    tool: str | None = None
    detail: dict = field(default_factory=dict)   # 命令 argv、内容摘要等
    reversible: bool = False

class Verdict(str, Enum):
    ALLOW = "allow"; APPROVE = "approve"; DENY = "deny"

@dataclass(frozen=True)
class AuthDecision:
    verdict: Verdict
    reason: str                 # 中文、可直接展示
    approval_id: str | None
    effects: tuple[Effect, ...] = ()
    scope_id: str | None = None
```

**统一入口（工具只能走它）**

```python
# agent/security/gateway.py
class AuthorizationGateway:
    def mode(self) -> PermissionMode
    async def authorize(self, effect: Effect, *, ctx: AuthContext | None = None) -> AuthDecision
    async def authorize_many(self, effects: Sequence[Effect], *, ctx=None) -> AuthDecision
    async def submit(self, operation: PendingOperation, *, ctx=None) -> ApprovalTicket   # 需审批时延迟执行
    def instruction(self) -> InstructionContext | None
```

- `authorize()` 返回 `ALLOW` 后可执行；`APPROVE` 时工具/执行器把操作交给 `submit()`（持久化 + 立即返回
  `ApprovalTicket(state="waiting")`），工具返回 `ToolResult(ok=True, ...)` + `pending_approval`
  元数据（"已提交，批准后自动执行"）；批准后由审批执行器真正执行并发布结果事件。
  **禁止**在工具调用里同步 await 用户点按钮来占住后续回应能力。
- 只有"已开始执行"的操作保留开始时权限；审批等待不算开始。
- `DENY` 必须给中文原因：当前模式禁止 / 内部运行数据需三次确认 / 对象不可定位 等。

### 2.2 指令与授权（模块：`agent/security/instructions.py`）

```python
@dataclass
class InstructionContext:
    instruction_id: str          # 每轮用户指令一个，形如 "instr_<turn_id>"
    text: str                    # 用户原话（授权依据）
    turn_id: str
    topic_id: str | None
    created_at: str
    grants: list[Grant]          # 本指令内的授权

@dataclass
class Grant:
    grant_id: str
    instruction_id: str
    operations: frozenset[str]   # {"write","delete","execute","install_deps",...}
    objects: list[Scope]         # Scope(kind="path"|"path_prefix"|"url_host"|"program", value=...)
    purpose: str
    source: str                  # "user_explicit" | "user_approved"
    created_at: str
    revoked: bool = False
```

- 每条用户指令建立 `InstructionContext`；**指令结束（turn 结束）即关闭其全部 grant**，不再用于后续指令。
  **绝不**因为同话题/同目录/历史许可而延续。
- 授权的两种来源：
  1. `user_explicit`：**确定性规则**从用户原话中提取，模块 `agent/security/intent.py`（保守、可测试）。
     只有同时出现"明确对象"+"明确动作"才生成 grant。例如"把 D:\\work\\a.docx 第 3 段改成 X 并保存"
     → write(D:\\work\\a.docx)、purpose=该指令；"修复 D:\\proj 里的这个项目" → write(D:\\proj 子树)
     + execute(项目目录内)。**不确定就不授权**，走审批。模型不能自行声明 grant。
  2. `user_approved`：用户点"允许"产生的**有界**授权（对象/操作/目的均来自审批单）。可提供
     "仅这次" 与 "本指令内对该范围" 两种范围；后者写入该指令的 grant，instruction 结束即失效。
- 审批不得扩张：一次批准只覆盖审批单列出的对象/操作/目的/范围；任何维度变化重新审批。
- 用途与例外字段进入审批单与审计记录。

### 2.3 判定规则（引擎 `agent/security/engine.py`）

```python
class AuthorizationEngine(AuthorizationGateway):
    def __init__(self, *, conn, approvals, settings, instance_id=None, workspace_root: Callable[[], Path],
                 internal_data_paths: Callable[[], list[Path]]): ...
```

**read_only**
- READ：允许（当前系统账户可读；凭据/秘钥红线仍 deny）。
- WRITE/CREATE/DELETE/EXECUTE(可写业务文件的任意脚本/自建工具)/SYSTEM_CHANGE：deny，
  原因文案："只读模式下 QIO 不能修改业务文件"。
- 允许的应用内部行为：用户主动导入材料时的应用落盘（`origin=user_saved_material`，由应用侧调用，
  不经过模型工具）、日志/审批状态等运行数据（由应用受控接口写）、工作区 `temp/` 下**有生命周期的**
  解析临时文件（由 `agent/security/tempfiles.py` 管理，退出/完成即清理，不隐藏成永久副本）。
  这些不构成模型任意写文件的能力，也不改变模式。
- 执行层兜底（见 §3）：子进程/脚本/自建工具一律禁止写业务文件；能可靠证明只读的 argv
  （`run_program` 白名单 + shell=False）可以执行，其余脚本执行路径**明确报告不可用**，不得静默降级。

**restricted**
- READ：默认自动允许（当前账户可读）；红线 deny。
- WRITE/CREATE：
  - 目标是 QIO 工作区内 **QIO 生成文件**（`origin=qio_generated`）→ allow。
  - 目标命中本指令 grant（含目录子树/明示文件）→ allow。
  - 其余（用户原有文件、外部目录、工作区内用户材料）→ approve（提交待审批操作）。
- DELETE：
  - QIO 生成的工作文件 → allow，但必须进系统回收站。
  - 用户材料/原有文件：命中本指令明确 delete grant → allow（进回收站）；否则提交**待删除清单审批**。
  - 删除授权不得从"修改项目"扩张出来。
- EXECUTE：
  - 命中 grant 范围内的项目命令（测试/构建/诊断，`run_program` 或指令明确允许的脚本）→ allow；
    必须限定 cwd/参数可证明，shell=False 优先。
  - 自由 shell / 任意脚本 → approve（每次）；无法约束的路径直接 deny 并说明。
- INSTALL_DEPS：QIO 专用环境内 → allow；系统级安装/系统配置 → approve。
- NETWORK_EGRESS：本地模型不涉及；把当前要求相关本地内容发给**用户已配置的云端模型** → allow；
  发给用户未指定的第三方服务 → approve，审批单必须列明"材料、接收服务、用途"；用户明确指定材料+服务可构成 grant。
- TOOL_REGISTER：工具创建通过检查/测试后可自主注册；注册不产生任何永久文件/网络授权，每次调用重新判定。
- SUBAGENT：子智能体只拿到父级 grant 的**子集**，不能扩大；超出父级范围由主智能体统一向用户申请（单层）。

**unrestricted**
- 普通操作 allow（仍受系统账户权限约束）。
- INTERNAL_DATA 直接维护：仍需三次确认（§4）。
- 真实的系统级危险动作仍受操作系统权限约束，但 QIO 不再加常规审批/范围限制。
- **不得保留与该定义冲突的旧红线名称**：".env/.git/.ssh/credentials" 等作为**保护对象**保留，但只限制
  "读写凭据与版本库内部数据"的**直接改写**（内部运行数据规则），不再作为"完全无限制模式仍拒绝一切"的红线；
  在 UI 与文档中如实表达。

**通用**
- 判定以**实际操作效果**为准，不用工具名/工作目录代替。
- 每次判定记录审计：`instruction_id`、对象、动作、用途、模式、批准范围、结果（新增表见 §10）。

### 2.4 上下文传播（模块：`agent/security/context.py`）

```python
current_instruction: ContextVar[InstructionContext | None]
agent_scope: ContextVar[AgentScope | None]      # 子智能体能力子集（父 grant ids + 大深度，不能扩大）
approval_origin: ContextVar[str]                # "user" | "background" | "subagent"
```
- `TurnRuntime`/loop 在每轮开始设置 `current_instruction`；子智能体创建时用父上下文生成
  `AgentScope`（含父 grant 子集、深度、用途）；子智能体内的所有 effect 必须带 `agent_scope`。
- 引擎在评估时: 先看 agent_scope，若 effect 超出父授权 → 主智能体统一申请，不允许子智能体直接弹窗。

---

## 3. 统一执行边界

### 3.1 工具侧（W2）
- `fs_read/fs_list/fs_find/fs_info`：READ effect（红线 deny）。
- `fs_write/fs_patch`：WRITE/PATCH effect，写入前经 `authorize`；允许时**执行前**重新核对文件身份
  （见 §7），原子写（同目录临时文件 + 替换），失败保原件。
- `run_program/run_shell` 与进程工具：EXECUTE effect；保留 `ComputerSandbox` 的 argv 危险分类，
  但最终判定由引擎给出。仅读替身/工作区下可执行文件一律不可信。
- `proc_kill`：只允许结束本实例管理的 pid（实例归属/JOB 归属），否则 approve。
- 脚本执行（脚本工具/自建工具）：`tools/sandbox.py` 子进程统一挂
  - Job Object（KILL_ON_JOB_CLOSE、内存/进程数上限）；
  - **只读/受限下的低完整性（Low IL）**：read_only 强制 Low IL（OS 层写拒绝 Medium 对象），
    restricted 下对**未获授权写范围**的脚本同样强制；`unrestricted` 不降级（与旧行为一致）。
  - 环境变量清洗、临时 cwd、超时、输出截断照旧，但**不得**把这些单独宣称为安全沙箱。
  - 事后核验：自动放行的脚本执行前记录受保护范围快照（mtime/size/身份），执行后比对，
    发现越界修改 → 上报事实（不能回滚的如实说明），并把该执行标记为违规。
- 自建工具：注册走既有检查/测试；**每次调用**重新经引擎判定；不因注册获得永久权限。
- 依赖安装：QIO 专用环境（tool_envs）内 allow；系统级 approve。
- 子智能体：创建时由主智能体分配 `AgentScope`（最小权限、父 grant 子集），引擎逐调用判定。

### 3.2 执行层约束的技术事实（必须写进文档/报告）
- Low IL 是 Windows 内核 MIC：能**阻止写入** Medium 完整性对象（用户文件、QIO 数据目录），
  不能阻止读取，**不能阻止网络**（除非另有系统级防火墙）。
- Job Object 能限制内存/进程数并在句柄关闭时内核级收掉进程树。
- 不存在"任意脚本 + 网络隔离"的可靠单机方案时如实报告，不声称受限。
- 所有承诺用 `scripts/verify_r9_execution_boundary.py` 在真实 Windows 上取证。

---

## 4. 内部运行数据（模块：`agent/security/internal_data.py`）

- 受保护路径：`%APPDATA%/qio` 下数据库、内部配置、凭据存储、实例注册表等（由 AppContext 提供路径清单）。
- 正常应用功能（设置保存、记忆整理、审批落库等）走各自受控接口，**不经过**该文件工具边界，不要求三次确认。
- 工具/脚本/自建工具/子智能体直接以文件方式维护内部数据：effect=`INTERNAL_DATA`，必须：
  1. 审批单写明维护内容与范围（文件、操作、目的）；
  2. 鲜明警告（"这是 QIO 内部运行数据，直接改写可能损坏应用"）；
  3. 用户**连续三次**确认（后端 CAS 状态：`confirmed_stage` 0→1→2→3），每次确认不得新开请求；
  4. 任一步取消即停止；对象/内容/范围变化 → 重置为 0 并重新确认；
  5. 确认状态持久化，后端校验；前端只呈现。
- 适用于全部模式（含 unrestricted），覆盖脚本/自建工具/子智能体。
- 一次批准 ≠ 无限改写：审批单绑定对象/内容/范围，执行前重核对。

---

## 5. 审批生命周期 v2（模块：`agent/tools/approval.py` + `agent/storage/approvals.py`）

### 5.1 语义
- 默认有效期 **600 秒**，从创建时算起（`created_at + 600s`）。刷新、暂缓、重启**不续期**。
- 单次消费：等待/批准/执行中/完成/失败/拒绝/取消/过期/中断待核对，状态见 §5.2。
- 审批单必须持久化**可恢复操作及必要上下文**：operation kind + payload（可序列化）、effect、对象身份指纹、
  指令 id 与原话、用途、模式、创建/过期时间、确认阶段、owner 实例。
- 批准后、真正执行前重新核对：当前模式、对象身份与实际位置、操作内容/范围、相关文件变化、批准是否仍有效、
  是否已执行。任一不符 → 不执行，标 `invalidated` 并如实说明（新的执行需重新审批）。
- 原子领取：`UPDATE pending_approvals SET state='executing', attempt=attempt+1, claimed_at=?
  WHERE approval_id=? AND state='approved'`，rowcount 必须为 1，否则视为重复领取，不执行。
- 崩溃恢复：启动时
  - `waiting` 且未过期 → 恢复为待审批（UI 可继续回应；owner 已死则接管）；
  - `waiting` 已过期 → `expired`；
  - `approved`（已决定未执行）→ 重核对后可执行；
  - `executing`（可能已部分执行）→ `interrupted_unverified`，**不自动重放**，界面给"待核对"入口；
  - 不因退出而把有效 waiting 永久取消。
- 非阻塞：`submit()` 立即返回 ticket；工具返回 pending 结果；批准后由后台执行器执行并发布结果。
  其他不依赖该审批的工作（其他 turn/要求）必须可以继续。

### 5.2 状态机
`waiting → approved → executing → completed | failed`；`waiting → rejected | cancelled | expired`；
`executing → interrupted_unverified`；`approved → invalidated`（重核对失败）。
`pending_approvals` 既有 `status` 列保留兼容（pending/interrupted/timeout 等），新列 `state` 为权威。
UI 与 API 用新状态；旧记录读取时映射。

### 5.3 恢复入口（executor）
```python
# agent/security/operations.py
@dataclass
class PendingOperation:
    kind: str          # "fs.write" | "fs.patch" | "fs.delete" | "proc.run_program" | "proc.shell"
                       # | "dep.install" | "net.egress" | "internal.data_write" | "tool.invoke"
    label: str
    payload: dict      # 可序列化执行参数
    effect: dict
    fingerprint: dict  # 对象身份/内容摘要，重核对
    instruction_id: str | None
    instruction_text: str | None
    purpose: str
    mode_at_request: str
    confirmations_required: int = 1   # INTERNAL_DATA 为 3

@dataclass
class ApprovalTicket:
    approval_id: str
    state: str = "waiting"
    expires_at: str
    confirmations_required: int = 1
```
- 执行器注册表：`register_operation(kind, fn)`，`async fn(payload, ctx: ExecutionContext) -> ExecResult`。
  每个操作在真实执行处再次走引擎的 `authorize_started()`（模式快照）与对象重核对。
- 结果：发布事件 `APPROVAL_EXECUTED {approval_id, ok, summary, detail}`，并落一条可读历史
  （`approval_history`）；原 turn 若仍存在则追加系统通知消息，不伪装成模型从未说过的话。

### 5.4 API 与事件
- `GET /api/approvals/pending` → `{approvals: [...]}`（含 operation 摘要、过期时间、confirmed_stage、
  requires_confirmations、source=tool/subagent/background）。
- `GET /api/approvals/history?limit=...`
- `POST /api/approvals/{id}/decide` `{decision:"approve"|"reject"|"cancel", expected_revision?}`
- `POST /api/approvals/{id}/confirm` `{stage: 1|2|3}`（内部数据三次确认；CAS）。
- 旧 `/api/approvals/{id}/respond` 保留兼容（映射到 decide），但新前端用新接口。
- 事件：`APPROVAL_REQUIRED`（既有）、`APPROVAL_RESULT`（既有）、`APPROVAL_UPDATED`（状态/确认阶段变化）、
  `APPROVAL_EXECUTED`（执行完成/失败/待核对）。**事件协议守卫测试必须同步更新前端消费**。

---

## 6. 源文件引用（模块：`agent/services/attachments.py` + `agent/storage/attachments.py`）

### 6.1 数据模型（`attachments` 表追加列，见 §10）
- `source_kind`：`desktop_path` | `dropped_path` | `browser_upload` | `clipboard_image` |
  `legacy_copy` | `missing`。
- `origin`：`user_original`（用户原文件）| `user_saved_material`（用户提供、QIO 首次落盘的材料）|
  `qio_generated`（QIO 自主生成的工作文件）。
- `file_id`：文件身份键（§7）；`content_sha256`；`reference_state`：`available|missing|ambiguous|relocated`；
  `material_enabled`（互动材料开关，默认 1）；`user_note`；`last_verified_at`；`legacy_paths`（JSON 历史事实）。
- 新记录 `stored_path` 为 NULL（不再有副本）。旧记录保留原样供迁移与只读回放。

### 6.2 服务 API（新入口，旧 `prepare/copy` 只服务旧数据回放）
```python
class AttachmentService:
    def register_source(self, *, path: str, source_kind: str, origin: str,
                        topic_id=None, name=None, user_note="") -> Attachment
    def write_material(self, *, stream, name: str, source_kind: str,   # browser_upload / clipboard_image
                       topic_id=None, user_note="") -> Attachment       # 首次落盘到 workspace/materials/
    def resolve_source(self, attachment_id: str, *, for_write=False) -> ResolvedSource
    def relocate_reference(self, attachment_id: str, path: str, *, confirmed_identity: bool) -> Attachment
    def set_material_enabled(self, attachment_id: str, enabled: bool) -> Attachment
    def remove_reference(self, attachment_id: str) -> dict   # 只删卡片/引用行，绝不删源文件
    def payload(self, att) -> dict                            # 见 6.4
```
```python
@dataclass
class ResolvedSource:
    attachment_id: str
    path: Path | None
    identity: FileIdentity | None
    state: str            # available | missing | ambiguous
    reason: str
    candidates: list[dict]
```
- `resolve_source` 是读取/预览/获准写回/媒体解析/历史附件读取的**唯一**解析入口；
  不同入口不得一处读源文件、另一处读旧副本。
- 写回：先 `resolve_source(for_write=True)` 再引擎授权；原子写；写前重核对 identity+hash；
  变化则不覆盖并说明。用户只说"修改这个文档"未明确写回方式 → 先读取，写前让用户选择
  （覆盖源文件 / 另存为新文件）；明确指定文件+内容+覆盖方式 → 当次授权直接执行，不重复审批。
- 删除卡片/引用 ≠ 删除源文件；复制卡片共享同一 attachmentId；撤销/重做保持共享。

### 6.3 入口诚实区分
- 桌面原生选择/拖放：必须拿真实路径 → `register_source`（不复制、不移动、不改名）。
- 只有字节没有原位置（浏览器上传、剪贴板内容）：`write_material` 首次落盘到工作区，
  记录 `source_kind` 与 `origin=user_saved_material`；**不得伪造路径**。
- 浏览器若在另一台设备：上传内容只是本机新落盘材料，不得声称能修改那台设备原文件（文案与 payload 标注）。

### 6.4 payload 扩展（供卡片/上下文）
```python
{"attachment_id","filename","media_kind","origin","source_kind","availability","size",
 "user_note","material_enabled","missing_reason","reference_id"}
```
互动上下文/卡片快照必须暴露以上信息 + 用户说明；不得只给"待补充文件说明"。

### 6.5 旧 100MB 副本策略
- 删除"≤100MB 自动复制 / >100MB 只记路径"的产品逻辑；资源/传输限制若需要，单独作为"上传大小限制"
  说明，不得包装成附件副本规则。
- 必要的短期处理文件（解析、安全写回）由 `tempfiles.py` 管理生命周期，失败/取消清理；
  不是隐藏的永久备份。

---

## 7. 文件身份与移动修复（模块：`agent/services/file_identity.py`）

```python
@dataclass(frozen=True)
class FileIdentity:
    volume: str          # Windows 卷序列号 / POSIX st_dev
    file_id: str         # Windows FILE_ID_128 / POSIX st_ino
    path: str
    size: int
    mtime_ns: int

def identity_of(path: Path) -> FileIdentity | None     # Windows: GetFileInformationByHandleEx(FileIdInfo)
def content_sha256(path: Path, *, chunk=1MB) -> str | None
def same_file(a: FileIdentity, b: FileIdentity) -> bool  # 只能靠 volume+file_id
```
- **同一文件编辑后仍是同一文件**（identity 不变，内容 hash 变）；**相同内容的另一份文件不当然是原文件**。
- 移动/改名失效修复：使用时自动在有限范围查找（原目录、父目录、工作区、用户指定的搜索根；
  不持续全盘监听）。只有 `volume+file_id` 相同 → 自动修复；仅同名/同大小/同 mtime/同 hash →
  **必须让用户选择**（展示候选、来源、"这是同一文件吗"）。无法唯一确认 → `ambiguous`。
- 记录 `relocated_at`/`last_verified_at`，修复事件进审计。

---

## 8. 文件卡片与互动材料

- 聊天附件与互动文件卡共用同一源引用与权限规则。
- 卡片删除 = 软删除卡片；引用行可清理，源文件保留；复制卡片共享 attachmentId；撤销/重做不破坏共享。
- "允许 QIO 查看"文案改为"作为互动材料"；开关仅控制是否主动纳入互动上下文，不是 OS 文件访问权限。
- 后端 `_card_snapshot`（`agent/interactive/*`）必须包含 §6.4 file_ref + 用户说明；
  互动上下文构造同步暴露这些字段。
- 载入/预览/按需读取共用 `resolve_source`；QIO 按用户要求自主决定是否读取，不要求每次提交解析全部文件。

---

## 9. 旧数据迁移（模块：`agent/storage/attachment_migration.py`）

- 先盘点：attachments 表、真实文件（stored_path/source_path）、messages 引用、卡片引用、预览引用。
- 迁移可恢复、可重复运行（幂等）；每行审计 `attachment_migration_audit(attachment_id, step, outcome, detail)`。
- 有有效原路径：核实对应关系后改为源引用；原文件与历史保存内容不同 → **保留差异事实**（legacy_paths），
  不静默替换历史材料。
- 无原路径或原件失效、QIO 保存文件仍在：把现存文件登记为**当前源文件**（origin=user_saved_material），
  不删除、不再复制一份，记录历史来源。
- 全部失效：保留 `missing` 状态与"重新指定位置"入口，不伪造恢复。
- 保留已有附件 id 与引用关系；确需换标识必须完整迁移所有使用处。
- 登记迁移与磁盘清理分开：先确保全部引用可用，再生成冗余材料清理清单，经批准移入系统回收站；
  首次迁移不强行搬动现存文件。
- 迁移入口：应用启动后后台执行一次 + 维护接口可重跑；中断后从审计断点继续。

---

## 10. DB Schema（Lead 已冻结，追加迁移 44/45）

> 只允许追加；W1/W3 不得自行修改历史迁移或占用编号。需要新列先向 Lead 申请。

**44（附件源引用）**
```sql
ALTER TABLE attachments ADD COLUMN source_kind TEXT;
ALTER TABLE attachments ADD COLUMN origin TEXT;
ALTER TABLE attachments ADD COLUMN file_id TEXT;
ALTER TABLE attachments ADD COLUMN content_sha256 TEXT;
ALTER TABLE attachments ADD COLUMN reference_state TEXT;
ALTER TABLE attachments ADD COLUMN material_enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE attachments ADD COLUMN user_note TEXT NOT NULL DEFAULT '';
ALTER TABLE attachments ADD COLUMN last_verified_at TEXT;
ALTER TABLE attachments ADD COLUMN legacy_paths TEXT NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS attachment_migration_audit (
  attachment_id TEXT NOT NULL, step TEXT NOT NULL, outcome TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
  PRIMARY KEY (attachment_id, step));
CREATE TABLE IF NOT EXISTS material_cleanup_candidates (
  attachment_id TEXT PRIMARY KEY, path TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'candidate', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
```

**45（审批 v2 + 审计）**
```sql
ALTER TABLE pending_approvals ADD COLUMN state TEXT;
ALTER TABLE pending_approvals ADD COLUMN operation TEXT NOT NULL DEFAULT '{}';
ALTER TABLE pending_approvals ADD COLUMN instruction_id TEXT;
ALTER TABLE pending_approvals ADD COLUMN instruction_text TEXT;
ALTER TABLE pending_approvals ADD COLUMN purpose TEXT;
ALTER TABLE pending_approvals ADD COLUMN fingerprint TEXT NOT NULL DEFAULT '{}';
ALTER TABLE pending_approvals ADD COLUMN mode_at_request TEXT;
ALTER TABLE pending_approvals ADD COLUMN mode_at_approval TEXT;
ALTER TABLE pending_approvals ADD COLUMN confirmed_stage INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pending_approvals ADD COLUMN revisions INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pending_approvals ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pending_approvals ADD COLUMN claimed_at TEXT;
ALTER TABLE pending_approvals ADD COLUMN result TEXT;
ALTER TABLE pending_approvals ADD COLUMN error TEXT;
ALTER TABLE pending_approvals ADD COLUMN resumed_at TEXT;
CREATE TABLE IF NOT EXISTS approval_audit (
  approval_id TEXT NOT NULL, seq INTEGER NOT NULL, action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}', at TEXT NOT NULL, PRIMARY KEY (approval_id, seq));
CREATE TABLE IF NOT EXISTS approval_history (
  approval_id TEXT PRIMARY KEY, kind TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', at TEXT NOT NULL);
```
`SCHEMA_VERSION → 45`。读取旧行时 `state IS NULL` → 按旧 status 映射（pending→waiting、interrupted→interrupted_unverified）。

---

## 11. 桌面壳：关闭 / 托盘 / 退出（W5）

- 设置键 `ui.close_behavior`：`"ask" | "tray" | "quit"`，默认 `"ask"`（经 `/api/settings/ui` 持久化；
  旧客户端无此字段时按默认，不报错）。
- 主窗口关闭（标题条关闭按钮、系统 CloseRequested、Alt+F4）统一进入 `WindowChrome` → Tauri 命令
  `request_close`：
  - ask：原生/前端确认框"最小化到托盘 / 退出 QIO / 取消"（记住选择可写回设置）；
  - tray：`window.hide()`，QIO 继续运行，托盘可恢复；不触发退出收尾；
  - quit：进入真正退出流程。
- 托盘（Tauri 2 `TrayIconBuilder`）：图标 + 菜单"显示主窗口 / 退出 QIO"；恢复窗口 = show + unminimize + focus；
  隐藏后必须能恢复，不能出现后端被重新拉起/重复实例。
- 真正退出流程（直接退出与托盘退出共用）：
  1. 前端守卫 `quitting=true`，不再发起新 turn；
  2. 调用后端 `POST /api/lifecycle/shutdown`：后端停止接受新工作（503）、保存状态、有界等待收尾
     （常量 `SHUTDOWN_GRACE_SECONDS`，初值 8.0，按安装版实测调整并记录）、结束本实例管理的执行/子智能体/子进程、
     持久化有效待审批（waiting 不取消，供重启恢复）；返回 `{done, report}` 或超时 `{timeout:true}`；
  3. 壳等待 sidecar 退出（按 pid 的进程树，非进程名），超时用 `taskkill /PID <pid> /T /F` 兜底并核验 pid 已消失；
  4. 退出应用。整个过程可重复调用不出错（幂等）。
- 复用现有实例归属、Job Object、按 pid 清理；绝不按进程名批量结束。
- 区分"停止当前执行"（取消 turn/操作，不退出）与"退出软件"（不永久取消有效待审批）。
- 后端 lifespan/aclose 必须在退出流程中获得执行机会（壳不能直接杀进程了事）。

---

## 12. 验证矩阵（最终验收；Lead 汇总证据）

后端自动化（新增文件名固定，便于最终汇总）：
- `test_security_modes.py`：三模式、旧值映射、持久化恢复、只有用户能改、模式展示。
- `test_authorization_engine.py`：read_only/restricted/unrestricted 判定、内部数据三次确认、删除审批。
- `test_instruction_scope.py`：明确指令授权不重复审批；后续指令不继承外部目录权限；grant 随指令关闭。
- `test_approval_lifecycle.py`：600s、状态机、重复批准、原子领取、对象变化、非阻塞。
- `test_approval_restart.py`：刷新/重启不续期、恢复 waiting、executing→interrupted_unverified 不重放。
- `test_internal_data_guard.py`：三连确认、任一步取消、范围变化重置、全部模式。
- `test_sandbox_readonly.py`：只读下脚本/自建工具/后代进程写业务文件被拒（含真实 Low IL 探针，Windows 标记）。
- `test_subagent_scope.py`：子智能体不能扩权、注册工具不产生长期权限。
- `test_attachment_source_refs.py`：登记不复制、无路径首次落盘、读取/预览/写回同一源、删除引用不删文件。
- `test_attachment_identity.py`：移动修复、同名不同文件不误认、编辑后仍同一文件。
- `test_attachment_migration.py`：有原路径/无原路径/全失效、幂等、断点恢复、不丢唯一内容。
- `test_file_cards_refs.py`：卡片快照含引用信息、删卡/复制/撤销共享。
- `test_approval_api.py`：新接口与事件、前端状态恢复。
前端：`vue-tsc --noEmit`、`npm test`（模式 UI、关闭行为 UI、文件卡/附件 UI、审批卡）。
真机（Windows，脚本进 `scripts/`，证据进 `_r9-evidence/`）：
- `verify_r9_execution_boundary.py`：Low IL 写拒绝、Job 收进程树、只读工具路径实测。
- `verify_r9_recycle_bin.py`：真实进系统回收站、失败保留、可恢复。
- `verify_r9_file_identity.py`：移动/改名/同名不同文件。
- `verify_r9_process_tree.py`：退出只结束本实例进程树，其他实例不受影响。
- 安装版：构建/运行证据（能完成就做，不能完成如实标注"未验证"及原因）。

---

## 13. 文件归属与集成顺序

| 分支 | worktree | 负责人范围 | 独占文件（主要） |
| --- | --- | --- | --- |
| `r9/main` | qio-r9-main | Lead：契约、schema、集成、验收 | `storage/schema.py`、`docs/*`、`scripts/verify_r9_*` |
| `r9/w1-auth` | qio-r9-w1 | 统一授权/审批持久化与恢复 | `agent/security/**`(engine/instructions/ops)、`tools/approval.py`、`storage/approvals.py`、`api/server.py`(approval/mode/lifespan)、`tools/registry.py`、`core/loop.py` 审批调用点、`services/app.py` 接线 |
| `r9/w2-exec` | qio-r9-w2 | 文件执行边界/脚本/自建工具 | `tools/fs_tools.py`、`cmd_tools.py`、`sandbox.py`、`isolation.py`、`subagent_tool.py`、`dev_auth.py`、`lifecycle.py`、`tool_envs.py`、`tools/creator.py` 及测试 |
| `r9/w3-attach` | qio-r9-w3 | 源文件引用/附件服务/旧数据迁移 | `services/attachments.py`、`services/file_identity.py`、`storage/attachment_migration.py`、`api/server.py`(附件段)、`interactive/board_tools.py` 引用快照 |
| `r9/w4-ui` | qio-r9-w4 | 文件卡/上传入口/预览/权限设置 UI | `frontend/src/services/attachments.ts`、`components/interactive/AddMenu.vue`、`interactive/board.ts`、新增设置/卡片组件、`SettingsView.vue`(权限段) |
| `r9/w5-shell` | qio-r9-w5 | 托盘/退出/受管理进程收尾 | `src-tauri/src/main.rs`、`backend_lifecycle.rs`、`Cargo.toml`、`tauri.conf.json`、`WindowChrome.vue`、`api/server.py`(lifecycle 段)、`SettingsView.vue`(关闭行为段) |

集成顺序：W1 → W2 → W3 → W4/W5（后端先合，前端最后）。冲突由 Lead 处理；各分支只能提交自己的 worktree。
**禁止**任何分支执行 `git merge main`、推送、发布。只允许本分支 commit。

## 14. 并行工作纪律

- 所有分支：`git status` 干净开始；只做定向测试，不跑全量（全量由 Lead 最终跑一次）。
- 不在日志/事件/测试输出暴露密钥；新增输出过 `agent/trace/redact.py`。
- 中文 UI 文案、中文汇报。禁止删测试/放宽断言/无依据 skip。
- 汇报必须区分：**已实现 / 已运行验证 / 仅源码检查**，附命令与输出位置。
- 遇到契约冲突先停下并报告 Lead，不自行另立一套。

## 15. 变更记录

- 2026-10-22 初版冻结（Lead，基线 249a6344）。
