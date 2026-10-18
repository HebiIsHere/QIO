#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第三方开源组件声明生成器（QIO）。

为什么需要它
------------
QIO 随安装包分发三套生态的第三方代码（Python 依赖、前端 npm 包、Rust crate），
外加内置嵌入模型与内置 CPython 运行时。MIT / BSD / ISC / OFL / PSF 这类许可证
要求「随二进制复制版权声明与许可证全文」，所以「仓库里没有这份声明」不是文档欠债，
而是分发时的合规缺口（见 docs/compliance.md）。

声明必须从**锁文件**推导，才能随依赖更新自动跟上，而不是靠人肉维护：

    输入（事实源）
      backend/uv.lock                    Python 依赖
      frontend/package-lock.json         前端依赖
      frontend/src-tauri/Cargo.lock      Rust crate
      licenses/overrides.json            本机与联网都取不到的条目（人工确认过）
      licenses/texts/*.txt               各许可证全文（首次联网落库后随仓库分发）

    输出
      licenses/third-party.json          机器可读清单（组件 -> 许可证 / 版权行）
      licenses/THIRD-PARTY-NOTICES.md    人类可读且自包含的声明（组件表 + 全文）
      licenses/texts/*.txt               本次用到但还缺全文的许可证

覆盖率由 scripts/licenses/check_third_party.py 校验（stdlib、离线、CI 跑）：
锁文件里出现的每个组件都必须在清单里，少一个就红。

用法
    python scripts/licenses/build_third_party.py             # 离线：本地已装依赖 + overrides
    python scripts/licenses/build_third_party.py --refresh   # 联网补 crates.io / PyPI 元数据

为什么需要 --refresh：锁文件是**全平台**的（Windows 上装不出来的 Linux/macOS/Android
依赖不会出现在本机 .venv 与 cargo 缓存里）。这些条目必须联网取一次元数据，结果落进
licenses/overrides.json 的 fetched 段，之后普通运行不再需要网络。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
TAURI = FRONTEND / "src-tauri"
LICENSES = ROOT / "licenses"
TEXTS = LICENSES / "texts"
OVERRIDES = LICENSES / "overrides.json"
MANIFEST = LICENSES / "third-party.json"
NOTICES = ROOT / "THIRD-PARTY-NOTICES.md"

UV_LOCK = BACKEND / "uv.lock"
NPM_LOCK = FRONTEND / "package-lock.json"
CARGO_LOCK = TAURI / "Cargo.lock"
SITE_PACKAGES = BACKEND / ".venv" / "Lib" / "site-packages"
CARGO_REGISTRY = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "registry" / "src"

# 反引号一律用 chr(96) 拼出来：脚本文本里不出现裸反引号，避免生成器自身被引号咬到。
BQ = chr(96)
FENCE = BQ * 3

USER_AGENT = "qio-third-party-notices/1.0 (offline-first license inventory; https://github.com/HebiIsHere/QIO)"
SPDX_TEXT_URL = "https://raw.githubusercontent.com/spdx/license-list-data/main/text/{id}.txt"
CRATES_API = "https://crates.io/api/v1/crates/{name}/{version}"
CRATE_DOWNLOAD = "https://static.crates.io/crates/{name}/{name}-{version}.crate"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
UV_PYTHON_DIRS = Path(os.environ.get("APPDATA", "")) / "uv" / "python"

# 本仓库自己的包不写进第三方声明
OWN_PACKAGES = {("python", "qio"), ("npm", "qio-frontend"), ("rust", "qio")}

LICENSE_FILE_RE = re.compile(r"(?i)^(licen[sc]e|copying|copyright|notice|unlicense)([-._].*)?$")
COPYRIGHT_RE = re.compile(
    r"^\s*(?:copyright|\(c\)|\u00a9)\s*(?:\(c\)|\u00a9)?\s*"
    r"(?P<years>\d{4}(?:\s*[-\u2013,]\s*\d{4})*)"
    r"[\s,.:;]*"
    r"(?P<holder>[^\n]{2,120})$",
    re.IGNORECASE,
)
COPYRIGHT_NO_YEAR_RE = re.compile(r"^\s*copyright\s+(?P<holder>[A-Z][^\n]{2,120})$", re.IGNORECASE)
# 许可证正文里出现的、不是「版权行」的句子（Apache-2.0 的样板最容易误伤）
COPYRIGHT_STOPWORDS = (
    "notice",
    "license",
    "holder",
    "owner",
    "that ",
    "to ",
    "in ",
    "for the",
    "grant",
    "and/or",
    "statement",
    "information",
    "law",
    "text",
    "authors of the font",
)

# ---------------------------------------------------------------------------
# 许可证标识归一化
# ---------------------------------------------------------------------------

# 建表依据：本仓库三套锁文件里实际出现过的原始写法。脚本会打印未命中的写法，
# 出现新的就补一行，而不是让它悄悄变成 UNKNOWN。
ALIASES = {
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "mit licence": "MIT",
    "expat": "MIT",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache": "Apache-2.0",
    "asl 2.0": "Apache-2.0",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "simplified bsd": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "isc": "ISC",
    "isc license": "ISC",
    "mpl-2.0": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "ofl-1.1": "OFL-1.1",
    "sil open font license 1.1": "OFL-1.1",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "python-2.0": "Python-2.0",
    "cnri-python": "CNRI-Python",
    "zlib": "Zlib",
    "zlib license": "Zlib",
    "cc0-1.0": "CC0-1.0",
    "cc0": "CC0-1.0",
    "0bsd": "0BSD",
    "unlicense": "Unlicense",
    "the unlicense": "Unlicense",
    "mit-0": "MIT-0",
    "blueoak-1.0.0": "BlueOak-1.0.0",
    "unicode-3.0": "Unicode-3.0",
    "unicode license v3": "Unicode-3.0",
    "unicode, inc. license agreement": "Unicode-3.0",
    "llvm exception": "LLVM-exception",
}

# 许可证正文的关键词 -> SPDX 标识（上游把整篇正文塞进 License 字段时用）
TEXT_SIGNATURES = (
    (("apache license", "version 2.0"), "Apache-2.0"),
    (("permission is hereby granted, free of charge",), "MIT"),
    (("redistribution and use in source and binary forms",), "BSD-3-Clause"),
    (("sil open font license",), "OFL-1.1"),
    (("mozilla public license",), "MPL-2.0"),
    (("python software foundation license",), "PSF-2.0"),
    (("gnu lesser general public license",), "LGPL"),
    (("gnu general public license",), "GPL"),
)


def normalize_license(raw: str) -> str:
    """把上游各种写法收敛成 SPDX 表达式；认不出来就返回空串（调用方负责报 UNKNOWN）。"""
    text = (raw or "").strip()
    if not text:
        return ""
    low = text.lower()
    if low in ALIASES:
        return ALIASES[low]
    if len(text) > 300:
        # 整篇正文塞在字段里：按特征识别，识别不出就别猜
        for needles, spdx in TEXT_SIGNATURES:
            if all(needle in low for needle in needles):
                return spdx
        return ""
    # 表达式：允许括号与 WITH 子句。Rust 生态里常见
    # "(Apache-2.0 OR MIT) AND BSD-3-Clause"、"Apache-2.0 WITH LLVM-exception OR MIT"，
    # 括号对「组件归属」没有信息量，WITH 只是给同一个标识加例外条款，两者都要能读。
    cleaned = text.replace("(", " ").replace(")", " ")
    pieces = re.split(r"\s+(OR|AND)\s+", cleaned)
    mapped = []
    operators = []
    for index, piece in enumerate(pieces):
        if index % 2 == 1:
            operators.append(piece.upper())
            continue
        operand = " ".join(piece.split())
        if not operand:
            continue
        # Rust 生态里还有 "MIT/Apache-2.0" 这种老式斜杠写法（等价于 OR）
        alternatives = []
        for raw_alt in re.split(r"\s*/\s*", operand):
            with_parts = [p.strip() for p in re.split(r"\s+WITH\s+", raw_alt, flags=re.IGNORECASE)]
            values = []
            for sub in with_parts:
                key = sub.lower()
                if key in ALIASES:
                    value = ALIASES[key]
                elif re.fullmatch(r"[A-Za-z0-9.\-+]+", sub):
                    value = sub  # 看起来已经是 SPDX 标识
                else:
                    return ""
                values.append(value)
            if values:
                alternatives.append(" WITH ".join(values))
        if alternatives:
            mapped.append(" OR ".join(alternatives))
    if not mapped:
        return ""
    # 全部操作数都认得出来才接受；对外保留**上游自己的写法**（含括号），
    # 只把空白归一 —— 声明是给人读的法律文本，把 "(A OR B) AND C" 改写成
    # "A OR B AND C" 会改变它的含义，那是我们不配做的改动。
    return " ".join(text.split())


