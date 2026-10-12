"""把 fixtures/preview 的真实样例经 services/preview.convert 转出来的**真实输出**打印成 JSON。

用途：前端渲染器测试用它当输入 —— 这样前端消费的是后端**真实**产出的形状，
而不是测试作者手抄的一份"看起来像"的数据。

跑法（backend 目录下）：
    .venv/Scripts/python.exe tests/fixtures/preview/dump_json.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
_TESTS = _HERE.parents[2]
_BACKEND = _TESTS.parent
for entry in (str(_BACKEND / "src"), str(_TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from agent.services import preview  # noqa: E402
from agent.services.attachments import Attachment  # noqa: E402
from fixtures.preview import build_all  # noqa: E402


def _attachment(path: Path) -> Attachment:
    return Attachment(
        id="att_sample",
        message_id=None,
        turn_id=None,
        topic_id=None,
        kind="copy",
        original_name=path.name,
        stored_path=str(path),
        source_path=None,
        size_bytes=path.stat().st_size,
        mtime=path.stat().st_mtime,
        sha256=None,
        state="ready",
        error=None,
        created_at="2026-10-12T00:00:00+00:00",
        updated_at="2026-10-12T00:00:00+00:00",
    )


def main() -> None:
    dest = Path(tempfile.mkdtemp(prefix="qio-preview-samples-"))
    samples = build_all(dest)
    payload = {}
    for key in ("docx", "xlsx", "pptx", "pdf", "code", "png"):
        path = samples[key]
        payload[key] = preview.convert(_attachment(path), path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
