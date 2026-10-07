# bot.py
"""
Главный файл бота
Версия 4.0 — убраны видеоплатформы, добавлен DOCX, Supabase с fallback,
/history, подпись автора, rate limiting, кнопка "Задать вопрос" только в саммари
"""

import os
import io
import sys
import re
import math
import uuid
import hashlib
import signal
import logging
import asyncio
import time
from typing import Optional, List, Dict, Any, Callable, Awaitable
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, UploadFile, File, Header, HTTPException
from openai import AsyncOpenAI
import uvicorn

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    FSInputFile,
    TelegramObject,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeAllGroupChats,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramUnauthorizedError, TelegramNetworkError
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

import config
import html
import access
import processors
import textkit
import database
import link


# ============================================================================
# УТИЛИТЫ: санитизация текста
# ============================================================================

def sanitize_llm_output(text: str) -> str:
    """
    Конвертирует Markdown-разметку от LLM в Telegram HTML и очищает мусор.

    Порядок:
    1. Убираем null-байты
    2. Экранируем &, < , > в обычном тексте (до подстановки тегов)
    3. Конвертируем MD-разметку → HTML-теги Telegram:
       **text** / __text__  → <b>text</b>
       *text* / _text_      → <i>text</i>
       `text`               → <code>text</code>
       ```block```          → <code>block</code>
       ### Заголовок        → <b>Заголовок</b>
    """
    import re

    # 1. Null-байты
    text = text.replace('\x00', '')

    # 1.5. Некоторые модели отвечают готовыми HTML-тегами вместо Markdown (<b>…</b>).
    # Разрешённые теги прячем за служебными символами, чтобы экранирование их не сломало.
    _TAGS = {'b': '\ue001', 'strong': '\ue001', 'i': '\ue002', 'em': '\ue002',
             'u': '\ue003', 's': '\ue004', 'code': '\ue005'}
    _REAL = {'\ue001': 'b', '\ue002': 'i', '\ue003': 'u', '\ue004': 's', '\ue005': 'code'}
    for ch in _REAL:
        text = text.replace(ch, '')
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(
        r'<(/?)(b|strong|i|em|u|s|code)>',
        lambda m: ('\ue0f0' if m.group(1) else '') + _TAGS[m.group(2).lower()],
        text, flags=re.IGNORECASE)

    # 2. Экранируем HTML-спецсимволы в сыром тексте
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')

    # 3. Конвертируем Markdown → Telegram HTML

    # Блоки кода (``` ... ```) — многострочные, первыми чтобы не трогать содержимое
    text = re.sub(r'```(?:\w+)?\n?(.*?)```', lambda m: '<code>' + m.group(1).strip() + '</code>', text, flags=re.DOTALL)

    # Инлайн код (`code`)
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)

    # Bold: **text** или __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text, flags=re.DOTALL)

    # Italic: *text* или _text_ (не трогаем уже заменённые __bold__)
    text = re.sub(r'\*([^*\n]+)\*', r'<i>\1</i>', text)
    text = re.sub(r'(?<![_a-zA-Zа-яёА-ЯЁ])_([^_\n]+)_(?![_a-zA-Zа-яёА-ЯЁ])', r'<i>\1</i>', text)

    # Заголовки Markdown (### / ## / #) → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # Возвращаем разрешённые теги; закрывающий — с маркером \ue0f0 перед служебным символом
    text = re.sub('\ue0f0([\ue001-\ue005])', lambda m: '</' + _REAL[m.group(1)] + '>', text)
    text = re.sub('[\ue001-\ue005]', lambda m: '<' + _REAL[m.group(0)] + '>', text)
    text = text.replace('\ue0f0', '')

    # Модель могла прислать незакрытые или криво вложенные теги — Telegram отвергает такой HTML.
    # Выравниваем: лишние закрывающие убираем, незакрытые закрываем в конце.
    stack, out, pos = [], [], 0
    for m in re.finditer(r'<(/?)(b|i|u|s|code)>', text):
        out.append(text[pos:m.start()])
        pos = m.end()
        closing, name = bool(m.group(1)), m.group(2)
        if not closing:
            stack.append(name)
            out.append(m.group(0))
        elif stack and stack[-1] == name:
            stack.pop()
            out.append(m.group(0))
        # иначе: закрывающий без пары или с нарушенной вложенностью — пропускаем
    out.append(text[pos:])
    text = ''.join(out) + ''.join(f'</{n}>' for n in reversed(stack))

    # Telegram rejects an empty message. This can happen when a reasoning-only
    # response is stripped completely (for example, <think>...</think>).
    if not text.strip():
        return '❌ Модель не вернула текст.'

    return text


def sanitize_for_db(text: str) -> str:
    """Убирает null-байты перед записью в Supabase."""
    return text.replace('\x00', '') if text else text


# ============================================================================
# УТИЛИТЫ: пользовательское имя файла
# ============================================================================

# Разрешённые символы в имени: буквы (рус/англ), цифры, пробел, _, -
# Всё остальное вычищается. Пробелы потом заменим на _.
_FILENAME_ALLOWED_RE = None  # инициализируется лениво в sanitize_filename


def sanitize_filename(raw: str, max_len: int) -> str:
    """
    Чистит пользовательский ввод имени файла.

    - Удаляет всё, кроме букв (рус/англ), цифр, пробела, _ и -
    - Заменяет пробелы и серии _/- на одно _
    - Обрезает до max_len символов
    - Возвращает пустую строку, если после очистки ничего не осталось

    Кириллица сохраняется как есть (Telegram и FS её корректно отображают;
    транслитерация добавляет неоднозначность и не нужна).
    """
    import re
    global _FILENAME_ALLOWED_RE
    if _FILENAME_ALLOWED_RE is None:
        _FILENAME_ALLOWED_RE = re.compile(r'[^A-Za-zА-Яа-яЁё0-9 _\-]')

    if not raw:
        return ""

    # 1. Удаляем запрещённые символы
    cleaned = _FILENAME_ALLOWED_RE.sub('', raw)
    # 2. Сжимаем последовательности пробелов/подчёркиваний/дефисов
    cleaned = re.sub(r'[\s_]+', '_', cleaned)
    cleaned = re.sub(r'-+', '-', cleaned)
    # 3. Убираем _ и - по краям
    cleaned = cleaned.strip('_-')
    # 4. Обрезаем по длине
    cleaned = cleaned[:max_len].strip('_-')
    return cleaned


def build_export_filename(
    user_id: int,
    mode: str,
    custom_name: Optional[str] = None,
) -> str:
    """
    Строит итоговое имя файла (без расширения).

    Формат:
      [custom_name__]<mode>_<user_id>_<YYYYMMDD_HHMMSS>

    user_id и timestamp обязательны — они защищают от race condition
    при параллельных экспортах в общем /tmp.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{mode}_{user_id}_{timestamp}"
    if custom_name:
        return f"{custom_name}__{base}"
    return f"export_{base}"


# ============================================================================
# УТИЛИТЫ: персистентность user_context в Supabase
# ============================================================================

def _serialize_ctx(ctx_data: Dict[str, Any]) -> Dict[str, Any]:
    """Подготавливает запись user_context к записи в JSONB."""
    payload = {
        "original": ctx_data.get("original", ""),
        "mode": ctx_data.get("mode"),
        "available_modes": ctx_data.get("available_modes", []),
        "cached_results": ctx_data.get("cached_results", {}),
        "type": ctx_data.get("type", "text"),
        "chat_id": ctx_data.get("chat_id"),
        "filename": ctx_data.get("filename"),
        "transcript_id": ctx_data.get("transcript_id"),
        "is_translated": ctx_data.get("is_translated", False),
        "speaker_names": ctx_data.get("speaker_names", {}),
        "dialogue_raw": ctx_data.get("dialogue_raw"),
    }
    t = ctx_data.get("time")
    if isinstance(t, datetime):
        payload["time"] = t.isoformat()
    elif isinstance(t, str):
        payload["time"] = t
    return payload


def _deserialize_ctx(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Восстанавливает запись user_context из JSONB."""
    text = payload.get("original", "")
    t_raw = payload.get("time")
    try:
        t = datetime.fromisoformat(t_raw) if t_raw else datetime.now()
    except (ValueError, TypeError):
        t = datetime.now()

    return {
        "text": text,
        "original": text,
        "mode": payload.get("mode"),
        "available_modes": payload.get("available_modes", ["basic"]),
        "cached_results": payload.get("cached_results", {
            "basic": None, "premium": None, "summary": None
        }),
        "type": payload.get("type", "text"),
        "chat_id": payload.get("chat_id"),
        "filename": payload.get("filename"),
        "transcript_id": payload.get("transcript_id"),
        "is_translated": payload.get("is_translated", False),
        "speaker_names": {int(k): v for k, v in (payload.get("speaker_names") or {}).items()},
        "dialogue_raw": payload.get("dialogue_raw"),
        "time": t,
    }


async def _persist_ctx(user_id: int, msg_id: int):
    """
    Фоновая задача: сохраняет один user_context в Supabase.
    Молча игнорирует ошибки — это не критичный путь.
    """
    try:
        ctx = user_context.get(user_id, {}).get(msg_id)
        if not ctx:
            return
        await database.save_user_context(user_id, msg_id, _serialize_ctx(ctx))
    except Exception as e:
        logger.debug(f"persist_ctx failed for {user_id}/{msg_id}: {e}")


def schedule_persist(user_id: int, msg_id: int):
    """Запускает persist в фоне без await (вызывается из любых хендлеров)."""
    if database.is_available():
        asyncio.create_task(_persist_ctx(user_id, msg_id))

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

load_dotenv()

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")
APP_SECRET_TOKEN = os.environ.get("APP_SECRET_TOKEN", "my_super_secret_123")
# Стиль коррекции для Android API: "basic" или "premium" (по умолчанию premium)
APP_CORR_STYLE = os.environ.get("APP_CORR_STYLE", "premium").strip().lower()
if APP_CORR_STYLE not in ("basic", "premium"):
    APP_CORR_STYLE = "premium"

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
    force=True
)
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not found! Exiting.")
    exit(1)

# === ИНИЦИАЛИЗАЦИЯ БОТА ===
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
start_time = time.time()
polling_task = None
is_shutting_down = False
shutdown_event = asyncio.Event()
stats = {"total_updates": 0, "errors": 0, "processed_messages": 0}

# Контекст: user_id -> { message_id: {...} }
user_context: Dict[int, Dict[int, Any]] = {}

# Активные диалоги: user_id -> message_id документа
active_dialogs: Dict[int, int] = {}

# Rate limiting: user_id пользователей, у которых идёт обработка прямо сейчас
processing_users: set = set()

# Ожидание ввода имени файла перед экспортом
# user_id -> {
#   "mode": str, "msg_id": int, "format": str,
#   "target_user_id": int, "prompt_msg_id": int,
#   "task": asyncio.Task (таймаут),
# }
pending_filename_inputs: Dict[int, Dict[str, Any]] = {}

groq_clients = []

# img2prompt: режим ожидания картинок (user_id -> до какого времени, unix) и кэш
# картинок для переключения стилей (token -> {user_id, jpeg, ratio, ts, results})
awaiting_img2prompt: Dict[int, float] = {}
img_prompt_cache: Dict[str, Dict[str, Any]] = {}
img_detail_pref: Dict[int, int] = {}    # user_id -> выбранная подробность (1-3)

# Имена собеседников: user_id -> {"msg_id": int, "ts": float} (ждём ответ пользователя)
pending_speaker_names: Dict[int, Dict[str, Any]] = {}
SPEAKER_NAMES_TIMEOUT = 600

# Группы
GROUP_TYPES = {"group", "supergroup"}
group_auto: Dict[int, bool] = {}              # chat_id -> авто-расшифровка голосовых
group_cache: Dict[str, Dict[str, Any]] = {}   # token -> {chat_id, text, ts, results}
group_notified: Dict[int, str] = {}           # chat_id -> день, когда писали «лимит исчерпан»
GROUP_CACHE_TTL = 3600
GROUP_CACHE_MAX = 200

# Inline-режим
inline_cache: Dict[str, Dict[str, Any]] = {}  # token -> {user_id, text, ts}
inline_claimed: Dict[str, float] = {}         # inline_message_id -> ts (защита от двойной обработки)


# ============================================================================
# ОБРАБОТКА СИГНАЛОВ (GRACEFUL SHUTDOWN)
# ============================================================================

def handle_sigterm(signum, frame):
    global is_shutting_down
    if is_shutting_down:
        return
    logger.info("📡 Received SIGTERM, initiating graceful shutdown...")
    is_shutting_down = True
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(lambda: asyncio.create_task(shutdown_event.set()))
    except RuntimeError:
        asyncio.run(shutdown_event.set())


# ============================================================================
# MIDDLEWARE
# ============================================================================

class ErrorHandlingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        global stats
        stats["total_updates"] += 1
        try:
            result = await handler(event, data)
            stats["processed_messages"] += 1
            return result
        except TelegramUnauthorizedError as e:
            stats["errors"] += 1
            logger.error(f"❌ Ошибка авторизации: {e}")
            raise
        except TelegramNetworkError as e:
            stats["errors"] += 1
            logger.error(f"❌ Сетевая ошибка: {e}")
            raise
        except Exception as e:
            stats["errors"] += 1
            logger.error(f"❌ Необработанная ошибка: {e}", exc_info=True)
            if is_shutting_down:
                raise
            try:
                if hasattr(event, "message") and event.message:
                    await event.message.answer("❌ Произошла внутренняя ошибка. Попробуйте позже.")
                elif hasattr(event, "callback_query") and event.callback_query:
                    await event.callback_query.message.answer("❌ Произошла внутренняя ошибка.")
            except Exception as notify_err:
                logger.debug(f"Не смогли уведомить пользователя об ошибке: {notify_err}")
            raise


dp.message.middleware(ErrorHandlingMiddleware())
dp.callback_query.middleware(ErrorHandlingMiddleware())


class AccessMiddleware(BaseMiddleware):
    """
    Лимиты для обычных пользователей. Админ (ADMIN_IDS) проходит без проверок.
    Здесь — только входные проверки (пауза, размеры, остаток); сама единица
    списывается позже, в processors, в момент реального обращения к ИИ.
    """

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        uid = user.id
        access.remember_user(user)
        token = access.current_user_id.set(uid)
        try:
            if not access.is_admin(uid):
                await access.ensure_loaded(uid)
                if isinstance(event, types.Message):
                    deny = self._check_message(event, uid)
                    if deny is not None:
                        if deny:   # пустая строка — отказать молча (например, альбом фото)
                            try:
                                await event.answer(deny)
                            except Exception as e:
                                logger.debug(f"Не смогли отправить отказ по лимиту: {e}")
                        return None
            return await handler(event, data)
        finally:
            access.current_user_id.reset(token)

    @staticmethod
    def _check_message(message: types.Message, uid: int) -> Optional[str]:
        text = message.text or ""
        if message.chat.type != "private":    # группы: лимит чата и проверки делают групповые хендлеры
            return None
        if text.startswith("/"):              # команды бесплатны
            return None
        if uid in pending_filename_inputs or uid in pending_speaker_names:   # ввод имени файла / имён
            return None

        wait = access.cooldown_left(uid)
        if wait > 0:
            if message.media_group_id:        # альбом: не засыпаем пользователя ответами
                return ""
            return f"⏳ Не так быстро — подождите {math.ceil(wait)} с."

        caps = access.check_message_caps(message)
        if caps:
            return caps

        if access.remaining(uid) <= 0:
            return access.limit_exhausted_message(uid)
        return None


