# bot.py
"""
Главный файл бота: хэндлеры, управление контекстом, видео-обработка
Версия 3.2 (Гибрид: 3.1 + middleware, стриминг, диалоговый режим из 6.4)
С ИНТЕГРАЦИЕЙ АНТИПАДЕНИЕ ШАБЛОНА (FastAPI + мониторинг + graceful shutdown)
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
from fastapi import FastAPI, Request, Response
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
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramUnauthorizedError, TelegramNetworkError
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

import config
import processors

# Попытка импорта psutil (опционально)
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logging.warning("psutil not installed. Some metrics will be unavailable.")

load_dotenv()

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")

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

# Глобальное хранилище: user_id -> { message_id: {"text": "...", "mode": "...", "time": ...} }
user_context: Dict[int, Dict[int, Any]] = {}

# Хранилище активных диалогов (user_id -> message_id документа)
active_dialogs: Dict[int, int] = {}

groq_clients = []
current_client_index = 0


# ============================================================================
# ОБРАБОТКА СИГНАЛОВ (GRACEFUL SHUTDOWN)
# ============================================================================

def handle_sigterm(signum, frame):
    """Обработчик сигнала SIGTERM от Render"""
    global is_shutting_down
    if is_shutting_down:
        return
    logger.info("📡 Received SIGTERM signal, initiating graceful shutdown...")
    is_shutting_down = True
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(lambda: asyncio.create_task(shutdown_event.set()))
    except RuntimeError:
        # Если нет запущенного цикла, создаем новый
        asyncio.run(initiate_shutdown())


async def initiate_shutdown():
    """Инициировать graceful shutdown"""
    shutdown_event.set()


# ============================================================================
# MIDDLEWARE ДЛЯ ОБРАБОТКИ ОШИБОК И МОНИТОРИНГА
# ============================================================================

class ErrorHandlingMiddleware(BaseMiddleware):
    """
    Middleware для обработки ошибок и автоматического восстановления
    Централизованная обработка исключений во всех хендлерах + мониторинг
    """
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
            logger.error(f"❌ Ошибка авторизации в middleware: {e}")
            if is_shutting_down:
                raise
            # Не пробуем сбросить вебхук здесь - это может вызвать рекурсию
            raise
        except TelegramNetworkError as e:
            stats["errors"] += 1
            logger.error(f"❌ Сетевая ошибка в middleware: {e}")
            if is_shutting_down:
                raise
            # Можно добавить автоматическое восстановление через retry
            raise
        except Exception as e:
            stats["errors"] += 1
            logger.error(f"❌ Необработанная ошибка в middleware: {e}", exc_info=True)
            
            if is_shutting_down:
                raise
                
            # Пробуем уведомить пользователя, если это возможно
            try:
                if hasattr(event, "message") and event.message:
                    await event.message.answer("❌ Произошла внутренняя ошибка. Попробуйте позже.")
                elif hasattr(event, "callback_query") and event.callback_query:
                    await event.callback_query.message.answer("❌ Произошла внутренняя ошибка. Попробуйте позже.")
            except:
                pass
            raise


# Регистрируем middleware
dp.message.middleware(ErrorHandlingMiddleware())
dp.callback_query.middleware(ErrorHandlingMiddleware())


# ============================================================================
# POLLING TASK (С АВТОМАТИЧЕСКИМ ВОССТАНОВЛЕНИЕМ)
# ============================================================================

async def run_polling():
    """Задача для запуска polling с автоматическим восстановлением после ошибок"""
    global is_shutting_down
    logger.info("🚀 Starting bot polling task...")
    
    while not is_shutting_down:
        try:
            logger.info("🔄 Polling started")
            await dp.start_polling(bot)
        except asyncio.CancelledError:
            logger.info("Polling task cancelled")
            break
        except Exception as e:
            if is_shutting_down:
                logger.info("Shutting down, exiting polling loop")
                break
            logger.error(f"❌ Polling crashed: {e}. Restarting in 5 seconds...", exc_info=True)
            await asyncio.sleep(5)


# ============================================================================
# FASTAPI ПРИЛОЖЕНИЕ (ДЛЯ ХОСТИНГА И МОНИТОРИНГА)
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл FastAPI приложения"""
    global polling_task
    
    logger.info("=" * 50)
    logger.info("🟢 FASTAPI APP STARTING")
    logger.info("=" * 50)
    
    # Шаг 1: Инициализация Groq клиентов
    logger.info("🔧 Initializing Groq clients...")
    init_groq_clients()
    processors.vision_processor.init_clients(groq_clients)
    
    # Инициализируем хранилище диалогов в processors если его нет
    if not hasattr(processors, 'document_dialogues'):
        processors.document_dialogues = {}
    
    # Шаг 2: Сброс вебхука перед запуском polling
    logger.info("📡 Clearing webhook...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook cleared")
    except Exception as e:
        logger.error(f"❌ Error clearing webhook: {e}")
    
    # Шаг 3: Запуск polling в фоне
    logger.info("🤖 Starting bot polling...")
    polling_task = asyncio.create_task(run_polling())
    
    # Шаг 4: Запуск фоновых задач очистки
    logger.info("🧹 Starting cleanup tasks...")
    cleanup_task = asyncio.create_task(cleanup_old_contexts())
    temp_cleanup_task = asyncio.create_task(cleanup_temp_files())
    
    # Шаг 5: Регистрация обработчиков сигналов
    logger.info("📡 Registering signal handlers...")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handle_sigterm, sig, None)
        except NotImplementedError:
            # Windows не поддерживает сигналы
            logger.warning(f"Signal handler for {sig} not supported on this platform")
    
    logger.info("=" * 50)
    logger.info("✅ BOT IS RUNNING")
    logger.info("=" * 50)
    
    yield  # Приложение работает
    
    # === SHUTDOWN ===
    logger.info("=" * 50)
    logger.info("🔴 SHUTTING DOWN")
    logger.info("=" * 50)
    
    # Останавливаем polling
    if polling_task and not polling_task.done():
        logger.info("🛑 Stopping polling...")
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            logger.info("✅ Polling stopped")
    
    # Отменяем фоновые задачи
    logger.info("🛑 Canceling cleanup tasks...")
    cleanup_task.cancel()
    temp_cleanup_task.cancel()
    
    try:
        await asyncio.gather(cleanup_task, temp_cleanup_task, return_exceptions=True)
    except:
        pass
    
    # Сохраняем контекст и закрываем сессии
    logger.info("📝 Saving context and closing sessions...")
    try:
        logger.info(f"   Users in context: {len(user_context)}")
        logger.info(f"   Active dialogs: {len(active_dialogs)}")
        user_context.clear()
        active_dialogs.clear()
        if hasattr(processors, 'document_dialogues'):
            processors.document_dialogues.clear()
        await bot.session.close()
        logger.info("✅ Cleanup complete")
    except Exception as e:
        logger.error(f"❌ Error during cleanup: {e}")
    
    logger.info("=" * 50)
    logger.info("✅ BOT STOPPED")
    logger.info("=" * 50)


