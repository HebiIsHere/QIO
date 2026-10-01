# -*- coding: utf-8 -*-
"""检索排序评测语料：确定性生成、可复现、不含任何个人数据。

为什么不用那 8 条老用例：它们只够冒烟，不足以决定生产权重。
这份语料按「事件事实 + 各话题噪声」组装，覆盖老用例里出现过的所有失败模式，
并放大到能分辨排序结构的规模：

  fact_update     事实更新（旧值 vs 最终值）→ 时效与相关性的冲突
  decision        很久以前但依然有效的决定 → 时效项不该压过相关性
  preference      偏好类（陈旧但有效）
  person_entity   人物/实体类
  cross_topic     跨话题召回（查询落在另一个话题的锚点上）
  switch          话题切换后的检索
  paraphrase      换词提问（纯词面 baseline 必然吃亏）
  noise           大量「最近但无关」的记忆不得压过相关旧记忆

生成规则确定（无随机数），corpus.json 已入库；本脚本用来重建/审阅它。
用法（backend 目录下）：uv run --frozen python evals/retrieval_ranking/build_corpus.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "corpus.json"

# 每个话题：(标题, 关键词, 事件事实列表)
# 事实 = (记忆正文, 换词后的提问, 关键词, 类型, 几天前, 是否被后续事实取代)
TOPICS = {
    "t_db": (
        "SQLite 迁移",
        ["sqlite", "迁移", "数据库", "索引", "wal"],
        [
            ("项目准备使用 PostgreSQL 作为主数据库，连接池按 20 配置", "一开始数据库选的什么", ["postgresql", "数据库", "连接池"], "decision", 220, True),
            ("最终决定数据库改用 SQLite，理由是单机部署维护成本低", "最后数据库定的是哪个", ["sqlite", "数据库", "部署"], "decision", 25, False),
            ("迁移脚本用 Python 写，按用户表、订单表、日志表分三批执行", "迁移脚本怎么分批跑的", ["迁移", "脚本", "分批"], "decision", 20, False),
            ("SQLite 打开了 WAL 模式，并发读写不再互相阻塞", "并发读写怎么解决的", ["sqlite", "wal", "并发"], "decision", 18, False),
            ("订单表给 user_id 和 created_at 加了联合索引，查询从 800ms 降到 40ms", "订单查询怎么优化的", ["索引", "订单", "查询"], "decision", 12, False),
            ("迁移过程中发现自增主键要改成 INTEGER PRIMARY KEY 才能复用 rowid", "自增主键踩了什么坑", ["主键", "rowid", "迁移"], "decision", 10, False),
        ],
    ),
    "t_ui": (
        "界面配色",
        ["界面", "配色", "布局", "主题", "暗色"],
        [
            ("最初界面用的是亮色主题，白色背景", "界面最开始什么风格", ["亮色", "主题", "背景"], "decision", 200, True),
            ("最终确定用暗色主题，背景取 #101216，正文文字用低饱和的浅灰", "现在界面走什么风格", ["暗色", "主题", "背景"], "decision", 30, False),
            ("用户偏好低饱和的深灰背景，说太亮的蓝色看久了眼睛累", "用户喜欢什么样的底色", ["偏好", "深灰", "底色"], "preference", 28, False),
            ("卡片阴影从 8px 改成 2px，层次靠边框而不是投影", "卡片层次怎么做的", ["卡片", "阴影", "边框"], "decision", 15, False),
            ("图表配色只保留三种强调色：青、橙、紫", "图表用了哪些强调色", ["图表", "强调色", "配色"], "decision", 9, False),
        ],
    ),
    "t_deploy": (
        "部署环境",
        ["部署", "上线", "云服务器", "扩容", "回滚"],
        [
            ("早期部署在自建机房的物理机上，运维要自己插网线", "以前部署在哪", ["机房", "物理机", "运维"], "decision", 400, True),
            ("现在部署环境已经迁到云上，用一台 4 核 8G 的云服务器", "现在跑在哪台机器上", ["云服务器", "部署", "配置"], "decision", 40, False),
            ("上线流程是：先灰度 10% 流量，观察半小时再全量", "上线怎么放量", ["灰度", "上线", "流量"], "decision", 35, False),
            ("回滚剧本里写死了三条命令：停服、切镜像、清缓存", "回滚怎么操作", ["回滚", "镜像", "缓存"], "decision", 33, False),
            ("扩容之后必须重启 worker，否则新进程读不到新的连接串", "扩容后要注意什么", ["扩容", "重启", "worker"], "decision", 22, False),
            ("云服务器带宽从 5M 升到 20M，静态资源改走对象存储", "带宽做过什么调整", ["带宽", "带宽", "对象存储"], "decision", 16, False),
        ],
    ),
    "t_travel": (
        "云南旅行",
        ["旅行", "云南", "滇西北", "机票", "行程"],
        [
            ("原来打算去海南，后来改成了云南", "旅行最初打算去哪", ["海南", "旅行", "计划"], "decision", 90, True),
            ("最终确认十月去云南走滇西北线路，昆明进、丽江出", "最后定的路线是什么", ["云南", "滇西北", "线路"], "decision", 45, False),
            ("机票已经出票，10 月 2 日早班机，回程 10 月 9 日", "机票定了哪天的", ["机票", "出票", "回程"], "decision", 44, False),
            ("行程安排是大理两天、丽江三天、香格里拉两天", "每个地方待几天", ["大理", "丽江", "香格里拉"], "decision", 42, False),
            ("住宿选的是古城里的客栈，预算每晚 300 以内", "住宿怎么定的", ["住宿", "客栈", "预算"], "decision", 41, False),
            ("租车走环湖路线，需要提前确认驾照能不能异地取车", "环湖怎么走", ["租车", "环湖", "驾照"], "decision", 38, False),
        ],
    ),
    "t_perf": (
        "性能优化",
        ["性能", "优化", "延迟", "压测", "接口"],
        [
            ("接口延迟的 P95 一开始是 1.2 秒，用户抱怨很卡", "接口一开始多慢", ["延迟", "p95", "接口"], "decision", 60, True),
            ("优化目标定为 P95 降到 200ms 以内", "性能目标是多少", ["目标", "p95", "200ms"], "decision", 55, False),
            ("压测脚本用 locust 写，单机并发 200", "压测怎么做的", ["压测", "locust", "并发"], "decision", 30, False),
            ("慢查询主要来自没加索引的模糊匹配，改成长列表预筛后降到 300ms", "慢查询的原因是什么", ["慢查询", "索引", "模糊匹配"], "decision", 28, False),
            ("缓存命中率只有 35%，加了一层本地缓存后到 78%", "缓存命中率怎么提上来的", ["缓存", "命中率", "本地缓存"], "decision", 20, False),
        ],
    ),
    "t_work": (
        "接口联调",
        ["接口", "联调", "排期", "鉴权", "字段"],
        [
            ("本周要完成与前端的接口联调，排期到周五", "联调排到什么时候", ["联调", "排期", "周五"], "decision", 12, False),
            ("联调卡在鉴权参数的字段命名上，前端要 token，后端返回 access_token", "联调卡在哪里", ["鉴权", "字段", "token"], "decision", 11, False),
            ("最终约定字段统一用 snake_case，前端做一次映射", "字段命名最后怎么统一的", ["字段", "snake_case", "映射"], "decision", 9, False),
            ("错误码从 500 改成业务码 + HTTP 200，前端按 code 分支", "错误码怎么处理的", ["错误码", "业务码", "分支"], "decision", 8, False),
        ],
    ),
    "t_people": (
        "团队成员",
        ["团队", "工程师", "负责人", "前端", "后端"],
        [
            ("小林是团队里的后端工程师，负责服务端和数据库", "小林负责什么", ["小林", "后端", "工程师"], "person", 60, False),
            ("老王是前端负责人，同时管发布流程", "老王是做什么的", ["老王", "前端", "发布"], "person", 58, False),
            ("阿 May 是测试，联调阶段由她来跑回归", "谁负责跑回归", ["阿may", "测试", "回归"], "person", 30, False),
            ("产品那边由小周对接，需求变更都要经过她", "需求变更找谁", ["小周", "产品", "需求"], "person", 26, False),
        ],
    ),
    "t_food": (
        "饮食偏好",
        ["饮食", "口味", "偏好", "忌口", "咖啡"],
        [
            ("用户偏好清淡饮食，不吃辣", "用户口味怎么样", ["清淡", "不吃辣", "口味"], "preference", 120, False),
            ("早餐通常是一杯美式加一个鸡蛋", "早餐一般吃什么", ["早餐", "美式", "鸡蛋"], "preference", 100, False),
            ("对香菜过敏，点餐时要去掉", "有什么忌口", ["香菜", "过敏", "忌口"], "preference", 95, False),
            ("晚上喝咖啡会失眠，所以下午三点以后不喝", "咖啡什么时候不能喝", ["咖啡", "失眠", "下午"], "preference", 80, False),
        ],
    ),
    "t_arch": (
        "缓存方案",
        ["缓存", "内存", "落盘", "架构", "一致性"],
        [
            ("架构决策：缓存采用内存加定期落盘，理由是延迟低", "缓存方案当时为什么这么选", ["缓存", "内存", "落盘", "延迟"], "decision", 200, False),
            ("缓存淘汰策略是 LRU，容量上限 2000 条", "缓存怎么淘汰", ["lru", "淘汰", "容量"], "decision", 150, False),
            ("落盘间隔设成 5 分钟，进程退出时再强制刷一次", "缓存多久落一次盘", ["落盘", "间隔", "退出"], "decision", 140, False),
            ("一致性上只保证最终一致，读路径允许短暂旧值", "缓存一致性怎么保证", ["一致性", "最终一致", "旧值"], "decision", 130, False),
        ],
    ),
    "t_study": (
        "学习方法",
        ["学习", "复习", "笔记", "考试", "计划"],
        [
            ("复习计划是每天两小时，先过一遍错题再看新内容", "复习怎么安排的", ["复习", "错题", "计划"], "decision", 50, False),
            ("笔记用康奈尔格式，左边写关键词右边写细节", "笔记怎么记的", ["笔记", "康奈尔", "关键词"], "decision", 48, False),
            ("线性代数打算先刷完历年题再回头看课本", "线代怎么复习", ["线代", "历年题", "课本"], "decision", 45, False),
        ],
    ),
}

# 通用噪声：最近发生但与任何话题都无关（用来检验「不能只看时效」）
NOISE = [
    "今天楼下在装修，吵得没法开会",
    "刚泡了杯咖啡，顺手把杯子洗了",
    "手机没电了，先充一会儿",
    "窗外下雨了，出门记得带伞",
    "中午吃了拉面，味道一般",
    "快递到楼下了，下去拿一下",
    "新买的键盘到了，手感不错",
    "同事推荐了一家面包店",
    "打印机又卡纸了",
    "周末想去电影院看新片",
    "空调温度调低了两度",
    "椅子有点晃，找时间修一下",
    "充电线接触不良，换了一根",
    "会议室的投影仪坏了",
    "顺手把桌面文件整理了一下",
    "刚把垃圾拿下楼",
    "天气转凉，把外套找出来了",
    "给花浇了水",
    "地铁上遇到老同学",
    "晚饭想简单煮个面",
]

# 查询模板：事实 -> (类别, 提问)。换词提问刻意避开记忆里的关键词。
PARAPHRASE_SUFFIX = "（换个说法问）"


CATEGORY_OF_KIND = {
    "decision": "decision",
    "preference": "preference",
    "person": "person_entity",
}


def build() -> dict:
    docs: list[dict] = []
    queries: list[dict] = []
    for tid, (title, keywords, facts) in TOPICS.items():
        superseded_ids = []
        for idx, (text, ask, kw, kind, days, superseded) in enumerate(facts):
            doc_id = f"{tid}_f{idx}"
            if superseded:
                superseded_ids.append(doc_id)
            docs.append(
                {
                    "id": doc_id,
                    "text": text,
                    "topic": tid,
                    "title": title,
                    "keywords": kw,
                    "kind": kind,
                    "created_days_ago": days,
                }
            )
        # 只为「当前仍然成立」的事实生成查询；被取代的旧值进 stale 名单，
        # 用来检查「旧值排在当前值之前」这种有害注入。
        for idx, (text, ask, kw, kind, days, superseded) in enumerate(facts):
            if superseded:
                continue
            category = "fact_update" if (superseded_ids and idx == len(facts) - 1 and any(
                f[5] for f in facts
            )) else CATEGORY_OF_KIND[kind]
            queries.append(
                {
                    "id": f"{tid}_q{idx}",
                    "query": ask,
                    "topic": tid,
                    "expected": [f"{tid}_f{idx}"],
                    "stale": list(superseded_ids),
                    "category": category,
                }
            )
        # 跨话题 / 切换查询问的都是「现在是什么结论」——期望值必须取**当前仍然成立**
        # 的那条事实（取被取代的旧值会让 ground truth 自相矛盾，测出来的 0 分不是排序问题）。
        current = [(idx, fact) for idx, fact in enumerate(facts) if not fact[5]]
        newest_idx, newest = current[-1]
        other = next(t for t in TOPICS if t != tid)
        queries.append(
            {
                "id": f"{tid}_cross",
                "query": f"之前聊过的{title}那件事，结论是什么",
                "topic": other,
                "expected": [f"{tid}_f{newest_idx}"],
                "stale": list(superseded_ids),
                "category": "cross_topic",
            }
        )
        # 换词查询：用第二条「当前成立」的事实的提问换个说法
        if len(current) > 1:
            para_idx, para_fact = current[1]
            queries.append(
                {
                    "id": f"{tid}_para",
                    "query": para_fact[1] + PARAPHRASE_SUFFIX,
                    "topic": tid,
                    "expected": [f"{tid}_f{para_idx}"],
                    "stale": list(superseded_ids),
                    "category": "paraphrase",
                }
            )
        # 话题切换：从别的锚点问过来，问的同样是当前结论
        queries.append(
            {
                "id": f"{tid}_switch",
                "query": f"换个话题，{newest[1]}",
                "topic": other,
                "expected": [f"{tid}_f{newest_idx}"],
                "stale": list(superseded_ids),
                "category": "switch",
            }
        )

    for i, text in enumerate(NOISE):
        docs.append(
            {
                "id": f"noise_{i}",
                "text": text,
                "topic": None,
                "title": "",
                "keywords": [],
                "kind": "ephemeral",
                "created_days_ago": i % 3,
            }
        )

    return {
        "name": "retrieval-ranking-deterministic",
        "provenance": "确定性生成（build_corpus.py），不含个人数据；用于排序结构与权重的对比实验",
        "docs": docs,
        "queries": queries,
    }


def main() -> int:
    payload = build()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    cats: dict = {}
    for q in payload["queries"]:
        cats[q["category"]] = cats.get(q["category"], 0) + 1
    print(f"docs={len(payload['docs'])} queries={len(payload['queries'])} -> {OUT}")
    print("categories:", json.dumps(cats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
