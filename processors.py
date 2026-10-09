# processors.py
"""
Обработчики текста: OCR, транскрибация, кружочки, коррекция, саммари, диалог, экспорт
Версия 5.1 — убрана обработка видеофайлов (только кружочки), добавлен DOCX
"""

import io
import os
import json
import logging
import base64
import asyncio
import subprocess
import mimetypes
import re
import time
import uuid
import random
from typing import Optional, Tuple, List, Dict, Any, AsyncGenerator
from datetime import timedelta
from openai import AsyncOpenAI

from collections import OrderedDict

import config
import access
import textkit

# Попытка импорта дополнительных библиотек
try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    import docx as python_docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

logger = logging.getLogger(__name__)

# Хранилище для диалогов о документах
document_dialogues: Dict[int, Dict[int, Dict[str, Any]]] = {}


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ GROQ
# ============================================================================

async def _make_groq_request(groq_clients: list, func, *args, **kwargs):
    """
    Запрос с честной ротацией ключей.

    Алгоритм:
    1. Создаём перемешанный пул индексов всех клиентов.
    2. Каждый клиент получает не более GROQ_RETRY_COUNT попыток.
    3. Между раундами (когда пройдены все клиенты) — повторный shuffle,
       чтобы упавший на rate-limit ключ не получил тот же слот в очереди.
    """
    if not groq_clients:
        raise Exception("Нет доступных Groq клиентов")

    errors = []
    client_count = len(groq_clients)
    total_attempts = client_count * config.GROQ_RETRY_COUNT

    # Стартовый перемешанный порядок индексов
    order = list(range(client_count))
    random.shuffle(order)

    for attempt in range(total_attempts):
        # На границе раунда — пересобираем порядок
        if attempt > 0 and attempt % client_count == 0:
            random.shuffle(order)

        client_index = order[attempt % client_count]
        client = groq_clients[client_index]
        try:
            logger.debug(f"Попытка {attempt + 1}/{total_attempts} с клиентом #{client_index}")
            return await func(client, *args, **kwargs)
        except Exception as e:
            error_msg = str(e)
            errors.append(f"Клиент {client_index}: {error_msg[:100]}")
            logger.warning(f"Ошибка запроса (попытка {attempt + 1}): {error_msg[:100]}")
            if "429" in error_msg or "rate_limit" in error_msg.lower():
                wait_time = 5 + (attempt * 2)
                logger.info(f"Rate limit, ждем {wait_time}с...")
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(1 + (attempt % 3))

    raise Exception(f"Все клиенты недоступны: {'; '.join(errors[:3])}")


def _err_text(prefix: str, e: Exception, n: int = 100) -> str:
    """Текст ошибки для пользователя; лимит показываем как лимит, а не как «ошибку»."""
    if isinstance(e, access.QuotaExceeded):
        return e.user_message
    return f"❌ {prefix}: {str(e)[:n]}"


def _truncate_text_for_model(text: str, model_type: str) -> str:
    model_limits = {
        "basic": 5000,
        "premium": 10000,
        "reasoning": 25000,
    }
    limit = model_limits.get(model_type, 5000)
    if len(text) > limit:
        logger.warning(f"Текст обрезан с {len(text)} до {limit} символов для {model_type}")
        return text[:limit] + "... [текст обрезан из-за лимитов API]"
    return text


# ============================================================================
# ТЕКСТОВАЯ LLM: OpenRouter (цепочка моделей) → откат на Groq
# ============================================================================

