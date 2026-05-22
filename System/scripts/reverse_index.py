"""反向索引：处理结果按 source 的 captured_from_date 写回对应日记的 ## Captured。"""

import os
from pathlib import Path
from typing import Optional

import frontmatter

import config
import daily
from utils import logger


def _source_capture_date(source_file: str) -> Optional[str]:
    """从源文件 frontmatter 读 captured_from_date；读不到返回 None。"""
    if not os.path.exists(source_file):
        return None
    try:
        post = frontmatter.load(source_file)
    except Exception as e:
        logger.debug("解析 frontmatter 失败 file=%s err=%s", source_file, e)
        return None
    val = post.get("captured_from_date") or post.get("captured_at") or ""
    val = str(val).strip()
    if not val:
        return None
    try:
        return daily.parse_date(val[:10])
    except ValueError:
        return None


def link_processed_to_daily(source_file: str, processed_file: str) -> Optional[str]:
    """把处理结果链接追加到对应日记的 ## Captured。

    优先级：
    1. 源文件 frontmatter 的 captured_from_date
    2. 今天

    返回被写入的日记路径，未写入返回 None。
    """
    date_str = _source_capture_date(source_file) or daily.today_str()
    daily_file = daily.ensure_daily(date_str)

    processed_stem = Path(processed_file).stem
    source_stem = Path(source_file).stem
    line = f"- [[{processed_stem}]] <- [[{source_stem}]]"

    changed = daily.append_to_section(
        daily_file,
        config.DAILY_CAPTURED_HEADER,
        line,
    )
    if changed:
        logger.info("反向索引: %s -> %s", daily_file, processed_stem)
        return daily_file
    return None
