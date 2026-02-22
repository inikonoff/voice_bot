# bot.py
"""
Главный файл бота: Версия 5.5 Enterprise Edition (Full Recovery & Render Fix)
Полностью восстановлена мультимодальность, исправлен двойной хендлер (FSM),
добавлена ротация ключей для стриминга, восстановлена надежность.
Интегрированы GroqClientManager и DialogueManager для Enterprise-уровня.
Добавлен мини-веб-сервер для совместимости с Render (Port Binding).
"""

import os
import sys
import signal
import logging
import asyncio
import time
from typing import Optional, Dict, Any
from datetime import datetime
from dotenv import load_dotenv
from aiohttp import web

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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage # Для продакшена использовать RedisStorage
from aiogram.exceptions import TelegramUnauthorizedError, TelegramNetworkError

import config
import processors

load_dotenv()

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEYS = os.environ.get("GROQ_API_KEYS", "")
PORT = int(os.environ.get("PORT", 10000))

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

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
# Для Enterprise-уровня рекомендуется использовать RedisStorage:
# from aiogram.fsm.storage.redis import RedisStorage
# storage = RedisStorage.from_url(config.REDIS_URL)
storage = MemoryStorage() # Используем MemoryStorage для демонстрации в рамках одного файла
dp = Dispatcher(storage=storage)

# Флаг для graceful shutdown
shutdown_event = asyncio.Event()


# ============================================================================
# FSM СОСТОЯНИЯ (для разделения обычного режима и QA)
# ============================================================================

class DialogStates(StatesGroup):
    """Состояния FSM для диалогового режима"""
    normal = State()  # Обычный режим (можно загружать новые документы)
    viewing_document = State()  # Просмотр документа (можно задавать вопросы)


# ============================================================================
# MIDDLEWARE ДЛЯ ОБРАБОТКИ ОШИБОК
# ============================================================================

