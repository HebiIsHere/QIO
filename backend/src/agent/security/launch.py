"""统一受控启动入口（R9-F W2，契约 §3.3）。

所有真实程序/脚本/shell 启动都要么走本模块的 `ControlledLauncher.launch`，要么复用
本模块的**同一套**事前约束与隔离/核验组件（`prepare`/`apply_isolation`/`postflight`）。
这里**不判权限**：能不能做由 AuthorizationEngine / 用户审批决定；本模块只保证
「决定之后，真正启动的东西仍是决定时的那个东西，并且执行边界与决定时一致」。

## 事前约束（不是事后提醒）

1. **spec_digest**：规范化 spec（程序身份 = 解析后的真实路径 + 文件身份 + 内容 sha256，
   shell 可执行文件解析身份，argv、cwd、shell 文本、脚本内容摘要）的 sha256。
   `launch` 在启动前**重算**并与 permit 比较；任何变化（换程序/换参数/换 cwd/
   换脚本内容）→ 不启动，许可失效。执行结束后再算一次：执行期间被替换 → 标记越界。
2. **模式重核对**：许可记录的模式与当前真实模式不一致且当前更严格（read_only）→ 不启动。
   审批/许可都不能绕过只读模式。
3. **写范围**：`permit.allowed_roots` 是显式写范围（不是 cwd 等价物）。
   * 事前强制（`enforce_write_scope=True`，只在小范围/验证场景显式开启）：Windows 上
     把这些目录临时打 Low 完整性标签（执行后恢复），受控进程只能写这些目录与它自己的
     临时目录；标签/写权限核实不了 → 不启动（不静默降级）。
   * 默认（普通命令）：**不**对用户工作区递归打标签；写范围只用于事后核验分类，
     结果里如实标注 `write_scope_pre_enforced=false` / `boundary=post_checked`。
     不把事后核验说成事前阻断。
4. **内部运行数据**：spec 里明确出现内部数据路径且 permit.internal_data_confirmed=False
   → 不启动（内部数据需要审批里的连续三次确认）。
5. **网络**：本机没有强制网络隔离。permit 显式要求主机白名单 → 明确「暂不可用」
   （不假装能做到）；未指定 → 执行照常，结果里如实标注 `network_not_isolated`。
6. **事后 guard 只是补充审计**：执行前后快照比对；QIO 自身写库/日志按启动前快照里的
   **确切文件条目**识别（证据随结果返回），不做整类排除；无法归因的改动仍按违规上报。

## 能力边界（必须一起读）

* 读没有隔离：Low 进程仍可读当前账户可读的内容；
* 网络没有强制隔离（见上）；
* **任意解释器/脚本在获准写范围内可以直接 unlink**：Windows MIC 只阻止写更高完整性
  对象，不能把删除改道系统回收站。因此「解释器内部删除」只能做到「每次经用户审批 +
  范围限制 + 结果如实上报」，不能宣称删除一定进回收站；
* 默认路径不事前置约束任意程序的写范围（没有容器/独立账户/文件系统过滤驱动）；
  `enforce_write_scope` 是 Low IL 能做到的**写**边界，不是完整文件/网络边界；
* 隔离在进程创建之后立即施加（`isolation.harden` 是追加式钩子）：存在极短的启动窗口，
  本模块不把它说成「启动前就拦住了」；
* POSIX 上 isolation 明确 unsupported：不提供强制隔离（行为与改动前一致），结果里标注。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agent.security.modes import PermissionMode, normalize_mode
from agent.tools import execution_guard, isolation, proc_cleanup, security_gate

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 16 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_TERM_VERIFY_SECONDS = 10.0

#: 允许执行的操作标记（execute_readonly = 已证明只读的程序启动）。
_EXECUTE_OPERATIONS = frozenset({"execute", "execute_readonly", "readonly"})
_READONLY_OPERATIONS = frozenset({"execute_readonly", "readonly"})

#: QIO 自身运行数据的文件形状（只用于事后核验分类，并且必须能在启动前快照里找到
#: 确切条目作为证据；不做整类排除）。
_OWN_RUNTIME_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".log", ".jsonl")

# 作为命令行参数传入的「脚本文件」必须按内容参与 spec_digest：只核对 argv 字符串
# 会让「批准后把脚本内容换掉」沿用旧许可（问题 A 的验收反例）。
_SCRIPT_SUFFIXES = frozenset(
    {
        ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".sh", ".bash", ".zsh",
        ".ps1", ".psm1", ".bat", ".cmd", ".rb", ".pl", ".php", ".lua", ".r",
    }
)
_ARGUMENT_IDENTITY_MAX_BYTES = 8 * 1024 * 1024
_MAX_ARGUMENT_FACTS = 16

# 程序内容 sha256 缓存：key = f"{realpath}|{size}|{mtime_ns}"。
# 程序文件被替换（大小/修改时间变化）→ 换 key 重算；缓存只省重复哈希，不影响身份核对。
_SCRIPT_BOOTSTRAP = (
    "import os, sys, time\n"
    "signal_path = sys.argv[1]\n"
    "source_path = sys.argv[2]\n"
    "user_argv = sys.argv[3:]\n"
    "deadline = time.monotonic() + 30.0\n"
    "while not os.path.exists(signal_path):\n"
    "    if time.monotonic() > deadline:\n"
    "        sys.stderr.write('QIO_LAUNCH_SIGNAL_TIMEOUT')\n"
    "        raise SystemExit(9)\n"
    "    time.sleep(0.005)\n"
    "with open(source_path, 'rb') as handle:\n"
    "    code = handle.read()\n"
    "sys.argv = [source_path, *user_argv]\n"
    "exec(compile(code, source_path, 'exec'), {'__name__': '__main__', '__file__': source_path, '__builtins__': __builtins__})\n"
)

_PROGRAM_HASH_CACHE: dict[str, str] = {}
_PROGRAM_HASH_LOCK = __import__("threading").Lock()


# --------------------------------------------------------------------------
# 数据契约（契约 §3.3）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchSpec:
    kind: str  # program | shell | script
    program: str | None = None
    argv: tuple[str, ...] = ()
    cwd: str | None = None
    shell_cmd: str | None = None
    script_source: str | None = None
    script_name: str | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def normalized(self) -> dict:
        """纯参数形状（不含「当前事实」，诊断/测试用）。"""
        return {
            "kind": str(self.kind or ""),
            "program": self.program,
            "argv": [str(item) for item in self.argv],
            "cwd": self.cwd,
            "shell_cmd": self.shell_cmd,
            "script_name": self.script_name,
        }


@dataclass(frozen=True)
class LaunchPermit:
    permit_id: str
    spec_digest: str
    authorized: bool
    mode: str
    allowed_roots: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ()
    network_hosts: tuple[str, ...] | None = None
    internal_data_confirmed: bool = False
    requirement_id: str | None = None
    approval_id: str | None = None
    source: str = "approval"  # instruction | approval | unrestricted
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "permit_id": self.permit_id,
            "spec_digest": self.spec_digest,
            "authorized": bool(self.authorized),
            "mode": self.mode,
            "allowed_roots": list(self.allowed_roots),
            "allowed_operations": list(self.allowed_operations),
            "network_hosts": None if self.network_hosts is None else list(self.network_hosts),
            "internal_data_confirmed": bool(self.internal_data_confirmed),
            "requirement_id": self.requirement_id,
            "approval_id": self.approval_id,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    category: str | None = None  # permission|permit_mismatch|boundary_violation|timeout|startup|...
    error: str | None = None
    guard: dict | None = None
    isolation: dict | None = None
    spec_digest: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "category": self.category,
            "error": self.error,
            "guard": self.guard,
            "isolation": self.isolation,
            "spec_digest": self.spec_digest,
        }


class LaunchUnavailable(RuntimeError):
    """无法可靠建立执行边界：调用方必须明确报「暂不可用」，不得静默降级。"""


# --------------------------------------------------------------------------
# 解析 / 规范化 / 摘要
# --------------------------------------------------------------------------


def normalize_mode_value(value: Any) -> str:
    """任意模式表示 → 新值字符串（旧值映射；读不到按受限，绝不静默提高权限）。"""
    try:
        raw = getattr(value, "value", value)
        return normalize_mode(str(raw or "")).value
    except Exception:  # noqa: BLE001 - 无法识别按受限
        return PermissionMode.RESTRICTED.value


def _resolve_program(program: str | None, cwd: str | None, computer: Any = None) -> str | None:
    """解析将要真实执行的可执行文件；找不到 → None（调用方必须拒绝启动）。"""
    text = str(program or "").strip().strip('"')
    if not text:
        return None
    resolver = getattr(computer, "resolve_program", None) if computer is not None else None
    if callable(resolver):
        try:
            found = resolver(text, cwd)
        except TypeError:
            try:
                found = resolver(text)
            except Exception:  # noqa: BLE001 - 解析失败按找不到处理（fail-closed）
                found = None
        except Exception:  # noqa: BLE001
            found = None
        if found:
            return str(found)
        # computer 解析不到时再退回本机解析（保持与 CreateProcess 的 PATH 语义一致）
    if Path(text).is_file():
        return str(Path(text).resolve())
    found = shutil.which(text)
    if not found:
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            found = shutil.which(text + suffix)
            if found:
                break
    return str(Path(found).resolve()) if found else None


def _shell_program() -> str:
    """本机真正会用的 shell 可执行文件（digest 必须包含它的身份）。"""
    if sys.platform == "win32":
        return os.environ.get("COMSPEC") or "cmd.exe"
    return "/bin/sh"


def _program_sha256(path: str) -> str | None:
    """程序文件内容摘要（带 size+mtime 键缓存；变化即重算，不影响身份核对）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = f"{os.path.realpath(path)}|{int(st.st_size)}|{int(getattr(st, 'st_mtime_ns', 0))}"
    with _PROGRAM_HASH_LOCK:
        cached = _PROGRAM_HASH_CACHE.get(key)
    if cached is not None:
        return cached
    digest = security_gate.content_sha256(path)
    if digest is not None:
        with _PROGRAM_HASH_LOCK:
            if len(_PROGRAM_HASH_CACHE) > 256:
                _PROGRAM_HASH_CACHE.clear()
            _PROGRAM_HASH_CACHE[key] = digest
    return digest


