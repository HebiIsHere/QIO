"""统一授权边界（R9）。

本包是**所有真实操作入口**共享的权限判定与执行边界（契约 §2）：
文件工具、命令、脚本、自建工具、依赖安装、子智能体与子进程都必须走
:class:`agent.security.gateway.AuthorizationGateway`，不允许各自再实现一套。

模块划分（契约 §2.1、§13）：

* :mod:`modes`         —— 三种权限模式与旧值映射；
* :mod:`effects`       —— Effect / Target / Verdict / AuthDecision 公共类型；
* :mod:`context`       —— 每轮指令、子智能体范围、审批来源的上下文传播；
* :mod:`instructions`  —— 指令授权（grant 随指令关闭，绝不跨指令继承）；
* :mod:`intent`        —— 从用户原话确定性提取授权（保守，绝不接收模型声明）；
* :mod:`engine`        —— 三模式判定引擎；
* :mod:`gateway`       —— 统一入口协议；
* :mod:`operations`    —— 可恢复的待执行操作、审批票据与执行器注册表；
* :mod:`internal_data` —— QIO 内部运行数据的受保护路径与三次确认要求；
* :mod:`audit`         —— 判定审计（可选）；
* :mod:`tempfiles`     —— 有生命周期的解析/写回临时文件。

安全表述必须诚实：这里的判定不等于操作系统沙箱；执行层约束（Low IL /
Job Object）只能降低风险，不能声称隔离（见契约 §3.2）。
"""

from agent.security.effects import (  # noqa: F401
    AuthDecision,
    Effect,
    EffectKind,
    Target,
    Verdict,
)
from agent.security.modes import (  # noqa: F401
    DEFAULT_MODE,
    LEGACY_MODE_MAP,
    MODE_LABELS,
    PermissionMode,
    is_valid_mode_value,
    label_for,
    normalize_mode,
)

__all__ = [
    "AuthDecision",
    "DEFAULT_MODE",
    "Effect",
    "EffectKind",
    "LEGACY_MODE_MAP",
    "MODE_LABELS",
    "PermissionMode",
    "Target",
    "Verdict",
    "is_valid_mode_value",
    "label_for",
    "normalize_mode",
]
