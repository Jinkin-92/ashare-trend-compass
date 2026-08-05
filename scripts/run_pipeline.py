#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日数据管道入口。"""

import sys
from pathlib import Path

# 将项目根目录加入 Python 路径
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import main

if __name__ == "__main__":
    sys.exit(main())
