"""媒体派生处理（契约 §4.3 / §4.4）：ffmpeg 探测、元数据、视频抽样帧、音频抽取与清理。

这一层的唯一职责是**把本机的真实能力变成可读的事实**：

* `ffmpeg_path()` 只说这台机器上到底有没有可用的转换程序（返回 `(路径, 来源说明)`，
  找不到时返回 `(None, 原因)`）；**不下载、不安装、不联网、不静默换成别的外部服务**；
* 找不到 ffmpeg 时，依赖它的能力（视频抽样 / 音频抽取）一律如实降级，原因里点明
  「这台机器上没有可用的转换程序，也没有随包提供」——不笼统说「不支持」；
* 边界写进 `reason` 而不是悄悄截断：单文件 `MAX_MEDIA_BYTES`、
  抽样帧数 `MAX_SAMPLE_FRAMES`、转写时长 `MAX_TRANSCRIBE_SECONDS`；
* 抽样帧**带真实时间位置**（`at`）：只代表取到的那几张，不是逐帧读完；
* 样本帧与音频片段**只写调用方给的本轮工作目录**，`cleanup_media` 在正常与失败
  路径上都能清干净（清不掉也不抛：调用方的收尾不该被清理失败打断）。

探测顺序（冻结）：`QIO_FFMPEG` 环境变量 → 应用资源目录（随包提供）→ PATH → 常见安装位置。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from agent.trace.redact import redact_text

#: 覆盖转换程序位置的环境变量（第一条，也是唯一允许用户显式指定的那条）
FFMPEG_ENV = "QIO_FFMPEG"

#: 单份媒体文件参与派生处理的字节上限（契约 §4.3）
MAX_MEDIA_BYTES = 512 * 1024 * 1024
#: 一次视频抽样最多取几帧（契约 §4.3）
MAX_SAMPLE_FRAMES = 12
#: 音频转写通道允许的最长秒数（契约 §4.3）
MAX_TRANSCRIBE_SECONDS = 300.0
#: 抽样帧默认输出边缘上限（长边像素）
DEFAULT_MAX_EDGE = 768
#: 转写用的音频规格：单声道 16 kHz wav
TRANSCRIBE_CHANNELS = 1
TRANSCRIBE_RATE = 16000

#: 找不到转换程序时的**主原因**（必须具体到「这台机器上没有」，而不是笼统的「不支持」）
NO_FFMPEG_REASON = (
    "这台机器上没有可用的转换程序，也没有随包提供："
    f"已按顺序查过 {FFMPEG_ENV} 环境变量、应用资源目录、PATH 与常见安装位置，"
    "都没有可用的 ffmpeg"
)

#: 子进程里被截断的 stderr 长度（诊断够用，不把整段噪声塞进 reason）
_STDERR_CHARS = 400


@dataclass
class FrameSample:
    """一次视频抽样的真实结果（不是「计划取几帧」）。"""

    frames: list[dict] = field(default_factory=list)
    requested: int = 0
    count: int = 0
    reason: str = ""
    engine: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1. 探测：这台机器上到底有没有可用的转换程序
# ---------------------------------------------------------------------------


def _which(name: str) -> str | None:
    """PATH 上找可执行文件（`shutil.which` 的唯一收口，便于测试替换）。"""
    found = shutil.which(name)
    return str(found) if found else None


def _bundled_ffmpeg_candidates() -> list[Path]:
    """随包提供的转换程序位置（应用资源目录）。

    只查**已经存在**的固定位置：不扫描整棵安装树（那既慢又会误命中无关文件）。
    """
    roots: list[Path] = []
    env_root = os.environ.get("QIO_RESOURCES_DIR")
    if env_root:
        roots.append(Path(env_root))
    # 打包后：可执行文件同级 / 上一级的 resources / _internal
    try:
        exe_dir = Path(sys.executable).resolve().parent
        roots.extend([exe_dir, exe_dir.parent, exe_dir / "resources", exe_dir / "_internal"])
    except OSError:  # pragma: no cover - 解析不出可执行文件路径时就没有随包候选
        pass
    # 源码运行：backend/ 的同级
    try:
        roots.append(Path(__file__).resolve().parents[3])
    except IndexError:  # pragma: no cover
        pass

    names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
    out: list[Path] = []
    for root in roots:
        for sub in ("", "bin", "ffmpeg", "ffmpeg/bin", "resources/bin"):
            base = root / sub if sub else root
            for name in names:
                candidate = base / name
                if candidate.is_file():
                    out.append(candidate)
    return out


def _common_ffmpeg_candidates() -> list[Path]:
    """常见安装位置（Windows 上 winget / scoop / chocolatey 的默认落点）。"""
    if os.name != "nt":
        return []
    local = os.environ.get("LOCALAPPDATA") or ""
    program_files = os.environ.get("ProgramFiles") or "C:/Program Files"
    program_data = os.environ.get("ProgramData") or "C:/ProgramData"
    candidates: list[Path] = []
    bases = [Path(p) for p in (local, program_files, program_data, "C:/ffmpeg", "C:/tools") if p]
    for base in bases:
        candidates.extend(
            [
                base / "ffmpeg" / "bin" / "ffmpeg.exe",
                base / "ffmpeg.exe",
                base / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
                base / "chocolatey" / "bin" / "ffmpeg.exe",
            ]
        )
    # winget 的版本化包目录（Gyan.FFmpeg_*）：只列一级，不深挖
    winget_packages = Path(local) / "Microsoft" / "WinGet" / "Packages" if local else None
    if winget_packages is not None and winget_packages.is_dir():
        try:
            for package in winget_packages.glob("Gyan.FFmpeg*"):
                candidates.extend(sorted(package.glob("*/bin/ffmpeg.exe")))
        except OSError:  # pragma: no cover - 目录不可读就当没有
            pass
    return candidates


def _usable(path: Path | str) -> bool:
    candidate = Path(path)
    try:
        return candidate.is_file()
    except OSError:  # pragma: no cover - 路径不可访问就不是可用候选
        return False


def ffmpeg_path() -> tuple[str | None, str]:
    """可用的 ffmpeg 路径 + 来源说明；找不到时 `(None, 可读原因)`。

    顺序（冻结）：`QIO_FFMPEG` → 应用资源目录（随包）→ PATH → 常见安装位置。
    环境变量指错了会**记进原因里继续往下找**：说清是哪条路指错了，
    而不是把「用户设了个错路径」笼统说成「这台机器上没有」。
    """
    notes: list[str] = []

    configured = str(os.environ.get(FFMPEG_ENV) or "").strip()
    if configured:
        if _usable(configured):
            return str(configured), f"来自 {FFMPEG_ENV} 环境变量"
        notes.append(f"{FFMPEG_ENV} 指向的位置没有可执行文件：{configured}")

    for candidate in _bundled_ffmpeg_candidates():
        if _usable(candidate):
            return str(candidate), "随包提供的转换程序（应用资源目录）"

    found = _which("ffmpeg")
    if found:
        return found, "PATH 上的 ffmpeg"

    for candidate in _common_ffmpeg_candidates():
        if _usable(candidate):
            return str(candidate), "常见安装位置"

    reason = NO_FFMPEG_REASON
    if notes:
        reason = "；".join([*notes, NO_FFMPEG_REASON])
    return None, reason


def ffprobe_path() -> tuple[str | None, str]:
    """ffprobe 位置：优先与 ffmpeg 同目录（winget/随包都是一起来的），否则查 PATH。"""
    ffmpeg, origin = ffmpeg_path()
    if ffmpeg:
        beside = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if _usable(beside):
            return str(beside), origin
    found = _which("ffprobe")
    if found:
        return found, "PATH 上的 ffprobe"
    return None, "这台机器上没有 ffprobe（元数据只能从 ffmpeg 的输出里读）"


# ---------------------------------------------------------------------------
# 2. 子进程（唯一的执行收口）
# ---------------------------------------------------------------------------


def _no_window() -> dict[str, Any]:
    """Windows 上不要弹出控制台窗口（后台进程不该打断用户）。"""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _run_ffmpeg(
    argv: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess:
    """跑一次转换程序（列表参数、无 shell、有超时）。失败**不抛**，由调用方如实降级。"""
    return subprocess.run(  # noqa: S603 - 参数是显式列表，绝不经过 shell
        [str(item) for item in argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=float(timeout),
        check=False,
        **_no_window(),
    )


def _stderr_tail(completed: subprocess.CompletedProcess) -> str:
    raw = completed.stderr or b""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return redact_text(text.strip()[-_STDERR_CHARS:])


# ---------------------------------------------------------------------------
# 3. 元数据
# ---------------------------------------------------------------------------


def _empty_probe(reason: str, *, container: str = "") -> dict:
    return {
        "container": container,
        "duration": None,
        "hasVideo": False,
        "hasAudio": False,
        "width": None,
        "height": None,
        "reason": reason,
    }


def _size_guard(path: Path, *, max_bytes: int | None) -> tuple[int | None, str]:
    limit = MAX_MEDIA_BYTES if max_bytes is None else int(max_bytes)
    try:
        if not path.is_file():
            return None, f"文件不在这个位置：{path.name}（没有可读的媒体可以探测）"
        size = path.stat().st_size
    except OSError as exc:  # 非 ascii 路径在部分平台会抛，照样要给人话
        return None, f"读不到这个文件的大小：{type(exc).__name__}: {redact_text(str(exc))}"
    if size <= 0:
        return None, f"这个文件是空的（0 字节）：没有可探测的媒体内容（{path.name}）"
    if size > limit:
        return None, (
            f"文件 {size} 字节超过媒体处理上限 {limit} 字节（MAX_MEDIA_BYTES={MAX_MEDIA_BYTES}）："
            "本次不做派生处理，也不截取半份内容"
        )
    return size, ""


def probe_media(path: Path, *, timeout: float = 20.0, max_bytes: int | None = None) -> dict:
    """这份媒体的真实元数据（容器 / 时长 / 有没有画面与声音 / 像素尺寸）。

    找不到 ffmpeg 时 `reason` 说明「这台机器上没有可用的转换程序」，其余字段如实为
    None / False —— 绝不用文件名与扩展名猜内容。
    """
    target = Path(path)
    _size, problem = _size_guard(target, max_bytes=max_bytes)
    if problem:
        return _empty_probe(problem)

    probe, origin = ffprobe_path()
    if probe:
        info = _probe_with_ffprobe(probe, target, timeout=timeout)
        if info is not None:
            return info
        # ffprobe 在但读不出来：继续用 ffmpeg 兜一次，别急着说「读不到」
    ffmpeg, ff_origin = ffmpeg_path()
    if not ffmpeg:
        return _empty_probe(ff_origin)
    return _probe_with_ffmpeg(ffmpeg, target, timeout=timeout)


def _probe_with_ffprobe(probe: str, path: Path, *, timeout: float) -> dict | None:
    argv = [
        probe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = _run_ffmpeg(argv, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads((completed.stdout or b"").decode("utf-8", "replace") or "{}")
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    container = ""
    duration: float | None = None
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    container = str(fmt.get("format_name") or "")
    duration = _to_float(fmt.get("duration"))
    width = height = None
    has_video = has_audio = False
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        kind = str(stream.get("codec_type") or "")
        if kind == "video":
            has_video = True
            width = width or _to_int(stream.get("width"))
            height = height or _to_int(stream.get("height"))
            if duration is None:
                duration = _to_float(stream.get("duration"))
        elif kind == "audio":
            has_audio = True
            if duration is None:
                duration = _to_float(stream.get("duration"))
    return {
        "container": container,
        "duration": duration,
        "hasVideo": has_video,
        "hasAudio": has_audio,
        "width": width,
        "height": height,
        "reason": "",
    }


def _probe_with_ffmpeg(ffmpeg: str, path: Path, *, timeout: float) -> dict:
    """没有 ffprobe 时的兜底：`ffmpeg -i` 的 stderr 里有同样的元数据。"""
    argv = [ffmpeg, "-hide_banner", "-i", str(path)]
    try:
        completed = _run_ffmpeg(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _empty_probe(f"探测超时（超过 {timeout:g} 秒）：没有拿到可确认的媒体元数据")
    except OSError as exc:
        return _empty_probe(
            f"调用转换程序失败：{type(exc).__name__}: {redact_text(str(exc))}"
        )
    text = _stderr_tail(completed)
    if "Duration:" not in text and "Stream #" not in text:
        return _empty_probe(
            "转换程序读不出这份文件的媒体信息"
            + (f"（{text}）" if text else "：它可能不是媒体文件，或者文件已损坏")
        )
    return _parse_ffmpeg_probe(text)


def _parse_ffmpeg_probe(text: str) -> dict:
    import re

    container = ""
    matched = re.search(r"Input #0,\s*([^,]+),", text)
    if matched:
        container = matched.group(1).strip()
    duration = None
    matched = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if matched:
        hours, minutes, seconds = matched.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    width = height = None
    has_video = has_audio = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Stream #") or "Video:" not in stripped and "Audio:" not in stripped:
            continue
        if "Video:" in stripped:
            has_video = True
            matched = re.search(r"(\d{2,5})x(\d{2,5})", stripped)
            if matched and width is None:
                width, height = int(matched.group(1)), int(matched.group(2))
        if "Audio:" in stripped:
            has_audio = True
    return {
        "container": container,
        "duration": duration,
        "hasVideo": has_video,
        "hasAudio": has_audio,
        "width": width,
        "height": height,
        "reason": "",
    }


def _to_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed < 0:  # NaN / 负数都不是可信时长
        return None
    return parsed


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 4. 视频抽样帧
# ---------------------------------------------------------------------------


def _even_positions(count: int, *, start: float, end: float) -> list[float]:
    """在 [start, end) 里按**均匀时间点**取 count 个位置（不是帧号，也不是解码顺序）。"""
    if count <= 0:
        return []
    span = max(0.0, float(end) - float(start))
    if span <= 0:
        return [round(float(start), 3)] * count
    step = span / count
    return [round(float(start) + step * index + step / 2.0, 3) for index in range(count)]


def sample_video_frames(
    path: Path,
    out_dir: Path,
    *,
    count: int = 6,
    max_edge: int = DEFAULT_MAX_EDGE,
    start: float = 0.0,
    end: float | None = None,
    timeout: float = 60.0,
    max_bytes: int | None = None,
) -> FrameSample:
    """按均匀时间点抽取 **至多 count 帧**（上限 `MAX_SAMPLE_FRAMES`），带真实时间位置。

    返回的每一帧 `{"at": float, "path": Path, "bytes": int}` 都是**真的写在本轮工作目录
    里的文件**；取不到的帧不补、不伪造 —— 只代表取到的那几张，不是逐帧读完视频。
    边界（帧数、字节、时间窗口）写进 `reason`。
    """
    target = Path(path)
    work = Path(out_dir)
    requested = int(count) if int(count) > 0 else 0
    capped = min(requested, MAX_SAMPLE_FRAMES)
    notes: list[str] = []
    if requested > MAX_SAMPLE_FRAMES:
        notes.append(
            f"请求 {requested} 帧，超上限 MAX_SAMPLE_FRAMES={MAX_SAMPLE_FRAMES}："
            f"本次只取 {capped} 帧（抽样只代表这些时间点，不代表逐帧读完）"
        )

    # 没有 ffmpeg 就没有抽样通道：先如实降级（一个文件都不写），再谈这份文件本身。
    ffmpeg, origin = ffmpeg_path()
    if not ffmpeg:
        return FrameSample(
            frames=[], requested=requested, count=0, reason=_join(f"视频抽样：{origin}", notes)
        )

    _size, problem = _size_guard(target, max_bytes=max_bytes)
    if problem:
        return FrameSample(frames=[], requested=requested, count=0, reason=_join(problem, notes))
    if capped <= 0:
        return FrameSample(
            frames=[],
            requested=requested,
            count=0,
            reason=_join("请求的帧数是 0：没有要抽的帧", notes),
        )

    info = probe_media(target, timeout=max(5.0, min(timeout, 20.0)), max_bytes=max_bytes)
    if info["reason"]:
        return FrameSample(
            frames=[],
            requested=requested,
            count=0,
            reason=_join(f"视频抽样需要先读元数据，但读不到：{info['reason']}", notes),
        )
    if not info["hasVideo"]:
        return FrameSample(
            frames=[],
            requested=requested,
            count=0,
            reason=_join(
                "这份文件里没有画面轨道（只有音频或没有流）："
                "取不到画面帧，也没有把它当成视频逐帧读过",
                notes,
            ),
        )

    duration = info["duration"]
    window_end = float(end) if end is not None else (
        duration if duration is not None and duration > 0 else float(start) + 1.0
    )
    if end is not None and duration is not None and end > duration:
        notes.append(f"请求的结束时间 {float(end):g}s 超过真实时长 {duration:g}s：按真实时长截到末尾")
    window_end = min(window_end, duration) if duration is not None else window_end
    if window_end <= float(start):
        return FrameSample(
            frames=[],
            requested=requested,
            count=0,
            reason=_join(
                f"时间窗口 [{float(start):g}s, {window_end:g}s] 是空的：没有可抽的时间点",
                notes,
            ),
        )

    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return FrameSample(
            frames=[],
            requested=requested,
            count=0,
            reason=_join(
                f"建不了样本工作目录：{type(exc).__name__}: {redact_text(str(exc))}", notes
            ),
        )

    frames: list[dict] = []
    failures: list[str] = []
    for index, at in enumerate(_even_positions(capped, start=float(start), end=window_end)):
        frame_path = work / f"frame_{index + 1:02d}.jpg"
        _unlink(frame_path)
        argv = [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-ss", f"{at:.3f}",
            "-i", str(target),
            "-frames:v", "1",
            "-vf", f"scale='min({int(max_edge)},iw)':-2",
            "-q:v", "3",
            str(frame_path),
        ]
        try:
            completed = _run_ffmpeg(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            _unlink(frame_path)
            failures.append(f"{at:g}s：抽样超时（超过 {timeout:g} 秒）")
            continue
        except OSError as exc:
            _unlink(frame_path)
            failures.append(f"{at:g}s：调用转换程序失败（{type(exc).__name__}）")
            continue
        try:
            size = frame_path.stat().st_size
        except OSError:
            size = 0
        if completed.returncode != 0 or size <= 0:
            _unlink(frame_path)
            detail = _stderr_tail(completed)
            failures.append(f"{at:g}s：{detail or '这一帧没有写出来'}")
            continue
        frames.append({"at": float(at), "path": frame_path, "bytes": int(size)})

    if not frames:
        reason = f"没有抽到任何一帧（用了 {Path(ffmpeg).name}，{origin}）"
        if failures:
            reason += "；失败原因：" + "；".join(failures[:3])
        return FrameSample(frames=[], requested=requested, count=0, reason=_join(reason, notes))
    if failures:
        notes.append(
            f"有 {len(failures)} 个时间点没取到帧（不补帧、不伪造）：" + "；".join(failures[:3])
        )
    return FrameSample(
        frames=frames,
        requested=requested,
        count=len(frames),
        reason=_join("", notes),
        engine={"provider": "ffmpeg", "model": Path(ffmpeg).name},
    )


# ---------------------------------------------------------------------------
# 5. 音频抽取（转写用）
# ---------------------------------------------------------------------------


def extract_audio(
    path: Path,
    out_path: Path,
    *,
    max_seconds: float = MAX_TRANSCRIBE_SECONDS,
    timeout: float = 90.0,
    max_bytes: int | None = None,
) -> dict:
    """把媒体里的音轨抽成**单声道 16 kHz wav**（转写通道的输入）。

    `{"path": Path|None, "seconds": float, "bytes": int, "reason": str}`：
    `path=None` 表示没有抽出来（没有 ffmpeg / 没有音轨 / 超出边界），`reason` 必须能看懂。
    时长上限 `max_seconds` 会写进 `reason`（截断是事实，不许含糊）。
    """
    target = Path(path)
    out = Path(out_path)
    limit_seconds = float(max_seconds) if float(max_seconds) > 0 else MAX_TRANSCRIBE_SECONDS
    notes: list[str] = []

    # 同抽样：没有转换程序就是没有音频抽取通道，先如实说清楚。
    ffmpeg, origin = ffmpeg_path()
    if not ffmpeg:
        return {
            "path": None,
            "seconds": 0.0,
            "bytes": 0,
            "reason": _join(f"音频抽取：{origin}", notes),
        }

    _size, problem = _size_guard(target, max_bytes=max_bytes)
    if problem:
        return {"path": None, "seconds": 0.0, "bytes": 0, "reason": _join(problem, notes)}

    info = probe_media(target, timeout=max(5.0, min(timeout, 20.0)), max_bytes=max_bytes)
    if info["reason"]:
        return {
            "path": None,
            "seconds": 0.0,
            "bytes": 0,
            "reason": _join(f"音频抽取需要先读元数据，但读不到：{info['reason']}", notes),
        }
    if not info["hasAudio"]:
        return {
            "path": None,
            "seconds": 0.0,
            "bytes": 0,
            "reason": _join(
                "这份文件里没有音频轨道：没有可以转写的语音内容"
                "（画面不会被当成声音，也不推断画面里的对话）",
                notes,
            ),
        }

    duration = info["duration"]
    window = limit_seconds
    if duration is not None and duration > 0:
        window = min(limit_seconds, duration)
        if duration > limit_seconds:
            notes.append(
                f"原始音频 {duration:g}s，超过转写时长上限 MAX_TRANSCRIBE_SECONDS="
                f"{MAX_TRANSCRIBE_SECONDS:g}s：本次只抽前 {limit_seconds:g}s，"
                "后面的内容没有转写"
            )
    elif limit_seconds >= MAX_TRANSCRIBE_SECONDS:
        notes.append(
            f"读不出真实时长：按上限 MAX_TRANSCRIBE_SECONDS={MAX_TRANSCRIBE_SECONDS:g}s 抽取"
        )

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "path": None,
            "seconds": 0.0,
            "bytes": 0,
            "reason": _join(
                f"建不了样本工作目录：{type(exc).__name__}: {redact_text(str(exc))}", notes
            ),
        }
    _unlink(out)

    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-y",
        "-i", str(target),
        "-t", f"{window:.3f}",
        "-vn",
        "-ac", str(TRANSCRIBE_CHANNELS),
        "-ar", str(TRANSCRIBE_RATE),
        "-c:a", "pcm_s16le",
        str(out),
    ]
    try:
        completed = _run_ffmpeg(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        _unlink(out)
        return {
            "path": None,
            "seconds": 0.0,
            "bytes": 0,
            "reason": _join(f"音频抽取超时（超过 {timeout:g} 秒）：没有拿到音频片段", notes),
        }
    except OSError as exc:
        _unlink(out)
        return {
            "path": None,
            "seconds": 0.0,
            "bytes": 0,
            "reason": _join(
                f"调用转换程序失败：{type(exc).__name__}: {redact_text(str(exc))}", notes
            ),
        }

    try:
        size = out.stat().st_size
    except OSError:
        size = 0
    if completed.returncode != 0 or size <= 0:
        _unlink(out)
        detail = _stderr_tail(completed)
        return {
            "path": None,
            "seconds": 0.0,
            "bytes": 0,
            "reason": _join(
                f"音频抽取失败{f'：{detail}' if detail else '：转换程序没有产出音频片段'}", notes
            ),
        }

    seconds = _wav_seconds(out)
    if seconds is None:
        seconds = window
    return {"path": out, "seconds": float(seconds), "bytes": int(size), "reason": _join("", notes)}


def _wav_seconds(path: Path) -> float | None:
    """按 wav 头读真实秒数（不猜、也不用转换程序再报一次）。"""
    import wave

    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if not rate:
                return None
            return float(handle.getnframes()) / float(rate)
    except (OSError, wave.Error):
        return None


# ---------------------------------------------------------------------------
# 6. 清理
# ---------------------------------------------------------------------------


def cleanup_media(work_dir: Path) -> None:
    """删掉本轮工作目录（样本帧 / 音频片段）。

    **不抛**：目录本来就不在、删不掉、路径其实是个文件 —— 都只是「清理没做到」，
    绝不让调用方的收尾流程被清理失败打断（失败路径同样要走到这里）。
    """
    target = Path(work_dir)
    try:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            return
        if target.exists():
            target.unlink()
    except OSError:  # pragma: no cover - 尽力而为，不打断调用方
        return


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return


def _join(reason: str, notes: list[str]) -> str:
    parts = [item for item in [reason, *(notes or [])] if item]
    return "；".join(parts)
