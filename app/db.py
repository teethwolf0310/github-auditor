"""数据库 session 工厂。"""
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .models import Base

_engine = None
_SessionLocal = None


# 在 CIFS 上锁易卡，打开 WAL + busy_timeout + 外键
@event.listens_for(__import__("sqlalchemy.engine", fromlist=["Engine"]).Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _init_engine(cfg: Config):
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine, _SessionLocal
    db_path = cfg.sqlite_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.raw_dir().mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path}"
    _engine = create_engine(
        url, echo=False, future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
    return _engine, _SessionLocal


def get_session(cfg: Config) -> Session:
    _, S = _init_engine(cfg)
    return S()