def license_ids(expression: str) -> list:
    """从 SPDX 表达式里取出需要附全文的标识（含 WITH 的例外条款与老式斜杠写法）。"""
    ids = []
    cleaned = (expression or "").replace("(", " ").replace(")", " ")
    for token in re.split(r"\s+(?:OR|AND|WITH)\s+|\s*/\s*", cleaned):
        token = token.strip()
        if token and token not in ids:
            ids.append(token)
    return ids


# ---------------------------------------------------------------------------
# 版权行提取
# ---------------------------------------------------------------------------


def clean_copyright_lines(text: str) -> list:
    """从许可证/声明文本里挑出真正的版权行（挡掉 Apache-2.0 那样的正文样板）。"""
    found = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or len(line) > 200:
            continue
        low = line.lower()
        match = COPYRIGHT_RE.match(line) or COPYRIGHT_NO_YEAR_RE.match(line)
        if not match:
            continue
        holder = match.group("holder").strip(" .,:;-")
        if not holder or any(word in holder.lower() for word in COPYRIGHT_STOPWORDS):
            continue
        if any(word in low for word in COPYRIGHT_STOPWORDS):
            continue
        normalized = " ".join(line.split())
        if re.match(r"^\(c\)", normalized, re.IGNORECASE):
            normalized = "Copyright " + normalized
        if normalized not in found:
            found.append(normalized)
        if len(found) >= 3:
            break
    return found