dp.message.middleware(AccessMiddleware())
dp.callback_query.middleware(AccessMiddleware())
dp.chosen_inline_result.middleware(AccessMiddleware())


# ============================================================================
# POLLING TASK
# ============================================================================

async def run_polling():
    global is_shutting_down
    logger.info("🚀 Starting bot polling task...")
    while not is_shutting_down:
        try:
            await dp.start_polling(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            if is_shutting_down:
                break
            logger.error(f"❌ Polling crashed: {e}. Restarting in 5s...", exc_info=True)
            await asyncio.sleep(5)


# ============================================================================
# FASTAPI
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global polling_task

    logger.info("=" * 50)
    logger.info("🟢 FASTAPI APP STARTING")
    logger.info("=" * 50)

    # Groq клиенты
    init_groq_clients()
    processors.vision_processor.init_clients(groq_clients)
    # OpenRouter (текстовые LLM)
    processors.init_text_clients()

    if not hasattr(processors, 'document_dialogues'):
        processors.document_dialogues = {}

    # Supabase
    db_ok = database.init_database()
    if db_ok:
        logger.info("✅ База данных подключена")
        # Восстанавливаем активные user_contexts из БД (переживаем рестарт Render)
        try:
            records = await database.load_active_user_contexts(config.CACHE_TIMEOUT_SECONDS)
            restored = 0
            for rec in records:
                uid = rec.get("user_id")
                mid = rec.get("msg_id")
                payload = rec.get("payload") or {}
                if not uid or not mid:
                    continue
                if uid not in user_context:
                    user_context[uid] = {}
                user_context[uid][mid] = _deserialize_ctx(payload)
                restored += 1
            logger.info(f"♻️  Восстановлено {restored} user_context из БД")
        except Exception as e:
            logger.warning(f"⚠️  Не удалось восстановить user_context: {e}")
    else:
        logger.info("📦 Работаем без базы данных")

    # Администраторы, лимиты и выбор модели
    if access.ADMIN_IDS:
        logger.info(f"👑 Админы: {sorted(access.ADMIN_IDS)}")
    else:
        logger.warning("⚠️ ADMIN_IDS не задан: лимиты действуют на ВСЕХ, включая вас, "
                       "а /model и /admin недоступны. Задайте ADMIN_IDS=<ваш Telegram ID>")
    try:
        await access.load_settings()
    except Exception as e:
        logger.warning(f"⚠️ Не удалось загрузить настройки модели: {e}")

    # Сброс вебхука
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook cleared")
    except Exception as e:
        logger.error(f"❌ Error clearing webhook: {e}")

    # Меню команд (кнопка «Меню» в интерфейсе Telegram)
    try:
        user_commands = [
            BotCommand(command="start",   description="👋 О боте"),
            BotCommand(command="help",    description="📋 Инструкция"),
            BotCommand(command="history", description="📜 История обработок"),
            BotCommand(command="img2prompt", description="🎨 Картинка → промпт"),
            BotCommand(command="limit",   description="📊 Мой дневной лимит"),
        ]
        await bot.set_my_commands(user_commands)
        admin_commands = user_commands + [
            BotCommand(command="model",  description="🧠 Выбор модели"),
            BotCommand(command="admin",  description="👑 Статистика и лимиты"),
            BotCommand(command="status", description="🛠 Состояние бота"),
        ]
        await bot.set_my_commands([
            BotCommand(command="autovoice", description="🎙 Авто-расшифровка голосовых (вкл/выкл)"),
            BotCommand(command="fix",       description="✨ Исправить сообщение (ответом)"),
            BotCommand(command="summary",   description="📊 Саммари (ответом)"),
            BotCommand(command="protocol",  description="📋 Протокол встречи (ответом)"),
            BotCommand(command="dialogue",  description="👥 Диалог по ролям (ответом)"),
            BotCommand(command="limit",     description="📊 Лимит чата"),
        ], scope=BotCommandScopeAllGroupChats())
        for admin_id in access.ADMIN_IDS:
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception as e:
                logger.debug(f"Админское меню для {admin_id} не задано (бот ещё не запускали?): {e}")
        logger.info("✅ Bot commands menu set")
    except Exception as e:
        logger.warning(f"⚠️ Could not set bot commands: {e}")

    # Запуск polling
    polling_task = asyncio.create_task(run_polling())

    # Фоновые задачи
    cleanup_task = asyncio.create_task(cleanup_old_contexts())
    temp_cleanup_task = asyncio.create_task(cleanup_temp_files())
    db_keepalive_task = asyncio.create_task(database.keep_alive_loop())

    # Сигналы
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handle_sigterm, sig, None)
        except NotImplementedError:
            pass

    logger.info("=" * 50)
    logger.info("✅ BOT IS RUNNING")
    logger.info("=" * 50)

    yield

    # === SHUTDOWN ===
    logger.info("🔴 SHUTTING DOWN")

    if polling_task and not polling_task.done():
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

    for task in [cleanup_task, temp_cleanup_task, db_keepalive_task]:
        task.cancel()
    await asyncio.gather(cleanup_task, temp_cleanup_task, db_keepalive_task, return_exceptions=True)

    user_context.clear()
    active_dialogs.clear()
    processing_users.clear()
    if hasattr(processors, 'document_dialogues'):
        processors.document_dialogues.clear()

    try:
        await bot.session.close()
    except Exception as e:
        logger.debug(f"bot.session.close failed during shutdown: {e}")

    logger.info("✅ BOT STOPPED")


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

# CORS — раньше не требовался: /api/dictate и /api/correct дёргает Android-
# приложение (не браузер), а на него CORS не распространяется. LINK —
# браузерная страница с другого домена (GitHub Pages), поэтому без этого
# fetch() из LINK будет падать по CORS независимо от того, что отвечает сервер.
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://ilsoft-dev.github.io"],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)

app.include_router(link.router)


@app.middleware("http")
async def monitor_requests(request: Request, call_next):
    stats["total_updates"] += 1
    try:
        return await call_next(request)
    except Exception as e:
        stats["errors"] += 1
        raise


@app.get("/health")
@app.head("/health")
@app.get("/ping")
async def health():
    return Response(
        content='{"status": "healthy", "service": "igramotey", "version": "4.0"}',
        media_type="application/json", status_code=200
    )


@app.get("/")
async def root():
    return {"service": "iГрамотей", "version": "4.0", "status": "running", "uptime": int(time.time() - start_time)}


@app.get("/metrics")
async def metrics():
    uptime = int(time.time() - start_time)
    text = f"""# HELP bot_uptime Uptime in seconds
# TYPE bot_uptime gauge
bot_uptime {uptime}
bot_requests_total {stats["total_updates"]}
bot_errors_total {stats["errors"]}
bot_processed_messages {stats["processed_messages"]}
bot_active_dialogs {len(active_dialogs)}
bot_users_in_context {len(user_context)}
"""
    if PSUTIL_AVAILABLE:
        try:
            ram_mb = psutil.Process().memory_info().rss / 1024 / 1024
            text += f"bot_ram_mb {ram_mb:.2f}\n"
        except Exception as e:
            logger.debug(f"psutil RAM read failed: {e}")
    return Response(content=text, media_type="text/plain")


# ============================================================================
# API ENDPOINT ДЛЯ ANDROID ПРИЛОЖЕНИЯ
# ============================================================================

@app.post("/api/dictate")
async def api_dictate(
    file: UploadFile = File(...),
    x_app_token: str = Header(None)
):
    """
    API для Android приложения:
    - Принимает аудиофайл (m4a)
    - Распознает речь (Whisper)
    - Делает красивую обработку (Llama)
    - Возвращает чистый текст
    """
    # Простейшая защита
    if x_app_token != APP_SECRET_TOKEN:
        logger.warning(f"API unauthorized attempt with token: {'***' if x_app_token else None}")
        raise HTTPException(status_code=403, detail="Forbidden")

    # Проверяем наличие Groq клиентов
    if not groq_clients:
        logger.error("API Dictate error: No Groq clients available")
        return {"status": "error", "text": "Сервис временно недоступен"}

    try:
        # Читаем файл
        audio_bytes = await file.read()
        
        # Проверка размера (не больше 20 МБ)
        if len(audio_bytes) > 20 * 1024 * 1024:
            return {"status": "error", "text": "Файл слишком большой (макс. 20 МБ)"}
        
        # Логируем запрос
        logger.info(f"API Dictate: file={file.filename}, size={len(audio_bytes)} bytes")
        
        # Транскрибация (Whisper через Groq)
        raw_text = await processors.transcribe_voice(audio_bytes, groq_clients)
        
        # Проверка результата
        if raw_text.startswith("❌"):
            logger.error(f"API Dictate transcription error: {raw_text}")
            return {"status": "error", "text": raw_text}
            
        if len(raw_text.strip()) < 2:
            return {"status": "error", "text": "Ничего не расслышал"}
        
        # Делаем коррекцию выбранным стилем (APP_CORR_STYLE: basic или premium)
        if APP_CORR_STYLE == "basic":
            corrected_text = await processors.correct_text_basic(raw_text, groq_clients)
        else:
            corrected_text = await processors.correct_text_premium(raw_text, groq_clients)
        
        if corrected_text.startswith("❌"):
            logger.error(f"API Dictate correction error: {corrected_text}")
            return {"status": "error", "text": corrected_text}

        # Чистим от маркдауна и лишних символов
        clean_text = (corrected_text
                     .replace("**", "")
                     .replace("__", "")
                     .replace("```", "")
                     .replace("#", "")
                     .strip())
        
        # Логируем успех
        logger.info(f"API Dictate success: {len(raw_text)} → {len(clean_text)} chars")
        
        return {
            "status": "success", 
            "text": clean_text,
            "original_length": len(raw_text),
            "processed_length": len(clean_text)
        }

    except Exception as e:
        logger.error(f"API Dictate error: {e}", exc_info=True)
        return {"status": "error", "text": f"Ошибка сервера: {str(e)[:50]}"}


@app.post("/api/correct")
async def api_correct(
    request: Request,
    x_app_token: str = Header(None)
):
    """
    API для обработки текста из буфера обмена в Android приложении:
    - Принимает JSON {"text": "..."}
    - Делает красивую обработку (Llama)
    - Возвращает чистый текст
    """
    # Простейшая защита
    if x_app_token != APP_SECRET_TOKEN:
        logger.warning(f"API /correct unauthorized attempt with token: {'***' if x_app_token else None}")
        raise HTTPException(status_code=403, detail="Forbidden")

    if not processors.has_text_llm(groq_clients):
        logger.error("API Correct error: No LLM clients available")
        return {"status": "error", "text": "Сервис временно недоступен"}

    try:
        body = await request.json()
        text = (body.get("text") or "").strip()

        # Проверки
        if len(text) < 3:
            return {"status": "error", "text": "Текст слишком короткий"}
        if len(text) > 20000:
            return {"status": "error", "text": "Текст слишком большой (макс. 20000 символов)"}

        logger.info(f"API Correct: {len(text)} chars")

        # Делаем коррекцию выбранным стилем (APP_CORR_STYLE: basic или premium)
        if APP_CORR_STYLE == "basic":
            corrected_text = await processors.correct_text_basic(text, groq_clients)
        else:
            corrected_text = await processors.correct_text_premium(text, groq_clients)

        if corrected_text.startswith("❌"):
            logger.error(f"API Correct error: {corrected_text}")
            return {"status": "error", "text": corrected_text}

        # Чистим от маркдауна
        clean_text = (corrected_text
                      .replace("**", "")
                      .replace("__", "")
                      .replace("```", "")
                      .replace("#", "")
                      .strip())

        logger.info(f"API Correct success: {len(text)} → {len(clean_text)} chars")

        return {
            "status": "success",
            "text": clean_text,
            "original_length": len(text),
            "processed_length": len(clean_text)
        }

    except Exception as e:
        logger.error(f"API Correct error: {e}", exc_info=True)
        return {"status": "error", "text": f"Ошибка сервера: {str(e)[:50]}"}


# ============================================================================
# GROQ КЛИЕНТЫ
# ============================================================================

def init_groq_clients():
    global groq_clients
    if not GROQ_API_KEYS:
        logger.warning("GROQ_API_KEYS not configured!")
        return
    keys = [k.strip() for k in GROQ_API_KEYS.split(",") if k.strip()]
    for key in keys:
        try:
            client = AsyncOpenAI(api_key=key, base_url="https://api.groq.com/openai/v1", timeout=config.GROQ_TIMEOUT)
            groq_clients.append(client)
            logger.info(f"✅ Groq client: {key[:8]}...")
        except Exception as e:
            logger.error(f"❌ Error init client {key[:8]}...: {e}")
    logger.info(f"✅ Total Groq clients: {len(groq_clients)}")
    logger.info(f"🎨 APP_CORR_STYLE = {APP_CORR_STYLE}")


# ============================================================================
# КОНТЕКСТ И КЭШ
# ============================================================================

def save_to_history(user_id: int, msg_id: int, text: str, mode: str = "basic", available_modes: list = None):
    if user_id not in user_context:
        user_context[user_id] = {}
    if len(user_context[user_id]) > config.MAX_CONTEXTS_PER_USER:
        oldest = min(user_context[user_id].keys(), key=lambda k: user_context[user_id][k]['time'])
        user_context[user_id].pop(oldest)
    user_context[user_id][msg_id] = {
        "text": text, "mode": mode, "time": datetime.now(),
        "available_modes": available_modes or ["basic"],
        "original": text,
        "cached_results": {"basic": None, "premium": None, "summary": None},
        "type": "text", "chat_id": None, "filename": None,
        "transcript_id": None,   # для связи с БД
    }
    # Бэкапим в Supabase, чтобы пережить рестарт Render
    schedule_persist(user_id, msg_id)


async def cleanup_old_contexts():
    while not is_shutting_down and not shutdown_event.is_set():
        try:
            await asyncio.sleep(config.CACHE_CHECK_INTERVAL)
            if is_shutting_down:
                break
            current_time = datetime.now()
            users_to_clean = []
            stale_keys: List[tuple] = []  # (user_id, msg_id) для удаления из БД

            for user_id, messages in user_context.items():
                for msg_id, ctx in list(messages.items()):
                    age = (current_time - ctx.get("time", current_time)).total_seconds()
                    if age > config.CACHE_TIMEOUT_SECONDS:
                        messages.pop(msg_id, None)
                        stale_keys.append((user_id, msg_id))
                if not messages:
                    users_to_clean.append(user_id)
            for uid in users_to_clean:
                user_context.pop(uid, None)

            # Чистим устаревшее в БД (один общий sweep — дешевле, чем N запросов)
            if database.is_available():
                try:
                    await database.cleanup_stale_user_contexts(config.CACHE_TIMEOUT_SECONDS)
                except Exception as e:
                    logger.debug(f"DB cleanup failed: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")


async def cleanup_temp_files():
    while not is_shutting_down and not shutdown_event.is_set():
        try:
            await asyncio.sleep(config.TEMP_FILE_RETENTION)
            if is_shutting_down or not config.CLEANUP_TEMP_FILES:
                continue
            current_time = datetime.now().timestamp()
            if not os.path.exists(config.TEMP_DIR):
                continue
            deleted = 0
            for filename in os.listdir(config.TEMP_DIR):
                if filename.startswith(('video_', 'audio_', 'text_', 'export_')):
                    filepath = os.path.join(config.TEMP_DIR, filename)
                    try:
                        if current_time - os.path.getmtime(filepath) > config.TEMP_FILE_RETENTION:
                            os.remove(filepath)
                            deleted += 1
                    except OSError as e:
                        logger.debug(f"Не смогли удалить {filepath}: {e}")
            if deleted:
                logger.debug(f"Cleaned up {deleted} temp files")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Temp cleanup error: {e}")


