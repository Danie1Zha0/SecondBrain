"""日记文件辅助：路径、按需创建、段落读写。"""

import os
import re
from datetime import date as date_cls, datetime, timedelta
from typing import Optional

import config
from utils import logger


# =========================
# 日期 / 路径
# =========================


def today_str() -> str:
    return datetime.now().strftime(config.DAILY_FILENAME_FORMAT)


def yesterday_str() -> str:
    d = datetime.now().date() - timedelta(days=1)
    return d.strftime(config.DAILY_FILENAME_FORMAT)


def parse_date(value: str) -> str:
    """容错解析多种常见日期写法，返回 strftime 后的标准字符串。"""
    value = (value or "").strip()
    if not value:
        return today_str()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            d = datetime.strptime(value, fmt).date()
            return d.strftime(config.DAILY_FILENAME_FORMAT)
        except ValueError:
            continue
    raise ValueError(f"无法解析日期: {value!r}")


def daily_path(date_str: Optional[str] = None) -> str:
    name = (date_str or today_str()) + ".md"
    return os.path.join(config.DAILY_PATH, name)


# =========================
# 创建（lazy）
# =========================


def _load_template() -> str:
    path = os.path.join(config.TEMPLATE_PATH, config.DAILY_TEMPLATE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        logger.warning("加载 daily 模板失败 path=%s err=%s，使用最小模板", path, e)
        return (
            "---\n"
            "type: daily\n"
            "date: {{date}}\n"
            "tags:\n"
            "  - daily\n"
            "---\n\n"
            "# {{date}} 日记\n\n"
            f"## {config.DAILY_INBOX_HEADER}\n\n- \n\n"
            f"## {config.DAILY_CAPTURED_HEADER}\n\n"
            f"## {config.DAILY_SUMMARY_HEADER}\n\n"
        )


def ensure_daily(date_str: Optional[str] = None) -> str:
    """返回日记文件路径，不存在则用模板创建。"""
    date_str = date_str or today_str()
    path = daily_path(date_str)
    if os.path.exists(path):
        return path

    os.makedirs(config.DAILY_PATH, exist_ok=True)
    content = _load_template().replace("{{date}}", date_str)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("创建日记: %s", path)
    return path


def daily_exists(date_str: Optional[str] = None) -> bool:
    return os.path.exists(daily_path(date_str or today_str()))


# =========================
# 段落读写
# =========================

# 匹配 `## Header`（1~6 级）当前段落到下一个同级或更高级标题之前。
def _section_regex(header: str) -> re.Pattern:
    return re.compile(
        rf"(^|\n)(?P<head>#{{1,6}}\s*{re.escape(header)}\s*\n)(?P<body>.*?)(?=\n#{{1,6}}\s|\Z)",
        re.DOTALL,
    )


def read_section(file_path: str, header: str) -> Optional[str]:
    """读取段落正文（不含标题行）。段落不存在返回 None。"""
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    m = _section_regex(header).search(text)
    if not m:
        return None
    return m.group("body")


def replace_section(
    file_path: str,
    header: str,
    new_body: str,
    *,
    level: int = 2,
    create_if_missing: bool = True,
) -> bool:
    """覆盖段落正文。返回是否变更。

    new_body 不应带最外层的标题行（会自动加 `## header`）。
    """
    if not os.path.exists(file_path):
        return False

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    body = new_body.rstrip() + "\n"
    pattern = _section_regex(header)
    m = pattern.search(text)
    if m:
        prefix = text[: m.start("body")]
        suffix = text[m.end("body"):]
        new_text = prefix + "\n" + body + suffix.lstrip("\n")
    else:
        if not create_if_missing:
            return False
        head = "#" * level + " " + header
        sep = "\n" if text.endswith("\n") else "\n\n"
        new_text = text.rstrip() + sep + "\n" + head + "\n\n" + body

    if new_text == text:
        return False

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True


def replace_marked_block(
    file_path: str,
    start_marker: str,
    end_marker: str,
    new_body: str,
    *,
    fallback_header: Optional[str] = None,
    level: int = 2,
) -> bool:
    """覆盖 `<!-- start -->` 与 `<!-- end -->` 之间的内容。

    - 标记齐全：仅替换标记之间的正文，标记外内容（含同段落里的人工补充）原样保留。
    - 标记缺失或不成对：把整个标记块（含 start/body/end）插入到 fallback_header 段末尾；
      若 fallback_header 也不存在则追加到文件末尾。

    返回是否变更。
    """
    if not os.path.exists(file_path):
        return False

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    body = new_body.strip("\n")
    block_inner = f"\n{body}\n" if body else "\n"

    start_idx = text.find(start_marker)
    end_idx = text.find(end_marker)
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        before = text[: start_idx + len(start_marker)]
        after = text[end_idx:]
        new_text = before + block_inner + after
    else:
        block = f"{start_marker}{block_inner}{end_marker}\n"
        if fallback_header:
            section_body = read_section(file_path, fallback_header)
            if section_body is None:
                head = "#" * level + " " + fallback_header
                sep = "\n" if text.endswith("\n") else "\n\n"
                new_text = text.rstrip() + sep + "\n" + head + "\n\n" + block
            else:
                new_body_full = section_body.rstrip() + "\n\n" + block
                if not replace_section(file_path, fallback_header, new_body_full, level=level):
                    return False
                return True
        else:
            sep = "\n" if text.endswith("\n") else "\n\n"
            new_text = text.rstrip() + sep + "\n" + block

    if new_text == text:
        return False

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True


def append_to_section(
    file_path: str,
    header: str,
    line: str,
    *,
    level: int = 2,
    dedup: bool = True,
) -> bool:
    """向段落末尾追加一行。dedup=True 时若该行已存在则跳过。"""
    if not os.path.exists(file_path):
        return False
    body = read_section(file_path, header)
    if body is None:
        new_body = line + "\n"
    else:
        if dedup and line.strip() and line.strip() in body:
            return False
        new_body = body.rstrip() + "\n" + line + "\n"
    return replace_section(file_path, header, new_body, level=level)


# =========================
# Inbox 行解析
# =========================

# 匹配 `- [ ] xxx` / `- [x] xxx` / `* [ ] xxx`，捕获状态与正文
_TASK_LINE = re.compile(r"^(\s*)([-*])\s*\[(?P<state> |x|X)\]\s*(?P<text>.+?)\s*$")


def find_unchecked_tasks(section_body: str):
    """返回 [(line_index_in_section, raw_line, text)]，仅返回未勾选且有实质内容的项。"""
    results = []
    for i, raw in enumerate(section_body.splitlines()):
        m = _TASK_LINE.match(raw)
        if not m:
            continue
        state = m.group("state")
        if state != " ":
            continue
        text = m.group("text").strip()
        if not text:
            continue
        results.append((i, raw, text))
    return results


_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def _strip_section(text: str, header: str) -> str:
    """删掉整段（含标题行）。找不到原样返回。"""
    pattern = re.compile(
        rf"(\n|^)#{{1,6}}\s*{re.escape(header)}\s*\n.*?(?=\n#{{1,6}}\s|\Z)",
        re.DOTALL,
    )
    return pattern.sub("", text)


def user_wrote_anything(file_path: str, date_str: str) -> bool:
    """对比日记与渲染后的模板，去掉系统自动维护的段落，看用户是否写过实质内容。

    系统维护段落：Inbox（分拣会改）、Captured（反向索引会改）、AI Day Summary（AI 写）。
    """
    if not os.path.exists(file_path):
        return False
    with open(file_path, "r", encoding="utf-8") as f:
        actual = f.read()
    template = _load_template().replace("{{date}}", date_str)

    def normalize(t: str) -> str:
        t = _strip_frontmatter(t)
        for h in (
            config.DAILY_INBOX_HEADER,
            config.DAILY_CAPTURED_HEADER,
            config.DAILY_SUMMARY_HEADER,
        ):
            t = _strip_section(t, h)
        return re.sub(r"\s+", "", t)

    return normalize(actual) != normalize(template)


def mark_task_checked(section_body: str, line_index: int) -> str:
    lines = section_body.splitlines()
    if line_index < 0 or line_index >= len(lines):
        return section_body
    raw = lines[line_index]
    m = _TASK_LINE.match(raw)
    if not m:
        return section_body
    indent, bullet = m.group(1), m.group(2)
    text = m.group("text")
    lines[line_index] = f"{indent}{bullet} [x] {text}"
    return "\n".join(lines)
