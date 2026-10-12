"""媒体用例共享的 ffmpeg 能力探测与真实素材夹具（test_media_service 与两个 e2e 文件共用）。

为什么单独一个模块：这三个文件都要「本机没有 ffmpeg 就 skip 并说明原因」这同一套判据。
共用一份，避免三处各写一遍探测（写了就会漂移）。

导入方式用 try/except 兼容两种 pytest rootdir 行为（CI 的 Linux 上 tests 不是包）：

    try:
        from media_shared import FFMPEG, needs_ffmpeg, real_audio, real_video
    except ImportError:  # 某些 rootdir 下按 tests.xxx 导入
        from tests.media_shared import FFMPEG, needs_ffmpeg, real_audio, real_video
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.services import media

# ---------------------------------------------------------------------------
# 本机实际能力（探测命令写进 skip 原因，便于如实汇报）
# ---------------------------------------------------------------------------


def _ffmpeg_bin() -> str | None:
    path, _origin = media.ffmpeg_path()
    return path


FFMPEG = _ffmpeg_bin()
needs_ffmpeg = pytest.mark.skipif(
    not FFMPEG,
    reason=(
        "这台机器上没有可用的 ffmpeg（探测顺序：QIO_FFMPEG 环境变量 → 应用资源目录 → "
        "PATH → 常见安装位置），所以无法现场生成真实音视频；本用例跳过，未假装通过"
    ),
)


def run_ffmpeg(args: list[str], *, timeout: float = 60.0) -> None:
    completed = subprocess.run(  # noqa: S603 - 参数是固定字符串列表，无 shell
        [str(FFMPEG), *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")[-800:]


@pytest.fixture()
def real_video(tmp_path: Path) -> Path:
    """2 秒真实 mp4（320x240，25fps，带一条正弦音轨）。"""
    if not FFMPEG:
        pytest.skip("没有 ffmpeg：无法现场生成测试视频")
    out = tmp_path / "clip.mp4"
    run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "lavfi",
            "-i", "testsrc=size=320x240:rate=25:duration=2",
            "-f", "lavfi",
            "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest",
            str(out),
        ]
    )
    assert out.is_file() and out.stat().st_size > 0
    return out


@pytest.fixture()
def real_audio(tmp_path: Path) -> Path:
    """2 秒真实 wav（单声道 44.1k 正弦）。"""

    out = tmp_path / "tone.wav"
    run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "lavfi",
            "-i", "sine=frequency=440:duration=2",
            "-ac", "1",
            "-ar", "44100",
            str(out),
        ]
    )
    assert out.is_file() and out.stat().st_size > 0
    return out


@pytest.fixture()
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """把探测结果换成「这台机器上没有」——降级路径必须永远可测。"""
    monkeypatch.setattr(media, "ffmpeg_path", lambda: (None, media.NO_FFMPEG_REASON))