_text_clients: list = []   # клиенты OpenRouter
_GROQ_KIND = {"subtitles": "premium", "img2prompt": "vision"}   # какую Groq-модель брать при откате
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def init_text_clients() -> int:
    """Создаёт клиентов OpenRouter из OPENROUTER_API_KEYS (или OPENROUTER_API_KEY)."""
    raw = os.environ.get("OPENROUTER_API_KEYS") or os.environ.get("OPENROUTER_API_KEY", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    headers = {"X-Title": config.OPENROUTER_TITLE}
    if config.OPENROUTER_REFERER:
        headers["HTTP-Referer"] = config.OPENROUTER_REFERER
    _text_clients.clear()
    for key in keys:
        _text_clients.append(AsyncOpenAI(
            api_key=key,
            base_url=config.OPENROUTER_BASE_URL,
            timeout=config.GROQ_TIMEOUT,
            default_headers=headers,
        ))
    if _text_clients:
        logger.info(f"✅ OpenRouter клиентов: {len(_text_clients)}; модели: {config.LLM_MODELS}")
    else:
        logger.warning("OPENROUTER_API_KEYS не задан — текстовые задачи идут через Groq")
    return len(_text_clients)


# ----------------------------------------------------------------------------
# YandexGPT (OpenAI-совместимый API Yandex AI Studio)
# ----------------------------------------------------------------------------

_yandex_client: Optional[AsyncOpenAI] = None
_yandex_folder: str = ""
_yandex_sem: Optional[asyncio.Semaphore] = None
_yandex_fails = 0               # сбоев подряд
_yandex_down_until = 0.0        # предохранитель: до этого момента Yandex пропускаем

# Вежливые отказы YandexGPT на «чувствительные» темы — это не правка, а заглушка
_YANDEX_REFUSAL_RE = re.compile(
    r"(не могу (ничего )?(сказать|обсуждать|ответить)|давайте (сменим|поговорим о чём)|"
    r"не могу помочь с (этим|данным)|не имею права обсуждать)", re.IGNORECASE)


class YandexRefusal(Exception):
    """Модель отказалась обрабатывать текст (фильтр контента)."""


def init_yandex_client() -> bool:
    """Создаёт клиента YandexGPT из YANDEX_API_KEY и YANDEX_FOLDER_ID."""
    global _yandex_client, _yandex_folder, _yandex_sem, _yandex_fails, _yandex_down_until
    key = os.environ.get("YANDEX_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    _yandex_client, _yandex_folder = None, folder
    _yandex_fails, _yandex_down_until = 0, 0.0
    if not (key and folder):
        logger.info("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы — YandexGPT выключен")
        return False
    headers = {}
    if not config.YANDEX_DATA_LOGGING:
        headers["x-data-logging-enabled"] = "false"     # не отдавать запросы Яндексу для обучения
    _yandex_client = AsyncOpenAI(
        api_key=key,
        base_url=config.YANDEX_BASE_URL,
        project=folder,                      # каталог → заголовок OpenAI-Project
        timeout=config.YANDEX_TIMEOUT,
        max_retries=0,                       # повторы и откат — наши
        default_headers=headers,
    )
    _yandex_sem = asyncio.Semaphore(max(1, config.YANDEX_MAX_CONCURRENT))
    logger.info(f"✅ YandexGPT: {config.YANDEX_BASE_URL}; модели: {_yandex_models()}")
    return True


def _yandex_models() -> List[str]:
    """Полные URI моделей: gpt://<folder>/<имя>."""
    return [m if m.startswith(("gpt://", "ds://")) else f"gpt://{_yandex_folder}/{m}"
            for m in config.YANDEX_MODELS]


def yandex_configured() -> bool:
    return _yandex_client is not None


def _yandex_ready() -> bool:
    return _yandex_client is not None and time.time() >= _yandex_down_until


def _yandex_ok() -> None:
    global _yandex_fails
    _yandex_fails = 0


def _yandex_failed(e: Exception) -> None:
    """Считает сбой инфраструктуры и при серии сбоев открывает предохранитель."""
    global _yandex_fails, _yandex_down_until
    _yandex_fails += 1
    if _yandex_fails >= config.YANDEX_BREAKER_FAILS:
        _yandex_down_until = time.time() + config.YANDEX_BREAKER_PAUSE
        _yandex_fails = 0
        logger.warning(f"YandexGPT: {config.YANDEX_BREAKER_FAILS} сбоя подряд — пауза "
                       f"{int(config.YANDEX_BREAKER_PAUSE)}с, идём по запасной цепочке ({str(e)[:80]})")


def _yandex_mode(kind: str) -> Optional[str]:
    """None / "first" / "only" — режим Yandex в профиле текущего пользователя."""
    if kind in config.FIXED_CHAIN_KINDS:      # vision-задачи: у YandexGPT нет картинок
        return None
    return access.profile_for(access.current_user_id.get()).get("yandex")


def _check_yandex_reply(r, src_len: int) -> str:
    choice = r.choices[0] if r.choices else None
    content = _clean_llm_text(choice.message.content if choice else "")
    if choice is not None and getattr(choice, "finish_reason", None) == "content_filter":
        raise YandexRefusal("content_filter")
    if not content:
        raise Exception("empty_content")
    if len(content) < 220 and _YANDEX_REFUSAL_RE.search(content):
        raise YandexRefusal(content[:80])
    return content


async def _yandex_completion(kind: str, messages: list, temperature: float,
                             max_tokens: Optional[int]) -> str:
    """
    Запрос к YandexGPT по цепочке моделей (config.YANDEX_MODELS).
    Исключения: YandexRefusal (отказ модели), прочие — сбой; вызывающий решает,
    идти ли дальше по запасной цепочке.
    """
    last_err: Optional[Exception] = None
    src_len = sum(len(m.get("content") or "") for m in messages if isinstance(m.get("content"), str))
    for model in _yandex_models():
        for attempt in range(2):
            kwargs: Dict[str, Any] = dict(model=model, messages=messages, temperature=temperature)
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            try:
                async with _yandex_sem:
                    r = await _yandex_client.chat.completions.create(**kwargs)
                content = _check_yandex_reply(r, src_len)
                _yandex_ok()
                return content
            except YandexRefusal as e:
                logger.warning(f"[{kind}] Yandex {model}: отказ модели ({e})")
                raise
            except Exception as e:
                last_err = e
                msg = str(e)
                low = msg.lower()
                logger.warning(f"[{kind}] Yandex {model}: {msg[:140]}")
                if "413" in msg or ("context" in low and "length" in low):
                    raise                          # слишком длинный вход — пусть вызывающий обрежет
                if "401" in msg or "403" in msg:   # ключ/роль/каталог — повторять бессмысленно
                    _yandex_failed(e)
                    raise
                if "404" in msg or "not found" in low:
                    break                          # такой модели нет — следующая
                if attempt == 0:
                    await asyncio.sleep(2 if "429" in msg else 0.5)
                    continue
                _yandex_failed(e)
    raise last_err or Exception("YandexGPT недоступен")


def has_text_llm(groq_clients: Optional[list] = None) -> bool:
    return bool(_text_clients or groq_clients or _yandex_client)


def text_llm_label() -> str:
    parts = []
    if _yandex_client:
        parts.append("🟡 YandexGPT" + (" (пауза)" if not _yandex_ready() else ""))
    if _text_clients:
        parts.append(f"✅ OpenRouter ({len(_text_clients)} ключ.)")
    if parts:
        return " → ".join(parts) + ", запас — Groq"
    return "Groq (OpenRouter не настроен)"


def _clean_llm_text(raw: Optional[str]) -> str:
    """Убирает <think>…</think>, если модель вернула рассуждения прямо в тексте."""
    s = _THINK_RE.sub("", raw or "")
    if re.search(r"<think>", s, re.IGNORECASE):   # незакрытый тег — всё после него мысли
        s = re.split(r"<think>", s, flags=re.IGNORECASE)[0]
    return s.strip()


# Модели OpenRouter, ответившие 404 (в т.ч. «unavailable for free»): на время пропускаем,
# чтобы не тратить запрос и секунды на заведомо мёртвую модель в начале цепочки.
_dead_models: Dict[str, float] = {}
_DEAD_TTL = 3600.0


def _mark_dead(model: str) -> None:
    _dead_models[model] = time.time() + _DEAD_TTL


def _live_chain(chain: list) -> list:
    now = time.time()
    live = [m for m in chain if _dead_models.get(m, 0) <= now]
    return live or list(chain)      # если «мертвы» все — пробуем всё равно


def _resolve_chain(kind: str) -> Tuple[list, bool]:
    """
    Цепочка моделей OpenRouter и разрешение отката на Groq для текущего
    пользователя (профиль из /model; без пользователя — общий профиль).
    """
    if kind in config.FIXED_CHAIN_KINDS:
        return list(config.LLM_MODELS[kind]), True
    prof = access.profile_for(access.current_user_id.get())
    models = prof.get("models")
    if models is None:
        models = config.LLM_MODELS[kind]
    return list(models), bool(prof.get("groq_fallback", True))


async def _text_completion(kind: str, groq_clients: list, messages: list,
                           temperature: float, max_tokens: Optional[int] = None) -> str:
    """
    Один текстовый запрос к LLM.
    1. Списывает 1 единицу лимита у текущего пользователя (админу — нет).
    2. OpenRouter: цепочка по профилю пользователя (см. /model и
       config.LLM_PROFILES), по LLM_RETRIES_PER_MODEL попыток на модель;
       404 — сразу к следующей модели.
    3. Если профиль допускает откат — Groq (config.GROQ_MODELS).
    """
    last_err: Optional[Exception] = None

    access.charge()   # 1 единица за обращение к ИИ (админ и фоновые задачи не списываются)
    chain, groq_fallback = _resolve_chain(kind)

    ymode = _yandex_mode(kind)
    if ymode:
        if _yandex_ready():
            try:
                return await _yandex_completion(kind, messages, temperature, max_tokens)
            except Exception as e:
                last_err = e
                if "413" in str(e):
                    raise
                if ymode == "only":
                    raise
                logger.warning(f"[{kind}] Yandex недоступен → запасная цепочка")
        elif ymode == "only":
            raise Exception("YandexGPT недоступен: " + (
                "не заданы YANDEX_API_KEY/YANDEX_FOLDER_ID" if not _yandex_client
                else "временная пауза после сбоев"))

    if chain and not _text_clients:
        last_err = Exception("OpenRouter не настроен (нет OPENROUTER_API_KEYS)")
    if chain and _text_clients:
        extra = config.OPENROUTER_EXTRA_BODY.get(kind)
        strict = len(chain) == 1            # конкретная модель из /model — без пропусков
        for model in (chain if strict else _live_chain(chain)):
            use_extra = bool(extra)
            for attempt in range(config.LLM_RETRIES_PER_MODEL):
                client = random.choice(_text_clients)
                kwargs: Dict[str, Any] = dict(model=model, messages=messages, temperature=temperature)
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens
                if use_extra:
                    kwargs["extra_body"] = extra
                try:
                    r = await client.chat.completions.create(**kwargs)
                    content = _clean_llm_text(r.choices[0].message.content if r.choices else "")
                    if not content:
                        raise Exception("empty_content")
                    return content
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    low = msg.lower()
                    logger.warning(f"[{kind}] {model}: {msg[:140]}")
                    if use_extra and ("400" in msg or "reasoning" in low):
                        use_extra = False          # модель не принимает reasoning-параметр
                        continue
                    if "413" in msg:
                        raise                      # слишком длинный вход — пусть вызывающий обрежет
                    if "404" in msg or "no endpoints" in low or "not a valid model" in low:
                        _mark_dead(model)
                        break                      # модели нет — следующая
                    if attempt < config.LLM_RETRIES_PER_MODEL - 1:   # после последней попытки не ждём
                        await asyncio.sleep(3 if ("429" in msg or "rate" in low) else 1)
        logger.warning(f"[{kind}] все модели OpenRouter недоступны"
                       + (", откат на Groq" if groq_fallback else ""))

    if groq_clients and groq_fallback:
        gk = _GROQ_KIND.get(kind, kind)

        async def _groq_call(client):
            kwargs: Dict[str, Any] = dict(
                model=config.GROQ_MODELS[gk], messages=messages, temperature=temperature)
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            if gk == "reasoning":
                kwargs["reasoning_effort"] = "low"
            r = await client.chat.completions.create(**kwargs)
            content = _clean_llm_text(r.choices[0].message.content if r.choices else "")
            if not content:
                raise Exception("empty_content")
            return content

        return await _make_groq_request(groq_clients, _groq_call)

    raise last_err or Exception("Нет доступных LLM-клиентов")


async def _list_ids(client) -> set:
    ids = set()
    async for m in client.models.list():
        ids.add(m.id)
    return ids


# Кэш живых списков моделей провайдеров (для /model и аудита). None — список не получен.
_live: Dict[str, Any] = {"or": None, "groq": None, "or_err": None, "groq_err": None,
                         "groq_n": 0, "ts": 0.0}
_LIVE_TTL = 600.0


async def refresh_live_models(groq_clients: list, force: bool = False) -> None:
    """Обновляет кэш списков моделей (GET /models у OpenRouter и Groq), не чаще раза в 10 минут."""
    if not force and time.time() - _live["ts"] < _LIVE_TTL:
        return
    _live["groq_n"] = len(groq_clients or [])
    for key, clients in (("or", _text_clients), ("groq", groq_clients)):
        if not clients:
            _live[key], _live[key + "_err"] = None, None
            continue
        try:
            _live[key], _live[key + "_err"] = await _list_ids(clients[0]), None
        except Exception as e:
            _live[key], _live[key + "_err"] = None, str(e)[:80]
    _live["ts"] = time.time()


def profile_status(key: str) -> Optional[str]:
    """
    None — профиль рабочий, иначе причина, почему его не стоит показывать в /model.
    Если список моделей у провайдера не получен, профиль не скрывается (неизвестно ≠ сломано).
    """
    prof = config.LLM_PROFILES[key]
    ymode = prof.get("yandex")
    if ymode and not _yandex_client:
        return "Yandex не настроен"
    if ymode == "only":
        return None
    models = prof.get("models")
    if models is None:                       # штатная цепочка / Yandex с откатом
        return None
    if not models:                           # профиль «только Groq»
        if _live["ts"] and _live["groq_n"] == 0:
            return "Groq не настроен"
        return None
    if not _text_clients:
        return "OpenRouter не настроен"
    live = _live["or"]
    now = time.time()
    for m in models:
        if live is not None and m not in live:
            return "модели нет у провайдера (снята или платная)"
        if _dead_models.get(m, 0) > now:
            return "модель отвечает 404"
    return None


async def audit_models(groq_clients: list) -> List[str]:
    """
    Сверяет модели из config с живыми списками провайдеров (GET /models).
    Возвращает строки о проблемах: модель отсутствует у провайдера (снята, переименована,
    бесплатный вариант отозван). Пустой список — всё на месте.
    """
    problems: List[str] = []
    await refresh_live_models(groq_clients, force=True)

    if _text_clients:
        if _live["or"] is None:
            problems.append(f"OpenRouter: список моделей не получен ({_live['or_err']})")
        else:
            live = _live["or"]
            used: Dict[str, List[str]] = {}
            for kind, models in config.LLM_MODELS.items():
                for m in models:
                    used.setdefault(m, []).append(kind)
            for prof_key, prof in config.LLM_PROFILES.items():
                for m in (prof.get("models") or []):
                    used.setdefault(m, []).append(f"/model:{prof_key}")
            for m, where in sorted(used.items()):
                if m not in live:
                    problems.append(f"OpenRouter: «{m}» нет в списке ({', '.join(where)})")

    if groq_clients:
        if _live["groq"] is None:
            problems.append(f"Groq: список моделей не получен ({_live['groq_err']})")
        else:
            live = _live["groq"]
            need = {config.GROQ_MODELS[k]: k for k in ("transcription", "basic", "premium", "reasoning")}
            for m in config.GROQ_VISION_MODELS:
                need.setdefault(m, "vision")
            for m, k in need.items():
                if m not in live:
                    problems.append(f"Groq: «{m}» нет в списке ({k})")
    return problems


async def log_model_audit(groq_clients: list) -> None:
    """Фоновая проверка при старте: результат в лог."""
    try:
        problems = await audit_models(groq_clients)
        if problems:
            for line in problems:
                logger.warning(f"⚠️ МОДЕЛЬ: {line}")
        else:
            logger.info("✅ Все модели из config найдены у провайдеров")
    except Exception as e:
        logger.warning(f"Проверка моделей не выполнена: {e}")


async def llm_check(groq_clients: list) -> str:
    """Проверка всех звеньев цепочки (для админ-команды /llmcheck). Квоту не списывает."""
    msgs = [{"role": "user", "content": "Ответь одним словом: ок"}]
    lines: List[str] = []

    async def probe(label: str, coro):
        t0 = time.time()
        try:
            r = await coro
            txt = (r.choices[0].message.content or "").strip()[:20] if r.choices else ""
            dt = time.time() - t0
            if txt:
                lines.append(f"✅ {label}: {dt:.1f} с «{txt}»")
            else:   # рассуждающая модель потратила токены на размышления — модель жива
                lines.append(f"✅ {label}: {dt:.1f} с (ответ пустой: токены ушли на размышления)")
        except Exception as e:
            msg = str(e)
            hint = " — временный лимит провайдера, модель на месте" if "429" in msg else ""
            lines.append(f"❌ {label}: {msg[:100]}{hint}")

    if _yandex_client:
        for m in _yandex_models():
            await probe(f"Yandex {m.split('/')[-1]}", _yandex_client.chat.completions.create(
                model=m, messages=msgs, temperature=0, max_tokens=64))
        if not _yandex_ready():
            lines.append("⏸ Yandex на паузе после сбоев (предохранитель)")
    else:
        lines.append("⚪ Yandex: не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID")

    if _text_clients:
        m = config.LLM_MODELS["premium"][0]
        await probe(f"OpenRouter {m}", random.choice(_text_clients).chat.completions.create(
            model=m, messages=msgs, temperature=0, max_tokens=64))
    else:
        lines.append("⚪ OpenRouter: ключ не задан")

    if groq_clients:
        m = config.GROQ_MODELS["premium"]
        await probe(f"Groq {m}", random.choice(groq_clients).chat.completions.create(
            model=m, messages=msgs, temperature=0, max_tokens=64))
    else:
        lines.append("⚪ Groq: клиентов нет")

    problems = await audit_models(groq_clients)
    lines.append("")
    if problems:
        lines.append("⚠️ Модели из настроек, которых нет у провайдера:")
        lines += [f"• {p}" for p in problems]
    else:
        lines.append("✅ Все модели из настроек найдены у провайдеров")
    dead = [m for m, t in _dead_models.items() if t > time.time()]
    if dead:
        lines.append("⏸ Временно пропускаются после 404: " + ", ".join(dead))
    return "\n".join(lines)


async def _open_text_stream(kind: str, groq_clients: list, messages: list,
                            temperature: float, max_tokens: int):
    """Открывает потоковый запрос: OpenRouter по цепочке моделей, затем Groq."""
    last_err: Optional[Exception] = None
    chain, groq_fallback = _resolve_chain(kind)

    ymode = _yandex_mode(kind)
    if ymode:
        if _yandex_ready():
            for model in _yandex_models():
                try:
                    return await _yandex_client.chat.completions.create(
                        model=model, messages=messages, temperature=temperature,
                        max_tokens=max_tokens, stream=True)
                except Exception as e:
                    last_err = e
                    logger.warning(f"[{kind}/stream] Yandex {model}: {str(e)[:140]}")
                    if "401" in str(e) or "403" in str(e):
                        break
            _yandex_failed(last_err or Exception("stream"))
            if ymode == "only":
                raise last_err or Exception("YandexGPT недоступен")
        elif ymode == "only":
            raise Exception("YandexGPT недоступен: " + (
                "не заданы YANDEX_API_KEY/YANDEX_FOLDER_ID" if not _yandex_client
                else "временная пауза после сбоев"))

    if chain and not _text_clients:
        last_err = Exception("OpenRouter не настроен (нет OPENROUTER_API_KEYS)")
    if chain and _text_clients:
        extra = config.OPENROUTER_EXTRA_BODY.get(kind)
        client = random.choice(_text_clients)
        for model in (chain if len(chain) == 1 else _live_chain(chain)):
            use_extra = bool(extra)
            for _ in range(2):
                kwargs: Dict[str, Any] = dict(model=model, messages=messages,
                                              temperature=temperature, max_tokens=max_tokens, stream=True)
                if use_extra:
                    kwargs["extra_body"] = extra
                try:
                    return await client.chat.completions.create(**kwargs)
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    logger.warning(f"[{kind}/stream] {model}: {msg[:140]}")
                    if use_extra and ("400" in msg or "reasoning" in msg.lower()):
                        use_extra = False
                        continue
                    if "404" in msg:
                        _mark_dead(model)
                    break
    if groq_clients and groq_fallback:
        gk = _GROQ_KIND.get(kind, kind)
        kwargs = dict(model=config.GROQ_MODELS[gk], messages=messages,
                      temperature=temperature, max_tokens=max_tokens, stream=True)
        if gk == "reasoning":
            kwargs["reasoning_effort"] = "low"
        return await random.choice(groq_clients).chat.completions.create(**kwargs)
    raise last_err or Exception("Нет доступных LLM-клиентов")


# ============================================================================
# IMG2PROMPT: картинка → промпт для генератора изображений
# ============================================================================

_COMMON_RATIOS = [(1, 1), (5, 4), (4, 3), (3, 2), (16, 9), (21, 9),
                  (4, 5), (3, 4), (2, 3), (9, 16), (9, 21)]


def _closest_ratio(w: int, h: int) -> str:
    import math
    target = math.log(w / h)
    a, b = min(_COMMON_RATIOS, key=lambda r: abs(math.log(r[0] / r[1]) - target))
    return f"{a}:{b}"


def prepare_image_for_vision(image_bytes: bytes) -> Tuple[bytes, str]:
    """
    Приводит картинку к JPEG разумного размера (PNG с прозрачностью, WEBP, HEIC-подобные
    форматы Pillow умеет читать; EXIF-поворот применяется). Возвращает (jpeg, "3:2").
    """
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        w, h = img.size
        if w < 1 or h < 1:
            raise ValueError("empty image")
        ratio = _closest_ratio(w, h)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        side = config.IMG2PROMPT_MAX_SIDE
        if max(img.size) > side:
            img.thumbnail((side, side), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=config.IMG2PROMPT_JPEG_QUALITY, optimize=True)
        return out.getvalue(), ratio
    except Exception as e:
        raise ValueError("Не удалось прочитать изображение. Пришлите JPG, PNG или WEBP.") from e


def _fit_length(text: str, max_chars: int) -> str:
    """
    Мягко укорачивает слишком длинный промпт по границе предложения/запятой.
    Хвост параметров Midjourney (--ar 16:9 --style raw) сохраняется.
    """
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    m = re.search(r"\s(--\w+(?:\s+[^\s-][^\s]*)?(?:\s+--\w+(?:\s+[^\s-][^\s]*)?)*)\s*$", text)
    tail = ""
    if m:
        tail = " " + m.group(1).strip()
        text = text[:m.start()].rstrip()
    budget = max(40, max_chars - len(tail))
    if len(text) > budget:
        cut = text[:budget]
        pos = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("; "), cut.rfind(", "))
        if pos < budget * 0.5:
            pos = cut.rfind(" ")
        cut = cut[:pos] if pos > 0 else cut
        text = cut.rstrip(" ,;")
        # точку добавляем только к прозе; у списков тегов (SD, Midjourney) её быть не должно
        is_prose = bool(re.search(r"[.!?]\s+[A-ZА-ЯЁ]", text))
        if is_prose and not text.endswith((".", "!", "?", "…")):
            text += "."
    return (text + tail).strip()


def fit_prompt_sections(parts: Dict[str, str], detail: int) -> Dict[str, str]:
    """Применяет потолок длины к основному промпту (1.25 × max_chars выбранной подробности)."""
    spec = config.IMG2PROMPT_DETAILS.get(detail) or config.IMG2PROMPT_DETAILS[config.IMG2PROMPT_DEFAULT_DETAIL]
    out = dict(parts)
    out["prompt"] = _fit_length(parts.get("prompt", ""), int(spec[2] * 1.25))
    return out


async def image_to_prompt(jpeg_bytes: bytes, ratio: str, style: str, groq_clients: list,
                          regenerate: bool = False, detail: int = 0) -> str:
    """
    Сырой ответ модели (секции ### PROMPT / SHORT / NEGATIVE / RU) или текст ошибки с «❌».
    Лимит списывается внутри _text_completion (1 единица за вызов).
    """
    if style not in config.IMG2PROMPT_STYLES:
        style = "u"
    if detail not in config.IMG2PROMPT_DETAILS:
        detail = config.IMG2PROMPT_DEFAULT_DETAIL
    _label, lo, hi, dtext = config.IMG2PROMPT_DETAILS[detail]
    instruction = config.IMG2PROMPT_STYLES[style][1].replace("{ratio}", ratio)
    detail_instruction = dtext.replace("{lo}", str(lo)).replace("{hi}", str(hi))
    prompt = (config.IMG2PROMPT_PROMPT.replace("{ratio}", ratio)
              .replace("{style_instruction}", instruction)
              .replace("{detail_instruction}", detail_instruction))
    b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ],
    }]
    temp = config.IMG2PROMPT_REGEN_TEMPERATURE if regenerate else config.IMG2PROMPT_TEMPERATURE
    try:
        return await _text_completion("img2prompt", groq_clients, messages, temp,
                                      max_tokens=config.IMG2PROMPT_MAX_TOKENS)
    except Exception as e:
        logger.error(f"img2prompt error: {e}")
        if "empty_content" in str(e):
            return "❌ Модель вернула пустой ответ. Попробуйте ещё раз."
        return _err_text("Ошибка создания промпта", e, 160)


