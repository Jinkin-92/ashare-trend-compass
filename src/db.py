# -*- coding: utf-8 -*-
"""本地 SQLite 数据库连接与表管理。"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from src.config import LOCAL_DB_PATH, ensure_dirs

logger = logging.getLogger(__name__)

Base = declarative_base()

# 确保目录存在后再创建引擎
ensure_dirs()

_engine = create_engine(
    f"sqlite:///{LOCAL_DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)

# 启用 WAL 模式，降低批量写入锁竞争
@event.listens_for(_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=5000")


SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """获取数据库会话上下文。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """创建所有表。"""
    # 延迟导入模型，避免循环依赖
    from src import models  # noqa: F401

    Base.metadata.create_all(bind=_engine)
    logger.info("Initialized local database: %s", LOCAL_DB_PATH)
