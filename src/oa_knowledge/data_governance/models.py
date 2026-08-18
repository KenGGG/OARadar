"""数据治理领域常量；持久化实体位于 ``db.models``。"""

DATA_RULES_VERSION = "data-v1"
CLEANUP_ITEM_STATUSES = frozenset(
    {"planned", "quarantined", "restored", "purged", "skipped", "failed"}
)
