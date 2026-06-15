"""主处理流程：去重、URL 抓取、LLM 摘要、写 Processed/Wiki、归档、失败队列。"""

import os
import re
import json
import time
import shutil
import traceback
from pathlib import Path

import frontmatter

import config
from utils import logger, sanitize_filename
from llm import ask_llm, ask_with_system
from capture import find_urls, fetch_article
from reverse_index import link_processed_to_daily


# =========================
# 内容清洗
# =========================


def clean_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# =========================
# 去重 / 路径
# =========================


def _processed_month_dir(file_path: str) -> str:
    """从源文件名提取 YYYY-MM 作为月份子目录，提取不到则用当前月。"""
    stem = Path(file_path).stem
    m = re.match(r"^(\d{4}-\d{2})-", stem)
    month = m.group(1) if m else time.strftime("%Y-%m")
    return os.path.join(config.PROCESSED_PATH, month)


def processed_path_for(file_path: str) -> str:
    month_dir = _processed_month_dir(file_path)
    return os.path.join(month_dir, Path(file_path).stem + "_processed.md")


def already_processed(file_path: str) -> bool:
    # 先检查带月份子目录的新路径
    if os.path.exists(processed_path_for(file_path)):
        return True
    # 兼容旧的无月份路径（迁移前存在的文件）
    flat = os.path.join(config.PROCESSED_PATH, Path(file_path).stem + "_processed.md")
    return os.path.exists(flat)


def looks_like_processed_artifact(file_path: str) -> bool:
    """文件名以 _processed.md 结尾的，认定是流水线自身产物，禁止再当源处理。"""
    return Path(file_path).stem.endswith("_processed")


def bootstrap_dirs() -> None:
    """确保 vault 必需目录存在。第一次 clone 后运行任意入口都会自动调用，幂等。

    其它模块（utils / 各处的 save_*）会再 lazy 创建自己的子目录；这里负责的是 watcher
    依赖的 INBOX 等顶层路径，避免空 vault 上 watchdog 启动直接 FileNotFoundError。
    """
    for path in (
        config.INBOX_PATH,
        config.DAILY_PATH,
        config.PROCESSED_PATH,
        config.WIKI_PATH,
        config.ARCHIVE_PATH,
    ):
        os.makedirs(path, exist_ok=True)


# =========================
# AI 输出解析
# =========================


def _extract_section(ai_output: str, header_name: str) -> str:
    """抓取 ``# <header>`` 段的正文。

    兼容两种格式：标题后换行（多行）或标题后正文直接跟在同一行（某些模型会把
    整段挤成一行）。正文一直取到下一个 ``\\n# 标题`` 或文末。
    """
    pattern = rf"#{{1,6}}[ \t]*{re.escape(header_name)}\b[ \t]*(.*?)(?=\n#{{1,6}}[ \t]|\Z)"
    m = re.search(pattern, ai_output, re.DOTALL)
    return m.group(1).strip() if m else ""


def _dedupe(items):
    out = []
    for x in items:
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
    return out


def _parse_bullet_kv(section: str):
    """解析 ``- [[名称]]: 值`` 形式的条目，兼容单行/多行。

    返回 [(名称, 值), ...]。值取到下一个 ``- [[`` 之前或段末，
    因此即便整段在一行（条目用 ``空格-空格[[`` 分隔）也能正确切分。
    """
    return [
        (k.strip(), v.strip())
        for k, v in re.findall(
            r"\[\[(.*?)\]\]\s*[:：]\s*(.+?)(?=\s-\s*\[\[|\Z)", section, re.DOTALL
        )
    ]


def extract_concepts(ai_output: str):
    section = _extract_section(ai_output, "Concepts")
    text_to_search = section if section else ai_output
    return _dedupe(re.findall(r"\[\[(.*?)\]\]", text_to_search))


