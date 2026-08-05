# -*- coding: utf-8 -*-
"""SQLAlchemy ORM 模型。"""

from sqlalchemy import Boolean, Column, Date, Float, Integer, String
from sqlalchemy import UniqueConstraint, Index

from src.db import Base


class Symbol(Base):
    """品种基础信息（分类树）。"""

    __tablename__ = "symbols"

    symbol_id = Column(String(32), primary_key=True)
    name = Column(String(100), nullable=False)
    node_type = Column(String(32), nullable=False, index=True)
    parent_id = Column(String(32), index=True)
    is_leaf = Column(Boolean, nullable=False, default=False)
    market_cap_float = Column(Float)  # 仅个股有效
    l2_industry_id = Column(String(32), index=True)  # 申万二级行业 ID（仅 stock 有效）
    data_status = Column(String(16))  # ok / no_data 等

    __table_args__ = (
        Index("ix_symbol_parent", "parent_id", "node_type"),
        Index("ix_symbol_l2", "l2_industry_id"),
    )


class DailyPrice(Base):
    """日线行情（个股来自 baostock/sina，行业/概念/指数来自 akshare）。"""

    __tablename__ = "daily_price"

    symbol_id = Column(String(32), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    amount = Column(Float)
    pct_chg = Column(Float)
    adj_factor = Column(Float)  # 复权因子（后复权时 close * adj_factor = 真实价）

    __table_args__ = (
        Index("ix_daily_price_symbol_date", "symbol_id", "trade_date"),
    )


class DailyIndicator(Base):
    """每日指标结果。"""

    __tablename__ = "daily_indicator"

    symbol_id = Column(String(32), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    temperature = Column(String(8), nullable=False, index=True)
    temperature_score = Column(Float)
    rs_score = Column(Integer)
    rs_score_prev_1d = Column(Integer)
    rs_score_prev_5d = Column(Integer)
    rs_score_trend = Column(String(16))
    # 右侧状态：NULL 表示"尚未计算"（温度/RS 写入时不填），由右侧状态机显式写入；
    # 增量续算依赖此约定识别种子行，不要加 default。
    is_right_side = Column(Boolean, index=True)
    right_side_days = Column(Integer)
    right_side_entry_temp = Column(String(8))

    __table_args__ = (
        Index("ix_indicator_date_temp", "trade_date", "temperature"),
        Index("ix_indicator_date_rs", "trade_date", "rs_score"),
    )


class WatchPool(Base):
    """自选池。"""

    __tablename__ = "watch_pools"

    pool_id = Column(String(32), primary_key=True)
    pool_name = Column(String(100), nullable=False)


class WatchPoolItem(Base):
    """自选池成分。"""

    __tablename__ = "watch_pool_items"

    pool_id = Column(String(32), primary_key=True)
    symbol_id = Column(String(32), primary_key=True)


class PoolRsScore(Base):
    """自选池维度的相对强度。"""

    __tablename__ = "pool_rs_score"

    pool_id = Column(String(32), primary_key=True)
    symbol_id = Column(String(32), primary_key=True)
    trade_date = Column(Date, primary_key=True)
    rs_score = Column(Integer)
