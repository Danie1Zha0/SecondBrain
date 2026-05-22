"""URL 抓取：requests 预下载 + trafilatura 解析为 Markdown。"""

import re

import config
from utils import logger

_URL_PATTERN = re.compile(r"https?://[^\s\)\]\>，。、；,]+", re.IGNORECASE)
_URL_TRAILING_TRIM = ".,;:!?。，；！？)]>'\""


def find_urls(text: str):
    raw = _URL_PATTERN.findall(text or "")
    return [u.rstrip(_URL_TRAILING_TRIM) for u in raw]


def _download(url: str):
    try:
        import requests
    except ImportError:
        logger.warning("requests 未安装，无法预下载 URL")
        return None

    try:
        resp = requests.get(
            url,
            timeout=config.URL_FETCH_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning("下载 URL 失败 url=%s err=%s", url, e)
        return None


def _extract(html: str):
    try:
        import trafilatura
    except ImportError:
        logger.warning("trafilatura 未安装，跳过正文提取")
        return None
    try:
        text = trafilatura.extract(
            html,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
        )
        if not text:
            text = trafilatura.extract(html, include_comments=False)
        return text
    except Exception as e:
        logger.warning("trafilatura 解析失败 err=%s", e)
        return None


def fetch_article(url: str):
    html = _download(url)
    if not html:
        return None
    return _extract(html)