def extract_definitions(ai_output: str):
    section = _extract_section(ai_output, "Definitions")
    if not section:
        return {}
    defs = {}
    for name, d in _parse_bullet_kv(section):
        if name and d:
            defs[name] = d
    return defs


def extract_relations(ai_output: str) -> dict:
    """解析 # Relations 段，返回 {概念: [真正相关的概念...]}。

    格式：``- [[概念]]: [[相关A]], [[相关B]]``。这是 A 项的核心——
    用 LLM 给出的语义相关项替代旧的"同篇全量共现"。
    """
    section = _extract_section(ai_output, "Relations")
    if not section:
        return {}
    relations: dict[str, list] = {}
    for concept, rhs in _parse_bullet_kv(section):
        targets = _dedupe(
            [t for t in re.findall(r"\[\[(.*?)\]\]", rhs) if t.strip() and t.strip() != concept]
        )
        if concept and targets:
            relations[concept] = targets
    return relations


# =========================
# Processed 输出
# =========================


def save_processed(source_file: str, ai_output: str, meta: dict) -> str:
    month_dir = _processed_month_dir(source_file)
    os.makedirs(month_dir, exist_ok=True)
    out_path = processed_path_for(source_file)

    def _val(v):
        return "" if v is None else v

    lines = [
        "---",
        f"source_file: {source_file}",
        f"archived_path: ",
        f"processed_time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"provider: {_val(meta.get('provider'))}",
        f"model: {_val(meta.get('model'))}",
        f"prompt_version: {_val(meta.get('prompt_version'))}",
        f"tokens_in: {_val(meta.get('tokens_in'))}",
        f"tokens_out: {_val(meta.get('tokens_out'))}",
        f"duration_ms: {_val(meta.get('duration_ms'))}",
        "---",
        "",
        ai_output,
        "",
    ]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info("已写入 Processed: %s", out_path)
    return out_path


