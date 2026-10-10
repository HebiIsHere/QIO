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
    (
        15,
        [
            # 阶段 2：派生任务（摘要 / 索引 / 实体 / 知识）。
            #
            # 为什么要单独一张表：封存片段是**对话本身**的状态（必须立刻完成、
            # 事务内完成），而摘要与索引是**可重试的派生数据**。以前两者绑在一起，
            # 结果就是「为了封块要等一次模型调用」，失败还会把封存一起拖回去。
            #
            # UNIQUE(kind, fragment_id, content_version) 保证同一份内容只派发一次：
            # 任务重试、进程重启、迟到结果都不会重复制造知识/实体/索引。
            """
            CREATE TABLE IF NOT EXISTS derived_tasks (
                id              TEXT PRIMARY KEY,
                kind            TEXT NOT NULL,
                fragment_id     TEXT NOT NULL,
                content_version INTEGER NOT NULL,
                state           TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT,
                run_after       TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                UNIQUE (kind, fragment_id, content_version)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_derived_tasks_due ON derived_tasks(state, run_after)",
            "CREATE INDEX IF NOT EXISTS idx_derived_tasks_fragment ON derived_tasks(fragment_id)",
        ],
    ),
    (
        16,
        [
            # 阶段 3：旧数据的关系与内容版本。
            #
            # 坏关系（指向不存在的片段、或指向别的话题的片段）不能留着假装是路径：
            # 断开来源并保留 unknown。历史本体（消息与摘要）一行不动。
            # 顺序很重要：**先断开坏来源，再按来源标记关系类型** ——
            # 反过来会把「来源已经不成立」的行也标成历史接续。
            """
            UPDATE fragments
               SET source_fragment_id = NULL
             WHERE source_fragment_id IS NOT NULL
               AND NOT EXISTS (
                     SELECT 1 FROM fragments AS src
                      WHERE src.id = fragments.source_fragment_id
                        AND src.topic_id = fragments.topic_id
                   )
            """,
            # 关系列是新加的，旧行全是 NULL。按「能确认到什么程度」分两档：
            # * 有 source_fragment_id 的（迁移 11 起只有「从历史继续」会写这个字段）
            #   → 可以确认是历史重新展开；
            # * 其余（普通延续，但分不清容量分段还是阶段变化）→ unknown，不猜。
            """
            UPDATE fragments
               SET relation_type = 'history_reopen'
             WHERE relation_type IS NULL AND source_fragment_id IS NOT NULL
            """,
            "UPDATE fragments SET relation_type = 'unknown' WHERE relation_type IS NULL",
            # 内容版本：封存过的旧片段用「消息条数」补齐，派生任务据此校验迟到结果。
            """
            UPDATE fragments
               SET content_version = (
                     SELECT COUNT(*) FROM messages WHERE messages.fragment_id = fragments.id
                   )
             WHERE content_version = 0
               AND closed_at IS NOT NULL
               AND EXISTS (SELECT 1 FROM messages WHERE messages.fragment_id = fragments.id)
            """,
            # 缺少摘要的旧封存片段：登记一次幂等补齐任务（这里不调用模型）。
            """
            INSERT OR IGNORE INTO derived_tasks
                (id, kind, fragment_id, content_version, state, attempts, last_error,
                 run_after, created_at, updated_at)
            SELECT 'task_backfill_' || id, 'summary', id, content_version, 'pending', 0, NULL,
                   NULL, datetime('now'), datetime('now')
              FROM fragments
             WHERE closed_at IS NOT NULL
               AND (summary IS NULL OR summary = '')
               AND content_version > 0
            """,
        ],
    ),
    (
        17,
        [
            # 阶段 4：容量按「真实轮次」统计，而不是按消息条数。
            # 消息带上它所属的轮次之后，「系统通知 / 工具消息不计轮」才可判定；
            # 绑带上记一句「这一轮是不是系统驱动的」，用于把通知轮排除在计数之外。
            "ALTER TABLE messages ADD COLUMN turn_id TEXT",
            "CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages(turn_id)",
            "ALTER TABLE turn_bindings ADD COLUMN system INTEGER NOT NULL DEFAULT 0",
        ],
    ),
    (
        18,
        [
            # 阶段 4：容量分段要**记住它接的是哪一段**。
            #
            # 之前的做法是：容量到点 → 封存 → 下一个消息来了再 lazy 建新片段，
            # 于是新片段没有来源、也没有 same_stage —— 路径在容量边界断掉，
            # 而规格要求「容量延续保留同阶段」。
            #
            # 这里只登记「下一次在这个话题上建片段时，它接谁」这条待用信息；
            # 真正的片段仍然在**确实有消息要写**时才创建（不制造空片段）。
            """
            CREATE TABLE IF NOT EXISTS fragment_continuations (
                topic_id           TEXT PRIMARY KEY,
                source_fragment_id TEXT NOT NULL,
                same_stage         INTEGER NOT NULL DEFAULT 1,
                reason             TEXT,
                created_at         TEXT NOT NULL
            )
            """,
        ],
    ),
    (
        19,
        [
            # 轨迹表以前只落「结果正文」，失败的调用落下来是一片空白 ——
            # 事后无法回答「它当时为什么失败」。失败原因必须有自己的列。
            "ALTER TABLE tool_calls ADD COLUMN error TEXT",
        ],
    ),
    (
        20,
        [
            # 工具调用历史：用户能回看的完整记录（打码后的参数与输出全文）。
            # 与轨迹表分工不同 —— 轨迹是审计摘要且可被用户关掉，这里是复盘用的正文。
            """
            CREATE TABLE IF NOT EXISTS tool_records (
                id             TEXT PRIMARY KEY,
                turn_id        TEXT NOT NULL,
                topic_id       TEXT,
                call_id        TEXT NOT NULL DEFAULT '',
                seq            INTEGER NOT NULL DEFAULT 0,
                tool_name      TEXT NOT NULL,
                arguments      TEXT NOT NULL DEFAULT '{}',
                output         TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT 'success',
                error          TEXT NOT NULL DEFAULT '',
                duration_ms    INTEGER,
                truncated      INTEGER NOT NULL DEFAULT 0,
                output_missing INTEGER NOT NULL DEFAULT 0,
                missing_reason TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tool_records_turn ON tool_records(turn_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_records_created ON tool_records(created_at)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_records_call
                ON tool_records(turn_id, call_id) WHERE call_id <> ''
            """,
        ],
    ),
    (
        21,
        [
            # 凭据体验：把「保存成功」与「验证通过」拆成两件事 ——
            # 保存只说明配置安全写入了；验证是一次真实调用，结果单独记。
            # `kind` 存连接协议，避免「测试用 OpenAI 客户端、正式跑 Anthropic 分支」
            # 这种两边不一致（历史行留 NULL，运行期按地址回退判断）。
            "ALTER TABLE credentials ADD COLUMN kind TEXT",
            "ALTER TABLE credentials ADD COLUMN verify_state TEXT NOT NULL DEFAULT 'unverified'",
            "ALTER TABLE credentials ADD COLUMN verified_at TEXT",
            "ALTER TABLE credentials ADD COLUMN verify_error TEXT",
            "ALTER TABLE credentials ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0",
            # 旧数据兼容：本次改动之前建的凭据都是「正在用」的，不能因为新增了一个
            # 验证状态就把它们标成未验证并停用。legacy = 按老行为视为可用。
            "UPDATE credentials SET verify_state = 'legacy'",
            # 旧库里第一条可用的主对话凭据成为显式默认项（新库这条语句不动任何行）。
            """UPDATE credentials SET is_default = 1 WHERE id = (
                   SELECT id FROM credentials
                    WHERE status = 'active' AND enabled = 1
                      AND tags LIKE '%"main-loop"%'
                    ORDER BY created_at, id LIMIT 1)""",
        ],
    ),
    (
        22,
        [
            # 用量归因：以前只有 budget_used 一个数字（而且运行期从不回写），
            # 「已用量」永远是 0，上限也就不可能生效。这里补上「进 / 出」两列，
            # 让界面能分开显示；budget_used 仍然保留为合计，预算闸门只认它。
            "ALTER TABLE credentials ADD COLUMN usage_input REAL NOT NULL DEFAULT 0",
            "ALTER TABLE credentials ADD COLUMN usage_output REAL NOT NULL DEFAULT 0",
        ],
    ),
    (
        23,
        [
            # 审批跨重启：等待中的审批以前只活在进程内存里，重启后它既不会执行，
            # 也没人告诉用户「那件事没做」。这里把每一次请求落一行，重启时把
            # 仍然是 pending 的记录标成 interrupted —— 读出来就是「那一次没有执行」。
            """
            CREATE TABLE IF NOT EXISTS pending_approvals (
                approval_id TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                payload     TEXT NOT NULL DEFAULT '{}',
                turn_id     TEXT,
                session_id  TEXT,
                created_at  TEXT NOT NULL,
                expires_at  TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                resolved_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_pending_approvals_status ON pending_approvals(status)",
        ],
    ),
    (
        24,
        [
            # Trace 阶段时间：一轮的时间去向（见 agent/trace/phases.py）。
            # 以前只记模型/工具耗时，一次真实 turn 的 49.8 秒在 Trace 里无法解释。
            # 旧行默认 '{}'，读取端按「没有阶段账本」处理（向后兼容）。
            "ALTER TABLE turn_traces ADD COLUMN phases TEXT NOT NULL DEFAULT '{}'",
        ],
    ),
    (
        25,
        [
            # Turn 队列台账：排队中的用户消息以前只活在进程内存里，重启即静默消失。
            # 这里给每一个**被 API 接受过**的 turn 落一行，并区分
            # queued / running / 终态 / interrupted（见 storage/turn_journal.py）。
            """
            CREATE TABLE IF NOT EXISTS turn_journal (
                turn_id         TEXT PRIMARY KEY,
                message         TEXT NOT NULL DEFAULT '',
                topic_id        TEXT,
                notify          INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'queued',
                created_at      TEXT NOT NULL,
                started_at      TEXT,
                ended_at        TEXT,
                updated_at      TEXT NOT NULL,
                reason          TEXT,
                user_message_id TEXT,
                recovered_at    TEXT,
                recovered_by    TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_turn_journal_status ON turn_journal(status)",
            "CREATE INDEX IF NOT EXISTS idx_turn_journal_created ON turn_journal(created_at)",
        ],
    ),
    (
        # 号段选择：26 / 27 / 28 已被同基线的其它修复分支占用（附件发送、进程审计、
        # 统一进程流等分支各自用到了 28），基线 main（6e073e9）自己停在 25。
        # `apply_migrations` 是「target <= 已记录版本就跳过」，所以如果这里也用 26，
        # 任何被那些分支碰过的**存量库**都会把这条迁移整段跳过去 —— 结果是
        # `instances` 表不存在、`AppContext.__init__` 直接抛
        # `sqlite3.OperationalError: no such table: instances`，后端起不来。
        # 取 29（= 已知最大号 28 + 1）保证：无论那些分支先合还是后合，
        # 这条迁移对任何存量库都**必然**会被应用一次。
        29,
        [
            # 实例归属（契约 C1）：一个进程实例在库里有身份，其它实例才能判断
            # 「它写下的记录现在还算不算活着」。
            #
            # 为什么需要：以前启动恢复只有一句「把 leave 的 pending/queued/running
            # 全标成 interrupted」—— 隐含假设「数据库只有我一个写入者」。第二个后端
            # 实例（多开、重启期间旧的还没退）一启动，就会把**还在跑**的实例的任务和
            # 待确认事项全部标成中断，并在它的恢复里重复认领同一批派生任务。
            #
            # 判据不许只看 pid 或只看时间阈值（两者都会误判）：
            #   exited_at 非空            → 死（显式退出）
            #   心跳新鲜（<= HEARTBEAT_TTL）→ 活
            #   心跳过期 **且** pid 不存在  → 死
            #   其余                      → 未知（unknown）—— 未知一律不改状态。
            """
            CREATE TABLE IF NOT EXISTS instances (
                instance_id    TEXT PRIMARY KEY,
                pid            INTEGER,
                host           TEXT,
                started_at     TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                exited_at      TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_instances_heartbeat ON instances(last_heartbeat)",
            # 记录 → 实例的归属。**一张表覆盖所有记录类型**（turn / approval / 派生任务），
            # 避免每种记录各自长一列 owner 而彼此口径不一。
            # 主键 (record_type, record_id) 让归属唯一：同一条记录不可能同时属于两个实例。
            """
            CREATE TABLE IF NOT EXISTS record_owners (
                record_type TEXT NOT NULL,
                record_id   TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                PRIMARY KEY (record_type, record_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_record_owners_instance ON record_owners(instance_id)",
            # turn_journal 自己的归属列：队列台账是恢复的第一入口，读它时
            # 不必再 join 一次归属表（老行为：所属者未知）。
            "ALTER TABLE turn_journal ADD COLUMN owner_instance_id TEXT",
            "CREATE INDEX IF NOT EXISTS idx_turn_journal_owner ON turn_journal(owner_instance_id)",
            # 派生任务的归属（契约 C7 需要 kind/owner_instance_id/claim_generation/
            # attempts/last_error）。kind / attempts / last_error 在迁移 15 已有，
            # 这里只补缺的两列：owner_instance_id 与 claim_generation。
            # claim_generation 由认领方递增：迟到结果带着旧 generation 回来时被丢弃。
            "ALTER TABLE derived_tasks ADD COLUMN owner_instance_id TEXT",
            "ALTER TABLE derived_tasks ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0",
            # 待确认事项（审批）的归属列：与 record_owners 双写，
            # 让「启动时该不该把它标成 interrupted」能按实例判定，而不是一律标。
            "ALTER TABLE pending_approvals ADD COLUMN owner_instance_id TEXT",
            # 知识版本链（契约 C4，B 组用）：只加身份列，**不在这里回填**。
            # 回填（chain_id=链根 id、version=1+祖先数）由 B 在读写侧用
            # 「列存在则用、不存在则降级」的适配层处理：历史库可能有同链重复
            # active，在本迁移里回填或建唯一索引会让迁移直接失败，与「保守保留
            # 原文与历史」冲突。每链唯一 active 由 B 在写入侧（BEGIN IMMEDIATE +
            # 条件校验）保证。
            "ALTER TABLE knowledge ADD COLUMN chain_id TEXT",
            "ALTER TABLE knowledge ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_chain ON knowledge(chain_id, version)",
            # 实体卡片人工纠正保护（M04，C 组用）：
            # revision 用来判断「这条卡片被人工改过几次」；
            # field_meta 承载各字段 source(user/auto)+revision、用户删除属性的墓碑、
            # 以及冲突时保留的可管理候选。旧行默认 0 / '{}'（没有人工修订记录）。
            "ALTER TABLE entity_cards ADD COLUMN revision INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE entity_cards ADD COLUMN field_meta TEXT NOT NULL DEFAULT '{}'",
        ],
    ),
    (
        # B01 补偿迁移：**高版本存量库不能漏对象**。
        #
        # 为什么需要它（真实踩到过）：`apply_migrations` 的语义是「target <= 已记录版本
        # 就整条跳过」，而 schema_version 是**只看版本号**、不看对象的。于是只要有一个
        # 存量库把版本号记到了 29（例如被某个兄弟分支的迁移碰过、或迁移 29 的 DDL 只落下
        # 一半就被旧代码/手改弄丢了），迁移 29 就永远不会再跑：`instances` 表不存在，
        # `AppContext.__init__` 直接抛 `sqlite3.exceptions.OperationalError: no such table:
        # instances`，后端起不来；缺列则更隐晦 —— 读到一半才炸。
        #
        # 这一条是**纯补偿**：只按 `IF NOT EXISTS` /「对象已存在就跳过」的幂等写法把本轮
        # 必需对象补齐，**不动任何一行的用户数据**（没有 DROP / DELETE / UPDATE）。
        # 它不改迁移 1–29 的含义；`storage/migrate.py` 的 `verify_required_objects()`
        # 还会在迁移序列跑完之后**按对象**再核一遍，缺了就重放这一条，仍缺就抛
        # `SchemaIncompleteError`（不再静默放过）。
        #
        # 顺序要求：每条 `ALTER TABLE ... ADD COLUMN` 之前必须先 `CREATE TABLE IF NOT
        # EXISTS` 出**完整现代形状** —— 表整个缺失时列才存在；表已存在时 CREATE 是空操作，
        # 后面的 ADD COLUMN 由「对象已存在就跳过」的幂等规则接住（旧库半截迁移的自愈同理）。
        # 编号必须 > 当前最大号（29），这样任何存量库都**必然**会跑到它一次。
        30,
        [
            # --- 实例归属（迁移 29 的核心对象）-----------------------------
            """
            CREATE TABLE IF NOT EXISTS instances (
                instance_id    TEXT PRIMARY KEY,
                pid            INTEGER,
                host           TEXT,
                started_at     TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                exited_at      TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_instances_heartbeat ON instances(last_heartbeat)",
            """
            CREATE TABLE IF NOT EXISTS record_owners (
                record_type TEXT NOT NULL,
                record_id   TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                PRIMARY KEY (record_type, record_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_record_owners_instance ON record_owners(instance_id)",
            # --- turn_journal：队列台账 + 归属列 ---------------------------
            """
            CREATE TABLE IF NOT EXISTS turn_journal (
                turn_id         TEXT PRIMARY KEY,
                message         TEXT NOT NULL DEFAULT '',
                topic_id        TEXT,
                notify          INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL DEFAULT 'queued',
                created_at      TEXT NOT NULL,
                started_at      TEXT,
                ended_at        TEXT,
                updated_at      TEXT NOT NULL,
                reason          TEXT,
                user_message_id TEXT,
                recovered_at    TEXT,
                recovered_by    TEXT,
                owner_instance_id TEXT
            )
            """,
            "ALTER TABLE turn_journal ADD COLUMN owner_instance_id TEXT",
            "CREATE INDEX IF NOT EXISTS idx_turn_journal_owner ON turn_journal(owner_instance_id)",
            # --- derived_tasks：归属 + 认领代次（迟到结果靠它被丢弃）--------
            """
            CREATE TABLE IF NOT EXISTS derived_tasks (
                id              TEXT PRIMARY KEY,
                kind            TEXT NOT NULL,
                fragment_id     TEXT NOT NULL,
                content_version INTEGER NOT NULL,
                state           TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT,
                run_after       TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                owner_instance_id TEXT,
                claim_generation INTEGER NOT NULL DEFAULT 0,
                UNIQUE (kind, fragment_id, content_version)
            )
            """,
            "ALTER TABLE derived_tasks ADD COLUMN owner_instance_id TEXT",
            "ALTER TABLE derived_tasks ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0",
            # --- pending_approvals：审批归属 -------------------------------
            """
            CREATE TABLE IF NOT EXISTS pending_approvals (
                approval_id TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                payload     TEXT NOT NULL DEFAULT '{}',
                turn_id     TEXT,
                session_id  TEXT,
                created_at  TEXT NOT NULL,
                expires_at  TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                resolved_at TEXT,
                owner_instance_id TEXT
            )
            """,
            "ALTER TABLE pending_approvals ADD COLUMN owner_instance_id TEXT",
            # --- knowledge：版本链身份列 -----------------------------------
            # 这里只建列，**不回填**（回填由读写侧的适配层负责）：历史库可能有同链
            # 重复 active，在本迁移里回填或建唯一索引会让迁移直接失败，与「保守保留
            # 原文与历史」冲突。
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
                export        INTEGER NOT NULL DEFAULT 0,
                node_ids      TEXT NOT NULL DEFAULT '[]',
                chain_id      TEXT,
                version       INTEGER NOT NULL DEFAULT 1
            )
            """,
            "ALTER TABLE knowledge ADD COLUMN chain_id TEXT",
            "ALTER TABLE knowledge ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_chain ON knowledge(chain_id, version)",
            # --- entity_cards：人工纠正保护（revision / field_meta）---------
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
                updated_at TEXT NOT NULL,
                revision   INTEGER NOT NULL DEFAULT 0,
                field_meta TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "ALTER TABLE entity_cards ADD COLUMN revision INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE entity_cards ADD COLUMN field_meta TEXT NOT NULL DEFAULT '{}'",
        ],
    ),
]

SCHEMA_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0

# `storage/migrate.py` 的补偿入口：`REQUIRED_OBJECTS` 缺失时**重放这一条**。
# 用编号固定引用（不是「最后一条」）：以后追加迁移不会把它挤掉。
COMPENSATION_VERSION = 30
