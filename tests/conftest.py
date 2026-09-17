"""pytest 夹具：使用临时文件 SQLite，隔离开发库。"""

import os
import tempfile

import pytest

# 必须在 import app.* 之前设置 DATABASE_URL
_tmp_dir = tempfile.mkdtemp(prefix="flag-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_tmp_dir, 'test_flags.db')}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import FlagConfig, FlagConfigVersion  # noqa: E402


@pytest.fixture(scope="session")
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # 不跑 lifespan 种子数据，测试自行准备配置
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_flags():
    """每个用例后清空开关与版本历史，保证 flag_key 可重复使用（快照保留）。"""
    yield
    db = SessionLocal()
    try:
        db.query(FlagConfigVersion).delete()
        db.query(FlagConfig).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def simple_flag(client):
    """两个 50/50 变体 + 一条规则。返回创建响应。"""
    payload = {
        "flag_key": "btn_color",
        "description": "按钮颜色实验",
        "variants": [
            {"key": "red", "weight": 50},
            {"key": "blue", "weight": 50},
        ],
        "rules": [
            {
                "priority": 1,
                "attribute": "region",
                "operator": "eq",
                "values": ["cn"],
                "variant_key": "red",
            }
        ],
    }
    resp = client.post("/api/v1/flags", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()