# Создаем FastAPI приложение
app = FastAPI(
    lifespan=lifespan,
    docs_url=None,  # Отключаем документацию в production
    redoc_url=None
)


# === FASTAPI MIDDLEWARE ДЛЯ МОНИТОРИНГА ===
@app.middleware("http")
async def monitor_requests(request: Request, call_next):
    """Мониторинг HTTP запросов"""
    stats["total_updates"] += 1
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        stats["errors"] += 1
        logger.error(f"HTTP error: {e}")
        raise


# === HEALTH CHECK ENDPOINTS ===
@app.get("/health")
@app.head("/health")
@app.get("/ping")
async def health():
    """Health check для Uptime Robot и Render"""
    return Response(
        content='{"status": "healthy", "service": "speech-flow-bot", "version": "3.2"}',
        media_type="application/json",
        status_code=200
    )


@app.get("/")
async def root():
    """Корневой эндпоинт"""
    return {
        "service": "Speech Flow Bot",
        "version": "3.2",
        "status": "running",
        "uptime": int(time.time() - start_time)
    }


@app.get("/metrics")
async def metrics():
    """Метрики для мониторинга (Prometheus format)"""
    uptime = int(time.time() - start_time)
    
    # Базовые метрики
    text = f"""# HELP bot_uptime Uptime in seconds
# TYPE bot_uptime gauge
bot_uptime {uptime}
# HELP bot_requests_total Total requests processed
# TYPE bot_requests_total counter
bot_requests_total {stats["total_updates"]}
# HELP bot_errors_total Total errors
# TYPE bot_errors_total counter
bot_errors_total {stats["errors"]}
# HELP bot_processed_messages Total processed messages
# TYPE bot_processed_messages counter
bot_processed_messages {stats["processed_messages"]}
# HELP bot_active_dialogs Active dialog sessions
# TYPE bot_active_dialogs gauge
bot_active_dialogs {len(active_dialogs)}
# HELP bot_users_in_context Users with active context
# TYPE bot_users_in_context gauge
bot_users_in_context {len(user_context)}
"""
    
    # Добавляем метрики psutil если доступны
    if PSUTIL_AVAILABLE:
        try:
            ram_mb = psutil.Process().memory_info().rss / 1024 / 1024
            cpu = psutil.Process().cpu_percent()
            text += f"""# HELP bot_ram_mb RAM usage in MB
# TYPE bot_ram_mb gauge
bot_ram_mb {ram_mb:.2f}
# HELP bot_cpu CPU usage percent
# TYPE bot_cpu gauge
bot_cpu {cpu}
"""
        except:
            pass
    
    return Response(content=text, media_type="text/plain")


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ GROQ КЛИЕНТОВ
# ============================================================================

