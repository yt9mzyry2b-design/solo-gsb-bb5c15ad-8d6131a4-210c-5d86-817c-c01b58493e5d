"""FastAPI 入口：功能开关判定服务。

路由：
- POST /api/v1/flags              创建/更新开关配置（已存在则 version+1，同事务保存不可变版本）
- GET  /api/v1/flags              列出全部开关
- GET  /api/v1/flags/{flag_key}   查看单个开关
- DELETE /api/v1/flags/{flag_key} 删除开关（版本历史一并删除）
- GET  /api/v1/flags/{flag_key}/versions            列出版本历史
- GET  /api/v1/flags/{flag_key}/versions/{version}  查看指定版本配置
- POST /api/v1/flags/{flag_key}/evaluate  判定（按路径中的 flag_key，可指定 config_version）
- POST /api/v1/flags/{flag_key}/preview-impact  发布前候选配置影响预览（只读，不落版本与快照）
- POST /api/v1/evaluate           判定（flag_key 在请求体中）
- GET  /api/v1/decisions/{request_id}     按 request_id 查询判定快照
- GET  /healthz                   健康检查
"""

import json
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crud
from app import engine as eng
from app.database import Base, engine, get_db
from app.engine import RuleEvaluationError, decide
from app.models import FlagConfig
from app.schemas import (
    EvaluateRequest,
    EvaluateResponse,
    FlagResponse,
    FlagUpsertRequest,
    FlagVersionResponse,
    PreviewChange,
    PreviewImpactRequest,
    PreviewImpactResponse,
    PreviewOutcome,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        crud.backfill_missing_versions(db)
    finally:
        db.close()
    _seed_demo_flags()
    yield


app = FastAPI(
    title="Feature Flag Decision API",
    version="1.0.0",
    description="有序属性规则 + 加权变体 + 稳定哈希分桶的功能开关判定服务",
    lifespan=lifespan,
)


def _seed_demo_flags() -> None:
    """首次启动时写入一个演示开关，已存在则不覆盖。"""
    db = next(get_db())
    try:
        if crud.get_flag_or_none(db, "new_checkout") is not None:
            return
        demo = FlagUpsertRequest(
            flag_key="new_checkout",
            description="新版结账页灰度：内部员工与白名单地区走规则，其余用户按 90/10 分桶",
            variants=[
                {"key": "on", "weight": 10, "description": "新版结账页"},
                {"key": "off", "weight": 90, "description": "旧版结账页"},
            ],
            rules=[
                {
                    "priority": 1,
                    "attribute": "employee",
                    "operator": "eq",
                    "values": [True],
                    "variant_key": "on",
                },
                {
                    "priority": 2,
                    "attribute": "region",
                    "operator": "in",
                    "values": ["cn-beta", "sg-beta"],
                    "variant_key": "on",
                },
            ],
        )
        crud.upsert_flag(db, demo)
    finally:
        db.close()


def _snapshot_to_response(snap, *, request_context: dict | None = None) -> dict:
    context = request_context if request_context is not None else json.loads(
        snap.request_context_json
    )
    return {
        "request_id": snap.request_id,
        "flag_key": snap.flag_key,
        "user_id": snap.user_id,
        "config_version": snap.config_version,
        "matched_rule": None,
        "bucket_value": snap.bucket_value,
        "variant_key": snap.variant_key,
        "decision_path": "rule" if snap.matched_rule_index is not None else "bucket",
        "explanation": json.loads(snap.explanation_json),
        # SnapshotResponse 扩展字段
        "request_context": context,
        "matched_rule_index": snap.matched_rule_index,
        "decided_at": snap.decided_at,
    }


def _replay_response(
    db: Session, snap, flag: FlagConfig, req: EvaluateRequest
) -> EvaluateResponse:
    """从快照重建判定响应（幂等重试场景）。

    matched_rule 详情优先取快照记录的 config_version 对应的历史版本配置，
    版本记录缺失（如开关被删除重建）时回退到当前配置。
    """
    body = _snapshot_to_response(snap, request_context=req.context)
    matched_rule = None
    if snap.matched_rule_index is not None:
        ver = crud.get_flag_version(db, snap.flag_key, snap.config_version)
        rules = crud.parse_rules(ver.rules_json if ver is not None else flag.rules_json)
        if 0 <= snap.matched_rule_index < len(rules):
            r = rules[snap.matched_rule_index]
            matched_rule = {
                "rule_index": snap.matched_rule_index,
                "priority": r.priority,
                "attribute": r.attribute,
                "operator": r.operator,
                "values": r.values,
                "variant_key": r.variant_key,
            }
    body["matched_rule"] = matched_rule
    allowed = EvaluateResponse.model_fields
    return EvaluateResponse(**{k: v for k, v in body.items() if k in allowed})


def _decision_response(
    request_id: str, flag: FlagConfig, req: EvaluateRequest, decision, config_version: int
) -> EvaluateResponse:
    matched_rule = None
    if decision.matched_rule is not None:
        r = decision.matched_rule
        matched_rule = {
            "rule_index": decision.matched_rule_index,
            "priority": r.priority,
            "attribute": r.attribute,
            "operator": r.operator,
            "values": r.values,
            "variant_key": r.variant_key,
        }
    return EvaluateResponse(
        request_id=request_id,
        flag_key=flag.flag_key,
        user_id=req.user_id,
        config_version=config_version,
        matched_rule=matched_rule,
        bucket_value=decision.bucket_value,
        variant_key=decision.variant_key,
        decision_path=decision.decision_path,
        explanation=decision.explanation,
    )


@app.exception_handler(RuleEvaluationError)
async def rule_error_handler(request: Request, exc: RuleEvaluationError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> dict:
    return {"status": "ok", "snapshots": crud.count_snapshots(db)}


@app.post("/api/v1/flags", response_model=FlagResponse, status_code=status.HTTP_201_CREATED)
def create_or_update_flag(payload: FlagUpsertRequest, db: Session = Depends(get_db)):
    try:
        flag = crud.upsert_flag(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return crud.flag_to_response(flag)


@app.get("/api/v1/flags", response_model=list[FlagResponse])
def list_flags(db: Session = Depends(get_db)):
    return [crud.flag_to_response(f) for f in crud.list_flags(db)]


@app.get("/api/v1/flags/{flag_key}", response_model=FlagResponse)
def get_flag(flag_key: str, db: Session = Depends(get_db)):
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    return crud.flag_to_response(flag)


@app.delete("/api/v1/flags/{flag_key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flag(flag_key: str, db: Session = Depends(get_db)) -> None:
    if not crud.delete_flag(db, flag_key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")


@app.get("/api/v1/flags/{flag_key}/versions", response_model=list[FlagVersionResponse])
def list_flag_versions(flag_key: str, db: Session = Depends(get_db)):
    if crud.get_flag_or_none(db, flag_key) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    return [crud.version_to_response(v) for v in crud.list_flag_versions(db, flag_key)]


@app.get("/api/v1/flags/{flag_key}/versions/{version}", response_model=FlagVersionResponse)
def get_flag_version(flag_key: str, version: int, db: Session = Depends(get_db)):
    if crud.get_flag_or_none(db, flag_key) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    ver = crud.get_flag_version(db, flag_key, version)
    if ver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"开关 {flag_key!r} 没有版本 {version}",
        )
    return crud.version_to_response(ver)


def _evaluate(req: EvaluateRequest, db: Session, flag_key: str) -> EvaluateResponse:
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在"
        )

    request_id = req.request_id or uuid.uuid4().hex

    # 幂等：相同 request_id 直接返回首次判定快照，保证重试结果一致
    existing = crud.get_snapshot(db, request_id)
    if existing is not None:
        if existing.flag_key != flag_key or existing.user_id != req.user_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"request_id={request_id} 已用于其他判定（flag_key/user_id 不一致）",
            )
        return _replay_response(db, existing, flag, req)

    # 解析本次判定使用的配置版本：缺省用最新版，指定时必须存在
    if req.config_version is None:
        rules_json, variants_json = flag.rules_json, flag.variants_json
        config_version = flag.version
    else:
        ver = crud.get_flag_version(db, flag_key, req.config_version)
        if ver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"开关 {flag_key!r} 没有版本 {req.config_version}",
            )
        rules_json, variants_json = ver.rules_json, ver.variants_json
        config_version = ver.version

    rules = crud.parse_rules(rules_json)
    variants = crud.parse_variants(variants_json)
    decision = decide(flag.flag_key, req.user_id, req.context, variants, rules)

    try:
        crud.save_snapshot(
            db,
            request_id=request_id,
            flag_key=flag.flag_key,
            config_version=config_version,
            user_id=req.user_id,
            context=req.context,
            decision=decision,
        )
    except IntegrityError:
        # 并发下 request_id 唯一键冲突：回退到已存在的快照
        db.rollback()
        existing = crud.get_snapshot(db, request_id)
        if existing is not None:
            return _replay_response(db, existing, flag, req)
        raise

    return _decision_response(request_id, flag, req, decision, config_version)


@app.post("/api/v1/evaluate", response_model=EvaluateResponse)
def evaluate_by_body(req: EvaluateRequest, db: Session = Depends(get_db)):
    if not req.flag_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须提供 flag_key"
        )
    return _evaluate(req, db, req.flag_key)


