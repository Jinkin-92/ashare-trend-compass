# -*- coding: utf-8 -*-
"""个股申万一级行业映射缓存（JSON），避免每次 sync_symbols 都全量请求外部接口。"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

CACHE_PATH: Path = PROJECT_ROOT / "data" / "industry_cache.json"
UNKNOWN_MARKERS = {"", "UNKNOWN", "IND_UNKNOWN", None}


def _ensure_dir() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_cache() -> dict:
    """加载行业缓存，返回 {code: industry_l1}。"""
    _ensure_dir()
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): (None if v in UNKNOWN_MARKERS else str(v)) for k, v in data.items()}
    except Exception as exc:
        logger.warning("加载行业缓存失败: %s", exc)
    return {}


def save_cache(cache: dict) -> None:
    """原子写入行业缓存。"""
    _ensure_dir()
    cache = {str(k): ("" if v in UNKNOWN_MARKERS else str(v)) for k, v in cache.items()}
    try:
        fd, tmp = tempfile.mkstemp(dir=CACHE_PATH.parent, suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_PATH)
    except Exception as exc:
        logger.warning("保存行业缓存失败: %s", exc)


def get_cached(cache: dict, code: str) -> Optional[str]:
    """从缓存读取，Unknown/空值统一返回 None。"""
    value = cache.get(str(code))
    if value in UNKNOWN_MARKERS:
        return None
    return value


def set_cached(cache: dict, code: str, industry: Optional[str]) -> None:
    """写入缓存，None 存为空字符串表示已查询但无结果。"""
    cache[str(code)] = "" if industry in UNKNOWN_MARKERS else str(industry)
