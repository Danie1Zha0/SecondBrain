# SecondBrain — Agent 上下文存档

供下一个 Agent 在没有历史会话的情况下快速恢复上下文。
配套阅读：`System/README.md`（面向用户使用文档）。

> 上次更新：2026-05-22

## 1. 项目目标

一个 Obsidian Vault + 本地/远程 LLM 的自动化"第二大脑"。

两条主流水线：

**A. 笔记入站流水线**：监听 `00_Inbox/`，对新增 Markdown 自动：
1. 摘要 + 概念提取 + 概念定义
2. 生成 `03_Processed/<stem>_processed.md`
3. 为每个概念创建/更新 `04_Wiki/<concept>.md`
4. 反向索引：把 `[[xxx_processed]] <- [[xxx]]` 追加到对应日记的 `## Captured`
5. 把原文件归档到 `06_Archive/<YYYY-MM>/`

**B. 日记三件套**：
1. **Quick Capture 分拣**：扫描日记 `## Inbox` 的 `- [ ]` 项 → 在 `00_Inbox` 落新 .md（带 `captured_from_date` 等 frontmatter）→ 把日记里那行勾上 `- [x]`
2. **AI Day Summary**：把当日日记 + 当日 `03_Processed` 的 # Summary 段喂给 LLM，写到日记 `## AI Day Summary` 的标记块之间
3. **Captured 反向索引**：A 的第 4 步

用户优先级：稳定性 > 自动化程度 > 复杂特性。允许 LLM 偶发失败，但流水线本身不能崩。

## 2. 项目结构

```
SecondBrain/
├── 00_Inbox/             # 新笔记入口，pipeline 监听
├── 01_Daily/             # 日记
├── 02_Projects/
├── 03_Processed/         # AI 摘要输出（去重锚点）
├── 04_Wiki/              # 概念百科
├── 05_Resources/
├── 06_Archive/<YYYY-MM>/ # 处理成功后原文件移动到这里
├── Attachments/
├── .env                  # API key 等敏感配置（不入 Git）
├── .gitignore
└── System/
    ├── README.md         # 用户文档
    ├── CONTEXT.md        # 本文件
    ├── scripts/
    │   ├── ai_pipeline.py    # 入口（CLI argparse: --all / --capture / --scan / --summary）
    │   ├── config.py         # 路径 / 模型 / 阈值 / 标记常量（含详细注解）
    │   ├── utils.py          # logger / sanitize_filename / wait_for_file_stable
    │   ├── llm.py            # ask_llm（OpenAI 兼容 + Ollama，含超时/重试）
    │   ├── capture.py        # URL 抓取（requests + trafilatura）
    │   ├── pipeline.py       # 主流程：处理/Wiki/归档/失败队列 + _processed.md 防御
    │   ├── watcher.py        # watchdog 监听 + 启动钩子（capture + 补昨日 + 冷扫描）
    │   ├── daily.py          # 日记路径 / 模板创建 / 段落读写 / 标记块替换 / 模板对比
    │   ├── inbox_sort.py     # Quick Capture 分拣（slug 规则化）
    │   ├── day_summary.py    # AI 日总结（标记块写入 / 空模板跳过 / 补昨日）
    │   └── reverse_index.py  # 处理结果反向写回日记 ## Captured
    ├── prompts/
    │   ├── wiki_prompt.txt          # 入站流水线系统提示词（带版本号）
    │   └── day_summary_prompt.txt   # 日总结系统提示词（带版本号）
    ├── templates/
    │   ├── daily.md          # 含 ## Inbox / ## Captured / ## AI Day Summary 标记块
    │   └── markor.md         # Markor 移动端模板
    ├── logs/                 # pipeline.log + 滚动备份
    └── state/
        └── failed.json       # 失败队列（按 file_path 聚合）
```

## 3. CLI 接口

