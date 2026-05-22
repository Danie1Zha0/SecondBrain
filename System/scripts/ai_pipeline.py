"""Obsidian AI Pipeline 入口。

默认行为：启动 watcher（含启动分拣 + 补昨日总结 + 冷扫描）。

子命令（一次性任务，不启动 watcher）：
  --all              一次跑完今天的全套流程：分拣 -> 补昨日总结 -> 扫描 -> 今日总结
  --capture [DATE]   分拣日记 ## Inbox 未勾选项到 00_Inbox（默认今天）
  --summary [DATE]   生成/重写 ## AI Day Summary（默认今天；--force 覆盖）
  --scan             冷扫描 00_Inbox 一次

多个子命令同时给出时按 capture -> scan -> summary 顺序执行。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai_pipeline", description="Obsidian AI Pipeline")
    p.add_argument(
        "--all",
        dest="run_all",
        action="store_true",
        help="一次跑完今天的全套：分拣 + 补昨日总结 + 扫描 + 今日总结，然后退出",
    )
    p.add_argument(
        "--capture",
        nargs="?",
        const="__today__",
        metavar="DATE",
        help="分拣指定日期日记的 Inbox（YYYY-MM-DD，默认今天）",
    )
    p.add_argument(
        "--summary",
        nargs="?",
        const="__today__",
        metavar="DATE",
        help="生成指定日期的 AI 日总结（默认今天）",
    )
    p.add_argument("--force", action="store_true", help="日总结已存在时覆盖")
    p.add_argument("--scan", action="store_true", help="冷扫描一次 00_Inbox 后退出")
    return p


def _run_all() -> None:
    from utils import logger
    from inbox_sort import sort_today_inbox
    from day_summary import ensure_yesterday_summary, summarize_day
    from pipeline import scan_inbox

    logger.info("=" * 50)
    logger.info("--all: 一次性跑全套")
    logger.info("=" * 50)

    try:
        created = sort_today_inbox()
        if created:
            logger.info("--all 分拣：新建 %s 个 Inbox 文件", created)
    except Exception as e:
        logger.warning("--all 分拣失败 err=%s", e)

    try:
        ensure_yesterday_summary()
    except Exception as e:
        logger.warning("--all 补昨日总结失败 err=%s", e)

    try:
        scan_inbox()
    except Exception as e:
        logger.warning("--all 扫描失败 err=%s", e)

    try:
        summarize_day()
    except Exception as e:
        logger.warning("--all 今日总结失败 err=%s", e)

    logger.info("--all 完成")


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    if args.run_all:
        _run_all()
        return 0

    if args.capture is not None or args.summary is not None or args.scan:
        from utils import logger
        from inbox_sort import sort_daily_inbox
        from day_summary import summarize_day
        from pipeline import scan_inbox
        from daily import parse_date

        if args.capture is not None:
            date_str = None if args.capture == "__today__" else parse_date(args.capture)
            created = sort_daily_inbox(date_str)
            logger.info("CLI capture 完成 created=%s", created)

        if args.scan:
            scan_inbox()

        if args.summary is not None:
            date_str = None if args.summary == "__today__" else parse_date(args.summary)
            summarize_day(date_str, force=args.force)

        return 0

    from watcher import start_watching
    start_watching()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
