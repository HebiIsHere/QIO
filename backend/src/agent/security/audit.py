"""授权判定审计（契约 §2.3「每次判定记录审计」，§5 审计表之外的轻量记录）。

审批状态迁移本身写入 approval_audit / approval_history（见 storage/approvals.py，
由 Lead 冻结的 schema 提供）。这里只保存**判定级**审计：哪条指令、什么对象、
什么动作、什么模式、结果如何 —— 不新增表，不阻塞主流程，不记录密钥原文。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque

from agent.security.effects import AuthDecision, Effect

logger = logging.getLogger(__name__)


def _redact(text: object) -> str:
    try:
        from agent.trace.redact import redact_text

        return redact_text(str(text or ""))
    except Exception:  # noqa: BLE001 - 脱敏失败时宁可少写
        return "[redacted]"


@dataclass
class AuditEntry:
    at: str
    mode: str
    effect_kind: str
    targets: list[str]
    verdict: str
    reason: str
    instruction_id: str | None = None
    purpose: str = ""
    scope_id: str | None = None
    tool: str | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "at": self.at,
            "mode": self.mode,
            "effect_kind": self.effect_kind,
            "targets": list(self.targets),
            "verdict": self.verdict,
            "reason": self.reason,
            "instruction_id": self.instruction_id,
            "purpose": self.purpose,
            "scope_id": self.scope_id,
            "tool": self.tool,
            "detail": self.detail,
        }


class AuthorizationAudit:
    """有界内存审计（最近 N 条）；同时写一条 debug 日志供 Trace/诊断查看。"""

    def __init__(self, max_entries: int = 300) -> None:
        self._entries: Deque[AuditEntry] = deque(maxlen=max(1, int(max_entries)))

    def record(
        self,
        *,
        effect: Effect,
        decision: AuthDecision,
        mode: str,
        instruction_id: str | None = None,
        detail: dict | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            at=datetime.now(timezone.utc).isoformat(),
            mode=str(mode),
            effect_kind=effect.kind.value,
            targets=[_redact(t.value)[:300] for t in effect.targets],
            verdict=decision.verdict.value,
            reason=_redact(decision.reason)[:300],
            instruction_id=instruction_id,
            purpose=_redact(effect.purpose)[:200],
            scope_id=decision.scope_id,
            tool=effect.tool,
            detail=dict(detail or {}),
        )
        self._entries.append(entry)
        logger.debug(
            "authorization verdict=%s kind=%s mode=%s instruction=%s targets=%s",
            entry.verdict,
            entry.effect_kind,
            entry.mode,
            entry.instruction_id,
            entry.targets,
        )
        return entry

    def recent(self, limit: int = 50) -> list[dict]:
        count = max(1, int(limit))
        return [entry.as_dict() for entry in list(self._entries)[-count:]]

    def clear(self) -> None:
        self._entries.clear()