```
python System/scripts/ai_pipeline.py                        # watcher 模式（默认）
python System/scripts/ai_pipeline.py --all                  # 一次跑完今天全套，退出
python System/scripts/ai_pipeline.py --capture [DATE]       # 分拣某日 daily 的 ## Inbox
python System/scripts/ai_pipeline.py --scan                 # 冷扫描一次 00_Inbox
python System/scripts/ai_pipeline.py --summary [DATE] [--force]  # 写当日 AI 总结
```

子命令组合时执行顺序：**capture → scan → summary**（保证 summary 看到本次处理产物）。

`--all` = `sort_today_inbox` → `ensure_yesterday_summary` → `scan_inbox` → `summarize_day(today)`，每步独立 try/except，错了只 warn 不中断。

watcher 模式启动钩子：`sort_today_inbox()` → `ensure_yesterday_summary()` → `scan_inbox()`，再进入 watch 循环。

## 4. 数据流

### 4.1 笔记入站（pipeline.process_markdown）

```
00_Inbox/foo.md (new) → watcher (on_created/modified/moved)
   ↓
filename stem 以 _processed 结尾？           # 防御：避免把流水线产物当源
   └── 是 → warn + return（不调 LLM、不归档）
   ↓
wait_for_file_stable                          # 大小连续稳定再处理
   ↓
already_processed?                            # 03_Processed/foo_processed.md 存在？
   └── 是 → 跳过
   ↓
attempts >= MAX_FAILURE_ATTEMPTS?              # failed.json 累计 >=3 次？
   └── 是 → 跳过 + 日志告警
   ↓
frontmatter.load → clean_text
   ↓
len(content) < URL_FETCH_THRESHOLD(200) 且含 URL？
   └── 是 → requests.get(timeout=15) → trafilatura.extract → 合并
   ↓
len < MIN_CONTENT_LENGTH(50) → 跳过
   ↓
ask_llm                                        # ollama 或 OpenAI 兼容
   - OpenAI SDK max_retries=0
   - 我方手动重试 REMOTE_MAX_RETRIES 次，指数退避 1-2-4-8s
   - 仅对 APITimeoutError/APIConnectionError/RateLimitError/5xx/408/429 重试
   ↓
save_processed → archived_path: 占位
   ↓
extract_concepts + extract_definitions
   ↓
for concept in concepts: create_or_update_wiki
   - 不存在 → 新建（Definition 由 LLM 给，缺省 "TODO"）
   - 存在 → _append_reference 追加 References（去重）
   ↓
link_processed_to_daily                        # 反向索引到日记 ## Captured
   - 优先用源 frontmatter 的 captured_from_date
   - 无则用今天
   - 日记不存在则按模板创建（lazy）
   ↓
archive_source → 06_Archive/<YYYY-MM>/foo.md（冲突追加 _1/_2…）
   ↓
_update_archived_path                          # 按行替换，不再用 re.subn（\N bug 教训）
   ↓
_clear_failure
```

异常路径：任何步骤抛错 → `_record_failure` 写入 `failed.json`。

### 4.2 Quick Capture 分拣（inbox_sort.sort_daily_inbox）

```
读 daily 的 ## Inbox 段
  ↓
找 - [ ] 行（已勾选 [x] 的跳过）
  ↓
for each 未勾选项:
  text 含 URL？
    是 → slug = host-firstpath（剥 www./m./mobile.，去常见后缀，非字母数字汉字 → '-'）
    否 → slug = 文本前 30 字（清掉 URL，sanitize_filename）
  ↓
  生成 00_Inbox/<时间戳>-<slug>.md
    frontmatter:
      type: capture
      captured_from: daily
      captured_from_date: <YYYY-MM-DD>
      captured_at: <full timestamp>
      source: <url 或空>
      tags: [quick-capture]
  ↓
  把日记中该行的 [ ] 改为 [x]
  ↓
全部分拣完一次性 replace_section
```

### 4.3 AI Day Summary（day_summary.summarize_day）

