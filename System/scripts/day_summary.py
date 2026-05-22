"""AI 日总结：把当日 daily 与当日处理过的笔记摘要喂给 LLM，写回 ## AI Day Summary。"""

import os
import re
import time
from pathlib import Path
from typing import Optional

import frontmatter

import config
import daily
from utils import logger


# =========================
# 收集当日的 processed 笔记
# =========================


def _collect_processed_for_date(date_str: str):
    """返回当日 03_Processed 里的笔记列表（按 processed_time 升序）。"""
    items = []
    if not os.path.isdir(config.PROCESSED_PATH):
        return items
    for name in os.listdir(config.PROCESSED_PATH):
        if not name.endswith("_processed.md"):
            continue
        path = os.path.join(config.PROCESSED_PATH, name)
        try:
            post = frontmatter.load(path)
        except Exception:
            continue
        pt = str(post.get("processed_time") or "").strip()
        if not pt.startswith(date_str):
            continue
        items.append((pt, path, post))
    items.sort(key=lambda x: x[0])
    return items


def _summary_of_processed(post) -> str:
    """从 processed 文件正文里抠 # Summary 段。抠不到就返回前 200 字。"""
    body = post.content or ""
    m = re.search(r"#\s*Summary\s*\n(.*?)(?=\n#\s|\Z)", body, re.DOTALL)
    if m:
        return m.group(1).strip()
    return body.strip()[:200]


# =========================
# Prompt
# =========================


def _load_prompt() -> str:
    path = os.path.join(config.PROMPT_PATH, "day_summary_prompt.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        logger.warning("加载 day_summary_prompt 失败 path=%s err=%s，使用默认", path, e)
        return (
            "你是日记助手。请基于以下当日内容写一段不超过 250 字的中文总结，"
            "提炼今日重点、亮点、问题、明日建议。直接给出正文，不要任何前缀。"
        )


def _build_user_content(date_str: str, daily_text: str, processed_items) -> str:
    parts = [
        f"# {date_str} 当日资料",
        "",
        "## 日记原文",
        "",
        daily_text.strip() or "(空)",
        "",
        "## 今日处理过的笔记摘要",
        "",
    ]
    if not processed_items:
        parts.append("(今日无处理过的笔记)")
    else:
        for pt, path, post in processed_items:
            stem = Path(path).stem.replace("_processed", "")
            parts.append(f"### [[{stem}]]")
            parts.append(_summary_of_processed(post))
            parts.append("")
    return "\n".join(parts)


# =========================
# 入口
# =========================


def _existing_ai_block(file_path: str) -> Optional[str]:
    """读出 ai-summary 标记之间的现有内容；标记不全或为空都返回 None。"""
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    s = text.find(config.DAILY_SUMMARY_MARK_START)
    e = text.find(config.DAILY_SUMMARY_MARK_END)
    if s == -1 or e == -1 or e <= s:
        return None
    inner = text[s + len(config.DAILY_SUMMARY_MARK_START):e].strip()
    return inner or None


def summarize_day(date_str: Optional[str] = None, force: bool = False) -> Optional[str]:
    """生成并写回指定日期的 AI 日总结。返回写入的日记路径。

    跳过条件：
    1. 标记块已有内容且非 --force
    2. 用户没写任何实质内容（仅模板）且当日无处理结果
    """
    date_str = date_str or daily.today_str()

    daily_file = daily.ensure_daily(date_str)
    with open(daily_file, "r", encoding="utf-8") as f:
        daily_text = f.read()

    existing = _existing_ai_block(daily_file)
    if existing and not force:
        logger.info("日总结已存在，跳过（用 force=True 覆盖）: %s", daily_file)
        return None

    processed_items = _collect_processed_for_date(date_str)

    if not processed_items and not daily.user_wrote_anything(daily_file, date_str):
        logger.info("日记仅含模板默认内容且今日无处理结果，跳过日总结: %s", daily_file)
        return None

    user_content = _build_user_content(date_str, daily_text, processed_items)

    logger.info(
        "生成日总结 date=%s processed=%s daily_chars=%s",
        date_str,
        len(processed_items),
        len(daily_text),
    )

    base_prompt = _load_prompt()
    messages = [
        {"role": "system", "content": base_prompt},
        {"role": "user", "content": user_content},
    ]

    text, meta = _ask_with_messages(messages)
    meta["prompt_version"] = config.DAILY_SUMMARY_PROMPT_VERSION

    body_lines = [
        text.strip(),
        "",
        f"<!-- generated_at: {time.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"provider: {meta.get('provider')} | model: {meta.get('model')} | "
        f"tokens_in: {meta.get('tokens_in')} | tokens_out: {meta.get('tokens_out')} | "
        f"duration_ms: {meta.get('duration_ms')} | "
        f"prompt_version: {meta.get('prompt_version')} -->",
    ]

    daily.replace_marked_block(
        daily_file,
        config.DAILY_SUMMARY_MARK_START,
        config.DAILY_SUMMARY_MARK_END,
        "\n".join(body_lines),
        fallback_header=config.DAILY_SUMMARY_HEADER,
    )
    logger.info(
        "日总结写入完成 daily=%s duration_ms=%s tokens_in=%s tokens_out=%s",
        daily_file,
        meta.get("duration_ms"),
        meta.get("tokens_in"),
        meta.get("tokens_out"),
    )
    return daily_file


def _ask_with_messages(messages):
    """直接复用 llm.ask_llm 的底层，但允许自定义 messages。"""
    import llm
    start = time.time()
    logger.info("调用 LLM (day_summary) provider=%s model=%s", config.LLM_PROVIDER, config.MODEL_NAME)
    if config.LLM_PROVIDER == "ollama":
        return llm._call_ollama(messages, start)
    if config.LLM_PROVIDER == "remote":
        return llm._call_remote(messages, start)
    raise ValueError(f"不支持的 LLM_PROVIDER: {config.LLM_PROVIDER}")


def ensure_yesterday_summary() -> Optional[str]:
    """启动时调用：昨天的日记如果存在且标记块还空，就补一次。"""
    y = daily.yesterday_str()
    if not daily.daily_exists(y):
        logger.debug("昨日无日记，跳过 ensure_yesterday_summary: %s", y)
        return None

    path = daily.daily_path(y)
    if _existing_ai_block(path):
        logger.debug("昨日总结已存在，跳过: %s", path)
        return None

    logger.info("检测到昨日 (%s) 缺日总结，开始尝试补写", y)
    try:
        return summarize_day(y)
    except Exception as e:
        logger.warning("补昨日总结失败 date=%s err=%s", y, e)
        return None
