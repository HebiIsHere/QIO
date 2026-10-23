"""迁移 63（R9-H3）：撤销 targeted_limits_review 例外产生的越权授权记录。

只追加，不改历史迁移。

R9-H2 曾在完全无限制模式为“限制未恢复”引入 targeted_limits_review，调用普通
ModelAuthorizationReviewer，并写入 authorization_records（source=model_review，
mode=unrestricted）。R9-H3 已移除该路径；这里的授权记录不再具有授权效力。

识别口径（可明确判断）：
  authorization_records.mode = 'unrestricted'
  AND source = 'model_review'
  AND revoked_at IS NULL
正常完全无限制模式不会产生 model_review 记录；受限模式记录 mode=restricted。
只置 revoked_at，保留行与审查审计，不删除历史，不清空其它授权。

注意：补偿迁移禁止改数据，因此补偿 64 只重放 DDL，不重放本 UPDATE。
"""

from __future__ import annotations

#: 一次性撤销（只在迁移 63 执行；不进补偿迁移）。
REVOKE_DATA = """
UPDATE authorization_records
SET revoked_at = COALESCE(revoked_at, '2026-10-23T00:00:00+00:00')
WHERE mode = 'unrestricted'
  AND source = 'model_review'
  AND revoked_at IS NULL
"""

MIGRATION: tuple[int, list[str]] = (63, [REVOKE_DATA])
