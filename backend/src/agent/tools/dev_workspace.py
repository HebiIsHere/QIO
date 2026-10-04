"""Tool development workspace: sandboxed per-task directory.

The main agent develops tools inside a workspace (write code/tests, run
tests, iterate) before submitting for approval. Files are restricted to
the workspace directory; the definition contract reuses ToolDefinition.
"""

from __future__ import annotations

import json
import re
import hashlib
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agent.tools.spec import ToolDefinition
from agent.tools.project_files import safe_rel_path

MAX_FILE_SIZE = 200_000
# 工作区 id 的形状（`DevWorkspace.create` 生成）：扫盘回填时只认它
_TASK_ID = re.compile(r"^ws_[0-9a-f]{12}$")
_REQUEST_MARKER = "# 开发需求"
# 开发任务状态文件（提交 / 测试 / 内容摘要）。放在工作区目录里，
# 与已有 `ws_*` 成果同源：重启后可以原样读回，不依赖内存表。
_STATE_FILE = "state.json"
# 权威状态文件的格式版本与来源标记：读回时必须同时对上，否则按「未知」处理。
# 只认自己写的那一份，避免把外部/旧格式的 state.json 当成证据。
_STATE_SCHEMA = 3
# 读回时**同时接受**旧号与新号（向后兼容是硬要求）：schema 2 是本改动之前
# 写下的文件（没有 abandoned 字段）。旧文件必须仍能读回测试证据 / 授权 /
# 提交摘要，不能因为升了 schema 就把它们当成「未知」丢掉。
_STATE_SCHEMAS = frozenset({2, _STATE_SCHEMA})
_STATE_SOURCE = "qio.dev_workspace"
# 后端独占的保留文件：文件工具不得写入。agent 只能改「项目内容」，
# 不能改「权威记录」。（诚实边界：同权限子进程仍能直接改磁盘上的文件，
# 保留名只是挡住了文件工具这条路径，不是完整安全边界。）
_RESERVED_NAMES = frozenset({_STATE_FILE, "request.md"})
# 清单文件也不是项目模块：`tool.json` 描述工具，不参与工具自身的运行。
_NON_PROJECT_NAMES = _RESERVED_NAMES | {"tool.json"}
# 测试证据的三态：没有证据 / 证据对应当前内容 / 证据已过期。
EVIDENCE_NONE = "none"
EVIDENCE_CURRENT = "current"
EVIDENCE_STALE = "stale"
_EVIDENCE_STATES = frozenset({EVIDENCE_NONE, EVIDENCE_CURRENT, EVIDENCE_STALE})


def _is_digest(value: object) -> bool:
    """sha256 十六进制摘要的形状校验（证据字段必须长这样才可信）。"""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


# ---- 授权的生命周期（D2）-------------------------------------------------
# 三种明确的生命周期，取代「用户不撤销就永久有效」：
#   once      —— 本次执行：只放行紧接着的这一次执行（放行的同时就用掉），
#                且只覆盖那一版内容（绑定内容摘要）。
#   task      —— 当前开发任务：任务还在开发中就一直有效；任务提交即结束。
#   long_term —— 长期授权（跨任务）：必须由用户**显式选择**，不会自动升级。
# 刻意不设「默认 24 小时」这种拍出来的时长：有效期只由这三条生命周期与
# 身份绑定决定（见 tools/dev_auth.py）。expires_at 只在审批明确带回时才记。
LIFETIME_ONCE = "once"
LIFETIME_TASK = "task"
LIFETIME_LONG_TERM = "long_term"
_LIFETIMES = frozenset({LIFETIME_ONCE, LIFETIME_TASK, LIFETIME_LONG_TERM})
LIFETIME_LABELS = {
    LIFETIME_ONCE: "本次执行",
    LIFETIME_TASK: "当前开发任务",
    LIFETIME_LONG_TERM: "长期授权（跨任务）",
}
DEFAULT_LIFETIME = LIFETIME_TASK

# 授权记录的**对外状态**（接口 / 界面看到的事实，不是内部枚举）：
# 一条授权要么现在真的算数（valid），要么有一条说清为什么不算数的理由。
AUTH_VALID = "valid"
AUTH_NONE = "none"            # 从来没有授权过
AUTH_LEGACY = "legacy"        # 旧格式记录：覆盖范围无法证明 → 必须重新确认
AUTH_CONSUMED = "consumed"    # 「本次执行」已经用掉
AUTH_ENDED = "ended"          # 开发任务已提交：任务级授权到此为止
AUTH_STALE = "stale"          # 策略 / 执行环境 / 被测内容变了
AUTH_NARROWED = "narrowed"    # 现在要的范围比授权过的更大 → 不得自动扩大
AUTH_EXPIRED = "expired"      # 记录里带了到期时刻且已到期

_LONG_TERM_FILE = "long_term_authorizations.json"

# ---- 放弃开发（终态）-------------------------------------------------------
# 「放弃」结束一项**没做完**的开发任务：进入不可逆终态，从未完成列表移除，
# 并收回这个任务的执行授权。它**不**删记录、不删工作区文件、不删已注册工具
# （见 _ABANDON-CONTRACT.md 第 1 章的语义边界）。
ABANDONED_PHASE = "abandoned"
# 放弃前置判定的状态词（契约 §2.4 / §2.5，前端按同一组词显示文案）
ABANDON_OK = "ok"
ABANDON_ALREADY = "already_abandoned"
ABANDON_NOT_FOUND = "not_found"
ABANDON_SUBMITTED = "submitted"
ABANDON_RUNNING = "running"
ABANDON_DONE = "abandoned"

