"""数据读写与配置校验。"""

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import engine as eng
from app.models import DecisionSnapshot, FlagConfig, FlagConfigVersion
from app.schemas import FlagUpsertRequest, RuleIn, VariantIn


class NotFound(Exception):
    """开关不存在。"""


def parse_rules(raw: str) -> list[eng.Rule]:
    return [eng.Rule(**item) for item in json.loads(raw or "[]")]


def parse_variants(raw: str) -> list[eng.Variant]:
    return [eng.Variant(**item) for item in json.loads(raw or "[]")]


def upsert_flag(db: Session, payload: FlagUpsertRequest) -> FlagConfig:
    """创建开关；已存在时版本号 +1 并整体覆盖规则/变体。

    同一事务中向 flag_config_versions 追加一份完整不可变快照。
    """
    rules_json = json.dumps(
        [r.model_dump() for r in sorted(payload.rules, key=lambda x: x.priority)],
        ensure_ascii=False,
    )
    variants_json = json.dumps([v.model_dump() for v in payload.variants], ensure_ascii=False)

    flag = db.scalar(select(FlagConfig).where(FlagConfig.flag_key == payload.flag_key))
    if flag is None:
        flag = FlagConfig(
            flag_key=payload.flag_key,
            description=payload.description,
            rules_json=rules_json,
            variants_json=variants_json,
            version=1,
        )
        db.add(flag)
    else:
        flag.description = payload.description
        flag.rules_json = rules_json
        flag.variants_json = variants_json
        flag.version += 1
    db.add(
        FlagConfigVersion(
            flag_key=payload.flag_key,
            version=flag.version,
            description=payload.description,
            rules_json=rules_json,
            variants_json=variants_json,
        )
    )
    db.commit()
    db.refresh(flag)
    return flag


def get_flag_or_none(db: Session, flag_key: str) -> FlagConfig | None:
    return db.scalar(select(FlagConfig).where(FlagConfig.flag_key == flag_key))


def list_flags(db: Session) -> Sequence[FlagConfig]:
    return db.scalars(select(FlagConfig).order_by(FlagConfig.flag_key)).all()


def delete_flag(db: Session, flag_key: str) -> bool:
    flag = get_flag_or_none(db, flag_key)
    if flag is None:
        return False
    # 版本历史随开关一起删除，避免删除后重建同 key 开关时版本号冲突
    db.execute(delete(FlagConfigVersion).where(FlagConfigVersion.flag_key == flag_key))
    db.delete(flag)
    db.commit()
    return True


def list_flag_versions(db: Session, flag_key: str) -> Sequence[FlagConfigVersion]:
    return db.scalars(
        select(FlagConfigVersion)
        .where(FlagConfigVersion.flag_key == flag_key)
        .order_by(FlagConfigVersion.version)
    ).all()


def get_flag_version(db: Session, flag_key: str, version: int) -> FlagConfigVersion | None:
    return db.scalar(
        select(FlagConfigVersion).where(
            FlagConfigVersion.flag_key == flag_key,
            FlagConfigVersion.version == version,
        )
    )


def backfill_missing_versions(db: Session) -> int:
    """为缺少版本记录的开关补齐当前版本快照（兼容本功能上线前已有的数据）。"""
    added = 0
    for flag in list_flags(db):
        if get_flag_version(db, flag.flag_key, flag.version) is None:
            db.add(
                FlagConfigVersion(
                    flag_key=flag.flag_key,
                    version=flag.version,
                    description=flag.description,
                    rules_json=flag.rules_json,
                    variants_json=flag.variants_json,
                    created_at=flag.updated_at,
                )
            )
            added += 1
    if added:
        db.commit()
    return added


def flag_to_response(flag: FlagConfig) -> dict:
    rules = [RuleIn(**item) for item in json.loads(flag.rules_json)]
    variants = [VariantIn(**item) for item in json.loads(flag.variants_json)]
    return {
        "flag_key": flag.flag_key,
        "description": flag.description,
        "version": flag.version,
        "variants": variants,
        "rules": rules,
        "created_at": flag.created_at,
        "updated_at": flag.updated_at,
    }


def version_to_response(ver: FlagConfigVersion) -> dict:
    rules = [RuleIn(**item) for item in json.loads(ver.rules_json)]
    variants = [VariantIn(**item) for item in json.loads(ver.variants_json)]
    return {
        "flag_key": ver.flag_key,
        "version": ver.version,
        "description": ver.description,
        "variants": variants,
        "rules": rules,
        "created_at": ver.created_at,
    }


