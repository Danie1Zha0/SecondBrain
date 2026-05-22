"""Quick Capture 分拣：扫描日记 ## Inbox 的未勾选项，落到 00_Inbox 并打勾。"""

import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse
from typing import Optional

import config
from utils import logger, sanitize_filename
import daily
from capture import find_urls


# =========================
# Slug 规则（D3: rule-based）
# =========================

_HOST_TRIM = re.compile(r"^(www|m|mobile)\.", re.IGNORECASE)


def _slug_from_url(url: str) -> str:
    try:
        u = urlparse(url)
        host = (u.netloc or "").lower()
        host = _HOST_TRIM.sub("", host)
        host = host.split(":")[0]
        path = (u.path or "").strip("/")
        first_seg = path.split("/")[0] if path else ""
        first_seg = re.sub(r"\.(html?|php|aspx?|md|pdf|jpg|png)$", "", first_seg, flags=re.IGNORECASE)
        first_seg = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff\-]+", "-", first_seg).strip("-")
        if host and first_seg:
            return f"{host}-{first_seg}"
        return host or first_seg or "link"
    except Exception:
        return "link"


def _slug_from_text(text: str) -> str:
    cleaned = re.sub(r"https?://\S+", "", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned[: config.INBOX_SLUG_MAX_LENGTH].strip()
    cleaned = sanitize_filename(cleaned) or "note"
    return cleaned


def make_slug(text: str) -> str:
    urls = find_urls(text)
    if urls:
        base = _slug_from_url(urls[0])
    else:
        base = _slug_from_text(text)
    base = sanitize_filename(base) or "note"
    return base


def _timestamp_prefix() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _unique_inbox_path(slug: str) -> str:
    os.makedirs(config.INBOX_PATH, exist_ok=True)
    prefix = _timestamp_prefix()
    base = f"{prefix}-{slug}"
    candidate = os.path.join(config.INBOX_PATH, base + ".md")
    if not os.path.exists(candidate):
        return candidate
    i = 1
    while True:
        candidate = os.path.join(config.INBOX_PATH, f"{base}_{i}.md")
        if not os.path.exists(candidate):
            return candidate
        i += 1


# =========================
# 落地 00_Inbox 文件
# =========================


def _build_inbox_md(text: str, source_date: str) -> str:
    urls = find_urls(text)
    primary_url = urls[0] if urls else ""
    tags = "  - quick-capture"
    front = [
        "---",
        "type: capture",
        "captured_from: daily",
        f"captured_from_date: {source_date}",
        f"captured_at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"source: {primary_url}",
        "tags:",
        tags,
        "---",
        "",
        text.strip(),
        "",
    ]
    return "\n".join(front)


def _create_inbox_file(text: str, source_date: str) -> Optional[str]:
    slug = make_slug(text)
    path = _unique_inbox_path(slug)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_build_inbox_md(text, source_date))
    except OSError as e:
        logger.warning("写入 Inbox 文件失败 slug=%s err=%s", slug, e)
        return None
    logger.info("Quick Capture 分拣: %s <- %s", path, text[:40])
    return path


# =========================
# 单日 / 当日分拣入口
# =========================


def sort_daily_inbox(date_str: Optional[str] = None) -> int:
    """对指定日期的日记的 ## Inbox 段做分拣。返回新建的文件数。"""
    date_str = date_str or daily.today_str()

    if not daily.daily_exists(date_str):
        logger.debug("日记不存在，跳过分拣: %s", date_str)
        return 0

    path = daily.daily_path(date_str)
    section = daily.read_section(path, config.DAILY_INBOX_HEADER)
    if section is None:
        logger.debug("日记无 ## %s 段，跳过分拣: %s", config.DAILY_INBOX_HEADER, path)
        return 0

    tasks = daily.find_unchecked_tasks(section)
    if not tasks:
        logger.debug("日记无未分拣项: %s", path)
        return 0

    logger.info("分拣日记 %s，发现 %s 个未分拣项", path, len(tasks))

    new_body = section
    created = 0
    for line_index, _raw, text in tasks:
        created_path = _create_inbox_file(text, date_str)
        if not created_path:
            continue
        new_body = daily.mark_task_checked(new_body, line_index)
        created += 1

    if created > 0:
        daily.replace_section(path, config.DAILY_INBOX_HEADER, new_body)
        logger.info("日记 Inbox 分拣完成 path=%s created=%s", path, created)
    return created


def sort_today_inbox() -> int:
    return sort_daily_inbox(daily.today_str())
