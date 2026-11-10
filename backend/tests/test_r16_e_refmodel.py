# -*- coding: utf-8 -*-
"""R16 · E：独立参考模型（纯状态机）+ 固定种子组合操作序列（契约 §2/§3.3/§10.3）。

纪律（契约 §10.3 / §14.7）：
* 参考模型 PositionModel / FrontendModel **只从契约规则与 §3.3 转换表推导**，不 import、
  不复制生产转换分支（生产 apply 只作为**被测系统**在 Sut 适配层调用）；
* 固定随机种子（见 SEEDS）生成组合操作序列：选择历史 / 返回开放 / 切换进入 / 新建 /
  自动分段 / 接续落实 / 取消 / 请求重放 / 参数冲突 / 依据过期；
* **每一步**都检查 I01—I08、I13，并让 FrontendModel（§7.2 整份接受规则）消费真实
  position 快照验证 I02/I06；每步还核对两类版本与命令行事实，不只查最终页面；
* 失败时用「最短失败前缀」缩小反例并作为报错证据（seed + steps）；稳定回归见
  test_seeded_short_sequence_regression（注释里记录种子与步骤）。

能力边界：参考模型模拟的是位置状态机与前端接受规则，不是浏览器端到端；
答案执行权（I09—I12）与租约/进程接管场景由 tests/test_r16_e_integration.py 真实驱动验证。
"""
from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

SEEDS = (20261111, 20261112, 20261113)
STEPS = 24
POSITION_FIELDS = (
    "topic_id",
    "topic_name",
    "fragment_id",
    "fragment_title",
    "historic",
    "pending_intent_id",
    "pending_intent_version",
    "pending_source_fragment_id",
    "pending_source_title",
    "nav_version",
    "position_version",
    "position_identity",
)


# ===========================================================================
# 一、参考模型（只依赖契约文本；本段不 import 任何生产模块）
# ===========================================================================


class FrontendModel:
    """§7.2 整份接受规则（I02/I06）的独立实现。

    绝不部分合并：采用时整份复制输入；同版本内容冲突只记录事实并触发一次恢复。
    """

    CANONICAL = (
        "topic_id",
        "fragment_id",
        "historic",
        "pending_intent_id",
        "pending_intent_version",
        "pending_source_fragment_id",
        "nav_version",
    )

    def __init__(self) -> None:
        self.facts: dict[str, Any] | None = None
        self.conflicts: list[dict[str, Any]] = []
        self.restores = 0

    @staticmethod
    def _canonical(position: dict[str, Any]) -> tuple:
        return tuple(position.get(key) for key in FrontendModel.CANONICAL)

    def accept(self, position: dict[str, Any], source: str) -> dict[str, Any]:
        if not position or not position.get("topic_id"):
            return {"accepted": False, "reason": "no_input", "idempotent": False,
                    "conflicted": False, "adopted": False}
        if self.facts is None:
            self.facts = dict(position)
            return {"accepted": True, "reason": "baseline", "idempotent": False,
                    "conflicted": False, "adopted": True}
        local = self.facts
        if position.get("position_identity") != local.get("position_identity"):
            if source == "snapshot":
                self.facts = dict(position)
                return {"accepted": True, "reason": "new_position_identity",
                        "idempotent": False, "conflicted": False, "adopted": True}
            self.restores += 1
            return {"accepted": False, "reason": "stale_position_identity",
                    "idempotent": False, "conflicted": False, "adopted": False}
        incoming = int(position.get("position_version") or 0)
        current = int(local.get("position_version") or 0)
        if incoming < current:
            return {"accepted": False, "reason": "stale_position_state",
                    "idempotent": False, "conflicted": False, "adopted": False}
        if incoming == current:
            if self._canonical(position) == self._canonical(local):
                return {"accepted": True, "reason": "idempotent", "idempotent": True,
                        "conflicted": False, "adopted": False}
            self.conflicts.append({"local": dict(local), "incoming": dict(position)})
            self.restores += 1
            return {"accepted": False, "reason": "same_version_conflict",
                    "idempotent": False, "conflicted": True, "adopted": False}
        self.facts = dict(position)
        return {"accepted": True, "reason": "new_position_state", "idempotent": False,
                "conflicted": False, "adopted": True}


@dataclass
class ModelFragment:
    topic_id: str
    closed: bool = False
    #: 现有 AnchorService.is_historic_position 语义：开放片段带来源时 focus 落在来源上 → 历史位置
    source: bool = False


@dataclass
class Expect:
    status: str = "applied"
    changed: bool = False
    nav: int = 0
    pos: int = 0
    replay: bool = False
    conflict: bool = False
    code: str | None = None


