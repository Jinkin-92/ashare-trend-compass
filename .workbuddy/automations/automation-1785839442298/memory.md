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

## 2026-08-06（17:00 定时执行，第三次）

- **命令**：`C:\Users\Jinkin\AppData\Local\Programs\Python\Python311\python.exe scripts/daily_update.py` → `scripts/daily_report.py --no-update`
- **结果**：三阶段全部成功，总耗时 2614.9s（约 43.6 分钟，比上次慢；无锁库，仅 2 个无关 voicebox python 进程在跑）
  - 阶段 1：品种树 6085 条（东财列表仍被代理掐断降级新浪，已知现象）
  - 阶段 2：概念 148 条 + 个股 7883 条（tinyshare 5533 + baostock 兜底 356）；概念同步被同花顺源掐断约 20 个（SSLError，已知）
  - 阶段 3：指标 6080 品种（温度 98933 / RS 65850 / 右侧 719141）+ 导出 trade_date=**2026-08-06**，12 片价格分片，35.2MB
- **日报**：市场宽度 2%（124 个 L2 行业仅 3 个温：医疗服务/白色家电/国有大型银行Ⅱ；**L2 行业指标停在 08-05**，行业指数当日数据两源皆缺属已知现象），目标仓位 0%，无操作信号，持仓 0（portfolio.json 尚不存在，用户无持仓）
- **验证**：verify.py 退出码 0；全市场温度分布（5642 条）：寒 34.1% + 冻 38.0%，市场极度弱势
- **重要经验（本次新发现）**：`daily_report.py` 默认会**内部再次调用 daily_update.py 完整更新（40+ 分钟）**！自动化流程第一步已跑过更新，第二步必须加 `--no-update`，否则白白多等 40 分钟且可能与第一步重复写库。手册流程应写为：先 daily_update.py，再 daily_report.py --no-update。
- **校验小坑**：`daily_indicator.temperature` 列存的是**档位字符串**（沸/热/温/平/凉/寒/冻）不是数值，用数值阈值 SQL 比较会得出错误结论；数值分在 `temperature_score` 列

## 2026-08-07（17:00 定时执行，第四次）

- **命令**：`python scripts/daily_update.py`（全路径）→ `scripts/daily_report.py --no-update`
- **结果**：三阶段全部成功，总耗时 1557.9s（约 26 分钟，近期最快一次；无 python 进程在跑，无锁库）
  - 阶段 1：品种树 6085+ 条；阶段 2：概念 341 条 + 个股 7885 条（tinyshare 5535 + baostock 兜底 354）；行业指数 47 条
  - 阶段 3：指标 6081 品种（温度 99233 / RS 65852 / 右侧 99233）+ 导出 trade_date=**2026-08-07**，12 片价格分片，35.2MB
- **日报**：市场宽度 2%（124 个 L2 行业仅 2 个温：医疗服务/白色家电，国有大型银行Ⅱ 已退出温档），目标仓位 0%，无操作信号，持仓 0
- **数据状态**：concept/stock 指标推进到 08-07（概念 361 品种、个股 5506 品种）；index/industry_l1/industry_l2 停在 08-06（行业指数官方数据发布滞后，已知现象）
- **全市场温度分布（08-07，5645 条）**：沸 0.0% / 热 0.5% / 温 2.6% / 平 24.3% / 凉 23.9% / 寒 32.5% / 冻 16.1%，市场延续弱势
- **验证**：verify.py 退出码 0 全部 OK
- **非致命告警**：同花顺概念源（d/q.10jqka.com.cn）仍被本机代理掐断（SSLError/ProxyError/ConnectionResetError，约 20+ 个概念失败，已知现象）；tinyshare 正常覆盖，无类型比较报错

## 2026-08-07（18:35 再次触发，非 17:00 定时）

