"""A 组反例/验证共用装置：最小 AttachmentService 桩 + 资源受限子进程探针。

装置自身的资源保护（不把无界资源耗尽留给主测试进程）：
* 子进程有墙钟超时；
* 探针用 tracemalloc 记录 Python 分配峰值（父进程据此断言）；
* 探针尽力施加内存上限（POSIX: RLIMIT_AS；Windows: Job Object，失败只影响该尽力项，
  墙钟超时与峰值断言仍然生效）。

本模块只服务 A 组用例（F01/F02/F21/F22/F23），不进入产品代码。

恢复说明（2026-10-09）：本文件曾因整理临时文件被误删；现由 __pycache__ 中的
_acc_a_probe.cpython-311.pyc 反汇编重建（STUB 与 run_probe 逻辑逐句还原），
已用 A 组用例实际复跑确认等价。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


STUB = "\nimport asyncio, json, os, sys, tracemalloc\n\n\ndef _cap():\n    \"\"\"尽力给探针进程加内存上限；失败不影响结论。\"\"\"\n    try:\n        if sys.platform == \"win32\":\n            from agent.tools import isolation\n            job = isolation._create_job()\n            isolation._assign_job(job, os.getpid())\n        else:\n            import resource\n            limit = 768 * 1024 * 1024\n            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))\n    except Exception:\n        pass\n\n\ndef _svc(path, name):\n    from agent.services.attachments import Attachment\n\n    att = Attachment(\n        id=\"att_probe\", message_id=None, turn_id=\"t\", topic_id=\"t\", kind=\"copy\",\n        original_name=name, stored_path=str(path), source_path=None,\n        size_bytes=os.path.getsize(path), mtime=None, sha256=None,\n        state=\"ready\", error=None, created_at=\"\", updated_at=\"\",\n    )\n\n    class _Svc:\n        def get(self, aid):\n            return att if aid == att.id else None\n\n        def availability(self, att):\n            return {\"readable_by_tool\": True}\n\n        def list(self, **kw):\n            return []\n\n    return att, _Svc()\n\n\ndef main():\n    path = sys.argv[1]\n    name = sys.argv[2]\n    offset = int(sys.argv[3]) if len(sys.argv) > 3 else 0\n    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 1\n    fragment = int(sys.argv[5]) if len(sys.argv) > 5 else 0\n    _cap()\n    from agent.tools.attachment_tools import ReadAttachmentTool\n\n    att, svc = _svc(path, name)\n    tool = ReadAttachmentTool(svc, active_turn_id=lambda: \"t\")\n    tracemalloc.start()\n    res = asyncio.run(\n        tool.run(attachment_id=att.id, offset=offset, limit=limit, fragment_offset=fragment)\n    )\n    _, peak = tracemalloc.get_traced_memory()\n    tracemalloc.stop()\n    print(\"PROBE_JSON:\" + json.dumps({\n        \"peak\": peak, \"ok\": res.ok, \"error\": res.error, \"content\": res.content,\n    }, ensure_ascii=False))\n\n\nif __name__ == \"__main__\":\n    main()\n"


def run_probe(
    tmp_path: Path,
    path: str,
    name: str,
    *,
    offset: int = 0,
    limit: int = 1,
    fragment: int = 0,
    timeout: float = 180.0,
) -> dict:
    import agent

    src = str(Path(agent.__file__).resolve().parents[1])
    probe = tmp_path / "_probe_acc_a.py"
    probe.write_text(STUB, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run(
        [
            sys.executable,
            str(probe),
            str(path),
            name,
            str(offset),
            str(limit),
            str(fragment),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if done.returncode != 0:
        raise AssertionError(
            f"探针退出码 {done.returncode}\nSTDOUT:\n{done.stdout[-2000:]}\nSTDERR:\n{done.stderr[-3000:]}"
        )
    for line in done.stdout.splitlines():
        if line.startswith("PROBE_JSON:"):
            return json.loads(line[len("PROBE_JSON:") :])
    raise AssertionError(
        f"探针没有输出结果：\nSTDOUT:\n{done.stdout[-2000:]}\nSTDERR:\n{done.stderr[-2000:]}"
    )
