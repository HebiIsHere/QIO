"""卡片内预览：转换层（契约 3.2，冻结接口）。

这个模块只做一件事：**把附件的真实字节转成可渲染的预览数据**。
它不写事件、不调模型、不改板面状态 —— 预览是"看"，不是"已理解"。

冻结接口（契约 3.2，不得单方面改签名）：

    RECIPE_VERSION / MAX_PREVIEW_BYTES / MAX_PREVIEW_UNITS /
    MAX_ROWS_PER_SHEET / MAX_COLS_PER_SHEET
    classify_preview(name) -> "image"|"pdf"|"docx"|"xlsx"|"pptx"|"code"|"legacy_office"|"unsupported"
    convert(att, path, *, cancel=None) -> dict        # 纯计算：不写库、不发事件、异常向上抛
    preview_cache_key(attachment_id, version, kind) -> str
    render_for(store, attachment_id) -> dict          # 带缓存与版本校验的入口

实现约束（契约 3.2 "实现约束（硬）"）：

* docx/xlsx/pptx 用 zipfile + xml.etree **增量解析**，直接复用
  tools/attachment_tools.py 已有的 _ZipBudget / _CountingReader（同一套
  "按实际读出的字节计量、不信任 ZIP 声明大小"的预算与取消语义）；
* pdf 只给**事实**（页数 / 标题 / 是否加密 / 可提取文本量），页面渲染在前端；
* 任何异常都变成 status="failed" + 可读 reason，**绝不抛出 500**；
* 加密 / 损坏 / 超限 / 未支持分别给不同 status 与可读 reason；
* 截断要如实标 truncated（单元数 / 行数 / 列数 / 行长）。

线程纪律（沿用 services/attachments.py 的约定）：

* 本模块的 DB 访问（attachment_previews 缓存）只发生在**调用 render_for 的那个线程**；
* 解析是纯计算，可以丢进 asyncio.to_thread；异步路由用 render_for_async()，
  它把 convert 放进工作线程、把缓存读写留在事件循环线程（共享 sqlite 连接只有一个）。

版本纪律（契约 3.3）：

* 缓存键 = (attachment_id, version, recipe)，版本变了自然不命中；
* 写缓存之前**再核对一次**当前版本：转换期间文件被改过（或被重新关联）时，
  旧版本的结果绝不写到新版本上（迟到结果不写缓存）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from agent.services.attachments import Attachment, AttachmentContentError, human_size
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 冻结常量（契约 3.2）
# ---------------------------------------------------------------------------

RECIPE_VERSION = 1
MAX_PREVIEW_BYTES = 64 * 1024 * 1024
MAX_PREVIEW_UNITS = 4000
MAX_ROWS_PER_SHEET = 500
MAX_COLS_PER_SHEET = 60

#: 单行上限：压缩过的 JSON / minified JS 一行能有几 MB，整行塞进预览没有意义
MAX_LINE_CHARS = 4000
#: 一次预览最多带多少张表（docx 表格数 / pptx 幻灯片内的表格由单元预算兜底）
MAX_TABLES = 200
#: PDF 可提取文本量的探测页数上限（只报事实，不做 OCR）
MAX_PDF_TEXT_PAGES = 50
#: 代码/文本预览最多读多少字节（MAX_PREVIEW_BYTES 之内再夹一道，避免整份读进内存）
CODE_READ_BYTES = 4 * 1024 * 1024

# ---------------------------------------------------------------------------
# 格式矩阵（契约 3.1）
# ---------------------------------------------------------------------------

IMAGE_KINDS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
LEGACY_OFFICE_EXTS = {".doc", ".xls", ".ppt"}
XLSX_EXTS = {".xlsx", ".xlsm"}
#: 契约 3.1 ①：html/xml/svg **不内联**（会执行脚本 / 带外链），但走"转换层返回文本"。
#: 注意：这里只是把它们分类成 code（返回纯文本行），前端**绝不**按 HTML 解析。
CODE_EXTS = {
    ".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json", ".jsonl", ".ndjson",
    ".json5", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".properties", ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
    ".svelte", ".java", ".kt", ".kts", ".scala", ".go", ".rs", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".cs", ".rb", ".php", ".pl", ".lua", ".r", ".m", ".swift", ".sh", ".bash",
    ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".sql", ".gradle", ".css", ".scss", ".less",
    ".rst", ".tex", ".diff", ".patch", ".html", ".htm", ".xhtml", ".svg", ".dockerfile",
    ".makefile",
}

#: 扩展名 → 语言 id（与前端 interactive/textCard.ts 的 detectLanguage 同一套口径）
LANGUAGE_BY_EXT = {
    "py": "python", "pyi": "python", "ts": "typescript", "tsx": "tsx", "js": "javascript",
    "mjs": "javascript", "cjs": "javascript", "jsx": "jsx", "vue": "vue", "svelte": "svelte",
    "java": "java", "kt": "kotlin", "kts": "kotlin", "scala": "scala", "go": "go",
    "rs": "rust", "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "hpp": "cpp",
    "cs": "csharp", "rb": "ruby", "php": "php", "pl": "perl", "lua": "lua", "r": "r",
    "m": "objectivec", "swift": "swift", "sh": "bash", "bash": "bash", "zsh": "bash",
    "ps1": "powershell", "psm1": "powershell", "bat": "dos", "cmd": "dos", "sql": "sql",
    "json": "json", "jsonl": "json", "ndjson": "json", "json5": "json", "yaml": "yaml",
    "yml": "yaml", "toml": "ini", "ini": "ini", "cfg": "ini", "conf": "ini", "env": "ini",
    "properties": "ini", "md": "markdown", "markdown": "markdown", "txt": "text",
    "log": "text", "csv": "text", "tsv": "text", "css": "css", "scss": "scss",
    "less": "less", "html": "xml", "htm": "xml", "xhtml": "xml", "xml": "xml",
    "svg": "xml", "rst": "rst", "tex": "latex", "diff": "diff", "patch": "diff",
    "gradle": "groovy", "dockerfile": "dockerfile", "makefile": "makefile",
}

_TEXT_ENCODINGS = ("utf-8", "gbk", "cp1252")


# ---------------------------------------------------------------------------
# 异常：全部由 render_for 收敛成 status/reason，绝不冒到 HTTP 层变成 500
# ---------------------------------------------------------------------------


class PreviewError(Exception):
    """预览转换失败（带可读 reason 与它对应的 status）。"""

    def __init__(self, reason: str, *, status: str = "failed") -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.status = status


class PreviewFailed(PreviewError):
    """损坏 / 读不了 / 超预算：status="failed"。"""


class PreviewUnsupported(PreviewError):
    """这种文件不做内嵌预览：status="unsupported"。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, status="unsupported")


