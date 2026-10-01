"""项目级依赖环境：QIO 管理的专用 Python（按需准备，绝不碰系统环境）。

为什么需要它：声明了第三方依赖也没有任何地方能把它装上 —— 工具永远跑不起来，
用户还得自己猜要装什么、装到哪。这里给每个**依赖集合**准备一个专用虚拟环境：
按需创建、只装声明过的包、记录指纹；之后这个项目的测试与运行都用它。

依赖可复现（这一层以前没有「锁定」）：

* 安装时向 pip 要一份 `--report`（pip 自己的解析结果），把它落成**锁定清单**：
  包名、解析到的版本、实际下载地址（URL 里的凭据与签名串一律不记）、产物的
  sha256、以及 Python 版本与平台；
* **环境身份**（目录名）= 「依赖集合 + Python 主次版本 + 平台」的指纹，不再只按
  依赖集合 —— 换 Python（3.11 → 3.12）不会再把旧环境当成可用；
* 锁定清单同时存一份在 `tool-envs/locks/<指纹>/`，**删环境不会删掉它**：重建时
  优先按记录里的精确版本安装（有哈希就进 pip 的哈希校验模式）；锁定版本这次装不
  上的时候，如实说明「已按声明的约束重新解析」，绝不假装还是原来那批版本；
* 「就绪」不只看有没有记录：还要逐包核对环境里真的装着锁定清单里的那些版本
  （有人手动 pip install -U 过就会失配 → 明确要求重新准备）。

环境生命周期（清理）：

* `inventory()` 列出所有环境（谁在引用、最后使用时间、状态、锁定版本）；
  `cleanup_candidates()` 只挑 orphan；`remove()` / `cleanup()` 负责删除；
* 删除必须显式 `confirm=True`；**仍在被已注册工具引用的环境，不提示就不删**：
  没有引用信息时保守拒绝删「可用」的环境，`allow_in_use=True` 才会删，并在结果里
  点名会被影响的工具；
* 删除保留锁定清单，所以工具下次会明确提示「专用环境需要重新准备」，且重建是
  同一批版本。

边界（说清楚，不夸大）：

* 只装 `requirements` 里声明的东西；不升级、不改系统环境、不注入凭据；
* 安装是一次网络 + 磁盘动作，**必须先拿到用户确认**（`dependency_install`）；
* 哈希来自 pip 报告的下载产物（索引提供的 sha256），锁的是**产物**：换索引、或
  索引上换了同一版本的另一个构建，校验会失败并如实报错 —— 这正是「不静默换版本」
  想要的；没有哈希（老 pip、本地路径安装）时只锁版本号，并在清单里标明；
* **不自己实现包管理器**：解析、下载、哈希校验都交给 pip，这里只负责记录与核对；
* 环境没准备好时**不静默回落到随包解释器** —— 那等于假装依赖已经装上了。

运维入口：`python -m agent.tools.tool_envs list|remove|cleanup --root <tool-envs>`。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

DEFAULT_INSTALL_TIMEOUT_SECONDS = 600.0
MANIFEST_NAME = "qio-env.json"
LOCK_NAME = "requirements.lock"
LOCK_RECORD_NAME = "lock.json"
LOCKS_DIR_NAME = "locks"
# 2 = 身份指纹 + 锁定清单（包/版本/来源/哈希/Python/平台）。1 = 只有依赖集合。
MANIFEST_SCHEMA = 2
_LEGACY_MANIFEST_SCHEMA = 1
# 安装输出只留末尾这些字符：报错要看得到，内存不能被 pip 的长日志吃掉。
_OUTPUT_TAIL_CHARS = 2000
# pip 的机器可读解析报告（pip >= 22.2）；读完就删，不留在环境里。
_PIP_REPORT_NAME = ".qio-pip-report.json"
# 「最后使用时间」写盘的最小间隔：工具调用很频繁，记账不该每次都写盘。
TOUCH_INTERVAL_SECONDS = 60.0

# 容器路径（E3）：与 tools/sandbox.py 探测的是同一个 docker 命令行。
# 这里不 import sandbox：环境管理器只负责「把镜像准备好」，执行由 sandbox 决定。
CONTAINER_BINARY = "docker"
CONTAINER_DOCKERFILE_NAME = "Dockerfile"
CONTAINER_CONTEXT_DIR = "containers"
CONTAINER_PROBE_TIMEOUT_SECONDS = 30.0

EnvRunner = Callable[..., Awaitable[tuple[bool, str]]]


# -- 小工具 ---------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(text: str | None) -> str:
    """错误详情里可能出现索引地址/URL：进用户可见的消息前过一遍脱敏。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        from agent.trace.redact import redact_text
    except Exception:  # noqa: BLE001 - 脱敏不可用不该让环境准备整体失败
        return raw
    return redact_text(raw)


def _normalize_requirements(requirements: Iterable[str]) -> list[str]:
    """规范化依赖集合：去空白、去重、排序（顺序无关 → 同一个环境）。"""
    seen: list[str] = []
    for item in requirements:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return sorted(seen)


def _requirements_key(requirements: Iterable[str]) -> str:
    """依赖集合的规范化键（用于「哪些工具引用了这个环境」）。"""
    return json.dumps(_normalize_requirements(requirements), ensure_ascii=False, separators=(",", ":"))


