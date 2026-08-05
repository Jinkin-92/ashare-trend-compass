# -*- coding: utf-8 -*-
"""品种分类树构建测试。"""

import pandas as pd
from unittest.mock import MagicMock

from src.classification import (
    ClassificationBuilder,
    NODE_TYPE_INDEX,
    NODE_TYPE_INDUSTRY_L1,
    NODE_TYPE_INDUSTRY_L2,
    NODE_TYPE_STOCK,
    NODE_TYPE_STOCK_GROUP,
    ROOT_ID,
    UNKNOWN_INDUSTRY_ID,
)


def _mock_fetcher() -> MagicMock:
    fetcher = MagicMock()
    # get_sw_industry_info 内部会去掉 .SI，因此 mock 返回已处理好的行业代码
    fetcher.get_sw_industry_info.return_value = pd.DataFrame(
        [
            {"行业代码": "801180", "行业名称": "房地产", "level": 1, "parent_name": "A_SHARE"},
            {"行业代码": "801010", "行业名称": "农林牧渔", "level": 1, "parent_name": "A_SHARE"},
            {"行业代码": "801181", "行业名称": "房地产开发", "level": 2, "parent_name": "房地产"},
        ]
    )
    fetcher.get_concept_list_ths.return_value = pd.DataFrame(
        columns=["concept_code", "concept_name"]
    )
    return fetcher


def test_build_includes_root_and_indices():
    fetcher = _mock_fetcher()
    fetcher.get_all_stocks.return_value = pd.DataFrame(
        columns=["symbol_id", "name", "industry_l1"]
    )
    df = ClassificationBuilder(fetcher).build()

    assert ROOT_ID in df["symbol_id"].values
    assert "IDX_000001" in df["symbol_id"].values
    index_row = df[df["symbol_id"] == "IDX_000001"].iloc[0]
    assert index_row["node_type"] == NODE_TYPE_INDEX
    assert index_row["parent_id"] == ROOT_ID


def test_build_maps_stocks_to_sw_industry():
    fetcher = _mock_fetcher()
    fetcher.get_all_stocks.return_value = pd.DataFrame(
        [
            {"symbol_id": "000001", "name": "平安银行", "industry_l1": "房地产"},
            {"symbol_id": "000002", "name": "万科A", "industry_l1": "房地产"},
            {"symbol_id": "600000", "name": "浦发银行", "industry_l1": "银行"},
        ]
    )
    df = ClassificationBuilder(fetcher).build()

    sw_real_estate = "SW_801180"
    assert sw_real_estate in df["symbol_id"].values
    real_estate_row = df[df["symbol_id"] == sw_real_estate].iloc[0]
    assert real_estate_row["node_type"] == NODE_TYPE_INDUSTRY_L1
    assert real_estate_row["parent_id"] == ROOT_ID

    assert df[df["symbol_id"] == "000001"].iloc[0]["parent_id"] == sw_real_estate
    assert df[df["symbol_id"] == "000002"].iloc[0]["parent_id"] == sw_real_estate
    # "银行" 不在 mock 的申万列表里，应兜底到 UNKNOWN
    assert df[df["symbol_id"] == "600000"].iloc[0]["parent_id"] == UNKNOWN_INDUSTRY_ID


def test_build_unknown_industry_group_exists():
    fetcher = _mock_fetcher()
    fetcher.get_all_stocks.return_value = pd.DataFrame(
        [{"symbol_id": "600000", "name": "浦发银行", "industry_l1": "银行"}]
    )
    df = ClassificationBuilder(fetcher).build()

    unknown = df[df["symbol_id"] == UNKNOWN_INDUSTRY_ID]
    assert not unknown.empty
    assert unknown.iloc[0]["node_type"] == NODE_TYPE_STOCK_GROUP
    assert unknown.iloc[0]["parent_id"] == ROOT_ID


def test_build_industry_l2_parent_resolved():
    fetcher = _mock_fetcher()
    fetcher.get_all_stocks.return_value = pd.DataFrame(
        columns=["symbol_id", "name", "industry_l1"]
    )
    df = ClassificationBuilder(fetcher).build()

    l2_id = "SW_801181"
    assert l2_id in df["symbol_id"].values
    l2_row = df[df["symbol_id"] == l2_id].iloc[0]
    assert l2_row["node_type"] == NODE_TYPE_INDUSTRY_L2
    assert l2_row["parent_id"] == "SW_801180"


def test_build_preserves_market_cap_float():
    fetcher = _mock_fetcher()
    fetcher.get_all_stocks.return_value = pd.DataFrame(
        [
            {
                "symbol_id": "000001",
                "name": "平安银行",
                "industry_l1": "房地产",
                "float_cap": 1234.5,
            }
        ]
    )
    df = ClassificationBuilder(fetcher).build()
    row = df[df["symbol_id"] == "000001"].iloc[0]
    assert row["market_cap_float"] == 1234.5
    assert row["node_type"] == NODE_TYPE_STOCK