# ---- 活跃执行登记（进程内、不落盘）----------------------------------------
# 运行登记是**进程内**事实：进程重启后本来就没有任何东西在跑，这是诚实的答案，
# 所以它不写 state.json。
RUN_TESTS = "tests"
RUN_SUBMIT = "submit"
_RUN_KINDS = frozenset({RUN_TESTS, RUN_SUBMIT})
STAGE_PREPARING = "preparing"
STAGE_WAITING_APPROVAL = "waiting_approval"
STAGE_EXECUTING = "executing"
_RUN_STAGES = frozenset({STAGE_PREPARING, STAGE_WAITING_APPROVAL, STAGE_EXECUTING})


def _str_list(value: object) -> list[str]:
    """把 scope 里的列表字段收敛成字符串列表（形状不对就回空列表）。"""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "")]
    return []


def _read_authorization(raw: object) -> dict | None:
    """读回「执行生成代码」的授权记录；形状不对就当作没有授权。

    记录里明明白白绑着这次执行的身份：能力策略指纹、执行环境、目录范围、
    网络范围、凭据范围、被测内容摘要，以及生命周期。用户要能查到自己到底
    同意了什么；判定也只认这些字段，不因为多存了一份说明而改变语义。
    """
    if not isinstance(raw, dict):
        return None
    fingerprint = raw.get("policy_fingerprint")
    executor = raw.get("executor")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    if not isinstance(executor, str) or not executor:
        return None
    scope = raw.get("scope")
    scope = dict(scope) if isinstance(scope, dict) else {}
    lifetime = raw.get("lifetime")
    return {
        "policy_fingerprint": fingerprint,
        "executor": executor,
        "at": str(raw.get("at") or ""),
        # 没有 lifetime 的记录来自旧版本：它覆盖什么范围无法证明，按 legacy
        # 处理 —— 下一次执行重新确认，绝不沿用一条说不清的授权。
        "lifetime": lifetime if lifetime in _LIFETIMES else "",
        "content_digest": str(raw.get("content_digest") or ""),
        "filesystem": _str_list(raw.get("filesystem", scope.get("filesystem"))),
        "network": bool(raw.get("network", scope.get("network"))),
        "network_allow": _str_list(
            raw.get("network_allow", scope.get("network_allow"))
        ),
        "credentials": _str_list(raw.get("credentials", scope.get("credentials"))),
        "consumed_at": str(raw.get("consumed_at") or ""),
        "expires_at": str(raw.get("expires_at") or ""),
        "owner_task_id": str(raw.get("owner_task_id") or ""),
        "scope": scope,
    }


def _authorization_state(record: dict | None, *, task, requested: dict | None) -> str:
    """这条授权现在还作数吗？不作数就给出**具体理由**（不是一句「没授权」）。

    `requested` 是这次要执行的身份（能力指纹 / 执行环境 / 目录 / 网络 / 凭据 /
    内容摘要）。范围只允许**收窄或相等**：现在要的比授权过的更大就是不覆盖，
    必须重新确认 —— 这就是「范围变化后旧授权不得自动扩大」。
    """
    if not record:
        return AUTH_NONE
    if not record.get("lifetime"):
        return AUTH_LEGACY
    if record.get("consumed_at"):
        return AUTH_CONSUMED
    expires_at = record.get("expires_at")
    if expires_at and expires_at < _now():
        return AUTH_EXPIRED
    if record.get("lifetime") == LIFETIME_TASK and task is not None and task.submitted:
        return AUTH_ENDED
    if requested:
        if record.get("policy_fingerprint") != str(
            requested.get("policy_fingerprint") or ""
        ):
            return AUTH_STALE
        if record.get("executor") != str(requested.get("executor") or ""):
            return AUTH_STALE
        for field in ("filesystem", "network_allow", "credentials"):
            if not set(requested.get(field) or []) <= set(record.get(field) or []):
                return AUTH_NARROWED
        if requested.get("network") and not record.get("network"):
            return AUTH_NARROWED
        if record.get("lifetime") == LIFETIME_ONCE:
            # 「本次执行」只覆盖被批准的那一版内容：摘要对不上（或拿不到）
            # 就不再算数。
            wanted = str(requested.get("content_digest") or "")
            if not wanted or wanted != record.get("content_digest"):
                return AUTH_STALE
    return AUTH_VALID


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_not_reserved(rel_path: str) -> None:
    """保留名在任何深度都不可写：`pkg/state.json` 同样不能覆盖后端状态。"""
    for segment in rel_path.split("/"):
        if segment in _RESERVED_NAMES:
            raise ValueError(f"{segment} 由后端维护，不能通过文件工具写入")


