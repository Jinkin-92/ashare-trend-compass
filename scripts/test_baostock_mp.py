# -*- coding: utf-8 -*-
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date


def fetch(c):
    import baostock as bs
    bs.login()
    rs = bs.query_history_k_data_plus(
        c,
        "date,open,high,low,close,volume,amount,pctChg",
        start_date="2024-01-01",
        end_date="2026-07-07",
        frequency="d",
        adjustflag="2",
    )
    data = []
    while rs.error_code == "0" and rs.next():
        data.append(rs.get_row_data())
    bs.logout()
    return c, len(data)


def main():
    codes = [
        "sh.600000",
        "sz.000001",
        "sz.000333",
        "sh.600519",
        "sz.300750",
        "sh.688981",
        "sh.601318",
        "sz.002594",
        "sh.600036",
        "sz.000858",
    ]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(fetch, codes))
    print(results, "time", time.time() - t0)


if __name__ == "__main__":
    main()
