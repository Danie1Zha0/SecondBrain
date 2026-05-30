# SecondBrain AI Pipeline

Obsidian 知识库的自动化笔记处理流水线。监听 `00_Inbox`，调用本地或远程 LLM 生成摘要与 Wiki 概念页。

## 目录约定

- `00_Inbox/` 收件箱，新笔记入口
- `01_Daily/` 日记
- `02_Projects/` 进行中的项目
- `03_Processed/` AI 处理后的摘要
- `04_Wiki/` 概念百科
- `05_Resources/` 参考资料
- `06_Archive/` 归档
- `Attachments/` 附件
- `System/`
  - `scripts/` Python 脚本
  - `prompts/` LLM 提示词
  - `templates/` Obsidian 模板
  - `logs/` 运行日志（自动滚动）
  - `state/` 失败队列等状态文件

## 模块结构

```
System/scripts/
  ai_pipeline.py    # CLI 入口（watcher / --capture / --summary / --scan）
  config.py         # 路径与环境变量
  utils.py          # 日志 / 文件名清洗 / 文件稳定检测
  llm.py            # ask_llm（超时、重试、usage）
  capture.py        # URL 抓取（trafilatura）
  pipeline.py       # 主处理流程
  watcher.py        # 文件监听 + 冷启动扫描 + 启动钩子
  daily.py          # 日记路径与段落读写
  inbox_sort.py     # Quick Capture 分拣（日记 Inbox -> 00_Inbox）
  day_summary.py    # AI 日总结
  reverse_index.py  # 处理结果反向写回日记 ## Captured
```

## 依赖

**PC（完整功能）：**

```
pip install python-frontmatter watchdog ollama openai python-dotenv trafilatura requests
```

**Android / Termux（无 watcher、无 URL 正文抓取）：**

```
pip install python-frontmatter openai python-dotenv requests
```

`trafilatura` 和 `watchdog` 是可选依赖，不安装时对应功能静默降级（URL 抓取跳过、不能用 watcher 模式），不影响 `--scan / --all` 等一次性命令。

## 启动

默认 watcher 模式：

```
python System/scripts/ai_pipeline.py
```

启动顺序：

1. 初始化 logger（写到 `System/logs/pipeline.log`，自动滚动）
2. 启动 watchdog 监听 `00_Inbox`
3. 启动钩子：
   - 分拣今天日记 `## Inbox` 的未勾选项 → 写到 `00_Inbox` 并自动勾上 `- [x]`
   - 检测昨天日记的 `## AI Day Summary`，缺则补写一次
4. 冷启动扫描：对 `00_Inbox` 里没有对应 `_processed.md` 的笔记，自动补处理
5. 持续监听新文件

一次性子命令（不启动 watcher）：

```
python System/scripts/ai_pipeline.py --all                # 一次跑完今天全套，跑完退出
python System/scripts/ai_pipeline.py --capture            # 分拣今天日记的 Inbox
python System/scripts/ai_pipeline.py --capture 2026-05-20 # 分拣指定日期
python System/scripts/ai_pipeline.py --summary            # 生成今日 AI 总结
python System/scripts/ai_pipeline.py --summary 2026-05-21 --force  # 覆盖指定日期总结
python System/scripts/ai_pipeline.py --scan               # 冷扫描一次 00_Inbox
python System/scripts/ai_pipeline.py --retry              # 恢复 failed.json 里的文件到 00_Inbox 并清除记录
python System/scripts/ai_pipeline.py --retry --scan       # 恢复后立即重处理
```

`--all` 等价于按以下顺序串起来跑：

1. `sort_today_inbox()` —— 分拣今天 daily 的 `## Inbox`
2. `ensure_yesterday_summary()` —— 昨天没总结就补
3. `scan_inbox()` —— 把 `00_Inbox` 全部未处理笔记跑一遍
4. `summarize_day()` —— 给今天写 `## AI Day Summary`（标记块已有内容时跳过；要重生加 `--force`）

任一步出错只 warn 不中断，跑完即退出。

`--capture / --scan / --summary` 也可以叠在一起用，固定按 capture → scan → summary 顺序执行（确保 summary 能看到 scan 刚处理出来的笔记）。

## 配置

工程根目录的 `.env`：

