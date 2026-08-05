# -*- coding: utf-8 -*-
"""项目配置管理。"""

import logging
import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH, override=False)


def _env_path(key: str, default: str) -> Path:
    raw = os.getenv(key, default)
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


# 路径配置
DSA_DB_PATH = _env_path("DSA_DB_PATH", "../../data/stock_analysis.db")
LOCAL_DB_PATH = _env_path("LOCAL_DB_PATH", "./data/trend_compass.db")
WEB_DATA_DIR = _env_path("WEB_DATA_DIR", "./web/data")
LOG_DIR = _env_path("LOG_DIR", "./data/logs")

# akshare 限速
AKSHARE_SLEEP_MIN = float(os.getenv("AKSHARE_SLEEP_MIN", "2.0"))
AKSHARE_SLEEP_MAX = float(os.getenv("AKSHARE_SLEEP_MAX", "5.0"))

# 个股日线同步并发数（东财源单请求 ~1.4s，8 并发补 5500 只缺口约 17 分钟）
STOCK_SYNC_MAX_WORKERS = int(os.getenv("STOCK_SYNC_MAX_WORKERS", "8"))

# 指数/行业/概念日线同步并发数（不同数据源站点，分组并行不叠加单站压力）
INDEX_SYNC_WORKERS = int(os.getenv("INDEX_SYNC_WORKERS", "2"))
INDUSTRY_DAILY_SYNC_WORKERS = int(os.getenv("INDUSTRY_DAILY_SYNC_WORKERS", "2"))
CONCEPT_SYNC_WORKERS = int(os.getenv("CONCEPT_SYNC_WORKERS", "3"))

# 行业映射批量查询并发数（首次重建品种树时使用）
INDUSTRY_SYNC_MAX_WORKERS = int(os.getenv("INDUSTRY_SYNC_MAX_WORKERS", "8"))

# 前端服务
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# RS 趋势箭头回顾天数
RS_TREND_LOOKBACK_DAYS: List[int] = [
    int(x.strip()) for x in os.getenv("RS_TREND_LOOKBACK_DAYS", "1,5").split(",") if x.strip().isdigit()
]

# 日期窗口
MIN_HISTORY_YEARS = 2  # 至少保留 2 年历史，用于计算 252 日动量


def ensure_dirs() -> None:
    """确保本地数据目录存在。"""
    for path in (LOCAL_DB_PATH.parent, WEB_DATA_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
        logger.debug("Ensured dir: %s", path)