def copyright_from_dir(directory) -> str:
    """从包目录里的 LICENSE / NOTICE / COPYING 抓版权行。"""
    if directory is None or not directory.is_dir():
        return ""
    candidates = []
    try:
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and LICENSE_FILE_RE.match(entry.name):
                candidates.append(entry)
        for nested in ("licenses", "LICENSES", "legal"):
            sub = directory / nested
            if sub.is_dir():
                candidates.extend(sorted(p for p in sub.iterdir() if p.is_file()))
    except OSError:
        return ""
    lines = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for item in clean_copyright_lines(text):
            if item not in lines:
                lines.append(item)
        if len(lines) >= 2:
            break
    return "\uff1b".join(lines[:2])


# ---------------------------------------------------------------------------
# 锁文件解析
# ---------------------------------------------------------------------------


def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_uv_lock() -> list:
    if not UV_LOCK.exists():
        raise SystemExit("缺少 " + str(UV_LOCK.relative_to(ROOT)))
    out = []
    for block in UV_LOCK.read_text(encoding="utf-8").split("[[package]]")[1:]:
        name = re.search(r'name = "([^"]+)"', block)
        version = re.search(r'version = "([^"]+)"', block)
        if not (name and version):
            continue
        if 'editable = "."' in block:
            continue  # 本仓库自己的包
        out.append({"eco": "python", "name": name.group(1), "version": version.group(1)})
    return out


def parse_package_lock() -> list:
    if not NPM_LOCK.exists():
        raise SystemExit("缺少 " + str(NPM_LOCK.relative_to(ROOT)))
    data = json.loads(NPM_LOCK.read_text(encoding="utf-8"))
    out = []
    for path, entry in (data.get("packages") or {}).items():
        if not path.startswith("node_modules/"):
            continue
        name = path.split("node_modules/")[-1]
        version = str(entry.get("version") or "")
        if not version:
            continue
        out.append(
            {
                "eco": "npm",
                "name": name,
                "version": version,
                "declared_license": entry.get("license"),
                "dir": FRONTEND / path,
            }
        )
    return out


def parse_cargo_lock() -> list:
    if not CARGO_LOCK.exists():
        raise SystemExit("缺少 " + str(CARGO_LOCK.relative_to(ROOT)))
    out = []
    for block in CARGO_LOCK.read_text(encoding="utf-8").split("[[package]]")[1:]:
        name = re.search(r'name = "([^"]+)"', block)
        version = re.search(r'version = "([^"]+)"', block)
        if not (name and version):
            continue
        out.append({"eco": "rust", "name": name.group(1), "version": version.group(1)})
    return out


# ---------------------------------------------------------------------------
# 本地解析（离线路径）
# ---------------------------------------------------------------------------


def python_dist_dir(name: str, version: str):
    if not SITE_PACKAGES.is_dir():
        return None
    target = name.replace("-", "_").lower()
    for entry in sorted(SITE_PACKAGES.iterdir()):
        if not entry.is_dir() or not entry.name.endswith(".dist-info"):
            continue
        stem = entry.name[: -len(".dist-info")]
        head, _, tail = stem.partition("-")
        if head.lower() == target and (not tail or tail == version):
            return entry
    return None


def read_metadata_fields(metadata: str) -> dict:
    fields = {}
    for line in metadata.splitlines():
        if not line or line[0] in " \t":
            continue
        key, _, value = line.partition(":")
        fields.setdefault(key.strip().lower(), []).append(value.strip())
    return fields


def python_license_from_metadata(metadata: str) -> str:
    fields = read_metadata_fields(metadata)
    expression = (fields.get("license-expression") or [""])[0]
    if expression:
        return normalize_license(expression)
    raw = " ".join(fields.get("license") or [])
    normalized = normalize_license(raw)
    if normalized:
        return normalized
    for classifier in fields.get("classifier") or []:
        if not classifier.startswith("License ::"):
            continue
        normalized = normalize_license(classifier.rsplit("::", 1)[-1].strip())
        if normalized:
            return normalized
    return ""


