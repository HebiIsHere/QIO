"""Schema definitions and ordered migrations.

Schema version 1 covers the 9 tables agreed in design review:
nodes / edges / fragments / messages / memory_index / knowledge /
cursor / credentials / credential_audit.
"""

MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id         TEXT PRIMARY KEY,
                type       TEXT NOT NULL CHECK (type IN ('user','entity','topic')),
                name       TEXT NOT NULL,
                meta       TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type)",
            """
            CREATE TABLE IF NOT EXISTS edges (
                id         TEXT PRIMARY KEY,
                src        TEXT NOT NULL REFERENCES nodes(id),
                dst        TEXT NOT NULL REFERENCES nodes(id),
                type       TEXT NOT NULL CHECK (type IN ('mention','related','owns')),
                weight     REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (src, dst, type)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src)",
            "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst)",
            """
            CREATE TABLE IF NOT EXISTS fragments (
                id               TEXT PRIMARY KEY,
                topic_id         TEXT NOT NULL REFERENCES nodes(id),
                start_message_id TEXT,
                end_message_id   TEXT,
                summary          TEXT,
                summary_model    TEXT,
                summary_version  INTEGER NOT NULL DEFAULT 0,
                created_at       TEXT NOT NULL,
                closed_at        TEXT,
                meta             TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_fragments_topic ON fragments(topic_id)",
            """
            CREATE TABLE IF NOT EXISTS messages (
                id           TEXT PRIMARY KEY,
                fragment_id  TEXT REFERENCES fragments(id),
                role         TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
                content      TEXT NOT NULL DEFAULT '',
                content_type TEXT NOT NULL DEFAULT 'text',
                model        TEXT,
                raw          TEXT NOT NULL DEFAULT '{}',
                created_at   TEXT NOT NULL,
                storage_tier TEXT NOT NULL DEFAULT 'hot' CHECK (storage_tier IN ('hot','cold'))
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_messages_fragment ON messages(fragment_id)",
            "CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)",
            """
            CREATE TABLE IF NOT EXISTS memory_index (
                id             TEXT PRIMARY KEY,
                fragment_id    TEXT NOT NULL REFERENCES fragments(id),
                topic_id       TEXT NOT NULL REFERENCES nodes(id),
                entity_ids     TEXT NOT NULL DEFAULT '[]',
                keywords       TEXT NOT NULL DEFAULT '[]',
                title          TEXT,
                token_estimate INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_memory_index_topic ON memory_index(topic_id)",
            """
            CREATE TABLE IF NOT EXISTS knowledge (
                id            TEXT PRIMARY KEY,
                category      TEXT NOT NULL CHECK (category IN
                              ('user_profile','agent_self','goal','general_fact','tool_experience')),
                state         TEXT NOT NULL CHECK (state IN
                              ('draft','pending_review','verified','active','expired','revoked')),
                content       TEXT NOT NULL,
                supersedes_id TEXT REFERENCES knowledge(id),
                topic_id      TEXT REFERENCES nodes(id),
                entity_ids    TEXT NOT NULL DEFAULT '[]',
                provenance    TEXT NOT NULL DEFAULT '{}',
                confidence    REAL,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                activated_at  TEXT,
                expired_at    TEXT,
                export        INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_knowledge_state ON knowledge(state)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_topic ON knowledge(topic_id)",
            """
            CREATE TABLE IF NOT EXISTS cursor (
                id          TEXT PRIMARY KEY,
                topic_id    TEXT REFERENCES nodes(id),
                fragment_id TEXT REFERENCES fragments(id),
                anchor_type TEXT NOT NULL CHECK (anchor_type IN ('active','pending','history')),
                updated_at  TEXT NOT NULL
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cursor_active ON cursor(anchor_type) WHERE anchor_type IN ('active','pending')",
            """
            CREATE TABLE IF NOT EXISTS credentials (
                id            TEXT PRIMARY KEY,
                version       INTEGER NOT NULL DEFAULT 1,
                tags          TEXT NOT NULL DEFAULT '[]',
                endpoint      TEXT,
                default_model TEXT,
                scope         TEXT NOT NULL DEFAULT 'null',
                budget        REAL,
                budget_used   REAL NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','revoked','expired')),
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                last_used_at  TEXT,
                note          TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS credential_audit (
                id            TEXT PRIMARY KEY,
                credential_id TEXT NOT NULL REFERENCES credentials(id),
                action        TEXT NOT NULL CHECK (action IN ('create','update','revoke','test')),
                from_version  INTEGER,
                to_version    INTEGER,
                triggered_by  TEXT,
                created_at    TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_credential_audit_cred ON credential_audit(credential_id)",
        ],
    ),
    (
        2,
        [
            """
            CREATE TABLE IF NOT EXISTS entity_mentions (
                id            TEXT PRIMARY KEY,
                entity_name   TEXT NOT NULL,
                topic_id      TEXT REFERENCES nodes(id),
                mention_count INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                UNIQUE (entity_name, topic_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_entity_mentions_name ON entity_mentions(entity_name)",
        ],
    ),
    (
        3,
        [
            # knowledge entries attach to any graph node (topic/entity/user)
            "ALTER TABLE knowledge ADD COLUMN node_ids TEXT NOT NULL DEFAULT '[]'",
            # migrate existing topic_id values into node_ids (json_array -> valid JSON)
            "UPDATE knowledge SET node_ids = json_array(topic_id) WHERE topic_id IS NOT NULL AND node_ids = '[]'",

            "CREATE INDEX IF NOT EXISTS idx_knowledge_node ON knowledge(node_ids)",
        ],
    ),
    (
        4,
        [
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                id         TEXT PRIMARY KEY,
                doc_type   TEXT NOT NULL CHECK (doc_type IN ('memory_index','topic')),
                ref_id     TEXT NOT NULL,
                model      TEXT NOT NULL,
                dims       INTEGER NOT NULL,
                vector     BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (doc_type, ref_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_embeddings_ref ON embeddings(doc_type, ref_id)",
            """
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ],
    ),
    (
        5,
        [
            """
            CREATE TABLE IF NOT EXISTS tools (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                definition TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active','removed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tools_status ON tools(status)",
        ],
    ),

    (
        6,
        [
            """
            CREATE TABLE IF NOT EXISTS tool_calls (
                id         TEXT PRIMARY KEY,
                topic_id   TEXT,
                tool_name  TEXT NOT NULL,
                arguments  TEXT NOT NULL DEFAULT '{}',
                result     TEXT NOT NULL DEFAULT '',
                ok         INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_topic ON tool_calls(topic_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_created ON tool_calls(created_at)",
        ],
    ),
    (
        7,
        [
            """
            CREATE TABLE IF NOT EXISTS entity_cards (
                id         TEXT PRIMARY KEY,
                node_id    TEXT REFERENCES nodes(id),
                name       TEXT NOT NULL,
                aliases    TEXT NOT NULL DEFAULT '[]',
                kind       TEXT,
                summary    TEXT,
                attributes TEXT NOT NULL DEFAULT '[]',
                state      TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_entity_cards_node ON entity_cards(node_id)",
            # 重建 edges：放开 type 的 CHECK，支持任意关系类型（母子/属于/饲养…）
            """
            CREATE TABLE IF NOT EXISTS edges_v2 (
                id         TEXT PRIMARY KEY,
                src        TEXT NOT NULL REFERENCES nodes(id),
                dst        TEXT NOT NULL REFERENCES nodes(id),
                type       TEXT NOT NULL,
                weight     REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (src, dst, type)
            )
            """,
            "INSERT INTO edges_v2 (id, src, dst, type, weight, created_at, updated_at) "
            "SELECT id, src, dst, type, weight, created_at, updated_at FROM edges",
            "DROP TABLE edges",
            "ALTER TABLE edges_v2 RENAME TO edges",
            "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src)",
            "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst)",
            # 重建 embeddings：doc_type 支持 entity_card
            """
            CREATE TABLE IF NOT EXISTS embeddings_v2 (
                id         TEXT PRIMARY KEY,
                doc_type   TEXT NOT NULL CHECK (doc_type IN ('memory_index','topic','entity_card')),
                ref_id     TEXT NOT NULL,
                model      TEXT NOT NULL,
                dims       INTEGER NOT NULL,
                vector     BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (doc_type, ref_id)
            )
            """,
            "INSERT INTO embeddings_v2 (id, doc_type, ref_id, model, dims, vector, created_at, updated_at) "
            "SELECT id, doc_type, ref_id, model, dims, vector, created_at, updated_at FROM embeddings",
            "DROP TABLE embeddings",
            "ALTER TABLE embeddings_v2 RENAME TO embeddings",
        ],
    ),
    (
        8,
        [
            # Soft enable/disable without conflating with `revoked`/`expired`.
            # Policy and secret reads treat `enabled = 0` as unusable.
            "ALTER TABLE credentials ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1",
        ],
    ),
    (
        9,
        [
            # Authorization is now expressed by tags (+ resolve required_tags),
            # the enabled flag, and a tool's credential_ref binding. The per-key
            # requestor whitelist (`scope`) is removed.
            "ALTER TABLE credentials DROP COLUMN scope",
        ],
    ),
    (
        10,
        [
            # Agent Trace：单轮 turn 的可观测性文档（JSON 列，避免保存大段原文）。
            """
            CREATE TABLE IF NOT EXISTS turn_traces (
                turn_id       TEXT PRIMARY KEY,
                status        TEXT NOT NULL DEFAULT 'running',
                started_at    TEXT NOT NULL,
                ended_at      TEXT,
                duration_ms   INTEGER,
                initial_topic TEXT,
                final_topic   TEXT,
                topic         TEXT NOT NULL DEFAULT '{}',
                injection     TEXT NOT NULL DEFAULT '{}',
                model_calls   TEXT NOT NULL DEFAULT '[]',
                tool_runs     TEXT NOT NULL DEFAULT '[]',
                writes        TEXT NOT NULL DEFAULT '{}',
                warnings      TEXT NOT NULL DEFAULT '[]',
                error         TEXT,
                final_preview TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_turn_traces_started ON turn_traces(started_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_turn_traces_status ON turn_traces(status)",
        ],
    ),
    (
        11,
        [
            # 从历史继续（第二阶段）：新片段记住它接续的是哪一段历史。
            # 旧片段保持不变（关闭的 Fragment 不得重新修改），分支关系只写在新行上。
            "ALTER TABLE fragments ADD COLUMN source_fragment_id TEXT",
        ],
    ),
    (
        12,
        [
            # 产品规则：「同一个 Topic 同时只能有一个开放 Fragment」。
            # 只靠 Python 的「先 SELECT 再 INSERT」在并发或中途失败时会破掉它，
            # 所以把规则交给数据库（部分唯一索引）。
            #
            # 但已有用户的库里可能已经存在多个开放片段 —— 直接建索引会让应用启动失败。
            # 因此先做**不删数据**的归一化：同一 topic 下只保留 (created_at, id) 最大的一条
            # 为开放，其余的存在时间较早的开放片段按自己的 created_at 归档关闭。
            """
            UPDATE fragments
               SET closed_at = created_at
             WHERE closed_at IS NULL
               AND EXISTS (
                     SELECT 1 FROM fragments AS newer
                      WHERE newer.topic_id = fragments.topic_id
                        AND newer.closed_at IS NULL
                        AND (newer.created_at > fragments.created_at
                             OR (newer.created_at = fragments.created_at
                                 AND newer.id > fragments.id))
                   )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fragments_one_open_per_topic "
            "ON fragments(topic_id) WHERE closed_at IS NULL",
        ],
    ),
    (
        13,
        [
            # 阶段 1：把「这一轮属于哪个 Topic / 哪个 Fragment」从「写入时再看一眼 Anchor」
            # 变成**一条持久绑定**。绑定在轮前确定，写入、导航推进、失败收尾都读同一条，
            # 因此后面的导航变化不会把已经提交的消息搬走。
            #
            # write_state: open | closed —— 写入是否还在进行（用于「尚有未完成写入的片段不得封存」）。
            # status: 该轮的终态（completed/failed/cancelled/unavailable），运行中为 NULL。
            """
            CREATE TABLE IF NOT EXISTS turn_bindings (
                turn_id       TEXT PRIMARY KEY,
                topic_id      TEXT NOT NULL,
                fragment_id   TEXT,
                intent_id     TEXT,
                intent_version INTEGER,
                write_state   TEXT NOT NULL DEFAULT 'open',
                status        TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_turn_bindings_topic ON turn_bindings(topic_id)",
            "CREATE INDEX IF NOT EXISTS idx_turn_bindings_fragment ON turn_bindings(fragment_id)",
            # 用户「从某段历史继续」的接续意图：点击时只登记，真正执行到本轮消息时才落实。
            # 不复用「模型建议换话题、等待确认」的 pending 状态（两者含义不同）。
            """
            CREATE TABLE IF NOT EXISTS continuation_intents (
                intent_id            TEXT PRIMARY KEY,
                topic_id             TEXT NOT NULL,
                source_fragment_id   TEXT,
                version              INTEGER NOT NULL DEFAULT 1,
                state                TEXT NOT NULL DEFAULT 'registered',
                resolved_fragment_id TEXT,
                request_id           TEXT,
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_continuation_intents_state ON continuation_intents(state)",
            # 同一个 request_id 只能有一条意图：传输层重发不会制造第二条接续路径。
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_continuation_intents_request "
            "ON continuation_intents(request_id) WHERE request_id IS NOT NULL",
        ],
    ),
    (
        14,
        [
            # 阶段 1 / 阶段 3 需要的片段元信息：
            # - relation_type：这条片段与它来源的关系（普通延续 / 历史重新展开 / 未知旧数据）
            # - boundary_reason：为什么在这里分段（容量 / 阶段变化 / 历史接续 / 其他）
            # - same_stage：容量延续是否仍在同一阶段（1/0，未知为 NULL）
            # - content_version：封存时固定的内容版本，派生任务（摘要/索引/实体）据此防止迟到结果覆盖新内容
            "ALTER TABLE fragments ADD COLUMN relation_type TEXT",
            "ALTER TABLE fragments ADD COLUMN boundary_reason TEXT",
            "ALTER TABLE fragments ADD COLUMN same_stage INTEGER",
            "ALTER TABLE fragments ADD COLUMN content_version INTEGER NOT NULL DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS idx_fragments_source ON fragments(source_fragment_id)",
        ],
    ),
]

SCHEMA_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0
