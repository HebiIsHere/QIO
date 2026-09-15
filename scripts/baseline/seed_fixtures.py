"""基线测试数据：往隔离数据目录写入确定性的 UI 场景。
用途：前端体验验收需要「短/长消息、表格、宽代码块、多个话题、长标题、
有/无片段的话题、有/无知识、有/无实体」这些真实形状的数据，而真实模型调用
既不稳定也需要密钥。因此这里直接写隔离数据目录（默认 %TEMP%\\qio-e2e），
不使用任何真实密钥，也不碰用户的正式数据目录。
用法：
    backend\\.venv\\Scripts\\python.exe scripts\\baseline\\seed_fixtures.py --reset
"""
from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("QIO_DATA_DIR") or (Path(os.environ.get("TEMP", ".")) / "qio-e2e"))
PREFIX = "基线-"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def now(offset_min: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_min)).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


LONG_ANSWER = """## 体验改进的四个抓手
这一轮要解决的是「操作有没有被接住、内容会不会被弄丢、动画会不会拖住手」，不是换一套视觉风格。
**第一，操作要立刻有反馈。** 点击发送之后，界面上必须马上出现「这条已经交出去了」的证据；
如果请求失败，也要让用户看到失败，而不是悄悄把输入清空。
**第二，当前状态要一直可辨。** 等的时候要知道还在等，取消要知道取消了，审批没成功就不能显示成成功。
**第三，页面变化尽量少。** 已经读到的内容不要因为切页、开弹窗、保存设置而重新排布；
正在读的位置、正在写的草稿、正在比较的两个片段，都应当保留下来。
**第四，动画要短而平稳。** 不是把动画做慢一点显得优雅，而是让速度变化自然：
起步不要突兀、停止不要急刹、不要回弹，也不要让动画排队挡住后续操作。
再补充一点关于「不要为了效果推迟内容」的说法：内容先出现，动画只是它的入场方式。
如果一段文字已经显示了，就不要让它再播一次；如果一次变化不影响理解，就不要加持续运动。
这一段故意写得比较长，用来检验长回复在虚拟滚动里的高度测量是否稳定。
"""

