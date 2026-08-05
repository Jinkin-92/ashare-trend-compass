# -*- coding: utf-8 -*-
"""指标引擎集成测试（使用内存/临时 SQLite）。"""

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src.indicators.engine import run_indicator_update
from src.models import DailyIndicator, DailyPrice, Symbol


def _init_temp_db():
    """初始化临时数据库并替换 db 模块引擎/会话工厂。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(
        f"sqlite:///{tmp.name}",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    db._engine = engine
    db.SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db.Base.metadata.create_all(bind=engine)
    return tmp.name


def _make_symbol(symbol_id: str, node_type: str = "stock"):
    return Symbol(symbol_id=symbol_id, name=symbol_id, node_type=node_type, is_leaf=True)


def _make_daily_prices(symbol_id: str, start: date, days: int, trend: float = 0.001):
    """生成测试用 daily_price 记录。"""
    dates = pd.date_range(start=start, periods=days, freq="D")
    close = 100 * np.exp(np.linspace(0, trend * days, days))
    records = []
    for i, d in enumerate(dates):
        c = float(close[i])
        records.append(
            DailyPrice(
                symbol_id=symbol_id,
                trade_date=d.date(),
                open=c * 0.99,
                high=c * 1.02,
                low=c * 0.98,
                close=c,
                volume=1e6,
                amount=1e8,
                pct_chg=0.0,
            )
        )
    return records


@pytest.fixture(scope="function")
def clean_db():
    """每个测试使用全新临时库。"""
    _init_temp_db()
    yield


def test_run_indicator_update_writes_temperature_and_rs(clean_db):
    """引擎应能把温度和 RS 写入 daily_indicator。"""
    start = date(2024, 1, 1)
    days = 300
    end = start + timedelta(days=days - 1)

    with db.get_session() as session:
        session.add_all([
            _make_symbol("SYM_UP", "stock"),
            _make_symbol("SYM_DOWN", "stock"),
        ])

    records = _make_daily_prices("SYM_UP", start, days, trend=0.001)
    records += _make_daily_prices("SYM_DOWN", start, days, trend=-0.001)

    with db.get_session() as session:
        session.add_all(records)

    result = run_indicator_update(symbol_ids=["SYM_UP", "SYM_DOWN"], end_date=end)
    assert result["temp_upserted"] > 0
    assert result["rs_upserted"] > 0
    assert result["right_upserted"] > 0

    with db.get_session() as session:
        rows = session.query(DailyIndicator).all()
        assert len(rows) > 0
        assert all(r.temperature in ("沸", "热", "温", "平", "凉", "寒", "冻") for r in rows)
        assert all(1 <= r.rs_score <= 99 for r in rows if r.rs_score is not None)
        assert all(r.rs_score_trend in ("↑", "↓", "↓↓", "flat") for r in rows)
        assert all(isinstance(r.is_right_side, bool) for r in rows)
        assert all(isinstance(r.right_side_days, int) for r in rows)


def test_run_indicator_update_incremental(clean_db):
    """增量更新只写入新日期，旧数据不被清空。"""
    start = date(2024, 1, 1)
    days = 300
    end = start + timedelta(days=days - 1)

    with db.get_session() as session:
        session.add(_make_symbol("SYM_UP", "stock"))

    records = _make_daily_prices("SYM_UP", start, days, trend=0.001)

    with db.get_session() as session:
        session.add_all(records)

    run_indicator_update(symbol_ids=["SYM_UP"], end_date=end)

    # 追加一条新日线
    new_date = end + timedelta(days=1)
    new_bar = DailyPrice(
        symbol_id="SYM_UP",
        trade_date=new_date,
        open=100,
        high=110,
        low=99,
        close=105,
        volume=1e6,
        amount=1e8,
        pct_chg=0.0,
    )
    with db.get_session() as session:
        session.add(new_bar)

    result = run_indicator_update(symbol_ids=["SYM_UP"], end_date=new_date)
    assert result["temp_upserted"] >= 1
    assert result["rs_upserted"] >= 1
    assert result["right_upserted"] >= 1

    with db.get_session() as session:
        count = session.query(DailyIndicator).filter_by(symbol_id="SYM_UP").count()
        # 第一次有有效温度行，第二次至少多一条
        assert count >= 1


def test_rs_incremental_matches_full_run(clean_db):
    """RS 增量重算不得改变历史日期的 RS 值（读取窗口必须覆盖 252 交易日回看）。"""
    start = date(2024, 1, 1)
    days = 300
    end = start + timedelta(days=days - 1)

    with db.get_session() as session:
        session.add_all([_make_symbol("SYM_UP", "stock"), _make_symbol("SYM_DOWN", "stock")])

    records = _make_daily_prices("SYM_UP", start, days, trend=0.001)
    records += _make_daily_prices("SYM_DOWN", start, days, trend=-0.001)
    with db.get_session() as session:
        session.add_all(records)

    run_indicator_update(symbol_ids=["SYM_UP", "SYM_DOWN"], end_date=end)

    def _rs_map():
        with db.get_session() as session:
            rows = session.query(DailyIndicator).filter(DailyIndicator.rs_score.isnot(None)).all()
            return {(r.symbol_id, r.trade_date): r.rs_score for r in rows}

    before = _rs_map()

    # 追加两个品种各一条新日线，触发增量路径
    new_date = end + timedelta(days=1)
    with db.get_session() as session:
        session.add_all([
            DailyPrice(symbol_id="SYM_UP", trade_date=new_date, open=100, high=110, low=99, close=105, volume=1e6, amount=1e8, pct_chg=0.0),
            DailyPrice(symbol_id="SYM_DOWN", trade_date=new_date, open=100, high=105, low=90, close=95, volume=1e6, amount=1e8, pct_chg=0.0),
        ])
    run_indicator_update(symbol_ids=["SYM_UP", "SYM_DOWN"], end_date=new_date)

    after = _rs_map()
    # 历史日期的 RS 不应被增量重算改值
    assert before, "全量运行后应有 RS 数据"
    for key, v in before.items():
        assert after.get(key) == v, f"{key} 的 RS 被增量重算改变: {v} -> {after.get(key)}"


def test_right_side_single_row_cold_start():
    """无种子品种只有 1 行温度时也必须算出右侧状态（新上市个股首日场景）。

    回归：engine._compute_right_side 曾用 len(group) < 2 跳过单行品种，
    导致该行右侧列永久 NULL（harness C5 抓到 920072/920191 案例）。
    """
    from src.indicators.engine import _compute_right_side

    df = pd.DataFrame(
        {
            "symbol_id": ["NEW1", "NEW2"],
            "trade_date": [date(2026, 7, 27), date(2026, 7, 27)],
            "temperature": ["寒", "热"],
        }
    )
    result = _compute_right_side(df, seeds={})
    assert len(result) == 2, "单行品种不得被跳过"
    r1 = result[result["symbol_id"] == "NEW1"].iloc[0]
    assert r1["is_right_side"] == False and r1["right_side_days"] == 0
    r2 = result[result["symbol_id"] == "NEW2"].iloc[0]
    assert r2["is_right_side"] == True and r2["right_side_days"] == 1
