"""配置区：路径、模型、超时、阈值。

VAULT_PATH 必须在 `.env` 或环境变量里设置；本仓库不携带任何用户特定路径。
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

# =========================
# 路径
# =========================

VAULT_PATH = os.environ.get("VAULT_PATH", "").strip()
if not VAULT_PATH:
    sys.stderr.write(
        "[config] 未设置 VAULT_PATH。请复制 .env.example 为 .env 并填入你的 vault 绝对路径。\n"
    )
    raise SystemExit(2)
if not os.path.isabs(VAULT_PATH):
    sys.stderr.write(
        f"[config] VAULT_PATH 必须是绝对路径，当前 = {VAULT_PATH!r}\n"
    )
    raise SystemExit(2)

INBOX_PATH = os.path.join(VAULT_PATH, "00_Inbox")
DAILY_PATH = os.path.join(VAULT_PATH, "01_Daily")
PROCESSED_PATH = os.path.join(VAULT_PATH, "03_Processed")
WIKI_PATH = os.path.join(VAULT_PATH, "04_Wiki")
ARCHIVE_PATH = os.path.join(VAULT_PATH, "06_Archive")
PROMPT_PATH = os.path.join(VAULT_PATH, "System", "prompts")
TEMPLATE_PATH = os.path.join(VAULT_PATH, "System", "templates")
LOG_PATH = os.path.join(VAULT_PATH, "System", "logs")
STATE_PATH = os.path.join(VAULT_PATH, "System", "state")

# =========================
# 模型
# =========================

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "remote").strip().lower()

OLLAMA_MODEL_NAME = os.environ.get("OLLAMA_MODEL_NAME", "qwen3:4b")

REMOTE_API_KEY = os.environ.get("REMOTE_API_KEY", "")
REMOTE_BASE_URL = os.environ.get(
    "REMOTE_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"
)
REMOTE_MODEL_NAME = os.environ.get("REMOTE_MODEL_NAME", "glm-4.5-air")

REMOTE_TIMEOUT = int(os.environ.get("REMOTE_TIMEOUT", "60"))
REMOTE_MAX_RETRIES = int(os.environ.get("REMOTE_MAX_RETRIES", "3"))

# =========================
# 采样参数（知识整理 / 日总结共用）
# =========================

# 低温度 -> 概念命名稳定、抽取式任务更可控。0.2 是经验平衡值。
# 注意：智谱 GLM 不支持 0.0（要求 0 < t <= 1），所以下限保持 0.1 以上。
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))

# nucleus sampling，与 temperature 二选一即可。同时设的话由 provider 取它认可的那个。
LLM_TOP_P = float(os.environ.get("LLM_TOP_P", "0.9"))

# 输出 token 上限，防止跑飞。中文 Summary+Concepts+Definitions+Key Ideas+Related 一般 800~1200 tokens。
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1500"))

MODEL_NAME = OLLAMA_MODEL_NAME if LLM_PROVIDER == "ollama" else REMOTE_MODEL_NAME

# 与 wiki_prompt.txt 同步变更
PROMPT_VERSION = "2026-05-21"

# =========================
# 阈值
# =========================

# 正文最少多少字符才视为"有内容"、值得调用 LLM；低于此值跳过。
MIN_CONTENT_LENGTH = 50

# 当原始正文低于该字符数且包含 URL 时，自动用 trafilatura 抓取网页正文并合并到输入里。
# 大于等于该值时认为正文已经足够，不再触发抓取。
URL_FETCH_THRESHOLD = 200

# 抓取 URL 的 HTTP 超时（秒）。requests.get(timeout=...) 直接传入。
URL_FETCH_TIMEOUT = 15

# watcher 触发后等待文件落地的策略：连续测到文件大小不变这么多次，才视为"写完"。
# 适用于同步工具（Syncthing/Obsidian 同步等）分块写入的场景。
FILE_STABILIZE_CHECKS = 2

# 每次大小检查之间的间隔（秒）。总等待时间约 = checks * interval。
FILE_STABILIZE_INTERVAL = 0.5

# 文件稳定检测的总超时（秒）。文件被持续写入超过该时长直接放弃，避免 watcher 永远卡住。
FILE_STABILIZE_MAX_WAIT = 30

# 生成 04_Wiki/<concept>.md 时概念名的最大长度（截断）。规避 Windows 路径 260 限制。
MAX_FILENAME_LENGTH = 120

# 日志单文件大小上限，超过会自动滚动为 pipeline.log.1, .2 ...
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024

# 滚动日志保留的历史份数（不含当前）。总磁盘占用约 = MAX_BYTES * (BACKUP_COUNT + 1)。
LOG_FILE_BACKUP_COUNT = 3

# 同一文件累计失败这么多次后，process_markdown 直接跳过，不再调用 LLM。
# 想强制再试一次：手动从 System/state/failed.json 删掉对应条目。
MAX_FAILURE_ATTEMPTS = 3

# failed.json 总条目数上限。超过时按 last_time 删除最早的条目。
FAILED_STATE_CAP = 200

# =========================
# 日记 / Daily
# =========================

# 日记文件名格式（strftime）。改成 "%Y/%m/%Y-%m-%d" 可以按月分子目录，但要同时调 ensure_daily 的子目录创建。
DAILY_FILENAME_FORMAT = "%Y-%m-%d"

# 日记模板路径（相对于 TEMPLATE_PATH）。
DAILY_TEMPLATE_NAME = "daily.md"

# 日记里"待分拣项"所在段落标题（不带 # 前缀）。
DAILY_INBOX_HEADER = "Inbox"

# 反向索引写入的段落标题（不带 # 前缀）。处理结果会以 - [[stem]] 形式追加到这里。
DAILY_CAPTURED_HEADER = "Captured"

# AI 日总结写入的段落标题（不带 # 前缀）。AI 内容只覆盖标记块之间，标记外的不动。
DAILY_SUMMARY_HEADER = "AI Day Summary"

# 日总结 AI 输出的"开始/结束"标记。改动这两个值会让旧日记里的旧标记失效（变成游离文本）。
DAILY_SUMMARY_MARK_START = "<!-- ai-summary:start -->"
DAILY_SUMMARY_MARK_END = "<!-- ai-summary:end -->"

# Inbox 分拣时生成 slug 的最大长度（再加上时间戳前缀），低于此值的文本会被全用，超过会截断。
INBOX_SLUG_MAX_LENGTH = 30

# 与 day_summary_prompt.txt 同步变更。
DAILY_SUMMARY_PROMPT_VERSION = "2026-05-22"