_SECTION_RE = re.compile(r"^\s*#{2,4}\s*(PROMPT|SHORT|NEGATIVE|RU)\s*:?\s*$", re.IGNORECASE | re.MULTILINE)


def parse_img_prompt(raw: str) -> Dict[str, str]:
    """Разбирает ответ модели на секции. Если формат нарушен — весь текст идёт в prompt."""
    raw = (raw or "").strip()
    parts: Dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(raw))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        parts[m.group(1).lower()] = raw[m.end():end].strip()

    def clean(v: str) -> str:
        v = v.strip().strip("`").strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'«":
            v = v[1:-1].strip()
        return re.sub(r"\s*\n\s*", " ", v)

    out = {k: clean(v) for k, v in parts.items()}
    if not out.get("prompt"):
        out["prompt"] = clean(re.sub(r"^\s*#{2,4}.*$", "", raw, flags=re.MULTILINE)) if raw else ""
    if out.get("negative", "").lower().strip(" .") in ("none", "n/a", "нет", "-", ""):
        out["negative"] = ""
    return out


# ============================================================================
# СТИЛИ ПРАВКИ, ПРОТОКОЛ, ДИАЛОГ ПО РОЛЯМ
# ============================================================================

async def _single_llm_task(kind: str, groq_clients: list, build_messages, text: str, temperature: float,
                           max_tokens: int, err_prefix: str, shorter_to: int) -> str:
    """Один запрос к LLM с единым разбором ошибок (413 → повтор с укороченным текстом)."""
    try:
        return await _text_completion(kind, groq_clients, build_messages(text), temperature, max_tokens=max_tokens)
    except Exception as e:
        logger.error(f"{err_prefix}: {e}")
        try:
            if "413" in str(e) or "rate_limit_exceeded" in str(e):
                return await _text_completion(kind, groq_clients,
                                              build_messages(text[:shorter_to] + "... [обрезано]"),
                                              temperature, max_tokens=max_tokens)
        except Exception as e2:
            e = e2
        if "empty_content" in str(e):
            return "❌ Модель вернула пустой ответ. Попробуйте ещё раз."
        return _err_text(err_prefix, e)


async def rewrite_in_style(text: str, style_key: str, groq_clients: list) -> str:
    """Переписывает текст в выбранном стиле (config.EDIT_STYLES)."""
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    if style_key not in config.EDIT_STYLES:
        return "❌ Неизвестный стиль"
    instruction = config.EDIT_STYLES[style_key][1]
    text = _truncate_text_for_model(text, "premium")

    def build(t: str) -> list:
        prompt = config.STYLE_PROMPT.replace("{style}", instruction).replace("{text}", t)
        return [{"role": "user", "content": prompt}]

    return await _single_llm_task("premium", groq_clients, build, text, config.MODEL_TEMPERATURES["premium"],
                                  4000, "Ошибка смены стиля", 6000)


async def make_protocol(text: str, groq_clients: list) -> str:
    """Протокол: тема, суть, решения, задачи (что, кто, срок), открытые вопросы."""
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    text = _truncate_text_for_model(text, "reasoning")

    def build(t: str) -> list:
        return [{"role": "user", "content": config.PROTOCOL_PROMPT + f"\n\nРасшифровка:\n{t}"}]

    return await _single_llm_task("reasoning", groq_clients, build, text, config.MODEL_TEMPERATURES["reasoning"],
                                  2500, "Ошибка составления протокола", 12000)