```
ensure_daily(date) → 路径
  ↓
existing = _existing_ai_block(daily_file)      # 标记之间已有内容？
  ↓
existing 非空且非 force → 跳过
  ↓
processed_items = 当日 03_Processed 列表（按 frontmatter.processed_time）
  ↓
processed_items 空且 user_wrote_anything 为 False → 跳过
  （user_wrote_anything = 与渲染后模板对比，去除 Inbox/Captured/AI Day Summary
    三段后是否还有差异）
  ↓
build_user_content（日记原文 + 每个 processed 的 # Summary 段）
  ↓
ask_llm（用 day_summary_prompt.txt + DAILY_SUMMARY_PROMPT_VERSION）
  ↓
replace_marked_block（仅替换 <!-- ai-summary:start --> 与 :end 之间）
  - 标记齐全 → 字符串切片精确替换
  - 标记缺失 → 把整个标记块追加到 ## AI Day Summary 段尾（兜底，不破坏现有内容）
```

## 5. 关键决策（why）

| 决策 | 原因 |
|---|---|
| 用 `03_Processed/<stem>_processed.md` 存在判定去重 | 进程重启不丢；删该文件即触发重处理；零额外存储 |
| 仅 watcher 触发时做 `wait_for_file_stable`，冷扫描不做 | 冷扫描时文件已经稳定，没必要再等 |
| OpenAI SDK `max_retries=0`，自己实现外层重试 | SDK 内部 3 次 × 60s 会和我们的重试嵌套，单次失败被放大到 9+ 分钟 |
| `_is_retryable` 用 `isinstance(openai.APITimeoutError, ...)` 而非纯关键词 | OpenAI 抛 "Request timed out."，关键词 "timeout" 无法匹配 "timed out"，曾经踩过 |
| `failed.json` 用 dict-by-path 而非 list | 支持累计计数、原地更新、按路径清除 |
| 归档后**回写** `archived_path` 而不是写归档后一次 | processed 文件提前写好是去重锚点，必须先于归档存在 |
| `_update_archived_path` 按行扫描替换，不用 `re.subn` | replacement 字符串里 `\N \w` 等会被 re 当成特殊转义；Windows 路径里 `\N` 必触发 `bad escape \N`。不是假设是真踩过：3 个文件假失败、8 个文件被二次处理（_processed_processed.md） |
| 失败的文件**不归档** | 保留在 inbox 是用户能直接看见的"有问题"信号 |
| 同名归档冲突追加 `_1/_2…` | 不覆盖、不报错、不阻塞主流程 |
| 拒绝处理 `_processed.md` 文件（pipeline / scan / watcher 三层守卫） | 同步工具或手滑可能把流水线产物搬进 `00_Inbox`；曾经一波处理掉了 8 个文件、烧了几千 token |
| AI Day Summary 用 `<!-- ai-summary:start/end -->` 标记块 | 用户可在标记外（同段、跨段）自由编辑；重生只覆盖标记内 |
| 空模板检测：`user_wrote_anything` 对比渲染后模板 | 不调一次毫无内容的 LLM；剥掉 Inbox/Captured/AI Day Summary 三个系统维护段落后比对 |
| 日记 lazy 创建（不在零点定时建） | 避免空模板污染；按需在 capture/summary/reverse_index 触发时建 |
| Quick Capture slug 规则化（非 LLM） | 零成本零延迟；URL 取 host+path，纯文本取首 30 字 |
| LLM 输入不做长度上限 | 用户明确选择"依赖远端报错"，不引入 tokenizer 依赖 |
| URL 抓取用 `requests + trafilatura.extract` 而非 `trafilatura.fetch_url` | 显式控制 timeout=15s；trafilatura 内置 fetch 在反爬场景不够稳 |

## 6. 配置约定

- **环境变量**（可在 `.env` 中覆盖）：`VAULT_PATH / LLM_PROVIDER / OLLAMA_MODEL_NAME / REMOTE_API_KEY / REMOTE_BASE_URL / REMOTE_MODEL_NAME / REMOTE_TIMEOUT / REMOTE_MAX_RETRIES`
- **代码常量**（改 `config.py`）：阈值区所有参数，详见文件内中文注解
- **段落标题**全部走 `config.DAILY_INBOX_HEADER / DAILY_CAPTURED_HEADER / DAILY_SUMMARY_HEADER` 常量；改名只改一处
- **AI Day Summary 标记**走 `config.DAILY_SUMMARY_MARK_START / MARK_END`；改这两个会让旧日记里的旧标记失效（变成游离文本，但仍可被兜底路径补救）
- **提示词**：改 `System/prompts/<...>_prompt.txt`，同步把 `config.py` 的对应版本号常量改成当天日期
- **当前 LLM 提供方**：远程，OpenAI 兼容接口（具体 base_url 与模型名见用户的 `.env`，不要把它写进代码或文档）