# ============================================================================
# КЛАВИАТУРЫ
# ============================================================================

def create_dialog_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🚪 Выйти из режима вопросов", callback_data=f"dialog_exit_{user_id}"))
    return builder.as_markup()


def create_keyboard(msg_id: int, current_mode: str, available_modes: list = None) -> InlineKeyboardMarkup:
    """Клавиатура после обработки. Кнопка 'Задать вопрос' только в режиме summary."""
    builder = InlineKeyboardBuilder()
    if available_modes is None:
        available_modes = ["basic", "premium"]

    mode_display = config.MODE_LABELS
    mode_buttons = []
    for mode_code in available_modes:
        if mode_code in mode_display:
            prefix = "✅ " if mode_code == current_mode else ""
            mode_buttons.append(InlineKeyboardButton(
                text=f"{prefix}{mode_display[mode_code]}",
                callback_data=f"mode_{mode_code}_{msg_id}"
            ))

    for i in range(0, len(mode_buttons), 2):
        if i + 1 < len(mode_buttons):
            builder.row(mode_buttons[i], mode_buttons[i + 1])
        else:
            builder.row(mode_buttons[i])

    if current_mode and current_mode in ("basic", "premium"):
        builder.row(
            InlineKeyboardButton(text="✏️ Работа над ошибками", callback_data=f"breakdown_{msg_id}")
        )

    if current_mode:
        builder.row(
            InlineKeyboardButton(text="📄 TXT", callback_data=f"export_{current_mode}_{msg_id}_txt"),
            InlineKeyboardButton(text="📊 PDF", callback_data=f"export_{current_mode}_{msg_id}_pdf"),
            InlineKeyboardButton(text="📝 DOCX", callback_data=f"export_{current_mode}_{msg_id}_docx"),
        )

    return builder.as_markup()


def create_options_keyboard(user_id: int, msg_id: int) -> InlineKeyboardMarkup:
    """Первичный выбор режима. Кнопка 'Задать вопрос' НЕ показывается здесь."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📝 Как есть", callback_data=f"process_{user_id}_basic_{msg_id}"),
        InlineKeyboardButton(text="✨ Красиво", callback_data=f"process_{user_id}_premium_{msg_id}"),
    )
    ctx_data = user_context.get(user_id, {}).get(msg_id)
    available = ctx_data.get("available_modes", []) if ctx_data else []
    extra = [
        InlineKeyboardButton(text=config.MODE_LABELS[m], callback_data=f"process_{user_id}_{m}_{msg_id}")
        for m in ("summary", "protocol", "dialogue") if m in available
    ]
    for i in range(0, len(extra), 2):
        builder.row(*extra[i:i + 2])
    builder.row(InlineKeyboardButton(text="🎭 Стиль текста…", callback_data=f"stm_{user_id}_{msg_id}"))
    return builder.as_markup()


def create_switch_keyboard(user_id: int, msg_id: int) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура переключения. Кнопка 'Задать вопрос' только если текущий режим — summary."""
    ctx_data = user_context.get(user_id, {}).get(msg_id)
    if not ctx_data:
        return None

    current = ctx_data.get("mode", "basic")
    available = ctx_data.get("available_modes", ["basic", "premium"])
    builder = InlineKeyboardBuilder()

    mode_display = config.MODE_LABELS
    mode_buttons = [
        InlineKeyboardButton(text=mode_display.get(m, m), callback_data=f"switch_{user_id}_{m}_{msg_id}")
        for m in available if m != current
    ]
    for i in range(0, len(mode_buttons), 2):
        if i + 1 < len(mode_buttons):
            builder.row(mode_buttons[i], mode_buttons[i + 1])
        else:
            builder.row(mode_buttons[i])

    # Кнопка "Задать вопрос" — только в режиме саммари
    if current == "summary" and len(ctx_data.get("original", "")) > 100:
        builder.row(InlineKeyboardButton(
            text="💬 Задать вопрос по тексту",
            callback_data=f"dialog_start_{user_id}_{msg_id}"
        ))

    # Стили и наглядный diff (diff бесплатный: считается кодом, без ИИ)
    style_row = [InlineKeyboardButton(text="🎭 Стиль…", callback_data=f"stm_{user_id}_{msg_id}")]
    if current in config.DIFF_MODES:
        style_row.append(InlineKeyboardButton(text="🔍 Что изменилось", callback_data=f"diff_{msg_id}"))
    builder.row(*style_row)

    # Кнопка "Работа над ошибками" — только для basic и premium
    if current in ("basic", "premium"):
        builder.row(InlineKeyboardButton(
            text="✏️ Работа над ошибками",
            callback_data=f"breakdown_{msg_id}"
        ))

    # Диалог по ролям: подставить имена вместо «Говорящий N»
    if current == "dialogue":
        builder.row(InlineKeyboardButton(text="👥 Назвать собеседников", callback_data=f"spk_{msg_id}"))

    # Кнопка перевода — если оригинал не на русском
    original = ctx_data.get("original", "")
    if original and processors.is_non_russian(original):
        if ctx_data.get("is_translated", False):
            builder.row(InlineKeyboardButton(
                text="↩️ Оригинал",
                callback_data=f"translate_back_{user_id}_{msg_id}"
            ))
        else:
            builder.row(InlineKeyboardButton(
                text="🌐 Перевести на русский",
                callback_data=f"translate_{user_id}_{msg_id}"
            ))

    if current:
        builder.row(
            InlineKeyboardButton(text="📄 TXT", callback_data=f"export_{user_id}_{current}_{msg_id}_txt"),
            InlineKeyboardButton(text="📊 PDF", callback_data=f"export_{user_id}_{current}_{msg_id}_pdf"),
            InlineKeyboardButton(text="📝 DOCX", callback_data=f"export_{user_id}_{current}_{msg_id}_docx"),
        )

    return builder.as_markup()


def _style_menu_kb(user_id: int, msg_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    btns = [
        InlineKeyboardButton(text=label, callback_data=f"switch_{user_id}_{key}_{msg_id}")
        for key, (label, _) in config.EDIT_STYLES.items()
    ]
    for i in range(0, len(btns), 2):
        builder.row(*btns[i:i + 2])
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"stmb_{user_id}_{msg_id}"))
    return builder.as_markup()


async def run_mode(mode: str, text: str, default: Optional[str] = None) -> str:
    """Единая точка запуска режимов обработки (кнопки, группы, inline)."""
    if mode == "basic":
        return await processors.correct_text_basic(text, groq_clients)
    if mode == "premium":
        return await processors.correct_text_premium(text, groq_clients)
    if mode == "summary":
        return await processors.summarize_text(text, groq_clients)
    if mode == "protocol":
        return await processors.make_protocol(text, groq_clients)
    if mode == "dialogue":
        return await processors.make_dialogue(text, groq_clients)
    if mode == "tr":
        return await processors.translate_to_russian(text, groq_clients)
    if mode in config.EDIT_STYLES:
        return await processors.rewrite_in_style(text, mode, groq_clients)
    return default if default is not None else "❌ Неизвестный режим"


# ============================================================================
# СОХРАНЕНИЕ ФАЙЛОВ
# ============================================================================

async def save_to_file(
    user_id: int,
    text: str,
    format_type: str,
    mode: str = "export",
    custom_name: Optional[str] = None,
) -> Optional[str]:
    """
    Сохраняет text в файл выбранного формата и возвращает путь.

    Имя строится через build_export_filename:
      [custom__]<mode>_<user_id>_<timestamp>.<ext>

    user_id + timestamp в имени гарантируют уникальность при параллельных
    экспортах в общем /tmp.
    """
    filename = build_export_filename(user_id, mode, custom_name)

    if format_type == "txt":
        filepath = f"{config.TEMP_DIR}/{filename}.txt"
        if await processors.save_to_txt(text, filepath):
            return filepath

    elif format_type == "pdf":
        filepath = f"{config.TEMP_DIR}/{filename}.pdf"
        if await processors.save_to_pdf(text, filepath):
            return filepath
        # fallback на txt
        filepath_txt = f"{config.TEMP_DIR}/{filename}.txt"
        if await processors.save_to_txt(text, filepath_txt):
            return filepath_txt

    elif format_type == "docx":
        filepath = f"{config.TEMP_DIR}/{filename}.docx"
        if await processors.save_to_docx(text, filepath):
            return filepath
        # fallback на txt
        filepath_txt = f"{config.TEMP_DIR}/{filename}.txt"
        if await processors.save_to_txt(text, filepath_txt):
            return filepath_txt

    return None


# ============================================================================
# СТРИМИНГ (ДИАЛОГ)
# ============================================================================

async def handle_streaming_answer(message: types.Message, user_id: int, msg_id: int, question: str):
    placeholder = await message.answer("💭 Думаю...")
    accumulated = ""
    last_edit_length = 0

    try:
        if is_shutting_down:
            await placeholder.edit_text("🛑 Бот останавливается.")
            return
        if not processors.has_text_llm(groq_clients):
            await placeholder.edit_text("❌ Нет доступных LLM-клиентов")
            return
        if user_id not in user_context or msg_id not in user_context[user_id]:
            await placeholder.edit_text("❌ Документ не найден. Начните заново.")
            active_dialogs.pop(user_id, None)
            return

        doc_text = user_context[user_id][msg_id].get("original", "")
        if not doc_text:
            await placeholder.edit_text("❌ Текст документа пуст")
            return

        if not hasattr(processors, 'document_dialogues'):
            processors.document_dialogues = {}
        if user_id not in processors.document_dialogues:
            processors.document_dialogues[user_id] = {}
        processors.document_dialogues[user_id][msg_id] = {"text": doc_text, "history": []}

        async for chunk in processors.stream_document_answer(user_id, msg_id, question, groq_clients):
            if chunk and not is_shutting_down:
                accumulated += chunk
                if len(accumulated) - last_edit_length > 30:
                    try:
                        display = accumulated + "▌"
                        if len(display) > 4096:
                            display = display[:4093] + "..."
                        await placeholder.edit_text(display, reply_markup=create_dialog_keyboard(user_id))
                    except Exception as e:
                        # типичный кейс — "message is not modified"
                        logger.debug(f"streaming edit_text skipped: {e}")
                    last_edit_length = len(accumulated)

        if is_shutting_down:
            return

        final = sanitize_llm_output(accumulated) if accumulated else "❌ Пустой ответ"
        if len(final) > 4096:
            final = final[:4093] + "..."
        await placeholder.edit_text(final, parse_mode="HTML", reply_markup=create_dialog_keyboard(user_id))

    except asyncio.CancelledError:
        try:
            await placeholder.edit_text("🛑 Генерация прервана.")
        except Exception as e:
            logger.debug(f"placeholder edit on cancel failed: {e}")
    except Exception as e:
        logger.error(f"Streaming error: {e}", exc_info=True)
        if not is_shutting_down:
            try:
                await placeholder.edit_text(f"❌ Ошибка при генерации: {str(e)[:200]}")
            except Exception as edit_err:
                logger.debug(f"error placeholder edit failed: {edit_err}")


# ============================================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: имя автора
# ============================================================================

def get_author_label(message: types.Message) -> str:
    """
    Возвращает строку вида '👤 Имя:' для подписи транскрипта.
    Приоритет: forward_from → forward_sender_name → from_user.
    Если голосовое пересланное — показываем оригинального автора.
    """
    # Пересланное сообщение с открытым профилем
    if message.forward_from:
        user = message.forward_from
        name = user.first_name or ""
        if user.last_name:
            name += f" {user.last_name}"
        return f"👤 {name}:\n" if name else ""

    # Пересланное сообщение с закрытым профилем (скрыл пересылки)
    if message.forward_sender_name:
        return f"👤 {message.forward_sender_name}:\n"

    # Обычное сообщение — автор тот кто прислал боту
    user = message.from_user
    if not user:
        return ""
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return f"👤 {name}:\n" if name else ""


# ============================================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: сохранение в БД в фоне
# ============================================================================

async def _bg_save_transcript(user_id: int, source_type: str, original_text: str, msg_id: int, message: types.Message):
    """Сохраняет транскрипт в БД в фоне. Не блокирует ответ пользователю."""
    await database.upsert_user(
        user_id,
        username=message.from_user.username if message.from_user else None,
        first_name=message.from_user.first_name if message.from_user else None,
    )
    transcript_id = await database.save_transcript(user_id, source_type, sanitize_for_db(original_text))
    # Сохраняем transcript_id в контекст для последующего сохранения результатов
    if transcript_id and user_id in user_context and msg_id in user_context[user_id]:
        user_context[user_id][msg_id]["transcript_id"] = transcript_id
    logger.debug(f"💾 БД: transcript_id={transcript_id} для user={user_id}")