def _read_request(task_dir: Path) -> str:
    """读回工作区的需求正文（写盘时带了「# 开发需求」文件头，这里去掉）。"""
    try:
        text = (task_dir / "request.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.lstrip().startswith(_REQUEST_MARKER):
        text = text.lstrip()[len(_REQUEST_MARKER):]
    return text.strip()


def _read_state(task_dir: Path) -> dict:
    """读回工作区状态；缺失、损坏、schema/来源对不上时回空字典（按「未知」处理）。"""
    try:
        raw = (task_dir / _STATE_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("schema") not in _STATE_SCHEMAS or data.get("source") != _STATE_SOURCE:
        # 旧格式 / 手写的 state.json：不认，一律回「未知」，绝不由此推断测试通过。
        return {}
    return data


def _write_state(task: "DevTask") -> None:
    """落盘任务状态（尽力而为：写不进去也不能让工具调用失败）。"""
    payload = {
        "schema": _STATE_SCHEMA,
        "source": _STATE_SOURCE,
        "id": task.id,
        "request": task.request,
        "created_at": task.created_at,
        "phase": task.phase,
        "submitted": task.submitted,
        "test_runs": task.test_runs,
        "last_test_passed": task.last_test_passed,
        "last_test_summary": task.last_test_summary,
        "last_test_at": task.last_test_at,
        "last_test_digest": task.last_test_digest,
        "evidence_state": task.evidence_state,
        "test_authorization": task.test_authorization,
        "submitted_digest": task.submitted_digest,
        "submitted_at": task.submitted_at,
        # 放弃是终态：它必须落盘，否则重启后任务会「复活」成未完成。
        "abandoned": task.abandoned,
        "abandoned_at": task.abandoned_at,
    }
    try:
        (task.dir / _STATE_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


@dataclass
class DevTask:
    id: str
    request: str
    dir: Path
    created_at: str = field(default_factory=_now)
    submitted: bool = False
    test_runs: int = 0
    # 权威状态（不再靠文件存在推断「测试通过」）：
    phase: str | None = None
    last_test_passed: bool | None = None
    last_test_summary: str | None = None
    last_test_at: str | None = None
    # 这条测试证据对应的内容摘要（版本标识）：文件一变，证据立即失效。
    last_test_digest: str | None = None
    evidence_state: str = EVIDENCE_NONE
    # 用户的「执行生成代码」授权记录：逐字段绑定这次执行的身份（能力策略指纹、
    # 执行环境、目录 / 网络 / 凭据范围、被测内容摘要）与生命周期，任一项变了、
    # 或者现在要的范围更大，就不再算数（见 dev_auth.py 与 _authorization_state）。
    test_authorization: dict | None = None
    submitted_digest: str | None = None
    submitted_at: str | None = None
    # 放弃开发：不可逆终态。一旦为 True，所有写操作对它都是无效操作
    # （见 DevWorkspace 各写方法的前置判定），绝不能擦回 False。
    abandoned: bool = False
    abandoned_at: str | None = None


class DevWorkspace:
    """In-memory task registry + per-task sandbox directories."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, DevTask] = {}
        # 工作区级（跨任务）的长期授权：与任务状态分开存，重启后照样生效，
        # 也让「长期」真的跨任务 —— 不然它只是任务级的另一个名字。
        self._long_term: list[dict] = []
        # 活跃执行登记：task_id → {kind, stage, started_at, stopper}。
        # 进程内事实，不落盘（重启后本来就没有东西在跑）。
        self._runs: dict[str, dict] = {}
        self._restore()
        self._restore_long_term()

    def _restore(self) -> None:
        """把磁盘上已有的工作区登记回内存。

        以前只有内存字典：应用一重启，磁盘上的 ws_* 目录还在（文件一个没少），
        但 dev_* 工具一律回「找不到工作区」，工具创建流程就断在那里 ——
        模型只会拿着同一个 id 反复重试（真实事故：连续 5 次失败）。

        有 state.json 就按它回填状态；没有就把测试结果标为**未知**（None），
        绝不因为目录里有文件就推断「测试通过」。
        """
        try:
            entries = sorted(self.root_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or not _TASK_ID.match(entry.name):
                continue
            state = _read_state(entry)
            try:
                created = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)
            except OSError:
                created = datetime.now(timezone.utc)
            last_test_digest = state.get("last_test_digest")
            last_test_passed = state.get("last_test_passed")
            evidence_state = state.get("evidence_state")
            # 证据字段必须自洽：摘要形状不对、没有结论、状态词不认识，都回「没有证据」。
            if (
                evidence_state not in _EVIDENCE_STATES
                or not _is_digest(last_test_digest)
                or not isinstance(last_test_passed, bool)
            ):
                evidence_state = EVIDENCE_NONE
                last_test_digest = None
            task = DevTask(
                id=entry.name,
                request=_read_request(entry),
                dir=entry,
                created_at=str(state.get("created_at") or created.isoformat()),
                submitted=bool(state.get("submitted", False)),
                test_runs=int(state.get("test_runs", 0) or 0),
                phase=state.get("phase"),
                last_test_passed=last_test_passed if evidence_state != EVIDENCE_NONE else None,
                last_test_summary=state.get("last_test_summary"),
                last_test_at=state.get("last_test_at"),
                last_test_digest=last_test_digest,
                evidence_state=evidence_state,
                # 旧 schema（2）没有这两个键：读回就是 False / None —— 那正是
                # 「本改动之前写下的任务都没有被放弃」这个事实。
                abandoned=bool(state.get("abandoned", False)),
                abandoned_at=state.get("abandoned_at"),
                # 放弃即收回授权（契约 §2.5）：磁盘上若还留着一条授权记录，
                # 读回时也不认 —— 终态的不变量不能靠「文件恰好是干净的」维持。
                test_authorization=(
                    None
                    if state.get("abandoned")
                    else _read_authorization(state.get("test_authorization"))
                ),
                submitted_digest=state.get("submitted_digest"),
                submitted_at=state.get("submitted_at"),
            )
            if task.abandoned:
                # 终态的 phase 只有一个合法值：旧文件里若写着别的，按终态纠正。
                task.phase = ABANDONED_PHASE
            self._tasks[entry.name] = task
            # 磁盘内容可能被外部改过（包括越权的同权限子进程）：对不上就当证据过期。
            if (
                task.evidence_state == EVIDENCE_CURRENT
                and task.last_test_digest != self.content_digest(task.id)
            ):
                task.evidence_state = EVIDENCE_STALE
                _write_state(task)

    def _restore_long_term(self) -> None:
        """读回工作区级的长期授权；格式不对一律当作没有（不猜、不放宽）。"""
        try:
            raw = (self.root_dir / _LONG_TERM_FILE).read_text(encoding="utf-8")
        except OSError:
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, list):
            return
        records = []
        for item in data:
            record = _read_authorization(item)
            if not record or record.get("lifetime") != LIFETIME_LONG_TERM:
                continue
            owner = self._tasks.get(record.get("owner_task_id") or "")
            if owner is not None and owner.abandoned:
                # 长期授权在放弃的那一刻就被收回了（契约 §2.5）。磁盘上若还留着，
                # 读回时也不认 —— 否则「放弃」只收掉了任务级那一份，长期那份还在
                # 暗处对**别的任务**生效。
                continue
            records.append(record)
        self._long_term = records

    def _write_long_term(self) -> None:
        """落盘长期授权（尽力而为：写不进去不能让授权流程崩掉）。"""
        try:
            (self.root_dir / _LONG_TERM_FILE).write_text(
                json.dumps(self._long_term, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    # -- lifecycle --------------------------------------------------------

    def create(self, request: str) -> DevTask:
        task_id = f"ws_{uuid.uuid4().hex[:12]}"
        task_dir = self.root_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=False)
        (task_dir / "request.md").write_text(
            f"# 开发需求\n\n{request}\n", encoding="utf-8"
        )
        # 写入 tool.json 空模板：让模型一进工作区就知道契约长什么样，
        # 直接改写该文件即可（避免模型卡在"先看一下模板结构"无法继续）。
        (task_dir / "tool.json").write_text(
            "{\n"
            '  "name": "",\n'
            '  "description": "",\n'
            '  "tool_type": "function",\n'
            '  "sync": true,\n'
            '  "parameters": {},\n'
            '  "code": "",\n'
            '  "entry": "",\n'
            '  "requirements": [],\n'
            '  "tests": []\n'
            "}\n",
            encoding="utf-8",
        )
        task = DevTask(id=task_id, request=request, dir=task_dir)
        self._tasks[task_id] = task
        task.phase = "created"
        _write_state(task)
        return task

    def task(self, task_id: str) -> DevTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[DevTask]:
        """按创建时间列出全部开发任务（重启后仍可枚举）。"""
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

    # -- 活跃执行登记（进程内、不落盘）--------------------------------------

    def begin_run(
        self,
        task_id: str,
        kind: str,
        *,
        stopper: Callable[[], None] | None = None,
    ) -> dict:
        """登记「这个任务上正在跑一次执行」。`kind`：tests / submit。

        `stopper` 是**唯一**决定 `can_stop` 的东西，默认 `None`：谁真的留下了
        「只停止这一次执行」的句柄，谁才登记它。当前架构下没有这样的句柄 ——
        开发工具跑在主 turn 的协程里（`core/loop.py` 用 `asyncio.gather` 直接
        await，没有留单次工具调用的取消句柄），所以这里登记不到 stopper，
        `can_stop` 就是 `False`：**如实报告「没有这个能力」**，不是硬编码常量。
        将来有人真的把句柄接进来，`can_stop` 会自动变 True。

        登记是进程内事实：进程重启后没有任何东西在跑，所以不写 state.json。
        """
        run = {
            "kind": str(kind or ""),
            "stage": STAGE_PREPARING,
            "started_at": _now(),
            "stopper": stopper if callable(stopper) else None,
        }
        # 同一个任务同一时刻只有一次执行（工具调用是顺序的）：后来者覆盖前者，
        # 免得登记表里留着一个永远不结束的僵尸运行。
        self._runs[task_id] = run
        return self._run_view(run)

    def set_run_stage(self, task_id: str, stage: str) -> None:
        """推进当前执行的阶段：preparing / waiting_approval / executing。

        没有登记在跑的执行时**什么都不做** —— 不凭空造一个运行出来（那会让
        「正在执行」变成假事实）。不认识的状态词同样不写：登记表里只放真话。
        """
        run = self._runs.get(task_id)
        if run is None or stage not in _RUN_STAGES:
            return
        run["stage"] = stage

    def end_run(self, task_id: str, kind: str) -> None:
        """结束登记。只结束**同一 kind** 的那一次：迟到的 `end_run("tests")`
        不能把另一个正在跑的 submit 登记抹掉。"""
        run = self._runs.get(task_id)
        if run is None or run.get("kind") != str(kind or ""):
            return
        self._runs.pop(task_id, None)

    def active_run(self, task_id: str) -> dict | None:
        """当前登记的运行（没有就是 None）。

        返回 `{"kind", "stage", "started_at", "can_stop"}`：`can_stop` 由登记里
        有没有 stopper 算出来，调用方不需要（也不允许）自己硬编码。
        """
        run = self._runs.get(task_id)
        if run is None:
            return None
        return self._run_view(run)

    @staticmethod
    def _run_view(run: dict) -> dict:
        return {
            "kind": run.get("kind") or "",
            "stage": run.get("stage") or "",
            "started_at": run.get("started_at") or "",
            "can_stop": callable(run.get("stopper")),
        }

    # -- 放弃开发（不可逆终态）---------------------------------------------

    def is_abandoned(self, task_id: str) -> bool:
        """这个任务是否已经放弃（工具层守卫用的唯一判定）。"""
        task = self._tasks.get(task_id)
        return bool(task is not None and task.abandoned)

    def abandon_readiness(self, task_id: str) -> dict:
        """放弃之前先算清楚：现在允许放弃吗？不允许的话是哪一种。

        规则顺序即优先级（契约 §2.4）：
        不存在 → 已经放弃（幂等成功）→ 已提交（拒绝）→ 正在执行（拒绝）→
        正在等审批（**允许**，等审批不等于在执行）→ 其余（允许）。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return self._readiness(
                ABANDON_NOT_FOUND,
                allowed=False,
                message=f"找不到这个开发任务：{task_id}",
            )
        if task.abandoned:
            return self._readiness(
                ABANDON_ALREADY,
                allowed=True,
                message="这个开发任务已经放弃过了，不用再放弃一次。",
            )
        if task.submitted:
            return self._readiness(
                ABANDON_SUBMITTED,
                allowed=False,
                message=(
                    "这个任务已经做完、工具已经注册。放弃开发只处理没做完的任务；"
                    "要移除已注册的工具，请用撤销工具那条路径。"
                ),
            )
        run = self.active_run(task_id)
        if run is not None and run["stage"] == STAGE_EXECUTING:
            # 「正在执行」与「正在等审批」必须分开：执行中放弃会留下一个
            # 还在写工作区的孤儿执行，所以这里明确拒绝，并如实说明能力。
            message = (
                "这个任务正在执行（正在跑测试或正在提交）。"
                + (
                    "请先停止当前执行，再放弃开发。"
                    if run["can_stop"]
                    else "现在没有只停止这一个任务的能力：请先停止当前执行，再放弃开发。"
                )
            )
            return self._readiness(
                ABANDON_RUNNING, allowed=False, run=run, message=message
            )
        if run is not None and run["stage"] == STAGE_WAITING_APPROVAL:
            # 等审批 ≠ 在执行：这时放弃是允许的。未决审批由接口层先作废
            # （approvals.invalidate_for_task），等待方会收到 cancelled 而不是异常。
            return self._readiness(
                ABANDON_OK,
                allowed=True,
                run=run,
                message=(
                    "这个任务正在等待你的确认：放弃会同时作废这次确认，"
                    "不会执行生成代码。"
                ),
            )
        # 其余（没有执行登记，或登记还停在 preparing）：允许放弃。
        # 有登记就把登记一并报出去（准备阶段也是事实），但它是「可以放弃」的一支。
        return self._readiness(
            ABANDON_OK,
            allowed=True,
            run=run,
            message=(
                "可以放弃这个开发任务：放弃后不会再执行、不会再注册工具；"
                "工作区文件与记录保留。"
            ),
        )

    @staticmethod
    def _readiness(
        status: str,
        *,
        allowed: bool,
        message: str,
        run: dict | None = None,
    ) -> dict:
        return {
            "status": status,
            "allowed": bool(allowed),
            "can_stop": bool(run.get("can_stop")) if run else False,
            "run": (
                {
                    "kind": run.get("kind") or "",
                    "stage": run.get("stage") or "",
                    "started_at": run.get("started_at") or "",
                }
                if run
                else None
            ),
            "message": message,
        }

    def abandon(self, task_id: str) -> dict:
        """放弃一项没做完的开发任务（调用方必须先过 `abandon_readiness`）。

        终态不可逆：`abandoned=True` + `abandoned_at` + `phase="abandoned"`，
        并收回这个任务的执行授权（含它建立的长期授权）。不删目录、不删文件、
        不清测试证据、不删已注册工具。

        这里**再判一次**前置条件（不只信调用方）：并发/迟到调用进来时，
        已提交或正在执行的任务同样一个字都不改。
        """
        readiness = self.abandon_readiness(task_id)
        status = readiness["status"]
        if status == ABANDON_ALREADY:
            return {
                "status": ABANDON_ALREADY,
                "ok": True,
                "revoked": False,
                "message": readiness["message"],
            }
        if not readiness["allowed"]:
            return {
                "status": status,
                "ok": False,
                "revoked": False,
                "message": readiness["message"],
            }
        task = self._tasks[task_id]
        task.abandoned = True
        task.abandoned_at = _now()
        task.phase = ABANDONED_PHASE
        # 复用既有的收回语义：任务级 + 这个任务建立的长期授权一起收回。
        revoked = self.revoke_test_authorization(task_id)
        _write_state(task)
        return {
            "status": ABANDON_DONE,
            "ok": True,
            "revoked": revoked,
            "message": (
                "已放弃这个开发任务：不会再执行、不会再注册工具；"
                "工作区文件与记录保留，已注册的工具不受影响。"
            ),
        }

    def status(self, task_id: str) -> dict:
        """任务的权威状态快照（给工具/界面/恢复用）。"""
        task = self._tasks.get(task_id)
        if task is None:
            return {}
        files = self.list_files(task_id)
        digest = self.content_digest(task_id)
        # 兜底重算：文件可能在工具之外被改（例如同权限子进程），
        # 只要摘要对不上，这条测试证据就不再算数。
        evidence_state = task.evidence_state
        if evidence_state == EVIDENCE_CURRENT and task.last_test_digest != digest:
            evidence_state = EVIDENCE_STALE
        authorization_state = _authorization_state(
            _read_authorization(task.test_authorization), task=task, requested=None
        )
        return {
            "id": task.id,
            "request": task.request,
            "created_at": task.created_at,
            "phase": task.phase,
            "submitted": task.submitted,
            "test_runs": task.test_runs,
            "last_test_passed": task.last_test_passed,
            "last_test_summary": task.last_test_summary,
            "last_test_at": task.last_test_at,
            "last_test_digest": task.last_test_digest,
            "evidence_state": evidence_state,
            # 「现在就能拿测试通过当结论吗」：只有对应当前内容的证据才算数。
            "test_evidence_current": evidence_state == EVIDENCE_CURRENT,
            "submitted_digest": task.submitted_digest,
            "submitted_at": task.submitted_at,
            # 放弃开发（不可逆终态）：界面的「未完成」视图按 !submitted && !abandoned 过滤。
            "abandoned": bool(task.abandoned),
            "abandoned_at": task.abandoned_at,
            # 有没有「在这个环境里跑它的测试」的授权（范围与生命周期见 authorizations()）。
            # 只有仍在期限内的记录才算（任务提交后任务级授权即结束；一次性授权
            # 用过就没了；旧格式记录必须重新确认）。
            "test_authorized": authorization_state == AUTH_VALID,
            "authorization_state": authorization_state,
            "authorization_lifetime": (task.test_authorization or {}).get("lifetime", ""),
            "files": files,
            "content_digest": digest,
        }

    def cleanup(self, task_id: str) -> None:
        """删掉工作区目录（只用于明确要丢弃的临时工作区）。"""
        task = self._tasks.pop(task_id, None)
        if task is not None:
            shutil.rmtree(task.dir, ignore_errors=True)

    def archive(self, task_id: str) -> Path | None:
        """把已完成的项目复制成不可变记录。

        提交成功后不再删除工作区：项目文件、需求、测试证据与状态都是
        「已完成任务」的一部分，重启、更新与修复都要靠它。这里额外留一份
        只读快照，保证之后在工作区里的继续改动不会覆盖已提交版本。
        """
        task = self._tasks.get(task_id)
        if task is None or task.abandoned:
            # 已放弃 = 终态：不复制、不建 archive 目录（契约 §2.2）。
            return None
        target = self.root_dir / "archive" / task_id
        try:
            target.mkdir(parents=True, exist_ok=True)
            for path in sorted(task.dir.rglob("*")):
                if not path.is_file():
                    continue
                dest = target / path.relative_to(task.dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        except OSError:
            return None
        return target

    def mark_submitted(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        # 已放弃的任务不能被「迟到的提交成功」复活成已提交。
        if task is not None and not task.abandoned:
            task.submitted = True
            task.phase = "submitted"
            task.submitted_at = _now()
            task.submitted_digest = self.content_digest(task_id)
            _write_state(task)

    def record_test(self, task_id: str, passed: bool, summary: str) -> None:
        """记录一次测试的权威结果（通过/失败 + 摘要 + 时刻 + 被测内容摘要）。

        证据绑定「被测内容」：只记「测试通过了」而不记跑的是哪一版，内容一改
        旧结论就变成了假证据（真实缺口：测试通过后把代码改成错的，任务列表
        仍然显示测试通过）。
        """
        task = self._tasks.get(task_id)
        # 已放弃 = 终态：迟到的测试结果既不能复活任务，也不能清掉 abandoned。
        if task is None or task.abandoned:
            return
        task.test_runs += 1
        task.last_test_passed = bool(passed)
        task.last_test_summary = summary
        task.last_test_at = _now()
        task.last_test_digest = self.content_digest(task_id)
        task.evidence_state = EVIDENCE_CURRENT
        task.phase = "testing_passed" if passed else "testing_failed"
        _write_state(task)

    # -- 执行生成代码的授权 ------------------------------------------------

    def grant_test_authorization(
        self,
        task_id: str,
        *,
        policy_fingerprint: str,
        executor: str,
        scope: dict | None = None,
        lifetime: str = DEFAULT_LIFETIME,
        content_digest: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        """记下用户「可以在这个环境里跑生成代码」的确认，**带明确生命周期**。

        记录里逐字段绑定这次执行的身份：能力策略指纹、执行环境、目录范围、
        网络范围、凭据范围、被测内容摘要。任何一个变了、或者现在要的范围比
        授权过的更大，这条记录就不再算数（见 `_authorization_state`）。

        `lifetime` 只接受三值之一，没有默认的「永久」：

        * `once`      —— 本次执行，放行的同时就用掉，且绑定内容摘要；
        * `task`      —— 当前开发任务，任务提交即结束；
        * `long_term` —— 长期（跨任务），只存进工作区级的长期授权表，
                         并且必须由用户显式选择（调用方负责不擅自升级）。

        `scope` 仍是给用户看的那份说明（能力 / 目录 / 网络 / 凭据引用），
        它与上面的绑定字段同源，不另造一套语义。
        """
        if lifetime not in _LIFETIMES:
            raise ValueError(f"unknown authorization lifetime: {lifetime!r}")
        task = self._tasks.get(task_id)
        # 已放弃的任务不再接受任何授权：否则「放弃后又被迟到审批授权」会让
        # 这个终态在语义上复活（下一次执行又会拿到授权）。
        if task is None or task.abandoned:
            return
        display = dict(scope or {})
        record = {
            "policy_fingerprint": str(policy_fingerprint or ""),
            "executor": str(executor or ""),
            "at": _now(),
            "lifetime": lifetime,
            "content_digest": str(content_digest or ""),
            "filesystem": _str_list(display.get("filesystem")),
            "network": bool(display.get("network")),
            "network_allow": _str_list(display.get("network_allow")),
            "credentials": _str_list(display.get("credentials")),
            "owner_task_id": task.id,
        }
        if expires_at:
            record["expires_at"] = str(expires_at)
        if display:
            record["scope"] = display
        if lifetime == LIFETIME_LONG_TERM:
            # 长期授权不绑某个任务的寿命：放在工作区级记录里，跨任务可见。
            self._long_term = [
                item for item in self._long_term if item.get("owner_task_id") != task.id
            ]
            self._long_term.append(record)
            self._write_long_term()
            return
        task.test_authorization = record
        _write_state(task)

    def authorization_records(self) -> list[dict]:
        """当前**还在有效期概念内**的授权记录（含长期），按授权时间倒序。

        这里不隐藏「已经不算数」的记录：界面要能说清「你同意过什么、现在还算不算」，
        所以每条都带 `state` 与理由。
        """
        rows: list[dict] = []
        for task in self.list_tasks():
            record = _read_authorization(task.test_authorization)
            if not record:
                continue
            rows.append(self._authorization_row(record, task=task))
        for record in self._long_term:
            rows.append(
                self._authorization_row(
                    record, task=self._tasks.get(record.get("owner_task_id") or "")
                )
            )
        rows.sort(key=lambda row: row["granted_at"], reverse=True)
        return rows

    def _authorization_row(self, record: dict, *, task) -> dict:
        scope = record.get("scope") if isinstance(record.get("scope"), dict) else {}
        lifetime = record.get("lifetime") or ""
        return {
            "task_id": task.id if task is not None else record.get("owner_task_id") or None,
            "owner_task_id": record.get("owner_task_id") or "",
            "request": task.request[:200] if task is not None else "",
            "submitted": bool(task.submitted) if task is not None else False,
            "executor": record.get("executor"),
            "isolated": bool(scope.get("isolated")) or record.get("executor") == "docker",
            "policy_fingerprint": record.get("policy_fingerprint"),
            "capabilities": list(scope.get("capabilities") or []),
            "filesystem": list(record.get("filesystem") or []),
            "network": bool(record.get("network")),
            "network_allow": list(record.get("network_allow") or []),
            "credentials": list(record.get("credentials") or []),
            "granted_at": record.get("at") or "",
            # 生命周期：本次执行 / 当前开发任务 / 长期授权（跨任务）
            "lifetime": lifetime,
            "lifetime_label": LIFETIME_LABELS.get(lifetime, "无法识别的旧授权"),
            "applies_to_all_tasks": lifetime == LIFETIME_LONG_TERM,
            "content_digest": record.get("content_digest") or "",
            "expires_at": record.get("expires_at") or "",
            "consumed_at": record.get("consumed_at") or "",
            # 现在还算不算数：不加 request 时按「这条记录本身是否仍然有效」判
            "state": _authorization_state(record, task=task, requested=None),
        }

    # 兼容旧名字：授权列表（接口直接把它交给前端）
    authorizations = authorization_records

    def revoke_test_authorization(self, task_id: str) -> bool:
        """收回这个任务的执行授权；返回是否真的收回了。

        收回之后下一次测试（或提交复测）会重新问用户一遍。已经注册的工具不受影响：
        它走的是注册审批，不是这条测试授权。

        如果这个任务还建立过**长期授权**，一并收回 —— 否则「收回」只收掉了
        任务级那一份，长期那一份还在暗处生效。
        """
        task = self._tasks.get(task_id)
        revoked = False
        if task is not None and task.test_authorization:
            task.test_authorization = None
            _write_state(task)
            revoked = True
        kept = [item for item in self._long_term if item.get("owner_task_id") != task_id]
        if len(kept) != len(self._long_term):
            self._long_term = kept
            self._write_long_term()
            revoked = True
        return revoked

    def consume_test_authorization(self, task_id: str) -> bool:
        """用掉一次「本次执行」的授权（只有 once 用得上）。

        语义：放行紧接着的这一次执行。用掉之后下一次必须重新确认 ——
        宁可多问一次，也不让一次性授权变成可以反复使用的长期授权。
        """
        task = self._tasks.get(task_id)
        record = _read_authorization(task.test_authorization) if task is not None else None
        # 已放弃的任务没有「用掉一次授权」这回事（授权在放弃时已经收回）。
        if task is None or task.abandoned or not record or record.get("consumed_at"):
            return False
        record["consumed_at"] = _now()
        task.test_authorization = record
        _write_state(task)
        return True

    def test_authorization_state(
        self, task_id: str, *, identity: dict | None = None, policy_fingerprint="", executor=""
    ) -> dict:
        """这次执行是否已有授权，以及**是哪一条、什么生命周期**放行的。

        返回 `{"state": ..., "lifetime": ..., "owner_task_id": ...}`：
        只有 `state == "valid"` 才算有授权；其余都是明确的不算数理由。
        长期授权对所有任务生效，所以除了任务自己的记录，还要看工作区级的长期表。
        """
        requested = dict(identity or {})
        if not requested:
            requested = {
                "policy_fingerprint": str(policy_fingerprint or ""),
                "executor": str(executor or ""),
            }
        task = self._tasks.get(task_id)
        candidates: list[tuple[dict, object]] = []
        if task is not None:
            own = _read_authorization(task.test_authorization)
            if own:
                candidates.append((own, task))
        for record in self._long_term:
            owner = self._tasks.get(record.get("owner_task_id") or "")
            candidates.append((record, owner))
        best = AUTH_NONE
        for record, owner in candidates:
            state = _authorization_state(record, task=owner, requested=requested)
            if state == AUTH_VALID:
                return {
                    "state": AUTH_VALID,
                    "lifetime": record.get("lifetime") or "",
                    "owner_task_id": record.get("owner_task_id") or task_id,
                    "executor": record.get("executor") or "",
                }
            if best == AUTH_NONE:
                best = state
        return {"state": best, "lifetime": "", "owner_task_id": "", "executor": ""}

    def test_authorized(
        self,
        task_id: str,
        *,
        policy_fingerprint: str = "",
        executor: str = "",
        identity: dict | None = None,
    ) -> bool:
        """这次执行是否已经被授权过（身份对得上、范围没有变大、生命周期未结束）。"""
        state = self.test_authorization_state(
            task_id,
            identity=identity,
            policy_fingerprint=policy_fingerprint,
            executor=executor,
        )
        return state["state"] == AUTH_VALID

    def set_phase(self, task_id: str, phase: str) -> None:
        task = self._tasks.get(task_id)
        # 已放弃 = 终态：阶段不能被任何迟到的调用改回去（哪怕写成 "abandoned"）。
        if task is None or task.abandoned:
            return
        task.phase = phase
        _write_state(task)

    # -- files ------------------------------------------------------------

    def _resolve(self, task_id: str, name: str) -> Path | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        rel_path = safe_rel_path(name)
        base = task.dir.resolve()
        path = (task.dir / rel_path).resolve()
        # 第二道闸：形状校验之外再看一次真实解析结果（符号链接也挡在这里）。
        if base not in path.parents:
            raise ValueError(f"path escapes workspace: {name!r}")
        return path

    def write_file(self, task_id: str, name: str, content: str) -> None:
        _check_not_reserved(safe_rel_path(name))
        task = self._tasks.get(task_id)
        if task is not None and task.abandoned:
            # 已放弃 = 终态：不落盘、不改内容摘要、不让证据复活。这里直接拒绝
            # （工具层在此之前已经给出明确失败，这里是最后一道闸）。
            raise ValueError("这个开发任务已经被放弃：不能再改工作区文件")
        path = self._resolve(task_id, name)
        if path is None:
            raise KeyError(f"workspace not found: {task_id}")
        if len(content) > MAX_FILE_SIZE:
            raise ValueError("file content too large")
        # 子目录按需创建：模型写 `pkg/util.py` 时不必先建目录。
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._refresh_evidence(task_id)

    def _refresh_evidence(self, task_id: str) -> None:
        """内容变了就让已有测试证据立即失效（并落盘，重启后不会复活）。"""
        task = self._tasks.get(task_id)
        if task is None or task.evidence_state != EVIDENCE_CURRENT:
            return
        if task.last_test_digest != self.content_digest(task_id):
            task.evidence_state = EVIDENCE_STALE
            _write_state(task)

    def read_file(self, task_id: str, name: str) -> str | None:
        path = self._resolve(task_id, name)
        if path is None or not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def list_files(self, task_id: str) -> list[str]:
        """工作区里的全部文件（相对 posix 路径，排序稳定）。

        以前只列顶层文件名：多文件项目一来，子目录里的模块就「看不见」，
        模型会以为文件丢了。`state.json` 是后端状态，不算项目文件。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return []
        return sorted(
            p.relative_to(task.dir).as_posix()
            for p in task.dir.rglob("*")
            if p.is_file() and p.name != _STATE_FILE
        )

    # -- content binding ---------------------------------------------------

    def content_digest(self, task_id: str) -> str | None:
        """工作区全部文件（不含 state.json）的内容摘要。

        审批 / 注册 / 测试证据都绑定这个摘要：内容变了，旧证据不能再用。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None
        digest = hashlib.sha256()
        try:
            paths = sorted(task.dir.rglob("*"))
        except OSError:
            return None
        for path in paths:
            if not path.is_file() or path.name == _STATE_FILE:
                continue
            rel = path.relative_to(task.dir).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    # -- definition helpers ----------------------------------------------

    def fact_for(self, task_id: str, tool_name: str) -> dict:
        """当前任务事实的机器可读快照（给 `core/turn_facts.py` 记账用）。

        报的是**操作之后**的状态：调用方在写完文件、跑完测试、提交完之后取一次，
        主循环据此知道「这一版到底验证到哪一步了」。取不到任务时返回空字典。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return {}
        status = self.status(task_id)
        definition = self.read_definition(task_id)
        # subagent 型工具没有确定性测试，不能算「还没有测试证据」。
        requires_tests = definition is None or definition.tool_type != "subagent"
        return {
            "dev_task": {
                "id": task.id,
                "tool_name": str(tool_name or ""),
                "phase": status.get("phase"),
                "version": status.get("content_digest"),
                "submitted": bool(status.get("submitted")),
                # 放弃是终态：主循环据此知道这个任务不能再继续做（也不会注册）。
                "abandoned": bool(status.get("abandoned")),
                "abandoned_at": status.get("abandoned_at"),
                "requires_tests": requires_tests,
                "test": {
                    "state": status.get("evidence_state") or EVIDENCE_NONE,
                    "passed": status.get("last_test_passed"),
                    "summary": status.get("last_test_summary"),
                },
            }
        }

    def write_definition(self, task_id: str, definition: ToolDefinition) -> None:
        self.write_file(
            task_id,
            "tool.json",
            json.dumps(definition.model_dump(), ensure_ascii=False, indent=2),
        )

    def project_files(self, task_id: str) -> dict[str, str]:
        """项目模块（相对路径 → 内容）。

        多文件项目里，`tool.json` 是清单、`request.md`/`state.json` 是后端记录，
        都不算项目模块 —— 定义里只带真正要跟着工具走的代码。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return {}
        files: dict[str, str] = {}
        for path in sorted(task.dir.rglob("*")):
            if not path.is_file() or path.name in _NON_PROJECT_NAMES:
                continue
            rel = path.relative_to(task.dir).as_posix()
            try:
                files[rel] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # 二进制 / 读不到的文件不进定义：宁可少带，也不写半个内容。
                continue
        return files

    def collect_definition(self, task_id: str) -> ToolDefinition | None:
        """清单 + 项目文件 = 可以注册的完整定义（注册后自包含）。"""
        definition = self.read_definition(task_id)
        if definition is None:
            return None
        definition.files = self.project_files(task_id)
        return definition

    def read_definition(self, task_id: str) -> ToolDefinition | None:
        raw = self.read_file(task_id, "tool.json")
        if raw is None:
            return None
        try:
            return ToolDefinition(**json.loads(raw))
        except Exception:
            return None
