"""
Build a deployable workbench HTML with the latest index-l1.json + hotlist.json inlined.

Why: 远程 fetch 在 CloudStudio 静态托管下可能因 CORS/MIME/缓存/网络抖动失败，
     让用户打开页面看不到数据。把数据 inline 进 HTML，100% 可靠。

Usage:
    python scripts/build_workbench_deploy.py
产出: tmp/workbench-deploy/index.html (含 inline INDEX_L1_DATA + HOTLIST_DATA)
"""
import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_HTML = PROJECT_ROOT / "web" / "invest-workbench.html"
SRC_INDEX_L1 = PROJECT_ROOT / "web" / "data" / "index-l1.json"
SRC_HOTLIST = PROJECT_ROOT / "web" / "data" / "hotlist.json"
OUT_DIR = PROJECT_ROOT / "tmp" / "workbench-deploy"
OUT_HTML = OUT_DIR / "index.html"
OUT_DATA = OUT_DIR / "data" / "index-l1.json"
OUT_HOTLIST = OUT_DIR / "data" / "hotlist.json"


def main():
    if not SRC_HTML.exists():
        raise FileNotFoundError(f"missing {SRC_HTML}")
    if not SRC_INDEX_L1.exists():
        raise FileNotFoundError(f"missing {SRC_INDEX_L1}")
    if not SRC_HOTLIST.exists():
        raise FileNotFoundError(f"missing {SRC_HOTLIST}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "data").mkdir(parents=True, exist_ok=True)

    html = SRC_HTML.read_text(encoding="utf-8")
    l1_data = SRC_INDEX_L1.read_text(encoding="utf-8")
    hot_data = SRC_HOTLIST.read_text(encoding="utf-8")

    l1_parsed = json.loads(l1_data)
    hot_parsed = json.loads(hot_data)

    if not isinstance(l1_parsed, dict) or not isinstance(l1_parsed.get("groups"), list):
        raise ValueError("index-l1.json 格式异常：缺少 groups 数组")

    l1_trade_date = l1_parsed.get("trade_date", "")
    l1_group_count = len(l1_parsed["groups"])
    hot_trade_date = hot_parsed.get("trade_date", "")
    print(f"[build_workbench_deploy] inline 罗盘 {l1_group_count} 个品种, trade_date={l1_trade_date}")
    print(f"[build_workbench_deploy] inline 信号 trade_date={hot_trade_date}")

    inline_script = (
        f'<script>window.INDEX_L1_DATA = {l1_data};</script>\n'
        f'<script>window.HOTLIST_DATA = {hot_data};</script>\n'
    )

    marker = '<script>\n"use strict";\n/* ================= 常量与工具 ================= */'
    if marker not in html:
        raise RuntimeError("未找到注入锚点（'use strict' 顶部），HTML 结构可能已变更")

    new_html = html.replace(marker, inline_script + marker, 1)

    OUT_HTML.write_text(new_html, encoding="utf-8")
    shutil.copy2(SRC_INDEX_L1, OUT_DATA)
    shutil.copy2(SRC_HOTLIST, OUT_HOTLIST)

    print(f"[build_workbench_deploy] wrote {OUT_HTML} ({len(new_html)} bytes)")
    print(f"[build_workbench_deploy] copied {SRC_INDEX_L1} -> {OUT_DATA}")
    print(f"[build_workbench_deploy] copied {SRC_HOTLIST} -> {OUT_HOTLIST}")


if __name__ == "__main__":
    main()