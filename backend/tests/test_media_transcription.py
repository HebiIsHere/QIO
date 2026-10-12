"""音频转写通道（契约 §4.4 / §4.2）：真实能读音频的那条路。

诚实口径是本文件的判据 —— 这些事实必须**分开**、不许含糊：

* 没有转写凭据 ≠ 有凭据但调用失败：两种原因的文案不同，界面据此给出不同的恢复入口；
* 能力**按每次运行解析**：同一个 adapter 实例这次有转写通道、下次没有，必须如实变化；
* 转写文字**只有真的从端点拿到才有**：失败路径不许出现任何编造的文字；
* 转写文字必须真的进入模型请求（不是只写在回执里）；
* 超过 MAX_TRANSCRIBE_SECONDS 时**不静默截断**：要么写明「只转写了前 N 秒」，要么不转写。

网络边界一律用假客户端（httpx.MockTransport + OpenAI 兼容 SDK），
**不联网、不用真实 Key**；ffmpeg 用假实现，所以本文件在**没有 ffmpeg 的机器上也能全跑**。
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import httpx
import pytest

from agent.adapters.base import ChatMessage
from agent.adapters.native import NativeAdapter
from agent.interactive import materials
from agent.services import media

# 只有**真的**从假端点拿到过这句话，才允许在材料里出现（失败路径要断言它不存在）。
REAL_TRANSCRIPT = "今天下午三点在会议室开项目同步会"
SECRET = "sk-not-a-real-key-local-fake-transport"


# ---------------------------------------------------------------------------
# 假 HTTP 客户端：记录 multipart 请求，返回一份固定转写
# ---------------------------------------------------------------------------


class _FakeEndpoint:
    """OpenAI 兼容端点：POST /audio/transcriptions，multipart 请求体。"""

    def __init__(self, *, text: str = REAL_TRANSCRIPT, status: int = 200) -> None:
        self.text = text
        self.status = status
        self.requests: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            raw = request.read()
            self.requests.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "content_type": request.headers.get("content-type", ""),
                    "authorization": request.headers.get("authorization", ""),
                    "raw": raw,
                    "text": raw.decode("utf-8", "replace"),
                    "octets": len(raw),
                }
            )
            if self.status != 200:
                return httpx.Response(
                    self.status,
                    json={"error": {"message": "假端点：这次调用失败了", "type": "invalid_request_error"}},
                )
            return httpx.Response(self.status, json={"text": self.text})

        return httpx.MockTransport(handler)

    @property
    def calls(self) -> list[dict]:
        return self.requests


def _adapter(endpoint: _FakeEndpoint, *, model: str = "whisper-test-model") -> NativeAdapter:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=SECRET,
        base_url="https://fake.invalid/v1",
        http_client=httpx.AsyncClient(transport=endpoint.transport()),
    )
    return NativeAdapter(client, model, endpoint="https://fake.invalid/v1")


# ---------------------------------------------------------------------------
# 假 ffmpeg：抽音频这条路不走真实转换程序（本机没有 ffmpeg 也照样可跑）
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_ffmpeg(monkeypatch):
    """把音频抽取换成假实现：真写一个合法 wav，秒数如实（不是伪造调用）。"""
    calls: list[dict] = []

    def fake_extract(path, out_path, *, max_seconds=media.MAX_TRANSCRIBE_SECONDS, timeout=90.0, max_bytes=None):
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        seconds = 2.0
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(b"\x00\x00" * int(16_000 * seconds))
        calls.append({"max_seconds": max_seconds})
        return {"path": target, "seconds": seconds, "bytes": target.stat().st_size, "reason": ""}

    # ffprobe 探测也一并换成假的：本机装了 ffprobe 时它同样会去跑真程序
    monkeypatch.setattr(media, "ffmpeg_path", lambda: (r"C:\fake\ffmpeg.exe", "测试假转换程序"))
    monkeypatch.setattr(media, "ffprobe_path", lambda: (None, "测试假转换程序：本次不用 ffprobe"))
    monkeypatch.setattr(media, "extract_audio", fake_extract)
    monkeypatch.setattr(materials.media, "extract_audio", fake_extract)
    return calls


def _audio_candidate(tmp_path: Path, name: str = "会议录音.mp3") -> dict:
    source = tmp_path / name
    source.write_bytes(b"ID3\x03\x00" + b"\x00" * 128)
    return {
        "attachmentId": "att_audio_1",
        "card_id": "card_audio_1",
        "name": name,
        "kind": "audio",
        "path": source,
        "bytes": source.stat().st_size,
        "version": "v1",
    }


# ---------------------------------------------------------------------------
# 1. 适配器：能力按运行解析
# ---------------------------------------------------------------------------


def test_native_adapter_has_no_transcription_channel_without_a_route():
    """拿不到转写凭据 → 这条 adapter 的 audio_transcription 保持 None（不声明没有的通道）。

    能力按**每次运行解析**：不是类的静态属性 —— 同一个实例装上路由之后必须能变。
    """
    endpoint = _FakeEndpoint()
    adapter = _adapter(endpoint)
    assert adapter.audio_transcription is None, "没有路由就声明了转写通道：那是编出来的能力"

    adapter.set_audio_route(
        endpoint="https://fake.invalid/v1", api_key=SECRET, model="whisper-test-model"
    )
    assert adapter.audio_transcription == "openai.audio.transcriptions"

    adapter.set_audio_route(None)
    assert adapter.audio_transcription is None, "路由撤掉之后能力必须跟着变（按运行解析）"


async def test_transcribe_audio_posts_multipart_and_returns_only_the_real_text():
    """有凭据 → 走 POST /audio/transcriptions（multipart），模型名来自路由，不硬编码。"""
    endpoint = _FakeEndpoint()
    adapter = _adapter(endpoint)
    adapter.set_audio_route(
        endpoint="https://fake.invalid/v1", api_key=SECRET, model="whisper-from-credential"
    )
    result = await adapter.transcribe_audio(b"RIFFfake-wav-bytes", filename="voice.wav", language="zh")
    assert result["text"] == REAL_TRANSCRIPT
    call = endpoint.calls[0]
    assert call["method"] == "POST"
    assert call["path"].endswith("/audio/transcriptions")
    assert call["content_type"].startswith("multipart/form-data")
    assert "whisper-from-credential" in call["text"], call["text"][:400]
    assert 'filename="voice.wav"' in call["text"]
    assert b"RIFFfake-wav-bytes" in call["raw"], "音频字节没有真的进请求体"


async def test_transcribe_audio_without_a_route_says_so_and_makes_no_request():
    """没有路由：**一个请求都不发**，原因说清是「没有转写凭据」。"""
    endpoint = _FakeEndpoint()
    adapter = _adapter(endpoint)
    with pytest.raises(RuntimeError) as err:
        await adapter.transcribe_audio(b"RIFFfake", filename="voice.wav")
    assert "转写凭据" in str(err.value) or "转写通道" in str(err.value), str(err.value)
    assert endpoint.calls == [], "没有凭据就不许发请求"


async def test_transcribe_audio_reports_the_provider_error_truthfully():
    """有凭据但调用失败：异常里带真实原因，绝不返回编造的文字。"""
    endpoint = _FakeEndpoint(status=400)
    adapter = _adapter(endpoint)
    adapter.set_audio_route(
        endpoint="https://fake.invalid/v1", api_key=SECRET, model="whisper-test-model"
    )
    with pytest.raises(Exception) as err:
        await adapter.transcribe_audio(b"RIFFfake", filename="voice.wav")
    message = str(err.value)
    assert "假端点" in message or "400" in message, message
    assert REAL_TRANSCRIPT not in message


# ---------------------------------------------------------------------------
# 2. 材料层：真的转写出条目 + 文字进模型请求
# ---------------------------------------------------------------------------


def test_audio_with_a_working_channel_becomes_audio_transcribed(tmp_path, fake_ffmpeg):
    """有凭据 → 材料条目 audio_transcribed，带 transcript / 时段 / 引擎，且文字进模型请求。"""
    endpoint = _FakeEndpoint()
    adapter = _adapter(endpoint)
    adapter.set_audio_route(
        endpoint="https://fake.invalid/v1", api_key=SECRET, model="whisper-from-credential"
    )
    transcriber = materials.transcriber_for_adapter(adapter, model="whisper-from-credential")
    assert transcriber is not None

    result = materials.resolve_media(
        candidates=[_audio_candidate(tmp_path)],
        audio_transcription=adapter.audio_transcription,
        transcriber=transcriber,
    )
    entry = result["entries"][0]

    assert entry["source"] == "audio_transcribed"
    assert entry["read"] is True
    assert entry["transcript"] == REAL_TRANSCRIPT
    assert entry["engine"]["provider"] == "openai.audio.transcriptions"
    assert entry["engine"]["model"] == "whisper-from-credential"
    assert entry["range"]["start"] is not None and entry["range"]["end"] is not None
    assert endpoint.calls, "材料层没有真的调用转写端点"
    assert endpoint.calls[0]["octets"] > 0

    # 「模型请求里真的带着这段文字」：走真实的 NativeAdapter 序列化路径。
    messages = [ChatMessage(role="user", content="听听这段录音说了什么")]
    materials.attach_transcripts(messages, result["entries"])
    sent = adapter.to_openai_messages(messages)
    payload = json.dumps(sent, ensure_ascii=False)
    assert REAL_TRANSCRIPT in payload, payload


def test_audio_without_a_transcription_credential_says_no_credential(tmp_path, fake_ffmpeg):
    """没有凭据：条目不 read、原因写明「没有转写凭据」，且**没有任何文字**。"""
    endpoint = _FakeEndpoint()
    adapter = _adapter(endpoint)
    assert adapter.audio_transcription is None
    assert materials.transcriber_for_adapter(adapter, model=None) is None

    result = materials.resolve_media(candidates=[_audio_candidate(tmp_path)])
    entry = result["entries"][0]

    assert entry["source"] == "audio_unsupported"
    assert entry["read"] is False
    assert "转写凭据" in entry["reason"], entry["reason"]
    assert not entry.get("transcript"), "没有转写却写了文字"
    assert endpoint.calls == [], "没有凭据却发了请求"


def test_audio_with_a_failing_channel_reports_the_real_failure(tmp_path, fake_ffmpeg):
    """有凭据但调用失败：原因区分「凭据缺失」，且不编造文字。"""
    endpoint = _FakeEndpoint(status=400)
    adapter = _adapter(endpoint)
    adapter.set_audio_route(
        endpoint="https://fake.invalid/v1", api_key=SECRET, model="whisper-test-model"
    )
    transcriber = materials.transcriber_for_adapter(adapter, model="whisper-test-model")

    result = materials.resolve_media(
        candidates=[_audio_candidate(tmp_path)],
        audio_transcription=adapter.audio_transcription,
        transcriber=transcriber,
    )
    entry = result["entries"][0]

    assert entry["source"] == "audio_unsupported"
    assert entry["read"] is False
    assert "转写" in entry["reason"], entry["reason"]
    assert "没有转写凭据" not in entry["reason"], "调用失败被说成了没有凭据：" + entry["reason"]
    assert not entry.get("transcript")
    assert endpoint.calls, "没有真的调用端点的失败不是失败"


def test_transcription_over_the_cap_is_not_silently_truncated(tmp_path, fake_ffmpeg, monkeypatch):
    """超过 MAX_TRANSCRIBE_SECONDS：不静默截断 —— 说清「只转写了前 N 秒」。"""
    endpoint = _FakeEndpoint()
    adapter = _adapter(endpoint)
    adapter.set_audio_route(
        endpoint="https://fake.invalid/v1", api_key=SECRET, model="whisper-test-model"
    )
    transcriber = materials.transcriber_for_adapter(adapter, model="whisper-test-model")

    long_duration = media.MAX_TRANSCRIBE_SECONDS + 120.0
    monkeypatch.setattr(
        materials.media,
        "probe_media",
        lambda path, **kwargs: {
            "container": "mp3",
            "duration": long_duration,
            "hasVideo": False,
            "hasAudio": True,
            "width": None,
            "height": None,
            "reason": "",
        },
    )

    result = materials.resolve_media(
        candidates=[_audio_candidate(tmp_path)],
        audio_transcription=adapter.audio_transcription,
        transcriber=transcriber,
    )
    entry = result["entries"][0]
    joined = entry["reason"] + " " + " ".join(entry.get("notes") or [])

    assert entry["source"] == "audio_transcribed"
    assert "只转写" in joined and "300" in joined, joined
    assert "没有转写（不截一半就当成完整内容）" not in joined, joined
    # 时段按**实际抽到的片段**记（这一段只有 2 秒），不假装读满了 300 秒
    assert entry["range"]["end"] == pytest.approx(2.0, abs=0.5), entry["range"]
