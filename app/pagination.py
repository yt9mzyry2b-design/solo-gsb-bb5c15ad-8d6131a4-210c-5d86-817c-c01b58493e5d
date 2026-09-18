"""判定快照审计列表的不透明游标。

游标设计目标（见 GET /api/v1/decisions）：

- **不透明**：客户端只能原样回传，不应解析或构造；线上格式为
  ``base64url(payload_json) + "." + checksum``，checksum 为规范 payload 的
  SHA-256 前 32 位（仅用于识别损坏 / 篡改，不提供加密意义上的认证）；
- **记录边界**：保存上一页最后一条的 ``(decided_at, request_id)``，
  配合 ``WHERE (decided_at, request_id) < (?, ?)`` 的键集分页，
  同一时间戳靠 request_id 决胜，天然不重不漏；
- **绑定过滤条件**：保存首次请求的全部过滤参数（flag_key / user_id /
  variant_key / decided_from / decided_to 的规范化形态），回传时与本次
  查询参数逐一比对，不一致即视为无效游标。

任何解码失败（结构不符、checksum 不符、字段缺失等）统一抛
``InvalidCursor``，由路由层转换为 422。
"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

CURSOR_VERSION = 1


class InvalidCursor(ValueError):
    """游标损坏、版本不支持或与本次过滤条件不匹配。"""


def normalize_dt(value: datetime | None) -> datetime | None:
    """把查询参数中的时间统一为**朴素 UTC**（与 SQLite 存储形态一致）。

    带时区的输入先换算到 UTC 再去掉 tzinfo；朴素时间按约定视为 UTC。
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@dataclass(frozen=True)
class DecisionFilters:
    """审计列表的组合过滤条件（游标只绑定这些字段，不含 limit）。"""

    flag_key: str | None = None
    user_id: str | None = None
    variant_key: str | None = None
    decided_from: datetime | None = None
    decided_to: datetime | None = None

    def canonical(self) -> dict[str, Any]:
        """供游标绑定 / 比较的规范化 JSON 形态。"""
        return {
            "flag_key": self.flag_key,
            "user_id": self.user_id,
            "variant_key": self.variant_key,
            "decided_from": self.decided_from.isoformat() if self.decided_from else None,
            "decided_to": self.decided_to.isoformat() if self.decided_to else None,
        }


def encode_cursor(decided_at: datetime, request_id: str, filters: DecisionFilters) -> str:
    """以页末行 (decided_at, request_id) 与本次过滤条件生成不透明游标。"""
    payload = {
        "v": CURSOR_VERSION,
        "ts": normalize_dt(decided_at).isoformat(),
        "rid": request_id,
        "f": filters.canonical(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body = base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=")
    checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{body.decode('ascii')}.{checksum}"


def decode_cursor(token: str, filters: DecisionFilters) -> tuple[datetime, str]:
    """校验并解码游标，返回边界 (decided_at, request_id)。

    游标内绑定的过滤条件必须与本次请求完全一致，否则抛 InvalidCursor。
    """
    try:
        body_b64, checksum = token.split(".", 1)
        padding = "=" * (-len(body_b64) % 4)
        raw = base64.urlsafe_b64decode(body_b64 + padding).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursor("游标格式损坏") from exc

    if hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] != checksum:
        raise InvalidCursor("游标校验失败")

    # 非对象 JSON（数组 / 数字 / null 等）在下标访问时抛 TypeError，统一视为损坏
    try:
        payload = json.loads(raw)
        version = payload["v"]
        ts_raw = payload["ts"]
        request_id = payload["rid"]
        bound = payload["f"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidCursor("游标内容不完整") from exc

    if not isinstance(bound, dict):
        raise InvalidCursor("游标内容结构损坏")
    if version != CURSOR_VERSION:
        raise InvalidCursor(f"不支持的游标版本 {version!r}")
    if bound != filters.canonical():
        raise InvalidCursor("游标绑定的过滤条件与本次请求不一致")

    try:
        boundary = datetime.fromisoformat(ts_raw)
    except (TypeError, ValueError) as exc:
        raise InvalidCursor("游标边界时间损坏") from exc
    if boundary.tzinfo is not None:
        boundary = boundary.replace(tzinfo=None)
    if not isinstance(request_id, str) or not request_id:
        raise InvalidCursor("游标边界 request_id 损坏")
    return boundary, request_id
