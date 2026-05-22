# SecondBrain

一个基于 [Obsidian](https://obsidian.md/) + LLM 的自动化知识库流水线。

监听 `00_Inbox/` 里新增的 Markdown 笔记，自动调用本地或远程 LLM 完成：摘要、概念抽取、概念定义、Wiki 页生成、归档。配套日记三件套：Quick Capture 分拣、AI 日总结、自动反向索引。

## 快速开始

### 1. 准备 vault

任意一个 Obsidian vault（或手动建一个空目录）。约定的目录结构在第一次跑 pipeline 时会被自动创建。

### 2. 安装依赖

```
pip install -r requirements.txt
```

需要 Python 3.10+。

### 3. 配置 `.env`

```
cp .env.example .env
```

编辑 `.env`，至少填上：

- `VAULT_PATH`：你的 vault 绝对路径
- `REMOTE_API_KEY` 与 `REMOTE_BASE_URL` / `REMOTE_MODEL_NAME`：任意 OpenAI 兼容接口（OpenAI / 智谱 / DeepSeek / NVIDIA NIM / Kimi 等均可）

如果想跑本地模型，把 `LLM_PROVIDER` 改成 `ollama` 并先 `ollama serve`。

### 4. 启动

```
# 默认：watcher 模式，持续监听
python System/scripts/ai_pipeline.py

# 一次性跑完今天全套（分拣 + 补昨日总结 + 扫描 + 今日总结），跑完退出
python System/scripts/ai_pipeline.py --all
```

详细 CLI 与工作流见 [`System/README.md`](System/README.md)。

## 仓库内容

```
.
├── README.md            # 本文件
├── LICENSE              # MIT
├── requirements.txt
├── .env.example         # 配置模板（复制为 .env 后填实际值）
├── .gitignore
└── System/
    ├── README.md        # 详细使用与配置文档
    ├── CONTEXT.md       # Agent 上下文存档（项目内部决策与教训）
    ├── scripts/         # Python 流水线代码
    ├── prompts/         # LLM 系统提示词
    └── templates/       # Obsidian 笔记模板（daily / markor）
```

仓库不携带任何用户笔记内容（`00_Inbox/` ~ `06_Archive/` 目录都已被 `.gitignore`），第一次启动 pipeline 时会按需创建。

## 许可

[MIT](LICENSE)