async def make_dialogue(text: str, groq_clients: list) -> str:
    """
    Разбивка расшифровки на реплики «Говорящий 1/2/…». Роли определяет LLM по смыслу
    (по голосу Whisper их не различает). Длинные записи обрабатываются частями с
    переносом нумерации; каждая часть стоит 1 единицу лимита.
    """
    if not text.strip():
        return config.ERROR_EMPTY_TEXT

    seg = segments_for(text)
    units = seg.split("\n") if seg else textkit.split_sentences(text)
    chunks = textkit.chunk_lines(units, config.DIALOGUE_CHUNK_CHARS)
    skipped = max(0, len(chunks) - config.DIALOGUE_MAX_CHUNKS)
    chunks = chunks[:config.DIALOGUE_MAX_CHUNKS]

    outputs: List[str] = []
    stop_note = ""
    for i, chunk in enumerate(chunks):
        prompt = config.DIALOGUE_PROMPT
        if outputs:
            prompt += "\n\n" + config.DIALOGUE_CONTINUATION.replace("{tail}", outputs[-1][-700:])
        prompt += f"\n\nРасшифровка:\n{chunk}"
        try:
            out = await _text_completion("reasoning", groq_clients, [{"role": "user", "content": prompt}],
                                         0.2, max_tokens=3800)
        except access.QuotaExceeded as qe:
            if not outputs:
                return qe.user_message
            stop_note = f"⚠️ Лимит исчерпан: обработано {len(outputs)} из {len(chunks)} частей."
            break
        except Exception as e:
            logger.error(f"Dialogue error: {e}")
            if not outputs:
                return "❌ Модель вернула пустой ответ. Попробуйте ещё раз." if "empty_content" in str(e) \
                    else _err_text("Ошибка разбивки по ролям", e)
            stop_note = f"⚠️ Ошибка на части {i + 1} из {len(chunks)}: остальное не обработано."
            break
        outputs.append(out.strip())

    result = "\n\n".join(outputs)
    if skipped:
        result += f"\n\n⚠️ Запись очень длинная: обработано {len(chunks)} частей, остальные {skipped} отброшены."
    if stop_note:
        result += "\n\n" + stop_note
    return result + f"\n\n_{config.DIALOGUE_NOTE}_"


# ============================================================================
# VISION PROCESSOR (OCR)
# ============================================================================

class VisionProcessor:
    def __init__(self):
        self.groq_clients = []

    def init_clients(self, groq_clients: list):
        self.groq_clients = groq_clients

    async def extract_text(self, image_bytes: bytes) -> str:
        if not self.groq_clients:
            return config.ERROR_NO_GROQ
        try:
            access.charge()
        except access.QuotaExceeded as qe:
            return qe.user_message

        base64_image = base64.b64encode(image_bytes).decode('utf-8')

        async def extract(client):
            last: Optional[Exception] = None
            for model in config.GROQ_VISION_MODELS:
                try:
                    response = await client.chat.completions.create(
                        model=model,
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": config.OCR_PROMPT},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                            ]
                        }],
                        temperature=config.VISION_TEMPERATURE,
                        max_tokens=config.VISION_MAX_TOKENS,
                    )
                    return response.choices[0].message.content
                except Exception as e:
                    low = str(e).lower()
                    if "404" in low or "model_not_found" in low or "decommission" in low or "does not exist" in low:
                        logger.warning(f"Vision: модель {model} недоступна на Groq → следующая")
                        last = e
                        continue
                    raise
            raise last or Exception("Нет доступной vision-модели Groq")

        try:
            return await _make_groq_request(self.groq_clients, extract)
        except Exception as e:
            logger.error(f"Vision OCR error: {e}")
            return _err_text("Ошибка распознавания текста", e)


vision_processor = VisionProcessor()


# ============================================================================
# VIDEO PROCESSING (только локальные файлы)
# ============================================================================

class VideoProcessor:
    @staticmethod
    async def check_video_duration(filepath: str) -> Optional[float]:
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception as e:
            logger.warning(f"Error checking video duration: {e}")
        return None

    @staticmethod
    async def extract_audio_from_video(video_path: str, output_path: str) -> bool:
        try:
            subprocess.run(
                ['ffmpeg', '-i', video_path, '-vn', '-acodec', 'libmp3lame',
                 '-ab', '64k', '-ar', str(config.AUDIO_SAMPLE_RATE), '-ac', '1', '-y', output_path],
                capture_output=True, timeout=300
            )
            return os.path.exists(output_path) and os.path.getsize(output_path) > 0
        except Exception as e:
            logger.error(f"Audio extraction error: {e}")
            return False


video_processor = VideoProcessor()


# ============================================================================
# AUDIO TRANSCRIPTION
# ============================================================================

def _format_timecode(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def _segments_to_timecoded_text(segments: list) -> str:
    lines = []
    for seg in segments:
        tc = _format_timecode(seg.get("start", 0))
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"{tc} {text}")
    return "\n".join(lines)


# ============================================================================
# СЕГМЕНТЫ РАСШИФРОВКИ (для режима «Диалог»)
# ============================================================================

_segments_by_text: "OrderedDict[str, str]" = OrderedDict()
_SEGMENTS_KEEP = 60


def _seg_key(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def remember_segments(text: str, segments: list) -> None:
    """Запоминает построчные фразы Whisper под ключом расшифровки (небольшой LRU в памяти)."""
    lines = []
    for seg in segments:
        t = (seg.get("text", "") if isinstance(seg, dict) else getattr(seg, "text", "")).strip()
        if t:
            lines.append(t)
    key = _seg_key(text)
    if not lines or not key:
        return
    _segments_by_text[key] = "\n".join(lines)
    _segments_by_text.move_to_end(key)
    while len(_segments_by_text) > _SEGMENTS_KEEP:
        _segments_by_text.popitem(last=False)


def segments_for(text: str) -> Optional[str]:
    """Построчная расшифровка, если текст получен из аудио в этой сессии бота."""
    return _segments_by_text.get(_seg_key(text))


async def transcribe_voice(audio_bytes: bytes, groq_clients: list, with_timecodes: bool = False) -> str:
    try:
        access.charge()
    except access.QuotaExceeded as qe:
        return qe.user_message

    async def transcribe(client):
        if with_timecodes:
            response = await client.audio.transcriptions.create(
                model=config.GROQ_MODELS["transcription"],
                file=("audio.ogg", audio_bytes, "audio/ogg"),
                language=config.AUDIO_LANGUAGE,
                response_format="verbose_json",
                temperature=config.MODEL_TEMPERATURES["transcription"],
            )
            segments = getattr(response, "segments", None)
            if segments:
                return _segments_to_timecoded_text(segments)
            return getattr(response, "text", str(response))
        else:
            # verbose_json даёт те же слова плюс сегменты: по ним режим «Диалог»
            # видит границы фраз. Текст возвращаем тот же, что и раньше.
            response = await client.audio.transcriptions.create(
                model=config.GROQ_MODELS["transcription"],
                file=("audio.ogg", audio_bytes, "audio/ogg"),
                language=config.AUDIO_LANGUAGE,
                response_format="verbose_json",
                temperature=config.MODEL_TEMPERATURES["transcription"],
            )
            if isinstance(response, str):
                return response
            text = (getattr(response, "text", None) or "").strip()
            segments = getattr(response, "segments", None)
            if segments:
                remember_segments(text, segments)
            return text or str(response)

    try:
        return await _make_groq_request(groq_clients, transcribe)
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return _err_text("Ошибка распознавания", e)


# ============================================================================
# TEXT PROCESSING - CORRECTION
# ============================================================================

async def correct_text_basic(text: str, groq_clients: list) -> str:
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    text = _truncate_text_for_model(text, "basic")

    def _msgs(t: str) -> list:
        return [{"role": "user", "content": config.BASIC_CORRECTION_PROMPT + f"\n\nТекст:\n{t}"}]

    temp = config.MODEL_TEMPERATURES["basic"]
    try:
        return await _text_completion("basic", groq_clients, _msgs(text), temp, max_tokens=4000)
    except Exception as e:
        logger.error(f"Basic correction error: {e}")
        try:
            if "413" in str(e) or "rate_limit_exceeded" in str(e):
                shorter = text[:3000] + "... [обрезано]"
                return await _text_completion("basic", groq_clients, _msgs(shorter), temp, max_tokens=4000)
        except Exception as e2:
            e = e2
        return _err_text("Ошибка коррекции", e)


async def correct_text_premium(text: str, groq_clients: list) -> str:
    if not text.strip():
        return config.ERROR_EMPTY_TEXT
    text = _truncate_text_for_model(text, "premium")

    def _msgs(t: str) -> list:
        return [{"role": "user", "content": config.PREMIUM_CORRECTION_PROMPT + f"\n\nТекст:\n{t}"}]

    temp = config.MODEL_TEMPERATURES["premium"]
    try:
        return await _text_completion("premium", groq_clients, _msgs(text), temp, max_tokens=4000)
    except Exception as e:
        logger.error(f"Premium correction error: {e}")
        try:
            if "413" in str(e) or "rate_limit_exceeded" in str(e):
                shorter = text[:5000] + "... [обрезано]"
                return await _text_completion("premium", groq_clients, _msgs(shorter), temp, max_tokens=4000)
        except Exception as e2:
            e = e2
        return _err_text("Ошибка коррекции", e)


# ============================================================================
# TEXT PROCESSING - SUMMARIZATION
# ============================================================================

async def summarize_text(text: str, groq_clients: list) -> str:
    if not text.strip():
        return config.ERROR_EMPTY_TEXT

    words_count = len(text.split())
    if words_count < config.MIN_WORDS_FOR_SUMMARY or len(text) < config.MIN_CHARS_FOR_SUMMARY:
        return config.ERROR_TEXT_TOO_SHORT_FOR_SUMMARY

    text = _truncate_text_for_model(text, "reasoning")

    # Рассуждающие модели иногда тратят весь бюджет токенов на «размышление»
    # и отдают пустой content без ошибки. _text_completion считает пустой ответ
    # ошибкой и переходит к следующей модели цепочки (затем к Groq).
    def _msgs(t: str) -> list:
        return [{"role": "user", "content": config.SUMMARIZATION_PROMPT + f"\n\nТекст:\n{t}"}]

    temp = config.MODEL_TEMPERATURES["reasoning"]
    try:
        return await _text_completion("reasoning", groq_clients, _msgs(text), temp, max_tokens=2000)
    except Exception as e:
        logger.error(f"Summarization error: {e}")
        try:
            if "413" in str(e) or "rate_limit_exceeded" in str(e):
                shorter = text[:10000] + "... [обрезано]"
                return await _text_completion("reasoning", groq_clients, _msgs(shorter), temp, max_tokens=2000)
        except Exception as e2:
            e = e2
        if "empty_content" in str(e):
            return "❌ Модели вернули пустой ответ. Попробуйте ещё раз чуть позже."
        return _err_text("Ошибка создания саммари", e)


# ============================================================================
# YOUTUBE СУБТИТРЫ
# ============================================================================

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    try:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        _WEBSHARE_PROXY_AVAILABLE = True
    except ImportError:
        _WEBSHARE_PROXY_AVAILABLE = False
    YT_TRANSCRIPT_AVAILABLE = True
except ImportError:
    YT_TRANSCRIPT_AVAILABLE = False
    _WEBSHARE_PROXY_AVAILABLE = False


def _make_ytt_api():
    """Создаёт YouTubeTranscriptApi с Webshare-прокси если заданы WEBSHARE_USERNAME/PASSWORD."""
    ws_user = os.environ.get("WEBSHARE_USERNAME", "").strip()
    ws_pass = os.environ.get("WEBSHARE_PASSWORD", "").strip()
    if ws_user and ws_pass and _WEBSHARE_PROXY_AVAILABLE:
        logger.debug("YouTube transcript: using Webshare proxy")
        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=ws_user,
                proxy_password=ws_pass,
            )
        )
    logger.debug("YouTube transcript: no proxy configured")
    return YouTubeTranscriptApi()