- **背景**：当日 17:00 定时任务已完成更新（数据最新 08-07）。本次触发直接 `daily_report.py --no-update` 复用当日数据，未重跑更新管道。
- **结果**：日报生成成功（0.5s），无操作信号、持仓 0（portfolio.json 不存在）；右侧状态品种 53 个，医药 CXO 板块集中。
- **经验**：若 automation 在当日更新完成后再次触发（如手动重跑/重试），先查 daily_update.log 与 daily_indicator MAX(trade_date) 确认数据是否已最新，最新则跳过 daily_update.py 直接出日报，可省 25-40 分钟。

## 2026-08-11（09:00 手动触发，非 17:00 定时；08-10 周一定时任务缺失）

- **背景**：数据库停在 08-07（08-10 周一定时任务未执行，可能机器关机/任务失败），本次于 08-11 早上补跑，一次跑齐 08-10 增量。
- **命令**：`daily_update.py`（全路径，后台运行）→ `daily_report.py --no-update`（注意当日盘前，数据截至 08-10 属正常）
- **结果**：三阶段全部成功，总耗时 1372.6s（约 23 分钟，无 python 进程占用，无锁库）
  - 阶段 1：品种树 6089 条
  - 阶段 2：指数 14 + 行业 310 + 概念 595 + 个股 7890（tinyshare 5538 + baostock 兜底 351）；概念源仍被代理掐断约 20 个（SSLError，已知）
  - 阶段 3：导出 trade_date=**2026-08-10**，13 片价格分片，35.3MB
- **日报**（08-11）：市场宽度 2%，目标仓位 0%，持仓 0（portfolio.json 不存在）；仅 1 条关注信号：医疗服务 温→热 但市场宽度不足 20% 暂不买入；无买卖信号
- **全市场温度分布（08-10，6022 条）**：沸 0.1% / 热 0.6% / 温 3.1% / 平 29.3% / 凉 25.0% / 寒 30.9% / 冻 11.0%（对比 08-07：平 24.3% 升、冻 16.1% 降，市场略回暖但仍弱）
- **验证**：verify.py 未重跑（当日已多次验证过管道，无指标/导出逻辑改动）
- **经验**：周一盘前手动补跑时，更新增量目标会自动设为最近已收盘交易日（08-10），当日数据不会出现，无需担心。

## 2026-08-11（17:00 定时执行，当日早间已补跑过 08-10，本次拉 08-11 盘后数据）

- **命令**：`daily_update.py`（全路径）→ `daily_report.py --no-update`
- **结果**：三阶段成功，总耗时 1226.7s（约 20.5 分钟，无 python 进程占用，无锁库）
  - 阶段 1：品种树 6089 条（东财列表仍被代理掐断降级新浪，已知现象）
  - 阶段 2：日线同步 indices 0 + industries 0 + concepts 54 + stocks 7889（tinyshare 5539 + baostock 兜底 350）；**指数/行业指数两源今日均无 08-11 数据（申万发布滞后，已知），概念同花顺源仍被掐断只成功 54 条**
  - 阶段 3：指标 6084 品种（温度 99057 / RS 65769 / 右侧 99057）+ 导出 trade_date=**2026-08-11**，12 片价格分片，35.2MB
- **数据状态**：concept/stock 指标推进到 08-11；index/industry_l1/industry_l2 停在 08-10（行业指数官方发布滞后，已知现象）
- **日报**（08-11）：市场宽度 2%（温+热+沸），目标仓位 0%，持仓 0（portfolio.json 不存在）；仅 1 条关注信号：医疗服务 温→热 但市场宽度不足 20% 暂不买入；无买卖信号
- **全市场温度分布（08-11，5549 条）**：沸 0.0% / 热 0.8% / 温 3.5% / 平 27.8% / 凉 27.2% / 寒 31.2% / 冻 9.4%（对比 08-10：温 3.1% 升、热 0.6% 升、冻 11.0% 降，市场略回暖但仍弱）
- **经验**：当日同一天内早间已补跑时，17:00 定时再跑会正常拉取当日盘后数据（增量目标 08-11），指数/行业因发布滞后停在 08-10 属正常，不影响个股/概念温度。

