"""用量归因与调用预算：把每一次**实际发出的请求**记到「用的那把钥匙」上。

为什么单独一个模块：主循环、子任务、后台维护（做梦分析 / 工具草稿）、派生提炼
（摘要 / 知识 / 实体 / 压缩）都在跑模型调用，而每条凭据各自带「用量上限」。
归因逻辑必须只有一份，否则迟早出现「主循环记了、后台提炼没记」这种半吊子，
上限照样不准。

契约（C8）：

* ``record_request_usage(store, key_id, input_tokens, output_tokens, *, incomplete=False)``
  是**唯一**的入账入口：进 / 出分开记，一次实际请求记一次（含内部重试的每次响应）；
* 记的是**真实调用**的用量（进 / 出分开），不是估算；
* 归因不了就**不记**（adapter 没有 key_id 时返回 None）——宁少记，不记错；
* 无用量失败**标 incomplete、不造数**：绝不写一个猜出来的 token 数进账本；
* 写库失败绝不能影响对话本身（内部吞掉异常并记日志）。

调用预算（M09）：

* 每次**实际请求前**核对累计用量（``ensure_adapter_request_allowed``），耗尽抛
  ``BudgetExhausted``；主循环 / 子任务 / 维护 / 后台提炼 / 内部重试同一规则；
* 耗尽后停止新增调用并给简短真实原因，**不静默换配置、不绕过用户上限**；
* 本轮输出预算（``core/budget.py`` 的 ``token_budget``）与凭据累计用量上限是两套
  语义，各自独立，互不覆盖。

并发调用规则（诚实说明，不做零误差账单承诺）：

* 「核对」发生在每次尝试**紧邻发送之前**，中间没有 ``await``；「登记」发生在响应
  回来时。因此已经发出去的请求不会被预算勒停（它已经计费了），预算归零那一刻
  在途的请求数最多是同一 adapter 上的并发调用数；
* 一旦某次响应被登记后 ``remaining <= 0``，**不会再有新的请求发出**；
* 没有预算（``budget is None``）与预算耗尽（``remaining <= 0``）是两种不同状态：
  前者不拦，后者拦（见 ``credentials/policy.py::remaining_budget``）。
"""

from __future__ import annotations

import logging
import weakref
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 没有用量可入账时的简短真实原因（不造数，只如实标记）。
INCOMPLETE_NO_USAGE = "没有用量数据"


def _redact(text: str) -> str:
    """日志/错误文本进任何输出之前先过统一脱敏（不得出现密钥原文）。"""
    try:
        from agent.trace.redact import redact_text

        return redact_text(text)
    except Exception:  # noqa: BLE001 - 脱敏不可用时宁可不带原文
        return ""


