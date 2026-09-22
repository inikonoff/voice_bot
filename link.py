# link.py
"""
Relay-эндпоинт для LINK (читалка на двух языках, ilsoft-dev.github.io/LINK).

Зачем: и Groq, и OpenRouter недоступны из Беларуси напрямую из браузера
("failed to fetch" без VPN). Этот сервер уже развёрнут на Render (США/ЕС),
поэтому запрос ОТ НЕГО проходит нормально.

Важно: API-ключ — ЛИЧНЫЙ ключ пользователя LINK (OpenRouter или Groq),
вводится в настройках самого LINK и приходит с каждым запросом в теле.
Он НЕ берётся из переменных окружения бота — тех ключей это не касается
вообще. На бэкенде ключ нигде не сохраняется: используется один раз для
одного вызова и забывается. Поэтому отдельный токен-заголовок (как у
/api/dictate, /api/correct) тут не нужен для защиты ЧУЖИХ квот: если ключ
невалиден, провайдер сам ответит 401, ничья чужая квота не тратится.

Но открытый POST-эндпоинт остаётся штукой, которую можно дёргать напрямую
(curl/скрипт), а не только из приложения — это не про чужие квоты
(ключ-то юзерский), а про то, что relay может превратиться в:
  1) бесплатный прокси-хост для абьюза Render (трафик/биллинг ваш,
     даже если провайдерский ключ чужой);
  2) generic-анонимайзер для обхода гео-блокировки/рейт-лимитов
     провайдера от третьего лица (нарушение ToS Groq/OpenRouter чужим
     ключом через вашу инфраструктуру).
Поэтому: белый список provider (не произвольный base_url), белый список
моделей, валидация формата и длины api_key, Origin/Referer-фильтр и
rate limiting (см. main.py) — всё вместе, ни один пункт по отдельности
не панацея.
"""
import logging
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

# SEC: slowapi — rate limiting по IP. Если пакет не установлен — эндпоинт
# всё равно будет работать, но без защиты от абуза (см. main.py: без
# `pip install slowapi` лимитер не подключится).
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    _limiter = Limiter(key_func=get_remote_address)
    _HAS_LIMITER = True
except Exception:
    _limiter = None
    _HAS_LIMITER = False

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/link", tags=["link"])

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "openai/gpt-oss-120b",
        # SEC: официальный префикс ключей Groq.
        "key_prefix": "gsk_",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        # Список бесплатных моделей у OpenRouter ротируется — если начнёт
        # 404/400, проверить актуальный ID на openrouter.ai/models?max_price=0
        "default_model": "openai/gpt-oss-20b:free",
        # SEC: официальный префикс ключей OpenRouter.
        "key_prefix": "sk-or-v1-",
    },
}

# SEC: модели, которые реально разрешено запрашивать через relay — не
# даём подставить в chat.completions произвольную дорогую модель.
ALLOWED_MODELS = {
    "groq": {"openai/gpt-oss-120b", "openai/gpt-oss-20b"},
    "openrouter": {"openai/gpt-oss-20b:free", "openai/gpt-oss-120b:free"},
}

MAX_MESSAGES_CHARS = 60_000  # грубый защитный лимит суммарного размера messages
MIN_KEY_LEN = 20
MAX_KEY_LEN = 256
REQUEST_TIMEOUT = 60.0

# SEC: разрешённые Origin для CORS/анти-абьюз проверки в релее. Для TWA
# (Trusted Web Activity) Chrome добавляет к запросам Origin вида
# capacitor://localhost/https://localhost в некоторых обёртках — держим
# оба на всякий случай; если у вас чистый TWA без Capacitor, эти два
# скорее всего никогда не появятся, но и не мешают.
ALLOWED_ORIGINS = {
    "https://ilsoft-dev.github.io",
    "capacitor://localhost",
    "https://localhost",
    "http://localhost",
}

# SEC: регулярка для вычистки ключей из ЛЮБЫХ текстов перед логом/ответом.
# Ловит gsk_... (Groq), sk-or-v1-... (OpenRouter) и общий sk-... на всякий
# случай. В отличие от str.replace(api_key, "***"), эта проверка не
# завязана на точное совпадение конкретной строки — поймает и ключ,
# попавший в текст ошибки в изменённом виде (URL-encoded, обрамлённый
# кавычками и т.п.), пока префикс и алфавит совпадают.
_KEY_RE = re.compile(
    r"(gsk_[A-Za-z0-9_\-]{8,}|sk-or-v1-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{16,})"
)


def _scrub(text: str) -> str:
    """SEC: убирает ключи из любого текста перед логированием/отдачей юзеру."""
    if not text:
        return text
    return _KEY_RE.sub("***", text)