```
LLM_PROVIDER=remote           # 或 ollama
REMOTE_API_KEY=...
REMOTE_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
REMOTE_MODEL_NAME=glm-4
REMOTE_TIMEOUT=60
REMOTE_MAX_RETRIES=3
OLLAMA_MODEL_NAME=qwen3:4b

# 采样参数（可选，给出的是默认值）
LLM_TEMPERATURE=0.2
LLM_TOP_P=0.9
```

- `LLM_PROVIDER=ollama` 时使用本地 Ollama，需要 `ollama serve` 已运行。
- `LLM_PROVIDER=remote` 时使用 OpenAI 兼容接口（OpenAI / 智谱 / DeepSeek / Kimi / NVIDIA 等）。
- `REMOTE_TIMEOUT` 单次请求超时秒数；`REMOTE_MAX_RETRIES` 仅对 5xx / 408 / 429 / 超时等可重试错误生效，指数退避。

### 采样参数建议

知识整理任务（wiki 摘要、日总结）属于抽取式输出，偏向稳定。默认值已经按此场景调过：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `LLM_TEMPERATURE` | `0.2` | 低温度 → 同一篇文章每次提到的概念名一致。智谱 GLM 要求 > 0，所以最低用 `0.1`。 |
| `LLM_TOP_P` | `0.9` | nucleus sampling，与 temperature 并存，多数 provider 取它认可的那个。 |

输出 token 数不做限制，让模型自然结束（遵循 prompt 格式指令后会在 `# Related` 处停止），依赖 provider 自身的 context window 保护。截断会破坏结构化输出，得不偿失。

## 工作流

1. 新增 `.md` 到 `00_Inbox`（手动拖入 / Markor 落地 / Quick Capture 分拣）
2. watcher 等文件大小稳定后读取内容
3. 正文 < 200 字且包含 URL 时，用 `trafilatura` 抓取网页正文并合并
4. 调用 LLM 生成 Summary / Concepts / Definitions / Key Ideas / Related
5. 写入 `03_Processed/<name>_processed.md`，frontmatter 含 provider / model / prompt_version / tokens / duration_ms
6. 对每个概念：
   - 若 `04_Wiki/<concept>.md` 不存在，新建并写入 LLM 给出的 Definition
   - 若已存在，把当前笔记追加到该 wiki 的 `## References`（去重）
7. 反向索引：按源文件 frontmatter 的 `captured_from_date` 找到对应日记（无则今天），把 `- [[<processed>]] <- [[<source>]]` 追加到日记 `## Captured`
8. 处理成功后把原始文件从 `00_Inbox` 移动到 `06_Archive/<YYYY-MM>/`；同名冲突时追加 `_1 / _2 ...`

## 日记三件套

### Quick Capture 分拣

- 在日记 `## Inbox` 段写 `- [ ] 文本` 或 `- [ ] https://xxx`
- 启动 pipeline 或 `--capture` 时扫描所有未勾选项：
  - 生成 slug（URL 取 `host-path`，纯文本取首 30 字），加时间戳前缀避免重名
  - 在 `00_Inbox/` 生成新文件，frontmatter 写入 `captured_from: daily`、`captured_from_date: <YYYY-MM-DD>`
  - 日记里对应行被替换为 `- [x] ...`
- 处理完成后该笔记自动出现在 `## Captured`，形成「写一行 → 文章变 wiki → 回链日记」的闭环

### AI 日总结

- 触发：`--summary [DATE]`，或 pipeline 启动时检测到昨日缺总结自动补写
- 输入：日记原文 + 当日 `03_Processed` 里所有笔记的 # Summary 段
- 输出：写到 `## AI Day Summary` 段里的标记块之间：

  ```
  ## AI Day Summary

  > 标记之间的内容由 AI 自动生成，重新生成时会被覆盖。标记之外（含本行提示）你可以自由编辑，不会被改动。

  <!-- ai-summary:start -->
  ...AI 内容...
  <!-- ai-summary:end -->
  ```
- **隔离规则**：只有 `<!-- ai-summary:start -->` 与 `<!-- ai-summary:end -->` 之间的内容会被覆盖；标记外（同段落里的人工补充、提示文字、frontmatter、其它任何段落）一律不动。
- **空模板跳过**：当用户没在日记里写任何实质内容（仅模板默认结构）且当日 `03_Processed` 没有产出时，不调 LLM。
- 标记块已有内容时跳过；`--force` 覆盖。
- 用户不小心删掉了标记？回退路径会把新的标记块追加到 `## AI Day Summary` 段末尾，已有的文字不受影响。