def extract_youtube_video_id(url: str) -> Optional[str]:
    """Извлекает video_id из любого формата YouTube-ссылки."""
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def is_youtube_url(url: str) -> bool:
    return bool(extract_youtube_video_id(url.strip()))


def _format_yt_timecode(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


async def fetch_youtube_subtitles(video_id: str) -> dict:
    """
    Загружает субтитры YouTube видео.
    Совместимо с youtube-transcript-api >= 1.0

    Стратегия:
    1. Пробуем fetch(languages=["ru", "en"])
    2. Если не вышло — берём первый доступный язык через list_transcripts
    3. Если всё равно нет — возвращаем реальную ошибку для диагностики
    """
    if not YT_TRANSCRIPT_AVAILABLE:
        return {"error": "❌ Для YouTube субтитров требуется установить youtube-transcript-api"}

    def _fetch():
        ytt = _make_ytt_api()
        result = None

        # Попытка 1: предпочитаем ru/en
        try:
            result = ytt.fetch(video_id, languages=["ru", "en"])
        except Exception as e1:
            logger.debug(f"YT fetch ru/en failed ({type(e1).__name__}): {e1}")

        # Попытка 2: любой язык через list_transcripts
        if result is None:
            try:
                transcript_list = ytt.list(video_id)
                # Сначала ручные субтитры, потом авто-сгенерированные
                transcript = None
                for t in transcript_list:
                    if not t.is_generated:
                        transcript = t
                        break
                if transcript is None:
                    for t in transcript_list:
                        transcript = t
                        break
                if transcript is None:
                    raise Exception("No transcripts found in list")
                result = transcript.fetch()
            except Exception as e2:
                logger.debug(f"YT list fallback failed ({type(e2).__name__}): {e2}")
                raise e2  # пробрасываем реальную ошибку

        lang = getattr(result, "language_code", "unknown")

        try:
            segments = result.to_raw_data()
        except AttributeError:
            segments = [
                {"text": s.text, "start": s.start, "duration": getattr(s, "duration", 0)}
                for s in result
            ]

        return segments, lang

    try:
        segments, lang = await asyncio.to_thread(_fetch)

        if not segments:
            return {"error": "❌ Субтитры пустые"}

        # Уточняем язык через langdetect
        sample = " ".join(s.get("text", "") for s in segments[:20])
        detected = detect_language(sample)
        if detected != "unknown":
            lang = detected

        return {"raw": segments, "lang": lang, "error": None}

    except Exception as e:
        err = str(e)
        err_type = type(e).__name__
        logger.error(f"YouTube subtitles error (type={err_type}): {err}")

        # Блокировка YouTube на облачном IP (RequestBlocked / IpBlocked)
        if "RequestBlocked" in err_type or "IpBlocked" in err_type or "blocked" in err.lower():
            return {"error": "❌ YouTube блокирует запросы с этого сервера.\nПопробуйте позже или используйте другой источник."}
        # Требуется PO-токен (новая защита YouTube с 2025)
        if "PoTokenRequired" in err_type:
            return {"error": "❌ YouTube требует авторизацию для этого видео (PO token). Субтитры недоступны."}
        # Субтитры отключены автором
        if "TranscriptsDisabled" in err_type or "disabled" in err.lower():
            return {"error": "❌ Субтитры отключены автором видео"}
        # Видео недоступно / приватное
        if "VideoUnavailable" in err_type or "unavailable" in err.lower():
            return {"error": "❌ Видео недоступно (возможно, приватное или удалено)"}
        # Нет субтитров ни на одном языке
        if "NoTranscriptFound" in err_type or "No transcripts" in err or "Could not retrieve" in err:
            return {"error": "❌ У этого видео нет субтитров ни на одном языке"}
        # Всё остальное — показываем реальную ошибку для диагностики
        return {"error": f"❌ Ошибка субтитров ({err_type}): {err[:200]}"}


def _segments_to_plain_text(segments: list) -> str:
    """Субтитры → сплошной текст для передачи в LLM."""
    return " ".join(s.get("text", "").replace("\n", " ").strip() for s in segments if s.get("text", "").strip())


def _segments_to_timecoded(segments: list) -> str:
    """Субтитры → текст с таймкодами."""
    lines = []
    for seg in segments:
        text = seg.get("text", "").replace("\n", " ").strip()
        if text:
            tc = _format_yt_timecode(seg.get("start", 0))
            lines.append(f"{tc} {text}")
    return "\n".join(lines)


async def format_subtitles_as_dialogue(raw_text: str, groq_clients: list) -> str:
    """
    LLM форматирует субтитры в читаемый диалог:
    - убирает рекламные интеграции
    - группирует реплики в абзацы по смыслу
    - сохраняет живую речь
    """
    truncated = raw_text[:12000] + ("... [обрезано]" if len(raw_text) > 12000 else "")

    prompt = f"""Перед тобой субтитры видео — сплошной текст из кусочков речи.

ЗАДАЧА:
Отформатируй в читаемый текст диалога/монолога:

1. Убери рекламные интеграции — фрагменты где говорящий явно рекламирует продукт, сервис или просит подписаться/поставить лайк. Замени их на: [реклама вырезана]
2. Объедини короткие обрывки в связные абзацы по смыслу (смена темы = новый абзац)
3. Исправь только явные ошибки распознавания — не редактируй стиль и речь
4. Сохрани все имена, факты, цифры
5. Если в тексте чётко слышно смену говорящего (по контексту) — можешь обозначить абзац с новой строки, но не придумывай имена

ФОРМАТ: только готовый текст, без предисловий

Субтитры:
{truncated}"""

    try:
        return await _text_completion(
            "subtitles", groq_clients, [{"role": "user", "content": prompt}], 0.2, max_tokens=6000)
    except Exception as e:
        logger.error(f"Subtitle formatting error: {e}")
        # Fallback — возвращаем сырой текст
        return raw_text


# ============================================================================
# YOUTUBE КЭШИРОВАНИЕ (in-memory + опциональный Supabase)
# ============================================================================
#
# Двухуровневый кэш:
#   L1 — память процесса (мгновенно, теряется при рестарте)
#   L2 — Supabase (переживает рестарт, общий для всех воркеров)
#
# Кэшируем И сырые субтитры, И результат LLM-форматирования, чтобы повторный
# запрос того же видео не дёргал ни YouTube, ни Groq.
# ============================================================================

# L1: video_id → {"segments": [...], "lang": str, "ts": float}
_yt_subs_cache: Dict[str, Dict[str, Any]] = {}
# L1: video_id → {"dialogue": str, "timecoded": str, "ts": float}
_yt_fmt_cache: Dict[str, Dict[str, Any]] = {}

YT_SUBS_TTL = 86400        # сырые субтитры живут в памяти 24 ч
YT_FMT_TTL = 604800        # форматирование живёт 7 дней
YT_MEM_CACHE_MAX = 200     # максимум видео в каждом in-memory словаре


def _yt_cache_valid(ts: float, ttl: int) -> bool:
    return (time.time() - ts) < ttl


def _yt_cache_evict(cache: Dict[str, Dict[str, Any]]):
    """LRU-подобная очистка: если словарь переполнен, удаляем самые старые."""
    if len(cache) <= YT_MEM_CACHE_MAX:
        return
    # сортируем по ts (старые первыми), удаляем избыток + запас
    overflow = len(cache) - YT_MEM_CACHE_MAX + 20
    oldest = sorted(cache.items(), key=lambda kv: kv[1].get("ts", 0))[:overflow]
    for vid, _ in oldest:
        cache.pop(vid, None)


async def get_cached_youtube(video_id: str, database=None) -> Optional[Dict[str, Any]]:
    """
    Достать видео из кэша (L1 → L2). Возвращает dict вида:
      {"segments": [...], "lang": str,
       "dialogue": Optional[str], "timecoded": Optional[str],
       "source": "memory" | "supabase"}
    либо None, если нигде нет.

    database — модуль database (передаётся из bot.py), может быть None.
    """
    # --- L1: память ---
    subs = _yt_subs_cache.get(video_id)
    if subs and _yt_cache_valid(subs["ts"], YT_SUBS_TTL):
        fmt = _yt_fmt_cache.get(video_id)
        fmt_valid = fmt and _yt_cache_valid(fmt["ts"], YT_FMT_TTL)
        logger.debug(f"YouTube {video_id}: L1 hit (fmt={'yes' if fmt_valid else 'no'})")
        return {
            "segments": subs["segments"],
            "lang": subs["lang"],
            "dialogue": fmt["dialogue"] if fmt_valid else None,
            "timecoded": fmt["timecoded"] if fmt_valid else None,
            "source": "memory",
        }

    # --- L2: Supabase ---
    if database is not None and database.is_available():
        row = await database.get_youtube_cache(video_id)
        if row and row.get("segments"):
            # прогреваем L1
            _yt_subs_cache[video_id] = {
                "segments": row["segments"],
                "lang": row["lang"],
                "ts": time.time(),
            }
            _yt_cache_evict(_yt_subs_cache)
            if row.get("dialogue_text"):
                _yt_fmt_cache[video_id] = {
                    "dialogue": row["dialogue_text"],
                    "timecoded": row.get("timecoded_text") or "",
                    "ts": time.time(),
                }
                _yt_cache_evict(_yt_fmt_cache)
            logger.debug(f"YouTube {video_id}: L2 (Supabase) hit")
            return {
                "segments": row["segments"],
                "lang": row["lang"],
                "dialogue": row.get("dialogue_text"),
                "timecoded": row.get("timecoded_text"),
                "source": "supabase",
            }

    return None


async def fetch_youtube_subtitles_cached(video_id: str, database=None) -> dict:
    """
    Обёртка над fetch_youtube_subtitles с кэшем сырых субтитров.

    Возвращает то же, что fetch_youtube_subtitles:
      {"raw": segments, "lang": str, "error": None}
    плюс служебное поле "source" ("memory" | "supabase" | "youtube").
    На ошибке: {"error": "..."} без "source".
    """
    cached = await get_cached_youtube(video_id, database)
    if cached:
        return {
            "raw": cached["segments"],
            "lang": cached["lang"],
            "error": None,
            "source": cached["source"],
            # пробрасываем готовое форматирование, если оно было в кэше
            "_cached_dialogue": cached.get("dialogue"),
            "_cached_timecoded": cached.get("timecoded"),
        }

    # промах кэша — реальный запрос к YouTube
    result = await fetch_youtube_subtitles(video_id)
    if result.get("error"):
        return result

    segments = result["raw"]
    lang = result["lang"]

    # пишем в L1
    _yt_subs_cache[video_id] = {"segments": segments, "lang": lang, "ts": time.time()}
    _yt_cache_evict(_yt_subs_cache)

    # пишем в L2 фоном
    if database is not None and database.is_available():
        asyncio.create_task(database.save_youtube_subtitles(video_id, segments, lang))

    result["source"] = "youtube"
    return result


async def format_subtitles_cached(
    video_id: str,
    raw_text: str,
    segments: list,
    groq_clients: list,
    database=None,
    precomputed_dialogue: Optional[str] = None,
    precomputed_timecoded: Optional[str] = None,
) -> dict:
    """
    Форматирование субтитров в диалог с кэшем результата LLM.

    Если precomputed_* переданы (пришли из кэша субтитров) — используем их
    без обращения к Groq.

    Возвращает: {"dialogue": str, "timecoded": str, "source": "memory"|"supabase"|"llm"}
    """
    # 0) форматирование уже пришло вместе с субтитрами из кэша
    if precomputed_dialogue:
        return {
            "dialogue": precomputed_dialogue,
            "timecoded": precomputed_timecoded or _segments_to_timecoded(segments),
            "source": "cache",
        }

    # 1) L1
    fmt = _yt_fmt_cache.get(video_id)
    if fmt and _yt_cache_valid(fmt["ts"], YT_FMT_TTL):
        logger.debug(f"YouTube {video_id}: format L1 hit")
        return {"dialogue": fmt["dialogue"], "timecoded": fmt["timecoded"], "source": "memory"}

    # 2) LLM
    dialogue_text = await format_subtitles_as_dialogue(raw_text, groq_clients)
    timecoded_text = _segments_to_timecoded(segments)

    # пишем в L1
    _yt_fmt_cache[video_id] = {
        "dialogue": dialogue_text,
        "timecoded": timecoded_text,
        "ts": time.time(),
    }
    _yt_cache_evict(_yt_fmt_cache)

    # пишем в L2 фоном
    if database is not None and database.is_available():
        asyncio.create_task(
            database.update_youtube_formatted(video_id, dialogue_text, timecoded_text)
        )

    return {"dialogue": dialogue_text, "timecoded": timecoded_text, "source": "llm"}


def clear_youtube_cache(video_id: Optional[str] = None):
    """Очистка in-memory кэша (одно видео или всё)."""
    if video_id:
        _yt_subs_cache.pop(video_id, None)
        _yt_fmt_cache.pop(video_id, None)
    else:
        _yt_subs_cache.clear()
        _yt_fmt_cache.clear()
    logger.info(f"YouTube in-memory cache cleared: {video_id or 'all'}")


# ============================================================================
# URL SCRAPING
# ============================================================================

def is_url(text: str) -> bool:
    """Проверяет, является ли текст обычным URL (не YouTube, не API-эндпоинт)."""
    text = text.strip()
    if not (text.startswith(("http://", "https://")) and " " not in text and len(text) > 10):
        return False
    if is_youtube_url(text):
        return False
    # Исключаем API-эндпоинты и служебные URL
    blocked = ("supabase.co", "api.", "/rest/v1/", "/graphql", "localhost", "127.0.0.1")
    return not any(b in text for b in blocked)


async def fetch_url_text(url: str) -> str:
    """Скачивает страницу по URL и извлекает текст. С retry при 429."""
    try:
        import httpx
        from html.parser import HTMLParser

        class _TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self._text = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "nav", "footer", "header", "aside", "menu"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style", "nav", "footer", "header", "aside", "menu"):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    stripped = data.strip()
                    if stripped:
                        self._text.append(stripped)

            def get_text(self):
                return "\n".join(self._text)

        # Несколько вариантов User-Agent для ротации
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]

        import random
        headers = {
            "User-Agent": random.choice(user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
        }

        last_error = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(2 * attempt)  # 2s, 4s между попытками
                headers["User-Agent"] = random.choice(user_agents)

            try:
                async with httpx.AsyncClient(
                    timeout=20,
                    follow_redirects=True,
                    headers=headers,
                ) as client:
                    response = await client.get(url)

                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 5))
                        wait = min(retry_after, 10)
                        logger.warning(f"URL 429, waiting {wait}s (attempt {attempt+1})")
                        await asyncio.sleep(wait)
                        last_error = f"429 Too Many Requests"
                        continue

                    if response.status_code == 403:
                        return "❌ Сайт закрыт для автоматических запросов (403 Forbidden)"

                    if response.status_code == 401:
                        return "❌ Сайт требует авторизации"

                    response.raise_for_status()

                    parser = _TextExtractor()
                    parser.feed(response.text)
                    text = parser.get_text()

                    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 20]
                    text = "\n".join(lines)

                    if not text or len(text) < 100:
                        return "❌ Не удалось извлечь текст со страницы. Возможно, контент загружается динамически (JavaScript)."

                    if len(text) > 30000:
                        text = text[:30000] + "\n... [страница обрезана]"

                    logger.info(f"Fetched URL {url}: {len(text)} chars")
                    return text

            except httpx.TimeoutException:
                last_error = "таймаут соединения"
                continue
            except httpx.HTTPStatusError as e:
                last_error = str(e)
                break

        return f"❌ Не удалось загрузить страницу: {last_error or 'неизвестная ошибка'}"

    except ImportError:
        return "❌ Для обработки ссылок требуется установить httpx"
    except Exception as e:
        logger.error(f"URL fetch error: {e}")
        return f"❌ Не удалось загрузить страницу: {str(e)[:100]}"