def init_groq_clients():
    """Инициализация клиентов Groq"""
    global groq_clients
    
    if not GROQ_API_KEYS:
        logger.warning("GROQ_API_KEYS not configured!")
        return
    
    keys = [key.strip() for key in GROQ_API_KEYS.split(",") if key.strip()]
    
    for key in keys:
        try:
            client = AsyncOpenAI(
                api_key=key,
                base_url="https://api.groq.com/openai/v1",
                timeout=config.GROQ_TIMEOUT,
            )
            groq_clients.append(client)
            logger.info(f"✅ Groq client initialized: {key[:8]}...")
        except Exception as e:
            logger.error(f"❌ Error initializing client {key[:8]}...: {e}")
    
    logger.info(f"✅ Total Groq clients: {len(groq_clients)}")


# ============================================================================
# УПРАВЛЕНИЕ КЭШЕМ И КОНТЕКСТОМ
# ============================================================================

def save_to_history(user_id: int, msg_id: int, text: str, mode: str = "basic", available_modes: list = None):
    """Сохраняем текст, привязывая его к ID сообщения"""
    if user_id not in user_context:
        user_context[user_id] = {}
    
    # Очистка старых записей, если их слишком много
    if len(user_context[user_id]) > config.MAX_CONTEXTS_PER_USER:
        oldest_msg = min(user_context[user_id].keys(), key=lambda k: user_context[user_id][k]['time'])
        user_context[user_id].pop(oldest_msg)
    
    # Создаем запись с расширенной информацией
    user_context[user_id][msg_id] = {
        "text": text,
        "mode": mode,
        "time": datetime.now(),
        "available_modes": available_modes or ["basic"],
        "original": text,
        "cached_results": {"basic": None, "premium": None, "summary": None},
        "type": "text",
        "chat_id": None,
        "filename": None
    }


