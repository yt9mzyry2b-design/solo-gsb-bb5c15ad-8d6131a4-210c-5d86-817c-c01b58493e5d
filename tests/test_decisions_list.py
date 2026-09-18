"""GET /api/v1/decisions 判定快照审计列表测试。"""

from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import DecisionSnapshot

BASE = datetime(2026, 9, 17, 8, 0, 0)


def _insert_snapshot(
    *,
    request_id: str,
    flag_key: str = "audit_flag",
    user_id: str = "u1",
    variant_key: str = "red",
    decided_at: datetime,
    config_version: int = 1,
    bucket_value=None,
    matched_rule_index=None,
):
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


def _drain(client, **params):
    """从首页开始翻完，返回 (全部 request_id 顺序, 页数)。"""
    seen, pages, cursor = [], 0, None
    while True:
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        body = client.get("/api/v1/decisions", params=q).json()
        pages += 1
        seen.extend(i["request_id"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    return seen, pages


def test_empty_list(client):
    body = client.get(
        "/api/v1/decisions", params={"flag_key": "__no_such_flag__"}
    ).json()
    assert body == {"items": [], "next_cursor": None}


def test_default_ordering_desc_decided_at_then_request_id(client):
    fk = "order_flag"
    _insert_snapshot(request_id="a-1", flag_key=fk, decided_at=BASE)
    _insert_snapshot(request_id="a-2", flag_key=fk, decided_at=BASE + timedelta(seconds=10))
    _insert_snapshot(request_id="a-3", flag_key=fk, decided_at=BASE + timedelta(seconds=10))

    body = client.get("/api/v1/decisions", params={"flag_key": fk}).json()
    # 时间倒序；同时间戳按 request_id 倒序
    assert [i["request_id"] for i in body["items"]] == ["a-3", "a-2", "a-1"]
    assert body["next_cursor"] is None


def test_pagination_no_dup_no_gap_with_same_timestamp(client):
    fk = "page_flag"
    # 全部同一时间戳，强制以 request_id 做并列裁决
    ids = [f"r{i:03d}" for i in range(5)]
    for rid in ids:
        _insert_snapshot(request_id=rid, flag_key=fk, decided_at=BASE)

    seen, pages = _drain(client, flag_key=fk, limit=2)
    assert seen == sorted(ids, reverse=True)
    assert len(seen) == len(set(seen)) == 5
    assert pages == 3  # 2 + 2 + 1

    # 中间页均带游标，末页 next_cursor 为 null
    body = client.get("/api/v1/decisions", params={"flag_key": fk, "limit": 2}).json()
    assert body["next_cursor"] is not None
    last = client.get(
        "/api/v1/decisions",
        params={"flag_key": fk, "limit": 2, "cursor": body["next_cursor"]},
    ).json()
    last2 = client.get(
        "/api/v1/decisions",
        params={"flag_key": fk, "limit": 2, "cursor": last["next_cursor"]},
    ).json()
    assert last2["next_cursor"] is None


def test_limit_default_is_20_and_bounds(client):
    fk = "limit_flag"
    for i in range(21):
        _insert_snapshot(request_id=f"l{i:03d}", flag_key=fk, decided_at=BASE + timedelta(seconds=i))

    body = client.get("/api/v1/decisions", params={"flag_key": fk}).json()
    assert len(body["items"]) == 20
    assert body["next_cursor"] is not None

    assert client.get("/api/v1/decisions", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/decisions", params={"limit": 101}).status_code == 422

    body = client.get("/api/v1/decisions", params={"flag_key": fk, "limit": 100}).json()
    assert len(body["items"]) == 21
    assert body["next_cursor"] is None


def test_limit_may_change_between_pages(client):
    fk = "limit_change_flag"
    for i in range(5):
        _insert_snapshot(request_id=f"c{i:03d}", flag_key=fk, decided_at=BASE + timedelta(seconds=i))

    first = client.get("/api/v1/decisions", params={"flag_key": fk, "limit": 2}).json()
    assert [i["request_id"] for i in first["items"]] == ["c004", "c003"]
    second = client.get(
        "/api/v1/decisions", params={"flag_key": fk, "limit": 2, "cursor": first["next_cursor"]}
    ).json()
    # limit 不绑定游标：第三页可以换大页长
    third = client.get(
        "/api/v1/decisions",
        params={"flag_key": fk, "limit": 10, "cursor": second["next_cursor"]},
    ).json()
    ids = [i["request_id"] for i in first["items"]]
    ids += [i["request_id"] for i in second["items"]]
    ids += [i["request_id"] for i in third["items"]]
    assert ids == [f"c{i:03d}" for i in range(4, -1, -1)]
    assert third["next_cursor"] is None


def test_combination_filters(client):
    fk = "combo_flag"
    _insert_snapshot(request_id="m1", flag_key=fk, user_id="alice", variant_key="red", decided_at=BASE)
    _insert_snapshot(request_id="m2", flag_key=fk, user_id="alice", variant_key="blue", decided_at=BASE)
    _insert_snapshot(request_id="m3", flag_key=fk, user_id="bob", variant_key="red", decided_at=BASE)
    _insert_snapshot(request_id="m4", flag_key="other", user_id="alice", variant_key="red", decided_at=BASE)

    body = client.get(
        "/api/v1/decisions",
        params={"flag_key": fk, "user_id": "alice", "variant_key": "red"},
    ).json()
    assert [i["request_id"] for i in body["items"]] == ["m1"]


def test_decided_from_and_to_inclusive(client):
    fk = "range_flag"
    _insert_snapshot(request_id="t0", flag_key=fk, decided_at=BASE)
    _insert_snapshot(request_id="t1", flag_key=fk, decided_at=BASE + timedelta(seconds=1))
    _insert_snapshot(request_id="t2", flag_key=fk, decided_at=BASE + timedelta(seconds=2))

    body = client.get(
        "/api/v1/decisions",
        params={
            "flag_key": fk,
            "decided_from": (BASE + timedelta(seconds=1)).isoformat(),
            "decided_to": (BASE + timedelta(seconds=2)).isoformat(),
        },
    ).json()
    assert [i["request_id"] for i in body["items"]] == ["t2", "t1"]


def test_timezone_offset_equivalent_to_utc(client):
    fk = "tz_flag"
    _insert_snapshot(request_id="z1", flag_key=fk, decided_at=BASE)

    # 2026-09-17T09:00:00+01:00 == 2026-09-17T08:00:00Z，边界包含
    body = client.get(
        "/api/v1/decisions",
        params={"flag_key": fk, "decided_to": "2026-09-17T09:00:00+01:00"},
    ).json()
    assert [i["request_id"] for i in body["items"]] == ["z1"]


def test_inverted_range_returns_422(client):
    resp = client.get(
        "/api/v1/decisions",
        params={
            "decided_from": "2026-09-17T10:00:00Z",
            "decided_to": "2026-09-17T08:00:00Z",
        },
    )
    assert resp.status_code == 422
    assert "decided_from" in resp.text


def test_bad_datetime_returns_422(client):
    assert client.get("/api/v1/decisions", params={"decided_from": "not-a-date"}).status_code == 422


def test_corrupt_cursor_returns_422(client):
    for token in ["garbage", "abc.xyz", "", "!!!", "eyJ2IjogOX0.sig"]:
        resp = client.get("/api/v1/decisions", params={"cursor": token})
        assert resp.status_code == 422, token


def test_cursor_signed_payload_tamper_returns_422(client):
    fk = "tamper_flag"
    for i in range(3):
        _insert_snapshot(request_id=f"s{i}", flag_key=fk, decided_at=BASE + timedelta(seconds=i))
    cursor = client.get(
        "/api/v1/decisions", params={"flag_key": fk, "limit": 1}
    ).json()["next_cursor"]

    payload_b64, sig = cursor.split(".", 1)
    # 翻转负载中的字符但保留原签名 -> HMAC 校验失败
    tampered = ("A" if payload_b64[0] != "A" else "B") + payload_b64[1:] + "." + sig
    assert client.get("/api/v1/decisions", params={"cursor": tampered}).status_code == 422


def test_cursor_bound_to_filters_mismatch_returns_422(client):
    fk = "bind_flag"
    for i in range(3):
        _insert_snapshot(
            request_id=f"b{i}", flag_key=fk, user_id="alice", decided_at=BASE + timedelta(seconds=i)
        )
    cursor = client.get(
        "/api/v1/decisions", params={"flag_key": fk, "limit": 1}
    ).json()["next_cursor"]

    # 换 flag_key
    resp = client.get(
        "/api/v1/decisions", params={"flag_key": "other", "limit": 1, "cursor": cursor}
    )
    assert resp.status_code == 422
    assert "过滤条件不一致" in resp.text

    # 加 user_id
    resp = client.get(
        "/api/v1/decisions",
        params={"flag_key": fk, "user_id": "alice", "limit": 1, "cursor": cursor},
    )
    assert resp.status_code == 422

    # 加时间过滤
    resp = client.get(
        "/api/v1/decisions",
        params={"flag_key": fk, "limit": 1, "cursor": cursor, "decided_from": BASE.isoformat()},
    )
    assert resp.status_code == 422

    # 完全一致（含 limit 可不同）才放行
    ok = client.get("/api/v1/decisions", params={"flag_key": fk, "limit": 2, "cursor": cursor})
    assert ok.status_code == 200


def test_new_writes_during_paging_do_not_enter_later_pages(client):
    fk = "stable_window_flag"
    for i in range(4):
        _insert_snapshot(request_id=f"w{i}", flag_key=fk, decided_at=BASE + timedelta(seconds=i))

    first = client.get(
        "/api/v1/decisions", params={"flag_key": fk, "limit": 2}
    ).json()
    assert [i["request_id"] for i in first["items"]] == ["w3", "w2"]

    # 翻页期间新写入两条（含时间戳最新的）
    _insert_snapshot(request_id="w-new-1", flag_key=fk, decided_at=BASE + timedelta(seconds=10))
    _insert_snapshot(request_id="w-new-2", flag_key=fk, decided_at=BASE + timedelta(seconds=11))

    seen = [i["request_id"] for i in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        body = client.get(
            "/api/v1/decisions", params={"flag_key": fk, "limit": 2, "cursor": cursor}
        ).json()
        seen.extend(i["request_id"] for i in body["items"])
        cursor = body["next_cursor"]

    assert seen == ["w3", "w2", "w1", "w0"]
    assert not any("new" in rid for rid in seen)

    # 全新首页（不带游标）能看到新写入
    fresh = client.get(
        "/api/v1/decisions", params={"flag_key": fk, "limit": 100}
    ).json()
    fresh_ids = [i["request_id"] for i in fresh["items"]]
    assert "w-new-2" in fresh_ids and "w-new-1" in fresh_ids


def test_items_follow_snapshot_response_shape(client):
    fk = "shape_flag"
    _insert_snapshot(
        request_id="shape-1",
        flag_key=fk,
        user_id="u9",
        variant_key="blue",
        decided_at=BASE,
        bucket_value=42,
        matched_rule_index=None,
        config_version=3,
    )
    item = client.get("/api/v1/decisions", params={"flag_key": fk}).json()["items"][0]
    assert set(item) == {
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
    }
    assert item["request_context"] == {}
    assert item["explanation"] == []
    assert item["bucket_value"] == 42
    assert item["config_version"] == 3


def test_list_coexists_with_single_lookup(client):
    fk = "coexist_flag"
    _insert_snapshot(request_id="single-1", flag_key=fk, decided_at=BASE)

    listing = client.get("/api/v1/decisions", params={"flag_key": fk}).json()
    assert listing["items"][0]["request_id"] == "single-1"
    single = client.get("/api/v1/decisions/single-1")
    assert single.status_code == 200
    assert single.json()["request_id"] == "single-1"