def resolve_python(component: dict) -> dict:
    dist = python_dist_dir(component["name"], component["version"])
    license_id = ""
    copyright_line = ""
    if dist is not None:
        try:
            metadata = (dist / "METADATA").read_text(encoding="utf-8", errors="replace")
        except OSError:
            metadata = ""
        license_id = python_license_from_metadata(metadata)
        copyright_line = copyright_from_dir(dist)
    return {
        "license": license_id,
        "copyright": copyright_line,
        "source": "site-packages" if dist is not None else "",
    }


def cargo_registry_dir(name: str, version: str):
    if not CARGO_REGISTRY.is_dir():
        return None
    for index in sorted(CARGO_REGISTRY.iterdir()):
        candidate = index / (name + "-" + version)
        if candidate.is_dir():
            return candidate
    return None


def resolve_rust(component: dict) -> dict:
    directory = cargo_registry_dir(component["name"], component["version"])
    if directory is None:
        return {"license": "", "copyright": "", "source": ""}
    license_id = ""
    cargo_toml = directory / "Cargo.toml"
    if cargo_toml.exists():
        try:
            text = cargo_toml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        match = re.search(r'^\s*license\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match:
            license_id = normalize_license(match.group(1))
        else:
            match = re.search(r'^\s*license-file\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if match:
                license_path = directory / match.group(1)
                if license_path.exists():
                    try:
                        license_id = normalize_license(
                            license_path.read_text(encoding="utf-8", errors="replace")[:4000]
                        )
                    except OSError:
                        license_id = ""
    return {
        "license": license_id,
        "copyright": copyright_from_dir(directory),
        "source": "cargo-registry",
    }


def resolve_npm(component: dict) -> dict:
    directory = component.get("dir")
    license_id = normalize_license(str(component.get("declared_license") or ""))
    if not license_id and directory is not None:
        package_json = directory / "package.json"
        if package_json.exists():
            try:
                data = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
                declared = data.get("license")
                if isinstance(declared, dict):
                    declared = declared.get("type")
                license_id = normalize_license(str(declared or ""))
            except (OSError, ValueError):
                pass
    return {
        "license": license_id,
        "copyright": copyright_from_dir(directory),
        "source": "node_modules" if directory is not None and directory.is_dir() else "package-lock",
    }


# ---------------------------------------------------------------------------
# 联网解析（--refresh）：锁文件是全平台的，本机装不出别的平台依赖
# ---------------------------------------------------------------------------


def fetch_bytes(url: str, retries: int = 2, timeout: int = 45) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - 联网失败要如实落进 overrides
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(url + " 取不到：" + str(last))


def fetch_json(url: str) -> dict:
    return json.loads(fetch_bytes(url).decode("utf-8", "replace"))


def refresh_rust(name: str, version: str, with_archive: bool = True) -> dict:
    """crates.io 元数据（可选：再读 .crate 包内的许可证文件，用来拿真实版权行）。

    `with_archive=False`（--fast）：只取元数据。crate 归档动辄几百 KB，一百多个平台专属
    crate 逐个下载会把一次维护者操作拖到几十分钟；先拿全许可证标识，再按需补版权行更划算。
    """
    result = {"license": "", "copyright": "", "attribution": "", "source": "crates.io"}
    try:
        meta = fetch_json(CRATES_API.format(name=name, version=version))
    except Exception as exc:  # noqa: BLE001
        result["note"] = "crates.io 元数据取不到：" + str(exc)
        return result
    info = meta.get("version") or {}
    result["license"] = normalize_license(str(info.get("license") or ""))
    result["attribution"] = str(info.get("repository") or info.get("homepage") or "")
    if not with_archive:
        return result
    try:
        blob = fetch_bytes(CRATE_DOWNLOAD.format(name=name, version=version))
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            for member in archive.getmembers():
                base = os.path.basename(member.name)
                if not member.isfile() or not LICENSE_FILE_RE.match(base):
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                lines = clean_copyright_lines(handle.read().decode("utf-8", "replace"))
                if lines:
                    result["copyright"] = "\uff1b".join(lines[:2])
                    break
    except Exception as exc:  # noqa: BLE001
        result.setdefault("note", "crate 归档取不到：" + str(exc))
    return result


def _metadata_from_archive(blob: bytes):
    """从 sdist/wheel 里读 METADATA/PKG-INFO 与许可证正文，返回 (metadata, license_text)。"""
    metadata = ""
    license_text = ""
    if blob[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                for entry in archive.namelist():
                    base = os.path.basename(entry)
                    if entry.endswith(".dist-info/METADATA") and not metadata:
                        metadata = archive.read(entry).decode("utf-8", "replace")
                    elif LICENSE_FILE_RE.match(base) and not license_text:
                        license_text = archive.read(entry).decode("utf-8", "replace")
        except (zipfile.BadZipFile, OSError):
            return "", ""
        return metadata, license_text
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            for member in archive.getmembers():
                base = os.path.basename(member.name)
                if not member.isfile():
                    continue
                if base in ("PKG-INFO", "METADATA") and not metadata:
                    handle = archive.extractfile(member)
                    if handle is not None:
                        metadata = handle.read().decode("utf-8", "replace")
                elif LICENSE_FILE_RE.match(base) and not license_text:
                    handle = archive.extractfile(member)
                    if handle is not None:
                        license_text = handle.read().decode("utf-8", "replace")
    except (tarfile.TarError, OSError):
        return "", ""
    return metadata, license_text


def refresh_python(name: str, version: str, with_archive: bool = True) -> dict:
    """PyPI JSON，可选再读最小的那个发行包（里面同时有 METADATA 与许可证正文）。

    `with_archive=False`（--fast）：只读 PyPI JSON。从国内网络下 PyPI 文件经常极慢，
    而 JSON 里已经有 License-Expression / Classifier / 作者；缺版权行的条目会记成
    「版权行见上游」，不会因此变成无归属。
    """
    result = {"license": "", "copyright": "", "attribution": "", "source": "pypi"}
    try:
        data = fetch_json(PYPI_JSON.format(name=name, version=version))
    except Exception as exc:  # noqa: BLE001
        result["note"] = "PyPI 元数据取不到：" + str(exc)
        return result
    info = data.get("info") or {}
    header = ["License-Expression: " + str(info.get("license_expression") or "")]
    header.append("License: " + str(info.get("license") or ""))
    header.extend("Classifier: " + str(c) for c in (info.get("classifiers") or []))
    result["license"] = python_license_from_metadata("\n".join(header))
    if not with_archive:
        if not result["copyright"] and info.get("author"):
            result["attribution"] = str(info.get("author"))
        return result
    urls = [u for u in (data.get("urls") or []) if u.get("url")]
    urls.sort(key=lambda u: int(u.get("size") or 0))
    for url_info in urls[:2]:
        try:
            blob = fetch_bytes(url_info["url"])
        except Exception:  # noqa: BLE001
            continue
        metadata, license_text = _metadata_from_archive(blob)
        if not result["license"] and metadata:
            result["license"] = python_license_from_metadata(metadata)
        if not result["copyright"] and license_text:
            result["copyright"] = "\uff1b".join(clean_copyright_lines(license_text)[:2])
        if result["license"] and result["copyright"]:
            break
    if not result["copyright"] and info.get("author"):
        result["attribution"] = str(info.get("author"))
    return result


# ---------------------------------------------------------------------------
# overrides 与许可证全文库
# ---------------------------------------------------------------------------


def load_overrides() -> dict:
    if not OVERRIDES.exists():
        return {"manual": {}, "fetched": {}}
    try:
        data = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(str(OVERRIDES.relative_to(ROOT)) + " 解析失败：" + str(exc))
    data.setdefault("manual", {})
    data.setdefault("fetched", {})
    return data


def write_text(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def save_overrides(data: dict) -> None:
    payload = {
        "_comment": [
            "manual：人工确认过的条目（脚本永不覆盖）。",
            "fetched：由 --refresh 联网写入（crates.io / PyPI），重跑会覆盖同键。",
            "键格式：<eco>:<name>@<version>；字段：license / copyright / attribution / note。",
        ],
        "manual": dict(sorted((data.get("manual") or {}).items())),
        "fetched": dict(sorted((data.get("fetched") or {}).items())),
    }
    write_text(OVERRIDES, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def local_text_for(license_id: str) -> str:
    """PSF-2.0 直接用本机 uv 下载的 CPython LICENSE.txt：那是我们真正随包分发的那份。"""
    if license_id == "PSF-2.0":
        for candidate in reversed(sorted(UV_PYTHON_DIRS.glob("cpython-*/LICENSE.txt"))):
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return ""


def ensure_texts(wanted: set, refresh: bool) -> dict:
    """保证每个用到的许可证都有一份全文；返回 {id: 原因}（空 dict = 都齐了）。"""
    TEXTS.mkdir(parents=True, exist_ok=True)
    missing = {}
    for license_id in sorted(wanted):
        if not re.fullmatch(r"[A-Za-z0-9.\-+]+", license_id):
            missing[license_id] = "不是 SPDX 标识，无法取全文"
            continue
        target = TEXTS / (license_id + ".txt")
        if target.exists() and target.stat().st_size > 200:
            continue
        text = local_text_for(license_id)
        if text:
            write_text(target, text if text.endswith("\n") else text + "\n")
            continue
        if not refresh:
            missing[license_id] = "缺全文（离线运行；用 --refresh 抓一次）"
            continue
        try:
            fetched = fetch_bytes(SPDX_TEXT_URL.format(id=license_id)).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            missing[license_id] = "SPDX 全文取不到：" + str(exc)
            continue
        if len(fetched) < 200:
            missing[license_id] = "SPDX 返回内容过短，未落库"
            continue
        write_text(target, fetched if fetched.endswith("\n") else fetched + "\n")
    return missing


# ---------------------------------------------------------------------------
# 随包分发的资产（不走锁文件：内置模型、内置 CPython、字体）
# ---------------------------------------------------------------------------

ASSETS = [
    {
        "eco": "bundled",
        "name": "CPython",
        "version": "3.11",
        "license": "PSF-2.0",
        "copyright": "Copyright (c) 2001-2026 Python Software Foundation. All Rights Reserved.",
        "note": "安装包自带的 Python 运行时（resources/python-runtime，构建期由 scripts/build_runtime.ps1 生成），"
        "供「带第三方依赖的生成工具」离线建 venv；许可见该目录的 LICENSE-CPython.txt。",
    },
    {
        "eco": "bundled",
        "name": "python-build-standalone",
        "version": "astral-sh",
        "license": "MPL-2.0",
        "copyright": "Copyright (c) Astral Software Inc.",
        "attribution": "https://github.com/astral-sh/python-build-standalone",
        "note": "上面那份 CPython 运行时的发行来源（scripts/build_runtime.ps1 通过 uv 取得）。",
    },
    {
        "eco": "bundled",
        "name": "bge-small-zh-v1.5",
        "version": "fp32",
        "license": "MIT",
        "copyright": "Copyright (c) BAAI (Beijing Academy of Artificial Intelligence)",
        "note": "内置中文嵌入模型（ONNX 转换版来自社区仓库 Xenova/bge-small-zh-v1.5），随安装包分发；"
        "完整声明见 resources/models/bge-small-zh-v1.5/ 下的 LICENSE-BAAI-bge-small-zh-v1.5.txt 与 NOTICE.txt。",
    },
    {
        "eco": "bundled",
        "name": "Noto Serif SC",
        "version": "font-files",
        "license": "OFL-1.1",
        "copyright": "Google Inc.",
        "attribution": "https://github.com/fontsource/font-files",
        "note": "中文衬线字体，经 @fontsource/noto-serif-sc 分发，字体文件随前端产物分发。",
    },
]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def clean_url(value: str) -> str:
    """把上游元数据里常见的脏写法收拾一下：`project-url` 的「标签, 地址」、git+ 前缀、引号。"""
    text = " ".join(str(value or "").split()).strip()
    if ", http" in text:
        text = text.split(", http", 1)[1]
        text = "http" + text
    if text.startswith("git+"):
        text = text[4:]
    if text.endswith(".git"):
        text = text[:-4]
    return text.replace('"', "").strip()


def package_attribution(component: dict) -> str:
    """上游包的可核查出处：优先仓库 / 作者，退到注册表包页。

    为什么要退到包页：锁文件是**全平台**的，本机装不出别的平台那一份（例如 `@esbuild/*` 的各平台二进制）。
    这些条目本地既没有 LICENSE 也没有 package.json，但它们在注册表里有确定的发布位置 ——
    声明里给出这个位置，比写「（未提供仓库地址）」有用得多，也让覆盖率检查有东西可判。
    """
    eco = component["eco"]
    name = str(component.get("name") or "")
    if eco == "npm":
        directory = component.get("dir")
        parts = []
        if directory is not None and directory.is_dir():
            try:
                data = json.loads((directory / "package.json").read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                data = {}
            author = data.get("author")
            if isinstance(author, dict):
                author = author.get("name")
            repo = data.get("repository")
            if isinstance(repo, dict):
                repo = repo.get("url")
            parts = [clean_url(str(x)) for x in (author, repo) if x]
        if not parts:
            parts = ["https://www.npmjs.com/package/" + name]
        return " / ".join(parts)
    if eco == "python":
        dist = python_dist_dir(component["name"], component["version"])
        parts = []
        if dist is not None:
            try:
                fields = read_metadata_fields((dist / "METADATA").read_text(encoding="utf-8", errors="replace"))
            except OSError:
                fields = {}
            author = (fields.get("author") or [""])[0] or (fields.get("author-email") or [""])[0]
            if author:
                parts.append(clean_url(author))
            urls = [clean_url(u) for u in (fields.get("project-url") or []) if u]
            if urls:
                parts.append(urls[0])
        if not parts:
            parts = ["https://pypi.org/project/" + name + "/"]
        return " / ".join(parts)
    if eco == "rust":
        directory = cargo_registry_dir(component["name"], component["version"])
        if directory is not None:
            try:
                text = (directory / "Cargo.toml").read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            match = re.search(r'^\s*repository\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if match:
                return match.group(1)
        # 本地缓存里没有这个 crate（别的平台专属依赖）：crates.io 的包页就是可核查的出处
        return "https://crates.io/crates/" + name
    return ""


def component_key(entry: dict) -> str:
    return str(entry.get("eco")) + ":" + str(entry.get("name")) + "@" + str(entry.get("version"))


def build_components(refresh: bool, overrides: dict, with_archives: bool = True, progress=print):
    raw = [*parse_uv_lock(), *parse_package_lock(), *parse_cargo_lock()]
    manual = overrides.get("manual") or {}
    fetched = overrides.get("fetched") or {}
    unresolved = []
    components = []

    for item in raw:
        if (item["eco"], item["name"]) in OWN_PACKAGES:
            continue
        key = item["eco"] + ":" + item["name"] + "@" + item["version"]
        if item["eco"] == "python":
            info = resolve_python(item)
        elif item["eco"] == "rust":
            info = resolve_rust(item)
        else:
            info = resolve_npm(item)

        license_id = info.get("license") or ""
        copyright_line = info.get("copyright") or ""
        attribution = package_attribution(item)
        note = ""

        # 已有 manual / fetched 记录的条目不再重取：一次维护者操作的结果必须能复用，
        # 否则每次重跑都要把一百多个上游包再问一遍。
        cached_entry = manual.get(key) or fetched.get(key) or {}
        # 只有「本地连归属都给不出来」时才需要问上游：
        #   * 没有许可证标识；或
        #   * 既没有版权行、也没有上游出处（仓库 / 作者）。
        # 版权行普遍不存在（Rust 生态大量 crate 只随包 Apache-2.0 正文），
        # 而本地 Cargo.toml / METADATA 已经能给出仓库与作者 —— 那种条目再联网只是白花时间。
        needs_network = not cached_entry and (not license_id or (not copyright_line and not attribution))
        if refresh and needs_network and item["eco"] in ("rust", "python"):
            progress("  refresh " + key)
            try:
                if item["eco"] == "rust":
                    fresh = refresh_rust(item["name"], item["version"], with_archive=with_archives)
                else:
                    fresh = refresh_python(item["name"], item["version"], with_archive=with_archives)
            except Exception as exc:  # noqa: BLE001
                fresh = {"note": "refresh 失败：" + str(exc)}
            merged = dict(fetched.get(key) or {})
            for field, value in fresh.items():
                if value:
                    merged[field] = value
            fetched[key] = merged
            overrides["fetched"] = fetched
            if len(fetched) % 10 == 0:
                # 增量落盘：一百多次联网取数中途被打断时，已经拿到的结果不该一起丢
                save_overrides(overrides)
            license_id = license_id or merged.get("license") or ""
            copyright_line = copyright_line or merged.get("copyright") or ""
            attribution = merged.get("attribution") or attribution
            note = merged.get("note") or ""

        override = manual.get(key) or fetched.get(key) or {}
        if override:
            license_id = license_id or override.get("license") or ""
            copyright_line = copyright_line or override.get("copyright") or ""
            attribution = override.get("attribution") or attribution
            note = override.get("note") or note
        if not license_id:
            license_id = "UNKNOWN"

        entry = {
            "eco": item["eco"],
            "name": item["name"],
            "version": item["version"],
            "license": license_id,
            "copyright": " ".join(copyright_line.split()),
        }
        if attribution:
            entry["attribution"] = " ".join(str(attribution).split())
        if note:
            entry["note"] = note
        if license_id == "UNKNOWN":
            unresolved.append(entry)
        # 同一个 name@version 可能在锁文件里出现多次（嵌套 node_modules、多份锁记录）：
        # 声明里只保留一条，避免清单里出现重复行。
        components = [existing for existing in components if component_key(existing) != key]
        components.append(entry)

    overrides["fetched"] = fetched
    return components, ASSETS, {"unresolved": unresolved}


def write_manifest(components, assets, inputs) -> None:
    payload = {
        "generated_by": "scripts/licenses/build_third_party.py",
        "coverage": "三套锁文件里的全部组件（含只在其它平台用到的），外加随包分发的资产",
        "inputs": inputs,
        "components": sorted(components, key=lambda c: (c["eco"], c["name"], c["version"])),
        "assets": assets,
    }
    write_text(MANIFEST, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def text_body(license_id: str) -> str:
    path = TEXTS / (license_id + ".txt")
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").rstrip("\n")


def component_line(entry: dict) -> str:
    line = "- " + BQ + entry["name"] + "@" + entry["version"] + BQ + "（" + entry["eco"] + "）"
    if entry.get("copyright"):
        line += " — " + entry["copyright"]
    elif entry.get("attribution"):
        line += " — 版权行见上游：" + entry["attribution"]
    if entry.get("note"):
        line += " — " + entry["note"]
    return line


def write_notices(components, assets, inputs, missing_texts) -> None:
    by_license = {}
    for entry in [*components, *assets]:
        by_license.setdefault(entry["license"], []).append(entry)

    lines = []
    lines.append("# QIO 第三方开源组件声明")
    lines.append("")
    lines.append("本文件由 " + BQ + "scripts/licenses/build_third_party.py" + BQ + " 生成，**请勿手工编辑**；")
    lines.append("覆盖率由 " + BQ + "scripts/licenses/check_third_party.py" + BQ + " 校验：锁文件里少一个组件就失败。")
    lines.append("")
    lines.append("覆盖范围：三套锁文件里的全部组件（含只在其它平台用到的），外加随包分发的资产。")
    lines.append("许可证全文见文末；正文里的版权行缺失时会在该条目上写明出处。")
    lines.append("")
    lines.append("生成输入（sha256 前 16 位）：")
    for rel in sorted(inputs):
        lines.append("- " + BQ + rel + BQ + " — " + BQ + inputs[rel] + BQ)
    lines.append("")
    lines.append("## 组件与许可证")
    lines.append("")
    for license_id in sorted(by_license):
        entries = sorted(by_license[license_id], key=lambda e: (e["eco"], e["name"], e["version"]))
        lines.append("### " + license_id + "（" + str(len(entries)) + " 个）")
        lines.append("")
        for entry in entries:
            lines.append(component_line(entry))
        lines.append("")
    without_copyright = [e for e in [*components, *assets] if not e.get("copyright")]
    if without_copyright:
        lines.append("## 上游未提供版权行的组件")
        lines.append("")
        lines.append("这些上游包没有可提取的版权行（其许可证正文也不含版权行）：")
        lines.append("")
        for entry in sorted(without_copyright, key=lambda e: (e["eco"], e["name"])):
            target = entry.get("attribution") or "（未提供仓库地址）"
            lines.append("- " + BQ + entry["name"] + "@" + entry["version"] + BQ + "（" + entry["eco"] + "，"
                         + entry["license"] + "）— " + target)
        lines.append("")
    lines.append("## 许可证全文")
    lines.append("")
    lines.append("双许可表达式（例如 `MIT OR Apache-2.0`、`Apache-2.0 WITH LLVM-exception`）按其中的每个标识各附一份全文。")
    lines.append("")
    # 按**标识**而不是按表达式附全文：表达式只是分组的名字，正文必须落到具体标识上，
    # 否则 "`MIT OR Apache-2.0`（123 个）" 这种分组会一份全文都不带。
    referenced_ids = set()
    for entry in [*components, *assets]:
        referenced_ids.update(license_ids(entry["license"]))
    for license_id in sorted(referenced_ids):
        body = text_body(license_id)
        lines.append("### " + license_id)
        lines.append("")
        if body:
            lines.append(FENCE)
            lines.append(body)
            lines.append(FENCE)
        else:
            lines.append("> 本仓尚未落库 " + license_id + " 的全文（" + missing_texts.get(license_id, "未知原因") + "）。")
        lines.append("")
    write_text(NOTICES, "\n".join(lines).rstrip("\n") + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 QIO 第三方组件声明")
    parser.add_argument("--refresh", action="store_true", help="联网补 crates.io / PyPI 元数据（写进 overrides.json）")
    parser.add_argument("--fast", action="store_true", help="--refresh 时只取元数据，不下载 crate / 发行包")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    overrides = load_overrides()
    components, assets, stats = build_components(
        refresh=args.refresh, overrides=overrides, with_archives=not args.fast
    )
    if args.refresh:
        save_overrides(overrides)

    wanted = set()
    for entry in [*components, *assets]:
        wanted.update(license_ids(entry["license"]))
    # UNKNOWN 没有对应的 SPDX 全文，也不该被"抓一次全文"掩盖过去：checker 会直接判红
    wanted.discard("UNKNOWN")
    missing_texts = ensure_texts(wanted, refresh=args.refresh)

    inputs = {}
    for path in (UV_LOCK, NPM_LOCK, CARGO_LOCK):
        if path.exists():
            inputs[str(path.relative_to(ROOT)).replace("\\", "/")] = sha256_of(path)[:16]

    write_manifest(components, assets, inputs)
    write_notices(components, assets, inputs, missing_texts)

    if not args.quiet:
        print("组件 " + str(len(components)) + " 个，另声明随包资产 " + str(len(assets)) + " 个")
        print("引用许可证标识 " + str(len({e["license"] for e in [*components, *assets]})) + " 种")
        if missing_texts:
            print("缺全文：")
            for license_id in sorted(missing_texts):
                print("  " + license_id + " — " + missing_texts[license_id])
        if stats["unresolved"]:
            print("仍未解析出许可证（checker 会失败）：")
            for entry in stats["unresolved"][:30]:
                print("  " + entry["eco"] + ":" + entry["name"] + "@" + entry["version"])
            print("  共 " + str(len(stats["unresolved"])) + " 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
