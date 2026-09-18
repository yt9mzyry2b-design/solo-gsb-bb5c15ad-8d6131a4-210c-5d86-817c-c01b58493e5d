"""GET /api/v1/decisions 审计列表：过滤、不透明游标分页与一致性测试。"""

import base64
import hashlib
import json
from datetime import datetime

from app.database import SessionLocal, engine
from app.models import DecisionSnapshot


def _insert_snapshot(
    *,
    request_id,
    flag_key="audit_flag",
    user_id="uA",
    variant_key="red",
    decided_at,
    matched_rule_index=None,
    bucket_value=42,
    config_version=1,
):
    """直接落一条快照，精确控制 decided_at / request_id（决定分页顺序的字段）。"""
    db = SessionLocal()
    try:
        db.add(
            DecisionSnapshot(
                request_id=request_id,
                flag_key=flag_key,
                user_id=user_id,
                request_context_json="{}",
                config_version=config_version,
                bucket_value=bucket_value,
                matched_rule_index=matched_rule_index,
                variant_key=variant_key,
                explanation_json="[]",
                decided_at=decided_at,
            )
        )
        db.commit()
    finally:
        db.close()


def _walk_all(client, **params):
    """从首页一路翻到末尾，返回 (全部 request_id 顺序, 全部 decided_at 顺序)。"""
    rids, ts = [], []
    cursor = None
    while True:
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        body = client.get("/api/v1/decisions", params=q).json()
        rids.extend(i["request_id"] for i in body["items"])
        ts.extend(i["decided_at"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    return rids, ts


# ---------------------------------------------------------------------------
# 基础行为
# ---------------------------------------------------------------------------


def test_list_empty_result_shape(client):
    body = client.get(
        "/api/v1/decisions", params={"flag_key": "never-existed-flag"}
    ).json()
    assert body == {"items": [], "next_cursor": None}


def test_list_default_limit_is_20(client):
    for i in range(21):
        _insert_snapshot(
            request_id=f"dl-default-{i:02d}",
            flag_key="dl_default",
            decided_at=datetime(2030, 1, 1, 0, 0, i),
        )
    body = client.get("/api/v1/decisions", params={"flag_key": "dl_default"}).json()
    assert len(body["items"]) == 20
    assert body["next_cursor"] is not None


def test_list_limit_bounds(client):
    assert client.get("/api/v1/decisions", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/decisions", params={"limit": 101}).status_code == 422
    ok = client.get("/api/v1/decisions", params={"limit": 1})
    assert ok.status_code == 200
    assert len(ok.json()["items"]) <= 1


def test_list_item_shape_matches_snapshot_response(client, simple_flag):
    eval_body = client.post(
        "/api/v1/flags/btn_color/evaluate",
        json={"user_id": "shape-user", "context": {"region": "cn"}},
    ).json()
    body = client.get(
        "/api/v1/decisions", params={"flag_key": "btn_color", "limit": 100}
    ).json()
    item = next(i for i in body["items"] if i["request_id"] == eval_body["request_id"])
    single = client.get(f"/api/v1/decisions/{eval_body['request_id']}").json()
    # 列表项沿用 SnapshotResponse：其全部字段与单条查询响应中的同名字段一致
    snapshot_fields = (
        "request_id",
        "flag_key",
        "user_id",
        "request_context",
        "config_version",
        "bucket_value",
        "matched_rule_index",
        "variant_key",
        "explanation",
        "decided_at",
    )
    for key in snapshot_fields:
        assert key in item
        assert item[key] == single[key]
    assert set(item.keys()) == set(snapshot_fields)


# ---------------------------------------------------------------------------
# 排序与键集分页（不重不漏）
# ---------------------------------------------------------------------------


def test_list_orders_by_decided_at_then_request_id_desc(client):
    ts = datetime(2030, 2, 1, 10, 0, 0)
    for rid in ("r-a", "r-b", "r-c"):  # 同一时间戳，靠 request_id 倒序决胜
        _insert_snapshot(request_id=rid, flag_key="dl_tie", decided_at=ts)
    _insert_snapshot(
        request_id="r-d", flag_key="dl_tie", decided_at=datetime(2030, 2, 1, 11, 0, 0)
    )
    rids, _ = _walk_all(client, flag_key="dl_tie", limit=2)
    assert rids == ["r-d", "r-c", "r-b", "r-a"]


def test_list_pagination_no_duplicate_or_missing_under_same_timestamp(client):
    ts = datetime(2030, 3, 1, 8, 0, 0)
    for i in range(5):
        _insert_snapshot(
            request_id=f"same-ts-{i}", flag_key="dl_same_ts", decided_at=ts
        )
    rids, decided = _walk_all(client, flag_key="dl_same_ts", limit=2)
    # 5 条同时间戳数据，分页大小 2：2 / 2 / 1，不重不漏
    assert rids == [f"same-ts-{i}" for i in range(4, -1, -1)]
    assert len(set(rids)) == 5
    assert len(rids) == 5
    assert all(d == ts.isoformat() for d in decided)


def test_list_exact_multiple_of_limit_has_null_cursor(client):
    for i in range(4):
        _insert_snapshot(
            request_id=f"exact-{i}",
            flag_key="dl_exact",
            decided_at=datetime(2030, 4, 1, 0, 0, i),
        )
    body = client.get("/api/v1/decisions", params={"flag_key": "dl_exact", "limit": 4}).json()
    assert len(body["items"]) == 4
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# 组合过滤与时间区间
# ---------------------------------------------------------------------------


def test_list_combined_filters(client):
    _insert_snapshot(request_id="hit", flag_key="fA", user_id="uA", variant_key="blue",
                     decided_at=datetime(2030, 5, 1, 9, 0, 0))
    _insert_snapshot(request_id="other-flag", flag_key="fB", user_id="uA", variant_key="blue",
                     decided_at=datetime(2030, 5, 1, 9, 0, 0))
    _insert_snapshot(request_id="other-user", flag_key="fA", user_id="uB", variant_key="blue",
                     decided_at=datetime(2030, 5, 1, 9, 0, 0))
    _insert_snapshot(request_id="other-variant", flag_key="fA", user_id="uA", variant_key="red",
                     decided_at=datetime(2030, 5, 1, 9, 0, 0))
    rids, _ = _walk_all(
        client, flag_key="fA", user_id="uA", variant_key="blue", limit=10
    )
    assert rids == ["hit"]


def test_list_time_range_is_inclusive(client):
    start = datetime(2030, 6, 1, 12, 0, 0)
    _insert_snapshot(request_id="at-start", flag_key="dl_range", decided_at=start)
    _insert_snapshot(request_id="middle", flag_key="dl_range",
                     decided_at=datetime(2030, 6, 2, 0, 0, 0))
    _insert_snapshot(request_id="at-end", flag_key="dl_range",
                     decided_at=datetime(2030, 6, 3, 6, 0, 0))
    _insert_snapshot(request_id="outside", flag_key="dl_range",
                     decided_at=datetime(2030, 6, 4, 0, 0, 0))
    rids, _ = _walk_all(
        client,
        flag_key="dl_range",
        decided_from="2030-06-01T12:00:00",
        decided_to="2030-06-03T06:00:00",
        limit=2,
    )
    assert set(rids) == {"at-start", "middle", "at-end"}


def test_list_timezone_offsets_are_equivalent(client):
    # 12:00 UTC == 20:00 +08:00，闭区间边界必须命中
    _insert_snapshot(
        request_id="tz-boundary", flag_key="dl_tz",
        decided_at=datetime(2030, 7, 1, 12, 0, 0),
    )
    body = client.get(
        "/api/v1/decisions",
        params={"flag_key": "dl_tz", "decided_from": "2030-07-01T20:00:00+08:00"},
    ).json()
    assert [i["request_id"] for i in body["items"]] == ["tz-boundary"]

    # 同一过滤条件用不同时区拼写：规范化后一致，游标必须可以继续使用
    first = client.get(
        "/api/v1/decisions",
        params={"flag_key": "dl_tz", "decided_from": "2030-07-01T12:00:00+00:00", "limit": 1},
    ).json()
    assert first["next_cursor"] is None  # 仅一条，直接到底


# ---------------------------------------------------------------------------
# 游标错误处理
# ---------------------------------------------------------------------------


def test_list_corrupt_cursor_returns_422(client):
    for bad in ("garbage", "abc.def", "", "!!!.???"):
        resp = client.get("/api/v1/decisions", params={"cursor": bad})
        assert resp.status_code == 422, bad

    # 结构可解码但 checksum 被篡改
    payload = json.dumps(
        {"v": 1, "ts": "2030-01-01T00:00:00", "rid": "x", "f": {
            "flag_key": None, "user_id": None, "variant_key": None,
            "decided_from": None, "decided_to": None,
        }},
        sort_keys=True, separators=(",", ":"),
    )
    body = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    resp = client.get("/api/v1/decisions", params={"cursor": f"{body}.{'0' * 32}"})
    assert resp.status_code == 422

    # 不支持的游标版本
    payload = json.dumps(
        {"v": 99, "ts": "2030-01-01T00:00:00", "rid": "x", "f": {
            "flag_key": None, "user_id": None, "variant_key": None,
            "decided_from": None, "decided_to": None,
        }},
        sort_keys=True, separators=(",", ":"),
    )
    body = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    checksum = hashlib.sha256(payload.encode()).hexdigest()[:32]
    resp = client.get("/api/v1/decisions", params={"cursor": f"{body}.{checksum}"})
    assert resp.status_code == 422

    # checksum 正确但 payload 不是 JSON 对象 / 缺少字段：同样 422，而不是 500
    for raw_payload in ("[]", "123", '"x"', "null", "{}",
                        json.dumps({"v": 1, "ts": "bad", "rid": "x", "f": {}})):
        body = base64.urlsafe_b64encode(raw_payload.encode()).rstrip(b"=").decode()
        checksum = hashlib.sha256(raw_payload.encode()).hexdigest()[:32]
        resp = client.get("/api/v1/decisions", params={"cursor": f"{body}.{checksum}"})
        assert resp.status_code == 422, raw_payload


def test_list_cursor_is_opaque(client):
    # 暴露给客户端的只是不透明字符串，不含可读 JSON
    for i in range(3):
        _insert_snapshot(
            request_id=f"opaque-{i}",
            flag_key="dl_opaque",
            decided_at=datetime(2030, 8, 1, 0, 0, i),
        )
    body = client.get(
        "/api/v1/decisions", params={"flag_key": "dl_opaque", "limit": 2}
    ).json()
    cursor = body["next_cursor"]
    assert cursor is not None
    assert '"' not in cursor and "flag_key" not in cursor and "2030" not in cursor


def test_list_cursor_bound_to_filters_mismatch_422(client):
    for i in range(3):
        _insert_snapshot(
            request_id=f"bind-{i}",
            flag_key="dl_bind_a",
            user_id="uA",
            decided_at=datetime(2030, 9, 1, 0, 0, i),
        )
    first = client.get(
        "/api/v1/decisions", params={"flag_key": "dl_bind_a", "limit": 1}
    ).json()
    cursor = first["next_cursor"]

    # 换 flag_key / 加 user_id 过滤 / 加时间窗，均视为不一致 -> 422
    assert client.get(
        "/api/v1/decisions", params={"flag_key": "dl_bind_b", "cursor": cursor}
    ).status_code == 422
    assert client.get(
        "/api/v1/decisions",
        params={"flag_key": "dl_bind_a", "user_id": "uA", "cursor": cursor},
    ).status_code == 422
    assert client.get(
        "/api/v1/decisions",
        params={"flag_key": "dl_bind_a", "decided_from": "2030-01-01T00:00:00", "cursor": cursor},
    ).status_code == 422

    # 原样回传（同样的过滤条件）则正常
    ok = client.get(
        "/api/v1/decisions", params={"flag_key": "dl_bind_a", "limit": 1, "cursor": cursor}
    )
    assert ok.status_code == 200
    # limit 不属于过滤条件，游标不绑定 limit，换页大小仍可用
    ok2 = client.get(
        "/api/v1/decisions", params={"flag_key": "dl_bind_a", "limit": 2, "cursor": cursor}
    )
    assert ok2.status_code == 200


def test_list_inverted_time_range_422(client):
    resp = client.get(
        "/api/v1/decisions",
        params={"decided_from": "2030-12-31T00:00:00", "decided_to": "2030-01-01T00:00:00"},
    )
    assert resp.status_code == 422
    assert "decided_from" in resp.text


def test_list_invalid_datetime_422(client):
    resp = client.get("/api/v1/decisions", params={"decided_from": "not-a-date"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 翻页期间的快照稳定性
# ---------------------------------------------------------------------------


def test_list_new_writes_during_pagination_do_not_enter_later_pages(client):
    base = datetime(2031, 1, 1, 0, 0, 0)
    for i in range(3):
        _insert_snapshot(
            request_id=f"stable-{i}",
            flag_key="dl_stable",
            decided_at=base.replace(second=i),
        )

    first = client.get(
        "/api/v1/decisions", params={"flag_key": "dl_stable", "limit": 2}
    ).json()
    assert [i["request_id"] for i in first["items"]] == ["stable-2", "stable-1"]
    cursor = first["next_cursor"]

    # 翻页期间新写入一条更晚的快照：不得进入后续页
    _insert_snapshot(
        request_id="stable-late",
        flag_key="dl_stable",
        decided_at=datetime(2032, 1, 1),
    )
    second = client.get(
        "/api/v1/decisions",
        params={"flag_key": "dl_stable", "limit": 2, "cursor": cursor},
    ).json()
    assert [i["request_id"] for i in second["items"]] == ["stable-0"]
    assert second["next_cursor"] is None

    # 从头开始的新查询（无游标）才能看到新写入
    fresh = client.get(
        "/api/v1/decisions", params={"flag_key": "dl_stable", "limit": 10}
    ).json()
    assert fresh["items"][0]["request_id"] == "stable-late"


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------


def test_list_snapshot_indexes_exist(client):
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    for expected in (
        "ix_snapshots_decided_at_request_id",
        "ix_snapshots_flag_decided",
        "ix_snapshots_user_decided",
        "ix_snapshots_variant_decided",
    ):
        assert expected in names, expected
