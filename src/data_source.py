# -*- coding: utf-8 -*-
"""数据源封装：只读读取 DSA DB + akshare 直采。"""

import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from sqlalchemy import create_engine

from src.config import (
    AKSHARE_SLEEP_MAX,
    AKSHARE_SLEEP_MIN,
    DSA_DB_PATH,
    INDUSTRY_SYNC_MAX_WORKERS,
    STOCK_SYNC_MAX_WORKERS,
)
from src.industry_cache import get_cached, load_cache, save_cache, set_cached

logger = logging.getLogger(__name__)


def to_sina_symbol(code: str) -> str:
    """将 6 位 A 股/指数代码转换为 sina 格式。"""
    base = code.strip().split(".")[0]
    if base.startswith(("43", "83", "87", "92", "89")):
        return f"bj{base}"
    if base.startswith(("6", "5", "68", "9", "11")):
        return f"sh{base}"
    return f"sz{base}"


SH_INDEX_CODES = {"000001", "000016", "000688", "000300", "000905", "000010", "000009"}


def to_sina_index_symbol(code: str) -> str:
    """宽基指数代码转 sina 格式（上海指数以 0/6/9 开头但仍需 sh 前缀）。"""
    base = code.strip().split(".")[0]
    if base.startswith(("3", "39")):
        return f"sz{base}"
    return f"sh{base}"


