"""Runaway guard: detect repeated failing tool calls and escalate.

Thresholds follow mainstream agent practice (e.g. nanobot):
    same call (tool+args) failing: 2 -> WARN, 5 -> BLOCK
    same tool failing overall:     3 -> WARN, 8 -> HALT
This is the safety net that lets the iteration ceiling be raised to 128
without letting a stuck model burn the whole budget on one broken action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class GuardVerdict(str, Enum):
    OK = "ok"
    WARN = "warn"
    BLOCK = "block"
    HALT = "halt"


@dataclass
class RunawayGuard:
    call_warn: int = 2
    call_block: int = 5
    tool_warn: int = 3
    tool_halt: int = 8
    _call_fails: dict[str, int] = field(default_factory=dict)
    _tool_fails: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def _key(tool: str, arguments: dict) -> str:
        return tool + "::" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)

    def observe(self, tool: str, arguments: dict, ok: bool) -> GuardVerdict:
        if ok:
            return GuardVerdict.OK
        key = self._key(tool, arguments)
        self._call_fails[key] = self._call_fails.get(key, 0) + 1
        self._tool_fails[tool] = self._tool_fails.get(tool, 0) + 1
        if self._tool_fails[tool] >= self.tool_halt:
            return GuardVerdict.HALT
        if self._call_fails[key] >= self.call_block:
            return GuardVerdict.BLOCK
        if self._tool_fails[tool] >= self.tool_warn:
            return GuardVerdict.WARN
        if self._call_fails[key] >= self.call_warn:
            return GuardVerdict.WARN
        return GuardVerdict.OK
