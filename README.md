# A-Share Trend Compass（A股趋势罗盘）

本地部署的 A 股趋势交易决策辅助工具。

- **温度择时**：七档趋势温度（沸/热/温/平/凉/寒/冻），判断“什么时候动手”。
- **强度选筹**：按品种类型全局排名的相对强度（RS），判断“动哪个品种”。
- **右侧状态机**：滞后带设计，过滤温/热分界线附近的反复触发。

## 快速开始

```bash
# 1. 进入项目目录
cd "stock analysis/ashare-trend-compass"

# 2. 复制环境变量模板并编辑
cp .env.example .env

# 3. 首次运行：创建本地数据库
python scripts/run_pipeline.py --init-only

# 4. 每日盘后运行完整管道
python scripts/run_pipeline.py

# 5. 启动本地静态服务器查看前端
python scripts/serve_web.py
# 访问 http://127.0.0.1:8080
```

## 数据来源

- **个股日线**：只读复用 DSA（`daily_stock_analysis`）的 `data/stock_analysis.db` → `stock_daily` 表；缺失部分由 akshare 补全。
- **行业/概念/指数**：通过 akshare 直接获取。
- **指标结果**：独立存储在 `data/trend_compass.db`，不写入 DSA 数据库。

## 项目结构

```
.
├── docs/PRD_v1.0.md      # 产品需求文档
├── src/                  # Python 计算管道
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── data_source.py
│   ├── classification.py
│   ├── pipeline.py
│   └── indicators/
├── scripts/              # 运行入口
│   ├── run_pipeline.py
│   └── serve_web.py
├── web/                  # 静态前端
│   ├── index.html
│   ├── detail.html
│   ├── css/
│   ├── js/
│   └── data/             # 管道输出的 JSON
└── data/                 # 本地 SQLite + 日志
```

## 免责声明

本工具仅供个人研究与决策参考，不构成投资建议。据此做出的任何交易决策，风险自负。

---

## 常用脚本（按使用频率）

### 一次性全量补齐 stock 1 年日线（首次部署 / 拉新环境时跑）

```bash
# 拉全市场 5,533 只 A 股 stock 近 1 年日线（baostock 串行，可断点续拉）
# 预估耗时 1.5-2 小时，进程可中断后用 --resume 续跑
python scripts/backfill_stocks.py

# 续跑（从 progress.json 恢复）
python scripts/backfill_stocks.py --resume

# 测试用：只拉前 5 只
python scripts/backfill_stocks.py --max-stocks 5
```

**断点文件**：`data/backfill_progress.json`（每 100 只落盘一次）

**日志**：`data/logs/backfill_stocks.log`

### 每日盘后自动更新（部署到 Task Scheduler）

```bash
# 同步品种树 + 日线（仅今天 1 天）+ 指标 + 导出 JSON，一键完成
python scripts/daily_update.py
```

**推荐定时任务**：每个交易日 16:30 跑（盘后 30 分钟）

**Windows Task Scheduler 配置示例**：
- 程序：`python`
- 参数：`scripts/daily_update.py`
- 起始位置：项目根目录
- 触发器：每个工作日 16:30

### 手动跑（开发调试）

```bash
# 完整管道
python scripts/run_pipeline.py

# 只算指标
python -m src.pipeline --skip-symbols --skip-daily --calc-only

# 只导出 JSON
python scripts/export_static.py

# 数据库结构升级
python scripts/migrate_v1.py
```

## 三级页面

| 页面 | URL | 数据来源 |
|---|---|---|
| 一级页（指数 + 31 L1 行业） | `index.html` | `data/index-l1.json` |
| 二级页（L1 行业详情） | `l1.html?l1=SW_801890` | `data/l1-{id}.json` |
| 三级页（L2 行业详情） | `l2.html?l2=SW_801072` | `data/l2-{id}.json` |
| 个股详情 | `detail.html?symbol=000001` | `data/indicator-{id}.json` |

## 数据库结构（v1 迁移后）

- `symbols` 6,079 行：分类树（指数 / 申万 L1/L2 / 概念 / 个股）
  - 新增 `l2_industry_id`（申万二级行业，stock 节点）
  - 新增 `data_status`（ok / no_data）
- `daily_price` ~150 万行（backfill 后）：日线 OHLCV + pct_chg + adj_factor
- `daily_indicator` ~200 万行：温度 / RS / 右侧状态

## 已知数据缺口 / 后续工作

- L1 / L2 行业指数 akshare 7-10 当日数据未发布时，export fallback 到 7-09 指标
- L2 行业 → 个股映射未维护（点 L2 行业进 l2.html 显示跳 L1 提示）
- adj_factor 字段已加但暂未填充（待阶段 D）