async def cleanup_old_contexts():
    """Фоновая задача: удаление контекстов старше CACHE_TIMEOUT_SECONDS"""
    while not is_shutting_down and not shutdown_event.is_set():
        try:
            await asyncio.sleep(config.CACHE_CHECK_INTERVAL)
            
            if is_shutting_down or shutdown_event.is_set():
                break
            
            current_time = datetime.now()
            users_to_clean = []
            
            for user_id, messages in user_context.items():
                for msg_id, ctx in list(messages.items()):
                    context_age = (current_time - ctx.get("time", current_time)).total_seconds()
                    
                    if context_age > config.CACHE_TIMEOUT_SECONDS:
                        messages.pop(msg_id, None)
                        logger.debug(f"Cleaned up message {msg_id} for user {user_id}")
                
                if not messages:
                    users_to_clean.append(user_id)
            
            for user_id in users_to_clean:
                if user_id in user_context:
                    del user_context[user_id]
                    logger.debug(f"Cleaned up empty user context for user {user_id}")
            
            if users_to_clean:
                logger.info(f"Cache cleanup: removed {len(users_to_clean)} contexts. Current users: {len(user_context)}")
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")


async def cleanup_temp_files():
    """Фоновая задача: удаление старых временных файлов"""
    while not is_shutting_down and not shutdown_event.is_set():
        try:
            await asyncio.sleep(config.TEMP_FILE_RETENTION)
            
            if is_shutting_down or shutdown_event.is_set() or not config.CLEANUP_TEMP_FILES:
                continue
            
            current_time = datetime.now().timestamp()
            temp_dir = config.TEMP_DIR
            
            if not os.path.exists(temp_dir):
                continue
            
            deleted_count = 0
            for filename in os.listdir(temp_dir):
                if filename.startswith('video_') or filename.startswith('audio_') or filename.startswith('text_'):
                    filepath = os.path.join(temp_dir, filename)
                    
                    try:
                        file_age = current_time - os.path.getmtime(filepath)
                        if file_age > config.TEMP_FILE_RETENTION:
                            os.remove(filepath)
                            deleted_count += 1
                            logger.debug(f"Deleted temp file: {filename}")
                    except Exception as e:
                        logger.debug(f"Error deleting temp file {filename}: {e}")
            
            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} temporary files")
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Temp files cleanup error: {e}")


# ============================================================================
# КЛАВИАТУРЫ
# ============================================================================

def create_dialog_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создание клавиатуры для режима диалога с документом"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🚪 Выйти из режима вопросов",
            callback_data=f"dialog_exit_{user_id}"
        )
    )
    return builder.as_markup()


def create_keyboard(msg_id: int, current_mode: str, available_modes: list = None) -> InlineKeyboardMarkup:
    """Создаем клавиатуру, где в callback_data зашит ID сообщения"""
    builder = InlineKeyboardBuilder()
    
    if available_modes is None:
        available_modes = ["basic", "premium"]
    
    mode_buttons = []
    
    mode_display = {
        "basic": "📝 Как есть",
        "premium": "✨ Красиво", 
        "summary": "📊 Саммари"
    }
    
    mode_codes = {
        "basic": "basic",
        "premium": "premium",
        "summary": "summary"
    }
    
    for mode_code in available_modes:
        if mode_code in mode_display:
            prefix = "✅ " if mode_code == current_mode else ""
            mode_buttons.append(
                InlineKeyboardButton(
                    text=f"{prefix}{mode_display[mode_code]}", 
                    callback_data=f"mode_{mode_codes.get(mode_code, mode_code)}_{msg_id}"
                )
            )
    
    for i in range(0, len(mode_buttons), 2):
        if i + 1 < len(mode_buttons):
            builder.row(mode_buttons[i], mode_buttons[i + 1])
        else:
            builder.row(mode_buttons[i])
    
    if current_mode:
        builder.row(
            InlineKeyboardButton(text="📄 TXT", callback_data=f"export_{current_mode}_{msg_id}_txt"),
            InlineKeyboardButton(text="📊 PDF", callback_data=f"export_{current_mode}_{msg_id}_pdf")
        )
    
    return builder.as_markup()


