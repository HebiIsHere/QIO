"""离线发布闸门：**不联网、不安装**，只核对已经产出的安装包与它引用的东西。

为什么需要它：完整 NSIS 安装包的真机 E2E（装 → 首次启动 → 打包后端起来 → 内置模型
就位 → … → 卸载）必须在有桌面会话、没有被沙箱限制写权限的机器上跑。那之前的
**可判定部分**不应该也一起变成"人工"——本脚本把它们做成可重复执行的绿灯/红灯：

1. 安装包本身：存在、体积合理、sha256 与发布目录里的 SHA256SUMS.txt 一致；
2. 更新清单 latest.json：版本与 frontend/src-tauri/tauri.conf.json 一致、
   指向的文件名就是安装包、签名结构合法；
3. 更新签名**是我方密钥签的**：minisign 签名里的 key id 必须等于 tauri.conf.json
   里内嵌公钥的 key id（结构核对，不做 ed25519 验签 —— 见下方说明）；
4. sidecar 与源码同步：frontend/src-tauri/binaries/qio-backend-*.exe 的 sha256
   必须等于最近一次 release 构建产物 target/release/qio-backend.exe 的 sha256
   （历史上出过"包里是旧后端"的事故，这条就是拦它的）；
5. 内置模型：resources/models 下的每个文件都要与 model_manifest.json 的
   bytes + sha256 对得上（存在才查，不存在就明确 SKIP，不静默放过）；
6. 打包配置自洽：externalBin / resources / createUpdaterArtifacts（**只看源码配置**）；
7. 实际产物是否带更新签名：按**产物本身**判定（.sig 在不在 + 构建 manifest 声明的 signing），
   不再按源码配置推断 —— 见下面"构建配置身份"；
8. 构建配置身份：安装包旁边那份 `<installer>.build.json`（build_installer.ps1 写出来），
   核对 installer sha256 / 版本 / commit / config overrides / signing；没有就明确记 WARN；
9. 卸载契约（静态）：`bundle.windows.nsis.installerHooks` 指向的钩子必须清掉**安装信息**
   （安装位置默认值 + Installer Language），并且**不能**删用户状态（DbBaseline）或数据目录；
10. 当前 commit：产物要能对到某一版源码。

结果状态有四种：PASS / FAIL / SKIP / **WARN**。WARN 的语义是"查到了、也如实说了，
但它本身不构成'不要发布'"（例如：显式标注的未签名测试产物）。它**不**计入失败，
历史比较里也单列，不会被读成"新失败"。

**诚实边界（不要把它读成"验签通过"）**：ed25519 验签需要非标准库实现（本机
backend venv 没有 cryptography）。这里只核对签名结构与 key id 归属 —— 能发现
"签名不是这把钥匙签的 / 签名文件损坏 / 清单指向了别的包"，但**不能**替代真正的
密码学验签。真机发布时应再跑一次 tauri 官方验签路径。

**结果落库（2026-10-02 追加）**：每次跑完把「时间 / commit / 版本 / 逐项 PASS-FAIL-SKIP /
安装包 sha256」追加到一份历史文件（默认 `docs/releases/release-history.jsonl`），并自动与
**上一条**比较，明确说出这次比上次多了/少了哪些通过项。放这里的理由：历史是发布决策的
一部分，应该跟 `docs/releases/v0.1.x.md` 一起进仓库、随 PR 评审、在 dist 被清掉后仍然可查；
`--history-file` 可以指到别处（CI 里想只留在产物目录就用它）。历史写失败不影响闸门判定，
只打一行警告 —— 判定必须只由真实产物决定。

用法：

    python scripts/release_gate.py                 # 自动找最新的 QIO_*_x64-setup.exe
    python scripts/release_gate.py --installer path\\to\\QIO_0.1.10_x64-setup.exe
    python scripts/release_gate.py --json out.json # 额外落一份机器可读结果
    python scripts/release_gate.py --no-history    # 只判定，不写历史
    python scripts/release_gate.py --history       # 读历史：最近几条 + 最近两条的差异
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 默认按脚本所在的检出算；产物不在同一个检出时用 --repo 指过去（例如 CI 里只挂了 dist）。
REPO = Path(__file__).resolve().parents[1]


def set_repo(path: Path) -> None:
    """把"从哪个检出核对 sidecar / 内置模型 / 打包配置"钉到参数上。"""
    global REPO, TAURI_CONF, BINARIES, RESOURCES, RELEASE_DIR, DEFAULT_DIST
    REPO = path
    TAURI_CONF = REPO / "frontend" / "src-tauri" / "tauri.conf.json"
    BINARIES = REPO / "frontend" / "src-tauri" / "binaries"
    RESOURCES = REPO / "frontend" / "src-tauri" / "resources"
    RELEASE_DIR = REPO / "frontend" / "src-tauri" / "target" / "release"
    DEFAULT_DIST = REPO.parent / "dist"


set_repo(REPO)
TAURI_CONF = REPO / "frontend" / "src-tauri" / "tauri.conf.json"
BINARIES = REPO / "frontend" / "src-tauri" / "binaries"
RESOURCES = REPO / "frontend" / "src-tauri" / "resources"
RELEASE_DIR = REPO / "frontend" / "src-tauri" / "target" / "release"
DEFAULT_DIST = REPO.parent / "dist"

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
# WARN：**查到了、也如实说出来了**，但它本身不构成"不要发布"。
# 存在的理由：像"这是已知的未签名测试产物"这种事，既不是 PASS（没有签名），
# 也不是 FAIL（它没有冒充发布产物）。以前只能塞进 SKIP，读起来像"没查"。
WARN = "WARN"

# 历史写在**脚本所在的检出**里（不是 --repo 指的产物目录）：发布记录要跟版本一起进仓库。
SCRIPT_REPO = Path(__file__).resolve().parents[1]
HISTORY_SCHEMA = 1
DEFAULT_HISTORY = SCRIPT_REPO / "docs" / "releases" / "release-history.jsonl"


@dataclass
class Result:
    name: str
    state: str
    detail: str


@dataclass
class Gate:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, state: str, detail: str) -> None:
        self.results.append(Result(name, state, detail))

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if r.state == FAIL]

    @property
    def warned(self) -> list[Result]:
        return [r for r in self.results if r.state == WARN]

    def report(self) -> str:
        width = max((len(r.name) for r in self.results), default=10)
        lines = [f"{'检查项'.ljust(width)}  结果   说明"]
        for r in self.results:
            lines.append(f"{r.name.ljust(width)}  {r.state}  {r.detail}")
        return "\n".join(lines)


def _configure_output() -> None:
    """报告层必须能在任何控制台编码下工作（cp1252 的发布机 / CI runner 都不能崩）。

    回归的事故形状（2026-10-02，Windows CI 抓到）：闸门的报告全是中文，而英文 Windows
    的 stdout 是 cp1252 —— 第一条 print 就抛 UnicodeEncodeError，闸门以 traceback 收场，
    「判定」根本没能跑完。做法与 scripts/frozen_worker_smoke.py 一致：

    * 老控制台（tty）：保留它自己的编码，编不出来的字符转义，绝不抛异常；
    * 重定向 / CI：直接写 UTF-8 字节，日志按 UTF-8 解码。

    判定结果与历史记录都不受编码影响。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            is_tty = bool(getattr(stream, "isatty", lambda: False)())
        except (OSError, ValueError):
            is_tty = False
        try:
            if is_tty:
                reconfigure(errors="backslashreplace")
            else:
                reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            continue


