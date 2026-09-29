"""Tool development workspace: sandboxed per-task directory.

The main agent develops tools inside a workspace (write code/tests, run
tests, iterate) before submitting for approval. Files are restricted to
the workspace directory; the definition contract reuses ToolDefinition.
"""

from __future__ import annotations

import json
import re
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent.tools.spec import ToolDefinition

MAX_FILE_SIZE = 200_000
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")
# 工作区 id 的形状（`DevWorkspace.create` 生成）：扫盘回填时只认它
_TASK_ID = re.compile(r"^ws_[0-9a-f]{12}$")
_REQUEST_MARKER = "# 开发需求"
# 开发任务状态文件（提交 / 测试 / 内容摘要）。放在工作区目录里，
# 与已有 `ws_*` 成果同源：重启后可以原样读回，不依赖内存表。
_STATE_FILE = "state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    """读回工作区状态；缺失或损坏时回空字典（调用方按「未知」处理）。"""
    try:
        raw = (task_dir / _STATE_FILE).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(task: "DevTask") -> None:
    """落盘任务状态（尽力而为：写不进去也不能让工具调用失败）。"""
    payload = {
        "id": task.id,
        "request": task.request,
        "created_at": task.created_at,
        "phase": task.phase,
        "submitted": task.submitted,
        "test_runs": task.test_runs,
        "last_test_passed": task.last_test_passed,
        "last_test_summary": task.last_test_summary,
        "last_test_at": task.last_test_at,
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
            self._tasks[entry.name] = DevTask(
                id=entry.name,
                request=_read_request(entry),
                dir=entry,
                created_at=str(state.get("created_at") or created.isoformat()),
                submitted=bool(state.get("submitted", False)),
                test_runs=int(state.get("test_runs", 0) or 0),
                phase=state.get("phase"),
                last_test_passed=state.get("last_test_passed"),
                last_test_summary=state.get("last_test_summary"),
                last_test_at=state.get("last_test_at"),
                submitted_digest=state.get("submitted_digest"),
                submitted_at=state.get("submitted_at"),
            )

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
            "submitted_digest": task.submitted_digest,
            "submitted_at": task.submitted_at,
            "files": files,
            "content_digest": self.content_digest(task_id),
        }

    def cleanup(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        if task is not None:
            import shutil

            shutil.rmtree(task.dir, ignore_errors=True)

    def mark_submitted(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            task.submitted = True
            task.phase = "submitted"
            task.submitted_at = _now()
            task.submitted_digest = self.content_digest(task_id)
            _write_state(task)

    def record_test(self, task_id: str, passed: bool, summary: str) -> None:
        """记录一次测试的权威结果（通过/失败 + 摘要 + 时刻）。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.test_runs += 1
        task.last_test_passed = bool(passed)
        task.last_test_summary = summary
        task.last_test_at = _now()
        task.phase = "testing_passed" if passed else "testing_failed"
        _write_state(task)

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
        if not _SAFE_NAME.match(name or ""):
            raise ValueError(f"unsafe file name: {name!r} (single file names only)")
        path = (task.dir / name).resolve()
        if task.dir.resolve() not in path.parents and path != task.dir.resolve():
            raise ValueError(f"path escapes workspace: {name!r}")
        return path

    def write_file(self, task_id: str, name: str, content: str) -> None:
        path = self._resolve(task_id, name)
        if path is None:
            raise KeyError(f"workspace not found: {task_id}")
        if len(content) > MAX_FILE_SIZE:
            raise ValueError("file content too large")
        path.write_text(content, encoding="utf-8")

    def read_file(self, task_id: str, name: str) -> str | None:
        path = self._resolve(task_id, name)
        if path is None or not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def list_files(self, task_id: str) -> list[str]:
        task = self._tasks.get(task_id)
        if task is None:
            return []
        return sorted(
            p.name
            for p in task.dir.iterdir()
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

    def write_definition(self, task_id: str, definition: ToolDefinition) -> None:
        self.write_file(
            task_id,
            "tool.json",
            json.dumps(definition.model_dump(), ensure_ascii=False, indent=2),
        )

    def read_definition(self, task_id: str) -> ToolDefinition | None:
        raw = self.read_file(task_id, "tool.json")
        if raw is None:
            return None
        try:
            return ToolDefinition(**json.loads(raw))
        except Exception:
            return None
