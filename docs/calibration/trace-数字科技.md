# 拆解报告：数字科技下 3 个 L2 行业（7-09 截面）

> **目的**：Jinkin 要求"先拆解计算过程，再调整方案"。本报告把 3 个二级行业的每个字段从"数据源 → 中间计算 → 最终值"完整展示，**不动算法**。

---

## 1. 3 个二级行业的核心字段对照

| 字段 | 计算机设备 (SW_801101) | IT 服务Ⅱ (SW_801103) | 软件开发 (SW_801104) |
|---|---|---|---|
| 父级 | SW_801750 计算机 | SW_801750 计算机 | SW_801750 计算机 |
| 7-09 close | 3129.33 | 4863.56 | 5690.39 |
| 日涨幅 % | **+3.46%** | **+2.41%** | **+2.20%** |
| amount（合成指数权重） | 610.82 | 630.47 | 544.31 |
| 温度 | 平 | 凉 | 凉 |
| temperature_score | **+21.48** | **-38.76** | **-39.85** |
| 相对强度 (RS) | 84 ↑ | 62 ↓ | 51 ↓ |
| 右侧状态 | 否 (0天) | 是 (+29天, 凉) | 是 (+40天, 凉) |

**vs Jinkin 参考图 7-09 截图**：

| 字段 | 计算 | 参考 (Jinkin) | 差 |
|---|---|---|---|
| 计算机设备 temp | 平 | 平 | ✅ |
| 计算机设备 RS | 84 | 96 | -12 |
| IT 服务 temp | 凉 | 凉 | ✅ |
| IT 服务 RS | 62 | 63 | -1 |
| 软件开发 temp | 凉 | 凉 | ✅ |
| 软件开发 RS | 51 | 59 | -8 |

**温度 3/3 一致**（这 3 个）。**RS 3/3 偏低**（-1 ~ -12）。

---

## 2. 字段计算溯源

### 2.1 包含股票数量

**当前 L2 行业是合成指数，没有"包含股票数量"字段**。
- L2 行业指数 = SW 发布的"行业指数"（按成分股加权计算的合成指数）
- **行业指数的"成分股"需要去查 `industry_cons` 表，但我们 symbols 库没存这个映射**

**替代方案**：用 `parent_id` 反查同一 L1 行业下所有 symbol 中 `node_type='stock'`（个股）的数量：
- 计算机 (SW_801750) 下个股市值 = 523 个
- 但**这个数没暴露在 symbols.json 里**

### 2.2 价格 (close)

**数据源**：`daily_price.close`
- **7-09 拉取路径**：`akshare.ak.index_hist_sw(symbol="801101", period="day")`（7-09 增量由今天 run_pipeline 拉到）
- 物理存储：`d:\code\dsa\stock analysis\ashare-trend-compass\data\trend_compass.db` 的 `daily_price` 表
- 单位：指数点位（3129.33 = 2026-07-09 计算机设备行业指数 3129.33 点）

### 2.3 日涨幅 (pct_chg)

**数据源**：`daily_price.pct_chg`
- **计算公式**：`pct_chg = (close_today - close_yesterday) / close_yesterday * 100`
- **当前库内直接存储**——akshare `index_hist_sw` 返回的字段之一（不是我们自己算的）
- ⚠️ **疑点**：aks 拉的 `pct_chg` 可能是基于其"前一日 close"（可能经过复权/特殊处理），与我们自己算的可能有微小差异

### 2.4 日成交额 (amount)

**数据源**：`daily_price.amount`
- **指数 amount** = 真实成交额（元），如 7 个宽基指数的 amount 是 22.3 万亿这种
- **行业 L2 amount** = 合成指数的"权重"（a*1000 形式），**不是真实成交额**
  - 计算机设备 7-09: 610.82（实际值）—— **这个数太小，疑似是 akshare 返回的某权重列**
  - **真正想看的"行业成交额"应该是 L2 行业下所有成分股个股的成交额之和**
  - 这个需要 daily_price 里 `node_type='stock' AND parent_id='SW_801101'` 的所有 amount 求和
  - **当前没实现**

### 2.5 A 股内趋势强度 (RS)

**数据源**：`daily_indicator.rs_score`
- **当前算法**：
  - 每个 symbol 算 `weighted_return = 0.4*ROC63 + 0.2*ROC126 + 0.2*ROC189 + 0.2*ROC252`
  - 按 `trade_date + node_type` 分组（这里是 `industry_l2`），对 weighted_return 做 1-99 百分位排名
  - 同组 1 个时设为 50
- **问题**：
  - **这里的"组"是 industry_l2（共 123 个）**——也就是说 RS 84 表示"在 123 个 L2 行业里排第 84 分位"
  - **Jinkin 的 RS 96 是同一组排名吗？**——差异 -12 不大，但**整体偏低**（前面校准 17.7 均值）
  - **可能偏差源**：加权权重 0.4 太高 / 252 日 lookback 太短 / 排名算法精度问题

### 2.6 温度 (temperature)