def _legacy_key_for(requirements: Iterable[str]) -> str:
    """旧版（schema 1）的目录名算法：只按依赖集合。

    只用于把升级前建的环境目录归属到声明它的工具上（清理时才知道谁在用），
    新环境不再用这个算法。
    """
    payload = json.dumps(_normalize_requirements(requirements), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _dist_info_key(text: str) -> str:
    """PEP 503 归一化：`Foo_Bar-1.0.dist-info` 与 `foo-bar` 等价。"""
    return re.sub(r"[-_.]+", "-", str(text).strip()).lower()


def _sanitize_url(url: str) -> str:
    """只留 scheme://host/path：URL 里的凭据（私有索引常见）与签名查询串一概不记。"""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit
    except Exception:  # noqa: BLE001 - 不可能发生，保守返回去掉查询串的原文
        return text.split("?", 1)[0]
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.split("?", 1)[0]
    if not parts.netloc:
        return text.split("?", 1)[0]
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def _output_tail(text: str | None) -> str:
    return _redact(text)[-_OUTPUT_TAIL_CHARS:]


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_text(path: Path, text: str) -> None:
    """先写临时文件再替换：中途崩了不会留下半份记录。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json(path: Path, data: dict) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _dir_size(directory: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory, onerror=lambda _exc: None):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, name))
    return total


def _read_pyvenv_cfg(directory: Path) -> dict:
    """真实建出来的环境用哪个 Python：`pyvenv.cfg` 是 venv 自己写的，比猜可靠。"""
    record: dict[str, Any] = {"version": None, "major_minor": None, "base": None, "source": "unknown"}
    try:
        text = (directory / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return record
    record["source"] = "pyvenv.cfg"
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key == "version":
            record["version"] = value
            parts = value.split(".")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                record["major_minor"] = f"{parts[0]}.{parts[1]}"
        elif key in {"home", "base-executable", "executable"} and value and not record["base"]:
            # 取第一个（venv 自己写的 home）：setdefault 会被初始的 None 挡住，别用。
            record["base"] = value
    return record


def _report_unsupported(output: str | None) -> bool:
    """老 pip（< 22.2）不认识 `--report`：只认这几种报错，别的失败不算。"""
    text = (output or "").lower()
    return "--report" in text and (
        "no such option" in text or "unrecognized arguments" in text or "unknown option" in text
    )


def _packages_from_report(path: Path) -> tuple[list[dict], str]:
    """解析 pip 的 `--report`：解析结果 + 实际下载地址 + 产物哈希。"""
    data = _read_json(path)
    if data is None:
        return [], ""
    packages: list[dict] = []
    for item in data.get("install") or []:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") or {}
        name = str(metadata.get("name") or "").strip()
        version = str(metadata.get("version") or "").strip()
        if not name or not version:
            continue
        info = item.get("download_info") or {}
        archive = info.get("archive_info") or {}
        hashes = archive.get("hashes") or {}
        digest = str(hashes.get("sha256") or "")
        if not digest:
            raw = str(archive.get("hash") or "")
            if raw.startswith("sha256="):
                digest = raw.split("=", 1)[1]
        packages.append(
            {
                "name": name,
                "version": version,
                "source": _sanitize_url(str(info.get("url") or "")),
                "hash": f"sha256:{digest}" if digest else "",
                "requested": bool(item.get("requested")),
            }
        )
    # 注意：报告顶层 `version` 是**报告格式**版本（"1"），
    # 真正装包的是 `pip_version` —— 记错了就成了「installer: 1」这种假信息。
    return packages, str(data.get("pip_version") or data.get("version") or "")


def _packages_from_freeze(output: str | None) -> list[dict]:
    """没有 `--report` 时的退路：`pip freeze` 至少能给出精确版本（没有来源与哈希）。"""
    packages: list[dict] = []
    for line in (output or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("-") or "==" not in text:
            continue
        name, _, version = text.partition("==")
        name, version = name.strip(), version.strip()
        if name and version:
            packages.append(
                {"name": name, "version": version, "source": "", "hash": "", "requested": False}
            )
    return packages


def _pin(package: Mapping[str, Any]) -> str:
    return f"{package.get('name')}=={package.get('version')}"


def _pin_list(record: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(record, Mapping):
        return []
    packages = record.get("packages") or []
    return [_pin(item) for item in packages if isinstance(item, Mapping)]


def _render_lock_file(packages: Sequence[Mapping[str, Any]], with_hashes: bool) -> str:
    """pip 可直接消费的锁定文件：精确版本；每个包都有哈希时写进 `--hash=`。

    带 `--hash=` 的 requirements 文件会让 pip 自动进入哈希校验模式 —— 索引上换了
    同一版本的另一个构建时安装会失败，而不是静默换成别的产物。
    """
    header = "# 由 QIO 自动生成（pip --report 的解析结果）：精确版本"
    header += "，并带 sha256 校验。" if with_hashes else "（这次没有可用的产物哈希，只锁版本号）。"
    lines = [header]
    for item in sorted(packages, key=lambda p: (str(p.get("name", "")).lower(), str(p.get("version", "")))):
        line = f"{item.get('name')}=={item.get('version')}"
        if with_hashes and item.get("hash"):
            line += f" --hash={item['hash']}"
        lines.append(line)
    return "\n".join(lines) + "\n"


# -- 安装执行（默认实现） --------------------------------------------------


def _clean_env() -> dict[str, str]:
    """安装用的环境：系统必需项 + 无业务变量、无凭据。"""
    keep = (
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
    )
    env = {key: os.environ[key] for key in keep if os.environ.get(key)}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def _default_runner(argv: list[str], timeout: float, cwd: str | None = None):
    """真的把命令跑起来（默认实现）。返回 (成功与否, 输出末尾)。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=_clean_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return False, f"无法启动安装命令：{exc}"
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        return False, f"安装超时（超过 {timeout:g} 秒）"
    text = output.decode("utf-8", errors="replace").strip()
    return process.returncode == 0, text[-_OUTPUT_TAIL_CHARS:]


# -- 结论与目录清单 --------------------------------------------------------


@dataclass(frozen=True)
class EnvStatus:
    """一次「环境是否可用」的结论：不编造、不含糊。"""

    ok: bool
    interpreter: str | None
    reason: str | None = None
    reused: bool = False
    installed: list[str] = field(default_factory=list)
    # 环境身份指纹（目录名）；调用方拿它去做日志/清理，不必自己再算一遍。
    fingerprint: str | None = None
    # "resolved" 全新解析 / "locked" 按锁定清单 / "re-resolved" 锁定不可用后重解析。
    resolution: str | None = None
    lock_available: bool = False
    note: str | None = None


@dataclass(frozen=True)
class ToolEnvEntry:
    """一个专用环境（或只剩锁定清单的记录）的现状。"""

    fingerprint: str
    directory: str | None
    requirements: list[str]
    # ready / incomplete / legacy / unknown / lock-only
    state: str
    ready: bool
    interpreter: str | None
    created_at: str | None
    last_used_at: str | None
    referenced_by: list[str]
    lock_available: bool
    locked_packages: list[str]
    size_bytes: int | None = None
    detail: str | None = None

    @property
    def orphan(self) -> bool:
        """没有已注册工具引用它（不代表可以随便删：删除仍要显式确认）。"""
        return not self.referenced_by


@dataclass(frozen=True)
class CleanupResult:
    ok: bool
    fingerprint: str | None = None
    directory: str | None = None
    removed: bool = False
    reason: str | None = None
    kept_lock: bool = False
    referenced_by: list[str] = field(default_factory=list)
    next_step: str | None = None


@dataclass(frozen=True)
class CleanupReport:
    candidates: int
    removed: list[CleanupResult] = field(default_factory=list)
    skipped: list[CleanupResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """没有跳过任何一个才算「清理完成」；跳过的一定带原因。"""
        return not self.skipped


@dataclass(frozen=True)
class ContainerStatus:
    """容器执行路径的依赖镜像现状（准备侧；执行侧由 tools/sandbox.py 接线）。"""

    ok: bool
    image: str | None = None
    base_image: str | None = None
    reused: bool = False
    built: bool = False
    reason: str | None = None
    note: str | None = None
    lock_available: bool = False
    pinned: list[str] = field(default_factory=list)


class ToolEnvManager:
    """按依赖集合管理专用 Python 环境，并记录可复现的版本锁定。"""

    def __init__(
        self,
        root: str | Path,
        *,
        base_python: str | None = None,
        runner: EnvRunner | None = None,
        timeout_seconds: float = DEFAULT_INSTALL_TIMEOUT_SECONDS,
        touch_interval_seconds: float = TOUCH_INTERVAL_SECONDS,
    ) -> None:
        self.root = Path(root)
        # 用哪个 Python 去建环境：随包的这一个（冻结态由 executor_env 保证不是后端 exe）。
        self.base_python = base_python or sys.executable
        self._runner: EnvRunner = runner or _default_runner
        self.timeout_seconds = timeout_seconds
        self.touch_interval_seconds = touch_interval_seconds

    # -- 身份与位置 -------------------------------------------------------

    def identity_for(self, requirements: Sequence[str]) -> dict:
        """环境身份：依赖集合 + 建它的 Python + 平台。目录名就是它的指纹。"""
        return {
            "requirements": _normalize_requirements(requirements),
            "python": {
                "implementation": sys.implementation.name,
                "major_minor": f"{sys.version_info[0]}.{sys.version_info[1]}",
                "platform": sysconfig.get_platform(),
            },
            "system": {"os": os.name, "platform": sys.platform, "machine": platform.machine()},
        }

    def fingerprint_for(self, requirements: Sequence[str]) -> str:
        payload = {"schema": MANIFEST_SCHEMA, **self.identity_for(requirements)}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def key_for(self, requirements: Sequence[str]) -> str:
        """历史名字：环境身份指纹（同一组依赖、同一 Python、同一平台 → 同一个环境）。"""
        return self.fingerprint_for(requirements)

    @property
    def interpreter_name(self) -> str:
        return "Scripts/python.exe" if os.name == "nt" else "bin/python"

    def directory_for(self, requirements: Sequence[str]) -> Path:
        return self.root / self.fingerprint_for(requirements)

    def interpreter_for(self, requirements: Sequence[str]) -> str:
        return str(self.directory_for(requirements) / self.interpreter_name)

    def _manifest_path(self, requirements: Sequence[str]) -> Path:
        return self.directory_for(requirements) / MANIFEST_NAME

    @property
    def locks_root(self) -> Path:
        return self.root / LOCKS_DIR_NAME

    def lock_directory(self, requirements: Sequence[str]) -> Path:
        return self.locks_root / self.fingerprint_for(requirements)

    def lock_file_for(self, requirements: Sequence[str]) -> Path:
        return self.lock_directory(requirements) / LOCK_NAME

    def lock_record_for(self, requirements: Sequence[str]) -> Path:
        return self.lock_directory(requirements) / LOCK_RECORD_NAME

    # -- 读取 -------------------------------------------------------------

    def _read_manifest(self, requirements: Sequence[str]) -> dict | None:
        """环境记录必须是「这个指纹、这份依赖集合、这个 schema」才算数。"""
        data = _read_json(self._manifest_path(requirements))
        if data is None or data.get("schema") != MANIFEST_SCHEMA:
            return None
        if data.get("fingerprint") != self.fingerprint_for(requirements):
            return None
        recorded = data.get("requirements")
        if not isinstance(recorded, list):
            return None
        if _normalize_requirements(recorded) != _normalize_requirements(requirements):
            return None
        return data

    def lock_for(self, requirements: Sequence[str]) -> dict | None:
        """持久锁定记录：删了环境也还在（在 `tool-envs/locks/` 下）。"""
        record = _read_json(self.lock_record_for(requirements))
        if record is None or record.get("fingerprint") != self.fingerprint_for(requirements):
            return None
        return record

    def locked_packages_for(self, requirements: Sequence[str]) -> list[str]:
        """已锁定版本的 `名字==版本` 列表（没有记录就返回空）。"""
        record = self.lock_for(requirements)
        return _pin_list(record)

    # -- 就绪判定 ---------------------------------------------------------

    def _site_packages(self, directory: Path) -> Path | None:
        candidates = [directory / "Lib" / "site-packages"]
        lib = directory / "lib"
        if lib.is_dir():
            candidates += sorted(lib.glob("python*/site-packages"))
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return None

    def _installed_distributions(self, site_packages: Path) -> set[str] | None:
        try:
            with os.scandir(site_packages) as entries:
                return {_dist_info_key(entry.name) for entry in entries}
        except OSError:
            return None

    def verify_lock(self, requirements: Sequence[str]) -> tuple[bool, str | None]:
        """环境里真的装着锁定清单里的那些版本吗（只看 dist-info，不跑子进程）。"""
        manifest = self._read_manifest(requirements)
        if manifest is None:
            return False, "没有这个环境的记录（没有记录就谈不上锁定）"
        packages = [item for item in (manifest.get("packages") or []) if isinstance(item, dict)]
        if not packages:
            # 连冻结清单都没有（老 pip、`--report`/`freeze` 都失败）：不假装能核对。
            return True, None
        site_packages = self._site_packages(self.directory_for(requirements))
        if site_packages is None:
            return False, "环境里找不到 site-packages"
        installed = self._installed_distributions(site_packages)
        if installed is None:
            return False, "读不出环境里已安装的包"
        missing = [
            _pin(item)
            for item in packages
            if _dist_info_key(f"{item.get('name')}-{item.get('version')}.dist-info") not in installed
        ]
        if missing:
            shown = "、".join(missing[:3]) + ("…" if len(missing) > 3 else "")
            return False, f"已安装的包与锁定版本不一致：{shown}"
        return True, None

    def readiness(self, requirements: Sequence[str]) -> tuple[bool, str | None]:
        """(是否可用, 不可用的原因)。只查不建。"""
        wanted = _normalize_requirements(requirements)
        if not wanted:
            return True, None
        directory = self.directory_for(wanted)
        if not directory.is_dir():
            return False, "这个依赖集合还没有准备过专用环境"
        manifest = self._read_manifest(wanted)
        if manifest is None:
            raw = _read_json(directory / MANIFEST_NAME)
            if isinstance(raw, dict) and raw.get("schema") == _LEGACY_MANIFEST_SCHEMA:
                return False, "只有旧格式的记录（没有身份与锁定），需要重新准备"
            if raw is not None:
                return False, "记录与当前 Python/平台的身份对不上，需要重新准备"
            return False, "环境目录没有记录（上次准备可能中断了）"
        if not Path(self.interpreter_for(wanted)).is_file():
            return False, "记录在，但解释器文件已经不在了"
        return self.verify_lock(wanted)

    def is_ready(self, requirements: Sequence[str]) -> bool:
        """环境真的可用 = 身份对得上 **且** 解释器真的在 **且** 锁定版本装对了。"""
        wanted = _normalize_requirements(requirements)
        if not wanted:
            return False
        return self.readiness(wanted)[0]

    def mark_used(self, requirements: Sequence[str]) -> None:
        """记一次「这个环境刚被用到」。写盘有节流；记账失败不影响工具调用。"""
        wanted = _normalize_requirements(requirements)
        if not wanted:
            return
        manifest = self._read_manifest(wanted)
        if manifest is None:
            return
        now = datetime.now(timezone.utc)
        last = manifest.get("last_used_at")
        if isinstance(last, str) and last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < self.touch_interval_seconds:
                    return
            except ValueError:
                pass
        manifest["last_used_at"] = now.isoformat()
        with contextlib.suppress(OSError, ValueError):
            _write_json(self._manifest_path(wanted), manifest)
            record = self.lock_for(wanted)
            if record is not None:
                record["last_used_at"] = manifest["last_used_at"]
                record["updated_at"] = record.get("updated_at") or manifest["last_used_at"]
                _write_json(self.lock_record_for(wanted), record)

    def _needs_preparation_reason(self, requirements: list[str], why: str | None) -> str:
        """没准备好时给模型/用户的话：说清怎么补上，以及重建用的是不是锁定版本。"""
        text = "这个工具声明了第三方依赖，专用环境还没准备好"
        if why:
            text += f"（{why}）"
        text += "：需要先跑一次测试（会问你是否允许安装依赖）再调用它。"
        locked = self.locked_packages_for(requirements)
        if locked:
            shown = "、".join(locked[:3]) + (f"…等 {len(locked)} 个包" if len(locked) > 3 else "")
            text += f"重建会复用已记录的锁定版本（{shown}），不会再解析成别的版本。"
        else:
            text += "目前没有已记录的锁定版本：重建时会按声明重新解析依赖版本。"
        return text

    def status_for(self, requirements: Sequence[str]) -> EnvStatus:
        """只查不建：没有依赖 → 用随包环境；有依赖但没准备好 → 明确说没准备好。"""
        wanted = _normalize_requirements(requirements)
        if not wanted:
            return EnvStatus(ok=True, interpreter=None)
        fingerprint = self.fingerprint_for(wanted)
        ready, why = self.readiness(wanted)
        if ready:
            self.mark_used(wanted)
            manifest = self._read_manifest(wanted) or {}
            return EnvStatus(
                ok=True,
                interpreter=self.interpreter_for(wanted),
                reused=True,
                fingerprint=fingerprint,
                resolution=manifest.get("resolution"),
                lock_available=self.lock_for(wanted) is not None,
            )
        return EnvStatus(
            ok=False,
            interpreter=None,
            reason=self._needs_preparation_reason(wanted, why),
            fingerprint=fingerprint,
            lock_available=self.lock_for(wanted) is not None,
        )

    # -- 准备 -------------------------------------------------------------

    async def ensure(
        self,
        requirements: Sequence[str],
        *,
        approvals=None,
        tool_name: str = "",
        task_id: str | None = None,
    ) -> EnvStatus:
        """按需准备专用环境；没有依赖时直接用随包环境（interpreter=None）。"""
        wanted = _normalize_requirements(requirements)
        if not wanted:
            return EnvStatus(ok=True, interpreter=None)
        fingerprint = self.fingerprint_for(wanted)
        lock = self.lock_for(wanted)
        if self.readiness(wanted)[0]:
            self.mark_used(wanted)
            manifest = self._read_manifest(wanted) or {}
            return EnvStatus(
                ok=True,
                interpreter=self.interpreter_for(wanted),
                reused=True,
                fingerprint=fingerprint,
                resolution=manifest.get("resolution"),
                lock_available=lock is not None,
            )
        if approvals is None:
            return EnvStatus(
                ok=False,
                interpreter=None,
                reason=(
                    "声明了第三方依赖，但没有可用的确认通道来征求安装许可："
                    "这次没有准备专用环境。"
                ),
                fingerprint=fingerprint,
                lock_available=lock is not None,
            )
        decision = await approvals.request(
            "dependency_install",
            self._approval_payload(wanted, tool_name=tool_name, task_id=task_id, lock=lock),
        )
        if getattr(decision, "decision", None) != "approved":
            return EnvStatus(
                ok=False,
                interpreter=None,
                reason="你（或超时）没有同意安装依赖：这次没有准备专用环境，测试未执行。",
                fingerprint=fingerprint,
                lock_available=lock is not None,
            )
        return await self._prepare(wanted, fingerprint=fingerprint)

    def _approval_payload(
        self, requirements: list[str], *, tool_name: str, task_id: str | None, lock: dict | None
    ) -> dict:
        detail = (
            "这个工具声明了第三方依赖，需要为它准备一个专用环境并安装："
            + "、".join(requirements)
            + "。只装这些包，不升级其它东西，也不碰系统环境。"
        )
        locked = _pin_list(lock)
        if locked:
            shown = "、".join(locked[:3]) + ("…" if len(locked) > 3 else "")
            detail += f"这次按已记录的锁定版本重建（{shown}），不会重新解析成别的版本。"
        else:
            detail += "这次会把 pip 解析到的精确版本记进锁定清单，以后重建按同一批版本装。"
        return {
            "packages": list(requirements),
            "tool": tool_name,
            "task_id": task_id,
            "detail": detail,
            "lock": {"available": bool(lock), "packages": locked},
        }

    async def _install(
        self,
        *,
        interpreter: str,
        requirements: list[str],
        directory: Path,
        lock_file: Path | None,
    ) -> tuple[bool, str, str, str, list[dict]]:
        """装依赖并拿到「装了什么」。返回 (成功, 输出, 锁定保真度, pip 版本, 包列表)。"""
        report_path = directory / _PIP_REPORT_NAME
        with contextlib.suppress(OSError):
            report_path.unlink()
        base = [interpreter, "-m", "pip", "install", "--disable-pip-version-check"]
        target = ["-r", str(lock_file)] if lock_file is not None else list(requirements)
        ok, output = await self._runner(
            base + ["--report", str(report_path)] + target, self.timeout_seconds, str(directory)
        )
        if not ok and _report_unsupported(output):
            # 老 pip 没有 `--report`：退回「先装、再问它装了什么」，版本仍可锁定。
            with contextlib.suppress(OSError):
                report_path.unlink()
            ok, output = await self._runner(base + target, self.timeout_seconds, str(directory))
            if not ok:
                return False, output, "unknown", "", []
            fidelity, installer, packages = await self._freeze(interpreter, directory)
            return True, output, fidelity, installer, packages
        if not ok:
            with contextlib.suppress(OSError):
                report_path.unlink()
            return False, output, "unknown", "", []
        packages, installer = _packages_from_report(report_path)
        fidelity = "pip-report" if packages else "none"
        if not packages:
            # 报告不可用（老 pip / 报告损坏）：至少用 pip freeze 记下装出来的版本。
            fidelity, installer, packages = await self._freeze(interpreter, directory)
        with contextlib.suppress(OSError):
            report_path.unlink()
        return True, output, fidelity, installer, packages

    async def _freeze(self, interpreter: str, directory: Path) -> tuple[str, str, list[dict]]:
        """退路：`pip freeze` 拿精确版本（没有来源与哈希，保真度如实标 freeze）。"""
        ok, output = await self._runner(
            [interpreter, "-m", "pip", "freeze", "--disable-pip-version-check"],
            self.timeout_seconds,
            str(directory),
        )
        packages = _packages_from_freeze(output) if ok else []
        if not packages:
            return "none", "", []
        return "freeze", "", packages

    def _store_lock(
        self,
        requirements: list[str],
        fingerprint: str,
        packages: list[dict],
        *,
        fidelity: str,
        installer: str,
        mode: str,
        python_record: dict,
        note: str | None = None,
    ) -> dict:
        """把锁定清单写两份（环境目录 + 持久目录）；返回写入结果的账。

        两份都要：环境目录那份跟着环境走（核对用），持久那份删了环境也还在（重建用）。
        """
        with_hashes = bool(packages) and all(item.get("hash") for item in packages)
        lock_text = _render_lock_file(packages, with_hashes)
        lock_sha = hashlib.sha256(lock_text.encode("utf-8")).hexdigest()
        created = _now()
        previous = self.lock_for(requirements)
        record: dict[str, Any] = {
            "schema": MANIFEST_SCHEMA,
            "fingerprint": fingerprint,
            "requirements": requirements,
            "identity": self.identity_for(requirements),
            "python": python_record,
            "created_at": (previous or {}).get("created_at") or created,
            "updated_at": created,
            "last_used_at": created,
            "resolution": mode,
            "installer": installer or "",
            "hashes": with_hashes,
            "fidelity": fidelity,
            "packages": packages,
        }
        if note:
            record["note"] = note
        stored = False
        error: str | None = None
        try:
            self.lock_directory(requirements).mkdir(parents=True, exist_ok=True)
            _write_text(self.lock_file_for(requirements), lock_text)
            _write_json(self.lock_record_for(requirements), record)
            stored = True
        except OSError as exc:
            error = f"锁定清单没能写入（{exc}）"
        return {
            "stored": stored,
            "error": error,
            "sha256": lock_sha,
            "hashes": with_hashes,
            "created_at": created,
        }

    async def _prepare(self, requirements: list[str], *, fingerprint: str) -> EnvStatus:
        directory = self.directory_for(requirements)
        try:
            directory.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return EnvStatus(False, None, f"无法创建专用环境的目录：{exc}", fingerprint=fingerprint)
        ok, output = await self._runner(
            [self.base_python, "-m", "venv", str(directory)],
            self.timeout_seconds,
            str(directory.parent),
        )
        if not ok:
            return EnvStatus(
                False,
                None,
                f"创建专用环境失败：{_output_tail(output) or '（没有输出）'}",
                fingerprint=fingerprint,
            )
        interpreter = self.interpreter_for(requirements)
        if not Path(interpreter).is_file():
            return EnvStatus(
                False, None, "专用环境建好了，但找不到它的 Python 解释器", fingerprint=fingerprint
            )
        python_record = _read_pyvenv_cfg(directory)
        expected = f"{sys.version_info[0]}.{sys.version_info[1]}"
        actual = python_record.get("major_minor")
        if actual and actual != expected:
            # 身份指纹按「当前后端的 Python」算：真建出来的版本对不上就不能记成可用。
            return EnvStatus(
                False,
                None,
                (
                    f"专用环境用的是 Python {actual}，与当前后端的 {expected} 不一致："
                    "已拒绝把它记成可用环境（依赖的 ABI 可能不匹配）。请检查 base_python 配置。"
                ),
                fingerprint=fingerprint,
            )
        lock_file = self.lock_file_for(requirements)
        use_lock = lock_file.is_file()
        mode = "locked" if use_lock else "resolved"
        note: str | None = None
        lock_failure: str | None = None
        ok, output, fidelity, installer, packages = await self._install(
            interpreter=interpreter,
            requirements=requirements,
            directory=directory,
            lock_file=lock_file if use_lock else None,
        )
        if not ok and use_lock:
            # 锁定版本这次装不上（索引上没了/哈希对不上/离线）：如实说清，再按声明解析。
            lock_failure = _output_tail(output)
            ok, output, fidelity, installer, packages = await self._install(
                interpreter=interpreter, requirements=requirements, directory=directory, lock_file=None
            )
            if ok:
                mode = "re-resolved"
                note = (
                    f"已记录的锁定版本这次装不上（{lock_failure[:200]}）："
                    "已按声明的约束重新解析，版本可能与上次不同。"
                )
        if not ok:
            reason = f"安装依赖失败：{_output_tail(output) or '（没有输出）'}"
            if lock_failure:
                reason += f"（按锁定版本安装也失败：{lock_failure[:200]}）"
            return EnvStatus(False, None, reason, fingerprint=fingerprint)
        stored = self._store_lock(
            requirements,
            fingerprint,
            packages,
            fidelity=fidelity,
            installer=installer,
            mode=mode,
            python_record=python_record,
            note=note,
        )
        created = stored["created_at"]
        lock_available = stored["stored"]
        lock_error = stored["error"]
        lock_sha = stored["sha256"]
        with_hashes = stored["hashes"]
        manifest: dict[str, Any] = {
            "schema": MANIFEST_SCHEMA,
            "fingerprint": fingerprint,
            "requirements": requirements,
            "created_at": created,
            "last_used_at": created,
            "resolution": mode,
            "python": python_record,
            "system": self.identity_for(requirements)["system"],
            "installer": installer or "",
            "fidelity": fidelity,
            "packages": packages,
            "lock": {
                "file": LOCK_NAME,
                "sha256": lock_sha,
                "packages": len(packages),
                "hashes": with_hashes,
                "stored": lock_available,
                "error": lock_error,
            },
        }
        if note:
            manifest["note"] = note
        elif lock_error:
            note = lock_error
        try:
            _write_json(self._manifest_path(requirements), manifest)
        except OSError as exc:
            return EnvStatus(False, None, f"安装完成但无法记录环境信息：{exc}", fingerprint=fingerprint)
        return EnvStatus(
            ok=True,
            interpreter=interpreter,
            reused=False,
            installed=sorted(requirements),
            fingerprint=fingerprint,
            resolution=mode,
            lock_available=lock_available,
            note=note,
        )

    # -- 容器执行路径的依赖（E3） -----------------------------------------

    async def _pip_capable_python(self, requirements: list[str]) -> tuple[str | None, str | None]:
        """找一个**带 pip** 的解释器来做「只解析」。

        为什么不直接用 base_python：uv 管理的 venv 默认不带 pip（实测 §uv run python -m pip§
        会 ModuleNotFoundError: No module named pip），而容器路径的解析不该因此失败。
        base_python 没有 pip 时，用 §python -m venv§ 建一个**空环境**（venv 的 pip 来自
        CPython 自带的 ensurepip，不需要联网）；这个目录以后宿主安装也复用，不浪费。
        """
        ok, _output = await self._runner(
            [self.base_python, "-m", "pip", "--version"], CONTAINER_PROBE_TIMEOUT_SECONDS, None
        )
        if ok:
            return self.base_python, None
        directory = self.directory_for(requirements)
        try:
            directory.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return None, f"无法创建专用环境的目录：{exc}"
        ok, output = await self._runner(
            [self.base_python, "-m", "venv", str(directory)], self.timeout_seconds, str(directory.parent)
        )
        if not ok:
            return None, f"创建用于解析的虚拟环境失败：{_output_tail(output) or '（没有输出）'}"
        interpreter = self.interpreter_for(requirements)
        if not Path(interpreter).is_file():
            return None, "虚拟环境建好了，但找不到它的 Python 解释器"
        ok, output = await self._runner(
            [interpreter, "-m", "pip", "--version"], CONTAINER_PROBE_TIMEOUT_SECONDS, str(directory)
        )
        if not ok:
            return None, f"虚拟环境里没有可用的 pip：{_output_tail(output) or '（没有输出）'}"
        return interpreter, None

    async def resolve_only(self, requirements: Sequence[str]) -> tuple[bool, list[dict], str]:
        """只解析、不安装：给容器镜像准备锁定清单（pip 的 `--dry-run --report`）。

        为什么需要它：容器路径要按锁定版本构建镜像；为了拿到锁定清单先在宿主上装一整套
        依赖是白装。`--dry-run` 只解析不落盘，`--ignore-installed` 让解析结果不受宿主
        已装包影响。**仍然只有 pip 一个解析器**：这里不自己算版本。
        """
        wanted = _normalize_requirements(requirements)
        if not wanted:
            return True, [], ""
        interpreter, problem = await self._pip_capable_python(wanted)
        if interpreter is None:
            return False, [], problem or "找不到带 pip 的解释器来解析依赖版本"
        report_path = self.locks_root / f".resolve-{self.fingerprint_for(wanted)}.json"
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            ok, output = await self._runner(
                [
                    interpreter, "-m", "pip", "install", "--disable-pip-version-check",
                    "--dry-run", "--ignore-installed", "--report", str(report_path), *wanted,
                ],
                self.timeout_seconds,
                str(report_path.parent),
            )
            if not ok:
                return False, [], output
            packages, installer = _packages_from_report(report_path)
            if not packages:
                # 没有解析结果就不能构建镜像：宁可不做，也不做一个「依赖是空的」镜像。
                return False, [], output + "\n（pip 没有给出解析报告，无法锁定版本）"
            return True, packages, installer
        finally:
            with contextlib.suppress(OSError):
                report_path.unlink()

    @property
    def container_root(self) -> Path:
        return self.root / CONTAINER_CONTEXT_DIR

    def container_python_tag(self) -> str:
        return f"{sys.version_info[0]}.{sys.version_info[1]}"

    def container_base_image(self) -> str:
        """基础镜像跟环境身份同一个 Python 主次版本：锁定清单是按它解析的。"""
        return f"python:{self.container_python_tag()}-slim"

    def container_image_for(self, requirements: Sequence[str]) -> str:
        """镜像 tag 就是这个环境的身份指纹：同一组依赖 + 同一 Python/平台 → 同一个镜像。"""
        return f"qio-tool-env:{self.container_python_tag()}-{self.fingerprint_for(requirements)}"

    def container_dockerfile(self, requirements: Sequence[str]) -> str:
        return (
            f"FROM {self.container_base_image()}\n"
            "# 只按锁定清单装：精确版本（有哈希就带 --hash，pip 会进哈希校验模式）。\n"
            "# 镜像按指纹 tag 复用，构建只发生一次；工具调用时不做任何联网解析/安装。\n"
            "COPY requirements.lock /tmp/qio-requirements.lock\n"
            "RUN python -m pip install --no-cache-dir --disable-pip-version-check \\\n"
            "      -r /tmp/qio-requirements.lock && rm -f /tmp/qio-requirements.lock\n"
        )

    def container_plan(self, requirements: Sequence[str]) -> dict:
        """容器依赖方案（纯数据：给诊断、界面与 sandbox 接线用）。"""
        wanted = _normalize_requirements(requirements)
        lock_file = self.lock_file_for(wanted) if wanted else None
        return {
            "image": self.container_image_for(wanted) if wanted else None,
            "base_image": self.container_base_image(),
            "lock_file": str(lock_file) if lock_file else None,
            "lock_available": bool(lock_file and lock_file.is_file()),
            "pinned": self.locked_packages_for(wanted) if wanted else [],
            "dockerfile": self.container_dockerfile(wanted) if wanted else "",
            "reuse": "本机已有这个 tag 的镜像 → 直接复用（不联网、不重装）；没有 → 用户同意后构建一次",
            "never": "不在每次工具调用时联网解析或安装依赖",
        }

    async def _docker_image_ready(self, image: str) -> bool:
        ok, _output = await self._runner(
            [CONTAINER_BINARY, "image", "inspect", image], CONTAINER_PROBE_TIMEOUT_SECONDS, None
        )
        return ok

    def _record_container(self, requirements: list[str], image: str) -> None:
        record = self.lock_for(requirements)
        if record is None:
            return
        record["container"] = {
            "image": image,
            "base": self.container_base_image(),
            "built_at": _now(),
            "hashes": bool(record.get("hashes")),
        }
        with contextlib.suppress(OSError, ValueError):
            _write_json(self.lock_record_for(requirements), record)

    def _container_approval_payload(
        self,
        requirements: list[str],
        *,
        tool_name: str,
        task_id: str | None,
        image: str,
        needs_resolve: bool,
    ) -> dict:
        detail = (
            "容器隔离执行 + 声明了第三方依赖：需要按锁定版本构建一个专用镜像（tag "
            + image
            + "）。构建一次之后按 tag 复用，**不会**在每次工具调用时联网安装。"
        )
        if needs_resolve:
            detail += (
                "目前还没有锁定清单：会先用 pip 做一次只解析不安装的解析（--dry-run），"
                "把精确版本记下来再构建。"
            )
        else:
            detail += "锁定清单已经存在，直接按它构建。"
        return {
            "packages": list(requirements),
            "tool": tool_name,
            "task_id": task_id,
            "detail": detail,
            "container": {
                "image": image,
                "base_image": self.container_base_image(),
                "resolve_only": needs_resolve,
            },
            "lock": {
                "available": not needs_resolve,
                "packages": self.locked_packages_for(requirements),
            },
        }

    async def ensure_container_image(
        self,
        requirements: Sequence[str],
        *,
        approvals=None,
        tool_name: str = "",
        task_id: str | None = None,
    ) -> ContainerStatus:
        """把「按锁定版本构建的镜像」准备好（构建一次，之后按 tag 离线复用）。

        顺序：锁定清单（没有就用 pip `--dry-run` 解析一次，宿主上什么都不装）→ 本机已有
        该 tag 的镜像就复用 → 否则拿到用户同意后 `docker build` 一次 → 记进持久锁定记录。
        容器执行本身由 tools/sandbox.py 决定（接线见给 lead 的 CROSS-ROUTE REQUEST）。
        """
        wanted = _normalize_requirements(requirements)
        if not wanted:
            return ContainerStatus(
                ok=True, note="没有依赖声明：容器路径用随包镜像即可，不需要专用镜像。"
            )
        fingerprint = self.fingerprint_for(wanted)
        image = self.container_image_for(wanted)
        base_image = self.container_base_image()
        lock_file = self.lock_file_for(wanted)
        pinned = self.locked_packages_for(wanted)
        if await self._docker_image_ready(image):
            return ContainerStatus(
                ok=True,
                image=image,
                base_image=base_image,
                reused=True,
                lock_available=lock_file.is_file(),
                pinned=pinned,
                note="本机已经有这个镜像（按锁定版本构建过）：直接复用，不联网、不重新安装。",
            )
        if approvals is None:
            return ContainerStatus(
                ok=False,
                image=image,
                base_image=base_image,
                lock_available=lock_file.is_file(),
                pinned=pinned,
                reason=(
                    "需要一个按锁定版本构建的容器镜像，但没有可用的确认通道来征求构建许可："
                    "这次没有构建，也不会在工具调用时临时 pip install。"
                ),
            )
        needs_resolve = not lock_file.is_file()
        decision = await approvals.request(
            "dependency_install",
            self._container_approval_payload(
                wanted,
                tool_name=tool_name,
                task_id=task_id,
                image=image,
                needs_resolve=needs_resolve,
            ),
        )
        if getattr(decision, "decision", None) != "approved":
            return ContainerStatus(
                ok=False,
                image=image,
                base_image=base_image,
                lock_available=lock_file.is_file(),
                pinned=pinned,
                reason="你（或超时）没有同意构建容器镜像：容器路径这次不可用（不会退回临时安装）。",
            )
        note: str | None = None
        if needs_resolve:
            resolved, packages, installer = await self.resolve_only(wanted)
            if not resolved:
                return ContainerStatus(
                    ok=False,
                    image=image,
                    base_image=base_image,
                    reason=(
                        "解析依赖版本失败（容器镜像要按锁定版本构建）："
                        + (_output_tail(installer) or "（没有输出）")
                    ),
                )
            stored = self._store_lock(
                wanted,
                fingerprint,
                packages,
                fidelity="pip-report-resolve-only",
                installer=installer,
                mode="resolved",
                python_record={
                    "version": ".".join(str(item) for item in sys.version_info[:3]),
                    "major_minor": self.container_python_tag(),
                    "base": self.base_python,
                    "source": "resolver（只解析，没有建环境）",
                },
                note=(
                    "这份锁定清单来自 pip --dry-run 解析（宿主上什么都没有安装），"
                    "供容器镜像构建使用。"
                ),
            )
            if not stored["stored"]:
                return ContainerStatus(
                    ok=False, image=image, base_image=base_image, reason=f"无法记录锁定清单：{stored['error']}"
                )
            pinned = self.locked_packages_for(wanted)
            lock_file = self.lock_file_for(wanted)
            note = "锁定清单是用 pip --dry-run 解析出来的（宿主上什么都没装）。"
        context = self.container_root / fingerprint
        try:
            context.mkdir(parents=True, exist_ok=True)
            (context / LOCK_NAME).write_text(lock_file.read_text(encoding="utf-8"), encoding="utf-8")
            (context / CONTAINER_DOCKERFILE_NAME).write_text(
                self.container_dockerfile(wanted), encoding="utf-8"
            )
        except OSError as exc:
            return ContainerStatus(
                ok=False, image=image, base_image=base_image, reason=f"无法准备容器镜像的构建上下文：{exc}"
            )
        ok, output = await self._runner(
            [
                CONTAINER_BINARY, "build",
                "-f", str(context / CONTAINER_DOCKERFILE_NAME),
                "-t", image,
                str(context),
            ],
            self.timeout_seconds,
            str(context),
        )
        if not ok:
            return ContainerStatus(
                ok=False,
                image=image,
                base_image=base_image,
                lock_available=lock_file.is_file(),
                pinned=pinned,
                reason=f"构建容器镜像失败：{_output_tail(output) or '（没有输出）'}",
            )
        self._record_container(wanted, image)
        return ContainerStatus(
            ok=True,
            image=image,
            base_image=base_image,
            built=True,
            lock_available=True,
            pinned=pinned,
            note=((note + " ") if note else "")
            + (
                "镜像已按锁定版本构建并打上指纹 tag：下次调用直接用本机镜像"
                "（docker image inspect 命中即复用），不会每次联网装。"
            ),
        )

    # -- 生命周期：谁在引用、哪些是 orphan、怎么删 -------------------------

    def references_from_definitions(self, definitions: Iterable[Any]) -> dict[str, list[str]]:
        """把「工具有哪些依赖声明」整理成 依赖集合 → 工具名 的引用表。

        键有两种：规范化依赖集合，以及旧版（schema 1）目录名算法算出来的指纹 ——
        升级前建的环境目录也要能归属到它的工具上，否则清理时看不出谁在用。
        """
        table: dict[str, list[str]] = {}
        for definition in definitions or []:
            name = str(getattr(definition, "name", "") or "").strip()
            requirements = _normalize_requirements(getattr(definition, "requirements", None) or [])
            if not requirements:
                continue
            for key in {_requirements_key(requirements), _legacy_key_for(requirements)}:
                names = table.setdefault(key, [])
                if name and name not in names:
                    names.append(name)
        for names in table.values():
            names.sort()
        return table

    def _references_for(
        self, table: Mapping[str, Sequence[str]], requirements: Sequence[str], fingerprint: str
    ) -> list[str]:
        names: list[str] = []
        for key in (_requirements_key(requirements), fingerprint):
            for name in table.get(key, []) or []:
                if name not in names:
                    names.append(name)
        return sorted(names)

    def _entry_for(
        self, directory: Path, table: Mapping[str, Sequence[str]], *, with_size: bool
    ) -> ToolEnvEntry:
        fingerprint = directory.name
        raw = _read_json(directory / MANIFEST_NAME)
        requirements = (
            _normalize_requirements(raw.get("requirements") or []) if isinstance(raw, dict) else []
        )
        lock = _read_json(self.locks_root / fingerprint / LOCK_RECORD_NAME)
        state: str
        ready = False
        detail: str | None = None
        if (
            isinstance(raw, dict)
            and raw.get("schema") == MANIFEST_SCHEMA
            and raw.get("fingerprint") == fingerprint
            and requirements
        ):
            ready, why = self.readiness(requirements)
            state = "ready" if ready else "incomplete"
            detail = why
        elif isinstance(raw, dict) and raw.get("schema") == _LEGACY_MANIFEST_SCHEMA:
            state, detail = "legacy", "旧格式的记录（没有身份与锁定）：需要重新准备"
        elif raw is None:
            state, detail = "unknown", "没有环境记录：无法归属到哪个工具"
        else:
            state, detail = "incomplete", "记录与目录身份不一致：需要重新准备"
        packages = [item for item in (raw or {}).get("packages") or [] if isinstance(item, dict)]
        if not packages:
            packages = [item for item in (lock or {}).get("packages") or [] if isinstance(item, dict)]
        return ToolEnvEntry(
            fingerprint=fingerprint,
            directory=str(directory),
            requirements=requirements,
            state=state,
            ready=ready,
            interpreter=str(directory / self.interpreter_name) if state != "unknown" else None,
            created_at=(raw or lock or {}).get("created_at"),
            last_used_at=(raw or {}).get("last_used_at") or (lock or {}).get("last_used_at"),
            referenced_by=self._references_for(table, requirements, fingerprint),
            lock_available=(self.locks_root / fingerprint / LOCK_NAME).is_file(),
            locked_packages=_pin_list(lock),
            size_bytes=_dir_size(directory) if with_size else None,
            detail=detail,
        )

    def inventory(
        self,
        *,
        referenced_by: Mapping[str, Sequence[str]] | None = None,
        include_unknown: bool = True,
        with_size: bool = False,
    ) -> list[ToolEnvEntry]:
        """列出所有专用环境（含只剩锁定清单的），并标注谁在引用。

        `referenced_by` 用 `references_from_definitions()` 的结果（应用启动时手上有
        的就是注册工具的定义）：只有提供引用信息，才能判断哪些环境已经 orphan。
        """
        table: Mapping[str, Sequence[str]] = referenced_by or {}
        entries: list[ToolEnvEntry] = []
        seen: set[str] = set()
        if self.root.is_dir():
            for directory in sorted(self.root.iterdir()):
                if not directory.is_dir() or directory.name == LOCKS_DIR_NAME:
                    continue
                seen.add(directory.name)
                entries.append(self._entry_for(directory, table, with_size=with_size))
        if self.locks_root.is_dir():
            for lock_dir in sorted(self.locks_root.iterdir()):
                if not lock_dir.is_dir() or lock_dir.name in seen:
                    continue
                record = _read_json(lock_dir / LOCK_RECORD_NAME) or {}
                requirements = _normalize_requirements(record.get("requirements") or [])
                entries.append(
                    ToolEnvEntry(
                        fingerprint=lock_dir.name,
                        directory=None,
                        requirements=requirements,
                        state="lock-only",
                        ready=False,
                        interpreter=None,
                        created_at=record.get("created_at"),
                        last_used_at=record.get("last_used_at"),
                        referenced_by=self._references_for(table, requirements, lock_dir.name),
                        lock_available=(lock_dir / LOCK_NAME).is_file(),
                        locked_packages=_pin_list(record),
                        size_bytes=0 if with_size else None,
                        detail="环境目录不在了，但锁定清单还在：重建会按同一批版本装。",
                    )
                )
        if not include_unknown:
            entries = [entry for entry in entries if entry.state != "unknown"]
        return entries

    def cleanup_candidates(
        self,
        *,
        referenced_by: Mapping[str, Sequence[str]] | None = None,
        include_unknown: bool = False,
        with_size: bool = False,
    ) -> list[ToolEnvEntry]:
        """可以被清理的：没有工具引用的环境（默认跳过归属不明的目录）。"""
        return [
            entry
            for entry in self.inventory(
                referenced_by=referenced_by, include_unknown=True, with_size=with_size
            )
            if entry.orphan and (include_unknown or entry.state != "unknown")
        ]

    async def remove(
        self,
        fingerprint: str,
        *,
        confirm: bool = False,
        referenced_by: Mapping[str, Sequence[str]] | None = None,
        allow_in_use: bool = False,
        forget_lock: bool = False,
        include_unknown: bool = True,
    ) -> CleanupResult:
        """删掉一个专用环境（**保留锁定清单**，下次按同一批版本重建）。

        三道闸：显式确认；仍被已注册工具引用时不提示就不删；没有引用信息时对「可用」
        的环境保守拒绝。删除只删环境目录，`tool-envs/locks/` 下的锁定记录留着 ——
        那是「重建还是同一批版本」的依据。
        """
        wanted = str(fingerprint or "").strip()
        entries = {
            entry.fingerprint: entry
            for entry in self.inventory(
                referenced_by=referenced_by, include_unknown=include_unknown, with_size=False
            )
        }
        entry = entries.get(wanted)
        if entry is None:
            return CleanupResult(
                ok=False, fingerprint=wanted or None, reason="找不到这个专用环境（可能已经删掉了）"
            )
        if not confirm:
            return CleanupResult(
                ok=False,
                fingerprint=entry.fingerprint,
                directory=entry.directory,
                reason="删除专用环境需要显式确认（confirm=True）：它会连同装好的依赖一起删掉。",
                referenced_by=entry.referenced_by,
                next_step="确认要删就再调一次并传 confirm=True。",
            )
        references = list(entry.referenced_by)
        if references and not allow_in_use:
            return CleanupResult(
                ok=False,
                fingerprint=entry.fingerprint,
                directory=entry.directory,
                referenced_by=references,
                reason=(
                    "这个环境正在被已注册的工具使用："
                    + "、".join(references)
                    + "；删掉它们会立刻跑不起来。确认要删请传 allow_in_use=True。"
                ),
                next_step="更稳妥的做法是先改这些工具的依赖声明，或确认它们不再需要这个环境。",
            )
        if entry.ready and referenced_by is None and not allow_in_use:
            return CleanupResult(
                ok=False,
                fingerprint=entry.fingerprint,
                directory=entry.directory,
                referenced_by=[],
                reason=(
                    "没有提供「哪些工具有引用」的信息，无法确认这个可用环境没被使用："
                    "请先查引用（references_from_definitions），或显式传 allow_in_use=True。"
                ),
            )
        removed_env = False
        target = Path(entry.directory) if entry.directory else None
        if target is not None and target.is_dir():
            try:
                await asyncio.to_thread(shutil.rmtree, target)
            except OSError as exc:
                return CleanupResult(
                    ok=False,
                    fingerprint=entry.fingerprint,
                    directory=str(target),
                    referenced_by=references,
                    reason=f"删除环境目录失败：{exc}",
                )
            removed_env = True
        lock_directory = self.locks_root / entry.fingerprint
        removed_lock = False
        if forget_lock and lock_directory.is_dir():
            try:
                await asyncio.to_thread(shutil.rmtree, lock_directory)
            except OSError as exc:
                return CleanupResult(
                    ok=False,
                    fingerprint=entry.fingerprint,
                    directory=entry.directory,
                    removed=removed_env,
                    referenced_by=references,
                    reason=f"环境删掉了，但锁定清单删不掉：{exc}",
                )
            removed_lock = True
        if not removed_env and not removed_lock:
            reason = "这个环境目录已经不在了"
            if lock_directory.is_dir():
                reason += "，只剩下锁定清单（要一起删请传 forget_lock=True）"
            return CleanupResult(
                ok=False,
                fingerprint=entry.fingerprint,
                directory=entry.directory,
                referenced_by=references,
                reason=reason,
            )
        kept_lock = lock_directory.is_dir()
        next_step = "这些工具下次调用会明确提示「专用环境需要重新准备」，跑测试时会再问一次安装许可。"
        if kept_lock:
            pins = entry.locked_packages[:3]
            next_step += "锁定清单已保留" + (
                f"（{'、'.join(pins)}…）" if pins else ""
            ) + "，重建按同一批版本装。"
        else:
            next_step += "没有保留锁定清单：重建会重新解析依赖版本。"
        if references:
            next_step += " 注意：受影响的工具是 " + "、".join(references) + "。"
        return CleanupResult(
            ok=True,
            fingerprint=entry.fingerprint,
            directory=entry.directory,
            removed=True,
            referenced_by=references,
            kept_lock=kept_lock,
            reason="已删除专用环境" + ("（环境目录 + 锁定清单）" if removed_lock else "（锁定清单保留）"),
            next_step=next_step,
        )

    async def cleanup(
        self,
        *,
        confirm: bool = False,
        referenced_by: Mapping[str, Sequence[str]] | None = None,
        include_unknown: bool = False,
        allow_in_use: bool = False,
        with_size: bool = False,
    ) -> CleanupReport:
        """批量清理 orphan 环境：逐条走 `remove()` 的同一套闸门。"""
        candidates = self.cleanup_candidates(
            referenced_by=referenced_by, include_unknown=include_unknown, with_size=with_size
        )
        removed: list[CleanupResult] = []
        skipped: list[CleanupResult] = []
        for entry in candidates:
            result = await self.remove(
                entry.fingerprint,
                confirm=confirm,
                referenced_by=referenced_by,
                allow_in_use=allow_in_use,
                include_unknown=include_unknown,
            )
            (removed if result.removed else skipped).append(result)
        return CleanupReport(candidates=len(candidates), removed=removed, skipped=skipped)


# -- 运维入口 -------------------------------------------------------------


def _default_root() -> Path | None:
    try:
        from agent.config import Settings

        return Path(Settings().data_dir) / "tool-envs"
    except Exception:  # noqa: BLE001 - 读不到配置就让调用方显式给 --root
        return None


def _entry_to_dict(entry: ToolEnvEntry) -> dict:
    return {
        "fingerprint": entry.fingerprint,
        "directory": entry.directory,
        "requirements": entry.requirements,
        "state": entry.state,
        "ready": entry.ready,
        "orphan": entry.orphan,
        "referenced_by": entry.referenced_by,
        "created_at": entry.created_at,
        "last_used_at": entry.last_used_at,
        "lock_available": entry.lock_available,
        "locked_packages": entry.locked_packages,
        "size_bytes": entry.size_bytes,
        "detail": entry.detail,
    }


def _result_to_dict(result: CleanupResult) -> dict:
    return {
        "ok": result.ok,
        "fingerprint": result.fingerprint,
        "directory": result.directory,
        "removed": result.removed,
        "referenced_by": result.referenced_by,
        "kept_lock": result.kept_lock,
        "reason": result.reason,
        "next_step": result.next_step,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m agent.tools.tool_envs list|remove|cleanup --root <tool-envs>`。

    清理只删环境目录、保留锁定清单；没有引用信息时不删「可用」的环境（保守），
    归属不明的旧目录要 `--include-unknown` 才会进入候选。
    """
    parser = argparse.ArgumentParser(
        prog="python -m agent.tools.tool_envs",
        description="QIO 专用依赖环境：查看（list）与清理（remove / cleanup）",
    )
    parser.add_argument("--root", help="环境目录（默认：QIO 数据目录下的 tool-envs）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="列出环境、引用与最后使用时间")
    remove_parser = sub.add_parser("remove", help="删除一个环境（保留锁定清单）")
    remove_parser.add_argument("fingerprint")
    remove_parser.add_argument("--yes", action="store_true", help="确认删除")
    remove_parser.add_argument("--allow-in-use", action="store_true", help="即使仍被引用也删")
    remove_parser.add_argument("--forget-lock", action="store_true", help="连锁定清单一起删")
    cleanup_parser = sub.add_parser("cleanup", help="清理没有工具引用的环境")
    cleanup_parser.add_argument("--yes", action="store_true", help="确认删除")
    cleanup_parser.add_argument(
        "--include-unknown", action="store_true", help="把归属不明的目录也当成候选"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root) if args.root else _default_root()
    if root is None:
        print("需要 --root：读不到 QIO 数据目录（agent.config.Settings）。", file=sys.stderr)
        return 2
    manager = ToolEnvManager(root)

    if args.command == "list":
        entries = manager.inventory(include_unknown=True)
        if args.json:
            print(json.dumps([_entry_to_dict(entry) for entry in entries], ensure_ascii=False, indent=2))
            return 0
        if not entries:
            print(f"没有专用环境：{root}")
            return 0
        for entry in entries:
            who = "、".join(entry.referenced_by) if entry.referenced_by else "（没有工具引用）"
            locked = f"，锁定 {len(entry.locked_packages)} 个包" if entry.locked_packages else ""
            print(
                f"{entry.fingerprint}  [{entry.state}]{locked}  引用：{who}"
                f"  最后使用：{entry.last_used_at or '未知'}"
            )
            if entry.requirements:
                print(f"  依赖：{'、'.join(entry.requirements)}")
            if entry.detail:
                print(f"  说明：{entry.detail}")
        return 0

    if args.command == "remove":
        result = asyncio.run(
            manager.remove(
                args.fingerprint,
                confirm=bool(args.yes),
                allow_in_use=bool(args.allow_in_use),
                forget_lock=bool(args.forget_lock),
            )
        )
        if args.json:
            print(json.dumps(_result_to_dict(result), ensure_ascii=False, indent=2))
        else:
            print(
                f"{'已删除' if result.removed else '没有删除'}：{result.reason or ''}"
                + (f"\n下一步：{result.next_step}" if result.next_step else "")
            )
        return 0 if result.removed else 1

    report = asyncio.run(
        manager.cleanup(confirm=bool(args.yes), include_unknown=bool(args.include_unknown))
    )
    if args.json:
        print(
            json.dumps(
                {
                    "candidates": report.candidates,
                    "removed": [_result_to_dict(item) for item in report.removed],
                    "skipped": [_result_to_dict(item) for item in report.skipped],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"候选 {report.candidates} 个，删除 {len(report.removed)} 个，跳过 {len(report.skipped)} 个")
        for item in report.skipped:
            print(f"  跳过 {item.fingerprint}：{item.reason}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - 手工运维入口
    raise SystemExit(main())
