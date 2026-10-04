"""测试前授权：执行 AI 生成的代码之前，先拿到用户对**执行边界**的确认。

为什么需要这一层：`dev_run_tests` 会把模型刚写出来的代码真的跑起来。以前这条
路径没有任何授权 —— 只要模型想测，代码就执行了；而审批（本来是为「执行生成
工具」准备的）只发生在**注册之后**。顺序错了：先执行，后确认。

这里的规则（见 docs/status.md「工具开发规范与可靠性修复」第一阶段）：

* 第一次在某个任务上执行生成代码之前，必须拿到一次确认；
* 说明必须说准：这是 AI 生成的代码、在什么环境里跑（有容器就是容器；
  没有就是**同权限子进程，不是安全沙箱**）、声明了哪些能力、不注入凭据；
* 拒绝 / 超时 / 拿不到审批服务 → **不执行**，返回一条明确的失败，
  而不是「先跑了再说」。

授权本身是**有生命周期、有明确身份**的（第二阶段收敛，取代「用户不撤销就
永久有效」）：

* 生命周期只有三种：`once` 本次执行 / `task` 当前开发任务（任务提交即结束）/
  `long_term` 长期授权（跨任务，必须由用户**显式选择**，不会自动升级）。
  **刻意不设「默认 24 小时」这种拍出来的时长**：有效期只由这三条生命周期与
  身份绑定决定；只有审批明确带回到期时刻时才记 `expires_at`；
* 身份逐字段绑定：任务、能力策略指纹、执行环境、目录范围、网络范围、凭据范围、
  被测内容摘要。任何一项变了、或者现在要的范围比授权过的更大，旧授权一律不算数
  —— 这就是「环境、策略、范围变化后，旧授权不得自动扩大」；
* 「本次执行」在放行的那一刻就用掉，并且只覆盖被批准的那一版内容（绑定内容摘要）。

能力说明用的是运行路径同一份策略（`policy.default_policy_for`），不另造一套。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from agent.tools.approval_present import describe_tool_call
from agent.tools.base import ToolResult
from agent.tools.dev_workspace import (
    AUTH_VALID,
    DEFAULT_LIFETIME,
    LIFETIME_LABELS,
    LIFETIME_LONG_TERM,
    LIFETIME_ONCE,
    LIFETIME_TASK,
)
from agent.tools.policy import (
    default_policy_for,
    isolation_label,
    policy_fingerprint,
    unprotected_surfaces,
)

# 复用既有的「工具执行」审批通道：界面上「想在受限环境里运行这个工具的测试」
# 这条描述本来就在 tools/approval_present.py 里登记好了。
APPROVAL_KIND = "tool_execution"
TEST_TOOL_NAME = "dev_run_tests"
_DEFAULT_TIMEOUT_SECONDS = 10.0


class GrantLifetime(str, Enum):
    """授权的生命周期：本次执行 / 当前开发任务 / 长期授权。"""

    ONCE = LIFETIME_ONCE
    TASK = LIFETIME_TASK
    LONG_TERM = LIFETIME_LONG_TERM


LIFETIME_CHOICES = (GrantLifetime.ONCE, GrantLifetime.TASK, GrantLifetime.LONG_TERM)

# 用户在界面上没选时按哪一种记：任务级（现有 UX：同一个任务问一次，任务结束
# 即失效）。它不是「永久」—— 任务提交、被收回、策略 / 环境 / 范围变化都会让它
# 失效；要跨任务长期有效必须用户显式选 long_term。
DEFAULT_LIFETIME_CHOICE = GrantLifetime(DEFAULT_LIFETIME)


@dataclass(frozen=True)
class TestBoundary:
    """这次执行的真实边界（用户看到的说明与机器可读字段同源）。"""

    executor: str
    isolated: bool
    timeout_seconds: float
    credentials_simulated: bool
    policy_fingerprint: str
    capabilities: list[str]
    lifetime: str = DEFAULT_LIFETIME


@dataclass(frozen=True)
class ExecutionIdentity:
    """这次要执行的东西的**真实身份**：授权逐字段绑定的就是它。

    判定用的 `as_request()`、审批载荷里的 `execution_identity`、落盘记录里的
    字段是同一份数据 —— 用户看到的、系统记住的、之后拿来判定的不是三套说法。
    """

    task_id: str
    policy_fingerprint: str
    executor: str
    filesystem: tuple[str, ...]
    network: bool
    network_allow: tuple[str, ...]
    credentials: tuple[str, ...]
    content_digest: str

    def as_request(self) -> dict:
        return {
            "task_id": self.task_id,
            "policy_fingerprint": self.policy_fingerprint,
            "executor": self.executor,
            "filesystem": list(self.filesystem),
            "network": self.network,
            "network_allow": list(self.network_allow),
            "credentials": list(self.credentials),
            "content_digest": self.content_digest,
        }


def execution_identity(
    *,
    task_id: str,
    definition: Any,
    executor: str,
    content_digest: str | None,
) -> ExecutionIdentity:
    """按运行路径同一份策略算出这次执行的身份（测试与运行不各算一套）。

    策略带上**真实执行器**：隔离等级因此进指纹 —— 容器没了 / 换回受限子进程
    都属于「执行环境变了」，旧授权不得沿用。
    """
    policy = default_policy_for(definition, executor=executor)
    return ExecutionIdentity(
        task_id=str(task_id or ""),
        policy_fingerprint=policy_fingerprint(policy),
        executor=str(executor or ""),
        filesystem=tuple(str(p) for p in policy.filesystem),
        network=bool(policy.network),
        network_allow=tuple(str(p) for p in policy.network_allow),
        credentials=tuple(str(c) for c in policy.credentials),
        content_digest=str(content_digest or ""),
    )


def lifetime_from_decision(decision: Any) -> GrantLifetime:
    """用户在审批里选的生命周期。

    没选、选了不认识的值、或者载荷形状不对 → 回默认（任务级），**不会**因为
    一个说不清的字段就升级成长期授权。
    """
    for source in (
        getattr(decision, "overrides", None),
        getattr(decision, "scope", None),
    ):
        if not isinstance(source, dict):
            continue
        raw = source.get("lifetime", source.get("grant_lifetime"))
        if raw is None:
            continue
        try:
            return GrantLifetime(str(raw))
        except ValueError:
            continue
    return DEFAULT_LIFETIME_CHOICE


def expires_at_from_decision(decision: Any) -> str | None:
    """审批明确带回的到期时刻（可选）。

    只有「能解析、带时区、且在未来」的 ISO 8601 才被接受：这里不发明时长，
    也不接受一个已经过期的时刻当成授权。
    """
    for source in (
        getattr(decision, "overrides", None),
        getattr(decision, "scope", None),
    ):
        if not isinstance(source, dict):
            continue
        raw = source.get("expires_at")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            moment = datetime.fromisoformat(raw.strip())
        except ValueError:
            continue
        if moment.tzinfo is None:
            continue
        if moment <= datetime.now(timezone.utc):
            continue
        return moment.isoformat()
    return None


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
    # 终态优先：已放弃的任务**不发起审批、不执行**。判定放在最前面 ——
    # 不能先看「有没有旧授权」，否则一条还没收回的授权会让放弃后的执行继续跑。
    if workspaces is not None and task_id and _is_abandoned(workspaces, task_id):
        return _blocked(
            "这个开发任务已经被放弃：不会执行生成代码，也不会注册工具。"
            "要重做请新建开发任务。"
        )
    executor, timeout = await _resolved_executor(sandbox)
    policy = default_policy_for(definition, executor=executor)
    identity = execution_identity(
        task_id=task_id,
        definition=definition,
        executor=executor,
        content_digest=_content_digest(workspaces, task_id),
    )
    if workspaces is not None and task_id:
        state = workspaces.test_authorization_state(
            task_id, identity=identity.as_request()
        )
        if state.get("state") == AUTH_VALID:
            # 「本次执行」在放行的这一刻就用掉：它授权的是一次执行，不是一段可以
            # 反复使用的时间。宁可下次再问一次，也不让它悄悄变成长期授权。
            if state.get("lifetime") == LIFETIME_ONCE:
                workspaces.consume_test_authorization(
                    state.get("owner_task_id") or task_id
                )
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
        policy_fingerprint=identity.policy_fingerprint,
        capabilities=policy.describe(),
    )
    # 授权范围：用户看到的说明、落盘的记录、之后能查到的范围是**同一份**数据。
    scope = _display_scope(identity=identity, boundary=boundary)
    decision = await approvals.request(
        APPROVAL_KIND,
        _approval_payload(
            task_id=task_id,
            definition=definition,
            boundary=boundary,
            scope=scope,
            identity=identity,
        ),
    )
    if getattr(decision, "decision", None) != "approved":
        return _blocked(_refusal_text(getattr(decision, "decision", "unknown")))
    if workspaces is not None and task_id:
        workspaces.grant_test_authorization(
            task_id,
            policy_fingerprint=identity.policy_fingerprint,
            executor=identity.executor,
            scope=scope,
            lifetime=lifetime_from_decision(decision).value,
            content_digest=identity.content_digest,
            expires_at=expires_at_from_decision(decision),
        )
    return None


def _content_digest(workspaces, task_id: str) -> str:
    """被测内容的摘要（拿不到就回空串：空摘要不会被当成「覆盖了这一版」）。"""
    if workspaces is None or not task_id:
        return ""
    try:
        return str(workspaces.content_digest(task_id) or "")
    except Exception:  # noqa: BLE001 - 取摘要失败不能让授权流程挂掉
        return ""


def _is_abandoned(workspaces, task_id: str) -> bool:
    """任务是否已放弃（拿不到判定就按「没放弃」——但调用方另有工具层守卫）。"""
    try:
        return bool(workspaces.is_abandoned(task_id))
    except Exception:  # noqa: BLE001 - 判定失败不能让授权流程挂掉
        return False


def _display_scope(*, identity: ExecutionIdentity, boundary: TestBoundary) -> dict:
    """给用户看的授权范围：与绑定字段同源，不另写一份说明。"""
    return {
        "executor": identity.executor,
        "isolated": boundary.isolated,
        "capabilities": list(boundary.capabilities),
        "filesystem": list(identity.filesystem),
        "network": identity.network,
        "network_allow": list(identity.network_allow),
        "credentials": list(identity.credentials),
    }


def _refusal_text(decision: str) -> str:
    if decision == "rejected":
        return (
            "你拒绝了这次执行：不会运行这段 AI 生成的代码。"
            "要跑测试（或提交时的复测）得先得到你的确认。"
        )
    if decision == "timeout":
        return "等待确认超时：这次没有执行生成代码。需要你确认之后才能跑测试。"
    if decision == "cancelled":
        # 「作废」不是「用户拒绝」：这条路径来自放弃开发（或本轮已停止）。
        # 说成「你拒绝了」会误导用户，让他以为是自己点的。
        return (
            "这次没有执行：这个开发任务已经被放弃，或本轮已停止"
            "（等待中的确认已作废，不是你的拒绝）。"
        )
    return f"这次没有执行生成代码（授权结论：{decision}）。"


def _blocked(message: str) -> ToolResult:
    return ToolResult(ok=False, error=message, category="permission", recoverable=False)


def _lifetime_detail(lifetime: str) -> str:
    label = LIFETIME_LABELS.get(lifetime, lifetime)
    if lifetime == LIFETIME_ONCE:
        return f"授权时长：{label}（只放行紧接着的这一次，且只覆盖当前这一版内容）"
    if lifetime == LIFETIME_LONG_TERM:
        return (
            f"授权时长：{label}（必须由你显式选择；之后其它开发任务不再重复询问）"
        )
    return f"授权时长：{label}（这个任务还在开发中就一直有效，任务提交后失效）"


def _approval_payload(
    *,
    task_id: str,
    definition: Any,
    boundary: TestBoundary,
    scope: dict,
    identity: ExecutionIdentity,
) -> dict:
    """给用户看的审批内容：说的必须是这次执行的**真实**边界与生命周期。"""
    described = describe_tool_call(TEST_TOOL_NAME, {"workspace": task_id})
    # 执行边界只有一个来源（tools/policy.py）：受限子进程不会被叫成安全沙箱。
    environment = isolation_label(boundary.executor)
    limits = unprotected_surfaces(boundary.executor)
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
        "声明的依赖：%s（只装这些；第一次测试时会另行征求安装许可）" % "、".join(requirements)
        if requirements
        else "声明的依赖：无（只用标准库）"
    )
    detail.append(
        "凭据：不注入真实凭据，凭据相关的分支本次是模拟的"
        if boundary.credentials_simulated
        else "凭据：本次没有用到凭据"
    )
    # 边界要说全：没有强制隔离时，「没有保护什么」必须和「能做什么」一样清楚。
    if limits:
        detail.append("这个执行环境**不**保护：" + "；".join(limits))
    # 授权不是「一句话生效到永远」：说清有效期怎么结束、怎么收回。
    detail.append(_lifetime_detail(boundary.lifetime))
    detail.append(
        "收回：随时可以在授权列表里收回；策略、执行环境、目录 / 网络 / 凭据范围"
        "或被测内容变化后，旧授权一律不再算数，会重新征求你的确认"
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
            "需要你确认上面的执行边界与授权时长；确认之后这个任务的测试不再重复问你。"
        ),
        "access": access,
        "detail": "\n".join(detail),
        # 能力说明用运行路径同一份策略（policy.describe），不是另造一套
        "capabilities": boundary.capabilities,
        # 这次执行的身份（逐字段绑定，判定用的就是它）
        "execution_identity": identity.as_request(),
        # 这次执行**没有**被保护的面（空列表 = 容器路径，容器本身是边界）
        "limits": list(limits),
        "isolation": {
            "executor": boundary.executor,
            "isolated": boundary.isolated,
            "label": environment,
        },
        # 可选的生命周期（界面可以让用户改成「只这一次」或「长期」；
        # 不选就是这里的默认值，不会自动升级）
        "grant": {
            "lifetime": boundary.lifetime,
            "lifetime_label": LIFETIME_LABELS.get(boundary.lifetime, boundary.lifetime),
            "options": [
                {"value": item.value, "label": LIFETIME_LABELS.get(item.value, item.value)}
                for item in LIFETIME_CHOICES
            ],
        },
        "code_boundary": {
            "task_id": task_id,
            "executor": boundary.executor,
            "isolated": boundary.isolated,
            "policy_fingerprint": boundary.policy_fingerprint,
            "credentials_simulated": boundary.credentials_simulated,
            "lifetime": boundary.lifetime,
            "content_digest": identity.content_digest,
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