# ============================================================================
# ОПРЕДЕЛЕНИЕ ЯЗЫКА
# ============================================================================

def detect_language(text: str) -> str:
    """Определяет язык текста. Возвращает код ('ru', 'en', ...) или 'unknown'."""
    try:
        from langdetect import detect
        return detect(text[:1000])
    except Exception:
        return "unknown"


def is_non_russian(text: str) -> bool:
    """Возвращает True если текст явно не на русском."""
    return detect_language(text) not in ("ru", "unknown")


# ============================================================================
# ПЕРЕВОД
# ============================================================================

async def translate_to_russian(text: str, groq_clients: list) -> str:
    """Переводит текст на русский язык."""
    if not text.strip():
        return config.ERROR_EMPTY_TEXT

    text_to_translate = _truncate_text_for_model(text, "premium")

    prompt = (
        "Переведи следующий текст на русский язык.\n"
        "Сохрани структуру, абзацы и форматирование оригинала.\n"
        "Переводи точно, без сокращений и добавлений.\n"
        "Выведи ТОЛЬКО перевод, без предисловий и комментариев.\n\n"
        f"Текст:\n{text_to_translate}"
    )

    try:
        return await _text_completion(
            "premium", groq_clients, [{"role": "user", "content": prompt}], 0.1, max_tokens=4000)
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return _err_text("Ошибка перевода", e)


# ============================================================================
# РАБОТА НАД ОШИБКАМИ
# ============================================================================

async def explain_corrections(original_text: str, corrected_text: str, groq_clients: list) -> str:
    """
    Сравнивает оригинал и исправленный текст, объясняет каждую правку.
    Использует premium модель — она точнее находит различия.
    """
    if not original_text.strip() or not corrected_text.strip():
        return "❌ Нет текста для разбора"

    # Если тексты идентичны — сразу говорим об этом
    if original_text.strip() == corrected_text.strip():
        return "🎓 <b>Работа над ошибками</b>\n\nОшибок не найдено — текст был чистым ✅"

    # Обрезаем оба текста чтобы уложиться в лимит модели
    max_len = 4000
    orig_truncated = original_text[:max_len] + ("... [обрезан]" if len(original_text) > max_len else "")
    corr_truncated = corrected_text[:max_len] + ("... [обрезан]" if len(corrected_text) > max_len else "")

    prompt = (
        config.EXPLAIN_CORRECTIONS_PROMPT
        + f"\n\nОРИГИНАЛ:\n{orig_truncated}"
        + f"\n\nИСПРАВЛЕННЫЙ ТЕКСТ:\n{corr_truncated}"
    )

    try:
        # низкая температура — нужна точность, не творчество
        return await _text_completion(
            "premium", groq_clients, [{"role": "user", "content": prompt}], 0.1, max_tokens=2000)
    except Exception as e:
        logger.error(f"Explain corrections error: {e}")
        return _err_text("Ошибка при разборе правок", e)


# ============================================================================
# ДИАЛОГОВЫЙ РЕЖИМ
# ============================================================================

def save_document_for_dialog(user_id: int, msg_id: int, document_text: str, source: str = "unknown"):
    if user_id not in document_dialogues:
        document_dialogues[user_id] = {}
    document_dialogues[user_id][msg_id] = {
        "full_text": document_text,
        "text": document_text,
        "original": document_text,
        "history": [],
        "timestamp": time.time(),
        "source": source
    }
    logger.info(f"💾 Документ для диалога: user={user_id}, msg={msg_id}, len={len(document_text)}")
    return document_dialogues[user_id][msg_id]


def get_document_text(user_id: int, msg_id: int) -> Optional[str]:
    if user_id not in document_dialogues or msg_id not in document_dialogues[user_id]:
        return None
    doc_data = document_dialogues[user_id][msg_id]
    for key in ["full_text", "text", "original"]:
        if key in doc_data and doc_data[key]:
            return doc_data[key]
    return None


async def stream_document_answer(
    user_id: int,
    msg_id: int,
    question: str,
    groq_clients: list
) -> AsyncGenerator[str, None]:
    if not has_text_llm(groq_clients):
        yield "❌ Нет доступных LLM-клиентов"
        return

    if user_id not in document_dialogues or msg_id not in document_dialogues[user_id]:
        yield "❌ Документ не найден. Сначала загрузите документ."
        return

    try:
        access.charge()   # один вопрос = одна единица (повторы внутри не списываются)
    except access.QuotaExceeded as qe:
        yield qe.user_message
        return

    doc_data = document_dialogues[user_id][msg_id]
    full_text = get_document_text(user_id, msg_id)
    if not full_text:
        yield "❌ Не удалось извлечь текст документа."
        return

    history = doc_data.get("history", [])
    context = ""
    for turn in history[-5:]:
        q = turn.get('question') or turn.get('q', '')
        a = turn.get('answer') or turn.get('a', '')
        if q and a:
            context += f"Вопрос: {q}\nОтвет: {a}\n\n"

    doc_preview = full_text[:20000] + "... [обрезан]" if len(full_text) > 20000 else full_text

    prompt = f"""Ты — ассистент, который отвечает на вопросы по содержанию документа.

Документ:
{doc_preview}

{context}

Вопрос:
{question}

Ответь на вопрос, используя только информацию из документа. Если ответа нет в документе, так и скажи.
Ответ должен быть подробным, но по существу."""

    async def _ask_once(busted=False):
        p = prompt if not busted else prompt + f"\n\n(intent-id: {int(time.time() * 1000)})"
        return await _open_text_stream(
            "reasoning", groq_clients,
            [
                {"role": "system", "content": "Ты отвечаешь строго по документу."},
                {"role": "user", "content": p},
            ],
            0.2, 2000,
        )

    try:
        stream = await _ask_once()

        full_answer = ""
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                piece = chunk.choices[0].delta.content
                full_answer += piece
                yield piece

        # Та же особенность gpt-oss-120b, что и в саммари: иногда весь
        # бюджет токенов уходит на "размышление", а видимый ответ пуст.
        # Стрим уже закончился, повторить его нельзя — делаем один
        # ретрай с нуля (тоже стримом, с меткой против кэша Groq).
        if not full_answer.strip():
            logger.warning("Stream Q&A: пустой ответ, повторяю попытку")
            stream = await _ask_once(busted=True)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    piece = chunk.choices[0].delta.content
                    full_answer += piece
                    yield piece

        if not full_answer.strip():
            full_answer = "❌ Модель дважды вернула пустой ответ. Попробуйте переформулировать вопрос или повторить чуть позже."
            yield full_answer

        history.append({
            "question": question, "answer": full_answer,
            "q": question, "a": full_answer,
            "timestamp": time.time()
        })
        doc_data["history"] = history[-config.MAX_DIALOG_HISTORY:]

    except Exception as e:
        logger.error(f"Stream error: {e}", exc_info=True)
        yield f"❌ Ошибка при генерации ответа: {str(e)[:100]}"


