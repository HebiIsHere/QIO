"""测试前授权：执行 AI 生成的代码之前，先拿到用户对**执行边界**的确认。

为什么需要这一层：`dev_run_tests` 会把模型刚写出来的代码真的跑起来。以前这条
路径没有任何授权 —— 只要模型想测，代码就执行了；而审批（本来是为「执行生成
工具」准备的）只发生在**注册之后**。顺序错了：先执行，后确认。

这里的规则（见 docs/status.md「工具开发规范与可靠性修复」第一阶段）：

* 第一次在某个任务上执行生成代码之前，必须拿到一次确认；确认按
  （能力策略指纹 + 实际执行环境）记在工作区里，之后同任务不再打扰用户；
* 说明必须说准：这是 AI 生成的代码、在什么环境里跑（有容器就是容器；
  没有就是**同权限子进程，不是安全沙箱**）、声明了哪些能力、不注入凭据；
* 拒绝 / 超时 / 拿不到审批服务 → **不执行**，返回一条明确的失败，
  而不是「先跑了再说」。

能力说明用的是运行路径同一份策略（`policy.default_policy_for`），不另造一套。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.tools.approval_present import describe_tool_call
from agent.tools.base import ToolResult
from agent.tools.policy import default_policy_for, policy_fingerprint

# 复用既有的「工具执行」审批通道：界面上「想在受限环境里运行这个工具的测试」
# 这条描述本来就在 tools/approval_present.py 里登记好了。
APPROVAL_KIND = "tool_execution"
TEST_TOOL_NAME = "dev_run_tests"
_DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class TestBoundary:
    """这次执行的真实边界（用户看到的说明与机器可读字段同源）。"""

    executor: str
    isolated: bool
    timeout_seconds: float
    credentials_simulated: bool
    policy_fingerprint: str
    capabilities: list[str]


async def ensure_test_authorization(
    *,
    workspaces,
    approvals,
    sandbox,
    task_id: str,
    definition: Any,
) -> ToolResult | None:
    """拿到「可以执行这个任务的生成代码」的授权。

    返回 None = 已授权（可以继续执行）；否则返回一条失败结果，调用方必须**不执行**。
    """
    executor, timeout = await _resolved_executor(sandbox)
    policy = default_policy_for(definition)
    fingerprint = policy_fingerprint(policy)
    if (
        workspaces is not None
        and task_id
        and workspaces.test_authorized(
            task_id, policy_fingerprint=fingerprint, executor=executor
        )
    ):
        return None

    if approvals is None:
        return _blocked(
            "没有可用的审批服务，所以拿不到你的授权：这次没有执行 AI 生成的代码。"
            "请在应用里正常使用工具开发流程（它会弹出确认）。"
        )

    boundary = TestBoundary(
        executor=executor,
        isolated=executor == "docker",
        timeout_seconds=timeout,
        credentials_simulated=bool(getattr(definition, "credential_ref", None)),
        policy_fingerprint=fingerprint,
        capabilities=policy.describe(),
    )
    # 授权范围：用户看到的说明、落盘的记录、之后能查到的范围是**同一份**数据。
    scope = {
        "executor": executor,
        "isolated": boundary.isolated,
        "capabilities": list(boundary.capabilities),
        "filesystem": list(policy.filesystem),
        "network": bool(policy.network),
        "network_allow": list(policy.network_allow),
        "credentials": list(policy.credentials),
    }
    decision = await approvals.request(
        APPROVAL_KIND,
        _approval_payload(
            task_id=task_id, definition=definition, boundary=boundary, scope=scope
        ),
    )
    if getattr(decision, "decision", None) != "approved":
        return _blocked(_refusal_text(getattr(decision, "decision", "unknown")))
    if workspaces is not None and task_id:
        workspaces.grant_test_authorization(
            task_id,
            policy_fingerprint=fingerprint,
            executor=executor,
            scope=scope,
        )
    return None


def _refusal_text(decision: str) -> str:
    if decision == "rejected":
        return (
            "你拒绝了这次执行：不会运行这段 AI 生成的代码。"
            "要跑测试（或提交时的复测）得先得到你的确认。"
        )
    if decision == "timeout":
        return "等待确认超时：这次没有执行生成代码。需要你确认之后才能跑测试。"
    return f"这次没有执行生成代码（授权结论：{decision}）。"


def _blocked(message: str) -> ToolResult:
    return ToolResult(ok=False, error=message, category="permission", recoverable=False)


def _approval_payload(
    *, task_id: str, definition: Any, boundary: TestBoundary, scope: dict
) -> dict:
    """给用户看的审批内容：说的必须是这次执行的**真实**边界。"""
    described = describe_tool_call(TEST_TOOL_NAME, {"workspace": task_id})
    environment = (
        "Docker 容器（隔离执行）"
        if boundary.isolated
        else "受限子进程（同一用户权限，不是安全沙箱）"
    )
    detail = [
        f"执行环境：{environment}（{boundary.executor}）",
        "工作目录：每次调用一个一次性临时目录",
        f"超时：{boundary.timeout_seconds:g} 秒，超时会结束这棵进程树",
        "生成代码：%s" % (getattr(definition, "name", "") or "（未命名）"),
    ]
    # 多文件项目与依赖也属于「这次到底要跑什么」的一部分：说清项目里有几个文件、
    # 声明了哪些第三方依赖（本机不会自动安装，缺了会明确报错）。
    files = getattr(definition, "files", None) or {}
    detail.append(f"项目文件：{len(files)} 个（另有入口代码）")
    requirements = list(getattr(definition, "requirements", None) or [])
    detail.append(
        "声明的依赖：%s（不会自动安装；缺哪个会明确报出来）" % "、".join(requirements)
        if requirements
        else "声明的依赖：无（只用标准库）"
    )
    detail.append(
        "凭据：不注入真实凭据，凭据相关的分支本次是模拟的"
        if boundary.credentials_simulated
        else "凭据：本次没有用到凭据"
    )
    access = [f"执行环境：{environment}"] + list(described.get("access") or [])
    return {
        "tool": TEST_TOOL_NAME,
        "arguments": {"workspace": task_id},
        "workspace": task_id,
        "generated_tool": getattr(definition, "name", None),
        **described,
        "explanation": (
            "这是一段 AI 生成的代码。第一次在这个任务上执行它之前，"
            "需要你确认上面的执行边界；确认之后这个任务的测试不再重复问你。"
        ),
        "access": access,
        "detail": "\n".join(detail),
        # 能力说明用运行路径同一份策略（policy.describe），不是另造一套
        "capabilities": boundary.capabilities,
        "code_boundary": {
            "task_id": task_id,
            "executor": boundary.executor,
            "isolated": boundary.isolated,
            "policy_fingerprint": boundary.policy_fingerprint,
            "credentials_simulated": boundary.credentials_simulated,
            # 与落盘的授权范围同源：批准的就是这一份
            "scope": scope,
        },
    }


async def _resolved_executor(sandbox) -> tuple[str, float]:
    """实际会用哪个执行器、超时多久（拿不到就按最保守的受限子进程记）。"""
    if sandbox is None:
        return "subprocess", _DEFAULT_TIMEOUT_SECONDS
    try:
        executor = await sandbox.effective_executor()
    except Exception:  # noqa: BLE001 - 说明用途，失败不能让授权流程挂掉
        executor = str(getattr(sandbox, "executor", "") or "subprocess")
    timeout = getattr(sandbox, "timeout_seconds", None) or _DEFAULT_TIMEOUT_SECONDS
    return str(executor), float(timeout)