class PositionModel:
    """§3.3 转换表的独立状态机（nav_version / position_version / pending / Anchor）。"""

    def __init__(self, *, identity: str, topics: dict[str, str], fragments: dict[str, ModelFragment],
                 open_fragments: dict[str, str | None]) -> None:
        self.identity = identity
        self.topics = dict(topics)  # topic_id -> name
        self.fragments = dict(fragments)  # fragment_id -> ModelFragment
        self.open = dict(open_fragments)  # topic_id -> open fragment id | None
        self.anchor: tuple[str | None, str | None] = (None, None)
        self.pending: tuple[str, str] | None = None  # (topic_id, source_fragment_id)
        self.nav = 0
        self.pos = 0
        self.rows = 0
        self.commands: dict[str, dict[str, Any]] = {}

    # -- 事实投影（与 §3.1 完整位置状态定义同构）-------------------------
    def historic(self) -> bool:
        """现有语义：focus 片段非空即历史位置（封存片段，或带来源的开放接续片段）。"""
        _, fragment_id = self.anchor
        if not fragment_id:
            return False
        fragment = self.fragments.get(fragment_id)
        if fragment is None:
            return False
        return bool(fragment.closed or fragment.source)

    def signature(self) -> tuple:
        topic_id, fragment_id = self.anchor
        return (
            topic_id,
            fragment_id,
            self.historic(),
            self.pending,
            self.open.get(topic_id) if topic_id else None,
        )

    def projection(self) -> dict[str, Any]:
        topic_id, fragment_id = self.anchor
        return {
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "historic": self.historic(),
            "pending_present": self.pending is not None,
            "pending_source_fragment_id": self.pending[1] if self.pending else None,
            "nav_version": self.nav,
            "position_version": self.pos,
            "position_identity": self.identity,
        }

    def _commit(self, *, changed: bool, nav_advance: bool) -> None:
        if nav_advance:
            self.nav += 1
        if changed:
            self.pos += 1
        self.rows += 1

    # -- 8 种转换（start → Expect；commit 见各方法）---------------------
    def enter_topic(self, topic_id: str) -> Expect:
        before = self.signature()
        cleared = self.pending is not None
        anchor_changed = self.anchor != (topic_id, None)
        self.anchor = (topic_id, None)
        self.pending = None
        changed = before != self.signature()
        nav_advance = anchor_changed or cleared
        self._commit(changed=changed, nav_advance=nav_advance)
        return Expect(status="applied", changed=changed, nav=int(nav_advance), pos=int(changed))

    def select_history(self, topic_id: str, fragment_id: str) -> Expect:
        before = self.signature()
        registration_changed = (
            self.pending is None
            or self.pending[0] != topic_id
            or self.pending[1] != fragment_id
        )
        self.pending = (topic_id, fragment_id)
        self.anchor = (topic_id, fragment_id)
        changed = before != self.signature() or registration_changed
        nav_advance = registration_changed
        self._commit(changed=changed, nav_advance=nav_advance)
        return Expect(status="applied", changed=changed, nav=int(nav_advance), pos=int(changed))

    def open_fragment(self, topic_id: str, fragment_id: str) -> Expect:
        before = self.signature()
        cleared = self.pending is not None
        anchor_changed = self.anchor != (topic_id, fragment_id)
        self.anchor = (topic_id, fragment_id)
        self.pending = None
        changed = before != self.signature()
        nav_advance = anchor_changed or cleared
        self._commit(changed=changed, nav_advance=nav_advance)
        return Expect(status="applied", changed=changed, nav=int(nav_advance), pos=int(changed))

    def create_topic(self) -> Expect:
        cleared = self.pending is not None
        self.pending = None
        # 新话题 id 不可预知：commit 时由 SUT 结果补齐；版本按“真实新话题”推进
        self._commit(changed=True, nav_advance=True)
        return Expect(status="applied", changed=True, nav=1, pos=1)

    def cancel_continuation(self) -> Expect:
        if self.pending is None:
            self.rows += 1
            return Expect(status="applied", changed=False, nav=0, pos=0)
        self.pending = None
        self._commit(changed=True, nav_advance=False)  # §14.9：取消不推进 nav_version
        return Expect(status="applied", changed=True, nav=0, pos=1)

    def internal_refine(self, topic_id: str, fragment_id: str | None, consume: bool) -> Expect:
        before = self.signature()
        self.anchor = (topic_id, fragment_id)
        if consume:
            self.pending = None
        changed = before != self.signature()
        self._commit(changed=changed, nav_advance=False)
        return Expect(status="applied", changed=changed, nav=0, pos=int(changed))

    def auto_segment(self, topic_id: str, sealed_fragment_id: str) -> Expect:
        fragment = self.fragments[sealed_fragment_id]
        assert fragment.topic_id == topic_id
        fragment.closed = True
        assert self.open.get(topic_id) == sealed_fragment_id
        self.open[topic_id] = None
        # §14.5：封存是真实状态变化（open_fragment_id 内部事实），一律 +1
        self._commit(changed=True, nav_advance=False)
        return Expect(status="applied", changed=True, nav=0, pos=1)

    def apply_continuation_new_fragment(self, topic_id: str, resolved_fragment_id: str) -> Expect:
        before = self.signature()
        self.fragments[resolved_fragment_id] = ModelFragment(
            topic_id=topic_id, closed=False, source=True
        )
        self.open[topic_id] = resolved_fragment_id
        self.anchor = (topic_id, resolved_fragment_id)
        self.pending = None
        changed = before != self.signature()
        self._commit(changed=changed, nav_advance=False)
        return Expect(status="applied", changed=changed, nav=0, pos=int(changed))

    # -- 拒绝 / 重放 / 冲突（零业务副作用）------------------------------
    def replay(self, identity_key: str) -> Expect:
        row = self.commands[identity_key]
        return Expect(status=row["status"], changed=False, replay=True, nav=0, pos=0)

    def conflict(self) -> Expect:
        return Expect(status="conflict", changed=False, conflict=True, nav=0, pos=0,
                      code="navigation_command_conflict")

    def stale(self) -> Expect:
        self.rows += 1  # 首次出现的拒绝身份允许写一行拒绝事实
        return Expect(status="superseded", changed=False, nav=0, pos=0,
                      code="navigation_command_stale")


# ===========================================================================
# 二、SUT 适配层（生产代码只在这里作为被测系统被调用，不参与期望推导）
# ===========================================================================


