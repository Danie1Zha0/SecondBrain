"""LLM 调用：支持 ollama / OpenAI 兼容远程，带超时与重试。"""

import os
import time

import config
from utils import logger


def _load_prompt() -> str:
    prompt_file = os.path.join(config.PROMPT_PATH, "wiki_prompt.txt")
    with open(prompt_file, "r", encoding="utf-8") as f:
        return f.read()


def _is_retryable(e: Exception) -> bool:
    try:
        import openai as _openai
        if isinstance(e, (_openai.APITimeoutError, _openai.APIConnectionError, _openai.RateLimitError)):
            return True
        if isinstance(e, _openai.APIStatusError):
            sc = getattr(e, "status_code", None)
            if sc is not None and (sc >= 500 or sc in (408, 429)):
                return True
    except ImportError:
        pass

    msg = str(e).lower()
    return any(k in msg for k in ("timed out", "timeout", "504", "503", "502", "connection", "temporarily"))


def _call_ollama(messages, start_ts):
    from ollama import chat as ollama_chat

    response = ollama_chat(
        model=config.MODEL_NAME,
        messages=messages,
        options={
            "temperature": config.LLM_TEMPERATURE,
            "top_p": config.LLM_TOP_P,
            "num_predict": config.LLM_MAX_TOKENS,
        },
    )
    text = response["message"]["content"]
    meta = {
        "provider": "ollama",
        "model": config.MODEL_NAME,
        "tokens_in": None,
        "tokens_out": None,
        "duration_ms": int((time.time() - start_ts) * 1000),
    }
    return text, meta


def _call_remote(messages, start_ts):
    import openai

    client = openai.OpenAI(
        api_key=config.REMOTE_API_KEY,
        base_url=config.REMOTE_BASE_URL,
        timeout=config.REMOTE_TIMEOUT,
        max_retries=0,
    )

    last_error: Exception | None = None
    for attempt in range(1, config.REMOTE_MAX_RETRIES + 1):
        attempt_start = time.time()
        try:
            response = client.chat.completions.create(
                model=config.MODEL_NAME,
                messages=messages,
                temperature=config.LLM_TEMPERATURE,
                top_p=config.LLM_TOP_P,
                max_tokens=config.LLM_MAX_TOKENS,
            )
            text = response.choices[0].message.content
            usage = getattr(response, "usage", None)
            meta = {
                "provider": "remote",
                "model": config.MODEL_NAME,
                "tokens_in": getattr(usage, "prompt_tokens", None) if usage else None,
                "tokens_out": getattr(usage, "completion_tokens", None) if usage else None,
                "duration_ms": int((time.time() - start_ts) * 1000),
            }
            return text, meta
        except Exception as e:
            last_error = e
            status = getattr(e, "status_code", None) or getattr(e, "status", None)
            duration_ms = int((time.time() - attempt_start) * 1000)
            logger.warning(
                "远程调用失败 attempt=%s/%s status=%s duration_ms=%s err=%s",
                attempt,
                config.REMOTE_MAX_RETRIES,
                status,
                duration_ms,
                e,
            )
            if not _is_retryable(e):
                break
            if attempt < config.REMOTE_MAX_RETRIES:
                backoff = min(2 ** (attempt - 1), 8)
                time.sleep(backoff)

    assert last_error is not None
    raise last_error


def ask_llm(content: str):
    """返回 (text, meta)。meta 包含 provider/model/tokens_in/tokens_out/duration_ms。"""
    base_prompt = _load_prompt()
    messages = [
        {"role": "system", "content": base_prompt},
        {"role": "user", "content": f"下面是内容：\n\n{content}"},
    ]

    logger.info(
        "调用 LLM provider=%s model=%s temperature=%s top_p=%s max_tokens=%s",
        config.LLM_PROVIDER,
        config.MODEL_NAME,
        config.LLM_TEMPERATURE,
        config.LLM_TOP_P,
        config.LLM_MAX_TOKENS,
    )
    start = time.time()

    if config.LLM_PROVIDER == "ollama":
        return _call_ollama(messages, start)
    if config.LLM_PROVIDER == "remote":
        return _call_remote(messages, start)
    raise ValueError(f"不支持的 LLM_PROVIDER: {config.LLM_PROVIDER}")