TABLE_AND_CODE = """对比数据如下，表格在窄窗口下可以横向滚动，不会把气泡撑破。
| 场景 | 现状 | 目标 | 触发条件 | 观察方式 | 所属阶段 |
| --- | --- | --- | --- | --- | --- |
| 打开设置 | 直接切换，无过渡 | 短交叉淡入，方向与返回一致 | 点击右上角齿轮 | 录屏逐帧看首末两帧 | 第二阶段 |
| 关闭星球 | 有淡出，约 500ms | 保留淡出但缩短到 220ms 以内 | 点收起星球 | 录屏对比总时长 | 第三阶段 |
| 侧栏开合 | 380ms 后再补一次居中 | 开合过程中连续跟随，不二次跳 | 点击展开按钮 | 连续点击观察是否抖动 | 第三阶段 |
下面是一段故意很宽的代码，用来检验代码块会不会溢出气泡：
```python
def assemble_context(topic_id: str, anchor_fragment_id: str | None, *, max_tokens: int = 4096, reserve: int = 1024) -> str:
    focus = load_focus_block(topic_id, anchor_fragment_id) if anchor_fragment_id else None
    short_term = collect_short_term(topic_id, limit=max_tokens - reserve)
    return render(focus=focus, short_term=short_term, budget=max_tokens, reserve=reserve, strict=True)
```
以及一段普通宽度的代码，用来对比宽度处理是否一致：
```ts
const follow = isNearBottom(el) && !userScrolledUp;
```
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "src"))
    from agent.storage.migrate import apply_migrations

    conn = sqlite3.connect(DATA_DIR / "app.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    apply_migrations(conn)
    return conn


def reset(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id FROM nodes WHERE type='topic' AND name LIKE ?", (PREFIX + "%",)).fetchall()
    for row in rows:
        tid = row["id"]
        for frag in conn.execute("SELECT id FROM fragments WHERE topic_id=?", (tid,)).fetchall():
            conn.execute("DELETE FROM messages WHERE fragment_id=?", (frag["id"],))
            conn.execute("DELETE FROM memory_index WHERE fragment_id=?", (frag["id"],))
        conn.execute("DELETE FROM cursor WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM fragments WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM knowledge WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM entity_mentions WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (tid, tid))
        conn.execute("DELETE FROM nodes WHERE id=?", (tid,))
    conn.commit()
    print(f"reset: 清除 {len(rows)} 个「基线-」话题")


def add_topic(conn: sqlite3.Connection, name: str) -> str:
    tid = new_id("topic")
    ts = now()
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (tid, "topic", name, "{}", ts, ts),
    )
    return tid


def add_fragment(conn: sqlite3.Connection, topic_id: str, summary: str | None, closed: bool, offset: int) -> str:
    fid = new_id("frag")
    created = now(offset)
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_model, summary_version, created_at, closed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (fid, topic_id, summary, "baseline-seed" if summary else None, 1 if summary else 0, created, created if closed else None),
    )
    return fid


def add_message(conn: sqlite3.Connection, fragment_id: str, role: str, content: str, offset: int) -> str:
    mid = new_id("msg")
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, model, raw, created_at, storage_tier) "
        "VALUES (?,?,?,?,?,?,?,?,'hot')",
        (mid, fragment_id, role, content, "text", "baseline-seed", "{}", now(offset)),
    )
    return mid


def add_index(conn: sqlite3.Connection, fragment_id: str, topic_id: str, summary: str, keywords: str) -> None:
    conn.execute(
        "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, title, token_estimate, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (new_id("idx"), fragment_id, topic_id, "[]", keywords, summary[:60], 120, now()),
    )


def add_knowledge(conn: sqlite3.Connection, topic_id: str, content: str, state: str, category: str = "general_fact") -> str:
    kid = new_id("know")
    ts = now()
    conn.execute(
        "INSERT INTO knowledge (id, category, state, content, topic_id, entity_ids, provenance, confidence, "
        "created_at, updated_at, activated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (kid, category, state, content, topic_id, "[]", "{}", 0.8, ts, ts, ts if state == "active" else None),
    )
    return kid


def add_entity(conn: sqlite3.Connection, topic_id: str, name: str, kind: str, summary: str) -> str:
    node_id = new_id("entity")
    ts = now()
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (node_id, "entity", name, "{}", ts, ts),
    )
    conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, attributes, state, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (new_id("card"), node_id, name, "[]", kind, summary, "[]", "active", ts, ts),
    )
    conn.execute(
        "INSERT OR IGNORE INTO entity_mentions (id, entity_name, topic_id, mention_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (new_id("mention"), name, topic_id, 1, ts, ts),
    )
    conn.execute(
        "INSERT OR IGNORE INTO edges (id, src, dst, type, weight, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (new_id("edge"), topic_id, node_id, "mention", 1.0, ts, ts),
    )
    return node_id


def main() -> None:
    conn = connect()
    print(f"data_dir={DATA_DIR}")
    if "--reset" in sys.argv:
        reset(conn)
    main_topic = add_topic(conn, PREFIX + "前端稳定化长对话与阅读连续性")
    old_frag = add_fragment(conn, main_topic, "早前讨论：虚拟滚动与跟随底部的判定口径", True, -240)
    add_message(conn, old_frag, "user", "滚动到底的判定是怎么写的？", -240)
    add_message(conn, old_frag, "assistant", "按「距底部 120px 内算跟随」，用户上翻就立刻停止跟随。", -239)
    add_index(conn, old_frag, main_topic, "虚拟滚动与跟随底部判定", '["滚动","跟随","虚拟列表"]')
    open_frag = add_fragment(conn, main_topic, None, False, -20)
    add_message(conn, open_frag, "user", "帮我梳理一下这一轮前端稳定化要做的事。", -20)
    add_message(conn, open_frag, "assistant", LONG_ANSWER, -19)
    add_message(conn, open_frag, "user", "把对比数据用表格给我，再给一段很宽的代码示例。", -12)
    add_message(conn, open_frag, "assistant", TABLE_AND_CODE, -11)
    add_message(conn, open_frag, "user", "如果我在设置页来回切换，草稿会丢吗？", -3)
    add_message(conn, open_frag, "assistant", "这是我要在基线里实测的一项，先不下结论。", -2)
    rich_topic = add_topic(conn, PREFIX + "这是一个用于检验超长标题在侧栏与星球详情里是否会溢出或截断的话题名称")
    rich_frag = add_fragment(conn, rich_topic, "知识页与实体页的可见性讨论", True, -90)
    add_message(conn, rich_frag, "user", "知识页默认筛选应该是什么？", -90)
    add_message(conn, rich_frag, "assistant", "默认筛选应当与「有内容可看」一致，而不是筛出空列表。", -89)
    add_index(conn, rich_frag, rich_topic, "知识页与实体页可见性", '["知识","实体","筛选"]')
    add_knowledge(conn, rich_topic, "知识页默认筛选为已启用，但状态为 active 的条目在部分数据下会被筛掉。", "active")
    add_knowledge(conn, rich_topic, "用户偏好：复制失败时必须显式提示，不允许显示成功。", "pending_review", "user_profile")
    add_entity(conn, rich_topic, "QIO 前端", "项目", "QIO 的 Vue3 前端，含对话页、设置页与星球页。")
    plain_topic = add_topic(conn, PREFIX + "只有片段没有知识也没有实体的话题")
    plain_frag = add_fragment(conn, plain_topic, "用于检验空的实体与知识区块", True, -60)
    add_message(conn, plain_frag, "user", "这个话题只用来占位。", -60)
    add_message(conn, plain_frag, "assistant", "好的。", -59)
    add_index(conn, plain_frag, plain_topic, "空的实体与知识区块", '["占位"]')
    empty_topic = add_topic(conn, PREFIX + "空白话题无片段无知识无实体")
    fillers = [
        "记忆检索的排序是否合理",
        "审批失败后的重试路径",
        "设置页分区反馈的语义色",
        "流式回复增量渲染的边界",
        "星球相机聚焦距离与时长",
        "代码块复制在受限 WebView 下的行为",
        "数字输入框的提交语义",
        "任务排队与取消的状态归属",
        "窗口变窄时侧栏的覆盖布局",
    ]
    for i, name in enumerate(fillers):
        tid = add_topic(conn, PREFIX + name)
        fid = add_fragment(conn, tid, f"摘要：{name}", True, -120 + i)
        add_message(conn, fid, "user", f"{name} 记录一条占位消息。", -120 + i)
        add_message(conn, fid, "assistant", "已记录。", -119 + i)
        add_index(conn, fid, tid, f"摘要：{name}", '["占位"]')
    conn.execute("DELETE FROM cursor WHERE anchor_type IN ('active','pending')")
    conn.execute(
        "INSERT INTO cursor (id, topic_id, fragment_id, anchor_type, updated_at) VALUES (?,?,?,?,?)",
        (new_id("cur"), main_topic, open_frag, "active", now()),
    )
    conn.commit()
    topics = conn.execute("SELECT COUNT(*) AS n FROM nodes WHERE type='topic'").fetchone()["n"]
    messages = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    print(f"topics={topics} messages={messages} main_topic={main_topic} rich_topic={rich_topic} empty_topic={empty_topic}")
    conn.close()


if __name__ == "__main__":
    main()