def _origin_allowed(request: Request) -> bool:
    """SEC: проверяем Origin/Referer.

    Важно понимать, ЧТО это даёт, а что нет: Origin в браузере
    выставляется самим браузером и не подделывается из JS — значит,
    чужой сайт не сможет незаметно дёргать relay через браузер жертвы
    (CSRF-подобный сценарий). Но curl/Python/любой скрипт ставит
    Origin руками за секунду — от целенаправленного скриптового абьюза
    это НЕ защищает. Реальная защита от такого — rate limiting
    (см. main.py) и, если понадобится, лёгкая аутентификация
    (токен/JWT) поверх этого.
    """
    origin = request.headers.get("origin")
    if origin and origin in ALLOWED_ORIGINS:
        return True
    referer = request.headers.get("referer")
    if referer:
        for allowed in ALLOWED_ORIGINS:
            if referer.startswith(allowed + "/") or referer == allowed:
                return True
    return False


# SEC: если slowapi есть — навешиваем лимит. 30/мин и 500/день на IP.
# Ослабляет, но не убирает риск абьюза: см. main.py про подключение
# лимитера к приложению и про CORSMiddleware.
if _HAS_LIMITER:
    @router.post("/chat")
    @_limiter.limit("30/minute")
    @_limiter.limit("500/day")
    async def link_chat(request: Request):
        return await _link_chat_impl(request)
else:
    @router.post("/chat")
    async def link_chat(request: Request):
        return await _link_chat_impl(request)


async def _link_chat_impl(request: Request):
    # SEC: 1) проверка Origin/Referer (см. предупреждение в _origin_allowed).
    if not _origin_allowed(request):
        # Не палим, что именно не так — просто 403.
        return JSONResponse(status_code=403, content={"status": "error", "error": "Forbidden"})

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

    # SEC: формат и длина ключа. Это НЕ проверка валидности (её всё равно
    # сделает сам провайдер, ответив 401 на плохой ключ) — это защита от
    # того, чтобы relay превращался в generic-прокси на произвольный чужой
    # сервис/токен под видом "ключа". Groq-ключи всегда начинаются с
    # gsk_, OpenRouter — с sk-or-v1-.
    if not api_key.startswith(provider["key_prefix"]):
        return {"status": "error", "error": f"Неверный формат ключа для {provider_name}"}
    if not (MIN_KEY_LEN <= len(api_key) <= MAX_KEY_LEN):
        return {"status": "error", "error": "Неверная длина ключа"}

    if not isinstance(messages, list) or not messages:
        return {"status": "error", "error": "Не передан messages (непустой список)"}

    # SEC: max_tokens — ограничиваем сверху, чтобы через релей не жгли
    # чужие квоты гигантскими ответами.
    try:
        max_tokens = int(max_tokens)
    except Exception:
        max_tokens = 1500
    max_tokens = max(1, min(max_tokens, 4000))

    # SEC: model — не даём передать произвольную строку, только из
    # разрешённого набора для провайдера. Если прислали не из списка —
    # тихо откатываемся на дефолтную модель провайдера, а не 400'им:
    # это не пользовательская ошибка (юзер модель не выбирает в UI),
    # а лишь defense-in-depth от подмены значения в теле запроса.
    if model not in ALLOWED_MODELS.get(provider_name, set()):
        model = provider["default_model"]

    # Грубая защита от случайно огромных запросов — не логируем
    # содержимое, только длины (ключ и подавно не логируем никогда).
    total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
    if total_chars > MAX_MESSAGES_CHARS:
        return {"status": "error", "error": f"Слишком большой запрос ({total_chars} символов, максимум {MAX_MESSAGES_CHARS})"}

    logger.info(
        f"LINK relay: provider={provider_name}, model={model}, "
        f"messages_chars={total_chars}, max_tokens={max_tokens}"
    )

    client = AsyncOpenAI(api_key=api_key, base_url=provider["base_url"], timeout=REQUEST_TIMEOUT)

    async def _ask(busted: bool = False):
        msgs = messages
        if busted:
            # Трюк против кэша на повторный идентичный промпт (тот же,
            # что и в voicebot, см. processors.summarize_text).
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
        # SEC: 1) чистим ключ регуляркой (ловит URL-encoded, base64, кавычки
        # и прочие варианты, которые простой .replace пропускал);
        # 2) полный текст пишем в лог (тоже очищенный), а юзеру отдаём
        # обобщённое сообщение без путей, версий библиотек и т.п. —
        # сырой текст исключения может раскрыть внутреннюю структуру
        # бэкенда.
        err_full = _scrub(str(e))
        logger.warning(f"LINK relay ({provider_name}) error: {err_full[:500]}")
        return {"status": "error", "error": "Ошибка провайдера. Проверьте ключ и попробуйте снова."}
