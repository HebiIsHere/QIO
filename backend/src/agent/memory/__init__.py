"""Memory domain: append-only transcript, fragments, summaries, index.

导出采用惰性解析：导入本包的子模块（例如 agent.memory.limits）不应连带导入
summary / ingest 等较重的模块，否则会与 knowledge 域形成循环导入。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "FragmentManager",
    "IndexBuilder",
    "MemoryWriter",
    "FragmentSummary",
    "summarize_fragment",
    "validate_summary_text",
]

_LAZY = {
    "FragmentManager": "agent.memory.fragment",
    "IndexBuilder": "agent.memory.index",
    "MemoryWriter": "agent.memory.ingest",
    "FragmentSummary": "agent.memory.summary",
    "summarize_fragment": "agent.memory.summary",
    "validate_summary_text": "agent.memory.summary",
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(target), name)
    globals()[name] = value
    return value
