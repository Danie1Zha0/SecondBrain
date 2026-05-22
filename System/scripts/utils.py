"""通用工具：日志、文件名清洗、文件稳定检测。"""

import os
import re
import time
import logging
from logging.handlers import RotatingFileHandler

import config

_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _setup_logger():
    os.makedirs(config.LOG_PATH, exist_ok=True)
    log_file = os.path.join(config.LOG_PATH, "pipeline.log")

    logger = logging.getLogger("ai_pipeline")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=config.LOG_FILE_MAX_BYTES,
        backupCount=config.LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    return logger


logger = _setup_logger()


def sanitize_filename(name: str, max_length: int = config.MAX_FILENAME_LENGTH) -> str:
    """清掉 Windows 非法字符并截断长度。"""
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("-", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .-")
    return cleaned


def wait_for_file_stable(
    file_path: str,
    checks: int = config.FILE_STABILIZE_CHECKS,
    interval: float = config.FILE_STABILIZE_INTERVAL,
    max_wait: float = config.FILE_STABILIZE_MAX_WAIT,
) -> bool:
    """轮询文件大小，连续 N 次稳定且非零才返回 True；超过 max_wait 仍未稳定返回 False。"""
    deadline = time.time() + max_wait
    prev = None
    stable = 0
    while time.time() < deadline:
        if not os.path.exists(file_path):
            return False
        try:
            size = os.path.getsize(file_path)
        except OSError:
            return False
        if size > 0 and size == prev:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0
            prev = size
        time.sleep(interval)
    return False