# ============================================================================
# ХЭНДЛЕРЫ БОТА
# ============================================================================

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    stats["processed_messages"] += 1
    await message.answer(config.START_MESSAGE, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    asyncio.create_task(database.upsert_user(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    ))


@dp.message(Command("help"))
async def help_handler(message: types.Message):
    stats["processed_messages"] += 1
    await message.answer(config.HELP_MESSAGE, parse_mode="HTML")


@dp.message(Command("status"))
async def status_handler(message: types.Message):
    stats["processed_messages"] += 1
    if not access.is_admin(message.from_user.id):
        await message.answer(access.limits_text(message.from_user.id), parse_mode="HTML")
        return
    docx_status = "✅" if processors.DOCX_AVAILABLE else "❌"
    db_status = "✅ Supabase" if database.is_available() else "❌ нет БД"
    temp_files = len([
        f for f in os.listdir(config.TEMP_DIR)
        if f.startswith(('video_', 'audio_', 'text_', 'export_'))
    ]) if os.path.exists(config.TEMP_DIR) else 0

    status_text = config.STATUS_MESSAGE.format(
        groq_count=len(groq_clients),
        users_count=len(user_context),
        vision_status="✅" if groq_clients else "❌",
        docx_status=docx_status,
        db_status=db_status,
        temp_files=temp_files,
    )
    status_text += f"\n🧠 Текстовая LLM: {processors.text_llm_label()}"
    status_text += f"\n🎛 Модель для всех: {config.LLM_PROFILES[access.global_profile_key()]['label']}"
    status_text += f"\n\n💬 Активных диалогов: {len(active_dialogs)}"
    await message.answer(status_text, parse_mode="HTML")


# ============================================================================
# IMG2PROMPT — картинка → промпт для генератора изображений
# ============================================================================

_IMAGE_EXTS = ("jpg", "jpeg", "png", "webp", "bmp", "gif")


def _image_file_id(message: Optional[types.Message]) -> Optional[str]:
    """file_id картинки из сообщения: фото или документ-изображение."""
    if message is None:
        return None
    if message.photo:
        return message.photo[-1].file_id
    doc = message.document
    if doc is not None:
        ext = (doc.file_name or "").lower().rsplit(".", 1)[-1] if "." in (doc.file_name or "") else ""
        if (doc.mime_type or "").startswith("image/") or ext in _IMAGE_EXTS:
            return doc.file_id
    return None


def _wants_img2prompt(message: types.Message) -> bool:
    """Включён режим ожидания или подпись к картинке вида «промпт»."""
    uid = message.from_user.id
    deadline = awaiting_img2prompt.get(uid)
    if deadline:
        if time.time() < deadline:
            awaiting_img2prompt[uid] = time.time() + config.IMG2PROMPT_AWAIT_SEC   # продлеваем
            return True
        awaiting_img2prompt.pop(uid, None)
    caption = message.caption or ""
    return bool(re.match(config.IMG2PROMPT_TRIGGER_RE, caption, re.IGNORECASE))


def _img_cache_put(user_id: int, jpeg: bytes, ratio: str) -> str:
    now = time.time()
    for t in [t for t, e in img_prompt_cache.items() if now - e["ts"] > config.IMG2PROMPT_CACHE_TTL]:
        img_prompt_cache.pop(t, None)
    while len(img_prompt_cache) >= config.IMG2PROMPT_CACHE_MAX:
        oldest = min(img_prompt_cache, key=lambda t: img_prompt_cache[t]["ts"])
        img_prompt_cache.pop(oldest, None)
    token = uuid.uuid4().hex[:8]
    img_prompt_cache[token] = {"user_id": user_id, "jpeg": jpeg, "ratio": ratio, "ts": now, "results": {}}
    return token


async def _get_detail(user_id: int) -> int:
    """Подробность пользователя: память → БД (один раз) → значение по умолчанию."""
    if user_id in img_detail_pref:
        return img_detail_pref[user_id]
    d = config.IMG2PROMPT_DEFAULT_DETAIL
    raw = await database.get_setting(f"img_detail:{user_id}")
    if raw and raw.isdigit() and int(raw) in config.IMG2PROMPT_DETAILS:
        d = int(raw)
    img_detail_pref[user_id] = d
    return d


async def _set_detail(user_id: int, detail: int) -> None:
    if detail in config.IMG2PROMPT_DETAILS:
        img_detail_pref[user_id] = detail
        await database.set_setting(f"img_detail:{user_id}", str(detail))


def _render_img_prompt(raw: str, style: str, detail: int = 0) -> str:
    detail = detail if detail in config.IMG2PROMPT_DETAILS else config.IMG2PROMPT_DEFAULT_DETAIL
    parts = processors.fit_prompt_sections(processors.parse_img_prompt(raw), detail)
    esc = lambda s, n: html.escape((s or "")[:n], quote=False)
    label = config.IMG2PROMPT_STYLES[style][0]
    dlabel = config.IMG2PROMPT_DETAILS[detail][0]
    n_chars = len(parts.get("prompt", ""))
    lines = [f"<b>{html.escape(label)}</b> · {html.escape(dlabel)} · <i>{n_chars} зн.</i>", ""]
    if style == "r":
        lines.append(esc(parts.get("prompt"), 3000))
    else:
        lines.append(f"<code>{esc(parts.get('prompt'), 2600)}</code>")
        short, prompt = parts.get("short", ""), parts.get("prompt", "")
        if short and short != prompt:
            lines += ["", "⚡ <b>Коротко</b>", f"<code>{esc(short, 300)}</code>"]
        if parts.get("negative"):
            lines += ["", "🚫 <b>Negative</b>", f"<code>{esc(parts['negative'], 600)}</code>"]
        if parts.get("ru"):
            lines += ["", f"🇷🇺 <i>{esc(parts['ru'], 400)}</i>"]
    lines += ["", "<i>Нажмите на промпт, чтобы скопировать</i>" if style != "r" else ""]
    return "\n".join(lines).rstrip()


def _img_prompt_kb(token: str, current: str, detail: int) -> InlineKeyboardMarkup:
    # callback_data: ip_<действие>_<стиль>_<подробность>_<токен>; действие: s — стиль, d — подробность, r — заново
    btns = []
    for key, (label, _) in config.IMG2PROMPT_STYLES.items():
        mark = "✅ " if key == current else ""
        btns.append(InlineKeyboardButton(text=mark + label, callback_data=f"ip_s_{key}_{detail}_{token}"))
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([
        InlineKeyboardButton(text=("✅ " if d == detail else "") + spec[0], callback_data=f"ip_d_{current}_{d}_{token}")
        for d, spec in config.IMG2PROMPT_DETAILS.items()
    ])
    rows.append([InlineKeyboardButton(text="🔄 Другой вариант", callback_data=f"ip_r_{current}_{detail}_{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def run_img2prompt(message: types.Message, file_id: str, user_id: int, style: str = "u"):
    if is_shutting_down:
        await message.answer("🛑 Бот останавливается, попробуйте позже.")
        return
    if not processors.has_text_llm(groq_clients):
        await message.answer("❌ Нет доступных LLM-клиентов")
        return
    if user_id in processing_users:
        await message.answer(config.ERROR_BUSY)
        return

    processing_users.add(user_id)
    status = await message.answer("🎨 Разглядываю картинку...")
    try:
        file_info = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, buf)
        jpeg, ratio = await asyncio.to_thread(processors.prepare_image_for_vision, buf.getvalue())

        detail = await _get_detail(user_id)
        raw = await processors.image_to_prompt(jpeg, ratio, style, groq_clients, detail=detail)
        if raw.startswith("❌"):
            await status.edit_text(raw)
            return
        token = _img_cache_put(user_id, jpeg, ratio)
        img_prompt_cache[token]["results"][f"{style}{detail}"] = raw
        await status.edit_text(_render_img_prompt(raw, style, detail), parse_mode="HTML",
                               reply_markup=_img_prompt_kb(token, style, detail))
    except ValueError as e:
        await status.edit_text(f"❌ {e}")
    except Exception as e:
        logger.error(f"img2prompt error: {e}", exc_info=True)
        if not is_shutting_down:
            await status.edit_text("❌ Не удалось обработать картинку. Попробуйте ещё раз.")
    finally:
        processing_users.discard(user_id)


@dp.message(Command("img2prompt", "prompt"))
async def img2prompt_handler(message: types.Message):
    stats["processed_messages"] += 1
    uid = message.from_user.id
    # команда в подписи к картинке или ответом на картинку — обрабатываем сразу
    file_id = _image_file_id(message) or _image_file_id(message.reply_to_message)
    if file_id:
        await run_img2prompt(message, file_id, uid)
        return
    awaiting_img2prompt[uid] = time.time() + config.IMG2PROMPT_AWAIT_SEC
    await message.answer(config.IMG2PROMPT_HINT, parse_mode="HTML")


@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message):
    stats["processed_messages"] += 1
    uid = message.from_user.id
    did = False
    if awaiting_img2prompt.pop(uid, None):
        await message.answer("✅ Режим «картинка → промпт» выключен. Фото снова читаются как текст.")
        did = True
    if pending_speaker_names.pop(uid, None):
        await message.answer("✅ Ввод имён собеседников отменён.")
        did = True
    if not did:
        await message.answer("Отменять нечего.")


@dp.callback_query(F.data.startswith("ip_"))
async def img2prompt_callback(callback: types.CallbackQuery):
    if is_shutting_down:
        await callback.answer("🛑 Бот останавливается", show_alert=True)
        return
    parts = (callback.data or "").split("_")
    if (len(parts) != 5 or parts[1] not in ("s", "d", "r") or parts[2] not in config.IMG2PROMPT_STYLES
            or not parts[3].isdigit() or int(parts[3]) not in config.IMG2PROMPT_DETAILS):
        await callback.answer()
        return
    _, action, style, detail_s, token = parts
    detail = int(detail_s)
    uid = callback.from_user.id
    entry = img_prompt_cache.get(token)
    if not entry or entry["user_id"] != uid:
        await callback.answer("Картинка устарела — пришлите её заново", show_alert=True)
        return
    entry["ts"] = time.time()
    if action == "d":
        await _set_detail(uid, detail)      # запоминаем как предпочтение для новых картинок

    regen = action == "r"
    key = f"{style}{detail}"
    cached = None if regen else entry["results"].get(key)
    if cached:      # уже сгенерированный вариант — бесплатно
        await callback.answer()
        await callback.message.edit_text(_render_img_prompt(cached, style, detail), parse_mode="HTML",
                                         reply_markup=_img_prompt_kb(token, style, detail))
        return

    if uid in processing_users:
        await callback.answer("Подождите, идёт обработка", show_alert=True)
        return
    processing_users.add(uid)
    await callback.answer("🎨 Пишу промпт...")
    try:
        raw = await processors.image_to_prompt(entry["jpeg"], entry["ratio"], style, groq_clients,
                                               regenerate=regen, detail=detail)
        if raw.startswith("❌"):
            # ошибка или лимит: ничего не кэшируем, прежний результат остаётся, ошибка — отдельным сообщением
            await callback.message.answer(raw)
            return
        entry["results"][key] = raw
        await callback.message.edit_text(_render_img_prompt(raw, style, detail), parse_mode="HTML",
                                         reply_markup=_img_prompt_kb(token, style, detail))
    except Exception as e:
        logger.error(f"img2prompt callback error: {e}", exc_info=True)
        if not is_shutting_down:
            await callback.message.answer("❌ Не удалось создать промпт. Попробуйте ещё раз.")
    finally:
        processing_users.discard(uid)


@dp.message(Command("limit"))
async def limit_handler(message: types.Message):
    stats["processed_messages"] += 1
    if message.chat.type in GROUP_TYPES:      # в группе показываем лимит чата
        await access.ensure_loaded(message.chat.id)
        await message.answer(access.group_limits_text(message.chat.id), parse_mode="HTML")
        return
    await message.answer(access.limits_text(message.from_user.id), parse_mode="HTML")


# ============================================================================
# ВЫБОР МОДЕЛИ (только администратор)
# ============================================================================

def _model_menu_text(uid: int) -> str:
    profiles = config.LLM_PROFILES
    personal = access.personal_profile_key(uid)
    mine_key = access.profile_key_for(uid)
    mine = profiles[mine_key]["label"] + (" (личный выбор)" if personal else " (общая)")
    return (
        "🧠 <b>Модель текстовой обработки</b>\n"
        "Коррекция, «Красиво», саммари, перевод, разбор правок, вопросы по документу, "
        "субтитры. Whisper и OCR остаются на Groq.\n\n"
        f"Сейчас у вас: <b>{mine}</b>\n"
        f"Для всех пользователей: <b>{profiles[access.global_profile_key()]['label']}</b>\n\n"
        "<i>Конкретная модель работает строго: если она недоступна, вы увидите "
        "ошибку, а не тихую подмену. «Авто» сама переключается между моделями и Groq.</i>"
    )


def _model_menu_kb(uid: int) -> InlineKeyboardMarkup:
    current = access.profile_key_for(uid)
    personal = access.personal_profile_key(uid)
    rows, row = [], []
    for key, prof in config.LLM_PROFILES.items():
        mark = "✅ " if key == current else ""
        row.append(InlineKeyboardButton(text=mark + prof["label"], callback_data=f"llm_set_{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if personal and personal != access.global_profile_key():
        rows.append([InlineKeyboardButton(text="📌 Сделать общей для всех", callback_data="llm_global")])
    if personal:
        rows.append([InlineKeyboardButton(text="↩️ Сбросить личный выбор", callback_data="llm_reset")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("model"))
async def model_handler(message: types.Message):
    stats["processed_messages"] += 1
    uid = message.from_user.id
    if not access.is_admin(uid):
        await message.answer("🧠 Модель подбирается автоматически. Выбирать её может только администратор.")
        return
    await message.answer(_model_menu_text(uid), parse_mode="HTML", reply_markup=_model_menu_kb(uid))


@dp.callback_query(F.data.startswith("llm_"))
async def model_callback(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not access.is_admin(uid):
        await callback.answer("Только для администратора", show_alert=True)
        return

    data = callback.data
    note = ""
    if data.startswith("llm_set_"):
        key = data[len("llm_set_"):]
        if key in config.LLM_PROFILES:
            await access.set_personal_profile(uid, key)
            note = f"Выбрано: {config.LLM_PROFILES[key]['label']}"
    elif data == "llm_global":
        key = access.profile_key_for(uid)
        await access.set_global_profile(key)
        note = f"Для всех: {config.LLM_PROFILES[key]['label']}"
    elif data == "llm_reset":
        await access.reset_personal_profile(uid)
        note = "Личный выбор сброшен"
    await callback.answer(note)

    try:
        await callback.message.edit_text(_model_menu_text(uid), parse_mode="HTML", reply_markup=_model_menu_kb(uid))
    except Exception as e:
        logger.debug(f"model menu edit skipped: {e}")


# ============================================================================
# АДМИН-ПАНЕЛЬ
# ============================================================================

@dp.message(Command("admin"))
async def admin_handler(message: types.Message):
    stats["processed_messages"] += 1
    if not access.is_admin(message.from_user.id):
        return
    day, active, total, top = await access.usage_snapshot()
    lines = [
        "👑 <b>Админ-панель</b>",
        f"📅 {day} ({config.LIMITS_TZ_LABEL})",
        f"👥 Активных пользователей сегодня: <b>{active}</b>",
        f"🧮 Потрачено единиц: <b>{total}</b>",
    ]
    if top:
        lines.append("\n🏆 <b>Больше всего за сегодня:</b>")
        for uid, name, n in top:
            lines.append(f"• {html.escape(name)} (<code>{uid}</code>) — {n}")
    lines += [
        "\n⚙️ <b>Лимиты для пользователей:</b>",
        f"• {config.USER_DAILY_LIMIT} единиц в сутки",
        f"• файл ≤ {config.USER_MAX_FILE_MB} МБ, голос ≤ {config.USER_MAX_AUDIO_SEC} с, "
        f"текст ≤ {config.USER_MAX_TEXT_CHARS} симв., пауза {config.USER_COOLDOWN_SEC} с",
        f"\n🎛 Модель для всех: <b>{config.LLM_PROFILES[access.global_profile_key()]['label']}</b>",
        f"🗄 БД: {'✅ Supabase (счётчики сохраняются)' if database.is_available() else '❌ нет (счётчики сбросятся при рестарте)'}",
        "\n<b>Команды:</b>",
        "/model — выбор модели",
        "/setlimit <code>ID число</code> — индивидуальный лимит (0 — заблокировать)",
        "/setlimit <code>ID reset</code> — вернуть общий лимит",
        "/status — техническое состояние",
    ]
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("setlimit"))
async def setlimit_handler(message: types.Message):
    stats["processed_messages"] += 1
    if not access.is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    usage = "Использование: <code>/setlimit ID число</code> или <code>/setlimit ID reset</code>"
    if len(parts) != 3 or not parts[1].lstrip("-").isdigit():
        await message.answer(usage, parse_mode="HTML")
        return
    target = int(parts[1])
    if access.is_admin(target):
        await message.answer("👑 У администратора лимитов нет.")
        return
    value = parts[2].lower()
    if value in ("reset", "default", "сброс"):
        await access.set_user_limit(target, None)
        await message.answer(f"✅ Для <code>{target}</code> действует общий лимит: {config.USER_DAILY_LIMIT}/сутки.",
                             parse_mode="HTML")
    elif value.isdigit():
        await access.set_user_limit(target, int(value))
        extra = " — пользователь заблокирован" if int(value) == 0 else ""
        await message.answer(f"✅ Лимит для <code>{target}</code>: {int(value)}/сутки{extra}.", parse_mode="HTML")
    else:
        await message.answer(usage, parse_mode="HTML")


@dp.message(Command("history"))
async def history_handler(message: types.Message):
    stats["processed_messages"] += 1
    user_id = message.from_user.id

    if not database.is_available():
        await message.answer("📜 История недоступна — база данных не подключена.")
        return

    records = await database.get_user_history(user_id, limit=10)
    if not records:
        await message.answer("📜 История пуста — ещё нет обработанных текстов.")
        return

    source_emoji = {
        "voice": "🎙️", "audio": "🎵", "video_note": "🎥",
        "file": "📄", "text": "📝"
    }
    lines = [f"📊 Обработано сообщений: {stats['processed_messages']}\n\n📜 <b>Последние 10 обработок:</b>\n"]
    for i, rec in enumerate(records, 1):
        emoji = source_emoji.get(rec.get("source_type", ""), "📌")
        preview = (rec.get("original_text") or "")[:80].replace("\n", " ")
        if len(rec.get("original_text", "")) > 80:
            preview += "..."
        dt = rec.get("created_at", "")[:16].replace("T", " ") if rec.get("created_at") else ""
        lines.append(f"{i}. {emoji} <i>{preview}</i>\n   <code>{dt}</code>")

    await message.answer("\n\n".join(lines), parse_mode="HTML")


@dp.message(Command("exit"))
async def exit_dialog_handler(message: types.Message):
    stats["processed_messages"] += 1
    user_id = message.from_user.id
    if user_id in active_dialogs:
        del active_dialogs[user_id]
        await message.answer("✅ Вы вышли из режима вопросов.")
    else:
        await message.answer("❌ Вы не находитесь в режиме вопросов.")


# ============================================================================
# ГОЛОСОВЫЕ И КРУЖОЧКИ
# ============================================================================

# ============================================================================
# ГРУППЫ
# ============================================================================
# Бот в группе: авто-расшифровка голосовых и кружков (включается /autovoice on),
# команды ответом на сообщение: /fix /summary /protocol /dialogue.
# Расход списывается с лимита ЧАТА (config.GROUP_DAILY_LIMIT); сообщения
# администратора бота бесплатны. Чтобы бот видел все голосовые, в @BotFather
# отключите Group Privacy (/setprivacy → Disable) или сделайте бота админом группы.
# Эти хендлеры стоят выше личных, поэтому личная логика в группах не срабатывает.

_GROUP_COMMAND_MODES = {"fix": "premium", "summary": "summary", "protocol": "protocol", "dialogue": "dialogue"}
_GROUP_ALLOWED_MODES = {"basic", "premium", "summary", "protocol", "dialogue"}


async def _group_auto_enabled(chat_id: int) -> bool:
    if chat_id not in group_auto:
        group_auto[chat_id] = (await database.get_setting(f"group_auto:{chat_id}")) == "1"
    return group_auto[chat_id]


async def _can_manage_group(message: types.Message) -> bool:
    if message.sender_chat and message.sender_chat.id == message.chat.id:   # анонимный админ
        return True
    uid = message.from_user.id if message.from_user else 0
    if access.is_admin(uid):
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, uid)
        return member.status in ("creator", "administrator")
    except Exception as e:
        logger.debug(f"get_chat_member failed: {e}")
        return False


async def _group_billing(message: types.Message) -> int:
    """Кого списывать: администратор бота — бесплатно (по его id), иначе лимит чата."""
    chat = message.chat
    access.remember_name(chat.id, f"👥 {chat.title or chat.id}")
    sender = message.from_user.id if message.from_user else 0
    if access.is_admin(sender):
        billing = sender
    else:
        await access.ensure_loaded(chat.id)
        billing = chat.id
    access.current_user_id.set(billing)
    return billing


async def _group_quota_ok(message: types.Message, billing: int, notify: bool = True) -> bool:
    if access.is_admin(billing) or (access.remaining(billing) or 0) > 0:
        return True
    day = access.today_str()
    if notify and group_notified.get(message.chat.id) != day:      # не чаще раза в сутки
        group_notified[message.chat.id] = day
        try:
            await message.reply(access.limit_exhausted_message(billing), parse_mode="HTML")
        except Exception as e:
            logger.debug(f"group notice failed: {e}")
    return False


def _group_cache_put(chat_id: int, text: str) -> str:
    now = time.time()
    for t in [t for t, e in group_cache.items() if now - e["ts"] > GROUP_CACHE_TTL]:
        group_cache.pop(t, None)
    while len(group_cache) >= GROUP_CACHE_MAX:
        group_cache.pop(min(group_cache, key=lambda t: group_cache[t]["ts"]), None)
    token = uuid.uuid4().hex[:8]
    group_cache[token] = {"chat_id": chat_id, "text": text, "ts": now, "results": {}, "busy": set()}
    return token


def _group_kb(token: str, text: str) -> Optional[InlineKeyboardMarkup]:
    modes = [m for m in ("premium", "summary", "protocol", "dialogue") if m in processors.get_available_modes(text)]
    btns = [InlineKeyboardButton(text=config.MODE_LABELS[m], callback_data=f"gr_{m}_{token}") for m in modes]
    if not btns:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)])


async def _download_media(file_id: str) -> bytes:
    info = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(info.file_path, buf)
    return buf.getvalue()


def _media_of(message: Optional[types.Message]):
    """(объект медиа, вид) для голосового, кружка, видео, аудио и аудио/видео-документов."""
    if message is None:
        return None, ""
    if message.voice:
        return message.voice, "audio"
    if message.video_note:
        return message.video_note, "video"
    if message.video:
        return message.video, "video"
    if message.audio:
        return message.audio, "audio"
    doc = message.document
    if doc and (doc.mime_type or "").startswith("audio/"):
        return doc, "audio"
    if doc and (doc.mime_type or "").startswith("video/"):
        return doc, "video"
    return None, ""


async def _transcribe_group_media(message: types.Message, billing: int) -> str:
    media, kind = _media_of(message)
    if media is None:
        return ""
    if not access.is_admin(billing):
        if (getattr(media, "duration", 0) or 0) > config.USER_MAX_AUDIO_SEC:
            return f"❌ Запись длиннее {config.USER_MAX_AUDIO_SEC // 60} мин. Длинные записи — в личку боту."
        if (getattr(media, "file_size", 0) or 0) > config.USER_MAX_FILE_MB * 1024 * 1024:
            return f"❌ Файл больше {config.USER_MAX_FILE_MB} МБ. Длинные записи — в личку боту."
    try:
        data = await _download_media(media.file_id)
    except Exception as e:
        logger.warning(f"group download failed: {e}")
        return "❌ Не удалось скачать файл (Telegram отдаёт ботам файлы до 20 МБ)."
    if kind == "video":
        return await processors.process_video_file(data, "video.mp4", groq_clients)
    return await processors.transcribe_voice(data, groq_clients)


@dp.message(Command("autovoice"))
async def autovoice_handler(message: types.Message, command: CommandObject):
    stats["processed_messages"] += 1
    if message.chat.type not in GROUP_TYPES:
        await message.answer("🎙 Это команда для групп: добавьте бота в чат и напишите там /autovoice on.")
        return
    arg = (command.args or "").strip().lower()
    chat_id = message.chat.id
    if arg in ("on", "off", "вкл", "выкл"):
        if not await _can_manage_group(message):
            await message.reply("Включать и выключать могут только администраторы чата.")
            return
        on = arg in ("on", "вкл")
        group_auto[chat_id] = on
        await database.set_setting(f"group_auto:{chat_id}", "1" if on else "0")
        await message.reply("🎙 Авто-расшифровка голосовых и кружков <b>включена</b>." if on
                            else "🔇 Авто-расшифровка <b>выключена</b>.", parse_mode="HTML")
        return
    state = "включена" if await _group_auto_enabled(chat_id) else "выключена"
    await message.reply(
        f"🎙 Авто-расшифровка голосовых: <b>{state}</b>.\n"
        "Включить/выключить: <code>/autovoice on</code> / <code>/autovoice off</code> (админы чата).\n\n"
        "Ответом на любое сообщение: /fix — исправить, /summary — саммари, /protocol — протокол, "
        "/dialogue — диалог по ролям. Работает и с голосовыми, и с аудиофайлами.\n"
        "Лимит чата: /limit",
        parse_mode="HTML",
    )


@dp.message(Command("fix", "summary", "protocol", "dialogue"), F.chat.type.in_(GROUP_TYPES))
async def group_command_handler(message: types.Message, command: CommandObject):
    stats["processed_messages"] += 1
    if is_shutting_down:
        return
    mode = _GROUP_COMMAND_MODES[command.command]
    target = message.reply_to_message
    if target is None:
        await message.reply(f"Ответьте командой /{command.command} на сообщение с текстом, голосовым или аудиофайлом.")
        return
    billing = await _group_billing(message)
    if not await _group_quota_ok(message, billing):
        return

    status = await message.reply("⏳ Обрабатываю…")
    try:
        text = (target.text or target.caption or "").strip()
        if not text:
            media, _ = _media_of(target)
            if media is None:
                await status.edit_text("Не вижу текста или записи в этом сообщении.")
                return
            await status.edit_text("🎙 Расшифровываю…")
            text = (await _transcribe_group_media(target, billing)).strip()
            if text.startswith("❌"):
                await status.edit_text(text)
                return
        if mode not in processors.get_available_modes(text):
            await status.edit_text("Текст слишком короткий для этого режима. Для /fix подойдёт любой.")
            return
        await status.edit_text(f"⏳ {config.MODE_LABELS.get(mode, mode)}…")
        result = await run_mode(mode, text)
        if result.startswith("❌") or result.startswith("📝"):
            await status.edit_text(result)
            return
        chunks = _split_for_telegram(sanitize_llm_output(result))
        await status.edit_text(chunks[0], parse_mode="HTML")
        for extra in chunks[1:]:
            await message.answer(extra, parse_mode="HTML")
    except Exception as e:
        logger.error(f"group command error: {e}", exc_info=True)
        try:
            await status.edit_text("❌ Не удалось обработать сообщение.")
        except Exception:
            pass


@dp.message(F.voice | F.video_note, F.chat.type.in_(GROUP_TYPES))
async def group_voice_handler(message: types.Message):
    """Авто-расшифровка голосовых и кружков (только если включено /autovoice on)."""
    if is_shutting_down or not await _group_auto_enabled(message.chat.id):
        return
    media, _ = _media_of(message)
    if media is None:
        return
    # тихо пропускаем слишком длинное/большое: в группах не шумим
    if (media.duration or 0) > config.USER_MAX_AUDIO_SEC or \
            (media.file_size or 0) > config.USER_MAX_FILE_MB * 1024 * 1024:
        return
    billing = await _group_billing(message)
    if not await _group_quota_ok(message, billing):
        return
    text = (await _transcribe_group_media(message, billing)).strip()
    if not text:
        return
    if text.startswith("❌"):
        await message.reply(text, parse_mode="HTML")
        return
    author = html.escape(message.from_user.full_name if message.from_user else "Участник")
    token = _group_cache_put(message.chat.id, text)
    shown = text if len(text) <= 3500 else text[:3500].rstrip() + "…"
    await message.reply(f"🎙 <b>{author}:</b>\n{html.escape(shown, quote=False)}", parse_mode="HTML",
                        reply_markup=_group_kb(token, text))


@dp.callback_query(F.data.startswith("gr_"))
async def group_callback(callback: types.CallbackQuery):
    parts = (callback.data or "").split("_", 2)
    if len(parts) != 3 or parts[1] not in _GROUP_ALLOWED_MODES or callback.message is None:
        await callback.answer()
        return
    _, mode, token = parts
    entry = group_cache.get(token)
    if not entry or entry["chat_id"] != callback.message.chat.id:
        await callback.answer("Запись устарела. Отправьте голосовое заново.", show_alert=True)
        return
    entry["ts"] = time.time()

    cached = entry["results"].get(mode)
    if cached:      # уже готовый режим — повторяем бесплатно
        await callback.answer()
        for chunk in _split_for_telegram(cached):
            await callback.message.reply(chunk, parse_mode="HTML")
        return
    if mode in entry["busy"]:
        await callback.answer("Уже обрабатывается…")
        return

    # списываем лимит чата (админ бота — бесплатно)
    chat = callback.message.chat
    access.remember_name(chat.id, f"👥 {chat.title or chat.id}")
    if access.is_admin(callback.from_user.id):
        access.current_user_id.set(callback.from_user.id)
    else:
        await access.ensure_loaded(chat.id)
        access.current_user_id.set(chat.id)
        if (access.remaining(chat.id) or 0) <= 0:
            await callback.answer(f"Лимит чата на сегодня исчерпан ({access.used_today(chat.id)}/{access.user_limit(chat.id)}).",
                                  show_alert=True)
            return

    entry["busy"].add(mode)
    await callback.answer("⏳ Обрабатываю…")
    try:
        result = await run_mode(mode, entry["text"])
        if result.startswith("❌") or result.startswith("📝"):
            await callback.message.reply(result)
            return
        rendered = sanitize_llm_output(result)
        entry["results"][mode] = rendered
        for chunk in _split_for_telegram(rendered):
            await callback.message.reply(chunk, parse_mode="HTML")
    except Exception as e:
        logger.error(f"group callback error: {e}", exc_info=True)
        await callback.message.reply("❌ Не удалось обработать запись.")
    finally:
        entry["busy"].discard(mode)


@dp.message(F.chat.type.in_(GROUP_TYPES))
async def group_sink(message: types.Message):
    """Всё остальное в группах игнорируем: личные хендлеры там не должны срабатывать."""
    return


@dp.message(F.voice)
async def voice_handler(message: types.Message):
    if is_shutting_down:
        await message.answer("🛑 Бот останавливается, попробуйте позже.")
        return

    user_id = message.from_user.id

    if user_id in active_dialogs:
        await message.answer("⏳ Голосовые вопросы пока не поддерживаются. Напишите текст.")
        return

    if user_id in processing_users:
        await message.answer(config.ERROR_BUSY)
        return

    processing_users.add(user_id)
    msg = await message.answer(config.MSG_PROCESSING_VOICE)

    try:
        file_info = await bot.get_file(message.voice.file_id)
        voice_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, voice_buffer)

        original_text = await processors.transcribe_voice(voice_buffer.getvalue(), groq_clients)

        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return

        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, mode="basic", available_modes=available_modes)

        if user_id in user_context and msg.message_id in user_context[user_id]:
            user_context[user_id][msg.message_id]["type"] = "voice"
            user_context[user_id][msg.message_id]["chat_id"] = message.chat.id

        # Сохраняем в БД в фоне
        asyncio.create_task(_bg_save_transcript(user_id, "voice", original_text, msg.message_id, message))

        author = get_author_label(message)
        preview = original_text[:config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."
        preview = sanitize_llm_output(preview)

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        await msg.edit_text(
            f"{author}✅ <b>Распознанный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id)
        )
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"message.delete() failed: {e}")

    except Exception as e:
        logger.error(f"Voice handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки голосового сообщения")
    finally:
        processing_users.discard(user_id)