def _non_negative(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


@dataclass(frozen=True)
class UsageReceipt:
    """一次实际请求的记账回执：用量记了什么、有没有记不完整。

    这是「用量记录问题」的独立表达 —— 它不改变这次请求本身的执行结果。
    """

    key_id: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    recorded: bool
    incomplete: bool
    reason: str | None = None

    @property
    def known(self) -> bool:
        """这次请求有没有可入账的真实用量。"""
        return self.recorded


# -- 唯一入账入口 ---------------------------------------------------------


def record_request_usage(
    store: Any,
    key_id: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    *,
    incomplete: bool = False,
    reason: str | None = None,
) -> UsageReceipt:
    """把**一次实际请求**的用量记到 ``key_id`` 这把凭据上。

    * 有已知用量（进 + 出 > 0）→ 写进凭据账本（``store.record_usage``）；
    * 没有用量且 ``incomplete=True``（失败/供应商没给）→ **不写任何 token 数**，
      只如实标记这次请求不完整（回执 + 日志；``store`` 若提供可选扩展点
      ``note_incomplete_usage(key_id, reason)`` 就一并通知它）；
    * 没有用量且 ``incomplete=False``（成功但供应商没报用量）→ 不写、也不标不完整；
    * 写入失败只记日志：**记账绝不能打断正在进行的回答**。
    """
    key = str(key_id) if key_id else None
    in_tokens = _non_negative(input_tokens)
    out_tokens = _non_negative(output_tokens)
    total = in_tokens + out_tokens
    note = _redact(reason) if reason else None

    if key is None:
        # 归因不了就不记：不把用量记到别人头上。
        return UsageReceipt(None, in_tokens, out_tokens, total, False, bool(incomplete), note)

    recorded = False
    if total > 0:
        try:
            store.record_usage(key, input_tokens=in_tokens, output_tokens=out_tokens)
            recorded = True
        except Exception:  # noqa: BLE001 - 记账失败不得打断请求
            logger.warning("credential usage accounting failed", exc_info=True)
            return UsageReceipt(
                key, in_tokens, out_tokens, total, False, bool(incomplete),
                note or "记账写入失败",
            )

    if incomplete:
        # 不造数：这里不写任何 token，只把「这次请求没能完整入账」表达出来。
        hook = getattr(store, "note_incomplete_usage", None)
        if callable(hook):
            try:
                hook(key, note or INCOMPLETE_NO_USAGE)
            except Exception:  # noqa: BLE001 - 扩展点失败同样不得打断请求
                logger.warning("credential incomplete-usage note failed", exc_info=True)
        logger.warning(
            "request usage incomplete for %s: %s", key, note or INCOMPLETE_NO_USAGE
        )

    return UsageReceipt(key, in_tokens, out_tokens, total, recorded, bool(incomplete), note)


# -- adapter 绑定：每次实际请求的自动归因 ---------------------------------

# 进程内默认凭据库（弱引用）：主循环 / 子任务 / 维护以外的直接 adapter 调用
# （例如 onboarding 追问）也要能归因。弱引用保证应用关掉后不会留着一个死账本。
_DEFAULT_STORE: "weakref.ReferenceType[Any] | None" = None


def set_default_accounting_store(store: Any) -> None:
    """登记进程内默认凭据库（由 ``CredentialPolicy`` 构造时调用）。"""
    global _DEFAULT_STORE
    try:
        _DEFAULT_STORE = weakref.ref(store) if store is not None else None
    except TypeError:  # 不支持弱引用的替身（测试用 dict 之类）：退化为不强引用
        _DEFAULT_STORE = None


def default_accounting_store() -> Any | None:
    if _DEFAULT_STORE is None:
        return None
    return _DEFAULT_STORE()


@dataclass
class RequestAccounting:
    """一条 adapter ↔ 凭据的记账绑定（含可观察计数）。"""

    store: Any
    key_id: str
    enforce_budget: bool = True
    requests: int = 0
    recorded: int = 0
    incomplete: int = 0
    _notes: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "requests": self.requests,
            "recorded": self.recorded,
            "incomplete": self.incomplete,
        }


def bind_request_accounting(
    adapter: Any,
    store: Any,
    key_id: str | None = None,
    *,
    enforce_budget: bool = True,
) -> RequestAccounting | None:
    """把「这条 adapter 的每次实际请求都算在 key_id 上」绑定到 adapter 实例。

    归因不了（没有 key_id 或没有凭据库）时返回 None —— 宁少记，不记错。
    """
    key = key_id or getattr(adapter, "key_id", None)
    if not key or store is None:
        return None
    binding = RequestAccounting(
        store=store, key_id=str(key), enforce_budget=bool(enforce_budget)
    )
    try:
        adapter._request_accounting = binding
    except Exception:  # noqa: BLE001 - 绑定不了就不记账，绝不打断请求
        return None
    return binding


def request_accounting(adapter: Any, *, allow_default: bool = True) -> RequestAccounting | None:
    """取这条 adapter 的记账绑定；显式绑定优先，其次进程默认凭据库。

    默认库只用于**明确声明 ``accounts_requests`` 的真实 adapter**：测试里的
    鸭子类型替身永远走调用方给的 ``usage_sink``，不会被隐式接管。
    """
    binding = getattr(adapter, "_request_accounting", None)
    if binding is not None:
        return binding
    if not allow_default or not getattr(adapter, "accounts_requests", False):
        return None
    key = getattr(adapter, "key_id", None)
    store = default_accounting_store()
    if not key or store is None:
        return None
    return bind_request_accounting(adapter, store, key)


def adapter_self_accounts(adapter: Any) -> bool:
    """这条 adapter 是否**自己**按请求记账（上层就不该再记一次）。"""
    if not getattr(adapter, "accounts_requests", False):
        return False
    return request_accounting(adapter) is not None


def accounting_snapshot(adapter: Any) -> dict[str, Any] | None:
    """这条 adapter 的记账计数（没有绑定就是 None）。用于展示与测试。"""
    binding = request_accounting(adapter)
    return binding.snapshot() if binding is not None else None


def ensure_adapter_request_allowed(adapter: Any) -> float | None:
    """实际请求**紧邻发送之前**核对累计用量；耗尽抛 ``BudgetExhausted``。

    核对本身失败（账本读不到）时**放行**：这是「记账不打断对话」的同一原则 ——
    宁可漏拦一次，也不能因为账本故障让用户完全用不了。
    """
    binding = request_accounting(adapter)
    if binding is None or not binding.enforce_budget:
        return None
    from agent.credentials.policy import BudgetExhausted, ensure_budget_available

    try:
        return ensure_budget_available(binding.store, binding.key_id)
    except BudgetExhausted:
        raise
    except Exception:  # noqa: BLE001 - 账本故障不得变成「请求失败」
        logger.warning("credential budget check failed; allowing the request", exc_info=True)
        return None


