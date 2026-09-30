"""用量归因：把模型调用的 token 记到「这次用的是哪把钥匙」上。

为什么单独一个模块：主循环、子 agent、后台维护（做梦分析 / 工具草稿）都在跑
模型调用，而每条凭据各自带「用量上限」。归因逻辑必须只有一份，否则迟早出现
「主循环记了、子任务没记」这种半吊子，上限照样不准。

契约：

* 记的是**真实调用**的用量（进 / 出分开），不是估算；
* 归因不了就**不记**（adapter 没有 key_id 时返回 None）——宁少记，不记错；
* 写库失败绝不能影响对话本身（sink 内部吞掉异常并记日志）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def credential_usage_sink(
    credentials: Any, adapter: Any
) -> Callable[[int, int], None] | None:
    """返回「把一次调用的进/出记到 adapter 所属凭据上」的回调；无法归因时 None。

    `credentials` 只用 `record_usage` 一个方法，测试里可以用替身。
    """
    key_id = getattr(adapter, "key_id", None)
    if not key_id:
        return None

    def sink(input_tokens: int, output_tokens: int) -> None:
        try:
            credentials.record_usage(
                key_id, input_tokens=input_tokens, output_tokens=output_tokens
            )
        except Exception:  # noqa: BLE001 - 记账失败不得打断正在进行的回答
            logger.warning("credential usage accounting failed", exc_info=True)

    return sink
