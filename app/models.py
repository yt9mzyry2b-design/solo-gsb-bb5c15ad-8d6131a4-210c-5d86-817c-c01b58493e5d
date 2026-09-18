"""SQLAlchemy ORM 模型。"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FlagConfig(Base):
    """功能开关配置。rules / variants 以 JSON 文本存储。"""

    __tablename__ = "flag_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flag_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    # [{ "priority": 1, "attribute": "region", "operator": "eq", "values": ["cn"],
    #    "variant_key": "red" }]
    rules_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # [{ "key": "red", "weight": 50, "description": "" }]
    variants_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class FlagConfigVersion(Base):
    """开关配置的不可变历史版本。

    每次创建 / 更新开关时，在同一事务中追加一行完整配置快照；
    历史行只增不改，判定可按 version 精确重放当时的规则与权重。
    """

    __tablename__ = "flag_config_versions"
    __table_args__ = (
        UniqueConstraint("flag_key", "version", name="uq_flag_config_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flag_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(512), default="")
    rules_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    variants_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DecisionSnapshot(Base):
    """一次判定请求的完整快照，可按 request_id 回查。"""

    __tablename__ = "decision_snapshots"
    __table_args__ = (
        # 列表审计：按 decided_at, request_id 倒序的键集分页主序索引
        Index("ix_snapshots_decided_at_request_id", "decided_at", "request_id"),
        # 常用等值过滤 + 倒序分页（indexed_column, decided_at, request_id 可由 SQLite 反向扫描）
        Index("ix_snapshots_flag_decided", "flag_key", "decided_at", "request_id"),
        Index("ix_snapshots_user_decided", "user_id", "decided_at", "request_id"),
        Index("ix_snapshots_variant_decided", "variant_key", "decided_at", "request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    flag_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_context_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_rule_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variant_key: Mapped[str] = mapped_column(String(128), nullable=False)
    explanation_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