def _update_archived_path(processed_file: str, archived_path: str) -> None:
    if not os.path.exists(processed_file):
        return
    try:
        with open(processed_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("更新 archived_path 失败 file=%s err=%s", processed_file, e)
        return

    changed = False
    for i, line in enumerate(lines):
        if line.startswith("archived_path:"):
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            lines[i] = f"archived_path: {archived_path}{eol}"
            changed = True
            break
    if not changed:
        return

    try:
        with open(processed_file, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError as e:
        logger.warning("更新 archived_path 失败 file=%s err=%s", processed_file, e)


# =========================
# Wiki 输出
# =========================


def _wiki_file_for(concept: str) -> str | None:
    safe_name = sanitize_filename(concept)
    if not safe_name:
        return None
    return os.path.join(config.WIKI_PATH, f"{safe_name}.md")


def _norm_name_key(name: str) -> str:
    """归一化键：去全部空白 + 小写。用于发现仅大小写/空格不同的同名词条。"""
    return re.sub(r"\s+", "", name or "").lower()


def _existing_wiki_variant(concept: str) -> str | None:
    """若 04_Wiki 里已存在仅大小写/空格不同的同名词条，返回其路径，否则 None。

    防止 ``AI agent`` / ``AI Agent`` / ``Open Router`` / ``OpenRouter`` 这类
    仅大小写或空格差异造成的重复建档（单复数差异不在此处理，过于激进，留给合并脚本）。
    """
    safe = sanitize_filename(concept)
    if not safe:
        return None
    target = _norm_name_key(safe)
    if not target:
        return None
    try:
        for fn in os.listdir(config.WIKI_PATH):
            if fn.endswith(".md") and _norm_name_key(fn[:-3]) == target:
                return os.path.join(config.WIKI_PATH, fn)
    except OSError:
        return None
    return None


def _read_section_links(text: str, header: str) -> list:
    """读取 ``## <header>`` 段里的所有 [[链接]] 名。"""
    m = re.search(
        rf"##[ \t]*{re.escape(header)}[ \t]*\n(.*?)(?=\n##[ \t]|\Z)", text, re.DOTALL
    )
    if not m:
        return []
    return _dedupe(re.findall(r"\[\[(.*?)\]\]", m.group(1)))


def _read_definition(text: str) -> str:
    m = re.search(r"##[ \t]*Definition[ \t]*\n(.*?)(?=\n##[ \t]|\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _replace_section_body(text: str, header: str, body: str) -> str:
    """把 ``## <header>`` 段的正文整体替换为 body（不含标题行）。段不存在则原样返回。

    用 ``[ \\t]*`` 而非 ``\\s*`` 限定标题行（避免跨行吞掉空行），并固定重建为
    ``## <header>\\n\\n<body>\\n``，保证重复更新时空行不累积、格式稳定。
    """
    pat = re.compile(
        rf"##[ \t]*{re.escape(header)}[ \t]*\n(.*?)(?=\n##[ \t]|\Z)", re.DOTALL
    )
    new_text, n = pat.subn(
        lambda m: f"## {header}\n\n" + body.rstrip() + "\n", text, count=1
    )
    return new_text if n else text


def _is_weak_definition(d: str) -> bool:
    d = (d or "").strip()
    return (not d) or d.upper() == "TODO"


def _append_reference(wiki_file: str, source_stem: str) -> None:
    with open(wiki_file, "r", encoding="utf-8") as f:
        text = f.read()

    ref_link = f"- [[{source_stem}]]"
    if ref_link in text:
        return

    pattern = re.compile(
        r"(##\s*References\s*\n)((?:.*?\n)*?)(?=\n##\s|\Z)", re.MULTILINE
    )
    m = pattern.search(text)
    if m:
        body = m.group(2)
        new_body = body.rstrip() + "\n" + ref_link + "\n"
        new_text = text[: m.start(2)] + new_body + text[m.end(2):]
    else:
        new_text = text.rstrip() + "\n\n## References\n\n" + ref_link + "\n"

    with open(wiki_file, "w", encoding="utf-8") as f:
        f.write(new_text)

    logger.info("追加 Wiki 引用: %s <- %s", wiki_file, source_stem)


def _update_existing_wiki(
    wiki_file: str,
    concept: str,
    source_stem: str,
    related: list,
    refined_def: str,
    raw_def: str,
) -> None:
    """词条已存在时（C 项）：

    1. 追加来源引用（保留可追溯性）。
    2. Related 做并集去重 + 增补（用 A 项的语义相关项，而非全量共现）。
    3. 定义择优，只在以下两种情况覆盖，避免用某篇文章的窄定义冲掉已有好定义：
       - 拿到 LLM 精炼后的定义（refined_def）；或
       - 现有定义为空/TODO，用本篇候选定义补上。
    """
    try:
        with open(wiki_file, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        logger.warning("读取 Wiki 失败，退化为仅追加引用 file=%s err=%s", wiki_file, e)
        _append_reference(wiki_file, source_stem)
        return

    changed = False

    # 2. Related 合并去重
    if related:
        existing = _read_section_links(text, "Related")
        merged = _dedupe([r for r in (existing + related) if r != concept])
        if merged != existing:
            body = "\n".join(f"- [[{r}]]" for r in merged)
            new_text = _replace_section_body(text, "Related", body)
            if new_text != text:
                text = new_text
                changed = True

    # 3. 定义择优
    cur = _read_definition(text)
    def_to_write = ""
    if refined_def and not _is_weak_definition(refined_def):
        def_to_write = refined_def.strip()
    elif _is_weak_definition(cur) and raw_def and not _is_weak_definition(raw_def):
        def_to_write = raw_def.strip()
    if def_to_write and def_to_write != cur:
        new_text = _replace_section_body(text, "Definition", def_to_write)
        if new_text != text:
            text = new_text
            changed = True

    if changed:
        try:
            with open(wiki_file, "w", encoding="utf-8") as f:
                f.write(text)
            logger.info("更新 Wiki（定义/Related）: %s", wiki_file)
        except OSError as e:
            logger.warning("写回 Wiki 失败 file=%s err=%s", wiki_file, e)

    # 1. 追加引用（_append_reference 自身会去重）
    _append_reference(wiki_file, source_stem)


def create_or_update_wiki(
    concept: str,
    source_file: str,
    definitions: dict,
    relations: dict,
    refined_defs: dict | None = None,
) -> None:
    os.makedirs(config.WIKI_PATH, exist_ok=True)
    wiki_file = _wiki_file_for(concept)
    if not wiki_file:
        logger.warning("概念名清洗后为空，跳过: %r", concept)
        return

    source_stem = Path(source_file).stem
    refined_defs = refined_defs or {}
    # A 项：用 LLM 给出的语义相关项，没有则留空（不再回退到全量共现噪声）
    related = _dedupe([r for r in relations.get(concept, []) if r != concept])

    # 防重：精确文件不存在时，复用仅大小写/空格不同的已有词条，避免重复建档
    if not os.path.exists(wiki_file):
        variant = _existing_wiki_variant(concept)
        if variant:
            wiki_file = variant

    if os.path.exists(wiki_file):
        _update_existing_wiki(
            wiki_file,
            concept,
            source_stem,
            related,
            refined_def=refined_defs.get(concept, ""),
            raw_def=definitions.get(concept, ""),
        )
        return

    related_text = "\n".join(f"- [[{r}]]" for r in related)
    definition_text = definitions.get(concept) or "TODO"

    content = (
        "---\n"
        f"created: {time.strftime('%Y-%m-%d')}\n"
        f"source: {Path(source_file).name}\n"
        "tags:\n"
        "  - wiki\n"
        "---\n\n"
        f"# {concept}\n\n"
        "## Definition\n\n"
        f"{definition_text}\n\n"
        "## Related\n\n"
        f"{related_text}\n\n"
        "## References\n\n"
        f"- [[{source_stem}]]\n"
    )

    with open(wiki_file, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info("创建 Wiki: %s", wiki_file)


# =========================
# C 项：已存在词条的定义精炼（每篇文章一次批量 LLM 调用）
# =========================

_REFINE_SYSTEM_PROMPT = (
    "你是知识库词条维护助手。下面给出若干概念，每个概念有一条「现有定义」和一条来自新文章的"
    "「候选定义」。请为每个概念输出一条最准确、凝练、与具体文章脱钩的通用定义（不超过50字，"
    "中文，不带标点结尾也可）。\n"
    "规则：\n"
    "- 综合两条定义择优融合，去掉只属于某篇文章的临时性描述。\n"
    "- 不要解释，不要引导语。\n"
    "- 严格逐行输出，格式：- [[概念]]: 定义\n"
)


def refine_existing_definitions(
    existing_concepts: list, definitions: dict
) -> dict:
    """C 项：对已存在且本篇给出了新候选定义的概念，批量调一次 LLM 融合精炼。

    失败/限速时返回 {}，调用方退化为保留原定义，绝不影响文章处理。
    """
    items = []
    for c in existing_concepts:
        wf = _wiki_file_for(c)
        if not wf or not os.path.exists(wf):
            continue
        new_def = (definitions.get(c) or "").strip()
        if not new_def or _is_weak_definition(new_def):
            continue
        try:
            with open(wf, "r", encoding="utf-8") as f:
                old_def = _read_definition(f.read())
        except OSError:
            continue
        if not old_def or _is_weak_definition(old_def):
            # 原定义为空/TODO，直接用新定义，无需 LLM
            continue
        if new_def == old_def:
            continue
        items.append((c, old_def, new_def))

    if not items:
        return {}

    lines = [
        f"- [[{c}]]: 现有定义「{old}」；候选定义「{new}」"
        for c, old, new in items
    ]
    user_content = "\n".join(lines)
    try:
        text, _meta = ask_with_system(_REFINE_SYSTEM_PROMPT, user_content)
    except Exception as e:
        logger.warning("定义精炼 LLM 调用失败，保留原定义 err=%s", e)
        return {}

    refined = {}
    for line in (text or "").splitlines():
        m = re.match(r"-\s*\[\[(.*?)\]\]\s*[:：]\s*(.+)", line.strip())
        if m:
            name, d = m.group(1).strip(), m.group(2).strip()
            if name and d and not _is_weak_definition(d):
                refined[name] = d
    if refined:
        logger.info("定义精炼：更新 %s 个概念定义", len(refined))
    return refined


# =========================
# URL 抓取
# =========================


def _maybe_fetch_url(content: str) -> str:
    if len(content) >= config.URL_FETCH_THRESHOLD:
        return content
    urls = find_urls(content)
    if not urls:
        return content
    url = urls[0]
    logger.info(
        "正文较短(%s < %s)，尝试抓取 URL: %s",
        len(content),
        config.URL_FETCH_THRESHOLD,
        url,
    )
    fetched = fetch_article(url)
    if not fetched or len(fetched) < config.MIN_CONTENT_LENGTH:
        logger.warning("URL 抓取失败或正文太短，使用原始内容")
        return content
    logger.info("URL 抓取成功，正文 %s 字", len(fetched))
    return f"{content}\n\n---\n\n# 抓取自 {url}\n\n{fetched}"


# =========================
# 归档
# =========================


def archive_source(file_path: str) -> str | None:
    """成功处理后把原始文件移动到 06_Archive/<YYYY-MM>/。"""
    if not os.path.exists(file_path):
        return None

    month_dir = os.path.join(config.ARCHIVE_PATH, time.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)

    name = Path(file_path).name
    dest = os.path.join(month_dir, name)

    if os.path.abspath(dest) == os.path.abspath(file_path):
        return None

    if os.path.exists(dest):
        stem = Path(name).stem
        suffix = Path(name).suffix
        idx = 1
        while True:
            candidate = os.path.join(month_dir, f"{stem}_{idx}{suffix}")
            if not os.path.exists(candidate):
                dest = candidate
                break
            idx += 1

    try:
        shutil.move(file_path, dest)
        logger.info("已归档: %s -> %s", file_path, dest)
        return dest
    except Exception as e:
        logger.warning("归档失败 file=%s err=%s", file_path, e)
        return None


# =========================
# 失败状态：按 file_path 聚合
# =========================


def _failed_state_file() -> str:
    return os.path.join(config.STATE_PATH, "failed.json")


def _load_failed_state() -> dict:
    state_file = _failed_state_file()
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    if isinstance(data, list):
        migrated: dict = {}
        for entry in data:
            fp = entry.get("file_path")
            if not fp:
                continue
            existing = migrated.get(fp) or {"attempts": 0, "history": []}
            existing["attempts"] = existing.get("attempts", 0) + 1
            existing["last_error_type"] = entry.get("error_type")
            existing["last_error_message"] = entry.get("error_message")
            existing["last_time"] = entry.get("time")
            existing.setdefault("history", []).append(
                {
                    "error_type": entry.get("error_type"),
                    "error_message": entry.get("error_message"),
                    "time": entry.get("time"),
                }
            )
            migrated[fp] = existing
        return migrated

    if isinstance(data, dict):
        return data

    return {}


def _save_failed_state(data: dict) -> None:
    os.makedirs(config.STATE_PATH, exist_ok=True)

    if len(data) > config.FAILED_STATE_CAP:
        sorted_items = sorted(
            data.items(),
            key=lambda kv: kv[1].get("last_time", ""),
            reverse=True,
        )
        data = dict(sorted_items[: config.FAILED_STATE_CAP])

    with open(_failed_state_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _record_failure(file_path: str, error: Exception) -> None:
    data = _load_failed_state()
    entry = data.get(file_path) or {"attempts": 0, "history": []}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["last_error_type"] = type(error).__name__
    entry["last_error_message"] = str(error)
    entry["last_time"] = now
    entry.setdefault("history", []).append(
        {
            "error_type": type(error).__name__,
            "error_message": str(error),
            "time": now,
        }
    )
    entry["history"] = entry["history"][-5:]
    data[file_path] = entry
    _save_failed_state(data)


def _clear_failure(file_path: str) -> None:
    data = _load_failed_state()
    if file_path in data:
        data.pop(file_path)
        _save_failed_state(data)


def _attempts_for(file_path: str) -> int:
    return _load_failed_state().get(file_path, {}).get("attempts", 0)


def _cleanup_stale_failures() -> None:
    data = _load_failed_state()
    if not data:
        return
    changed = False
    for fp in list(data.keys()):
        if os.path.exists(fp):
            continue
        if already_processed(fp):
            logger.info("清理失败记录（已处理）: %s", fp)
            data.pop(fp)
            changed = True
    if changed:
        _save_failed_state(data)


def _find_archived(src_path: str) -> str | None:
    """从 _processed.md frontmatter 或 06_Archive 目录找回原文件路径。"""
    processed = processed_path_for(src_path)
    if os.path.exists(processed):
        try:
            post = frontmatter.load(processed)
            ap = post.metadata.get("archived_path", "")
            if ap and os.path.exists(ap):
                return ap
        except Exception:
            pass

    # 按文件名在 06_Archive/<YYYY-MM>/ 里逐月倒序搜索
    basename = os.path.basename(src_path)
    if os.path.isdir(config.ARCHIVE_PATH):
        for month_dir in sorted(os.listdir(config.ARCHIVE_PATH), reverse=True):
            candidate = os.path.join(config.ARCHIVE_PATH, month_dir, basename)
            if os.path.exists(candidate):
                return candidate
    return None


def retry_failed() -> tuple[int, int]:
    """将 failed.json 里的文件全部恢复到 00_Inbox 准备重处理。

    对每条记录依次执行：
    1. 若源文件仍在 00_Inbox，无需恢复
    2. 否则从 _processed.md 的 archived_path 或 06_Archive 目录找回，移回 00_Inbox
    3. 删除对应的 _processed.md（若存在）
    4. 从 failed.json 清除记录

    返回 (recovered, skipped)。
    """
    os.makedirs(config.INBOX_PATH, exist_ok=True)
    data = _load_failed_state()
    if not data:
        logger.info("failed.json 为空，没有需要重试的文件")
        return 0, 0

    recovered = 0
    skipped = 0

    for src_path in list(data.keys()):
        # 1. 源文件是否已在 00_Inbox
        if os.path.exists(src_path):
            logger.info("源文件仍在 00_Inbox: %s", src_path)
        else:
            # 2. 找回归档文件
            archived = _find_archived(src_path)
            if not archived:
                logger.warning("找不到原文件，跳过: %s", src_path)
                skipped += 1
                continue
            try:
                shutil.move(archived, src_path)
                logger.info("已恢复: %s -> %s", archived, src_path)
            except Exception as e:
                logger.error("恢复失败 archived=%s err=%s", archived, e)
                skipped += 1
                continue

        # 3. 删除 _processed.md（若存在）
        processed = processed_path_for(src_path)
        if os.path.exists(processed):
            try:
                os.remove(processed)
                logger.info("已删除 processed: %s", processed)
            except Exception as e:
                logger.warning("删除 _processed.md 失败 err=%s", e)

        # 4. 清除 failed.json 记录
        data.pop(src_path)
        recovered += 1

    _save_failed_state(data)
    logger.info("retry_failed 完成：恢复 %s 个，跳过 %s 个", recovered, skipped)
    return recovered, skipped


# =========================
# 主入口
# =========================


def process_markdown(file_path: str) -> None:
    try:
        if looks_like_processed_artifact(file_path):
            logger.warning(
                "拒绝把流水线产物当源处理（文件名以 _processed.md 结尾）: %s",
                file_path,
            )
            return

        if already_processed(file_path):
            return

        attempts = _attempts_for(file_path)
        if attempts >= config.MAX_FAILURE_ATTEMPTS:
            logger.warning(
                "文件累计失败 %s 次，跳过（请手动清除 failed.json 后重试）: %s",
                attempts,
                file_path,
            )
            return

        logger.info("=" * 50)
        logger.info("开始处理: %s (历史失败=%s)", file_path, attempts)

        post = frontmatter.load(file_path)
        content = clean_text(post.content)

        if len(content) < config.URL_FETCH_THRESHOLD:
            content = _maybe_fetch_url(content)

        if len(content) < config.MIN_CONTENT_LENGTH:
            logger.info("内容太短，跳过: %s", file_path)
            return

        logger.info("调用 LLM...")
        ai_output, meta = ask_llm(content)
        meta["prompt_version"] = config.PROMPT_VERSION
        logger.info(
            "LLM 完成 duration_ms=%s tokens_in=%s tokens_out=%s",
            meta.get("duration_ms"),
            meta.get("tokens_in"),
            meta.get("tokens_out"),
        )

        if not ai_output or not ai_output.strip():
            raise ValueError("LLM 返回空内容（content=None 或空字符串），不写入 _processed.md 以便下次重试")

        processed_file = save_processed(file_path, ai_output, meta)

        concepts = extract_concepts(ai_output)
        definitions = extract_definitions(ai_output)
        relations = extract_relations(ai_output)
        logger.info("提取 Concepts: %s", concepts)

        # C 项：对已存在的概念，批量精炼定义（一次 LLM 调用，失败则退化保留原定义）
        existing_concepts = [
            c for c in concepts
            if (_wiki_file_for(c) and os.path.exists(_wiki_file_for(c)))
        ]
        refined_defs = refine_existing_definitions(existing_concepts, definitions)

        for concept in concepts:
            create_or_update_wiki(
                concept, file_path, definitions, relations, refined_defs
            )

        try:
            link_processed_to_daily(file_path, processed_file)
        except Exception as e:
            logger.warning("反向索引写回日记失败 file=%s err=%s", file_path, e)

        archived = archive_source(file_path)
        if archived:
            _update_archived_path(processed_file, archived)

        _clear_failure(file_path)
        logger.info("处理完成")

    except Exception as e:
        logger.error(
            "处理失败 file=%s err=%s\n%s", file_path, e, traceback.format_exc()
        )
        _record_failure(file_path, e)


def scan_inbox() -> None:
    """启动时对 00_Inbox 里没有对应 processed 文件的笔记补处理。"""
    if not os.path.isdir(config.INBOX_PATH):
        logger.warning("Inbox 目录不存在: %s", config.INBOX_PATH)
        return

    _cleanup_stale_failures()

    pending = []
    stale_processed = []
    skipped_artifacts = []
    for name in sorted(os.listdir(config.INBOX_PATH)):
        if not name.endswith(".md"):
            continue
        full = os.path.join(config.INBOX_PATH, name)
        if looks_like_processed_artifact(full):
            skipped_artifacts.append(full)
            continue
        if already_processed(full):
            stale_processed.append(full)
        else:
            pending.append(full)

    for f in skipped_artifacts:
        logger.warning(
            "冷启动扫描：跳过流水线产物（_processed.md 不该出现在 Inbox）: %s",
            f,
        )

    for f in stale_processed:
        logger.info("冷启动扫描：%s 已处理过，直接归档", f)
        archive_source(f)

    if not pending:
        logger.info("冷启动扫描：无未处理文件")
        return

    logger.info("冷启动扫描：发现 %s 个未处理文件", len(pending))
    for f in pending:
        process_markdown(f)
