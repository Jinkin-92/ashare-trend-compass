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
- 2026-08-11：**RS 切换快窗口（10/21/63/126, 0.4/0.3/0.2/0.1）**——4 日回放（07-28/08-03/08-04/08-10）快窗 MAE 12.8 vs IBD 25.7，恢复 07-29 校准结论（08-05 仓库重建时回退为 IBD）。`scripts/recalc_rs.py --days 60` 回灌近 60 自然日。温度：趋势动物 08-10 起换新版 51 品种分类且温度语义变化（默认档 温→平），旧参考日不可用于阈值校准。详见 `docs/calibration/2026-08-10.md`
- 2026-08-12：**温度结构性修复（方案 C，用户拍板）**——回撤因子「距250日高点」创新高时贡献恒为 0（纯拖累，raw 上限 0.8，领涨板块永远到不了热/沸）→ 改为 250 日区间位置 `tanh((pos−0.5)×4)`；负向拉伸 2.0→1.5。08-10 参考：一致率 29.4→41.2%、±1档 72.5→86.3%、signed −1.00→−0.71，医疗服务 热→沸与参考一致。`scripts/recalc_temperature.py --days 60` 全品种回灌。新图基线 = 41.2% / 86.3% / −0.71
- 2026-08-13：**分档阈值重拟合（7 日综合拟合，417 样本）**——signed 偏差 7/7 参考日全负（本地系统性偏冷），跨新旧图时代方向一致。`SCORE_THRESHOLDS`：温 20→30、平/凉 −35→−65、凉/寒 −60→−80、寒/冻 −85→−95（沸 75/热 50 不变），`_raw_bucket_idx` 改为读常量。效果：全 7 日一致率 22.8%→42.9%、±1档 61.2%→79.9%、signed −0.99→−0.41；新两日（08-10/08-12，库存值口径）86.3%/90.2% 一致、±1档均 98.0%。回灌 257,751 行，pytest 57/57，门禁全过。详见 `docs/calibration/2026-08-12.md`
- 2026-08-14：**8 日综合回放 + 收敛判断**——`SCORE_THRESHOLDS` 不再调。08-13 真实对比 90.2% / ±1档 100% / signed -0.06（5 个 ±1 档偏差全无 ±2 以上）；新 3 日（08-10/08-12/08-13）一致率 86.3%/90.2%/90.2%、±1档 98-100%、signed 几乎 0，阈值已稳定收敛。pooled 8 日 48.1% / 81.8% / -0.37 较 7 日略好是新增一日拉高，阈值本身未变。**遗留 3 类结构性问题**（已超出阈值可解）：① 医疗服务顶层响应偏快（08-12 温→沸 + 08-13 热→沸）；② 中药/化学制药顶部响应慢（score -3 到 +12 判平，参考温）；③ 小幅下跌品种被判凉（光伏风电/轨交军工，参考平）。详见 `docs/calibration/2026-08-13.md`
- 校准工具：`scripts/optimize_params.py`（v1 时代网格搜索，已过时）、`scripts/eval_multiday.py`（多日回放评估，当前主力，DEFAULT_REFS 含全部 8 个参考日 07-09/07-21/07-28/08-03/08-04/08-10/08-12/08-13）、`scripts/calib_compare.py`（DB 库存值 vs 参考图真实对比）、`scripts/recalc_temperature.py`（回灌温度，--days N 控制窗口）
- **L2 行业指数发布滞后已知现象**：eval_multiday 用 `≤ cutoff` 的价格回放，所以 L2 数据未发布时跑出来的「当日一致率」实际是用前一交易日数据算的预测。真实对比要用 `scripts/calib_compare.py`，需要 daily_indicator 已写到 cutoff；L2 daily_indicator 滞后于 daily_price 半天到一天。手动补拉：`DailyPriceSync(fetcher).sync_industries(symbols, end_date=cutoff)` → `recalc_temperature.py --days 7` → `calib_compare.py docs/calibration/<date>-reference.csv`

