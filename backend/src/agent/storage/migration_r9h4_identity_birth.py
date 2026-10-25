"""迁移 65（R9-H4）：内部数据身份生命周期依据（birth token）。

internal_data_identities 之前只有 volume:file_id；文件真正删除后编号可能被新文件
复用。增加 birth_key（Windows 创建时间 / POSIX 可获得的 birth/ctime 依据），
身份匹配时 identity+birth 同时一致才认为是同一对象；旧行 birth_key NULL 表示
生命周期依据未知，只能按路径/历史登记保守处理，不能声称同一文件。

只追加；补偿 66 重放 DDL。数据本身不删除。
"""

from __future__ import annotations

MIGRATION: tuple[int, list[str]] = (
    65,
    [
        "ALTER TABLE internal_data_identities ADD COLUMN birth_key TEXT",
    ],
)
