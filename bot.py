# bot.py
"""
Главный файл бота
Версия 4.0 — убраны видеоплатформы, добавлен DOCX, Supabase с fallback,
/history, подпись автора, rate limiting, кнопка "Задать вопрос" только в саммари
"""

import os
import io
import sys
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
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    FSInputFile,
    TelegramObject,
    BotCommand,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramUnauthorizedError, TelegramNetworkError
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

import config
import processors
import database


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

    # Сброс вебхука
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook cleared")
    except Exception as e:
        logger.error(f"❌ Error clearing webhook: {e}")

    # Меню команд (кнопка «Меню» в интерфейсе Telegram)
    try:
        await bot.set_my_commands([
            BotCommand(command="start",   description="👋 О боте"),
            BotCommand(command="help",    description="📋 Инструкция"),
            BotCommand(command="history", description="📜 История обработок"),
        ])
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
        logger.warning(f"API unauthorized attempt with token: {x_app_token}")
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
        
        # Делаем "Красиво" (Llama 70B)
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

    mode_display = {"basic": "📝 Как есть", "premium": "✨ Красиво", "summary": "📊 Саммари"}
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
    if ctx_data and "summary" in ctx_data.get("available_modes", []):
        builder.row(InlineKeyboardButton(text="📊 Саммари", callback_data=f"process_{user_id}_summary_{msg_id}"))
    return builder.as_markup()


def create_switch_keyboard(user_id: int, msg_id: int) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура переключения. Кнопка 'Задать вопрос' только если текущий режим — summary."""
    ctx_data = user_context.get(user_id, {}).get(msg_id)
    if not ctx_data:
        return None

    current = ctx_data.get("mode", "basic")
    available = ctx_data.get("available_modes", ["basic", "premium"])
    builder = InlineKeyboardBuilder()

    mode_display = {"basic": "📝 Как есть", "premium": "✨ Красиво", "summary": "📊 Саммари"}
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

    # Кнопка "Работа над ошибками" — только для basic и premium
    if current in ("basic", "premium"):
        builder.row(InlineKeyboardButton(
            text="✏️ Работа над ошибками",
            callback_data=f"breakdown_{msg_id}"
        ))

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
        if not groq_clients:
            await placeholder.edit_text("❌ Нет доступных Groq клиентов")
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
    """Возвращает строку вида '👤 Имя:' для подписи транскрипта."""
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
    status_text += f"\n\n💬 Активных диалогов: {len(active_dialogs)}"
    await message.answer(status_text, parse_mode="HTML")


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

        asyncio.create_task(_bg_save_transcript(user_id, source_type, original_text, msg.message_id, message))

        preview = original_text[:config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."

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

        await callback.message.edit_text(f"⏳ Обрабатываю ({mode})...")

        if mode == "basic":
            result = await processors.correct_text_basic(original_text, groq_clients)
        elif mode == "premium":
            result = await processors.correct_text_premium(original_text, groq_clients)
        elif mode == "summary":
            result = await processors.summarize_text(original_text, groq_clients)
        else:
            result = original_text

        result_clean = sanitize_llm_output(result)
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

        if new_mode == "basic":
            processed = await processors.correct_text_basic(original_text, groq_clients)
        elif new_mode == "premium":
            processed = await processors.correct_text_premium(original_text, groq_clients)
        elif new_mode == "summary":
            processed = await processors.summarize_text(original_text, groq_clients)
        else:
            processed = original_text

        processed_clean = sanitize_llm_output(processed)
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
        if target_mode not in available_modes:
            await callback.answer("⚠️ Этот режим недоступен", show_alert=True)
            return

        cached = ctx_data["cached_results"].get(target_mode)

        if cached:
            result = cached
        else:
            await callback.message.edit_text(f"⏳ Обрабатываю ({target_mode})...")
            original_text = ctx_data.get("original", ctx_data.get("text", ""))

            if target_mode == "basic":
                result = await processors.correct_text_basic(original_text, groq_clients)
            elif target_mode == "premium":
                result = await processors.correct_text_premium(original_text, groq_clients)
            elif target_mode == "summary":
                result = await processors.summarize_text(original_text, groq_clients)
            else:
                result = "❌ Неизвестный режим"

            result = sanitize_llm_output(result)
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
