# A-Share Trend Compass - Agent 协作规则

> 本文件面向 AI 编码 Agent，读完即可在不熟悉项目的情况下安全地工作。
> 项目注释、文档、日志均为中文，交付说明也用中文。

## 1. 项目定位

- 位于 `d:\code\dsa\stock analysis\ashare-trend-compass\`。
- 与 `daily_stock_analysis/`（DSA）是兄弟项目，**只读复用 DSA 数据，不修改 DSA 源码/配置/数据库**。
- 目标：本地部署的 A 股趋势交易辅助工具，三大核心：
  - **温度择时**：七档趋势温度（沸/热/温/平/凉/寒/冻），由趋势方向（连续偏离度）+ 多周期动量 + 波动扩张三维合成，按数据长度自适应选周期；分档走状态机——只能相邻档转移，沸/冻有 3 个交易日缓冲期（见 `src/indicators/temperature.py` 顶部 docstring；2026-07-29 按趋势动物参考数据校准过阈值与方向分，记录见 `docs/calibration/2026-07-29.md`）。
  - **强度选筹**：按品种类型全局排名的相对强度（RS 1-99 分位）。窗口口径 0.4·r10+0.3·r21+0.2·r63+0.1·r126（2026-07-29 校准的快窗口，替代旧 IBD 63/126/189/252）。
  - **右侧状态机**：进入需「热」「沸」；进入后「温」及以上维持并累计天数，回落到「平」及以下退出清零；天数一律按**交易日**计（规则源自 `docs/jinkin-philosophy.txt`，实现见 `src/indicators/right_side.py`）。
- 纯本地工具：无 Web 框架、无构建步骤，前端是静态 HTML + ECharts，数据靠管道每日盘后产出 JSON。

## 2. 技术栈与运行时架构

- **语言**：Python 3.10+（pyproject target: py310/py311/py312），依赖见 `requirements.txt`：`pandas` / `numpy` / `sqlalchemy>=2.0` / `akshare` / `tenacity` / `requests` / `python-dotenv` / `exchange-calendars` / `tinyshare` / `baostock`。
  - `tinyshare`（需 `TINYSHARE_TOKEN`）已进入主链路：`daily_sync` 个股补缺走 tinyshare 全市场按日批量（`pro.daily`+`pro.adj_factor` 前复权化）；申万行业指数日线在 akshare `index_hist_sw` 滞后/失败时由 `data_source._get_industry_index_daily_tinyshare`（`pro.sw_daily`，数值与官网逐日一致已核对）兜底；`scripts/build_stock_l2_mapping_sw.py` 用 akshare `index_component_sw` 重建 L2→个股挂载；`baostock` 是个股日线兜底源（TCP 直连，不受本机 HTTP 代理影响）。
  - 注意：本机有系统级 HTTP 代理，东财/新浪 HTTP 接口在批量并发下会被代理掐断（2026-07 实测），因此东财/新浪只作小流量源，批量补缺一律 tinyshare 优先、baostock 兜底。
- **存储**：SQLite × 2。
  - `data/stock_analysis.db`（DSA 主库，`.env` 的 `DSA_DB_PATH`）：**只读**，个股日线来源。
  - `data/trend_compass.db`（`LOCAL_DB_PATH`）：本项目的全部写入目标（品种树 / 日线 / 指标）。连接走 `src/db.py`，已启用 WAL + busy_timeout。
- **前端**：`web/` 静态站点（HTML/CSS/原生 JS + ECharts），直接 `fetch` `web/data/*.json`，无打包器、无 npm。
- **运行形态**：每日盘后跑一次批处理管道 → SQLite → 导出静态 JSON → 静态服务器查看。

## 3. 数据流（关键路径）

```
akshare / baostock / sina / tinyshare / DSA DB(只读)
        │  src/data_source.py（AkShareFetcher + DSAReader，带限速与行业缓存）
        ▼
src/classification.py   品种分类树（指数 / 申万 L1 / L2 / 概念 / 个股）→ symbols 表
src/daily_sync.py       日线增量同步 → daily_price 表
src/indicators/engine.py 温度（全量算、回写最近 21 自然日窗口 + 按价格改写断点前推）/ RS（回看 260 自然日）/ 右侧状态（按品种种子状态续算，种子随温度重写窗口回滚）→ daily_indicator 表
src/exporter.py         导出静态 JSON → web/data/
```

核心表（`src/models.py`）：

- `symbols`：分类树。`node_type` 区分节点类型；个股有 `l2_industry_id`、`data_status`（ok / no_data）。
- `daily_price`：OHLCV + `pct_chg` + `adj_factor`（字段已建，暂未填充）。
- `daily_indicator`：温度（含 score）、RS（含 1/5 日前排名与趋势箭头）、右侧状态（`is_right_side` / `right_side_days` / 入场温度）。右侧三列**无默认值，NULL 表示尚未计算**——温度/RS 写入时不填，由右侧状态机显式写入；增量续算靠 `IS NOT NULL` 识别种子行，不要给这三列加 default。

前端页面与 JSON 的对应关系：

| 页面 | URL | 数据 |
|---|---|---|
| 一级页（指数 + 31 个 L1 行业） | `index.html` | `web/data/index-l1.json` |
| 二级页（L1 详情） | `l1.html?l1=SW_801890` | `web/data/l1-{id}.json` |
| 三级页（L2 详情） | `l2.html?l2=SW_801072` | `web/data/l2-{id}.json` |
| 个股详情 | `detail.html?symbol=000001` | `web/data/indicators/indicator-{id}.json` |

## 4. 常用命令

```bash
# 环境准备
cp .env.example .env          # 然后按需编辑路径
pip install -r requirements.txt

# 初始化数据库表（管道/建表改动后用它验证）
python scripts/run_pipeline.py --init-only

# 每日盘后一键更新（品种树 + 增量日线 + 指标 + 导出 JSON）
python scripts/daily_update.py

# 开发调试：分步手动跑
python scripts/run_pipeline.py                                   # 完整管道
python -m src.pipeline --skip-symbols --skip-daily --calc-only   # 只算指标
python scripts/export_static.py                                  # 只导出 JSON

# 首次部署：全量补齐 1 年日线（1.5-2 小时，断点见 data/backfill_progress.json）
python scripts/backfill_stocks.py --resume

# 启动前端
python scripts/serve_web.py      # http://127.0.0.1:8080（端口走 .env WEB_PORT）
# 或双击 启动前端.bat（固定端口 8765）

# 端到端验证：检查所有 JSON 端点 + 模拟前端渲染（含 NaN/Infinity 泄漏检查）
python verify.py

# 单元测试
python -m pytest tests/

# 代码风格（配置在 pyproject.toml，Black 行宽 120，isort profile=black）
black . && isort .
```

部署：Windows Task Scheduler 每个交易日 16:30 跑 `python scripts/daily_update.py`（起始位置 = 项目根目录）。

## 5. 目录边界

- `src/`：计算管道。`config.py`（.env 集中配置）→ `db.py` / `models.py`（ORM）→ `data_source.py`（多源抓取 + 限速）→ `classification.py` / `daily_sync.py` → `indicators/`（temperature / relative_strength / right_side / engine）→ `exporter.py`。
- `src/industry_cache.py`：行业映射磁盘缓存（`data/industry_cache.json`），减少 akshare 调用。
- `scripts/`：CLI 入口与运维脚本。`run_pipeline.py` / `daily_update.py` / `serve_web.py` / `export_static.py` 是日常入口；`backfill_*` / `fill_*` / `fix_*` / `migrate_v1.py` 多为一次性修复脚本，改前先读 docstring，别当常驻流程动。
- `scripts/harness/`：质量门禁（改动后必跑，全部 PASS 才算闭环）：`check_charts.mjs`（node，对数/线性坐标轴范围 + detail 图表功能）、`check_right_side.py`（右侧状态机五大约束，规则独立实现全量重算比对）、`check_temperature.py`（温度状态机全历史：相邻转移 + 沸/冻 3 日缓冲 + score 非空）、`check_rs.py`（RS 独立重算比对 + 取值域）、`check_l2_coverage.py`（L2→个股挂载覆盖率，含源数据抽验）、`check_data_freshness.py`（日线/指标/导出 JSON 推进到最近已收盘交易日）。
- `web/`：静态前端 + 管道输出的 JSON（`web/data/*.json` 已 gitignore，是生成物）。
- `tests/`：pytest 单测，每个文件手动 `sys.path.insert` 项目根。
- `docs/`：PRD、USER_GUIDE、`jinkin-philosophy.txt`（右侧状态机的规则来源，改 right_side 逻辑前必读）、`calibration/`（温度算法校准记录）。
- `data/`：SQLite、日志、断点文件，均为运行时产物。

## 6. 测试策略

- 单测在 `tests/`，命名 `test_*.py`，覆盖温度分档、RS 排名、右侧状态机、分类树、导出器等纯函数逻辑；用构造的 DataFrame 测，不依赖真实数据库和网络。
- 跑法：`python -m pytest tests/`（无 pytest 配置文件，测试自行处理 import 路径）。
- 集成验证：`python verify.py` 校验 `web/data` 全部 JSON 可解析且无 NaN/Infinity 泄漏——改导出器后必跑。

## 7. 开发约定

- 代码风格：Black 行宽 120（`pyproject.toml`），中文注释/日志允许且是主流；模块 docstring 普遍写清算法/用法，改动时同步更新。
- 配置一律走 `.env`（`src/config.py` 读取）：DSA 库路径、本地库路径、JSON 输出目录、日志目录、akshare 限速（2-5 秒随机 sleep）、并发数、前端端口等。**不写死绝对路径、密钥、账号**。
- SQLite 批量写入注意占位符上限：`src/indicators/engine.py` 用 `_BATCH_SIZE = 900` 分块，新增批量 SQL 时沿用此模式。
- 指标计算按 `_CALC_BATCH_SYMBOLS = 500` 分块流式处理：6000+ 品种一次性读 `daily_price` 会 OOM（160 万行 × 9 列），温度/RS/右侧三个步骤都不得退回全表一次读。RS 横截面排名本身必须全量，分块只到"各品种加权收益率"这一层。`exporter.export_prices` 同理（chunk_size=500），导出前会清理旧 `prices-*.json` 分片。
- 对外部数据源要有礼貌：akshare 请求必须走限速（`AKSHARE_SLEEP_MIN/MAX`），单只失败不阻塞整体。
- 日志统一 `logging`，落盘到 `data/logs/`；Windows 下注意 stdout UTF-8（见 `pipeline.setup_logging`）。

## 8. 硬规则（安全边界）

- 未经明确确认，不执行 `git commit` / `git push` / `git reset` / `git rebase`。
- **不写入或删除** `daily_stock_analysis/`、`data/stock_analysis.db`（DSA 主库只读）。
- 保持最小依赖：优先用现有包；新增依赖必须写入 `requirements.txt` 并说明原因。
- `data/*.db`、`web/data/*.json` 是生成物（已 gitignore），不要手改后当成果提交——改生成它们的代码。

## 9. 默认交付结构

修改后交付说明应包含：

1. 改了什么
2. 为什么这么改
3. 验证情况
4. 未验证项
5. 风险点
6. 回滚方式

验证底线：Python 改动至少 `python -m py_compile <changed_files>`；管道/数据库改动跑 `python scripts/run_pipeline.py --init-only`；前端改动起本地静态服务器实测。

## 10. 已知缺口 / 注意事项

- L2 → 个股挂载的权威来源是 `symbols.l2_industry_id`（由 `scripts/build_stock_l2_mapping_sw.py` 重建，akshare `index_component_sw` 逐 L2 拉成分）；`pipeline.sync_symbols` 每次 upsert 品种树后会自动调和 `parent_id ← l2_industry_id`（分类树对个股只能给到 L1/IND_UNKNOWN，不能让它冲掉 L2 挂载）。北交所（43/83/87/88/89/92 开头）与退市/长期停牌股不在申万成分内，属正常未映射；门禁 `scripts/harness/check_l2_coverage.py` 只统计近 60 天有行情的沪深活跃股。
- 申万官网行业指数日线发布滞后（上午常缺最近一个交易日），`get_industry_index_daily` 会自动转 tinyshare `sw_daily` 兜底；行业/概念指数当日数据两源都缺时，exporter 会 fallback 到前一交易日指标。
- `adj_factor` 字段已建未填充；`daily_full_sync.py` / `daily_full_sync_v2.py` 是 `daily_update.py` 之外的备选同步路径，改动前先确认当前实际使用的是哪条链路。
