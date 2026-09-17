"""数据读写与配置校验。"""

import json
from collections.abc import Sequence

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
