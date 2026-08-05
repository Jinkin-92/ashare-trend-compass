# -*- coding: utf-8 -*-
"""数据源封装测试。"""

import threading
from unittest.mock import MagicMock, patch

import pandas as pd

from src.data_source import AkShareFetcher, to_sina_index_symbol, to_sina_symbol


def _fetcher() -> AkShareFetcher:
    """返回一个不做网络睡眠的 fetcher，用于测试。"""
    return AkShareFetcher(sleep_min=0.0, sleep_max=0.0)


def _industry_change_response(records):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"records": records}
    return response


def _profile_response(detail):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"records": [{"F032V": detail}]}
    return response


def test_to_sina_symbol():
    assert to_sina_symbol("600000") == "sh600000"
    assert to_sina_symbol("000001") == "sz000001"
    assert to_sina_symbol("300001") == "sz300001"
    assert to_sina_symbol("830009") == "bj830009"
    assert to_sina_symbol("600000.SH") == "sh600000"


def test_to_sina_index_symbol():
    assert to_sina_index_symbol("000001") == "sh000001"
    assert to_sina_index_symbol("399001") == "sz399001"


@patch("src.data_source.requests.post")
def test_fetch_single_industry_direct_returns_latest_sw(mock_post):
    mock_post.return_value = _industry_change_response(
        [
            {"F002V": "申银万国行业分类标准", "F004V": "银行", "VARYDATE": "2020-01-01"},
            {"F002V": "申银万国行业分类标准", "F004V": "非银金融", "VARYDATE": "2024-01-01"},
            {"F002V": "证监会行业分类标准", "F004V": "金融业", "VARYDATE": "2024-01-01"},
        ]
    )
    result = AkShareFetcher._fetch_single_industry_direct("600000", "fake-mcode", 0, 0)
    assert result == "非银金融"


@patch("src.data_source.requests.post")
def test_fetch_single_industry_direct_returns_none_when_no_records(mock_post):
    mock_post.return_value = _industry_change_response([])
    result = AkShareFetcher._fetch_single_industry_direct("600000", "fake-mcode", 0, 0)
    assert result is None


@patch("src.data_source.save_cache")
@patch("src.data_source.load_cache")
@patch("src.data_source.requests.post")
def test_build_stock_industry_map_uses_cache(mock_post, mock_load, mock_save):
    mock_load.return_value = {"000001": "房地产"}
    fetcher = _fetcher()
    result = fetcher.build_stock_industry_map(["000001"])
    assert result == {"000001": "房地产"}
    mock_post.assert_not_called()
    mock_save.assert_not_called()


@patch("src.data_source.save_cache")
@patch("src.data_source.load_cache")
@patch("akshare.stock_info_sz_name_code")
@patch("akshare.stock_info_bj_name_code")
@patch("src.data_source.requests.post")
def test_build_stock_industry_map_falls_back_to_profile_when_no_sw(
    mock_post, mock_bj, mock_sz, mock_load, mock_save
):
    mock_load.return_value = {}
    mock_sz.return_value = pd.DataFrame(
        columns=["A股代码", "A股简称", "所属行业"]
    )
    mock_bj.return_value = pd.DataFrame(
        columns=["证券代码", "证券简称", "所属行业"]
    )

    def _post_side_effect(url, **kwargs):
        if "p_stock2110" in url:
            return _industry_change_response(
                [{"F002V": "证监会行业分类标准", "F004V": "金融业", "VARYDATE": "2024-01-01"}]
            )
        return _profile_response("货币金融服务")

    mock_post.side_effect = _post_side_effect
    fetcher = _fetcher()
    result = fetcher.build_stock_industry_map(["600000"])
    assert result == {"600000": "银行"}


@patch("src.data_source.save_cache")
@patch("src.data_source.load_cache")
@patch("akshare.stock_info_sz_name_code")
@patch("akshare.stock_info_bj_name_code")
@patch("src.data_source.requests.post")
def test_build_stock_industry_map_uses_exact_batch_mapping(
    mock_post, mock_bj, mock_sz, mock_load, mock_save
):
    mock_load.return_value = {}
    mock_sz.return_value = pd.DataFrame(
        [
            {"A股代码": "000001", "A股简称": "深发展", "所属行业": "J 房地产业"},
        ]
    )
    mock_bj.return_value = pd.DataFrame(
        columns=["证券代码", "证券简称", "所属行业"]
    )
    fetcher = _fetcher()
    result = fetcher.build_stock_industry_map(["000001"])
    assert result == {"000001": "房地产"}
    mock_post.assert_not_called()


def test_rate_limit_is_thread_safe():
    fetcher = AkShareFetcher(sleep_min=0.0, sleep_max=0.0)
    errors = []

    def worker():
        try:
            fetcher._rate_limit()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