class ErrorHandlingMiddleware(BaseMiddleware):
    """
    Middleware для обработки ошибок и автоматического восстановления
    """
    async def __call__(
        self,
        handler,
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramUnauthorizedError as e:
            logger.error(f"❌ Ошибка авторизации в middleware: {e}")
            raise
        except TelegramNetworkError as e:
            logger.error(f"❌ Сетевая ошибка в middleware: {e}")
            if hasattr(event, "message") and event.message:
                await event.message.answer("❌ Произошла сетевая ошибка. Пожалуйста, проверьте ваше интернет-соединение и попробуйте позже.")
            elif hasattr(event, "callback_query") and event.callback_query:
                await event.callback_query.message.answer("❌ Произошла сетевая ошибка. Пожалуйста, проверьте ваше интернет-соединение и попробуйте позже.")
            raise
        except Exception as e:
            logger.error(f"❌ Необработанная ошибка в middleware: {e}", exc_info=True)
            # Пробуем уведомить пользователя
            if hasattr(event, "message") and event.message:
                await event.message.answer("❌ Произошла внутренняя ошибка. Попробуйте позже.")
            elif hasattr(event, "callback_query") and event.callback_query:
                await event.callback_query.message.answer("❌ Произошла внутренняя ошибка. Попробуйте позже.")
            raise


# Регистрируем middleware
dp.message.middleware(ErrorHandlingMiddleware())
dp.callback_query.middleware(ErrorHandlingMiddleware())


# ============================================================================
# УЛУЧШЕННЫЙ STARTUP/SHUTDOWN
# ============================================================================

async def on_startup(bot: Bot):
    """Действия при запуске бота"""
    logger.info("=" * 50)
    logger.info("🚀 ЗАПУСК БОТА v5.5 Enterprise Edition")
    logger.info("=" * 50)
    
    # Проверка подключения к Telegram
    try:
        me = await bot.get_me()
        logger.info(f"✅ Бот @{me.username} (ID: {me.id}) успешно подключен")
    except Exception as e:
        logger.error(f"❌ Не удалось подключиться к Telegram: {e}")
        raise
    
    # Сброс вебхука
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Вебхук сброшен")
    except Exception as e:
        logger.error(f"❌ Ошибка при сбросе вебхука: {e}")
    
    # Инициализация Groq клиентов через менеджер
    try:
        await processors.groq_client_manager.initialize(GROQ_API_KEYS)
        logger.info(f"✅ Доступно Groq клиентов: {len(processors.groq_client_manager._clients)}")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Groq клиентов: {e}")

    # Создание временной директории
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    
    logger.info("=" * 50)
    logger.info("✅ БОТ ГОТОВ К РАБОТЕ")
    logger.info("=" * 50)


async def on_shutdown(bot: Bot):
    """Действия при остановке бота"""
    logger.info("=" * 50)
    logger.info("👋 ОСТАНОВКА БОТА v5.5 Enterprise Edition")
    logger.info("=" * 50)
    
    # Сохраняем статистику
    logger.info(f"📊 Активных диалогов: {len(processors.dialogue_manager.document_dialogues)}")
    
    # Закрываем сессии
    try:
        await bot.session.close()
        logger.info("✅ Сессия бота закрыта")
    except Exception as e:
        logger.error(f"❌ Ошибка при закрытии сессии: {e}")
    
    # Очищаем хранилища (для MemoryStorage)
    try:
        if isinstance(dp.storage, MemoryStorage):
            await dp.storage.close()
            await dp.storage.wait_closed()
            logger.info("✅ MemoryStorage очищен и закрыт")
        processors.dialogue_manager.document_dialogues.clear()
        logger.info("✅ Хранилища диалогов очищены")
    except Exception as e:
        logger.error(f"❌ Ошибка при очистке хранилищ: {e}")

    # Очистка временных файлов
    if config.CLEANUP_TEMP_FILES:
        try:
            await cleanup_temp_files_on_shutdown()
            logger.info("✅ Временные файлы очищены при завершении работы.")
        except Exception as e:
            logger.error(f"❌ Ошибка при очистке временных файлов при завершении работы: {e}")
    
    logger.info("=" * 50)
    logger.info("✅ БОТ ОСТАНОВЛЕН")
    logger.info("=" * 50)


# Регистрируем обработчики
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


# ============================================================================
# ОБРАБОТКА СИГНАЛОВ
# ============================================================================

def handle_sigterm(signum, frame):
    """Обработчик сигнала SIGTERM от Render"""
    logger.info("📡 Received SIGTERM signal, initiating graceful shutdown...")
    shutdown_event.set()


async def shutdown_gracefully():
    """Graceful shutdown"""
    logger.info("🛑 Starting graceful shutdown...")
    shutdown_event.set()
    
    # Даём время на завершение текущих обработок
    logger.info("⏳ Waiting for ongoing tasks to complete (up to 30 seconds)...")
    await asyncio.sleep(30) # Даем время на завершение текущих задач
    
    await on_shutdown(bot)
    logger.info("✅ Graceful shutdown complete")
    sys.exit(0)


# ============================================================================
# УПРАВЛЕНИЕ КЭШЕМ И ВРЕМЕННЫМИ ФАЙЛАМИ
# ============================================================================

async def cleanup_old_contexts_and_dialogues():
    """Фоновая задача: удаление контекстов и диалогов старше CACHE_TIMEOUT_SECONDS"""
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(config.CACHE_CHECK_INTERVAL)
            
            if shutdown_event.is_set():
                break
            
            # Очистка контекстов (если используется MemoryStorage)
            if isinstance(dp.storage, MemoryStorage):
                pass 

            # Очистка диалогов документов
            processors.dialogue_manager.cleanup_old_dialogues()
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cache/dialogue cleanup error: {e}")


async def cleanup_temp_files_periodic():
    """Фоновая задача: периодическое удаление старых временных файлов"""
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(config.TEMP_FILE_RETENTION)
            
            if shutdown_event.is_set():
                break
            
            if config.CLEANUP_TEMP_FILES and os.path.exists(config.TEMP_DIR):
                current_time = time.time()
                for filename in os.listdir(config.TEMP_DIR):
                    filepath = os.path.join(config.TEMP_DIR, filename)
                    if os.path.isfile(filepath):
                        file_age = current_time - os.path.getmtime(filepath)
                        if file_age > config.TEMP_FILE_RETENTION:
                            try:
                                os.remove(filepath)
                                logger.debug(f"Удален старый временный файл: {filepath}")
                            except Exception as e:
                                logger.error(f"Ошибка при удалении временного файла {filepath}: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Periodic temp file cleanup error: {e}")

async def cleanup_temp_files_on_shutdown():
    """Удаление всех временных файлов при завершении работы."""
    if os.path.exists(config.TEMP_DIR):
        for filename in os.listdir(config.TEMP_DIR):
            filepath = os.path.join(config.TEMP_DIR, filename)
            try:
                if os.path.isfile(filepath):
                    os.remove(filepath)
                    logger.debug(f"Удален временный файл при завершении работы: {filepath}")
                elif os.path.isdir(filepath):
                    if not os.listdir(filepath):
                        os.rmdir(filepath)
                        logger.debug(f"Удалена пустая временная директория: {filepath}")
            except Exception as e:
                logger.error(f"Ошибка при удалении временного файла/директории {filepath} при завершении работы: {e}")
        try:
            if not os.listdir(config.TEMP_DIR):
                os.rmdir(config.TEMP_DIR)
                logger.debug(f"Удалена корневая временная директория: {config.TEMP_DIR}")
        except Exception as e:
            logger.error(f"Ошибка при удалении корневой временной директории {config.TEMP_DIR}: {e}")


# ============================================================================
# ИНЛАЙН-КЛАВИАТУРЫ
# ============================================================================

def get_correction_keyboard(message_id: int, current_mode: str, available_modes: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    modes_map = {
        "basic": "📝 Как есть",
        "premium": "✨ Красиво",
        "summary": "📊 Саммари"
    }
    for mode_key in available_modes:
        text = modes_map.get(mode_key, mode_key)
        if mode_key == current_mode:
            text = f"✅ {text}"
        builder.button(text=text, callback_data=f"correct_{message_id}_{mode_key}")
    builder.button(text="⬇️ Скачать TXT", callback_data=f"export_txt_{message_id}")
    builder.button(text="⬇️ Скачать PDF", callback_data=f"export_pdf_{message_id}")
    builder.adjust(3, 2)
    return builder.as_markup()

def get_document_dialog_keyboard(message_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Задать вопрос по документу", callback_data=f"ask_doc_{message_id}")
    builder.button(text="Завершить диалог", callback_data=f"end_doc_dialog_{message_id}")
    builder.adjust(1)
    return builder.as_markup()


# ============================================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================================

@dp.message(Command("start"))
async def command_start_handler(message: types.Message, state: FSMContext) -> None:
    await message.answer(config.START_MESSAGE, parse_mode="HTML")
    await state.set_state(DialogStates.normal)

@dp.message(Command("help"))
async def command_help_handler(message: types.Message) -> None:
    await message.answer(config.HELP_MESSAGE, parse_mode="HTML")

@dp.message(Command("status"))
async def command_status_handler(message: types.Message) -> None:
    status_info = await processors.get_status_info(processors.groq_client_manager._clients)
    status_message = config.STATUS_MESSAGE.format(
        groq_count=status_info["groq_count"],
        users_count=status_info["users_count"],
        vision_status=status_info["vision_status"],
        docx_status=status_info["docx_status"],
        temp_files=status_info["temp_files"],
        vad_status=status_info["vad_status"],
        s3_status=status_info["s3_status"],
        redis_status=status_info["redis_status"],
    )
    await message.answer(status_message, parse_mode="HTML")


# ============================================================================
# ОБРАБОТЧИКИ СООБЩЕНИЙ (ТЕКСТ, ФОТО, ВИДЕО, АУДИО, ДОКУМЕНТЫ)
# ============================================================================

@dp.message(F.text, DialogStates.normal)
async def handle_text_message(message: types.Message, state: FSMContext) -> None:
    if message.text.startswith("http"): return
    await message.answer("Обрабатываю текст...", reply_markup=ReplyKeyboardRemove())
    processed_text, original_text, file_type = await processors.process_content(None, message.text, "text", processors.groq_client_manager._clients)
    
    if processed_text.startswith("❌"):
        await message.answer(processed_text)
        return

    sent_message = await message.answer(
        processed_text,
        reply_markup=get_correction_keyboard(message.message_id, "basic", ["basic", "premium", "summary"])
    )
    processors.dialogue_manager.add_document_context(
        message.from_user.id, sent_message.message_id, original_text
    )
    await state.update_data(last_processed_message_id=sent_message.message_id)

@dp.message(F.photo, DialogStates.normal)
async def handle_photo_message(message: types.Message, state: FSMContext) -> None:
    await message.answer("Загружаю и распознаю изображение...", reply_markup=ReplyKeyboardRemove())
    file_info = await bot.get_file(message.photo[-1].file_id)
    downloaded_file_path = os.path.join(config.TEMP_DIR, f"{file_info.file_unique_id}.jpg")
    await bot.download_file(file_info.file_path, downloaded_file_path)

    processed_text, original_text, file_type = await processors.process_content(downloaded_file_path, None, "photo", processors.groq_client_manager._clients)
    
    if processed_text.startswith("❌"):
        await message.answer(processed_text)
        return

    sent_message = await message.answer(
        processed_text,
        reply_markup=get_correction_keyboard(message.message_id, "basic", ["basic", "premium", "summary"])
    )
    processors.dialogue_manager.add_document_context(
        message.from_user.id, sent_message.message_id, original_text
    )
    await state.update_data(last_processed_message_id=sent_message.message_id)
    os.remove(downloaded_file_path)

@dp.message(F.voice | F.audio, DialogStates.normal)
async def handle_audio_message(message: types.Message, state: FSMContext) -> None:
    await message.answer(config.MSG_PROCESSING_VOICE, reply_markup=ReplyKeyboardRemove())
    audio = message.voice or message.audio
    if audio.file_size and audio.file_size > 20 * 1024 * 1024:
    await message.answer(config.ERROR_FILE_TOO_LARGE)
    return
    file_info = await bot.get_file(audio.file_id)
    downloaded_file_path = os.path.join(config.TEMP_DIR, f"{file_info.file_unique_id}.ogg")
    await bot.download_file(file_info.file_path, downloaded_file_path)

    processed_text, original_text, file_type = await processors.process_content(downloaded_file_path, None, "voice", processors.groq_client_manager._clients)
    
    if processed_text.startswith("❌"):
        await message.answer(processed_text)
        return

    sent_message = await message.answer(
        processed_text,
        reply_markup=get_correction_keyboard(message.message_id, "basic", ["basic", "premium", "summary"])
    )
    processors.dialogue_manager.add_document_context(
        message.from_user.id, sent_message.message_id, original_text
    )
    await state.update_data(last_processed_message_id=sent_message.message_id)
    os.remove(downloaded_file_path)

@dp.message(F.video | F.video_note, DialogStates.normal)
async def handle_video_message(message: types.Message, state: FSMContext) -> None:
    video = message.video or message.video_note
    MAX_TG_FILE_SIZE = 20 * 1024 * 1024  # 20 MB — лимит Telegram Bot API
    if video.file_size and video.file_size > MAX_TG_FILE_SIZE:
        await message.answer(config.ERROR_FILE_TOO_LARGE)
        return

    await message.answer(config.MSG_PROCESSING_VIDEO, reply_markup=ReplyKeyboardRemove())
    file_info = await bot.get_file(video.file_id)
    downloaded_file_path = os.path.join(config.TEMP_DIR, f"{file_info.file_unique_id}.mp4")
    await bot.download_file(file_info.file_path, downloaded_file_path)

    processed_text, original_text, file_type = await processors.process_content(downloaded_file_path, None, "video", processors.groq_client_manager._clients)
    
    if processed_text.startswith("❌"):
        await message.answer(processed_text)
        return

    sent_message = await message.answer(
        processed_text,
        reply_markup=get_correction_keyboard(message.message_id, "basic", ["basic", "premium", "summary"])
    )
    processors.dialogue_manager.add_document_context(
        message.from_user.id, sent_message.message_id, original_text
    )
    await state.update_data(last_processed_message_id=sent_message.message_id)
    os.remove(downloaded_file_path)

@dp.message(F.document, DialogStates.normal)
async def handle_document_message(message: types.Message, state: FSMContext) -> None:
    if message.document.file_size > config.FILE_SIZE_LIMIT:
        await message.answer(config.ERROR_FILE_TOO_LARGE)
        return

    await message.answer("Загружаю и обрабатываю документ...", reply_markup=ReplyKeyboardRemove())
    file_info = await bot.get_file(message.document.file_id)
    original_filename = message.document.file_name
    downloaded_file_path = os.path.join(config.TEMP_DIR, original_filename)
    await bot.download_file(file_info.file_path, downloaded_file_path)

    processed_text, original_text, file_type = await processors.process_content(downloaded_file_path, None, "document", processors.groq_client_manager._clients)
    
    if processed_text.startswith("❌"):
        await message.answer(processed_text)
        os.remove(downloaded_file_path)
        return

    sent_message = await message.answer(
        processed_text,
        reply_markup=get_correction_keyboard(message.message_id, "basic", ["basic", "premium", "summary"])
    )
    processors.dialogue_manager.add_document_context(
        message.from_user.id, sent_message.message_id, original_text
    )
    await state.update_data(last_processed_message_id=sent_message.message_id)
    os.remove(downloaded_file_path)

@dp.message(F.text.regexp(r"https?://[^\s]+"), DialogStates.normal)
async def handle_url_message(message: types.Message, state: FSMContext) -> None:
    await message.answer("Обрабатываю ссылку...", reply_markup=ReplyKeyboardRemove())
    processed_text, original_text, file_type = await processors.process_content(None, message.text, "url", processors.groq_client_manager._clients)
    
    if processed_text.startswith("❌"):
        await message.answer(processed_text)
        return

    sent_message = await message.answer(
        processed_text,
        reply_markup=get_correction_keyboard(message.message_id, "basic", ["basic", "premium", "summary"])
    )
    processors.dialogue_manager.add_document_context(
        message.from_user.id, sent_message.message_id, original_text
    )
    await state.update_data(last_processed_message_id=sent_message.message_id)


# ============================================================================
# ОБРАБОТЧИКИ CALLBACK QUERY (КНОПКИ)
# ============================================================================

@dp.callback_query(F.data.startswith("correct_"))
async def callback_correct_text(callback_query: types.CallbackQuery, state: FSMContext) -> None:
    _, original_message_id_str, mode = callback_query.data.split("_")
    original_message_id = int(original_message_id_str)
    user_id = callback_query.from_user.id

    context_data = processors.dialogue_manager.get_document_context(user_id, original_message_id)
    if not context_data:
        await callback_query.answer("Контекст сообщения не найден.")
        return

    original_text = context_data["text"]
    current_mode = context_data["mode"]
    available_modes = context_data["available_modes"]

    if mode == current_mode:
        await callback_query.answer(f"Текст уже в режиме \'{mode}\'.")
        return

    await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.message.answer(f"Применяю режим \'{mode}\'...")
    await callback_query.answer()

    corrected_text = await processors.apply_correction(original_text, mode)

    if corrected_text.startswith("❌"):
        await callback_query.message.answer(corrected_text)
        await callback_query.message.edit_reply_markup(
            reply_markup=get_correction_keyboard(original_message_id, current_mode, available_modes)
        )
        return

    sent_message = await callback_query.message.answer(
        corrected_text,
        reply_markup=get_correction_keyboard(original_message_id, mode, available_modes)
    )
    processors.dialogue_manager.add_document_context(
        user_id, sent_message.message_id, original_text
    )
    if user_id in processors.dialogue_manager.document_dialogues and original_message_id in processors.dialogue_manager.document_dialogues[user_id]:
        processors.dialogue_manager.document_dialogues[user_id][original_message_id]["mode"] = mode


@dp.callback_query(F.data.startswith("export_txt_"))
async def callback_export_txt(callback_query: types.CallbackQuery) -> None:
    _, original_message_id_str = callback_query.data.split("_")
    original_message_id = int(original_message_id_str)
    user_id = callback_query.from_user.id

    context_data = processors.dialogue_manager.get_document_context(user_id, original_message_id)
    if not context_data:
        await callback_query.answer("Контекст сообщения не найден.")
        return

    text_to_export = callback_query.message.text
    if not text_to_export:
        await callback_query.answer("Нечего экспортировать.")
        return

    file_path = os.path.join(config.TEMP_DIR, f"export_{original_message_id}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(text_to_export)
    
    await callback_query.message.answer_document(FSInputFile(file_path), caption="Ваш текст в формате TXT")
    await callback_query.answer()
    os.remove(file_path)

@dp.callback_query(F.data.startswith("export_pdf_"))
async def callback_export_pdf(callback_query: types.CallbackQuery) -> None:
    await callback_query.answer("Функция экспорта в PDF временно недоступна. Используйте TXT.")


@dp.callback_query(F.data.startswith("ask_doc_"))
async def callback_ask_document(callback_query: types.CallbackQuery, state: FSMContext) -> None:
    _, original_message_id_str = callback_query.data.split("_")
    original_message_id = int(original_message_id_str)
    user_id = callback_query.from_user.id

    context_data = processors.dialogue_manager.get_document_context(user_id, original_message_id)
    if not context_data:
        await callback_query.answer("Контекст документа не найден.")
        return

    await state.set_state(DialogStates.viewing_document)
    await state.update_data(current_document_message_id=original_message_id)
    await callback_query.message.answer(
        "Задайте ваш вопрос по документу. Чтобы завершить диалог, нажмите \'Завершить диалог\'.",
        reply_markup=get_document_dialog_keyboard(original_message_id)
    )
    await callback_query.answer()

@dp.message(F.text, DialogStates.viewing_document)
async def handle_document_question(message: types.Message, state: FSMContext) -> None:
    user_data = await state.get_data()
    original_message_id = user_data.get("current_document_message_id")

    if not original_message_id:
        await message.answer("Ошибка: не удалось определить документ для диалога. Начните заново.")
        await state.set_state(DialogStates.normal)
        return

    await message.answer("Ищу ответ на ваш вопрос...", reply_markup=ReplyKeyboardRemove())
    bot_response = await processors.dialogue_manager.answer_document_question(
        message.from_user.id, original_message_id, message.text
    )
    await message.answer(bot_response, reply_markup=get_document_dialog_keyboard(original_message_id))

@dp.callback_query(F.data.startswith("end_doc_dialog_"), DialogStates.viewing_document)
async def callback_end_document_dialog(callback_query: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DialogStates.normal)
    await state.update_data(current_document_message_id=None)
    await callback_query.message.answer("Диалог по документу завершен. Вы можете загрузить новый файл или текст.", reply_markup=ReplyKeyboardRemove())
    await callback_query.answer()


# ============================================================================
# WEB SERVER ДЛЯ RENDER (HEALTH CHECK)
# ============================================================================

async def health_check(request):
    """Отвечает на GET и HEAD запросы для проверки состояния сервиса."""
    return web.Response(text="Bot is alive!")

async def start_web_server():
    """Запускает веб-сервер и ждет сигнала о завершении."""
    app = web.Application()
    app.router.add_route("*", "/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    try:
        await site.start()
        logger.info(f"🚀 Веб-сервер запущен на порту {PORT} и готов к проверкам.")
        # Запускаем бота в фоновом режиме ПОСЛЕ старта сервера
        asyncio.create_task(dp.start_polling(bot))
        # Ждем сигнала о завершении
        await shutdown_event.wait()
    finally:
        await runner.cleanup()
        logger.info("🛑 Веб-сервер остановлен.")


# ============================================================================
# ЗАПУСК БОТА
# ============================================================================

async def main() -> None:
    # Регистрация обработчика сигнала SIGTERM
    signal.signal(signal.SIGTERM, handle_sigterm)

    # Запуск фоновых задач
    asyncio.create_task(cleanup_old_contexts_and_dialogues())
    asyncio.create_task(cleanup_temp_files_periodic())

    # Запуск веб-сервера (он сам запустит бота внутри)
    await start_web_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
