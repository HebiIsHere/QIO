"""Tool development workspace: sandboxed per-task directory.

The main agent develops tools inside a workspace (write code/tests, run
tests, iterate) before submitting for approval. Files are restricted to
the workspace directory; the definition contract reuses ToolDefinition.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent.tools.spec import ToolDefinition

MAX_FILE_SIZE = 200_000
_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DevTask:
    id: str
    request: str
    dir: Path
    created_at: str = field(default_factory=_now)
    submitted: bool = False
    test_runs: int = 0


class DevWorkspace:
    """In-memory task registry + per-task sandbox directories."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, DevTask] = {}

    # -- lifecycle --------------------------------------------------------

    def create(self, request: str) -> DevTask:
        task_id = f"ws_{uuid.uuid4().hex[:12]}"
        task_dir = self.root_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=False)
        (task_dir / "request.md").write_text(
            f"# 开发需求\n\n{request}\n", encoding="utf-8"
        )
        task = DevTask(id=task_id, request=request, dir=task_dir)
        self._tasks[task_id] = task
        return task

    def task(self, task_id: str) -> DevTask | None:
        return self._tasks.get(task_id)

    def cleanup(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        if task is not None:
            import shutil

            shutil.rmtree(task.dir, ignore_errors=True)

    def mark_submitted(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            task.submitted = True

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
        return sorted(p.name for p in task.dir.iterdir() if p.is_file())

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
