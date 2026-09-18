"""审计列表的不透明键集游标。

游标对客户端不透明：base64url(JSON) 负载 + HMAC-SHA256 签名，
记录上一页最后一条快照的排序边界（decided_at + request_id）、
本次查询的快照写入上界水印（自增 id，用于冻结翻页期间的新写入），
以及绑定的全部过滤条件。

- 翻页时过滤条件与游标不一致、时间区间倒置 -> 由路由层返回 422；
- 游标损坏（base64/JSON 解析失败、字段缺失、签名不符）-> 抛 InvalidCursor，
  由路由层统一转 422；
- 游标不可被构造或篡改：签名密钥仅存在于服务进程内。
"""

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime

CURSOR_VERSION = 1

# 进程内随机签名密钥：游标只在服务存活期内有效，重启后旧游标自然失效（按损坏处理 -> 422）。
# 不做跨进程持久化，避免向磁盘引入新密钥；多副本场景需改为共享密钥配置。
_SECRET = os.urandom(32)


class InvalidCursor(Exception):
    """游标损坏、被篡改或字段不完整。"""


@dataclass(frozen=True)
class CursorFilter:
    """游标绑定的本次过滤条件（规范化后的形态）。"""

    flag_key: str | None
    user_id: str | None
    variant_key: str | None
    decided_from: datetime | None
    decided_to: datetime | None


@dataclass(frozen=True)
class DecodedCursor:
    filters: CursorFilter
    # 键集边界：上一页最后一条（排序后最小）的 (decided_at, request_id)
    boundary_ts: datetime
    boundary_request_id: str
    # 上界水印：首页查询结果首行的自增 id；后续页只放行 id <= watermark_id 的行
    watermark_id: int


def _sign(payload_b64: str) -> str:
    mac = hmac.new(_SECRET, payload_b64.encode("ascii"), hashlib.sha256)
    return mac.hexdigest()


def encode_cursor(
    *,
    boundary_ts: datetime,
    boundary_request_id: str,
    watermark_id: int,
    filters: CursorFilter,
) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "ts": boundary_ts.isoformat(),
        "rid": boundary_request_id,
        "wm": watermark_id,
        "f": {
            "flag_key": filters.flag_key,
            "user_id": filters.user_id,
            "variant_key": filters.variant_key,
            "decided_from": filters.decided_from.isoformat() if filters.decided_from else None,
            "decided_to": filters.decided_to.isoformat() if filters.decided_to else None,
        },
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{payload_b64}.{_sign(payload_b64)}"


def decode_cursor(token: str) -> DecodedCursor:
    try:
        payload_b64, sig = token.split(".", 1)
    except (ValueError, AttributeError) as exc:
        raise InvalidCursor("游标格式错误") from exc

    if not hmac.compare_digest(sig, _sign(payload_b64)):
        raise InvalidCursor("游标签名校验失败")

    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:
        raise InvalidCursor("游标内容损坏") from exc

    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise InvalidCursor("游标版本不受支持")
    try:
        filters_raw = payload["f"]
        filters = CursorFilter(
            flag_key=filters_raw["flag_key"],
            user_id=filters_raw["user_id"],
            variant_key=filters_raw["variant_key"],
            decided_from=(
                datetime.fromisoformat(filters_raw["decided_from"])
                if filters_raw["decided_from"] is not None
                else None
            ),
            decided_to=(
                datetime.fromisoformat(filters_raw["decided_to"])
                if filters_raw["decided_to"] is not None
                else None
            ),
        )
        boundary_ts = datetime.fromisoformat(payload["ts"])
        boundary_request_id = payload["rid"]
        watermark_id = int(payload["wm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursor("游标字段缺失或非法") from exc

    if not isinstance(boundary_request_id, str) or not boundary_request_id:
        raise InvalidCursor("游标边界非法")
    if watermark_id < 1:
        raise InvalidCursor("游标水印非法")
    return DecodedCursor(
        filters=filters,
        boundary_ts=boundary_ts,
        boundary_request_id=boundary_request_id,
        watermark_id=watermark_id,
    )
