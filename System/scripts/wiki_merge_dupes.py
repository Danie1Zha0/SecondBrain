#!/usr/bin/env python3
"""合并 04_Wiki 里的重复/变体词条（大小写、空格，可选单复数）。

把同一概念的多个变体文件合并成一个「规范」词条：
  - 定义：优先用非 TODO 的最佳定义。
  - Related：各变体并集去重（并把指向变体的链接统一成规范名）。
  - References：各变体并集去重。
  - 全库（04_Wiki / 03_Processed / 01_Daily）把 ``[[变体]]`` 改写为 ``[[规范]]``。
  - 删除多余变体文件，只留规范文件。

安全：
  - 默认 dry-run，仅出报告 System/logs/wiki_merge_report.md，不动文件。
  - --apply 才真正写回/删除。
  - --plural 才合并单复数差异（默认只合并大小写/空格差异，最稳）。

用法：
  python3 wiki_merge_dupes.py                 # dry-run，大小写/空格
  python3 wiki_merge_dupes.py --plural        # dry-run，含单复数
  python3 wiki_merge_dupes.py --plural --apply # 真正合并
"""

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from utils import logger  # noqa: E402
import pipeline as p  # noqa: E402


REPORT_FILE = os.path.join(config.LOG_PATH, "wiki_merge_report.md")
# 改写链接时扫描的目录（06_Archive 为历史归档，不动）
LINK_SCAN_DIRS = [config.WIKI_PATH, config.PROCESSED_PATH, config.DAILY_PATH]


def _key_space(name: str) -> str:
    return re.sub(r"\s+", "", name or "").lower()


def _key_plural(name: str) -> str:
    s = _key_space(name)
    # 仅对 ASCII 字母结尾的去英文复数 es/s
    if re.search(r"[a-z]s$", s):
        s = re.sub(r"es$|s$", "", s)
    return s


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _entry_stats(name: str):
    path = os.path.join(config.WIKI_PATH, f"{name}.md")
    text = _read(path)
    refs = p._read_section_links(text, "References")
    related = p._read_section_links(text, "Related")
    definition = p._read_definition(text)
    return {
        "name": name,
        "path": path,
        "text": text,
        "refs": refs,
        "related": related,
        "definition": definition,
        "has_def": 0 if p._is_weak_definition(definition) else 1,
    }


def _pick_canonical(stats: list) -> dict:
    """规范名优先级：有定义 > 首字母大写 > 含大写 > 引用多 > 名字短(偏单数)。

    名字本身的展示质量优先于引用数（内容反正会并集合并），避免选到全小写变体。
    """
    def score(s):
        name = s["name"]
        starts_upper = 1 if name[:1].isupper() else 0
        has_upper = 1 if re.search(r"[A-Z]", name) else 0
        return (s["has_def"], starts_upper, has_upper, len(s["refs"]), -len(name))
    return max(stats, key=score)


def _best_definition(stats: list, canonical: dict) -> str:
    if canonical["has_def"]:
        return canonical["definition"]
    cands = [s["definition"] for s in stats if s["has_def"]]
    return max(cands, key=len) if cands else canonical["definition"]


def _rewrite_links(variant_to_canon: dict, apply: bool) -> int:
    """全库把 [[变体]] / [[变体|别名]] / [[变体#锚]] 改写为规范名。返回改动文件数。"""
    changed_files = 0
    for d in LINK_SCAN_DIRS:
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                fp = os.path.join(root, fn)
                try:
                    text = _read(fp)
                except OSError:
                    continue
                new = text
                for variant, canon in variant_to_canon.items():
                    # [[variant]]、[[variant|x]]、[[variant#x]]，变体名按字面转义
                    pat = re.compile(
                        r"\[\[" + re.escape(variant) + r"(?=[\]\|#])"
                    )
                    new = pat.sub(f"[[{canon}", new)
                if new != text:
                    changed_files += 1
                    if apply:
                        try:
                            with open(fp, "w", encoding="utf-8") as f:
                                f.write(new)
                        except OSError as e:
                            logger.warning("链接改写写回失败 %s err=%s", fp, e)
    return changed_files


def _merge_group(stats: list, report: list, apply: bool) -> dict:
    """合并一组变体，返回 {变体名: 规范名}（不含规范→自身）。"""
    canonical = _pick_canonical(stats)
    canon_name = canonical["name"]
    variants = [s for s in stats if s["name"] != canon_name]

    # 合并 Related / References（排除自身与所有变体名）
    variant_names = {s["name"] for s in stats}
    merged_related = p._dedupe(
        [r for s in stats for r in s["related"] if r not in variant_names]
    )
    merged_refs = p._dedupe([r for s in stats for r in s["refs"]])
    merged_def = _best_definition(stats, canonical)

    report.append(f"### {canon_name}")
    report.append(f"- 合并: {', '.join(s['name'] for s in variants)} → **{canon_name}**")
    report.append(f"- 定义: `{merged_def}`")
    report.append(f"- Related({len(merged_related)}) / References({len(merged_refs)})")
    report.append("")

    if apply:
        text = canonical["text"]
        if merged_def:
            text = p._replace_section_body(text, "Definition", merged_def)
        text = p._replace_section_body(
            text, "Related", "\n".join(f"- [[{r}]]" for r in merged_related)
        )
        text = p._replace_section_body(
            text, "References", "\n".join(f"- [[{r}]]" for r in merged_refs)
        )
        with open(canonical["path"], "w", encoding="utf-8") as f:
            f.write(text)
        for s in variants:
            try:
                os.remove(s["path"])
            except OSError as e:
                logger.warning("删除变体失败 %s err=%s", s["path"], e)

    return {s["name"]: canon_name for s in variants}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正合并（默认 dry-run）")
    ap.add_argument("--plural", action="store_true", help="同时合并英文单复数差异")
    args = ap.parse_args()

    keyfn = _key_plural if args.plural else _key_space
    names = [f[:-3] for f in os.listdir(config.WIKI_PATH) if f.endswith(".md")]
    groups = collections.defaultdict(list)
    for n in names:
        groups[keyfn(n)].append(n)
    dups = {k: v for k, v in groups.items() if len(v) > 1}

    mode = "APPLY" if args.apply else "DRY-RUN"
    key_desc = "大小写/空格/单复数" if args.plural else "大小写/空格"
    report = [
        f"# Wiki 去重合并报告 [{mode}]  分组键: {key_desc}",
        f"重复组: {len(dups)}",
        "",
    ]

    variant_to_canon: dict = {}
    for _k, members in sorted(dups.items()):
        stats = [_entry_stats(n) for n in members]
        variant_to_canon.update(_merge_group(stats, report, args.apply))

    rewritten = _rewrite_links(variant_to_canon, args.apply) if variant_to_canon else 0
    report.insert(2, f"合并变体 {len(variant_to_canon)} 个，链接改写文件 {rewritten} 个\n")

    os.makedirs(config.LOG_PATH, exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print(f"[wiki_merge] {mode} {key_desc}：重复组 {len(dups)}，合并变体 {len(variant_to_canon)}，链接改写文件 {rewritten}")
    print(f"[wiki_merge] 报告：{REPORT_FILE}")


if __name__ == "__main__":
    main()
