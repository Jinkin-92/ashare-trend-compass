# -*- coding: utf-8 -*-
"""仓位管理器：持仓追踪、信号生成、交易记录。

设计原则：
- 以 JSON 文件持久化，简单可靠
- 温度状态机驱动信号（与 backtest_portfolio_v2 保持一致）
- 支持 WeChat 消息格式输出
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# 策略参数（与 backtest_portfolio_v2 对齐）
# ============================================================
TRAILING_STOP = 0.10          # 距高点回撤 N% 止盈
SCALE_IN_RATIO = 0.50         # 沸区加仓比例
COST_BPS = 20                 # 双边千2 摩擦
MAX_SINGLE_WEIGHT = 0.15      # 单品种最大权重
BREADTH_HIGH = 0.50            # 市场宽度 >= 50% → 高仓位
BREADTH_MID = 0.20             # 市场宽度 >= 20% → 中仓位
ALLOC_HIGH = 0.60
ALLOC_MID = 0.30
SIGNAL_SCORE_MIN = 38          # 温→热 信号最低分数
DRAWDOWN_MAX = -0.25           # 买入最大回撤容忍
WARMUP_MIN_DAYS = 5            # 温区最小持续天数

# ============================================================
# 数据模型
# ============================================================

@dataclass
class Position:
    symbol_id: str
    symbol_name: str
    entry_date: str
    entry_price: float
    weight: float          # 占总资产比例
    peak_price: float
    status: str            # holding / reduced / exited
    scaled_in: bool = False
    state_current: str = ""
    state_prev: str = ""

@dataclass
class TradeRecord:
    symbol_id: str
    symbol_name: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_pct: float
    exit_reason: str
    scaled: bool = False

@dataclass
class Signal:
    """交易信号。"""
    symbol_id: str
    symbol_name: str
    action: str            # BUY / SCALE_IN / REDUCE / EXIT / WATCH
    current_state: str
    prev_state: str
    score: float
    price: float
    reason: str
    suggested_weight: float = 0.0

@dataclass
class PortfolioState:
    positions: Dict[str, Position] = field(default_factory=dict)
    trade_history: List[TradeRecord] = field(default_factory=list)
    cash_pct: float = 1.0
    last_update: str = ""

# ============================================================
# 仓位管理器
# ============================================================

class PositionManager:
    """管理持仓、生成交易信号、记录交易历史。"""

    def __init__(self, data_dir: Optional[Path] = None):
        if data_dir is None:
            from src.config import PROJECT_ROOT
            data_dir = PROJECT_ROOT / "data"
        self.data_dir = Path(data_dir)
        self.portfolio_file = self.data_dir / "portfolio.json"
        self.state = self._load()

    # ---- 持久化 ----

    def _load(self) -> PortfolioState:
        if self.portfolio_file.exists():
            try:
                with open(self.portfolio_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                state = PortfolioState()
                state.cash_pct = raw.get("cash_pct", 1.0)
                state.last_update = raw.get("last_update", "")
                for sid, p in raw.get("positions", {}).items():
                    state.positions[sid] = Position(**p)
                for t in raw.get("trade_history", []):
                    state.trade_history.append(TradeRecord(**t))
                return state
            except Exception as e:
                logger.warning("加载 portfolio.json 失败，使用空状态: %s", e)
        return PortfolioState()

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        raw = {
            "cash_pct": self.state.cash_pct,
            "last_update": self.state.last_update,
            "positions": {sid: asdict(p) for sid, p in self.state.positions.items()},
            "trade_history": [asdict(t) for t in self.state.trade_history[-200:]],
        }
        with open(self.portfolio_file, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2, default=str)

    # ---- 市场宽度 ----

    def calc_breadth(self, sector_states: Dict[str, str]) -> Tuple[float, float]:
        """计算市场宽度和目标仓位。

        Args:
            sector_states: {symbol_id: temperature_state}

        Returns:
            (breadth, target_alloc)
        """
        if not sector_states:
            return 0.0, 0.0
        warm_hot = sum(1 for s in sector_states.values() if s in ("温", "热", "沸"))
        breadth = warm_hot / len(sector_states)
        if breadth >= BREADTH_HIGH:
            target = ALLOC_HIGH
        elif breadth >= BREADTH_MID:
            target = ALLOC_MID
        else:
            target = 0.0
        return breadth, target

    # ---- 信号生成 ----

    def generate_signals(
        self,
        sector_data: List[dict],
        sector_states: Dict[str, str],
    ) -> Tuple[List[Signal], dict]:
        """根据最新温度数据生成交易信号。

        Args:
            sector_data: [{symbol_id, name, state, prev_state, score, price, ...}]
            sector_states: {symbol_id: state}

        Returns:
            (signals, summary)
        """
        signals: List[Signal] = []
        today = date.today().isoformat()

        # 当前持仓的 symbol_id 集合
        held_ids = set(self.state.positions.keys())

        breadth, target_alloc = self.calc_breadth(sector_states)

        for row in sector_data:
            sid = row["symbol_id"]
            name = row["name"]
            state = row["state"]
            prev_state = row.get("prev_state", "")
            score = row.get("score", 0.0)
            price = row.get("price", 0.0)
            drawdown = row.get("drawdown_250", 0.0)   # 250日回撤
            warm_days = row.get("warm_days", 0)

            # ---- EXIT: 持仓品种 ----
            if sid in held_ids:
                pos = self.state.positions[sid]
                # 1. 移动止盈
                if price > 0 and price < pos.peak_price * (1 - TRAILING_STOP):
                    signals.append(Signal(
                        symbol_id=sid, symbol_name=name,
                        action="EXIT", current_state=state, prev_state=prev_state,
                        score=score, price=price,
                        reason=f"移动止盈 (高点{pos.peak_price:.1f} → 现价{price:.1f}, 回撤{(1-price/pos.peak_price)*100:.1f}%)",
                    ))
                    continue

                # 2. 安全网：跌入凉/寒/冻
                if state in ("凉", "寒", "冻") and pos.state_current in ("温", "热", "沸"):
                    signals.append(Signal(
                        symbol_id=sid, symbol_name=name,
                        action="EXIT", current_state=state, prev_state=prev_state,
                        score=score, price=price,
                        reason=f"安全网 ({pos.state_current}→{state})",
                    ))
                    continue

                # 3. 热→温 清仓
                if state == "温" and pos.state_current == "热":
                    signals.append(Signal(
                        symbol_id=sid, symbol_name=name,
                        action="EXIT", current_state=state, prev_state=prev_state,
                        score=score, price=price,
                        reason=f"热→温 清仓",
                    ))
                    continue

                # 4. 沸→热 减持 50%
                if state == "热" and pos.state_current == "沸":
                    signals.append(Signal(
                        symbol_id=sid, symbol_name=name,
                        action="REDUCE", current_state=state, prev_state=prev_state,
                        score=score, price=price,
                        reason=f"沸→热 减持一半",
                    ))
                    continue

                # 5. 热→沸 加仓
                if state == "沸" and pos.state_current == "热":
                    if not pos.scaled_in:
                        signals.append(Signal(
                            symbol_id=sid, symbol_name=name,
                            action="SCALE_IN", current_state=state, prev_state=prev_state,
                            score=score, price=price,
                            reason=f"热→沸 加仓{int(SCALE_IN_RATIO*100)}%",
                            suggested_weight=pos.weight * SCALE_IN_RATIO,
                        ))
                    continue

                # 更新 peak
                if price > pos.peak_price:
                    pos.peak_price = price

            # ---- BUY: 温→热 (非持仓) ----
            elif state == "热" and prev_state == "温":
                if target_alloc <= 0:
                    signals.append(Signal(
                        symbol_id=sid, symbol_name=name,
                        action="WATCH", current_state=state, prev_state=prev_state,
                        score=score, price=price,
                        reason=f"温→热 但市场宽度{int(breadth*100)}%不足20%，暂不买入",
                    ))
                elif score < SIGNAL_SCORE_MIN:
                    signals.append(Signal(
                        symbol_id=sid, symbol_name=name,
                        action="WATCH", current_state=state, prev_state=prev_state,
                        score=score, price=price,
                        reason=f"温→热 但强度{score:.0f}<{SIGNAL_SCORE_MIN}",
                    ))
                elif drawdown < DRAWDOWN_MAX:
                    signals.append(Signal(
                        symbol_id=sid, symbol_name=name,
                        action="WATCH", current_state=state, prev_state=prev_state,
                        score=score, price=price,
                        reason=f"温→热 但回撤{drawdown*100:.0f}%超过{abs(int(DRAWDOWN_MAX*100))}%",
                    ))
                elif warm_days < WARMUP_MIN_DAYS:
                    signals.append(Signal(
                        symbol_id=sid, symbol_name=name,
                        action="WATCH", current_state=state, prev_state=prev_state,
                        score=score, price=price,
                        reason=f"温→热 但温区仅{warm_days}天<{WARMUP_MIN_DAYS}天",
                    ))
                else:
                    # 计算建议权重
                    n_current = len(self.state.positions)
                    single_w = min(target_alloc / max(n_current + 1, 1), MAX_SINGLE_WEIGHT)
                    remaining = target_alloc - sum(p.weight for p in self.state.positions.values())
                    single_w = min(single_w, remaining)
                    if single_w >= 0.01:
                        signals.append(Signal(
                            symbol_id=sid, symbol_name=name,
                            action="BUY", current_state=state, prev_state=prev_state,
                            score=score, price=price,
                            reason=f"温→热 强度{score:.0f} 回撤{drawdown*100:.0f}% 温区{warm_days}天",
                            suggested_weight=single_w,
                        ))

            # ---- WATCH: 非持仓但值得关注 ----
            elif state == "温" and prev_state in ("平", "凉"):
                # 即将进入温→热的值得关注
                if score >= 25:
                    signals.append(Signal(
                        symbol_id=sid, symbol_name=name,
                        action="WATCH", current_state=state, prev_state=prev_state,
                        score=score, price=price,
                        reason=f"{prev_state}→温 强度{score:.0f} 值得关注",
                    ))

        # 对持仓品种更新状态
        for sid in held_ids:
            if sid in sector_states:
                pos = self.state.positions[sid]
                pos.state_prev = pos.state_current
                pos.state_current = sector_states[sid]

        summary = {
            "date": today,
            "breadth": breadth,
            "target_alloc": target_alloc,
            "current_exposure": sum(p.weight for p in self.state.positions.values()),
            "cash_pct": self.state.cash_pct,
            "n_positions": len(self.state.positions),
            "n_buy": sum(1 for s in signals if s.action == "BUY"),
            "n_scale_in": sum(1 for s in signals if s.action == "SCALE_IN"),
            "n_reduce": sum(1 for s in signals if s.action == "REDUCE"),
            "n_exit": sum(1 for s in signals if s.action == "EXIT"),
            "n_watch": sum(1 for s in signals if s.action == "WATCH"),
        }
        return signals, summary

    # ---- 执行交易 ----

    def execute_signal(self, sig: Signal, user_confirmed: bool = True):
        """执行（或模拟执行）一个信号。user_confirmed=False 仅记录。"""
        if not user_confirmed:
            return

        today = date.today().isoformat()

        if sig.action == "BUY":
            # 如果接近满仓，缩现有仓位
            current_exp = sum(p.weight for p in self.state.positions.values())
            if current_exp + sig.suggested_weight > ALLOC_HIGH:
                excess = current_exp + sig.suggested_weight - ALLOC_HIGH
                if current_exp > 0.001:
                    scale = excess / current_exp
                    for p in self.state.positions.values():
                        p.weight *= (1 - scale)

            self.state.positions[sig.symbol_id] = Position(
                symbol_id=sig.symbol_id,
                symbol_name=sig.symbol_name,
                entry_date=today,
                entry_price=sig.price,
                weight=sig.suggested_weight,
                peak_price=sig.price,
                status="holding",
                scaled_in=False,
                state_current=sig.current_state,
                state_prev=sig.prev_state,
            )
            self.state.cash_pct = 1.0 - sum(p.weight for p in self.state.positions.values())

        elif sig.action == "EXIT":
            if sig.symbol_id in self.state.positions:
                pos = self.state.positions.pop(sig.symbol_id)
                ret = (sig.price / pos.entry_price - 1.0) * 100 - COST_BPS / 100.0
                self.state.trade_history.append(TradeRecord(
                    symbol_id=sig.symbol_id, symbol_name=sig.symbol_name,
                    entry_date=pos.entry_date, exit_date=today,
                    entry_price=pos.entry_price, exit_price=sig.price,
                    return_pct=ret, exit_reason=sig.reason,
                    scaled=pos.scaled_in,
                ))
                self.state.cash_pct = 1.0 - sum(p.weight for p in self.state.positions.values())

        elif sig.action == "REDUCE":
            if sig.symbol_id in self.state.positions:
                pos = self.state.positions[sig.symbol_id]
                # 减持一半
                sold_weight = pos.weight / 2
                pos.weight -= sold_weight
                self.state.cash_pct += sold_weight
                # 记录部分退出
                partial_ret = (sig.price / pos.entry_price - 1.0) * 100 - COST_BPS / 100.0
                self.state.trade_history.append(TradeRecord(
                    symbol_id=sig.symbol_id, symbol_name=sig.symbol_name,
                    entry_date=pos.entry_date, exit_date=today,
                    entry_price=pos.entry_price, exit_price=sig.price,
                    return_pct=partial_ret, exit_reason=f"减持50%: {sig.reason}",
                    scaled=pos.scaled_in,
                ))

        elif sig.action == "SCALE_IN":
            if sig.symbol_id in self.state.positions:
                pos = self.state.positions[sig.symbol_id]
                add_w = min(sig.suggested_weight, self.state.cash_pct)
                if add_w > 0.001:
                    pos.weight += add_w
                    pos.scaled_in = True
                    self.state.cash_pct -= add_w

        # 更新状态
        self.state.last_update = today
        self._save()

    # ---- 格式化输出 ----

    def format_daily_report(self, signals: List[Signal], summary: dict) -> str:
        """生成微信可读的日报文本。"""
        lines = []
        lines.append(f"📊 A股趋势罗盘 · 日报")
        lines.append(f"📅 {summary['date']}")
        lines.append("")

        # 市场概览
        lines.append(f"🌡️ 市场宽度: {summary['breadth']*100:.0f}% (温+热+沸)")
        lines.append(f"🎯 目标仓位: {summary['target_alloc']*100:.0f}%")
        lines.append(f"💼 当前仓位: {summary['current_exposure']*100:.1f}%")
        lines.append(f"💰 现金比例: {self.state.cash_pct*100:.1f}%")
        lines.append(f"📦 持仓数量: {summary['n_positions']} 个")
        lines.append("")

        # 当前持仓
        if self.state.positions:
            lines.append("── 当前持仓 ──")
            for sid, pos in self.state.positions.items():
                pl = (pos.peak_price / pos.entry_price - 1.0) * 100 if pos.entry_price > 0 else 0
                dd_from_peak = (pos.peak_price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0
                sc = " [加仓]" if pos.scaled_in else ""
                lines.append(f"  {pos.symbol_name}: {pos.weight*100:.1f}% | "
                           f"{pos.state_current} | "
                           f"入场{pos.entry_date} | "
                           f"浮盈{pl:+.1f}%{sc}")
            lines.append("")

        # 操作信号
        actions = {
            "BUY": "🟢 买入",
            "SCALE_IN": "🔵 加仓",
            "REDUCE": "🟡 减持",
            "EXIT": "🔴 清仓",
            "WATCH": "👀 关注",
        }

        priority_order = ["EXIT", "REDUCE", "BUY", "SCALE_IN", "WATCH"]
        for action in priority_order:
            action_signals = [s for s in signals if s.action == action]
            if not action_signals:
                continue
            lines.append(f"── {actions[action]} ({len(action_signals)}) ──")
            for s in action_signals:
                w_str = f" 建议{int(s.suggested_weight*100)}%" if s.suggested_weight > 0 else ""
                lines.append(f"  {s.symbol_name}: {s.reason}{w_str}")
            lines.append("")

        if not signals:
            lines.append("── 今日无操作信号 ──")
            lines.append("")

        # 最近交易记录
        if self.state.trade_history:
            lines.append("── 最近5笔交易 ──")
            for t in self.state.trade_history[-5:]:
                emoji = "✅" if t.return_pct > 0 else "❌"
                lines.append(f"  {emoji} {t.symbol_name}: {t.entry_date}→{t.exit_date} "
                           f"{t.return_pct:+.1f}% [{t.exit_reason}]")
            lines.append("")

        lines.append("📝 请回复操作指令：买入/加仓/减持/清仓 [品种名] 确认执行")
        return "\n".join(lines)

    def format_position_summary(self) -> str:
        """生成持仓摘要。"""
        lines = ["── 持仓概览 ──"]
        total_exposure = sum(p.weight for p in self.state.positions.values())
        lines.append(f"总仓位: {total_exposure*100:.1f}%  现金: {self.state.cash_pct*100:.1f}%")
        if not self.state.positions:
            lines.append("（空仓）")
        else:
            for sid, pos in self.state.positions.items():
                pl = (pos.peak_price / pos.entry_price - 1.0) * 100 if pos.entry_price > 0 else 0
                lines.append(f"  {pos.symbol_name} {pos.weight*100:.1f}% [{pos.state_current}] 入场{pos.entry_date} 浮盈{pl:+.1f}%")
        return "\n".join(lines)


# ============================================================
# 便捷函数
# ============================================================

def load_sector_temperature_data(db_path: Optional[str] = None) -> List[dict]:
    """从数据库加载最新 L2 行业温度数据，附带状态变化信息。

    Returns:
        [{symbol_id, name, state, prev_state, score, price, drawdown_250, warm_days}, ...]
    """
    from src.db import get_session
    from sqlalchemy import text

    if db_path is None:
        from src.config import LOCAL_DB_PATH
        db_path = str(LOCAL_DB_PATH)

    with get_session() as s:
        # 获取所有 L2 行业
        symbols = s.execute(text(
            "SELECT symbol_id, name FROM symbols WHERE node_type='industry_l2'"
        )).all()

        # 获取每个行业最新的温度数据（JOIN daily_indicator + daily_price）
        results = []
        for sid, name in symbols:
            rows = s.execute(text("""
                SELECT dp.trade_date, di.temperature_score, di.temperature,
                       dp.close, dp.high
                FROM daily_price dp
                JOIN daily_indicator di
                    ON dp.symbol_id = di.symbol_id AND dp.trade_date = di.trade_date
                WHERE dp.symbol_id = :sid AND di.temperature_score IS NOT NULL
                ORDER BY dp.trade_date DESC
                LIMIT 250
            """), {"sid": sid}).fetchall()

            if len(rows) < 2:
                continue

            # 最新两天
            latest = rows[0]   # (trade_date, score, temperature, close, high)
            prev = rows[1]

            # 计算 250 日高点回撤（用 high）
            hi250 = max(r[4] for r in rows if r[4] is not None) if rows else latest[3]
            dd = (latest[3] - hi250) / (hi250 + 1e-9) if hi250 > 0 else 0

            # 计算温区持续天数
            warm_days = 0
            for r in rows:
                if r[2] == "温":
                    warm_days += 1
                else:
                    break

            results.append({
                "symbol_id": sid,
                "name": name,
                "state": latest[2],        # temperature
                "prev_state": prev[2],
                "score": float(latest[1]) if latest[1] is not None else 0.0,  # temperature_score
                "price": float(latest[3]) if latest[3] is not None else 0.0,  # close
                "drawdown_250": float(dd),
                "warm_days": warm_days,
            })

        return results


def build_sector_state_map(sector_data: List[dict]) -> Dict[str, str]:
    """从 sector_data 构建 {symbol_id: state} 映射。"""
    return {row["symbol_id"]: row["state"] for row in sector_data if row["state"]}
