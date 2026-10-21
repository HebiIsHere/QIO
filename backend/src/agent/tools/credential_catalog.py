"""list_available_credentials 工具（阶段四 A；契约 §14.3）。

给 QIO 在发起子任务前**自主选择**适用凭据与模型：返回 credential_ref / provider /
models / capabilities / availability / usage_limits，以及模型凭据与业务服务凭据的
区分。**永不返回密钥原文**；模型不能凭目录内容绕过政策（选择与校验在
agent.credentials.selection 与 SubagentTool 里执行）。

参数 schema 由执行模型（pydantic）生成；说明文案只引用 prompts.py。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent import prompts
from agent.credentials import selection
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult
from agent.tools.contracts import json_schema_for

MAX_DIRECTORY_ENTRIES = 64


class ListAvailableCredentialsArgs(BaseModel):
    """按用途列出可用凭据目录（默认子任务用途）。"""

    model_config = ConfigDict(extra="forbid")

    purpose: Literal["subagent", "model", "service", "all"] = Field(
        default="subagent",
        description="用途：subagent=可用于子任务的模型凭据；model=全部模型凭据；"
        "service=业务服务凭据（搜索等）；all=全部。",
    )


class ListAvailableCredentialsTool(Tool):
    name = "list_available_credentials"
    description = str(
        prompts.TOOL_DESCRIPTIONS.get("list_available_credentials")
        or "列出可用于指定任务的凭据配置引用、模型、能力、可用状态和使用限制，不返回密钥。"
    )
    parameters = json_schema_for(ListAvailableCredentialsArgs)
    arg_model = ListAvailableCredentialsArgs
    is_concurrency_safe = True

    def __init__(self, credentials, *, settings_store: Any = None) -> None:
        self.credentials = credentials
        self.settings_store = settings_store

    async def run(self, **kwargs: Any) -> ToolResult:
        try:
            args = ListAvailableCredentialsArgs.model_validate(kwargs)
        except Exception as exc:  # noqa: BLE001 - 参数校验失败如实返回
            return ToolResult(ok=False, error=f"参数不合法：{exc}")
        try:
            entries = selection.list_available_credentials(
                self.credentials,
                purpose=args.purpose,
                settings_store=self.settings_store,
            )
        except selection.CredentialSelectionError as exc:
            return ToolResult(ok=False, error=str(exc))
        entries = entries[:MAX_DIRECTORY_ENTRIES]
        model_entries = [item for item in entries if item.get("kind") == "model"]
        service_entries = [item for item in entries if item.get("kind") == "service"]
        data = {
            "purpose": args.purpose,
            "credentials": entries,
            "model_credentials": model_entries,
            "service_credentials": service_entries,
            "count": len(entries),
            "secrets_included": False,
            "note": "目录只含元数据；credential_ref 用于 _qio_subtask，实际密钥由程序取用。",
        }
        return ToolResult(
            ok=True,
            status="completed",
            code="ok",
            content=json_dumps(data),
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            data=data,
        )


def json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
