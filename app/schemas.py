"""请求 / 响应数据模型（Pydantic v2）。"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Operator = Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "prefix", "suffix", "contains"]


class RuleIn(BaseModel):
    """有序属性规则。priority 越小优先级越高。"""

    priority: int = Field(ge=1, description="优先级，数字越小越先判定")
    attribute: str = Field(min_length=1, description="要检查的用户属性名，如 region / age")
    operator: Operator = "eq"
    values: list[Any] = Field(default_factory=list, description="比较值；eq/ne/gt 等取第一个")
    variant_key: str = Field(min_length=1, description="命中后返回的变体 key")

    @field_validator("values")
    @classmethod
    def _values_not_empty(cls, v: list[Any]) -> list[Any]:
        if not v:
            raise ValueError("规则必须提供至少一个比较值 values")
        return v


class VariantIn(BaseModel):
    """加权变体。所有权重之和必须等于 100。"""

    key: str = Field(min_length=1)
    weight: int = Field(ge=0, le=100, description="桶权重，全部变体之和必须为 100")
    description: str = ""


class FlagConfigBody(BaseModel):
    """开关配置体（variants + rules 及配套校验）。

    创建 / 更新接口与发布前影响预览共用同一套候选配置校验。
    """

    variants: list[VariantIn] = Field(min_length=2, description="至少配置两个加权变体")
    rules: list[RuleIn] = Field(default_factory=list)

    @field_validator("variants")
    @classmethod
    def _weights_sum_to_100(cls, v: list[VariantIn]) -> list[VariantIn]:
        total = sum(item.weight for item in v)
        if total != 100:
            raise ValueError(f"变体权重之和必须为 100，当前为 {total}")
        keys = [item.key for item in v]
        if len(set(keys)) != len(keys):
            raise ValueError("变体 key 不允许重复")
        return v

    @model_validator(mode="after")
    def _rules_consistent(self) -> "FlagConfigBody":
        priorities = [r.priority for r in self.rules]
        if len(set(priorities)) != len(priorities):
            raise ValueError("规则 priority 不允许重复")
        variant_keys = {v.key for v in self.variants}
        for r in self.rules:
            if r.variant_key not in variant_keys:
                raise ValueError(f"规则(priority={r.priority})引用了不存在的变体 {r.variant_key!r}")
        return self


class FlagUpsertRequest(FlagConfigBody):
    flag_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    description: str = ""


class FlagResponse(BaseModel):
    flag_key: str
    description: str
    version: int
    variants: list[VariantIn]
    rules: list[RuleIn]
    created_at: Any
    updated_at: Any


class FlagVersionResponse(BaseModel):
    """某一历史版本的完整不可变配置。"""

    flag_key: str
    version: int
    description: str
    variants: list[VariantIn]
    rules: list[RuleIn]
    created_at: Any


class EvaluateRequest(BaseModel):
    flag_key: str | None = Field(default=None, max_length=128, description="路径式调用可省略")
    user_id: str = Field(min_length=1, max_length=128)
    context: dict[str, Any] = Field(
        default_factory=dict, description="用户属性，规则按此匹配，如 {\"region\": \"cn\", \"age\": 20}"
    )
    request_id: str | None = Field(default=None, max_length=64, description="可选，幂等用；缺省自动生成")
    config_version: int | None = Field(
        default=None, ge=1, description="可选，指定使用的历史配置版本；缺省使用最新版"
    )


class ExplanationStep(BaseModel):
    step: str
    detail: dict[str, Any] = Field(default_factory=dict)


class EvaluateResponse(BaseModel):
    request_id: str
    flag_key: str
    user_id: str
    config_version: int
    matched_rule: dict[str, Any] | None = Field(
        description="命中的规则；无规则命中时为 null"
    )
    bucket_value: int | None = Field(description="0-99 的稳定哈希分桶值；规则命中时为 null")
    variant_key: str
    decision_path: Literal["rule", "bucket"]
    explanation: list[ExplanationStep]


class SnapshotResponse(BaseModel):
    request_id: str
    flag_key: str
    user_id: str
    request_context: dict[str, Any]
    config_version: int
    bucket_value: int | None
    matched_rule_index: int | None
    variant_key: str
    explanation: list[ExplanationStep]
    decided_at: Any


class DecisionListResponse(BaseModel):
    """判定快照审计列表：不透明键集游标分页。

    ``items`` 字段结构与 SnapshotResponse 完全一致，按
    (decided_at DESC, request_id DESC) 排序；无更多数据时 ``next_cursor`` 为 null。
    """

    items: list[SnapshotResponse]
    next_cursor: str | None = Field(description="下一页不透明游标；末页为 null")


class PreviewUser(BaseModel):
    """发布前影响预览的单个评估对象。"""

    user_id: str = Field(min_length=1, max_length=128)
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="用户属性，规则按此匹配，如 {\"region\": \"cn\", \"age\": 20}",
    )


class PreviewImpactRequest(FlagConfigBody):
    """发布前影响预览请求：候选配置与 1～500 个互异 user_id。

    flag_key 取路径值，候选 variants/rules 的校验与创建接口完全一致。
    """

    users: list[PreviewUser] = Field(min_length=1, max_length=500)

    @field_validator("users")
    @classmethod
    def _user_ids_unique(cls, v: list[PreviewUser]) -> list[PreviewUser]:
        ids = [u.user_id for u in v]
        if len(set(ids)) != len(ids):
            raise ValueError("user_id 不允许重复")
        return v


class PreviewOutcome(BaseModel):
    """单个配置下的判定结果（只读判定，无快照与解释链）。"""

    variant_key: str
    decision_path: Literal["rule", "bucket"]
    matched_rule_index: int | None = Field(
        description="排序后命中的规则下标（从 0 开始）；无规则命中时为 null"
    )


class PreviewChange(BaseModel):
    """结果发生变化的单个用户（按请求顺序返回）。"""

    user_id: str
    current: PreviewOutcome
    candidate: PreviewOutcome


class PreviewImpactResponse(BaseModel):
    flag_key: str
    current_version: int = Field(description="当前线上配置版本；候选配置不会产生新版本")
    total_users: int = Field(description="输入用户数")
    changed_users: int = Field(description="结果发生变化的用户数")
    changes: list[PreviewChange]
    # {"<当前变体>": {"<候选变体>": 迁移人数, ...}, ...}，仅含非零计数
    migration_counts: dict[str, dict[str, int]]
