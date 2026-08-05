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
]

SCHEMA_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0