@dp.message(F.video_note)
async def video_note_handler(message: types.Message):
    if is_shutting_down:
        await message.answer("🛑 Бот останавливается, попробуйте позже.")
        return

    user_id = message.from_user.id

    if user_id in active_dialogs:
        await message.answer("⏳ Голосовые вопросы пока не поддерживаются. Напишите текст.")
        return

    if user_id in processing_users:
        await message.answer(config.ERROR_BUSY)
        return

    processing_users.add(user_id)
    msg = await message.answer("🎥 Обрабатываю кружочек...")

    try:
        file_info = await bot.get_file(message.video_note.file_id)
        buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, buffer)

        original_text = await processors.process_video_file(buffer.getvalue(), "video_note.mp4", groq_clients, with_timecodes=False)

        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return

        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, mode="basic", available_modes=available_modes)

        if user_id in user_context and msg.message_id in user_context[user_id]:
            user_context[user_id][msg.message_id]["type"] = "video_note"
            user_context[user_id][msg.message_id]["chat_id"] = message.chat.id

        asyncio.create_task(_bg_save_transcript(user_id, "video_note", original_text, msg.message_id, message))

        author = get_author_label(message)
        preview = original_text[:config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        await msg.edit_text(
            f"{author}✅ <b>Распознанный текст из кружочка:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id)
        )
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"message.delete() failed: {e}")

    except Exception as e:
        logger.error(f"Video note handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки кружочка")
    finally:
        processing_users.discard(user_id)