# ============================================================================
# FILE PROCESSING
# ============================================================================

async def process_video_file(video_bytes: bytes, filename: str, groq_clients: list, with_timecodes: bool = False) -> str:
    # Лимит проверяем до тяжёлой работы: ffmpeg на исчерпавшем лимит пользователе не запускаем.
    uid = access.current_user_id.get()
    if uid is not None and not access.is_admin(uid) and (access.remaining(uid) or 0) <= 0:
        return access.QuotaExceeded(access.used_today(uid), access.user_limit(uid)).user_message

    # Расширение приходит от пользователя (имя файла): оставляем только буквы/цифры.
    raw_ext = filename.rsplit(".", 1)[-1] if "." in filename else "mp4"
    file_ext = re.sub(r"[^a-z0-9]", "", raw_ext.lower())[:5] or "mp4"
    # Уникальный суффикс: два пользователя в одну секунду больше не перезаписывают файлы друг друга.
    stamp = f"{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    temp_video_path = f"{config.TEMP_DIR}/video_{stamp}.{file_ext}"
    temp_audio_path = f"{config.TEMP_DIR}/audio_{stamp}.mp3"

    try:
        with open(temp_video_path, 'wb') as f:
            f.write(video_bytes)

        duration = await video_processor.check_video_duration(temp_video_path)
        if duration and duration > 3600:
            return config.ERROR_VIDEO_TOO_LONG

        if not await video_processor.extract_audio_from_video(temp_video_path, temp_audio_path):
            return "❌ Ошибка извлечения звука из видео"

        with open(temp_audio_path, 'rb') as f:
            audio_bytes = f.read()

        return await transcribe_voice(audio_bytes, groq_clients, with_timecodes=with_timecodes)

    except Exception as e:
        logger.error(f"Error processing video file: {e}")
        return _err_text("Ошибка обработки видеофайла", e)

    finally:
        # Временные файлы удаляются при любом исходе: успех, ошибка, отмена задачи.
        for p in (temp_video_path, temp_audio_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.debug(f"temp file cleanup failed for {p}: {e}")


async def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Извлечение текста из PDF. Тяжёлая работа вынесена в thread чтобы не блокировать event loop."""
    if not PDFPLUMBER_AVAILABLE:
        return "❌ Для работы с PDF требуется установить pdfplumber"

    def _extract_sync():
        pdf_buffer = io.BytesIO(pdf_bytes)
        text = ""
        page_count = 0

        with pdfplumber.open(pdf_buffer) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                if config.PDF_MAX_PAGES and page_num > config.PDF_MAX_PAGES:
                    break

                page_text = page.extract_text()
                if page_text:
                    text += f"\n--- Страница {page_num} ---\n"
                    text += page_text + "\n"

                tables = page.find_tables()
                if tables:
                    for table_idx, table in enumerate(tables, 1):
                        text += f"\n[Таблица {table_idx} на странице {page_num}]\n"
                        table_data = table.extract()
                        for row in table_data:
                            if row:
                                text += " | ".join(str(cell) if cell else "" for cell in row) + "\n"

                page_count += 1

        if not text.strip():
            raise ValueError("Не удалось извлечь текст из PDF")

        logger.info(f"Extracted text from {page_count} PDF pages, {len(pdf_bytes) // 1024} KB")
        return text.strip()

    try:
        return await asyncio.to_thread(_extract_sync)
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return f"❌ Ошибка обработки PDF: {str(e)}"


async def extract_text_from_docx(docx_bytes: bytes) -> str:
    if not DOCX_AVAILABLE:
        return "❌ Для работы с DOCX требуется установить python-docx"
    try:
        doc_buffer = io.BytesIO(docx_bytes)
        doc = python_docx.Document(doc_buffer)
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if not text.strip():
            return "❌ Документ пуст"
        return text.strip()
    except Exception as e:
        logger.error(f"DOCX extraction error: {e}")
        return f"❌ Ошибка обработки DOCX: {str(e)}"


def _decode_text_bytes(data: bytes) -> str:
    """Декодирует байты в текст: BOM, UTF-16, UTF-8, затем кириллические кодировки."""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1251", "koi8-r", "cp866"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _normalize_text(text: str) -> str:
    """Единый вид переносов, без NUL и лишних пустых строк."""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def extract_text_from_txt(txt_bytes: bytes) -> str:
    try:
        return _normalize_text(_decode_text_bytes(txt_bytes))
    except Exception as e:
        logger.error(f"TXT reading error: {e}")
        return f"❌ Ошибка чтения текстового файла: {str(e)}"


def _strip_markdown_front_matter(text: str) -> str:
    """Убирает YAML front matter (--- ... ---) в начале .md файла."""
    return re.sub(r"\A---\s*\n.*?\n(?:---|\.\.\.)\s*\n", "", text, count=1, flags=re.DOTALL)


async def extract_text_from_markdown(md_bytes: bytes) -> str:
    """Markdown читается как есть (разметка сохраняется), без front matter."""
    return _normalize_text(_strip_markdown_front_matter(_decode_text_bytes(md_bytes)))


_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "blockquote", "pre", "ul", "ol", "table", "title"}
_SKIP_TAGS = {"script", "style", "head", "noscript", "svg", "nav", "footer", "aside"}


def _html_to_text(html_str: str) -> str:
    from html.parser import HTMLParser

    class _P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts: list = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in _SKIP_TAGS:
                self.skip += 1
            if tag in _BLOCK_TAGS and not self.skip:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in _SKIP_TAGS and self.skip:
                self.skip -= 1
            if tag in _BLOCK_TAGS and not self.skip:
                self.parts.append("\n")

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    p = _P()
    p.feed(html_str)
    p.close()
    text = "".join(p.parts)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return _normalize_text(text)


async def extract_text_from_html(html_bytes: bytes) -> str:
    raw = html_bytes[:2000].decode("ascii", errors="ignore")
    m = re.search(r'charset=["\']?([\w-]+)', raw, re.IGNORECASE)
    text = None
    if m:
        try:
            text = html_bytes.decode(m.group(1))
        except (LookupError, UnicodeDecodeError):
            text = None
    if text is None:
        text = _decode_text_bytes(html_bytes)
    return await asyncio.to_thread(_html_to_text, text)


def _xml_text_blocks(xml_bytes: bytes, block_tags: set) -> str:
    """Собирает текст блочных элементов XML (абзацы, заголовки) в строки."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)
    lines = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in block_tags:
            t = "".join(el.itertext()).strip()
            if t:
                lines.append(t)
    return "\n\n".join(lines)


async def extract_text_from_fb2(fb2_bytes: bytes) -> str:
    """FB2: абзацы <p> из <body> (без бинарных вложений и заметок-обложек)."""
    def _run():
        import xml.etree.ElementTree as ET
        root = ET.fromstring(fb2_bytes)
        out = []
        for body in root.iter():
            if body.tag.rsplit("}", 1)[-1] != "body":
                continue
            for el in body.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag in ("p", "v", "subtitle", "text-author"):
                    t = "".join(el.itertext()).strip()
                    if t:
                        out.append(t)
                elif tag == "empty-line":
                    out.append("")
        return _normalize_text("\n".join(out))
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"FB2 error: {e}")
        return f"❌ Ошибка чтения FB2: {str(e)[:100]}"


async def extract_text_from_odt(odt_bytes: bytes) -> str:
    def _run():
        import zipfile
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(io.BytesIO(odt_bytes)) as z:
            root = ET.fromstring(z.read("content.xml"))
        ns_text = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
        lines = []

        def walk(el):
            for ch in el:
                if ch.tag in (ns_text + "p", ns_text + "h"):
                    parts = []
                    for node in ch.iter():
                        if node.tag == ns_text + "tab":
                            parts.append("\t")
                        elif node.tag == ns_text + "line-break":
                            parts.append("\n")
                        elif node.tag == ns_text + "s":
                            parts.append(" " * int(node.get(ns_text + "c", "1")))
                        if node.text and node.tag not in (ns_text + "tab", ns_text + "line-break"):
                            parts.append(node.text)
                        if node is not ch and node.tail:
                            parts.append(node.tail)
                    lines.append("".join(parts).strip())
                else:
                    walk(ch)
        walk(root)
        return _normalize_text("\n".join(lines))
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"ODT error: {e}")
        return f"❌ Ошибка чтения ODT: {str(e)[:100]}"


async def extract_text_from_epub(epub_bytes: bytes) -> str:
    def _run():
        import zipfile
        import posixpath
        import xml.etree.ElementTree as ET
        z = zipfile.ZipFile(io.BytesIO(epub_bytes))
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
        opf = ET.fromstring(z.read(opf_path))
        base = posixpath.dirname(opf_path)
        manifest = {}
        for e in opf.iter():
            if e.tag.endswith("}item") or e.tag == "item":
                manifest[e.get("id")] = e.get("href")
        order = [e.get("idref") for e in opf.iter() if e.tag.endswith("itemref") or e.tag == "itemref"]
        chapters = []
        for idref in order:
            href = manifest.get(idref)
            if not href:
                continue
            path = posixpath.normpath(posixpath.join(base, href.split("#")[0]))
            if path not in z.namelist():
                continue
            raw = z.read(path)
            chapters.append(_html_to_text(_decode_text_bytes(raw)))
        return _normalize_text("\n\n".join(c for c in chapters if c))
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"EPUB error: {e}")
        return f"❌ Ошибка чтения EPUB: {str(e)[:100]}"


