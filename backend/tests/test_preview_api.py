"""卡片内预览 · HTTP 接口（契约 §3.3）定向测试。

这些用例走**真实 FastAPI 路由**（TestClient + 真实 sqlite + 真实磁盘副本）：
* GET /api/attachments/{id}/preview（含 ?refresh=1）；
* GET/PUT/DELETE /api/interactive/boards/{boardId}/preview[/{cardId}]；
* GET /api/attachments/{id}/content 的 Range 行为（**只读核验既有路由**：够用就不改 server.py）。

注册方式与 lead 在 server.py 里要加的那一行**完全一致**：register_preview_routes(app)。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from agent.api.preview_routes import POSITION_KEYS, normalize_position
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import board_store
from agent.services import preview as preview_service
from agent.storage.db import connect, transaction
from agent.storage.migrate import apply_migrations
from fixtures.preview import (
    DOCX_HEADING,
    PDF_TITLE,
    PPTX_SLIDE1_TITLE,
    XLSX_A1,
    XLSX_B2_FORMULA,
    build_all,
    png_bytes,
)

BOARD_ID = "board_preview_test"
CARD_ID = "card_0001"


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "preview_api.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    # 预览路由现在由 create_app 内部注册（server.py 里的 register_preview_routes(app)）。
    # 测试不再手动注册一次：重复注册会让同一路径出现两条相同路由，从而掩盖
    # 「路由到底有没有真的挂上去」这个事实 —— 本用例的前提就是走真实注册路径。
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def samples(tmp_path: Path) -> dict[str, Path]:
    return build_all(tmp_path / "samples")


def _upload(client: TestClient, path: Path, name: str | None = None) -> dict:
    resp = client.post(
        "/api/attachments/upload",
        content=Path(path).read_bytes(),
        headers={
            "Content-Type": "application/octet-stream",
            "X-QIO-Name": quote(name or Path(path).name),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["attachment"]


def _board_with_card(
    client: TestClient, attachment_id: str, *, card_id: str = CARD_ID, board_id: str = BOARD_ID
) -> None:
    conn = client.app.state.ctx.conn
    state = {
        "boardId": board_id,
        "seq": 0,
        "updatedAt": "2026-10-12T00:00:00+00:00",
        "cards": [
            {
                "id": card_id,
                "kind": "file",
                "content": "",
                "meta": {
                    "name": "样例文档.docx",
                    "file": {"name": "样例文档.docx", "attachmentId": attachment_id, "size": 1},
                },
                "x": 0,
                "y": 0,
                "w": 320,
                "h": 200,
                "checked": False,
                "hidden": False,
                "folded": False,
                "bookmarked": False,
                "deleted": False,
                "createdAt": "2026-10-12T00:00:00+00:00",
                "updatedAt": "2026-10-12T00:00:00+00:00",
            }
        ],
        "groups": [],
        "links": [],
        "selection": [],
    }
    board_store.save_board(conn, board_id, state, reason="test")


# ---------------------------------------------------------------------------
# GET /api/attachments/{id}/preview
# ---------------------------------------------------------------------------


def test_docx_preview_over_http(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    resp = client.get(f"/api/attachments/{att['id']}/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["kind"] == "docx"
    assert body["attachmentId"] == att["id"]
    assert body["recipe"] == preview_service.RECIPE_VERSION
    assert body["version"].startswith("sha256:")
    assert body["truncated"] is False
    assert body["data"]["kind"] == "docx"
    heading = body["data"]["blocks"][0]
    assert heading["type"] == "heading"
    assert "".join(run["text"] for run in heading["runs"]) == DOCX_HEADING


def test_xlsx_preview_keeps_formula_and_cached_value(client: TestClient, samples):
    att = _upload(client, samples["xlsx"])
    body = client.get(f"/api/attachments/{att['id']}/preview").json()
    cells = {
        cell["ref"]: cell
        for sheet in body["data"]["sheets"]
        for row in sheet["rows"]
        for cell in row["cells"]
    }
    assert cells["A1"]["value"] == XLSX_A1
    assert cells["B2"]["formula"] == XLSX_B2_FORMULA
    assert cells["B2"]["cached"] == "12"


def test_pdf_preview_gives_facts_only(client: TestClient, samples):
    att = _upload(client, samples["pdf"])
    body = client.get(f"/api/attachments/{att['id']}/preview").json()
    assert body["kind"] == "pdf"
    assert body["data"]["pages"] == 1
    assert body["data"]["title"] == PDF_TITLE
    assert body["data"]["encrypted"] is False
    assert body["data"]["textChars"] > 0


def test_pptx_preview_gives_slides(client: TestClient, samples):
    att = _upload(client, samples["pptx"])
    body = client.get(f"/api/attachments/{att['id']}/preview").json()
    assert body["kind"] == "pptx"
    assert body["data"]["slides"][0]["title"] == PPTX_SLIDE1_TITLE


def test_preview_cache_hit_and_refresh_over_http(client: TestClient, samples):
    att = _upload(client, samples["xlsx"])
    first = client.get(f"/api/attachments/{att['id']}/preview").json()
    assert first["stats"]["cached"] is False
    second = client.get(f"/api/attachments/{att['id']}/preview").json()
    assert second["stats"]["cached"] is True
    refreshed = client.get(f"/api/attachments/{att['id']}/preview?refresh=1").json()
    assert refreshed["stats"]["cached"] is False
    assert refreshed["data"] == first["data"]


def test_unknown_attachment_preview_is_404(client: TestClient):
    resp = client.get("/api/attachments/att_nope/preview")
    assert resp.status_code == 404


def test_legacy_office_preview_is_200_unsupported_not_500(client: TestClient, samples):
    att = _upload(client, samples["legacy_doc"])
    resp = client.get(f"/api/attachments/{att['id']}/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "unsupported"
    assert body["kind"] == "legacy_office"
    assert body["data"]["kind"] == "legacy_office"
    assert "旧版" in body["reason"]


def test_broken_docx_preview_is_200_failed_not_500(client: TestClient, samples):
    att = _upload(client, samples["broken_docx"])
    resp = client.get(f"/api/attachments/{att['id']}/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["data"] is None
    assert body["reason"]


def test_unsupported_extension_preview_says_why(client: TestClient, samples):
    att = _upload(client, samples["png"], name="照片.png")
    ok = client.get(f"/api/attachments/{att['id']}/preview").json()
    assert ok["status"] == "ready"
    assert ok["data"] == {"kind": "image", "width": 8, "height": 6, "bytes": len(png_bytes())}


# ---------------------------------------------------------------------------
# GET /content 的 Range 行为（只读核验：不改 server.py 也能满足契约 §3.3 第一行）
# ---------------------------------------------------------------------------


def test_content_without_range_is_unchanged_with_real_length(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    size = samples["docx"].stat().st_size
    resp = client.get(f"/api/attachments/{att['id']}/content")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == str(size)
    assert len(resp.content) == size
    assert resp.headers.get("accept-ranges") == "bytes"
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_content_with_range_returns_206_and_content_range(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    size = samples["docx"].stat().st_size
    resp = client.get(
        f"/api/attachments/{att['id']}/content", headers={"Range": "bytes=0-3"}
    )
    assert resp.status_code == 206, resp.text
    assert resp.headers["content-range"] == f"bytes 0-3/{size}"
    assert resp.headers["content-length"] == "4"
    assert resp.headers.get("accept-ranges") == "bytes"
    assert resp.content == samples["docx"].read_bytes()[:4]


def test_content_range_suffix_and_open_ended(client: TestClient, samples):
    att = _upload(client, samples["png"])
    raw = samples["png"].read_bytes()
    tail = client.get(f"/api/attachments/{att['id']}/content", headers={"Range": "bytes=-4"})
    assert tail.status_code == 206
    assert tail.content == raw[-4:]
    head = client.get(f"/api/attachments/{att['id']}/content", headers={"Range": "bytes=4-"})
    assert head.status_code == 206
    assert head.content == raw[4:]
    assert head.headers["content-range"] == f"bytes 4-{len(raw) - 1}/{len(raw)}"


# ---------------------------------------------------------------------------
# 板面阅读位置
# ---------------------------------------------------------------------------


def test_board_preview_roundtrip(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    _board_with_card(client, att["id"])
    version = client.get(f"/api/attachments/{att['id']}/preview").json()["version"]

    empty = client.get(f"/api/interactive/boards/{BOARD_ID}/preview")
    assert empty.status_code == 200
    assert empty.json() == {"state": {"positions": {}}}

    position = {"mode": "docx", "page": 2, "zoom": 1.5, "scrollTop": 120}
    put = client.put(
        f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}",
        json={"attachmentId": att["id"], "version": version, "position": position},
    )
    assert put.status_code == 200, put.text
    item = put.json()
    assert item["attachmentId"] == att["id"]
    assert item["version"] == version
    assert item["position"] == position
    assert item["updatedAt"]

    state = client.get(f"/api/interactive/boards/{BOARD_ID}/preview").json()["state"]["positions"]
    assert set(state.keys()) == {CARD_ID}
    assert state[CARD_ID]["position"] == position
    assert state[CARD_ID]["version"] == version


def test_board_preview_drops_unknown_position_keys(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    _board_with_card(client, att["id"])
    version = client.get(f"/api/attachments/{att['id']}/preview").json()["version"]
    client.put(
        f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}",
        json={
            "attachmentId": att["id"],
            "version": version,
            "position": {
                "mode": "docx",
                "page": 3,
                "sheet": "  ",
                "evil": "丢弃",
                "pageIndex": 9,
                "zoom": 999,
            },
        },
    )
    item = client.get(f"/api/interactive/boards/{BOARD_ID}/preview").json()["state"]["positions"][CARD_ID]
    assert item["position"] == {"mode": "docx", "page": 3}
    assert set(item["position"]).issubset(set(POSITION_KEYS))


def test_board_preview_rejects_other_attachment_with_409(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    other = _upload(client, samples["xlsx"])
    _board_with_card(client, att["id"])
    resp = client.put(
        f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}",
        json={"attachmentId": other["id"], "version": "v", "position": {"mode": "xlsx"}},
    )
    assert resp.status_code == 409
    assert "不一致" in resp.text


def test_board_preview_does_not_fall_back_to_meta_image(client: TestClient, samples):
    """旧卡片把附件登记在 meta.image：卡片附件 id 只认 meta.file（回退已删除）。

    否则一张**没有 meta.file** 的卡会因为残留的 meta.image 被当成关联了附件，
    把阅读位置写到它并不是真的在用的附件上。
    """
    att = _upload(client, samples["docx"])
    conn = client.app.state.ctx.conn
    state = {
        "boardId": BOARD_ID,
        "seq": 0,
        "updatedAt": "2026-10-12T00:00:00+00:00",
        "cards": [
            {
                "id": CARD_ID,
                "kind": "file",
                "content": "",
                "meta": {
                    "name": "旧的.png",
                    "image": {"name": "旧的.png", "attachmentId": att["id"]},
                },
                "x": 0,
                "y": 0,
                "w": 320,
                "h": 200,
                "checked": False,
                "hidden": False,
                "folded": False,
                "bookmarked": False,
                "deleted": False,
                "createdAt": "2026-10-12T00:00:00+00:00",
                "updatedAt": "2026-10-12T00:00:00+00:00",
            }
        ],
        "groups": [],
        "links": [],
        "selection": [],
    }
    board_store.save_board(conn, BOARD_ID, state, reason="test meta.image fallback")
    resp = client.put(
        f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}",
        json={"attachmentId": att["id"], "version": "v", "position": {"mode": "docx"}},
    )
    assert resp.status_code == 409, resp.text
    assert "不一致" in resp.text


def test_board_preview_unknown_card_is_404(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    _board_with_card(client, att["id"])
    resp = client.put(
        f"/api/interactive/boards/{BOARD_ID}/preview/card_nope",
        json={"attachmentId": att["id"], "version": "v", "position": {}},
    )
    assert resp.status_code == 404


def test_board_preview_stale_version_is_not_written(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    _board_with_card(client, att["id"])
    version = client.get(f"/api/attachments/{att['id']}/preview").json()["version"]

    client.put(
        f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}",
        json={"attachmentId": att["id"], "version": version, "position": {"mode": "docx", "page": 5}},
    )

    # 文件被改 / 重新关联：版本变了
    with transaction(client.app.state.ctx.conn):
        client.app.state.ctx.conn.execute(
            "UPDATE attachments SET sha256 = ? WHERE id = ?", ("b" * 64, att["id"])
        )
    stale = client.put(
        f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}",
        json={"attachmentId": att["id"], "version": version, "position": {"mode": "docx", "page": 99}},
    )
    assert stale.status_code == 200
    assert stale.json()["position"] == {}  # 旧版本的位置绝不写到新版本上
    assert stale.json()["version"] == "sha256:" + "b" * 64

    # 读回来的也是默认位置 + 当前版本；库里那一行（旧版本）没有被改写
    item = client.get(f"/api/interactive/boards/{BOARD_ID}/preview").json()["state"]["positions"][CARD_ID]
    assert item["position"] == {}
    assert item["version"] == "sha256:" + "b" * 64
    row = client.app.state.ctx.conn.execute(
        "SELECT version, position FROM board_preview_state WHERE board_id = ? AND card_id = ?",
        (BOARD_ID, CARD_ID),
    ).fetchone()
    assert row["version"] == version
    assert json.loads(row["position"]) == {"mode": "docx", "page": 5}


def test_board_preview_delete_reports_the_fact(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    _board_with_card(client, att["id"])
    version = client.get(f"/api/attachments/{att['id']}/preview").json()["version"]
    client.put(
        f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}",
        json={"attachmentId": att["id"], "version": version, "position": {"mode": "docx"}},
    )
    first = client.delete(f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}")
    assert first.status_code == 200
    assert first.json() == {"ok": True, "deleted": True}
    second = client.delete(f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}")
    assert second.json() == {"ok": True, "deleted": False}
    assert client.get(f"/api/interactive/boards/{BOARD_ID}/preview").json()["state"]["positions"] == {}


def test_board_preview_put_requires_attachment_id(client: TestClient, samples):
    att = _upload(client, samples["docx"])
    _board_with_card(client, att["id"])
    resp = client.put(
        f"/api/interactive/boards/{BOARD_ID}/preview/{CARD_ID}",
        json={"version": "v", "position": {}},
    )
    assert resp.status_code == 400


def test_normalize_position_only_keeps_contract_keys():
    assert normalize_position(None) == {}
    assert normalize_position("nope") == {}
    assert normalize_position({"mode": "gif", "page": 1}) == {"page": 1}
    assert normalize_position({"mode": "pdf", "page": -1, "slide": 2}) == {
        "mode": "pdf",
        "slide": 2,
    }
    assert normalize_position({"zoom": "2"}) == {}
    assert normalize_position({"scrollTop": True}) == {}