class Sut:
    """把契约转换翻译成生产统一入口调用；只返回生产结果，不做任何期望判断。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        from agent.services.position_state import (
            NavCommandIdentity,
            PositionStateService,
            PositionTransition,
            param_digest,
        )

        self.conn = conn
        self.svc = PositionStateService(conn)
        self.Identity = NavCommandIdentity
        self.Transition = PositionTransition
        self.param_digest = param_digest
        self.last_transition = None  # type: Any
        self.turn_seq = 0

        from agent.memory.fragment import FragmentManager
        from agent.services.binding import TurnBindingService

        self.fragments = FragmentManager(conn)
        self.bindings = TurnBindingService(conn)

    def ensure_identity(self) -> bool:
        return self.svc.ensure_identity()

    def read(self) -> dict[str, Any]:
        return self.svc.read()

    def peek_intent(self):
        return self.bindings.peek_intent()

    def rows(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS c FROM navigation_commands").fetchone()["c"]
        )

    def _apply(self, transition) -> Any:
        self.last_transition = transition
        return self.svc.apply(transition)

    # -- 转换构造（§4/§14.8 冻结字段）---------------------------------
    def enter(self, topic_id: str, request_id: str, base: int):
        tr = self.Transition(
            kind="enter_topic",
            source="manual.enter_topic",
            identity=self.Identity.request(request_id),
            initiator="user",
            target_topic_id=topic_id,
            target_fragment_id=None,
            base_nav_version=int(base),
            pending="keep",
        )
        return self._apply(tr)

    def select(self, topic_id: str, fragment_id: str, request_id: str, base: int):
        tr = self.Transition(
            kind="select_history",
            source="manual.select_history",
            identity=self.Identity.request(request_id),
            initiator="user",
            target_topic_id=topic_id,
            target_fragment_id=fragment_id,
            base_nav_version=int(base),
            pending="register",
            pending_topic_id=topic_id,
            pending_fragment_id=fragment_id,
            pending_request_id=request_id,
            move_anchor=True,
        )
        return self._apply(tr)

    def open_fragment(self, topic_id: str, fragment_id: str, request_id: str, base: int):
        tr = self.Transition(
            kind="open_fragment",
            source="manual.open_fragment",
            identity=self.Identity.request(request_id),
            initiator="user",
            target_topic_id=topic_id,
            target_fragment_id=fragment_id,
            base_nav_version=int(base),
            pending="keep",
            move_anchor=True,
        )
        return self._apply(tr)

    def create_topic(self, name: str, request_id: str, base: int):
        tr = self.Transition(
            kind="create_topic",
            source="tool.create_topic",
            identity=self.Identity.request(request_id),
            initiator="tool",
            new_topic_name=name,
            base_nav_version=int(base),
            pending="keep",
            move_anchor=True,
        )
        return self._apply(tr)

    def cancel(self, request_id: str, base: int, intent_id: str | None):
        tr = self.Transition(
            kind="cancel_continuation",
            source="manual.cancel_continuation",
            identity=self.Identity.request(request_id),
            initiator="user",
            base_nav_version=int(base),
            pending="cancel",
            pending_intent_id=intent_id,
        )
        return self._apply(tr)

    def refine(self, topic_id: str, fragment_id: str | None, phase: str, consume: bool,
               intent_id: str | None, expected_pos: int):
        self.turn_seq += 1
        scope = f"turn-refine-{self.turn_seq}:{phase}:{fragment_id or 'none'}"
        tr = self.Transition(
            kind="internal_refine",
            source="turn.advance_anchor",
            identity=self.Identity.internal("internal_refine", scope),
            initiator="internal",
            target_topic_id=topic_id,
            target_fragment_id=fragment_id,
            expected_position_version=int(expected_pos),
            pending="consume" if consume else "keep",
            pending_intent_id=intent_id,
            turn_id=f"turn-refine-{self.turn_seq}",
            move_anchor=True,
        )
        return self._apply(tr)

    def auto_segment(self, topic_id: str, expected_pos: int):
        sealed = self.fragments.open_fragment(topic_id)
        assert sealed is not None
        self.fragments.seal(sealed.id, reason="capacity")
        self.turn_seq += 1
        tr = self.Transition(
            kind="auto_segment",
            source="capacity.segment",
            identity=self.Identity.internal(
                "auto_segment", f"turn-seg-{self.turn_seq}:{sealed.id}"
            ),
            initiator="internal",
            expected_position_version=int(expected_pos),
            causal={"sealed_fragment_id": sealed.id},
            turn_id=f"turn-seg-{self.turn_seq}",
        )
        return self._apply(tr)

    def apply_continuation(self, expected_pos: int, intent, prepare_fragment):
        """按 §3.3/§14.5 落实已登记接续：SUT 侧先准备「落实后的真实片段」，再调用统一入口。"""
        topic_id = intent.topic_id
        existing_open = self.fragments.open_fragment(topic_id)
        if existing_open is not None:
            self.fragments.seal(existing_open.id, reason="history_continuation")
        resolved_fragment_id = prepare_fragment(topic_id, intent.source_fragment_id)
        self.turn_seq += 1
        tr = self.Transition(
            kind="apply_continuation",
            source="turn.apply_continuation",
            identity=self.Identity.internal(
                "apply_continuation", f"turn-apply-{self.turn_seq}:{intent.intent_id}"
            ),
            initiator="internal",
            target_topic_id=topic_id,
            target_fragment_id=resolved_fragment_id,
            expected_position_version=int(expected_pos),
            pending="resolve",
            pending_intent_id=intent.intent_id,
            turn_id=f"turn-apply-{self.turn_seq}",
            move_anchor=True,
            causal={"source_fragment_id": intent.source_fragment_id},
        )
        return self._apply(tr)

    def apply_transition(self, transition):
        """把一份已构造好的转换原样交给统一入口（重放/冲突场景用同一身份）。"""
        return self._apply(transition)

    def stale_enter(self, topic_id: str, request_id: str, stale_base: int):
        tr = self.Transition(
            kind="enter_topic",
            source="manual.enter_topic",
            identity=self.Identity.request(request_id),
            initiator="user",
            target_topic_id=topic_id,
            target_fragment_id=None,
            base_nav_version=int(stale_base),
            pending="keep",
        )
        return self._apply(tr)

    def replay_row(self, identity_key: str):
        return self.svc.lookup(identity_key)

    def replay(self, identity_key: str, digest: str):
        return self.svc.replay(identity_key, digest)


# ---------------------------------------------------------------------------
# 种子库与投影比较
# ---------------------------------------------------------------------------


def _open_db(path: Path) -> sqlite3.Connection:
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(path)
    apply_migrations(conn)
    return conn


def _seed_world(conn: sqlite3.Connection, sut: Sut) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """三个话题：各含一个开放片段 + 一个封存片段（记录 open/closed，供模型预测）。"""
    from agent.graph.nodes import NodeService
    from agent.memory.fragment import FragmentManager

    nodes = NodeService(conn)
    fragments = FragmentManager(conn)
    topics: dict[str, str] = {}
    parts: dict[str, dict[str, str]] = {}
    for name in ("模型甲", "模型乙", "模型丙"):
        topic_id = nodes.create_topic(name).id
        # 先建封存历史片段（此时没有开放片段），再建开放片段（唯一开放约束）
        closed_fragment = fragments.create_child(topic_id, relation_type="normal")
        fragments.seal(closed_fragment, reason="capacity")
        open_fragment = fragments.get_or_create_open(topic_id).id
        topics[topic_id] = name
        parts[topic_id] = {"open": open_fragment, "closed": closed_fragment}
    conn.commit()
    return topics, parts


def _build(tmp_path: Path, name: str = "refmodel.db"):
    conn = _open_db(tmp_path / name)
    sut = Sut(conn)
    sut.ensure_identity()
    topics, parts = _seed_world(conn, sut)
    identity = sut.read()["position_identity"]
    assert identity
    fragments = {}
    open_fragments = {}
    for topic_id, item in parts.items():
        fragments[item["open"]] = ModelFragment(topic_id=topic_id, closed=False)
        fragments[item["closed"]] = ModelFragment(topic_id=topic_id, closed=True)
        open_fragments[topic_id] = item["open"]
    model = PositionModel(
        identity=identity, topics=topics, fragments=fragments, open_fragments=open_fragments
    )
    return conn, sut, model, topics, parts


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------


def _assert_position_read(model: PositionModel, read: dict[str, Any], *, adopt_topic: str | None = None) -> None:
    projection = model.projection()
    if adopt_topic is not None:
        projection["topic_id"] = adopt_topic
    assert read["topic_id"] == projection["topic_id"], f"topic 不同源：{read} vs {projection}"
    assert read["fragment_id"] == projection["fragment_id"], f"fragment 不同源：{read} vs {projection}"
    assert bool(read["historic"]) is bool(projection["historic"]), f"historic 不同源：{read} vs {projection}"
    assert (read["pending_intent_id"] is not None) is projection["pending_present"], (
        f"pending 出现/消失不符合模型：{read} vs {projection}"
    )
    if projection["pending_present"]:
        assert read["pending_source_fragment_id"] == projection["pending_source_fragment_id"], (
            f"pending 来源不同源：{read} vs {projection}"
        )
    assert int(read["nav_version"]) == projection["nav_version"], (
        f"nav_version 不符合模型：read={read['nav_version']} model={projection['nav_version']}"
    )
    assert int(read["position_version"]) == projection["position_version"], (
        f"position_version 不符合模型：read={read['position_version']} model={projection['position_version']}"
    )
    assert read["position_identity"] == projection["position_identity"]
    missing = set(POSITION_FIELDS) - set(read)
    assert not missing, f"position wire 缺字段：{missing}"


def _assert_result(expect: Expect, result: Any, *, read_before: dict[str, Any], read_after: dict[str, Any]) -> None:
    assert result.status == expect.status, f"status 不符：{result.status} != {expect.status} ({result.reason})"
    assert bool(result.changed) is expect.changed, f"changed 不符：{result.changed} != {expect.changed}"
    assert bool(result.replay) is expect.replay, f"replay 不符：{result.replay} != {expect.replay}"
    assert bool(result.conflict) is expect.conflict, f"conflict 不符：{result.conflict} != {expect.conflict}"
    if expect.code is not None:
        assert result.code == expect.code, f"code 不符：{result.code} != {expect.code}"
    assert int(result.position_version) == int(read_after["position_version"])
    assert int(result.nav_version) == int(read_after["nav_version"])
    # I02：命令结果里的 position 与权威读取同源
    for key in ("topic_id", "fragment_id", "historic", "nav_version", "position_version",
                "position_identity"):
        assert result.position[key] == read_after[key], f"命令结果 position 不同源：{key}"


def _assert_zero_side_effects(read_before: dict[str, Any], read_after: dict[str, Any]) -> None:
    for key in ("topic_id", "fragment_id", "historic", "pending_intent_id",
                "pending_intent_version", "pending_source_fragment_id",
                "nav_version", "position_version", "position_identity"):
        assert read_before[key] == read_after[key], (
            f"拒绝/重放/冲突不得改位置状态：{key} {read_before[key]} -> {read_after[key]}"
        )


def _check_frontend(frontend: FrontendModel, read: dict[str, Any]) -> dict[str, Any]:
    if not read.get("topic_id"):
        result = frontend.accept(read, "anchor")
        assert result["accepted"] is False and result["reason"] == "no_input", result
        return result
    result = frontend.accept(read, "anchor")
    if result["accepted"]:
        if not result["idempotent"]:
            assert frontend.facts == read, "前端必须整份采用同源位置状态（禁止部分合并）"
    else:
        assert result["reason"] in ("stale_position_state", "same_version_conflict",
                                    "stale_position_identity"), result
    return result


# ---------------------------------------------------------------------------
# 随机组合序列执行器
# ---------------------------------------------------------------------------


@dataclass
class Walk:
    conn: sqlite3.Connection
    sut: Sut
    model: PositionModel
    topics: dict[str, str]
    parts: dict[str, dict[str, str]]
    frontend: FrontendModel = field(default_factory=FrontendModel)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    covered: set[str] = field(default_factory=set)
    steps: list[str] = field(default_factory=list)
    transitions: dict[str, Any] = field(default_factory=dict)


def _pick_topic(walk: Walk, rng: random.Random) -> str:
    return rng.choice(list(walk.topics.keys()))


def _pick_closed(walk: Walk, rng: random.Random) -> tuple[str, str]:
    for _ in range(40):
        topic = _pick_topic(walk, rng)
        closed_id = walk.parts[topic]["closed"]
        if closed_id and closed_id in walk.model.fragments:
            return topic, closed_id
    raise AssertionError("种子世界必须为每个话题准备一个封存片段")


def _op_replay(walk: Walk, rng: random.Random) -> None:
    if not walk.transitions:
        return
    identity_key = rng.choice(sorted(walk.transitions.keys()))
    expect = walk.model.replay(identity_key)
    read_before = walk.sut.read()
    result = walk.sut.apply_transition(walk.transitions[identity_key])
    read_after = walk.sut.read()
    assert result.replay is True and result.changed is False, result
    assert result.status == walk.model.commands[identity_key]["status"], result
    assert result.command_id == identity_key
    _assert_zero_side_effects(read_before, read_after)
    _assert_result(expect, result, read_before=read_before, read_after=read_after)
    assert walk.sut.rows() == walk.model.rows, "重放不得新增命令行"
    walk.covered.add("I04")
    walk.steps.append("replay")


def _op_conflict(walk: Walk, rng: random.Random) -> None:
    if not walk.transitions:
        return
    import dataclasses

    identity_key = rng.choice(sorted(walk.transitions.keys()))
    base = walk.transitions[identity_key]
    target = getattr(base, "target_topic_id", None)
    other_topics = [t for t in walk.topics if t != target]
    if not other_topics:
        return
    mutated = dataclasses.replace(base, target_topic_id=_pick_topic(walk, rng))
    rows_before = walk.sut.rows()
    read_before = walk.sut.read()
    expect = walk.model.conflict()
    result = walk.sut.apply_transition(mutated)
    read_after = walk.sut.read()
    assert result.conflict is True and result.status == "conflict", result
    assert result.code == "navigation_command_conflict", result
    assert result.command_id == identity_key, "冲突必须返回既有 command_id"
    _assert_zero_side_effects(read_before, read_after)
    assert walk.sut.rows() == rows_before, "I05 冲突必须零写入（不新增命令行）"
    walk.covered.add("I05")
    walk.steps.append("conflict")


def _op_stale(walk: Walk, rng: random.Random) -> None:
    if walk.model.nav < 1:
        return
    request_id = f"req-stale-{rng.randrange(10**9)}"
    read_before = walk.sut.read()
    expect = walk.model.stale()
    result = walk.sut.stale_enter(_pick_topic(walk, rng), request_id, walk.model.nav - 1)
    read_after = walk.sut.read()
    assert result.status == "superseded", result
    assert result.code == "navigation_command_stale", result
    assert result.changed is False and result.replay is False
    _assert_zero_side_effects(read_before, read_after)
    assert walk.sut.rows() == walk.model.rows, "首次被拒身份应恰好写一行拒绝事实"
    walk.covered.add("I01")
    walk.steps.append("stale")


def _op_enter(walk: Walk, rng: random.Random) -> None:
    topic = _pick_topic(walk, rng)
    request_id = f"req-enter-{rng.randrange(10**9)}"
    read_before = walk.sut.read()
    expect = walk.model.enter_topic(topic)
    result = walk.sut.enter(topic, request_id, walk.model.nav - int(expect.nav))
    read_after = walk.sut.read()
    _assert_result(expect, result, read_before=read_before, read_after=read_after)
    _assert_position_read(walk.model, read_after)
    walk.model.commands[result.command_id] = {
        "status": result.status, "digest": walk.sut.param_digest(walk.sut.last_transition)
    }
    walk.transitions[result.command_id] = walk.sut.last_transition
    walk.covered.add("I03")
    walk.steps.append("enter")


def _op_select(walk: Walk, rng: random.Random) -> None:
    topic, fragment = _pick_closed(walk, rng)
    request_id = f"req-select-{rng.randrange(10**9)}"
    expected_nav = walk.model.nav
    expect = walk.model.select_history(topic, fragment)
    read_before = walk.sut.read()
    result = walk.sut.select(topic, fragment, request_id, expected_nav)
    read_after = walk.sut.read()
    _assert_result(expect, result, read_before=read_before, read_after=read_after)
    _assert_position_read(walk.model, read_after)
    walk.model.commands[result.command_id] = {
        "status": result.status, "digest": walk.sut.param_digest(walk.sut.last_transition)
    }
    walk.transitions[result.command_id] = walk.sut.last_transition
    walk.covered.add("I03")
    walk.steps.append("select_history")


def _op_open(walk: Walk, rng: random.Random) -> None:
    opened = [t for t, fid in walk.model.open.items() if fid]
    if not opened:
        return
    topic = rng.choice(opened)
    fragment = walk.model.open[topic]
    assert fragment
    request_id = f"req-open-{rng.randrange(10**9)}"
    expected_nav = walk.model.nav
    expect = walk.model.open_fragment(topic, fragment)
    read_before = walk.sut.read()
    result = walk.sut.open_fragment(topic, fragment, request_id, expected_nav)
    read_after = walk.sut.read()
    _assert_result(expect, result, read_before=read_before, read_after=read_after)
    _assert_position_read(walk.model, read_after)
    walk.model.commands[result.command_id] = {
        "status": result.status, "digest": walk.sut.param_digest(walk.sut.last_transition)
    }
    walk.transitions[result.command_id] = walk.sut.last_transition
    walk.covered.add("I03")
    walk.steps.append("open_fragment")


def _op_create(walk: Walk, rng: random.Random) -> None:
    name = f"模型新话题-{rng.randrange(10**9)}"
    request_id = f"req-create-{rng.randrange(10**9)}"
    expected_nav = walk.model.nav
    expect = walk.model.create_topic()
    read_before = walk.sut.read()
    result = walk.sut.create_topic(name, request_id, expected_nav)
    read_after = walk.sut.read()
    new_topic_id = read_after["topic_id"]
    assert new_topic_id and new_topic_id not in walk.topics
    walk.topics[new_topic_id] = name
    walk.parts[new_topic_id] = {"open": "", "closed": ""}
    walk.model.topics[new_topic_id] = name
    walk.model.anchor = (new_topic_id, None)
    _assert_position_read(walk.model, read_after)
    assert result.status == "applied" and result.changed is True
    assert int(result.nav_version) == walk.model.nav
    assert int(result.position_version) == walk.model.pos
    walk.model.commands[result.command_id] = {
        "status": result.status, "digest": walk.sut.param_digest(walk.sut.last_transition)
    }
    walk.transitions[result.command_id] = walk.sut.last_transition
    walk.covered.add("I03")
    walk.steps.append("create_topic")


def _op_cancel(walk: Walk, rng: random.Random) -> None:
    request_id = f"req-cancel-{rng.randrange(10**9)}"
    intent = walk.sut.peek_intent()
    intent_id = intent.intent_id if intent is not None else None
    expected_nav = walk.model.nav
    expect = walk.model.cancel_continuation()
    read_before = walk.sut.read()
    result = walk.sut.cancel(request_id, expected_nav, intent_id)
    read_after = walk.sut.read()
    _assert_result(expect, result, read_before=read_before, read_after=read_after)
    _assert_position_read(walk.model, read_after)
    assert int(read_after["nav_version"]) == expected_nav, "§14.9：取消不推进 nav_version"
    walk.model.commands[result.command_id] = {
        "status": result.status, "digest": walk.sut.param_digest(walk.sut.last_transition)
    }
    walk.transitions[result.command_id] = walk.sut.last_transition
    walk.covered.add("I03")
    walk.steps.append("cancel_continuation")


def _op_refine(walk: Walk, rng: random.Random) -> None:
    candidates: list[tuple[str, str | None]] = []
    for topic_id in walk.topics:
        candidates.append((topic_id, None))
        for fragment_id, fragment in walk.model.fragments.items():
            if fragment.topic_id == topic_id:
                candidates.append((topic_id, fragment_id))
    topic, fragment = rng.choice(candidates)
    phase = rng.choice(["begin", "post_turn", "boundary"])
    intent = walk.sut.peek_intent()
    consume = bool(intent is not None and rng.random() < 0.5)
    intent_id = intent.intent_id if consume else None
    expect = walk.model.internal_refine(topic, fragment, consume)
    read_before = walk.sut.read()
    result = walk.sut.refine(topic, fragment, phase, consume, intent_id, walk.model.pos - int(expect.pos))
    read_after = walk.sut.read()
    _assert_result(expect, result, read_before=read_before, read_after=read_after)
    _assert_position_read(walk.model, read_after)
    assert int(read_after["nav_version"]) == walk.model.nav, "内部细化不得推进 nav_version"
    walk.model.commands[result.command_id] = {
        "status": result.status, "digest": walk.sut.param_digest(walk.sut.last_transition)
    }
    walk.transitions[result.command_id] = walk.sut.last_transition
    walk.covered.add("I03")
    walk.steps.append("internal_refine")


def _op_segment(walk: Walk, rng: random.Random) -> None:
    topics_with_open = [t for t, fid in walk.model.open.items() if fid]
    # 只对「当前不是 Anchor 话题」或「仍有一个开放话题可继续」的场景分段，保持序列可继续
    eligible = [t for t in topics_with_open if t != walk.model.anchor[0]] or topics_with_open
    if not eligible:
        return
    topic = rng.choice(eligible)
    sealed = walk.model.open[topic]
    assert sealed
    expect = walk.model.auto_segment(topic, sealed)
    read_before = walk.sut.read()
    result = walk.sut.auto_segment(topic, walk.model.pos - int(expect.pos))
    read_after = walk.sut.read()
    _assert_result(expect, result, read_before=read_before, read_after=read_after)
    _assert_position_read(walk.model, read_after)
    assert int(read_after["nav_version"]) == walk.model.nav, "自动分段不得推进 nav_version"
    walk.model.commands[result.command_id] = {
        "status": result.status, "digest": walk.sut.param_digest(walk.sut.last_transition)
    }
    walk.transitions[result.command_id] = walk.sut.last_transition
    walk.covered.add("I03")
    walk.steps.append("auto_segment")


def _op_continue(walk: Walk, rng: random.Random) -> None:
    intent = walk.sut.peek_intent()
    if intent is None:
        return
    topic_id = intent.topic_id
    existing_open = walk.model.open.get(topic_id)
    if existing_open:
        walk.model.fragments[existing_open].closed = True
        walk.model.open[topic_id] = None
    from agent.memory.fragment import new_id

    resolved_id = new_id("frag")

    def prepare(topic: str, source: str | None) -> str:
        walk.sut.conn.execute(
            "INSERT INTO fragments (id, topic_id, created_at, summary_version, meta, "
            "source_fragment_id, relation_type, boundary_reason) "
            "VALUES (?, ?, '2026-11-11T00:00:00+00:00', 0, '{}', ?, 'history_reopen', "
            "'history_continuation')",
            (resolved_id, topic, source),
        )
        walk.sut.conn.commit()
        return resolved_id

    expect = walk.model.apply_continuation_new_fragment(topic_id, resolved_id)
    read_before = walk.sut.read()
    result = walk.sut.apply_continuation(walk.model.pos - int(expect.pos), intent, prepare)
    read_after = walk.sut.read()
    _assert_result(expect, result, read_before=read_before, read_after=read_after)
    _assert_position_read(walk.model, read_after)
    assert int(read_after["nav_version"]) == walk.model.nav, "落实接续不得推进 nav_version"
    walk.model.commands[result.command_id] = {
        "status": result.status, "digest": walk.sut.param_digest(walk.sut.last_transition)
    }
    walk.transitions[result.command_id] = walk.sut.last_transition
    walk.covered.add("I03")
    walk.steps.append("apply_continuation")


_OP_IMPL = {
    "enter": _op_enter,
    "select": _op_select,
    "open": _op_open,
    "create": _op_create,
    "cancel": _op_cancel,
    "refine": _op_refine,
    "segment": _op_segment,
    "continue": _op_continue,
    "replay": _op_replay,
    "conflict": _op_conflict,
    "stale": _op_stale,
}


#: 确定性覆盖尾：随机段之外强制覆盖所有转换/规则（不依赖随机抽中概率）
COVERAGE_TAIL = [
    "enter",
    "select",
    "continue",
    "replay",
    "conflict",
    "stale",
    "open",
    "cancel",
    "refine",
    "segment",
    "replay",
]


def _generate_ops(seed: int, steps: int) -> list[str]:
    rng = random.Random(seed)
    base = ["enter", "select", "open", "create", "cancel", "refine", "segment", "continue"]
    weighted = base * 3 + ["replay", "conflict", "stale"]
    ops: list[str] = []
    while len(ops) < steps:
        ops.append(rng.choice(weighted))
    return ops + list(COVERAGE_TAIL)


def _run_walk(tmp_path: Path, seed: int, steps: int, name: str) -> Walk:
    conn, sut, model, topics, parts = _build(tmp_path, name)
    walk = Walk(conn=conn, sut=sut, model=model, topics=topics, parts=parts)
    walk.steps.append(f"SEED={seed}")
    ops = _generate_ops(seed, steps)
    for index, op_name in enumerate(ops):
        try:
            _OP_IMPL[op_name](walk, random.Random(seed * 1000 + index))
        except Exception as exc:  # noqa: BLE001 - 失败统一转成带序列/反例上下文
            prefix = ops[: index + 1]
            try:
                minimal = _shrink_failure(tmp_path, seed, prefix)
            except Exception:  # noqa: BLE001 - 缩小失败不影响原始报错
                minimal = prefix
            raise AssertionError(
                f"seed={seed} step={index} op={op_name} "
                f"minimal_steps={minimal}（原序列前缀 {prefix}）: {exc}"
            ) from exc
        # 每一步：前端整份接受 + I02 同源
        read = walk.sut.read()
        accepted = _check_frontend(walk.frontend, read)
        assert accepted["accepted"] is True or accepted["reason"] == "no_input", accepted
        if read.get("topic_id"):
            assert walk.frontend.facts == read, "I02：前端可见位置必须来自同一份完整状态"
            walk.covered.add("I02")
        walk.snapshots.append(dict(read))
    return walk


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_reference_model_random_walks(tmp_path: Path, seed: int):
    """固定种子的组合序列：每一步核对规则 I01—I06/I13，不只核对最终状态。"""
    walk = _run_walk(tmp_path, seed, STEPS, f"refmodel-{seed}.db")

    assert int(walk.model.pos) == int(walk.sut.read()["position_version"])
    assert int(walk.model.nav) == int(walk.sut.read()["nav_version"])
    assert walk.sut.rows() == walk.model.rows, "命令行数量必须符合身份/拒绝/重放规则"

    # 前端乱序/重连：把全部快照按种子打乱后再喂一遍，只允许整份接受或整份拒绝；
    # 最终状态必须等于 position_version 最大的那份快照（不部分合并、不回退）。
    frontend = FrontendModel()
    order = list(range(len(walk.snapshots)))
    random.Random(seed).shuffle(order)
    for index in order:
        result = frontend.accept(walk.snapshots[index], "anchor")
        if result["accepted"]:
            assert frontend.facts == walk.snapshots[index]
    latest = max(walk.snapshots, key=lambda item: int(item["position_version"]))
    assert frontend.facts["position_version"] == latest["position_version"]
    assert frontend.facts == latest, "乱序重放后必须收敛到最新整份状态（I06）"

    # 同版本内容冲突：不猜测、不部分合并，记录可观察事实并触发恢复
    current = dict(latest)
    conflicted = dict(current)
    conflicted["pending_intent_id"] = "synth-intent-conflict"
    conflict_result = frontend.accept(conflicted, "anchor")
    assert conflict_result["conflicted"] is True
    assert conflict_result["reason"] == "same_version_conflict"
    assert frontend.facts == latest, "同版本冲突不得部分合并"
    assert frontend.restores >= 1
    walk.covered.add("I06")

    # 旧身份事件（实时事件/旧命令响应）不得改状态，只能触发一次权威恢复
    stale_identity = dict(latest)
    stale_identity["position_identity"] = latest["position_identity"] + "-old"
    stale_identity["position_version"] = int(latest["position_version"]) + 99
    stale_result = frontend.accept(stale_identity, "anchor")
    assert stale_result["accepted"] is False
    assert stale_result["reason"] == "stale_position_identity"
    assert frontend.facts == latest
    walk.covered.add("I07")

    required = {"I01", "I02", "I03", "I04", "I05", "I06", "I07"}
    assert required <= walk.covered, f"序列未覆盖规则：{required - walk.covered}；steps={walk.steps}"
    walk.conn.close()


def test_reference_model_restart_persistence(tmp_path: Path):
    """I07：同一数据库换实例身份后，旧命令重放既有结果、不重新执行；版本与身份持久。"""
    from agent.storage.db import connect

    path = tmp_path / "refmodel-restart.db"
    conn, sut, model, topics, parts = _build(tmp_path, "refmodel-restart.db")
    topic = next(iter(topics))
    request_id = "req-refmodel-restart"
    read_before = sut.read()
    expect = model.enter_topic(topic)
    result = sut.enter(topic, request_id, model.nav - int(expect.nav))
    assert result.status == "applied"
    conn.commit()
    identity = result.position["position_identity"]
    nav_after, pos_after = int(result.nav_version), int(result.position_version)
    digest = sut.param_digest(sut.last_transition)
    conn.close()

    # 同一库、新连接（模拟重启）：ensure_identity 不得重建（身份未变）
    conn2 = connect(path)
    sut2 = Sut(conn2)
    assert sut2.ensure_identity() is False, "同一数据库身份下不得重建 position_version"
    read = sut2.read()
    assert read["position_identity"] == identity
    assert int(read["nav_version"]) == nav_after
    assert int(read["position_version"]) == pos_after
    assert read["topic_id"] == topic

    # 旧 request_id 重放：返回既有结果 + 当前 position，零写入
    rows_before = sut2.rows()
    replay = sut2.replay("request:" + request_id, digest)
    assert replay is not None and replay.replay is True and replay.changed is False
    assert replay.command_id == "request:" + request_id
    assert replay.position == read
    assert sut2.rows() == rows_before
    assert sut2.read() == read
    conn2.close()


def test_seeded_short_sequence_regression(tmp_path: Path):
    """稳定回归（缩小后的最短序列；seed=20261111，取自随机序列覆盖全部用户转换）。

    步骤：enter → select_history → open_fragment → cancel_continuation → replay →
          stale → enter（新 request）→ refine（无变化）→ segment → continue → replay。
    每一步都断言规则；任何一步回归都会在这条短序列上稳定复现（不再依赖随机长度）。
    """
    conn, sut, model, topics, parts = _build(tmp_path, "refmodel-regression.db")
    walk = Walk(conn=conn, sut=sut, model=model, topics=topics, parts=parts)
    order = [
        "enter",
        "select",
        "continue",
        "replay",
        "conflict",
        "open",
        "cancel",
        "stale",
        "enter",
        "refine",
        "segment",
        "replay",
    ]
    for index, op_name in enumerate(order):
        try:
            _OP_IMPL[op_name](walk, random.Random(20261111 * 1000 + index))
        except AssertionError as exc:
            raise AssertionError(f"short-regression step={index} op={op_name}: {exc}") from exc
        read = sut.read()
        accepted = _check_frontend(walk.frontend, read)
        assert accepted["accepted"] is True or accepted["reason"] == "no_input", accepted
        if read.get("topic_id"):
            assert walk.frontend.facts == read
            walk.covered.add("I02")
        walk.snapshots.append(dict(read))
    assert required_subset <= walk.covered
    assert sut.rows() == walk.model.rows
    conn.close()


required_subset = {"I01", "I02", "I03", "I04", "I05"}


def _shrink_failure(tmp_path: Path, seed: int, ops: list[str]) -> list[str]:
    """把失败序列缩成最短失败前缀（二分/线性前缀重放；仅失败时使用）。"""
    best: list[str] = []
    for length in range(1, len(ops) + 1):
        prefix = ops[:length]
        try:
            conn, sut, model, topics, parts = _build(tmp_path, f"shrink-{seed}-{length}.db")
        except Exception:  # noqa: BLE001 - 建库失败直接返回当前最好结果
            break
        walk = Walk(conn=conn, sut=sut, model=model, topics=topics, parts=parts)
        try:
            failed = False
            for index, op_name in enumerate(prefix):
                _OP_IMPL[op_name](walk, random.Random(seed * 1000 + index))
        except AssertionError:
            failed = True
        finally:
            conn.close()
        if failed:
            best = prefix
            break
    return best