## 7. 已知设计取舍 / 未做事项

- **on_modified 防抖未做**：同步工具多次触发会调多次 `process_markdown`，但 `already_processed` 会快速返回，仅 CPU 浪费
- **LLM 输入长度无上限**：超大文档可能爆 context window，依赖远端报错
- **多文件并发处理未做**：所有处理串行；单用户日常笔记体量足够
- **CLI 重处理工具未做**：当前手动重处理要删 `03_Processed/<stem>_processed.md` + 把文件从 `06_Archive` 拷回 `00_Inbox`；如需常用建议加 `--reprocess <stem>` 参数
- **wiki 的 `Related` 板块在追加时不更新**：第二篇笔记引用同一概念只会追加 `## References`，不会把它的其它 concepts 加到该 wiki 的 `## Related`
- **OLLAMA 路径未实测**：远程路径用得多，本地 ollama 仅模块结构兼容，未做端到端验证
- **Quick Capture 不分拣历史日记**：`sort_today_inbox` 只看今天；想分拣旧的用 `--capture YYYY-MM-DD`
- **AI Day Summary 不支持 streaming**：一次性返回；卡顿时用户看不到进度
- **_processed.md 守卫只看文件名后缀**：理论上有人可以故意命名 `xxx_processed.md` 但实际是新笔记；目前可接受这个误伤换稳定

## 8. 编码风格 / 约定

- 全部 UTF-8；文件 IO 一律 `encoding="utf-8"`
- 日志走 `utils.logger`，禁用 `print`（除冒烟脚本）
- 用户可见的提示信息用中文，代码注释允许英中混合
- 不在代码里硬编码 API Key / base_url；都从 `os.environ` 读
- 不主动新增 `requirements.txt`，依赖直接用 `pip install`（用户偏好）
- 文件写入操作前一律 `os.makedirs(..., exist_ok=True)`
- 异常一律打 `traceback.format_exc()`，便于事后追因
- **不要在 `re.sub / re.subn` 的 replacement 参数里直接传含路径或用户输入的字符串**——用 lambda 或换成按行替换。`\N \w \D` 这些都是合法转义会报错
- 写新模块时尽量做"段落操作不破坏未知文本"原则：能用切片就别用大正则替换整段

## 9. 给下一个 Agent 的快速上手

1. 先读 `System/README.md` 掌握用户面接口
2. 改代码前先读本文件第 5 节"关键决策"，避免重蹈覆辙
3. 不要碰 `.env`、`03_Processed/*.md`、`04_Wiki/*.md`、`01_Daily/*.md`（除非用户授权）、`.obsidian/`
4. 涉及多种实现方案时，先用 `AskQuestion` 让用户选；用户明确说过"不要自己做假设"
5. 改完模块后建议跑一次冒烟（写 `System/scripts/_smoke_*.py` 调用核心函数，跑完删掉）。冒烟脚本不要碰真实 vault，用 `tempfile.mkdtemp()` + `os.environ["VAULT_PATH"]=` 隔离
6. 调试错误时用户偏好"先看 terminal/log 实证再下结论"，避免凭代码猜测
7. 在 Agent 模式才能写文件；Ask 模式禁止写入
8. PowerShell 控制台中文会乱码，但 `pipeline.log` 是真 UTF-8，看日志为准
9. 单纯做实验性 Python 脚本时小心 `\N` 这种 unicodeescape——docstring 用 raw string `r"""..."""`，路径常量 raw 化
10. 上下文快用完时主动更新本文件（用户提过这条要求）
