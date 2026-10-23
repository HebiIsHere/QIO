"""迁移 59（R9-H）：用户要求的结构化限制持久化（契约 §6）。

契约：docs/r9h-contracts.md §6。只追加，不改历史迁移。

为什么需要：

* R9-G 的 user_requirements.limits 是**展平文字**（每次修正追加一句话），引擎
  无法可靠地用文字判断"限制了什么对象/操作"，重启/审批恢复/后台继续后限制可能丢失。
* 本轮给每条要求保存结构化限制（JSON 数组），并显式区分三种状态：
  - structured：已由模型语义理解并核对（[] = 明确无限制）；
  - legacy：历史要求，结构化限制从未理解（NULL 表示**未知**，不是无限制）；
  - unresolved：尝试重新理解失败/无法可靠恢复（同样不得当作无限制）。
* structured_limits_source 记录理解依据（模型 + 提示词版本 / 错误码 / 恢复入口），
  不存推理全文、不含密钥原文。

幂等：追加列由 migrate.py 的 ADD COLUMN 预检保护；可被补偿迁移 60 重放。
"""

from __future__ import annotations

MIGRATION: tuple[int, list[str]] = (
    59,
    [
        # NULL = 历史未知；'[]' = 明确无限制；否则 JSON 数组
        "ALTER TABLE user_requirements ADD COLUMN structured_limits TEXT",
        # legacy / structured / unresolved（见契约 §6；默认 legacy 保证旧库不被误解为
        # "明确无限制"）
        "ALTER TABLE user_requirements ADD COLUMN structured_limits_state TEXT NOT NULL DEFAULT 'legacy'",
        # 理解依据：模型+提示词版本 / 错误码 / 恢复入口（不含推理全文）
        "ALTER TABLE user_requirements ADD COLUMN structured_limits_source TEXT",
    ],
)