class PreviewCancelled(PreviewError):
    """协作取消：工作线程在单元边界看到取消标志就停下。"""

    def __init__(self, reason: str = "预览转换已取消（客户端离开或发起了新的请求）") -> None:
        super().__init__(reason)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _attr(node, name: str) -> str:
    """按 local name 取属性值。

    WordprocessingML 的属性是**带命名空间**的（w:val / w:numId / w:ilvl），
    直接 `node.attrib.get("val")` 永远拿不到值 —— 这里统一按 local name 取。
    """
    for key, value in node.attrib.items():
        if _local(key) == name:
            return str(value or "")
    return ""


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise PreviewCancelled()


def _suffix(name: str) -> str:
    base = str(name or "").replace("\\", "/").split("/")[-1].lower()
    dot = base.rfind(".")
    if dot <= 0:
        return ""
    return base[dot:]


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------


def classify_preview(name: str) -> str:
    """按文件名给出预览类别（契约 3.2 的八种取值之一）。"""
    base = str(name or "").replace("\\", "/").split("/")[-1].lower()
    suffix = _suffix(base)
    if not suffix:
        return "unsupported"
    if suffix in IMAGE_KINDS:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix in XLSX_EXTS:
        return "xlsx"
    if suffix == ".pptx":
        return "pptx"
    if suffix in LEGACY_OFFICE_EXTS:
        return "legacy_office"
    if suffix in CODE_EXTS or base in ("dockerfile", "makefile"):
        return "code"
    return "unsupported"


def _unsupported_reason(name: str) -> str:
    """未支持类型的可读原因（诚实说清"为什么"，不含糊）。"""
    base = str(name or "").replace("\\", "/").split("/")[-1]
    suffix = _suffix(base)
    if not suffix:
        return f"「{base or '这个文件'}」没有扩展名，认不出类型：不做内嵌预览（可以打开或下载）"
    if suffix in (".tif", ".tiff", ".ico"):
        return f"{suffix} 图片浏览器不能直接内嵌显示：不做内嵌预览（可以打开或下载）"
    if suffix in (".zip", ".rar", ".7z", ".gz", ".tar"):
        return "压缩包不解压（避免递归解压与体积风险）：不做内嵌预览（可以打开或下载）"
    if suffix in (".mp3", ".wav", ".mp4", ".mov", ".avi", ".mkv", ".webm", ".ogg", ".flac", ".m4a"):
        return f"{suffix} 是音视频：预览层不内嵌播放（可以打开或下载）"
    if suffix in (".exe", ".dll", ".so", ".dylib", ".msi", ".bin", ".db", ".sqlite", ".sqlite3", ".iso"):
        return f"{suffix} 是可执行文件或二进制数据：不读取、不预览"
    return f"{suffix} 目前没有内嵌预览（可以打开或下载）"


def _legacy_office_reason(name: str) -> str:
    suffix = _suffix(name)
    label = {".doc": "旧版 Word（.doc）", ".xls": "旧版 Excel（.xls）", ".ppt": "旧版 PowerPoint（.ppt）"}.get(
        suffix, f"旧版 Office（{suffix}）"
    )
    return (
        f"{label}是二进制格式，不能内嵌预览：需要用户用系统程序打开；"
        "可以在这里打开原文件或另存为新格式（.docx/.xlsx/.pptx）后再预览"
    )


# ---------------------------------------------------------------------------
# 单元计量（stats 与 truncated 的口径）
# ---------------------------------------------------------------------------


def _count_units(data: dict) -> int:
    kind = data.get("kind")
    if kind == "docx":
        tables = data.get("tables") or []
        return len(data.get("blocks") or []) + sum(len(t.get("rows") or []) for t in tables)
    if kind == "xlsx":
        return sum(len(sheet.get("rows") or []) for sheet in (data.get("sheets") or []))
    if kind == "pptx":
        slides = data.get("slides") or []
        return len(slides) + sum(len(s.get("texts") or []) for s in slides)
    if kind == "code":
        return len(data.get("lines") or [])
    return 1


# ---------------------------------------------------------------------------
# 代码 / 文本
# ---------------------------------------------------------------------------


def _decode_text(raw: bytes) -> tuple[str, str]:
    """按内容嗅探编码（BOM 优先）；解不出来时用替换字符，但如实说明。"""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace"), "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", "replace"), "utf-8"
    for encoding in _TEXT_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8（有无法解码的字节，已用替换字符显示）"


def language_for(name: str) -> str:
    base = str(name or "").replace("\\", "/").split("/")[-1].lower()
    if base in ("dockerfile", "makefile"):
        return base
    return LANGUAGE_BY_EXT.get(_suffix(base).lstrip("."), "")


