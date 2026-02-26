# main.py
"""
Ядро и жизненный цикл приложения.
FastAPI + aiogram polling + graceful shutdown + /health + /metrics
Версия 4.0 (FastAPI-архитектура на базе ai_studio_code шаблона)
"""

import os
import sys
import signal
import logging
import asyncio
import time
import psutil
from contextlib import asynccontextmanager
from typing import Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, Response, Request
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from openai import AsyncOpenAI

import config
import processors
from handlers import router, set_shared_state

load_dotenv()

# ============================================================================
# ЛОГИРОВАНИЕ
# ============================================================================

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

# ============================================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# ============================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not found! Exiting.")
    sys.exit(1)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
dp.include_router(router)

# ============================================================================
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# ============================================================================

start_time = time.time()
polling_task = None
is_shutting_down = False
shutdown_event = asyncio.Event()
stats: Dict[str, int] = {"total_requests": 0, "errors": 0}

groq_clients = []


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ GROQ
# ============================================================================

def init_groq_clients():
    """Инициализация клиентов Groq из переменной окружения"""
    global groq_clients

    if not GROQ_API_KEYS:
        logger.warning("GROQ_API_KEYS not configured!")
        return

    keys = [k.strip() for k in GROQ_API_KEYS.split(",") if k.strip()]
    for key in keys:
        try:
            client = AsyncOpenAI(
                api_key=key,
                base_url="https://api.groq.com/openai/v1",
                timeout=config.GROQ_TIMEOUT,
            )
            groq_clients.append(client)
            logger.info(f"✅ Groq client: {key[:8]}...")
        except Exception as e:
            logger.error(f"❌ Groq client error {key[:8]}...: {e}")

    logger.info(f"✅ Total Groq clients: {len(groq_clients)}")


# ============================================================================
# ФОНОВЫЕ ЗАДАЧИ (планировщик вместо while True)
# ============================================================================

async def _run_periodically(interval: int, coro_factory):
    """Универсальная обёртка: запускать корутину каждые N секунд"""
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.ensure_future(coro_factory())),
                timeout=interval,
            )
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Periodic task error: {e}")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break


async def _cleanup_contexts():
    """Разовая чистка устаревших контекстов (вызывается планировщиком)"""
    from datetime import datetime
    from handlers import user_context

    current_time = datetime.now()
    users_to_clean = []

    for user_id, messages in list(user_context.items()):
        for msg_id, ctx in list(messages.items()):
            age = (current_time - ctx.get("time", current_time)).total_seconds()
            if age > config.CACHE_TIMEOUT_SECONDS:
                messages.pop(msg_id, None)
                logger.debug(f"Cleaned msg {msg_id} for user {user_id}")
        if not messages:
            users_to_clean.append(user_id)

    for uid in users_to_clean:
        user_context.pop(uid, None)

    if users_to_clean:
        logger.info(
            f"Cache cleanup: removed {len(users_to_clean)} users. "
            f"Active: {len(user_context)}"
        )


async def _cleanup_temp_files():
    """Разовая чистка временных файлов (вызывается планировщиком)"""
    if not config.CLEANUP_TEMP_FILES:
        return

    temp_dir = config.TEMP_DIR
    if not os.path.exists(temp_dir):
        return

    now = time.time()
    deleted = 0
    for fname in os.listdir(temp_dir):
        if fname.startswith(("video_", "audio_", "text_")):
            fpath = os.path.join(temp_dir, fname)
            try:
                if now - os.path.getmtime(fpath) > config.TEMP_FILE_RETENTION:
                    os.remove(fpath)
                    deleted += 1
            except Exception as e:
                logger.debug(f"Temp cleanup error {fname}: {e}")

    if deleted:
        logger.debug(f"Deleted {deleted} temp files")


# ============================================================================
# POLLING TASK
# ============================================================================

async def run_polling():
    """Фоновой таск с автоперезапуском при падении"""
    global is_shutting_down
    while not is_shutting_down:
        try:
            logger.info("🚀 Starting bot polling...")
            await dp.start_polling(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            if is_shutting_down:
                break
            logger.error(f"❌ Polling crashed: {e}. Restarting in 5s...")
            await asyncio.sleep(5)


# ============================================================================
# GRACEFUL SHUTDOWN
# ============================================================================

def handle_sigterm(signum, frame):
    global is_shutting_down
    if is_shutting_down:
        return
    logger.info("📡 SIGTERM received — graceful shutdown...")
    is_shutting_down = True
    loop = asyncio.get_running_loop()
    loop.call_soon_threadsafe(shutdown_event.set)


# ============================================================================
# LIFESPAN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global polling_task

    logger.info("=" * 50)
    logger.info("🟢 App starting — v4.0")
    logger.info("=" * 50)

    # Инициализация
    init_groq_clients()
    processors.vision_processor.init_clients(groq_clients)
    if not hasattr(processors, "document_dialogues"):
        processors.document_dialogues = {}

    # Передаём клиентов в handlers
    set_shared_state(bot, groq_clients)

    # Гарантируем наличие temp dir
    os.makedirs(config.TEMP_DIR, exist_ok=True)

    # Webhook сброс
    await bot.delete_webhook(drop_pending_updates=True)

    # Сигналы
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_sigterm, sig, None)

    # Фоновые задачи
    polling_task = asyncio.create_task(run_polling())
    context_cleanup_task = asyncio.create_task(
        _run_periodically(config.CACHE_CHECK_INTERVAL, _cleanup_contexts)
    )
    temp_cleanup_task = asyncio.create_task(
        _run_periodically(config.TEMP_FILE_RETENTION, _cleanup_temp_files)
    )

    logger.info("✅ Bot ready")

    yield  # Сервер работает

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("🔴 Shutting down...")

    is_shutting_down = True
    shutdown_event.set()

    for task in (polling_task, context_cleanup_task, temp_cleanup_task):
        if task and not task.done():
            task.cancel()

    await asyncio.gather(
        polling_task,
        context_cleanup_task,
        temp_cleanup_task,
        return_exceptions=True,
    )

    await bot.session.close()
    logger.info("✅ Shutdown complete")


# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def monitor_requests(request: Request, call_next):
    stats["total_requests"] += 1
    try:
        return await call_next(request)
    except Exception:
        stats["errors"] += 1
        raise


@app.get("/health")
@app.head("/health")
@app.get("/")
@app.get("/ping")
async def health():
    return Response(content="OK", status_code=200)


@app.get("/metrics")
async def metrics():
    uptime = int(time.time() - start_time)
    try:
        proc = psutil.Process()
        ram_mb = proc.memory_info().rss / 1024 / 1024
        cpu = proc.cpu_percent()
    except Exception:
        ram_mb = cpu = 0

    text = (
        f"# HELP bot_uptime Uptime in seconds\n"
        f"# TYPE bot_uptime gauge\n"
        f"bot_uptime {uptime}\n"
        f"# HELP bot_ram_mb RAM usage MB\n"
        f"bot_ram_mb {ram_mb:.2f}\n"
        f"# HELP bot_cpu CPU percent\n"
        f"bot_cpu {cpu}\n"
        f"# HELP bot_requests Total HTTP requests\n"
        f"bot_requests {stats['total_requests']}\n"
        f"# HELP bot_errors Total HTTP errors\n"
        f"bot_errors {stats['errors']}\n"
    )
    return Response(content=text, media_type="text/plain")


# ============================================================================
# ТОЧКА ВХОДА
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info", workers=1)