### Captured 反向索引

- pipeline 处理成功后自动追加到 `## Captured`，去重
- 日期取自源文件 frontmatter 的 `captured_from_date`，没有就写今天

## 去重

判断依据：`03_Processed/<stem>_processed.md` 是否存在。

- 处理成功后会写入该文件，后续不再重复处理。
- 想重跑某篇笔记：删掉对应的 `_processed.md`，下次启动或文件再次触发时会重新生成。

## 防御：避免把流水线产物当源处理

如果 `_processed.md` 文件出现在 `00_Inbox/`（手滑、同步工具误同步、Obsidian 创建链接 stub 等场景），watcher / scan / `--all` 都会跳过它并打 WARNING 日志，绝不再当新笔记调 LLM。判断依据：文件名 stem 以 `_processed` 结尾。

**不要把 `_processed.md` 文件放到 `00_Inbox/`**。它们的归宿是 `03_Processed/`。

## 失败处理

`System/state/failed.json` 按文件路径聚合记录失败：

```json
{
  "C:\\...\\00_Inbox\\foo.md": {
    "attempts": 2,
    "last_error_type": "APITimeoutError",
    "last_error_message": "...",
    "last_time": "2026-05-21 16:00:00",
    "history": [ ... ]
  }
}
```

规则：
- 单个文件累计失败 **3 次**（`MAX_FAILURE_ATTEMPTS`）后自动跳过，需手动从 `failed.json` 删除该项才会再尝试。
- 总条目数封顶 **200** 条（`FAILED_STATE_CAP`），超出按 `last_time` 删最早的。
- 处理成功后自动从 `failed.json` 移除对应条目。
- 冷启动扫描时会清理已经在 `03_Processed` 里有结果的孤儿失败记录。
- 旧的列表格式（`[{...}, {...}]`）会在首次加载时自动迁移到字典格式。

## 修改提示词

1. 编辑 `System/prompts/wiki_prompt.txt`
2. 同步修改 `System/scripts/config.py` 中的 `PROMPT_VERSION`，方便在 `03_Processed` 里追踪是哪版 prompt 产出的结果

## Obsidian 建议

- 核心插件 Templates：模板目录设为 `System/templates`
- 核心插件 Daily notes：模板选 `System/templates/daily.md`，日记目录 `01_Daily`
- 社区插件 Dataview：基于 `03_Processed` frontmatter 统计模型使用与耗时

## Android 部署（Termux）

pipeline 已在 Android + Termux 上验证可用。

### 前提

- 从 **F-Droid** 安装 Termux（不要用 Play Store 版，版本过旧）
- Obsidian vault 放在公共目录，如 `/storage/emulated/0/Documents/SecondBrain/`

### 安装步骤

```bash
# 1. 申请存储权限
termux-setup-storage

# 2. 安装 Python 和 git
pkg install python git

# 3. clone 仓库（私有仓库用 Token 认证）
cd /storage/emulated/0/Documents
git clone https://用户名:TOKEN@github.com/用户名/SecondBrain.git

# 4. 安装 Python 依赖（不需要 watchdog / trafilatura）
pip install python-frontmatter openai python-dotenv requests

# 5. 配置 .env
cd SecondBrain
cp .env.example .env
# 编辑 VAULT_PATH 为 /storage/emulated/0/Documents/SecondBrain
nano .env
```

### 运行

```bash
cd /storage/emulated/0/Documents/SecondBrain
python System/scripts/ai_pipeline.py --scan
# 或一次跑完全套
python System/scripts/ai_pipeline.py --all
```

不使用 watcher 模式（Android 后台进程不稳定）。推荐用 **Termux:Widget** 在主屏创建一键触发快捷方式：

```bash
mkdir -p ~/.shortcuts
cat > ~/.shortcuts/scan.sh << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
cd /storage/emulated/0/Documents/SecondBrain
python System/scripts/ai_pipeline.py --scan
EOF
chmod +x ~/.shortcuts/scan.sh
```

### vault 多端同步

PC ↔ 手机 vault 数据同步推荐 **Syncthing**（免费、P2P），代码仓库更新用 `git pull`。

## 日志

`System/logs/pipeline.log`，使用 `RotatingFileHandler`，单文件 5MB，保留 3 个历史，达到上限自动滚动为 `pipeline.log.1` 等。
