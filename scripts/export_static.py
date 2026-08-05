#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出静态 JSON 文件，供前端直接 fetch 渲染。"""

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.exporter import export_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导出静态 JSON")
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="输出目录，默认走 .env 的 WEB_DATA_DIR",
    )
    args = parser.parse_args()

    config.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    out_dir = Path(args.out) if args.out else config.WEB_DATA_DIR
    result = export_all(out_dir)
    print(
        f"[OK] 导出完成 trade_date={result.trade_date} "
        f"价格分片={result.price_chunks} 总字节={result.bytes_total:,} -> {out_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