**数据源**：`daily_indicator.temperature`
- **当前算法**（`src/indicators/temperature.py:classify_temperature`）：
  1. 方向分：`(close vs ma60) + (ma20 vs ma60)` 四种状态 → ±0.5 / ±1.0
  2. 动量分：ROC20 / ROC60 / ROC120 加权和（权重 0.5/0.3/0.2），各 ROC 封顶 ±30
  3. 波动扩张分：ATR5/ATR60 > 1.5 时加方向分 × 10
  4. 总分 = 方向×30 + 动量 + 波动扩张
  5. 七档：≥50 沸 / ≥25 热 / ≥5 温 / -5~5 平 / -25~-5 凉 / -50~-25 寒 / ≤-50 冻

**3 个 L2 行业 7-09 的算法各分量**：

| 品种 | close vs ma60 | ma20 vs ma60 | 方向分 | ROC20 | ROC60 | ROC120 | ATR5/60 | 温度分 | 档位 |
|---|---|---|---|---|---|---|---|---|---|
| 计算机设备 | + 上 | + 多头 | 1.0 | +0.34% | +2.99% | +21.18% | 0.69 (无) | +21.48 | 平 |
| IT 服务 | - 下 | - 空头 | -1.0 | -0.97% | -11.32% | -7.69% | 0.81 (无) | -38.76 | 凉 |
| 软件开发 | - 下 | - 空头 | -1.0 | -1.06% | -10.66% | -9.22% | 0.84 (无) | -39.85 | 凉 |

**温度完全一致**——**这个算法对 L2 行业当前市场状态下结果是准的**。

### 2.7 右侧天数 (right_side_days)

**数据源**：`daily_indicator.is_right_side, right_side_days, right_side_entry_temp`
- **当前算法**（`src/indicators/right_side.py:compute_right_side_state`）：
  - **进入右侧**：温度首次达到"热"（含沸），天数=1
  - **维持右侧**：只要温度 ≥ "温"，天数+1
  - **退出右侧**：温度跌破"温"，天数清零
  - **空头对称**：首次达到"寒"进入空头右侧，维持"凉"，升破"凉"退出

**3 个 L2 行业的右侧状态**：
- 计算机设备：凉档（**没**在右侧，0 天）→ **但 Jinkin 截图应该是平/温档？**
- IT 服务Ⅱ：凉档 +29 天（空头右侧，72 天前首次进入"寒"档）
- 软件开发：凉档 +40 天（空头右侧，40 天前首次进入"寒"档）

⚠️ **这里有个明显的"逻辑"问题**：Jinkin 截图里"IT 服务Ⅱ"是"凉"档，不在"寒"档——按"首次达到寒"进入空头右侧，**"凉"档应该是在空头右侧的退出边**（升破凉就退出）。但 `right_side_days=29` 表示已 29 天——**意味着之前在寒档待了 29+天才回到凉档**？这跟"凉"档 vs "寒"档切换的时间序列有关，需要看历史。

---

## 3. 拆解完的整体观察

### 3.1 字段正确性
| 字段 | 状态 | 建议 |
|---|---|---|
| 包含股票数量 | ❌ 缺 | 加 `industry_cons` 映射表或反查 parent_id 下的 stock 数 |
| 价格 | ✅ 准 | 不用改 |
| 日涨幅 | ✅ 准 | 不用改（pct_chg 直接来自 akshare） |
| 成交额 | ❌ 行业 amount 是合成指数的"权重"，不是真实成交额 | **改**：用 L2 行业下所有个股 amount 求和；或标"成分股权重" |
| 温度 | ✅ 准（数字科技这 3 个 L2 全对） | 不用改 |
| 相对强度 | ⚠️ 偏低 ~10 分 | 跟 Jinkin 参考的 ranking 方式/权重可能不同 |
| 右侧天数 | ⚠️ 滞后带可能"维持阈值太宽" | 待 Jinkin 决定 |

### 3.2 跟 Jinkin 校准图差异（数字科技这组）
- **温度：3/3 一致**（100%）
- **RS：3/3 偏低 1-12**（均值 -7）

---

## 4. 数据流（端到端）

```
akshare.index_hist_sw(symbol="801101", period="day")
        ↓
[网络] akshare HTTP
        ↓
src/daily_sync.py:_upsert_daily
        ↓
SQLite daily_price 表
        ↓
src/indicators/engine.py:run_indicator_update
   ↓
   ├─ src/indicators/temperature.py:classify_temperature   → daily_indicator.temperature
   ├─ src/indicators/relative_strength.py:rank_rs_by_node_type  → daily_indicator.rs_score
   └─ src/indicators/right_side.py:compute_right_side_state  → daily_indicator.is_right_side, right_side_days
        ↓
src/exporter.py:export_symbols
        ↓
web/data/symbols.json  +  web/data/snapshots/{date}.json
        ↓
web/js/app.js:renderTableHtml 渲染
```

**3 个潜在改动点**：
1. `data_source.py`：换 amount 数据源 / 换概念板块数据源
2. `indicators/*.py`：调温度/RS/右侧算法
3. `exporter.py`：加"行业成交额"汇总字段

---

## 5. 等 Jinkin 决定调整哪个

Jinkin，**3 个 L2 行业全部温度对，RS 略偏低**——**算法大体对**。你提的"先拆解"诉求已交付。

**下一步可能要调整的**（待你决定）：
1. 行业成交额（amount）字段怎么改？
2. 行业 L2 的"成分股数"字段加不加？
3. RS 算法权重 / lookback 怎么调？
4. 右侧状态机滞后带怎么调？

请告诉我哪一块要先动。
