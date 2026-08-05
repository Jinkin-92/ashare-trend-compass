#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""启动本地静态服务器，供前端访问。"""

import http.server
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import WEB_HOST, WEB_PORT

WEB_ROOT = ROOT / "web"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def end_headers(self):
        # 本地数据每日盘后更新，禁止浏览器缓存旧 JSON/页面（用户曾因缓存看到过期指标）
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


if __name__ == "__main__":
    # ThreadingHTTPServer：单线程 TCPServer 会被浏览器的 keep-alive 长连接独占，导致后续请求挂起
    with http.server.ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler) as httpd:
        print(f"Serving A-Share Trend Compass at http://{WEB_HOST}:{WEB_PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")