async def extract_text_from_rtf(rtf_bytes: bytes) -> str:
    """Минимальный RTF → текст без внешних зависимостей (кириллица, \\uN, \\'hh)."""
    def _run():
        s = rtf_bytes.decode("latin-1")
        m = re.search(r"\\ansicpg(\d+)", s)
        cp = f"cp{m.group(1)}" if m else "cp1251"
        skip_dest = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer",
                     "footnote", "themedata", "colorschememapping", "latentstyles", "datastore",
                     "listtable", "listoverridetable", "rsidtbl", "generator", "xmlnstbl", "fldinst"}
        out = []
        stack = []            # (skip, uc_skip)
        skip = False
        uc = 1
        pending_skip = 0
        pending_bytes = bytearray()

        def flush_bytes():
            if pending_bytes:
                try:
                    out.append(bytes(pending_bytes).decode(cp, errors="replace"))
                except LookupError:
                    out.append(bytes(pending_bytes).decode("cp1251", errors="replace"))
                pending_bytes.clear()

        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == "{":
                flush_bytes()
                stack.append((skip, uc))
                i += 1
                if s.startswith("\\*", i):
                    skip = True
                continue
            if c == "}":
                flush_bytes()
                if stack:
                    skip, uc = stack.pop()
                i += 1
                continue
            if c == "\\":
                i += 1
                if i >= n:
                    break
                d = s[i]
                if d in "\\{}":
                    flush_bytes()
                    if not skip and not pending_skip:
                        out.append(d)
                    elif pending_skip:
                        pending_skip -= 1
                    i += 1
                elif d == "'":
                    hx = s[i + 1:i + 3]
                    i += 3
                    if pending_skip:
                        pending_skip -= 1
                    elif not skip:
                        try:
                            pending_bytes.append(int(hx, 16))
                        except ValueError:
                            pass
                elif d == "\n" or d == "\r":
                    flush_bytes()
                    if not skip:
                        out.append("\n")
                    i += 1
                elif d == "~":
                    flush_bytes()
                    if not skip:
                        out.append("\u00a0")
                    i += 1
                elif d == "-" or d == "_":
                    i += 1
                else:
                    m2 = re.match(r"([a-zA-Z]+)(-?\d+)? ?", s[i:])
                    if not m2:
                        i += 1
                        continue
                    word, arg = m2.group(1), m2.group(2)
                    i += m2.end()
                    flush_bytes()
                    if word in skip_dest:
                        skip = True
                    elif word == "uc" and arg is not None:
                        uc = int(arg)
                    elif word == "u" and arg is not None and not skip:
                        code = int(arg)
                        if code < 0:
                            code += 65536
                        out.append(chr(code))
                        pending_skip = uc
                    elif word in ("par", "line", "sect", "page") and not skip:
                        out.append("\n")
                    elif word == "tab" and not skip:
                        out.append("\t")
                    elif word == "emdash" and not skip:
                        out.append("—")
                    elif word == "endash" and not skip:
                        out.append("–")
                    elif word == "bullet" and not skip:
                        out.append("•")
                    elif word == "lquote" and not skip:
                        out.append("‘")
                    elif word == "rquote" and not skip:
                        out.append("’")
                    elif word == "ldblquote" and not skip:
                        out.append("«")
                    elif word == "rdblquote" and not skip:
                        out.append("»")
                continue
            if c in "\r\n":
                i += 1
                continue
            flush_bytes()
            if pending_skip:
                pending_skip -= 1
            elif not skip:
                out.append(c)
            i += 1
        flush_bytes()
        return _normalize_text("".join(out))
    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"RTF error: {e}")
        return f"❌ Ошибка чтения RTF: {str(e)[:100]}"


_SUB_TS = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}.*$")


async def extract_text_from_subtitles(sub_bytes: bytes) -> str:
    """SRT / VTT: убирает номера реплик, тайминги и теги, склеивает реплики в текст."""
    text = _normalize_text(_decode_text_bytes(sub_bytes))
    lines = []
    for line in text.split("\n"):
        t = line.strip()
        if not t or t.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        if re.fullmatch(r"\d+", t) or _SUB_TS.match(t):
            continue
        t = re.sub(r"<[^>]+>|\{\\[^}]*\}", "", t).strip()
        if t:
            lines.append(t)
    return _normalize_text(" ".join(lines))


_PLAIN_EXTS = {"txt", "text", "log", "rst", "tex", "org", "adoc", "nfo"}
_MD_EXTS = {"md", "markdown", "mdown", "mkd"}
_HTML_EXTS = {"html", "htm", "xhtml"}
_SUB_EXTS = {"srt", "vtt"}


def _looks_like_text(data: bytes) -> bool:
    """Эвристика для неизвестных расширений: нет NUL-байтов и почти нет управляющих."""
    sample = data[:4096]
    if not sample or b"\x00" in sample and not sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    ctrl = sum(1 for b in sample if b < 9 or 13 < b < 32)
    return ctrl / len(sample) < 0.02


async def extract_text_from_file(file_bytes: bytes, filename: str, groq_clients: list) -> str:
    mime_type, _ = mimetypes.guess_type(filename)
    file_ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''

    if mime_type and mime_type.startswith('image/') or file_ext in ['jpg', 'jpeg', 'png', 'bmp', 'gif', 'webp']:
        vision_processor.init_clients(groq_clients)
        return await vision_processor.extract_text(file_bytes)

    if mime_type == 'application/pdf' or file_ext == 'pdf':
        return await extract_text_from_pdf(file_bytes)

    if mime_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' or file_ext == 'docx':
        return await extract_text_from_docx(file_bytes)

    if file_ext == 'odt':
        return await extract_text_from_odt(file_bytes)
    if file_ext == 'rtf':
        return await extract_text_from_rtf(file_bytes)
    if file_ext == 'epub':
        return await extract_text_from_epub(file_bytes)
    if file_ext == 'fb2':
        return await extract_text_from_fb2(file_bytes)
    if file_ext in _MD_EXTS:
        return await extract_text_from_markdown(file_bytes)
    if file_ext in _HTML_EXTS:
        return await extract_text_from_html(file_bytes)
    if file_ext in _SUB_EXTS:
        return await extract_text_from_subtitles(file_bytes)
    if file_ext in _PLAIN_EXTS or mime_type == 'text/plain':
        return await extract_text_from_txt(file_bytes)

    if file_ext == 'doc':
        return config.ERROR_DOC_NOT_SUPPORTED

    # Неизвестное расширение: если внутри похоже на текст — читаем как текст
    if _looks_like_text(file_bytes) and not file_ext in ('zip', 'rar', '7z', 'exe', 'apk', 'mp3', 'mp4'):
        return await extract_text_from_txt(file_bytes)

    return config.ERROR_UNSUPPORTED_FORMAT


# ============================================================================
# ЭКСПОРТ В ФАЙЛЫ
# ============================================================================

async def save_to_txt(text: str, filepath: str) -> bool:
    try:
        def _write():
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
        await asyncio.to_thread(_write)
        return True
    except Exception as e:
        logger.error(f"TXT save error: {e}")
        return False


async def save_to_pdf(text: str, filepath: str) -> bool:
    try:
        def _write():
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
            from reportlab.lib.utils import simpleSplit

            c = canvas.Canvas(filepath, pagesize=A4)
            width, height = A4
            margin = 50
            line_height = 14
            y = height - margin

            c.setFont("Helvetica-Bold", 14)
            c.drawString(margin, y, "Обработанный текст")
            y -= 30
            c.setFont("Helvetica", 10)
            from datetime import datetime
            c.drawString(margin, y, f"Создано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            y -= 40
            c.setFont("Helvetica", 11)
            max_width = width - 2 * margin

            for paragraph in text.split('\n'):
                if not paragraph.strip():
                    y -= line_height
                    continue
                for line in simpleSplit(paragraph, "Helvetica", 11, max_width):
                    if y < margin + 20:
                        c.showPage()
                        y = height - margin
                        c.setFont("Helvetica", 11)
                    c.drawString(margin, y, line)
                    y -= line_height
            c.save()

        await asyncio.to_thread(_write)
        return True
    except ImportError:
        logger.warning("reportlab not installed, falling back to txt")
        return False
    except Exception as e:
        logger.error(f"PDF save error: {e}")
        return False


async def save_to_docx(text: str, filepath: str) -> bool:
    """Сохраняет текст в DOCX через python-docx."""
    if not DOCX_AVAILABLE:
        logger.warning("python-docx not installed")
        return False

    try:
        def _write():
            from docx import Document as DocxDocument
            from docx.shared import Pt, Inches
            from docx.enum.text import WD_ALIGN_PARAGRAPH
            from datetime import datetime

            doc = DocxDocument()

            # Заголовок
            title = doc.add_heading("Обработанный текст", level=1)
            title.alignment = WD_ALIGN_PARAGRAPH.LEFT

            # Дата
            date_para = doc.add_paragraph(f"Создано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            date_para.runs[0].font.size = Pt(9)
            date_para.runs[0].font.color.rgb = None  # серый через style

            doc.add_paragraph()  # отступ

            # Основной текст: каждый абзац — отдельный параграф
            for line in text.split('\n'):
                p = doc.add_paragraph(line)
                p.runs[0].font.size = Pt(11) if p.runs else None

            doc.save(filepath)

        await asyncio.to_thread(_write)
        return True
    except Exception as e:
        logger.error(f"DOCX save error: {e}")
        return False


# ============================================================================
# РАЗБОР ПО КОСТОЧКАМ
# ============================================================================

async def breakdown_corrections(original_text: str, corrected_text: str, groq_clients: list) -> str:
    """
    Объясняет каждое исправление между оригиналом и исправленным текстом.
    Использует premium-модель для точности.
    """
    if not original_text.strip() or not corrected_text.strip():
        return "❌ Нет текста для разбора."

    if original_text.strip() == corrected_text.strip():
        return "✅ Текст был чистым — исправлений нет."

    # Обрезаем чтобы уложиться в лимит — берём оба текста
    max_chars = 4000
    orig = original_text[:max_chars]
    corr = corrected_text[:max_chars]

    prompt = (
        config.BREAKDOWN_PROMPT
        + f"\n\nОРИГИНАЛ:\n{orig}"
        + f"\n\nИСПРАВЛЕННЫЙ ТЕКСТ:\n{corr}"
    )

    try:
        return await _text_completion(
            "premium", groq_clients, [{"role": "user", "content": prompt}], 0.2, max_tokens=2000)
    except Exception as e:
        logger.error(f"Breakdown error: {e}")
        return _err_text("Ошибка при разборе", e)


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def get_available_modes(text: str) -> list:
    words_count = len(text.split())
    text_length = len(text)
    available = ["basic", "premium"]
    if words_count >= config.MIN_WORDS_FOR_SUMMARY and text_length >= config.MIN_CHARS_FOR_SUMMARY:
        available.append("summary")
        available.append("protocol")
    if words_count >= config.MIN_WORDS_FOR_DIALOGUE:
        available.append("dialogue")
    return available


# ============================================================================
# ЭКСПОРТ
# ============================================================================

__all__ = [
    'transcribe_voice',
    'correct_text_basic',
    'correct_text_premium',
    'summarize_text',
    'breakdown_corrections',
    'extract_text_from_file',
    'get_available_modes',
    'vision_processor',
    'save_document_for_dialog',
    'stream_document_answer',
    'get_document_text',
    'document_dialogues',
    'save_to_txt',
    'save_to_pdf',
    'save_to_docx',
    'explain_corrections',
    'breakdown_corrections',
    'is_url',
    'fetch_url_text',
    'is_youtube_url',
    'extract_youtube_video_id',
    'fetch_youtube_subtitles',
    'fetch_youtube_subtitles_cached',
    'format_subtitles_as_dialogue',
    'format_subtitles_cached',
    'get_cached_youtube',
    'clear_youtube_cache',
    '_segments_to_plain_text',
    '_segments_to_timecoded',
    'YT_TRANSCRIPT_AVAILABLE',
    'detect_language',
    'is_non_russian',
    'translate_to_russian',
    'PDFPLUMBER_AVAILABLE',
    'DOCX_AVAILABLE',
]