## 门禁现状（2026-08-11）
- `check_rs.py` PASS（已对齐快窗口；当日排名池过滤 + nth(-1) 保留 NaN 两处修正）。
- `check_temperature.py` 已按 08-07「标签绕过状态机」语义重写（C1 改为抽样重算比对）。
- `check_right_side.py` **既有失败**（4 项）：门禁内嵌重算与生产语义长期漂移（2024 年老数据起 28 万处天数偏差），与当日改动无关（已用备份库逐列比对证实温度/右侧列零差异）。修复需对齐门禁内嵌的右侧规则实现，属独立任务。
- `check_data_freshness.py` 行业指数（index/L1/L2）当日数据发布滞后一天，已知环境现象，次日自愈。

## 个人投资工作台 `web/invest-workbench.html`
- 单文件 HTML 工作台，三模块：今天要处理（持仓浮亏 ≤-5% / ≤-8% 触发关注或止损审视，日报未完成待办自动顺延）／趋势罗盘（指数+申万 L1）／持仓+简要日报。
- **UI 偏好（用户硬规则，2026-08-11 确认）**：
  - 罗盘行的「温度」列直接显示档位文字（沸/热/温/平/凉/寒/冻），**不要退回数值列**。
  - 「RS」列只表示趋势强度（1-99 分位），不要混入温度信息。
- 演示持仓 2026-08-11 起改为用户实盘：512890 红利ETF汇 91800×1.116、601899 紫金矿业 300×34.39、002714 牧原股份 100×40.20、300308 中际旭创 100×887.98（总市值 20.56万）。
- 部署到 CloudStudio（workbuddy_cloudstudio_deploy）：必须带 `port: 3000`，否则偶发 400/504；目录结构 `index.html + data/index-l1.json`。
- 部署目录固定为 `tmp/workbench-deploy/`（脚本里现做：复制 web/invest-workbench.html → index.html、复制 web/data/index-l1.json → data/index-l1.json），不要改成部署 web/ 整个目录（会和 trend compass 自己的页面混在一起）。
- **当前线上版本**：https://ea9ef048e4ad47e884aec3dc1afcc9b4.app.workbuddy.link （sandboxId ea9ef048e4ad47e884aec3dc1afcc9b4，2026-08-19 重建）。旧 a97d0e7...bj7.agentos-app.net 已下线。
- **原位更新技巧（2026-08-12 确认）**：对同一目录重新部署会复用原 sandboxId，链接不变，无需下线旧版；只有换了部署目录/内容结构才生成新 sandboxId，此时旧版需 `action: "unpublish"` + shareLink 主动下线。线上数据是部署时快照，管道盘后跑完再重新部署一次即可刷新。
- 线上版本管理路径：设置 - 数据管理 - 我发布的应用。
- 关键经验值千金的踩坑：用户已打开的线上版页面持有 localStorage 旧数据，改完源代码重部署后不会自动同步数据，需要用户在页面上「清空」后重新加载或重新导入备份。
- **2026-08-19 历史 bug 修复**：`fmtPct2` 函数 line 937 源码 `"%</span>';` 缺一个双引号（应是 `"%</span>";`），导致主脚本整个 parse 失败 → `init()` 不跑 → 罗盘/信号全空。inline 数据方案可解决 fetch 失败但解决不了源码 parse 失败，必须先修语法。修后 `node --check` 通过 + chrome headless 截图确认数据正常。部署脚本 `scripts/build_workbench_deploy.py` 同时 inline 罗盘和信号 JSON。
- **部署脚本 `scripts/build_workbench_deploy.py`**：把 `web/data/index-l1.json` + `web/data/hotlist.json` 内容直接 inline 进 HTML 输出到 `tmp/workbench-deploy/index.html`，再用 cloudstudio-deploy 推上去。避开了 fetch / CORS / 浏览器 cache 三个不确定性。

## 自动化 cron（17:00 盘后）的双通道推送
- **飞书「日报群」**：chat_id = `oc_53c459acc37010341577f98755231cb5`（bot 身份发送，已验证 2026-08-19）。
- 推送命令（写到 cron prompt）：`lark-cli im +messages-send --chat-id oc_53c459acc37010341577f98755231cb5 --as bot --text "<日报全文>"`。
- 工作台 deploy 与日报推送是 cron 任务的最后两步（在「等待操作指令」之前）。
- **2026-08-19 修订**：cron 的"第四步部署工作台"应改为跑 `python scripts/build_workbench_deploy.py`（而不是手动 cp + deploy），这样 inline 数据自动带进去。
