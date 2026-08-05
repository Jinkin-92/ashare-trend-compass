# -*- coding: utf-8 -*-
"""针对 IND_UNKNOWN 个股做第二轮申万行业回填（带重试，低并发）。"""

import logging
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LOCAL_DB_PATH
from src.data_source import AkShareFetcher
from src.industry_cache import load_cache, save_cache, set_cached

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def get_unknown_codes(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT symbol_id FROM symbols WHERE node_type='stock' AND parent_id='IND_UNKNOWN'"
    )
    codes = [r[0] for r in cur.fetchall()]
    conn.close()
    return codes


def main():
    db_path = str(LOCAL_DB_PATH)
    codes = get_unknown_codes(db_path)
    if not codes:
        logger.info("没有 IND_UNKNOWN 个股，无需回填")
        return

    logger.info("需回填行业的 IND_UNKNOWN 个股: %s 只", len(codes))
    fetcher = AkShareFetcher()
    cache = load_cache()

    workers = int(os.getenv("INDUSTRY_SYNC_MAX_WORKERS", "3"))
    sem = threading.Semaphore(workers)
    mcode = fetcher._get_cninfo_mcode()
    generated_at = time.time()
    MCODE_REFRESH_SECONDS = 600.0

    def refresh_mcode_if_needed():
        nonlocal mcode, generated_at
        if time.time() - generated_at > MCODE_REFRESH_SECONDS:
            logger.info("刷新 cninfo mcode...")
            mcode = fetcher._get_cninfo_mcode()
            generated_at = time.time()

    def fetch_one(code: str):
        with sem:
            refresh_mcode_if_needed()
            return code, fetcher._fetch_single_industry_direct(
                code, mcode, sleep_min=0.1, sleep_max=0.3, retries=2
            )

    mapped_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for i, (code, industry) in enumerate(executor.map(fetch_one, codes), start=1):
            set_cached(cache, code, industry)
            if industry:
                mapped_count += 1
            if i % 100 == 0:
                logger.info("回填进度: %s/%s 已映射 %s", i, len(codes), mapped_count)
                save_cache(cache)

    save_cache(cache)
    logger.info(
        "回填完成: 共 %s 只, 本次映射 %s 只, 剩余未知 %s 只",
        len(codes),
        mapped_count,
        len(codes) - mapped_count,
    )


if __name__ == "__main__":
    main()
