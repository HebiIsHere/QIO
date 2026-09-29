"""压力语料生成器：按 qio 的任务形状造记忆、话题、实体、工具与查询。

设计要点：

- **确定性**：同一个 seed 必然产出同一份语料（测试守着这一点）。
- **答案由构造决定**：每条查询的正确答案是"它由哪条记忆派生"这件事本身，
  不靠人手写期望。事实更新类同时放入旧值与新值，只有新值算正确。
- **关键词可答性实算**：`keyword_answerable` 由查询与正确记忆的字面重叠算出，
  用来把"模型在关键词答不对的子集上有多少增量"单独统计出来。

内容是按模板和词表拼出来的中文，不是自然语料 —— 这是本实验的已知边界，
结论只能说明"在这套任务形状下模型能不能把活干完"。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from agent.selector.tokenize import tokenize

CATEGORIES = ("fact_update", "same_topic", "entity_ref", "cross_topic", "paraphrase", "recent_noise")

# -- 词表 -----------------------------------------------------------------

DOMAINS = [
    "数据库", "前端界面", "接口设计", "部署运维", "缓存策略", "权限模型", "日志系统",
    "消息队列", "支付流程", "订单系统", "用户画像", "搜索排序", "推荐算法", "数据同步",
    "备份恢复", "监控告警", "灰度发布", "代码审查", "测试策略", "性能优化", "内存管理",
    "并发控制", "文件存储", "图片压缩", "音频处理", "视频转码", "文本分词", "向量检索",
    "模型微调", "提示工程", "账号安全", "密钥管理", "桌面打包", "安装升级", "离线同步",
    "多语言支持", "无障碍访问", "主题配色", "图表渲染", "快捷键",
]

ASPECTS = ["迁移", "索引", "备份", "权限", "缓存", "队列"]

# 查询侧同义词表：与上面的记忆用词**不共享任何 2-gram**。硬类别（跨话题引用、
# 近义表述、无关近因）只用这张表提问，所以关键词路径不可能靠内容词命中 ——
# 这正是「模型的增量价值」要落在的那个子集。单字虚词（的/是/了）不算重叠。
DOMAINS_QUERY = [
    "存储层", "视图层", "通信协议", "上线流程", "加速方案", "授权体系", "记录体系",
    "异步通道", "结算步骤", "交易体系", "人群标签", "结果排名", "偏好预测", "一致性复制",
    "容灾流程", "观测提示", "分批上线", "同行评审", "验收方针", "提速改造", "运存调度",
    "并行协调", "磁盘归档", "画质瘦身", "声波加工", "影像格式替换", "字串切块", "相似度查找",
    "权重校准", "指令设计", "登录防护", "凭据保管", "安装包构建", "版本更新", "断网复制",
    "本地化适配", "读屏可用", "外观颜色", "可视化绘制", "组合手势",
]

ASPECTS_QUERY = ["切换", "目录", "容灾", "准入", "加速", "通道"]

KINDS = ["decision", "knowledge", "preference", "chatter"]

# 同族替换对：(旧值, 新值)。用来构造"事实更新"用例。
VALUE_PAIRS = [
    ("PostgreSQL", "SQLite"), ("REST", "GraphQL"), ("Redis", "本地文件"),
    ("单体部署", "容器部署"), ("定时轮询", "事件推送"), ("固定宽高", "响应式布局"),
    ("全量同步", "增量同步"), ("同步写入", "异步写入"), ("手工发布", "灰度发布"),
    ("单库查询", "读写分离"),
]

REASONS = ["维护成本更低", "性能更稳", "团队更熟悉", "依赖更少", "排查更容易", "扩展更省事"]

ENTITIES_NAME = ["小林", "阿岚", "老周", "小满", "阿良", "小荷", "阿澄", "小舟", "阿禾", "小野"]
ENTITIES_TITLE = ["项目", "服务", "模块", "系统", "平台"]

NOVEL_DOMAINS = ["天文观测", "园艺种植", "烹饪调味", "钓鱼装备", "露营路线", "陶艺拉坯"]

# 记忆侧的"讨论事项"= 基名 × 面名，同一个话题下 50 个组合互不重复，
# 这样每条查询的正确记忆才是唯一的（否则同一话题下会有几十条近乎相同的候选，
# 标注就失去意义）。查询侧另有一套完全不共享 2-gram 的说法。
FACT_BASE = [
    "最终选择", "定稿方案", "上线口径", "验收标准", "备份策略",
    "排期结论", "回滚条件", "容量下限", "故障口径", "交接清单",
]
FACT_FACET = ["方案", "细节", "边界", "顺序", "口径"]
SIDE_BASE = [
    "接入方式", "回滚流程", "监控口径", "命名规范", "灰度范围",
    "交接方式", "校验规则", "归档要求", "告警门槛", "升级节奏",
]
SIDE_FACET = ["草案", "说明", "约定", "记录", "口径"]
QFACT_BASE = [
    "最后结果", "定下来的做法", "发布标准", "通过条件", "兜底办法",
    "时间安排", "可回退要求", "容量底线", "出错口径", "移交目录",
]
QFACT_FACET = ["做法", "要点", "范围", "次序", "说法"]

CHATTER = [
    "顺手整理了一下桌面", "楼下换了家咖啡店", "出门忘了带伞", "把抽屉清了一遍",
    "傍晚去散了会儿步", "给绿植浇了水", "把旧硬盘翻出来看了看", "换了新的鼠标垫",
    "午休睡了半小时", "把窗台擦了一遍",
]


def content_overlap(query: str, text: str) -> bool:
    """查询与文本是否共享**内容**词。

    只看 2-gram 与长度 ≥2 的拉丁词：单字虚词（的、是、了）在语料里几乎无处不在，
    把它们算成「字面可答」会让这个标记失去意义；qio 的 BM25 里这些字也被 IDF 压得很低。
    """

    def content_tokens(s: str) -> set[str]:
        return {t for t in tokenize(s) if len(t) >= 2}

    return bool(content_tokens(query) & content_tokens(text))

TOOLS = [
    ("memory_search", "在历史记忆里检索与当前问题相关的内容"),
    ("knowledge_lookup", "查询知识库条目，用于事实性回答"),
    ("topic_create", "新建一个话题，用于开启新的讨论方向"),
    ("topic_switch", "把当前讨论切换到另一个已有话题"),
    ("fragment_close", "关闭当前片段并生成结论文本"),
    ("web_search", "联网搜索最新的公开信息"),
    ("file_read", "读取工作区里的文件内容"),
    ("file_write", "把内容写入工作区文件"),
    ("code_run", "在受限子进程里执行一段代码"),
    ("python_test", "运行项目测试并返回结果"),
    ("git_status", "查看工作区改动状态"),
    ("shell_exec", "执行一条本地命令行指令"),
    ("image_view", "查看一张本地图片"),
    ("entity_card_get", "读取实体卡的详细信息"),
    ("entity_card_update", "更新实体卡的摘要与属性"),
    ("calendar_lookup", "查询日程与时间安排"),
    ("reminder_set", "设置一个提醒"),
    ("note_save", "把一条想法保存为笔记"),
    ("table_render", "把数据渲染成表格"),
    ("chart_plot", "把序列数据画成图表"),
    ("json_validate", "校验一段 JSON 是否合法"),
    ("regex_test", "测试一条正则表达式的匹配结果"),
    ("text_diff", "比较两段文本的差异"),
    ("translate_text", "把文本翻译成另一种语言"),
    ("summarize_text", "把长文本压缩成摘要"),
    ("token_count", "估算文本的 token 数量"),
    ("password_strength", "评估口令强度"),
    ("url_fetch", "抓取一个网页的正文"),
    ("rss_read", "读取订阅源的最新条目"),
    ("mail_draft", "起草一封邮件"),
    ("doc_export", "把内容导出成文档"),
    ("slide_build", "生成一份演示文稿"),
    ("sheet_edit", "编辑电子表格里的单元格"),
    ("pdf_extract", "从 PDF 中提取文本"),
    ("audio_transcribe", "把音频转写成文字"),
    ("video_trim", "裁剪一段视频"),
    ("qr_generate", "生成一个二维码"),
    ("hash_compute", "计算一段内容的哈希"),
    ("uuid_generate", "生成一个唯一标识"),
    ("time_now", "读取当前时间"),
    ("timezone_convert", "在不同时区之间换算时间"),
    ("unit_convert", "换算长度、重量、温度等单位"),
    ("currency_convert", "按汇率换算金额"),
    ("dns_lookup", "查询域名的解析记录"),
    ("port_scan_local", "检查本机端口占用"),
    ("disk_usage", "查看磁盘占用情况"),
    ("process_list", "列出正在运行的进程"),
    ("env_read", "读取一个环境变量的值"),
    ("keyring_get", "从本机密钥库读取一个凭据"),
    ("log_tail", "查看最近的运行日志"),
    ("trace_open", "打开一次运行的追踪记录"),
    ("benchmark_run", "运行一次性能基准测试"),
    ("schema_migrate", "执行数据库迁移"),
    ("backup_create", "创建一份数据备份"),
    ("restore_verify", "校验一份备份是否可用"),
    ("cache_clear", "清空缓存"),
    ("index_rebuild", "重建检索索引"),
    ("embedding_encode", "把一段文本编码成向量"),
    ("rerank_pairs", "对候选列表重新排序"),
]


# -- 数据结构 -------------------------------------------------------------


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    keywords: tuple[str, ...]
    #: 提问用的同义说法（与 title 不共享 2-gram）；硬类别查询只用它
    query_title: str = ""


@dataclass(frozen=True)
class Memory:
    id: str
    text: str
    topic_id: str
    kind: str
    age_days: float
    entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityCard:
    id: str
    name: str
    aliases: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class ToolSpecLite:
    name: str
    description: str


@dataclass(frozen=True)
class RecallCase:
    id: str
    category: str
    query: str
    expected: tuple[str, ...]
    stale: tuple[str, ...]
    keyword_answerable: bool
    #: 查询与正确记忆共享的内容词占查询的比例（0 = 完全不相交）
    overlap: float = 0.0
    #: 难度分层：literal（近似原文）/ partial（部分重合）/ disjoint（零重合）
    tier: str = "literal"


def _overlap_ratio(query: str, text: str) -> float:
    query_tokens = {t for t in tokenize(query) if len(t) >= 2}
    if not query_tokens:
        return 0.0
    text_tokens = {t for t in tokenize(text) if len(t) >= 2}
    return len(query_tokens & text_tokens) / len(query_tokens)


def _tier_of(overlap: float) -> str:
    if overlap >= 0.4:
        return "literal"
    return "partial" if overlap > 0 else "disjoint"


@dataclass(frozen=True)
class TopicCase:
    id: str
    category: str
    message: str
    current_topic_id: str | None
    expected_topic_id: str | None
    expected_mode: str


@dataclass(frozen=True)
class EntityCase:
    id: str
    query: str
    expected_card_id: str


@dataclass(frozen=True)
class ToolCase:
    id: str
    query: str
    expected_tool: str


@dataclass(frozen=True)
class DedupCase:
    id: str
    candidate_name: str
    expected_duplicate: bool
    duplicate_of: str | None


@dataclass(frozen=True)
class StressCorpus:
    seed: int
    topics: list[Topic] = field(default_factory=list)
    memories: list[Memory] = field(default_factory=list)
    entities: list[EntityCard] = field(default_factory=list)
    tools: list[ToolSpecLite] = field(default_factory=list)
    recall_cases: list[RecallCase] = field(default_factory=list)
    topic_cases: list[TopicCase] = field(default_factory=list)
    entity_cases: list[EntityCase] = field(default_factory=list)
    tool_cases: list[ToolCase] = field(default_factory=list)
    dedup_cases: list[DedupCase] = field(default_factory=list)


# -- 生成 -----------------------------------------------------------------


def _build_topics(n_topics: int) -> list[Topic]:
    topics: list[Topic] = []
    for i in range(n_topics):
        domain = DOMAINS[i % len(DOMAINS)]
        aspect = ASPECTS[(i // len(DOMAINS)) % len(ASPECTS)]
        title = f"{domain}{aspect}"
        keywords = tuple(dict.fromkeys(tokenize(title)))[:5]
        query_title = (
            f"{DOMAINS_QUERY[i % len(DOMAINS)]}"
            f"{ASPECTS_QUERY[(i // len(DOMAINS)) % len(ASPECTS)]}"
        )
        topics.append(
            Topic(id=f"t_{i:04d}", title=title, keywords=keywords, query_title=query_title)
        )
    return topics


def _build_entities(n_entities: int) -> list[EntityCard]:
    cards: list[EntityCard] = []
    for i in range(n_entities):
        name = ENTITIES_NAME[i % len(ENTITIES_NAME)]
        title = ENTITIES_TITLE[(i // len(ENTITIES_NAME)) % len(ENTITIES_TITLE)]
        full = f"{name}{i:03d}"
        cards.append(
            EntityCard(
                id=f"e_{i:04d}",
                name=full,
                aliases=(f"{full[:2]}{title}",),
                summary=f"{full} 负责 {DOMAINS[i % len(DOMAINS)]} 相关工作",
            )
        )
    return cards


@dataclass(frozen=True)
class _Block:
    """一"块"记忆：同一个话题下的一组相关片段 + 它的提问说法。"""

    topic: Topic
    entity: EntityCard
    new_fact: Memory
    old_fact: Memory
    side: Memory
    knowledge: Memory
    entity_mem: Memory
    fact_subject: str
    side_subject: str
    query_subject: str


def _generate_blocks(
    rng: random.Random, topics: list[Topic], entities: list[EntityCard], n_memories: int
) -> tuple[list[Memory], list[_Block]]:
    """按块生成记忆。

    每个话题的第 k 块用 (基名 k%10, 面名 (k//10)%5) 组合出**话题内唯一**的讨论事项，
    保证每条查询的正确记忆只有一条 —— 否则同一话题下几十条近乎相同的片段会让标注失去意义。
    """
    memories: list[Memory] = []
    blocks_out: list[_Block] = []
    per_topic: dict[str, int] = {}
    index = 0
    while len(memories) < n_memories:
        topic = topics[index % len(topics)]
        entity = entities[index % len(entities)]
        k = per_topic.get(topic.id, 0)
        per_topic[topic.id] = k + 1
        old_value, new_value = VALUE_PAIRS[k % len(VALUE_PAIRS)]
        reason = REASONS[k % len(REASONS)]
        fact_subject = _subject(FACT_BASE[k % len(FACT_BASE)], FACT_FACET[(k // len(FACT_BASE)) % len(FACT_FACET)])
        side_subject = _subject(SIDE_BASE[k % len(SIDE_BASE)], SIDE_FACET[(k // len(SIDE_BASE)) % len(SIDE_FACET)])
        query_subject = _subject(QFACT_BASE[k % len(QFACT_BASE)], QFACT_FACET[(k // len(QFACT_BASE)) % len(QFACT_FACET)])
        mi = len(memories)
        new_fact = Memory(
            id=f"m_{mi:06d}",
            text=f"{topic.title}的{fact_subject}是{new_value}，理由是{reason}。",
            topic_id=topic.id,
            kind="decision",
            age_days=round(rng.uniform(2, 20), 1),
        )
        old_fact = Memory(
            id=f"m_{mi + 1:06d}",
            text=f"{topic.title}的{fact_subject}是{old_value}，理由是{reason}。",
            topic_id=topic.id,
            kind="decision",
            age_days=round(rng.uniform(60, 140), 1),
        )
        side = Memory(
            id=f"m_{mi + 2:06d}",
            text=f"{topic.title}的{side_subject}是{new_value}，{reason}。",
            topic_id=topic.id,
            kind="decision",
            age_days=round(rng.uniform(10, 45), 1),
        )
        knowledge = Memory(
            id=f"m_{mi + 3:06d}",
            text=f"{topic.title}相关资料：{fact_subject}上 {new_value} 与 {old_value} 各有取舍。",
            topic_id=topic.id,
            kind="knowledge",
            age_days=round(rng.uniform(30, 200), 1),
        )
        entity_mem = Memory(
            id=f"m_{mi + 4:06d}",
            text=f"{entity.name}负责{topic.title}的{side_subject}，当前状态是{reason}。",
            topic_id=topic.id,
            kind="knowledge",
            age_days=round(rng.uniform(5, 60), 1),
            entity_ids=(entity.id,),
        )
        preference = Memory(
            id=f"m_{mi + 5:06d}",
            text=f"用户偏好{topic.title}的{side_subject}先小范围验证，{reason}。",
            topic_id=topic.id,
            kind="preference",
            age_days=round(rng.uniform(20, 120), 1),
        )
        chatter = Memory(
            id=f"m_{mi + 6:06d}",
            text=f"{CHATTER[k % len(CHATTER)]}，没什么要紧的。",
            topic_id=topic.id,
            kind="chatter",
            age_days=round(rng.uniform(0, 3), 1),
        )
        for memory in (new_fact, old_fact, side, knowledge, entity_mem, preference, chatter):
            if len(memories) < n_memories:
                memories.append(memory)
        blocks_out.append(
            _Block(
                topic=topic,
                entity=entity,
                new_fact=new_fact,
                old_fact=old_fact,
                side=side,
                knowledge=knowledge,
                entity_mem=entity_mem,
                fact_subject=fact_subject,
                side_subject=side_subject,
                query_subject=query_subject,
            )
        )
        index += 1
    return memories, blocks_out


def _subject(base: str, facet: str) -> str:
    return f"{base}{facet}"


def _generate_recall_cases(rng: random.Random, blocks: list[_Block], n: int) -> list[RecallCase]:
    """每条查询由某个块派生：正确答案就是这个块里的那条记忆。"""
    texts = {}
    for block in blocks:
        for memory in (block.new_fact, block.old_fact, block.side, block.knowledge, block.entity_mem):
            texts[memory.id] = memory.text
    cases: list[RecallCase] = []
    for i in range(n):
        block = blocks[i % len(blocks)]
        topic = block.topic
        category = CATEGORIES[i % len(CATEGORIES)]
        stale: tuple[str, ...] = ()
        expected: tuple[str, ...]
        if category == "fact_update":
            # 一半字面问法（关键词答得出），一半同义说法（答不出）
            hard = (i // len(CATEGORIES)) % 2 == 1
            query = (
                f"{topic.query_title}那块的{block.query_subject}定了吗？"
                if hard
                else f"{topic.title}的{block.fact_subject}是什么？"
            )
            expected, stale = (block.new_fact.id,), (block.old_fact.id,)
        elif category == "same_topic":
            query = f"{topic.title}的{block.side_subject}是怎么定的？"
            expected = (block.side.id,)
        elif category == "entity_ref":
            golden = block.entity.aliases[0] if i % 2 else block.entity.name
            query = f"{golden}现在负责哪一块？"
            expected = (block.entity_mem.id,)
        elif category == "cross_topic":
            # 一半保留话题原词（部分重合），一半换成同义说法（零重合）
            name = topic.title if (i // len(CATEGORIES)) % 2 == 0 else topic.query_title
            query = f"之前聊过的{name}后来是怎么定的？"
            expected = (block.new_fact.id,)
        elif category == "paraphrase":
            name = topic.title if (i // len(CATEGORIES)) % 2 == 0 else topic.query_title
            query = f"{name}那块最后选的是哪个？"
            expected = (block.new_fact.id,)
        else:  # recent_noise：正确答案是较早的知识条目，语料里有更新的闲聊干扰
            query = f"{topic.query_title}那两套思路谁更合适？"
            expected = (block.knowledge.id,)
        overlap = max(_overlap_ratio(query, texts[m]) for m in expected)
        cases.append(
            RecallCase(
                id=f"r_{i:05d}",
                category=category,
                query=query,
                expected=expected,
                stale=stale,
                keyword_answerable=overlap > 0,
                overlap=round(overlap, 4),
                tier=_tier_of(overlap),
            )
        )
    return cases


def _generate_other_cases(
    rng: random.Random,
    memories: list[Memory],
    topics: list[Topic],
    entities: list[EntityCard],
    n: int,
) -> tuple[list[TopicCase], list[EntityCase], list[ToolCase], list[DedupCase]]:
    topic_cases: list[TopicCase] = []
    for i in range(n):
        mode = ("in_topic", "switch", "new_topic")[i % 3]
        current = topics[i % len(topics)]
        if mode == "in_topic":
            message = f"继续 {current.title} 这块，接着往下说"
            expected_topic, expected_mode = current.id, "in_topic"
        elif mode == "switch":
            other = topics[(i + 7) % len(topics)]
            message = f"换个话题，{other.title} 现在到哪一步了"
            expected_topic, expected_mode = other.id, "switch"
        else:
            novel = NOVEL_DOMAINS[i % len(NOVEL_DOMAINS)]
            message = f"想聊聊{novel}，这跟前面没关系"
            expected_topic, expected_mode = None, "new_topic"
        topic_cases.append(
            TopicCase(
                id=f"tp_{i:05d}",
                category=mode,
                message=message,
                current_topic_id=current.id,
                expected_topic_id=expected_topic,
                expected_mode=expected_mode,
            )
        )

    entity_cases = [
        EntityCase(
            id=f"en_{i:05d}",
            query=f"{entities[i % len(entities)].name}负责什么？",
            expected_card_id=entities[i % len(entities)].id,
        )
        for i in range(n)
    ]
    tool_cases = [
        ToolCase(
            id=f"tl_{i:05d}",
            query=TOOLS[i % len(TOOLS)][1],
            expected_tool=TOOLS[i % len(TOOLS)][0],
        )
        for i in range(n)
    ]
    dedup_cases: list[DedupCase] = []
    for i in range(n):
        if i % 2 == 0:
            topic = topics[i % len(topics)]
            dedup_cases.append(
                DedupCase(
                    id=f"dd_{i:05d}",
                    candidate_name=topic.title,
                    expected_duplicate=True,
                    duplicate_of=topic.id,
                )
            )
        else:
            novel = NOVEL_DOMAINS[i % len(NOVEL_DOMAINS)]
            dedup_cases.append(
                DedupCase(
                    id=f"dd_{i:05d}",
                    candidate_name=f"{novel}{ASPECTS[i % len(ASPECTS)]}",
                    expected_duplicate=False,
                    duplicate_of=None,
                )
            )
    return topic_cases, entity_cases, tool_cases, dedup_cases


def generate(
    seed: int = 20260923,
    n_memories: int = 10_000,
    n_topics: int = 200,
    n_queries: int = 1_000,
    n_entities: int = 200,
    n_tools: int = 60,
) -> StressCorpus:
    rng = random.Random(seed)
    topics = _build_topics(n_topics)
    entities = _build_entities(n_entities)
    memories, blocks = _generate_blocks(rng, topics, entities, n_memories)
    recall_cases = _generate_recall_cases(rng, blocks, n_queries)
    topic_cases, entity_cases, tool_cases, dedup_cases = _generate_other_cases(
        rng, memories, topics, entities, n_queries
    )
    return StressCorpus(
        seed=seed,
        topics=topics,
        memories=memories,
        entities=entities,
        tools=[ToolSpecLite(name=n, description=d) for n, d in TOOLS[:n_tools]],
        recall_cases=recall_cases,
        topic_cases=topic_cases,
        entity_cases=entity_cases,
        tool_cases=tool_cases,
        dedup_cases=dedup_cases,
    )