@dp.message(F.audio)
async def audio_handler(message: types.Message):
    if is_shutting_down:
        await message.answer("🛑 Бот останавливается, попробуйте позже.")
        return

    user_id = message.from_user.id
    active_dialogs.pop(user_id, None)

    if user_id in processing_users:
        await message.answer(config.ERROR_BUSY)
        return

    processing_users.add(user_id)
    msg = await message.answer(config.MSG_TRANSCRIBING)

    try:
        file_info = await bot.get_file(message.audio.file_id)
        audio_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, audio_buffer)

        original_text = await processors.transcribe_voice(audio_buffer.getvalue(), groq_clients)

        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return

        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, mode="basic", available_modes=available_modes)

        if user_id in user_context and msg.message_id in user_context[user_id]:
            user_context[user_id][msg.message_id]["type"] = "audio"
            user_context[user_id][msg.message_id]["chat_id"] = message.chat.id

        asyncio.create_task(_bg_save_transcript(user_id, "audio", original_text, msg.message_id, message))

        author = get_author_label(message)
        preview = original_text[:config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        await msg.edit_text(
            f"{author}✅ <b>Распознанный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id)
        )
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"message.delete() failed: {e}")

    except Exception as e:
        logger.error(f"Audio handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки аудиофайла")
    finally:
        processing_users.discard(user_id)


@dp.message(F.text.regexp(r'https?://(www\.)?(youtube\.com|youtu\.be)/\S+'))
async def youtube_handler(message: types.Message):
    """Обработка YouTube-ссылок: субтитры → диалог + саммари."""
    if is_shutting_down:
        await message.answer("🛑 Бот останавливается, попробуйте позже.")
        return

    user_id = message.from_user.id

    if user_id in active_dialogs:
        await message.answer("⏳ Сначала выйдите из режима вопросов: /exit")
        return

    if user_id in processing_users:
        await message.answer(config.ERROR_BUSY)
        return

    url = message.text.strip()
    video_id = processors.extract_youtube_video_id(url)
    if not video_id:
        await message.answer("❌ Не удалось распознать YouTube-ссылку")
        return

    processing_users.add(user_id)
    msg = await message.answer(config.MSG_FETCHING_SUBTITLES)

    try:
        result = await processors.fetch_youtube_subtitles(video_id)

        if result["error"]:
            await msg.edit_text(result["error"])
            return

        segments = result["raw"]
        lang = result["lang"]
        raw_text = processors._segments_to_plain_text(segments)
        timecoded_text = processors._segments_to_timecoded(segments)

        await msg.edit_text(config.MSG_FORMATTING_SUBTITLES)

        # Форматируем в диалог через LLM (убираем рекламу, группируем)
        dialogue_text = await processors.format_subtitles_as_dialogue(raw_text, groq_clients)

        # Делаем саммари параллельно — уже есть готовый dialogue_text
        await msg.edit_text("📊 Делаю саммари...")
        summary = await processors.summarize_text(dialogue_text, groq_clients)
        if summary.startswith("❌"):
            summary = dialogue_text[:500] + "..."

        # Сохраняем оба варианта в контекст
        available_modes = ["basic", "premium", "summary"]
        save_to_history(user_id, msg.message_id, dialogue_text, mode="summary", available_modes=available_modes)

        if user_id in user_context and msg.message_id in user_context[user_id]:
            ctx = user_context[user_id][msg.message_id]
            ctx["type"] = "youtube"
            ctx["chat_id"] = message.chat.id
            ctx["original"] = dialogue_text
            ctx["timecoded"] = timecoded_text   # сырой с таймкодами, для экспорта
            ctx["cached_results"]["summary"] = summary
            ctx["yt_lang"] = lang
            ctx["yt_url"] = url
            schedule_persist(user_id, msg.message_id)

        asyncio.create_task(_bg_save_transcript(user_id, "youtube", dialogue_text, msg.message_id, message))

        lang_flag = "🇷🇺" if lang == "ru" else "🌐"
        display = summary if len(summary) <= 4000 else summary[:3997] + "..."

        await msg.edit_text(
            f"📺 <b>YouTube</b> {lang_flag}\n"
            f"<a href='{url}'>youtu.be/{video_id}</a>\n\n"
            f"{display}",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=create_switch_keyboard(user_id, msg.message_id)
        )
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"message.delete() failed: {e}")

    except Exception as e:
        logger.error(f"YouTube handler error: {e}")
        await msg.edit_text(f"❌ Ошибка обработки YouTube: {str(e)[:100]}")
    finally:
        processing_users.discard(user_id)


@dp.message(F.text.regexp(r'https?://\S+'))
async def url_handler(message: types.Message):
    """Обработка ссылок: скрейпим страницу и сразу показываем саммари."""
    url = message.text.strip()

    # Пропускаем API-эндпоинты и служебные URL — обрабатываем как обычный текст
    if not processors.is_url(url):
        await text_handler(message)
        return

    if is_shutting_down:
        await message.answer("🛑 Бот останавливается, попробуйте позже.")
        return

    user_id = message.from_user.id

    if user_id in active_dialogs:
        await message.answer("⏳ Сначала выйдите из режима вопросов: /exit")
        return

    if user_id in processing_users:
        await message.answer(config.ERROR_BUSY)
        return

    processing_users.add(user_id)
    msg = await message.answer(config.MSG_FETCHING_URL)

    try:
        page_text = await processors.fetch_url_text(url)

        if page_text.startswith("❌"):
            await msg.edit_text(page_text)
            return

        await msg.edit_text("📊 Делаю саммари страницы...")
        summary = await processors.summarize_text(page_text, groq_clients)

        # Если текст слишком короткий для саммари — показываем как есть
        if summary.startswith("❌"):
            summary = page_text

        available_modes = processors.get_available_modes(page_text)
        if "summary" not in available_modes:
            available_modes.append("summary")

        save_to_history(user_id, msg.message_id, page_text, mode="summary", available_modes=available_modes)

        if user_id in user_context and msg.message_id in user_context[user_id]:
            user_context[user_id][msg.message_id]["type"] = "url"
            user_context[user_id][msg.message_id]["chat_id"] = message.chat.id
            user_context[user_id][msg.message_id]["original"] = page_text
            user_context[user_id][msg.message_id]["cached_results"]["summary"] = summary
            schedule_persist(user_id, msg.message_id)

        asyncio.create_task(_bg_save_transcript(user_id, "url", page_text, msg.message_id, message))

        domain = url.split("/")[2] if len(url.split("/")) > 2 else url
        display = summary if len(summary) <= 4000 else summary[:3997] + "..."

        await msg.edit_text(
            f"🌐 <b>{domain}</b>\n\n{display}",
            parse_mode="HTML",
            reply_markup=create_switch_keyboard(user_id, msg.message_id)
        )
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"message.delete() failed: {e}")

    except Exception as e:
        logger.error(f"URL handler error: {e}")
        await msg.edit_text(f"❌ Ошибка обработки ссылки: {str(e)[:100]}")
    finally:
        processing_users.discard(user_id)


@dp.message(F.text)
async def text_handler(message: types.Message):
    if is_shutting_down:
        await message.answer("🛑 Бот останавливается, попробуйте позже.")
        return

    user_id = message.from_user.id
    original_text = message.text.strip()

    # Перехват: пользователь вводит имя файла для экспорта
    if user_id in pending_filename_inputs:
        await _handle_filename_input(message)
        return

    # Перехват: пользователь называет собеседников (режим «Диалог»)
    pend = pending_speaker_names.get(user_id)
    if pend:
        if time.time() - pend["ts"] < SPEAKER_NAMES_TIMEOUT:
            await _handle_speaker_names(message, pend)
            return
        pending_speaker_names.pop(user_id, None)

    # Диалоговый режим
    if user_id in active_dialogs:
        msg_id = active_dialogs[user_id]
        await handle_streaming_answer(message, user_id, msg_id, message.text)
        return

    if original_text.startswith("/"):
        return

    if user_id in processing_users:
        await message.answer(config.ERROR_BUSY)
        return

    processing_users.add(user_id)
    msg = await message.answer("📝 Анализирую текст...")

    try:
        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, mode="basic", available_modes=available_modes)

        if user_id in user_context and msg.message_id in user_context[user_id]:
            user_context[user_id][msg.message_id]["type"] = "text"
            user_context[user_id][msg.message_id]["chat_id"] = message.chat.id
            user_context[user_id][msg.message_id]["original"] = original_text

        asyncio.create_task(_bg_save_transcript(user_id, "text", original_text, msg.message_id, message))

        preview = original_text[:config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        await msg.edit_text(
            f"📝 <b>Полученный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id)
        )
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"message.delete() failed: {e}")

    except Exception as e:
        logger.error(f"Text handler error: {e}")
        await msg.edit_text("❌ Ошибка обработки текста")
    finally:
        processing_users.discard(user_id)


@dp.message(F.photo | F.document)
async def file_handler(message: types.Message):
    if is_shutting_down:
        await message.answer("🛑 Бот останавливается, попробуйте позже.")
        return

    user_id = message.from_user.id

    # Картинка → промпт: режим ожидания (/img2prompt) или подпись «промпт» к фото
    img_id = _image_file_id(message)
    if img_id and _wants_img2prompt(message):
        await run_img2prompt(message, img_id, user_id)
        return

    active_dialogs.pop(user_id, None)

    if user_id in processing_users:
        await message.answer(config.ERROR_BUSY)
        return

    processing_users.add(user_id)
    msg = await message.answer("📁 Обрабатываю файл...")

    try:
        file_info = None
        filename = ""

        if message.photo:
            file_info = await bot.get_file(message.photo[-1].file_id)
            filename = f"photo_{file_info.file_unique_id}.jpg"
        elif message.document:
            file_info = await bot.get_file(message.document.file_id)
            filename = message.document.file_name or f"file_{file_info.file_unique_id}"

        file_buffer = io.BytesIO()
        await bot.download_file(file_info.file_path, file_buffer)
        file_bytes = file_buffer.getvalue()

        if len(file_bytes) > config.FILE_SIZE_LIMIT:
            await msg.edit_text(config.ERROR_FILE_TOO_LARGE)
            return

        file_ext = filename.lower().split('.')[-1] if '.' in filename else ''

        # Прогресс-сообщение для PDF
        if file_ext == 'pdf':
            await msg.edit_text(config.MSG_PROCESSING_PDF)
        else:
            await msg.edit_text("🔍 Извлекаю текст...")

        original_text = await processors.extract_text_from_file(file_bytes, filename, groq_clients)

        if original_text.startswith("❌"):
            await msg.edit_text(original_text)
            return

        if not original_text.strip() or len(original_text.strip()) < config.MIN_TEXT_LENGTH:
            await msg.edit_text(config.ERROR_NO_TEXT_IN_FILE)
            return

        available_modes = processors.get_available_modes(original_text)
        save_to_history(user_id, msg.message_id, original_text, mode="basic", available_modes=available_modes)

        if user_id in user_context and msg.message_id in user_context[user_id]:
            user_context[user_id][msg.message_id]["type"] = "file"
            user_context[user_id][msg.message_id]["chat_id"] = message.chat.id
            user_context[user_id][msg.message_id]["filename"] = filename
            user_context[user_id][msg.message_id]["original"] = original_text

        source_type = "file"
        if file_ext == "pdf":
            source_type = "pdf"
        elif file_ext in ("md", "markdown", "mdown", "mkd"):
            source_type = "markdown"
        elif file_ext in ("srt", "vtt"):
            source_type = "subtitles"

        asyncio.create_task(_bg_save_transcript(user_id, source_type, original_text, msg.message_id, message))

        preview = original_text[:config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."
        preview = html.escape(preview)   # в файлах (md/html/xml) бывают < и &, которые ломают parse_mode=HTML

        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"

        is_image = filename.startswith("photo_") or file_ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']
        file_type_label = "изображения" if is_image else "файла"

        await msg.edit_text(
            f"✅ <b>Извлечённый текст из {file_type_label}:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b> {modes_text}\n"
            f"<b>Выберите вариант обработки:</b>",
            parse_mode="HTML",
            reply_markup=create_options_keyboard(user_id, msg.message_id)
        )
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"message.delete() failed: {e}")

    except Exception as e:
        logger.error(f"File handler error: {e}")
        await msg.edit_text(f"❌ Ошибка обработки файла: {str(e)[:100]}")
    finally:
        processing_users.discard(user_id)


# ============================================================================
# ДИАЛОГОВЫЕ CALLBACKS
# ============================================================================

@dp.callback_query(F.data.startswith("dialog_start_"))
async def dialog_start_callback(callback: types.CallbackQuery):
    await callback.answer()
    if is_shutting_down:
        await callback.message.answer("🛑 Бот останавливается.")
        return

    parts = callback.data.split("_")
    if len(parts) < 4:
        return

    user_id = int(parts[2])
    msg_id = int(parts[3])

    if callback.from_user.id != user_id:
        await callback.answer("⚠️ Это не ваш запрос!", show_alert=True)
        return

    if user_id not in user_context or msg_id not in user_context[user_id]:
        await callback.message.edit_text("❌ Документ не найден. Попробуйте заново.")
        return

    doc_text = user_context[user_id][msg_id].get("original", "")
    if not hasattr(processors, 'document_dialogues'):
        processors.document_dialogues = {}
    if user_id not in processors.document_dialogues:
        processors.document_dialogues[user_id] = {}
    processors.document_dialogues[user_id][msg_id] = {"text": doc_text, "history": []}
    active_dialogs[user_id] = msg_id

    filename = user_context[user_id][msg_id].get("filename", "документ")
    await callback.message.edit_text(
        f"💬 <b>Режим вопросов активирован</b>\n\n"
        f"📄 Документ: {filename}\n"
        f"📊 Размер текста: {len(doc_text)} символов\n\n"
        f"Задавайте вопросы по содержимому.\n"
        f"Для выхода — /exit или кнопка ниже.",
        parse_mode="HTML",
        reply_markup=create_dialog_keyboard(user_id)
    )