class DSAReader:
    """只读访问 DSA 的 stock_daily 表。"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or DSA_DB_PATH)
        self.engine = create_engine(f"sqlite:///{self.db_path}")

    def read_stock_daily(
        self,
        codes: Optional[List[str]] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> pd.DataFrame:
        """读取个股日线数据。"""
        query = "SELECT code, date, open, high, low, close, volume, amount, pct_chg FROM stock_daily WHERE 1=1"
        params = {}
        if codes:
            placeholders = ",".join(f":c{i}" for i in range(len(codes)))
            query += f" AND code IN ({placeholders})"
            params.update({f"c{i}": c for i, c in enumerate(codes)})
        if start_date:
            query += " AND date >= :start_date"
            params["start_date"] = start_date.isoformat()
        if end_date:
            query += " AND date <= :end_date"
            params["end_date"] = end_date.isoformat()

        df = pd.read_sql(query, self.engine, params=params)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df.rename(columns={"code": "symbol_id", "date": "trade_date"})
        return df

    def list_available_codes(self) -> List[str]:
        """获取 DSA 中已有日线的所有股票代码。"""
        df = pd.read_sql("SELECT DISTINCT code FROM stock_daily", self.engine)
        return df["code"].astype(str).tolist() if not df.empty else []


class AkShareFetcher:
    """基于 akshare 的数据获取器，内置限速与降级。"""

    def __init__(self, sleep_min: float = AKSHARE_SLEEP_MIN, sleep_max: float = AKSHARE_SLEEP_MAX):
        self.sleep_min = sleep_min
        self.sleep_max = sleep_max
        self._last_request_time: Optional[float] = None
        self._lock = threading.Lock()
        self._industry_cache: Dict[str, Optional[str]] = load_cache()

    def _rate_limit(self) -> None:
        """请求前限速（线程安全）。"""
        with self._lock:
            if self._last_request_time is not None:
                elapsed = time.time() - self._last_request_time
                if elapsed < self.sleep_min:
                    time.sleep(self.sleep_min - elapsed)
            time.sleep(random.uniform(self.sleep_min, self.sleep_max))
            self._last_request_time = time.time()

    @staticmethod
    def _pace_sleep(pace: Optional[Tuple[float, float]]) -> None:
        """线程内独立 jitter 限速：提供 pace 时替代全局 _rate_limit，用于并发拉取。"""
        if pace is not None:
            time.sleep(random.uniform(*pace))

    # ------------------------------------------------------------------
    # 品种列表
    # ------------------------------------------------------------------
    def get_all_stocks(self) -> pd.DataFrame:
        """
        获取 A 股全市场基础信息。
        优先东财（含所属行业），失败则降级为新浪基础列表 + 深交所/北交所行业映射。
        """
        try:
            return self._get_all_stocks_em()
        except Exception as exc:
            logger.warning("东财全市场股票列表失败: %s，降级为新浪/交易所基础列表", exc)
            return self._get_all_stocks_fallback()

    def _get_all_stocks_em(self) -> pd.DataFrame:
        import akshare as ak

        self._rate_limit()
        logger.info("[akshare] fetching stock_zh_a_spot_em")
        df = ak.stock_zh_a_spot_em()
        keep = {"代码", "名称", "所属行业", "总市值", "流通市值", "成交额"}
        df = df[[c for c in keep if c in df.columns]].copy()
        df = df.rename(
            columns={
                "代码": "symbol_id",
                "名称": "name",
                "所属行业": "industry_l1",
                "总市值": "total_cap",
                "流通市值": "float_cap",
                "成交额": "amount",
            }
        )
        return df

    # 证监会门类 -> 申万一级行业的精确映射（仅用于批量交易所列表的 fallback）
    _CSRC_TO_SW_EXACT = {
        "农、林、牧、渔业": "农林牧渔",
        "电力、热力、燃气及水生产和供应业": "公用事业",
        "建筑业": "建筑装饰",
        "批发和零售业": "商贸零售",
        "交通运输、仓储和邮政业": "交通运输",
        "住宿和餐饮业": "社会服务",
        "房地产业": "房地产",
        "租赁和商务服务业": "社会服务",
        "科学研究和技术服务业": "社会服务",
        "水利、环境和公共设施管理业": "环保",
        "居民服务、修理和其他服务业": "社会服务",
        "教育": "社会服务",
        "卫生和社会工作": "医药生物",
        "文化、体育和娱乐业": "传媒",
        "综合": "综合",
    }

    # 证监会细分行业 / 公司概况中的 "所属行业" -> 申万一级行业（按匹配优先级排序）
    _CSRC_DETAIL_TO_SW_RULES = [
        # 金融（更具体的在前）
        ("货币金融服务", "银行"),
        ("资本市场服务", "非银金融"),
        ("保险业", "非银金融"),
        ("其他金融业", "非银金融"),
        # 房地产
        ("房地产", "房地产"),
        # 消费
        ("酒、饮料和精制茶", "食品饮料"),
        ("食品", "食品饮料"),
        ("烟草", "食品饮料"),
        ("饮料", "食品饮料"),
        ("纺织", "纺织服饰"),
        ("服装", "纺织服饰"),
        ("皮革", "纺织服饰"),
        ("家具", "轻工制造"),
        ("木材", "轻工制造"),
        ("造纸", "轻工制造"),
        ("印刷", "轻工制造"),
        ("文教、工美、体育和娱乐用品", "轻工制造"),
        ("医药", "医药生物"),
        ("医疗", "医药生物"),
        ("汽车", "汽车"),
        ("家用电器", "家用电器"),
        ("美容护理", "美容护理"),
        # TMT
        ("软件和信息技术", "计算机"),
        ("互联网", "计算机"),
        ("计算机、通信和其他电子设备", "电子"),
        ("电信、广播电视和卫星传输", "通信"),
        ("广播电视", "传媒"),
        ("新闻出版", "传媒"),
        ("电影和影视", "传媒"),
        ("文化艺术", "传媒"),
        ("体育", "传媒"),
        ("娱乐", "传媒"),
        # 周期/制造
        ("电气机械和器材", "电力设备"),
        ("专用设备", "机械设备"),
        ("通用设备", "机械设备"),
        ("仪器仪表", "机械设备"),
        ("金属制品", "机械设备"),
        ("铁路、船舶、航空航天", "机械设备"),
        ("金属制品、机械和设备修理", "机械设备"),
        ("其他制造业", "机械设备"),
        ("化学纤维", "基础化工"),
        ("橡胶和塑料", "基础化工"),
        ("化学", "基础化工"),
        ("石油加工、炼焦和核燃料", "石油石化"),
        ("煤炭开采", "煤炭"),
        ("石油和天然气开采", "石油石化"),
        ("黑色金属矿", "钢铁"),
        ("有色金属矿", "有色金属"),
        ("非金属矿", "建筑材料"),
        ("黑色金属冶炼和压延", "钢铁"),
        ("有色金属冶炼和压延", "有色金属"),
        ("非金属矿物制品", "建筑材料"),
        ("废弃资源综合利用", "环保"),
        ("建筑", "建筑装饰"),
        # 能源/公用/交运
        ("电力、热力", "公用事业"),
        ("燃气", "公用事业"),
        ("水的生产", "公用事业"),
        ("水利管理", "公用事业"),
        ("生态保护和环境治理", "环保"),
        ("交通运输", "交通运输"),
        ("仓储", "交通运输"),
        ("邮政", "交通运输"),
        # 农业/采矿
        ("农业", "农林牧渔"),
        ("林业", "农林牧渔"),
        ("畜牧业", "农林牧渔"),
        ("渔业", "农林牧渔"),
        ("农、林、牧、渔", "农林牧渔"),
        ("开采辅助", "石油石化"),
        # 商业/服务
        ("批发", "商贸零售"),
        ("零售", "商贸零售"),
        ("住宿", "社会服务"),
        ("餐饮", "社会服务"),
        ("商务服务", "社会服务"),
        ("租赁", "社会服务"),
        ("专业技术服务", "社会服务"),
        ("研究和试验发展", "社会服务"),
        ("科技推广和应用服务", "社会服务"),
        ("公共设施管理", "社会服务"),
        ("居民服务", "社会服务"),
        ("修理", "社会服务"),
        ("教育", "社会服务"),
        ("卫生", "医药生物"),
        ("社会工作", "社会服务"),
        ("综合", "综合"),
    ]

    @staticmethod
    def _normalize_csrc_sector(raw: Optional[str]) -> Optional[str]:
        if pd.isna(raw):
            return None
        s = str(raw).strip()
        # 去除开头的 "A " / "J " 等门类代码前缀
        s = re.sub(r"^[A-Z]\s+", "", s)
        return AkShareFetcher._CSRC_TO_SW_EXACT.get(s)

    @classmethod
    def _map_csrc_detail_to_sw(cls, detail: Optional[str]) -> Optional[str]:
        if pd.isna(detail):
            return None
        s = str(detail).strip()
        for keyword, sw_name in cls._CSRC_DETAIL_TO_SW_RULES:
            if keyword in s:
                return sw_name
        return None

    def get_sw_industry_from_industry_change(self, code: str) -> Optional[str]:
        """通过巨潮行业变更接口查询单只股票的申万一级行业（行业门类）。"""
        import akshare as ak

        code = str(code).split(".")[0].strip()
        try:
            self._rate_limit()
            logger.debug("[akshare] fetching stock_industry_change_cninfo %s", code)
            df = ak.stock_industry_change_cninfo(symbol=code)
            if df.empty:
                return None
            sw = df[df["分类标准"].astype(str).str.contains("申银万国", na=False)]
            if sw.empty:
                return None
            sw = sw.sort_values("变更日期", ascending=False)
            name = sw.iloc[0]["行业门类"]
            if pd.isna(name):
                return None
            name = str(name).strip()
            return name if name else None
        except Exception as exc:
            logger.debug("查询 %s 申万行业变更失败: %s", code, exc)
            return None

    def get_sw_industry_from_profile(self, code: str) -> Optional[str]:
        """通过巨潮公司概况接口的 "所属行业" 映射到申万一级行业。"""
        import akshare as ak

        code = str(code).split(".")[0].strip()
        try:
            self._rate_limit()
            logger.debug("[akshare] fetching stock_profile_cninfo %s", code)
            df = ak.stock_profile_cninfo(symbol=code)
            if df.empty or "所属行业" not in df.columns:
                return None
            detail = df["所属行业"].iloc[0]
            return self._map_csrc_detail_to_sw(detail)
        except Exception as exc:
            logger.debug("查询 %s 公司概况失败: %s", code, exc)
            return None

    @staticmethod
    def _get_cninfo_mcode() -> str:
        """生成巨潮资讯接口所需的 Accept-Enckey（py_mini_racer，仅在主线程调用）。"""
        import akshare as ak
        import py_mini_racer

        js_path = Path(ak.__file__).parent / "data" / "cninfo.js"
        js_code = py_mini_racer.MiniRacer()
        js_code.eval(js_path.read_text(encoding="utf-8"))
        return js_code.call("getResCode1")

    @staticmethod
    def _fetch_single_industry_direct(code: str, mcode: str, sleep_min: float, sleep_max: float, retries: int = 3) -> Optional[str]:
        """
        直接调用巨潮资讯 HTTP 接口查询申万一级行业，失败则 fallback 到公司概况。
        不经过 akshare 的 py_mini_racer，可安全并发；对网络超时自动重试。
        """
        code = str(code).split(".")[0].strip()

        headers = {
            "Accept": "*/*",
            "Accept-Enckey": mcode,
            "Origin": "https://webapi.cninfo.com.cn",
            "Referer": "https://webapi.cninfo.com.cn/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
        }

        last_exc = None
        for attempt in range(retries):
            time.sleep(random.uniform(sleep_min, sleep_max))
            try:
                # 1) 行业变更接口：直接拿申万一级
                url = "https://webapi.cninfo.com.cn/api/stock/p_stock2110"
                params = {
                    "scode": code,
                    "sdate": "2000-01-01",
                    "edate": date.today().isoformat(),
                }
                r = requests.post(url, params=params, headers=headers, timeout=25)
                r.raise_for_status()
                records = r.json().get("records", [])
                sw_rows = [rec for rec in records if "申银万国" in str(rec.get("F002V", ""))]
                if sw_rows:
                    latest = max(sw_rows, key=lambda x: x.get("VARYDATE", "") or "")
                    name = latest.get("F004V")
                    if name:
                        return str(name).strip()

                # 2) 公司概况接口：证监会细分行业映射到申万一级
                url2 = "https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1133"
                params2 = {"scode": code}
                r2 = requests.post(url2, params=params2, headers=headers, timeout=25)
                r2.raise_for_status()
                records2 = r2.json().get("records", [])
                if records2:
                    detail = records2[0].get("F032V")
                    mapped = AkShareFetcher._map_csrc_detail_to_sw(detail)
                    if mapped:
                        return mapped
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                logger.debug("cninfo 查询 %s 超时/连接错误 (attempt %s/%s): %s", code, attempt + 1, retries, exc)
                continue
            except Exception as exc:
                last_exc = exc
                logger.debug("cninfo 查询 %s 异常 (attempt %s/%s): %s", code, attempt + 1, retries, exc)
                break

        return None

    def build_stock_industry_map(self, codes: List[str]) -> Dict[str, Optional[str]]:
        """
        为一批股票构建 {code: 申万一级行业名称} 映射。
        优先级：本地缓存 > 交易所批量列表 > 巨潮申万接口 > 巨潮公司概况。
        """
        import akshare as ak

        codes = [str(c).split(".")[0].strip() for c in codes]
        cache = self._industry_cache
        result: Dict[str, Optional[str]] = {}

        # 1. 本地缓存命中
        need_lookup = []
        for c in codes:
            cached = get_cached(cache, c)
            if cached is not None:
                result[c] = cached
            else:
                need_lookup.append(c)

        if not need_lookup:
            return result

        # 2. 批量交易所列表做精确映射（仅覆盖深市/北交所部分）
        batch_map: Dict[str, str] = {}
        def _map_exchange_industry(raw):
            # 深交所返回门类（如 "J 金融业"），北交所返回细分行业（如 "汽车制造业"）
            ind = self._normalize_csrc_sector(raw)
            if ind:
                return ind
            return self._map_csrc_detail_to_sw(raw)

        try:
            self._rate_limit()
            sz = ak.stock_info_sz_name_code()
            if "所属行业" in sz.columns and "A股代码" in sz.columns:
                for _, row in sz.iterrows():
                    c = str(row["A股代码"]).strip()
                    ind = _map_exchange_industry(row["所属行业"])
                    if ind:
                        batch_map[c] = ind
        except Exception as exc:
            logger.debug("深交所行业批量获取失败: %s", exc)
        try:
            self._rate_limit()
            bj = ak.stock_info_bj_name_code()
            if "所属行业" in bj.columns and "证券代码" in bj.columns:
                for _, row in bj.iterrows():
                    c = str(row["证券代码"]).strip()
                    ind = _map_exchange_industry(row["所属行业"])
                    if ind:
                        batch_map[c] = ind
        except Exception as exc:
            logger.debug("北交所行业批量获取失败: %s", exc)

        need_fetch = []
        for c in need_lookup:
            if c in batch_map:
                result[c] = batch_map[c]
                set_cached(cache, c, batch_map[c])
            else:
                need_fetch.append(c)

        # 3. 并发逐个查询巨潮接口（多 worker，靠信号量控制并发度）
        if need_fetch:
            logger.info("需逐个查询行业的个股: %s 只", len(need_fetch))
            workers = max(1, INDUSTRY_SYNC_MAX_WORKERS)
            sem = threading.Semaphore(workers)

            # mcode 会过期，用一个可在线程间读取的 holder，主线程定时刷新
            mcode_lock = threading.Lock()
            mcode_holder = {
                "mcode": self._get_cninfo_mcode(),
                "generated_at": time.time(),
            }
            MCODE_REFRESH_SECONDS = 600.0

            def _refresh_mcode_if_needed() -> None:
                with mcode_lock:
                    if time.time() - mcode_holder["generated_at"] > MCODE_REFRESH_SECONDS:
                        logger.info("刷新 cninfo mcode...")
                        mcode_holder["mcode"] = self._get_cninfo_mcode()
                        mcode_holder["generated_at"] = time.time()

            def _current_mcode() -> str:
                with mcode_lock:
                    return mcode_holder["mcode"]

            def _fetch_one(c: str) -> tuple:
                with sem:
                    return c, self._fetch_single_industry_direct(c, _current_mcode(), 0.02, 0.1)

            with ThreadPoolExecutor(max_workers=workers) as executor:
                for i, (c, industry) in enumerate(executor.map(_fetch_one, need_fetch), start=1):
                    result[c] = industry
                    set_cached(cache, c, industry)
                    if i % 500 == 0:
                        logger.info("行业查询进度: %s/%s", i, len(need_fetch))
                        _refresh_mcode_if_needed()
                        save_cache(cache)
                save_cache(cache)

        save_cache(cache)
        return result

    def _get_all_stocks_fallback(self) -> pd.DataFrame:
        import akshare as ak

        self._rate_limit()
        logger.info("[akshare] fetching stock_info_a_code_name")
        df = ak.stock_info_a_code_name().rename(columns={"code": "symbol_id", "name": "name"})

        codes = df["symbol_id"].astype(str).tolist()
        industry_map = self.build_stock_industry_map(codes)
        df["industry_l1"] = df["symbol_id"].map(industry_map)
        return df

    # ------------------------------------------------------------------
    # 行业分类
    # ------------------------------------------------------------------
    def get_sw_industry_info(self) -> pd.DataFrame:
        """获取申万一级与二级行业信息，返回统一格式。"""
        import akshare as ak

        self._rate_limit()
        first = ak.sw_index_first_info()
        first = first[["行业代码", "行业名称"]].copy()
        first["level"] = 1
        first["parent_name"] = "A_SHARE"

        self._rate_limit()
        second = ak.sw_index_second_info()
        second = second[["行业代码", "行业名称", "上级行业"]].copy()
        second["level"] = 2
        second = second.rename(columns={"上级行业": "parent_name"})

        df = pd.concat([first, second], ignore_index=True)
        df["行业代码"] = df["行业代码"].str.replace(".SI", "", regex=False)
        return df

    def get_concept_index_daily(
        self, name: str, start_date: date, end_date: date,
        pace: Optional[Tuple[float, float]] = None,
    ) -> pd.DataFrame:
        """获取同花顺概念指数日线（按概念中文名）。pace 提供时用线程内 jitter 限速（可并发）。"""
        import akshare as ak

        if pace is not None:
            self._pace_sleep(pace)
        else:
            self._rate_limit()
        logger.debug("[akshare] fetching stock_board_concept_index_ths %s", name)
        df = ak.stock_board_concept_index_ths(
            symbol=name,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
        if df.empty:
            return df
        # 中文列名归一化（akshare 返回：日期/开盘价/最高价/最低价/收盘价/成交量/成交额）
        col_map = {
            "日期": "trade_date",
            "开盘价": "open",
            "最高价": "high",
            "最低价": "low",
            "收盘价": "close",
            "成交量": "volume",
            "成交额": "amount",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "trade_date" not in df.columns:
            logger.warning("ths 概念 %s 返回列名异常: %s", name, df.columns.tolist())
            return pd.DataFrame(columns=["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"])
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
        if df.empty:
            return df
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["pct_chg"] = df["close"].pct_change() * 100
        return df[["trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]

    def get_concept_list_ths(self) -> pd.DataFrame:
        """获取同花顺概念板块列表（仅节点，日线可能暂缺）。"""
        import akshare as ak

        self._rate_limit()
        try:
            df = ak.stock_board_concept_name_ths()
            return df.rename(columns={"code": "concept_code", "name": "concept_name"})
        except Exception as exc:
            logger.warning("同花顺概念列表获取失败: %s", exc)
            return pd.DataFrame(columns=["concept_code", "concept_name"])

    # ------------------------------------------------------------------
    # 日线行情
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Baostock 个股日线（无 py_mini_racer，适合全市场批量）
    # ------------------------------------------------------------------
    _baostock_login_lock = threading.Lock()
    _baostock_logged_in = False

    @classmethod
    def _ensure_baostock_login(cls) -> None:
        import baostock as bs

        with cls._baostock_login_lock:
            if not cls._baostock_logged_in:
                res = bs.login()
                if res.error_code != "0":
                    raise RuntimeError(f"baostock 登录失败: {res.error_msg}")
                cls._baostock_logged_in = True

    @staticmethod
    def _to_baostock_code(raw_code: str) -> str:
        base = str(raw_code).split(".")[0].strip()
        if base.startswith(("6", "68", "9", "11", "5")):
            return f"sh.{base}"
        return f"sz.{base}"

    def get_stock_daily_baostock(
        self, code: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        """通过 baostock 获取个股前复权日线（线程不安全，需外部串行）。"""
        import baostock as bs

        self._ensure_baostock_login()
        bscode = self._to_baostock_code(code)
        rs = bs.query_history_k_data_plus(
            bscode,
            "date,open,high,low,close,volume,amount,pctChg",
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="2",
        )
        data = []
        while rs.error_code == "0" and rs.next():
            data.append(rs.get_row_data())
        if not data:
            return pd.DataFrame(
                columns=["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
            )
        df = pd.DataFrame(data, columns=["trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"])
        df["symbol_id"] = code
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]

    def get_stock_daily_sina(
        self, code: str, start_date: date, end_date: date,
        pace: Optional[Tuple[float, float]] = None,
    ) -> pd.DataFrame:
        import akshare as ak

        if pace is not None:
            self._pace_sleep(pace)
        else:
            self._rate_limit()
        symbol = to_sina_symbol(code)
        logger.debug("[akshare] fetching stock_zh_a_daily %s", symbol)
        df = ak.stock_zh_a_daily(
            symbol=symbol,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            adjust="qfq",
        )
        if df.empty:
            return df
        df = df.rename(
            columns={
                "date": "trade_date",
                "日期": "trade_date",
                "open": "open",
                "开盘": "open",
                "high": "high",
                "最高": "high",
                "low": "low",
                "最低": "low",
                "close": "close",
                "收盘": "close",
                "volume": "volume",
                "成交量": "volume",
                "amount": "amount",
                "成交额": "amount",
            }
        )
        if "trade_date" not in df.columns:
            logger.warning("新浪个股 %s 返回列名异常: %s", symbol, df.columns.tolist())
            return pd.DataFrame(columns=["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"])
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df["symbol_id"] = code
        df["pct_chg"] = df["close"].pct_change() * 100
        return df[["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]

    def get_market_daily_tinyshare(self, trade_dates: List[date]) -> pd.DataFrame:
        """tinyshare 全市场按交易日批量日线（pro.daily + pro.adj_factor 前复权化）。

        每次调用拉全市场单日（~0.5s），补缺窗口（几到几十个交易日）比逐股接口快几个数量级。
        复权口径：以窗口内最后一个交易日的复权因子为锚（与 akshare qfq「最新价=真实价」一致）。
        成交量/成交额单位与库内现有数据一致（手 / 千元），不做换算。
        """
        import os

        import tinyshare as ts

        token = os.environ.get("TINYSHARE_TOKEN")
        if not token:
            raise RuntimeError("TINYSHARE_TOKEN 未设置（.env 缺失？）")
        ts.set_token(token)
        pro = ts.pro_api()

        frames, factors = [], []
        for d in sorted(trade_dates):
            ds = d.strftime("%Y%m%d")
            try:
                df = pro.daily(trade_date=ds)
                if df is not None and not df.empty:
                    frames.append(df)
                af = pro.adj_factor(trade_date=ds)
                if af is not None and not af.empty:
                    factors.append(af)
            except Exception as exc:
                logger.warning("tinyshare 全市场日线 %s 失败: %s", ds, exc)
            time.sleep(0.3)
        if not frames:
            return pd.DataFrame(
                columns=["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
            )

        daily = pd.concat(frames, ignore_index=True)
        daily["symbol_id"] = daily["ts_code"].str.replace(r"\.(SZ|SH|BJ)$", "", regex=True)
        # trade_date 可能混 int/str，统一 astype(str) 再解析，并丢弃解析失败的行
        # （否则后续 sort_values/比较会在 date 与 float(NaN) 之间抛 TypeError）
        daily["trade_date"] = pd.to_datetime(daily["trade_date"].astype(str), format="%Y%m%d", errors="coerce").dt.date
        daily = daily.dropna(subset=["trade_date"])

        if factors:
            fac = pd.concat(factors, ignore_index=True)
            fac["symbol_id"] = fac["ts_code"].str.replace(r"\.(SZ|SH|BJ)$", "", regex=True)
            fac["trade_date"] = pd.to_datetime(fac["trade_date"].astype(str), format="%Y%m%d", errors="coerce").dt.date
            fac = fac.dropna(subset=["trade_date"])
            fac = fac.sort_values("trade_date")
            anchor = fac.groupby("symbol_id")["adj_factor"].last().rename("anchor_factor")
            daily = daily.merge(
                fac[["symbol_id", "trade_date", "adj_factor"]],
                on=["symbol_id", "trade_date"],
                how="left",
            ).merge(anchor, on="symbol_id", how="left")
            ratio = daily["adj_factor"] / daily["anchor_factor"]
            for col in ["open", "high", "low", "close"]:
                daily[col] = daily[col] * ratio.fillna(1.0)
            daily = daily.drop(columns=["adj_factor", "anchor_factor"])

        daily = daily.rename(columns={"vol": "volume"})
        for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
            daily[col] = pd.to_numeric(daily[col], errors="coerce")
        return daily[["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]

    def get_stock_daily_em(
        self, code: str, start_date: date, end_date: date,
        pace: Optional[Tuple[float, float]] = None,
        retries: int = 3,
    ) -> pd.DataFrame:
        """东财个股前复权日线（stock_zh_a_hist，按区间拉取，比 sina 全量接口轻得多，适合批量补缺）。"""
        import akshare as ak

        last_exc = None
        for attempt in range(retries):
            if pace is not None:
                self._pace_sleep(pace)
            else:
                self._rate_limit()
            try:
                df = ak.stock_zh_a_hist(
                    symbol=str(code).split(".")[0].strip(),
                    period="daily",
                    start_date=start_date.strftime("%Y%m%d"),
                    end_date=end_date.strftime("%Y%m%d"),
                    adjust="qfq",
                )
                break
            except Exception as exc:
                last_exc = exc
                logger.debug("东财个股 %s 第 %s/%s 次失败: %s", code, attempt + 1, retries, exc)
                time.sleep(1 + attempt)
        else:
            raise last_exc
        if df is None or df.empty:
            return pd.DataFrame(
                columns=["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
            )
        df = df.rename(
            columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
                "涨跌幅": "pct_chg",
            }
        )
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df["symbol_id"] = str(code).split(".")[0].strip()
        for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]

    def get_index_daily_sina(
        self, code: str, start_date: date, end_date: date,
        pace: Optional[Tuple[float, float]] = None,
    ) -> pd.DataFrame:
        import akshare as ak

        if pace is not None:
            self._pace_sleep(pace)
        else:
            self._rate_limit()
        symbol = to_sina_index_symbol(code)
        logger.debug("[akshare] fetching stock_zh_index_daily %s", symbol)
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df.empty:
            return df
        df = df.rename(
            columns={
                "date": "trade_date",
                "日期": "trade_date",
                "open": "open",
                "开盘": "open",
                "high": "high",
                "最高": "high",
                "low": "low",
                "最低": "low",
                "close": "close",
                "收盘": "close",
                "volume": "volume",
                "成交量": "volume",
            }
        )
        if "trade_date" not in df.columns and "trade_date" in df.index.names:
            df = df.reset_index()
        if "trade_date" not in df.columns:
            logger.warning("新浪指数 %s 返回列名异常: %s", symbol, df.columns.tolist())
            return pd.DataFrame(columns=["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"])
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
        if df.empty:
            return df
        df["symbol_id"] = code
        df["amount"] = df["volume"] * df["close"]
        df["pct_chg"] = df["close"].pct_change() * 100
        return df[["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]

    def get_industry_index_daily(
        self, symbol: str, start_date: date, end_date: date,
        pace: Optional[Tuple[float, float]] = None,
    ) -> pd.DataFrame:
        """获取申万行业指数日线。pace 提供时用线程内 jitter 限速（可并发）。"""
        import akshare as ak

        if pace is not None:
            self._pace_sleep(pace)
        else:
            self._rate_limit()
        logger.debug("[akshare] fetching index_hist_sw %s", symbol)
        try:
            df = ak.index_hist_sw(symbol=symbol, period="day")
        except Exception as exc:
            logger.info("index_hist_sw %s 失败（转 tinyshare 兜底）: %s", symbol, exc)
            df = pd.DataFrame()
        if not df.empty:
            # 列：代码, 日期, 收盘, 开盘, 最高, 最低, 成交量, 成交额
            df = df.rename(
                columns={
                    "代码": "symbol_id",
                    "日期": "trade_date",
                    "开盘": "open",
                    "收盘": "close",
                    "最高": "high",
                    "最低": "low",
                    "成交量": "volume",
                    "成交额": "amount",
                }
            )
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
        if df.empty:
            # 申万官网发布时间滞后（上午常缺最近一个交易日），用 tinyshare sw_daily 兜底。
            # 已逐日核对两者数值完全一致（ratio=1.0），可安全混用。
            df = self._get_industry_index_daily_tinyshare(symbol, start_date, end_date)
            if df.empty:
                return df
        else:
            df["pct_chg"] = df["close"].pct_change() * 100
            df = df[["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]
        return df

    def _get_industry_index_daily_tinyshare(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        """申万行业指数日线备选源：tinyshare sw_daily（需 TINYSHARE_TOKEN）。"""
        import os

        token = os.environ.get("TINYSHARE_TOKEN")
        if not token:
            return pd.DataFrame()
        try:
            import tinyshare as ts

            ts.set_token(token)
            pro = ts.pro_api()
            df = pro.sw_daily(
                ts_code=f"{symbol}.SI",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            )
        except Exception as exc:
            logger.warning("tinyshare sw_daily %s 失败: %s", symbol, exc)
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"vol": "volume", "pct_change": "pct_chg"})
        df["symbol_id"] = symbol
        df["trade_date"] = pd.to_datetime(df["trade_date"].astype(str), format="%Y%m%d", errors="coerce").dt.date
        df = df.dropna(subset=["trade_date"])
        for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["symbol_id", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]
