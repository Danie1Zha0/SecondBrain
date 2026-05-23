"""文件监听：watchdog + 文件稳定检测 + 启动时冷扫描。"""

import time

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import config
from utils import logger, wait_for_file_stable
from pipeline import (
    process_markdown,
    scan_inbox,
    looks_like_processed_artifact,
    already_processed,
    bootstrap_dirs,
)
from inbox_sort import sort_today_inbox
from day_summary import ensure_yesterday_summary


class InboxHandler(FileSystemEventHandler):
    def _maybe_process(self, src_path: str) -> None:
        if not src_path.endswith(".md"):
            return
        if looks_like_processed_artifact(src_path):
            logger.warning(
                "watcher 跳过流水线产物（_processed.md 不该进 Inbox）: %s",
                src_path,
            )
            return
        # 已经处理过的（多半是归档动作触发的二次事件，或同名文件被同步工具重写），静默跳过，
        # 避免在 wait_for_file_stable 那里因为源文件已移走而打 WARNING。
        if already_processed(src_path):
            logger.debug("watcher 跳过已处理文件: %s", src_path)
            return
        if not wait_for_file_stable(src_path):
            logger.warning("文件未稳定或不存在，跳过: %s", src_path)
            return
        process_markdown(src_path)

    def on_created(self, event):
        if event.is_directory:
            return
        self._maybe_process(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._maybe_process(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        self._maybe_process(event.dest_path)


def start_watching() -> None:
    bootstrap_dirs()

    observer = Observer()
    handler = InboxHandler()
    observer.schedule(handler, config.INBOX_PATH, recursive=False)
    observer.start()

    logger.info("=" * 50)
    logger.info("Obsidian AI Pipeline 已启动")
    logger.info("监听目录: %s", config.INBOX_PATH)
    logger.info("模型供应商: %s", config.LLM_PROVIDER)
    logger.info("模型名称: %s", config.MODEL_NAME)
    logger.info("Prompt 版本: %s", config.PROMPT_VERSION)
    logger.info("=" * 50)

    try:
        _run_startup_tasks()
        scan_inbox()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("收到中断，正在停止...")
        observer.stop()

    observer.join()


def _run_startup_tasks() -> None:
    """启动时的一次性任务：Inbox 分拣 + 补昨日总结。失败不影响主流程。"""
    try:
        created = sort_today_inbox()
        if created:
            logger.info("启动分拣：新建 %s 个 Inbox 文件", created)
    except Exception as e:
        logger.warning("启动分拣失败 err=%s", e)

    try:
        ensure_yesterday_summary()
    except Exception as e:
        logger.warning("启动补昨日总结失败 err=%s", e)
