# 自动化执行记录：A股趋势罗盘 每日盘后数据更新

## 2026-08-05（首次执行，手动/测试触发于 12:26）

- **命令**：`python scripts/daily_update.py`（实际需用 `C:\Users\Jinkin\AppData\Local\Programs\Python\Python311\python.exe` 全路径）
- **结果**：三个阶段中阶段 1/2 成功，**阶段 3 首次运行因 `sqlite3 database is locked` 失败**，通过补跑导出完成闭环
- **关键问题与处理**：
  1. 默认 `python`（3.13.14，Git Bash PATH 上的版本）无项目依赖（缺 dotenv），import 即失败 → 改用系统 Python 3.11.9（装有 akshare/baostock/tinyshare/pandas/dotenv 全部依赖）
  2. 阶段 3 写指标时 `database is locked`：检测到 PID 157336（serve_web.py，常驻）与 PID 60256（另一并发 `run_indicator_update()` 进程，12:37 启动、12:47 退出）持有连接。等并发进程结束后，指标数据已由该进程完整写入（daily_indicator 最新 2026-08-04，6050 条）
  3. 补跑 `scripts/export_static.py`（1m21s，36.4MB，6078 品种指标 + 13 片价格分片 + 31 L1 + 123 L2）
  4. `verify.py` 端到端验证：全部 OK，退出码 0
- **数据状态**：daily_price / daily_indicator 最新交易日 2026-08-04（当日 8/5 数据盘后才出，属正常）
- **已知告警（非致命）**：概念指数同步约 20 个失败（同花顺 d.10jqka.com.cn 被本机代理掐断，ProxyError/SSLError，AGENTS.md 已记载此现象）；tinyshare 批量补缺报 `'<' not supported between instances of 'datetime.date' and 'float'` 后转 baostock 兜底成功
- **经验**：
  - 本自动化必须用 Python 3.11 全路径执行，不能用裸 `python`
  - 若 serve_web.py 或并发指标进程在跑，阶段 3 可能锁库；重试前先查 python 进程
  - 输出管道用了 `| tail`，日志实时性看 `data/logs/daily_update.log`

## 2026-08-05（17:00 定时执行，第二次）

- **命令**：`C:\Users\Jinkin\AppData\Local\Programs\Python\Python311\python.exe scripts/daily_update.py`（正确全路径，一次成功）
- **结果**：三阶段全部成功，总耗时 1419.7s（约 23.7 分钟），无锁库（仅 serve_web.py 常驻，未冲突）
  - 阶段 1：品种树 6084 条（东财列表被代理掐断降级新浪，已知现象）
  - 阶段 2：概念 100 条 + 个股 7882 条（tinyshare 5532 + baostock 兜底 357）
  - 阶段 3：指标 6079 品种（温度 99158 / RS 66007 / 右侧 99195）+ 导出 35.4MB，trade_date=**2026-08-05**（盘后数据已出）
- **验证**：`verify.py` 全部 OK 退出码 0；daily_price/daily_indicator 最新交易日 2026-08-05，覆盖 5611 品种
- **非致命告警**：同花顺概念源被代理掐断（SSLError）；baostock 拉取 WinError 10054 连接重置（自动重试补齐）
- **耗时分布**：概念同步约 10 分钟（375 个受 akshare 限速）、个股批量补缺约 3 分钟、指标计算约 9 分钟、导出约 1.5 分钟