def save_snapshot(
    db: Session,
    *,
    request_id: str,
    flag_key: str,
    config_version: int,
    user_id: str,
    context: dict,
    decision: eng.Decision,
) -> DecisionSnapshot:
    snapshot = DecisionSnapshot(
        request_id=request_id,
        flag_key=flag_key,
        user_id=user_id,
        request_context_json=json.dumps(context, ensure_ascii=False, sort_keys=True),
        config_version=config_version,
        bucket_value=decision.bucket_value,
        matched_rule_index=decision.matched_rule_index,
        variant_key=decision.variant_key,
        explanation_json=json.dumps(decision.explanation, ensure_ascii=False),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def get_snapshot(db: Session, request_id: str) -> DecisionSnapshot | None:
    return db.scalar(select(DecisionSnapshot).where(DecisionSnapshot.request_id == request_id))


def count_snapshots(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(DecisionSnapshot)) or 0


def max_snapshot_id(db: Session) -> int | None:
    """返回当前最大自增 id（首页发起时的高水位）；空表返回 None。"""
    return db.scalar(select(func.max(DecisionSnapshot.id)))


def ensure_decision_audit_indexes(db: Session) -> None:
    """为已存在的 decision_snapshots 表幂等补齐审计列表索引。

    ``Base.metadata.create_all`` 只建新表、不会向老表追加新索引，
    因此升级启动时显式执行 CREATE INDEX IF NOT EXISTS（SQLite 与
    PostgreSQL 9.5+ 均支持），缺失才创建，已存在则为空操作。
    """
    from sqlalchemy import text

    statements = (
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_decided_at_request_id "
        "ON decision_snapshots (decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_flag_decided "
        "ON decision_snapshots (flag_key, decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_user_decided "
        "ON decision_snapshots (user_id, decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_variant_decided "
        "ON decision_snapshots (variant_key, decided_at, request_id)",
        "CREATE INDEX IF NOT EXISTS ix_decision_snapshots_flag_user_decided "
        "ON decision_snapshots (flag_key, user_id, decided_at, request_id)",
    )
    for ddl in statements:
        db.execute(text(ddl))
    db.commit()


def list_snapshots(
    db: Session,
    *,
    flag_key: str | None = None,
    user_id: str | None = None,
    variant_key: str | None = None,
    decided_from: datetime | None = None,
    decided_to: datetime | None = None,
    limit: int = 20,
    # 键集边界：严格位于 (decided_at < boundary_ts) 或
    # (decided_at == boundary_ts AND request_id < boundary_request_id)
    boundary_ts: datetime | None = None,
    boundary_request_id: str | None = None,
    # 上界高水位（首页请求发起时全表最大自增 id）：翻页期间新写入
    # 快照的 id 更大，一律被 id <= max_id 排除，保证翻页窗口不漂移、不重不漏。
    max_id: int | None = None,
) -> Sequence[DecisionSnapshot]:
    """按 (decided_at DESC, request_id DESC) 键集分页查询判定快照。

    多取 1 行（limit + 1）用于判断是否还有下一页；同一 decided_at 用
    request_id 做唯一并列裁决，保证同一时间戳不重不漏。
    """
    stmt = select(DecisionSnapshot)

    if flag_key is not None:
        stmt = stmt.where(DecisionSnapshot.flag_key == flag_key)
    if user_id is not None:
        stmt = stmt.where(DecisionSnapshot.user_id == user_id)
    if variant_key is not None:
        stmt = stmt.where(DecisionSnapshot.variant_key == variant_key)
    if decided_from is not None:
        stmt = stmt.where(DecisionSnapshot.decided_at >= decided_from)
    if decided_to is not None:
        stmt = stmt.where(DecisionSnapshot.decided_at <= decided_to)

    if boundary_ts is not None:
        # 必须携带成对的 request_id（游标保证）
        stmt = stmt.where(
            (DecisionSnapshot.decided_at < boundary_ts)
            | (
                (DecisionSnapshot.decided_at == boundary_ts)
                & (DecisionSnapshot.request_id < boundary_request_id)
            )
        )

    if max_id is not None:
        stmt = stmt.where(DecisionSnapshot.id <= max_id)

    stmt = stmt.order_by(
        DecisionSnapshot.decided_at.desc(), DecisionSnapshot.request_id.desc()
    ).limit(limit + 1)
    return db.scalars(stmt).all()
