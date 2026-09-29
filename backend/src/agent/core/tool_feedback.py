"""统一的工具结果反馈（公共执行层）。

无论原生工具调用还是文本兼容模式，工具结果最终都由 `AgentLoop` 回填成一条
`role="tool"` 的消息交给模型。以前这条消息只写 `ToolResult.content`：失败且
content 为空时，模型收到的是**空正文**，随后「我成功了」也照样被接受。

本模块是那条消息的唯一构造点，保证失败至少带上：
状态（成功/失败/取消）、错误类别、简短原因、是否可重试、调用标识，
以及脱敏、限长后的诊断详情。

设计边界（诚实声明）：
* 这里只负责「如实描述已经发生的事实」，不做任何成功/验证的判定；
* 诊断文本先经 `trace.redact` 脱敏再限长，避免密钥进入模型上下文或日志。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from agent.tools.base import ToolResult

# 状态语义：取消是独立语义，不能并进「失败」。
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_STATUS_LABELS = {
    STATUS_SUCCESS: "成功",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消",
}

# 诊断详情（stderr / 部分输出）写入模型消息前的字符上限。
DEFAULT_DIAGNOSTIC_LIMIT = 2000


class ErrorCategory(str, Enum):
    """失败类别：跨工具统一，供模型、界面、日志共用。"""

    ENVIRONMENT = "environment"                 # 运行环境不可用（解释器/容器缺失）
    STARTUP = "startup"                         # 进程未能启动
    CODE_ERROR = "code_error"                   # 生成代码抛异常
    ASSERTION = "assertion"                      # 断言/期望不匹配
    OUTPUT_FORMAT = "output_format"             # 输出不是约定格式
    MISSING_DEPENDENCY = "missing_dependency"   # 缺依赖
    PERMISSION = "permission"                   # 缺权限 / 未授权
    EXTERNAL_SERVICE = "external_service"       # 外部服务失败
    TIMEOUT = "timeout"                         # 超时
    CANCELLED = "cancelled"                     # 用户取消
    UNKNOWN = "unknown"


_CATEGORY_LABELS = {
    ErrorCategory.ENVIRONMENT: "运行环境不可用",
    ErrorCategory.STARTUP: "启动失败",
    ErrorCategory.CODE_ERROR: "代码异常",
    ErrorCategory.ASSERTION: "断言失败",
    ErrorCategory.OUTPUT_FORMAT: "输出格式错误",
    ErrorCategory.MISSING_DEPENDENCY: "缺少依赖",
    ErrorCategory.PERMISSION: "缺少权限或未授权",
    ErrorCategory.EXTERNAL_SERVICE: "外部服务失败",
    ErrorCategory.TIMEOUT: "超时",
    ErrorCategory.CANCELLED: "已取消",
    ErrorCategory.UNKNOWN: "未知错误",
}

# 哪些类别「换个参数/补充条件再试」有意义。环境/权限/缺依赖/取消默认不可重试：
# 不解决前置条件就重试只会空转。
_RETRYABLE = {
    ErrorCategory.CODE_ERROR,
    ErrorCategory.ASSERTION,
    ErrorCategory.OUTPUT_FORMAT,
    ErrorCategory.EXTERNAL_SERVICE,
    ErrorCategory.TIMEOUT,
    ErrorCategory.UNKNOWN,
}


def category_label(category: str) -> str:
    try:
        return _CATEGORY_LABELS[ErrorCategory(category)]
    except (ValueError, KeyError):
        return category


def classify_error(error: str | None) -> ErrorCategory:
    """按错误正文推断类别。工具自己给了 `category` 时应优先用它。"""
    text = (error or "").lower()
    if not text:
        return ErrorCategory.UNKNOWN
    if "已被取消" in text or "cancelled" in text or "canceled" in text:
        return ErrorCategory.CANCELLED
    if any(k in text for k in ("no module named", "modulenotfound", "importerror",
                               "missing dependency", "缺少依赖")):
        return ErrorCategory.MISSING_DEPENDENCY
    if any(k in text for k in ("timeout", "超时", "timed out")):
        return ErrorCategory.TIMEOUT
    if any(k in text for k in ("未授权", "没有权限", "permission denied",
                               "not permitted", "permissionerror", "未执行（")):
        return ErrorCategory.PERMISSION
    if any(k in text for k in ("没有可用的容器", "docker not available",
                               "interpreter", "解释器", "环境不可用", "executor")):
        return ErrorCategory.ENVIRONMENT
    if any(k in text for k in ("assertion", "断言", "mismatch")):
        return ErrorCategory.ASSERTION
    if any(k in text for k in ("json", "did not print", "输出格式", "schema")):
        return ErrorCategory.OUTPUT_FORMAT
    if any(k in text for k in ("connect", "http", "tls", "ssl", "502", "503",
                               "429", "external", "外部服务")):
        return ErrorCategory.EXTERNAL_SERVICE
    # 形如 "ValueError: ..." 的 Python 异常
    if "error:" in text or "exception" in text or "traceback" in text:
        return ErrorCategory.CODE_ERROR
    return ErrorCategory.CODE_ERROR


def resolve_category(result: ToolResult) -> ErrorCategory:
    if result.ok:
        return ErrorCategory.UNKNOWN
    if result.category:
        try:
            return ErrorCategory(result.category)
        except ValueError:
            pass
    return classify_error(result.error)


def is_recoverable(result: ToolResult) -> bool:
    if result.recoverable is not None:
        return result.recoverable
    return resolve_category(result) in _RETRYABLE


def status_of(result: ToolResult) -> str:
    if result.ok:
        return STATUS_SUCCESS
    if resolve_category(result) is ErrorCategory.CANCELLED:
        return STATUS_CANCELLED
    return STATUS_FAILED


def _redact_and_limit(text: str, limit: int) -> str:
    from agent.trace.redact import redact_text

    cleaned = redact_text(text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + f"\n…[诊断已截断，共 {len(cleaned)} 字符]"


def facts(result: ToolResult) -> dict[str, Any]:
    """结构化的结果事实（给事件/界面/日志用，与给模型的消息同源）。"""
    category = resolve_category(result) if not result.ok else None
    return {
        "status": status_of(result),
        "ok": bool(result.ok),
        "category": category.value if category else None,
        "category_label": category_label(category.value) if category else None,
        "recoverable": is_recoverable(result) if not result.ok else None,
        "reason": (result.error or "").strip() or None,
    }


def message(
    result: ToolResult,
    *,
    tool_name: str | None = None,
    call_id: str | None = None,
    diagnostic_limit: int = DEFAULT_DIAGNOSTIC_LIMIT,
) -> str:
    """把一次工具结果变成交给模型的正文。

    成功且正文非空时**原样返回 content**（保持既有行为与提示词契约）；
    其余情况给出结构化的中文事实，确保模型不会收到空正文。
    """
    label = tool_name or "工具"
    if result.ok:
        if (result.content or "").strip():
            return result.content
        return f"{label} 执行成功，但没有返回内容。"

    category = resolve_category(result)
    status = status_of(result)
    lines = [f"【工具结果】{label}"]
    if call_id:
        lines.append(f"call_id: {call_id}")
    lines.append(f"状态: {_STATUS_LABELS.get(status, status)}（{status}）")
    lines.append(f"错误类别: {category_label(category.value)}（{category.value}）")
    lines.append(f"原因: {(_redact_and_limit(result.error or '', 400) or '（没有给出说明）')}")
    lines.append(f"是否可重试: {'是' if is_recoverable(result) else '否'}")
    if (result.content or "").strip():
        lines.append("有效结果（部分输出）:")
        lines.append(_redact_and_limit(result.content, diagnostic_limit))
    return "\n".join(lines)