@app.post("/api/v1/flags/{flag_key}/evaluate", response_model=EvaluateResponse)
def evaluate_by_path(flag_key: str, req: EvaluateRequest, db: Session = Depends(get_db)):
    if req.flag_key is not None and req.flag_key != flag_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径 flag_key 与请求体 flag_key 不一致",
        )
    return _evaluate(req, db, flag_key)


@app.post(
    "/api/v1/flags/{flag_key}/preview-impact",
    response_model=PreviewImpactResponse,
)
def preview_impact(
    flag_key: str, req: PreviewImpactRequest, db: Session = Depends(get_db)
) -> PreviewImpactResponse:
    """发布前评估候选配置：当前版本与候选配置分别做只读判定并对比。

    - 候选 variants/rules 沿用创建接口的全部校验（Pydantic 层返回 422）；
    - 不写入配置版本，也不写入 DecisionSnapshot；
    - 仅返回 variant_key / decision_path / matched_rule_index 任一发生变化的用户，
      顺序与请求一致；汇总输入数、变化数及当前变体 -> 候选变体的迁移计数。
    """
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在"
        )

    current_rules = crud.parse_rules(flag.rules_json)
    current_variants = crud.parse_variants(flag.variants_json)

    # 候选配置仅存在于请求内存中；RuleIn/VariantIn 与创建接口同结构、同校验
    candidate_rules = [
        eng.Rule(**r.model_dump())
        for r in sorted(req.rules, key=lambda x: x.priority)
    ]
    candidate_variants = [eng.Variant(**v.model_dump()) for v in req.variants]

    changes: list[PreviewChange] = []
    migration: dict[str, dict[str, int]] = {}
    for user in req.users:
        current_decision = decide(
            flag_key, user.user_id, user.context, current_variants, current_rules
        )
        candidate_decision = decide(
            flag_key, user.user_id, user.context, candidate_variants, candidate_rules
        )

        current_outcome = PreviewOutcome(
            variant_key=current_decision.variant_key,
            decision_path=current_decision.decision_path,
            matched_rule_index=current_decision.matched_rule_index,
        )
        candidate_outcome = PreviewOutcome(
            variant_key=candidate_decision.variant_key,
            decision_path=candidate_decision.decision_path,
            matched_rule_index=candidate_decision.matched_rule_index,
        )

        if (
            current_outcome.variant_key != candidate_outcome.variant_key
            or current_outcome.decision_path != candidate_outcome.decision_path
            or current_outcome.matched_rule_index != candidate_outcome.matched_rule_index
        ):
            changes.append(
                PreviewChange(
                    user_id=user.user_id,
                    current=current_outcome,
                    candidate=candidate_outcome,
                )
            )
            # 仅变体迁移计入汇总；变体不变（仅判定路径 / 命中规则变化）不算迁移
            if current_outcome.variant_key != candidate_outcome.variant_key:
                migration.setdefault(current_outcome.variant_key, {})
                bucket = migration[current_outcome.variant_key]
                bucket[candidate_outcome.variant_key] = (
                    bucket.get(candidate_outcome.variant_key, 0) + 1
                )

    return PreviewImpactResponse(
        flag_key=flag_key,
        current_version=flag.version,
        total_users=len(req.users),
        changed_users=len(changes),
        changes=changes,
        migration_counts=migration,
    )


@app.get("/api/v1/decisions/{request_id}")
def get_decision(request_id: str, db: Session = Depends(get_db)):
    snap = crud.get_snapshot(db, request_id)
    if snap is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到 request_id={request_id} 的判定快照"
        )
    return _snapshot_to_response(snap)
