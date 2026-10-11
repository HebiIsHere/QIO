"""诊断探针：打印互动接口的载荷形状（排错用，不属于产品代码）。

用法：
    cd backend
    uv run --frozen python ..\scripts\interactive-real-model-verify\probe_api.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_verify as rv  # noqa: E402


def main() -> int:
    backend = rv.LiveBackend()
    backend.start()
    try:
        http = rv.Http(backend.port)
        s = rv.new_session(backend, http, "probe")
        st, body = s.state()
        print("state keys:", sorted(body.keys()), "seq=", body.get("seq"))

        note = rv._card("c_note", "text", "探针注释", checked=True)
        print("PUT /state:", s.put_state(rv._state([note]))[0])
        print("draft put:", http("PUT", f"/api/interactive/drafts/{s.board_id}", {"drafts": {"chat": "x"}}))
        print("draft get:", http("GET", f"/api/interactive/drafts/{s.board_id}"))

        st, created = s.demo_intents()
        print("create demo:", st)
        st, listed = s.intents()
        print("list keys:", sorted((listed or {}).keys()))
        for it in (listed or {}).get("intents") or []:
            print(
                "  intent:",
                {k: it.get(k) for k in ("id", "title", "status", "dependsOn", "conflictsWith")},
            )
        print("conflicts:", (listed or {}).get("conflicts"))
        return 0
    finally:
        backend.stop()


if __name__ == "__main__":
    raise SystemExit(main())