def _git_commit(repo: Path) -> str:
    """当前 commit（拿不到就空串）：历史要能对上「哪一版源码出的这个包」。"""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def make_record(gate: "Gate", installer: Path, digest: str, version: str) -> dict:
    """把一次判定压成一条历史记录（逐项状态 + 关键标识）。"""
    import datetime

    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    for r in gate.results:
        counts[r.state] = counts.get(r.state, 0) + 1
    return {
        "schema": HISTORY_SCHEMA,
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "commit": _git_commit(SCRIPT_REPO),
        "version": version,
        "installer": installer.name,
        "installer_sha256": digest,
        "counts": counts,
        "results": {r.name: r.state for r in gate.results},
        "failed": [r.name for r in gate.failed],
    }


def append_history(path: Path, record: dict) -> None:
    """追加一条 JSONL 记录。失败只警告：闸门判定不能被「写不了历史」左右。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"[warn] 历史记录没写成（{path}）：{exc}", file=sys.stderr)


def load_history(path: Path) -> list[dict]:
    """读回历史；坏行跳过（历史文件不该让闸门崩）。"""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def compare_records(previous: dict, current: dict) -> dict:
    """这次比上次：哪些项新通过 / 新失败 / 一直失败，以及检查项本身的增减。"""
    prev = previous.get("results") or {}
    cur = current.get("results") or {}
    # 只有 FAIL 才是"失败"。WARN / SKIP 不是 —— 否则"已知未签名测试产物"这种
    # 如实记录会被历史比较读成"新失败"，把诚实标注变成噪声。
    newly_failed = sorted(k for k, v in cur.items() if v == FAIL and prev.get(k) == PASS)
    newly_warned = sorted(k for k, v in cur.items() if v == WARN and prev.get(k) == PASS)
    newly_passed = sorted(k for k, v in cur.items() if v == PASS and k in prev and prev[k] != PASS)
    still_failing = sorted(k for k, v in cur.items() if v == FAIL and prev.get(k) not in (PASS, None))
    return {
        "previous_ts": previous.get("ts"),
        "previous_version": previous.get("version"),
        "previous_commit": previous.get("commit"),
        "newly_passed": newly_passed,
        "newly_failed": newly_failed,
        "newly_warned": newly_warned,
        "still_failing": still_failing,
        "added_items": sorted(set(cur) - set(prev)),
        "removed_items": sorted(set(prev) - set(cur)),
    }


def format_comparison(diff: dict) -> list[str]:
    def names(key: str) -> str:
        values = diff[key]
        return "、".join(values) if values else "无"

    lines = [
        f"与上一条比较（{diff['previous_ts']} / v{diff['previous_version']} / "
        f"{(diff['previous_commit'] or '?')[:8]}）："
    ]
    lines.append(f"  新通过：{names('newly_passed')}")
    lines.append(f"  新失败：{names('newly_failed')}")
    lines.append(f"  新警告：{names('newly_warned')}")
    lines.append(f"  一直失败：{names('still_failing')}")
    if diff["added_items"] or diff["removed_items"]:
        lines.append(f"  检查项增减：+{names('added_items')} / -{names('removed_items')}")
    return lines


def print_history(path: Path, limit: int) -> int:
    """--history：最近几条 + 最近两条的差异。"""
    records = load_history(path)
    if not records:
        print(f"没有历史记录：{path}")
        return 0
    print(f"# 发布闸门历史 {path}（{len(records)} 条）")
    print(f"{'时间':<26} {'版本':<10} {'commit':<10} {'PASS':>5} {'WARN':>5} {'FAIL':>5} {'SKIP':>5}  安装包")
    for record in records[-limit:]:
        counts = record.get("counts") or {}
        print(
            f"{(record.get('ts') or '?')[:26]:<26} {(record.get('version') or '?')[:10]:<10} "
            f"{(record.get('commit') or '?')[:8]:<10} "
            f"{counts.get(PASS, 0):>5} {counts.get(WARN, 0):>5} {counts.get(FAIL, 0):>5} "
            f"{counts.get(SKIP, 0):>5}  "
            f"{record.get('installer')}"
        )
    if len(records) >= 2:
        print()
        print("\n".join(format_comparison(compare_records(records[-2], records[-1]))))
    return 0


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _minisign_key_id(b64: str) -> tuple[str, str]:
    """从 minisign 载荷里取 (算法, key id 十六进制)。结构不合法就抛 ValueError。

    Tauri 的 .sig / latest.json.signature / 内嵌公钥都是**两层**结构：
    外层 base64 解出来是 minisign 文本（untrusted comment 行 + 一行 base64），
    内层那行才是 42 字节公钥 / 74 字节签名。实测踩过：只解一层会得到 309 字符。
    """
    outer = base64.b64decode(b64.strip().encode("ascii"), validate=False)
    text = outer.decode("utf-8", errors="replace")
    payload = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("untrusted comment") or line.startswith("trusted comment"):
            continue
        payload = line
        break
    if not payload:
        raise ValueError("minisign 文本里没有任何 base64 载荷")
    blob = base64.b64decode(payload.encode("ascii"), validate=False)
    if len(blob) not in (42, 74):
        raise ValueError(f"minisign 载荷长度异常：{len(blob)}（应为 42 公钥 / 74 签名）")
    alg = blob[:2].decode("ascii", errors="replace")
    return alg, blob[2:10].hex()


def check_installer(gate: Gate, installer: Path) -> str:
    if not installer.exists():
        gate.add("installer", FAIL, f"找不到安装包：{installer}")
        return ""
    size_mb = installer.stat().st_size / (1024 * 1024)
    if size_mb < 50:
        gate.add("installer", FAIL, f"{installer.name} 只有 {size_mb:.1f} MB，不像一个含模型与后端的完整包")
    else:
        gate.add("installer", PASS, f"{installer.name}（{size_mb:.1f} MB）")
    digest = sha256_of(installer)
    gate.add("installer.sha256", PASS, digest)
    return digest


def check_sums(gate: Gate, dist: Path, installer: Path, digest: str) -> None:
    sums = dist / "SHA256SUMS.txt"
    if not sums.exists():
        gate.add("sha256sums", SKIP, f"没有 {sums}（发布目录里未汇总）")
        return
    entries: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            entries[parts[0]] = parts[1].lower()
    recorded = entries.get(installer.name)
    if recorded is None:
        gate.add("sha256sums", FAIL, f"SHA256SUMS.txt 里没有 {installer.name}")
    elif recorded != digest:
        gate.add("sha256sums", FAIL, f"{installer.name} 哈希不一致：清单 {recorded[:16]}… vs 实际 {digest[:16]}…")
    else:
        gate.add("sha256sums", PASS, f"{installer.name} 与 SHA256SUMS.txt 一致")


def check_manifest(gate: Gate, dist: Path, installer: Path, conf: dict) -> None:
    manifest_path = dist / "latest.json"
    if not manifest_path.exists():
        gate.add("latest.json", FAIL, f"缺少更新清单：{manifest_path}")
        return
    try:
        raw = manifest_path.read_bytes()
        if raw[:3] == b"\xef\xbb\xbf":
            gate.add("latest.json.bom", FAIL, "清单带 UTF-8 BOM：Rust 侧 serde_json 会解析失败")
        else:
            gate.add("latest.json.bom", PASS, "无 BOM")
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        gate.add("latest.json", FAIL, f"解析失败：{exc}")
        return
    version = str(manifest.get("version") or "")
    conf_version = str(conf.get("version") or "")
    if not version:
        gate.add("latest.json.version", FAIL, "清单里没有 version")
    elif version != conf_version:
        gate.add("latest.json.version", FAIL, f"清单 {version} != tauri.conf.json {conf_version}")
    else:
        gate.add("latest.json.version", PASS, version)
    platforms = manifest.get("platforms") or {}
    entry = platforms.get("windows-x86_64") or {}
    url = str(entry.get("url") or "")
    if not url:
        gate.add("latest.json.url", FAIL, "缺少 windows-x86_64.url")
    elif Path(url).name != installer.name:
        gate.add("latest.json.url", FAIL, f"清单指向 {Path(url).name}，实际安装包是 {installer.name}")
    else:
        gate.add("latest.json.url", PASS, Path(url).name)
    sig = str(entry.get("signature") or "").strip()
    if not sig:
        gate.add("latest.json.signature", FAIL, "缺少签名：更新包没有签名等于没有更新保障")
        return
    try:
        alg, key_id = _minisign_key_id(sig)
    except ValueError as exc:
        gate.add("latest.json.signature", FAIL, f"签名结构不合法：{exc}")
        return
    pubkey = str(((conf.get("plugins") or {}).get("updater") or {}).get("pubkey") or "")
    if not pubkey:
        gate.add("latest.json.signature", FAIL, "tauri.conf.json 里没有内嵌 updater 公钥")
        return
    try:
        _palg, pkey_id = _minisign_key_id(pubkey)
    except ValueError as exc:
        gate.add("latest.json.signature", FAIL, f"内嵌公钥结构不合法：{exc}")
        return
    if key_id != pkey_id:
        gate.add(
            "latest.json.signature",
            FAIL,
            f"签名 key id {key_id} 与内嵌公钥 {pkey_id} 不一致：这个签名不是应用信任的钥匙签的",
        )
    else:
        gate.add(
            "latest.json.signature",
            PASS,
            f"签名结构合法（alg={alg}，key id={key_id} 与内嵌公钥一致；未做 ed25519 验签）",
        )


def check_sig_file(gate: Gate, dist: Path, installer: Path) -> None:
    sig_path = installer.with_name(installer.name + ".sig")
    if not sig_path.exists():
        gate.add("installer.sig", FAIL, f"缺少 {sig_path.name}")
        return
    try:
        alg, key_id = _minisign_key_id(sig_path.read_text(encoding="utf-8", errors="replace"))
    except ValueError as exc:
        gate.add("installer.sig", FAIL, f"{sig_path.name} 结构不合法：{exc}")
        return
    gate.add("installer.sig", PASS, f"{sig_path.name} 结构合法（alg={alg}，key id={key_id}）")


def _fmt_time(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def check_sidecar(gate: Gate, installer: Path) -> None:
    """包里的后端是不是**这一版源码**构建出来的。

    这个检查存在的理由是一次真实事故：安装包里的后端比源码早一个多月，
    内置模型加载失败后静默退回 BM25，包看起来是好的。
    """
    if not BINARIES.exists():
        gate.add("sidecar", FAIL, f"没有 sidecar 目录：{BINARIES}（tauri build 会直接把包打失败）")
        return
    candidates = sorted(BINARIES.glob("qio-backend-*.exe"))
    if not candidates:
        gate.add("sidecar", FAIL, f"{BINARIES} 里没有 qio-backend-*.exe")
        return
    sidecar = candidates[-1]
    digest = sha256_of(sidecar)
    sidecar_mtime = sidecar.stat().st_mtime
    gate.add(
        "sidecar",
        PASS,
        f"{sidecar.name}（{sidecar.stat().st_size / (1024 * 1024):.1f} MB，"
        f"{_fmt_time(sidecar_mtime)}，sha256 {digest[:16]}…）",
    )

    staged = RELEASE_DIR / "qio-backend.exe"
    if installer.exists() and sidecar_mtime > installer.stat().st_mtime:
        gate.add(
            "sidecar.fresh",
            FAIL,
            f"安装包（{_fmt_time(installer.stat().st_mtime)}）比 sidecar（{_fmt_time(sidecar_mtime)}）旧："
            "dist 里这个包包含的是更早构建的后端，必须重新打包再发布",
        )
        return
    if not staged.exists():
        gate.add("sidecar.fresh", SKIP, f"没有 {staged}，无法逐字节比对打包时用的后端")
        return
    staged_digest = sha256_of(staged)
    if staged_digest != digest:
        gate.add(
            "sidecar.fresh",
            FAIL,
            "binaries 里的 sidecar 与最近一次 release 构建的后端不一致（重打包会换掉包里的后端）："
            f"binaries {digest[:12]}… @{_fmt_time(sidecar_mtime)} vs "
            f"release {staged_digest[:12]}… @{_fmt_time(staged.stat().st_mtime)}",
        )
    else:
        gate.add("sidecar.fresh", PASS, "sidecar 与 release 构建的后端逐字节一致")


def check_models(gate: Gate) -> None:
    models_root = RESOURCES / "models"
    if not models_root.exists():
        gate.add(
            "models",
            SKIP,
            f"没有 {models_root}：构建前未跑 scripts/models/fetch_model.py，"
            "本次无法核对内置模型（打包脚本会在这一步直接失败，不会静默出包）",
        )
        return
    manifests = sorted(models_root.rglob("model_manifest.json"))
    if not manifests:
        gate.add("models", FAIL, f"{models_root} 下没有 model_manifest.json")
        return
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            gate.add("models", FAIL, f"{manifest_path} 解析失败：{exc}")
            continue
        base = manifest_path.parent
        problems: list[str] = []
        checked = 0
        for entry in manifest.get("files") or []:
            name = str(entry.get("file") or "")
            target = base / name
            if not target.exists():
                problems.append(f"缺少 {name}")
                continue
            if int(entry.get("bytes") or -1) != target.stat().st_size:
                problems.append(f"{name} 体积不符（清单 {entry.get('bytes')} vs 实际 {target.stat().st_size}）")
                continue
            if str(entry.get("sha256") or "") and sha256_of(target) != str(entry["sha256"]):
                problems.append(f"{name} sha256 不符")
                continue
            checked += 1
        if problems:
            gate.add("models", FAIL, f"{base.name}: " + "；".join(problems))
        else:
            gate.add("models", PASS, f"{base.name}: {checked} 个文件与清单逐一对上")


def check_bundle_config(gate: Gate, conf: dict) -> None:
    bundle = conf.get("bundle") or {}
    problems: list[str] = []
    external = bundle.get("externalBin") or []
    if not any(str(x).endswith("qio-backend") for x in external):
        problems.append("externalBin 里没有 qio-backend")
    resources = bundle.get("resources") or {}
    if "models" not in json.dumps(resources, ensure_ascii=False):
        problems.append("resources 里没有 models")
    if not bundle.get("createUpdaterArtifacts"):
        problems.append("createUpdaterArtifacts 未开启：更新包不会带签名")
    if problems:
        gate.add("bundle.config", FAIL, "；".join(problems))
    else:
        # 注意口径：这一项只看**源码配置**是否自洽。实际产物到底有没有更新签名，
        # 由 updater.artifacts 按产物判定 —— 上一阶段就是在这里漏过一次：
        # 构建用 --config 关掉了 createUpdaterArtifacts，产物没有签名，这里却报 PASS。
        gate.add(
            "bundle.config",
            PASS,
            "源码配置自洽（externalBin / resources / createUpdaterArtifacts=true）；"
            "实际产物是否带更新签名见 updater.artifacts",
        )


def check_nsis_strings(gate: Gate, installer: Path, conf: dict) -> None:
    try:
        blob = installer.read_bytes()
    except OSError as exc:
        gate.add("nsis.payload", FAIL, f"读不出安装包：{exc}")
        return
    version = str(conf.get("version") or "")
    problems: list[str] = []
    if b"Nullsoft" not in blob:
        problems.append("没有 Nullsoft 标记（不像 NSIS 安装包）")
    # PE 版本资源是 UTF-16LE，命令/资源字符串里可能有 ASCII 形式：两种都查
    version_bytes = [version.encode("ascii", errors="ignore"), version.encode("utf-16-le")]
    if version and not any(v and v in blob for v in version_bytes):
        problems.append(f"包内没有找到版本号 {version}（ASCII 与 UTF-16LE 都查过）")
    if b"QIO" not in blob:
        problems.append("包内没有产品名 QIO")
    if problems:
        gate.add("nsis.payload", FAIL, "；".join(problems))
    else:
        gate.add("nsis.payload", PASS, f"NSIS 结构标记、产品名与版本号 {version} 都在")


# ---------------------------------------------------------------------------
# 安装器契约（静态）与构建配置身份
#
# 为什么这几项是"静态"的：真机跑一次安装/卸载需要能写注册表的桌面会话。
# 本会话的沙箱给子进程的是受限令牌（WriteRegStr / DeleteRegKey 静默 ACCESS_DENIED），
# CI runner 上也没有装出来的应用。但"卸载时到底删了什么"是**源码里可判定的事实**，
# 而且恰恰是最容易在后续重构里被悄悄改坏的地方（比如有人图省事把 DeleteRegValue
# 改成 DeleteRegKey，就把用户状态一起删了）。
# ---------------------------------------------------------------------------

INSTALL_LOCATION_KEY = r"Software\qio\QIO"
# 这个键下**不属于安装信息**的值：后端写的数据库身份基线（用户状态）。
USER_STATE_VALUES = ("DbBaseline",)
# 数据目录：保留与否只能由「删除应用数据」复选框决定，卸载钩子不许碰。
APP_DATA_DIRS = (r"$APPDATA\com.qio.app", r"$LOCALAPPDATA\com.qio.app")


def nsis_hooks_path(conf: dict) -> Path | None:
    """tauri.conf.json 里 bundle.windows.nsis.installerHooks 指向的 .nsh（相对配置目录）。"""
    windows = ((conf.get("bundle") or {}).get("windows") or {})
    rel = (windows.get("nsis") or {}).get("installerHooks") or ""
    if not rel:
        return None
    return (TAURI_CONF.parent / str(rel)).resolve()


def _uninstall_hook_body(text: str) -> str | None:
    """把卸载钩子宏（PREUNINSTALL / POSTUNINSTALL）的正文拼起来；都没有就返回 None。"""
    bodies: list[str] = []
    for name in ("NSIS_HOOK_PREUNINSTALL", "NSIS_HOOK_POSTUNINSTALL"):
        bodies += re.findall(r"!macro\s+" + re.escape(name) + r"\b(.*?)!macroend", text, re.S)
    return "\n".join(bodies) if bodies else None


def check_uninstall_contract(gate: Gate, conf: dict) -> None:
    """普通卸载必须清掉**安装信息**，且**绝不能**动用户状态与数据目录。"""
    hooks = nsis_hooks_path(conf)
    if hooks is None:
        gate.add(
            "uninstall.contract",
            FAIL,
            "没有配置 bundle.windows.nsis.installerHooks：Tauri 默认的卸载段只在"
            "「删除应用数据」勾选时才清理安装位置记录，于是普通卸载会留下 "
            f"{INSTALL_LOCATION_KEY}，下一次安装会把默认目录指到一个已经被删掉的路径",
        )
        return
    if not hooks.exists():
        gate.add("uninstall.contract", FAIL, f"installerHooks 指向的文件不存在：{hooks}")
        return
    text = hooks.read_text(encoding="utf-8", errors="replace")
    body = _uninstall_hook_body(text)
    if body is None:
        gate.add(
            "uninstall.contract",
            FAIL,
            f"{hooks.name} 里没有 NSIS_HOOK_POSTUNINSTALL / NSIS_HOOK_PREUNINSTALL："
            "文件在、但什么都没挂上（Tauri 只会插入存在的宏）",
        )
        return

    problems: list[str] = []
    if not re.search(r'DeleteRegValue\s+SHCTX\s+"Software\\qio\\QIO"\s+""', body):
        problems.append("没有删除安装位置（该键的默认值）的 DeleteRegValue")
    for stmt in re.findall(r"DeleteRegKey[^\r\n]*", body):
        if "Software\\qio" in stmt and "/ifempty" not in stmt:
            problems.append(
                "对整个 Software\\qio\\QIO 用了不带 /ifempty 的 DeleteRegKey："
                "会把 DbBaseline（用户状态）一起删掉，等于改了数据保留策略"
            )
    for value in USER_STATE_VALUES:
        if re.search(r"DeleteRegValue[^\r\n]*" + re.escape(value), body):
            problems.append(f"在删用户状态值 {value}")
    for directory in APP_DATA_DIRS:
        if directory in body:
            problems.append(f"碰了数据目录 {directory}（保留策略只能由「删除应用数据」复选框决定）")
    if re.search(r"\bRMDir\b[^\r\n]*com\.qio\.app", body, re.I):
        problems.append("在卸载钩子里删数据目录")

    nsis_conf = (((conf.get("bundle") or {}).get("windows") or {}).get("nsis") or {})
    mode = str(nsis_conf.get("installMode") or "currentUser（未显式配置，Tauri 默认）")
    if problems:
        gate.add("uninstall.contract", FAIL, "；".join(problems))
    else:
        gate.add(
            "uninstall.contract",
            PASS,
            f"卸载钩子会清安装信息（{INSTALL_LOCATION_KEY} 的默认值与 Installer Language），"
            f"且用 /ifempty 保留 DbBaseline；installMode={mode}",
        )


def check_repo_identity(gate: Gate) -> None:
    """产物必须能对到某一版源码：把当前 commit 写进判定结果。"""
    commit = _git_commit(REPO)
    if commit:
        gate.add("repo.commit", PASS, f"{commit[:12]}（{REPO}）")
    else:
        gate.add(
            "repo.commit",
            WARN,
            f"取不到 git commit（{REPO} 不是 git 检出？）—— 这份产物无法对到某一版源码",
        )


def build_manifest_path(installer: Path) -> Path:
    """构建 manifest 与产物同名同目录：QIO_x.y.z_x64-setup.exe.build.json。"""
    return installer.with_name(installer.name + ".build.json")


def check_build_manifest(gate: Gate, installer: Path, digest: str, conf: dict) -> dict | None:
    """构建配置身份：**构建过程写下来的事实**，不是"按默认配置推断"。

    没有它的时候，闸门只能读 tauri.conf.json 猜这次构建用了什么配置 —— 上一阶段
    就因此漏过一次（--config 关掉 createUpdaterArtifacts，产物没签名却报 PASS）。
    """
    path = build_manifest_path(installer)
    if not path.exists():
        gate.add(
            "build.manifest",
            WARN,
            f"没有 {path.name}：这次构建用的配置无法核对（只能按 tauri.conf.json 推断）。"
            "用 scripts/build_installer.ps1 构建会自动写出它",
        )
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        gate.add("build.manifest", FAIL, f"{path.name} 解析失败：{exc}")
        return None
    if not isinstance(manifest, dict):
        gate.add("build.manifest", FAIL, f"{path.name} 不是一个 JSON 对象")
        return None

    problems: list[str] = []
    recorded = str(manifest.get("installer_sha256") or "").lower()
    if recorded and recorded != digest.lower():
        problems.append(
            f"manifest 记的安装包 sha256 {recorded[:16]}… 与实际 {digest[:16]}… 不一致"
            "（这份 manifest 描述的是另一个产物）"
        )
    if str(manifest.get("installer") or "") and str(manifest["installer"]) != installer.name:
        problems.append(f"manifest 记的安装包名 {manifest['installer']} 与选中的 {installer.name} 不一致")
    version = str(manifest.get("version") or "")
    conf_version = str(conf.get("version") or "")
    if version and conf_version and version != conf_version:
        problems.append(f"manifest 版本 {version} != tauri.conf.json {conf_version}")
    if problems:
        gate.add("build.manifest", FAIL, "；".join(problems))
        return manifest

    overrides = manifest.get("config_overrides") or {}
    gate.add(
        "build.manifest",
        PASS,
        f"commit={str(manifest.get('commit') or '?')[:8]} version={version or '?'} "
        f"built_at={manifest.get('built_at') or '?'} signing={manifest.get('signing') or '?'} "
        f"overrides={'无' if not overrides else json.dumps(overrides, ensure_ascii=False)}",
    )
    return manifest


def check_updater_artifacts(
    gate: Gate, installer: Path, conf: dict, manifest: dict | None
) -> None:
    """更新产物**按实际产物**判定，不按源码配置推断。"""
    sig = installer.with_name(installer.name + ".sig")
    wants = bool(((conf.get("bundle") or {}).get("createUpdaterArtifacts")))
    signing = str((manifest or {}).get("signing") or "")
    declared_unsigned = signing == "unsigned-test" or "UNSIGNED-TEST" in installer.name
    overrides = json.dumps((manifest or {}).get("config_overrides") or {}, ensure_ascii=False)

    if sig.exists():
        gate.add("updater.artifacts", PASS, f"实际产物带更新签名：{sig.name}")
        return
    if declared_unsigned:
        gate.add(
            "updater.artifacts",
            WARN,
            f"实际产物**没有**更新签名（{sig.name} 不存在），且它被显式标成未签名测试产物"
            f"（signing={signing or 'UNSIGNED-TEST 名称'}，config_overrides={overrides}）。"
            "这不是可发布产物；正式签名需要 TAURI_SIGNING_PRIVATE_KEY(_PASSWORD)",
        )
        return
    if wants:
        gate.add(
            "updater.artifacts",
            FAIL,
            f"tauri.conf.json 要求 createUpdaterArtifacts，但实际产物没有 {sig.name}"
            "（构建时被 --config 覆盖，或签名失败）—— 这正是只看源码配置会漏掉的那种不一致",
        )
    else:
        gate.add("updater.artifacts", WARN, f"源码配置没有要求更新产物，实际产物也没有 {sig.name}")


def _make_fake_minisign(key_id: bytes, payload_len: int) -> str:
    """造一份结构合法（但不做真实验签）的 minisign 载荷，供自检用。"""
    blob = b"ED" + key_id + bytes(payload_len - 10)
    inner = "untrusted comment: fake\n" + base64.b64encode(blob).decode("ascii") + "\n"
    return base64.b64encode(inner.encode("utf-8")).decode("ascii")


def build_fixture(
    root: Path,
    *,
    sidecar_newer: bool = False,
    bad_hash: bool = False,
    hook: str = "good",
    unsigned: bool = False,
    manifest_foreign: bool = False,
    no_manifest: bool = False,
) -> Path:
    """造一个最小的发布产物目录（自检与历史回归测试共用，只写合成数据）。

    * `bad_hash=True`：SHA256SUMS.txt 记一个错的哈希；
    * `sidecar_newer=True`：造出「包里是旧后端」；
    * `hook`：`good`（正常卸载钩子）/ `dangerous`（整键 DeleteRegKey，会删掉用户状态）/
      `none`（根本没配 installerHooks）；
    * `unsigned=True`：产物是显式标注的未签名测试产物（没有 .sig / latest.json，
      但 build manifest 声明 signing=unsigned-test 与 config_overrides）；
    * `manifest_foreign=True`：build manifest 描述的是另一个产物（哈希对不上）；
    * `no_manifest=True`：根本没有 build manifest（只能记 WARN）。

    返回 dist 目录。
    """
    (root / "frontend" / "src-tauri" / "binaries").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "src-tauri" / "resources" / "models" / "fake-model").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "src-tauri" / "target" / "release").mkdir(parents=True, exist_ok=True)
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    key_id = bytes(range(8))
    (root / "frontend" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps(
            {
                "version": "9.9.9",
                "bundle": {
                    "externalBin": ["binaries/qio-backend"],
                    "resources": {"resources/models": "models"},
                    "createUpdaterArtifacts": True,
                    **(
                        {"windows": {"nsis": {"installerHooks": "nsis/installer-hooks.nsh"}}}
                        if hook != "none"
                        else {}
                    ),
                },
                "plugins": {"updater": {"pubkey": _make_fake_minisign(key_id, 42)}},
            }
        ),
        encoding="utf-8",
    )
    name = "QIO_9.9.9_x64-setup-UNSIGNED-TEST.exe" if unsigned else "QIO_9.9.9_x64-setup.exe"
    installer_path = dist / name
    installer_path.write_bytes(b"MZ" + b"Nullsoft" + "9.9.9".encode("utf-16-le") + b"QIO" + b"x" * 60_000_000)
    if not unsigned:
        (dist / f"{name}.sig").write_text(_make_fake_minisign(key_id, 74), encoding="utf-8")
    digest = sha256_of(installer_path)
    recorded = "0" * 64 if bad_hash else digest
    (dist / "SHA256SUMS.txt").write_text(f"{name}  {recorded}\n", encoding="utf-8")
    if not unsigned:
        (dist / "latest.json").write_text(
            json.dumps(
                {
                    "version": "9.9.9",
                    "platforms": {
                        "windows-x86_64": {
                            "signature": _make_fake_minisign(key_id, 74),
                            "url": f"https://example.invalid/{name}",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
    sidecar = root / "frontend" / "src-tauri" / "binaries" / "qio-backend-x86_64-pc-windows-msvc.exe"
    sidecar.write_bytes(b"SIDECAR")
    (root / "frontend" / "src-tauri" / "target" / "release" / "qio-backend.exe").write_bytes(b"SIDECAR")
    model_file = root / "frontend" / "src-tauri" / "resources" / "models" / "fake-model" / "model.onnx"
    model_file.write_bytes(b"MODEL")
    (model_file.parent / "model_manifest.json").write_text(
        json.dumps({"files": [{"file": "model.onnx", "bytes": 5, "sha256": sha256_of(model_file)}]}),
        encoding="utf-8",
    )
    # 真实构建顺序：先 build_sidecar，再 tauri build（安装包更晚）。
    # sidecar_newer=True 就反过来造出「包里是旧后端」这件事。
    import os
    import time

    now = time.time()
    if sidecar_newer:
        os.utime(installer_path, (now - 3600, now - 3600))
        os.utime(sidecar, (now, now))
    else:
        os.utime(sidecar, (now - 3600, now - 3600))
        os.utime(installer_path, (now, now))

    # 卸载钩子文件（Tauri 通过 bundle.windows.nsis.installerHooks 挂它）
    if hook != "none":
        hooks_dir = root / "frontend" / "src-tauri" / "nsis"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        if hook == "dangerous":
            body = (
                '!macro NSIS_HOOK_POSTUNINSTALL\n'
                '  DeleteRegKey SHCTX "Software\\qio\\QIO"\n'
                '!macroend\n'
            )
        else:
            body = (
                '!macro NSIS_HOOK_POSTUNINSTALL\n'
                '  DeleteRegValue SHCTX "Software\\qio\\QIO" ""\n'
                '  DeleteRegValue SHCTX "Software\\qio\\QIO" "Installer Language"\n'
                '  DeleteRegKey /ifempty SHCTX "Software\\qio\\QIO"\n'
                '  DeleteRegKey /ifempty SHCTX "Software\\qio"\n'
                '!macroend\n'
            )
        (hooks_dir / "installer-hooks.nsh").write_text(body, encoding="utf-8")

    # 构建 manifest（构建过程写下来的配置身份）
    if not no_manifest:
        manifest_digest = "0" * 64 if manifest_foreign else digest
        (dist / f"{name}.build.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "installer": name,
                    "installer_sha256": manifest_digest,
                    "version": "9.9.9",
                    "commit": "f" * 40,
                    "built_at": "2026-10-02T00:00:00+00:00",
                    "updater_artifacts": not unsigned,
                    "config_overrides": (
                        {"bundle": {"createUpdaterArtifacts": False}} if unsigned else {}
                    ),
                    "signing": "unsigned-test" if unsigned else "signed",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return dist



def run_checks(
    gate: Gate,
    dist: Path,
    installer: Path,
    conf: dict,
    *,
    with_repo_identity: bool = True,
) -> str:
    """闸门的检查清单。

    **main 与 --selftest 必须共用这一份**：否则会出现"自检绿了、真跑却查了别的"，
    自检就不再证明任何事。返回安装包 sha256。
    """
    if with_repo_identity:
        check_repo_identity(gate)
    digest = check_installer(gate, installer)
    check_sums(gate, dist, installer, digest)
    if conf:
        check_manifest(gate, dist, installer, conf)
        check_bundle_config(gate, conf)
        check_uninstall_contract(gate, conf)
        if installer.exists():
            check_nsis_strings(gate, installer, conf)
    if installer.exists():
        check_sig_file(gate, dist, installer)
    check_sidecar(gate, installer)
    check_models(gate)
    manifest = check_build_manifest(gate, installer, digest, conf) if installer.exists() else None
    if conf:
        check_updater_artifacts(gate, installer, conf, manifest)
    return digest


def _selftest() -> int:
    """自检：造一个最小的发布目录，先要求全绿，再逐一注入缺陷要求变红。

    每个用例写清"期望哪些项红、哪些项只是警告" —— 特别是
    「未签名测试产物」：签名两项必须红，而 updater.artifacts 只能是 WARN，
    不能既不是 PASS 也不是 FAIL 地被吞掉。
    """
    import shutil
    import tempfile

    cases = (
        dict(label="健康产物", kwargs={}, expect_fail=set()),
        dict(label="哈希不符", kwargs={"bad_hash": True}, expect_fail={"sha256sums"}),
        dict(label="安装包比 sidecar 旧", kwargs={"sidecar_newer": True}, expect_fail={"sidecar.fresh"}),
        dict(label="没有卸载钩子", kwargs={"hook": "none"}, expect_fail={"uninstall.contract"}),
        dict(
            label="卸载钩子删整键（会带走 DbBaseline）",
            kwargs={"hook": "dangerous"},
            expect_fail={"uninstall.contract"},
        ),
        dict(
            label="没有构建 manifest（记 WARN，不判失败）",
            kwargs={"no_manifest": True},
            expect_fail=set(),
            expect_warn={"build.manifest"},
        ),
        dict(
            label="manifest 描述的是别的产物",
            kwargs={"manifest_foreign": True},
            expect_fail={"build.manifest"},
        ),
        dict(
            label="未签名测试产物：签名项红、更新产物只记 WARN",
            kwargs={"unsigned": True},
            expect_fail={"installer.sig", "latest.json"},
            expect_warn={"updater.artifacts"},
        ),
    )

    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="qio-gate-selftest-"))  # noqa: S108 - 自检临时目录
    try:
        for case in cases:
            label = case["label"]
            root = tmp / label.replace("/", "_")
            build_fixture(root, **case["kwargs"])
            set_repo(root)
            gate = Gate()
            conf = json.loads(
                (root / "frontend" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
            )
            names = sorted((root / "dist").glob("QIO_*_x64-setup*.exe"))
            installer = names[-1] if names else root / "dist" / "QIO_9.9.9_x64-setup.exe"
            run_checks(gate, root / "dist", installer, conf, with_repo_identity=False)

            failed = {r.name for r in gate.failed}
            warned = {r.name for r in gate.warned}
            expect_fail = set(case["expect_fail"])
            expect_warn = set(case.get("expect_warn") or ())
            if failed != expect_fail:
                failures.append(f"{label}: 期望失败 {sorted(expect_fail) or '无'}，实际 {sorted(failed) or '无'}")
                continue
            missing_warn = expect_warn - warned
            if missing_warn:
                failures.append(f"{label}: 期望 {sorted(missing_warn)} 记 WARN，实际没记")
                continue
            suffix = f"（失败 {sorted(failed) or '无'}；警告 {sorted(warned) or '无'}）"
            print(f"  [OK] {label} {suffix}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        set_repo(Path(__file__).resolve().parents[1])

    if failures:
        print("[FAIL] 发布闸门自检失败：")
        for item in failures:
            print("  -", item)
        return 1
    print(
        "[PASS] 发布闸门自检通过：健康产物全绿；哈希不符 / 后端过期 / 缺卸载钩子 / "
        "钩子删用户状态 / manifest 串包 都能变红；未签名测试产物只记 WARN"
    )
    return 0


def main() -> int:
    _configure_output()
    parser = argparse.ArgumentParser(description="QIO 离线发布闸门（不联网、不安装）")
    parser.add_argument("--repo", help="产出这些安装包的检出根目录（默认脚本所在检出）")
    parser.add_argument("--dist", help="发布产物目录（默认 <repo>/../dist）")
    parser.add_argument("--installer", help="指定安装包；默认取 dist 里最新的 QIO_*_x64-setup.exe")
    parser.add_argument("--json", help="把机器可读结果写到这个文件")
    parser.add_argument("--selftest", action="store_true", help="用合成产物自检闸门本身")
    parser.add_argument("--history-file", help=f"发布历史 JSONL（默认 {DEFAULT_HISTORY}）")
    parser.add_argument("--no-history", action="store_true", help="只判定，不写历史")
    parser.add_argument("--history", action="store_true", help="读历史：最近几条 + 最近两条的差异")
    parser.add_argument("--history-limit", type=int, default=10, help="--history 打印最近几条")
    args = parser.parse_args()
    if args.selftest:
        return _selftest()
    history_path = Path(args.history_file) if args.history_file else DEFAULT_HISTORY
    if args.history:
        return print_history(history_path, args.history_limit)

    if args.repo:
        set_repo(Path(args.repo).resolve())
    dist = Path(args.dist) if args.dist else DEFAULT_DIST
    gate = Gate()

    if args.installer:
        installer = Path(args.installer)
    else:
        found = sorted(dist.glob("QIO_*_x64-setup.exe"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
        installer = found[-1] if found else dist / "QIO_0.0.0_x64-setup.exe"

    conf: dict = {}
    try:
        conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
        gate.add("tauri.conf.json", PASS, f"version={conf.get('version')}")
    except (OSError, ValueError) as exc:
        gate.add("tauri.conf.json", FAIL, f"读不出/解析失败：{exc}")

    digest = run_checks(gate, dist, installer, conf)

    print(gate.report())
    failures = gate.failed
    warnings = gate.warned
    print()
    if warnings:
        for item in warnings:
            print(f"  警告 {item.name}：{item.detail}")
        print()
    if failures:
        print(f"发布闸门：{len(failures)} 项不通过 —— 不要发布。")
    else:
        print("发布闸门：本地可判定的项目全部通过。")
        print("仍未覆盖（必须人工在真机做）：安装 → 首次启动 → 打包后端启动 → 内置模型就位 →")
        print("设置 provider → 保存凭据 → 对话 → 创建工具 → 测试前授权 → 依赖安装 → 测试 →")
        print("提交 → 注册 → 调用 → 工具记录 → 重启恢复 → 更新 → 卸载。")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "installer": str(installer),
                    "sha256": digest,
                    # 判定结果必须能对到"哪一版源码 + 哪次构建配置"
                    "commit": _git_commit(REPO),
                    "version": str(conf.get("version") or ""),
                    "results": [r.__dict__ for r in gate.results],
                    "failed": len(failures),
                    "warned": len(warnings),
                    "warnings": [r.name for r in warnings],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # 结果落库：先跟上一条比较（明确说出增减），再追加本次记录。
    if not args.no_history:
        record = make_record(gate, installer, digest, str(conf.get("version") or ""))
        previous = load_history(history_path)
        if previous:
            print()
            print("\n".join(format_comparison(compare_records(previous[-1], record))))
        append_history(history_path, record)
        print(f"\n历史记录 +1 -> {history_path}（共 {len(previous) + 1} 条）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