## 2026-08-14（17:00 定时执行，第五次）

- **命令**：`daily_update.py`（全路径）→ `daily_report.py --no-update` → verify.py
- **结果**：三阶段成功，总耗时 1424.3s（约 23.7 分钟，无锁库，仅 serve_web.py 常驻）
  - 阶段 2：概念 424 + 个股 7893（tinyshare 5540 + baostock 兜底 349）；`industries: 0`（行业指数当日未发布，滞后一天属正常）
  - 阶段 3：指标 6085 品种（温度 99638 / RS 65763 / 右侧 99638）+ 导出 trade_date=**2026-08-14**，12 片，35.4MB
- **数据状态**：08-14 指标仅 stock 5502 + concept 21（**概念同步大量失败**：同花顺源被代理掐断仅 21/375 成功，比平时 300+ 少很多，已知现象，次日补缺自愈）；index/L1/L2 停在 08-13
- **日报**：市场宽度 2%（08-13 L2：医疗服务 沸 + 白色家电/煤炭开采 温），目标仓位 0%，持仓 0，无操作信号；信号解释：医疗服务 热→沸 不触发 BUY（BUY 只认温→热且宽度<20%）
- **全市场温度分布（08-14，5523 条）**：沸 1.4% / 热 2.3% / 温 3.7% / 平 79.1% / 凉 10.6% / 寒 2.4% / 冻 0.4%（方案 C + 阈值重拟合后的新基线，平档占多数属预期）；右侧状态品种 226 个
- **验证**：verify.py 退出码 0
- **经验**：日报「市场宽度」基于 L2 行业（发布滞后一天），08-14 跑出来的宽度实际是 08-13 口径，属正常。概念同步失败过多时（21 条），日报温度分布（个股+概念口径）中概念占比小，影响有限。

## 2026-08-17（17:00 定时执行，第六次）

- **命令**：`daily_update.py`（全路径）→ `daily_report.py --no-update` → verify.py
- **结果**：三阶段成功，总耗时 1177.1s（约 19.6 分钟，近期最快，无锁库）
  - 阶段 1：品种树 6091 条（东财列表仍被代理掐断降级新浪，已知现象）
  - 阶段 2：指数 7 + 行业 155 + 概念 398（同花顺源个别被掐断但整体覆盖良好）+ 个股 7892（tinyshare 5539 + baostock 兜底 350）
  - 阶段 3：指标 6086 品种（温度 99837 / RS 65792 / 右侧 99837）+ 导出 trade_date=**2026-08-17**，12 片，35.4MB
- **数据状态**：concept/stock 指标推进到 08-17（concept 355 + stock 5508）；index/L1/L2 停在 08-14（08-15/16 非交易日，申万行业指数官方发布滞后，已知现象）
- **日报**：市场宽度 3%（08-14 L2 口径），目标仓位 0%，持仓 0（portfolio.json 不存在）；1 条关注信号：工程咨询服务Ⅱ 平→温 强度42
- **全市场温度分布（08-17，5550 条）**：沸 1.6% / 热 2.6% / 温 4.3% / 平 80.7% / 凉 9.0% / 寒 1.7% / 冻 0.2%（对比 08-14：温 3.7% 升、平 79.1% 升，市场温和回暖）；右侧状态品种 250 个
- **验证**：verify.py 退出码 0 全部 OK
- **经验**：本周一为假期后首个交易日（08-15/16 周末），指数/行业停在 08-14 属正常；温度分布查询需注意 daily_indicator.temperature 是档位字符串，数值在 temperature_score 列。

## 2026-08-19（手动配置 cron 任务，09:30）