def account_adapter_request(
    adapter: Any,
    usage: Any = None,
    *,
    failed: bool = False,
    reason: str | None = None,
) -> UsageReceipt | None:
    """把**这次响应**的真实用量归因入账（由 adapter 在每次实际请求后调用）。

    ``usage`` 是统一语义的 ``ModelUsage``（可以是 None）；``failed=True`` 表示这次
    请求失败（例如解析失败 / 传输错误 / 没有响应）。

    三种情形的语义：

    * 有真实用量（进 + 出 > 0）→ 记一次，回执 ``recorded=True``；这次请求的记账
      是**完整**的（请求成不成功是另一件事 —— 失败由异常/调用方表达）；
    * 完全没有用量数据（``usage is None``）→ 标 ``incomplete``，**不写任何 token**：
      无论是失败（``failed=True``）还是成功但供应商没报用量；
    * 有用量对象但没有正向 token（例如供应商报 0/0）→ 没东西可写，不算不完整。

    没有绑定就返回 None（不记账、不报错）。
    """
    binding = request_accounting(adapter)
    if binding is None:
        return None
    binding.requests += 1
    in_tokens = _non_negative(getattr(usage, "input_tokens", 0)) if usage is not None else 0
    out_tokens = _non_negative(getattr(usage, "output_tokens", 0)) if usage is not None else 0
    known = (in_tokens + out_tokens) > 0
    incomplete = (not known) and usage is None
    if incomplete and reason is None:
        reason = f"请求失败且{INCOMPLETE_NO_USAGE}" if failed else INCOMPLETE_NO_USAGE
    receipt = record_request_usage(
        binding.store,
        binding.key_id,
        in_tokens,
        out_tokens,
        incomplete=incomplete,
        reason=reason,
    )
    if receipt.recorded:
        binding.recorded += 1
    if receipt.incomplete:
        binding.incomplete += 1
    return receipt


def account_adapter_failure(
    adapter: Any, exc: BaseException | None = None, *, reason: str | None = None
) -> UsageReceipt | None:
    """失败且没有用量可用时的记账：标 incomplete，不造数。"""
    if reason is None and exc is not None:
        reason = f"{type(exc).__name__}: {_redact(str(exc))[:160]}"
    return account_adapter_request(adapter, None, failed=True, reason=reason)


# -- 向后兼容：现有调用方用的 sink ----------------------------------------


class CredentialUsageSink:
    """按凭据记账的 sink（可调用对象）。

    之所以是一个对象而不是裸函数：它知道自己是**为哪条 adapter** 建的。真实 adapter
    已经在请求层记过账（含内部重试），上层就不该再用这个 sink 记第二遍 ——
    身份判断交给调用方（见 ``covers_requests``），而不是靠「谁看起来像 sink」。
    """

    def __init__(self, credentials: Any, adapter: Any, key_id: str) -> None:
        self.store = credentials
        self.adapter = adapter
        self.key_id = key_id
        self.binding = bind_request_accounting(
            adapter, credentials, key_id, enforce_budget=True
        )

    @property
    def covers_requests(self) -> bool:
        """这条 adapter 是否已经自己按请求记账（此时上层再记就是双记）。"""
        return bool(getattr(self.adapter, "accounts_requests", False) and self.binding is not None)

    def __call__(self, input_tokens: int, output_tokens: int) -> UsageReceipt:
        return record_request_usage(self.store, self.key_id, input_tokens, output_tokens)


def credential_usage_sink(
    credentials: Any, adapter: Any
) -> CredentialUsageSink | None:
    """返回「把一次调用的进/出记到 adapter 所属凭据上」的回调；无法归因时 None。

    兼容性（不破坏既有调用方）：

    * 传进来的 ``credentials`` 只要求有 ``record_usage(key_id, input_tokens=, output_tokens=)``；
    * 返回值仍是可调用对象：直接调用它（既有测试与自定义接线）照样会入账一次；
    * 副作用是把这条 adapter 绑定到该凭据：真实 adapter 从此在**每次实际请求**
      （含内部重试的每次响应、后台提炼的直接调用）自己记账，上层就不必再记 ——
      这正是「迁移到统一入口、消除双记」的落点（``covers_requests``）。

    鸭子类型的测试替身 adapter 不带 ``accounts_requests``，因此不会被自动接管：
    上层仍按老路径调用这个 sink。
    """
    key_id = getattr(adapter, "key_id", None)
    if not key_id:
        return None
    return CredentialUsageSink(credentials, adapter, str(key_id))
