#!/usr/bin/env python3
"""一次性 Wiki 清洗（D 项）。

对 04_Wiki 存量词条做两件事：
  1. 定义：refine/补写（TODO 仅在 LLM 确信时补，不编造）。
  2. Related：用 LLM 做语义过滤，只保留真正相关的项——**只删不增**
     （过滤结果与原有 Related 取交集，杜绝幻觉新增链接）。

特性：
  - 默认 dry-run：只写报告 System/logs/wiki_cleanup_report.md，不改文件。
  - --apply 才真正写回。
  - 断点续跑：已处理的记到 System/state/wiki_cleanup_done.json，重跑自动跳过。
  - 批量：每次 LLM 调用处理 --batch 个词条，降调用数。
  - --limit N：只处理前 N 个（先小样本验证）。

用法：
  python3 wiki_cleanup.py              # dry-run 全量，出报告
  python3 wiki_cleanup.py --limit 10   # dry-run 前 10 个
  python3 wiki_cleanup.py --apply      # 真正写回（可反复跑，断点续）
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from utils import logger  # noqa: E402
from llm import chat  # noqa: E402
import pipeline as p  # noqa: E402


DONE_FILE = os.path.join(config.STATE_PATH, "wiki_cleanup_done.json")
REPORT_FILE = os.path.join(config.LOG_PATH, "wiki_cleanup_report.md")

SYSTEM_PROMPT = (
    "你是知识库词条清洗助手。我给你若干词条，每个含：概念名、现有定义、候选相关概念。\n"
    "对每个词条做两件事：\n"
    "1) 定义：给出准确、通用、与具体文章无关、不超过50字的中文定义。\n"
    "   - 现有定义不错就微调或原样保留。\n"
    "   - 现有定义是 TODO 或空：仅在你确信该概念含义时才补写；不确定就原样输出 TODO，绝不编造。\n"
    "2) 相关：只保留与该概念**真正语义相关**（同主题/上下位/技术或因果相关）的候选项，删掉无关项。\n"
    "   - 只能从给定候选里选，绝不新增候选之外的概念。宁缺毋滥。\n"
    "严格按下面格式逐条输出，不要任何额外文字、不要解释：\n"
    "@@序号\n"
    "DEF: <定义或TODO>\n"
    "REL: [[a]], [[b]]\n"
)


def _load_done() -> set:
    if not os.path.exists(DONE_FILE):
        return set()
    try:
        with open(DONE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_done(done: set) -> None:
    os.makedirs(config.STATE_PATH, exist_ok=True)
    with open(DONE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f, ensure_ascii=False, indent=2)


def _norm_def(s: str) -> str:
    """归一化定义用于比较：去所有空白 + 去句末标点。"""
    s = re.sub(r"\s+", "", s or "")
    return s.rstrip("。.;；，,")


def _read_title(text: str, fallback: str) -> str:
    m = re.search(r"^#[ \t]+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def _build_user_block(idx: int, title: str, definition: str, related: list) -> str:
    rel = ", ".join(f"[[{r}]]" for r in related) if related else "（无）"
    return (
        f"@@{idx}\n"
        f"概念: {title}\n"
        f"现有定义: {definition or 'TODO'}\n"
        f"候选相关: {rel}\n"
    )


def _parse_response(text: str) -> dict:
    """解析 LLM 输出，返回 {序号: {'def': str, 'rel': [names]}}。

    兼容两种格式：每条 ``@@N`` 独占多行，或整条 ``@@N DEF: ... REL: ...`` 挤在一行。
    每条从 ``@@N`` 取到下一个 ``@@N`` 或文末；DEF 取到 REL 之前。
    """
    out: dict[int, dict] = {}
    for m in re.finditer(r"@@\s*(\d+)(.*?)(?=@@\s*\d+|\Z)", text or "", re.DOTALL):
        n = int(m.group(1))
        body = m.group(2)
        dm = re.search(r"DEF:\s*(.*?)(?=REL:|\Z)", body, re.DOTALL)
        rm = re.search(r"REL:\s*(.*)", body, re.DOTALL)
        definition = dm.group(1).strip() if dm else ""
        rel = re.findall(r"\[\[(.*?)\]\]", rm.group(1)) if rm else []
        out[n] = {"def": definition.strip(), "rel": [r.strip() for r in rel if r.strip()]}
    return out


def _collect_entries(limit: int | None, done: set):
    files = sorted(
        f for f in os.listdir(config.WIKI_PATH) if f.endswith(".md")
    )
    entries = []
    for fname in files:
        if fname in done:
            continue
        path = os.path.join(config.WIKI_PATH, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        title = _read_title(text, fname[:-3])
        definition = p._read_definition(text)
        related = p._read_section_links(text, "Related")
        entries.append(
            {
                "fname": fname,
                "path": path,
                "title": title,
                "definition": definition,
                "related": related,
            }
        )
        if limit and len(entries) >= limit:
            break
    return entries


def _apply_entry(entry: dict, result: dict) -> tuple[bool, dict]:
    """返回 (changed, change_detail)。change_detail 用于报告。"""
    path = entry["path"]
    orig_related = entry["related"]
    orig_def = entry["definition"]
    title = entry["title"]

    new_def_raw = (result.get("def") or "").strip()
    # Related 只做交集过滤（只删不增），并去掉自身
    orig_set = set(orig_related)
    filtered = p._dedupe(
        [r for r in result.get("rel", []) if r in orig_set and r != title]
    )

    # 定义择优：LLM 给了非 TODO 的非空定义就用，否则保留原值
    if new_def_raw and not p._is_weak_definition(new_def_raw):
        final_def = new_def_raw
    else:
        final_def = orig_def

    # 只在「去空白 + 去句末标点」后仍不同才算真改动，避免纯标点/空格的无谓 churn
    def_changed = (
        bool(final_def.strip())
        and _norm_def(final_def) != _norm_def(orig_def)
    )
    if not def_changed:
        final_def = orig_def
    # Related 变化：过滤后与原集合不同（通常是删减）
    rel_changed = filtered != orig_related

    detail = {
        "fname": entry["fname"],
        "title": title,
        "old_def": orig_def,
        "new_def": final_def if def_changed else None,
        "removed_rel": [r for r in orig_related if r not in filtered],
        "kept_rel": filtered,
        "rel_changed": rel_changed,
    }

    if not (def_changed or rel_changed):
        return False, detail
    return True, detail


def _write_entry(entry: dict, detail: dict) -> None:
    path = entry["path"]
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if detail["new_def"]:
        text = p._replace_section_body(text, "Definition", detail["new_def"])
    if detail["rel_changed"]:
        body = "\n".join(f"- [[{r}]]" for r in detail["kept_rel"])
        text = p._replace_section_body(text, "Related", body)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _report_line(detail: dict) -> str:
    parts = [f"### {detail['title']}  ({detail['fname']})"]
    if detail["new_def"]:
        parts.append(f"- 定义: `{detail['old_def']}` → `{detail['new_def']}`")
    if detail["removed_rel"]:
        parts.append("- 删除无关 Related: " + ", ".join(detail["removed_rel"]))
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写回（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 个词条")
    ap.add_argument("--batch", type=int, default=8, help="每次 LLM 调用处理多少词条")
    ap.add_argument("--sleep", type=float, default=1.5, help="批次间隔秒（限速）")
    ap.add_argument("--max-retries", type=int, default=5, help="单批最大重试次数")
    ap.add_argument("--backoff", type=float, default=15.0, help="429 退避基数秒")
    ap.add_argument("--backoff-max", type=float, default=120.0, help="429 退避上限秒")
    args = ap.parse_args()

    done = _load_done() if args.apply else set()
    entries = _collect_entries(args.limit, done)
    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("wiki_cleanup [%s] 待处理 %s 个词条 batch=%s", mode, len(entries), args.batch)

    if not entries:
        print(f"[wiki_cleanup] {mode}: 没有待处理词条")
        return

    os.makedirs(config.LOG_PATH, exist_ok=True)
    report = [f"# Wiki 清洗报告 [{mode}]  {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    changed_count = 0
    processed = 0

    aborted = False
    for start in range(0, len(entries), args.batch):
        batch = entries[start : start + args.batch]
        user = "\n".join(
            _build_user_block(i, e["title"], e["definition"], e["related"])
            for i, e in enumerate(batch)
        )
        # 限流（429）韧性：失败时长退避并重试同一批；连续失败则优雅停止，
        # 剩余词条保持未完成、下次重跑自动续上，绝不空转烧请求。
        text = None
        for retry in range(args.max_retries):
            try:
                text, _meta = chat(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user},
                    ]
                )
                break
            except Exception as e:
                wait = min(args.backoff * (2 ** retry), args.backoff_max)
                logger.warning(
                    "批次 LLM 调用失败 start=%s retry=%s/%s err=%s，退避 %ss",
                    start, retry + 1, args.max_retries, e, wait,
                )
                time.sleep(wait)
        if text is None:
            logger.warning("批次 start=%s 持续失败，停止本次运行（剩余下次续跑）", start)
            aborted = True
            break

        results = _parse_response(text)
        for i, entry in enumerate(batch):
            res = results.get(i)
            processed += 1
            if not res:
                logger.warning("未解析到结果：%s", entry["fname"])
                continue
            changed, detail = _apply_entry(entry, res)
            if changed:
                changed_count += 1
                report.append(_report_line(detail))
                report.append("")
                if args.apply:
                    try:
                        _write_entry(entry, detail)
                    except OSError as we:
                        logger.warning("写回失败 %s err=%s", entry["fname"], we)
                        continue
            if args.apply:
                done.add(entry["fname"])

        if args.apply:
            _save_done(done)
        logger.info(
            "进度 %s/%s 改动累计 %s", min(start + args.batch, len(entries)), len(entries), changed_count
        )
        if args.sleep:
            time.sleep(args.sleep)

    report.insert(1, f"处理 {processed} 个，建议/已改动 {changed_count} 个\n")
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    status = "中断（限流，剩余下次续跑）" if aborted else "完成"
    print(f"[wiki_cleanup] {mode} {status}：处理 {processed}，改动 {changed_count}")
    print(f"[wiki_cleanup] 报告：{REPORT_FILE}")


if __name__ == "__main__":
    main()
