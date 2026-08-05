# 连续校准工作流

> **目标**：用户每日提供一张「趋势动物 A 股趋势」截图，我转录为 `reference.csv`，跑 `calib_compare.py` 生成对比，按累计偏差迭代修正底层算法。
> **基线日期**：2026-08-03（见 `2026-08-03.md`）

---

## 工作流（每日）

```
1. 用户给截图（PNG，放到项目根，文件名带日期 hash）
2. 我肉眼读图 → 写入 docs/calibration/YYYY-MM-DD-reference.csv
3. 跑：python scripts/calib_compare.py docs/calibration/YYYY-MM-DD-reference.csv
4. 看输出（stdout + diff.csv）→ 判断偏差方向
5. 偏差稳定 ≥ 3 日 → 改 src/indicators/temperature.py / relative_strength.py
6. 回灌历史数据：python scripts/backfill_stocks.py --resume 拉增量 + 重算指标
7. 验证：再跑 calib_compare.py 对比 → 基线更新
```

---

## reference.csv 格式

```csv
group,name,ref_temperature,ref_pct_chg,ref_amount,ref_rs
民生消费,银行,凉,-0.3,384亿,99
民生消费,白酒,平,0.1,152亿,96
...
```

- `group`：截图里的分组标题（A 股-民生消费 等）
- `name`：校准图里的名称（已剥 Ⅱ/Ⅲ 后缀的简称）
- `ref_temperature`：冻/寒/凉/平/温/热/沸
- `ref_pct_chg`：日变动%，保留原始正负号
- `ref_amount`：日交易额（保留原格式字符串）
- `ref_rs`：强度 0-99

## 匹配规则（脚本内）

1. 先精确匹配 `symbols.name`
2. 失败：剥 Ⅱ/Ⅲ 后缀再试
3. 失败：查 `ALIAS` 别名表（白酒→白酒Ⅱ、IT 服务→IT 服务Ⅱ 等）
4. 仍失败：计入 `unmatched`，跳过

新增别名直接改 `scripts/calib_compare.py` 顶部 `ALIAS` 字典。

## 累积偏差追踪

每次跑 `calib_compare.py` 输出三组数据到 stdout：

```
== YYYY-MM-DD ==
参考品种 N，匹配 symbols M，未匹配 [list]
温度完全一致 X/N = Y%   ±1档 Z/N = W%
温度偏差分布: {0档: a, 1档: b, 2档: c}  (正=本地偏热, 负=偏冷)
RS 差(本地-参考): 均值 +X.X  MAE Y.Y  中位 Z
```

写入 `docs/calibration/YYYY-MM-DD.md`：
1. 偏差结构（哪几个行业组系统偏冷/偏热？哪几个 RS 偏差最大？）
2. 根因假设（状态机延迟？RS 口径响应慢？阈值边界？）
3. 修正方向（CONFIRM_DAYS +1/-1？RS 换快窗口？阈值微调？）
4. 验证：改完算法后回测 7 日/15 日/30 日累计 MAE

---

## 当前基线（2026-08-03）

| 指标 | 数值 |
|---|---|
| 温度完全一致率 | 50.0% |
| 温度 ±1 档率 | 96.7% |
| 温度偏差分布 | 0 档 30 / 1 档 28 / 2 档 2 |
| RS 偏差 | 均值 −8.0 / MAE 22.6 / 中位 18 |

## 已知偏差模式

| 类型 | 表现 | 根因 | 修正方向 |
|---|---|---|---|
| 温度 | 轨交设备/基建/通信服务 等连续下跌后档位反应慢 1-2 档 | 状态机 N=2 日确认 + 缓冲期叠加 | 评估 N=1 vs N=3 |
| RS | 民生消费 / 城市建设 / 数字科技 系统性偏低 10-20 分 | IBD 口径 [21/63/126/252] 在反转行情响应慢 | 评估快窗口 [10/21/63/126] |
| 个股 | 校准图含 9 只个股（银行/白酒/交通运输 等） | 本项目未单独对比（个股不在申万 L1/L2 里） | 后续扩展个股 symbol_id 映射 |

## 修正流程的纪律

1. **不轻易改阈值**：7-29 已经按"趋势动物"校准过一次。新参考数据出现反向偏差要先确认是不是单日异常（连续 3 日同方向才动手）
2. **一次只动一个变量**：温度阈值 / 温度状态机参数 / RS 口径分开改，单独验证
3. **保留所有旧数据**：每次修改前 `data/trend_compass.db` 备份到 `data/backups/trend_compass-YYYY-MM-DD-pre-{param}.db`
4. **改完用同一批 reference.csv 复跑**：保证对比口径稳定

## 浏览器入口

- 主对比页：`http://127.0.0.1:8080/compare8m3.html`
- 校准参数扫描：`http://127.0.0.1:8080/calibration.html`（温度参数 + RS 双口径对比）

## 关键文件清单

| 文件 | 用途 |
|---|---|
| `docs/calibration/YYYY-MM-DD-reference.csv` | 当日校准图原始转录（手动写） |
| `docs/calibration/YYYY-MM-DD-diff.csv` | 跑 calib_compare.py 自动生成 |
| `docs/calibration/YYYY-MM-DD.md` | 当日偏差分析报告（自动生成 + 人工补充） |
| `docs/calibration/correction-workflow.md` | 本文档 |
| `scripts/calib_compare.py` | CLI 对比工具 |
| `scripts/build_compare_with_calib.py` | 浏览器版对比页生成器 |
| `src/indicators/temperature.py` | 温度算法（待修正） |
| `src/indicators/relative_strength.py` | RS 算法（待修正） |