"""Planet 浏览数据层：轻量话题概览 + 确定性的「浏览序列 / 游标」。

第二阶段的产品定义（见 docs/architecture.md 的 Planet 一节）：

* 星球是**长期话题的空间化浏览景观**，不是认知地图、不是全量可视化；
* **总话题数**（数据层，无上限）与**当前可见话题数**（视觉层，固定容量）是两件事；
* 旋转不等于真实球体地理旋转 —— 旋转同时推动话题流，任何话题都可能被展示；
* 顺序不需要用户理解，也不在 UI 里暴露任何分数。

本模块只回答两个问题：

1. 有哪些话题可以展示（`overview()`，不读 Message 原文）；
2. 接下来该展示哪一批（`browse()`，同一 seed 结果确定，可前进可后退）。

排序策略保持「轻量」：稳定哈希决定底色，近期活跃给一点点加权，
当前话题在第一圈获得展示优先，最近展示过的话题在新一圈被排到后面
（新鲜度 / 曝光抑制）。不做 embedding、不做降维、不做语义坐标。
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from dataclasses import asdict, dataclass

# 视觉层容量：星球表面同时承载多少个话题点。
# 上半球只看得见一半，所以「正面可见」大约是它的一半（8 个左右），
# 落在 spec 要求的 8~16 个区间里。上限受融合环 shader 的 uniform 数组约束
# （frontend planetShader.MAX_TOPICS = 16），所以这里必须 ≤ 16。
VISIBLE_CAPACITY = 16

# 单次请求最多返回多少个话题（防止前端一次把全部话题拉走再自己洗牌）。
MAX_BATCH = 32

# 近期活跃最多能把顺序提前多少（0.15 相当于约 15% 的序列跨度）。
_RECENCY_BONUS = 0.15

# 第一圈里，当前所在话题额外提前的量：保证它一定落在第一批。
_CURRENT_TOPIC_BONUS = 0.5


@dataclass(frozen=True)
class PlanetTopic:
    """星球浏览需要的最小话题信息（不含 Message 原文）。"""

    topic_id: str
    title: str
    fragment_count: int
    last_activity: str | None
    summary_preview: str | None
    visual_seed: int


def _unit_hash(*parts: object) -> float:
    """把任意键映射到 [0, 1)：同一个 (seed, topic_id) 永远得到同一个值。"""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def visual_seed_of(topic_id: str) -> int:
    """话题的稳定视觉身份种子。

    本阶段**只提供、不消费**（未来程序化构筑物阶段可以用它给同一个话题
    生成稳定视觉身份）。它由 topic_id 推导，因此不需要任何数据库字段。
    """
    digest = hashlib.blake2b(topic_id.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big")


def encode_cursor(seed: int, pass_index: int, index: int) -> str:
    """浏览游标：`{seed}.{pass_index}.{index}`。

    seed 是这一轮浏览会话的底色（同 seed + 同话题集合 = 同顺序）；
    pass_index 是第几圈（走完一圈后 +1，顺序会重新洗一次，避免机械循环）；
    index 是这一圈里的窗口起点。
    """
    return f"{int(seed)}.{int(pass_index)}.{int(index)}"


def parse_cursor(cursor: str) -> tuple[int, int, int] | None:
    """解析游标；格式不对返回 None（调用方按「重新开始」处理，不抛给用户）。"""
    try:
        seed_s, pass_s, index_s = cursor.split(".")
        return int(seed_s), max(0, int(pass_s)), max(0, int(index_s))
    except (AttributeError, ValueError):
        return None


def _topic_ended_meta(raw_meta: str | None) -> bool:
    """话题是否已结束（标记存在 nodes.meta.ended_at）。"""
    import json

    try:
        meta = json.loads(raw_meta or "{}")
    except (TypeError, ValueError):
        return False
    return bool(isinstance(meta, dict) and meta.get("ended_at"))


class PlanetBrowseService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- 第一层：概览 ---------------------------------------------------

    def overview(self) -> list[PlanetTopic]:
        """主视图：一次聚合查询拿到全部**未结束**话题的轻量信息，绝不读 Message 原文。"""
        return self._overview(ended=False)

    def ended_overview(self) -> list[PlanetTopic]:
        """已结束分组：话题结束不等于删掉，它只是离开主视图。"""
        return self._overview(ended=True)

    def _overview(self, *, ended: bool) -> list[PlanetTopic]:
        rows = self.conn.execute(
            """
            SELECT n.id AS topic_id,
                   n.name AS title,
                   n.meta AS meta,
                   COUNT(f.id) AS fragment_count,
                   MAX(f.created_at) AS last_activity
            FROM nodes n
            LEFT JOIN fragments f ON f.topic_id = n.id
            WHERE n.type = 'topic'
            GROUP BY n.id, n.name, n.meta
            ORDER BY n.created_at
            """
        ).fetchall()
        previews = self._latest_summaries()
        return [
            PlanetTopic(
                topic_id=row["topic_id"],
                title=row["title"] or row["topic_id"],
                fragment_count=int(row["fragment_count"] or 0),
                last_activity=row["last_activity"],
                summary_preview=previews.get(row["topic_id"]),
                visual_seed=visual_seed_of(row["topic_id"]),
            )
            for row in rows
            if _topic_ended_meta(row["meta"]) is ended
        ]

    def _latest_summaries(self) -> dict[str, str]:
        rows = self.conn.execute(
            """
            SELECT f.topic_id AS topic_id, f.summary AS summary
            FROM fragments f
            JOIN (
                SELECT topic_id, MAX(created_at) AS m
                FROM fragments
                WHERE summary IS NOT NULL AND summary <> ''
                GROUP BY topic_id
            ) latest
              ON latest.topic_id = f.topic_id AND latest.m = f.created_at
            WHERE f.summary IS NOT NULL AND f.summary <> ''
            """
        ).fetchall()
        out: dict[str, str] = {}
        for row in rows:
            out.setdefault(row["topic_id"], row["summary"])
        return out

    # -- 第二层：浏览序列与游标 -----------------------------------------

    def browse(
        self,
        *,
        cursor: str | None = None,
        direction: str = "forward",
        count: int = VISIBLE_CAPACITY,
        exclude: list[str] | tuple[str, ...] = (),
        current_topic_id: str | None = None,
        seed: int | None = None,
    ) -> dict:
        """返回下一批适合展示的话题。

        `direction="forward"` 取当前游标之后的一批；`"backward"` 取之前的一批
        （所以短距离反向浏览能拿回刚刚离开的内容，而不是重新随机一批）。
        """
        if direction not in ("forward", "backward"):
            raise ValueError(f"unsupported direction: {direction}")
        count = max(1, min(int(count), MAX_BATCH))

        topics = self.overview()
        total = len(topics)
        if total == 0:
            base_seed = int(seed) if seed is not None else random.randrange(1, 2**31)
            return {
                "seed": base_seed,
                "pass_index": 0,
                "cursor": encode_cursor(base_seed, 0, 0),
                "prev_cursor": encode_cursor(base_seed, 0, 0),
                "next_cursor": encode_cursor(base_seed, 0, 0),
                "has_more": False,
                "pass_changed": False,
                "total": 0,
                "visible_capacity": VISIBLE_CAPACITY,
                "items": [],
            }

        parsed = parse_cursor(cursor) if cursor else None
        if parsed is not None:
            base_seed, pass_index, index = parsed
            if seed is not None:
                base_seed = int(seed)
        else:
            base_seed = int(seed) if seed is not None else random.randrange(1, 2**31)
            pass_index = 0
            index = 0
        index = max(0, min(index, total))

        sequence = self._sequence(
            base_seed,
            pass_index,
            topics,
            current_topic_id if pass_index == 0 else None,
            list(exclude) if pass_index > 0 else [],
        )

        if direction == "backward":
            start = max(0, index - count)
            end = index
        else:
            start = index
            end = min(total, start + count)

        items = [asdict(t) for t in sequence[start:end]]

        pass_changed = direction == "forward" and end >= total and end > start
        if direction == "backward":
            has_more = start > 0
        else:
            has_more = end < total

        if pass_changed:
            next_cursor = encode_cursor(base_seed, pass_index + 1, 0)
        else:
            next_cursor = encode_cursor(base_seed, pass_index, end)

        return {
            "seed": base_seed,
            "pass_index": pass_index,
            "cursor": encode_cursor(base_seed, pass_index, start),
            "prev_cursor": encode_cursor(base_seed, pass_index, start),
            "next_cursor": next_cursor,
            "has_more": has_more,
            "pass_changed": pass_changed,
            "total": total,
            "visible_capacity": VISIBLE_CAPACITY,
            "items": items,
        }

    def _sequence(
        self,
        base_seed: int,
        pass_index: int,
        topics: list[PlanetTopic],
        current_topic_id: str | None,
        exclude: list[str],
    ) -> list[PlanetTopic]:
        """确定性顺序：稳定哈希打底 + 少量近期活跃加权 + 曝光抑制。"""
        seed = base_seed + pass_index  # 每走完一圈换一个底色，避免机械循环
        ranks = self._recency_ranks(topics)
        ordered = sorted(
            topics,
            key=lambda t: (
                _unit_hash(seed, t.topic_id) - _RECENCY_BONUS * ranks[t.topic_id],
                t.topic_id,
            ),
        )
        if current_topic_id is not None:
            head = [t for t in ordered if t.topic_id == current_topic_id]
            if head:
                ordered = head + [t for t in ordered if t.topic_id != current_topic_id]
        if exclude:
            excluded = set(exclude)
            # 新一圈里，最近展示过的话题排到后面（新鲜度抑制），但并不永久去重
            ordered = [t for t in ordered if t.topic_id not in excluded] + [
                t for t in ordered if t.topic_id in excluded
            ]
        return ordered

    @staticmethod
    def _recency_ranks(topics: list[PlanetTopic]) -> dict[str, float]:
        """近期活跃 → 接近 0；从未活动 → 1（作为哈希打底之上的轻微偏置）。"""
        by_activity = sorted(
            topics,
            key=lambda t: (t.last_activity or "", t.topic_id),
            reverse=True,
        )
        span = max(1, len(by_activity) - 1)
        return {t.topic_id: idx / span for idx, t in enumerate(by_activity)}
