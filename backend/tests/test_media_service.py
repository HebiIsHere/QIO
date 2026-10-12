"""media 服务：ffmpeg 探测 / 媒体元数据 / 视频抽样 / 音频抽取与清理（契约 §4.3、§4.4）。

诚实口径是本文件的判据：

* 找不到 ffmpeg 时 `ffmpeg_path()` 必须给出**可读原因**，依赖它的能力全部如实降级，
  绝不假装转换成功，也绝不静默换成别的外部服务；
* 边界（MAX_MEDIA_BYTES / MAX_SAMPLE_FRAMES / MAX_TRANSCRIBE_SECONDS）必须写进 `reason`；
* 样本帧与音频片段只写在本轮工作目录，`cleanup_media` 在**失败路径**上也要清理干净；
* 抽样帧带真实时间位置：只代表取到的那几张，不是逐帧读完。

真实用例（本机有 ffmpeg 时）：用 ffmpeg 现场生成 2 秒测试视频与音频写进测试临时目录。
本机/CI 没有 ffmpeg 时这些用例 **skip 并说明原因**（不假装通过）；纯降级路径用
monkeypatch 把 ffmpeg 探测结果换成「没有」，因此永远可跑。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
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


def _run_ffmpeg(args: list[str], *, timeout: float = 60.0) -> None:
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
    _run_ffmpeg(
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
    if not FFMPEG:
        pytest.skip("没有 ffmpeg：无法现场生成测试音频")
    out = tmp_path / "tone.wav"
    _run_ffmpeg(
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


# ---------------------------------------------------------------------------
# 1. ffmpeg 探测
# ---------------------------------------------------------------------------


def test_ffmpeg_path_reports_a_readable_reason_when_missing(monkeypatch, tmp_path):
    """探测顺序里一处都没有时：返回 (None, 可读原因)，原因里点明「这台机器上没有」。"""
    monkeypatch.delenv(media.FFMPEG_ENV, raising=False)
    monkeypatch.setattr(media, "_bundled_ffmpeg_candidates", lambda: [])
    monkeypatch.setattr(media, "_which", lambda _name: None)
    monkeypatch.setattr(media, "_common_ffmpeg_candidates", lambda: [])

    path, origin = media.ffmpeg_path()

    assert path is None
    assert "没有" in origin
    assert "ffmpeg" in origin
    assert media.NO_FFMPEG_REASON in origin or origin == media.NO_FFMPEG_REASON


def test_ffmpeg_env_var_wins_and_must_be_an_executable(monkeypatch, tmp_path):
    """QIO_FFMPEG 是第一条：指到一个真实可执行文件时就用它；指到不存在的位置就跳过它。"""
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"MZ fake")
    monkeypatch.setenv(media.FFMPEG_ENV, str(fake))
    monkeypatch.setattr(media, "_which", lambda _name: None)
    monkeypatch.setattr(media, "_bundled_ffmpeg_candidates", lambda: [])
    monkeypatch.setattr(media, "_common_ffmpeg_candidates", lambda: [])

    path, origin = media.ffmpeg_path()

    assert path == str(fake)
    assert "QIO_FFMPEG" in origin

    monkeypatch.setenv(media.FFMPEG_ENV, str(tmp_path / "nope.exe"))
    path2, origin2 = media.ffmpeg_path()
    assert path2 is None
    assert "QIO_FFMPEG" in origin2  # 说清是哪条路指错了，而不是笼统说没有


@needs_ffmpeg
def test_ffmpeg_path_finds_a_real_binary_on_this_machine():
    path, origin = media.ffmpeg_path()
    assert path is not None
    assert Path(path).is_file()
    assert origin


# ---------------------------------------------------------------------------
# 2. probe_media
# ---------------------------------------------------------------------------


def test_probe_media_without_ffmpeg_is_honest(no_ffmpeg, tmp_path):
    target = tmp_path / "whatever.mp4"
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    info = media.probe_media(target)

    assert info["duration"] is None
    assert info["hasVideo"] is False
    assert info["hasAudio"] is False
    assert info["width"] is None and info["height"] is None
    assert media.NO_FFMPEG_REASON in info["reason"]
    assert "没有" in info["reason"]


def test_probe_media_rejects_oversized_and_missing_files(tmp_path):
    missing = tmp_path / "gone.mp4"
    info = media.probe_media(missing)
    assert info["duration"] is None
    assert "不在" in info["reason"] or "找不到" in info["reason"]

    big = tmp_path / "big.mp4"
    big.write_bytes(b"x")
    info = media.probe_media(big, max_bytes=0)
    assert info["duration"] is None
    assert str(media.MAX_MEDIA_BYTES) in info["reason"] or "上限" in info["reason"]


@needs_ffmpeg
def test_probe_media_reads_real_video_and_audio_facts(real_video: Path):
    info = media.probe_media(real_video)

    assert info["reason"] == ""
    assert info["hasVideo"] is True
    assert info["hasAudio"] is True
    assert info["width"] == 320 and info["height"] == 240
    assert info["duration"] is not None and 1.5 <= info["duration"] <= 2.6
    assert info["container"]


@needs_ffmpeg
def test_probe_media_sees_an_audio_only_file(real_audio: Path):
    info = media.probe_media(real_audio)

    assert info["hasAudio"] is True
    assert info["hasVideo"] is False
    assert info["width"] is None and info["height"] is None


# ---------------------------------------------------------------------------
# 3. sample_video_frames
# ---------------------------------------------------------------------------


def test_sample_frames_without_ffmpeg_explains_and_writes_nothing(no_ffmpeg, tmp_path):
    out_dir = tmp_path / "work"
    result = media.sample_video_frames(tmp_path / "clip.mp4", out_dir)

    assert result.frames == []
    assert media.NO_FFMPEG_REASON in result.reason
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


def test_sample_frames_caps_the_requested_count_and_says_so(monkeypatch, tmp_path):
    """count 超过 MAX_SAMPLE_FRAMES 时按上限取，并把边界写进 reason。

    `count` 是**真实取到几帧**（这里一个文件都没有，所以是 0）；上限这件事
    由 `reason` 如实说明，绝不把「打算取几帧」报成「取到了几帧」。
    """
    def fake_ffmpeg(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("没有 ffmpeg 的路径不该真的去调用转换程序")

    monkeypatch.setattr(media, "_run_ffmpeg", fake_ffmpeg)
    result = media.sample_video_frames(
        tmp_path / "clip.mp4", tmp_path / "work", count=99
    )
    assert result.requested == 99
    assert result.count == 0
    assert str(media.MAX_SAMPLE_FRAMES) in result.reason
    assert "11" not in result.reason.split("MAX_SAMPLE_FRAMES=")[1][:3]


@needs_ffmpeg
def test_sample_frames_are_real_files_with_time_positions(real_video: Path, tmp_path: Path):
    out_dir = tmp_path / "frames"
    result = media.sample_video_frames(real_video, out_dir, count=4)

    assert result.reason == ""
    assert len(result.frames) == 4
    for frame in result.frames:
        assert Path(frame["path"]).is_file()
        assert frame["bytes"] > 0
        assert 0.0 <= float(frame["at"]) <= 2.6
    times = [float(frame["at"]) for frame in result.frames]
    assert times == sorted(times)
    assert len(set(round(t, 2) for t in times)) >= 3  # 真的按时间点分散取，不是同一帧
    for frame in result.frames:
        assert Path(frame["path"]).parent == out_dir  # 只写本轮工作目录


@needs_ffmpeg
def test_sample_frames_honours_the_time_window(real_video: Path, tmp_path: Path):
    result = media.sample_video_frames(
        real_video, tmp_path / "win", count=3, start=0.5, end=1.0
    )
    assert result.reason == ""
    assert len(result.frames) == 3
    for frame in result.frames:
        assert 0.5 <= float(frame["at"]) <= 1.0 + 0.05


@needs_ffmpeg
def test_sample_frames_onto_an_audio_only_file_is_honest(real_audio: Path, tmp_path: Path):
    """音频没有画面：如实说「取不到帧」，不留半张图，也不报 success。"""
    result = media.sample_video_frames(real_audio, tmp_path / "noframes", count=3)

    assert result.frames == []
    assert result.reason


def test_size_guard_rejects_oversized_and_empty_files(tmp_path):
    """字节边界写在 reason 里（抽样与音频抽取共用这一道闸，与有没有 ffmpeg 无关）。"""
    big = tmp_path / "big.mp4"
    big.write_bytes(b"x" * 10)
    _size, problem = media._size_guard(big, max_bytes=4)
    assert _size is None
    assert "上限" in problem and "4" in problem

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    _size, problem = media._size_guard(empty, max_bytes=1024)
    assert _size is None
    assert "空" in problem

    missing = tmp_path / "missing.mp4"
    _size, problem = media._size_guard(missing, max_bytes=1024)
    assert _size is None
    assert "不在" in problem


# ---------------------------------------------------------------------------
# 4. extract_audio
# ---------------------------------------------------------------------------


def test_extract_audio_without_ffmpeg_explains(no_ffmpeg, tmp_path):
    out = tmp_path / "work" / "audio.wav"
    result = media.extract_audio(tmp_path / "clip.mp4", out)

    assert result["path"] is None
    assert result["seconds"] == 0.0
    assert media.NO_FFMPEG_REASON in result["reason"]


@needs_ffmpeg
def test_extract_audio_produces_mono_16k_wav(real_video: Path, tmp_path: Path):
    out = tmp_path / "work" / "audio.wav"
    result = media.extract_audio(real_video, out)

    assert result["reason"] == ""
    assert Path(result["path"]) == out
    assert out.is_file()
    assert result["bytes"] == out.stat().st_size > 0
    with wave.open(str(out), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        assert 1.5 <= handle.getnframes() / 16000 <= 2.6
    assert 1.5 <= result["seconds"] <= 2.6


@needs_ffmpeg
def test_extract_audio_clamps_to_max_transcribe_seconds(real_video: Path, tmp_path: Path):
    out = tmp_path / "work" / "short.wav"
    result = media.extract_audio(real_video, out, max_seconds=0.5)

    assert 0.3 <= result["seconds"] <= 0.8
    assert str(media.MAX_TRANSCRIBE_SECONDS) in result["reason"] or "0.5" in result["reason"]
    with wave.open(str(out), "rb") as handle:
        assert 0.3 <= handle.getnframes() / 16000 <= 0.8


def test_extract_audio_without_an_audio_track_is_honest(tmp_path):
    """纯视频（无声）→ 没有音频可抽：reason 讲清楚，不产出一个空壳文件。"""
    if not FFMPEG:
        pytest.skip("没有 ffmpeg：无法现场生成无声视频")
    silent = tmp_path / "silent.mp4"
    _run_ffmpeg(
        [
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(silent),
        ]
    )
    out = tmp_path / "work" / "silent.wav"
    result = media.extract_audio(silent, out)

    assert result["path"] is None
    assert result["reason"]
    assert not out.exists()


# ---------------------------------------------------------------------------
# 5. cleanup_media
# ---------------------------------------------------------------------------


def test_cleanup_media_removes_only_work_dir_contents(tmp_path):
    work = tmp_path / "work"
    nested = work / "frames"
    nested.mkdir(parents=True)
    (work / "audio.wav").write_bytes(b"x")
    (nested / "frame_1.jpg").write_bytes(b"y")
    keep = tmp_path / "keep.txt"
    keep.write_bytes(b"z")

    media.cleanup_media(work)

    assert not work.exists()
    assert keep.is_file()


def test_cleanup_media_is_quiet_on_failure_paths(tmp_path):
    """失败路径也要清理：目录已经不在、路径是文件、权限都不该抛出去。"""
    media.cleanup_media(tmp_path / "never-existed")

    a_file = tmp_path / "a_file.wav"
    a_file.write_bytes(b"x")
    media.cleanup_media(a_file)
    assert not a_file.exists()

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "frame.jpg").write_bytes(b"y")
    media.cleanup_media(locked)
    assert not locked.exists()