def create_options_keyboard(user_id: int, msg_id: int) -> InlineKeyboardMarkup:
    """Клавиатура с вариантами обработки для первого выбора"""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text="📝 Как есть", callback_data=f"process_{user_id}_basic_{msg_id}"),
        InlineKeyboardButton(text="✨ Красиво", callback_data=f"process_{user_id}_premium_{msg_id}"),
    )
    
    ctx_data = None
    if user_id in user_context:
        for m_id, ctx in user_context[user_id].items():
            if m_id == msg_id:
                ctx_data = ctx
                break
    
    available_modes = ctx_data.get("available_modes", []) if ctx_data else []
    
    if "summary" in available_modes:
        builder.row(
            InlineKeyboardButton(text="📊 Саммари", callback_data=f"process_{user_id}_summary_{msg_id}"),
        )
    
    if ctx_data and len(ctx_data.get("original", "")) > 100:  # Только для достаточно длинных текстов
        builder.row(
            InlineKeyboardButton(
                text="💬 Задать вопрос по тексту", 
                callback_data=f"dialog_start_{user_id}_{msg_id}"
            ),
        )
    
    return builder.as_markup()


def create_switch_keyboard(user_id: int, msg_id: int) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура для переключения между режимами"""
    ctx_data = None
    if user_id in user_context:
        for m_id, ctx in user_context[user_id].items():
            if m_id == msg_id:
                ctx_data = ctx
                break
    
    if not ctx_data:
        return None
    
    current = ctx_data.get("mode", "basic")
    available = ctx_data.get("available_modes", ["basic", "premium"])
    
    builder = InlineKeyboardBuilder()
    
    mode_buttons = []
    mode_display = {
        "basic": "📝 Как есть",
        "premium": "✨ Красиво",
        "summary": "📊 Саммари"
    }
    
    for mode in available:
        if mode != current:
            mode_buttons.append(
                InlineKeyboardButton(
                    text=mode_display.get(mode, mode), 
                    callback_data=f"switch_{user_id}_{mode}_{msg_id}"
                )
            )
    
    for i in range(0, len(mode_buttons), 2):
        if i + 1 < len(mode_buttons):
            builder.row(mode_buttons[i], mode_buttons[i + 1])
        else:
            builder.row(mode_buttons[i])
    
    if len(ctx_data.get("original", "")) > 100:
        builder.row(
            InlineKeyboardButton(
                text="💬 Задать вопрос по тексту", 
                callback_data=f"dialog_start_{user_id}_{msg_id}"
            ),
        )
    
    if current:
        builder.row(
            InlineKeyboardButton(text="📄 TXT", callback_data=f"export_{user_id}_{current}_{msg_id}_txt"),
            InlineKeyboardButton(text="📊 PDF", callback_data=f"export_{user_id}_{current}_{msg_id}_pdf")
        )
    
    return builder.as_markup()


# ============================================================================
# СОХРАНЕНИЕ ФАЙЛОВ
# ============================================================================

async def save_to_file(user_id: int, text: str, format_type: str) -> Optional[str]:
    """Сохраняем текст в файл (TXT или PDF)"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"text_{user_id}_{timestamp}"
    
    if format_type == "txt":
        filepath = f"{config.TEMP_DIR}/{filename}.txt"
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            logger.debug(f"Saved TXT file: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"Error saving TXT: {e}")
            return None
        
    elif format_type == "pdf":
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
            from reportlab.lib.utils import simpleSplit
            
            filepath = f"{config.TEMP_DIR}/{filename}.pdf"
            c = canvas.Canvas(filepath, pagesize=A4)
            width, height = A4
            
            margin = 50
            line_height = 14
            y = height - margin
            
            c.setFont("Helvetica-Bold", 14)
            c.drawString(margin, y, "Обработанный текст")
            y -= 30
            
            c.setFont("Helvetica", 10)
            c.drawString(margin, y, f"Создано: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
            y -= 40
            
            c.setFont("Helvetica", 11)
            max_width = width - 2 * margin
            
            for paragraph in text.split('\n'):
                if not paragraph.strip():
                    y -= line_height
                    continue
                    
                lines = simpleSplit(paragraph, "Helvetica", 11, max_width)
                
                for line in lines:
                    if y < margin + 20:
                        c.showPage()
                        y = height - margin
                        c.setFont("Helvetica", 11)
                    c.drawString(margin, y, line)
                    y -= line_height
            
            c.save()
            logger.debug(f"Saved PDF file: {filepath}")
            return filepath
            
        except ImportError:
            logger.warning("Reportlab not installed, using txt fallback")
            filepath = f"{config.TEMP_DIR}/{filename}.txt"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)
            return filepath
        except Exception as e:
            logger.error(f"Error saving PDF: {e}")
            return None
    
    return None


# ============================================================================
# СТРИМИНГ ОТВЕТОВ В ДИАЛОГОВОМ РЕЖИМЕ
# ============================================================================

async def handle_streaming_answer(message: types.Message, user_id: int, msg_id: int, question: str):
    """Обработка стримингового ответа на вопрос с визуализацией процесса"""
    placeholder = await message.answer("💭 Думаю...")
    
    accumulated = ""
    last_edit_length = 0
    edit_counter = 0
    
    try:
        if is_shutting_down:
            await placeholder.edit_text("🛑 Бот останавливается, попробуйте позже.")
            return
        
        # Проверяем наличие Groq клиентов
        if not groq_clients:
            await placeholder.edit_text("❌ Ошибка: нет доступных Groq клиентов")
            return
        
        # Проверяем наличие документа в контексте
        if user_id not in user_context or msg_id not in user_context[user_id]:
            await placeholder.edit_text("❌ Документ не найден. Начните заново.")
            if user_id in active_dialogs:
                del active_dialogs[user_id]
            return
        
        # Получаем текст документа
        doc_text = user_context[user_id][msg_id].get("original", "")
        if not doc_text:
            await placeholder.edit_text("❌ Текст документа пуст")
            return
        
        # Сохраняем в document_dialogues для processors
        if not hasattr(processors, 'document_dialogues'):
            processors.document_dialogues = {}
        
        if user_id not in processors.document_dialogues:
            processors.document_dialogues[user_id] = {}
        
        processors.document_dialogues[user_id][msg_id] = {
            "text": doc_text,
            "history": []
        }
        
        # Получаем стриминг ответа
        async for chunk in processors.stream_document_answer(
            user_id,
            msg_id,
            question,
            groq_clients
        ):
            if chunk and not is_shutting_down:
                accumulated += chunk
                
                # Обновляем сообщение при накоплении
                if len(accumulated) - last_edit_length > 30:
                    try:
                        display_text = accumulated + "▌"
                        if len(display_text) > 4096:
                            display_text = display_text[:4093] + "..."
                        
                        await placeholder.edit_text(
                            display_text,
                            reply_markup=create_dialog_keyboard(user_id)
                        )
                        edit_counter += 1
                    except Exception as edit_error:
                        logger.error(f"Ошибка редактирования: {edit_error}")
                    last_edit_length = len(accumulated)
        
        if is_shutting_down:
            return
        
        # Финальный текст без курсора
        final_text = accumulated if accumulated else "❌ Пустой ответ"
        if len(final_text) > 4096:
            final_text = final_text[:4093] + "..."
        
        await placeholder.edit_text(
            final_text,
            reply_markup=create_dialog_keyboard(user_id)
        )
        
        logger.debug(f"Стриминг завершен: {edit_counter} обновлений, {len(accumulated)} символов")
        
        # Сохраняем вопрос и ответ в историю
        if user_id in processors.document_dialogues and msg_id in processors.document_dialogues[user_id]:
            if "history" not in processors.document_dialogues[user_id][msg_id]:
                processors.document_dialogues[user_id][msg_id]["history"] = []
            
            processors.document_dialogues[user_id][msg_id]["history"].append({
                "question": question,
                "answer": accumulated,
                "timestamp": datetime.now().isoformat()
            })
        
    except asyncio.CancelledError:
        logger.info("Streaming cancelled")
        try:
            await placeholder.edit_text("🛑 Генерация прервана.")
        except:
            pass
    except Exception as e:
        logger.error(f"Ошибка стриминга: {e}", exc_info=True)
        if not is_shutting_down:
            try:
                await placeholder.edit_text(f"❌ Ошибка при генерации ответа: {str(e)[:200]}")
            except:
                pass


# ============================================================================
# ХЭНДЛЕРЫ БОТА
# ============================================================================

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    """Команда /start"""
    stats["processed_messages"] += 1
    await message.answer(
        config.START_MESSAGE,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(Command("help"))
async def help_handler(message: types.Message):
    """Команда /help"""
    stats["processed_messages"] += 1
    await message.answer(
        config.HELP_MESSAGE,
        parse_mode="HTML"
    )


@dp.message(Command("status"))
async def status_handler(message: types.Message):
    """Команда /status"""
    stats["processed_messages"] += 1
    
    docx_status = "✅"
    try:
        import docx
    except ImportError:
        docx_status = "❌"
    
    temp_files = len([f for f in os.listdir(config.TEMP_DIR) 
                     if f.startswith('video_') or f.startswith('audio_') or f.startswith('text_')]) if os.path.exists(config.TEMP_DIR) else 0
    
    status_text = config.STATUS_MESSAGE.format(
        groq_count=len(groq_clients),
        users_count=len(user_context),
        vision_status="✅" if groq_clients else "❌",
        docx_status=docx_status,
        temp_files=temp_files
    )
    
    status_text += f"\n\n💬 Активных диалогов: {len(active_dialogs)}"
    status_text += f"\n📊 Всего обработано сообщений: {stats['processed_messages']}"
    
    await message.answer(status_text, parse_mode="HTML")


@dp.message(Command("exit"))
async def exit_dialog_handler(message: types.Message):
    """Команда для выхода из диалогового режима"""
    stats["processed_messages"] += 1
    user_id = message.from_user.id
    
    if user_id in active_dialogs:
        del active_dialogs[user_id]
        await message.answer("✅ Вы вышли из режима вопросов.")
    else:
        await message.answer("❌ Вы не находитесь в режиме вопросов.")


@dp.message(F.voice)
async def voice_handler(message: types.Message):
    """Обработка голосовых сообщений и кружочков"""
    if is_shutting_down:
        await message.answer("🛑 Бот останавливается, попробуйте позже.")
        return
        
    user_id = message.from_user.id
    
    # Если пользователь в диалоговом режиме, обрабатываем как вопрос
    if user_id in active_dialogs:
        await message.answer("⏳ Голосовые вопросы пока не поддерживаются. Напишите текст.")
        return
    
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
        
        save_to_history(
            user_id, 
            msg.message_id, 
            original_text, 
            mode="basic", 
            available_modes=available_modes
        )
        
        if user_id in user_context and msg.message_id in user_context[user_id]:
            user_context[user_id][msg.message_id]["type"] = "voice"
            user_context[user_id][msg.message_id]["chat_id"] = message.chat.id
            user_context[user_id][msg.message_id]["cached_results"] = {"basic": None, "premium": None, "summary": None}
        
        preview = original_text[:config.PREVIEW_LENGTH]
        if len(original_text) > config.PREVIEW_LENGTH:
            preview += "..."
        
        modes_text = "📝 Как есть, ✨ Красиво"
        if "summary" in available_modes:
            modes_text += ", 📊 Саммари"
        
        await msg.edit_text(
            f"✅ <b>Распознанный текст:</b>\n\n"
            f"<i>{preview}</i>\n\n"
            f"<b>Доступные режимы:</b