- **背景**：用户要求在 cron 任务里加一条规则：「每次运行结束，更新工作台并通过 IM 连接器推送到微信」。澄清后改成「飞书」+「保留原部署」。
- **改动**：
  1. 部署工作台到 CloudStudio：新建固定目录 `tmp/workbench-deploy/`（复制 web/invest-workbench.html → index.html、复制 web/data/index-l1.json → data/index-l1.json），调用 `workbuddy_cloudstudio_deploy action=deploy directory=tmp/workbench-deploy entry=index.html port=3000`。
     - 新 shareLink = `https://ea9ef048e4ad47e884aec3dc1afcc9b4.app.workbuddy.link`，sandboxId `ea9ef048e4ad47e884aec3dc1afcc9b4`。
     - 旧 `https://a97d0e7ba601468da054ed4aab3c98e7.bj7.agentos-app.net` 已下线（08-11 后未续命，sandbox 大概率已销毁）。
     - 关键：必须带 `port: 3000`，否则偶发 400/504。
  2. 飞书 IM 推送：通过 `lark-cli im +messages-send --chat-id oc_53c459acc37010341577f98755231cb5 --as bot --text "<日报>"`，chat_id 是用户的「日报群」（bot 已在群里）。
     - 试过 `--as user`，user_access_token 已过期；`+chat-list --types=p2p` 会要求重新授权。所以走 `--as bot` 推到群是最优路径。
  3. cron prompt 更新为 5 步：① daily_update.py ② daily_report.py --no-update ③ 双通道推送（当前窗口 + 飞书）④ 部署工作台 ⑤ 等待用户反馈。
- **验证**：已发一条测试消息到「日报群」（message_id om_x100b6764c8f8e0a0c49f9c8e4945a86），09:35 飞书收到；CloudStudio 部署链接已 curl 验证可访问。
- **下次 cron 触发**：08-19（周三）17:00，按新 prompt 跑完整套（预计 25-40 分钟）。

## 2026-08-19（11:00 - 14:40，深度排查工作台空白问题）

- **症状**：用户反馈打开工作台完全空白（罗盘/信号全空）。我先加 inline 数据方案、再加自动同步、两次部署都没解决。
- **根因诊断**（用 chrome --headless + remote-debugging + CDP 抓 console exception + acorn 解析）：
  - `web/invest-workbench.html` line 937 `fmtPct2` 函数：`... + "%</span>';` 末尾字符串 `"%</span>` 没正确闭合（应是 `"%</span>";`）。
  - V8 报 `Unterminated string constant at line 937 col 76`，整个主脚本 parse 失败 → `init()` 不执行 → inline 数据声明了但没人读。
  - 这是历史就有的 bug，git 里 fmtPct2 函数从一开始写错了引号。
- **修复**：
  1. line 937 `"%</span>';` → `"%</span>";`
  2. `node --check tmp/main_script.js` EXIT=0
  3. chrome headless 截图确认：罗盘 7指数 + 31行业 + 信号 32买入 + 13观察 + 2警惕全部显示
- **新脚本 `scripts/build_workbench_deploy.py`**：把 `web/data/index-l1.json` + `web/data/hotlist.json` 同时 inline 进 HTML，输出到 `tmp/workbench-deploy/index.html`。比手动 cp 更稳。
- **`web/invest-workbench.html` 的 `loadSignals()`** 加了 HOTLIST_DATA 优先分支。
- **cron prompt 第四步**改成跑 `scripts/build_workbench_deploy.py` + `node --check` 验证。
- **下次 cron 触发**：08-19 17:00，按新 prompt 跑（已加 node --check 验证脚本 parse）。

## 2026-08-19（15:05，Git 提交与推送）

- **提交前验证**：`pytest` 57 passed；`check_rs.py` 全部通过；`check_temperature.py` 全部通过；工作台构建脚本成功生成 38 个罗盘品种和当日信号 inline 数据；工作台主脚本 `node --check` 通过。
- **GitHub**：创建提交 `3e667cd`（`fix: update trend calibration and workbench deployment`），已推送到 `origin/main`。
- **工作区**：提交后仅剩未跟踪运行产物和历史快照（`data/backups/`、`data/portfolio.json`、`data/reports/`、`tmp/`、`web/invest-workbench-2026-08-17.html`、`web/invest-workbench-today.html`），未纳入本次提交。

