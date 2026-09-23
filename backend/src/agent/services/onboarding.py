"""OnboardingService：首次引导（欢迎页）的状态与落库。

spec: docs/superpowers/specs/2026-08-18-onboarding-design.md
追加规则（2026-09-23）：只要「本版本还没展示过欢迎页」，即便 onboarding 已完成，
也强制展开一次 —— `show_wizard = (not wizard_seen) or (welcome_version != app_version)`。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService
from agent.graph.nodes import NodeService
from agent.knowledge.lifecycle import KnowledgeService
from agent.storage.settings import SettingsStore

DONE_KEY = "onboarding.done"
SEEN_KEY = "onboarding.wizard_seen"
WELCOME_VERSION_KEY = "onboarding.welcome_version"
HINT_DISMISSED_KEY = "onboarding.hint_dismissed"
DONE_AT_KEY = "onboarding.done_at"
NAME_KEY = "onboarding.name"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OnboardingStatus:
    done: bool
    has_credential: bool
    has_name: bool
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
            "wizard_seen": self.wizard_seen,
            "welcome_version": self.welcome_version,
            "app_version": self.app_version,
            "show_wizard": self.show_wizard,
            "hint_dismissed": self.hint_dismissed,
        }


class OnboardingService:
    def __init__(self, conn: sqlite3.Connection, app_version: str) -> None:
        self.conn = conn
        self.app_version = app_version
        self.settings = SettingsStore(conn)

    # -- 状态 -------------------------------------------------------------

    def status(self) -> OnboardingStatus:
        credential_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM credentials"
        ).fetchone()["n"]
        wizard_seen = self.settings.get_bool(SEEN_KEY, False)
        welcome_version = self.settings.get(WELCOME_VERSION_KEY) or ""
        return OnboardingStatus(
            done=self.settings.get_bool(DONE_KEY, False),
            has_credential=credential_count > 0,
            has_name=bool((self.settings.get(NAME_KEY) or "").strip()),
            wizard_seen=wizard_seen,
            welcome_version=welcome_version,
            app_version=self.app_version,
            show_wizard=(not wizard_seen) or (welcome_version != self.app_version),
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
