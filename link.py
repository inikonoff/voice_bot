# link.py
"""
Relay-эндпоинт для LINK (читалка на двух языках, ilsoft-dev.github.io/LINK).

Зачем: и Groq, и (возможно) OpenRouter могут быть недоступны из Беларуси
напрямую из браузера ("failed to fetch" без VPN — тот же принцип, что и
с другими американскими AI-сервисами). Этот сервер уже развёрнут на
Render (США/ЕС), поэтому запрос ОТ НЕГО проходит нормально.

Важно: API-ключ — ЛИЧНЫЙ ключ пользователя LINK (OpenRouter или Groq),
вводится в настройках самого LINK и приходит с каждым запросом в теле.
Он НЕ берётся из переменных окружения бота — те ключи это не касается
вообще. Поэтому отдельный токен-заголовок (как у /api/dictate,
/api/correct) тут не нужен: если ключ невалиден, провайдер сам ответит
401, ничья чужая квота не тратится.

provider — имя из белого списка, а не произвольный base_url: иначе этот
эндпоинт превращается в открытый прокси на любой хост с любым ключом —
лишний риск без всякой пользы.
"""

import logging
import time

from fastapi import APIRouter, Request
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/link", tags=["link"])

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "openai/gpt-oss-120b",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        # Список бесплатных моделей у OpenRouter ротируется — если начнёт
        # 404/400, проверить актуальный ID на openrouter.ai/models?max_price=0
        "default_model": "openai/gpt-oss-20b:free",
    },
}
MAX_MESSAGES_CHARS = 60_000   # грубый защитный лимит суммарного размера messages
REQUEST_TIMEOUT = 60.0


@router.post("/chat")
async def link_chat(request: Request):
    """
    Проксирует chat-completion запрос к Groq ИЛИ OpenRouter (по выбору
    клиента), используя ключ, присланный LINK в теле запроса.

    Тело запроса:
        {
            "provider": "groq" | "openrouter",   # по умолчанию "groq"
            "api_key": "gsk_..." / "sk-or-v1-...",
            "messages": [{"role": "...", "content": "..."}, ...],
            "max_tokens": 1500,          # опционально
            "model": "..."               # опционально, иначе дефолт провайдера
        }

    Ответ (тот же формат, что у /api/dictate и /api/correct — единый
    паттерн status/error по всему проекту):
        {"status": "success", "content": "..."}
        {"status": "error", "error": "..."}
    """
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "error": "Некорректный JSON в теле запроса"}

    provider_name = (body.get("provider") or "groq").strip().lower()
    provider = PROVIDERS.get(provider_name)
    if not provider:
        return {"status": "error", "error": f"Неизвестный provider: {provider_name!r} (допустимо: {list(PROVIDERS)})"}

    api_key = (body.get("api_key") or "").strip()
    messages = body.get("messages")
    max_tokens = body.get("max_tokens", 1500)
    model = body.get("model") or provider["default_model"]

    if not api_key:
        return {"status": "error", "error": "Не передан api_key"}
    if not isinstance(messages, list) or not messages:
        return {"status": "error", "error": "Не передан messages (непустой список)"}

    # Грубая защита от случайно огромных запросов — не логируем содержимое,
    # только длины (ключ и подавно не логируем никогда).
    total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
    if total_chars > MAX_MESSAGES_CHARS:
        return {"status": "error", "error": f"Слишком большой запрос ({total_chars} символов, максимум {MAX_MESSAGES_CHARS})"}

    logger.info(f"LINK relay: provider={provider_name}, model={model}, messages_chars={total_chars}, max_tokens={max_tokens}")

    client = AsyncOpenAI(api_key=api_key, base_url=provider["base_url"], timeout=REQUEST_TIMEOUT)

    async def _ask(busted: bool = False):
        msgs = messages
        if busted:
            # Тот же трюк против кэша на повторный идентичный промпт, что и
            # в самом LINK, и в voicebot (см. processors.summarize_text).
            msgs = list(messages)
            msgs[0] = {**msgs[0], "content": msgs[0].get("content", "") + f"\n\n(intent-id: {int(time.time() * 1000)})"}
        # reasoning_effort понимает Groq (gpt-oss); OpenRouter для моделей,
        # где параметр неприменим, просто игнорирует лишнее поле — не критично.
        response = await client.chat.completions.create(
            model=model,
            messages=msgs,
            temperature=0.6,
            max_tokens=max_tokens,
            reasoning_effort="low",
        )
        return (response.choices[0].message.content or "").strip()

    try:
        content = await _ask()
        if not content:
            logger.warning(f"LINK relay ({provider_name}): пустой ответ, повторяю попытку")
            content = await _ask(busted=True)
        if not content:
            return {"status": "error", "error": "Модель дважды вернула пустой ответ. Попробуйте ещё раз."}
        return {"status": "success", "content": content}

    except Exception as e:
        # Ключ пользователя никогда не попадает в текст ошибки — str(e) от
        # openai-клиента может содержать сам ключ в некоторых вариантах
        # исключений (например, при неверном base_url), поэтому подчищаем.
        err_text = str(e).replace(api_key, "***")
        logger.warning(f"LINK relay ({provider_name}) error: {err_text[:200]}")
        return {"status": "error", "error": err_text[:200]}
