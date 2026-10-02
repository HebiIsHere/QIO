"""C2.1 整库敏感信息扫描（只读，真实开发库）。

用法（在 frontend 目录下）：
    uv run --frozen --project ../backend python visual/scan/scan_db_sensitive.py

输出只有计数与命中的列名，不打印任何原文；**不写库**（URI 用 mode=ro）。
"""
import json
import re
import sqlite3

DB = "D:/QIO-data/app.db"
PATH_RE = re.compile(r"[A-Za-z]:\\|[A-Za-z]:/|\\\\[A-Za-z0-9_.-]+\\")
SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{6,}|bearer\s+[A-Za-z0-9._\-]+|api[_-]?key\s*[:=]|qio_key_|-----BEGIN)"
)


def scan(conn, table, columns):
    cols = ", ".join(columns)
    rows = conn.execute("SELECT " + cols + " FROM " + table).fetchall()
    path_hits = {}
    secret_hits = {}
    for row in rows:
        for col in columns:
            value = row[col]
            if value is None:
                continue
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            if PATH_RE.search(text):
                path_hits[col] = path_hits.get(col, 0) + 1
            if SECRET_RE.search(text):
                secret_hits[col] = secret_hits.get(col, 0) + 1
    print(table.ljust(16), "rows=", len(rows), "PATH:", path_hits or "无", "SECRET形态:", secret_hits or "无")


def main():
    conn = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    scan(conn, "turn_traces", [
        "topic", "injection", "model_calls", "tool_runs", "writes", "warnings",
        "error", "final_preview", "initial_topic", "final_topic",
    ])
    scan(conn, "tool_calls", ["arguments", "result", "error"])
    scan(conn, "tool_records", ["arguments", "output", "error"])
    scan(conn, "messages", ["content"])
    conn.close()


if __name__ == "__main__":
    main()
