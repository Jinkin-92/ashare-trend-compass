# -*- coding: utf-8 -*-
"""品种分类树构建与维护。"""

import logging
from datetime import date
from typing import List

import pandas as pd

from src.data_source import AkShareFetcher

logger = logging.getLogger(__name__)

NODE_TYPE_ROOT = "root"
NODE_TYPE_INDEX = "index"
NODE_TYPE_INDUSTRY_L1 = "industry_l1"
NODE_TYPE_INDUSTRY_L2 = "industry_l2"
NODE_TYPE_CONCEPT = "concept"
NODE_TYPE_STOCK = "stock"
NODE_TYPE_STOCK_GROUP = "stock_group"

ROOT_ID = "A_SHARE"
UNKNOWN_INDUSTRY_ID = "IND_UNKNOWN"
CONCEPT_ROOT_ID = "CONCEPT_ROOT"


class ClassificationBuilder:
    """构建 symbols 分类树。"""

    def __init__(self, fetcher: AkShareFetcher):
        self.fetcher = fetcher

    def build(self) -> pd.DataFrame:
        """构建当前 A 股品种树。"""
        records: List[dict] = []

        # 1. 根节点
        records.append(
            {
                "symbol_id": ROOT_ID,
                "name": "A股",
                "node_type": NODE_TYPE_ROOT,
                "parent_id": None,
                "is_leaf": False,
            }
        )

        # 2. 主要宽基指数
        index_symbols = [
            ("IDX_000001", "上证指数", "000001"),
            ("IDX_399001", "深证成指", "399001"),
            ("IDX_399006", "创业板指", "399006"),
            ("IDX_000688", "科创50", "000688"),
            ("IDX_000016", "上证50", "000016"),
            ("IDX_000300", "沪深300", "000300"),
            ("IDX_000905", "中证500", "000905"),
        ]
        for sid, name, _code in index_symbols:
            records.append(
                {
                    "symbol_id": sid,
                    "name": name,
                    "node_type": NODE_TYPE_INDEX,
                    "parent_id": ROOT_ID,
                    "is_leaf": True,
                }
            )

        # 3. 申万行业一级/二级
        industry_map = {}  # name -> symbol_id
        sw_df = self.fetcher.get_sw_industry_info()
        l1_rows = sw_df[sw_df["level"] == 1]
        l2_rows = sw_df[sw_df["level"] == 2]

        for _, row in l1_rows.iterrows():
            sid = f"SW_{row['行业代码']}"
            industry_map[row["行业名称"]] = sid
            records.append(
                {
                    "symbol_id": sid,
                    "name": row["行业名称"],
                    "node_type": NODE_TYPE_INDUSTRY_L1,
                    "parent_id": ROOT_ID,
                    "is_leaf": False,
                }
            )

        parent_name_to_id = {row["行业名称"]: f"SW_{row['行业代码']}" for _, row in l1_rows.iterrows()}
        for _, row in l2_rows.iterrows():
            sid = f"SW_{row['行业代码']}"
            parent_id = parent_name_to_id.get(row["parent_name"], ROOT_ID)
            records.append(
                {
                    "symbol_id": sid,
                    "name": row["行业名称"],
                    "node_type": NODE_TYPE_INDUSTRY_L2,
                    "parent_id": parent_id,
                    "is_leaf": True,
                }
            )

        # 4. 概念板块（节点占位，日线后续单独处理）
        concept_root_added = False
        try:
            concepts = self.fetcher.get_concept_list_ths()
            if not concepts.empty:
                records.append(
                    {
                        "symbol_id": CONCEPT_ROOT_ID,
                        "name": "概念板块",
                        "node_type": NODE_TYPE_CONCEPT,
                        "parent_id": ROOT_ID,
                        "is_leaf": False,
                    }
                )
                concept_root_added = True
                for _, row in concepts.head(500).iterrows():  # MVP 先取前 500
                    sid = f"CONCEPT_{row['concept_code']}"
                    records.append(
                        {
                            "symbol_id": sid,
                            "name": row["concept_name"],
                            "node_type": NODE_TYPE_CONCEPT,
                            "parent_id": CONCEPT_ROOT_ID,
                            "is_leaf": True,
                        }
                    )
        except Exception as exc:
            logger.warning("概念板块列表获取失败: %s", exc)

        # 5. 全市场个股
        try:
            stocks = self.fetcher.get_all_stocks()
            if not stocks.empty:
                # 未匹配行业的兜底节点
                records.append(
                    {
                        "symbol_id": UNKNOWN_INDUSTRY_ID,
                        "name": "未分类个股",
                        "node_type": NODE_TYPE_STOCK_GROUP,
                        "parent_id": ROOT_ID,
                        "is_leaf": False,
                    }
                )

                for _, row in stocks.iterrows():
                    code = str(row["symbol_id"]).strip()
                    name = str(row["name"]).strip()
                    industry = row.get("industry_l1")
                    parent_id = UNKNOWN_INDUSTRY_ID
                    if pd.notna(industry):
                        parent_id = industry_map.get(str(industry).strip(), UNKNOWN_INDUSTRY_ID)

                    record = {
                        "symbol_id": code,
                        "name": name,
                        "node_type": NODE_TYPE_STOCK,
                        "parent_id": parent_id,
                        "is_leaf": True,
                    }
                    float_cap = row.get("float_cap")
                    if pd.notna(float_cap):
                        record["market_cap_float"] = float(float_cap)
                    records.append(record)
        except Exception as exc:
            logger.error("构建个股分类失败: %s", exc)

        df = pd.DataFrame(records)
        if df.empty:
            return df

        # 去重，保留先出现的（上面按优先级添加）
        df = df.drop_duplicates(subset=["symbol_id"], keep="first")
        return df

    def get_required_dates(self, years: int = 2) -> tuple:
        """返回数据拉取的起止日期。"""
        today = date.today()
        start = date(today.year - years, today.month, today.day)
        return start, today
