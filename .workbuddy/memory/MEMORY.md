# 项目长期记忆：A-Share Trend Compass

## 运行环境（重要）
- **必须使用 Python 3.11 全路径执行管道**：`C:\Users\Jinkin\AppData\Local\Programs\Python\Python311\python.exe`。
  裸 `python`（Git Bash PATH 上为 3.13.14）未装项目依赖（缺 dotenv/pandas/akshare），直接 import 失败。
  Python 3.11.9 装有全部依赖：akshare 1.18.64、baostock 0.9.2、pandas 3.0.3、SQLAlchemy 2.0.51、tinyshare 0.1036、python-dotenv 1.2.2。
- 项目根目录无 venv，无 npm 构建。

## 运行形态与注意事项
- 每日管道：`python scripts/daily_update.py`（品种树 → 日线增量 → 指标 → 导出），日志在 `data/logs/daily_update.log`。
- **数据库锁风险**：`serve_web.py`（PID 常驻，启动前端静态服务）和并发的 `run_indicator_update()` 进程会持有 trend_compass.db 写锁，管道阶段 3 可能报 `database is locked`。重试/排障前先查 python 进程（Get-CimInstance Win32_Process）。
- 本机有系统级 HTTP 代理：同花顺概念源（d.10jqka.com.cn）批量并发下会被掐断（ProxyError/SSLError），属已知现象，非致命；tinyshare 批量补缺偶发类型比较报错（datetime.date vs float），自动转 baostock 兜底。
- 盘前跑管道时当日数据未出，daily_price/indicator 停在最近已收盘交易日属正常。

## 质量门禁
- 导出后跑 `verify.py`（退出码 0 才算闭环）。
- harness 门禁：`scripts/harness/check_*.py` + `check_charts.mjs`，改动指标/前端逻辑后必跑。

## 数据现状锚点
- 2026-08-05：daily_price / daily_indicator 最新交易日 = 2026-08-04（6050+ 条指标/日）。
- 品种树 6436 条（含指数 7 + L1 31 + L2 123 + 概念 + 个股），导出 JSON 6078 品种。

## 温度算法校准历史
- 2026-07-29：初始阈值设定（沸≥50, 热≥25, 温≥3, 平(-19,3), 凉(-50,-19], 寒(-80,-50], 冻≤-80）
- 2026-08-03：校准基线 — 温度一致率 50%，±1档率 96.7%，RS 偏差 -8.0
- 2026-08-05：大幅调参 — 方向 MA 改为 MA5/MA10，ROC 增加(5,10)窗口，dir权重20→12，roc权重0.7→1.0，vol系数8→4，EMA span 3→2，CONFIRM_DAYS 2→1，阈值整体下移（沸≥35, 热≥10, 温≥-12, 平(-34,-12), 凉(-65,-34], 寒(-95,-65], 冻≤-95）。结果：一致率 49.0%，±1档率 87.8%，signed_mean +0.08
- 校准工具：`scripts/optimize_params.py`（网格搜索参数组合）
