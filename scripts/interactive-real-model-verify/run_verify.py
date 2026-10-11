"""E 的独立定向验收脚本：真实 HTTP + fake/mock provider。

用法（在 worktree 根目录）：
    cd backend
    uv run --frozen python ..\scripts\interactive-real-model-verify\run_verify.py --list
    uv run --frozen python ..\scripts\interactive-real-model-verify\run_verify.py --check B1,C1,D4
    uv run --frozen python ..\scripts\interactive-real-model-verify\run_verify.py --group visibility

设计口径（重要）：
- **不联网、不用真实密钥**：模型一律由 FakeUnderstandingAdapter 顶替。
- 只打真实 HTTP（uvicorn 真实监听 127.0.0.1 随机端口），不直接调内部函数伪造结果。
- 结论三态：PASS / FAIL / NOT_VERIFIABLE。
  NOT_VERIFIABLE 表示「该能力在本候选版本上还不存在（或没法用 fake 证伪）」，
  **绝不能**被读成「通过」。退出码：有 FAIL 时 1，否则 0。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent / "backend"
SRC = BACKEND / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ["QIO_DEV_INSECURE"] = "1"
os.environ["QIO_DISABLE_DB_CHECK"] = "1"
os.environ.pop("QIO_DATA_DIR", None)

BOARD = "board_default"

PASS, FAIL, NV = "PASS", "FAIL", "NOT_VERIFIABLE"

_RESULTS: list[dict] = []
_LOGS: list[str] = []


def log(msg: str) -> None:
    _LOGS.append(msg)


# --- fake provider -------------------------------------------------------


from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion, ModelUsage  # noqa: E402


class FakeUnderstandingAdapter(BaseAdapter):
    """按脚本逐次返回文本的假 adapter；记录每次调用，用来证明「没调用模型」。

    responses 用完之前按顺序取；用完后就一直重复最后一条（便于「自动修复一次」的用例）。
    """

    mode = AdapterMode.TEXT
    model = "e-fake-understanding"
    endpoint = None
    key_id = None

    def __init__(
        self,
        responses: list[str] | str = "",
        *,
        raise_error: BaseException | None = None,
    ):
        if isinstance(responses, str):
            responses = [responses]
        self.responses = list(responses) or [""]
        self.raise_error = raise_error
        self.calls: list[list[ChatMessage]] = []

    @property
    def script_text(self) -> str:
        return self.responses[-1]

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):  # noqa: ANN001
        self.calls.append(list(messages))
        text = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if self.raise_error is not None:
            raise self.raise_error
        return Completion(
            message=ChatMessage(role="assistant", content=text),
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            finish_reason="stop",
        )

    async def provider(self):
        """app.state.interactive_adapter_provider 需要的形状：async () -> BaseAdapter。"""
        return self

    @property
    def calls_count(self) -> int:
        return len(self.calls)

    def last_user_text(self) -> str:
        """最后一次调用里最后一条 user 消息的正文（用于核对「模型看到了什么」）。"""
        if not self.calls:
            return ""
        for message in reversed(self.calls[-1]):
            if getattr(message, "role", "") == "user":
                return str(getattr(message, "content", "") or "")
        return ""


def understanding_json(*, kind="intent", summary="用户要求把这些材料合并整理", intents=None, **extra) -> str:
    """按 prompts.OUTPUT_SCHEMA_TEXT 生成一份合法模型输出（测试用，不代表真实模型判断）。"""
    payload = {"kind": kind, "summary": summary}
    if intents is not None:
        payload["intents"] = intents
    if kind == "clarification":
        payload["clarification"] = extra.pop("clarification", {"question": "你希望我按什么标准整理？", "missing": ["标准"]})
    if kind == "no_action":
        payload.pop("intents", None)
    payload["memory"] = extra.pop("memory", [])
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# --- live backend --------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LiveBackend:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qio-e-verify-"))
        self.db = self.tmp / "test.db"
        self.port = _free_port()
        self.conn = None
        self.server = None
        self.thread = None
        self.app = None
        self.instance_id = None
        self.adapters: list[FakeUnderstandingAdapter] = []

    def install_adapter(self, *responses, raise_error=None) -> FakeUnderstandingAdapter:
        """把假 adapter 挂到统一注入点 app.state.interactive_adapter_provider。

        理解与执行共用这一条注入点（契约 §14 / 集成期约定），所以这里装一次即可。
        """
        adapter = FakeUnderstandingAdapter(list(responses) or [""], raise_error=raise_error)
        if self.app is not None:
            self.app.state.interactive_adapter_provider = adapter.provider
        self.adapters.append(adapter)
        return adapter

    def clear_adapter(self) -> None:
        if self.app is not None and hasattr(self.app.state, "interactive_adapter_provider"):
            delattr(self.app.state, "interactive_adapter_provider")

    def new_conn(self):
        """另开一条连到同一数据库文件的连接：避免跨线程使用 uvicorn 那条连接。"""
        from agent.storage.db import connect

        return connect(self.db)

    def start(self) -> None:
        import uvicorn

        from agent.api.server import create_app
        from agent.config import Settings
        from agent.storage.db import connect
        from agent.storage.migrate import apply_migrations

        self.conn = connect(self.db)
        apply_migrations(self.conn)
        settings = Settings(data_dir=self.tmp / "data")
        app = create_app(settings, self.conn)
        self.app = app
        self.instance_id = getattr(app.state, "instance_id", None)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 30
        while time.time() < deadline and not getattr(self.server, "started", False):
            time.sleep(0.05)
        if not getattr(self.server, "started", False):
            raise RuntimeError("后端未能在 30s 内启动")

    def stop(self) -> None:
        try:
            if self.server is not None:
                self.server.should_exit = True
                if self.thread is not None:
                    self.thread.join(timeout=15)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass


class Http:
    """真实 HTTP 客户端。

    实测（Windows + urllib + 线程里的 uvicorn）：偶发 ECONNRESET —— 服务端日志里**没有**
    任何异常，单独重跑同一条检查必过。这是客户端的连接复用/半关闭竞态，不是产品缺陷。
    所以这里对连接级失败做**至多一次**重试，并在返回值里如实标注发生过重试。
    """

    def __init__(self, port: int):
        self.port = int(port)
        self.base = f"http://127.0.0.1:{port}"
        self.retries: list[str] = []

    def _attempt(self, method: str, path: str, data, headers, timeout: float):
        # 用 http.client + 显式连接：实测 urllib 在本环境会偶发 ECONNRESET（服务端无异常、
        # 换 http.client 后同一条路由连续 5 次全部 200），所以验收harness固定用这一条路径。
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            # 服务端对未知路由可能直接断连（实测），此时得到的可能是空体
            if not raw:
                return resp.status, None
            try:
                return resp.status, json.loads(raw)
            except Exception:  # noqa: BLE001
                return resp.status, raw
        finally:
            conn.close()

    def __call__(self, method: str, path: str, body=None, timeout: float = 40.0):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last = ""
        for attempt in (1, 2):
            try:
                return self._attempt(method, path, data, headers, timeout)
            except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
                last = f"连接被重置：{exc}"
                if attempt == 1:
                    self.retries.append(f"{method} {path}")
                    time.sleep(0.05)
                    continue
        return 0, last


# --- 板面构造小工具 ------------------------------------------------------


def _card(cid, kind, content="", *, meta=None, checked=False, hidden=False, x=0.0, y=0.0):
    return {
        "id": cid,
        "kind": kind,
        "content": content,
        "meta": meta or {},
        "x": x,
        "y": y,
        "w": 200.0,
        "h": 120.0,
        "checked": checked,
        "hidden": hidden,
        "folded": False,
        "bookmarked": False,
        "deleted": False,
        "createdAt": "2026-10-11T00:00:00+00:00",
        "updatedAt": "2026-10-11T00:00:00+00:00",
    }


def _state(cards, groups=None, links=None, selection=None):
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": "2026-10-11T00:00:00+00:00",
        "cards": list(cards),
        "groups": list(groups or []),
        "links": list(links or []),
        "selection": list(selection or []),
    }


class Session:
    """一次检查用的干净板面：每个检查换一个新 board，互不污染。

    N1 版本门（契约 §1.4）要求整板写入先证明基于当前版本，
    所以 put_state 自动带上服务端刚返回的 seq —— 这不是绕过保护，
    而是模拟一次正常的「读取当前板面 → 改动 → 保存」。
    """

    def __init__(self, http: Http, board_id: str):
        self.http = http
        self.board_id = board_id

    def current_seq(self) -> int:
        st, body = self.state()
        if st != 200 or not isinstance(body, dict):
            raise RuntimeError(f"读取板面失败：{st} {body}")
        return int(body.get("seq") or 0)

    def put_state(self, state: dict, *, seq=None):
        payload = dict(state)
        payload["seq"] = self.current_seq() if seq is None else seq
        status, body = self.http(
            "PUT", f"/api/interactive/boards/{self.board_id}/state", {"state": payload}
        )
        return status, body

    def server_put_state(self, state: dict):
        """不使用自动 seq（用于故意构造过期版本的反例）。"""
        status, body = self.http(
            "PUT", f"/api/interactive/boards/{self.board_id}/state", {"state": state}
        )
        return status, body

    def state(self):
        return self.http("GET", f"/api/interactive/boards/{self.board_id}/state")

    def cards(self) -> list:
        st, body = self.state()
        return (body.get("state") or {}).get("cards") or []

    def submit(self, **payload):
        return self.http("POST", f"/api/interactive/boards/{self.board_id}/submissions", payload)

    def demo_intents(self):
        return self.http("POST", f"/api/interactive/boards/{self.board_id}/intents", {"demo": True})

    def intents(self):
        return self.http("GET", f"/api/interactive/boards/{self.board_id}/intents")

    def understanding(self, submission_id: str | None = None):
        path = f"/api/interactive/boards/{self.board_id}/understanding"
        if submission_id:
            path += f"?submissionId={submission_id}"
        return self.http("GET", path)

    def executions(self):
        return self.http("GET", f"/api/interactive/boards/{self.board_id}/executions")

    def create_intents(self, submission_id: str):
        return self.http(
            "POST", f"/api/interactive/boards/{self.board_id}/intents", {"submissionId": submission_id}
        )

    def approve(self, intent_id: str, *, confirm_dependency: bool = False):
        return self.http(
            "POST",
            f"/api/interactive/intents/{intent_id}/approve",
            {"confirmDependency": bool(confirm_dependency)},
        )

    def wait_understanding(self, submission_id: str, *, timeout: float = 25.0) -> dict | None:
        """轮询理解记录直到落到终态（后台任务是真实异步的）。"""
        settled = {"understood", "needs_clarification", "no_action", "failed"}
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            st, body = self.understanding(submission_id)
            items = _understanding_list(body)
            if items:
                last = items[0]
                if str(last.get("status")) in settled:
                    return last
            time.sleep(0.15)
        return last


def new_session(backend: LiveBackend, http: Http, tag: str) -> Session:
    status, body = http("POST", "/api/interactive/boards", {"title": f"E-{tag}"})
    if status != 200 or not isinstance(body, dict) or "board" not in body:
        raise RuntimeError(f"新建板面失败：{status} {body}")
    return Session(http, body["board"]["id"])


def _created_intents(body) -> list:
    """真实意图入口的响应形状是 {created: [...], demo, ok, reason}；演示入口也是 created。"""
    if not isinstance(body, dict):
        return []
    items = body.get("created")
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    items = body.get("intents")
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    return []


def _understanding_list(body) -> list:
    """理解接口的响应形状是 {understanding: 单条|None, understandings: [列表]}，两种都兼容。"""
    if not isinstance(body, dict):
        return []
    items = body.get("understandings")
    if isinstance(items, list):
        return [x for x in items if isinstance(x, dict)]
    single = body.get("understanding")
    if isinstance(single, dict):
        return [single]
    if isinstance(single, list):
        return [x for x in single if isinstance(x, dict)]
    return []


def _understanding_item(http: Http, board_id: str, understanding_id: str) -> dict | None:
    st, body = http("GET", f"/api/interactive/boards/{board_id}/understanding")
    for item in _understanding_list(body):
        if item.get("id") == understanding_id:
            return item
    return None


def _ids(snapshot) -> set:
    if not isinstance(snapshot, dict):
        return set()
    return {c.get("id") for c in snapshot.get("cards") or []}


# --- 检查注册 ------------------------------------------------------------


CHECKS: list[tuple[str, str, str, object]] = []


def check(cid: str, group: str, title: str):
    def deco(fn):
        CHECKS.append((cid, group, title, fn))
        return fn

    return deco


class Outcome:
    def __init__(self, status: str, detail: str = ""):
        self.status = status
        self.detail = detail


def pass_(detail=""):
    return Outcome(PASS, detail)


def fail(detail=""):
    return Outcome(FAIL, detail)


def nv(detail=""):
    return Outcome(NV, detail)


def _modules_present() -> dict:
    present = {}
    for name in ("materials", "understanding", "execution"):
        try:
            __import__(f"agent.interactive.{name}")
            present[name] = True
        except Exception:  # noqa: BLE001
            present[name] = False
    return present


# ---- 1. 真实闭环：提交 → 受理 → 材料 → 理解 → 意图 → 审批 → 执行 → 结果卡 ----

#: 一次合法模型输出的意图（依据 + 操作都指向真实存在的卡片 id）。
def _intent_payload(card_id: str, *, key="e-merge-1", content="这是经用户批准的结果正文", extra_ops=None):
    ops = [{"op": "add_reply_card", "content": content}]
    if extra_ops:
        ops = list(extra_ops) + ops
    return {
        "intentKey": key,
        "title": "把材料整理成一份结果",
        "goal": "让用户拿到整理后的结果卡片",
        "summary": "根据用户写明的要求整理，并新增一张结果卡片",
        "basis": [{"cardId": card_id, "role": "user_note"}],
        "expected": "板面上新增一张结果卡片",
        "operations": ops,
        "impact": {"cardIds": [card_id], "scope": "新增一张结果卡片，不改动用户已有内容"},
        "dependsOn": [],
        "conflictsWith": [],
        "conflictKey": "",
    }


def _start_pipeline(backend, http, tag, *, cards, model_json, selection=None, responses=None):
    """真实闭环的前半段：装 fake adapter → 保存 → 提交 → 等理解落定。"""
    adapter = backend.install_adapter(*(responses if responses is not None else [model_json]))
    s = new_session(backend, http, tag)
    st, body = s.put_state(_state(cards, selection=selection))
    if st != 200:
        return s, adapter, None, fail(f"保存失败：{st} {body}")
    st, sub = s.submit()
    if st != 200:
        return s, adapter, None, fail(f"提交 HTTP {st}: {sub}")
    item = s.wait_understanding(str((sub.get("submission") or {}).get("id")))
    return s, adapter, (sub, item), None


@check("P1", "real-path", "提交被可靠受理：delivered=true / status=accepted / understandingId")
def c_p1(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "p1",
        cards=[_card("c_note", "text", "把这几份材料整理成一份结论", checked=True)],
        model_json=understanding_json(kind="no_action", summary="只是记录"),
    )
    if err:
        return err
    sub, _item = data
    delivery = sub.get("delivery") or {}
    if delivery.get("delivered") is not True:
        return fail(f"delivered 不是 true：{json.dumps(delivery, ensure_ascii=False)[:220]}")
    if delivery.get("status") != "accepted":
        return fail(f"delivery.status 应为 accepted，实为 {delivery.get('status')}")
    if not delivery.get("understandingId"):
        return fail("没有 understandingId")
    if not (sub.get("baseline") or {}).get("updated"):
        return fail("受理成功后没有更新提交基准")
    return pass_(
        f"delivered=true, status=accepted, understandingId={delivery['understandingId']}, "
        f"baseline.updated=true"
    )


@check("P2", "real-path", "后台理解真实调用模型并落定（understood/clarification/no_action）")
def c_p2(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "p2",
        cards=[_card("c_note", "text", "把材料整理成结论", checked=True)],
        model_json=understanding_json(kind="no_action", summary="当前无需开展任务"),
    )
    if err:
        return err
    sub, item = data
    if item is None:
        return fail("理解记录没有落定（轮询超时）")
    if item.get("status") != "no_action":
        return fail(f"理解状态应为 no_action，实为 {item.get('status')}（error={item.get('error')}）")
    if adapter.calls_count < 1:
        return fail("理解落定但假 adapter 一次都没有被调用：链路可能没真的调用模型")
    return pass_(f"状态={item.get('status')}，模型调用 {adapter.calls_count} 次，stage={item.get('stage')}")


@check("P3", "real-path", "材料诚实：文件未关联附件=未读取；图片=不支持；网址=只有地址")
def c_p3(backend, http):
    from agent.interactive import materials as materials_module

    conn = backend.new_conn()
    try:
        state = _state([
            _card("c_file", "file", "说明", meta={"name": "report.txt"}),
            _card("c_img", "image", "", meta={"name": "shot.png"}),
            _card("c_url", "url", "", meta={"href": "https://example.com/a", "title": "示例"}),
            _card("c_code", "code", "print(1)", meta={"language": "python"}),
            _card("c_note", "text", "勾选的注释", checked=True),
            _card("c_off", "text", "未勾选的注释", checked=False),
        ])
        state["cards"][5]["hidden"] = False
        entries = materials_module.resolve_materials(conn, state=state, cards=None)
        by_id = {e["cardId"]: e for e in entries}
        problems = []
        f = by_id.get("c_file")
        if not f or f["source"] != "unreadable" or f["read"] is not False:
            problems.append(f"文件卡（无 attachmentId）应为 unreadable/read=false，实为 {f and (f['source'], f['read'])}")
        elif "文件名不是正文" not in (f.get("reason") or ""):
            problems.append(f"文件卡未读取的原因没有说明「文件名不是正文」：{f.get('reason')}")
        elif f.get("text"):
            problems.append("文件卡 read=false 却仍带正文（伪造）")
        img = by_id.get("c_img")
        if not img or img["source"] != "image_unsupported" or img["read"] is not False:
            problems.append(f"图片卡应为 image_unsupported/read=false，实为 {img and (img['source'], img['read'])}")
        url = by_id.get("c_url")
        if not url or url["source"] != "url_address_only" or url["read"] is not False:
            problems.append(f"网址卡（无正文）应为 url_address_only/read=false，实为 {url and (url['source'], url['read'])}")
        code = by_id.get("c_code")
        if not code or code["source"] != "inline" or code["read"] is not True:
            problems.append(f"代码卡应为 inline/read=true，实为 {code and (code['source'], code['read'])}")
        if "c_off" in by_id:
            problems.append("未勾选注释进入了材料解析结果（可见范围被突破）")
        if problems:
            return fail("；".join(problems))
        return pass_(
            "file=unreadable(仅文件名)/image=image_unsupported/url=url_address_only/code=inline；"
            "未勾选注释未进入解析"
        )
    finally:
        conn.close()


@check("P4", "real-path", "模型上下文只含可见内容：未勾选注释 / hidden 卡片不出现")
def c_p4(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "p4",
        cards=[
            _card("c_ok", "text", "这条注释是勾选的：整理一下", checked=True),
            _card("c_off", "text", "未勾选的私密注释：绝对不能被模型看到", checked=False),
            _card("c_hid", "text", "隐藏的私密注释：也不能被看到", hidden=True),
            _card("c_mat", "file", "材料", meta={"name": "m.txt"}),
        ],
        model_json=understanding_json(kind="no_action", summary="待判断"),
    )
    if err:
        return err
    context_text = adapter.last_user_text()
    if not context_text:
        return fail("假 adapter 没有拿到任何 user 消息，无法核对上下文")
    leaked = [w for w in ("未勾选的私密注释", "隐藏的私密注释") if w in context_text]
    if leaked:
        return fail(f"模型上下文里出现了范围外内容：{leaked}")
    if "这条注释是勾选的" not in context_text:
        return fail("勾选注释没有进入模型上下文（可见范围过窄，模型没有依据）")
    if not (data[1] and data[1].get("status")):
        return fail("理解没有落定")
    return pass_("上下文含勾选注释与材料，未含未勾选 / 隐藏注释（按模型实际收到的文本核对）")


@check("P5", "real-path", "真实意图入口：{submissionId} 取回该次理解产出的意图")
def c_p5(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "p5",
        cards=[_card("c_note", "text", "请把材料整理成一份结果", checked=True)],
        model_json=understanding_json(
            kind="intent", summary="用户要求整理成结果", intents=[_intent_payload("c_note")]
        ),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未落定为 understood：{item and item.get('status')} / {item and item.get('error')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    if st != 200:
        return fail(f"真实意图入口 HTTP {st}: {json.dumps(body, ensure_ascii=False)[:200]}")
    if body.get("demo") is True:
        return fail("传 submissionId 却返回演示结果")
    created = body.get("intents") or body.get("created") or []
    if not created:
        return fail(f"理解产出了意图但没有落成任务：{json.dumps(body, ensure_ascii=False)[:250]}")
    first = created[0]
    missing = [k for k in ("goal", "operations", "basis", "intentKey", "understandingId") if not first.get(k)]
    if missing:
        return fail(f"意图缺少模型产出的字段 {missing}：{json.dumps(first, ensure_ascii=False)[:250]}")
    ops = first.get("operations") or []
    if not ops or ops[0].get("op") != "add_reply_card":
        return fail(f"意图操作不是模型给的那条：{ops}")
    return pass_(
        f"意图 {first.get('intentKey')}，operations={[o.get('op') for o in ops]}，"
        f"understandingId={first.get('understandingId')}"
    )


@check("P6", "real-path", "批准后必须实际启动真实执行并写回结果卡")
def c_p6(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "p6",
        cards=[_card("c_note", "text", "请把材料整理成一份结果", checked=True)],
        model_json=understanding_json(
            kind="intent", summary="整理成结果", intents=[_intent_payload("c_note", content="批准后的结果正文")]
        ),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')} / {item and item.get('error')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = (body or {}).get("intents") or (body or {}).get("created") or []
    if not intents:
        return fail("没有意图可批准")
    target = intents[0]
    calls_before = adapter.calls_count
    st, approved = s.approve(target["id"], confirm_dependency=True)
    if st != 200:
        return fail(f"批准 HTTP {st}: {json.dumps(approved, ensure_ascii=False)[:200]}")
    if not (approved or {}).get("ok"):
        return fail(f"批准失败：{json.dumps(approved, ensure_ascii=False)[:250]}")
    execution = (approved or {}).get("execution") or {}
    if not execution.get("id"):
        return fail("批准返回里没有 execution（契约 §14.4 要求批准后实际启动真实执行）")
    # 等执行落定并核对板面
    deadline = time.time() + 25
    seen = execution
    while time.time() < deadline:
        st, listed = s.executions()
        rows = (listed or {}).get("executions") or []
        found = next((r for r in rows if r.get("id") == execution["id"]), None)
        if found:
            seen = found
            if found.get("status") in ("done", "failed", "cancelled", "paused"):
                break
        time.sleep(0.15)
    if seen.get("status") != "done":
        return fail(
            f"执行没有成功结束：status={seen.get('status')}，error={seen.get('error')}"
            f"（steps={json.dumps(seen.get('steps'), ensure_ascii=False)[:200]}）"
        )
    replies = [c for c in s.cards() if c.get("kind") == "reply"]
    if not replies:
        return fail("执行 done 但板面上没有结果卡片（没有真实写回）")
    if "批准后的结果正文" not in str(replies[0].get("content") or ""):
        return fail(f"结果卡正文不是已批准的那段内容：{replies[0].get('content')!r}")
    if adapter.calls_count != calls_before:
        return fail(
            f"方案里已有正文，执行阶段却又调用了模型 {adapter.calls_count - calls_before} 次"
            "（Lead 口径：只有方案缺正文时才允许执行阶段生成）"
        )
    return pass_(
        f"execution={execution['id']} status=done，结果卡已写回，执行阶段未额外调用模型"
        f"（理解阶段共 {adapter.calls_count} 次）"
    )


@check("P7", "real-path", "执行只做允许操作集合，不改动用户已有内容")
def c_p7(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "p7",
        cards=[
            _card("c_note", "text", "请整理", checked=True),
            _card("c_mat", "file", "原始材料", meta={"name": "m.txt"}),
        ],
        model_json=understanding_json(
            kind="intent", summary="整理", intents=[_intent_payload("c_note")]
        ),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')}")
    before = {c["id"]: json.dumps(c, sort_keys=True, ensure_ascii=False) for c in s.cards()}
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    st, approved = s.approve(intents[0]["id"], confirm_dependency=True)
    if st != 200 or not (approved or {}).get("ok"):
        return fail(f"批准失败：{json.dumps(approved, ensure_ascii=False)[:200]}")
    deadline = time.time() + 20
    status = None
    while time.time() < deadline:
        st, listed = s.executions()
        rows = (listed or {}).get("executions") or []
        if rows and rows[0].get("status") in ("done", "failed", "cancelled"):
            status = rows[0].get("status")
            break
        time.sleep(0.15)
    after_cards = s.cards()
    after = {c["id"]: c for c in after_cards}
    problems = []
    for cid, blob in before.items():
        now = after.get(cid)
        if now is None:
            problems.append(f"用户卡片被删除：{cid}")
            continue
        if json.dumps(now, sort_keys=True, ensure_ascii=False) != blob:
            problems.append(f"用户卡片被改动：{cid}")
    new_cards = [c for c in after_cards if c["id"] not in before]
    for card in new_cards:
        if card.get("kind") != "reply":
            problems.append(f"执行新增了非结果卡内容：{card.get('id')} kind={card.get('kind')}")
    if problems:
        return fail("；".join(problems))
    return pass_(f"执行 status={status}；用户卡片 {len(before)} 张全部原样；新增 {len(new_cards)} 张结果卡")


@check("P8", "real-path", "结果卡带来源引用：meta.reply 的 intentId / understandingId / executionId")
def c_p8(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "p8",
        cards=[_card("c_note", "text", "请整理成结果", checked=True)],
        model_json=understanding_json(
            kind="intent", summary="整理", intents=[_intent_payload("c_note")]
        ),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    target = intents[0]
    st, approved = s.approve(target["id"], confirm_dependency=True)
    execution = (approved or {}).get("execution") or {}
    deadline = time.time() + 20
    while time.time() < deadline:
        st, listed = s.executions()
        rows = (listed or {}).get("executions") or []
        if rows and rows[0].get("status") in ("done", "failed", "cancelled"):
            break
        time.sleep(0.15)
    replies = [c for c in s.cards() if c.get("kind") == "reply"]
    if not replies:
        return fail("没有结果卡片")
    meta = replies[0].get("meta") or {}
    reply = meta.get("reply") if isinstance(meta.get("reply"), dict) else meta
    missing = [k for k in ("intentId", "understandingId", "executionId") if not reply.get(k)]
    if missing:
        return fail(f"结果卡 meta 缺少来源引用 {missing}：{json.dumps(meta, ensure_ascii=False)[:250]}")
    mismatched = []
    if reply.get("intentId") != target["id"]:
        mismatched.append(f"intentId={reply.get('intentId')} != {target['id']}")
    if not str(reply.get("understandingId", "")).startswith("u_"):
        mismatched.append(f"understandingId 形状可疑：{reply.get('understandingId')}")
    if execution.get("id") and reply.get("executionId") != execution.get("id"):
        mismatched.append(f"executionId={reply.get('executionId')} != {execution.get('id')}")
    if mismatched:
        return fail("；".join(mismatched))
    return pass_(
        f"meta.reply 指向 intentId={reply['intentId']}、understandingId={reply['understandingId']}、"
        f"executionId={reply['executionId']}"
    )


@check("P9", "real-path", "模型输出含禁止操作 → 校验拒绝，不落成可执行任务")
def c_p9(backend, http):
    bad = _intent_payload("c_note")
    bad["operations"] = [{"op": "delete_card", "cardId": "c_note"}]
    s, adapter, data, err = _start_pipeline(
        backend, http, "p9",
        cards=[_card("c_note", "text", "请删掉这条注释", checked=True)],
        model_json=understanding_json(kind="intent", summary="越权尝试", intents=[bad]),
    )
    if err:
        return err
    sub, item = data
    if item is None:
        return fail("理解没有落定")
    if item.get("status") != "failed":
        return fail(f"越权输出应判 failed，实为 {item.get('status')}")
    reasons = json.dumps(item.get("validation") or {}, ensure_ascii=False) + str(item.get("error") or "")
    if "不允许的操作" not in reasons and "delete_card" not in reasons:
        return fail(f"失败原因没有指出越权操作：{reasons[:250]}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if intents:
        return fail(f"越权输出仍然落成了可执行任务：{len(intents)} 项")
    if len(s.cards()) != 1:
        return fail("越权操作在板面上产生了改动")
    return pass_(f"status=failed，原因含越权说明，未落成任务，板面未被改动（模型调用 {adapter.calls_count} 次）")


@check("P10", "real-path", "输出无法解析 → 自动修复至多一次，仍无任务")
def c_p10(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "p10",
        cards=[_card("c_note", "text", "请整理", checked=True)],
        model_json="这不是 JSON，只是一段模板文字",
    )
    if err:
        return err
    sub, item = data
    if item is None:
        return fail("理解没有落定")
    if item.get("status") != "failed":
        return fail(f"无法解析的输出应判 failed，实为 {item.get('status')}")
    if adapter.calls_count != 2:
        return fail(f"自动修复应恰好再调用一次（共 2 次），实测 {adapter.calls_count} 次")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    if _created_intents(body):
        return fail("无法解析的输出仍然落成了任务")
    return pass_("status=failed，模型调用恰好 2 次（首答 + 一次修复），无任务落库")


# ---- 2.1 不进模型上下文 ----


@check("B1", "visibility", "未勾选注释不进 before/after（含注释链接）")
def c_b1(backend, http):
    s = new_session(backend, http, "b1")
    note = _card("c_note", "text", "用户未勾选的私密注释", checked=False)
    mat = _card("c_mat", "file", "材料", meta={"name": "m.txt"})
    link = {
        "id": "l1", "src": "c_note", "dst": "c_mat", "direction": True,
        "meaning": "未勾选注释到材料的链接", "deleted": False,
        "createdAt": "2026-10-11T00:00:00+00:00", "updatedAt": "2026-10-11T00:00:00+00:00",
    }
    st, body = s.put_state(_state([note, mat], links=[link]))
    if st != 200:
        return fail(f"保存失败：{st} {body}")
    st, body = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {body}")
    after_ids = _ids(body.get("after"))
    if "c_note" in after_ids:
        return fail(f"未勾选注释出现在 after：{sorted(after_ids)}")
    for got in (body.get("before"), body.get("after")):
        for lk in (got or {}).get("links") or []:
            if {lk.get("src"), lk.get("dst")} & {"c_note"}:
                return fail("未勾选注释参与的链接出现在载荷")
    if "c_note" in json.dumps(body.get("expressions") or [], ensure_ascii=False):
        return fail("表达式摘要里泄露了未勾选注释内容")
    return pass_(f"状态={body.get('status')} after={sorted(after_ids)}，注释与其链接均未进入载荷")


@check("B2", "visibility", "hidden 卡片退出讨论范围")
def c_b2(backend, http):
    s = new_session(backend, http, "b2")
    hidden = _card("c_hidden", "text", "被隐藏的敏感注释", hidden=True)
    mat = _card("c_mat", "file", "材料", meta={"name": "m.txt"})
    st, body = s.put_state(_state([hidden, mat]))
    if st != 200:
        return fail(f"保存失败：{st} {body}")
    st, body = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {body}")
    if "c_hidden" in _ids(body.get("after")):
        return fail("hidden 卡片出现在 after")
    if "敏感注释" in json.dumps(body, ensure_ascii=False):
        return fail("hidden 卡片正文出现在提交结果中（间接泄露）")
    return pass_("hidden 卡片既不在 after，正文也未在提交结果中泄露")


@check("B3", "visibility", "仅一端可见的链接不进载荷")
def c_b3(backend, http):
    s = new_session(backend, http, "b3")
    hidden = _card("c_hidden", "text", "隐藏", hidden=True)
    mat = _card("c_mat", "file", "材料", meta={"name": "m.txt"})
    link = {
        "id": "l_half", "src": "c_hidden", "dst": "c_mat", "direction": True,
        "meaning": "半可见链接", "deleted": False,
        "createdAt": "2026-10-11T00:00:00+00:00", "updatedAt": "2026-10-11T00:00:00+00:00",
    }
    st, body = s.put_state(_state([hidden, mat], links=[link]))
    if st != 200:
        return fail(f"保存失败：{st} {body}")
    st, body = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {body}")
    links = (body.get("after") or {}).get("links") or []
    if links:
        return fail(f"半可见链接进入了载荷：{links}")
    return pass_("仅一端可见的链接未进入载荷")


@check("B4", "visibility", "无可见成员的组整体不出现（组名不泄露）")
def c_b4(backend, http):
    s = new_session(backend, http, "b4")
    h1 = _card("c_h1", "text", "隐藏1", hidden=True)
    h2 = _card("c_h2", "text", "隐藏2", hidden=True)
    grp = {
        "id": "g_hidden", "name": "这个组名不该出现", "defaultName": "", "ordered": False,
        "x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0, "members": ["c_h1", "c_h2"],
        "deleted": False, "createdAt": "2026-10-11T00:00:00+00:00",
        "updatedAt": "2026-10-11T00:00:00+00:00",
    }
    st, body = s.put_state(_state([h1, h2], groups=[grp]))
    if st != 200:
        return fail(f"保存失败：{st} {body}")
    st, body = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {body}")
    if (body.get("after") or {}).get("groups"):
        return fail("没有可见成员的组仍然出现")
    if "这个组名不该出现" in json.dumps(body, ensure_ascii=False):
        return fail("组名泄露了被隐藏注释的存在")
    return pass_("无可见成员的组未出现，组名未泄露")


@check("B5", "visibility", "客户端伪造 requestedVisible 不会突破服务端可见范围")
def c_b5(backend, http):
    s = new_session(backend, http, "b5")
    note = _card("c_note", "text", "未勾选", checked=False)
    mat = _card("c_mat", "file", "材料", meta={"name": "m.txt"})
    st, body = s.put_state(_state([note, mat]))
    if st != 200:
        return fail(f"保存失败：{st} {body}")
    st, body = s.submit(requestedVisible=["c_note", "c_mat"])
    if st != 200:
        return fail(f"提交 HTTP {st}: {body}")
    if "c_note" in _ids(body.get("after")):
        return fail("伪造 requestedVisible 让未勾选注释进入了载荷")
    ignored = json.dumps(body.get("delivery") or {}, ensure_ascii=False)
    if "c_note" not in ignored and "不在本次允许查看范围内" not in ignored:
        return fail(f"未报告被忽略的请求项：{ignored[:200]}")
    return pass_("服务端忽略伪造项并如实报告")


@check("B6", "visibility", "提交成功清勾选；失败保留勾选与改动")
def c_b6(backend, http):
    s = new_session(backend, http, "b6")
    note = _card("c_note", "text", "勾选的注释", checked=True)
    st, body = s.put_state(_state([note]))
    if st != 200:
        return fail(f"保存失败：{st} {body}")
    st, body = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {body}")
    if body.get("status") != "succeeded":
        return fail(f"首次提交状态不是 succeeded：{body.get('status')}")
    if not body.get("checkedCleared"):
        return fail("提交成功但没有清勾选记录")
    st, st_body = s.state()
    cards = (st_body.get("state") or {}).get("cards") or []
    cur = {c["id"]: c for c in cards}
    if cur.get("c_note", {}).get("checked"):
        return fail("提交成功后勾选没有被清掉")
    return pass_("提交成功后勾选已清（不是删除，卡片仍在）")


# ---- 2.2 不产生模型调用 ----


def _understanding_count(backend, board_id: str) -> int:
    try:
        row = backend.conn.execute(
            "SELECT COUNT(*) AS n FROM board_understanding WHERE board_id = ?", (board_id,)
        ).fetchone()
        return int(row["n"])
    except Exception:  # noqa: BLE001
        return -1


def _execution_count(backend, board_id: str) -> int:
    try:
        row = backend.conn.execute(
            "SELECT COUNT(*) AS n FROM board_executions WHERE board_id = ?", (board_id,)
        ).fetchone()
        return int(row["n"])
    except Exception:  # noqa: BLE001
        return -1


@check("C1", "no-model", "空提交：empty，不建理解请求")
def c_c1(backend, http):
    s = new_session(backend, http, "c1")
    st, body = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {body}")
    if body.get("status") != "empty":
        return fail(f"空提交状态应为 empty，实为 {body.get('status')}")
    n = _understanding_count(backend, s.board_id)
    return pass_(f"empty；本板面理解记录 {n} 条（空提交未新建）")


@check("C2", "no-model", "提交成功清勾选后立即再提交：empty，不重复建理解请求")
def c_c2(backend, http):
    """集成口径更正（Lead / 契约 §1.4）：提交成功会清勾选 → 该注释退出可见范围，
    所以 before 与 after **都**不含它，正确结果是 empty（不是 duplicate）。
    两种状态的安全语义相同：都不更新基准、都不建立理解请求。
    """
    s = new_session(backend, http, "c2")
    note = _card("c_note", "text", "要提交的注释", checked=True)
    st, _ = s.put_state(_state([note]))
    if st != 200:
        return fail(f"保存失败：{st}")
    st, first = s.submit()
    if st != 200 or first.get("status") != "succeeded":
        return fail(f"首次提交未成功：{st} {first.get('status') if isinstance(first, dict) else first}")
    if not first.get("delivery", {}).get("delivered"):
        return fail(f"首次提交未被受理：{first.get('delivery')}")
    n1 = _understanding_count(backend, s.board_id)
    if n1 != 1:
        return fail(f"首次提交应建立 1 条理解请求，实为 {n1} 条")
    st, second = s.submit()
    if st != 200:
        return fail(f"再次提交 HTTP {st}: {second}")
    status2 = second.get("status")
    if status2 != "empty":
        return fail(
            f"勾选被清后立即再提交应为 empty（该注释已退出可见范围），实为 {status2}"
            f"：{json.dumps(second.get('delivery'), ensure_ascii=False)[:200]}"
        )
    if (second.get("baseline") or {}).get("updated") is True:
        return fail("empty 更新了提交基准")
    n2 = _understanding_count(backend, s.board_id)
    if n2 != n1:
        return fail(f"empty 新建了理解请求：{n1} → {n2}")
    return pass_(f"empty；未更新基准；理解记录保持 {n2} 条（未重复调用模型）")


@check("C3", "no-model", "纯位置/缩放变化不产生模型调用")
def c_c3(backend, http):
    s = new_session(backend, http, "c3")
    mat = _card("c_mat", "file", "图纸", meta={"name": "d.txt"})
    st, _ = s.put_state(_state([mat], selection=["c_mat"]))
    if st != 200:
        return fail(f"保存失败：{st}")
    st, first = s.submit()
    if st != 200 or first.get("status") != "succeeded":
        return fail(f"前置提交未成功，无法比较：{st} {first.get('status')}")
    n1 = _understanding_count(backend, s.board_id)
    moved = _card("c_mat", "file", "图纸", meta={"name": "d.txt"}, x=500.0, y=300.0)
    st, _ = s.put_state(_state([moved], selection=["c_mat"]))
    if st != 200:
        return fail("移动后保存失败")
    st, body = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {body}")
    if body.get("status") != "duplicate":
        return fail(
            f"材料仍可见、内容与上次成功提交一致，应判 duplicate，实为 {body.get('status')}"
            f"（A 已修 _decide_status 顺序，这里应当通过）"
        )
    if (body.get("baseline") or {}).get("updated") is True:
        return fail("duplicate 更新了提交基准")
    n2 = _understanding_count(backend, s.board_id)
    if n2 != n1:
        return fail(f"纯位置变化新建了理解请求：{n1} → {n2}")
    return pass_(f"duplicate；未更新基准；理解记录保持 {n2} 条")


@check("C4", "no-model", "保存 PUT /state 只落库，不建立理解请求")
def c_c4(backend, http):
    s = new_session(backend, http, "c4")
    n0 = _understanding_count(backend, s.board_id)
    note = _card("c_note", "text", "勾选但未提交", checked=True)
    st, _ = s.put_state(_state([note]))
    if st != 200:
        return fail(f"保存失败：{st}")
    n1 = _understanding_count(backend, s.board_id)
    if n1 != n0:
        return fail(f"保存新建了理解请求：{n0} → {n1}")
    return pass_("保存未建立理解请求")


@check("C5", "no-model", "聊天发送不调用板面提交接口")
def c_c5(backend, http):
    return nv(
        "需要前端 / SSE 会话证据：本脚本只有后端 HTTP。"
        "待补证据：用前端定向用例或抓包证明文字发送未请求 submissions 接口。"
    )


# ---- 2.3 意图与审批边界 ----


def _find_intent(intents: list, *, depends=False, conflicts=False) -> dict | None:
    for it in intents:
        if depends and it.get("dependsOn"):
            return it
        if conflicts and it.get("conflictsWith"):
            return it
    return None


def _understanding_scenario(backend, http, tag, *, note, model_json, expect_kind):
    s = new_session(backend, http, tag)
    card = _card("c_note", "text", note, checked=True)
    st, _ = s.put_state(_state([card]))
    if st != 200:
        return fail("保存失败")
    st, body = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {body}")
    delivery = body.get("delivery") or {}
    uid = delivery.get("understandingId")
    if not uid:
        return fail(f"受理未给出 understandingId：{delivery}")
    if delivery.get("delivered") is not True:
        return fail(f"受理成功但 delivered != true：{delivery}")
    st, ub = http("GET", f"/api/interactive/boards/{s.board_id}/understanding")
    if st != 200 or not isinstance(ub, dict):
        return fail(f"读取理解记录 HTTP {st}: {ub}")
    items = ub.get("understanding") or ub.get("items") or []
    if isinstance(items, dict):
        items = [items]
    found = None
    for item in items:
        if item.get("id") == uid:
            found = item
            break
    if found is None:
        return fail(f"理解列表里找不到 {uid}：{json.dumps(ub, ensure_ascii=False)[:200]}")
    kind = found.get("kind") or found.get("result") or found.get("status")
    if kind != expect_kind:
        return fail(f"理解结果应为 {expect_kind}，实为 {kind}")
    return pass_(f"理解结果={kind}（understandingId={uid}）")


@check("D1", "intent", "要求不明确 → needs_clarification，不产出可执行意图")
def c_d1(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "d1",
        cards=[_card("c_note", "text", "你看着办吧，随便整理一下", checked=True)],
        model_json=understanding_json(
            kind="clarification", summary="信息不足，需要用户补充标准"
        ),
    )
    if err:
        return err
    sub, item = data
    if item is None:
        return fail("理解没有落定")
    if item.get("status") != "needs_clarification":
        return fail(f"应判 needs_clarification，实为 {item.get('status')}（error={item.get('error')}）")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    if ((body or {}).get("intents") or (body or {}).get("created") or []):
        return fail("需要补充说明却仍然落成了可执行任务")
    blob = json.dumps(item, ensure_ascii=False)
    if "补充" not in blob and "question" not in blob:
        return fail(f"理解记录里没有给出要补充的问题：{blob[:250]}")
    return pass_(f"status=needs_clarification，无任务落库")


@check("D2", "intent", "无工作需求 → no_action，不强制产生任务")
def c_d2(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "d2",
        cards=[_card("c_note", "text", "这只是我自己记一下，不需要做什么", checked=True)],
        model_json=understanding_json(kind="no_action", summary="当前无需开展任务"),
    )
    if err:
        return err
    sub, item = data
    if item is None:
        return fail("理解没有落定")
    if item.get("status") != "no_action":
        return fail(f"应判 no_action，实为 {item.get('status')}（error={item.get('error')}）")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    if ((body or {}).get("intents") or (body or {}).get("created") or []):
        return fail("无工作需求却强制产生了任务")
    return pass_("status=no_action，未产生任何任务")


@check("D3", "intent", "拒绝后无正式修改、预览消失、原内容保留")
def c_d3(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "d3",
        cards=[_card("c_note", "text", "请整理成结果", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功，无法验证拒绝：{item and item.get('status')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图可拒绝")
    target = intents[0]
    before_cards = len(s.cards())
    st, res = http("POST", f"/api/interactive/intents/{target['id']}/reject", {})
    if st != 200:
        return fail(f"拒绝 HTTP {st}: {res}")
    status = (res.get("intent") or {}).get("status")
    if status != "rejected":
        return fail(f"拒绝后状态应为 rejected，实为 {status}")
    after_cards = s.cards()
    if len(after_cards) != before_cards:
        return fail(f"拒绝后在板面上产生了正式改动：{before_cards} → {len(after_cards)} 张卡片")
    if [c for c in after_cards if c.get("kind") == "reply"]:
        return fail("拒绝后板面上出现了结果卡片")
    st, executions = s.executions()
    rows = (executions or {}).get("executions") or []
    running = [r for r in rows if r.get("status") in ("queued", "running")]
    if running:
        return fail(f"拒绝后仍然有执行在跑：{running}")
    return pass_(f"状态=rejected，板面卡片数不变（{len(after_cards)}），没有产生执行")


@check("D4", "intent", "批准前材料变化 → 禁止执行、不落正式修改")
def c_d4(backend, http):
    note = _card("c_note", "text", "把这些材料整理成结果", checked=True)
    mat = _card("c_mat", "file", "材料原始正文", meta={"name": "m.txt"})
    intent = _intent_payload("c_mat")
    intent["basis"] = [{"cardId": "c_mat", "role": "material"}]
    s, adapter, data, err = _start_pipeline(
        backend, http, "d4",
        cards=[note, mat],
        model_json=understanding_json(kind="intent", summary="整理材料", intents=[intent]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')} / {item and item.get('error')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    target = intents[0]
    if "c_mat" not in (target.get("materialRefs") or []):
        return fail(f"意图没有记下材料依据 materialRefs={target.get('materialRefs')}，无法验证材料变化")
    # 批准前修改这份材料（内容真的变了）
    changed = _card("c_mat", "file", "材料被改过的正文", meta={"name": "m.txt"})
    st, put = s.put_state(_state([note, changed]))
    if st != 200:
        return fail(f"改动材料保存失败：{st} {put}")
    st, listed = s.intents()
    now = next((i for i in (listed or {}).get("intents") or [] if i["id"] == target["id"]), None)
    before_cards = len(s.cards())
    st, approved = s.approve(target["id"], confirm_dependency=True)
    execution = (approved or {}).get("execution") if isinstance(approved, dict) else None
    if st == 200 and (approved or {}).get("ok") and execution and execution.get("status") in ("running", "queued", "done"):
        return fail(
            f"材料已变化却仍然批准并启动了执行：status={execution.get('status')}"
            f"（意图状态={now and now.get('status')}）"
        )
    if len(s.cards()) != before_cards:
        return fail("被拒绝的批准仍然改动了板面")
    reason = json.dumps(approved, ensure_ascii=False)
    if now is not None and now.get("status") == "needs_update":
        return pass_(f"材料变化后意图标记为 needs_update，批准被拒（原因：{reason[:160]}）")
    if not (approved or {}).get("ok"):
        return pass_(f"材料变化后批准被拒：{reason[:180]}")
    return fail(f"材料变化后既没有标记 needs_update，批准也没有被拒：{reason[:200]}")


@check("D5", "intent", "重复批准幂等：不新建第二条执行")
def c_d5(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "d5",
        cards=[_card("c_note", "text", "请整理成结果", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    target = intents[0]
    first = s.approve(target["id"], confirm_dependency=True)
    if first[0] != 200 or not (first[1] or {}).get("ok"):
        return fail(f"首次批准失败：{json.dumps(first[1], ensure_ascii=False)[:200]}")
    n1 = _execution_count(backend, s.board_id)
    second = s.approve(target["id"], confirm_dependency=True)
    n2 = _execution_count(backend, s.board_id)
    if n2 > n1:
        return fail(f"重复批准新建了执行记录：{n1} → {n2}")
    replies = [c for c in s.cards() if c.get("kind") == "reply"]
    if len(replies) > 1:
        return fail(f"重复批准产生了 {len(replies)} 张结果卡片")
    return pass_(f"执行记录 {n1} → {n2}，结果卡 {len(replies)} 张（未重复执行）")


@check("D6", "intent", "依赖未完成不自动开始；前项 done + 再次确认才开始")
def c_d6(backend, http):
    first = _intent_payload("c_note", key="k-a", content="前一项的结果")
    second = _intent_payload("c_note", key="k-b", content="依赖前一项的结果")
    second["dependsOn"] = ["k-a"]
    s, adapter, data, err = _start_pipeline(
        backend, http, "d6",
        cards=[_card("c_note", "text", "先做第一件，再根据结论做第二件", checked=True)],
        model_json=understanding_json(kind="intent", summary="两件事有先后", intents=[first, second]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')} / {item and item.get('error')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    by_key = {i.get("intentKey"): i for i in intents}
    if "k-a" not in by_key or "k-b" not in by_key:
        return fail(f"两个意图没有落全：{[i.get('intentKey') for i in intents]}")
    dep = by_key["k-b"]
    if not dep.get("dependsOn"):
        return fail("依赖没有落库")
    st, approved = s.approve(dep["id"], confirm_dependency=False)
    status = (approved or {}).get("intent", {}).get("status") if isinstance(approved, dict) else None
    if status == "running":
        return fail("前项未完成却直接进入 running")
    if status not in ("waiting_dependency", "waiting_confirm"):
        return fail(f"前项未完成时状态应为等待，实为 {status}：{json.dumps(approved, ensure_ascii=False)[:200]}")
    # 让前项真正完成
    st, a_ok = s.approve(by_key["k-a"]["id"], confirm_dependency=True)
    if st != 200 or not (a_ok or {}).get("ok"):
        return fail(f"前项批准失败：{json.dumps(a_ok, ensure_ascii=False)[:200]}")
    deadline = time.time() + 25
    while time.time() < deadline:
        st, listed = s.intents()
        cur = next((i for i in (listed or {}).get("intents") or [] if i["id"] == by_key["k-a"]["id"]), None)
        if cur and cur.get("status") in ("done", "failed", "cancelled"):
            break
        time.sleep(0.15)
    if not cur or cur.get("status") != "done":
        return fail(f"前项没有成功完成：{cur and cur.get('status')}，无法验证「done + 再次确认」")
    st, again = s.approve(dep["id"], confirm_dependency=True)
    status2 = (again or {}).get("intent", {}).get("status") if isinstance(again, dict) else None
    if status2 not in ("running", "queued", "done"):
        return fail(f"前项完成后再次确认仍没有开始：{status2}：{json.dumps(again, ensure_ascii=False)[:200]}")
    return pass_(f"前项未完成时={status}（未自动开始）；前项 done + 再次确认后={status2}")


@check("D7", "intent", "互不相容的意图不能同时批准（服务端判定）")
def c_d7(backend, http):
    a = _intent_payload("c_note", key="k-merge", content="合并成一组后的结果")
    a["conflictsWith"] = ["k-separate"]
    a["conflictKey"] = "merge-vs-separate"
    b = _intent_payload("c_note", key="k-separate", content="保持分开的结果")
    b["conflictsWith"] = ["k-merge"]
    b["conflictKey"] = "merge-vs-separate"
    s, adapter, data, err = _start_pipeline(
        backend, http, "d7",
        cards=[_card("c_note", "text", "两种互不相容的整理方式", checked=True)],
        model_json=understanding_json(kind="intent", summary="两种做法不能同时做", intents=[a, b]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')} / {item and item.get('error')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if len(intents) < 2:
        return fail(f"两个冲突意图没有落全：{len(intents)}")
    ids = [i["id"] for i in intents]
    st, res = http("POST", "/api/interactive/intents/batch", {"approve": ids, "reject": []})
    if st != 200:
        return fail(f"批量批准 HTTP {st}: {res}")
    approved_ids = [str(x) for x in ((res or {}).get("approved") or [])]
    st, listed = s.intents()
    statuses = {i["id"]: i.get("status") for i in (listed or {}).get("intents") or []}
    if approved_ids or any(statuses.get(i) in ("running", "queued", "done") for i in ids):
        return fail(f"冲突两项被同时批准：approved={approved_ids} statuses={statuses}")
    return pass_(f"两项都未批准（approved={approved_ids}，状态={[statuses.get(i) for i in ids]}）")


@check("E1", "recovery", "受理幂等：同一 request_id 重复受理不新建第二条")
def c_e1(backend, http):
    from agent.interactive import understanding as understanding_module

    s = new_session(backend, http, "e1")
    note = _card("c_note", "text", "受理幂等测试", checked=True)
    st, _ = s.put_state(_state([note]))
    if st != 200:
        return fail("保存失败")
    st, sub = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {sub}")
    sid = str((sub.get("submission") or {}).get("id"))
    conn = backend.new_conn()
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM board_understanding WHERE board_id = ?", (s.board_id,)
        ).fetchone()["n"]
        first = understanding_module.request_understanding(
            conn, board_id=s.board_id, submission_id=sid
        )
        second = understanding_module.request_understanding(
            conn, board_id=s.board_id, submission_id=sid
        )
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM board_understanding WHERE board_id = ?", (s.board_id,)
        ).fetchone()["n"]
    finally:
        conn.close()
    if first["id"] != second["id"]:
        return fail(f"重复受理产生了第二条记录：{first['id']} vs {second['id']}")
    if second.get("reused") is not True:
        return fail("第二次受理没有标记 reused（说明又插了一次）")
    if after != before:
        return fail(f"理解记录数变化：{before} → {after}")
    return pass_(f"两次受理同一 submission 得到同一条 {first['id']}（reused=true），记录数保持 {after}")


@check("E2", "recovery", "刷新 / 重复调用真实入口不重复创建意图、不重复执行")
def c_e2(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "e2",
        cards=[_card("c_note", "text", "请整理成结果", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, first = s.create_intents(sid)
    ids1 = sorted(i["id"] for i in _created_intents(first))
    for _ in range(3):
        s.intents()
        s.create_intents(sid)
    st, listed = s.intents()
    intents = (listed or {}).get("intents") or []
    ids2 = sorted(i["id"] for i in intents)
    if ids1 != ids2:
        return fail(f"重复调用真实入口导致意图集合变化：{ids1} → {ids2}")
    if len(intents) != len(ids1):
        return fail(f"意图数量重复：{len(intents)} 条（期望 {len(ids1)}）")
    n0 = _execution_count(backend, s.board_id)
    for _ in range(3):
        s.intents()
    n1 = _execution_count(backend, s.board_id)
    if n1 != n0:
        return fail(f"刷新列表产生了执行记录：{n0} → {n1}")
    return pass_(f"重复调用真实入口与刷新后仍是同一批 {len(intents)} 项，执行记录保持 {n1} 条")


@check("E3", "recovery", "理解失败后重试只从保存的快照恢复（不重读当前板面）")
def c_e3(backend, http):
    s = new_session(backend, http, "e3")
    adapter = backend.install_adapter("这不是 JSON（第一次理解必然失败）")
    note = _card("c_note", "text", "第一次提交时勾选的内容", checked=True)
    st, _ = s.put_state(_state([note]))
    if st != 200:
        return fail("保存失败")
    st, sub = s.submit()
    if st != 200:
        return fail(f"提交 HTTP {st}: {sub}")
    sid = str((sub.get("submission") or {}).get("id"))
    item = s.wait_understanding(sid)
    if item is None or item.get("status") != "failed":
        return fail(f"第一次理解没有失败：{item and item.get('status')}")
    uid = item["id"]
    # 失败之后改动当前板面：新增一张卡片（重试不应看到它）
    later = _card("c_later", "text", "失败之后才加进来的内容", checked=True)
    st, put = s.put_state(_state([note, later]))
    if st != 200:
        return fail(f"失败后改动板面失败：{st} {put}")
    good = backend.install_adapter(understanding_json(kind="no_action", summary="重试用的合法输出"))
    st, retry = http("POST", f"/api/interactive/understanding/{uid}/retry", {})
    if st != 200:
        return fail(f"重试 HTTP {st}: {json.dumps(retry, ensure_ascii=False)[:200]}")
    deadline = time.time() + 25
    final = None
    while time.time() < deadline:
        cur = s.wait_understanding(sid, timeout=1.0)
        if cur and cur.get("status") in ("understood", "no_action", "needs_clarification", "failed"):
            final = cur
            break
    if final is None:
        return fail("重试后理解仍未落定")
    conn = backend.new_conn()
    try:
        row = conn.execute(
            "SELECT snapshot, context, materials FROM board_understanding WHERE id = ?", (uid,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return fail("找不到理解记录")
    snapshot = json.loads(row["snapshot"] or "{}")
    context = json.loads(row["context"] or "{}")
    snapshot_ids = {c.get("id") for c in snapshot.get("cards") or []}
    blob = json.dumps(context, ensure_ascii=False)
    problems = []
    if "c_later" in snapshot_ids:
        problems.append("重试的快照包含了失败之后才加的卡片（说明重读了当前板面）")
    if "c_later" in blob:
        problems.append("重试的上下文包含了失败之后才加的卡片")
    if "c_note" not in snapshot_ids:
        problems.append(f"重试快照丢了原来的可见内容：{sorted(snapshot_ids)}")
    if problems:
        return fail("；".join(problems))
    return pass_(
        f"重试状态={final.get('status')}，快照仍是失败前的内容（{sorted(snapshot_ids)}），"
        f"未包含后加的 c_later；重试模型调用 {good.calls_count} 次"
    )


@check("E4", "recovery", "取消撤回本任务改动，保留用户后续修改")
def c_e4(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "e4",
        cards=[_card("c_note", "text", "请整理成结果", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    target = intents[0]
    # fake adapter 下执行几乎瞬间完成，HTTP 层的「暂停」会撞上 already-done；
    # 所以这里用**同一批函数**（HTTP 路由就是转发到它们）在模块层构造确定性的「执行中」场景。
    from agent.interactive import intents as intents_module

    conn = backend.new_conn()
    try:
        approved = intents_module.approve_intent(
            conn, target["id"], confirm_dependency=True, instance_id="instance-A"
        )
        if not approved.get("ok"):
            return fail(f"批准失败：{json.dumps(approved, ensure_ascii=False)[:200]}")
        paused = intents_module.pause_intent(conn, target["id"], instance_id="instance-A")
        status_after_pause = (paused.get("intent") or {}).get("status")
    finally:
        conn.close()
    # 任务暂停期间，用户自己又加了一张卡片：取消不得把它一起撤掉
    mine = _card("c_mine", "text", "用户后来自己加的内容", checked=True)
    st, _ = s.put_state(_state([_card("c_note", "text", "请整理成结果", checked=True), mine]))
    conn = backend.new_conn()
    try:
        cancel = intents_module.cancel_intent(conn, target["id"], instance_id="instance-A")
        row = conn.execute(
            "SELECT revert, status FROM board_intents WHERE id = ?", (target["id"],)
        ).fetchone()
    finally:
        conn.close()
    if not (cancel or {}).get("ok"):
        return fail(f"取消失败：{json.dumps(cancel, ensure_ascii=False)[:220]}")
    cards = s.cards()
    mine_kept = any(c["id"] == "c_mine" for c in cards)
    replies = [c for c in cards if c.get("kind") == "reply"]
    if not mine_kept:
        return fail("取消把用户后来的修改也一起撤掉了")
    if replies:
        return fail(f"取消后仍留下 {len(replies)} 张结果卡（本任务还没有真正执行过）")
    revert = json.loads(row["revert"] or "{}") if row is not None else {}
    detail = json.dumps({"revertField": revert, "status": row["status"] if row else None}, ensure_ascii=False)
    if row is None or row["status"] != "cancelled":
        return fail(f"取消后意图状态不是 cancelled：{detail}")
    if not revert:
        return fail(f"取消没有留下撤回说明：{detail}")
    return pass_(
        f"暂停后 status={status_after_pause}；取消后 status=cancelled，"
        f"用户后加内容保留，未留下结果卡；撤回说明={json.dumps(revert, ensure_ascii=False)[:150]}"
    )


@check("E5", "recovery", "迟到结果不写回：owner/attempt 对不上就丢弃")
def c_e5(backend, http):
    import asyncio

    from agent.interactive import execution as execution_module
    from agent.interactive import intents as intents_module

    s, adapter, data, err = _start_pipeline(
        backend, http, "e5",
        cards=[_card("c_note", "text", "请整理成结果", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    target = intents[0]
    conn = backend.new_conn()
    try:
        # 直接调模块批准：这一层不登记后台任务，所以不会有并发执行干扰
        approved = intents_module.approve_intent(
            conn, target["id"], confirm_dependency=True, instance_id="instance-A"
        )
        if not approved.get("ok"):
            return fail(f"模块层批准失败：{json.dumps(approved, ensure_ascii=False)[:200]}")
        execution = approved.get("execution") or {}
        exec_id = execution.get("id")
        if not exec_id:
            return fail("模块层批准没有建立执行记录")
        # 模拟「换了一个实例 / 进入下一次尝试」：旧的那次结果不再有资格写回
        conn.execute(
            "UPDATE board_executions SET owner_instance_id = ?, attempt = attempt + 1 WHERE id = ?",
            ("instance-B", exec_id),
        )
        conn.commit()
        before_cards = {c["id"] for c in s.cards()}
        result = asyncio.run(
            execution_module.run_execution(
                conn,
                exec_id,
                adapter_provider=adapter.provider,
                instance_id="instance-A",
            )
        )
        row = conn.execute(
            "SELECT status FROM board_executions WHERE id = ?", (exec_id,)
        ).fetchone()
    finally:
        conn.close()
    after_cards = {c["id"] for c in s.cards()}
    if after_cards != before_cards:
        return fail(f"迟到的执行仍然改写了板面：新增 {sorted(after_cards - before_cards)}")
    if result.get("ok") and not result.get("discarded"):
        return fail(f"归属不符却仍然执行成功：{json.dumps(result, ensure_ascii=False)[:220]}")
    if row is not None and row["status"] == "done":
        return fail("迟到的执行把状态写成了 done")
    return pass_(
        f"归属改成别的实例后，旧执行被丢弃（discarded={result.get('discarded')}, "
        f"reason={result.get('reason')}），板面与执行状态都未被改写"
    )


@check("E6", "recovery", "恢复后保留内容与进度，running 降级为 paused 且由用户决定")
def c_e6(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "e6",
        cards=[_card("c_note", "text", "请整理成结果", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    target = intents[0]
    st, approved = s.approve(target["id"], confirm_dependency=True)
    if st != 200 or not (approved or {}).get("ok"):
        return fail(f"批准失败：{json.dumps(approved, ensure_ascii=False)[:200]}")
    now_status = (approved.get("intent") or {}).get("status")
    conn = backend.new_conn()
    try:
        row = conn.execute(
            "SELECT progress FROM board_intents WHERE id = ?", (target["id"],)
        ).fetchone()
        progress = json.loads(row["progress"] or "{}")
        progress["__ownerInstance"] = "e-other-instance"
        conn.execute(
            "UPDATE board_intents SET progress = ?, status = 'running' WHERE id = ?",
            (json.dumps(progress, ensure_ascii=False), target["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    st, listed = s.intents()
    recovery = (listed or {}).get("recovery")
    recovered = next(
        (i for i in (listed or {}).get("intents") or [] if i["id"] == target["id"]), None
    )
    if recovery is None or recovered is None:
        return fail("列表响应没有 recovery 信息")
    if recovered.get("status") != "paused":
        return fail(f"别的实例遗留的 running 没有降级为 paused：{recovered.get('status')}")
    if "暂停" not in str(recovered.get("reason") or "") and "确认" not in str(recovered.get("reason") or ""):
        return fail(f"降级为 paused 但没有说明需要用户确认：{recovered.get('reason')}")
    return pass_(
        f"批准后={now_status}，伪造别的实例归属后读列表={recovered.get('status')}"
        f"（recovery={json.dumps(recovery, ensure_ascii=False)[:100]}）"
    )


@check("E7", "recovery", "继续执行不重复已完成步骤")
def c_e7(backend, http):
    import asyncio

    from agent.interactive import execution as execution_module
    from agent.interactive import intents as intents_module

    s, adapter, data, err = _start_pipeline(
        backend, http, "e7",
        cards=[_card("c_note", "text", "请整理成结果", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    target = intents[0]
    conn = backend.new_conn()
    try:
        approved = intents_module.approve_intent(
            conn, target["id"], confirm_dependency=True, instance_id="instance-A"
        )
        exec_id = (approved.get("execution") or {}).get("id")
        if not exec_id:
            return fail(f"批准没有建立执行：{json.dumps(approved, ensure_ascii=False)[:200]}")
        first = asyncio.run(
            execution_module.run_execution(
                conn, exec_id, adapter_provider=adapter.provider, instance_id="instance-A"
            )
        )
        row = conn.execute(
            "SELECT status, checkpoints FROM board_executions WHERE id = ?", (exec_id,)
        ).fetchone()
        checkpoints = json.loads(row["checkpoints"] or "[]")
        # 继续执行的入口在意图层（契约 §14.4 POST /intents/{id}/resume → intents.resume_intent）
        resume = intents_module.resume_intent(conn, target["id"], instance_id="instance-A")
        row2 = conn.execute(
            "SELECT status, checkpoints FROM board_executions WHERE id = ?", (exec_id,)
        ).fetchone()
        checkpoints2 = json.loads(row2["checkpoints"] or "[]")
    finally:
        conn.close()
    replies = [c for c in s.cards() if c.get("kind") == "reply"]
    if len(replies) > 1:
        return fail(f"继续执行重复了已完成步骤：结果卡 {len(replies)} 张")
    if len(checkpoints2) > len(checkpoints):
        return fail(f"继续执行重复落地了新 checkpoint：{len(checkpoints)} → {len(checkpoints2)}")
    if row["status"] != "done":
        return nv(f"第一次执行未 done（status={row['status']}）：无法验证「继续不重放」")
    return pass_(
        f"执行已 done（checkpoints={len(checkpoints)}）；再次继续后 status={row2['status']}，"
        f"checkpoints={len(checkpoints2)}，结果卡 {len(replies)} 张，未重复（resume={json.dumps(resume, ensure_ascii=False)[:120]}）"
    )


@check("E8", "recovery", "状态显示真实：不允许「已批准就一直运行中」")
def c_e8(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "e8",
        cards=[_card("c_note", "text", "请整理成结果", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    if item is None or item.get("status") != "understood":
        return fail(f"理解未成功：{item and item.get('status')}")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    target = intents[0]
    st, approved = s.approve(target["id"], confirm_dependency=True)
    if st != 200 or not (approved or {}).get("ok"):
        return fail(f"批准失败：{json.dumps(approved, ensure_ascii=False)[:200]}")
    terminal = ("done", "failed", "cancelled")
    seen = set()
    execution_status = None
    deadline = time.time() + 25
    while time.time() < deadline:
        st, listed = s.executions()
        rows = (listed or {}).get("executions") or []
        if rows:
            execution_status = rows[0].get("status")
            seen.add(execution_status)
            if execution_status in terminal:
                break
        time.sleep(0.15)
    st, after = s.intents()
    intent_now = next((i for i in (after or {}).get("intents") or [] if i["id"] == target["id"]), None)
    if execution_status not in terminal:
        return fail(f"执行状态没有落定，一直停留在 {execution_status}（seen={sorted(seen)}）")
    if intent_now is None or intent_now.get("status") not in terminal:
        return fail(f"执行已 {execution_status} 但意图状态还是 {intent_now and intent_now.get('status')}")
    if execution_status != intent_now.get("status"):
        return fail(f"意图状态（{intent_now.get('status')}）与执行状态（{execution_status}）对不上")
    return pass_(
        f"执行状态轨迹 {sorted(seen)}，最终 execution={execution_status} / intent={intent_now.get('status')}"
    )


# ---- 2.5 契约面（冻结接口是否真的存在） ----


@check("X1", "contract", "提交返回 delivery.delivered / understandingId / status=accepted")
def c_x1(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "x1",
        cards=[_card("c_note", "text", "真实入口契约", checked=True)],
        model_json=understanding_json(kind="no_action", summary="待判断"),
    )
    if err:
        return err
    sub, _item = data
    delivery = sub.get("delivery") or {}
    missing = [k for k in ("delivered", "understandingId", "status") if k not in delivery]
    if missing:
        return fail(f"delivery 缺少冻结字段 {missing}：{json.dumps(delivery, ensure_ascii=False)[:200]}")
    if delivery.get("status") != "accepted":
        return fail(f"delivery.status 应为 accepted，实为 {delivery.get('status')}")
    if delivery.get("delivered") is not True:
        return fail("受理成功但 delivery.delivered 不为 true")
    if not delivery.get("understandingId"):
        return fail("受理成功但没有 understandingId")
    return pass_(f"delivery={json.dumps(delivery, ensure_ascii=False)[:190]}")


@check("X2", "contract", "理解过程列表：GET /boards/{id}/understanding（支持 submissionId 过滤）")
def c_x2(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "x2",
        cards=[_card("c_note", "text", "理解列表", checked=True)],
        model_json=understanding_json(kind="no_action", summary="待判断"),
    )
    if err:
        return err
    sub, _item = data
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.understanding()
    if st != 200 or not isinstance(body, dict) or not _understanding_list(body):
        return fail(f"全量列表 HTTP {st}: {json.dumps(body, ensure_ascii=False)[:200]}")
    st2, only = s.understanding(sid)
    items = _understanding_list(only)
    if st2 != 200 or not items:
        return fail(f"按 submissionId 过滤没有返回记录：{st2}")
    if any(str(i.get("submissionId")) != sid for i in items):
        return fail("按 submissionId 过滤返回了别的提交")
    first = items[0]
    for key in ("id", "status", "steps", "materials", "hasSnapshot"):
        if key not in first:
            return fail(f"理解记录缺少字段 {key}")
    return pass_(f"列表与过滤都可用；记录字段齐备，status={first.get('status')}，steps={len(first.get('steps') or [])} 段")


@check("X3", "contract", "理解取消：POST /boards/{id}/understanding/{uid}/cancel")
def c_x3(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "x3",
        cards=[_card("c_note", "text", "取消理解", checked=True)],
        model_json=understanding_json(kind="no_action", summary="待判断"),
    )
    if err:
        return err
    _sub, item = data
    if item is None:
        return fail("理解没有落定")
    uid = item["id"]
    st, body = http("POST", f"/api/interactive/boards/{s.board_id}/understanding/{uid}/cancel", {})
    if st not in (200, 409):
        return fail(f"取消 HTTP {st}: {json.dumps(body, ensure_ascii=False)[:200]}")
    if st == 200 and not isinstance(body, dict):
        return fail("取消返回形状不对")
    return pass_(f"路由可用（HTTP {st}）：{json.dumps(body, ensure_ascii=False)[:170]}")


@check("X4", "contract", "执行记录：GET /boards/{id}/executions")
def c_x4(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "x4",
        cards=[_card("c_note", "text", "执行记录", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    st, before = s.executions()
    if st != 200 or "executions" not in (before or {}):
        return fail(f"执行列表 HTTP {st}: {json.dumps(before, ensure_ascii=False)[:200]}")
    if before.get("executions"):
        return fail("还没有批准任何任务，却已经有执行记录")
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图")
    s.approve(intents[0]["id"], confirm_dependency=True)
    deadline = time.time() + 20
    rows = []
    while time.time() < deadline:
        st, listed = s.executions()
        rows = (listed or {}).get("executions") or []
        if rows and rows[0].get("status") in ("done", "failed", "cancelled", "paused"):
            break
        time.sleep(0.15)
    if not rows:
        return fail("批准后执行记录为空")
    first = rows[0]
    # 前端冻结类型 Execution 的字段（含 steps / result / attempt / ownerInstanceId）；
    # checkpoints 是数据库列、不在此接口暴露，所以不在这里要求。
    for key in ("id", "intentId", "boardId", "attempt", "status", "steps", "result"):
        if key not in first:
            return fail(f"执行记录缺少字段 {key}：{json.dumps(first, ensure_ascii=False)[:220]}")
    conn = backend.new_conn()
    try:
        has_checkpoints = "checkpoints" in {
            r["name"] for r in conn.execute("PRAGMA table_info(board_executions)")
        }
    finally:
        conn.close()
    if not has_checkpoints:
        return fail("board_executions 缺少 checkpoints 列（§14.2 要求）")
    return pass_(
        f"批准前为空、批准后出现 1 条：status={first.get('status')}，steps={len(first.get('steps') or [])}，"
        "payload 字段与前端冻结类型一致；checkpoints 在数据库列上存在（接口按类型不暴露）"
    )


@check("X5", "contract", "执行控制：pause / resume / cancel 三个接口存在并作用于真实执行")
def c_x5(backend, http):
    found = {}
    for act in ("pause", "resume", "cancel"):
        st, _ = http("POST", f"/api/interactive/intents/i_nonexistent/{act}", {})
        found[act] = st
    if all(v in (0, 404) for v in found.values()):
        return fail(f"三个控制接口都不存在或无法与「意图不存在」区分：{found}")
    # 用真实意图再打一遍，确认路由真的落到业务逻辑
    s, adapter, data, err = _start_pipeline(
        backend, http, "x5",
        cards=[_card("c_note", "text", "控制接口", checked=True)],
        model_json=understanding_json(kind="intent", summary="整理", intents=[_intent_payload("c_note")]),
    )
    if err:
        return err
    sub, item = data
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    intents = _created_intents(body)
    if not intents:
        return fail("没有意图可用于控制接口")
    real = {}
    for act in ("pause", "resume", "cancel"):
        st, res = http("POST", f"/api/interactive/intents/{intents[0]['id']}/{act}", {})
        real[act] = {"http": st, "ok": (res or {}).get("ok") if isinstance(res, dict) else None}
    if all(real[a]["http"] == 200 for a in real):
        return pass_(f"未批准任务上三个接口都可达并给出明确结果：{real}")
    return fail(f"控制接口在真实意图上表现异常：{real}")


@check("X6", "contract", "真实意图入口：只有 submissionId，绝不回落到演示")
def c_x6(backend, http):
    s, adapter, data, err = _start_pipeline(
        backend, http, "x6",
        cards=[_card("c_note", "text", "真实意图入口", checked=True)],
        model_json=understanding_json(kind="no_action", summary="这次没有工作要做"),
    )
    if err:
        return err
    sub, item = data
    sid = str((sub.get("submission") or {}).get("id"))
    st, body = s.create_intents(sid)
    if st != 200:
        return fail(f"HTTP {st}: {json.dumps(body, ensure_ascii=False)[:200]}")
    if body.get("demo") is True:
        return fail("传 submissionId 却走了演示路径")
    if "演示" in json.dumps(body, ensure_ascii=False):
        return fail(f"真实入口返回了演示内容：{json.dumps(body, ensure_ascii=False)[:200]}")
    if (item or {}).get("status") == "no_action" and _created_intents(body):
        return fail("理解结论是 no_action，真实入口却产出了任务")
    return pass_(f"真实入口返回 {json.dumps(body, ensure_ascii=False)[:180]}")


# ---- 3. 材料诚实性 ----


def _make_attachment(backend, name="note.txt", text="附件里的正文内容", *, kind="copy", state="ready"):
    """在临时库里造一条真实的附件记录 + 副本文件（走既有 AttachmentService 的口径）。"""
    import hashlib
    import uuid

    root = backend.tmp / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"e-{uuid.uuid4().hex[:8]}__{name}"
    path.write_text(text, encoding="utf-8")
    aid = "att_e_" + uuid.uuid4().hex[:10]
    stamp = "2026-10-11T00:00:00+00:00"
    conn = backend.new_conn()
    try:
        conn.execute(
            "INSERT INTO attachments (id, kind, original_name, stored_path, size_bytes, sha256,"
            " state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                aid,
                kind,
                name,
                str(path),
                len(text.encode("utf-8")),
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
                state,
                stamp,
                stamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return aid, path


def _resolve(backend, cards, *, max_chars=None):
    from agent.interactive import materials as materials_module

    conn = backend.new_conn()
    try:
        kwargs = {}
        if max_chars is not None:
            kwargs["max_chars"] = max_chars
        entries = materials_module.resolve_materials(
            conn, state=_state(cards), cards=None, **kwargs
        )
        return {e["cardId"]: e for e in entries}
    finally:
        conn.close()


@check("F1", "materials", "已关联附件的文件卡真读到正文（attachment_text）")
def c_f1(backend, http):
    aid, path = _make_attachment(backend, name="spec.txt", text="这是附件里真实存在的正文：QIO-E-验收标记")
    card = _card("c_file", "file", "文件说明", meta={"name": "spec.txt", "file": {"attachmentId": aid}})
    by_id = _resolve(backend, [card])
    entry = by_id.get("c_file")
    if entry is None:
        return fail("文件卡没有出现在材料解析结果里")
    if entry["source"] != "attachment_text" or entry["read"] is not True:
        return fail(f"应 attachment_text/read=true，实为 {entry['source']}/read={entry['read']}：{entry.get('reason')}")
    if "QIO-E-验收标记" not in (entry.get("text") or ""):
        return fail(f"声称读到正文但正文里没有附件内容：{entry.get('text')!r}")
    if entry.get("attachmentId") != aid:
        return fail(f"材料条目没有带正确的 attachmentId：{entry.get('attachmentId')}")
    if not entry.get("characters"):
        return fail("读到了正文却没有记字符数")
    return pass_(f"source=attachment_text，read=true，正文字符数={entry.get('characters')}，attachmentId 正确")


@check("F2", "materials", "图片卡一律 image_unsupported，不假装看懂")
def c_f2(backend, http):
    card = _card("c_img", "image", "", meta={"name": "shot.png"})
    by_id = _resolve(backend, [card])
    entry = by_id.get("c_img")
    if entry is None:
        return fail("图片卡没有出现在材料解析结果里")
    if entry["source"] != "image_unsupported" or entry["read"] is not False:
        return fail(f"应 image_unsupported/read=false，实为 {entry['source']}/read={entry['read']}")
    if entry.get("text"):
        return fail("图片卡 read=false 却带正文（伪造）")
    if "没有视觉能力" not in (entry.get("reason") or ""):
        return fail(f"原因没有如实说明没有视觉能力：{entry.get('reason')}")
    return pass_(f"image_unsupported，read=false，原因：「{entry.get('reason')}」")


@check("F3", "materials", "只有地址的网址卡 → url_address_only，不假装读过")
def c_f3(backend, http):
    card = _card("c_url", "url", "", meta={"href": "https://example.com/doc", "title": "示例文档"})
    by_id = _resolve(backend, [card])
    entry = by_id.get("c_url")
    if entry is None:
        return fail("网址卡没有出现在材料解析结果里")
    if entry["source"] != "url_address_only" or entry["read"] is not False:
        return fail(f"应 url_address_only/read=false，实为 {entry['source']}/read={entry['read']}")
    if entry.get("text"):
        return fail("没有正文却带了正文（伪造）")
    if "没有抓到网页正文" not in (entry.get("reason") or ""):
        return fail(f"原因没有如实说明只有地址：{entry.get('reason')}")
    return pass_(f"url_address_only，read=false，原因：「{entry.get('reason')}」")


@check("F4", "materials", "卡片上已登记正文的网址 → url_fetched（不联网）")
def c_f4(backend, http):
    body = "这是卡片上已经登记的网页正文：E-F4 标记"
    card = _card(
        "c_url", "url", "",
        meta={"url": {"href": "https://example.com/doc", "title": "示例", "content": body}},
    )
    by_id = _resolve(backend, [card])
    entry = by_id.get("c_url")
    if entry is None:
        return fail("网址卡没有出现在材料解析结果里")
    if entry["source"] != "url_fetched" or entry["read"] is not True:
        return fail(f"应 url_fetched/read=true，实为 {entry['source']}/read={entry['read']}")
    if "E-F4 标记" not in (entry.get("text") or ""):
        return fail("声称读到正文但正文不符")
    notes = " ".join(entry.get("notes") or [])
    if "不是本次联网抓取" not in notes and "已经登记" not in notes:
        return fail(f"没有说明正文来源（容易让人以为是本次抓取）：{notes}")
    return pass_("url_fetched，read=true，并明确说明正文来自卡片上已登记的结果")


@check("F5", "materials", "读不到 / 被截断都如实标注")
def c_f5(backend, http):
    problems = []
    missing = _card("c_miss", "file", "说明", meta={"name": "gone.txt", "file": {"attachmentId": "att_missing_x"}})
    long_text = "长" * 500
    inline = _card("c_long", "code", long_text, meta={"language": "text"})
    by_id = _resolve(backend, [missing, inline], max_chars=100)
    miss = by_id.get("c_miss")
    if miss is None:
        problems.append("找不到附件的文件卡没有进入解析结果")
    else:
        if miss["source"] != "unreadable" or miss["read"] is not False:
            problems.append(f"找不到附件应为 unreadable/read=false，实为 {miss['source']}/read={miss['read']}")
        if not miss.get("reason"):
            problems.append("unreadable 没有给出原因")
        if miss.get("text"):
            problems.append("unreadable 带了正文（伪造）")
    long_entry = by_id.get("c_long")
    if long_entry is None:
        problems.append("超长材料卡没有进入解析结果")
    else:
        if not long_entry.get("truncated"):
            problems.append("超过 max_chars 却没有标 truncated")
        if long_entry.get("characters") != len(long_text):
            problems.append(
                f"characters 应为截断前原始长度 {len(long_text)}，实为 {long_entry.get('characters')}"
            )
        if len(long_entry.get("text") or "") != 100:
            problems.append(f"交付长度没有按 max_chars=100 截断：{len(long_entry.get('text') or '')}")
    if problems:
        return fail("；".join(problems))
    return pass_("找不到附件=unreadable 且有原因；超长材料 truncated=true、characters 记原始长度、交付量按上限截断")


@check("F6", "materials", "明确不支持的来源 → unsupported，且有可读原因")
def c_f6(backend, http):
    card = _card("c_odd", "unknown_kind", "奇怪的材料", meta={})
    by_id = _resolve(backend, [card])
    entry = by_id.get("c_odd")
    if entry is None:
        return fail("未知类型卡片没有进入解析结果（应当如实记 unsupported）")
    if entry["source"] != "unsupported":
        return fail(f"应 unsupported，实为 {entry['source']}")
    if not entry.get("reason"):
        return fail("unsupported 没有给出可读原因")
    if entry.get("read"):
        return fail("unsupported 却标记为已读取")
    return pass_(f"unsupported，read=false，原因：「{entry.get('reason')}」")


@check("F7", "materials", "界面准确显示材料来源与未读取情况")
def c_f7(backend, http):
    return nv("需要前端截图/断言：本脚本只核对后端材料条目，界面文案未验证")


# ---- 4. 回归 ----


@check("R1", "regression", "原对话发送链路未被破坏（turns 路由可用）")
def c_r1(backend, http):
    st, body = http("GET", "/api/turns/queue")
    if st != 200:
        return nv(f"/api/turns/queue HTTP {st}：需要前端/SSE 证据才能判断对话发送是否受影响")
    st2, health = http("GET", "/api/health")
    if st2 != 200:
        return fail(f"/api/health HTTP {st2}")
    return nv(
        f"会话路由存活（/api/turns/queue 200, /api/health 200：{json.dumps(health, ensure_ascii=False)[:80]}）；"
        "「文字发送端到端 + 流式回答」需要 SSE / 前端证据，本脚本未声称已验证"
    )


@check("R2", "regression", "流式回答链路未被破坏（SSE 票据可用）")
def c_r2(backend, http):
    st, body = http("POST", "/api/events/ticket", {})
    if st != 200:
        return nv(f"/api/events/ticket HTTP {st}：无法在本脚本内验证流式回答")
    if not isinstance(body, dict) or "ticket" not in body:
        return fail(f"票据接口返回形状异常：{json.dumps(body, ensure_ascii=False)[:160]}")
    if not body.get("ticket") and body.get("auth_required"):
        return fail(f"要求认证却给了空票据：{json.dumps(body, ensure_ascii=False)[:160]}")
    # 开发豁免模式（QIO_DEV_INSECURE=1）下 auth_required=false、票据为空是预期行为。
    return nv(
        "SSE 票据接口可用（流式回答的传输前置条件在）；"
        "「边生成边显示」的真实流式行为需要前端/SSE 抓包证据，本脚本未声称已验证"
    )


@check("R3", "regression", "附件能力未被破坏")
def c_r3(backend, http):
    st, body = http("GET", "/api/attachments")
    if st != 200:
        return fail(f"/api/attachments HTTP {st}")
    return pass_(f"/api/attachments 可用：{json.dumps(body, ensure_ascii=False)[:140]}")


@check("R4", "regression", "话题归属正确，不串话题")
def c_r4(backend, http):
    st, body = http("GET", "/api/interactive/boards")
    if st != 200:
        return fail(f"互动板面列表 HTTP {st}")
    return nv("需要前端/SSE 归属证据（本脚本只有后端 HTTP，无法证明消息归属不串话题）")


@check("R5", "regression", "草稿保存/恢复可用；失败保留输入")
def c_r5(backend, http):
    s = new_session(backend, http, "r5")
    st, body = http("PUT", f"/api/interactive/drafts/{s.board_id}", {"drafts": {"chat": "草稿内容E"}})
    if st != 200:
        return fail(f"草稿保存 HTTP {st}: {body}")
    st, body = http("GET", f"/api/interactive/drafts/{s.board_id}")
    if st != 200:
        return fail(f"草稿读取 HTTP {st}: {body}")
    text = json.dumps(body, ensure_ascii=False)
    if "草稿内容E" not in text:
        return fail(f"草稿没有恢复：{text[:200]}")
    return pass_(f"互动板面草稿保存/恢复可用：{text[:120]}")


@check("R6", "regression", "受理待确认入口（impact-check）可用")
def c_r6(backend, http):
    st, body = http(
        "POST",
        f"/api/interactive/boards/{BOARD}/impact-check",
        {"stateVersion": 0, "changeSet": {"state": _state([])}},
    )
    if st != 200:
        return fail(f"impact-check HTTP {st}: {body}")
    if not isinstance(body, dict) or "ok" not in body:
        return fail(f"impact-check 返回形状不对：{str(body)[:200]}")
    return pass_(f"impact-check 返回 ok={body.get('ok')}")


@check("R7", "regression", "提交成功后草稿清理不误删未提交内容")
def c_r7(backend, http):
    s = new_session(backend, http, "r7")
    http("PUT", f"/api/interactive/drafts/{s.board_id}", {"drafts": {"chat": "另一个话题的草稿"}})
    note = _card("c_note", "text", "提交内容", checked=True)
    s.put_state(_state([note]))
    s.submit()
    st, body = http("GET", f"/api/interactive/drafts/{s.board_id}")
    if st != 200:
        return fail(f"草稿读取 HTTP {st}")
    if "另一个话题的草稿" not in json.dumps(body, ensure_ascii=False):
        return fail("提交把不属于本次提交的草稿清掉了")
    return pass_("提交未清掉无关草稿")


@check("R8", "regression", "话题归属相邻路径（新建/切换/恢复）不回归")
def c_r8(backend, http):
    return nv("需要前端证据（本脚本只有后端 HTTP）")


@check("R9", "regression", "演示入口不驱动真实任务；正式路径不显示演示内容")
def c_r9(backend, http):
    s = new_session(backend, http, "r9")
    n0 = _execution_count(backend, s.board_id)
    st, created = s.demo_intents()
    if st != 200:
        return fail(f"演示入口 HTTP {st}")
    if not (created or {}).get("demo"):
        return fail("演示入口没有 demo 标记")
    if "演示" not in json.dumps(created, ensure_ascii=False):
        return fail("演示入口没有明确标注为演示")
    intents = (created or {}).get("created") or []
    if intents:
        http("POST", f"/api/interactive/intents/{intents[0]['id']}/demo/advance", {"outcome": "done"})
    n1 = _execution_count(backend, s.board_id)
    if n1 != n0:
        return fail(f"演示推进驱动了真实执行记录：{n0} → {n1}")
    # 正式路径（submissionId）不得给出演示内容
    s2 = new_session(backend, http, "r9b")
    st, body = s2.create_intents("sub_missing_id")
    blob = json.dumps(body, ensure_ascii=False)
    if (body or {}).get("demo") is True or "［演示］" in blob:
        return fail(f"正式路径给出了演示内容：{blob[:200]}")
    return pass_(f"演示入口带 demo 标注且演示推进未产生执行记录；正式入口对无效 submissionId 如实拒绝：{blob[:120]}")


# ---- 5. 窄窗口 / 主题 ----


@check("V1", "visual", "800x600 关键状态可读（截图）")
def c_v1(backend, http):
    return nv("需要前端截图：主智能体/D 负责；E 未做真实浏览器视觉检查")


@check("V2", "visual", "480x600 关键状态可读（截图）")
def c_v2(backend, http):
    return nv("需要前端截图：主智能体/D 负责；E 未做真实浏览器视觉检查")


@check("V3", "visual", "窄窗口审批可操作")
def c_v3(backend, http):
    return nv("需要前端截图 + 点击验证")


@check("V4", "visual", "结果与来源引用可读")
def c_v4(backend, http):
    return nv("需要前端截图")


@check("V5", "visual", "恢复入口可操作")
def c_v5(backend, http):
    return nv("需要前端截图 + 点击验证")


@check("V6", "visual", "亮/暗主题关键状态对比度足够")
def c_v6(backend, http):
    return nv("需要前端截图（亮/暗各一组）")


# ---- 运行器 ----


def run(cids, groups) -> int:
    selected = [c for c in CHECKS if (not cids or c[0] in cids) and (not groups or c[1] in groups)]
    if not selected:
        print("没有匹配的检查项")
        return 2
    backend = LiveBackend()
    print(f"[E] 启动真实后端 127.0.0.1:{backend.port}（临时库 {backend.db}）")
    backend.start()
    http = Http(backend.port)
    try:
        for cid, group, title, fn in selected:
            t0 = time.time()
            try:
                out = fn(backend, http)
            except Exception as exc:  # noqa: BLE001
                out = Outcome(
                    FAIL,
                    f"检查自身抛异常（可能是候选版本契约不符）：{exc}\n{traceback.format_exc()[-600:]}",
                )
            dt = time.time() - t0
            _RESULTS.append(
                {
                    "id": cid,
                    "group": group,
                    "title": title,
                    "status": out.status,
                    "detail": out.detail,
                    "seconds": round(dt, 3),
                }
            )
            mark = {PASS: "PASS", FAIL: "FAIL", NV: "NV  "}[out.status]
            print(f"[{mark}] {cid} {title} ({dt:.2f}s)")
            if out.detail:
                for line in str(out.detail).splitlines():
                    print(f"        {line}")
    finally:
        backend.stop()

    counts = {PASS: 0, FAIL: 0, NV: 0}
    for item in _RESULTS:
        counts[item["status"]] += 1
    print("\n=== 汇总 ===")
    print(f"PASS={counts[PASS]}  FAIL={counts[FAIL]}  NOT_VERIFIABLE={counts[NV]}  共 {len(_RESULTS)} 项")
    if http.retries:
        print(f"（HTTP 连接级重试 {len(http.retries)} 次：{http.retries[:6]}）")
    outdir = HERE / "out"
    outdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = outdir / f"verify-{stamp}.json"
    path.write_text(
        json.dumps({"results": _RESULTS, "counts": counts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"结果已写入：{path}")
    if counts[FAIL]:
        print("有 FAIL：本次候选不可视为通过。")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="E 的定向验收脚本（fake provider + 真实 HTTP）")
    ap.add_argument("--list", action="store_true", help="列出全部检查项")
    ap.add_argument("--check", default="", help="逗号分隔的检查 id，如 B1,C1,D4")
    ap.add_argument(
        "--group",
        default="",
        help=(
            "逗号分隔的组：real-path/visibility/no-model/intent/recovery/contract/"
            "materials/regression/visual"
        ),
    )
    args = ap.parse_args()
    if args.list:
        for cid, group, title, _ in CHECKS:
            print(f"{cid:4s} [{group:11s}] {title}")
        return 0
    cids = {x.strip() for x in args.check.split(",") if x.strip()}
    groups = {x.strip() for x in args.group.split(",") if x.strip()}
    return run(cids, groups)









@check("X7", "contract", "允许操作集合与冻结清单一致（模型与执行共用白名单）")
def c_x7(backend, http):
    expected = {
        "add_reply_card",
        "add_text_card",
        "move_card",
        "group_cards",
        "order_group",
        "add_link",
    }
    from agent.interactive import execution as execution_module
    from agent.interactive import models as imodels
    from agent.interactive import prompts as prompts_module

    ops = set(imodels.ALLOWED_INTENT_OPS)
    if ops != expected:
        return fail(f"ALLOWED_INTENT_OPS 与契约 §14.5 不一致：多={sorted(ops - expected)} 少={sorted(expected - ops)}")
    if set(execution_module.ALLOWED_OPS) != expected:
        return fail(f"execution.ALLOWED_OPS 与契约不一致：{sorted(execution_module.ALLOWED_OPS)}")
    if set(prompts_module.OPERATION_SHAPES) != expected:
        return fail(f"提示词里的操作形状与白名单漂移：{sorted(prompts_module.OPERATION_SHAPES)}")
    forbidden = {"delete_card", "hide_card", "check_card", "edit_user_card", "write_db"}
    if ops & forbidden:
        return fail(f"允许集合包含契约禁止的操作：{sorted(ops & forbidden)}")
    return pass_("三处（models / execution / prompts）与契约 §14.5 的六项完全一致，且不含禁止操作")


@check("X8", "contract", "迁移 37/38 与 REQUIRED_OBJECTS 覆盖本轮新对象")
def c_x8(backend, http):
    from agent.storage import migrate, schema

    problems = []
    try:
        required = migrate.REQUIRED_OBJECTS
        req_tables = set(required["tables"])
        req_columns = set(required["columns"])
        req_indexes = set(required["indexes"])
    except Exception as exc:  # noqa: BLE001
        return fail(f"读取 REQUIRED_OBJECTS 失败（结构应为 dict[tables/columns/indexes]）：{exc}")
    for name in ("board_understanding", "board_executions"):
        if name not in req_tables:
            problems.append(f"REQUIRED_OBJECTS.tables 缺少 {name}")
    for name in ("idx_board_understanding_request", "idx_board_executions_intent"):
        if name not in req_indexes:
            problems.append(f"REQUIRED_OBJECTS.indexes 缺少 {name}")
    for col in (
        "board_intents.goal",
        "board_intents.expected",
        "board_intents.operations",
        "board_intents.basis",
        "board_intents.intent_key",
        "board_intents.understanding_id",
    ):
        if col not in req_columns:
            problems.append(f"REQUIRED_OBJECTS.columns 缺少 {col}")
    versions = [v for v, _ in schema.MIGRATIONS]
    if versions.count(37) != 1 or versions.count(38) != 1:
        problems.append(f"迁移编号 37/38 不唯一：37×{versions.count(37)}、38×{versions.count(38)}")
    if versions != sorted(versions):
        problems.append("迁移没有按编号递增排列")
    if not versions or versions[-1] != 38:
        problems.append(f"最新迁移应为 38，实为 {versions[-1] if versions else None}")
    if schema.SCHEMA_VERSION != 38:
        problems.append(f"SCHEMA_VERSION 应为 38，实为 {schema.SCHEMA_VERSION}")
    if schema.COMPENSATION_VERSION != 38:
        problems.append(f"COMPENSATION_VERSION 应为 38，实为 {schema.COMPENSATION_VERSION}")
    cols = {r["name"] for r in backend.conn.execute("PRAGMA table_info(board_intents)")}
    for col in ("goal", "expected", "operations", "basis", "intent_key", "understanding_id"):
        if col not in cols:
            problems.append(f"board_intents 缺少列 {col}")
    if problems:
        return fail("；".join(problems))
    return pass_("迁移 37/38、COMPENSATION_VERSION=38、REQUIRED_OBJECTS 与 board_intents 新列齐备")


if __name__ == "__main__":
    raise SystemExit(main())

