@dp.callback_query(F.data.startswith("dialog_exit_"))
async def dialog_exit_callback(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    if len(parts) < 3:
        return
    user_id = int(parts[2])
    if callback.from_user.id != user_id:
        return
    active_dialogs.pop(user_id, None)
    await callback.message.edit_text("✅ Вышли из режима вопросов.")


# ============================================================================
# PROCESS / MODE / SWITCH CALLBACKS
# ============================================================================

@dp.callback_query(F.data.startswith("process_"))
async def process_callback(callback: types.CallbackQuery):
    if is_shutting_down:
        await callback.answer("🛑 Бот останавливается", show_alert=True)
        return

    await callback.answer()

    try:
        parts = callback.data.split("_")
        if len(parts) < 4:
            return

        user_id = int(parts[1])
        mode = parts[2]
        msg_id = int(parts[3])

        if callback.from_user.id != user_id:
            return

        ctx_data = user_context.get(user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.answer("❌ Данные устарели. Перешлите сообщение.", show_alert=True)
            return

        original_text = ctx_data.get("original", ctx_data.get("text", ""))

        await callback.message.edit_text(f"⏳ Обрабатываю ({config.MODE_LABELS.get(mode, mode)})...")
        result = await run_mode(mode, original_text, default=original_text)

        result_clean = sanitize_llm_output(result)
        if result_clean.startswith("❌"):
            # ошибка или лимит: не кэшируем и не меняем режим, даём повторить
            await callback.message.edit_text(result_clean, parse_mode="HTML",
                                             reply_markup=create_options_keyboard(user_id, msg_id))
            return
        user_context[user_id][msg_id]["mode"] = mode
        user_context[user_id][msg_id]["cached_results"][mode] = result_clean
        schedule_persist(user_id, msg_id)

        # Сохраняем результат в БД в фоне
        transcript_id = ctx_data.get("transcript_id")
        if transcript_id:
            asyncio.create_task(database.save_result(transcript_id, mode, result_clean))

        available_modes = ctx_data.get("available_modes", ["basic", "premium"])

        if len(result_clean) > 4000:
            await callback.message.delete()
            for i in range(0, len(result_clean), 4000):
                await callback.message.answer(result_clean[i:i+4000], parse_mode="HTML")
            await callback.message.answer(
                "💾 <b>Переключение и экспорт:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(user_id, msg_id)
            )
        else:
            await callback.message.edit_text(
                result_clean,
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(user_id, msg_id)
            )

    except Exception as e:
        logger.error(f"Process callback error: {e}")
        if not is_shutting_down:
            await callback.message.edit_text("❌ Ошибка обработки")


@dp.callback_query(F.data.startswith("mode_"))
async def mode_callback(callback: types.CallbackQuery):
    if is_shutting_down:
        await callback.answer("🛑 Бот останавливается", show_alert=True)
        return

    await callback.answer()

    try:
        parts = callback.data.split("_")
        if len(parts) < 3:
            return

        new_mode = parts[1]
        msg_id = int(parts[2])
        user_id = callback.from_user.id

        ctx_data = user_context.get(user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.answer("❌ Данные устарели.", show_alert=True)
            return
        if ctx_data["mode"] == new_mode:
            return

        await callback.answer("Обрабатываю...")
        original_text = ctx_data.get("original", ctx_data.get("text", ""))

        processed = await run_mode(new_mode, original_text, default=original_text)

        processed_clean = sanitize_llm_output(processed)
        if processed_clean.startswith("❌"):
            # ошибка или лимит: прежний результат остаётся на экране, ошибку шлём отдельно
            await callback.message.answer(processed_clean, parse_mode="HTML")
            return
        user_context[user_id][msg_id]["mode"] = new_mode
        user_context[user_id][msg_id]["cached_results"][new_mode] = processed_clean
        schedule_persist(user_id, msg_id)

        transcript_id = ctx_data.get("transcript_id")
        if transcript_id:
            asyncio.create_task(database.save_result(transcript_id, new_mode, processed_clean))

        await callback.message.edit_text(
            processed_clean,
            parse_mode="HTML",
            reply_markup=create_keyboard(msg_id, new_mode, ctx_data.get("available_modes", ["basic", "premium"]))
        )

    except Exception as e:
        logger.error(f"Mode callback error: {e}")
        if not is_shutting_down:
            await callback.message.edit_text("❌ Ошибка переключения")


@dp.callback_query(F.data.startswith("switch_"))
async def switch_callback(callback: types.CallbackQuery):
    if is_shutting_down:
        await callback.answer("🛑 Бот останавливается", show_alert=True)
        return

    await callback.answer()

    try:
        parts = callback.data.split("_")
        if len(parts) < 4:
            return

        target_user_id = int(parts[1])
        target_mode = parts[2]
        msg_id = int(parts[3])

        if callback.from_user.id != target_user_id:
            return

        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.answer("❌ Текст не найден. Обработайте заново.")
            return

        available_modes = ctx_data.get("available_modes", ["basic", "premium"])
        if target_mode not in available_modes and target_mode not in config.EDIT_STYLES:
            await callback.answer("⚠️ Этот режим недоступен", show_alert=True)
            return

        cached = ctx_data["cached_results"].get(target_mode)

        if cached:
            result = cached
        else:
            await callback.message.edit_text(f"⏳ Обрабатываю ({config.MODE_LABELS.get(target_mode, target_mode)})...")
            original_text = ctx_data.get("original", ctx_data.get("text", ""))
            result = await run_mode(target_mode, original_text)

            result = sanitize_llm_output(result)
            if result.startswith("❌"):
                await callback.message.edit_text(result, parse_mode="HTML",
                                                 reply_markup=create_switch_keyboard(target_user_id, msg_id))
                return
            user_context[target_user_id][msg_id]["cached_results"][target_mode] = result
            schedule_persist(target_user_id, msg_id)

            transcript_id = ctx_data.get("transcript_id")
            if transcript_id:
                asyncio.create_task(database.save_result(transcript_id, target_mode, sanitize_for_db(result)))

        user_context[target_user_id][msg_id]["mode"] = target_mode
        # Санитизируем для Telegram (если result пришёл из кэша — ещё не обработан)
        result = sanitize_llm_output(result)

        if len(result) > 4000:
            await callback.message.delete()
            for i in range(0, len(result), 4000):
                await callback.message.answer(result[i:i+4000], parse_mode="HTML")
            await callback.message.answer(
                "💾 <b>Переключение и экспорт:</b>",
                parse_mode="HTML",
                reply_markup=create_switch_keyboard(target_user_id, msg_id)
            )
        else:
            await callback.message.edit_text(result, parse_mode="HTML", reply_markup=create_switch_keyboard(target_user_id, msg_id))

    except Exception as e:
        logger.error(f"Switch callback error: {e}")
        if not is_shutting_down:
            await callback.message.edit_text("❌ Ошибка переключения")


# ============================================================================
# EXPORT CALLBACK — двухшаговый flow с пользовательским именем
# ============================================================================

async def _do_export(
    callback_or_message,
    target_user_id: int,
    mode: str,
    msg_id: int,
    export_format: str,
    custom_name: Optional[str],
):
    """
    Создаёт файл и отправляет пользователю.
    callback_or_message — types.CallbackQuery ИЛИ types.Message; нужен только
    chat для answer_document/answer.
    """
    # Получаем chat для отправки
    if hasattr(callback_or_message, "message"):
        chat_msg = callback_or_message.message
    else:
        chat_msg = callback_or_message

    ctx_data = user_context.get(target_user_id, {}).get(msg_id)
    if not ctx_data:
        await chat_msg.answer("❌ Текст не найден.")
        return

    text = ctx_data["cached_results"].get(mode) or ctx_data.get("original", ctx_data.get("text", ""))
    if not text:
        await chat_msg.answer("⚠️ Текст не найден")
        return

    format_labels = {"txt": "📄 TXT", "pdf": "📊 PDF", "docx": "📝 DOCX"}
    status_msg = await chat_msg.answer(f"📁 Создаю {format_labels.get(export_format, 'файл')}...")

    filepath = await save_to_file(
        target_user_id, text, export_format, mode=mode, custom_name=custom_name,
    )

    if not filepath:
        try:
            await status_msg.edit_text("❌ Ошибка создания файла")
        except Exception as e:
            logger.debug(f"edit_text failed in export: {e}")
        return

    filename = os.path.basename(filepath)
    caption_map = {"txt": "📄 Текстовый файл", "pdf": "📊 PDF файл", "docx": "📝 DOCX файл"}
    caption = caption_map.get(export_format, "📁 Файл")

    try:
        document = FSInputFile(filepath, filename=filename)
        await chat_msg.answer_document(document=document, caption=caption)
        try:
            await status_msg.delete()
        except Exception as e:
            logger.debug(f"status_msg delete failed: {e}")
    finally:
        try:
            os.remove(filepath)
        except OSError as e:
            logger.debug(f"temp file cleanup failed: {e}")


def _make_filename_prompt_keyboard(token: str) -> InlineKeyboardMarkup:
    """Клавиатура под промптом ввода имени: только 'Без названия' и 'Отмена'."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🏷️ Без названия", callback_data=f"noname_{token}"),
        InlineKeyboardButton(text="✖️ Отмена",      callback_data=f"cancelexp_{token}"),
    )
    return builder.as_markup()


async def _filename_input_timeout(user_id: int, prompt_msg_id: int, chat_id: int):
    """Снимает запрос имени через таймаут, если пользователь молчит."""
    try:
        await asyncio.sleep(config.CUSTOM_FILENAME_INPUT_TIMEOUT)
        if user_id not in pending_filename_inputs:
            return
        pending_filename_inputs.pop(user_id, None)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=prompt_msg_id,
                text=config.MSG_FILENAME_TIMEOUT,
            )
        except Exception as e:
            logger.debug(f"timeout edit_message failed: {e}")
    except asyncio.CancelledError:
        # Нормальный путь — пользователь успел ответить
        pass


@dp.callback_query(F.data.startswith("export_"))
async def export_callback(callback: types.CallbackQuery):
    """Шаг 1: спрашиваем имя файла. Реальное создание — в продолжении flow."""
    if is_shutting_down:
        await callback.answer("🛑 Бот останавливается", show_alert=True)
        return

    await callback.answer()

    try:
        parts = callback.data.split("_")

        # Два формата: export_mode_msgid_fmt (из create_keyboard)
        # и export_userid_mode_msgid_fmt (из create_switch_keyboard)
        if len(parts) == 4:
            mode = parts[1]
            msg_id = int(parts[2])
            export_format = parts[3]
            target_user_id = callback.from_user.id
        elif len(parts) == 5:
            target_user_id = int(parts[1])
            mode = parts[2]
            msg_id = int(parts[3])
            export_format = parts[4]
        else:
            return

        if callback.from_user.id != target_user_id:
            return

        ctx_data = user_context.get(target_user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.answer("❌ Текст не найден.")
            return

        # Если уже идёт ожидание имени — отменяем предыдущее
        prev = pending_filename_inputs.pop(target_user_id, None)
        if prev:
            prev_task = prev.get("task")
            if prev_task and not prev_task.done():
                prev_task.cancel()

        # Шаг 1: отправляем промпт с инлайн-клавиатурой
        token = f"{mode}_{msg_id}_{export_format}"
        prompt = config.MSG_ASK_FILENAME.format(max_len=config.CUSTOM_FILENAME_MAX_LENGTH)
        prompt_msg = await callback.message.answer(
            prompt,
            parse_mode="HTML",
            reply_markup=_make_filename_prompt_keyboard(token),
        )

        # Запускаем таймаут
        timeout_task = asyncio.create_task(
            _filename_input_timeout(target_user_id, prompt_msg.message_id, callback.message.chat.id)
        )

        pending_filename_inputs[target_user_id] = {
            "mode": mode,
            "msg_id": msg_id,
            "format": export_format,
            "target_user_id": target_user_id,
            "prompt_msg_id": prompt_msg.message_id,
            "chat_id": callback.message.chat.id,
            "task": timeout_task,
        }

    except Exception as e:
        logger.error(f"Export callback error: {e}")
        if not is_shutting_down:
            await callback.message.answer("❌ Ошибка подготовки экспорта")


@dp.callback_query(F.data.startswith("noname_"))
async def export_noname_callback(callback: types.CallbackQuery):
    """Пользователь нажал «Без названия» → экспорт с автогенерируемым именем."""
    if is_shutting_down:
        await callback.answer("🛑 Бот останавливается", show_alert=True)
        return

    await callback.answer()
    user_id = callback.from_user.id
    pending = pending_filename_inputs.pop(user_id, None)
    if not pending:
        try:
            await callback.message.edit_text("⚠️ Запрос устарел. Нажмите кнопку формата ещё раз.")
        except Exception as e:
            logger.debug(f"noname edit_text failed: {e}")
        return

    task = pending.get("task")
    if task and not task.done():
        task.cancel()

    try:
        await callback.message.delete()
    except Exception as e:
        logger.debug(f"noname prompt delete failed: {e}")

    await _do_export(
        callback,
        target_user_id=pending["target_user_id"],
        mode=pending["mode"],
        msg_id=pending["msg_id"],
        export_format=pending["format"],
        custom_name=None,
    )


@dp.callback_query(F.data.startswith("cancelexp_"))
async def export_cancel_callback(callback: types.CallbackQuery):
    """Отмена ввода имени."""
    await callback.answer("Отменено")
    user_id = callback.from_user.id
    pending = pending_filename_inputs.pop(user_id, None)
    if pending:
        task = pending.get("task")
        if task and not task.done():
            task.cancel()
    try:
        await callback.message.delete()
    except Exception as e:
        logger.debug(f"cancel prompt delete failed: {e}")


async def _handle_filename_input(message: types.Message):
    """
    Обработка текста-ответа на запрос имени файла.
    Вызывается из text_handler, когда user_id есть в pending_filename_inputs.
    """
    user_id = message.from_user.id
    pending = pending_filename_inputs.get(user_id)
    if not pending:
        return  # на всякий случай, race protection

    raw = (message.text or "").strip()

    # Проверка длины ДО санитизации (чтобы предупредить пользователя честно)
    if len(raw) > config.CUSTOM_FILENAME_MAX_LENGTH:
        await message.answer(
            config.MSG_FILENAME_TOO_LONG.format(max_len=config.CUSTOM_FILENAME_MAX_LENGTH)
        )
        return  # pending не убираем — даём пользователю ещё попытку

    # Чистим
    custom = sanitize_filename(raw, config.CUSTOM_FILENAME_MAX_LENGTH)
    if not custom:
        await message.answer(config.MSG_FILENAME_EMPTY_AFTER_CLEAN)
        return

    # Принимаем — снимаем pending и таймаут
    pending_filename_inputs.pop(user_id, None)
    task = pending.get("task")
    if task and not task.done():
        task.cancel()

    # Удаляем сообщение пользователя с именем — чисто косметика
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"user filename msg delete failed: {e}")

    # Удаляем промпт
    try:
        await bot.delete_message(
            chat_id=pending["chat_id"], message_id=pending["prompt_msg_id"]
        )
    except Exception as e:
        logger.debug(f"prompt delete failed: {e}")

    await _do_export(
        message,
        target_user_id=pending["target_user_id"],
        mode=pending["mode"],
        msg_id=pending["msg_id"],
        export_format=pending["format"],
        custom_name=custom,
    )



# ============================================================================
# TRANSLATE CALLBACKS
# ============================================================================

@dp.callback_query(F.data.startswith("translate_back_"))
async def translate_back_callback(callback: types.CallbackQuery):
    """Возврат к оригинальному тексту после перевода."""
    await callback.answer()
    parts = callback.data.split("_")
    if len(parts) < 4:
        return
    user_id = int(parts[2])
    msg_id = int(parts[3])

    if callback.from_user.id != user_id:
        return

    ctx_data = user_context.get(user_id, {}).get(msg_id)
    if not ctx_data:
        await callback.answer("❌ Данные устарели.", show_alert=True)
        return

    current_mode = ctx_data.get("mode", "basic")
    original_result = ctx_data["cached_results"].get(current_mode) or ctx_data.get("original", "")
    ctx_data["is_translated"] = False

    display = original_result if len(original_result) <= 4000 else original_result[:3997] + "..."
    await callback.message.edit_text(display, reply_markup=create_switch_keyboard(user_id, msg_id))


@dp.callback_query(F.data.regexp(r'^translate_\d+_\d+$'))
async def translate_callback(callback: types.CallbackQuery):
    """Перевод текущего варианта на русский язык."""
    if is_shutting_down:
        await callback.answer("🛑 Бот останавливается", show_alert=True)
        return

    await callback.answer()

    try:
        parts = callback.data.split("_")
        if len(parts) < 3:
            return
        user_id = int(parts[1])
        msg_id = int(parts[2])

        if callback.from_user.id != user_id:
            return

        ctx_data = user_context.get(user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.answer("❌ Данные устарели.", show_alert=True)
            return

        current_mode = ctx_data.get("mode", "basic")
        text_to_translate = ctx_data["cached_results"].get(current_mode) or ctx_data.get("original", "")

        if not text_to_translate:
            await callback.answer("⚠️ Нет текста для перевода", show_alert=True)
            return

        await callback.message.edit_text(config.MSG_TRANSLATING)

        translated = await processors.translate_to_russian(text_to_translate, groq_clients)

        if translated.startswith("❌"):
            await callback.message.answer(translated)
            return

        ctx_data["is_translated"] = True

        display = translated if len(translated) <= 4000 else translated[:3997] + "..."
        await callback.message.edit_text(sanitize_llm_output(display), parse_mode="HTML", reply_markup=create_switch_keyboard(user_id, msg_id))

    except Exception as e:
        logger.error(f"Translate callback error: {e}")
        if not is_shutting_down:
            await callback.message.answer("❌ Ошибка перевода")


# ============================================================================
# BREAKDOWN CALLBACK — "Разобрать по косточкам"
# ============================================================================

@dp.callback_query(F.data.startswith("breakdown_"))
async def breakdown_callback(callback: types.CallbackQuery):
    """Разбор исправлений между оригиналом и обработанным текстом."""
    if is_shutting_down:
        await callback.answer("🛑 Бот останавливается", show_alert=True)
        return

    await callback.answer("🧠 Анализирую правки...")

    try:
        parts = callback.data.split("_")
        if len(parts) < 2:
            return

        msg_id = int(parts[1])
        user_id = callback.from_user.id

        ctx_data = user_context.get(user_id, {}).get(msg_id)
        if not ctx_data:
            await callback.message.answer("❌ Данные устарели. Обработайте текст заново.")
            return

        current_mode = ctx_data.get("mode", "basic")
        if current_mode not in ("basic", "premium"):
            await callback.answer("⚠️ Разбор доступен только для режимов «Как есть» и «Красиво»", show_alert=True)
            return

        original_text = ctx_data.get("original", "")
        corrected_text = ctx_data["cached_results"].get(current_mode)

        if not corrected_text:
            await callback.message.answer("❌ Сначала выберите режим обработки (Как есть или Красиво).")
            return

        status_msg = await callback.message.answer("🧠 Разбираю по косточкам...")

        result = await processors.breakdown_corrections(original_text, corrected_text, groq_clients)

        mode_label = "«Как есть»" if current_mode == "basic" else "«Красиво»"
        await status_msg.edit_text(
            f"🧠 <b>Разбор правок — режим {mode_label}:</b>\n\n{sanitize_llm_output(result)}",
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Breakdown callback error: {e}")
        if not is_shutting_down:
            await callback.message.answer("❌ Ошибка при разборе правок")


# ============================================================================
# СТИЛИ ТЕКСТА, НАГЛЯДНЫЙ DIFF, ИМЕНА СОБЕСЕДНИКОВ
# ============================================================================

def _split_for_telegram(text: str, limit: int = 4000) -> List[str]:
    """Режет длинный HTML-текст по переводам строк/пробелам (без разрыва слов)."""
    parts: List[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        parts.append(text)
    return parts


@dp.callback_query(F.data.startswith("stm_"))
async def style_menu_callback(callback: types.CallbackQuery):
    """«🎭 Стиль…»: подменяем клавиатуру на выбор стиля (текст сообщения не трогаем)."""
    parts = (callback.data or "").split("_")
    if len(parts) != 3 or not parts[1].lstrip("-").isdigit() or not parts[2].isdigit():
        await callback.answer()
        return
    uid, msg_id = int(parts[1]), int(parts[2])
    if callback.from_user.id != uid:
        await callback.answer()
        return
    if not user_context.get(uid, {}).get(msg_id):
        await callback.answer("❌ Данные устарели. Отправьте текст заново.", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=_style_menu_kb(uid, msg_id))
    except Exception as e:
        logger.debug(f"style menu edit skipped: {e}")


@dp.callback_query(F.data.startswith("stmb_"))
async def style_menu_back_callback(callback: types.CallbackQuery):
    parts = (callback.data or "").split("_")
    if len(parts) != 3 or not parts[1].lstrip("-").isdigit() or not parts[2].isdigit():
        await callback.answer()
        return
    uid, msg_id = int(parts[1]), int(parts[2])
    if callback.from_user.id != uid:
        await callback.answer()
        return
    ctx = user_context.get(uid, {}).get(msg_id)
    await callback.answer()
    if not ctx:
        return
    has_result = bool(ctx["cached_results"].get(ctx.get("mode")))
    kb = create_switch_keyboard(uid, msg_id) if has_result else create_options_keyboard(uid, msg_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception as e:
        logger.debug(f"style menu back skipped: {e}")


@dp.callback_query(F.data.startswith("diff_"))
async def diff_callback(callback: types.CallbackQuery):
    """Наглядный diff: что изменилось между оригиналом и текущим результатом. Не тратит лимит."""
    parts = (callback.data or "").split("_")
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer()
        return
    msg_id = int(parts[1])
    uid = callback.from_user.id
    ctx = user_context.get(uid, {}).get(msg_id)
    if not ctx:
        await callback.answer("❌ Данные устарели. Обработайте текст заново.", show_alert=True)
        return
    mode = ctx.get("mode")
    corrected = ctx["cached_results"].get(mode) if mode else None
    if mode not in config.DIFF_MODES or not corrected:
        await callback.answer("Сравнение доступно после «Как есть», «Красиво» или смены стиля.", show_alert=True)
        return
    await callback.answer("🔍 Сравниваю...")
    original = ctx.get("original", "")
    pages, changes = await asyncio.to_thread(textkit.make_diff_pages, original, textkit.strip_markup(corrected))
    label = html.escape(config.MODE_LABELS.get(mode, mode))
    for i, page in enumerate(pages):
        if i == 0 and changes:
            page = page.replace("Что изменилось</b>", f"Что изменилось</b> · {label}", 1)
        await callback.message.answer(page, parse_mode="HTML")


@dp.callback_query(F.data.startswith("spk_"))
async def speakers_callback(callback: types.CallbackQuery):
    """«👥 Назвать собеседников»: следующим сообщением пользователь присылает имена."""
    parts = (callback.data or "").split("_")
    if len(parts) != 2 or not parts[1].isdigit():
        await callback.answer()
        return
    msg_id = int(parts[1])
    uid = callback.from_user.id
    ctx = user_context.get(uid, {}).get(msg_id)
    dialogue = ctx["cached_results"].get("dialogue") if ctx else None
    if not dialogue:
        await callback.answer("❌ Сначала запустите режим «Диалог».", show_alert=True)
        return
    nums = textkit.speaker_numbers(ctx.get("dialogue_raw") or dialogue)
    pending_speaker_names[uid] = {"msg_id": msg_id, "ts": time.time()}
    await callback.answer()
    found = f"Нашёл говорящих: <b>{len(nums)}</b>.\n" if nums else ""
    await callback.message.answer(
        "👥 <b>Как зовут собеседников?</b>\n" + found +
        "\nНапишите имена по порядку через запятую: <code>Анна, Игорь</code>\n"
        "или с номерами: <code>1=Анна, 2=Игорь</code>\n\n/cancel — отмена",
        parse_mode="HTML",
    )


async def _handle_speaker_names(message: types.Message, pend: Dict[str, Any]):
    uid = message.from_user.id
    msg_id = pend["msg_id"]
    ctx = user_context.get(uid, {}).get(msg_id)
    if not ctx or not ctx["cached_results"].get("dialogue"):
        pending_speaker_names.pop(uid, None)
        await message.answer("❌ Данные устарели. Запустите «Диалог» заново.")
        return
    names = textkit.parse_speaker_names(message.text or "")
    if not names:
        await message.answer("Не разобрал имена. Пример: <code>Анна, Игорь</code> или <code>1=Анна, 2=Игорь</code>. "
                             "/cancel — отмена", parse_mode="HTML")
        return
    pending_speaker_names.pop(uid, None)

    raw = ctx.get("dialogue_raw") or ctx["cached_results"]["dialogue"]   # версия с «Говорящий N»
    ctx["dialogue_raw"] = raw
    merged = dict(ctx.get("speaker_names") or {})
    merged.update(names)
    ctx["speaker_names"] = merged
    renamed = textkit.apply_speaker_names(raw, merged)
    ctx["cached_results"]["dialogue"] = renamed
    ctx["mode"] = "dialogue"
    schedule_persist(uid, msg_id)

    chunks = _split_for_telegram(renamed)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await message.answer(chunk, parse_mode="HTML",
                             reply_markup=create_switch_keyboard(uid, msg_id) if last else None)


# ============================================================================
# INLINE-РЕЖИМ: «@бот текст» в любом чате
# ============================================================================
# Запрос ограничен 256 символами (ограничение Telegram). Сам запрос ничего не
# стоит: он лишь предлагает варианты. Обработка идёт после выбора варианта:
#   • если у бота в @BotFather включён /setinlinefeedback — сразу автоматически;
#   • иначе — по кнопке «▶️» в отправленном сообщении.

_INLINE_ALLOWED = {m for m, _, _ in config.INLINE_MODES}


def _inline_cache_put(user_id: int, text: str) -> str:
    now = time.time()
    for t in [t for t, e in inline_cache.items() if now - e["ts"] > config.INLINE_CACHE_TTL]:
        inline_cache.pop(t, None)
    for k in [k for k, ts in inline_claimed.items() if now - ts > config.INLINE_CACHE_TTL]:
        inline_claimed.pop(k, None)
    token = hashlib.sha1(f"{user_id}:{text}".encode("utf-8")).hexdigest()[:12]
    if token not in inline_cache:
        while len(inline_cache) >= config.INLINE_CACHE_MAX:
            inline_cache.pop(min(inline_cache, key=lambda t: inline_cache[t]["ts"]), None)
    inline_cache[token] = {"user_id": user_id, "text": text, "ts": now}
    return token


def _inline_button(mode: str, title: str, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"▶️ {title}", callback_data=f"iq_{mode}_{token}")
    ]])


@dp.inline_query()
async def inline_query_handler(query: types.InlineQuery):
    text = (query.query or "").strip()
    if len(text) < config.INLINE_MIN_CHARS:
        hint = types.InlineQueryResultArticle(
            id="hint",
            title="Напишите текст после имени бота",
            description="Исправлю ошибки, поменяю стиль, сокращу или переведу (до 256 знаков)",
            input_message_content=types.InputTextMessageContent(
                message_text="✍️ Напишите @имя_бота и текст, и я исправлю его прямо в переписке.",
                parse_mode=None,
            ),
        )
        await query.answer([hint], cache_time=1, is_personal=True)
        return

    token = _inline_cache_put(query.from_user.id, text)
    preview = text if len(text) <= 60 else text[:57] + "…"
    results = [
        types.InlineQueryResultArticle(
            id=f"{mode}:{token}",
            title=title,
            description=f"{desc} · {preview}",
            input_message_content=types.InputTextMessageContent(message_text=text, parse_mode=None),
            reply_markup=_inline_button(mode, title, token),
        )
        for mode, title, desc in config.INLINE_MODES
    ]
    await query.answer(results, cache_time=0, is_personal=True)


async def _process_inline(inline_message_id: str, user_id: int, mode: str, token: str):
    entry = inline_cache.get(token)
    if not entry or entry["user_id"] != user_id or mode not in _INLINE_ALLOWED:
        return
    if inline_message_id in inline_claimed:
        return                                  # уже обрабатывается (авто-режим + кнопка)
    title = next((t for m, t, _ in config.INLINE_MODES if m == mode), mode)
    access.current_user_id.set(user_id)
    await access.ensure_loaded(user_id)

    async def edit(text: str, parse_mode=None, markup=None):
        try:
            await bot.edit_message_text(text=text, inline_message_id=inline_message_id,
                                        parse_mode=parse_mode, reply_markup=markup)
        except Exception as e:
            logger.debug(f"inline edit failed: {e}")

    if not access.is_admin(user_id) and (access.remaining(user_id) or 0) <= 0:
        await edit(access.limit_exhausted_message(user_id).replace("<b>", "").replace("</b>", ""),
                   markup=_inline_button(mode, title, token))
        return

    inline_claimed[inline_message_id] = time.time()
    await edit("⏳ Обрабатываю…")
    try:
        result = await run_mode(mode, entry["text"])
    except Exception as e:
        logger.error(f"inline process error: {e}", exc_info=True)
        result = "❌ Ошибка обработки. Попробуйте ещё раз."
    if result.startswith("❌") or result.startswith("📝"):
        inline_claimed.pop(inline_message_id, None)          # можно повторить кнопкой
        await edit(f"{result}\n\n{entry['text']}", markup=_inline_button(mode, title, token))
        return
    await edit(sanitize_llm_output(result)[:4000], parse_mode="HTML")


@dp.chosen_inline_result()
async def chosen_inline_handler(chosen: types.ChosenInlineResult):
    """Приходит, если в @BotFather включён /setinlinefeedback: обрабатываем сразу после выбора."""
    if not chosen.inline_message_id:
        return
    mode, _, token = (chosen.result_id or "").partition(":")
    if token:
        await _process_inline(chosen.inline_message_id, chosen.from_user.id, mode, token)


@dp.callback_query(F.data.startswith("iq_"))
async def inline_callback(callback: types.CallbackQuery):
    parts = (callback.data or "").split("_", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    _, mode, token = parts
    if not callback.inline_message_id:
        await callback.answer("Кнопка работает только в сообщении, отправленном через inline.", show_alert=True)
        return
    entry = inline_cache.get(token)
    if not entry:
        await callback.answer("Запрос устарел. Напишите @бота и текст заново.", show_alert=True)
        return
    if callback.from_user.id != entry["user_id"]:
        await callback.answer("Обработать может только автор сообщения.", show_alert=True)
        return
    await callback.answer()
    await _process_inline(callback.inline_message_id, callback.from_user.id, mode, token)


# ============================================================================
# ТОЧКА ВХОДА
# ============================================================================

if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 8080))
        logger.info(f"🚀 Starting server on port {port}")
        uvicorn.run(
            "bot:app",
            host="0.0.0.0",
            port=port,
            log_level="info",
            workers=1,
            loop="asyncio"
        )
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C)")
    except Exception as e:
        logger.critical(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)
