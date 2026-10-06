"""互动模式第一阶段：独立复核的复现用例（agent-verify）。

这些用例**只描述我亲自复现过的事实**，不替开发者补正常路径的功能测试。
命名约定：

- test_...：断言「契约要求的行为」，通过即说明该条边界成立；
- test_known_defect_...：断言「契约要求的行为」，但当前实现不成立（已报告缺陷），
  用 xfail(strict=False) 标注，保证套件仍然绿、同时把缺陷固定在代码里可复现。

对应报告：docs/interactive-verify-report.md
"""

from __future__ import annotations

import json
import socket
import ssl
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import models, submission
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

BOARD = "board_verify"


# --- 工具 ---------------------------------------------------------------


def _state(cards=(), groups=(), links=(), selection=(), board: str = BOARD) -> dict:
    return {
        "boardId": board,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups),
        "links": list(links),
        "selection": list(selection),
    }


def _note(content: str, *, checked: bool = False, **fields) -> dict:
    return models.new_card("text", content, checked=checked, **fields)


def _material(kind: str = "file", content: str = "", **fields) -> dict:
    return models.new_card(kind, content, **fields)


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class Harness:
    """一套「同一个数据库、可以被重新打开」的 app 实例。"""

    def __init__(self, db_path: Path, data_dir: Path) -> None:
        self.db_path = db_path
        self.data_dir = data_dir
        self.open()

    def open(self) -> None:
        self.conn = connect(self.db_path)
        apply_migrations(self.conn)
        app = create_app(Settings(data_dir=self.data_dir), self.conn)
        app.state.ctx.credentials._kr = MemoryKeyring()
        self.app = app
        self.client = TestClient(app)

    def reopen(self) -> None:
        """关掉旧实例，用**同一个数据库**再开一个新的（模拟重新打开应用）。"""
        try:
            self.client.close()
        finally:
            self.conn.close()
        self.open()

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            self.conn.close()

    def state(self, board: str = BOARD) -> dict:
        return self.client.get(f"/api/interactive/boards/{board}/state").json()

    def intents(self, board: str = BOARD) -> dict:
        return self.client.get(f"/api/interactive/boards/{board}/intents").json()

    def statuses(self, board: str = BOARD) -> dict[str, str]:
        return {item["id"]: item["status"] for item in self.intents(board)["intents"]}

    def submissions(self, board: str = BOARD) -> list[dict]:
        return self.client.get(f"/api/interactive/boards/{board}/submissions").json()["submissions"]

    def put_state(self, state: dict, board: str = BOARD):
        return self.client.put(f"/api/interactive/boards/{board}/state", json={"state": state})

    def submit(self, board: str = BOARD, **payload):
        return self.client.post(f"/api/interactive/boards/{board}/submissions", json=payload)

    def seed_demo(self, board: str = BOARD) -> dict:
        self.put_state(_state([_material("file", "材料一", meta={"name": "a.pdf"}),
                               _material("file", "材料二", meta={"name": "b.xlsx"})], board=board))
        self.client.post(f"/api/interactive/boards/{board}/intents", json={"demo": True})
        return self.intents(board)


@pytest.fixture()
def harness(tmp_path: Path):
    h = Harness(tmp_path / "verify.db", tmp_path / "data")
    yield h
    h.close()


# --- 1. 权限边界 --------------------------------------------------------


