"""OnboardingService：首次引导（欢迎页）的状态与落库。

spec: docs/superpowers/specs/2026-08-18-onboarding-design.md
追加规则（2026-09-23）：只要「本版本还没展示过欢迎页」，即便 onboarding 已完成，
也强制展开一次 —— `show_wizard = (not wizard_seen) or (welcome_version != app_version)`。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService
from agent.graph.nodes import NodeService
from agent.knowledge.lifecycle import KnowledgeItem, KnowledgeService
from agent.storage.settings import SettingsStore

DONE_KEY = "onboarding.done"
SEEN_KEY = "onboarding.wizard_seen"
WELCOME_VERSION_KEY = "onboarding.welcome_version"
HINT_DISMISSED_KEY = "onboarding.hint_dismissed"
DONE_AT_KEY = "onboarding.done_at"
NAME_KEY = "onboarding.name"
SELF_CARD_KEY = "onboarding.self_card_id"

# 「刚更新到这个版本的老用户，无论如何展开一次」——这个门槛是一次性的：
# 只有从低于它的版本升到不低于它的版本时才弹一次，之后的版本更新不再弹。
MIGRATION_VERSION = "0.1.7"


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in (value or "0").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts) or (0,)

# 每个字段一条知识，配上固定前缀：重复提交时按前缀找到同名旧条目并取代它，
# 于是「改掉某一项」不会牵动其他项，也能各自结束。
FIELD_PREFIX = {
    "name": "称呼：",
    "background": "背景：",
    "current_focus": "最近在做：",
    "interests": "长期关注：",
    "familiarity": "熟悉程度：",
    "dont_do": "不要做：",
    "how_to_talk": "表达方式：",
}
PREFERENCE_PREFIX = "偏好（"
GOAL_PREFIX = "目标："

PREFERENCE_LABELS = {
    "verbosity": "详略",
    "tone": "语气",
    "explanation": "解释方式",
    "collaboration": "协作方式",
}


def goal_field_key(goal: str) -> str:
    """目标（多值字段）的稳定身份：同内容 = 同一条，不同目标互不覆盖。"""
    digest = hashlib.sha1(goal.strip().encode("utf-8")).hexdigest()
    return f"goal:{digest}"


def _same_field(item: KnowledgeItem, prefix: str, identity: str) -> bool:
    """这一行是不是「同一项」：优先按稳定身份，旧行退回前缀匹配（兼容已有数据）。"""
    key = (item.provenance or {}).get("field_key")
    if key:
        return str(key) == identity
    return item.content.startswith(prefix)


def _backfill_field_key(conn: sqlite3.Connection, knowledge_id: str, identity: str) -> None:
    """给旧行补上稳定身份，之后的重复提交/纠正都按身份走。"""
    import json

    row = conn.execute(
        "SELECT provenance FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()
    if row is None:
        return
    try:
        prov = json.loads(row["provenance"] or "{}")
    except (TypeError, ValueError):
        prov = {}
    if not isinstance(prov, dict) or prov.get("field_key") == identity:
        return
    prov["field_key"] = identity
    conn.execute(
        "UPDATE knowledge SET provenance = ?, updated_at = ? WHERE id = ?",
        (json.dumps(prov, ensure_ascii=False), _now(), knowledge_id),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OnboardingStatus:
    done: bool
    has_credential: bool
    has_name: bool
    has_content: bool
    wizard_seen: bool
    welcome_version: str
    app_version: str
    show_wizard: bool
    hint_dismissed: bool

    def to_dict(self) -> dict:
        return {
            "done": self.done,
            "has_credential": self.has_credential,
            "has_name": self.has_name,
            "has_content": self.has_content,
            "wizard_seen": self.wizard_seen,
            "welcome_version": self.welcome_version,
            "app_version": self.app_version,
            "show_wizard": self.show_wizard,
            "hint_dismissed": self.hint_dismissed,
        }


class OnboardingService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        app_version: str,
        *,
        main_credential=None,
    ) -> None:
        self.conn = conn
        self.app_version = app_version
        self.settings = SettingsStore(conn)
        # 「有没有配好模型」= 主循环**真的能选中**一把密钥，而不是"表里有记录"：
        # 没有用途标签、被禁用、已撤销、预算用尽的密钥都选不中。
        self._main_credential = main_credential

    def _usable_credential(self) -> bool:
        if self._main_credential is not None:
            try:
                return self._main_credential() is not None
            except Exception:  # noqa: BLE001 - 判定失败时退回保守检查
                pass
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM credentials WHERE status = 'active' AND enabled = 1"
        ).fetchone()
        return row["n"] > 0

    # -- 状态 -------------------------------------------------------------

    def status(self) -> OnboardingStatus:
        # 「主页有没有内容」= 这个客户端**不是空白**：聊过、建过话题、有过知识或实体卡，
        # 任何一样都算。新用户（完全没有内容）不能在密钥那一步跳过，也不能关掉引导；
        # 老用户只是再次运行设置助手时，随时可以关。
        #
        # 早期实现只数消息，导致「有 6 个话题、14 条知识、只是没聊过天」的客户端
        # 被判成新用户、引导关不掉 —— 与产品语义不符。
        content_count = sum(
            self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("messages", "nodes", "knowledge", "entity_cards")
        )
        wizard_seen = self.settings.get_bool(SEEN_KEY, False)
        welcome_version = self.settings.get(WELCOME_VERSION_KEY) or ""
        crossing_migration = (
            _version_tuple(welcome_version)
            < _version_tuple(MIGRATION_VERSION)
            <= _version_tuple(self.app_version)
        )
        return OnboardingStatus(
            done=self.settings.get_bool(DONE_KEY, False),
            has_credential=self._usable_credential(),
            has_name=bool((self.settings.get(NAME_KEY) or "").strip()),
            has_content=content_count > 0,
            wizard_seen=wizard_seen,
            welcome_version=welcome_version,
            app_version=self.app_version,
            show_wizard=(not wizard_seen) or crossing_migration,
            hint_dismissed=self.settings.get_bool(HINT_DISMISSED_KEY, False),
        )

    def mark_seen(self) -> OnboardingStatus:
        """向导一旦打开就记「这个版本已经展示过欢迎页」。"""
        self.settings.set(SEEN_KEY, "1")
        self.settings.set(WELCOME_VERSION_KEY, self.app_version)
        return self.status()

    def complete(self) -> OnboardingStatus:
        self.settings.set(DONE_KEY, "1")
        self.settings.set(DONE_AT_KEY, _now())
        return self.status()

    def set_hint_dismissed(self, dismissed: bool) -> OnboardingStatus:
        self.settings.set(HINT_DISMISSED_KEY, "1" if dismissed else "0")
        return self.status()

    # -- 落库 -------------------------------------------------------------

    def save_profile(
        self,
        *,
        name: str,
        intro: str = "",
        tags: list[dict] | None = None,
        style: str = "",
        goals: list[str] | None = None,
    ) -> dict:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("name is required")
        clean_tags = [
            {"key": str(t.get("key", "")).strip(), "value": str(t.get("value", "")).strip()}
            for t in (tags or [])
            if str(t.get("key", "")).strip() and str(t.get("value", "")).strip()
        ]
        clean_goals = [str(g).strip() for g in (goals or []) if str(g).strip()]

        knowledge_id = self._write_profile_knowledge(clean_name, intro.strip())
        entity_id = self._write_self_card(clean_name, intro.strip(), clean_tags, style.strip())
        topics = self._seed_topics(clean_goals)
        self.settings.set(NAME_KEY, clean_name)
        return {
            "name": clean_name,
            "knowledge_id": knowledge_id,
            "entity_id": entity_id,
            "topics": topics,
        }

    def _write_profile_knowledge(self, name: str, intro: str) -> str:
        """`user_profile` 知识：内容不变则复用，内容变了则 supersede 旧的。"""
        knowledge = KnowledgeService(self.conn)
        active = [
            item
            for item in knowledge.list_items(category="user_profile")
            if item.state.value == "active"
        ]
        content = f"用户称呼：{name}"
        if intro:
            content = f"{content}；自我介绍：{intro}"
        for item in active:
            if item.content == content:
                return item.id
        supersedes = active[0].id if active else None
        item = knowledge.create(
            category="user_profile",
            content=content,
            provenance={"source": "onboarding"},
            supersedes_id=supersedes,
        )
        knowledge.submit(item.id)
        knowledge.verify(item.id, verified_by="user")
        knowledge.activate(item.id)
        return item.id

    def _write_self_card(self, name: str, intro: str, tags: list[dict], style: str) -> str:
        cards = EntityCardService(self.conn)
        existing = cards.find_by_name(name)
        attributes = [
            EntityAttribute(key=t["key"], value=t["value"], confidence=1.0) for t in tags
        ]
        if style:
            attributes.append(EntityAttribute(key="回答风格", value=style, confidence=1.0))
        card = cards.upsert(
            EntityCardCandidate(
                name=name,
                kind="person",
                summary=intro,
                attributes=attributes,
            )
        )
        return card.id if existing is None else existing.id

    def _seed_topics(self, goals: list[str]) -> list[str]:
        nodes = NodeService(self.conn)
        existing = {topic.name for topic in nodes.list_topics()}
        created: list[str] = []
        for goal in goals:
            if goal in existing:
                continue
            nodes.create_topic(goal)
            existing.add(goal)
            created.append(goal)
        return created

    # -- 一次性提交（v2）--------------------------------------------------

    def submit(self, payload: dict) -> dict:
        """核对清单确认后的一次性写入。

        规则：
        - 用户自己填的 → 直接生效（verified_by="user"）；
        - 模型推测的（`inferred`）→ 停在待确认，不参与回答；
        - 每个字段一条知识、挂在「你」或指定话题上；
        - **单值字段**（称呼/背景/最近在做/长期关注/熟悉程度/不要做/表达方式/单值偏好）
          按前缀身份替换旧版本；
        - **多值字段 goals**（M03）按 `goal:<sha1(内容)>` 稳定身份 upsert：
          新目标不撤销其他目标、重复提交不增项、旧目标留在历史链里；
        - `written` 清单按**实际生效结果**生成（`created/reused/superseded/state`），
          不把被本次提交取代的旧条目报成成功。
        """
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")

        user_node_id = NodeService(self.conn).get_or_create_user_root().id
        background = str(payload.get("background") or "").strip()
        focus = str(payload.get("current_focus") or "").strip()
        focus_ended = bool(payload.get("current_focus_ended"))
        interests = [str(i).strip() for i in (payload.get("interests") or []) if str(i).strip()]
        familiarity = str(payload.get("familiarity") or "").strip()
        limits = payload.get("limits") or {}
        preferences = payload.get("preferences") or []
        goals = [str(g).strip() for g in (payload.get("goals") or []) if str(g).strip()]
        inferred = payload.get("inferred") or []

        written: list[dict] = []
        written.append(
            self._put_field(
                "user_profile", FIELD_PREFIX["name"], f"{FIELD_PREFIX['name']}{name}", [user_node_id]
            )
        )
        if background:
            written.append(
                self._put_field(
                    "user_profile",
                    FIELD_PREFIX["background"],
                    f"{FIELD_PREFIX['background']}{background}",
                    [user_node_id],
                )
            )
        if focus:
            written.append(
                self._put_field(
                    "user_profile",
                    FIELD_PREFIX["current_focus"],
                    f"{FIELD_PREFIX['current_focus']}{focus}",
                    [user_node_id],
                    ended=focus_ended,
                )
            )
        if interests:
            written.append(
                self._put_field(
                    "user_profile",
                    FIELD_PREFIX["interests"],
                    f"{FIELD_PREFIX['interests']}{'、'.join(interests)}",
                    [user_node_id],
                )
            )
        if familiarity:
            written.append(
                self._put_field(
                    "user_profile",
                    FIELD_PREFIX["familiarity"],
                    f"{FIELD_PREFIX['familiarity']}{familiarity}",
                    [user_node_id],
                )
            )
        if str(limits.get("dont_do") or "").strip():
            written.append(
                self._put_field(
                    "user_profile",
                    FIELD_PREFIX["dont_do"],
                    f"{FIELD_PREFIX['dont_do']}{str(limits['dont_do']).strip()}",
                    [user_node_id],
                )
            )
        if str(limits.get("how_to_talk") or "").strip():
            written.append(
                self._put_field(
                    "user_profile",
                    FIELD_PREFIX["how_to_talk"],
                    f"{FIELD_PREFIX['how_to_talk']}{str(limits['how_to_talk']).strip()}",
                    [user_node_id],
                )
            )

        for pref in preferences:
            kind = str(pref.get("kind") or "").strip()
            value = str(pref.get("value") or "").strip()
            if not kind or not value:
                continue
            label = PREFERENCE_LABELS.get(kind, kind)
            scope = pref.get("scope") or {}
            if str(scope.get("type") or "global") == "topic" and scope.get("topic_title"):
                topic_title = str(scope["topic_title"]).strip()
                node_ids = [self._topic_id(topic_title)]
                prefix = f"{PREFERENCE_PREFIX}{label}·仅{topic_title}）"
            else:
                node_ids = [user_node_id]
                prefix = f"{PREFERENCE_PREFIX}{label}）"
            written.append(
                self._put_field("user_profile", prefix, f"{prefix}：{value}", node_ids)
            )

        topics: list[str] = []
        for goal in goals:
            # M03：目标是多值字段，每条一个稳定身份（goal:<sha1(内容)>），
            # upsert 语义 —— 新目标不会撤销其他目标，重复提交也不增项。
            written.append(
                self._put_field(
                    "goal",
                    GOAL_PREFIX,
                    f"{GOAL_PREFIX}{goal}",
                    [user_node_id],
                    field_key=goal_field_key(goal),
                    multi_value=True,
                )
            )
            topics.append(self._topic_id(goal))

        pending: list[dict] = []
        for item in inferred:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            pending.append(
                self._put_pending(
                    category=str(item.get("category") or "user_profile"),
                    content=content,
                    node_ids=[user_node_id],
                    reason=str(item.get("reason") or ""),
                )
            )

        self_card_id = self._ensure_self_card(name, background)
        self.settings.set(NAME_KEY, name)
        return {
            "name": name,
            "self_card_id": self_card_id,
            "written": [item for item in written if item],
            "pending": pending,
            "topics": topics,
        }

    def _topic_id(self, title: str) -> str:
        nodes = NodeService(self.conn)
        for topic in nodes.list_topics():
            if topic.name == title:
                return topic.id
        return nodes.create_topic(title).id

    def _put_field(
        self,
        category: str,
        prefix: str,
        content: str,
        node_ids: list[str],
        *,
        ended: bool = False,
        field_key: str | None = None,
        multi_value: bool = False,
    ) -> dict:
        """写一条字段知识：同一「身份」的旧条目被取代（内容一样就复用）。

        - `field_key` 是这一项的身份（例如「偏好（详略）」「目标：<sha1>」），
          所以不同维度、不同适用范围、**不同目标**互不覆盖；
        - `multi_value=True`（目标）只做 upsert：不撤销其他目标，
          旧目标仍留在版本链/历史里，可查可管理；
        - `multi_value=False`（称呼/背景/单值偏好）保持替换语义：新版本取代
          同身份的旧版本，旧行保留在链上（不删除、可追历史）；
        - 旧行没有 `field_key` 时退回前缀匹配，并顺手补上身份（兼容已有数据）；
        - 「已结束」标记只在本次显式要求时添加，**不会**被重复提交清掉。
        """
        identity = field_key or prefix
        knowledge = KnowledgeService(self.conn)
        active = [
            item
            for item in knowledge.list_items(category=category)
            if item.state.value == "active"
        ]
        same = [item for item in active if _same_field(item, prefix, identity)]
        target = next((item for item in same if item.content == content), None)
        created = False
        superseded: str | None = None
        if target is None:
            # 多值字段：绝不取代别的目标；单值字段：取代同身份旧条目
            supersedes = None if multi_value else (same[0].id if same else None)
            superseded = supersedes
            new_item = knowledge.create(
                category=category,
                content=content,
                node_ids=node_ids,
                provenance={"source": "onboarding", "field_key": identity},
                supersedes_id=supersedes,
            )
            knowledge.submit(new_item.id)
            knowledge.verify(new_item.id, verified_by="user")
            target = knowledge.activate(new_item.id)
            created = True
        elif (target.provenance or {}).get("field_key") != identity:
            _backfill_field_key(self.conn, target.id, identity)
        if ended and target is not None and not (target.provenance or {}).get("ended_at"):
            target = knowledge.mark_ended(target.id, reason="onboarding")
        return {
            "id": target.id if target else None,
            "content": content,
            "node_ids": list(node_ids),
            "field": identity,
            "state": target.state.value if target else None,
            "created": created,
            "reused": not created,
            "superseded": superseded,
        }

    def _put_pending(
        self, *, category: str, content: str, node_ids: list[str], reason: str
    ) -> dict:
        """模型推测出来的内容：停在待确认，不参与回答。"""
        from agent.knowledge.lifecycle import CATEGORIES

        safe_category = category if category in CATEGORIES else "general_fact"
        knowledge = KnowledgeService(self.conn)
        existing = [
            item
            for item in knowledge.list_items(category=safe_category)
            if item.content == content and item.state.value == "pending_review"
        ]
        if existing:
            return {"id": existing[0].id, "content": content, "state": "pending_review"}
        created = knowledge.create(
            category=safe_category,
            content=content,
            node_ids=node_ids,
            provenance={"source": "onboarding", "inferred": True, "reason": reason},
        )
        knowledge.submit(created.id)
        return {"id": created.id, "content": content, "state": "pending_review"}

    def _ensure_self_card(self, name: str, summary: str) -> str:
        """始终只有一份「你」：改名时把信息带过去，旧名字记成曾用名。"""
        cards = EntityCardService(self.conn)
        card_id = self.settings.get(SELF_CARD_KEY)
        card = cards.get(card_id) if card_id else None
        if card is None:
            card = cards.find_by_name(name)
        if card is None:
            created = cards.upsert(
                EntityCardCandidate(name=name, kind="person", summary=summary)
            )
            self.settings.set(SELF_CARD_KEY, created.id)
            return created.id

        aliases = list(card.aliases)
        if card.name != name and card.name not in aliases:
            aliases.append(card.name)
        # 以前从对话里学到的别的称呼：并进这一张，不做第二份「你」
        for other in cards.list_active():
            if other.id == card.id:
                continue
            if other.name in {card.name, name} or other.name in aliases:
                if other.name not in aliases:
                    aliases.append(other.name)
                cards.revoke(other.id)
        if card.name != name:
            cards.rename(card.id, name, aliases=aliases)
        elif aliases != card.aliases:
            cards.revise(card.id, aliases=aliases)
        if summary:
            cards.revise(card.id, summary=summary)
        self.settings.set(SELF_CARD_KEY, card.id)
        return card.id
