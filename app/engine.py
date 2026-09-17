"""功能开关判定引擎。

判定顺序：
1. 按 priority 升序逐条评估规则，返回首个命中规则的 variant_key；
   规则所需属性缺失（或类型不兼容）一律视为“未命中”并继续下一条。
2. 没有规则命中时，对 ``flag_key + ":" + user_id`` 做 SHA-256 稳定哈希，
   映射到 0-99 的桶，再按变体权重顺序切桶选变体。

同一用户在配置不变时结果稳定：哈希输入只含 flag_key 与 user_id。
"""

import hashlib
from dataclasses import dataclass
from typing import Any

# 桶空间固定为 100，与“权重总和必须为 100”对应
BUCKET_SPACE = 100


@dataclass(frozen=True)
class Rule:
    priority: int
    attribute: str
    operator: str
    values: list[Any]
    variant_key: str


@dataclass(frozen=True)
class Variant:
    key: str
    weight: int
    description: str = ""


class RuleEvaluationError(ValueError):
    """配置本身非法（如规则指向不存在的变体）。"""


def stable_bucket(flag_key: str, user_id: str) -> int:
    """返回 0..99 的稳定分桶值。

    SHA-256("flag_key:user_id") 大端取前 8 字节对 100 取模；
    仅依赖 flag_key 与 user_id，配置不变则结果不变。
    """
    raw = f"{flag_key}:{user_id}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") % BUCKET_SPACE


def _compare(actual: Any, operator: str, expected: list[Any]) -> tuple[bool, str]:
    """执行单个操作符比较，返回 (是否命中, 原因说明)。"""
    target = expected[0]

    if operator == "eq":
        return actual == target, f"{actual!r} {'==' if actual == target else '!='} {target!r}"
    if operator == "ne":
        return actual != target, f"{actual!r} {'!=' if actual != target else '=='} {target!r}"
    if operator == "in":
        return actual in expected, f"{actual!r} {'∈' if actual in expected else '∉'} {expected!r}"
    if operator == "not_in":
        return actual not in expected, f"{actual!r} {'∉' if actual not in expected else '∈'} {expected!r}"

    if operator in ("gt", "gte", "lt", "lte"):
        if isinstance(actual, bool) or isinstance(target, bool) or not _is_number(actual) or not _is_number(target):
            return False, f"数值比较要求两侧均为数字，得到 {type(actual).__name__} 与 {type(target).__name__}"
        if operator == "gt":
            return actual > target, f"{actual} > {target} => {actual > target}"
        if operator == "gte":
            return actual >= target, f"{actual} >= {target} => {actual >= target}"
        if operator == "lt":
            return actual < target, f"{actual} < {target} => {actual < target}"
        return actual <= target, f"{actual} <= {target} => {actual <= target}"

    if operator in ("prefix", "suffix", "contains"):
        if not isinstance(actual, str) or not isinstance(target, str):
            return False, f"{operator} 要求两侧均为字符串"
        if operator == "prefix":
            return actual.startswith(target), f"{actual!r} startswith {target!r} => {actual.startswith(target)}"
        if operator == "suffix":
            return actual.endswith(target), f"{actual!r} endswith {target!r} => {actual.endswith(target)}"
        return target in actual, f"{target!r} in {actual!r} => {target in actual}"

    return False, f"未知操作符 {operator}"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float))


def evaluate_rule(rule: Rule, context: dict[str, Any]) -> tuple[bool, str]:
    """评估单条规则。属性缺失或值为 None 一律视为未命中。"""
    if rule.attribute not in context or context[rule.attribute] is None:
        return False, f"缺少属性 {rule.attribute!r}，视为未命中"
    return _compare(context[rule.attribute], rule.operator, rule.values)


def pick_variant_by_weight(variants: list[Variant], bucket: int) -> tuple[str, tuple[int, int]]:
    """按 variants 顺序切 [0,100) 的桶区间，返回 (variant_key, 命中区间)。

    权重为 0 的变体不会被选中。
    """
    cursor = 0
    for v in variants:
        start, end = cursor, cursor + v.weight
        if start <= bucket < end:
            return v.key, (start, end)
        cursor = end
    # 理论不可达：权重已保证总和 100
    raise RuleEvaluationError(f"分桶 {bucket} 未落入任何变体区间，请检查权重配置")


@dataclass
class Decision:
    variant_key: str
    decision_path: str  # "rule" | "bucket"
    bucket_value: int | None
    matched_rule: Rule | None
    matched_rule_index: int | None  # 排序后从 0 开始
    explanation: list[dict[str, Any]]


def decide(
    flag_key: str,
    user_id: str,
    context: dict[str, Any],
    variants: list[Variant],
    rules: list[Rule],
) -> Decision:
    """完整判定流程并产出解释链。"""
    explanation: list[dict[str, Any]] = []
    variant_keys = {v.key for v in variants}

    ordered = sorted(rules, key=lambda r: r.priority)
    explanation.append(
        {
            "step": "rules_loaded",
            "detail": {
                "rule_count": len(ordered),
                "evaluation_order": [
                    {"rule_index": i, "priority": r.priority, "attribute": r.attribute}
                    for i, r in enumerate(ordered)
                ],
            },
        }
    )

    # 1) 有序规则判定
    for index, rule in enumerate(ordered):
        if rule.variant_key not in variant_keys:
            raise RuleEvaluationError(
                f"priority={rule.priority} 的规则引用了不存在的变体 {rule.variant_key!r}"
            )
        hit, reason = evaluate_rule(rule, context)
        explanation.append(
            {
                "step": "rule_evaluated",
                "detail": {
                    "rule_index": index,
                    "priority": rule.priority,
                    "attribute": rule.attribute,
                    "operator": rule.operator,
                    "values": rule.values,
                    "result": "hit" if hit else "miss",
                    "reason": reason,
                },
            }
        )
        if hit:
            explanation.append(
                {
                    "step": "rule_selected",
                    "detail": {
                        "rule_index": index,
                        "priority": rule.priority,
                        "variant_key": rule.variant_key,
                        "reason": "首个命中规则，跳过哈希分桶",
                    },
                }
            )
            return Decision(
                variant_key=rule.variant_key,
                decision_path="rule",
                bucket_value=None,
                matched_rule=rule,
                matched_rule_index=index,
                explanation=explanation,
            )

    # 2) 稳定哈希分桶
    explanation.append(
        {"step": "no_rule_matched", "detail": {"reason": "所有规则均未命中，进入哈希分桶"}}
    )
    bucket = stable_bucket(flag_key, user_id)
    explanation.append(
        {
            "step": "bucket_computed",
            "detail": {
                "hash_input": f"{flag_key}:{user_id}",
                "algorithm": "sha256[:8] % 100",
                "bucket_value": bucket,
            },
        }
    )

    variant_key, (start, end) = pick_variant_by_weight(variants, bucket)
    ranges = []
    cursor = 0
    for v in variants:
        ranges.append({"variant_key": v.key, "weight": v.weight, "range": [cursor, cursor + v.weight]})
        cursor += v.weight

    explanation.append(
        {
            "step": "variant_selected",
            "detail": {
                "bucket_value": bucket,
                "selected_variant": variant_key,
                "selected_range": [start, end],
                "variant_ranges": ranges,
            },
        }
    )

    return Decision(
        variant_key=variant_key,
        decision_path="bucket",
        bucket_value=bucket,
        matched_rule=None,
        matched_rule_index=None,
        explanation=explanation,
    )