def test_unchecked_and_hidden_notes_links_and_groups_stay_out_of_payload(harness: Harness):
    """未勾选 / 明确隐藏的注释，其文字、id 与链接都不得进入 before/after/visibleRange。"""
    secret = _note("SECRET_UNCHECKED")
    hidden = _note("SECRET_HIDDEN", checked=True, hidden=True)
    visible = _note("VISIBLE_NOTE", checked=True)
    material = _material("file", "材料", meta={"name": "m.pdf"})
    link_leak = models.new_link(visible["id"], secret["id"], meaning="SECRET_LINK_MEANING")
    link_hidden = models.new_link(hidden["id"], material["id"], meaning="SECRET_LINK_2")
    group_hidden_only = models.new_group("SECRET_GROUP_NAME", members=[hidden["id"]])
    group_mixed = models.new_group("混合组", members=[secret["id"], material["id"]])

    harness.put_state(
        _state(
            [secret, hidden, visible, material],
            [group_hidden_only, group_mixed],
            [link_leak, link_hidden],
        )
    )
    result = harness.submit(requestedVisible=[secret["id"], hidden["id"]]).json()
    assert result["status"] == "succeeded"

    payload = _dump(
        {
            "before": result["before"],
            "after": result["after"],
            "visibleRange": result["visibleRange"],
            "expressions": result["expressions"],
        }
    )
    for leaked in (
        "SECRET_UNCHECKED",
        secret["id"],
        "SECRET_HIDDEN",
        hidden["id"],
        "SECRET_LINK_MEANING",
        link_leak["id"],
        "SECRET_LINK_2",
        link_hidden["id"],
    ):
        assert leaked not in payload, f"{leaked} 不该出现在提交载荷里"
    # 一名可见成员都没有的组整体不出现（否则组名会间接暴露隐藏注释）
    assert [g["name"] for g in result["after"]["groups"]] == ["混合组"]
    assert result["after"]["groups"][0]["members"] == [material["id"]]
    assert "已忽略" in result["delivery"]["detail"], "客户端塞进来的未勾选卡片必须被忽略并说明"


def test_server_derives_visible_range_from_saved_state_not_request(harness: Harness):
    """绕过前端直接打 API：请求里塞未勾选卡片 id 也进不了 before/after。"""
    note = _note("NEVER_GRANTED")
    harness.put_state(_state([note]))
    result = harness.submit(requestedVisible=[note["id"]]).json()
    assert note["id"] not in _dump(result["before"])
    assert note["id"] not in _dump(result["after"])
    assert note["id"] not in _dump(result["visibleRange"])


def test_hidden_forced_off_and_reply_never_checkable_through_put_state(harness: Harness):
    """PUT /state 的 normalize 真的生效：G6 隐藏强制取消勾选、G7 reply 恒不勾选。"""
    state = harness.put_state(
        _state(
            [
                {"id": "c_h", "kind": "text", "content": "hidden", "checked": True, "hidden": True},
                {"id": "c_r", "kind": "reply", "content": "reply", "checked": True},
            ]
        )
    ).json()["state"]
    by_id = {card["id"]: card for card in state["cards"]}
    assert by_id["c_h"]["checked"] is False
    assert by_id["c_r"]["checked"] is False


def test_normalize_fixes_duplicates_dangling_links_and_empty_groups(harness: Harness):
    """结构异常的状态：重复 id、重复成员、指向不存在卡片、空组、跨组重复都应被清掉。"""
    state = harness.put_state(
        _state(
            [
                {"id": "c_dup", "kind": "file", "content": "first"},
                {"id": "c_dup", "kind": "file", "content": "second"},
                {"id": "c_a", "kind": "text", "content": "A", "checked": True},
                {"id": "c_b", "kind": "text", "content": "B", "checked": True},
                {"id": "c_del", "kind": "file", "content": "deleted", "deleted": True},
            ],
            [
                {"id": "g1", "name": "G1", "ordered": True, "members": ["c_a", "c_a", "c_del", "c_x"]},
                {"id": "g2", "name": "G2", "members": ["c_a"]},
                {"id": "g3", "name": "empty", "members": []},
            ],
            [
                {"id": "l1", "src": "c_a", "dst": "c_b"},
                {"id": "l2", "src": "c_a", "dst": "c_b"},
                {"id": "l3", "src": "c_a", "dst": "c_del"},
                {"id": "l4", "src": "c_a", "dst": "c_missing"},
            ],
            ["c_a", "c_del", "c_missing"],
        )
    ).json()["state"]
    assert sum(1 for c in state["cards"] if c["id"] == "c_dup") == 1
    # g1 去重并清掉已删除 / 不存在的成员；g2 与 g1 争同一张卡（G1），g3 是空组（G3）
    assert [g["members"] for g in state["groups"]] == [["c_a"]]
    assert [link["id"] for link in state["links"]] == ["l1"]
    assert state["selection"] == ["c_a"]