def _program_facts(program: str | None, cwd: str | None, computer: Any = None) -> dict:
    """程序身份事实：解析后的真实路径 + 文件身份 + 大小/修改时间 + 内容摘要。"""
    resolved = _resolve_program(program, cwd, computer)
    identity = security_gate.file_identity(resolved) if resolved else {}
    return {
        "requested": program,
        "resolved": resolved,
        "identity": security_gate.identity_key(identity) if identity else None,
        "size": identity.get("size") if identity else None,
        "mtime_ns": identity.get("mtime_ns") if identity else None,
        "sha256": _program_sha256(resolved) if resolved else None,
    }


def _resolved_cwd(spec: LaunchSpec) -> str | None:
    text = str(spec.cwd or "").strip()
    if not text:
        return None
    try:
        return str(Path(text).resolve())
    except (OSError, ValueError):
        return text


def _looks_like_path(text: str, suffix: str) -> bool:
    return any(sep in text for sep in ("/", "\\")) or suffix in _SCRIPT_SUFFIXES


def _has_script_shebang(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def _shell_tokens(command: str) -> list[str]:
    """shell 命令的粗略 token 化（只用于找脚本文件参数，不作为解析/权限依据）。"""
    tokens: list[str] = []
    for part in str(command or "").replace(";", " ").replace("|", " ").split():
        token = part.strip().strip("\"'").rstrip("&,")
        if token:
            tokens.append(token)
    return tokens


def _argument_file_facts(spec: LaunchSpec) -> list[dict]:
    """argv / shell 命令里作为脚本文件的参数：内容摘要必须进入 spec_digest。

    只核对 argv 字符串不够：python job.py 这种「解释器 + 脚本文件」的启动，脚本
    内容在批准之后被替换时 argv 完全不变。这里对**存在或明确不存在的脚本文件**
    记录内容 sha256（相对路径按 spec.cwd 解析；没有 cwd 时相对路径不做核对），
    让摘要同时覆盖「被执行的脚本内容」。
    """
    cwd = _resolved_cwd(spec)
    tokens: list[str] = [str(item) for item in spec.argv]
    if str(spec.kind) == "shell" and spec.shell_cmd:
        tokens.extend(_shell_tokens(str(spec.shell_cmd)))
    facts: list[dict] = []
    seen: set[str] = set()
    for text in tokens:
        raw = str(text or "").strip().strip("\"'")
        if not raw or len(raw) > 4096:
            continue
        suffix = Path(raw).suffix.lower()
        if not _looks_like_path(raw, suffix):
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            if not cwd:
                continue
            candidate = Path(cwd) / candidate
        try:
            candidate = candidate.resolve()
        except (OSError, ValueError):
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        entry: dict[str, Any] = {"path": key}
        try:
            stat = candidate.stat()
        except OSError:
            if suffix in _SCRIPT_SUFFIXES:
                entry["exists"] = False
                facts.append(entry)
            continue
        if not candidate.is_file():
            continue
        if suffix not in _SCRIPT_SUFFIXES and not _has_script_shebang(candidate):
            continue
        if int(stat.st_size) > _ARGUMENT_IDENTITY_MAX_BYTES:
            entry.update(
                {
                    "exists": True,
                    "size": int(stat.st_size),
                    "mtime_ns": int(getattr(stat, "st_mtime_ns", 0)),
                    "identity": "too_large_to_hash",
                }
            )
        else:
            entry.update(
                {
                    "exists": True,
                    "size": int(stat.st_size),
                    "mtime_ns": int(getattr(stat, "st_mtime_ns", 0)),
                    "sha256": _program_sha256(key),
                }
            )
        facts.append(entry)
        if len(facts) >= _MAX_ARGUMENT_FACTS:
            break
    return facts


def build_spec_facts(spec: LaunchSpec, *, computer: Any = None) -> dict:
    """构造 spec 的「当前事实」：程序身份/argv/cwd/脚本内容摘要。"""
    script_source = spec.script_source or ""
    facts: dict[str, Any] = {
        "kind": str(spec.kind or ""),
        "argv": [str(item) for item in spec.argv],
        "cwd": _resolved_cwd(spec),
        "shell_cmd": spec.shell_cmd,
        "script_sha256": (
            hashlib.sha256(script_source.encode("utf-8", errors="replace")).hexdigest()
            if script_source
            else None
        ),
        "script_name": spec.script_name,
        # argv/命令里作为脚本文件传入的参数也按内容参与摘要（批准后换脚本 → 失效）
        "argument_files": _argument_file_facts(spec),
    }
    if str(spec.kind) == "shell":
        # shell 的真实身份是解析后的 COMSPEC/sh；只把命令文本算进去不是「程序身份」。
        facts["program_facts"] = _program_facts(_shell_program(), spec.cwd, computer)
    else:
        facts["program_facts"] = _program_facts(spec.program, spec.cwd, computer)
    return facts


def compute_spec_digest(
    spec: LaunchSpec, *, computer: Any = None, facts: dict | None = None
) -> str:
    """规范化 spec 的 sha256（程序身份/argv/cwd/脚本内容）。

    执行前必须重算并与 `LaunchPermit.spec_digest` 比较：任何一项变化都使许可失效。
    """
    payload = facts if facts is not None else build_spec_facts(spec, computer=computer)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_permit(
    spec: LaunchSpec,
    *,
    computer: Any = None,
    mode: Any = PermissionMode.RESTRICTED,
    allowed_roots: Sequence[str] = (),
    allowed_operations: Sequence[str] = ("execute",),
    network_hosts: Sequence[str] | None = None,
    internal_data_confirmed: bool = False,
    requirement_id: str | None = None,
    approval_id: str | None = None,
    source: str = "approval",
    reason: str = "",
) -> LaunchPermit:
    """按当前事实构造许可（工具/审批执行器在授权通过后调用）。"""
    return LaunchPermit(
        permit_id=f"permit_{uuid.uuid4().hex[:12]}",
        spec_digest=compute_spec_digest(spec, computer=computer),
        authorized=True,
        mode=normalize_mode_value(mode),
        allowed_roots=tuple(str(item) for item in allowed_roots if str(item)),
        allowed_operations=tuple(str(item) for item in allowed_operations),
        network_hosts=None if network_hosts is None else tuple(str(item) for item in network_hosts),
        internal_data_confirmed=bool(internal_data_confirmed),
        requirement_id=requirement_id,
        approval_id=approval_id,
        source=str(source or "approval"),
        reason=str(reason or ""),
    )


def write_roots_for(computer: Any = None, mode: Any = None) -> tuple[str, ...]:
    """普通命令的写范围事实：用户工作区根（拿不到就不编；不代表 cwd）。"""
    if normalize_mode_value(mode) == PermissionMode.READ_ONLY.value:
        return ()
    root = getattr(computer, "root", None) if computer is not None else None
    try:
        value = root() if callable(root) else root
    except Exception:  # noqa: BLE001
        value = None
    if not value:
        return ()
    try:
        return (str(Path(str(value)).resolve()),)
    except (OSError, ValueError):
        return ()


# --------------------------------------------------------------------------
# 审批重放：payload 与审批记录的重核对（不判权限，只核对「还是不是同一个操作」）
# --------------------------------------------------------------------------


def recheck_approved_spec(
    spec: LaunchSpec,
    fingerprint: Mapping[str, Any] | None,
    *,
    computer: Any = None,
    effect: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> tuple[bool, str, str]:
    """执行前重核对「本次要启动的东西」与审批单记录是否一致。

    返回 `(是否一致, 中文原因, 当前 spec_digest)`。不一致时调用方必须**不执行**并
    返回「审批失效」的明确结果（不是重新弹一次审批）。
    """
    data = dict(fingerprint or {})
    digest = compute_spec_digest(spec, computer=computer)

    expected_digest = str(data.get("spec_digest") or "")
    if expected_digest and expected_digest != digest:
        return (
            False,
            "审批单记录的程序/参数/工作目录/脚本内容与本次执行不一致（spec_digest 不一致）："
            "许可已失效，本次不执行，请重新提出",
            digest,
        )

    # 程序身份（内容摘要）：审批后程序文件被替换 → 不只是 argv 变了
    expected_program_sha = str(data.get("program_sha256") or "")
    if expected_program_sha:
        facts = build_spec_facts(spec, computer=computer)
        current_sha = str((facts.get("program_facts") or {}).get("sha256") or "")
        if current_sha != expected_program_sha:
            return (
                False,
                "要执行的程序文件在审批之后发生了变化（内容摘要不一致）：许可已失效，本次不执行",
                digest,
            )

    # payload 整体摘要：任何字段被改（argv/cwd/timeout/…）都能发现
    expected_payload_digest = str(data.get("payload_digest") or "")
    if expected_payload_digest and payload is not None:
        current = hashlib.sha256(
            json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if current != expected_payload_digest:
            return (
                False,
                "审批单记录的执行参数与本次 payload 不一致：许可已失效，本次不执行",
                digest,
            )

    # 审批时保存的 effect 快照：argv / cwd / shell 命令逐项核对
    expected_effect = dict(effect or {})
    detail = dict(expected_effect.get("detail") or {})
    expected_argv = detail.get("argv")
    if expected_argv and str(spec.kind) == "program":
        actual_argv = [str(spec.program or ""), *[str(item) for item in spec.argv]]
        if [str(item) for item in expected_argv] != actual_argv:
            return (
                False,
                "审批单记录的程序/参数（argv）与本次执行不一致：许可已失效，本次不执行",
                digest,
            )
    expected_command = detail.get("command")
    if expected_command is not None and str(spec.shell_cmd or "") != str(expected_command):
        return (
            False,
            "审批单记录的 shell 命令与本次执行不一致：许可已失效，本次不执行",
            digest,
        )
    expected_cwd = detail.get("cwd")
    if expected_cwd is None:
        for target in expected_effect.get("targets") or []:
            item = dict(target or {})
            if item.get("kind") == "path" and str(item.get("value") or ""):
                expected_cwd = str(item.get("value"))
                break
    if expected_cwd is not None:
        actual_cwd = _resolved_cwd(spec) or ""
        try:
            expected_norm = str(Path(str(expected_cwd)).resolve())
        except (OSError, ValueError):
            expected_norm = str(expected_cwd)
        if expected_norm and actual_cwd and expected_norm != actual_cwd:
            return (
                False,
                "审批单记录的工作目录（cwd）与本次执行不一致：许可已失效，本次不执行",
                digest,
            )
    return True, "", digest


# --------------------------------------------------------------------------
# 受控启动
# --------------------------------------------------------------------------


class ControlledLauncher:
    """统一受控启动器。

    * `computer`：提供 resolve_program 与工作区根（可空）；
    * `security`：AuthorizationEngine/Gateway（只用来读模式/内部数据路径，不判权限）；
    * `mode_provider`：覆盖模式读取（默认读 security.mode()）；
    * `internal_data_paths`：内部运行数据路径 provider（可空，默认读 security）；
    * `protected_scope_provider`：事后 guard 的保护范围（默认工作区根 + 内部数据）；
    * `own_runtime_paths`：QIO 自身会写的确切文件 provider；None = 用启动前快照里的
      确切条目作为证据（不做整类排除）。
    """

    def __init__(
        self,
        *,
        computer: Any = None,
        security: Any = None,
        mode_provider: Callable[[], Any] | None = None,
        internal_data_paths: Callable[[], Sequence[Any]] | None = None,
        protected_scope_provider: Callable[[], Sequence[Any]] | None = None,
        own_runtime_paths: Callable[[], Sequence[Any]] | None = None,
        guard_enabled: bool = True,
        max_stdout_bytes: int = MAX_STDOUT_BYTES,
        max_stderr_bytes: int = MAX_STDERR_BYTES,
        guard_max_entries: int = execution_guard.DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.computer = computer
        self.security = security
        self.mode_provider = mode_provider
        self.internal_data_paths = internal_data_paths
        self.protected_scope_provider = protected_scope_provider
        self.own_runtime_paths = own_runtime_paths
        self.guard_enabled = bool(guard_enabled)
        self.max_stdout_bytes = max(1024, int(max_stdout_bytes))
        self.max_stderr_bytes = max(1024, int(max_stderr_bytes))
        self.guard_max_entries = max(1000, int(guard_max_entries))
        self._touched_roots: list[dict] = []

    # -- 上下文读取 --------------------------------------------------------

    def current_mode(self) -> str:
        if callable(self.mode_provider):
            try:
                return normalize_mode_value(self.mode_provider())
            except Exception:  # noqa: BLE001 - 读不到模式按受限（保守，不提高权限）
                return PermissionMode.RESTRICTED.value
        if self.security is not None:
            try:
                getter = getattr(self.security, "mode", None)
                value = getter() if callable(getter) else getter
                if value is not None:
                    return normalize_mode_value(value)
            except Exception:  # noqa: BLE001
                pass
        if self.computer is not None:
            try:
                value = getattr(self.computer, "mode", None)
                if value is not None:
                    return normalize_mode_value(value)
            except Exception:  # noqa: BLE001
                pass
        return PermissionMode.RESTRICTED.value

    def internal_roots(self) -> list[Path]:
        roots: list[Any] = []
        if callable(self.internal_data_paths):
            try:
                roots = list(self.internal_data_paths() or [])
            except Exception:  # noqa: BLE001 - 读不到按空（不伪造保护范围）
                roots = []
        elif self.security is not None:
            raw = getattr(self.security, "internal_data_paths", None)
            try:
                value = raw() if callable(raw) else raw
                if isinstance(value, (list, tuple, set)):
                    roots = list(value)
                elif value is not None:
                    roots = [value]
            except Exception:  # noqa: BLE001
                roots = []
        resolved: list[Path] = []
        for item in roots:
            try:
                path = Path(str(item)).resolve()
            except (OSError, ValueError):
                continue
            if path not in resolved:
                resolved.append(path)
        return resolved

    def protected_roots(self) -> list[str]:
        if callable(self.protected_scope_provider):
            try:
                return [str(item) for item in (self.protected_scope_provider() or []) if str(item)]
            except Exception:  # noqa: BLE001 - 读不到不伪造保护范围
                return []
        roots: list[str] = []
        if self.computer is not None:
            try:
                root = getattr(self.computer, "root", None)
                value = root() if callable(root) else root
                if value:
                    roots.append(str(Path(str(value)).resolve()))
            except Exception:  # noqa: BLE001
                pass
        roots.extend(str(item) for item in self.internal_roots())
        return roots

    # -- 事前检查 ----------------------------------------------------------

    def _internal_mentions(self, spec: LaunchSpec) -> list[str]:
        roots = self.internal_roots()
        if not roots:
            return []
        texts = [
            str(spec.program or ""),
            str(spec.shell_cmd or ""),
            str(spec.script_source or ""),
            str(spec.cwd or ""),
            *[str(item) for item in spec.argv],
        ]
        hits: list[str] = []
        for root in roots:
            raw = str(root)
            alt = raw.replace("\\", "/")
            matched = any(raw and (raw in text or alt in text) for text in texts if text)
            if not matched:
                # 只有在 argv 看起来像路径时才 resolve（保持便宜；不是权限判定）。
                for item in spec.argv:
                    text = str(item)
                    if not any(sep in text for sep in ("/", "\\", ":")):
                        continue
                    try:
                        candidate = Path(text).resolve()
                    except (OSError, ValueError):
                        continue
                    try:
                        if candidate == root or candidate.is_relative_to(root):
                            matched = True
                            break
                    except (OSError, ValueError):
                        continue
            if matched and raw not in hits:
                hits.append(raw)
        return hits

    @staticmethod
    def _validate_spec(spec: LaunchSpec) -> str | None:
        kind = str(spec.kind or "").strip()
        if kind not in ("program", "shell", "script"):
            return f"不支持的启动类型：{kind or '(空)'}"
        if kind == "program" and not str(spec.program or "").strip():
            return "program 启动缺少 program"
        if kind == "shell" and not str(spec.shell_cmd or "").strip():
            return "shell 启动缺少命令文本"
        if kind == "script" and spec.script_source is None:
            return "script 启动缺少脚本内容"
        if kind in ("program", "script") and not str(spec.program or "").strip():
            return "启动缺少解释器/程序"
        if float(spec.timeout or 0) <= 0:
            return "timeout 必须大于 0"
        return None

    def _low_integrity_required(
        self, mode: str, permit: LaunchPermit, *, enforce_write_scope: bool
    ) -> bool:
        """按模式/许可决定是否强制 Low IL（Windows 内核写边界）。"""
        if not isolation.supported():
            return False
        if mode == PermissionMode.UNRESTRICTED.value:
            return False
        if mode == PermissionMode.READ_ONLY.value:
            return True
        return bool(enforce_write_scope)

    def prepare(
        self,
        spec: LaunchSpec,
        permit: LaunchPermit,
        *,
        enforce_write_scope: bool = False,
    ) -> tuple[str | None, str]:
        """事前检查（不启动）：返回 (拒绝原因, 分类)；None = 可以启动。**不抛异常**。"""
        invalid = self._validate_spec(spec)
        if invalid:
            return invalid, "startup"
        if not permit.authorized:
            return "这次启动没有获得授权：不执行", "permission"
        if str(spec.kind) in ("program", "script"):
            resolved = _resolve_program(spec.program, spec.cwd, self.computer)
            if not resolved:
                what = "解释器" if str(spec.kind) == "script" else "程序"
                return f"找不到{what}：{spec.program}", "startup"
        mode = normalize_mode_value(permit.mode)
        # 0) 模式重核对：当前模式比许可时更严格（切到只读）→ 许可失效
        current = self.current_mode()
        if current == PermissionMode.READ_ONLY.value and mode != PermissionMode.READ_ONLY.value:
            return (
                "当前已切换到只读模式，这次许可不再允许执行：本次不启动（审批/许可不能绕过只读）",
                "permission",
            )
        # 1) 许可必须真的授予执行操作
        operations = {str(item) for item in permit.allowed_operations}
        if not operations & _EXECUTE_OPERATIONS:
            return "许可没有授予执行（execute）操作：本次不启动", "permission"
        # 2) 事前摘要核对：程序/脚本/argv/cwd/shell 文本任何变化 → 许可失效
        current_digest = compute_spec_digest(spec, computer=self.computer)
        if current_digest != permit.spec_digest:
            return (
                "许可失效：程序/脚本/参数/工作目录与授权时不一致（spec_digest 不一致），"
                "本次不启动；请重新授权",
                "permit_mismatch",
            )
        # 3) 只读模式：只有「已证明只读」的程序启动被允许，且不允许写范围
        if mode == PermissionMode.READ_ONLY.value:
            if not (operations & _READONLY_OPERATIONS) or permit.allowed_roots:
                return (
                    "只读模式下不允许带写范围/未证明只读的启动：只能执行可证明只读（无写入）的程序",
                    "permission",
                )
        # 4) 内部运行数据：未完成三次确认 → 不启动
        mentions = self._internal_mentions(spec)
        if mentions and not permit.internal_data_confirmed:
            return (
                "脚本/命令涉及 QIO 内部运行数据（" + "、".join(mentions) + "）："
                "必须先完成连续三次确认；本次未确认，不启动",
                "permission",
            )
        # 5) 网络白名单：本机没有强制网络隔离，显式要求时明确不可用（不静默降级）
        if permit.network_hosts is not None:
            return (
                "本机无法按主机白名单强制限制网络（没有 WFP/防火墙集成）："
                "这个路径暂不可用；需要联网的操作只能按「允许整条命令/脚本」审批并如实标注边界",
                "unsupported",
            )
        # 6) 事前强制写范围：需要 OS 层能真正建立边界
        if enforce_write_scope:
            if not isolation.supported():
                return (
                    "本平台没有强制写范围边界（POSIX 上 isolation 明确 unsupported）："
                    "事前强制写范围暂不可用；不静默降级",
                    "unsupported",
                )
            missing = [root for root in permit.allowed_roots if not Path(root).is_dir()]
            if missing:
                return "授权写范围不存在或不是目录：" + "、".join(missing) + "；本次不启动", "boundary_unavailable"
        return None, ""

    # -- 隔离（sandbox 也复用这一层） -------------------------------------

    def apply_isolation(
        self,
        process: Any,
        spec: LaunchSpec,
        permit: LaunchPermit,
        *,
        scratch_dir: str | None = None,
        policy: Any = None,
        extra_writable_dirs: "list[str] | tuple[str, ...] | None" = None,
        mode: Any = None,
        authorized_write_scope: bool = False,
        low_integrity_required: bool | None = None,
    ):
        """对刚创建的子进程施加真实强制隔离（isolation.harden 的唯一包装层）。

        `low_integrity_required=None` 时按 harden 自己的模式规则推导（保持既有语义）；
        调用方也可以显式指定。**永远不抛异常**（harden 的契约）。
        """
        kwargs: dict[str, Any] = {
            "scratch_dir": scratch_dir,
            "policy": policy,
        }
        if extra_writable_dirs is not None:
            kwargs["extra_writable_dirs"] = extra_writable_dirs
        if mode is not None:
            kwargs["mode"] = mode
        kwargs["authorized_write_scope"] = bool(authorized_write_scope)
        if low_integrity_required is not None:
            kwargs["low_integrity_required"] = bool(low_integrity_required)
        return isolation.harden(process, **kwargs)

    # -- 事后核验（补充审计，不替代事前约束） ------------------------------

    def postflight(self, spec: LaunchSpec, permit: LaunchPermit) -> dict:
        """执行后再算一次 spec_digest：程序/脚本执行期间被替换 → 许可失效/标记越界。"""
        try:
            digest = compute_spec_digest(spec, computer=self.computer)
        except Exception as exc:  # noqa: BLE001 - 核验失败如实上报
            return {"changed": True, "digest": "", "error": f"{type(exc).__name__}: {exc}"}
        changed = digest != permit.spec_digest
        return {
            "changed": changed,
            "digest": digest,
            "expected": permit.spec_digest,
            "detail": (
                "程序/脚本在执行期间发生了变化（spec_digest 不一致）"
                if changed
                else "执行前后 spec_digest 一致"
            ),
        }

    def _classify_guard(
        self,
        report: dict,
        permit: LaunchPermit,
        before: execution_guard.Snapshot | None,
        *,
        mode: str,
        enforce_write_scope: bool,
    ) -> dict:
        if before is None:
            return {
                "ok": True,
                "verified": False,
                "violations": [],
                "authorized_changes": [],
                "self_writes": [],
                "checked": 0,
                "detail": "本次没有做执行前快照（没有可核验的保护范围或 unrestricted 模式），不声称已核验",
                "pre_enforced": False,
                "boundary": "not_checked",
            }
        if report.get("error"):
            return {
                "ok": False,
                "verified": False,
                "violations": [],
                "authorized_changes": [],
                "self_writes": [],
                "detail": "事后核验不可用：" + str(report.get("detail") or ""),
                "pre_enforced": bool(enforce_write_scope),
                "boundary": "post_check_failed",
            }
        allowed = [str(Path(root).resolve()) for root in permit.allowed_roots]
        internal = [str(item) for item in self.internal_roots()]
        own_provider = self.own_runtime_paths
        own_set: set[str] = set()
        if callable(own_provider):
            try:
                own_set = {str(Path(str(item)).resolve()) for item in (own_provider() or [])}
            except Exception:  # noqa: BLE001
                own_set = set()
        violations: list[dict] = []
        authorized: list[dict] = []
        self_writes: list[dict] = []
        for item in report.get("violations", []):
            path = str(item.get("path") or "")
            if _path_under_any(path, allowed):
                authorized.append(dict(item, classification="authorized_write"))
                continue
            evidence = None
            if path:
                try:
                    resolved = str(Path(path).resolve())
                except (OSError, ValueError):
                    resolved = path
                if own_set and resolved in own_set:
                    evidence = {"path": resolved, "evidence": "caller_provided_qio_runtime_path"}
                elif before is not None and resolved in before.entries:
                    if _path_under_any(resolved, internal) and _looks_like_own_runtime(resolved):
                        stat = before.entries[resolved]
                        evidence = {
                            "path": resolved,
                            "size": int(stat[0]),
                            "mtime_ns": int(stat[1]),
                            "evidence": "pre_execution_snapshot_of_qio_runtime_file",
                        }
            if evidence is not None:
                self_writes.append(
                    dict(
                        item,
                        classification="qio_self_write_candidate",
                        evidence=evidence,
                        attribution_note=(
                            "这个文件在启动前就存在且属于 QIO 自身运行数据（db/log）；"
                            "QIO 后端与受控子进程都可能写它，无法区分写入者，"
                            "按自身写入处理（有启动前快照证据）"
                        ),
                    )
                )
                continue
            violations.append(dict(item, classification="unattributed_change"))
        return {
            "ok": not violations,
            "verified": True,
            "violations": violations,
            "authorized_changes": authorized,
            "self_writes": self_writes,
            "checked": report.get("checked"),
            "truncated": bool(report.get("truncated")),
            "detail": report.get("detail") or "",
            "pre_enforced": bool(enforce_write_scope),
            "boundary": "pre_enforced_write_scope" if enforce_write_scope else "post_checked_only",
            "disclaimer": (
                "事后核验是补充审计，不是事前约束；无法回滚已发生的修改。"
                "QIO 自身写库/日志按启动前快照里的确切文件条目识别（证据见 self_writes），"
                "不做整类排除。"
            ),
        }

    # -- 执行 --------------------------------------------------------------

    async def launch(
        self,
        spec: LaunchSpec,
        permit: LaunchPermit,
        *,
        enforce_write_scope: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> LaunchResult:
        """受控启动一次；**拿不准就不启动**（返回明确的不可用/权限原因）。

        `enforce_write_scope=True`：小范围/验证场景显式开启事前写范围（Windows Low IL）；
        默认 False：普通命令只做事后核验分类，结果里如实标注没有事前阻断。
        """
        pre_error, pre_category = self.prepare(
            spec, permit, enforce_write_scope=enforce_write_scope
        )
        mode = normalize_mode_value(permit.mode)
        if pre_error:
            return LaunchResult(
                ok=False,
                category=pre_category,
                error=pre_error,
                spec_digest=permit.spec_digest,
                isolation={
                    "capability_boundaries": self._capability_boundaries(
                        spec, permit, mode, enforce_write_scope
                    )
                },
                guard=None,
            )

        guard_roots = self.protected_roots()
        guard: execution_guard.ExecutionGuard | None = None
        guard_before: execution_guard.Snapshot | None = None
        if (
            self.guard_enabled
            and guard_roots
            and mode != PermissionMode.UNRESTRICTED.value
        ):
            guard = execution_guard.ExecutionGuard(
                lambda: guard_roots, max_entries=self.guard_max_entries
            )
            try:
                guard_before = await security_gate.to_thread(guard.capture)
            except Exception:  # noqa: BLE001 - 快照失败不阻断，但结果如实标注
                guard_before = None

        scratch = tempfile.mkdtemp(prefix="qio-launch-")
        process: Any = None
        hardening = None
        over_limit = False
        cleanup_confirmed = True
        script_path: Path | None = None
        self._touched_roots = []
        try:
            try:
                argv, script_path, bootstrap_path, signal_path = self._build_argv(spec, scratch)
            except LaunchUnavailable as exc:
                return LaunchResult(
                    ok=False,
                    category="unsupported",
                    error=str(exc),
                    spec_digest=permit.spec_digest,
                    isolation={
                        "capability_boundaries": self._capability_boundaries(
                            spec, permit, mode, enforce_write_scope
                        )
                    },
                )
            cwd = _resolved_cwd(spec) or scratch
            try:
                Path(cwd).exists()
            except OSError:
                cwd = scratch
            spawn_kwargs: dict[str, Any] = {
                "cwd": cwd,
                "stdin": asyncio.subprocess.DEVNULL,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
            }
            if env is not None:
                spawn_kwargs["env"] = dict(env)
            spawn_kwargs.update(proc_cleanup.new_session_kwargs())
            try:
                process = await asyncio.create_subprocess_exec(*argv, **spawn_kwargs)
            except FileNotFoundError as exc:
                return LaunchResult(
                    ok=False,
                    category="startup",
                    error=f"启动失败：找不到程序（{exc}）",
                    spec_digest=permit.spec_digest,
                    isolation={
                        "capability_boundaries": self._capability_boundaries(
                            spec, permit, mode, enforce_write_scope
                        )
                    },
                )
            except OSError as exc:
                return LaunchResult(
                    ok=False,
                    category="startup",
                    error=f"启动失败：{exc}",
                    spec_digest=permit.spec_digest,
                    isolation={
                        "capability_boundaries": self._capability_boundaries(
                            spec, permit, mode, enforce_write_scope
                        )
                    },
                )
            # 追加式真实隔离：Job Object（收整棵树）+ 按模式/许可强制的 Low IL。
            low_required = self._low_integrity_required(
                mode, permit, enforce_write_scope=enforce_write_scope
            )
            if enforce_write_scope:
                self._record_root_labels(permit.allowed_roots)
            hardening = self.apply_isolation(
                process,
                spec,
                permit,
                scratch_dir=scratch,
                extra_writable_dirs=(
                    list(permit.allowed_roots) if enforce_write_scope else None
                ),
                mode=mode,
                low_integrity_required=low_required,
            )
            if hardening.low_integrity_required and not hardening.boundary_ok():
                cleanup_confirmed = await self._kill_tree(process)
                isolation.release(process)
                process = None
                return LaunchResult(
                    ok=False,
                    category="boundary_unavailable",
                    error=(
                        "当前模式/许可要求低完整性写边界，但本机无法核实它已生效："
                        "本次不启动（不静默降级）"
                    ),
                    spec_digest=permit.spec_digest,
                    isolation=self._isolation_payload(
                        hardening, permit, spec, mode, enforce_write_scope
                    ),
                    guard=None,
                )
            if signal_path is not None:
                # 隔离已施加并核实：现在才放行脚本（用户脚本的副作用此前不会发生）
                try:
                    signal_path.write_bytes(b"go")
                except OSError as exc:
                    await self._kill_tree(process)
                    isolation.release(process)
                    process = None
                    return LaunchResult(
                        ok=False,
                        category="unsupported",
                        error=f"脚本隔离已建立但放行信号写入失败：本次未执行用户脚本（{exc}）",
                        spec_digest=permit.spec_digest,
                        isolation=self._isolation_payload(
                            hardening, permit, spec, mode, enforce_write_scope
                        ),
                    )
            try:
                stdout, stderr, over_limit = await asyncio.wait_for(
                    self._collect(process),
                    timeout=float(spec.timeout or DEFAULT_TIMEOUT_SECONDS),
                )
            except asyncio.TimeoutError:
                cleanup_confirmed = await self._kill_tree(process)
                isolation.release(process)
                process = None
                guard_report = await self._guard_report(
                    guard, guard_before, permit, mode, enforce_write_scope
                )
                suffix = "（进程已终止）" if cleanup_confirmed else "（清理未确认，进程可能仍在运行）"
                return LaunchResult(
                    ok=False,
                    category="timeout",
                    error=f"执行超时：已终止进程树{suffix}",
                    guard=guard_report,
                    isolation=self._isolation_payload(
                        hardening, permit, spec, mode, enforce_write_scope
                    ),
                    spec_digest=permit.spec_digest,
                )
            except asyncio.CancelledError:
                await self._kill_tree(process)
                isolation.release(process)
                process = None
                self._report_guard_on_cancel(guard, guard_before)
                raise
            with contextlib.suppress(Exception):
                await process.wait()
            returncode = process.returncode
            # 关掉 job 句柄：KILL_ON_JOB_CLOSE，后代进程不会漏网。
            isolation.release(process)
            process = None
            script_check = self._check_script_file(
                script_path, spec, bootstrap_path=bootstrap_path
            )
            post = self.postflight(spec, permit)
            changed = bool(post.get("changed"))
            guard_report = await self._guard_report(
                guard, guard_before, permit, mode, enforce_write_scope
            )
            isolation_payload = self._isolation_payload(
                hardening, permit, spec, mode, enforce_write_scope
            )
            isolation_payload["script_content"] = script_check
            stdout_text = _cap_text(stdout, self.max_stdout_bytes)
            stderr_text = _cap_text(stderr, self.max_stderr_bytes)
            if over_limit:
                return LaunchResult(
                    ok=False,
                    returncode=returncode,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    category="output_limit",
                    error="输出超过上限，进程已终止",
                    guard=guard_report,
                    isolation=isolation_payload,
                    spec_digest=permit.spec_digest,
                )
            if changed or script_check.get("changed"):
                return LaunchResult(
                    ok=False,
                    returncode=returncode,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    category="boundary_violation",
                    error=(
                        "程序/脚本在执行期间发生了变化（spec_digest/脚本文件内容不一致）："
                        "本次执行的结果不可作为已授权操作的事实"
                    ),
                    guard=guard_report,
                    isolation=isolation_payload,
                    spec_digest=permit.spec_digest,
                )
            if guard_report.get("violations"):
                shown = "；".join(
                    str(item.get("path") or "") for item in guard_report["violations"][:5]
                )
                return LaunchResult(
                    ok=False,
                    returncode=returncode,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    category="boundary_violation",
                    error=(
                        "执行越界：检测到授权写范围之外的受保护文件被修改"
                        "（无法回滚，只能如实上报）：" + shown
                    ),
                    guard=guard_report,
                    isolation=isolation_payload,
                    spec_digest=permit.spec_digest,
                )
            if guard_report.get("boundary") == "post_check_failed":
                return LaunchResult(
                    ok=False,
                    returncode=returncode,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    category="boundary_unverified",
                    error=(
                        "执行完成，但事后核验失败：无法确认有没有越界修改"
                        "（这是事实，不是失败原因猜测）"
                    ),
                    guard=guard_report,
                    isolation=isolation_payload,
                    spec_digest=permit.spec_digest,
                )
            return LaunchResult(
                ok=bool(returncode == 0),
                returncode=returncode,
                stdout=stdout_text,
                stderr=stderr_text,
                category=None if returncode == 0 else "exit_code",
                error=None if returncode == 0 else f"退出码 {returncode}",
                guard=guard_report,
                isolation=isolation_payload,
                spec_digest=permit.spec_digest,
            )
        finally:
            if process is not None:
                await self._kill_tree(process)
                isolation.release(process)
            self._restore_write_scope()
            shutil.rmtree(scratch, ignore_errors=True)

    # -- 启动细节 ----------------------------------------------------------

    def _build_argv(
        self, spec: LaunchSpec, scratch: str
    ) -> tuple[list[str], Path | None, Path | None, Path | None]:
        """构造真实命令行。

        script 类型额外返回 (用户脚本文件, 引导文件, 放行信号文件)：引导脚本先等信号，
        宿主完成隔离并核实后才写信号 —— 用户脚本的副作用**不会**在隔离生效前发生。
        """
        kind = str(spec.kind)
        if kind == "program":
            resolved = _resolve_program(spec.program, spec.cwd, self.computer)
            if not resolved:
                raise LaunchUnavailable("程序无法解析为真实可执行文件：暂不可用")
            return [resolved, *[str(item) for item in spec.argv]], None, None, None
        if kind == "shell":
            shell = _shell_program()
            resolved = _resolve_program(shell, spec.cwd, self.computer) or shell
            if sys.platform == "win32":
                return [resolved, "/d", "/s", "/c", str(spec.shell_cmd)], None, None, None
            return [resolved, "-c", str(spec.shell_cmd)], None, None, None
        # script：解释器 + 引导文件（等待放行信号）+ 真实脚本文件 + 参数
        resolved = _resolve_program(spec.program, spec.cwd, self.computer)
        if not resolved:
            raise LaunchUnavailable("脚本解释器无法解析为真实可执行文件：暂不可用")
        scratch_path = Path(scratch)
        signal_path = scratch_path / "qio-launch-signal"
        bootstrap_path = scratch_path / "qio-launch-bootstrap.py"
        source_path = scratch_path / "qio-launch-source.py"
        name = str(spec.script_name or "").strip() or "qio-script.py"
        suffix = Path(name).suffix or ".py"
        named_path = scratch_path / (Path(name).stem + suffix)
        # 用字节写入：文本模式在 Windows 会把 \n 转成 \r\n，导致「执行前后脚本内容核对」
        # 永远把合法脚本误判成被替换。
        source_bytes = str(spec.script_source or "").encode("utf-8", errors="replace")
        named_path.write_bytes(source_bytes)
        source_path.write_bytes(source_bytes)
        bootstrap_path.write_bytes(_SCRIPT_BOOTSTRAP.encode("utf-8"))
        argv = [
            resolved,
            str(bootstrap_path),
            str(signal_path),
            str(source_path),
            *[str(item) for item in spec.argv],
        ]
        return argv, named_path, bootstrap_path, signal_path

    def _check_script_file(
        self,
        script_path: Path | None,
        spec: LaunchSpec,
        *,
        bootstrap_path: Path | None = None,
    ) -> dict:
        """脚本内容执行前后核对：真正被执行的临时脚本/引导文件必须与许可内容一致。"""
        if str(spec.kind) != "script" or script_path is None:
            return {"checked": False, "reason": "不是脚本启动"}
        try:
            raw = script_path.read_bytes()
        except OSError as exc:
            return {
                "checked": False,
                "changed": True,
                "error": f"{type(exc).__name__}: {exc}",
                "detail": "执行后读不到临时脚本文件，无法核对脚本内容",
            }
        actual = hashlib.sha256(raw).hexdigest()
        expected = hashlib.sha256(
            str(spec.script_source or "").encode("utf-8", errors="replace")
        ).hexdigest()
        result = {
            "checked": True,
            "changed": actual != expected,
            "expected_sha256": expected,
            "actual_sha256": actual,
            "detail": (
                "脚本文件执行前后内容不一致（文件被替换）"
                if actual != expected
                else "脚本文件执行前后内容一致"
            ),
        }
        if bootstrap_path is not None:
            expected_bootstrap = hashlib.sha256(_SCRIPT_BOOTSTRAP.encode("utf-8")).hexdigest()
            try:
                actual_bootstrap = hashlib.sha256(bootstrap_path.read_bytes()).hexdigest()
            except OSError as exc:
                actual_bootstrap = f"error:{type(exc).__name__}: {exc}"
            result["bootstrap"] = {
                "expected_sha256": expected_bootstrap,
                "actual_sha256": actual_bootstrap,
                "changed": actual_bootstrap != expected_bootstrap,
            }
            if actual_bootstrap != expected_bootstrap:
                result["changed"] = True
                result["detail"] += "；引导文件执行前后内容不一致"
        return result

    async def _collect(self, process: Any) -> tuple[bytes, bytes, bool]:
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        sizes: dict[str, int] = {"stdout": 0, "stderr": 0}
        over = {"value": False}

        async def _read(stream: Any, limit: int, key: str) -> None:
            while True:
                chunk = await stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    return
                room = limit - sizes[key]
                if room > 0:
                    kept = chunk[:room]
                    chunks[key].append(kept)
                    sizes[key] += len(kept)
                if sizes[key] >= limit and not over["value"]:
                    over["value"] = True
                    # 输出超限：立刻结束进程树，否则对端会一直写
                    await self._kill_tree(process)
                    return

        await asyncio.gather(
            _read(process.stdout, self.max_stdout_bytes, "stdout"),
            _read(process.stderr, self.max_stderr_bytes, "stderr"),
            return_exceptions=True,
        )
        return b"".join(chunks["stdout"]), b"".join(chunks["stderr"]), over["value"]

    async def _kill_tree(self, process: Any) -> bool:
        """终止本次启动的进程树并做退出验证（返回是否确认收干净，语义不回退）。"""
        try:
            return await proc_cleanup.terminate_process_tree(
                process, wait_exit_seconds=_TERM_VERIFY_SECONDS
            )
        except Exception:  # noqa: BLE001 - 清理失败如实返回未确认
            return False

    # -- 事后核验 ----------------------------------------------------------

    async def _guard_report(
        self,
        guard: execution_guard.ExecutionGuard | None,
        before: execution_guard.Snapshot | None,
        permit: LaunchPermit,
        mode: str,
        enforce_write_scope: bool,
    ) -> dict:
        if guard is None or before is None:
            return self._classify_guard(
                {"violations": []},
                permit,
                None,
                mode=mode,
                enforce_write_scope=enforce_write_scope,
            )
        try:
            after = await security_gate.to_thread(guard.capture)
            report = guard.compare(before, after)
        except Exception as exc:  # noqa: BLE001 - 核验失败如实标注
            return self._classify_guard(
                {
                    "error": True,
                    "violations": [],
                    "detail": f"{type(exc).__name__}: {exc}",
                },
                permit,
                before,
                mode=mode,
                enforce_write_scope=enforce_write_scope,
            )
        return self._classify_guard(
            report,
            permit,
            before,
            mode=mode,
            enforce_write_scope=enforce_write_scope,
        )

    def _report_guard_on_cancel(
        self,
        guard: execution_guard.ExecutionGuard | None,
        before: execution_guard.Snapshot | None,
    ) -> None:
        if guard is None or before is None:
            return
        try:
            report = guard.compare(before, guard.capture())
        except Exception:  # noqa: BLE001
            return
        if report.get("violations"):
            logger.warning(
                "controlled launch cancelled after modifying protected paths: %s",
                [item.get("path") for item in report["violations"][:5]],
            )

    # -- 证据载荷 ----------------------------------------------------------

    def _isolation_payload(
        self,
        hardening: Any,
        permit: LaunchPermit,
        spec: LaunchSpec,
        mode: str,
        enforce_write_scope: bool,
    ) -> dict:
        payload = hardening.as_dict() if hardening is not None else {}
        payload["permit"] = permit.as_dict()
        payload["write_scope"] = {
            "allowed_roots": list(permit.allowed_roots),
            "pre_enforced": bool(enforce_write_scope),
            "labeled_low_during_execution": bool(
                enforce_write_scope and permit.allowed_roots
            ),
            "restored_after_execution": list(self._touched_roots),
        }
        payload["capability_boundaries"] = self._capability_boundaries(
            spec, permit, mode, enforce_write_scope
        )
        return payload

    def _capability_boundaries(
        self,
        spec: LaunchSpec,
        permit: LaunchPermit,
        mode: str,
        enforce_write_scope: bool,
    ) -> list[str]:
        boundaries = ["read_not_isolated：Low IL 不阻止读取当前账户可读的内容"]
        if permit.network_hosts is None:
            boundaries.append(
                "network_not_isolated：本机没有强制网络隔离（可发往任意可达主机）"
            )
        if mode == PermissionMode.UNRESTRICTED.value:
            boundaries.append("unrestricted_mode：无限制模式不建立写范围/Low IL 边界")
        elif not isolation.supported():
            boundaries.append(
                "posix_no_forced_isolation：POSIX 上 isolation 明确 unsupported，"
                "本次没有内核强制边界（与改动前一致）"
            )
        if enforce_write_scope:
            boundaries.append(
                "write_scope_enforced_by_low_il：仅靠 Windows MIC 限制写范围；"
                "不是容器/账户隔离，读与网络不受限"
            )
            if str(spec.kind) in ("shell", "script"):
                boundaries.append(
                    "delete_not_recycled_within_allowed_roots：任意解释器/脚本可在获准写范围内"
                    "直接 unlink，无法强制改道系统回收站；只能每次审批 + 范围限制 + 事实上报"
                )
        else:
            boundaries.append(
                "write_scope_not_pre_enforced：普通命令的写范围只做事后核验分类，"
                "没有事前阻断（任意程序/脚本的副作用无法在启动前可靠限定）"
            )
            boundaries.append(
                "interpreter_side_effects_not_containable：解释器/脚本可执行任意副作用"
                "（写/删/外发）；受控入口只能保证「执行的是许可时的那个程序/内容」+ 事后审计"
            )
        return boundaries

    # -- 写范围标签恢复（仅事前强制路径） ---------------------------------

    def _record_root_labels(self, roots: Sequence[str]) -> None:
        if not isolation.supported():
            return
        for root in roots:
            self._touched_roots.append(
                {
                    "root": str(root),
                    "previous_label": isolation.integrity_label_of(str(root)),
                    "restored": False,
                }
            )

    def _restore_write_scope(self) -> None:
        """执行结束后把临时标 Low 的授权写范围恢复为 Medium（尽力而为，留证据）。

        诚实边界：恢复的是「目录及子项 = Medium」；原本就带 Low 标签的条目保持原样。
        恢复失败只记录在隔离证据里，不覆盖主结果。
        """
        touched = list(self._touched_roots)
        self._touched_roots = []
        if not isolation.supported():
            return
        for item in touched:
            root = item.get("root")
            if not root:
                continue
            previous = str(item.get("previous_label") or "")
            if isolation.label_is_low(previous):
                item["restored"] = True
                item["note"] = "原本就是 Low 完整性标签：保持原样"
                continue
            try:
                done = __import__("subprocess").run(
                    ["icacls", str(root), "/setintegritylevel", "(OI)(CI)M", "/T"],
                    capture_output=True,
                    timeout=60,
                )
                item["restored"] = bool(done.returncode == 0)
                item["restored_to"] = isolation.integrity_label_of(str(root))
            except Exception as exc:  # noqa: BLE001 - 恢复失败只记证据
                item["restored"] = False
                item["restore_error"] = f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _looks_like_own_runtime(path: Any) -> bool:
    name = Path(str(path)).name.lower()
    return any(name.endswith(pattern) for pattern in _OWN_RUNTIME_SUFFIXES)


def _path_under_any(path: str, roots: Sequence[str]) -> bool:
    if not path:
        return False
    try:
        candidate = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            base = Path(root).resolve()
            if candidate == base or candidate.is_relative_to(base):
                return True
        except (OSError, ValueError):
            continue
    return False


def _cap_text(raw: bytes, limit: int) -> str:
    text = (raw or b"").decode("utf-8", errors="replace")
    if len(text.encode("utf-8", errors="replace")) <= limit:
        return text
    clipped = text.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="replace")
    return clipped + f"{chr(10)}…（输出已截断，上限 {limit} 字节）"


__all__ = [
    "LaunchSpec",
    "LaunchPermit",
    "LaunchResult",
    "LaunchUnavailable",
    "ControlledLauncher",
    "build_spec_facts",
    "compute_spec_digest",
    "build_permit",
    "normalize_mode_value",
    "recheck_approved_spec",
    "write_roots_for",
]
