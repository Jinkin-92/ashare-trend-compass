# -*- coding: utf-8 -*-
"""每日盘后报告：数据更新 + 策略信号 + 持仓管理。

流程：
1. 运行 daily_update.py 拉取最新数据 + 指标重算
2. 加载所有 L2 行业温度数据
3. 生成交易信号（买入/加仓/减持/清仓/关注）
4. 输出 WeChat 日报文本

典型用法：
    # 每日 17:00 cron 自动运行
    python scripts/daily_report.py

    # 手动运行
    python scripts/daily_report.py --no-update   # 跳过数据更新
    python scripts/daily_report.py --execute      # 自动执行信号（谨慎！默认仅建议）
"""

import argparse
import logging
import sys
import time
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config
from src.position_manager import (
    PositionManager, Signal, load_sector_temperature_data,
    build_sector_state_map,
)
from src.pipeline import TrendPipeline

logger = logging.getLogger("daily_report")


def setup_logging():
    config.ensure_dirs()
    log_path = ROOT / "data" / "logs" / "daily_report.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def is_trading_day() -> bool:
    """简单判断是否为交易日（周一至周五，暂时不处理节假日）。"""
    wd = date.today().weekday()
    return wd < 5  # 0=Mon, 6=Sun


def run_data_update() -> bool:
    """运行数据更新流程。返回是否成功。"""
    logger.info("===== 数据更新 =====")
    cmd = [sys.executable, str(ROOT / "scripts" / "daily_update.py")]
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        logger.error("daily_update.py 退出码 %d", rc)
        return False
    logger.info("数据更新完成")
    return True


def run_report(args) -> str:
    """运行完整日报流程。返回日报文本。"""
    t0 = time.time()

    # 1. 数据更新
    if not args.no_update:
        if not is_trading_day():
            logger.info("今日非交易日，跳过数据更新")
        else:
            ok = run_data_update()
            if not ok:
                return "❌ 数据更新失败，请检查日志。"

    # 2. 加载温度数据
    logger.info("===== 加载温度数据 =====")
    sector_data = load_sector_temperature_data()
    if not sector_data:
        return "❌ 无法加载行业温度数据，请检查数据库。"
    logger.info("加载 %d 个行业温度数据", len(sector_data))

    sector_states = build_sector_state_map(sector_data)

    # 3. 生成信号
    logger.info("===== 生成交易信号 =====")
    pm = PositionManager()
    signals, summary = pm.generate_signals(sector_data, sector_states)

    # 4. 如果需要自动执行
    if args.execute:
        logger.info("===== 自动执行信号 =====")
        for sig in signals:
            if sig.action in ("BUY", "EXIT", "REDUCE", "SCALE_IN"):
                pm.execute_signal(sig, user_confirmed=True)
                logger.info("已执行: %s %s", sig.action, sig.symbol_name)

    # 5. 生成日报
    report = pm.format_daily_report(signals, summary)

    elapsed = time.time() - t0
    logger.info("日报生成完成: %.1f 秒", elapsed)

    return report


def parse_args():
    p = argparse.ArgumentParser(description="每日盘后策略报告")
    p.add_argument("--no-update", action="store_true", help="跳过数据更新步骤")
    p.add_argument("--execute", action="store_true", help="自动执行交易信号（默认仅建议）")
    p.add_argument("--save", type=str, default=None, help="将日报保存到指定文件")
    return p.parse_args()


if __name__ == "__main__":
    setup_logging()
    args = parse_args()

    try:
        report = run_report(args)
    except Exception as e:
        logger.exception("日报生成异常")
        report = f"❌ 日报生成失败: {e}"

    # 输出日报（供 clawbot/cron 捕获）
    print(report)

    # 可选保存
    if args.save:
        save_path = Path(args.save)
        if not save_path.is_absolute():
            save_path = ROOT / args.save
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(report, encoding="utf-8")
        logger.info("日报已保存到: %s", save_path)

    # 同时保存到 data/reports/ 目录
    reports_dir = ROOT / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    today_str = date.today().isoformat()
    report_file = reports_dir / f"daily-report-{today_str}.txt"
    report_file.write_text(report, encoding="utf-8")
    logger.info("日报已保存: %s", report_file)