SAVE_PATH_MODULES = (
    Path(__file__).resolve().parents[1] / "src" / "agent" / "interactive" / "board_store.py",
    Path(__file__).resolve().parents[1] / "src" / "agent" / "interactive" / "submission.py",
    Path(__file__).resolve().parents[1] / "src" / "agent" / "api" / "interactive_store.py",
)
FORBIDDEN_IN_SAVE_PATH = (
    "import requests",
    "import httpx",
    "import aiohttp",
    "import openai",
    "import anthropic",
    "import urllib.request",
    "import socket",
    "urlopen(",
)


def test_save_path_source_has_no_model_or_network_imports():
    """保存路径的源码里不许出现模型 / 网络客户端（静态证据，和运行时检查互为补充）。"""
    for path in SAVE_PATH_MODULES:
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_IN_SAVE_PATH:
            assert needle not in text, f"{path.name} 不该出现 {needle}"


def test_put_state_makes_no_network_calls_and_no_submissions(harness: Harness, monkeypatch):
    """保存路径绝不调用模型 / 网络：打断真正的出网 API，PUT /state 仍必须成功。

    只打断「真的会出网」的入口（create_connection / getaddrinfo / TLS 握手）；
    不动 socket.socket.connect —— Windows 上 asyncio 事件循环的自管道会用它，
    打断它等于打断测试自己的 TestClient。
    """
    before = len(harness.submissions())

    def _no_network(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("保存路径不该发起任何网络连接")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", _no_network)
    monkeypatch.setattr(httpx.Client, "send", _no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_network)

    note = _note("保存的内容")
    response = harness.put_state(_state([note]))
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert harness.client.get(f"/api/interactive/boards/{BOARD}/state").status_code == 200

    monkeypatch.undo()
    assert len(harness.submissions()) == before, "保存（PUT /state）不得产生提交记录"


def test_save_keeps_content_check_flags_and_drafts_without_submitting(harness: Harness):
    """保存 = 落库：内容与勾选都在，草稿单独存，提交列表不增加。"""
    note = _note("保存但还没提交", checked=True)
    harness.put_state(_state([note]))
    harness.client.put(f"/api/interactive/drafts/{BOARD}", json={"drafts": {"k": "草稿内容"}})

    loaded = harness.state()
    assert [c["content"] for c in loaded["state"]["cards"]] == ["保存但还没提交"]
    assert loaded["state"]["cards"][0]["checked"] is True
    assert loaded["drafts"]["drafts"] == {"k": "草稿内容"}
    assert loaded["submissions"] == []


# --- 2. 恢复行为 --------------------------------------------------------


def test_reopen_reads_back_content_drafts_baseline_and_previews(harness: Harness):
    """内容、草稿、提交基准、待审批预览、暂停任务，在新实例（同一数据库）里都读得回来。"""
    note = _note("勾选的注释", checked=True)
    harness.put_state(_state([note, _material("file", "材料", meta={"name": "m.pdf"})]))
    harness.client.put(f"/api/interactive/drafts/{BOARD}", json={"drafts": {"note": "未提交的草稿"}})
    assert harness.submit().json()["status"] == "succeeded"
    harness.seed_demo()
    before = {
        "state": harness.state(),
        "intents": harness.intents(),
        "submissions": harness.submissions(),
    }
    previews = [item["preview"] for item in before["intents"]["intents"]]

    harness.reopen()

    after = {
        "state": harness.state(),
        "intents": harness.intents(),
        "submissions": harness.submissions(),
    }
    assert [c["content"] for c in after["state"]["state"]["cards"]] == [
        c["content"] for c in before["state"]["state"]["cards"]
    ]
    assert [(c["x"], c["y"]) for c in after["state"]["state"]["cards"]] == [
        (c["x"], c["y"]) for c in before["state"]["state"]["cards"]
    ]
    assert after["state"]["drafts"]["drafts"] == {"note": "未提交的草稿"}
    assert after["state"]["baseline"] == before["state"]["baseline"]
    assert len(after["submissions"]) == len(before["submissions"])
    assert [item["preview"] for item in after["intents"]["intents"]] == previews, (
        "待审批预览必须原样恢复"
    )


def test_reopen_does_not_auto_submit_or_resume(harness: Harness):
    """重新打开不自动提交、不自动恢复执行：running 降级 paused，反复读取不再变化。"""
    listing = harness.seed_demo()
    plain = [
        item["id"]
        for item in listing["intents"]
        if not item["dependsOn"] and item["id"] not in listing["conflicts"][0]
    ][0]
    assert harness.client.post(f"/api/interactive/intents/{plain}/approve", json={}).json()["ok"]
    assert harness.statuses()[plain] == "running"

    submissions_before = len(harness.submissions())
    harness.reopen()

    assert harness.statuses()[plain] == "paused", "重新打开时 running 必须降级为 paused"
    for _ in range(3):
        harness.intents()
        harness.state()
    assert harness.statuses()[plain] == "paused", "反复读取不得让它自动恢复"
    assert len(harness.submissions()) == submissions_before, "重新打开不得自动提交"
    # 暂停的任务必须由用户再次确认才继续（不带确认不会自动开始）
    again = harness.client.post(f"/api/interactive/intents/{plain}/approve", json={}).json()
    assert again["ok"] is False and again["reason"] == "confirm_required"
    assert harness.statuses()[plain] == "paused"
    resumed = harness.client.post(
        f"/api/interactive/intents/{plain}/approve", json={"confirmDependency": True}
    ).json()
    assert resumed["ok"] is True and resumed["intent"]["status"] == "running"


def test_failed_submit_keeps_changes_and_checks_and_does_not_advance_baseline(
    harness: Harness, monkeypatch
):
    """提交失败：改动与勾选保留、基准不提前更新，可以再试一次。"""
    note = _note("失败也要保留", checked=True)
    harness.put_state(_state([note]))

    def _boom(_snapshot):  # noqa: ANN001
        raise RuntimeError("injected failure")

    # 注入失败只为了走到 submit_board 的 failed 分支；走的是产品代码自己的异常处理。
    monkeypatch.setattr(submission, "content_fingerprint", _boom)
    failed = harness.submit().json()
    monkeypatch.undo()

    assert failed["status"] == "failed"
    assert failed["baseline"]["updated"] is False
    state = harness.state()
    assert state["baseline"] is None
    assert state["state"]["cards"][0]["checked"] is True
    assert [c["content"] for c in state["state"]["cards"]] == ["失败也要保留"]
    assert [row["status"] for row in state["submissions"]] == ["failed"]

    retry = harness.submit().json()
    assert retry["status"] == "succeeded"
    assert retry["checkedCleared"] == [note["id"]]
    assert harness.state()["baseline"] is not None


# --- 3. 对抗性尝试 ------------------------------------------------------


def test_repeated_identical_submit_is_idempotent(harness: Harness):
    """连续两次提交同样内容：不重复调用、基准不推进。"""
    material = _material("file", "材料", meta={"name": "m.pdf"})
    harness.put_state(_state([material]))
    first = harness.submit().json()
    assert first["status"] == "succeeded" and first["baseline"]["seq"] == 1

    harness.put_state(_state([material]))
    second = harness.submit().json()
    third = harness.submit().json()
    assert second["status"] in ("empty", "duplicate")
    assert third["status"] in ("empty", "duplicate")
    assert second["baseline"]["updated"] is False
    assert third["baseline"]["updated"] is False
    succeeded = [row for row in harness.submissions() if row["status"] == "succeeded"]
    assert len(succeeded) == 1, "只有第一次成功提交推进基准"
    assert "没有调用 QIO" in second["delivery"]["reason"]


def test_duplicate_status_when_content_unchanged_after_delete_and_readd(harness: Harness):
    """同一窗口「撤回后又加回同样的材料」：内容没变 → duplicate，不更新基准。"""
    material = _material("file", "内容一样", meta={"name": "s.pdf"})
    harness.put_state(_state([material]))
    assert harness.submit().json()["status"] == "succeeded"

    deleted = dict(material, deleted=True)
    readded = _material("file", "内容一样", meta={"name": "s.pdf"})
    harness.put_state(_state([deleted, readded]))
    result = harness.submit().json()
    assert result["status"] == "duplicate"
    assert result["baseline"]["updated"] is False
    assert result["baseline"]["seq"] == 1


def test_conflicting_pair_in_one_batch_is_refused_entirely(harness: Harness):
    """互不相容的一对一起送进批量批准：两项都不批准。"""
    listing = harness.seed_demo()
    pair = listing["conflicts"][0]
    result = harness.client.post(
        "/api/interactive/intents/batch", json={"approve": pair}
    ).json()
    assert result["approved"] == []
    assert all(item["reason"] == "conflict" for item in result["results"])
    statuses = harness.statuses()
    assert statuses[pair[0]] == "pending" and statuses[pair[1]] == "pending"


def test_dependency_approve_without_confirm_never_starts(harness: Harness):
    """依赖任务不带 confirmDependency：只进入等待，绝不偷偷开始。"""
    listing = harness.seed_demo()
    dependent = next(item for item in listing["intents"] if item["dependsOn"])
    result = harness.client.post(
        f"/api/interactive/intents/{dependent['id']}/approve", json={}
    ).json()
    assert result["ok"] is True and result["intent"]["status"] == "waiting_dependency"
    assert harness.statuses()[dependent["id"]] == "waiting_dependency"

    # 前项完成后变成 waiting_confirm；不带确认仍然不开始
    parent = dependent["dependsOn"][0]
    harness.client.post(f"/api/interactive/intents/{parent}/approve", json={})
    harness.client.post(
        f"/api/interactive/intents/{parent}/demo/advance", json={"outcome": "done"}
    )
    assert harness.statuses()[dependent["id"]] == "waiting_confirm"
    blocked = harness.client.post(
        f"/api/interactive/intents/{dependent['id']}/approve", json={}
    ).json()
    assert blocked["ok"] is False and blocked["reason"] == "confirm_required"
    confirmed = harness.client.post(
        f"/api/interactive/intents/{dependent['id']}/approve",
        json={"confirmDependency": True},
    ).json()
    assert confirmed["ok"] is True and confirmed["intent"]["status"] == "running"


def test_rejected_intent_keeps_board_content(harness: Harness):
    """拒绝后预览消失、原内容保留。"""
    listing = harness.seed_demo()
    target = next(item for item in listing["intents"] if not item["dependsOn"])
    state_before = harness.state()["state"]
    assert harness.client.post(f"/api/interactive/intents/{target['id']}/reject", json={}).json()["ok"]
    assert harness.statuses()[target["id"]] == "rejected"
    assert harness.state()["state"]["cards"] == state_before["cards"], "拒绝不得改动板面内容"


# --- 4. 已报告缺陷的固定复现（xfail：修好后会自动 XPASS） ------------------


def test_batch_approved_intent_survives_intent_list_refresh(harness: Harness):
    """批量批准后紧接着刷新意图列表，任务必须保持 running。

    这是独立复核发现的阻断缺陷（batch_decide 不透传 instance_id）的回归用例；
    修好后从 xfail 改成正式断言。
    """
    listing = harness.seed_demo()
    plain = [
        item["id"]
        for item in listing["intents"]
        if not item["dependsOn"] and item["id"] not in listing["conflicts"][0]
    ][0]
    result = harness.client.post(
        "/api/interactive/intents/batch", json={"approve": [plain]}
    ).json()
    assert result["approved"] == [plain]
    assert result["results"][0]["intent"]["status"] == "running"
    # 界面在批量审批后立刻刷新意图列表（stores/interactive.ts decideBatch → loadIntents）
    listing = harness.intents()
    assert {item["id"]: item["status"] for item in listing["intents"]}[plain] == "running"
    assert listing["recovery"] == {"paused": []}, "同一进程刷新不得把刚批准的任务暂停掉"


def test_revoked_note_does_not_return_to_before(harness: Harness):
    """先前提交过 → 取消勾选 → 删除：before / after / expressions 都不得再出现它。

    这是独立复核发现的阻断缺陷（project_baseline 让「已删除」优先于「未勾选」）的回归用例。
    """
    note = _note("REVOKED_TEXT", checked=True)
    material = _material("file", "材料", meta={"name": "m.pdf"})
    link = models.new_link(note["id"], material["id"], meaning="REVOKED_LINK")
    harness.put_state(_state([note, material], links=[link]))
    assert harness.submit().json()["status"] == "succeeded"

    revoked = dict(note, checked=False, deleted=True)
    harness.put_state(_state([revoked, material], links=[link]))
    result = harness.submit().json()
    before = _dump(result["before"])
    assert "REVOKED_TEXT" not in before
    assert note["id"] not in before
    assert "REVOKED_LINK" not in before
    assert link["id"] not in before


# --- 5. 修复后的复测（8903ab1）：批量身份 / 撤回对照 / 暂停继续 -----------


def _raw_progress(harness: Harness, intent_id: str) -> dict:
    row = harness.conn.execute(
        "SELECT progress FROM board_intents WHERE id = ?", (intent_id,)
    ).fetchone()
    return json.loads(row["progress"] or "{}")


def _plain_intent(listing: dict) -> str:
    """挑一个既不依赖前项、也不在冲突对里的待审批意图。"""
    return [
        item["id"]
        for item in listing["intents"]
        if not item["dependsOn"] and item["id"] not in listing["conflicts"][0]
    ][0]


def test_batch_approve_survives_same_process_but_pauses_in_new_instance(harness: Harness):
    """批量批准：同一实例里刷新仍是 running；换一个实例（新 app）再读才降级 paused。"""
    listing = harness.seed_demo()
    plain = _plain_intent(listing)
    batch = harness.client.post(
        "/api/interactive/intents/batch", json={"approve": [plain]}
    ).json()
    assert batch["approved"] == [plain]
    assert "__ownerInstance" in _raw_progress(harness, plain), "批量批准也必须记录执行者身份"

    first = harness.intents()
    assert {item["id"]: item["status"] for item in first["intents"]}[plain] == "running"
    assert first["recovery"] == {"paused": []}, "同一进程刷新不得把刚批准的任务暂停"

    harness.reopen()  # 换一个 instance_id
    second = harness.intents()
    assert {item["id"]: item["status"] for item in second["intents"]}[plain] == "paused"
    assert second["recovery"]["paused"] == [plain]


def test_revocation_keeps_before_after_expressions_pending_and_range_clean(harness: Harness):
    """取消勾选 + 删除：before / after / expressions / pending / visibleRange 全都不许出现。"""
    note = _note("REVOKED_TEXT", checked=True)
    material = _material("file", "材料", meta={"name": "m.pdf"})
    link = models.new_link(note["id"], material["id"], meaning="REVOKED_LINK")
    harness.put_state(_state([note, material], links=[link]))
    assert harness.submit().json()["status"] == "succeeded"

    revoked = dict(note, checked=False, deleted=True)
    harness.put_state(_state([revoked, material], links=[link]))
    pending = _dump(harness.state()["pending"])
    result = harness.submit().json()
    blob = _dump(
        {
            "before": result["before"],
            "after": result["after"],
            "expressions": result["expressions"],
            "visibleRange": result["visibleRange"],
            "pending": pending,
        }
    )
    for leaked in ("REVOKED_TEXT", note["id"], "REVOKED_LINK", link["id"]):
        assert leaked not in blob, f"{leaked} 不得出现在提交载荷或未提交预览里"
    assert result["before"]["links"] == []
    assert result["after"]["links"] == []


def test_deleting_a_still_checked_note_still_expresses_withdrawal(harness: Harness):
    """对照：仍然勾选时删除 → before 仍保留它，并生成「撤回」表达。"""
    note = _note("WITHDRAWN_TEXT", checked=True)
    harness.put_state(_state([note]))
    assert harness.submit().json()["status"] == "succeeded"

    # 前端「删除」= deleted: true，勾选状态不变（frontend/src/interactive/board.ts removeCard）
    harness.put_state(_state([dict(note, deleted=True)]))
    result = harness.submit().json()
    assert result["status"] == "succeeded"
    assert note["id"] in _dump(result["before"]), "仍勾选时删除要保留「撤回」表达"
    assert "note_deleted" in [e["kind"] for e in result["expressions"]]
    assert note["id"] not in _dump(result["after"])


def test_deleting_a_material_still_expresses_withdrawal(harness: Harness):
    """对照：材料删除仍表达撤回（material_removed）。"""
    material = _material("file", "要撤回的材料", meta={"name": "d.pdf"})
    harness.put_state(_state([material]))
    assert harness.submit().json()["status"] == "succeeded"

    harness.put_state(_state([dict(material, deleted=True)]))
    result = harness.submit().json()
    assert "material_removed" in [e["kind"] for e in result["expressions"]]
    assert material["id"] in _dump(result["before"])


def test_paused_resume_requires_confirm_and_refreshes_material_watch(harness: Harness):
    """暂停继续：不带确认 → confirm_required；带确认 → running 且刷新材料指纹、保留进度。"""
    listing = harness.seed_demo()
    plain = _plain_intent(listing)
    refs = next(i for i in listing["intents"] if i["id"] == plain)["materialRefs"]
    assert refs, "演示意图应当带材料依据"

    assert harness.client.post(f"/api/interactive/intents/{plain}/approve", json={}).json()["ok"]
    assert harness.statuses()[plain] == "running"

    # 改一项被依赖的材料并保存 → 服务端自己把执行中的任务暂停并保留进度
    state = harness.state()["state"]
    for card in state["cards"]:
        if card["id"] in refs:
            card["content"] = "材料已经改过了"
    harness.put_state(state)
    assert harness.statuses()[plain] == "paused"
    old = _raw_progress(harness, plain)
    assert "__materialWatch" in old and "__ownerInstance" not in old

    without = harness.client.post(
        f"/api/interactive/intents/{plain}/approve", json={}
    ).json()
    assert without["ok"] is False and without["reason"] == "confirm_required"
    assert harness.statuses()[plain] == "paused"

    resumed = harness.client.post(
        f"/api/interactive/intents/{plain}/approve", json={"confirmDependency": True}
    ).json()
    assert resumed["ok"] is True and resumed["intent"]["status"] == "running"
    fresh = _raw_progress(harness, plain)
    assert fresh["__materialWatch"] != old["__materialWatch"], "继续时要按当前材料刷新指纹"
    assert fresh["done"] == old["done"] and fresh["total"] == old["total"], "进度要保留"
    assert "__ownerInstance" in fresh, "继续后要重新记录执行者身份"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("reject", "closed"),
        ("failed", "closed"),
        ("cancelled", "closed"),
        ("done", "done"),
    ],
)
def test_closed_intents_cannot_be_revived_by_approve(tmp_path: Path, outcome: str, expected: str):
    """已拒绝 / 已失败 / 已取消 / 已完成都不能通过 approve 复活。"""
    harness = Harness(tmp_path / "closed.db", tmp_path / "data")
    try:
        plain = _plain_intent(harness.seed_demo())
        terminal = "rejected" if outcome == "reject" else outcome
        if outcome == "reject":
            harness.client.post(f"/api/interactive/intents/{plain}/reject", json={})
        else:
            harness.client.post(f"/api/interactive/intents/{plain}/approve", json={})
            harness.client.post(
                f"/api/interactive/intents/{plain}/demo/advance", json={"outcome": outcome}
            )
        assert harness.statuses()[plain] == terminal
        again = harness.client.post(
            f"/api/interactive/intents/{plain}/approve", json={"confirmDependency": True}
        ).json()
        assert again["ok"] is False and again["reason"] == expected
        assert harness.statuses()[plain] == terminal
    finally:
        harness.close()

