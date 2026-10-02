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
6. 打包配置自洽：externalBin / resources / createUpdaterArtifacts。

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

    def report(self) -> str:
        width = max((len(r.name) for r in self.results), default=10)
        lines = [f"{'检查项'.ljust(width)}  结果   说明"]
        for r in self.results:
            lines.append(f"{r.name.ljust(width)}  {r.state}  {r.detail}")
        return "\n".join(lines)


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
    newly_failed = sorted(k for k, v in cur.items() if v != PASS and prev.get(k) == PASS)
    newly_passed = sorted(k for k, v in cur.items() if v == PASS and k in prev and prev[k] != PASS)
    still_failing = sorted(k for k, v in cur.items() if v != PASS and prev.get(k) not in (PASS, None))
    return {
        "previous_ts": previous.get("ts"),
        "previous_version": previous.get("version"),
        "previous_commit": previous.get("commit"),
        "newly_passed": newly_passed,
        "newly_failed": newly_failed,
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
    print(f"{'时间':<26} {'版本':<10} {'commit':<10} {'PASS':>5} {'FAIL':>5} {'SKIP':>5}  安装包")
    for record in records[-limit:]:
        counts = record.get("counts") or {}
        print(
            f"{(record.get('ts') or '?')[:26]:<26} {(record.get('version') or '?')[:10]:<10} "
            f"{(record.get('commit') or '?')[:8]:<10} "
            f"{counts.get(PASS, 0):>5} {counts.get(FAIL, 0):>5} {counts.get(SKIP, 0):>5}  "
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
        gate.add("bundle.config", PASS, "externalBin / resources / createUpdaterArtifacts 自洽")


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


def _make_fake_minisign(key_id: bytes, payload_len: int) -> str:
    """造一份结构合法（但不做真实验签）的 minisign 载荷，供自检用。"""
    blob = b"ED" + key_id + bytes(payload_len - 10)
    inner = "untrusted comment: fake\n" + base64.b64encode(blob).decode("ascii") + "\n"
    return base64.b64encode(inner.encode("utf-8")).decode("ascii")


def build_fixture(root: Path, *, sidecar_newer: bool = False, bad_hash: bool = False) -> Path:
    """造一个最小的发布产物目录（自检与历史回归测试共用，只写合成数据）。

    `bad_hash=True` 让 SHA256SUMS.txt 记一个错的哈希；`sidecar_newer=True` 造出
    「包里是旧后端」这件事。返回 dist 目录。
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
                },
                "plugins": {"updater": {"pubkey": _make_fake_minisign(key_id, 42)}},
            }
        ),
        encoding="utf-8",
    )
    installer_path = dist / "QIO_9.9.9_x64-setup.exe"
    installer_path.write_bytes(b"MZ" + b"Nullsoft" + "9.9.9".encode("utf-16-le") + b"QIO" + b"x" * 60_000_000)
    (dist / "QIO_9.9.9_x64-setup.exe.sig").write_text(_make_fake_minisign(key_id, 74), encoding="utf-8")
    digest = sha256_of(dist / "QIO_9.9.9_x64-setup.exe")
    recorded = "0" * 64 if bad_hash else digest
    (dist / "SHA256SUMS.txt").write_text(f"QIO_9.9.9_x64-setup.exe  {recorded}\n", encoding="utf-8")
    (dist / "latest.json").write_text(
        json.dumps(
            {
                "version": "9.9.9",
                "platforms": {
                    "windows-x86_64": {
                        "signature": _make_fake_minisign(key_id, 74),
                        "url": "https://example.invalid/QIO_9.9.9_x64-setup.exe",
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
    return dist



def _selftest() -> int:
    """自检：造一个最小的发布目录，先要求全绿，再逐一注入缺陷要求变红。"""
    import shutil
    import tempfile

    failures: list[str] = []

    tmp = Path(tempfile.mkdtemp(prefix="qio-gate-selftest-"))  # noqa: S108 - 自检临时目录
    try:
        for label, kwargs, want_fail in (
            ("健康产物", {}, None),
            ("哈希不符", {"bad_hash": True}, "sha256sums"),
            ("安装包比 sidecar 旧", {"sidecar_newer": True}, "sidecar.fresh"),
        ):
            root = tmp / label
            build_fixture(root, **kwargs)
            set_repo(root)
            gate = Gate()
            conf = json.loads((root / "frontend" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
            installer = root / "dist" / "QIO_9.9.9_x64-setup.exe"
            digest = check_installer(gate, installer)
            check_sums(gate, root / "dist", installer, digest)
            check_manifest(gate, root / "dist", installer, conf)
            check_bundle_config(gate, conf)
            check_nsis_strings(gate, installer, conf)
            check_sig_file(gate, root / "dist", installer)
            check_sidecar(gate, installer)
            check_models(gate)
            failed = {r.name for r in gate.failed}
            if want_fail is None:
                if failed:
                    failures.append(f"{label}: 期望全绿，实际失败 {sorted(failed)}")
            elif want_fail not in failed:
                failures.append(f"{label}: 期望 {want_fail} 变红，实际失败 {sorted(failed) or '无'}")
            else:
                print(f"  [OK] {label} -> {want_fail} 正确变红")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        set_repo(Path(__file__).resolve().parents[1])

    if failures:
        print("[FAIL] 发布闸门自检失败：")
        for item in failures:
            print("  -", item)
        return 1
    print("[PASS] 发布闸门自检通过：健康产物全绿，哈希不符 / 后端过期 都能变红")
    return 0


def main() -> int:
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

    digest = check_installer(gate, installer)
    check_sums(gate, dist, installer, digest)
    if conf:
        check_manifest(gate, dist, installer, conf)
        check_bundle_config(gate, conf)
        if installer.exists():
            check_nsis_strings(gate, installer, conf)
    if installer.exists():
        check_sig_file(gate, dist, installer)
    check_sidecar(gate, installer)
    check_models(gate)

    print(gate.report())
    failures = gate.failed
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
                    "results": [r.__dict__ for r in gate.results],
                    "failed": len(failures),
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