def _convert_code(att: Attachment, path: Path, cancel: threading.Event | None) -> dict:
    size = _file_size(path)
    read_bytes = min(size, CODE_READ_BYTES, MAX_PREVIEW_BYTES)
    with open(path, "rb") as handle:
        raw = handle.read(read_bytes)
    _check_cancel(cancel)
    if b"\x00" in raw[: 64 * 1024]:
        raise PreviewFailed("这个文件的内容含二进制字节（NUL），不是文本：不做文本预览（可以下载）")
    text, encoding = _decode_text(raw)
    all_lines = text.splitlines()
    truncated = size > read_bytes
    lines: list[str] = []
    for line in all_lines[:MAX_PREVIEW_UNITS]:
        if len(line) > MAX_LINE_CHARS:
            lines.append(line[:MAX_LINE_CHARS])
            truncated = True
        else:
            lines.append(line)
    if len(all_lines) > MAX_PREVIEW_UNITS:
        truncated = True
    return {
        "kind": "code",
        "language": language_for(att.original_name),
        "lines": lines,
        "totalLines": len(all_lines),
        "encoding": encoding,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# 图片（只给事实：宽高与字节数；位图字节由 /content 走原有的认证链路）
# ---------------------------------------------------------------------------


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError as exc:
        raise PreviewFailed(f"读不到这个文件：{exc.strerror or exc}") from exc


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    """复用附件工具的头部解析（PNG/JPEG/GIF/BMP/WebP）；解不出就如实给 None。"""
    try:
        from agent.tools.attachment_tools import _image_dimensions as impl
    except Exception:  # pragma: no cover - 只可能出现在裁剪过的发行包
        return None
    try:
        return impl(path)
    except OSError:
        return None


def _convert_image(att: Attachment, path: Path) -> dict:
    size = _file_size(path)
    if size > MAX_PREVIEW_BYTES:
        raise PreviewFailed(
            f"图片 {human_size(size)} 超过预览上限 {human_size(MAX_PREVIEW_BYTES)}："
            "不做内嵌预览（避免为看一眼把整张位图读进内存；可以打开或下载）"
        )
    size_pair = _image_dimensions(path)
    width = height = None
    if size_pair:
        width, height = int(size_pair[0]), int(size_pair[1])
    return {"kind": "image", "width": width, "height": height, "bytes": size}


# ---------------------------------------------------------------------------
# PDF：只给事实（页数 / 标题 / 是否加密 / 可提取文本量）
# ---------------------------------------------------------------------------


def _pdf_reader(path: Path):
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 打包缺库时才会走到
        raise PreviewFailed(
            "服务端没有安装 PDF 解析库（pypdf）：给不出页数等事实（可以打开或下载副本）"
        ) from exc
    try:
        return PdfReader(str(path))
    except Exception as exc:
        raise PreviewFailed(
            f"这个 PDF 打不开（可能已损坏或不是有效的 PDF）：{type(exc).__name__}"
        ) from exc


def _convert_pdf(path: Path, cancel: threading.Event | None) -> dict:
    reader = _pdf_reader(path)
    title = ""
    try:
        meta = reader.metadata
        raw_title = getattr(meta, "title", None) if meta is not None else None
        title = str(raw_title).strip() if raw_title else ""
    except Exception:  # noqa: BLE001 - 元数据坏了不影响页数事实
        title = ""
    encrypted = bool(getattr(reader, "is_encrypted", False))
    if encrypted:
        decrypted = False
        try:
            decrypted = bool(reader.decrypt(""))
        except Exception:  # noqa: BLE001 - 空密码解不开就按加密处理
            decrypted = False
        if not decrypted:
            return {
                "kind": "pdf",
                "pages": None,
                "title": title,
                "encrypted": True,
                "textChars": 0,
                "reason": "这个 PDF 有加密（需要密码）：不能内联预览；可以打开或下载后用本地程序输入密码查看",
            }
    try:
        pages = len(reader.pages)
    except Exception as exc:
        raise PreviewFailed(
            f"这个 PDF 的页面结构读不出来（可能已损坏）：{type(exc).__name__}"
        ) from exc
    text_chars = 0
    probed = 0
    for index, page in enumerate(reader.pages):
        if index >= MAX_PDF_TEXT_PAGES:
            break
        _check_cancel(cancel)
        probed += 1
        try:
            text_chars += len(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - 单页取不到文本不算整份失败
            continue
    return {
        "kind": "pdf",
        "pages": int(pages),
        "title": title,
        "encrypted": False,
        "textChars": int(text_chars),
        "textProbedPages": int(probed),
    }


# ---------------------------------------------------------------------------
# ZIP 预算：直接复用 tools/attachment_tools.py 的实现（惰性 import，避免 import 期拉起 tools 包）
# ---------------------------------------------------------------------------

_ZIP_IMPL: dict[str, Any] | None = None


def _zip_impl() -> dict[str, Any]:
    global _ZIP_IMPL
    if _ZIP_IMPL is None:
        from agent.tools import attachment_tools as impl

        _ZIP_IMPL = {
            "budget": impl._ZipBudget,
            "reader": impl._CountingReader,
            "cancelled": impl._JobCancelled,
            "limit": impl.AttachmentLimitExceeded,
            "parse_member": impl._parse_member_xml,
        }
    return _ZIP_IMPL


def _open_zip(path: Path):
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise PreviewFailed(
            "这个文件不是有效的 Office/OOXML 压缩包（zip 结构损坏，或文件被加密）"
        ) from exc
    return archive


# ---------------------------------------------------------------------------
# DOCX：段落 / 标题 / 列表 / 表格（runs 带粗体斜体）
# ---------------------------------------------------------------------------


def _docx_numbering(archive, names, budget, reader_cls, cancel) -> dict[str, bool]:
    """numId → 是否有序（从 word/numbering.xml 的 numFmt 推）：推不出就不写（前端标"编号未知"）。"""
    if "word/numbering.xml" not in names:
        return {}
    parse_member = _zip_impl()["parse_member"]
    try:
        root = parse_member(archive, "word/numbering.xml", budget, cancel)
    except Exception:  # noqa: BLE001 - numbering 坏了不该让整份预览失败
        return {}
    num_to_abstract: dict[str, str] = {}
    abstract_fmt: dict[str, str] = {}
    for node in root.iter():
        name = _local(node.tag)
        if name == "num":
            num_id = _attr(node, "numId")
            for child in node:
                if _local(child.tag) == "abstractNumId":
                    num_to_abstract[num_id] = _attr(child, "val")
        elif name == "abstractNum":
            abstract_id = _attr(node, "abstractNumId")
            for child in node:
                if _local(child.tag) != "lvl":
                    continue
                if (_attr(child, "ilvl") or "0") != "0":
                    continue
                for prop in child:
                    if _local(prop.tag) == "numFmt":
                        abstract_fmt[abstract_id] = _attr(prop, "val")
    result: dict[str, bool] = {}
    for num_id, abstract_id in num_to_abstract.items():
        fmt = abstract_fmt.get(abstract_id, "")
        if fmt == "bullet":
            result[num_id] = False
        elif fmt:
            result[num_id] = True
    return result


def _docx_run_text(run) -> str:
    parts: list[str] = []
    for node in run.iter():
        name = _local(node.tag)
        if name == "t":
            parts.append(node.text or "")
        elif name == "tab":
            parts.append("\t")
        elif name in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _docx_flag(parent, tag_name: str) -> bool:
    for node in parent.iter():
        if _local(node.tag) != tag_name:
            continue
        value = _attr(node, "val").strip().lower()
        return value not in ("0", "false", "off", "none")
    return False


def _docx_heading_level(paragraph) -> int | None:
    style = ""
    outline: str | None = None
    for node in paragraph.iter():
        name = _local(node.tag)
        if name == "pStyle":
            style = _attr(node, "val").strip()
        elif name == "outlineLvl":
            outline = _attr(node, "val").strip()
    match = None
    import re as _re

    found = _re.match(r"^(?:heading|标题)\s*(\d+)", style, _re.IGNORECASE)
    if found:
        match = int(found.group(1))
    elif outline is not None and outline.isdigit():
        match = int(outline) + 1
    if match is None:
        return None
    return max(1, min(3, match))


def _docx_paragraph(paragraph, numbering: dict[str, bool]) -> dict | None:
    level = _docx_heading_level(paragraph)
    num_id: str | None = None
    for node in paragraph.iter():
        if _local(node.tag) == "numId":
            num_id = _attr(node, "val").strip() or None
            break
    runs: list[dict] = []
    for child in paragraph:
        if _local(child.tag) != "r":
            continue
        text = _docx_run_text(child)
        if not text:
            continue
        runs.append(
            {
                "text": text,
                "bold": _docx_flag(child, "b"),
                "italic": _docx_flag(child, "i"),
            }
        )
    plain = "".join(run["text"] for run in runs)
    if level is not None:
        block_type = "heading"
    elif num_id is not None:
        block_type = "list"
    else:
        block_type = "paragraph"
    if not plain.strip() and block_type != "list":
        return None
    ordered = None
    if block_type == "list":
        ordered = numbering.get(num_id) if num_id else None
    return {
        "type": block_type,
        "level": level,
        "ordered": ordered,
        "runs": runs,
    }


def _docx_table_rows(table) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table:
        if _local(row.tag) != "tr":
            continue
        cells: list[str] = []
        for cell in row:
            if _local(cell.tag) != "tc":
                continue
            if len(cells) >= MAX_COLS_PER_SHEET:
                break
            parts = [
                "".join(run["text"] for run in (_docx_runs(p) or []))
                for p in cell.iter()
                if _local(p.tag) == "p"
            ]
            cells.append("\n".join(part for part in parts if part).strip())
        if any(cells):
            rows.append(cells)
        if len(rows) >= MAX_ROWS_PER_SHEET:
            break
    return rows


def _docx_runs(paragraph) -> list[dict]:
    runs: list[dict] = []
    for child in paragraph:
        if _local(child.tag) != "r":
            continue
        text = _docx_run_text(child)
        if text:
            runs.append({"text": text, "bold": _docx_flag(child, "b"), "italic": _docx_flag(child, "i")})
    return runs


def _convert_docx(path: Path, cancel: threading.Event | None) -> dict:
    impl = _zip_impl()
    budget = impl["budget"]()
    reader_cls = impl["reader"]
    try:
        return _docx_data(path, cancel, budget, reader_cls)
    except impl["limit"] as exc:
        raise PreviewFailed(f"预览解析超出预算：{exc}") from exc
    except impl["cancelled"] as exc:
        raise PreviewCancelled() from exc


def _docx_data(path, cancel, budget, reader_cls) -> dict:
    blocks: list[dict] = []
    tables: list[dict] = []
    truncated = False
    units = 0
    with _open_zip(path) as archive:
        names = archive.namelist()
        budget.add_members(len(names))
        if "word/document.xml" not in names:
            raise PreviewFailed("这个 .docx 里没有 word/document.xml：不是有效的 Word 文档")
        numbering = _docx_numbering(archive, names, budget, reader_cls, cancel)
        with archive.open("word/document.xml") as raw:
            reader = reader_cls(raw, budget, member="word/document.xml", cancel=cancel)
            in_table = 0
            for event, elem in ElementTree.iterparse(reader, events=("start", "end")):
                _check_cancel(cancel)
                name = _local(elem.tag)
                if event == "start":
                    if name == "tbl":
                        in_table += 1
                    continue
                if name == "tbl":
                    in_table = max(0, in_table - 1)
                    if len(tables) < MAX_TABLES:
                        rows = _docx_table_rows(elem)
                        if rows:
                            tables.append({"rows": rows})
                            units += len(rows)
                            if len(rows) >= MAX_ROWS_PER_SHEET:
                                truncated = True
                    else:
                        truncated = True
                    elem.clear()
                elif name == "p" and in_table == 0:
                    block = _docx_paragraph(elem, numbering)
                    if block is not None:
                        if units >= MAX_PREVIEW_UNITS:
                            truncated = True
                        else:
                            blocks.append(block)
                            units += 1
                    elem.clear()
                if units >= MAX_PREVIEW_UNITS:
                    truncated = True
    return {"kind": "docx", "blocks": blocks, "tables": tables, "truncated": truncated}


# ---------------------------------------------------------------------------
# XLSX：工作表 + 行列 + 单元格地址 + 公式 / 缓存值（不计算公式）
# ---------------------------------------------------------------------------


def _xlsx_sheet_order(archive, names, budget, cancel) -> list[tuple[str, str]]:
    """[(工作表名, sheet xml 路径)]，按 workbook.xml 的顺序；读不出就按文件名排序兜底。"""
    parse_member = _zip_impl()["parse_member"]
    ordered: list[tuple[str, str]] = []
    if "xl/workbook.xml" in names:
        try:
            root = parse_member(archive, "xl/workbook.xml", budget, cancel)
            rel_root = None
            if "xl/_rels/workbook.xml.rels" in names:
                rel_root = parse_member(archive, "xl/_rels/workbook.xml.rels", budget, cancel)
            rel_targets: dict[str, str] = {}
            if rel_root is not None:
                for rel in rel_root:
                    rel_id = rel.attrib.get("Id") or ""
                    target = (rel.attrib.get("Target") or "").lstrip("/")
                    if not rel_id or not target:
                        continue
                    rel_targets[rel_id] = target if target.startswith("xl/") else f"xl/{target}"
            order = 0
            for sheet in root.iter():
                if _local(sheet.tag) != "sheet":
                    continue
                order += 1
                rel_id = ""
                for key, value in sheet.attrib.items():
                    if _local(key) == "id":
                        rel_id = value
                name = sheet.attrib.get("name") or f"工作表{order}"
                target = rel_targets.get(rel_id) or f"xl/worksheets/sheet{order}.xml"
                ordered.append((name, target))
        except PreviewError:
            raise
        except Exception:  # noqa: BLE001 - workbook 读不出来就用兜底顺序
            ordered = []
    if ordered:
        return ordered
    return [
        (f"工作表{index + 1}", name)
        for index, name in enumerate(
            sorted(
                n
                for n in names
                if n.startswith("xl/worksheets/") and n.endswith(".xml")
            )
        )
    ]


def _xlsx_shared_strings(archive, names, budget, reader_cls, cancel) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    values: list[str] = []
    with archive.open("xl/sharedStrings.xml") as raw:
        reader = reader_cls(raw, budget, member="xl/sharedStrings.xml", cancel=cancel)
        for _event, elem in ElementTree.iterparse(reader, events=("end",)):
            if _local(elem.tag) != "si":
                continue
            values.append("".join(node.text or "" for node in elem.iter() if _local(node.tag) == "t"))
            elem.clear()
    return values


def _xlsx_cell(cell, shared: list[str]) -> dict | None:
    ref = cell.attrib.get("r") or ""
    kind = cell.attrib.get("t") or ""
    formula: str | None = None
    raw_value: str | None = None
    inline: str | None = None
    for node in cell:
        name = _local(node.tag)
        if name == "f":
            formula = node.text or ""
        elif name == "v":
            raw_value = node.text or ""
        elif name == "is":
            inline = "".join(child.text or "" for child in node.iter() if _local(child.tag) == "t")
    resolved: str | None
    if kind == "inlineStr":
        resolved = inline
    elif kind == "s":
        resolved = raw_value
        if raw_value is not None and raw_value.strip():
            try:
                resolved = shared[int(raw_value)]
            except (ValueError, IndexError):
                resolved = raw_value
    elif kind == "b":
        resolved = "TRUE" if (raw_value or "").strip() == "1" else "FALSE"
    else:
        resolved = raw_value
    resolved = resolved.strip() if isinstance(resolved, str) else resolved
    cached = resolved if formula is not None else None
    value = None if formula is not None else resolved
    if not ref and not value and formula is None:
        return None
    if value is None and formula is None and cached is None:
        return None
    return {
        "ref": ref,
        "value": value if value not in ("", None) else ("" if value == "" else None),
        "formula": formula,
        "cached": cached if cached not in ("", None) else ("" if cached == "" else None),
    }


def _convert_xlsx(path: Path, cancel: threading.Event | None) -> dict:
    impl = _zip_impl()
    budget = impl["budget"]()
    reader_cls = impl["reader"]
    try:
        return _xlsx_data(path, cancel, budget, reader_cls)
    except impl["limit"] as exc:
        raise PreviewFailed(f"预览解析超出预算：{exc}") from exc
    except impl["cancelled"] as exc:
        raise PreviewCancelled() from exc


def _xlsx_data(path, cancel, budget, reader_cls) -> dict:
    sheets: list[dict] = []
    truncated = False
    units = 0
    with _open_zip(path) as archive:
        names = archive.namelist()
        budget.add_members(len(names))
        if not any(n.startswith("xl/worksheets/") for n in names):
            raise PreviewFailed("这个表格里没有工作表（xl/worksheets 缺失）：不是有效的 .xlsx/.xlsm")
        ordered = _xlsx_sheet_order(archive, names, budget, cancel)
        shared = _xlsx_shared_strings(archive, names, budget, reader_cls, cancel)
        for sheet_name, target in ordered:
            if target not in names:
                continue
            rows: list[dict] = []
            with archive.open(target) as raw:
                reader = reader_cls(raw, budget, member=target, cancel=cancel)
                for _event, elem in ElementTree.iterparse(reader, events=("end",)):
                    _check_cancel(cancel)
                    if _local(elem.tag) != "row":
                        continue
                    try:
                        index = int(elem.attrib.get("r") or (len(rows) + 1))
                    except ValueError:
                        index = len(rows) + 1
                    cells: list[dict] = []
                    for cell in elem:
                        if _local(cell.tag) != "c":
                            continue
                        if len(cells) >= MAX_COLS_PER_SHEET:
                            truncated = True
                            break
                        parsed = _xlsx_cell(cell, shared)
                        if parsed is not None:
                            cells.append(parsed)
                    if len(rows) >= MAX_ROWS_PER_SHEET or units >= MAX_PREVIEW_UNITS:
                        truncated = True
                        elem.clear()
                        continue
                    if cells:
                        rows.append({"index": index, "cells": cells})
                        units += 1
                    elem.clear()
            sheets.append({"name": sheet_name, "rows": rows})
            if len(rows) >= MAX_ROWS_PER_SHEET:
                truncated = True
            if units >= MAX_PREVIEW_UNITS:
                truncated = True
    return {"kind": "xlsx", "sheets": sheets, "truncated": truncated}


# ---------------------------------------------------------------------------
# PPTX：幻灯片 + 文本（含备注）+ 图片清单 + 表格
# ---------------------------------------------------------------------------

_NOTES_PREFIX = "备注："


def _pptx_slide_order(archive, names, budget, cancel) -> list[tuple[int, str]]:
    parse_member = _zip_impl()["parse_member"]
    ordered: list[tuple[int, str]] = []
    if "ppt/presentation.xml" in names:
        try:
            root = parse_member(archive, "ppt/presentation.xml", budget, cancel)
            rel_root = None
            if "ppt/_rels/presentation.xml.rels" in names:
                rel_root = parse_member(archive, "ppt/_rels/presentation.xml.rels", budget, cancel)
            rel_targets: dict[str, str] = {}
            if rel_root is not None:
                for rel in rel_root:
                    rel_id = rel.attrib.get("Id") or ""
                    target = (rel.attrib.get("Target") or "").lstrip("/")
                    if rel_id and target:
                        rel_targets[rel_id] = target if target.startswith("ppt/") else f"ppt/{target}"
            index = 0
            for node in root.iter():
                if _local(node.tag) != "sldId":
                    continue
                index += 1
                rel_id = ""
                for key, value in node.attrib.items():
                    if _local(key) == "id":
                        rel_id = value
                target = rel_targets.get(rel_id)
                if target:
                    ordered.append((index, target))
        except Exception:  # noqa: BLE001 - 读不出顺序就用文件名兜底
            ordered = []
    if ordered:
        return ordered
    slides = sorted(n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
    return [(i + 1, name) for i, name in enumerate(slides)]


def _pptx_rels(archive, slide_path: str, budget, cancel) -> dict[str, str]:
    """slide 的 rels：rId → 目标路径（图片 / 备注页）。"""
    parse_member = _zip_impl()["parse_member"]
    folder, _, filename = slide_path.rpartition("/")
    rels_path = f"{folder}/_rels/{filename}.rels"
    if rels_path not in archive.namelist():
        return {}
    base = "ppt/"
    result: dict[str, str] = {}
    try:
        root = parse_member(archive, rels_path, budget, cancel)
    except Exception:  # noqa: BLE001 - rels 坏了就当作没有图片/备注
        return {}
    for rel in root:
        rel_id = rel.attrib.get("Id") or ""
        target = (rel.attrib.get("Target") or "").lstrip("/")
        if not rel_id or not target:
            continue
        if target.startswith("../"):
            target = target[3:]
        result[rel_id] = target if target.startswith("ppt/") else f"{base}{target}"
    return result


def _pptx_shape_texts(shape) -> list[str]:
    texts: list[str] = []
    for paragraph in shape.iter():
        if _local(paragraph.tag) != "p":
            continue
        line = "".join(node.text or "" for node in paragraph.iter() if _local(node.tag) == "t")
        if line.strip():
            texts.append(line.strip())
    return texts


def _pptx_placeholder_type(shape) -> str:
    for node in shape.iter():
        if _local(node.tag) != "ph":
            continue
        return (node.attrib.get("type") or "").strip().lower()
    return ""


def _pptx_tables(shape) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for node in shape.iter():
        if _local(node.tag) != "tbl":
            continue
        rows: list[list[str]] = []
        for row in node:
            if _local(row.tag) != "tr":
                continue
            cells: list[str] = []
            for cell in row:
                if _local(cell.tag) != "tc":
                    continue
                if len(cells) >= MAX_COLS_PER_SHEET:
                    break
                cells.append(" ".join(_pptx_shape_texts(cell)).strip())
            rows.append(cells)
            if len(rows) >= MAX_ROWS_PER_SHEET:
                break
        if rows:
            tables.append(rows)
    return tables


def _convert_pptx(path: Path, cancel: threading.Event | None) -> dict:
    impl = _zip_impl()
    budget = impl["budget"]()
    reader_cls = impl["reader"]
    try:
        return _pptx_data(path, cancel, budget, reader_cls)
    except impl["limit"] as exc:
        raise PreviewFailed(f"预览解析超出预算：{exc}") from exc
    except impl["cancelled"] as exc:
        raise PreviewCancelled() from exc


def _pptx_data(path, cancel, budget, reader_cls) -> dict:
    parse_member = _zip_impl()["parse_member"]
    slides: list[dict] = []
    truncated = False
    units = 0
    with _open_zip(path) as archive:
        names = set(archive.namelist())
        budget.add_members(len(names))
        if not any(n.startswith("ppt/slides/slide") for n in names):
            raise PreviewFailed("这个演示文稿里没有幻灯片（ppt/slides 缺失）：不是有效的 .pptx")
        for index, slide_path in _pptx_slide_order(archive, names, budget, cancel):
            if units >= MAX_PREVIEW_UNITS:
                truncated = True
                break
            if slide_path not in names:
                continue
            _check_cancel(cancel)
            try:
                root = parse_member(archive, slide_path, budget, cancel)
            except PreviewError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单页坏了不该让整份预览失败
                slides.append(
                    {
                        "index": index,
                        "title": "",
                        "texts": [f"（这一页读不出来：{type(exc).__name__}）"],
                        "images": [],
                        "tables": [],
                    }
                )
                units += 1
                continue
            rels = _pptx_rels(archive, slide_path, budget, cancel)
            title = ""
            texts: list[str] = []
            tables: list[list[list[str]]] = []
            for shape in root.iter():
                if _local(shape.tag) != "sp" and _local(shape.tag) != "graphicFrame":
                    continue
                shape_tables = _pptx_tables(shape)
                has_table = any(_local(node.tag) == "tbl" for node in shape.iter())
                if has_table:
                    # 表格文字归 tables：同一段文字不在 texts 里重复出现一遍
                    tables.extend(shape_tables)
                    continue
                lines = _pptx_shape_texts(shape)
                placeholder = _pptx_placeholder_type(shape)
                if not title and placeholder in ("title", "ctrTitle".lower()) and lines:
                    title = lines[0]
                    lines = lines[1:]
                texts.extend(lines)
            images: list[dict] = []
            notes_path = ""
            for target in rels.values():
                if "/media/" in target:
                    info = None
                    try:
                        info = archive.getinfo(target)
                    except KeyError:
                        info = None
                    images.append(
                        {
                            "name": target.rsplit("/", 1)[-1],
                            "bytes": int(info.file_size) if info is not None else 0,
                        }
                    )
                elif "notesSlide" in target:
                    notes_path = target
            if notes_path and notes_path in names:
                try:
                    notes_root = parse_member(archive, notes_path, budget, cancel)
                    for line in _pptx_shape_texts(notes_root):
                        texts.append(_NOTES_PREFIX + line)
                except Exception:  # noqa: BLE001 - 备注读不出来不影响正文
                    pass
            texts = [line for line in texts if line.strip()]
            slides.append(
                {
                    "index": int(index),
                    "title": title,
                    "texts": texts[:MAX_PREVIEW_UNITS],
                    "images": images,
                    "tables": tables,
                }
            )
            units += 1 + len(texts)
            if len(texts) > MAX_PREVIEW_UNITS:
                truncated = True
    if len(slides) > MAX_PREVIEW_UNITS:
        truncated = True
    return {"kind": "pptx", "slides": slides, "truncated": truncated}


# ---------------------------------------------------------------------------
# convert：按分类分派（纯计算；异常向上抛，由 render_for 收敛成 status/reason）
# ---------------------------------------------------------------------------


def convert(att: Attachment, path: Path, *, cancel: threading.Event | None = None) -> dict:
    """把文件转成预览数据（契约 3.2 冻结接口）。

    返回的是**该 kind 的 data 形状本身**（含 "kind" 字段），与契约 3.2 的 jsonc 逐字对应：
    data 里出现 "truncated" 的类别（docx/xlsx/pptx/code）自己带截断事实；
    image/pdf 的数据形状里没有 truncated（它们本来就没有"截断"这回事）。

    不写库、不发事件；失败抛 PreviewFailed / PreviewUnsupported / PreviewCancelled。
    """
    kind = classify_preview(att.original_name)
    if kind == "legacy_office":
        raise PreviewUnsupported(_legacy_office_reason(att.original_name))
    if kind == "unsupported":
        raise PreviewUnsupported(_unsupported_reason(att.original_name))
    if kind == "image":
        return _convert_image(att, path)
    if kind == "code":
        return _convert_code(att, path, cancel)
    size = _file_size(path)
    if size > MAX_PREVIEW_BYTES:
        raise PreviewFailed(
            f"这个文件 {human_size(size)} 超过预览转换上限 {human_size(MAX_PREVIEW_BYTES)}："
            "不做转换（可以打开或下载后用本地程序查看）"
        )
    if kind == "pdf":
        return _convert_pdf(path, cancel)
    if kind == "docx":
        return _convert_docx(path, cancel)
    if kind == "xlsx":
        return _convert_xlsx(path, cancel)
    if kind == "pptx":
        return _convert_pptx(path, cancel)
    raise PreviewUnsupported(_unsupported_reason(att.original_name))


# ---------------------------------------------------------------------------
# 缓存：attachment_previews（迁移 41）
# ---------------------------------------------------------------------------


def preview_cache_key(attachment_id: str, version: str, kind: str) -> str:
    """缓存身份：配方版本进键（换配方 = 全部重算），与表主键 (attachment_id, version, recipe) 同构。"""
    return f"r{RECIPE_VERSION}:{attachment_id}:{version}:{kind}"


def attachment_version(att: Attachment) -> str:
    """附件版本（与 interactive/materials.py 的 _attachment_version 同一口径）。

    两份实现必须一致：预览位置按 attachmentId+version 归属，而提交/审批按材料版本对账；
    两处算出不同版本会让"同一个附件"在两个功能里被当成两个版本。测试锁死这条等值。
    """
    sha = getattr(att, "sha256", None)
    if sha:
        return f"sha256:{sha}"
    mtime = "" if att.mtime is None else f"{float(att.mtime):.3f}"
    return f"att:{att.id}:{int(att.size_bytes or 0)}:{mtime}:{att.updated_at or ''}"


def _cached_envelope(conn, attachment_id: str, version: str, kind: str) -> dict | None:
    try:
        row = conn.execute(
            "SELECT kind, status, reason, truncated, payload, bytes FROM attachment_previews"
            " WHERE attachment_id = ? AND version = ? AND recipe = ?",
            (str(attachment_id), str(version), int(RECIPE_VERSION)),
        ).fetchone()
    except sqlite3.Error:  # 迁移还没跑到的库：当没缓存，别把预览功能弄挂
        return None
    if row is None:
        return None
    if (row["kind"] or "") != str(kind):
        return None  # 同名同版本但类别不同（比如文件被替换成另一类）：不用旧类别的结果
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None and (row["status"] or "") == "ready":
        return None
    stats = payload.get("stats") if isinstance(payload, dict) else {}
    stats = dict(stats) if isinstance(stats, dict) else {}
    stats["cached"] = True
    return {
        "status": str(row["status"] or "ready"),
        "kind": str(row["kind"] or kind),
        "version": str(version),
        "attachmentId": str(attachment_id),
        "recipe": int(RECIPE_VERSION),
        "data": data,
        "reason": str(row["reason"] or ""),
        "truncated": bool(row["truncated"]),
        "stats": stats,
    }


def _store_envelope(conn, envelope: dict) -> None:
    """写一条缓存（只写 ready / unsupported：失败可能是暂时的，不该被缓存钉死）。"""
    status = str(envelope.get("status") or "")
    if status not in ("ready", "unsupported"):
        return
    payload = json.dumps(
        {"data": envelope.get("data"), "stats": envelope.get("stats") or {}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        with transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO attachment_previews"
                " (attachment_id, version, kind, recipe, status, reason, truncated, payload, bytes, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(envelope.get("attachmentId") or ""),
                    str(envelope.get("version") or ""),
                    str(envelope.get("kind") or ""),
                    int(RECIPE_VERSION),
                    status,
                    str(envelope.get("reason") or ""),
                    1 if envelope.get("truncated") else 0,
                    payload,
                    len(payload.encode("utf-8")),
                    _now(),
                ),
            )
    except sqlite3.Error as exc:  # 写缓存失败不该让用户看不到预览
        logger.warning("预览缓存写入失败（不影响本次预览）：%s", exc)


# ---------------------------------------------------------------------------
# render_for：带缓存与版本校验的入口
# ---------------------------------------------------------------------------


def _envelope(
    *,
    attachment_id: str,
    version: str,
    kind: str,
    status: str,
    data: dict | None,
    reason: str = "",
    truncated: bool = False,
    stats: dict | None = None,
) -> dict:
    return {
        "status": status,
        "kind": kind,
        "version": version,
        "attachmentId": attachment_id,
        "recipe": int(RECIPE_VERSION),
        "data": data,
        "reason": reason,
        "truncated": bool(truncated),
        "stats": stats or {},
    }


def _status_for(data: dict) -> str:
    if not isinstance(data, dict):
        return "failed"
    if data.get("reason"):
        return "unsupported"
    return "ready"


def render_for(
    store,
    attachment_id: str,
    *,
    refresh: bool = False,
    cancel: threading.Event | None = None,
) -> dict:
    """带缓存与版本校验的预览入口（契约 3.2 冻结签名 + 两个可选关键字参数）。

    * store：AttachmentService（要有 get / conn / content_target），缓存读写落在**调用线程**；
    * refresh=True：绕过缓存重算（HTTP 的 ?refresh=1）；
    * cancel：协作取消标志（工作线程在单元边界检查）。

    **异常不出这个函数**：任何失败都收敛成 {"status":"failed"|"unsupported", "reason": ...}，
    路由层据此给 200 + 真实原因，绝不 500。
    """
    started = time.monotonic()
    att = store.get(attachment_id)
    if att is None:
        return _envelope(
            attachment_id=str(attachment_id),
            version="",
            kind="",
            status="failed",
            data=None,
            reason="没有这个附件（可能已被移除）",
            stats={"bytes": 0, "units": 0, "cached": False, "millis": 0},
        )
    version = attachment_version(att)
    kind = classify_preview(att.original_name)
    if not refresh:
        cached = _cached_envelope(store.conn, attachment_id, version, kind)
        if cached is not None:
            return cached
    data: dict | None = None
    truncated = False
    reason = ""
    status = "ready"
    size_bytes = int(att.size_bytes or 0)
    try:
        if kind in ("legacy_office", "unsupported"):
            raise PreviewUnsupported(
                _legacy_office_reason(att.original_name)
                if kind == "legacy_office"
                else _unsupported_reason(att.original_name)
            )
        else:
            path, _name = store.content_target(attachment_id)
            data = convert(att, path, cancel=cancel)
            status = _status_for(data)
            reason = str(data.get("reason") or "")
            truncated = bool(data.get("truncated"))
            size_bytes = _file_size(path)
    except AttachmentContentError as exc:
        # 引用型 / 还没就绪 / 副本丢了：content_target 的原话已经是人话，直接用它
        status = "failed"
        reason = str(exc)
        data = None
    except PreviewCancelled as exc:
        return _envelope(
            attachment_id=attachment_id,
            version=version,
            kind=kind,
            status="failed",
            data=None,
            reason=str(exc),
            stats={"bytes": size_bytes, "units": 0, "cached": False, "millis": int((time.monotonic() - started) * 1000)},
        )
    except PreviewError as exc:
        status = exc.status
        reason = exc.reason
        # legacy_office / unsupported 的数据形状就是 {kind, reason}（契约 §3.2）
        data = {"kind": kind, "reason": exc.reason} if exc.status == "unsupported" else None
    except Exception as exc:  # noqa: BLE001 - 任何异常都收敛，绝不 500
        logger.warning("预览转换失败（%s）：%s", attachment_id, exc)
        status = "failed"
        reason = f"预览转换失败（{type(exc).__name__}）：{exc}"
        data = None
    units = _count_units(data) if isinstance(data, dict) else 0
    stats = {
        "bytes": int(size_bytes),
        "units": int(units),
        "cached": False,
        "millis": int((time.monotonic() - started) * 1000),
    }
    envelope = _envelope(
        attachment_id=attachment_id,
        version=version,
        kind=kind,
        status=status,
        data=data,
        reason=reason,
        truncated=truncated,
        stats=stats,
    )
    # 迟到结果不写缓存：转换期间版本变了（文件被改 / 重新关联）就整份丢弃。
    if cancel is not None and cancel.is_set():
        envelope["reason"] = envelope["reason"] or "预览转换已取消（结果没有写入缓存）"
        return envelope
    if status in ("ready", "unsupported"):
        current = store.get(attachment_id, check=False)
        if current is not None and attachment_version(current) != version:
            envelope["reason"] = (
                envelope["reason"] or "附件在转换期间发生了变化：这次结果没有写入缓存"
            )
            return envelope
        _store_envelope(store.conn, envelope)
    return envelope


async def render_for_async(store, attachment_id: str, *, refresh: bool = False) -> dict:
    """异步入口：解析跑在工作线程，缓存读写留在事件循环线程（共享 sqlite 连接只有一条线程）。

    异步路径额外做两件同步版本做不到的事：
    * 同一附件的并发预览只保留**最新**那一代（旧代被取消标志中止，迟到结果不写缓存）；
    * 客户端断开时调用 cancel_preview() 让工作线程在单元边界停下。
    """
    import asyncio

    att = store.get(attachment_id)
    if att is None:
        return render_for(store, attachment_id, refresh=True)
    generation, cancel = _begin_preview(attachment_id)
    try:
        result = await asyncio.to_thread(
            render_for, store, attachment_id, refresh=refresh, cancel=cancel
        )
    except BaseException:
        # 请求被取消（客户端断开等手段）：让工作线程在下一个单元边界停下，
        # 它跑完也不会写缓存（render_for 里那道"取消就不写"的检查）。
        cancel.set()
        raise
    finally:
        _end_preview(attachment_id, generation)
    return result


_InFlight = dict[str, tuple[int, threading.Event]]
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: _InFlight = {}
_INFLIGHT_GEN: dict[str, int] = {}


def _begin_preview(attachment_id: str) -> tuple[int, threading.Event]:
    """开始一代预览：同一附件上的上一代被取消（它还在跑就尽早停）。"""
    key = str(attachment_id)
    with _INFLIGHT_LOCK:
        previous = _INFLIGHT.get(key)
        if previous is not None:
            previous[1].set()
        generation = _INFLIGHT_GEN.get(key, 0) + 1
        _INFLIGHT_GEN[key] = generation
        cancel = threading.Event()
        _INFLIGHT[key] = (generation, cancel)
        return generation, cancel


def _end_preview(attachment_id: str, generation: int) -> None:
    key = str(attachment_id)
    with _INFLIGHT_LOCK:
        current = _INFLIGHT.get(key)
        if current is not None and current[0] == generation:
            _INFLIGHT.pop(key, None)


def cancel_preview(attachment_id: str) -> None:
    """让这个附件在途的预览转换停下（收起卡片 / 换附件 / 卸载时调用）。"""
    with _INFLIGHT_LOCK:
        current = _INFLIGHT.get(str(attachment_id))
        if current is not None:
            current[1].set()
