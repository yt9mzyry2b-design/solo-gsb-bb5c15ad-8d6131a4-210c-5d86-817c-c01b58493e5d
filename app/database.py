"""数据库连接与会话管理。"""

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB_PATH = os.path.join(_BASE_DIR, "data", "flags.db")

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_DEFAULT_DB_PATH}")

# SQLite 文件所在目录不存在时主动创建，避免首次连接失败
if DATABASE_URL.startswith("sqlite:///"):
    _db_file = DATABASE_URL.removeprefix("sqlite:///")
    if _db_file and _db_file != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(_db_file)), exist_ok=True)

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator:
    """FastAPI 依赖：每个请求一个数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
