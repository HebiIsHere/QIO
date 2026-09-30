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
_STATE_SCHEMA = 2
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


def _read_authorization(raw: object) -> dict | None:
    """读回「执行生成代码」的授权记录；形状不对就当作没有授权。"""
    if not isinstance(raw, dict):
        return None
    fingerprint = raw.get("policy_fingerprint")
    executor = raw.get("executor")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    if not isinstance(executor, str) or not executor:
        return None
    return {
        "policy_fingerprint": fingerprint,
        "executor": executor,
        "at": str(raw.get("at") or ""),
    }


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
    if data.get("schema") != _STATE_SCHEMA or data.get("source") != _STATE_SOURCE:
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
    # 用户的「执行生成代码」授权记录：绑在（能力策略指纹 + 实际执行环境）上，
    # 任一项变了就要重新确认（见 dev_auth.py）。
    test_authorization: dict | None = None
    submitted_digest: str | None = None
    submitted_at: str | None = None


class DevWorkspace:
    """In-memory task registry + per-task sandbox directories."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, DevTask] = {}
        self._restore()

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
                test_authorization=_read_authorization(state.get("test_authorization")),
                submitted_digest=state.get("submitted_digest"),
                submitted_at=state.get("submitted_at"),
            )
            self._tasks[entry.name] = task
            # 磁盘内容可能被外部改过（包括越权的同权限子进程）：对不上就当证据过期。
            if (
                task.evidence_state == EVIDENCE_CURRENT
                and task.last_test_digest != self.content_digest(task.id)
            ):
                task.evidence_state = EVIDENCE_STALE
                _write_state(task)

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
        if task is None:
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
        if task is not None:
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
        if task is None:
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
        self, task_id: str, *, policy_fingerprint: str, executor: str
    ) -> None:
        """记下用户「可以在这个环境里跑这个任务的生成代码」的确认。

        绑的是（能力策略指纹 + 实际执行环境），不是内容摘要：测试本来就是
        「改一版、跑一次」的循环，绑内容会让每次迭代都重新弹窗。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.test_authorization = {
            "policy_fingerprint": str(policy_fingerprint or ""),
            "executor": str(executor or ""),
            "at": _now(),
        }
        _write_state(task)

    def test_authorized(
        self, task_id: str, *, policy_fingerprint: str, executor: str
    ) -> bool:
        """这次执行是否已经在授权范围内（策略与执行环境都对得上）。"""
        task = self._tasks.get(task_id)
        record = task.test_authorization if task is not None else None
        if not record:
            return False
        return (
            record.get("policy_fingerprint") == str(policy_fingerprint or "")
            and record.get("executor") == str(executor or "")
        )

    def set_phase(self, task_id: str, phase: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
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
